"""Internal web research: grounded web evidence → cited, verifiable answer.

Part 3 Phase 4D. The one place a ``WEB_RESEARCH`` plan is executed. It is
**not a language model** and it does not pretend to be one: it collects the
platform's own web evidence and composes an answer from *what the pages
literally say*, attributed line by line, with the temporal and evidential
limits stated in the same breath.

**Where it sits.** The analyst consults this layer only for the
``WEB_RESEARCH`` route, only after the source-directive gate, the
deterministic financial engines, the Part 2C composer and the Part 2D
internal open-ended engine have all declined, and only for the company the
analyst is bound to. Its result goes through the same funnel as every other
answer — citation audit, guardrails, annotation, memory, language rendering
— by way of ``ResearchAnalyst._deterministic``. Nothing here verifies,
translates or renders; those are the funnel's jobs and they are not
duplicated.

**How evidence is gathered — the order is the policy.**

1. ``WebQueryGenerator`` (4B) turns the plan into a bounded query set.
2. ``SelfOwnedWebIndex`` (4B) ranks the pages the platform already stores.
3. ``TargetedWebDiscovery`` (4C) is offered the local result. Its
   sufficiency gate — *absent*, *thin*, *stale* or *sufficient* — decides
   whether a bounded live fetch of the company's own verified origins runs.
   Every fetched byte goes through the existing safety stack inside the web
   service; this module opens no socket and constructs no URL.
4. The merged candidates are turned into ``EvidenceKind.WEB`` citations —
   fetched, verified pages only. An exchange *reference* the platform never
   fetched is counted and mentioned; it is not cited.

**How the answer is composed — bounded patterns, nothing generative.**

* One line per source: title, host, source class, the publication date
  *if the page states one* (never inferred), the retrieval date labelled as
  such, the citation marker, and one verbatim sentence from the page.
* Three labels, always present: what the *platform verified* (the pages
  exist, were fetched, and contain the quoted text), what the *sources
  report* (the claims — not platform-verified facts), and what is *not
  established* by this evidence.
* Sources that disagree are both quoted and named. Disagreement is detected
  by two narrow rules — opposing status words, or disjoint figures in the
  same unit — and it is *reported*, never resolved. The platform does not
  pick a winner.
* A current-affairs question answered from dated pages carries the date of
  the most recent one and the caveat that later developments are absent.
  Undated pages are said to be undated; a retrieval date is never dressed up
  as a publication date.
* When nothing usable was found, the result says exactly that, and says
  *why* — local corpus empty, discovery switched off, discovery refused, or
  pages fetched that did not speak to the topic. It never guesses.

**What the result contract guarantees.** ``WebResearchStatus`` is explicit:
evidence found, stale, conflicting, insufficient, discovery disabled,
discovery refused, or not applicable. Only *not applicable* hands the
question back to the analyst's next layer; every other status is the answer.
There is deliberately no path from an evidence gap to an external provider.

Provider isolation: this module imports no provider, no HTTP client and no
translator, and it works unchanged with ``AI_EXTERNAL_PROVIDERS_ENABLED``
off. Citation keys are minted by the platform's single implementation
(``WebProvenance.citation_key`` → ``mint_web_citation_key``); no key is ever
composed here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any, Callable, Iterable, Sequence
from urllib.parse import urlsplit

import structlog

from app.domain.ai.types import Citation, EvidenceKind, WebProvenance
from app.domain.web.types import source_class_label
from app.services.ai.planner import ExecutionRoute, web_research_signal
from app.services.ai.planner.web_query import WebQueryGenerator, WebQueryStatus
from app.services.web.index import LOCAL_INDEX_ORIGIN, SelfOwnedWebIndex
from app.services.web.targeted_discovery import (
    EXCHANGE_REFERENCE_ORIGIN,
    LIVE_FETCH_PERSISTED_ORIGIN,
    LIVE_FETCH_TRANSIENT_ORIGIN,
    DiscoveryStatus,
    TargetedWebDiscovery,
)

log = structlog.get_logger(__name__)


# ===========================================================================
# Result contract
# ===========================================================================
class WebResearchStatus(StrEnum):
    """What the research produced. Explicit so no failure can pass as success."""

    #: Usable, current evidence; the answer is composed from it.
    EVIDENCE_FOUND = "evidence_found"
    #: Answered, but for a current-affairs question the freshest dated page
    #: is older than the policy allows; the answer says so.
    STALE_EVIDENCE = "stale_evidence"
    #: Answered, but the sources disagree; both are quoted, none chosen.
    CONFLICTING_EVIDENCE = "conflicting_evidence"
    #: Nothing usable: the local corpus and (if it ran) discovery produced no
    #: page that speaks to the topic. The answer is the honest gap.
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    #: Nothing usable locally and the web-evidence kill switch is off.
    DISCOVERY_DISABLED = "discovery_disabled"
    #: Nothing usable locally and discovery declined to fetch — no verified
    #: company-owned origin, live fetching off by policy, or every seed
    #: refused by the safety stack.
    DISCOVERY_REFUSED = "discovery_refused"
    #: Not this layer's question (wrong route, no query could be formed).
    #: The only status that hands the question back.
    NOT_APPLICABLE = "not_applicable"


ANSWERED_STATUSES: frozenset[WebResearchStatus] = frozenset({
    WebResearchStatus.EVIDENCE_FOUND,
    WebResearchStatus.STALE_EVIDENCE,
    WebResearchStatus.CONFLICTING_EVIDENCE,
})

GAP_STATUSES: frozenset[WebResearchStatus] = frozenset({
    WebResearchStatus.INSUFFICIENT_EVIDENCE,
    WebResearchStatus.DISCOVERY_DISABLED,
    WebResearchStatus.DISCOVERY_REFUSED,
})


@dataclass(frozen=True, slots=True)
class WebResearchPolicy:
    """The bounds of the synthesis. Every number here is a documented choice."""

    #: Below this a candidate is not evidence for the question. The same
    #: floor the 4C sufficiency gate applies to "strong" local candidates.
    min_relevance: float = 0.35
    #: Pages quoted in one answer. Web evidence is supporting colour; a
    #: handful of attributed sentences is what a reader can check.
    max_sources: int = 4
    #: For a recency-sensitive question, dated evidence older than this is
    #: answered with the stale caveat and the STALE_EVIDENCE status.
    stale_after_days: int = 30
    #: Longest verbatim quote per source, cut at a word boundary.
    quote_chars: int = 280
    #: Require the quoted passage to mention a substantive topic term (not a
    #: recency or news word). Off only when the question has no such term.
    require_topic_match: bool = True

    def __post_init__(self) -> None:
        if not 0.0 <= self.min_relevance <= 1.0:
            raise ValueError("min_relevance must be within [0, 1]")
        if self.max_sources < 1:
            raise ValueError("max_sources must be at least 1")
        if self.stale_after_days < 1:
            raise ValueError("stale_after_days must be at least 1")
        if self.quote_chars < 40:
            raise ValueError("quote_chars must be at least 40")


@dataclass(frozen=True, slots=True)
class WebClaim:
    """One attributed, verbatim line of evidence."""

    citation: Citation
    quote: str
    origin: str
    published_at: datetime | None
    retrieved_at: datetime | None
    #: ``(value, unit)`` figures found in the quote, for the conflict rule.
    figures: tuple[tuple[str, str], ...] = ()
    #: Status words found in the quote: ``"positive"`` / ``"negative"``.
    stance: str | None = None

    @property
    def title(self) -> str:
        return (self.citation.web.title if self.citation.web else "") or self.host

    @property
    def host(self) -> str:
        url = self.citation.web.url if self.citation.web else ""
        return (urlsplit(url).hostname or "").lower()


@dataclass(frozen=True, slots=True)
class WebResearchAnswer:
    """The result. Carries the fields the analyst's funnel reads
    (``content``, ``used_citations``, ``missing``) plus the evidence to add
    to the context and the diagnostics that explain the status."""

    status: WebResearchStatus
    #: Canonical English answer with ``[key]`` markers. Empty only for
    #: NOT_APPLICABLE — a hand-back has no text to audit.
    content: str = ""
    #: Verified web citations to add to the GroundedContext (never replacing
    #: what it already holds). Fetched pages only.
    web_citations: tuple[Citation, ...] = ()
    #: The subset actually referenced by a marker in ``content``.
    used_citations: tuple[Citation, ...] = ()
    #: What the answer needed and did not get, in the funnel's vocabulary.
    missing: tuple[str, ...] = ()
    queries: tuple[str, ...] = ()
    topic_terms: tuple[str, ...] = ()
    company_id: str | None = None
    recency_sensitive: bool = False
    freshest_published_at: datetime | None = None
    #: The 4C layer's own status, when it ran.
    discovery_status: str | None = None
    local_candidates: int = 0
    #: Exchange references that matched but were never fetched (not cited).
    unfetched_references: int = 0
    notes: tuple[str, ...] = ()
    reason: str = ""

    @property
    def answered(self) -> bool:
        return self.status in ANSWERED_STATUSES

    @property
    def applicable(self) -> bool:
        """Whether this layer owns the question (answer or honest gap)."""
        return self.status is not WebResearchStatus.NOT_APPLICABLE

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "answered": self.answered,
            "content": self.content,
            "web_citations": [c.key for c in self.web_citations],
            "used_citations": [c.key for c in self.used_citations],
            "missing": list(self.missing),
            "queries": list(self.queries),
            "topic_terms": list(self.topic_terms),
            "company_id": self.company_id,
            "recency_sensitive": self.recency_sensitive,
            "freshest_published_at": (
                self.freshest_published_at.isoformat()
                if self.freshest_published_at else None
            ),
            "discovery_status": self.discovery_status,
            "local_candidates": self.local_candidates,
            "unfetched_references": self.unfetched_references,
            "notes": list(self.notes),
            "reason": self.reason,
        }


# ===========================================================================
# Candidate → citation (fetched, verified pages only)
# ===========================================================================
_WHITESPACE = re.compile(r"\s+")
_DATE = "%d %b %Y"


def _clean(text: str | None, *, limit: int | None = None) -> str:
    """Page-controlled text made safe for the answer body.

    Square brackets become parentheses so page text can never spell a
    citation marker; the renderer's mask sentinel is removed for the same
    reason; whitespace is collapsed.
    """
    cleaned = _WHITESPACE.sub(" ", (text or "")).replace("[", "(").replace("]", ")")
    cleaned = cleaned.replace("§", " ").strip()
    if limit is not None and len(cleaned) > limit:
        cleaned = cleaned[:limit].rstrip() + "…"
    return cleaned


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def citation_for_candidate(candidate: Any) -> Citation | None:
    """One ``EvidenceKind.WEB`` citation for a fetched page, or ``None``.

    ``None`` for anything that is not a verified fetch: an exchange
    reference (a pointer the platform never fetched), a candidate without a
    URL or a retrieval time (unverifiable), or one with no text to quote.
    The key is minted by the platform's single implementation from the
    canonical URL; the provenance carries the URL, canonical URL, title,
    publication date *as stated by the page*, retrieval time and content
    hash — nothing is filled in.
    """
    origin = getattr(candidate, "origin", LOCAL_INDEX_ORIGIN) or LOCAL_INDEX_ORIGIN
    if origin == EXCHANGE_REFERENCE_ORIGIN:
        return None
    url = (getattr(candidate, "source_url", None) or "").strip()
    retrieved_at = _as_utc(getattr(candidate, "retrieved_at", None))
    if not url or retrieved_at is None:
        return None
    snippet = _clean(getattr(candidate, "snippet", None))
    if not snippet:
        return None

    canonical = (getattr(candidate, "canonical_url", None) or "").strip()
    title = _clean(getattr(candidate, "title", None), limit=120)
    published_at = _as_utc(getattr(candidate, "published_at", None))
    provenance = WebProvenance(
        url=url,
        title=title,
        canonical_url=canonical,
        published_at=published_at,
        retrieved_at=retrieved_at,
        content_hash=str(getattr(candidate, "content_hash", None) or ""),
    )
    host = (urlsplit(canonical or url).hostname or "").lower()
    label_class = source_class_label(getattr(candidate, "source_class", None))
    shown = title or host
    source = f"{shown} — {label_class} ({host})"
    if published_at is not None:
        source += f", published {published_at:{_DATE}}"
    source += f", retrieved {retrieved_at:{_DATE}}"
    return Citation(
        key=provenance.citation_key(),
        label=f"[{label_class}] {shown}",
        kind=EvidenceKind.WEB,
        value=snippet,
        unit="",
        source=source,
        document_id=getattr(candidate, "document_id", None),
        chunk_id=getattr(candidate, "chunk_id", None),
        page=getattr(candidate, "page", None),
        confidence=None,
        snippet=snippet,
        web=provenance,
    )


# ===========================================================================
# Bounded pattern vocabulary
# ===========================================================================
#: Words that, in a quoted sentence, say a development has happened.
_POSITIVE_STATUS = re.compile(
    r"\b(commissioned|completed|complete|operational|commenced|achieved|"
    r"on track|on schedule|inaugurated|went live|has been launched)\b", re.I,
)
#: Words that say it has not, or has slipped.
_NEGATIVE_STATUS = re.compile(
    r"\b(delayed|deferred|postponed|cancelled|canceled|stalled|on hold|"
    r"suspended|scrapped|under review|pushed back|behind schedule)\b", re.I,
)
#: A figure with a unit the platform recognises, for the disagreement rule.
_FIGURE = re.compile(
    r"(?<![\w.])(\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*"
    r"(mtpa|mtpy|mt|tpa|tonnes?|tons?|mw|gw|crore|cr|lakh|lakhs|bn|billion|"
    r"mn|million|%|percent|per cent)(?!\w)", re.I,
)
_UNIT_ALIASES = {
    "cr": "crore", "percent": "%", "per cent": "%", "mn": "million",
    "bn": "billion", "tons": "tonnes", "ton": "tonnes", "tonne": "tonnes",
    "lakhs": "lakh", "mtpy": "mtpa",
}
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_WORDS = ("no", "one", "two", "three", "four", "five", "six", "seven", "eight")


def _count(n: int) -> str:
    """Small counts as words: a digit in an uncited line reads as a figure."""
    return _WORDS[n] if 0 <= n < len(_WORDS) else str(n)


def _plural(n: int, noun: str) -> str:
    return noun if n == 1 else noun + "s"


def _class_of(citation: Citation) -> str:
    """The source-class label the citation was built with."""
    label = citation.label or ""
    if label.startswith("[") and "]" in label:
        return label[1:label.index("]")]
    return source_class_label(None)


def _figures(text: str) -> tuple[tuple[str, str], ...]:
    out: list[tuple[str, str]] = []
    for value, unit in _FIGURE.findall(text):
        unit = unit.lower()
        unit = _UNIT_ALIASES.get(unit, unit)
        out.append((value.replace(",", ""), unit))
    return tuple(out)


def _stance(text: str) -> str | None:
    positive = bool(_POSITIVE_STATUS.search(text))
    negative = bool(_NEGATIVE_STATUS.search(text))
    if positive and not negative:
        return "positive"
    if negative and not positive:
        return "negative"
    return None


def _term_pattern(term: str) -> re.Pattern[str]:
    return re.compile(rf"\b{re.escape(term)}\w{{0,3}}\b", re.I)


def _substantive(terms: Iterable[str]) -> tuple[str, ...]:
    """Topic terms that name the subject, not its recency or genre."""
    out: list[str] = []
    for term in terms:
        text = (term or "").strip()
        if not text:
            continue
        if web_research_signal(text) in {"recency", "news"}:
            continue
        if text.lower() not in {t.lower() for t in out}:
            out.append(text)
    return tuple(out)


def _pick_quote(snippet: str, terms: Sequence[str], limit: int) -> str:
    """The first sentence that mentions a topic term, else the first sentence.

    One sentence, not a paragraph: the citation audit measures coverage per
    sentence, and a second sentence without a marker would read as an
    uncited claim. Cut at a word boundary so a figure is never split.
    """
    sentences = [s.strip() for s in _SENTENCE_END.split(snippet) if s.strip()]
    if not sentences:
        return ""
    # Fallback: the first sentence of substance — an abbreviation fragment
    # such as "Rs." is not a quote worth attributing.
    chosen = next((s for s in sentences if len(s) >= 20), sentences[0])
    patterns = [_term_pattern(t) for t in terms]
    for sentence in sentences:
        if any(p.search(sentence) for p in patterns):
            chosen = sentence
            break
    if len(chosen) > limit:
        cut = chosen[:limit]
        space = cut.rfind(" ")
        if space > 40:
            cut = cut[:space]
        chosen = cut.rstrip(" ,;:") + "…"
    return chosen


# ===========================================================================
# The engine
# ===========================================================================
class InternalWebResearchEngine:
    """Deterministic web-evidence research for the ``WEB_RESEARCH`` route.

    ``index`` is the 4B ``SelfOwnedWebIndex`` (or anything with its
    ``search(queries, company_id=...)``), ``discovery`` the 4C
    ``TargetedWebDiscovery`` (or anything with its ``discover(queries,
    company_id=..., local=...)``) — ``None`` means no live discovery at
    all, ``generator`` the 4B ``WebQueryGenerator``. All three are
    injected so tests can pin the seams; ``for_session`` is the production
    wiring.
    """

    def __init__(
        self,
        *,
        index: Any,
        discovery: Any = None,
        generator: Any = None,
        policy: WebResearchPolicy | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.index = index
        self.discovery = discovery
        self.generator = generator or WebQueryGenerator()
        self.policy = policy or WebResearchPolicy()
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    @classmethod
    def for_session(
        cls, db: Any, *, policy: WebResearchPolicy | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> "InternalWebResearchEngine":
        """The production shape: the 4B index and the 4C discovery over the
        same session the analyst's other services use. The discovery layer
        keeps its own kill switch (``WEB_EVIDENCE_ENABLED``) and policy."""
        return cls(
            index=SelfOwnedWebIndex(db),
            discovery=TargetedWebDiscovery(db),
            generator=WebQueryGenerator(),
            policy=policy,
            clock=clock,
        )

    # ---------------------------------------------------------------- api
    def research(self, plan: Any, context: Any) -> WebResearchAnswer:
        """Plan → queries → local index → (discovery) → synthesis.

        ``context`` is the analyst's ``GroundedContext``; its company is
        authoritative. The plan's entity has already been checked against it
        by the analyst, so the search is scoped to ``context.company_id``
        even when the plan resolved no company of its own.
        """
        route = getattr(plan, "execution_route", None)
        if route is not ExecutionRoute.WEB_RESEARCH:
            return WebResearchAnswer(
                status=WebResearchStatus.NOT_APPLICABLE,
                reason=f"route {getattr(route, 'value', route)!s} is not web research",
            )

        queries = self.generator.generate(plan)
        texts = tuple(getattr(queries, "texts", ()) or ())
        status = getattr(queries, "status", None)
        if not texts or (status is not None and status is not WebQueryStatus.GENERATED):
            return WebResearchAnswer(
                status=WebResearchStatus.NOT_APPLICABLE,
                reason=f"no query could be formed ({getattr(status, 'value', status)!s}): "
                       f"{getattr(queries, 'reason', '')}",
            )

        company_id = (getattr(context, "company_id", None) or None)
        company_name = getattr(context, "name", "") or getattr(context, "ticker", "") or ""
        notes: list[str] = []

        local = None
        try:
            local = self.index.search(queries, company_id=company_id)
        except Exception:  # noqa: BLE001 - the answer must not depend on the index
            log.exception("stored web index unavailable", company_id=company_id)
            notes.append("stored web index unavailable; treated as empty")

        discovery_result = None
        if self.discovery is not None:
            try:
                discovery_result = self.discovery.discover(
                    queries, company_id=company_id, local=local,
                )
            except Exception:  # noqa: BLE001 - discovery is best effort
                log.exception("targeted web discovery failed", company_id=company_id)
                notes.append("live discovery failed; local evidence only")

        if discovery_result is not None:
            candidates = tuple(getattr(discovery_result, "merged", ()) or ())
        else:
            candidates = tuple(getattr(local, "candidates", ()) or ()) if local else ()

        answer = self.synthesise(
            plan, queries, candidates, discovery=discovery_result, local=local,
            company_name=company_name, company_id=company_id, notes=tuple(notes),
        )
        log.info(
            "internal web research",
            status=answer.status.value, company_id=company_id,
            queries=list(answer.queries), local_candidates=answer.local_candidates,
            discovery=answer.discovery_status, cited=[c.key for c in answer.used_citations],
        )
        return answer

    # ---------------------------------------------------------- synthesis
    def synthesise(
        self,
        plan: Any,
        queries: Any,
        candidates: Sequence[Any],
        *,
        discovery: Any = None,
        local: Any = None,
        company_name: str = "",
        company_id: str | None = None,
        notes: tuple[str, ...] = (),
    ) -> WebResearchAnswer:
        """Pure: candidates in, cited answer (or honest gap) out."""
        policy = self.policy
        texts = tuple(getattr(queries, "texts", ()) or ())
        topic_terms = tuple(getattr(queries, "topic_terms", ()) or ())
        recency_sensitive = bool(getattr(queries, "recency_sensitive", False))
        substantive = _substantive(topic_terms)
        topic = " ".join(substantive) if substantive else "current developments"
        company = _clean(company_name) or "this company"
        now = _as_utc(self._clock()) or datetime.now(timezone.utc)
        local_count = len(tuple(getattr(local, "candidates", ()) or ())) if local else 0
        discovery_status = getattr(getattr(discovery, "status", None), "value", None)

        claims, unfetched, off_topic = self._claims(candidates, substantive)

        common = dict(
            queries=texts, topic_terms=topic_terms, company_id=company_id,
            recency_sensitive=recency_sensitive, discovery_status=discovery_status,
            local_candidates=local_count, unfetched_references=unfetched,
        )

        if not claims:
            status, reason = self._gap_status(discovery, local, off_topic)
            content = self._gap_content(
                company, topic, status, discovery=discovery, local=local,
                off_topic=off_topic, unfetched=unfetched,
            )
            return WebResearchAnswer(
                status=status, content=content, missing=(f"web evidence about {topic}",),
                notes=tuple(notes), reason=reason, **common,
            )

        dated = [c.published_at for c in claims if c.published_at is not None]
        freshest = max(dated) if dated else None
        stale = (
            recency_sensitive and freshest is not None
            and now - freshest > timedelta(days=policy.stale_after_days)
        )
        conflicts = self._conflicts(claims)
        if conflicts:
            status = WebResearchStatus.CONFLICTING_EVIDENCE
        elif stale:
            status = WebResearchStatus.STALE_EVIDENCE
        else:
            status = WebResearchStatus.EVIDENCE_FOUND

        content = self._answer_content(
            company, topic, claims, substantive,
            conflicts=conflicts, freshest=freshest, stale=stale,
            recency_sensitive=recency_sensitive, unfetched=unfetched,
        )
        citations = tuple(c.citation for c in claims)
        return WebResearchAnswer(
            status=status, content=content, web_citations=citations,
            used_citations=citations, freshest_published_at=freshest,
            notes=tuple(notes),
            reason=(
                "sources disagree; both attributed" if conflicts
                else "freshest dated evidence is older than the policy allows" if stale
                else "answered from verified web evidence"
            ),
            **common,
        )

    # ------------------------------------------------------------ claims
    def _claims(
        self, candidates: Sequence[Any], substantive: Sequence[str],
    ) -> tuple[tuple[WebClaim, ...], int, int]:
        """Usable claims, unfetched-reference count, off-topic count."""
        policy = self.policy
        claims: list[WebClaim] = []
        seen_keys: set[str] = set()
        unfetched = 0
        off_topic = 0
        patterns = [_term_pattern(t) for t in substantive]
        for candidate in candidates:
            origin = getattr(candidate, "origin", LOCAL_INDEX_ORIGIN) or LOCAL_INDEX_ORIGIN
            if origin == EXCHANGE_REFERENCE_ORIGIN:
                unfetched += 1
                continue
            citation = citation_for_candidate(candidate)
            if citation is None:
                continue
            if float(getattr(candidate, "relevance", 0.0) or 0.0) < policy.min_relevance:
                off_topic += 1
                continue
            haystack = f"{citation.web.title if citation.web else ''} {citation.value}"
            if policy.require_topic_match and patterns and not any(
                p.search(haystack) for p in patterns
            ):
                off_topic += 1
                continue
            if citation.key in seen_keys:
                continue
            quote = _pick_quote(str(citation.value), substantive, policy.quote_chars)
            if not quote:
                continue
            seen_keys.add(citation.key)
            claims.append(WebClaim(
                citation=citation, quote=quote, origin=origin,
                published_at=citation.web.published_at if citation.web else None,
                retrieved_at=citation.web.retrieved_at if citation.web else None,
                figures=_figures(quote), stance=_stance(quote),
            ))
            if len(claims) >= policy.max_sources:
                break
        return tuple(claims), unfetched, off_topic

    # --------------------------------------------------------- conflicts
    def _conflicts(self, claims: Sequence[WebClaim]) -> tuple[str, ...]:
        """Disagreements between sources, as attributed sentences.

        Two narrow rules. (1) One source's quote says a development has
        happened and another's says it has slipped. (2) Two sources give
        figures in the same unit and share none of them. Both are reported
        with the marker of each source; neither is resolved here.
        """
        out: list[str] = []
        names = self._names(claims)
        positive = [c for c in claims if c.stance == "positive"]
        negative = [c for c in claims if c.stance == "negative"]
        if positive and negative:
            a, b = positive[0], negative[0]
            out.append(
                f"Sources differ on the status: {names[a.citation.key]} describes "
                f"it as achieved or on track {a.citation.marker}, while "
                f"{names[b.citation.key]} describes it as delayed or under review "
                f"{b.citation.marker}."
            )
        by_unit: dict[str, list[tuple[WebClaim, set[str]]]] = {}
        for claim in claims:
            for value, unit in claim.figures:
                by_unit.setdefault(unit, [])
                entry = next((e for e in by_unit[unit] if e[0] is claim), None)
                if entry is None:
                    by_unit[unit].append((claim, {value}))
                else:
                    entry[1].add(value)
        for unit, entries in sorted(by_unit.items()):
            if len(entries) < 2:
                continue
            first_claim, first_values = entries[0]
            for other_claim, other_values in entries[1:]:
                if first_values & other_values:
                    continue
                unit_label = unit.upper() if unit != "%" else "%"
                out.append(
                    f"Sources report different figures in {unit_label}: "
                    f"{names[first_claim.citation.key]} gives "
                    f"{', '.join(f'{v} {unit_label}' for v in sorted(first_values))} "
                    f"{first_claim.citation.marker}; {names[other_claim.citation.key]} "
                    f"gives {', '.join(f'{v} {unit_label}' for v in sorted(other_values))} "
                    f"{other_claim.citation.marker}."
                )
                break
        return tuple(out)

    # ----------------------------------------------------------- content
    @staticmethod
    def _origin_words(claims: Sequence[WebClaim]) -> str:
        """How the pages reached the platform, in words, with the raw origin."""
        friendly = {
            LOCAL_INDEX_ORIGIN: "from the stored web index",
            LIVE_FETCH_PERSISTED_ORIGIN: "fetched live and stored",
            LIVE_FETCH_TRANSIENT_ORIGIN: "fetched live, not stored",
        }
        counts: dict[str, int] = {}
        for claim in claims:
            counts[claim.origin] = counts.get(claim.origin, 0) + 1
        order = (LOCAL_INDEX_ORIGIN, LIVE_FETCH_PERSISTED_ORIGIN, LIVE_FETCH_TRANSIENT_ORIGIN)
        parts = [f"{_count(counts[o])} {friendly[o]} ({o})" for o in order if o in counts]
        parts += [f"{_count(n)} ({o})" for o, n in counts.items() if o not in order]
        return ", ".join(parts)

    @staticmethod
    def _names(claims: Sequence[WebClaim]) -> dict[str, str]:
        """A display name per citation key: the title, made unique by the
        page path when two pages share one title."""
        titles: dict[str, int] = {}
        for claim in claims:
            titles[claim.title] = titles.get(claim.title, 0) + 1
        names: dict[str, str] = {}
        for claim in claims:
            name = claim.title
            if titles[name] > 1 and claim.citation.web is not None:
                path = urlsplit(claim.citation.web.canonical_url
                                or claim.citation.web.url).path or "/"
                name = f"{name} ({_clean(path, limit=60)})"
            names[claim.citation.key] = name
        return names

    def _answer_content(
        self, company: str, topic: str, claims: Sequence[WebClaim],
        substantive: Sequence[str], *, conflicts: Sequence[str],
        freshest: datetime | None, stale: bool, recency_sensitive: bool,
        unfetched: int,
    ) -> str:
        classes: list[str] = []
        for claim in claims:
            label = _class_of(claim.citation)
            if label not in classes:
                classes.append(label)
        n = len(claims)
        lines = [
            f"Web evidence on {company} — {topic}.",
            "",
            (
                f"Verified by the platform: {_count(n)} verified {_plural(n, 'page')} "
                f"(origin {self._origin_words(claims)}; source class "
                f"{', '.join(classes)}). Each page was fetched by this platform and "
                "contains the text quoted below. A publication date is stated only "
                "where the page states one; the retrieval date is when the platform "
                "read the page, not the page's own date. Quotes are verbatim; "
                "nothing beyond them is inferred."
            ),
            "",
            "Source-reported claims (what the pages say — attributed, not platform-verified facts):",
        ]
        names = self._names(claims)
        for claim in claims:
            label_class = _class_of(claim.citation)
            if claim.published_at is not None:
                dates = f"published {claim.published_at:{_DATE}}"
            else:
                dates = "publication date not stated"
            if claim.retrieved_at is not None:
                dates += f", retrieved {claim.retrieved_at:{_DATE}}"
            lines.append(
                f"- {names[claim.citation.key]} ({claim.host}, {label_class}), {dates} "
                f"{claim.citation.marker}: \"{claim.quote}\""
            )

        if conflicts:
            lines.append("")
            lines.append(
                " ".join(conflicts)
                + " The platform does not reconcile or choose between them; "
                "both are shown as reported."
            )

        uncovered = [
            term for term in substantive
            if not any(_term_pattern(term).search(c.quote) for c in claims)
        ]
        lines.append("")
        if uncovered:
            lines.append(
                f"Not established by this evidence: no quoted page speaks to "
                f"'{', '.join(uncovered)}'; whether the position has changed since "
                "the dates above is also not established."
            )
        else:
            lines.append(
                "Not established by this evidence: whether the position has changed "
                "since the dates above. The platform verified the pages, not the claims."
            )

        if recency_sensitive:
            lines.append("")
            if freshest is None:
                lines.append(
                    "Temporal note: the platform cannot establish how current this is — "
                    "none of the pages states a publication date, and the retrieval "
                    "dates above are not publication dates."
                )
            else:
                marker = next(
                    c.citation.marker for c in claims if c.published_at == freshest
                )
                if stale:
                    lines.append(
                        f"Temporal note: the most recent dated evidence is from "
                        f"{freshest:{_DATE}} {marker}; that is older than the "
                        "freshness window this platform applies to a question about "
                        "the latest position, so developments after that date are "
                        "not reflected."
                    )
                else:
                    lines.append(
                        f"Temporal note: the most recent dated evidence is from "
                        f"{freshest:{_DATE}} {marker}; developments after that date "
                        "are not reflected."
                    )
        if unfetched:
            lines.append("")
            lines.append(
                f"{_count(unfetched).capitalize()} exchange filing "
                f"{_plural(unfetched, 'reference')} matched the topic but "
                f"{'was' if unfetched == 1 else 'were'} not fetched; "
                f"{'it is' if unfetched == 1 else 'they are'} not used as evidence."
            )
        return "\n".join(lines)

    # --------------------------------------------------------------- gaps
    @staticmethod
    def _gap_status(
        discovery: Any, local: Any, off_topic: int,
    ) -> tuple[WebResearchStatus, str]:
        status = getattr(discovery, "status", None)
        if status is DiscoveryStatus.DISABLED:
            return (WebResearchStatus.DISCOVERY_DISABLED,
                    "no usable local evidence and WEB_EVIDENCE_ENABLED is off")
        if status in {DiscoveryStatus.NO_TARGET, DiscoveryStatus.PLANNED_ONLY}:
            return (WebResearchStatus.DISCOVERY_REFUSED,
                    f"no usable local evidence and discovery declined ({status.value})")
        if status is DiscoveryStatus.NOTHING_FOUND:
            fetched = tuple(getattr(discovery, "fetched", ()) or ())
            refusals = tuple(getattr(discovery, "refusals", ()) or ())
            if refusals and not fetched:
                return (WebResearchStatus.DISCOVERY_REFUSED,
                        "every discovery seed was refused by the safety stack")
        if off_topic:
            return (WebResearchStatus.INSUFFICIENT_EVIDENCE,
                    "the pages found do not speak to the topic")
        return (WebResearchStatus.INSUFFICIENT_EVIDENCE, "no usable web evidence")

    @staticmethod
    def _gap_content(
        company: str, topic: str, status: WebResearchStatus, *, discovery: Any,
        local: Any, off_topic: int, unfetched: int,
    ) -> str:
        local_count = len(tuple(getattr(local, "candidates", ()) or ())) if local else 0
        if local is None:
            stored = "the stored web index could not be read"
        elif local_count == 0:
            stored = (
                "no stored web page for this company matched the question"
                if getattr(local, "corpus_documents", 0)
                else "no stored web page is in scope for this company"
            )
        elif off_topic:
            stored = f"the stored passages that matched were not about {topic}"
        else:
            stored = "no stored passage could be verified as evidence"

        d_status = getattr(discovery, "status", None)
        if discovery is None:
            live = "not available for this analyst; nothing was fetched"
        elif d_status is DiscoveryStatus.DISABLED:
            live = "WEB_EVIDENCE_ENABLED is off — nothing was planned or fetched"
        elif d_status is DiscoveryStatus.NO_TARGET:
            live = (
                "refused — no verified company-owned origin is on record for this "
                "company, so no page was fetched (the platform never guesses a URL)"
            )
        elif d_status is DiscoveryStatus.PLANNED_ONLY:
            live = "planned but not executed — live fetching is disabled by policy"
        elif d_status is DiscoveryStatus.NO_QUERIES:
            live = "not attempted — no query text"
        elif d_status is DiscoveryStatus.LOCAL_SUFFICIENT:
            live = (
                "not attempted — the stored evidence met the sufficiency policy, "
                f"but its passages were not about {topic}"
            )
        elif d_status is DiscoveryStatus.NOTHING_FOUND:
            fetched = tuple(getattr(discovery, "fetched", ()) or ())
            refusals = tuple(getattr(discovery, "refusals", ()) or ())
            if refusals and not fetched:
                live = (
                    "every seed on the company's own origins was declined by the "
                    "platform's safety, robots or quality checks; nothing was fetched"
                )
            else:
                live = (
                    "the company's own pages were fetched through the platform's "
                    f"safety checks and none carried a passage about {topic}"
                )
        else:
            live = f"pages were fetched but none carried a passage about {topic}"

        lines = [
            f"Current web evidence is insufficient to answer this about {company} ({topic}).",
            "",
            f"Stored web pages (searched first): {stored}.",
            f"Live discovery: {live}.",
        ]
        if unfetched:
            lines.append(
                f"Exchange filing references: {_count(unfetched)} matched the topic but "
                f"{'was' if unfetched == 1 else 'were'} not fetched and "
                f"{'is' if unfetched == 1 else 'are'} not used as evidence."
            )
        lines.append("")
        lines.append(
            f"No answer is inferred beyond the evidence: the current {topic} of "
            f"{company} cannot be stated from what the platform holds."
        )
        return "\n".join(lines)


__all__ = [
    "ANSWERED_STATUSES",
    "GAP_STATUSES",
    "InternalWebResearchEngine",
    "WebClaim",
    "WebResearchAnswer",
    "WebResearchPolicy",
    "WebResearchStatus",
    "citation_for_candidate",
]
