"""Targeted, on-demand web discovery for one safely resolved company.

Part 3, Phase 4C. Sits between the self-owned local web index (Phase 4B)
and the existing web fetch stack, and does exactly one thing: when the
local index does not hold enough evidence for a question, fetch a *small,
fixed, allowlisted* set of pages on the company's own origins and return
them in the same candidate contract the local index uses.

    question → question planner → web query generator (Phase 4B)
             → local web index (Phase 4B)  ── sufficient ──▶ done, nothing fetched
             → TargetedWebDiscovery (this module)
             → WebSearchService.search(candidate_urls=seeds)
                 → UrlSafetyPolicy → RobotsPolicy → HostPoliteness
                 → WebFetcher (redirect re-validation, byte and MIME limits)
                 → extract_page → assess (quality) → optional ingestion
             → WebEvidenceCandidate(origin=live_fetch_*) merged with local

**What this module deliberately is not.** It is not a search engine, not a
crawler and not a query generator. It never follows a link, never derives
a domain from the words of a question, never calls an external search
API or a language model, and never talks to a socket itself: every fetch
goes through :class:`~app.services.web.service.WebSearchService`, which
already owns the allowlist, the safety and robots checks, the fetcher,
extraction, quality assessment, deduplication and persistence. Reusing it
means there is one place where "may the platform read this URL?" is
decided.

Targets, in the order the platform trusts them
----------------------------------------------
1. The company's own website (``Company.website``).
2. Its *verified* investor-relations URL (``CompanyCrawlState.ir_url`` with
   ``ir_url_confidence`` at or above the platform's verified threshold). An
   unverified IR URL is a guess produced by IR discovery from the company's
   name — exactly the kind of derived domain this layer must not fetch — so
   it is reported, not seeded.
3. Exchange and regulator sources the project already holds: the corporate
   announcements the filing crawl has *already discovered* for the company
   (``DiscoveredFiling`` rows). They are surfaced as references, never
   fetched here — the filing pipeline owns exchange formats, downloads and
   ingestion, and this layer must not become a second exchange client.
4. Approved financial-media hosts — only from an allowlist that already
   exists in the project. ``quality.MEDIA_HOSTS`` is empty by design, so no
   media host is ever a target; the code path is kept honest by refusing
   anything not pinned rather than by a comment.

Paths appended to an origin come only from the service's fixed
conventional-path table (``/investors``, ``/investors/results``,
``/press-releases`` ...), selected — never composed — by the query text.

Sufficiency policy (deterministic, documented, tested)
-----------------------------------------------------
Local evidence is checked first and live discovery runs only when, for the
configured :class:`DiscoveryPolicy`:

* ``ABSENT``       — the local index returned no candidate at all;
* ``INSUFFICIENT`` — fewer than ``min_local_candidates`` local candidates
  reach ``min_local_relevance``;
* ``STALE``        — the question is recency-sensitive ("latest", "recent",
  "आज" ...) and the freshest local candidate (publication date, else
  retrieval date) is older than ``max_local_age_days`` or undated.

Otherwise the local evidence is ``SUFFICIENT`` and nothing is fetched.
Recency sensitivity is an explicit input (the query generator reports it);
it is never inferred here.

Persistence
-----------
The result states exactly what happened to each page. With
``persist=True`` (the service's own default) an accepted page is ingested
through the existing ``web_page`` path — the same table, storage and
content-hash skip the nightly crawl uses — and the candidate carries the
document id with ``origin == "live_fetch_persisted"``. With
``persist=False`` nothing is written and the candidate is transient:
``document_id is None`` and ``origin == "live_fetch_transient"``. No new
table, migration or store is introduced.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from enum import StrEnum
from typing import Any, Callable, Sequence
from urllib.parse import urljoin

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.data.filings.base import recency_factor
from app.domain.filings.collection import CollectionStatus
from app.domain.web.types import (
    WebDocumentRef,
    WebFetchPolicy,
    WebSearchQuery,
    WebSearchResult,
    WebSourceClass,
)
from app.models.company import Company
from app.models.filing_collection import CompanyCrawlState, DiscoveredFiling
from app.services.web.extract import canonicalize_url
from app.services.web.index import (
    WebEvidenceCandidate,
    WebIndexSearchResult,
    blend_score,
    dedupe_candidates,
    freshness_basis,
    iter_query_texts,
    rank_key,
)
from app.services.web.quality import (
    EXCHANGE_HOSTS,
    MEDIA_HOSTS,
    REGULATOR_HOSTS,
    web_authority,
)
from app.services.web.service import (
    WebEvidenceError,
    WebSearchService,
    host_of,
    origin_of,
)

log = structlog.get_logger(__name__)

__all__ = [
    "EXCHANGE_REFERENCE_ORIGIN",
    "LIVE_FETCH_PERSISTED_ORIGIN",
    "LIVE_FETCH_TRANSIENT_ORIGIN",
    "DiscoveryPolicy",
    "DiscoveryRefusal",
    "DiscoverySeed",
    "DiscoveryStatus",
    "DiscoveryTrigger",
    "SufficiencyDecision",
    "TargetedDiscoveryResult",
    "TargetedWebDiscovery",
    "assess_local_evidence",
    "merge_candidates",
]

# ---------------------------------------------------------------------------
# Origins a candidate may carry (see ``WebEvidenceCandidate.origin``)
# ---------------------------------------------------------------------------
#: A page fetched now and ingested through the existing ``web_page`` path.
LIVE_FETCH_PERSISTED_ORIGIN = "live_fetch_persisted"
#: A page fetched now and deliberately not written anywhere.
LIVE_FETCH_TRANSIENT_ORIGIN = "live_fetch_transient"
#: An exchange/regulator announcement the filing crawl already discovered.
#: Nothing was fetched; the candidate points at what the platform holds.
EXCHANGE_REFERENCE_ORIGIN = "exchange_reference"

#: Signal recorded on every live-fetched candidate.
LIVE_FETCH_SIGNAL = "live_fetch"
EXCHANGE_REFERENCE_SIGNAL = "exchange_reference"

_TOKEN = re.compile(r"[0-9A-Za-z\u0900-\u097F][0-9A-Za-z\u0900-\u097F&/\-]*")
#: Function words that carry no topic. Kept tiny and explicit: relevance
#: here is a transparent overlap count, not a language model.
_STOPWORDS = frozenset({
    "the", "and", "for", "with", "from", "that", "this", "what", "how",
    "why", "when", "are", "was", "were", "has", "have", "does", "did",
    "its", "their", "about", "into", "over", "any", "all", "not",
    "ka", "ki", "ke", "ko", "se", "me", "hai", "hain", "kya", "kaun",
    "क्या", "का", "की", "के", "को", "से", "में", "है", "हैं", "और",
})
_ROUND = 6
_WHITESPACE = re.compile(r"\s+")
#: How many of a company's most recent discovered filings are scanned for
#: query overlap. A bound on the read, not on what is returned.
_EXCHANGE_SCAN_ROWS = 200


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class DiscoveryPolicy:
    """Every bound this layer honours, with a hard ceiling on each.

    Fetch-level limits — bytes per page, redirects, timeouts, attempts,
    allowed ports — are the existing :class:`WebFetchPolicy`'s and are not
    redefined here; ``fetch_policy`` passes one through and
    ``max_bytes_per_page`` may only *tighten* its byte cap.
    """

    #: Query texts consulted for path selection and relevance.
    max_queries: int = 4
    #: Conventional paths appended to each company origin.
    max_paths_per_origin: int = 3
    #: Candidate URLs planned in total (website, verified IR).
    max_seed_urls: int = 6
    #: Pages actually attempted (a prefix of the seeds).
    max_fetched_pages: int = 4
    #: Candidates returned after merging with local evidence.
    max_results: int = 6
    #: Link-following depth. Always 0: the seeds *are* the crawl. Following
    #: links is the background crawl job's business, under its own bounds.
    max_depth: int = 0
    #: Already-discovered exchange announcements surfaced as references.
    max_exchange_references: int = 4
    #: Announcements older than this are not offered as current evidence.
    exchange_reference_max_age_days: int = 365

    # -- sufficiency (see module docstring) -----------------------------
    min_local_candidates: int = 2
    min_local_relevance: float = 0.35
    max_local_age_days: int = 7

    #: ``False`` turns this layer into a planner: seeds and references are
    #: computed, nothing is fetched. The global kill switch
    #: (``settings.WEB_EVIDENCE_ENABLED``) is checked independently.
    allow_live_fetch: bool = True
    #: Whether accepted pages are ingested as ``web_page`` documents. Mirrors
    #: ``WebSearchQuery.persist``'s default so the existing contract is not
    #: silently changed; a caller that wants transient evidence says so.
    persist: bool = True
    fetch_policy: WebFetchPolicy | None = None
    max_bytes_per_page: int | None = None

    MAX_QUERIES_CEILING = 8
    MAX_PATHS_CEILING = 6
    MAX_SEEDS_CEILING = 16
    MAX_FETCH_CEILING = 8
    MAX_RESULTS_CEILING = 16
    MAX_DEPTH_CEILING = 0
    MAX_EXCHANGE_CEILING = 10

    def __post_init__(self) -> None:
        def bound(name: str, value: int, ceiling: int, floor: int = 1) -> None:
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError(f"{name} must be an integer")
            if value < floor or value > ceiling:
                raise ValueError(
                    f"{name} must be between {floor} and {ceiling}, got {value}"
                )

        bound("max_queries", self.max_queries, self.MAX_QUERIES_CEILING)
        bound("max_paths_per_origin", self.max_paths_per_origin, self.MAX_PATHS_CEILING)
        bound("max_seed_urls", self.max_seed_urls, self.MAX_SEEDS_CEILING)
        bound("max_fetched_pages", self.max_fetched_pages, self.MAX_FETCH_CEILING)
        bound("max_results", self.max_results, self.MAX_RESULTS_CEILING)
        bound("max_depth", self.max_depth, self.MAX_DEPTH_CEILING, floor=0)
        bound("max_exchange_references", self.max_exchange_references,
              self.MAX_EXCHANGE_CEILING, floor=0)
        bound("exchange_reference_max_age_days",
              self.exchange_reference_max_age_days, 3650, floor=1)
        bound("min_local_candidates", self.min_local_candidates, 50)
        bound("max_local_age_days", self.max_local_age_days, 3650, floor=0)
        if not 0.0 <= float(self.min_local_relevance) <= 1.0:
            raise ValueError("min_local_relevance must be within [0, 1]")
        if self.max_bytes_per_page is not None:
            ceiling = (self.fetch_policy or WebFetchPolicy()).max_bytes
            if self.max_bytes_per_page < 1 or self.max_bytes_per_page > ceiling:
                raise ValueError(
                    "max_bytes_per_page may only tighten the fetch policy "
                    f"(1..{ceiling}), got {self.max_bytes_per_page}"
                )

    def effective_fetch_policy(self) -> WebFetchPolicy | None:
        """The fetch policy handed to the service, byte cap tightened if asked."""
        if self.max_bytes_per_page is None:
            return self.fetch_policy
        base = self.fetch_policy or WebFetchPolicy()
        return replace(base, max_bytes=min(base.max_bytes, self.max_bytes_per_page))

    def as_dict(self) -> dict[str, Any]:
        return {
            "max_queries": self.max_queries,
            "max_paths_per_origin": self.max_paths_per_origin,
            "max_seed_urls": self.max_seed_urls,
            "max_fetched_pages": self.max_fetched_pages,
            "max_results": self.max_results,
            "max_depth": self.max_depth,
            "max_exchange_references": self.max_exchange_references,
            "exchange_reference_max_age_days": self.exchange_reference_max_age_days,
            "min_local_candidates": self.min_local_candidates,
            "min_local_relevance": self.min_local_relevance,
            "max_local_age_days": self.max_local_age_days,
            "allow_live_fetch": self.allow_live_fetch,
            "persist": self.persist,
            "max_bytes_per_page": (
                self.effective_fetch_policy() or WebFetchPolicy()
            ).max_bytes,
        }


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------
class DiscoveryTrigger(StrEnum):
    """Why local evidence was judged insufficient."""

    ABSENT = "local_absent"
    INSUFFICIENT = "local_insufficient"
    STALE = "local_stale"
    FORCED = "forced"


class DiscoveryStatus(StrEnum):
    """What this layer did."""

    #: Local evidence met the policy; nothing was planned or fetched.
    LOCAL_SUFFICIENT = "local_sufficient"
    #: The web evidence kill switch is off; nothing touched the network.
    DISABLED = "disabled"
    #: No queries were supplied; nothing to select paths or relevance with.
    NO_QUERIES = "no_queries"
    #: No safely resolved company, an unknown company id, or a company
    #: with no usable own origin. No seed is ever built from a name.
    NO_TARGET = "insufficient_discovery_target"
    #: Seeds and references were planned but live fetching is off.
    PLANNED_ONLY = "planned_only"
    #: Discovery ran and produced at least one candidate.
    DISCOVERED = "discovered"
    #: Discovery ran; every seed was declined and no reference matched.
    NOTHING_FOUND = "nothing_found"


@dataclass(frozen=True, slots=True)
class SufficiencyDecision:
    """The documented gate, with the numbers it was decided on."""

    trigger: DiscoveryTrigger | None
    reason: str
    local_candidates: int
    strong_candidates: int
    freshest_evidence_at: datetime | None
    recency_sensitive: bool

    @property
    def discover(self) -> bool:
        return self.trigger is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "trigger": self.trigger.value if self.trigger else None,
            "discover": self.discover,
            "reason": self.reason,
            "local_candidates": self.local_candidates,
            "strong_candidates": self.strong_candidates,
            "freshest_evidence_at": _iso(self.freshest_evidence_at),
            "recency_sensitive": self.recency_sensitive,
        }


@dataclass(frozen=True, slots=True)
class DiscoverySeed:
    """One URL this layer intends to hand to the service."""

    url: str
    host: str
    source_class: str
    #: ``company_website`` or ``verified_ir`` — which allowlisted target
    #: the seed belongs to.
    target: str
    #: How the URL was formed: ``verified_ir_url`` or ``path:<path>``.
    basis: str
    #: The query whose path hint selected it (``""`` for the IR URL itself).
    query: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "url": self.url, "host": self.host, "source_class": self.source_class,
            "target": self.target, "basis": self.basis, "query": self.query,
        }


@dataclass(frozen=True, slots=True)
class DiscoveryRefusal:
    """A URL that was not fetched, or was fetched and declined, and why."""

    url: str
    reason: str
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"url": self.url, "reason": self.reason, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class TargetedDiscoveryResult:
    """Everything one :meth:`TargetedWebDiscovery.discover` call decided."""

    status: DiscoveryStatus
    decision: SufficiencyDecision
    company_id: str | None
    queries: tuple[str, ...]
    #: Planned seeds, in fetch order (a prefix of ``max_fetched_pages`` is
    #: attempted).
    seeds: tuple[DiscoverySeed, ...] = ()
    #: Live-fetched pages the service accepted.
    fetched: tuple[WebEvidenceCandidate, ...] = ()
    #: Already-held exchange/regulator announcements matching the queries.
    exchange_references: tuple[WebEvidenceCandidate, ...] = ()
    refusals: tuple[DiscoveryRefusal, ...] = ()
    #: Local candidates first, discovered ones merged in, deduplicated by
    #: canonical URL / content hash / document id, ranked and capped.
    merged: tuple[WebEvidenceCandidate, ...] = ()
    pages_attempted: int = 0
    #: How many discovered candidates were dropped as duplicates.
    deduplicated: int = 0
    #: Whether accepted pages were ingested (``persist``) or kept transient.
    persisted: bool = False
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def discovered(self) -> tuple[WebEvidenceCandidate, ...]:
        return tuple(self.fetched) + tuple(self.exchange_references)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "decision": self.decision.as_dict(),
            "company_id": self.company_id,
            "queries": list(self.queries),
            "seeds": [s.as_dict() for s in self.seeds],
            "fetched": [c.as_dict() for c in self.fetched],
            "exchange_references": [c.as_dict() for c in self.exchange_references],
            "refusals": [r.as_dict() for r in self.refusals],
            "merged": [c.as_dict() for c in self.merged],
            "pages_attempted": self.pages_attempted,
            "deduplicated": self.deduplicated,
            "persisted": self.persisted,
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------
# Sufficiency (pure)
# ---------------------------------------------------------------------------
def assess_local_evidence(
    local: WebIndexSearchResult | None,
    *,
    policy: DiscoveryPolicy,
    recency_sensitive: bool,
    now: datetime,
) -> SufficiencyDecision:
    """Apply the documented sufficiency policy to a local index result.

    Pure: no I/O, no clock of its own. See the module docstring for the
    rules; this is their only implementation.
    """
    candidates = tuple(getattr(local, "candidates", ()) or ()) if local is not None else ()
    strong = [
        c for c in candidates
        if float(getattr(c, "relevance", 0.0) or 0.0) >= policy.min_local_relevance
    ]
    dates = [d for d in (_evidence_date(c) for c in strong) if d is not None]
    freshest = max(dates) if dates else None

    if not candidates:
        return SufficiencyDecision(
            trigger=DiscoveryTrigger.ABSENT,
            reason="the local web index returned no candidate",
            local_candidates=0, strong_candidates=0,
            freshest_evidence_at=None, recency_sensitive=recency_sensitive,
        )
    if len(strong) < policy.min_local_candidates:
        return SufficiencyDecision(
            trigger=DiscoveryTrigger.INSUFFICIENT,
            reason=(
                f"{len(strong)} local candidate(s) reach relevance "
                f"{policy.min_local_relevance:g}; the policy needs "
                f"{policy.min_local_candidates}"
            ),
            local_candidates=len(candidates), strong_candidates=len(strong),
            freshest_evidence_at=freshest, recency_sensitive=recency_sensitive,
        )
    if recency_sensitive:
        window = timedelta(days=policy.max_local_age_days)
        if freshest is None:
            return SufficiencyDecision(
                trigger=DiscoveryTrigger.STALE,
                reason=(
                    "the question is recency-sensitive and no strong local "
                    "candidate carries a publication or retrieval date"
                ),
                local_candidates=len(candidates), strong_candidates=len(strong),
                freshest_evidence_at=None, recency_sensitive=True,
            )
        age = _as_utc(now) - freshest
        if age > window:
            return SufficiencyDecision(
                trigger=DiscoveryTrigger.STALE,
                reason=(
                    f"the question is recency-sensitive and the freshest strong "
                    f"local candidate is {age.days} day(s) old; the policy window "
                    f"is {policy.max_local_age_days} day(s)"
                ),
                local_candidates=len(candidates), strong_candidates=len(strong),
                freshest_evidence_at=freshest, recency_sensitive=True,
            )
    return SufficiencyDecision(
        trigger=None,
        reason=(
            f"{len(strong)} local candidate(s) reach relevance "
            f"{policy.min_local_relevance:g}"
            + (
                f" and the freshest is within {policy.max_local_age_days} day(s)"
                if recency_sensitive else ""
            )
        ),
        local_candidates=len(candidates), strong_candidates=len(strong),
        freshest_evidence_at=freshest, recency_sensitive=recency_sensitive,
    )


# ---------------------------------------------------------------------------
# Merge (pure)
# ---------------------------------------------------------------------------
def merge_candidates(
    local: Sequence[WebEvidenceCandidate],
    discovered: Sequence[WebEvidenceCandidate],
    *,
    limit: int,
) -> tuple[tuple[WebEvidenceCandidate, ...], int]:
    """Local evidence first, discovered evidence merged in, one per page.

    Identity is decided the way the index decides it (canonical URL,
    content hash, document id). A discovered page whose bytes the index
    already holds is dropped in favour of the stored copy — that copy has
    a passage-level snippet and a document to cite; a page whose URL is
    held but whose bytes changed replaces the older snapshot, because the
    page now reads differently. Returns the ranked, capped list and how
    many discovered candidates were dropped as duplicates.
    """
    local_list = list(local)
    local_hashes = {c.content_hash for c in local_list if c.content_hash}
    unique_discovered: list[WebEvidenceCandidate] = []
    dropped = 0
    for candidate in discovered:
        if candidate.content_hash and candidate.content_hash in local_hashes:
            dropped += 1
            continue
        unique_discovered.append(candidate)
    merged = dedupe_candidates(local_list + unique_discovered)
    dropped += len(local_list) + len(unique_discovered) - len(merged)
    ranked = sorted(merged, key=rank_key)[: max(0, int(limit))]
    return tuple(ranked), dropped


# ---------------------------------------------------------------------------
# The layer
# ---------------------------------------------------------------------------
class TargetedWebDiscovery:
    """Bounded, allowlisted, on-demand discovery on top of the web service.

    ``search_service`` is the seam: the production wiring builds a
    :class:`WebSearchService` over the session, and tests inject one whose
    transport, resolver and robots fetch are fakes — the safety, robots,
    politeness, redirect, byte, MIME, extraction and quality paths stay
    real either way, because they live inside the service and its fetcher.
    """

    def __init__(
        self,
        db: Session,
        *,
        search_service: WebSearchService | None = None,
        policy: DiscoveryPolicy | None = None,
        clock: Callable[[], datetime] | None = None,
        enabled: bool | None = None,
    ) -> None:
        self.db = db
        self.policy = policy or DiscoveryPolicy()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._enabled = enabled
        self._service = search_service

    # ------------------------------------------------------------- wiring
    @property
    def enabled(self) -> bool:
        """The kill switch: the same setting that governs the crawl job."""
        if self._enabled is not None:
            return bool(self._enabled)
        return bool(getattr(settings, "WEB_EVIDENCE_ENABLED", False))

    @property
    def service(self) -> WebSearchService:
        if self._service is None:
            fetch_policy = self.policy.effective_fetch_policy()
            kwargs: dict[str, Any] = {"now": self._clock}
            if fetch_policy is not None:
                kwargs["policy"] = fetch_policy
            self._service = WebSearchService(self.db, **kwargs)
        return self._service

    # ---------------------------------------------------------------- api
    def assess(
        self, local: WebIndexSearchResult | None, *, recency_sensitive: bool = False,
    ) -> SufficiencyDecision:
        """The sufficiency gate on its own (pure)."""
        return assess_local_evidence(
            local, policy=self.policy, recency_sensitive=recency_sensitive,
            now=self._clock(),
        )

    def plan_seeds(
        self, company_id: str | None, queries: Any,
    ) -> tuple[tuple[DiscoverySeed, ...], tuple[DiscoveryRefusal, ...], tuple[str, ...]]:
        """Deterministic seed list for a company. Reads rows; never fetches."""
        texts = self._queries(queries)
        company = self._company(company_id)
        if company is None:
            return (), (), (self._no_target_note(company_id),)
        state, pinned = self.service.allowlist_for(company)
        return self._seeds(company, state, pinned, texts)

    def discover(
        self,
        queries: Any,
        *,
        company_id: str | None,
        local: WebIndexSearchResult | None = None,
        recency_sensitive: bool | None = None,
        persist: bool | None = None,
        force: bool = False,
    ) -> TargetedDiscoveryResult:
        """Local first; a bounded live fetch only when the policy says so.

        ``queries`` is anything :func:`iter_query_texts` understands — the
        generator's query set (duck-typed via ``.texts``), or plain
        strings. ``recency_sensitive`` defaults to the query set's own
        ``recency_sensitive`` flag when it has one, else ``False``.
        """
        texts = self._queries(queries)
        if recency_sensitive is None:
            recency_sensitive = bool(getattr(queries, "recency_sensitive", False))
        persist_pages = self.policy.persist if persist is None else bool(persist)
        local_candidates = tuple(getattr(local, "candidates", ()) or ()) if local else ()
        now = self._clock()

        decision = assess_local_evidence(
            local, policy=self.policy, recency_sensitive=recency_sensitive, now=now,
        )
        if force and not decision.discover:
            decision = replace(
                decision, trigger=DiscoveryTrigger.FORCED,
                reason="discovery forced by the caller; " + decision.reason,
            )

        def finish(status: DiscoveryStatus, **fields: Any) -> TargetedDiscoveryResult:
            merged, dropped = merge_candidates(
                local_candidates,
                tuple(fields.get("fetched", ())) + tuple(fields.get("exchange_references", ())),
                limit=self.policy.max_results,
            )
            return TargetedDiscoveryResult(
                status=status, decision=decision, company_id=company_id,
                queries=tuple(texts), merged=merged, deduplicated=dropped,
                persisted=bool(fields.pop("persisted", False)), **fields,
            )

        if not decision.discover:
            return finish(
                DiscoveryStatus.LOCAL_SUFFICIENT,
                notes=("local evidence is sufficient; nothing was fetched",),
            )
        if not texts:
            return finish(
                DiscoveryStatus.NO_QUERIES,
                notes=("no query text was supplied; nothing was fetched",),
            )
        if not self.enabled:
            return finish(
                DiscoveryStatus.DISABLED,
                notes=("WEB_EVIDENCE_ENABLED is off; nothing was planned or fetched",),
            )

        company = self._company(company_id)
        if company is None:
            return finish(
                DiscoveryStatus.NO_TARGET, notes=(self._no_target_note(company_id),),
            )

        state, pinned = self.service.allowlist_for(company)
        seeds, refusals, notes = self._seeds(company, state, pinned, texts)
        references = self._exchange_references(company, texts, now=now)
        if not seeds and not references:
            return finish(
                DiscoveryStatus.NO_TARGET, refusals=refusals,
                notes=notes + ("no usable company-owned origin and no held "
                               "exchange reference: nothing to fetch",),
            )
        if not self.policy.allow_live_fetch:
            return finish(
                DiscoveryStatus.PLANNED_ONLY, seeds=seeds,
                exchange_references=references, refusals=refusals,
                notes=notes + ("live fetching is disabled by policy",),
            )

        attempted = seeds[: self.policy.max_fetched_pages]
        fetched: list[WebEvidenceCandidate] = []
        if attempted:
            result = self._fetch(company, attempted, texts, persist=persist_pages)
            if isinstance(result, DiscoveryRefusal):
                refusals = refusals + (result,)
                notes = notes + ("the web service refused the seed list; nothing was fetched",)
            else:
                fetched = self._candidates_from(
                    result, company=company, texts=texts, today=now.date(),
                )
                refusals = refusals + tuple(
                    DiscoveryRefusal(url=url, reason=str(getattr(reason, "value", reason)),
                                     detail=dict(result.details).get(url, ""))
                    for url, reason in result.rejected
                )
        if len(seeds) > len(attempted):
            notes = notes + (
                f"{len(seeds) - len(attempted)} seed(s) beyond max_fetched_pages="
                f"{self.policy.max_fetched_pages} were not attempted",
            )
        if persist_pages:
            notes = notes + (
                "accepted pages were ingested as web_page documents "
                "(origin=live_fetch_persisted)",
            )
        else:
            notes = notes + (
                "accepted pages are transient evidence and were not stored "
                "(origin=live_fetch_transient)",
            )
        status = (
            DiscoveryStatus.DISCOVERED if fetched or references
            else DiscoveryStatus.NOTHING_FOUND
        )
        return finish(
            status, seeds=seeds, fetched=tuple(fetched),
            exchange_references=references, refusals=refusals,
            pages_attempted=len(attempted), persisted=persist_pages and bool(fetched),
            notes=notes,
        )

    # ------------------------------------------------------------ targets
    def _company(self, company_id: str | None) -> Company | None:
        if not company_id or not str(company_id).strip():
            return None
        try:
            return self.db.get(Company, str(company_id).strip())
        except Exception:  # noqa: BLE001 - an unavailable table is "no target"
            log.exception("company lookup failed", company_id=company_id)
            return None

    @staticmethod
    def _no_target_note(company_id: str | None) -> str:
        if not company_id:
            return (
                "insufficient discovery target: no safely resolved company; a "
                "company name in the question is not a fetch target and no "
                "domain is derived from it"
            )
        return (
            f"insufficient discovery target: company '{company_id}' is not "
            "known to the platform; no domain is derived from the question"
        )

    def _seeds(
        self,
        company: Company,
        state: CompanyCrawlState | None,
        pinned: dict[str, WebSourceClass],
        texts: Sequence[str],
    ) -> tuple[tuple[DiscoverySeed, ...], tuple[DiscoveryRefusal, ...], tuple[str, ...]]:
        policy = self.policy
        notes: list[str] = []
        refusals: list[DiscoveryRefusal] = []
        seeds: list[DiscoverySeed] = []
        seen: set[str] = set()

        website_origin = origin_of((getattr(company, "website", None) or "").strip())
        ir_url = (getattr(state, "ir_url", None) or "").strip()
        ir_verified = bool(ir_url) and self.service.ir_is_verified(state)
        ir_origin = origin_of(ir_url) if ir_verified else ""

        if not website_origin:
            notes.append(
                "company website is not set or is not an absolute URL; the "
                "website target is unavailable"
            )
        if ir_url and not ir_verified:
            notes.append(
                "the IR URL on record is not verified; it is not a discovery target"
            )
        if not MEDIA_HOSTS:
            notes.append("no approved financial-media allowlist exists; no media target")

        def add(url: str, *, target: str, source_class: WebSourceClass,
                basis: str, query: str = "") -> None:
            if len(seeds) >= policy.max_seed_urls:
                return
            canonical = canonicalize_url(url)
            if not canonical:
                refusals.append(DiscoveryRefusal(url=url, reason="not_canonicalizable"))
                return
            host = host_of(canonical)
            if host not in pinned:
                # Cannot happen for a URL built on a pinned origin; kept so a
                # future origin source cannot slip an unpinned host through.
                refusals.append(DiscoveryRefusal(
                    url=canonical, reason="host_not_pinned",
                    detail=f"'{host}' is not pinned for {company.ticker}",
                ))
                return
            if canonical in seen:
                return
            seen.add(canonical)
            seeds.append(DiscoverySeed(
                url=canonical, host=host, source_class=source_class.value,
                target=target, basis=basis, query=query,
            ))

        paths = self._paths(texts)

        # 1. The company's own website: conventional paths only.
        if website_origin:
            for path, query in paths[: policy.max_paths_per_origin]:
                add(urljoin(website_origin, path), target="company_website",
                    source_class=pinned.get(host_of(website_origin), WebSourceClass.COMPANY_WEBSITE),
                    basis=f"path:{path}", query=query)

        # 2. The verified IR page itself, then the same paths on its origin
        #    when that origin differs from the website's.
        if ir_verified:
            ir_class = pinned.get(host_of(ir_url), WebSourceClass.VERIFIED_IR)
            add(ir_url, target="verified_ir", source_class=ir_class, basis="verified_ir_url")
            if ir_origin and ir_origin != website_origin:
                for path, query in paths[: policy.max_paths_per_origin]:
                    add(urljoin(ir_origin, path), target="verified_ir",
                        source_class=ir_class, basis=f"path:{path}", query=query)

        # 3./4. Exchange, regulator and media hosts are pinned for
        #    validation but never receive constructed URLs: the filing
        #    providers own exchange formats, and no media allowlist exists.
        return tuple(seeds), tuple(refusals), tuple(notes)

    def _paths(self, texts: Sequence[str]) -> list[tuple[str, str]]:
        """Ordered union of the fixed-table paths the queries select."""
        out: list[tuple[str, str]] = []
        seen: set[str] = set()
        for text in texts[: self.policy.max_queries]:
            for path in self.service.conventional_paths(text):
                if path not in seen:
                    seen.add(path)
                    out.append((path, text))
        return out

    # ------------------------------------------------------- exchange refs
    def _exchange_references(
        self, company: Company, texts: Sequence[str], *, now: datetime,
    ) -> tuple[WebEvidenceCandidate, ...]:
        """Already-discovered exchange/regulator announcements that match.

        Read-only over ``DiscoveredFiling``: what the filing crawl found on
        the exchanges for this company. Matching is a plain content-term
        overlap with the queries; nothing is downloaded and nothing is
        written. A row with an ingested document carries its id so the
        consumer can cite the filing the platform already holds.
        """
        policy = self.policy
        if policy.max_exchange_references <= 0:
            return ()
        terms = self._content_terms(texts, company)
        if not terms:
            return ()
        cutoff = _as_utc(now) - timedelta(days=policy.exchange_reference_max_age_days)
        try:
            rows = list(self.db.execute(
                select(DiscoveredFiling)
                .where(DiscoveredFiling.company_id == company.id)
                .where(DiscoveredFiling.status != CollectionStatus.SKIPPED.value)
                .order_by(DiscoveredFiling.id.desc())
                .limit(_EXCHANGE_SCAN_ROWS)
            ).scalars().all())
        except Exception:  # noqa: BLE001 - a missing table yields no references
            log.exception("discovered filings unavailable", company_id=company.id)
            return ()
        # Newest first, undated last, ties on the higher id — sorted here so
        # the order does not depend on how a dialect sorts NULLs.
        rows.sort(
            key=lambda r: (
                _as_utc(r.published_on) if r.published_on else datetime.min.replace(tzinfo=timezone.utc),
                int(r.id or 0),
            ),
            reverse=True,
        )

        out: list[WebEvidenceCandidate] = []
        seen_urls: set[str] = set()
        seen_hashes: set[str] = set()
        for row in rows:
            published = _as_utc(row.published_on) if row.published_on else None
            if published is not None and published < cutoff:
                continue
            relevance, matched = _overlap(row.title or "", terms)
            if relevance <= 0.0:
                continue
            url = (row.source_url or "").strip()
            canonical = canonicalize_url(url) if url else ""
            host = host_of(canonical) if canonical else ""
            if canonical and canonical in seen_urls:
                continue
            if row.content_sha256 and row.content_sha256 in seen_hashes:
                continue
            if canonical:
                seen_urls.add(canonical)
            if row.content_sha256:
                seen_hashes.add(row.content_sha256)
            source_class = (
                WebSourceClass.REGULATOR if host in REGULATOR_HOSTS
                else WebSourceClass.EXCHANGE
            )
            authority = web_authority(source_class, published_at=published)
            basis_date, basis = freshness_basis(published, None)
            freshness = recency_factor(basis_date, today=now.date())
            snippet = _WHITESPACE.sub(" ", f"{row.source}: {row.title or ''}").strip()
            out.append(WebEvidenceCandidate(
                document_id=row.document_id,
                chunk_id=None,
                company_id=company.id,
                source_url=url or None,
                canonical_url=canonical or None,
                title=(row.title or None),
                source_class=source_class.value,
                published_at=published,
                retrieved_at=None,
                snippet=snippet[:400],
                relevance=round(relevance, _ROUND),
                authority=round(authority, _ROUND),
                freshness=round(freshness, _ROUND),
                freshness_basis=basis,
                score=round(blend_score(relevance, authority, freshness), _ROUND),
                matched_queries=matched,
                signals=(EXCHANGE_REFERENCE_SIGNAL,),
                content_hash=row.content_sha256 or None,
                origin=EXCHANGE_REFERENCE_ORIGIN,
            ))
            if len(out) >= policy.max_exchange_references:
                break
        return tuple(out)

    # ---------------------------------------------------------------- fetch
    def _fetch(
        self, company: Company, seeds: Sequence[DiscoverySeed],
        texts: Sequence[str], *, persist: bool,
    ) -> WebSearchResult | DiscoveryRefusal:
        """One call into the service with exactly the planned seeds.

        ``max_urls`` equals the seed count so the service attempts these
        URLs and nothing else — its own IR/default-path fallbacks are cut
        off — and ``limit`` equals it too so no accepted page is dropped
        before relevance is applied here.
        """
        urls = tuple(s.url for s in seeds)
        query = WebSearchQuery(
            company_id=company.id,
            query=texts[0] if texts else "",
            candidate_urls=urls,
            limit=len(urls),
            max_urls=len(urls),
            persist=persist,
        )
        try:
            return self.service.search(query)
        except WebEvidenceError as exc:
            log.warning("targeted discovery refused", company_id=company.id, error=str(exc))
            return DiscoveryRefusal(url=urls[0] if urls else "", reason="service_refused",
                                    detail=str(exc))

    def _candidates_from(
        self, result: WebSearchResult, *, company: Company,
        texts: Sequence[str], today: date,
    ) -> list[WebEvidenceCandidate]:
        terms = self._content_terms(texts, company)
        out: list[WebEvidenceCandidate] = []
        for ref in result.documents:
            out.append(self._candidate(ref, company_id=company.id, terms=terms, today=today))
        return out

    @staticmethod
    def _candidate(
        ref: WebDocumentRef, *, company_id: str,
        terms: dict[str, tuple[str, ...]], today: date,
    ) -> WebEvidenceCandidate:
        text = f"{ref.title or ''} {ref.preview or ''}"
        relevance, matched = _overlap(text, terms)
        authority = web_authority(ref.source_class, published_at=ref.published_at)
        basis_date, basis = freshness_basis(ref.published_at, ref.retrieved_at)
        freshness = recency_factor(basis_date, today=today)
        persisted = ref.document_id is not None
        return WebEvidenceCandidate(
            document_id=ref.document_id,
            chunk_id=None,
            company_id=company_id,
            source_url=ref.url,
            canonical_url=ref.canonical_url or canonicalize_url(ref.url) or None,
            title=ref.title or None,
            source_class=ref.source_class.value,
            published_at=ref.published_at,
            retrieved_at=ref.retrieved_at,
            snippet=_WHITESPACE.sub(" ", ref.preview or "").strip(),
            relevance=round(relevance, _ROUND),
            authority=round(authority, _ROUND),
            freshness=round(freshness, _ROUND),
            freshness_basis=basis,
            score=round(blend_score(relevance, authority, freshness), _ROUND),
            matched_queries=matched,
            signals=(LIVE_FETCH_SIGNAL,),
            content_hash=ref.content_hash or None,
            origin=LIVE_FETCH_PERSISTED_ORIGIN if persisted else LIVE_FETCH_TRANSIENT_ORIGIN,
        )

    # -------------------------------------------------------------- helpers
    def _queries(self, queries: Any) -> tuple[str, ...]:
        texts: list[str] = []
        for raw in iter_query_texts(queries):
            cleaned = _WHITESPACE.sub(" ", str(raw or "")).strip()[:200].strip()
            if cleaned and cleaned.casefold() not in {t.casefold() for t in texts}:
                texts.append(cleaned)
            if len(texts) >= self.policy.max_queries:
                break
        return tuple(texts)

    @staticmethod
    def _content_terms(
        texts: Sequence[str], company: Company,
    ) -> dict[str, tuple[str, ...]]:
        """Per query, the content tokens relevance is measured on.

        The company's own name and ticker are removed — every page on its
        own site mentions them — as are function words. A query left with
        nothing keeps its full token set, so a subject-only query still
        measures something rather than nothing.
        """
        lead = set(_tokens(getattr(company, "name", "") or ""))
        lead.update(_tokens(getattr(company, "ticker", "") or ""))
        out: dict[str, tuple[str, ...]] = {}
        for text in texts:
            tokens = _tokens(text)
            content = tuple(t for t in tokens if t not in lead and t not in _STOPWORDS)
            out[text] = content or tuple(t for t in tokens if t not in _STOPWORDS) or tokens
        return out


# ---------------------------------------------------------------------------
# Module helpers
# ---------------------------------------------------------------------------
def _tokens(text: str) -> tuple[str, ...]:
    out: list[str] = []
    for match in _TOKEN.findall(text or ""):
        token = match.casefold().strip("-/&")
        if not token:
            continue
        if len(token) < 3 and not any("\u0900" <= ch <= "\u097F" for ch in token):
            continue
        if token not in out:
            out.append(token)
    return tuple(out)


def _overlap(
    text: str, terms: dict[str, tuple[str, ...]],
) -> tuple[float, tuple[str, ...]]:
    """Best per-query fraction of content terms present in ``text``."""
    haystack = set(_tokens(text))
    best = 0.0
    matched: list[str] = []
    for query, wanted in terms.items():
        if not wanted:
            continue
        hits = sum(1 for t in wanted if t in haystack)
        if hits <= 0:
            continue
        matched.append(query)
        best = max(best, hits / len(wanted))
    return min(1.0, best), tuple(matched)


def _evidence_date(candidate: Any) -> datetime | None:
    published = getattr(candidate, "published_at", None)
    retrieved = getattr(candidate, "retrieved_at", None)
    basis = published if published is not None else retrieved
    return _as_utc(basis) if isinstance(basis, datetime) else None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


# Exchange hosts are imported so the module states, in code, that they are
# validated against and never constructed upon.
_NEVER_CONSTRUCTED_HOSTS: frozenset[str] = frozenset(EXCHANGE_HOSTS) | frozenset(REGULATOR_HOSTS)
