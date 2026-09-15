"""Tests for the Part 2C internal composition contract."""

from __future__ import annotations

import ast
from dataclasses import FrozenInstanceError, replace

import pytest

from app.domain.ai.types import Citation
from app.services.ai.citation_engine import audit
from app.services.ai.financial_intent import (
    INVESTMENT_INTENTS,
    FinancialIntent,
)
from app.services.ai.internal_composer import (
    CompositionStatus,
    ComposedAnswer,
    InternalComposer,
)
from app.services.ai.planner.question_planner import QuestionPlanner
from app.services.ai.planner.types import (
    ExecutionRoute,
    IntentFamily,
    IntentMatch,
    QueryType,
)
from tests.test_financial_answer_engine import engine_for, full_context


def _plan(question: str):
    return QuestionPlanner().plan(question)


def _engine_answers(plan, context):
    return [engine_for(match.intent).answer(match.intent, context)
            for match in plan.intents]


def _all_intents_plan():
    """Build a composition plan containing the complete current dispatch set."""
    seed = _plan("What is the P/E and what is the financial quality?")
    matches = tuple(
        IntentMatch(
            intent=intent,
            matched_on="test",
            position=position,
            family=(
                IntentFamily.INVESTMENT
                if intent in INVESTMENT_INTENTS
                else IntentFamily.PHASE1
            ),
        )
        for position, intent in enumerate(FinancialIntent)
    )
    return replace(
        seed,
        intents=matches,
        query_type=QueryType.MULTI_INTENT,
        execution_route=ExecutionRoute.COMPOSITION_REQUIRED,
    )


def test_multi_intent_plan_is_ready_for_composition():
    plan = _plan("What is the P/E and what is the financial quality?")
    result = InternalComposer().compose(plan)

    assert plan.execution_route is ExecutionRoute.COMPOSITION_REQUIRED
    assert result.status is CompositionStatus.READY
    assert result.route is ExecutionRoute.COMPOSITION_REQUIRED
    assert result.intents == plan.intent_values
    assert result.reason == "Multiple matched intents require internal composition."


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
    assert result.intents == plan.intent_values
    assert result.reason == (
        "This route is not handled by the Part 2C composition layer."
    )


def test_internal_reasoning_route_is_not_composed_yet():
    plan = _plan("Compare Reliance with TCS.")
    result = InternalComposer().compose(plan)

    assert plan.execution_route is ExecutionRoute.INTERNAL_REASONING
    assert result.status is CompositionStatus.NOT_COMPOSABLE
    assert result.route is ExecutionRoute.INTERNAL_REASONING
    assert result.intents == plan.intent_values


def test_decline_route_is_not_composed():
    plan = _plan("")
    result = InternalComposer().compose(plan)

    assert plan.execution_route is ExecutionRoute.DECLINE
    assert result.status is CompositionStatus.NOT_COMPOSABLE
    assert result.route is ExecutionRoute.DECLINE
    assert result.intents == plan.intent_values


def test_compose_answer_returns_none_for_non_composable_routes():
    context = full_context()
    composer = InternalComposer()

    for question in ("What is the P/E?", "Compare Reliance with TCS.", ""):
        assert composer.compose_answer(_plan(question), context) is None


def test_multi_intent_composition_reuses_existing_engine_text_verbatim():
    plan = _plan("What is the P/E and what is the financial quality?")
    context = full_context()

    result = InternalComposer().compose_answer(plan, context)

    assert result is not None
    answers = _engine_answers(plan, context)
    assert result.intents == tuple(answer.intent for answer in answers)
    assert result.content == " ".join(answer.content for answer in answers)
    assert result.intents == (FinancialIntent.PE, FinancialIntent.FINANCIAL_QUALITY)


def test_reversed_intent_order_controls_composed_content_order():
    plan = _plan("What are the weaknesses and strengths?")
    context = full_context()

    result = InternalComposer().compose_answer(plan, context)

    assert result is not None
    answers = _engine_answers(plan, context)
    assert result.intents == (FinancialIntent.WEAKNESSES, FinancialIntent.STRENGTHS)
    assert result.content == " ".join(answer.content for answer in answers)
    assert result.content.index(answers[0].content) < result.content.index(answers[1].content)


def test_investment_and_financial_intents_use_their_existing_engines():
    plan = _plan("What is the P/E and what are the strengths?")
    context = full_context()

    result = InternalComposer().compose_answer(plan, context)

    assert result is not None
    assert plan.intent_values == ("pe", "strengths")
    assert result.content == " ".join(
        engine_for(match.intent).answer(match.intent, context).content
        for match in plan.intents
    )


def test_every_current_financial_intent_has_a_dispatchable_handler():
    plan = _all_intents_plan()
    result = InternalComposer().compose_answer(plan, full_context())

    assert result is not None
    assert result.intents == tuple(FinancialIntent)
    assert len(result.content) > 0


def test_citations_are_deduplicated_by_key_in_first_use_order():
    plan = _plan("What is the P/E and what is the valuation?")
    context = full_context()
    answers = _engine_answers(plan, context)

    result = InternalComposer().compose_answer(plan, context)

    assert result is not None
    expected: list[Citation] = []
    seen: set[str] = set()
    for answer in answers:
        for citation in answer.used_citations:
            if citation.key not in seen:
                seen.add(citation.key)
                expected.append(citation)
    assert result.used_citations == tuple(expected)
    assert [citation.key for citation in result.used_citations] == [
        citation.key for citation in expected
    ]
    assert len(result.used_citations) == len({c.key for c in result.used_citations})
    assert "pe_ratio" in [citation.key for citation in answers[0].used_citations]
    assert "pe_ratio" in [citation.key for citation in answers[1].used_citations]


def test_missing_evidence_is_merged_without_rewriting_values():
    plan = _plan("What is the debt and what is the P/B?")
    context = full_context(
        gross_debt=True,
        net_debt=True,
        net_debt_ebitda=True,
        pb=True,
    )
    answers = _engine_answers(plan, context)

    result = InternalComposer().compose_answer(plan, context)

    assert result is not None
    expected: list[str] = []
    for answer in answers:
        for value in answer.missing:
            if value not in expected:
                expected.append(value)
    assert result.missing == tuple(expected)
    assert result.missing == (
        "gross_debt", "net_debt", "net_debt_ebitda", "pb",
    )


def test_human_readable_missing_evidence_is_preserved_verbatim():
    plan = _plan("What is the valuation and what is the P/B?")
    valuation_keys = (
        "wacc", "cost_of_equity", "dcf_value", "dcf_upside",
        "relative_target", "pe_ratio", "ev_ebitda", "weighted_value",
        "valuation_upside", "valuation_recommendation",
    )
    context = full_context(
        **{key: key != "wacc" for key in valuation_keys},
        pb=True,
    )

    result = InternalComposer().compose_answer(plan, context)

    assert result is not None
    assert "the trailing P/E" in result.missing
    assert "the weighted intrinsic value" in result.missing
    assert "pb" in result.missing
    assert all(value == value.strip() for value in result.missing)


def test_duplicate_missing_evidence_is_reported_once_without_deduplicating_intents():
    plan = _plan("What is the P/B and what is the P/E?")
    pb_match = next(
        match for match in plan.intents if match.intent is FinancialIntent.PB
    )
    duplicate_plan = replace(
        plan,
        intents=(pb_match, pb_match),
        query_type=QueryType.MULTI_INTENT,
        execution_route=ExecutionRoute.COMPOSITION_REQUIRED,
    )

    result = InternalComposer().compose_answer(
        duplicate_plan, full_context(pb=True),
    )

    assert result is not None
    assert result.intents == (FinancialIntent.PB, FinancialIntent.PB)
    assert result.missing == ("pb",)


def test_composed_citations_pass_the_existing_citation_audit():
    # Use real output from both deterministic engine families. This test does
    # not mask known issues in an engine's own output by weakening the shared
    # audit.
    plan = _plan("What is the P/E and what is the overall assessment?")
    result = InternalComposer().compose_answer(plan, full_context())

    assert result is not None
    verdict = audit(result.content, list(result.used_citations))
    assert verdict.unknown_keys == []
    assert verdict.uncited_numbers == []
    assert verdict.is_supported, verdict.summary


def test_composed_answer_is_frozen_and_uses_tuples():
    answer = ComposedAnswer(
        intents=(FinancialIntent.PE,),
        content="content",
        used_citations=(),
        missing=(),
    )

    with pytest.raises(FrozenInstanceError):
        answer.content = "changed"  # type: ignore[misc]

    assert isinstance(answer.intents, tuple)
    assert isinstance(answer.used_citations, tuple)
    assert isinstance(answer.missing, tuple)


def test_compose_answer_does_not_mutate_the_plan_or_context():
    plan = _plan("What is the P/E and what is the financial quality?")
    context = full_context()
    plan_before = plan.as_dict()
    context_before = tuple(context.citations)

    InternalComposer().compose_answer(plan, context)

    assert plan.as_dict() == plan_before
    assert tuple(context.citations) == context_before


def test_composition_is_repeatable_for_the_same_plan_and_context():
    plan = _plan("What is the P/E and what is the financial quality?")
    context = full_context()
    composer = InternalComposer()

    first = composer.compose_answer(plan, context)
    second = composer.compose_answer(plan, context)

    assert first is not None
    assert second == first


def test_internal_composer_imports_no_provider_or_retrieval_modules():
    from app.services.ai import internal_composer

    tree = ast.parse(internal_composer.__file__ and open(
        internal_composer.__file__, encoding="utf-8",
    ).read())
    imported_modules = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported_modules.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module is not None
        for alias in node.names
    )
    forbidden = (
        "gemini", "openai", "openrouter", "provider", "retrieval", "rag",
    )
    assert not any(
        any(term in module.lower() for term in forbidden)
        for module in imported_modules
    )


def test_composer_does_not_change_plan():
    plan = _plan("What is the P/E and financial quality?")
    before = plan.as_dict()

    InternalComposer().compose(plan)

    assert plan.as_dict() == before
