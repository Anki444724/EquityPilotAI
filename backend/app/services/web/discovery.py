"""Bounded, same-host discovery over a pinned origin — Part 3 Phase 2.

Phase 1 (see :mod:`app.services.web.service`) fetches a *closed* candidate
list assembled from company rows and a fixed path table; its type module is
explicit that "there is no discovery" there. This module is the discovery
layer Phase 2 adds: starting from one seed URL on an already-pinned origin,
it walks links breadth-first, scores what it finds, and hands the caller
accepted pages. It changes one thing — reach — and must change nothing else:
every security control still lives in the Phase-1 collaborators, and this
module calls them rather than re-deciding anything.

What this is
------------
* **Strict same-host BFS.** Links are followed only when their host matches
  the seed's host exactly — ``www.acme.example`` does not pull in
  ``acme.example``, and neither does any other host. Cross-host links are
  refused as findings, not fetched.
* **Bounded by construction.** Pages, depth, per-page fan-out, the query-url
  budget and the sitemap entry count are all ceilings with class-level hard
  caps; a caller cannot size an open-web crawl by passing large numbers.
  ``max_pages`` bounds *fetch attempts*, the sitemap probe included, so one
  crawl costs at most ``max_pages`` requests against the host — that is the
  invariant the politeness of the fetcher is there to pace, not to raise.
* **Deduplicated at enqueue, not at fetch.** Every candidate is canonicalized
  with the Phase-1 :func:`~app.services.web.extract.canonicalize_url` — the
  *only* canonicalizer in this package — and tested against the seen-set the
  moment it is considered, so one page is fetched at most once no matter how
  many forms of its URL the site links.
* **`robots.txt`, SSRF, redirect and size controls: delegated, entirely.**
  URLs go through the injected :class:`~app.services.web.fetcher.WebFetcher`,
  whose order of operations (safety → robots → delay → streamed request →
  per-hop revalidation) is the Phase-1 contract. There is no second robots
  evaluator, no second safety layer and no HTTP client here, and a refusal
  from the fetcher becomes a refusal in the report verbatim.
* **Bounded sitemap discovery.** ``/sitemap.xml`` on the seed origin is read
  once, its ``<loc>`` entries are subject to every candidate check, and a
  ``<sitemapindex>`` is recorded and stopped at: child sitemaps are never
  fetched. The probe rides the fetcher like every other request, which means
  it only sees bodies the Phase-1 MIME allowlist admits (an ``application/xml``
  server answer arrives as a typed ``sitemap_unavailable`` diagnostic, not a
  crawl failure — Phase-1 files are not this layer's to widen). The probe
  consumes one unit of ``max_pages`` like any other request.
* **Query-explosion prevention.** A URL that still carries a query string
  after canonicalization consumes a budget that defaults to zero, so calendar
  pages, faceted navigation and ``?month=1..12``×``?year=2015..2026`` fan-out
  cannot outrun ``max_pages``. Tracking parameters are removed by the
  canonicalizer first and never spend the budget.
* **Quality-filtered, no persistence.** Pages are extracted, assessed and
  deduplicated with the Phase-1 quality functions, then returned as
  :class:`~app.domain.web.types.WebDocumentRef` values with
  ``document_id=None`` — exactly the shape a read-only Phase-1 run yields.
  There is no ingestion, no cache and no database session anywhere in this
  module; a caller that wants persistence hands the refs to the existing
  :class:`~app.services.web.service.WebSearchService` path, not a second one.

Hardening requirements carried from review
------------------------------------------
* **H1** — a discovered link path or a sitemap ``<loc>`` path containing
  ``%2e``, ``%2f`` or ``%5c`` (case-insensitive) is refused as
  ``MALFORMED_HREF`` **as written**: the URL is not decoded, not rewritten,
  and not fetched. Percent-decoding before the check would let ``%2f`` turn
  into a path separator the same way ``/`` already is.
* **M1** — ``same_host_only=False`` fails closed: the crawl raises rather
  than becoming cross-host.
* **M2** — ``respect_robots=False`` fails closed: the crawl raises rather
  than fetching without robots. There is no bypass flag anywhere in this
  package, and a field that reads like one may not act like one.
* **M3** — the crawler's byte ceiling may not exceed the injected fetcher's
  policy ``max_bytes``; the fetcher's streamed cap is the only ceiling
  enforced against bytes actually received, so a crawler configured above it
  would be a cap that quietly isn't.
* **F-1** — :func:`seed_from_url` rejects encoded path delimiters before any
  DNS, robots or network activity; the check is pure string work and the
  function touches no collaborator.
* **F-2** — a redirect whose *final* URL carries encoded path delimiters is
  rejected before the page is accepted or its links are expanded, and the
  refusal preserves the URL exactly as the transport reported it.
* **F-3** — the fetcher's byte ceiling is re-read from the live collaborator
  at the top of every :meth:`WebCrawlerDiscovery.crawl`, so swapping the
  fetcher after construction cannot slip past M3.

Because a caller can construct :class:`CrawlSeed` directly and skip
:func:`seed_from_url`, ``crawl()`` re-runs the seed validation — bounds,
unsafe-flag checks and the encoded-path check — itself, before the first
line of the walk that could reach a socket. The F-1 guarantee is a property
of the crawl, not of one entry point.

No new dependencies
-------------------
``structlog`` plus the standard library at module level; ``bs4`` is imported
lazily inside the one function that parses links, mirroring
:func:`app.services.web.extract.extract_html`. Nothing here imports a
provider, a model client, the database or the document-ingestion path, and
``tests/test_web_crawler_reuse_contracts.py`` asserts that structurally.
"""
from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ElementTree
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Callable
from urllib.parse import unquote, urljoin, urlsplit

import structlog

from app.domain.web.types import (
    WebDocumentRef,
    WebFetchPolicy,
    WebRejectionReason,
)
from app.services.web.extract import (
    build_clean_html,
    canonicalize_url,
    collapse_whitespace,
    extract_page,
)
from app.services.web.fetcher import FetchedDocument, WebFetchError, WebFetcher
from app.services.web.quality import assess, classify, near_duplicate_key
from app.services.web.safety import ALLOWED_SCHEMES

log = structlog.get_logger(__name__)

#: Encoded path delimiters, matched case-insensitively (H1). ``%2e`` is a
#: dot, ``%2f`` a slash, ``%5c`` a backslash; every one is a path separator
#: *in waiting*, which is why a path carrying one is refused rather than
#: decoded and judged. The scan is over the path component only: the query
#: budget already caps what a query can be used for, and the review rule is
#: about delimiters.
_ENCODED_PATH_DELIMITERS = re.compile(r"%(?:2e|2f|5c)", re.IGNORECASE)

#: A bare ``#fragment`` addresses a position in the page already queued.
_FRAGMENT_ONLY = re.compile(r"^#.*$")

#: Schemes that are never crawl targets, checked on the raw href before
#: resolution so ``mailto:`` cannot be urljoin'd into something else first.
_NON_WEB_SCHEMES = frozenset(
    {"mailto", "tel", "javascript", "data", "file", "ftp", "ws", "wss"}
)

#: ``<a href>`` scheme prefix, RFC 3986 shape.
_HREF_SCHEME = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.\-]*):")

#: The one sitemap location discovery reads. Not configurable on purpose:
#: a configurable probe path is a path a caller — or an attacker's content —
#: influences.
_SITEMAP_PATH = "/sitemap.xml"

#: Content class value strings, for the one comparison the crawler makes.
_HTML_CLASS_VALUE = "html"

#: Filename suffix by content class, mirroring the ingestion table: PDF text
#: extraction resolves a format from the filename, and an unknown extension
#: is a refusal deep in the document stack. The suffix follows the *class*,
#: never the URL.
_SUFFIXES = {"html": ".html", "text": ".txt", "pdf": ".pdf"}


class CrawlRejectionReason(StrEnum):
    """Discovery-layer refusals.

    Deliberately *not* a merge into :class:`~app.domain.web.types.
    WebRejectionReason`: that enum is the closed Phase-1 vocabulary and
    Phase-1 files are not this layer's to edit. Fetch-level failures carry
    the Phase-1 reason verbatim through the report; these are the reasons
    only a crawler can give.
    """

    #: H1/F-1/F-2: the href, ``<loc>`` or redirect-final URL carries ``%2e``,
    #: ``%2f`` or ``%5c`` in its path. Refused with the URL as written.
    MALFORMED_HREF = "malformed_href"
    #: The candidate's host is not the seed's host exactly.
    OFFSITE_LINK = "offsite_link"
    #: The canonical URL still carries a query string and the crawl's query
    #: budget is spent — the wall pagination fan-out hits.
    QUERY_BUDGET_EXCEEDED = "query_budget_exceeded"
    #: ``/sitemap.xml`` could not be fetched or parsed. A diagnostic, not a
    #: failure of the crawl — the BFS proceeds without the sitemap.
    SITEMAP_UNAVAILABLE = "sitemap_unavailable"
    #: A ``<sitemapindex>`` child was seen and not followed. Bounded sitemap
    #: discovery does not recurse into child files.
    CHILD_SITEMAP_SKIPPED = "child_sitemap_skipped"
    #: A ``<loc>`` that is not an absolute http(s) URL.
    SITEMAP_LOC_MALFORMED = "sitemap_loc_malformed"
    #: Entries beyond ``max_sitemap_urls`` in a well-formed ``<urlset>``;
    #: recorded once so "the sitemap was bigger than the budget" is visible
    #: without reconstructing it from a truncation flag.
    SITEMAP_CAP_REACHED = "sitemap_cap_reached"


#: Either vocabulary; both are StrEnums, so a caller compares or reads
#: ``.value`` uniformly.
Reason = CrawlRejectionReason | WebRejectionReason


class CrawlPolicyError(ValueError):
    """A bound the caller asked to break; the crawl refuses to run.

    M1/M2/M3, F-3, seed validation and out-of-range bounds all land here.
    Raised rather than reported: each of these is a caller or configuration
    error, and a refusal to exist is clearer than a crawl that ran with
    unsafe flags. ``reason`` is attached where the refusal maps onto a
    :class:`CrawlRejectionReason` (the seed's malformed href does).
    """

    def __init__(self, detail: str, *, reason: Reason | None = None) -> None:
        super().__init__(f"{detail}" if reason is None else f"{reason.value}: {detail}")
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True, slots=True)
class CrawlSeed:
    """One bounded crawl: where to start and how far to go.

    Every number is a ceiling and every ceiling has a class-level hard cap,
    checked at construction *and* re-checked by ``crawl()`` — the dataclass
    is frozen, but a caller can still reach through ``object.__setattr__``,
    and a control that works only if the caller cooperates is not a control.
    """

    #: Absolute http(s) URL on a host the caller's safety policy already
    #: pins. Validated lexically (and only lexically) by
    #: :func:`seed_from_url`, and again by ``crawl()`` before any network
    #: activity. Pinned-host membership itself remains the fetcher's safety
    #: policy's answer — this module must not grow a second allowlist.
    url: str
    #: Label only. The crawler holds no database session and never resolves a
    #: company; it appears in the report so a caller can key results.
    company_id: str = ""
    #: Fetch attempts, the seed and the sitemap probe included. Refusals
    #: spend budget deliberately: an unfetchable page is still a request.
    max_pages: int = 8
    #: Link hops from the seed. ``0`` fetches the seed alone; ``1`` adds its
    #: direct links, and so on.
    max_depth: int = 1
    #: Candidates taken from one page, in document order. A footer linking
    #: 500 ways contributes 50, not 500.
    max_links_per_page: int = 50
    #: URLs that still carry a query string after canonicalization may be
    #: queued until this budget is spent. Zero by default: faceted sites are
    #: an explicit opt-in per crawl.
    max_query_urls: int = 0
    #: Read ``/sitemap.xml`` on the seed origin once. Never recursed.
    include_sitemap: bool = True
    #: Cap on enqueued ``<loc>`` entries from that one file.
    max_sitemap_urls: int = 25
    #: Host roles for classification, supplied as plain host strings by the
    #: caller (who read them from the company row / verified IR record).
    #: The DB lookup stays in the service, which owns the session; ``classify``
    #: still makes the decision, so the authority table is not forked here.
    company_hosts: tuple[str, ...] = ()
    ir_hosts: tuple[str, ...] = ()
    #: M1: only ``True`` is a legal value. The field exists so that "the
    #: crawler can be told to leave the host" is *testably false* — False
    #: fails closed rather than being silently ignored.
    same_host_only: bool = True
    #: M2: the same argument as ``same_host_only``, for robots.
    respect_robots: bool = True

    #: Hard caps: a bound larger than this is not ambition, it is a
    #: configuration that stopped being a bound.
    MAX_PAGES_CEILING = 64
    MAX_DEPTH_CEILING = 3
    MAX_LINKS_CEILING = 200
    MAX_QUERY_URLS_CEILING = 32
    MAX_SITEMAP_URLS_CEILING = 100

    def __post_init__(self) -> None:
        # Frozen instances can carry mutable *content*: host tuples are
        # normalised here so downstream set membership sees one canonical
        # shape regardless of how the caller spelled the hosts.
        object.__setattr__(self, "company_hosts", tuple(
            h.strip().lower() for h in self.company_hosts if h and h.strip()
        ))
        object.__setattr__(self, "ir_hosts", tuple(
            h.strip().lower() for h in self.ir_hosts if h and h.strip()
        ))
        validate_seed_bounds(self)


def validate_seed_bounds(seed: CrawlSeed) -> None:
    """Numeric bounds, checked live rather than once.

    Called from ``CrawlSeed.__post_init__`` *and* from ``crawl()``: the seed
    is the attacker-adjacent surface (a tool layer could build one from
    model output), so its limits are re-proved at use, not trusted from
    construction.
    """
    limits = (
        ("max_pages", seed.max_pages, 1, CrawlSeed.MAX_PAGES_CEILING),
        ("max_depth", seed.max_depth, 0, CrawlSeed.MAX_DEPTH_CEILING),
        ("max_links_per_page", seed.max_links_per_page, 1, CrawlSeed.MAX_LINKS_CEILING),
        ("max_query_urls", seed.max_query_urls, 0, CrawlSeed.MAX_QUERY_URLS_CEILING),
        ("max_sitemap_urls", seed.max_sitemap_urls, 0, CrawlSeed.MAX_SITEMAP_URLS_CEILING),
    )
    for name, value, low, high in limits:
        if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
            raise CrawlPolicyError(f"{name}={value!r} is not an integer in [{low}, {high}]")
    if seed.same_host_only is not True:
        raise CrawlPolicyError(
            "same_host_only=False is refused: this crawler follows links only "
            "within the seed's host, and a caller that wants a different host "
            "should start a crawl seeded on that host (M1)"
        )
    if seed.respect_robots is not True:
        raise CrawlPolicyError(
            "respect_robots=False is refused: robots.txt is enforced by the "
            "injected fetcher and cannot be turned off from here (M2)"
        )


@dataclass(frozen=True, slots=True)
class CrawlRefusal:
    """One declined URL, with the reason and the raw string involved.

    ``url`` is preserved exactly as it was seen — for a malformed href that
    *is* the point (H1, F-2): the diagnostic must show the encoded bytes,
    not a decoded guess of what they might have meant.
    """

    url: str
    reason: Reason
    detail: str
    depth: int = 0
    #: Which crawl input produced the candidate: ``"seed"``, ``"link"`` or
    #: ``"sitemap"`` (redirect rejections carry ``"redirect"``).
    source: str = "link"

    def as_dict(self) -> dict[str, object]:
        return {
            "url": self.url,
            "reason": getattr(self.reason, "value", str(self.reason)),
            "detail": self.detail,
            "depth": self.depth,
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class CrawlReport:
    """The outcome of one bounded crawl.

    Refusals are returned, not raised, exactly as in Phase 1: a company
    whose footer links into an unfetchable blog should still produce the
    evidence from its own investor pages, with a visible reason next to each
    miss. ``rejected``/``details`` are shaped like ``WebSearchResult``'s so a
    caller folds the two together without an adapter.
    """

    seed_url: str
    host: str
    company_id: str = ""
    pages: tuple[WebDocumentRef, ...] = ()
    refusals: tuple[CrawlRefusal, ...] = ()
    #: Fetch attempts spent (seed and sitemap probe included).
    pages_fetched: int = 0
    #: Candidates that survived every check and were queued.
    links_enqueued: int = 0
    #: Candidates dropped because their canonical form was already known.
    deduplicated: int = 0
    #: True when candidates remained queued at the ``max_pages`` wall.
    truncated: bool = False
    #: One of ``"skipped"``, ``"unavailable"``, ``"index"``, ``"ok"`` — what
    #: the sitemap probe concluded, so an operator does not have to diff
    #: refusal lists to learn whether the sitemap was consulted.
    sitemap_status: str = "skipped"

    @property
    def any_accepted(self) -> bool:
        return bool(self.pages)

    @property
    def rejected(self) -> tuple[tuple[str, Reason], ...]:
        return tuple((r.url, r.reason) for r in self.refusals)

    @property
    def details(self) -> tuple[tuple[str, str], ...]:
        return tuple((r.url, r.detail) for r in self.refusals)

    def as_dict(self) -> dict[str, object]:
        return {
            "seed_url": self.seed_url,
            "host": self.host,
            "company_id": self.company_id,
            "pages": [p.as_dict() for p in self.pages],
            "refusals": [r.as_dict() for r in self.refusals],
            "pages_fetched": self.pages_fetched,
            "links_enqueued": self.links_enqueued,
            "deduplicated": self.deduplicated,
            "truncated": self.truncated,
            "sitemap_status": self.sitemap_status,
        }


def seed_from_url(
    url: str,
    *,
    company_id: str = "",
    max_pages: int = 8,
    max_depth: int = 1,
    max_links_per_page: int = 50,
    max_query_urls: int = 0,
    include_sitemap: bool = True,
    max_sitemap_urls: int = 25,
    company_hosts: tuple[str, ...] = (),
    ir_hosts: tuple[str, ...] = (),
) -> CrawlSeed:
    """Build a :class:`CrawlSeed`, validating the URL lexically first (F-1).

    Deliberately a pure function over a string: it never touches a resolver,
    the robots policy or the transport, so a rejected seed cannot have cost a
    packet. Host pinning stays where Phase 1 left it — the fetcher's safety
    policy answers "is this host allowed" because it owns the allowlist.

    The seed's URL is canonicalized (fragment and tracking parameters drop
    out; the path is preserved verbatim, per the canonicalizer's contract)
    and encoded path delimiters in it are rejected as ``MALFORMED_HREF``.
    Unsafe flags are not expressible through this entry point at all —
    ``same_host_only``/``respect_robots`` simply have no parameter — so the
    only way to arrive at M1/M2 is direct construction, which ``crawl()``
    still refuses.
    """
    canonical = validate_seed_url(url)
    return CrawlSeed(
        url=canonical,
        company_id=company_id,
        max_pages=max_pages,
        max_depth=max_depth,
        max_links_per_page=max_links_per_page,
        max_query_urls=max_query_urls,
        include_sitemap=include_sitemap,
        max_sitemap_urls=max_sitemap_urls,
        company_hosts=company_hosts,
        ir_hosts=ir_hosts,
    )


def validate_seed_url(url: str) -> str:
    """The seed-URL checks every entry point must pass.

    Shared by :func:`seed_from_url` and ``crawl()`` — one source for that
    validation, which is the safe answer to a caller who constructs a
    ``CrawlSeed`` directly and skips the constructor. Raises
    :class:`CrawlPolicyError`; returns the canonical form to crawl from.
    """
    candidate = (url or "").strip()
    if not candidate:
        raise CrawlPolicyError("the seed URL is empty")
    parts = urlsplit(candidate)
    scheme = (parts.scheme or "").lower()
    if scheme and scheme not in ALLOWED_SCHEMES:
        raise CrawlPolicyError(
            f"seed scheme '{scheme}' is not one of {sorted(ALLOWED_SCHEMES)}"
        )
    if not scheme or not parts.netloc or not parts.hostname:
        raise CrawlPolicyError(f"seed '{candidate}' is not an absolute http(s) URL")
    try:
        # Lexical only, on purpose: which ports are *reachable* is the
        # fetcher's safety policy decision (``allowed_ports``) and the seed
        # gets that check at fetch time; duplicating the port set here would
        # create a second, drift-prone copy of it.
        _ = parts.port
    except ValueError as exc:
        raise CrawlPolicyError(f"unparseable port in seed '{candidate}'") from exc
    if _ENCODED_PATH_DELIMITERS.search(parts.path or ""):
        # F-1, and the same check closes the direct-construction bypass.
        # Named MALFORMED_HREF and quoting the URL as written: the *refusal*
        # is the useful diagnostic, and a decoded echo would not be
        # reproducible.
        raise CrawlPolicyError(
            f"seed '{candidate}' carries an encoded path delimiter "
            "(%2e, %2f or %5c) in its path; refused as malformed_href "
            "before any DNS, robots or network activity",
            reason=CrawlRejectionReason.MALFORMED_HREF,
        )
    return canonicalize_url(candidate)


@dataclass(slots=True)
class _CrawlState:
    """Mutable ledger for one crawl. Private; never returned.

    A dedicated object instead of a pile of parallel parameters keeps the
    seen-set, the queue and the budgets from drifting apart mid-loop — the
    enqueue-time dedup guarantee lives here, and one place to look is the
    whole point.
    """

    seed: CrawlSeed
    seed_url: str
    seed_host: str
    seed_origin: str
    effective_ceiling: int
    seen: set[str] = field(default_factory=set)
    queue: deque[tuple[str, int]] = field(default_factory=deque)
    refusals: list[CrawlRefusal] = field(default_factory=list)
    pages: list[WebDocumentRef] = field(default_factory=list)
    content_hashes: set[str] = field(default_factory=set)
    near_seen: dict[str, str] = field(default_factory=dict)
    fetched: int = 0
    enqueued: int = 0
    deduplicated: int = 0
    query_budget: int = 0

    def enqueue(self, canonical: str, depth: int) -> None:
        """Queue one surviving candidate. Dedup is *here*, at enqueue time."""
        if canonical in self.seen:
            self.deduplicated += 1
            return
        self.seen.add(canonical)
        self.queue.append((canonical, depth))
        self.enqueued += 1

    @property
    def budget_left(self) -> bool:
        return self.fetched < self.seed.max_pages


class WebCrawlerDiscovery:
    """Bounded link walking over one pinned origin, on top of Phase 1."""

    def __init__(
        self,
        *,
        fetcher: WebFetcher,
        policy: WebFetchPolicy | None = None,
        max_bytes: int | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        #: Public and read live on purpose (F-3): the byte-ceiling check
        #: consults *this* attribute on every crawl, so a collaborator
        #: swapped in afterwards is measured against what it actually
        #: enforces, not what it enforced at construction.
        self.fetcher = fetcher
        #: One policy for the assessment floor and preview cap — the
        #: fetcher's, unless the caller states otherwise. A private second
        #: policy is how two answers to "how small is too small" get written.
        self.policy = policy or getattr(fetcher, "policy", None) or WebFetchPolicy()
        #: M3: the crawler's own ceiling, when it names one, may not exceed
        #: the fetcher's. Checked here for a fast failure and again per
        #: crawl (F-3), because ``policy.max_bytes`` is mutable state.
        self.max_bytes = max_bytes
        self._validate_byte_ceiling()
        self._now = now or (lambda: datetime.now(timezone.utc))

    # ------------------------------------------------------------ ceilings
    def _live_fetcher_ceiling(self) -> int:
        """The fetcher's byte ceiling *as it stands now*."""
        return int(self.fetcher.policy.max_bytes)

    def _validate_byte_ceiling(self) -> None:
        live = self._live_fetcher_ceiling()
        if self.max_bytes is not None and self.max_bytes > live:
            raise CrawlPolicyError(
                f"crawler byte ceiling {self.max_bytes:,} exceeds the injected "
                f"fetcher's live ceiling {live:,}; the fetcher's streamed cap "
                "is the only cap enforced against bytes actually received, so "
                "a crawler configured above it would bound nothing (M3/F-3)"
            )

    def effective_max_bytes(self) -> int:
        """The ceiling one crawl honours: ``min(crawler, live fetcher)``."""
        live = self._live_fetcher_ceiling()
        return live if self.max_bytes is None else min(self.max_bytes, live)

    # ===================================================================
    # Entry point
    # ===================================================================
    def crawl(self, seed: CrawlSeed) -> CrawlReport:
        """Walk the seed's host inside the seed's bounds; never persist.

        The guard order is load-bearing: bounds and unsafe flags first
        (M1/M2), the live byte ceiling second (M3/F-3), the seed URL third
        (F-1 and the direct-seed bypass), all before the first line of the
        walk that could reach a socket — a rejected crawl costs nothing to
        reject, and tests can assert that.
        """
        validate_seed_bounds(seed)
        self._validate_byte_ceiling()
        seed_url = validate_seed_url(seed.url)

        parts = urlsplit(seed_url)
        state = _CrawlState(
            seed=seed,
            seed_url=seed_url,
            seed_host=(parts.hostname or "").lower(),
            seed_origin=f"{(parts.scheme or '').lower()}://{(parts.netloc or '').lower()}",
            effective_ceiling=self.effective_max_bytes(),
            seen={seed_url},
            queue=deque([(seed_url, 0)]),
            query_budget=seed.max_query_urls,
        )

        log.info(
            "web crawl started", seed_url=seed_url, host=state.seed_host,
            company_id=seed.company_id, max_pages=seed.max_pages,
            max_depth=seed.max_depth, byte_ceiling=state.effective_ceiling,
        )

        sitemap_status = "skipped"
        if seed.include_sitemap and state.budget_left:
            sitemap_status = self._discover_sitemap(state)

        while state.queue and state.budget_left:
            url, depth = state.queue.popleft()
            state.fetched += 1
            self._visit(state, url=url, depth=depth)

        return CrawlReport(
            seed_url=seed_url,
            host=state.seed_host,
            company_id=seed.company_id,
            pages=tuple(state.pages),
            refusals=tuple(state.refusals),
            pages_fetched=state.fetched,
            links_enqueued=state.enqueued,
            deduplicated=state.deduplicated,
            truncated=bool(state.queue),
            sitemap_status=sitemap_status,
        )

    # ===================================================================
    # One URL: fetch, vouch for the landing, accept, expand
    # ===================================================================
    def _visit(self, state: _CrawlState, *, url: str, depth: int) -> None:
        try:
            fetched = self.fetcher.fetch(url)
        except WebFetchError as exc:
            # The Phase-1 vocabulary passes through verbatim: robots,
            # size, content-type, 403, timeout — the crawler neither
            # re-decides nor re-labels what the fetcher decided.
            state.refusals.append(
                CrawlRefusal(
                    url=exc.url or url, reason=exc.reason,
                    detail=exc.detail, depth=depth,
                    source="seed" if url == state.seed_url else "link",
                )
            )
            return
        except Exception as exc:  # noqa: BLE001 - one page must not end a run
            state.refusals.append(
                CrawlRefusal(
                    url=url, reason=WebRejectionReason.TRANSPORT_ERROR,
                    detail=f"{type(exc).__name__}: {exc}", depth=depth,
                )
            )
            return

        # F-2: the fetcher validated every *hop*; nobody has yet vouched for
        # the final URL's path carrying delimiters this crawler refuses in a
        # discovered href. Rejected before acceptance and before link
        # expansion, with the URL preserved exactly as reported.
        final_url = fetched.final_url or url
        if _ENCODED_PATH_DELIMITERS.search(urlsplit(final_url).path or ""):
            state.refusals.append(
                CrawlRefusal(
                    url=final_url, reason=CrawlRejectionReason.MALFORMED_HREF,
                    detail=(
                        f"redirect of '{url}' landed on '{final_url}', whose "
                        "path carries an encoded delimiter; refused before "
                        "acceptance — the URL is shown as reported, not decoded"
                    ),
                    depth=depth, source="redirect",
                )
            )
            return

        if fetched.size_bytes > state.effective_ceiling:
            state.refusals.append(
                CrawlRefusal(
                    url=url, reason=WebRejectionReason.TOO_LARGE,
                    detail=(
                        f"{fetched.size_bytes:,} bytes exceeds the crawl "
                        f"ceiling {state.effective_ceiling:,}"
                    ),
                    depth=depth,
                )
            )
            return

        # Link expansion runs *before* content acceptance, on purpose.
        # Declining a page as evidence (a stub, a wall, a duplicate) is a
        # judgement about its bytes; refusing to traverse it would also be a
        # judgement about the site's graph — and a cookie-walled home page is
        # still where the real links hang. Duplicates are harmless here: the
        # same hrefs are already in the seen-set from the page that produced
        # them, so enqueue-time dedup makes that expansion cost nothing
        # beyond the parse. A redirect onto a different host is the one thing
        # that does stop expansion: it was validated by the fetcher's safety
        # policy, so the page is citable — but it is not the seed's site,
        # and following *its* links would let one pinned host launder the
        # whole web. BFS expansion stays on the seed host.
        final_host = (urlsplit(final_url).hostname or "").lower()
        if (
            depth < state.seed.max_depth
            and fetched.content_class.value == _HTML_CLASS_VALUE
            and final_host == state.seed_host
        ):
            self._expand_links(state, fetched, page_url=final_url, depth=depth)

        reference = self._accept_page(state, fetched, url=url, depth=depth)
        if reference is None:
            return
        state.pages.append(reference)

    # ===================================================================
    # One page: extract, assess, dedup, reference
    # ===================================================================
    def _accept_page(
        self,
        state: _CrawlState,
        fetched: FetchedDocument,
        *,
        url: str,
        depth: int,
    ) -> WebDocumentRef | None:
        """Extract → assess → dedup → :class:`WebDocumentRef`, or a refusal.

        The same three judgements the Phase-1 service makes before it would
        persist — made without the fourth: this method has no session, no
        cache and no ingestion service to call, so "no persistence inside
        the crawler" is structural, not a promise.
        """
        refusals = state.refusals
        try:
            extracted = extract_page(
                fetched.content,
                url=fetched.final_url or url,
                content_class=fetched.content_class,
                charset=fetched.charset,
                filename=_filename_for(url, fetched),
            )
        except Exception as exc:  # noqa: BLE001 - an extraction failure is a refusal
            refusals.append(
                CrawlRefusal(
                    url=url, reason=WebRejectionReason.UNSUPPORTED_CONTENT,
                    detail=f"extraction failed ({type(exc).__name__}: {exc})",
                    depth=depth,
                )
            )
            return None

        assessment = assess(
            extracted.text, policy=self.policy, content_class=fetched.content_class,
        )
        if assessment.rejected:
            refusals.append(
                CrawlRefusal(
                    url=url, reason=assessment.reason,
                    detail=assessment.detail, depth=depth,
                )
            )
            return None

        # Exact bytes first (one content hash is one page), then the
        # near-duplicate key — the same two-stage content judgement the
        # ingest path makes, scoped to this run: a run-level map costs
        # nothing, and a cross-run map would be a second cache
        # implementation.
        if fetched.sha256 in state.content_hashes:
            refusals.append(
                CrawlRefusal(
                    url=url, reason=WebRejectionReason.DUPLICATE_CONTENT,
                    detail="identical bytes already fetched in this crawl",
                    depth=depth,
                )
            )
            return None
        key = near_duplicate_key(extracted.text)
        if key in state.near_seen:
            refusals.append(
                CrawlRefusal(
                    url=url, reason=WebRejectionReason.DUPLICATE_CONTENT,
                    detail=(
                        f"same text as '{state.near_seen[key]}' already "
                        "accepted in this crawl"
                    ),
                    depth=depth,
                )
            )
            return None
        state.content_hashes.add(fetched.sha256)
        state.near_seen[key] = url

        title = extracted.title or fetched.host or state.seed_host
        clean = build_clean_html(
            title=title,
            text=extracted.text,
            source_url=url,
            retrieved_at=fetched.retrieved_at,
            published_at=extracted.published_at,
            author=extracted.author,
        )
        seed = state.seed
        return WebDocumentRef(
            url=url,
            canonical_url=canonicalize_url(extracted.canonical_url or url),
            host=fetched.host or state.seed_host,
            # ``classify`` still decides; the caller merely states which
            # hosts the company's records own. An exchange or regulator
            # origin keeps its stronger class even when seeded directly.
            source_class=classify(
                fetched.host or state.seed_host,
                company_hosts=frozenset(seed.company_hosts),
                ir_hosts=frozenset(seed.ir_hosts),
            ),
            content_class=fetched.content_class,
            title=title,
            retrieved_at=fetched.retrieved_at,
            published_at=extracted.published_at,
            author=extracted.author,
            content_hash=hashlib.sha256(clean).hexdigest(),
            size_bytes=len(clean),
            status_code=fetched.status_code,
            final_url=fetched.final_url,
            document_id=None,
            preview=collapse_whitespace(extracted.text)[
                : self.policy.citation_value_chars
            ],
        )

    # ===================================================================
    # Links
    # ===================================================================
    def _expand_links(
        self, state: _CrawlState, fetched: FetchedDocument, *, page_url: str, depth: int
    ) -> None:
        """Collect, judge and enqueue one page's links.

        Judgements run in the order their costs run: a raw-string check
        (H1) before canonicalization, host equality before the seen-set,
        the query budget at the last moment before the queue. Nothing is
        fetched here; fetching happens when the queue drains to it, and the
        fetcher re-checks robots and safety per URL at that time.
        """
        hrefs = _page_hrefs(fetched.text, limit=state.seed.max_links_per_page)
        for raw in hrefs:
            state_refusal = self._consider_link(state, raw, base_url=page_url, depth=depth)
            if state_refusal is not None:
                state.refusals.append(state_refusal)

    def _consider_link(
        self, state: _CrawlState, raw: str, *, base_url: str, depth: int
    ) -> CrawlRefusal | None:
        """Judge one href. ``None`` keeps it (or drops it silently); a
        refusal declines it *as a finding*.

        Silent drops — in-page fragments, non-web schemes, off-origin ports —
        are noise: recording each would let a footer of ``#anchors`` drown
        the refusals an operator actually reads. A refusal, by contrast, is
        something a caller might act on: an offsite link or a malformed href
        says something about the site.
        """
        href = (raw or "").strip()
        if not href or _FRAGMENT_ONLY.match(href):
            return None

        scheme_match = _HREF_SCHEME.match(href)
        if scheme_match and scheme_match.group(1).lower() in _NON_WEB_SCHEMES:
            return None

        # Resolve first, then judge the resolved form: the base URL is
        # itself validated and same-origin, so urljoin cannot invent reach.
        joined = urljoin(base_url, href)
        parts = urlsplit(joined)
        if (parts.scheme or "").lower() not in ALLOWED_SCHEMES:
            return None

        # H1, on the resolved path, as written — before canonicalization
        # (which must not get the chance to "tidy" it) and before the
        # seen-set (a malformed URL is a finding, not a duplicate).
        if _ENCODED_PATH_DELIMITERS.search(parts.path or ""):
            return CrawlRefusal(
                url=joined, reason=CrawlRejectionReason.MALFORMED_HREF,
                detail=(
                    "path carries an encoded delimiter (%2e, %2f or %5c); "
                    "refused as written — discovery does not decode links to "
                    "judge them"
                ),
                depth=depth,
            )

        host = (parts.hostname or "").lower()
        if host != state.seed_host:
            return CrawlRefusal(
                url=joined, reason=CrawlRejectionReason.OFFSITE_LINK,
                detail=(
                    f"host '{host or '(none)'}' is not the seed host "
                    f"'{state.seed_host}'; strict same-host crawling does not "
                    "follow it"
                ),
                depth=depth,
            )
        try:
            port = parts.port
        except ValueError:
            return None  # unparseable port: noise, not content
        if port is not None and f"{(parts.scheme or '').lower()}://{parts.netloc.lower()}" != state.seed_origin:
            return None

        canonical = canonicalize_url(joined)
        if not canonical or canonical == state.seed_url:
            return None  # the seen-set catches the rest at enqueue

        query = urlsplit(canonical).query
        if query:
            if state.query_budget <= 0:
                return CrawlRefusal(
                    url=canonical, reason=CrawlRejectionReason.QUERY_BUDGET_EXCEEDED,
                    detail=(
                        "URL still carries a query string after "
                        "canonicalization and this crawl's query budget is "
                        "exhausted — the wall that keeps pagination from "
                        "outrunning max_pages"
                    ),
                    depth=depth,
                )
            state.query_budget -= 1
            # A query-bearing URL spends budget *and* must still clear
            # dedup; the decrement is taken now because the point of the
            # budget is to bound enqueued query URLs, not examined ones.

        state.enqueue(canonical, depth + 1)
        return None

    # ===================================================================
    # Sitemap
    # ===================================================================
    def _discover_sitemap(self, state: _CrawlState) -> str:
        """One ``/sitemap.xml`` read. Returns the status string.

        Bounded and non-recursive by contract: only the origin's own sitemap
        path is probed, only ``<url><loc>`` entries within the seed's budget
        are enqueued (at depth 0, siblings of the seed), and a
        ``<sitemapindex>`` is recorded and stopped at — following child
        sitemaps would be sitemap-driven recursion, which is how "bounded"
        quietly becomes "however many files the site names". The probe
        consumes one unit of ``max_pages`` like any other request.
        """
        seed = state.seed
        parts = urlsplit(state.seed_url)
        sitemap_url = (
            f"{(parts.scheme or 'https').lower()}://"
            f"{(parts.netloc or '').lower()}{_SITEMAP_PATH}"
        )
        if sitemap_url in state.seen:
            return "skipped"
        state.seen.add(sitemap_url)
        state.fetched += 1
        try:
            fetched = self.fetcher.fetch(sitemap_url)
        except WebFetchError as exc:
            state.refusals.append(
                CrawlRefusal(
                    url=sitemap_url, reason=CrawlRejectionReason.SITEMAP_UNAVAILABLE,
                    detail=f"sitemap probe refused: {exc.detail}", source="sitemap",
                )
            )
            return "unavailable"
        except Exception as exc:  # noqa: BLE001 - the crawl proceeds without it
            state.refusals.append(
                CrawlRefusal(
                    url=sitemap_url, reason=CrawlRejectionReason.SITEMAP_UNAVAILABLE,
                    detail=f"sitemap probe failed ({type(exc).__name__}: {exc})",
                    source="sitemap",
                )
            )
            return "unavailable"

        if fetched.size_bytes > state.effective_ceiling:
            state.refusals.append(
                CrawlRefusal(
                    url=sitemap_url, reason=WebRejectionReason.TOO_LARGE,
                    detail="sitemap body exceeds the crawl ceiling",
                    source="sitemap",
                )
            )
            return "unavailable"

        try:
            root = ElementTree.fromstring(fetched.content)
        except ElementTree.ParseError as exc:
            state.refusals.append(
                CrawlRefusal(
                    url=sitemap_url, reason=CrawlRejectionReason.SITEMAP_UNAVAILABLE,
                    detail=f"sitemap XML unparseable ({exc})", source="sitemap",
                )
            )
            return "unavailable"

        if root.tag.rsplit("}", 1)[-1] == "sitemapindex":
            for element in root.iter():
                if element.tag.rsplit("}", 1)[-1] != "loc":
                    continue
                state.refusals.append(
                    CrawlRefusal(
                        url=(element.text or "").strip() or "(empty loc)",
                        reason=CrawlRejectionReason.CHILD_SITEMAP_SKIPPED,
                        detail=(
                            "child sitemap of a sitemapindex; bounded sitemap "
                            "discovery does not recurse into child files"
                        ),
                        source="sitemap",
                    )
                )
            return "index"

        enqueued = 0
        overflow = 0
        for element in root.iter():
            if element.tag.rsplit("}", 1)[-1] != "loc":
                continue
            raw = (element.text or "").strip()
            if not raw:
                continue
            refusal = self._consider_sitemap_loc(state, raw)
            if refusal is not None:
                state.refusals.append(refusal)
                continue
            canonical = canonicalize_url(raw)
            if canonical in state.seen:
                state.deduplicated += 1
                continue
            if enqueued >= seed.max_sitemap_urls:
                overflow += 1
                continue
            state.enqueue(canonical, 0)
            enqueued += 1
        if overflow:
            state.refusals.append(
                CrawlRefusal(
                    url=sitemap_url, reason=CrawlRejectionReason.SITEMAP_CAP_REACHED,
                    detail=(
                        f"{overflow} further <loc> entries beyond the "
                        f"{seed.max_sitemap_urls}-entry sitemap cap were not "
                        "enqueued"
                    ),
                    source="sitemap",
                )
            )
        return "ok"

    def _consider_sitemap_loc(self, state: _CrawlState, raw: str) -> CrawlRefusal | None:
        """Every candidate check that applies to a ``<loc>``, in cost order."""
        parts = urlsplit(raw)
        if (parts.scheme or "").lower() not in ALLOWED_SCHEMES or not parts.netloc:
            return CrawlRefusal(
                url=raw, reason=CrawlRejectionReason.SITEMAP_LOC_MALFORMED,
                detail=(
                    "<loc> must be an absolute http(s) URL; relative and "
                    "non-web locations are refused as written"
                ),
                source="sitemap",
            )
        if _ENCODED_PATH_DELIMITERS.search(parts.path or ""):
            # H1, applied verbatim to sitemap paths.
            return CrawlRefusal(
                url=raw, reason=CrawlRejectionReason.MALFORMED_HREF,
                detail=(
                    "sitemap <loc> path carries an encoded delimiter "
                    "(%2e, %2f or %5c); refused as written"
                ),
                source="sitemap",
            )
        host = (parts.hostname or "").lower()
        if host != state.seed_host:
            return CrawlRefusal(
                url=raw, reason=CrawlRejectionReason.OFFSITE_LINK,
                detail=(
                    f"host '{host or '(none)'}' is not the seed host "
                    f"'{state.seed_host}'; the sitemap of one origin does not "
                    "extend the crawl to another"
                ),
                source="sitemap",
            )
        try:
            if parts.port is not None and (
                f"{(parts.scheme or '').lower()}://{parts.netloc.lower()}"
                != state.seed_origin
            ):
                return CrawlRefusal(
                    url=raw, reason=CrawlRejectionReason.OFFSITE_LINK,
                    detail="different origin (port) than the seed",
                    source="sitemap",
                )
        except ValueError:
            return CrawlRefusal(
                url=raw, reason=CrawlRejectionReason.SITEMAP_LOC_MALFORMED,
                detail="unparseable port in <loc>", source="sitemap",
            )
        return None


def _page_hrefs(html_text: str, *, limit: int) -> list[str]:
    """Raw ``<a href>`` strings of one page, in document order, capped.

    A fresh parse of the fetched body, mirroring
    :func:`app.services.web.extract.extract_html`: ``bs4`` is imported
    lazily, the same as the extraction layer imports it, so an environment
    without it still imports this module and degrades to no-link-expansion
    rather than failing at import time. A page that will not parse yields no
    links; it has already been through ``extract_page`` to be here at all.
    """
    if not html_text:
        return []
    try:
        from bs4 import BeautifulSoup
    except ImportError:  # pragma: no cover - bs4 is in requirements
        log.warning("bs4 unavailable; link expansion disabled")
        return []
    try:
        soup = BeautifulSoup(html_text, "html.parser")
        anchors = soup.find_all("a")
    except Exception as exc:  # noqa: BLE001 - a broken page is not a crash
        log.info("link parse failed", error=type(exc).__name__)
        return []
    hrefs: list[str] = []
    for tag in anchors:
        raw = tag.get("href")
        if isinstance(raw, str):
            hrefs.append(raw)
            if len(hrefs) >= limit:
                break
    return hrefs


def _filename_for(url: str, fetched: FetchedDocument) -> str:
    """A filename hint for the PDF adapter only.

    The extension follows the content class, never the URL — the same rule
    the service states out loud, kept local in its minimal form because the
    only consumer here is ``extract_pdf``'s format hint.
    """
    last = unquote(urlsplit(url or "").path).rsplit("/", 1)[-1]
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", last).strip("._")[:60] or "web_page"
    suffix = _SUFFIXES.get(fetched.content_class.value, ".html")
    if stem.lower().endswith(suffix):
        stem = stem[: -len(suffix)]
    return f"{stem}{suffix}"[:120]


__all__ = [
    "CrawlPolicyError",
    "CrawlRejectionReason",
    "CrawlRefusal",
    "CrawlReport",
    "CrawlSeed",
    "WebCrawlerDiscovery",
    "seed_from_url",
]
