"""Internal reasoning and composition for planned financial questions.

Part 2C.

This module consumes an already-built QuestionPlan. It does not re-plan the
question, create evidence, perform retrieval, score companies, or generate
citations. Execution of grounded answers remains delegated to the existing
answer engines; this layer only coordinates composition.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.domain.ai.types import Citation
from app.services.ai.context_builder import GroundedContext
from app.services.ai.financial_answer_engine import (
    DeterministicAnswer,
    FinancialAnswerEngine,
)
from app.services.ai.financial_intent import INVESTMENT_INTENTS, FinancialIntent
from app.services.ai.investment_answer_engine import InvestmentAnswerEngine
from app.services.ai.planner.types import ExecutionRoute, QuestionPlan


class CompositionStatus(StrEnum):
    """Outcome of the internal composition decision."""

    READY = "ready"
    NOT_COMPOSABLE = "not_composable"


@dataclass(frozen=True, slots=True)
class CompositionDecision:
    """A provider-free decision about how a plan should be handled.

    This is deliberately not answer prose. A later execution step can use the
    selected route and ordered intents to invoke the appropriate existing
    deterministic components.
    """

    status: CompositionStatus
    route: ExecutionRoute
    intents: tuple[str, ...]
    reason: str


@dataclass(frozen=True, slots=True)
class ComposedAnswer:
    """A provider-free answer composed from existing deterministic engines.

    The component engines provide all answer prose; this object only joins
    their already-rendered content in planner order. Citation support is
    fixed at composition time by merging the actual citations used by those
    engines, and missing evidence is reported rather than fabricated.
    """

    intents: tuple[FinancialIntent, ...]
    content: str
    used_citations: tuple[Citation, ...] = ()
    missing: tuple[str, ...] = ()


class InternalComposer:
    """Prepare multi-intent plans for deterministic composition.

    Only ``COMPOSITION_REQUIRED`` is composable. The class does not duplicate
    planner logic or any financial-analysis logic: it dispatches each intent
    to the existing deterministic answer engine that already owns it.
    """

    def compose(self, plan: QuestionPlan) -> CompositionDecision:
        """Return a deterministic composition decision for ``plan``."""
        if plan.execution_route is ExecutionRoute.COMPOSITION_REQUIRED:
            # The planner only emits this route for len(intents) > 1, so there
            # is no zero-intent case to guard against here.
            return CompositionDecision(
                status=CompositionStatus.READY,
                route=plan.execution_route,
                intents=plan.intent_values,
                reason="Multiple matched intents require internal composition.",
            )

        return CompositionDecision(
            status=CompositionStatus.NOT_COMPOSABLE,
            route=plan.execution_route,
            intents=plan.intent_values,
            reason=(
                "This route is not handled by the Part 2C composition layer."
            ),
        )

    @staticmethod
    def _engine_for(
        intent: FinancialIntent,
        financial_engine: FinancialAnswerEngine,
        investment_engine: InvestmentAnswerEngine,
    ) -> FinancialAnswerEngine:
        """Return the existing deterministic engine responsible for ``intent``.

        ``INVESTMENT_INTENTS`` is the single source of truth for this split.
        InvestmentAnswerEngine subclasses FinancialAnswerEngine, so the
        returned type is the common existing answer-engine interface.
        """
        if intent in INVESTMENT_INTENTS:
            return investment_engine
        return financial_engine

    @classmethod
    def _validate_engine_coverage(
        cls,
        financial_engine: FinancialAnswerEngine,
        investment_engine: InvestmentAnswerEngine,
    ) -> None:
        """Fail loudly if the current intent enum has no deterministic handler.

        This check deliberately discovers handlers from the existing engine
        naming convention rather than maintaining a second intent registry.
        Adding a FinancialIntent without adding its engine handler therefore
        fails at the composition boundary instead of dropping that intent.
        """
        for intent in FinancialIntent:
            engine = cls._engine_for(intent, financial_engine, investment_engine)
            handler = getattr(engine, f"_build_{intent.value}", None)
            if not callable(handler):
                raise RuntimeError(
                    "No deterministic answer handler exists for "
                    f"FinancialIntent.{intent.name} ({intent.value})."
                )

    def compose_answer(
        self,
        plan: QuestionPlan,
        context: GroundedContext,
    ) -> ComposedAnswer | None:
        """Compose all intents in a multi-intent plan without a provider.

        The supplied context is authoritative. This method never creates or
        broadens it; it only passes it to the existing deterministic answer
        engines. Each engine's content is preserved verbatim, while citations
        and missing evidence are merged in first-use order.
        """
        decision = self.compose(plan)
        if decision.status is not CompositionStatus.READY:
            return None

        financial_engine = FinancialAnswerEngine()
        investment_engine = InvestmentAnswerEngine()
        self._validate_engine_coverage(financial_engine, investment_engine)

        intents = tuple(match.intent for match in plan.intents)
        answers: list[DeterministicAnswer] = []
        for intent in intents:
            engine = self._engine_for(intent, financial_engine, investment_engine)
            handler = getattr(engine, f"_build_{intent.value}", None)
            if not callable(handler):
                # This is also guarded by _validate_engine_coverage, but keep
                # the per-intent failure local and explicit if dispatch grows.
                raise RuntimeError(
                    "No deterministic answer handler exists for "
                    f"FinancialIntent.{intent.name} ({intent.value})."
                )
            answers.append(engine.answer(intent, context))

        used_citations: list[Citation] = []
        seen_citation_keys: set[str] = set()
        missing: list[str] = []
        seen_missing: set[str] = set()
        for answer in answers:
            for citation in answer.used_citations:
                if citation.key not in seen_citation_keys:
                    seen_citation_keys.add(citation.key)
                    used_citations.append(citation)
            for value in answer.missing:
                if value not in seen_missing:
                    seen_missing.add(value)
                    missing.append(value)

        return ComposedAnswer(
            intents=intents,
            content=" ".join(answer.content for answer in answers),
            used_citations=tuple(used_citations),
            missing=tuple(missing),
        )
