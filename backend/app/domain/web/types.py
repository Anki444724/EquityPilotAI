"""Closed types for the Phase-1 self-owned web evidence layer.

The shape of this module is the whole safety argument for Part 3 Phase 1, so
it is worth stating plainly.

**Phase 1 is not open-web search.** There is no query language, no ranking
across the internet, no discovery. There is a bounded list of URLs derived
from sources the platform *already* trusts about a company — its own website,
its verified investor-relations URL, and the exchange and regulator hosts the
filing providers already read — and there is a fetch of exactly those. The
name :class:`WebSearchQuery` is kept because that is what the caller is doing
from its own point of view (asking for evidence), not because Phase 1 searches
anything.

**Every refusal is typed.** :class:`WebRejectionReason` enumerates the ways a
URL can be declined. A caller can therefore act on a refusal — retry a 5xx,
stop asking about a host with no robots policy — instead of parsing a message.
That matters because the security-relevant refusals (a redirect into a private
address, an unpinned host) must be *assertable* in a test, and a string is not.

**Authority is never invented here.** :class:`WebSourceClass` names where a
page came from; the numeric weight lives in
:mod:`app.services.web.quality`, which ranks every web class below every
filing class. This module deliberately carries no score.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from app.domain.ai.types import Citation, mint_web_citation_key

#: The inline citation marker the prompt tells the model to emit, and which
#: :mod:`app.services.ai.citation_engine` enforces. A web citation key that
#: does not match this pattern is rendered as an *unknown key* and the whole
#: response is reported as unsupported — so the pattern is asserted here, at
#: the point a key is minted, rather than discovered at audit time.
CITATION_KEY_PATTERN = re.compile(r"\[([a-z][a-z0-9_.]{1,60})\]")


class WebSourceClass(StrEnum):
    """Where a fetched page came from, as cited to the reader.

    Ordered from most to least authoritative *within* the web tier. Every one
    of these ranks below every filing class — including ``other`` — because a
    page the platform fetched is not a document the company lodged.

    The distinction is load-bearing for the same reason ``EvidenceKind`` is: a
    company's own audited annual report and a paragraph on its marketing site
    carry very different weight, and a reader (or a model) that cannot tell
    them apart will treat the second as the first.
    """

    #: The company's own website, as recorded on the company row.
    COMPANY_WEBSITE = "company_website"
    #: An investor-relations URL that discovery *verified* — 200, or a 403/405
    #: that proves the page exists behind a bot filter (see
    #: ``IRDiscoveryService`` for why those count).
    VERIFIED_IR = "verified_ir"
    #: An exchange host (NSE/BSE/SEC filing domains).
    EXCHANGE = "exchange"
    #: A regulator or government filing host.
    REGULATOR = "regulator"
    #: Recognised financial media. Not reachable in Phase 1 (no media host is
    #: pinned); present so the ordering is complete when Phase 2 adds one.
    REPUTABLE_MEDIA = "reputable_media"
    #: Anything else on a pinned host whose role is not established. Never
    #: treated as authoritative.
    UNKNOWN = "unknown"


#: How each class is named in the evidence block and to the reader. The class
#: determines how much weight a page may carry, so it is stated on every web
#: citation — a reader must be able to tell the company's own IR page from a
#: page the platform merely pinned.
WEB_SOURCE_CLASS_LABELS: dict[WebSourceClass, str] = {
    WebSourceClass.COMPANY_WEBSITE: "Company Website",
    WebSourceClass.VERIFIED_IR: "Investor Relations",
    WebSourceClass.EXCHANGE: "Exchange",
    WebSourceClass.REGULATOR: "Regulator",
    WebSourceClass.REPUTABLE_MEDIA: "Media",
    WebSourceClass.UNKNOWN: "Web",
}


def source_class_label(value: str | None) -> str:
    """The human label for a stored ``source_class`` string.

    Accepts the raw string because the value is read back from a database
    column; an unrecognised or absent class labels as ``Web`` rather than
    echoing the raw value into a prompt.
    """
    try:
        return WEB_SOURCE_CLASS_LABELS[WebSourceClass((value or "").strip().lower())]
    except ValueError:
        return WEB_SOURCE_CLASS_LABELS[WebSourceClass.UNKNOWN]


class WebContentClass(StrEnum):
    """The three content families Phase 1 accepts."""

    HTML = "html"
    TEXT = "text"
    PDF = "pdf"


#: Media type → content class. An **allowlist**: a type absent here is refused
#: rather than guessed at, because the alternative is persisting a binary the
#: extractor cannot read and calling it evidence. ``application/octet-stream``
#: is deliberately absent — it is what a server says when it does not know,
#: which is not the same as saying the bytes are safe to parse.
ACCEPTED_CONTENT_TYPES: dict[str, WebContentClass] = {
    "text/html": WebContentClass.HTML,
    "application/xhtml+xml": WebContentClass.HTML,
    "text/plain": WebContentClass.TEXT,
    "application/pdf": WebContentClass.PDF,
}


def content_class_for(content_type: str) -> WebContentClass | None:
    """Resolve a ``Content-Type`` header to an accepted class, or ``None``.

    Parameters are stripped (``text/html; charset=utf-8`` is HTML) and the
    comparison is case-insensitive, because both vary across servers and
    neither changes what the bytes are.
    """
    media_type = (content_type or "").split(";", 1)[0].strip().lower()
    return ACCEPTED_CONTENT_TYPES.get(media_type)


class WebRejectionReason(StrEnum):
    """Every way Phase 1 can decline a URL.

    Grouped by the layer that raises it: URL safety, robots, transport,
    payload, then extraction and quality. Typed rather than free-text so a
    refusal is assertable and classifiable by a caller.
    """

    # --- URL safety (app.services.web.safety) --------------------------
    MALFORMED_URL = "malformed_url"
    SCHEME_NOT_ALLOWED = "scheme_not_allowed"
    HOST_NOT_PINNED = "host_not_pinned"
    PORT_NOT_ALLOWED = "port_not_allowed"
    ADDRESS_NOT_PUBLIC = "address_not_public"
    UNRESOLVABLE_HOST = "unresolvable_host"
    # --- robots (app.services.web.robots) ------------------------------
    ROBOTS_DISALLOWED = "robots_disallowed"
    # --- transport (app.services.web.fetcher) --------------------------
    REDIRECT_LIMIT = "redirect_limit"
    REDIRECT_WITHOUT_LOCATION = "redirect_without_location"
    FORBIDDEN_BY_SERVER = "forbidden_by_server"
    TOO_LARGE = "too_large"
    TIMEOUT = "timeout"
    TRANSPORT_ERROR = "transport_error"
    HTTP_ERROR = "http_error"
    CONTENT_TYPE_NOT_ALLOWED = "content_type_not_allowed"
    EMPTY_RESPONSE = "empty_response"
    # --- extraction / quality (extract, quality) -----------------------
    EXTRACTION_EMPTY = "extraction_empty"
    #: Retrieved and decodable, but too short to be evidence — a stub or a
    #: redirect notice. Distinct from an empty extraction because the two send
    #: an operator to different fixes.
    CONTENT_TOO_SHORT = "content_too_short"
    UNSUPPORTED_CONTENT = "unsupported_content"
    # --- persistence (service) -----------------------------------------
    #: The bytes are already the company's — either a near-identical page
    #: accepted earlier in the same run, or a document with the same content
    #: hash. Not an error, and not something to re-ingest.
    DUPLICATE_CONTENT = "duplicate_content"
    #: The ingestion path refused the bytes (storage full, size cap). Distinct
    #: from a fetch failure: the page was retrieved and could not be kept.
    STORAGE_REFUSED = "storage_refused"


@dataclass(frozen=True, slots=True)
class WebFetchPolicy:
    """The bounds one fetch runs inside. Bounded by construction.

    Every field is a ceiling, and every ceiling exists because the thing it
    bounds is attacker-influenced: a server chooses its own response size, its
    own redirect chain and its own content type. A cap that is not enforced on
    bytes actually received is not a cap (see
    :class:`app.services.web.fetcher.HttpxTransport`, which enforces it while
    streaming).
    """

    #: Whole-request timeout, seconds. Both connect and read.
    timeout_seconds: float = 20.0
    #: Ceiling on the response body. 8 MB is generous for an article and two
    #: orders of magnitude below the volume this deployment has to share.
    max_bytes: int = 8 * 1024 * 1024
    #: Redirect hops. A chain longer than this is either misconfigured or
    #: deliberately trying to outlast the validation.
    max_redirects: int = 3
    #: Attempts per hop. Retries happen **only** when the failure was
    #: classified retryable — never on a 4xx or a policy refusal.
    max_attempts: int = 2
    #: Ports a pinned host may be reached on. Anything else is a refusal,
    #: which is what stops a pinned hostname being used to reach an arbitrary
    #: service on the same machine.
    allowed_ports: frozenset[int] = frozenset({80, 443})
    #: Ceiling on ``robots.txt``. A policy file larger than this cannot be
    #: honestly evaluated, so it is treated as no policy (see robots module).
    robots_max_bytes: int = 512 * 1024
    #: TTL for a cached robots decision, seconds.
    robots_ttl_seconds: float = 3_600.0
    #: Minimum extracted characters for a page to count as evidence. Below
    #: this it is a stub, an error page or a cookie wall.
    min_content_chars: int = 200
    #: Cap on the persisted extract preview carried into a citation.
    preview_chars: int = 400
    #: Cap on the passage carried in ``Citation.value``.
    citation_value_chars: int = 600
    #: An honest, identifiable user agent. Deliberately **not** a browser
    #: string: impersonating Chrome to get past a bot filter is exactly the
    #: bypass this layer must not perform, and a site that refuses a named
    #: research bot has expressed a preference the platform honours.
    user_agent: str = (
        "EquityPilotAI/1.0 (+https://github.com/Anki444724/EquityPilotAI; "
        "research bot)"
    )
    #: The token ``robots.txt`` rules are matched against.
    robots_user_agent: str = "EquityPilotAI"
    #: Seconds to wait between requests to the same host when robots.txt
    #: supplies no crawl-delay. Small, but not zero.
    default_crawl_delay: float = 1.0


@dataclass(frozen=True, slots=True)
class WebDocumentRef:
    """What a fetched page is, once it has been accepted.

    Carries the provenance a reader needs to check the claim themselves: the
    URL actually fetched, the canonical URL the page claims, when it was
    published (only when the page genuinely says so), and when the platform
    retrieved it. ``published_at`` is ``None`` far more often than not, and
    that is correct — a fabricated date is worse than an absent one.
    """

    url: str
    canonical_url: str
    host: str
    source_class: WebSourceClass
    content_class: WebContentClass
    title: str
    retrieved_at: datetime
    published_at: datetime | None = None
    author: str | None = None
    content_hash: str = ""
    size_bytes: int = 0
    status_code: int = 0
    final_url: str = ""
    #: The document row this page was persisted as, when it was persisted.
    #: ``None`` for a read-only run (``WebSearchQuery.persist=False``), which
    #: is why it is an id rather than a flag.
    document_id: int | None = None
    #: The page's opening text, bounded by the fetch policy. This is what a
    #: citation quotes, so it is the extractor's output rather than a summary
    #: of it.
    preview: str = ""

    def citation_key(self) -> str:
        """A key the citation marker regex accepts, stable for this page.

        Delegates to :func:`app.domain.ai.types.mint_web_citation_key` so the
        key minted here and the key a citation carries when it is later
        reconstructed from stored provenance are the same key by construction,
        not by coincidence.
        """
        key = mint_web_citation_key(self.canonical_url or self.url)
        # Fail here rather than at audit time: a key the marker pattern rejects
        # is reported as an invented citation and renders the whole answer
        # unsupported.
        assert CITATION_KEY_PATTERN.fullmatch(f"[{key}]"), (
            f"minted key {key!r} does not match the citation marker pattern"
        )
        return key

    def as_dict(self) -> dict[str, object]:
        return {
            "url": self.url,
            "canonical_url": self.canonical_url,
            "host": self.host,
            "source_class": self.source_class.value,
            "content_class": self.content_class.value,
            "title": self.title,
            "author": self.author,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "retrieved_at": self.retrieved_at.isoformat(),
            "content_hash": self.content_hash,
            "size_bytes": self.size_bytes,
            "status_code": self.status_code,
            "final_url": self.final_url,
            "document_id": self.document_id,
            "preview": self.preview,
        }


@dataclass(frozen=True, slots=True)
class WebSearchQuery:
    """A bounded request for web evidence about one company.

    ``limit`` caps how many pages contribute evidence; ``max_urls`` caps how
    many are even attempted. Both exist so a caller cannot turn a question
    into an unbounded crawl by accident.
    """

    company_id: str
    query: str = ""
    #: URLs the caller already holds that are on a pinned host — a filing
    #: page, a press release it discovered elsewhere. Still safety-checked and
    #: robots-checked; being supplied by the caller is not a trust signal.
    candidate_urls: tuple[str, ...] = ()
    limit: int = 5
    max_urls: int = 8
    #: Persist accepted pages as documents. Off makes the service a pure
    #: reader, which is what a dry run wants.
    persist: bool = True


@dataclass(frozen=True, slots=True)
class WebSearchResult:
    """The outcome of one bounded retrieval.

    Rejections are returned rather than raised: a company whose website is
    unreachable should still contribute whatever its IR page yielded, and the
    caller should be able to see *why* the rest was declined. The one
    exception is a refusal that indicates a programming error (an unpinned
    host handed in by the caller), which the service raises.
    """

    company_id: str
    query: str
    documents: tuple[WebDocumentRef, ...] = ()
    citations: tuple[Citation, ...] = field(default_factory=tuple)
    #: ``(url, reason)`` for everything declined, in attempt order.
    rejected: tuple[tuple[str, WebRejectionReason], ...] = field(default_factory=tuple)
    #: Human-readable detail per rejection, keyed by URL. Separate from the
    #: typed reasons because the reason is for control flow and this is for a
    #: log line or an operator.
    details: tuple[tuple[str, str], ...] = field(default_factory=tuple)

    @property
    def any_accepted(self) -> bool:
        return bool(self.documents)

    def rejections_for(self, reason: WebRejectionReason) -> tuple[str, ...]:
        return tuple(url for url, why in self.rejected if why is reason)

    def as_dict(self) -> dict[str, object]:
        return {
            "company_id": self.company_id,
            "query": self.query,
            "documents": [d.as_dict() for d in self.documents],
            "citations": [c.key for c in self.citations],
            "rejected": [
                {"url": url, "reason": reason.value}
                for url, reason in self.rejected
            ],
        }
