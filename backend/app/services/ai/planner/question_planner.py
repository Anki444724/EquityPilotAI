"""The Internal Question Planner (Part 2B).

Turns a natural-language question into a structured plan, without an
external provider anywhere in the path.

    question
      -> language detection
      -> planning-only normalisation
      -> source-directive parsing
      -> entity resolution
      -> intent matching
      -> query classification
      -> execution route
      -> required evidence
      -> confidence / ambiguity / missing requirements
      -> QuestionPlan

**It does not answer.** The plan names intents and evidence; the reasoning
and composition layers that come later do the answering. There is no field
on :class:`QuestionPlan` that could hold a sentence of prose, a figure, a
score or a citation — not by convention, but because the type has nowhere
to put one.

**It is in shadow mode.** Nothing in the production answer path calls this
yet. ``ResearchAnalyst`` still uses ``FinancialIntentResolver`` and the two
deterministic engines exactly as before; routing them through the planner is
Part 2C. Until then a wrong plan can cost nothing but a wrong plan.

**It invents nothing.** A company appears in a plan only because
``CompanyService.named_in`` returned it or the conversation already had one
pinned. Evidence appears only because the ContextBuilder publishes that
key. When either is unavailable the plan says so in
``missing_requirements`` rather than filling the gap.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from app.domain.ai.sourcing import SourceDirective, parse_directive
from app.domain.language.types import Language
from app.services.ai.memory import ConversationMemory
from app.services.language.adapter import LanguageAdapter

from .evidence import evidence_for_all
from .intent_matcher import IntentMatcher
from .types import (
    Confidence, EntityResolution, EntityStatus, ExecutionRoute, IntentFamily,
    QuestionPlan, QueryType,
)
from .vocabulary import is_comparison, is_vague_evaluative


#: Anything the platform could treat as a company mention. Duck-typed on
#: purpose: the planner reads ``id``/``ticker``/``name`` and nothing else, so
#: a test can supply a stand-in without a database.
CompanyLike = Any

#: The authoritative resolver's shape: text in, every candidate company out.
#: ``CompanyService.named_in`` matches this exactly. The planner calls it and
#: applies the caller's rule to the result; it never resolves a company
#: itself, and never creates one.
CompanyResolver = Callable[[str], Sequence[CompanyLike]]


#: Financial vocabulary that carries no supported intent. Its presence is
#: what separates "not supported yet" (a specific figure was asked for) from
#: "open-ended" (nothing specific was asked for at all). Checked against both
#: the raw and the normalised question, because a Devanagari term such as
#: मुनाफा only becomes recognisable after normalisation.
_FINANCIAL_NOUN = re.compile(
    r"\b(?:revenue|sales|turnover|profit|loss|pat|ebitda|ebit|margin|eps|"
    r"debt|equity|assets|liabilit\w*|cash|capex|dividend|ratio|roe|roce|roic|"
    r"valuation|price|market\s+cap|growth|cagr|crore|lakh|cash\s+flow|"
    r"working\s+capital|interest|tax|depreciation|inventory|receivable|"
    r"payable|altman|book\s+value|intrinsic|wacc|dcf)\b",
    re.IGNORECASE,
)

#: A level figure with no intent behind it — revenue or net profit as a
#: plain number. These are the two the platform deliberately does NOT model
#: as intents yet.
_LEVEL_FIGURE = re.compile(
    r"\b(?:revenue|net\s+profit|net\s+income|profit|sales|turnover|top\s*line)\b",
    re.IGNORECASE,
)

#: Deferral notice for those two. Repeated in the documentation; a plan that
#: silently called them "unsupported" would leave the next phase to
#: rediscover why.
_DEFERRED_TO_2C = (
    "TODO — Part 2C: 'revenue' and 'net_profit' are not FinancialIntent "
    "members. The ContextBuilder publishes 'revenue' and 'pat' citations but "
    "no intent consumes them, so this is deferred rather than invented here."
)


@dataclass(frozen=True, slots=True)
class _Classification:
    """The query type and route a question resolves to."""

    query_type: QueryType
    route: ExecutionRoute


class QuestionPlanner:
    """Understands a question and describes what answering it requires.

    Stateless after construction and safe to reuse: it holds no session,
    writes nothing and caches nothing about the companies it sees.
    """

    def __init__(
        self,
        *,
        company_resolver: CompanyResolver | None = None,
        memory: ConversationMemory | None = None,
        matcher: IntentMatcher | None = None,
        adapter: LanguageAdapter | None = None,
    ) -> None:
        # Every collaborator is injected so the planner can be exercised
        # without a database, a network or a provider. None of them is
        # contacted for anything other than reading what is already there.
        self._resolver = company_resolver
        self._memory = memory
        self._matcher = matcher or IntentMatcher()
        self._adapter = adapter or LanguageAdapter()

    # ------------------------------------------------------------------ api
    def plan(
        self,
        question: str,
        *,
        language: Language | None = None,
        source: SourceDirective | None = None,
    ) -> QuestionPlan:
        """Produce the plan for one question.

        `language` overrides detection when the caller already knows it.
        `source` lets a caller hand in a restriction the API layer parsed
        (``ChatRequest.source``); when omitted the question is parsed with
        the platform's own :func:`parse_directive`, exactly as the analyst
        does.
        """
        original = question or ""

        # One normalisation pass. It yields both the detection (already
        # enriched with the mixed-language signal) and the planning text,
        # and it is the only place the planner touches the adapter.
        query = self._adapter.normalise_query(original)
        detection = query.detection
        normalised = query.english
        resolved_language = language or detection.language

        directive = source if source is not None else parse_directive(original)

        if not original.strip():
            return self._empty_plan(original, normalised, resolved_language,
                                    detection, directive)

        entity, entity_notes = self._resolve_entity(original)
        intents = self._match_intents(original, normalised)
        classification = self._classify(original, normalised, intents, entity,
                                        directive)
        evidence = evidence_for_all(tuple(m.intent for m in intents))
        confidence = self._confidence(classification.query_type, entity)

        ambiguity, missing = self._gaps(
            original, normalised, intents, entity, classification, evidence,
        )

        return QuestionPlan(
            original_question=original,
            normalized_question=normalised,
            language=resolved_language,
            detection=detection,
            entity=entity,
            intents=intents,
            query_type=classification.query_type,
            execution_route=classification.route,
            required_evidence=evidence,
            confidence=confidence,
            ambiguity=ambiguity,
            missing_requirements=missing,
            source_directive=directive,
            notes=entity_notes + self._notes(
                original, normalised, intents, entity, classification,
            ),
        )

    # -------------------------------------------------------------- entity
    def _resolve_entity(
        self, question: str,
    ) -> tuple[EntityResolution, tuple[str, ...]]:
        """The subject of the question, via the authoritative resolver.

        The planner never resolves a company itself and never creates one.
        ``CompanyService.named_in`` decides who was named; the planner only
        applies the same rule the chat endpoint already applies — exactly
        one is a subject, several is an ambiguity, none falls back to the
        conversation's pinned company.
        """
        notes: list[str] = []
        candidates: list[CompanyLike] = []

        if self._resolver is not None:
            try:
                candidates = list(self._resolver(question) or [])
            except Exception:  # noqa: BLE001 - planning must never raise
                # A resolver failure is not an empty answer: reporting
                # "unresolved" would claim the question named nobody.
                candidates = []
                notes.append(
                    "The company resolver failed; entity status is reported "
                    "as unresolved rather than guessed."
                )

        if len(candidates) == 1:
            only = candidates[0]
            return EntityResolution(
                status=EntityStatus.RESOLVED,
                company_id=self._attr(only, "id"),
                ticker=self._attr(only, "ticker"),
                name=self._attr(only, "name"),
                basis="named in the question",
            ), tuple(notes)

        if len(candidates) > 1:
            tickers = [
                self._attr(c, "ticker") or self._attr(c, "name") or "?"
                for c in candidates
            ]
            return EntityResolution(
                status=EntityStatus.AMBIGUOUS,
                candidates=tuple(t for t in tickers if t),
                basis=f"{len(candidates)} companies named in the question",
            ), tuple(notes)

        pinned = self._pinned()
        if pinned is not None:
            company_id, ticker, name = pinned
            notes.append(
                "No company was named; using the company pinned to this "
                "conversation."
            )
            return EntityResolution(
                status=EntityStatus.CONTEXT_ONLY,
                company_id=company_id, ticker=ticker, name=name,
                basis="pinned conversation company",
            ), tuple(notes)

        return EntityResolution(
            status=EntityStatus.UNRESOLVED,
            basis="no company named and no conversation context",
        ), tuple(notes)

    def _pinned(self) -> tuple[str | None, str | None, str | None] | None:
        """The company the conversation already pinned, if any.

        Reads the existing ``ConversationMemory``. It is not a second
        company-memory system: the same object the analyst already uses is
        consulted read-only.
        """
        memory = self._memory
        if memory is None:
            return None
        if not (memory.ticker or memory.company_name or memory.company_id):
            return None
        return memory.company_id, memory.ticker, memory.company_name

    @staticmethod
    def _attr(company: CompanyLike, name: str) -> str | None:
        value = getattr(company, name, None)
        return value if isinstance(value, str) and value else None

    # -------------------------------------------------------------- intent
    def _match_intents(
        self, original: str, normalised: str,
    ) -> tuple[Any, ...]:
        """Match on the raw question, falling back to the normalised form.

        The raw question is tried first because Hinglish keeps its English
        technical terms intact — "financial quality" is in the raw text —
        and matching there preserves the exact order the user asked things
        in. Normalisation is the fallback for the cases the raw text cannot
        serve, chiefly pure Devanagari, where the adapter's term mapping is
        what makes the vocabulary visible.

        Negative evidence is tested against the raw question in both passes;
        see :meth:`IntentMatcher.match`.
        """
        matches = self._matcher.match(original, raw_text=original)
        if matches:
            return matches
        if normalised.strip().lower() == original.strip().lower():
            return ()
        return self._matcher.match(normalised, raw_text=original)

    # ---------------------------------------------------------- classify
    def _classify(
        self,
        original: str,
        normalised: str,
        intents: tuple[Any, ...],
        entity: EntityResolution,
        directive: SourceDirective,
    ) -> _Classification:
        """The query type and the route it implies.

        Ordered by how binding each condition is. A source restriction comes
        first because provenance outranks everything else: answering from
        the database when the user asked for documents only is not a partial
        answer, it is the wrong answer.
        """
        if directive.scope.is_restricted:
            return _Classification(QueryType.SOURCE_DIRECTED,
                                   ExecutionRoute.SOURCE_ROUTER)

        if is_comparison(original) or entity.status is EntityStatus.AMBIGUOUS:
            # Never squeezed into a single-company deterministic intent: the
            # engines are single-company by construction, and a comparison
            # they did not make would be a comparison that was invented.
            return _Classification(QueryType.COMPARISON,
                                   ExecutionRoute.INTERNAL_REASONING)

        if len(intents) > 1:
            return _Classification(QueryType.MULTI_INTENT,
                                   ExecutionRoute.COMPOSITION_REQUIRED)

        if len(intents) == 1:
            only = intents[0]
            return _Classification(
                QueryType.DETERMINISTIC,
                ExecutionRoute.DETERMINISTIC_INVESTMENT
                if only.family is IntentFamily.INVESTMENT
                else ExecutionRoute.DETERMINISTIC_FINANCIAL,
            )

        if is_vague_evaluative(original):
            return _Classification(QueryType.AMBIGUOUS, ExecutionRoute.DECLINE)

        if _FINANCIAL_NOUN.search(f"{original} {normalised}"):
            return _Classification(QueryType.UNSUPPORTED,
                                   ExecutionRoute.DECLINE)

        return _Classification(QueryType.OPEN_ENDED,
                               ExecutionRoute.INTERNAL_REASONING)

    # --------------------------------------------------------- confidence
    @staticmethod
    def _confidence(
        query_type: QueryType, entity: EntityResolution,
    ) -> Confidence:
        """How much the plan can be trusted.

        Categorical by design. A single recognised intent about a known
        company is HIGH; the same intent about an unknown company is only
        MEDIUM, because the subject is inferred rather than stated.
        """
        if query_type is QueryType.UNSUPPORTED:
            return Confidence.NONE
        if query_type in {QueryType.AMBIGUOUS, QueryType.COMPARISON,
                          QueryType.OPEN_ENDED}:
            return Confidence.LOW
        if query_type is QueryType.SOURCE_DIRECTED:
            return Confidence.MEDIUM
        if query_type is QueryType.DETERMINISTIC:
            return Confidence.HIGH if entity.is_usable else Confidence.MEDIUM
        # Multi-intent: every intent is recognised, but nothing today can
        # compose them, so the plan is a description rather than a schedule.
        return Confidence.MEDIUM if entity.is_usable else Confidence.LOW

    # --------------------------------------------------------------- gaps
    def _gaps(
        self,
        original: str,
        normalised: str,
        intents: tuple[Any, ...],
        entity: EntityResolution,
        classification: _Classification,
        evidence: tuple[Any, ...],
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Ambiguity and missing requirements.

        Separate because they mean different things: ambiguity is what made
        the plan less certain, missing requirements are what someone would
        have to supply for it to be executable.
        """
        ambiguity: list[str] = []
        missing: list[str] = []

        if entity.status is EntityStatus.AMBIGUOUS:
            names = ", ".join(entity.candidates)
            ambiguity.append(
                f"The question names more than one company ({names}); the "
                "planner does not choose between them."
            )
            missing.append(f"entity: disambiguate between {names}")
        elif entity.status is EntityStatus.UNRESOLVED:
            missing.append(
                "entity: no company could be resolved from the question or "
                "from the conversation context"
            )
        elif entity.status is EntityStatus.CONTEXT_ONLY:
            ambiguity.append(
                "No company was named; the subject comes from conversation "
                "context and is weaker than an explicit mention."
            )

        if classification.query_type is QueryType.AMBIGUOUS:
            ambiguity.append(
                "The question evaluates the company without naming a "
                "supported intent. 'overall_assessment' is the closest "
                "supported intent, but selecting it would be a guess."
            )
            missing.append(
                "disambiguation: restate which supported intent is meant"
            )

        if classification.query_type is QueryType.UNSUPPORTED:
            missing.append(
                "intent: no supported deterministic intent matches this "
                "question"
            )
            if _LEVEL_FIGURE.search(f"{original} {normalised}"):
                missing.append(
                    "deferred: a level figure was asked for (revenue or net "
                    "profit); see the Part 2C note"
                )

        if classification.query_type is QueryType.COMPARISON:
            missing.append(
                "comparison: the deterministic engines are single-company; "
                "no comparison is produced"
            )

        for req in evidence:
            if not req.published:
                missing.append(
                    f"evidence: '{req.key}' is required but is not published "
                    "by the ContextBuilder"
                )

        if not intents and classification.query_type is QueryType.OPEN_ENDED:
            ambiguity.append(
                "No supported intent was recognised; the question is treated "
                "as open-ended rather than mapped to a nearest guess."
            )

        return tuple(ambiguity), tuple(missing)

    # -------------------------------------------------------------- notes
    def _notes(
        self,
        original: str,
        normalised: str,
        intents: tuple[Any, ...],
        entity: EntityResolution,
        classification: _Classification,
    ) -> tuple[str, ...]:
        notes: list[str] = []

        if normalised.strip().lower() != original.strip().lower():
            notes.append(
                "Planning normalisation rewrote the question for matching "
                "only; retrieval, scoring and the deterministic resolver "
                "still receive their existing inputs unchanged."
            )

        if classification.query_type is QueryType.UNSUPPORTED and _LEVEL_FIGURE.search(
            f"{original} {normalised}"
        ):
            notes.append(_DEFERRED_TO_2C)

        if classification.query_type is QueryType.MULTI_INTENT:
            notes.append(
                "Multiple supported intents were recognised. The existing "
                "executor answers exactly one or none, so this plan is not "
                "yet executable; composition is Part 2C."
            )

        if classification.query_type is QueryType.SOURCE_DIRECTED:
            notes.append(
                "A source restriction is in force. The existing fail-closed "
                "source router remains authoritative; the planner describes "
                "the restriction and does not enforce it."
            )

        if classification.query_type is QueryType.COMPARISON:
            notes.append(
                "Comparison detected. No comparison is fabricated: the "
                "deterministic engines are single-company."
            )

        if intents and entity.status is EntityStatus.UNRESOLVED:
            notes.append(
                "Intents were recognised but no company was resolved; the "
                "plan describes what is being asked, not who it is about."
            )

        return tuple(notes)

    # -------------------------------------------------------------- empty
    def _empty_plan(
        self,
        original: str,
        normalised: str,
        language: Language,
        detection: Any,
        directive: SourceDirective,
    ) -> QuestionPlan:
        """An empty question plans as unsupported, not as an empty success."""
        return QuestionPlan(
            original_question=original,
            normalized_question=normalised,
            language=language,
            detection=detection,
            entity=EntityResolution(
                status=EntityStatus.UNRESOLVED, basis="empty question",
            ),
            intents=(),
            query_type=QueryType.UNSUPPORTED,
            execution_route=ExecutionRoute.DECLINE,
            required_evidence=(),
            confidence=Confidence.NONE,
            ambiguity=("The question is empty.",),
            missing_requirements=("question: no question was supplied",),
            source_directive=directive,
            notes=(),
        )


__all__ = ["CompanyResolver", "CompanyLike", "QuestionPlanner"]
