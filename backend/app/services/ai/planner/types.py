"""The vocabulary of a question plan.

This module states what a :class:`QuestionPlan` *is*. It computes nothing,
fetches nothing and answers nothing — the same separation the language
package draws between its types and its renderer.

Three rules shaped the design.

**A plan is a description, never an answer.** Nothing here can carry a
figure, a score, a grade or a verdict. The strongest thing a plan can say
about a company is that the question asks about one. Prose belongs to the
reasoning and composition layers that come later, and the type system here
gives them nowhere to put it early.

**Unknown is a first-class value.** ``Confidence.NONE``,
``EntityStatus.UNRESOLVED`` and ``ExecutionRoute.DECLINE`` exist because a
planner that always reports something is a planner that guesses. Every field
that could be guessed has an explicit "not determined" state.

**Everything is frozen.** A plan is an artefact: it is logged, compared
between languages in tests, and handed to a later layer that must be able to
rely on it. Mutable state in it would make "the same question produces the
same plan" untestable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from app.domain.ai.sourcing import SourceDirective
from app.domain.ai.types import EvidenceKind
from app.domain.language.detect import Detection
from app.domain.language.types import Language
from app.services.ai.financial_intent import FinancialIntent


class QueryType(StrEnum):
    """What kind of question this is.

    Small and closed on purpose. Every member maps to a distinct planning
    decision, and a member that changed nothing downstream would be
    decoration — the failure mode "query type" enums usually die of.
    """

    #: One supported intent, answerable by an existing deterministic engine.
    DETERMINISTIC = "deterministic"
    #: Several supported intents. No single engine answers the whole
    #: question, so a partial answer would be a wrong answer.
    MULTI_INTENT = "multi_intent"
    #: Two or more subjects placed against each other. The deterministic
    #: engines are single-company by construction, so this cannot be
    #: executed by them and must not be squeezed into one of them.
    COMPARISON = "comparison"
    #: The question restricts where the evidence may come from. Provenance
    #: is a claim about the answer, not a detail of it.
    SOURCE_DIRECTED = "source_directed"
    #: Recognisable as a financial question, but not one the platform
    #: currently supports. Deferred to a later phase, never approximated.
    UNSUPPORTED = "unsupported"
    #: Genuinely broad — no specific figure or verdict is being asked for.
    OPEN_ENDED = "open_ended"
    #: Understood only partially. The planner knows it was asked something
    #: evaluative but not which supported intent was meant.
    AMBIGUOUS = "ambiguous"


class ExecutionRoute(StrEnum):
    """Where the plan would be executed.

    The planner does not execute any of these. It *names* the route so the
    composition layer can dispatch without re-deriving the analysis, and so
    a reader can see why a question went where it went.
    """

    #: ``FinancialAnswerEngine`` — the Phase 1 canonical-figure intents.
    DETERMINISTIC_FINANCIAL = "deterministic_financial"
    #: ``InvestmentAnswerEngine`` — the Phase 2A investment intents, which
    #: interpret the existing ``ScoreResult``.
    DETERMINISTIC_INVESTMENT = "deterministic_investment"
    #: Several intents; needs the reasoning/composition layer (Part 2C).
    COMPOSITION_REQUIRED = "composition_required"
    #: A source restriction is in force, which outranks every other route.
    SOURCE_ROUTER = "source_router"
    #: Needs internal reasoning that does not exist yet.
    INTERNAL_REASONING = "internal_reasoning"
    #: Nothing safe to execute. Declining is the answer.
    DECLINE = "decline"


class Confidence(StrEnum):
    """How much the plan can be trusted.

    Not a probability and deliberately not a float: a number invites
    thresholding in calling code, and the difference between these levels is
    categorical, not gradual.
    """

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    NONE = "none"


class EntityStatus(StrEnum):
    """How well the subject of the question was identified."""

    #: Exactly one known company named in or pinned to the question.
    RESOLVED = "resolved"
    #: More than one candidate. The planner records the ambiguity rather
    #: than picking, because picking is a guess the user cannot see.
    AMBIGUOUS = "ambiguous"
    #: No company named and no conversation context to fall back on.
    UNRESOLVED = "unresolved"
    #: No company named, but the conversation has one pinned. Usable, and
    #: weaker than an explicit mention — recorded as such.
    CONTEXT_ONLY = "context_only"


class IntentFamily(StrEnum):
    """Which deterministic engine owns an intent, when one does."""

    #: A Phase 1 canonical financial figure.
    PHASE1 = "phase1"
    #: A Phase 2A investment-intelligence intent.
    INVESTMENT = "investment"


@dataclass(frozen=True, slots=True)
class IntentMatch:
    """One intent the question was recognised as asking for.

    ``position`` is the character offset of the match in the text that was
    scrutinised — the raw question on the first pass, the normalised
    question on the fallback pass. It is what preserves the order the user
    asked things in. Order matters: "strengths and weaknesses" and
    "weaknesses and strengths" are the same two intents and are not the
    same question.
    """

    intent: FinancialIntent
    #: The rule that fired, for audit — a plan that cannot explain itself
    #: cannot be debugged when it is wrong.
    matched_on: str
    #: Offset in the scrutinised text. Every rule yields one: a regex
    #: supplies ``start()`` and a literal alias supplies ``find()``, so a
    #: match always has a position to order by.
    position: int
    family: IntentFamily
    #: Higher wins when two matches overlap.
    precedence: int = 0


@dataclass(frozen=True, slots=True)
class EvidenceRequirement:
    """One piece of evidence the answer will need.

    Requirement metadata only — never a value. A plan that carried numbers
    would be a plan that had already started answering, and every
    safety property downstream rests on the answer containing nothing the
    platform did not compute.

    ``published`` exists because some evidence an intent legitimately
    requires is not yet produced by the ContextBuilder. Recording that here
    is how the plan stays honest instead of implying the figure is
    available.
    """

    key: str
    label: str
    kind: EvidenceKind | None
    #: ``False`` for supporting context the answer can be given without.
    required: bool = True
    #: Where the evidence comes from, in the platform's own words.
    source: str = ""
    #: ``False`` when the ContextBuilder does not currently emit this key.
    published: bool = True
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "kind": self.kind.value if self.kind else None,
            "required": self.required,
            "source": self.source,
            "published": self.published,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class EntityResolution:
    """The subject of the question, and how sure the planner is.

    ``candidates`` is populated for :attr:`EntityStatus.AMBIGUOUS` so the
    caller can ask the user to choose. Identity is never invented: a
    company appears here only because the authoritative resolver returned
    it, or because the conversation already had one pinned.
    """

    status: EntityStatus
    #: Identifier of the resolved company, when there is exactly one.
    company_id: str | None = None
    ticker: str | None = None
    name: str | None = None
    #: Every candidate returned by the resolver, in the resolver's order.
    candidates: tuple[str, ...] = ()
    #: How the subject was established, for the audit trail.
    basis: str = ""

    @property
    def is_usable(self) -> bool:
        """Whether execution could proceed on this subject alone."""
        return self.status in {EntityStatus.RESOLVED, EntityStatus.CONTEXT_ONLY}

    def as_dict(self) -> dict:
        return {
            "status": self.status.value,
            "company_id": self.company_id,
            "ticker": self.ticker,
            "name": self.name,
            "candidates": list(self.candidates),
            "basis": self.basis,
            "is_usable": self.is_usable,
        }


@dataclass(frozen=True, slots=True)
class QuestionPlan:
    """What a question is asking for, and what answering it would require.

    The output of the Question Planner and the input of whatever executes
    it. There is deliberately no field here that could hold a sentence of
    answer prose, a figure, a score or a citation: the type is the guardrail.

    ``normalized_question`` is the planner's own English working text,
    produced by the Language Adapter's inbound normalisation. It is for
    planning only — it is never handed back to retrieval, scoring or the
    resolver, all of which keep their existing inputs unchanged.
    """

    original_question: str
    normalized_question: str

    language: Language
    detection: Detection

    entity: EntityResolution
    intents: tuple[IntentMatch, ...]
    query_type: QueryType
    execution_route: ExecutionRoute

    required_evidence: tuple[EvidenceRequirement, ...]
    confidence: Confidence

    #: Why the plan is not more certain than it is. Empty for a clean plan.
    ambiguity: tuple[str, ...] = ()
    #: What is needed before this plan could safely execute.
    missing_requirements: tuple[str, ...] = ()

    #: The parsed source restriction, when the question expressed one.
    source_directive: SourceDirective | None = None
    #: Human-readable observations. Never answer content.
    notes: tuple[str, ...] = field(default_factory=tuple)

    # -------------------------------------------------------------- reading
    @property
    def intent_values(self) -> tuple[str, ...]:
        return tuple(m.intent.value for m in self.intents)

    @property
    def has_intent(self) -> bool:
        return bool(self.intents)

    @property
    def is_executable(self) -> bool:
        """Whether an existing deterministic engine could run this plan."""
        return self.execution_route in {
            ExecutionRoute.DETERMINISTIC_FINANCIAL,
            ExecutionRoute.DETERMINISTIC_INVESTMENT,
        }

    def evidence_keys(self, *, required_only: bool = True) -> tuple[str, ...]:
        return tuple(
            e.key for e in self.required_evidence
            if e.required or not required_only
        )

    def as_dict(self) -> dict:
        """A JSON-safe view. Used for logging and for cross-language tests."""
        return {
            "original_question": self.original_question,
            "normalized_question": self.normalized_question,
            "language": self.language.value,
            "detection": self.detection.as_dict(),
            "entity": self.entity.as_dict(),
            "intents": [
                {
                    "intent": m.intent.value,
                    "matched_on": m.matched_on,
                    "position": m.position,
                    "family": m.family.value,
                    "precedence": m.precedence,
                }
                for m in self.intents
            ],
            "query_type": self.query_type.value,
            "execution_route": self.execution_route.value,
            "required_evidence": [e.as_dict() for e in self.required_evidence],
            "confidence": self.confidence.value,
            "ambiguity": list(self.ambiguity),
            "missing_requirements": list(self.missing_requirements),
            "source_directive": (
                None if self.source_directive is None else
                {
                    "scope": self.source_directive.scope.value,
                    "inferred": self.source_directive.inferred,
                    "exact_refusal": self.source_directive.exact_refusal,
                }
            ),
            "notes": list(self.notes),
        }
