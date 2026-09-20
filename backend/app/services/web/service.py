"""Bounded retrieval of web evidence for one company.

What this service is
--------------------
Given a company and a short query, it produces evidence from a **closed set of
hosts**: the company's own website, its investor-relations URL once discovery
has verified it, and the exchange and regulator hosts the filing providers
already read. Nothing else is reachable — a URL handed in by a caller is
refused unless its host is pinned for that company.

What it is not
--------------
**Not a search engine.** There is no index of the web here, no ranking across
pages the platform has not seen, and no link following. Candidate URLs come
from the pinned records plus a fixed table of conventional document paths, and
the *only* thing the query text does is select which group of those paths to
try — it cannot introduce a path, a host or a parameter. A prompt-injected
instruction cannot steer this service at an arbitrary URL, because the service
has no mechanism for taking one.

**Not a second ingestion path.** An accepted page is rendered as a small,
boilerplate-free HTML document and handed to the *existing*
:class:`~app.services.documents.ingestion.DocumentIngestionService` as
``doc_type='web_page'``. Chunking, embedding, indexing, retrieval and the
knowledge vault then run exactly as they do for an upload. The platform does
not learn a second way to make a document searchable, and Phase 1 adds no
retrieval engine, no index and no citation model.

**Not a bypass.** Fetching honours ``robots.txt`` before the socket is opened,
paces requests per host, identifies itself honestly, and treats a 403 as a
refusal to be reported rather than an obstacle to be worked around. See
:mod:`app.services.web.fetcher`.

Cache behaviour
---------------
``Namespace.WEB`` holds the last known provenance of each fetched URL, and is
the only namespace this service invalidates. Two things follow, and they are
the mitigation for the cache-thrash finding in the Part 3 audit:

* a page whose bytes are unchanged since the last fetch never reaches
  :meth:`DocumentIngestionService.accept` at all — the content hash is
  compared first, so no job is enqueued, no pipeline run happens and no cache
  is invalidated;
* a page that *is* new is ingested through the existing path, which
  invalidates ``Namespace.RAG`` at the end of its own run. That invalidation
  lives in ``ingestion.run_job`` and is outside this phase's authorised edit
  set; it is reported as a residual rather than silently worked around by
  adding a second invalidation here.

Provider independence
---------------------
Nothing on this path calls a model, an embedding API or a reranker. The
service fetches, extracts, and hands bytes to the ingestion service; indexing
and embedding happen later, in the existing worker, under the platform's own
configuration. ``tests/test_web_end_to_end.py`` completes a full search and
citation with ``AI_EXTERNAL_PROVIDERS_ENABLED=false`` and asserts that the only
host contacted is the company's pinned one; ``tests/test_web_citations.py``
pins the same independence at the citation and scope layer.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.ai.types import Citation, EvidenceKind, WebProvenance
from app.domain.documents.types import DocumentType
from app.domain.web.types import (
    WebContentClass,
    WebDocumentRef,
    WebFetchPolicy,
    WebRejectionReason,
    WebSearchQuery,
    WebSearchResult,
    WebSourceClass,
    source_class_label,
)
from app.models.company import Company
from app.models.filing_collection import CompanyCrawlState
from app.services.documents.ingestion import DocumentIngestionService, IngestionError
from app.services.platform.cache import Namespace
from app.services.platform.cache import cache as default_cache
from app.services.web.extract import (
    build_clean_html,
    canonicalize_url,
    collapse_whitespace,
    extract_page,
)
from app.services.web.fetcher import (
    FetchedDocument,
    HostPoliteness,
    Transport,
    WebFetchError,
    WebFetcher,
)
from app.services.web.quality import (
    EXCHANGE_HOSTS,
    IR_URL_VERIFIED_CONFIDENCE,
    REGULATOR_HOSTS,
    assess,
    classify,
    host_is_pinned_by,
    near_duplicate_key,
    web_authority,
)
from app.services.web.robots import RobotsPolicy
from app.services.web.safety import Resolver, UrlSafetyError, UrlSafetyPolicy

log = structlog.get_logger(__name__)

#: ``uploaded_by`` recorded on every web-ingested document. Kept as a literal
#: so a row can be traced back to this path — and never mistaken for a user.
WEB_UPLOADER = "web_evidence"

#: Conventional document paths, grouped by what the query is asking for. A
#: fixed table, matched by keyword: the query *selects* a group and can never
#: contribute to one. Paths are only ever appended to a company-owned origin —
#: never to an exchange or regulator host.
_PATH_HINTS: tuple[tuple[re.Pattern[str], tuple[str, ...]], ...] = (
    (
        re.compile(r"annual\s*report|annual_report|integrated\s*report|\bar\b", re.I),
        ("/investors/annual-reports", "/investor-relations/annual-reports",
         "/annual-reports"),
    ),
    (
        re.compile(r"quarter|result|earnings|financial|\bq[1-4]\b", re.I),
        ("/investors/results", "/investor-relations/results", "/financial-results"),
    ),
    (
        re.compile(r"presentation|shareholding|shareholder|investor|\bir\b", re.I),
        ("/investors", "/investor-relations", "/investors/presentations"),
    ),
    (
        re.compile(r"press|news|media|announce", re.I),
        ("/press-releases", "/news", "/media"),
    ),
)

#: Tried when the query suggests nothing in particular. The IR page is the one
#: a listed company is required to maintain, so it is the best default guess.
_DEFAULT_PATHS: tuple[str, ...] = ("/investors", "/investor-relations")

#: Suffix by content class. The ingestion path resolves the format from the
#: filename and refuses an unknown extension, so this is not cosmetic.
_SUFFIXES: dict[WebContentClass, str] = {
    WebContentClass.HTML: ".html",
    WebContentClass.TEXT: ".txt",
    WebContentClass.PDF: ".pdf",
}

_FILENAME_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


class WebEvidenceError(Exception):
    """A caller error: an unknown company, or an unpinned URL handed in.

    Raised rather than returned because it means the caller's own model of the
    allowlist is wrong. Reporting it as one page's rejection would hide a
    programming error behind a data-quality message.
    """


@dataclass(frozen=True, slots=True)
class _Candidate:
    """A URL to try, with the class its host will be classified as."""

    url: str
    source_class: WebSourceClass


class WebSearchService:
    """Fetches, extracts and persists evidence from a company's pinned hosts."""

    #: Cap on the query text. It reaches a log line and the persisted metadata
    #: and nothing else, but an unbounded string from an API caller is still
    #: worth bounding.
    MAX_QUERY_CHARS = 200

    def __init__(
        self,
        db: Session,
        *,
        ingestion: DocumentIngestionService | None = None,
        policy: WebFetchPolicy | None = None,
        safety: UrlSafetyPolicy | None = None,
        robots: RobotsPolicy | None = None,
        fetcher: WebFetcher | None = None,
        transport: Transport | None = None,
        politeness: HostPoliteness | None = None,
        resolver: Resolver | None = None,
        cache_service: Any | None = None,
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.db = db
        self.policy = policy or WebFetchPolicy()
        self.ingestion = ingestion or DocumentIngestionService(db)
        self.cache = cache_service or default_cache
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._sleep = sleep
        self._transport = transport
        #: Name resolution, injectable for the same reason the transport is:
        #: the resolved address is what the safety check actually rules on,
        #: so a test that cannot substitute a resolver cannot exercise that
        #: check without a network.
        self._resolver = resolver
        #: A caller-supplied safety policy, when there is one. The working
        #: policy is deliberately *not* held here: which hosts are reachable
        #: is a property of the company being searched, so it is resolved per
        #: search (see `_fetcher_for`). A single instance-level policy could
        #: only be empty — refusing every pinned host, which is the failure
        #: this shape exists to prevent — or shared between companies.
        self._safety_override = safety
        self._robots = robots or RobotsPolicy(
            user_agent=self.policy.robots_user_agent,
            max_bytes=self.policy.robots_max_bytes,
            ttl_seconds=self.policy.robots_ttl_seconds,
        )
        #: Shared across searches so per-host pacing survives a second query
        #: on the same service instance instead of restarting with a new
        #: fetcher.
        self._politeness = politeness or HostPoliteness(
            default_delay=self.policy.default_crawl_delay, sleep=sleep,
        )
        self._fetcher_override = fetcher

    def _fetcher_for(self, pinned: dict[str, WebSourceClass]) -> WebFetcher:
        """The fetcher for one company's search, allowlist included.

        The pinned hosts are read from the company row and its crawl state, so
        they cannot be known before the search begins. Building the safety
        policy from them here is what makes "the company's own website and its
        verified IR page, and nothing else" true of the running code rather
        than only of the documentation.

        An injected policy or fetcher wins: a caller that supplies one is
        stating which hosts it wants reachable.
        """
        if self._fetcher_override is not None:
            return self._fetcher_override
        return WebFetcher(
            policy=self.policy,
            safety=self._safety_override
            or UrlSafetyPolicy(
                allowed_hosts=tuple(pinned), resolver=self._resolver,
            ),
            robots=self._robots,
            transport=self._transport,
            politeness=self._politeness,
            sleep=self._sleep,
            now=self._now,
        )

    # ===================================================================
    # Entry point
    # ===================================================================
    def search(self, query: WebSearchQuery) -> WebSearchResult:
        """Retrieve bounded evidence for ``query``.

        Never raises for one page's failure: a company whose website is
        unreachable should still contribute whatever its IR page yielded, and
        the caller should be able to see why the rest was declined.
        """
        company = self.db.get(Company, query.company_id)
        if company is None:
            raise WebEvidenceError(f"unknown company '{query.company_id}'")

        state = self._crawl_state(company.id)
        pinned = self._pinned_hosts(company, state)
        candidates = self._candidates(company, state, query, pinned)
        fetcher = self._fetcher_for(pinned)
        log.info(
            "web evidence search", company_id=company.id,
            query=query.query[: self.MAX_QUERY_CHARS],
            pinned_hosts=sorted(pinned), candidates=len(candidates),
        )

        documents: list[WebDocumentRef] = []
        citations: list[Citation] = []
        rejected: list[tuple[str, WebRejectionReason]] = []
        details: dict[str, str] = {}
        seen_content: dict[str, str] = {}

        for candidate in candidates[: max(1, query.max_urls)]:
            try:
                fetched = fetcher.fetch(candidate.url)
            except WebFetchError as exc:
                rejected.append((candidate.url, exc.reason))
                details[candidate.url] = exc.detail
                continue
            except UrlSafetyError as exc:  # pragma: no cover - the fetcher wraps these
                rejected.append((candidate.url, exc.reason))
                details[candidate.url] = exc.detail
                continue
            except Exception as exc:  # noqa: BLE001 - one page must not end a run
                rejected.append((candidate.url, WebRejectionReason.TRANSPORT_ERROR))
                details[candidate.url] = f"{type(exc).__name__}: {exc}"
                continue

            outcome = self._accept_page(
                company, candidate, fetched, query, seen_content,
            )
            if isinstance(outcome, tuple):
                reason, detail = outcome
                rejected.append((candidate.url, reason))
                details[candidate.url] = detail
                continue
            documents.append(outcome)
            citations.append(_citation_for(outcome))

        ranked = tuple(sorted(
            documents,
            key=lambda d: (
                -web_authority(d.source_class, published_at=d.published_at),
                -d.retrieved_at.timestamp(),
            ),
        )[: max(1, query.limit)])
        kept = {d.citation_key() for d in ranked}
        return WebSearchResult(
            company_id=company.id,
            query=query.query[: self.MAX_QUERY_CHARS],
            documents=ranked,
            citations=tuple(c for c in citations if c.key in kept),
            rejected=tuple(rejected),
            details=tuple(sorted(details.items())),
        )

    # ===================================================================
    # Allowlist
    # ===================================================================
    def _crawl_state(self, company_id: str) -> CompanyCrawlState | None:
        try:
            return self.db.scalar(
                select(CompanyCrawlState).where(
                    CompanyCrawlState.company_id == company_id
                )
            )
        except Exception:  # noqa: BLE001 - a missing table is not a fetch failure
            log.exception("crawl state unavailable", company_id=company_id)
            return None

    def _pinned_hosts(
        self, company: Company, state: CompanyCrawlState | None,
    ) -> dict[str, WebSourceClass]:
        """The hosts this company's evidence may come from.

        The exchange and regulator hosts are pinned so a caller-supplied
        filing URL can be validated and classified. Phase 1 does not construct
        URLs on them: the filing providers already own those formats, and
        duplicating them here would create a second source of truth for which
        URL means which filing.
        """
        pinned: dict[str, WebSourceClass] = {}

        website = _host_of(getattr(company, "website", None) or "")
        if website:
            pinned[website] = WebSourceClass.COMPANY_WEBSITE

        ir_host = _host_of(getattr(state, "ir_url", None) or "")
        if ir_host:
            # An unverified IR URL still pins the host, but is classified as
            # the company's own site: discovery's guess must not be promoted
            # to a class that reads as confirmed.
            pinned.setdefault(
                ir_host,
                WebSourceClass.VERIFIED_IR if self._ir_is_verified(state)
                else WebSourceClass.COMPANY_WEBSITE,
            )

        for host in EXCHANGE_HOSTS:
            pinned.setdefault(host, WebSourceClass.EXCHANGE)
        for host in REGULATOR_HOSTS:
            pinned.setdefault(host, WebSourceClass.REGULATOR)
        return pinned

    @staticmethod
    def _ir_is_verified(state: CompanyCrawlState | None) -> bool:
        confidence = getattr(state, "ir_url_confidence", None)
        if confidence is None:
            return False
        try:
            return float(confidence) >= IR_URL_VERIFIED_CONFIDENCE
        except (TypeError, ValueError):
            return False

    # ===================================================================
    # Candidate URLs
    # ===================================================================
    def _candidates(
        self,
        company: Company,
        state: CompanyCrawlState | None,
        query: WebSearchQuery,
        pinned: dict[str, WebSourceClass],
    ) -> list[_Candidate]:
        """The bounded, ordered list of URLs to try for this query."""
        candidates: list[_Candidate] = []
        website_host = _host_of(getattr(company, "website", None) or "")
        ir_host = _host_of(getattr(state, "ir_url", None) or "")

        def add(url: str) -> None:
            if not url:
                return
            canonical = canonicalize_url(url)
            if not canonical or any(c.url == canonical for c in candidates):
                return
            candidates.append(_Candidate(
                url=canonical,
                source_class=classify(
                    _host_of(canonical),
                    company_hosts=frozenset({website_host}) - {""},
                    ir_hosts=(
                        frozenset({ir_host}) if self._ir_is_verified(state)
                        else frozenset()
                    ),
                ),
            ))

        # 1. URLs the caller holds. Validated before anything is fetched: an
        #    unpinned host is a caller error, not a page to be declined.
        for url in query.candidate_urls:
            host = _host_of(url)
            if host not in pinned:
                raise WebEvidenceError(
                    f"'{url}' is not on a host pinned for {company.ticker}: "
                    f"allowed hosts are {', '.join(sorted(pinned))}"
                )
            add(url)

        # 2. The IR page, then conventional document paths on the company's own
        #    origins. All from the fixed table above.
        add((getattr(state, "ir_url", None) or "").strip())
        for origin in _company_origins(company, state):
            for path in self._paths_for(query.query):
                add(urljoin(origin, path))

        return candidates

    def _paths_for(self, query_text: str) -> tuple[str, ...]:
        """The conventional paths this query selects, from the fixed table."""
        for pattern, paths in _PATH_HINTS:
            if pattern.search(query_text or ""):
                return paths
        return _DEFAULT_PATHS

    # ===================================================================
    # Read-only seam for the on-demand targeted discovery layer
    # ===================================================================
    # These expose decisions the service already makes — which hosts a
    # company's evidence may come from, whether its IR URL is verified, and
    # which conventional paths a query selects — so a caller can plan a
    # bounded seed list from the same rules instead of re-deriving them.
    # None of them opens a socket or writes; fetching still happens only
    # through :meth:`search`.
    def allowlist_for(
        self, company: Company,
    ) -> tuple[CompanyCrawlState | None, dict[str, WebSourceClass]]:
        """The company's crawl state and the hosts pinned for it."""
        state = self._crawl_state(company.id)
        return state, self._pinned_hosts(company, state)

    def ir_is_verified(self, state: CompanyCrawlState | None) -> bool:
        """Whether the crawl state's IR URL meets the verified threshold."""
        return self._ir_is_verified(state)

    def conventional_paths(self, query_text: str) -> tuple[str, ...]:
        """The fixed-table paths ``query_text`` selects (never derived from it)."""
        return self._paths_for(query_text)

    # ===================================================================
    # One page: extract, assess, persist
    # ===================================================================
    def _accept_page(
        self,
        company: Company,
        candidate: _Candidate,
        fetched: FetchedDocument,
        query: WebSearchQuery,
        seen_content: dict[str, str],
    ) -> WebDocumentRef | tuple[WebRejectionReason, str]:
        """Extract, assess and ingest one fetched page.

        Returns the accepted reference, or ``(reason, detail)`` — the caller
        records the refusal and carries on.
        """
        try:
            extracted = extract_page(
                fetched.content,
                url=fetched.final_url or candidate.url,
                content_class=fetched.content_class,
                charset=fetched.charset,
                filename=_filename_for(candidate.url, fetched.content_class),
            )
        except Exception as exc:  # noqa: BLE001 - an extraction failure is a refusal
            return (
                WebRejectionReason.UNSUPPORTED_CONTENT,
                f"extraction failed ({type(exc).__name__}: {exc})",
            )

        assessment = assess(
            extracted.text, policy=self.policy,
            content_class=fetched.content_class,
        )
        if assessment.rejected:
            return (assessment.reason, assessment.detail)

        text = extracted.text
        key = near_duplicate_key(text)
        if key in seen_content and seen_content[key] != candidate.url:
            return (
                WebRejectionReason.DUPLICATE_CONTENT,
                f"same text as '{seen_content[key]}' already accepted in this run",
            )
        seen_content[key] = candidate.url

        canonical_url = canonicalize_url(extracted.canonical_url or candidate.url)
        title = extracted.title or fetched.host or _host_of(candidate.url)
        clean = build_clean_html(
            title=title,
            text=text,
            source_url=candidate.url,
            retrieved_at=fetched.retrieved_at,
            published_at=extracted.published_at,
            author=extracted.author,
        )
        content_hash = hashlib.sha256(clean).hexdigest()
        preview = collapse_whitespace(text)[: self.policy.citation_value_chars]

        if not query.persist:
            return self._reference(
                candidate, fetched, extracted, canonical_url, title, clean,
                preview,
                document_id=None, content_hash=content_hash,
            )

        # Unchanged since the last fetch: nothing to ingest, no job, no
        # pipeline run, no cache invalidation. This is what keeps a repeated
        # search from costing a re-ingest per page.
        cached = self._cached_page(company.id, canonical_url)
        if cached and cached.get("content_hash") == content_hash:
            return self._reference(
                candidate, fetched, extracted, canonical_url, title, clean,
                preview,
                document_id=cached.get("document_id"),
                content_hash=content_hash,
            )

        metadata = self._metadata(
            candidate, fetched, extracted, canonical_url, title, content_hash,
            preview, query,
        )
        filename = _filename_for(candidate.url, fetched.content_class)
        try:
            accepted = self.ingestion.accept(
                company.id,
                clean,
                filename,
                doc_type=DocumentType.WEB_PAGE,
                uploaded_by=WEB_UPLOADER,
                declared_size=len(clean),
                metadata=metadata,
            )
        except IngestionError as exc:
            return (WebRejectionReason.STORAGE_REFUSED, str(exc))

        document = accepted.document
        if (document.doc_type or "") != DocumentType.WEB_PAGE.value:
            # Byte-identical to a document this company already holds as an
            # upload. The bytes are the same; the provenance is not, and
            # relabelling an upload as a fetched page would be a lie about
            # where it came from.
            return (
                WebRejectionReason.DUPLICATE_CONTENT,
                f"identical bytes already exist as document #{document.id} "
                f"({document.doc_type})",
            )

        document.source_url = candidate.url
        document.source_class = candidate.source_class.value
        document.published_at = extracted.published_at
        document.retrieved_at = fetched.retrieved_at
        self.db.commit()

        self._remember_page(
            company.id, canonical_url, document_id=document.id,
            content_hash=content_hash,
        )
        return self._reference(
            candidate, fetched, extracted, canonical_url, title, clean, preview,
            document_id=document.id, content_hash=content_hash,
        )

    def _metadata(
        self,
        candidate: _Candidate,
        fetched: FetchedDocument,
        extracted: Any,
        canonical_url: str,
        title: str,
        content_hash: str,
        preview: str,
        query: WebSearchQuery,
    ) -> dict[str, Any]:
        """Provenance written to ``doc_metadata`` at accept time.

        Recorded at accept rather than after parsing so the row is
        self-describing while it is still queued — an operator looking at a
        stuck job can see what URL it came from without waiting for the
        pipeline.
        """
        return {
            "web": {
                "source_url": candidate.url,
                "final_url": fetched.final_url,
                "canonical_url": canonical_url,
                "source_class": candidate.source_class.value,
                "title": title,
                "author": extracted.author,
                "published_at": (
                    extracted.published_at.isoformat()
                    if extracted.published_at else None
                ),
                "retrieved_at": fetched.retrieved_at.isoformat(),
                "content_type": fetched.content_type_media,
                "status_code": fetched.status_code,
                "size_bytes": fetched.size_bytes,
                "raw_sha256": fetched.sha256,
                "content_hash": content_hash,
                "redirect_chain": list(fetched.redirect_chain),
                "robots": fetched.robots.status.value,
                "query": (query.query or "")[: self.MAX_QUERY_CHARS],
                "preview": preview,
            }
        }

    def _reference(
        self,
        candidate: _Candidate,
        fetched: FetchedDocument,
        extracted: Any,
        canonical_url: str,
        title: str,
        clean: bytes,
        preview: str,
        *,
        document_id: int | None,
        content_hash: str,
    ) -> WebDocumentRef:
        return WebDocumentRef(
            url=candidate.url,
            canonical_url=canonical_url,
            host=fetched.host or _host_of(candidate.url),
            source_class=candidate.source_class,
            content_class=fetched.content_class,
            title=title,
            retrieved_at=fetched.retrieved_at,
            published_at=extracted.published_at,
            author=extracted.author,
            content_hash=content_hash,
            size_bytes=len(clean),
            status_code=fetched.status_code,
            final_url=fetched.final_url,
            document_id=document_id,
            preview=preview,
        )

    # ===================================================================
    # Namespace.WEB — the provenance of a URL, not its contents
    # ===================================================================
    def _cached_page(self, company_id: str, canonical_url: str) -> dict | None:
        """What this platform last recorded about a URL, or ``None``.

        Only ever used to skip work for bytes that are demonstrably identical;
        the fetch happens first, and the hash comparison decides. A stale
        entry can therefore cost nothing worse than a skipped re-ingest of
        unchanged content.
        """
        try:
            value = self.cache.get(Namespace.WEB, "page", company_id, canonical_url)
        except Exception:  # noqa: BLE001 - the cache must never break a fetch
            log.warning("web cache read failed", company_id=company_id)
            return None
        return value if isinstance(value, dict) else None

    def _remember_page(
        self, company_id: str, canonical_url: str, *,
        document_id: int | None, content_hash: str,
    ) -> None:
        try:
            self.cache.set(
                Namespace.WEB,
                {"document_id": document_id, "content_hash": content_hash},
                "page", company_id, canonical_url,
            )
        except Exception:  # noqa: BLE001 - a cache write is not part of the fetch
            log.warning("web cache write failed", company_id=company_id)


def _host_of(url: str | None) -> str:
    return (urlsplit(url or "").hostname or "").lower()


def _origin(url: str | None) -> str:
    parts = urlsplit(url or "")
    if not parts.scheme or not parts.hostname:
        return ""
    return f"{parts.scheme}://{parts.netloc}"


def host_of(url: str | None) -> str:
    """The lower-cased hostname of ``url`` (``""`` when it has none)."""
    return _host_of(url)


def origin_of(url: str | None) -> str:
    """``scheme://netloc`` of ``url``, or ``""`` when it lacks either part."""
    return _origin(url)


def _company_origins(
    company: Company, state: CompanyCrawlState | None,
) -> list[str]:
    """The company-owned origins a candidate path may be appended to."""
    origins: list[str] = []
    for url in (
        (getattr(state, "ir_url", None) or "").strip(),
        (getattr(company, "website", None) or "").strip(),
    ):
        origin = _origin(url)
        if origin and origin not in origins:
            origins.append(origin)
    return origins


def _filename_for(url: str, content_class: WebContentClass) -> str:
    """A filename the ingestion path can resolve a format from.

    Built from the URL's last path segment so a stored document is
    recognisable, then sanitized and bounded. The extension comes from the
    *content class*, never from the URL: a server that serves HTML at
    ``/report.pdf`` must not have its bytes handed to the PDF parser.
    """
    suffix = _SUFFIXES[content_class]
    stem = (urlsplit(url or "").path or "").rstrip("/").rsplit("/", 1)[-1]
    stem = _FILENAME_UNSAFE.sub("_", stem)[:80].strip("._")
    if stem.lower().endswith(suffix):
        stem = stem[: -len(suffix)]
    if not stem:
        stem = f"web_{hashlib.sha256((url or '').encode('utf-8')).hexdigest()[:8]}"
    return f"{stem}{suffix}"[:120]


def _citation_for(ref: WebDocumentRef) -> Citation:
    """One citation for an accepted page, carrying its provenance.

    The key comes from the reference (single minting implementation), the
    value is the page's opening text — what the model will quote — and the
    class is stated in the label, so a reader can see whether this is the
    company's own IR page or something weaker.
    """
    provenance = WebProvenance(
        url=ref.url,
        title=ref.title,
        canonical_url=ref.canonical_url,
        published_at=ref.published_at,
        retrieved_at=ref.retrieved_at,
        content_hash=ref.content_hash,
    )
    label_class = source_class_label(ref.source_class.value)
    source = f"{ref.title or ref.host} — {label_class} ({ref.host})"
    if ref.published_at:
        source += f", published {ref.published_at:%d %b %Y}"
    source += f", retrieved {ref.retrieved_at:%d %b %Y}"
    return Citation(
        key=ref.citation_key(),
        label=f"[{label_class}] {ref.title or ref.host}",
        kind=EvidenceKind.WEB,
        value=ref.preview or ref.canonical_url,
        unit="",
        source=source,
        document_id=ref.document_id,
        web=provenance,
    )


__all__ = [
    "WEB_UPLOADER",
    "WebEvidenceError",
    "WebSearchService",
    "host_of",
    "origin_of",
]
