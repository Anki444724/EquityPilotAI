"""Google Custom Search discovery for general-topic questions.

This is not a second company-page searcher. The company-pinned service stays
the security boundary for company-owned pages. Here the only external
discovery call is the Custom Search JSON API. A snippet is counted and
discarded. Evidence is a page this platform fetched through the existing
fetcher, robots check, extractor and quality gate.

Nothing in this module plans a question, opens a client at import time, or
logs the request URL. The API key stays in the request parameters and out of
exceptions.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from app.data.filings.base import recency_factor
from app.domain.web.types import WebRejectionReason
from app.services.web.extract import canonicalize_url, extract_page
from app.services.web.index import WebEvidenceCandidate, blend_score, freshness_basis
from app.services.web.quality import assess, classify, web_authority
from app.services.web.safety import (
    UrlSafetyError,
    UrlSafetyPolicy,
)

CSE_ENDPOINT = "https://www.googleapis.com/customsearch/v1"
MAX_RESULTS = 5
MAX_QUERY_CHARS = 256
#: Hard ceiling on a Custom Search body. A larger body is refused and the
#: remainder is not read. The key never enters a log line or an exception.
MAX_CSE_BODY_BYTES = 1_048_576
ORIGIN = "google_live_fetch"
_SNIPPET_CHARS = 400

_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9]{1,}|[\u0900-\u097F]{2,}")
_RECENCY = re.compile(
    r"\b(?:latest|recent|recently|newest|today|todays|aaj|current)\b"
    r"|आज|ताज़ा|ताजा|नवीनतम|हालिया",
    re.IGNORECASE,
)
_FILLERS = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be", "to", "of", "in", "on",
    "for", "and", "or", "what", "whats", "how", "does", "do", "did", "me",
    "mein", "mai", "ka", "ki", "ke", "ko", "se", "par", "pe", "aur", "ya",
    "hai", "hain", "kya", "kaise", "karta", "karti", "karte", "kaam", "work",
    "works", "working", "kyu", "kyun", "kyon", "why", "aaj", "today", "latest",
    "recent", "recently", "about", "tell", "please", "hua", "hui", "hoga",
    "my", "this", "that", "with", "from", "into", "its", "it", "current",
    "क्या", "है", "हैं", "कैसे", "में", "का", "की", "के", "को", "से", "और",
    "या", "काम", "करता", "करती", "करते", "आज", "क्यों",
})
_BLOCKED_HOSTS = frozenset({"localhost", "metadata.google.internal"})
_BLOCKED_SUFFIXES = (".local", ".internal", ".localhost")


class GoogleSearchError(Exception):
    """A sanitized search failure. The message is fixed text, never a body."""


class GoogleSearchDisabled(GoogleSearchError):
    """The feature flag is off. No network was attempted."""


class GoogleSearchRefused(GoogleSearchError):
    """The request was refused before a result could be used."""


class PublicUrlSafetyPolicy(UrlSafetyPolicy):
    """Fetch any public http(s) host. Still refuse private and internal ones.

    A dotted public name is treated as pinned so the existing address, port
    and scheme checks still run. A name without a dot, a local suffix, or
    URL credentials never reaches ``super().check``.
    """

    def __init__(self, *, resolver=None, allowed_ports=None) -> None:
        super().__init__(
            allowed_hosts=(),
            resolver=resolver,
            allowed_ports=allowed_ports,
        )

    def host_is_pinned(self, host: str) -> bool:
        candidate = (host or "").strip().lower().rstrip(".")
        if "." not in candidate:
            return False
        if candidate in _BLOCKED_HOSTS:
            return False
        if any(candidate.endswith(suffix) for suffix in _BLOCKED_SUFFIXES):
            return False
        return True

    def check(self, url: str, *, resolve: bool = True):
        parts = urlsplit((url or "").strip())
        if parts.username or parts.password:
            raise UrlSafetyError(
                WebRejectionReason.MALFORMED_URL,
                "URL credentials are not allowed",
            )
        return super().check(url, resolve=resolve)


@dataclass(frozen=True, slots=True)
class GoogleSearchResult:
    """Candidates built from fetched pages. Snippets are a count, not text."""

    query: str
    topic_terms: tuple[str, ...]
    recency_sensitive: bool
    candidates: tuple[WebEvidenceCandidate, ...]
    snippets_discarded: int
    refusals: tuple[str, ...] = ()

    @property
    def queries(self) -> tuple[str, ...]:
        return (self.query,) if self.query else ()


def topic_terms(question: str) -> tuple[str, ...]:
    """Significant tokens for relevance. Digit-only tokens are not topics."""
    found: list[str] = []
    seen: set[str] = set()
    for token in _TOKEN.findall(question or ""):
        key = token.casefold()
        if key in _FILLERS or key in seen or token.isdigit():
            continue
        seen.add(key)
        found.append(token)
        if len(found) >= 6:
            break
    return tuple(found)


def question_is_recency_sensitive(question: str) -> bool:
    return bool(_RECENCY.search(question or ""))


def _bounded_body(chunks, *, limit: int = MAX_CSE_BODY_BYTES) -> bytes:
    """Read a response until ``limit``, then refuse without reading further."""
    total = 0
    parts: list[bytes] = []
    for chunk in chunks:
        if not chunk:
            continue
        piece = bytes(chunk)
        total += len(piece)
        if total > limit:
            raise GoogleSearchRefused("the search service returned an unreadable response")
        parts.append(piece)
    return b"".join(parts)


class _HttpxSearchTransport:
    """One Custom Search GET. The client is built here, not at import.

    The body is streamed and capped. The client does not follow redirects,
    does not install event hooks, and does not enable wire or debug logging.
    The API key stays in the request parameters and is never logged.
    """

    def get(self, url: str, *, params: Mapping[str, Any], headers: Mapping[str, str], timeout: float):
        import httpx

        failed = False
        response = None
        try:
            # No event hooks and no debug logging: either would record the
            # request URL, and the API key is a query parameter.
            with httpx.Client(follow_redirects=False, timeout=timeout) as client:
                with client.stream(
                    "GET", url, params=dict(params), headers=dict(headers),
                ) as streamed:
                    body = _bounded_body(streamed.iter_bytes())
                    response = _SearchBody(streamed.status_code, body)
        except GoogleSearchError:
            raise
        except Exception:
            failed = True
        if failed or response is None:
            raise GoogleSearchRefused("the search service refused the request")
        return response


class _SearchBody:
    """The capped Custom Search body. Tests and the parser read ``content``."""

    def __init__(self, status_code: int, content: bytes) -> None:
        self.status_code = status_code
        self.content = content


class GoogleWebDiscovery:
    """Bounded Custom Search plus a fetch of the result pages.

    Construction and ``from_settings`` do not resolve a host or open a
    client. A disabled flag returns before credentials are even read into a
    request.
    """

    def __init__(
        self,
        *,
        enabled: bool | None = None,
        api_key: str | None = None,
        engine_id: str | None = None,
        api_transport: Any = None,
        page_transport: Any = None,
        safety: PublicUrlSafetyPolicy | None = None,
        robots: Any = None,
        politeness: Any = None,
        resolver: Any = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._enabled_override = enabled
        self._api_key = api_key
        self._engine_id = engine_id
        self._api_transport = api_transport
        self._page_transport = page_transport
        self._safety = safety
        self._robots = robots
        self._politeness = politeness
        self._resolver = resolver
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    @classmethod
    def from_settings(cls) -> "GoogleWebDiscovery":
        return cls()

    def __repr__(self) -> str:
        return "GoogleWebDiscovery(enabled={})".format("yes" if self.enabled else "no")

    @property
    def enabled(self) -> bool:
        if self._enabled_override is not None:
            return bool(self._enabled_override)
        from app.core.config import settings
        return bool(settings.GOOGLE_SEARCH_ENABLED)

    def research(self, question: str) -> GoogleSearchResult:
        if not self.enabled:
            raise GoogleSearchDisabled("live web search is not enabled")
        key, cx = self._credentials()
        if not key or not cx:
            raise GoogleSearchRefused("live web search is not configured")
        query = " ".join((question or "").split())
        if not query:
            raise GoogleSearchRefused("the search query is empty")
        if len(query) > MAX_QUERY_CHARS:
            raise GoogleSearchRefused("the search query exceeds the length limit")
        payload = self._fetch_payload(query, key, cx)
        raw_items = payload.get("items", [])
        if raw_items is None:
            raw_items = []
        if not isinstance(raw_items, list):
            raise GoogleSearchRefused("the search service returned an unreadable response")
        return self._collect(
            query,
            raw_items[:MAX_RESULTS],
            topic_terms(question),
            question_is_recency_sensitive(question),
        )

    def _credentials(self) -> tuple[str, str]:
        if self._api_key is None and self._engine_id is None:
            from app.core.config import settings
            return (
                (settings.GOOGLE_SEARCH_API_KEY or "").strip(),
                (settings.GOOGLE_SEARCH_ENGINE_ID or "").strip(),
            )
        return ((self._api_key or "").strip(), (self._engine_id or "").strip())

    def _fetch_payload(self, query: str, key: str, cx: str) -> dict:
        transport = self._api_transport or _HttpxSearchTransport()
        failed = False
        response = None
        try:
            response = transport.get(
                CSE_ENDPOINT,
                params={"key": key, "cx": cx, "q": query, "num": MAX_RESULTS},
                headers={"Accept": "application/json"},
                timeout=10.0,
            )
        except GoogleSearchError:
            raise
        except Exception:
            failed = True
        if failed or response is None:
            raise GoogleSearchRefused("the search service refused the request")
        if _status_code(response) != 200:
            raise GoogleSearchRefused("the search service refused the request")
        parsed = None
        try:
            parsed = json.loads(_body_bytes(response).decode("utf-8"))
        except GoogleSearchError:
            raise
        except Exception:
            parsed = None
        if not isinstance(parsed, dict):
            raise GoogleSearchRefused("the search service returned an unreadable response")
        return parsed

    def _collect(
        self,
        query: str,
        items: list,
        terms: tuple[str, ...],
        recency: bool,
    ) -> GoogleSearchResult:
        safety = self._safety or PublicUrlSafetyPolicy(resolver=self._resolver)
        seen: set[str] = set()
        candidates: list[WebEvidenceCandidate] = []
        refusals: list[str] = []
        discarded = 0
        fetcher = None
        for item in items:
            if not isinstance(item, dict):
                continue
            snippet = item.get("snippet")
            if isinstance(snippet, str) and snippet.strip():
                discarded += 1
            link = item.get("link")
            if not isinstance(link, str) or not link.strip():
                continue
            canonical = _canonical(link)
            if canonical in seen:
                continue
            try:
                safety.check(link)
            except UrlSafetyError as exc:
                # Dedup state is committed only after the URL is safe. An
                # unsafe userinfo URL shares a canonical form with the same
                # page without credentials; recording it first would suppress
                # the later safe result.
                refusals.append(exc.reason.value)
                continue
            seen.add(canonical)
            if fetcher is None:
                fetcher = self._build_fetcher(safety)
            try:
                fetched = fetcher.fetch(link)
            except Exception as exc:
                reason = getattr(getattr(exc, "reason", None), "value", None)
                refusals.append(reason or "fetch_refused")
                continue
            final = _canonical(fetched.final_url or link)
            if final in seen and final != canonical:
                continue
            seen.add(final)
            built = _candidate_from_page(
                fetched, query, terms, item.get("title"), self._clock(),
            )
            if built is None:
                refusals.append("quality_rejected")
                continue
            candidates.append(built)
        return GoogleSearchResult(
            query=query,
            topic_terms=terms,
            recency_sensitive=recency,
            candidates=tuple(candidates),
            snippets_discarded=discarded,
            refusals=tuple(refusals),
        )

    def _build_fetcher(self, safety: PublicUrlSafetyPolicy):
        from app.services.web.fetcher import HostPoliteness, WebFetcher
        from app.services.web.robots import RobotsPolicy

        robots = self._robots if self._robots is not None else RobotsPolicy(
            resolver=self._resolver,
        )
        politeness = self._politeness if self._politeness is not None else HostPoliteness()
        return WebFetcher(
            policy=_page_fetch_policy(),
            safety=safety,
            robots=robots,
            transport=self._page_transport,
            politeness=politeness,
            now=self._clock,
        )


def _page_fetch_policy():
    """The company-chat interactive budget, not the 20s/2-attempt default."""
    from app.services.ai.internal_web_research import INTERACTIVE_FETCH_POLICY

    return INTERACTIVE_FETCH_POLICY


def _status_code(response: Any) -> int:
    try:
        return int(getattr(response, "status_code", 0))
    except (TypeError, ValueError):
        return 0


def _body_bytes(response: Any) -> bytes:
    raw = getattr(response, "content", None)
    if raw is None:
        raw = getattr(response, "text", b"")
    if isinstance(raw, str):
        data = raw.encode("utf-8")
    else:
        data = bytes(raw or b"")
    if len(data) > MAX_CSE_BODY_BYTES:
        raise GoogleSearchRefused("the search service returned an unreadable response")
    return data


def _canonical(url: str) -> str:
    try:
        return canonicalize_url(url) or url.strip()
    except Exception:
        return (url or "").strip()


def _mentions(term: str, text: str) -> bool:
    if not term or not text:
        return False
    if term.isascii():
        return re.search(rf"\b{re.escape(term)}\b", text, re.IGNORECASE) is not None
    return term.casefold() in text.casefold()


def _snippet(text: str) -> str:
    cleaned = " ".join((text or "").split())
    if len(cleaned) <= _SNIPPET_CHARS:
        return cleaned
    cut = cleaned[:_SNIPPET_CHARS]
    space = cut.rfind(" ")
    if space > 200:
        cut = cut[:space]
    return cut.rstrip() + "…"


def _label(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:200]


def _candidate_from_page(fetched, query: str, terms: tuple[str, ...], item_title: Any, now: datetime):
    try:
        extracted = extract_page(
            fetched.content,
            url=fetched.final_url or fetched.url,
            content_class=fetched.content_class,
            charset=fetched.charset,
        )
    except Exception:
        return None
    assessment = assess(extracted.text, content_class=fetched.content_class)
    if not assessment.accepted:
        return None
    title = extracted.title or _label(item_title)
    if terms and not any(_mentions(term, f"{title} {extracted.text}") for term in terms):
        return None
    published = extracted.published_at
    retrieved = fetched.retrieved_at or now
    if retrieved.tzinfo is None:
        retrieved = retrieved.replace(tzinfo=timezone.utc)
    source_class = classify(fetched.host)
    authority = web_authority(source_class, published_at=published)
    basis_date, basis = freshness_basis(published, retrieved)
    freshness = recency_factor(basis_date, today=retrieved.date())
    relevance = 1.0
    final_url = fetched.final_url or fetched.url
    return WebEvidenceCandidate(
        document_id=None,
        chunk_id=None,
        company_id=None,
        source_url=final_url,
        canonical_url=_canonical(final_url),
        title=title or None,
        source_class=source_class.value,
        published_at=published,
        retrieved_at=retrieved,
        snippet=_snippet(extracted.text),
        relevance=relevance,
        authority=round(authority, 6),
        freshness=round(freshness, 6),
        freshness_basis=basis,
        score=round(blend_score(relevance, authority, freshness), 6),
        matched_queries=(query,),
        signals=("live_fetch",),
        content_hash=fetched.sha256 or hashlib.sha256(fetched.content).hexdigest(),
        origin=ORIGIN,
    )
