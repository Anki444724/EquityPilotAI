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


class InternalComposer:
    """Prepare multi-intent plans for deterministic composition.

    The first Part 2C increment is intentionally narrow: only
    ``COMPOSITION_REQUIRED`` is composable. The class does not duplicate
    planner logic or any financial-analysis logic.
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
