"""Tests for the Part 2C internal composition contract."""

from app.services.ai.internal_composer import (
    CompositionStatus,
    InternalComposer,
)
from app.services.ai.planner.question_planner import QuestionPlanner
from app.services.ai.planner.types import ExecutionRoute


def _plan(question: str):
    return QuestionPlanner().plan(question)


def test_multi_intent_plan_is_ready_for_composition():
    plan = _plan("What is the P/E and what is the financial quality?")
    result = InternalComposer().compose(plan)

    assert plan.execution_route is ExecutionRoute.COMPOSITION_REQUIRED
    assert result.status is CompositionStatus.READY
    assert result.route is ExecutionRoute.COMPOSITION_REQUIRED
    assert result.intents == plan.intent_values


def test_composition_preserves_planner_intent_order():
    plan = _plan("What are the strengths and weaknesses?")
    result = InternalComposer().compose(plan)

    assert result.status is CompositionStatus.READY
    assert plan.intent_values == ("strengths", "weaknesses")
    assert result.intents == ("strengths", "weaknesses")


def test_composition_preserves_reversed_intent_order():
    plan = _plan("What are the weaknesses and strengths?")
    result = InternalComposer().compose(plan)

    assert result.status is CompositionStatus.READY
    assert plan.intent_values == ("weaknesses", "strengths")
    assert result.intents == ("weaknesses", "strengths")


def test_single_deterministic_route_is_not_composed():
    plan = _plan("What is the P/E?")
    result = InternalComposer().compose(plan)

    assert plan.execution_route is ExecutionRoute.DETERMINISTIC_FINANCIAL
    assert result.status is CompositionStatus.NOT_COMPOSABLE
    assert result.route is ExecutionRoute.DETERMINISTIC_FINANCIAL


def test_internal_reasoning_route_is_not_composed_yet():
    plan = _plan("Compare Reliance with TCS.")
    result = InternalComposer().compose(plan)

    assert plan.execution_route is ExecutionRoute.INTERNAL_REASONING
    assert result.status is CompositionStatus.NOT_COMPOSABLE
    assert result.route is ExecutionRoute.INTERNAL_REASONING


def test_decline_route_is_not_composed():
    plan = _plan("")
    result = InternalComposer().compose(plan)

    assert plan.execution_route is ExecutionRoute.DECLINE
    assert result.status is CompositionStatus.NOT_COMPOSABLE
    assert result.route is ExecutionRoute.DECLINE


def test_composer_does_not_change_plan():
    plan = _plan("What is the P/E and financial quality?")
    before = plan.as_dict()

    InternalComposer().compose(plan)

    assert plan.as_dict() == before
