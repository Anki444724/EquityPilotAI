"""Priority 1 — the company's own investor-relations website.

There is no standard for an Indian IR page. Every company hand-builds one,
they change without notice, several sit behind JavaScript that a plain HTTP
client cannot execute, and a few block non-browser agents outright. A crawler
against them is therefore *best effort by construction*, and this module is
written to say so rather than to pretend otherwise.

The design consequence: **NSE is the spine, IR is the supplement.** NSE
publishes a machine-readable announcements API that has been verified to serve
real PDFs, so the guaranteed floor of the system does not depend on any
company's web team. When an IR page yields documents, they are richer —
investor presentations and transcripts often appear there first — and when it
yields nothing the platform still has the exchange record.

What this does:

* fetch the registered IR URL, follow one level of likely sub-pages
  ("investors", "financials", "annual report"), and collect PDF links;
* resolve relative links, deduplicate, and cap the number followed;
* return `Filing` objects the rest of the layer already understands.

What it deliberately does not do: execute JavaScript, log in, solve captchas,
or crawl beyond one hop. Each of those is a large increase in fragility for a
source that is already supplementary.
"""
from __future__ import annotations

import re
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Any

import structlog

from app.data.filings.base import (
    Filing, FilingProvider, FilingResult, FilingType, SourceCategory,
)
from app.domain.filings.collection import classify, is_noise

log = structlog.get_logger(__name__)

_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")

#: Sub-pages worth one extra hop from the landing page.
_FOLLOW_HINTS = (
    "investor", "financial", "annual-report", "annual_report", "annualreport",
    "quarterly", "results", "presentation", "disclosure", "shareholder",
)

#: Anchor text or href fragments that never lead to a filing.
_SKIP_HINTS = (
    "javascript:", "mailto:", "tel:", "#", "login", "career", "privacy",
    "cookie", "sitemap", "unsubscribe",
)

MAX_SUBPAGES = 4
MAX_LINKS_PER_PAGE = 400


class _LinkParser(HTMLParser):
    """Collect (href, anchor text) pairs.

    A hand-rolled parser rather than BeautifulSoup because the platform has no
    HTML dependency and adding one for a best-effort source is poor value.
    `HTMLParser` is in the standard library and copes with the malformed
    markup these pages routinely contain.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        for name, value in attrs:
            if name == "href" and value:
                self._href = value.strip()
                self._text = []
                return

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href is not None:
            if len(self.links) < MAX_LINKS_PER_PAGE:
                text = " ".join("".join(self._text).split())
                self.links.append((self._href, text))
            self._href = None
            self._text = []


class InvestorRelationsProvider(FilingProvider):
    """Documents published on a company's own IR pages."""

    name = "Investor Relations"
    category = SourceCategory.ANNUAL_REPORT
    markets = frozenset({"India"})

    def __init__(self, *, timeout: float = 20.0,
                 polite_delay: float = 1.0) -> None:
        self.timeout = timeout
        # Crawling someone's website is a favour they extend, not a right.
        # A delay between requests keeps the platform a good citizen and
        # reduces the chance of being blocked outright.
        self.polite_delay = polite_delay

    def available(self) -> bool:
        return True

    # ------------------------------------------------------------ transport
    def _get(self, url: str) -> tuple[str, str] | None:
        """Fetch a page. Returns (final_url, body) or None."""
        request = urllib.request.Request(url, headers={
            "User-Agent": _UA,
            "Accept": "text/html,application/xhtml+xml",
            # Never advertise gzip: urllib does not transparently decompress,
            # and the body comes back as bytes that fail to decode. This cost
            # a session's debugging on SEC EDGAR earlier in the engagement.
            "Accept-Encoding": "identity",
        })
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                content_type = (response.headers.get("Content-Type") or "").lower()
                if "html" not in content_type:
                    return None
                body = response.read(3_000_000)
                charset = response.headers.get_content_charset() or "utf-8"
                return response.geturl(), body.decode(charset, errors="replace")
        except (urllib.error.HTTPError, urllib.error.URLError, OSError) as exc:
            log.debug("ir fetch failed", url=url[:120], error=str(exc)[:120])
            return None
        except Exception as exc:  # noqa: BLE001
            log.debug("ir fetch error", url=url[:120], error=str(exc)[:120])
            return None

    @staticmethod
    def _links(base_url: str, html: str) -> list[tuple[str, str]]:
        parser = _LinkParser()
        try:
            parser.feed(html)
        except Exception:  # noqa: BLE001 — malformed markup must not raise
            pass
        out: list[tuple[str, str]] = []
        for href, text in parser.links:
            low = href.lower()
            if any(hint in low for hint in _SKIP_HINTS):
                continue
            out.append((urllib.parse.urljoin(base_url, href), text))
        return out

    # ---------------------------------------------------------------- fetch
    def fetch(
        self,
        ticker: str,
        *,
        filing_types: list[FilingType] | None = None,
        limit: int = 25,
        ir_url: str | None = None,
        **kwargs: Any,
    ) -> FilingResult:
        started = time.perf_counter()

        if not ir_url:
            # Not an error: most companies simply have no registered IR page.
            # Reported as such so the dashboard distinguishes "no URL" from
            # "URL failed".
            return FilingResult(
                filings=[], source=self.name, category=self.category,
                error="no investor-relations URL registered",
                latency_ms=(time.perf_counter() - started) * 1000,
            )

        pages: list[tuple[str, str]] = []
        landing = self._get(ir_url)
        if landing is None:
            return FilingResult(
                filings=[], source=self.name, category=self.category,
                error="investor-relations page unreachable",
                latency_ms=(time.perf_counter() - started) * 1000,
            )
        pages.append(landing)

        # One hop into the sub-pages most likely to hold documents.
        followed = 0
        for url, text in self._links(landing[0], landing[1]):
            if followed >= MAX_SUBPAGES:
                break
            haystack = f"{url} {text}".lower()
            if url.lower().endswith(".pdf"):
                continue
            if not any(hint in haystack for hint in _FOLLOW_HINTS):
                continue
            if url == landing[0]:
                continue
            time.sleep(self.polite_delay)
            page = self._get(url)
            if page is not None:
                pages.append(page)
                followed += 1

        # Collect PDF links across everything fetched.
        seen: set[str] = set()
        filings: list[Filing] = []
        for page_url, html in pages:
            for url, text in self._links(page_url, html):
                if not url.lower().split("?")[0].endswith(".pdf"):
                    continue
                if url in seen:
                    continue
                seen.add(url)

                title = text or _title_from_url(url)
                if is_noise(title):
                    continue
                classification = classify(title, url=url)
                filings.append(Filing(
                    category=self.category,
                    filing_type=classification.filing_type,
                    title=title[:480],
                    reference=url,
                    url=url,
                    summary=None,
                    extra={
                        "doc_type": classification.doc_type.value,
                        "classification_confidence": classification.confidence,
                        "discovered_via": "investor_relations",
                    },
                ))
                if len(filings) >= limit:
                    break
            if len(filings) >= limit:
                break

        elapsed = (time.perf_counter() - started) * 1000
        log.info("ir crawl complete", ticker=ticker, pages=len(pages),
                 filings=len(filings), ms=round(elapsed, 1))
        return FilingResult(
            filings=filings, source=self.name, category=self.category,
            latency_ms=elapsed,
        )


def _title_from_url(url: str) -> str:
    """A readable title when the anchor text was empty (an image link)."""
    tail = urllib.parse.urlparse(url).path.rsplit("/", 1)[-1]
    tail = re.sub(r"\.pdf$", "", tail, flags=re.IGNORECASE)
    return re.sub(r"[_\-]+", " ", tail).strip() or "Untitled document"
