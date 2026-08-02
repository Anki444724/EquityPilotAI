"""Automatic discovery of investor-relations URLs.

The audit found `company_crawl_state.ir_url` NULL for all 501 rows, so the
brief's Priority-1 source contributed nothing and every discovered filing came
from NSE. This service fills that column without a human typing 500 URLs.

Why it probes rather than reads an API
--------------------------------------
There is no authoritative source for an Indian company's IR page, and this was
established by testing rather than assumed:

* NSE's `quote-equity` endpoint returns HTTP 403 to this environment.
* BSE's `ListofScripData` returns 4,928 rows and **no website field at all** —
  the only URL it carries (`NSURL`) is a link back to BSE's own price page.
* BSE's `CompanyProfile` and `ComplianceHeader` endpoints do not return JSON.

So the domain is derived from the company name and a small set of conventional
IR paths is probed. That is a heuristic, and it is treated as one: every
discovered URL is stored with a confidence and the method that found it, so a
low-confidence guess is visibly a guess.

Why HTTP 403 counts as a hit
----------------------------
Measured on real IR pages: `cipla.com/investors` answers 200, while
`tcs.com/investor-relations` and `infosys.com/investors` answer **403** — they
exist and are simply refusing a non-browser client. Treating 403 as failure
would discard the pages of two of India's largest companies. A 404 is a real
miss; a 403 is a page that exists behind a bot filter, recorded at lower
confidence.
"""

from __future__ import annotations

import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

import structlog
from sqlalchemy import select

from app.models.company import Company
from app.models.filing_collection import CompanyCrawlState

log = structlog.get_logger(__name__)

_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")

#: Conventional IR paths, most common first. Ordered so the cheapest likely
#: hit is tried before the long tail; probing stops at the first success.
IR_PATHS: tuple[str, ...] = (
    "/investors",
    "/investor-relations",
    "/investors.html",
    "/en/investors",
    "/investor",
    "/investors/financial-results",
    "/company/investors",
)

#: Suffixes stripped from a company name before deriving a domain.
_NAME_NOISE = re.compile(
    r"\b(ltd|limited|india|indian|corporation|corp|company|co|the|and|"
    r"industries|enterprises|holdings|group|plc|inc)\b",
    re.IGNORECASE,
)

#: Status codes that mean "this page exists".
#:
#: 403 is included deliberately — see the module docstring. Confidence is
#: lowered rather than the hit discarded.
_EXISTS = frozenset({200, 301, 302, 403, 405})

CONFIDENCE_VERIFIED = 0.90     # 200: fetched and confirmed
CONFIDENCE_BLOCKED = 0.60      # 403/405: exists, refused a bot
CONFIDENCE_SEEDED = 0.95       # from the curated map — checked by hand

#: Companies whose IR domain cannot be derived from the name. Kept short and
#: only for cases confirmed by probe; this is a correction list, not a
#: substitute for discovery.
SEED_DOMAINS: dict[str, str] = {
    "TCS": "https://www.tcs.com/investor-relations",
    "INFY": "https://www.infosys.com/investors",
    "CIPLA": "https://www.cipla.com/investors",
    "RELIANCE": "https://www.ril.com/investors",
    "HDFCBANK": "https://www.hdfcbank.com/personal/about-us/investor-relations",
    "ICICIBANK": "https://www.icicibank.com/about-us/investor-relations",
    "LT": "https://investors.larsentoubro.com",
    "ASIANPAINT": "https://www.asianpaints.com/investors.html",
}


@dataclass(slots=True)
class DiscoveryOutcome:
    ticker: str
    url: str | None = None
    confidence: float = 0.0
    method: str | None = None
    attempts: int = 0
    error: str | None = None

    @property
    def found(self) -> bool:
        return bool(self.url)


@dataclass(slots=True)
class DiscoveryReport:
    outcomes: list[DiscoveryOutcome] = field(default_factory=list)

    @property
    def found(self) -> list[DiscoveryOutcome]:
        return [o for o in self.outcomes if o.found]

    def as_dict(self) -> dict[str, Any]:
        return {
            "attempted": len(self.outcomes),
            "found": len(self.found),
            "by_method": {
                method: sum(1 for o in self.found if o.method == method)
                for method in {o.method for o in self.found if o.method}
            },
            "results": [
                {"ticker": o.ticker, "url": o.url, "confidence": o.confidence,
                 "method": o.method, "error": o.error}
                for o in self.outcomes
            ],
        }


def candidate_domains(company: Company) -> list[str]:
    """Plausible domains for a company, best guess first.

    Derived from the registered name with corporate suffixes stripped:
    "Cipla Ltd." -> cipla, so https://www.cipla.com. Crude, and deliberately
    so — a wrong guess costs one HTTP probe and is discarded, while the
    alternative is 500 hand-entered URLs that rot.
    """
    if company.website:
        parsed = urllib.parse.urlparse(company.website)
        if parsed.netloc:
            return [f"{parsed.scheme or 'https'}://{parsed.netloc}"]

    name = _NAME_NOISE.sub(" ", company.name or "")
    slug = re.sub(r"[^a-z0-9]+", "", name.lower())
    if not slug or len(slug) < 3:
        slug = re.sub(r"[^a-z0-9]+", "", (company.ticker or "").lower())
    if not slug:
        return []

    return [
        f"https://www.{slug}.com",
        f"https://www.{slug}.co.in",
        f"https://{slug}.com",
    ]


class IRDiscoveryService:
    """Finds and stores an investor-relations URL for each company."""

    def __init__(
        self,
        db: Any,
        *,
        timeout: float = 8.0,
        polite_delay: float = 0.3,
        probe: Any = None,
    ) -> None:
        self.db = db
        self.timeout = timeout
        self.polite_delay = polite_delay
        #: Injectable so tests never touch the network.
        self._probe = probe or self._http_probe

    def _http_probe(self, url: str) -> int | None:
        """Return the status code, or None when the host does not resolve."""
        request = urllib.request.Request(
            url, headers={"User-Agent": _UA, "Accept": "text/html"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.status
        except urllib.error.HTTPError as exc:
            return exc.code
        except Exception:  # noqa: BLE001 — DNS failure, TLS error, timeout
            return None

    def discover_for(self, company: Company) -> DiscoveryOutcome:
        outcome = DiscoveryOutcome(ticker=company.ticker)

        seeded = SEED_DOMAINS.get((company.ticker or "").upper())
        if seeded:
            outcome.url = seeded
            outcome.confidence = CONFIDENCE_SEEDED
            outcome.method = "seed"
            return outcome

        for domain in candidate_domains(company):
            for path in IR_PATHS:
                url = f"{domain}{path}"
                outcome.attempts += 1
                status = self._probe(url)
                if status is None:
                    # Host does not resolve; every other path on this domain
                    # will fail identically, so abandon the domain rather than
                    # spending seven probes proving it.
                    break
                if status in _EXISTS:
                    outcome.url = url
                    outcome.confidence = (
                        CONFIDENCE_VERIFIED if status in (200, 301, 302)
                        else CONFIDENCE_BLOCKED
                    )
                    outcome.method = f"probe:{status}"
                    return outcome
                if self.polite_delay:
                    time.sleep(self.polite_delay)

        outcome.error = f"no IR page found in {outcome.attempts} probes"
        return outcome

    def run(self, *, limit: int = 25, overwrite: bool = False) -> DiscoveryReport:
        """Discover URLs for companies that have none, and store them."""
        stmt = (
            select(Company, CompanyCrawlState)
            .join(CompanyCrawlState,
                  CompanyCrawlState.company_id == Company.id)
            .where(
                Company.listing_status == "active",
                Company.exchange.in_(("NSE", "BSE", "NSE/BSE")),
            )
        )
        if not overwrite:
            stmt = stmt.where(CompanyCrawlState.ir_url.is_(None))
        stmt = stmt.order_by(Company.ticker).limit(limit)

        report = DiscoveryReport()
        for company, state in self.db.execute(stmt).all():
            try:
                outcome = self.discover_for(company)
            except Exception as exc:  # noqa: BLE001 — one company must not stop the run
                outcome = DiscoveryOutcome(
                    ticker=company.ticker,
                    error=f"{type(exc).__name__}: {exc}"[:200],
                )
            report.outcomes.append(outcome)

            if outcome.found:
                state.ir_url = outcome.url
                state.ir_url_confidence = outcome.confidence
                state.ir_url_method = outcome.method
            state.ir_url_checked_at = _utcnow()

        self.db.commit()
        log.info("ir discovery complete", attempted=len(report.outcomes),
                 found=len(report.found))
        return report


def _utcnow():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)
