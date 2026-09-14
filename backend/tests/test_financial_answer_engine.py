"""Unit tests for the deterministic financial answer engine.

Covers every supported intent, the exact citation keys each answer uses, the
missing-evidence behaviour, determinism, and the no-fabrication property
(verifying each answer with the platform's own citation audit against the
evidence the answer was built from).

The intent set now spans both deterministic engines (Phase 1 canonical
figures and Phase 2A investment intelligence), so the cross-cutting tests
below dispatch each intent to its own engine — the same dispatch the analyst
performs — and the fixture context carries a real ``ScoreResult`` alongside
its citations, exactly as the ContextBuilder produces them together.
"""
from __future__ import annotations

import functools

import pytest

from app.domain.ai.types import Citation, EvidenceKind
from app.domain.scoring.inputs import ScoringInputs
from app.domain.scoring.weights import DEFAULT_PROFILE
from app.domain.financials.statements import (
    build_balance_sheet, build_cash_flow, build_income_statement,
)
from app.services.ai.citation_engine import audit
from app.services.ai.context_builder import GroundedContext
from app.services.ai.financial_answer_engine import (
    DeterministicAnswer, FinancialAnswerEngine,
)
from app.services.ai.financial_intent import (
    INVESTMENT_INTENTS, FinancialIntent, FinancialIntentResolver,
)
from app.services.ai.investment_answer_engine import InvestmentAnswerEngine
from app.services.scoring.overall_score import ScoreResult, compute_score
from tests.conftest import make_financials

NAME = "Acme Industries"
TICKER = "ACME"


def cite(key: str, value, *, unit: str = "", kind: EvidenceKind = EvidenceKind.STATEMENT,
         fiscal_year: int | None = 2025, label: str | None = None) -> Citation:
    return Citation(
        key=key, label=label or key.replace("_", " ").title(), kind=kind,
        value=value, unit=unit, source="test fixture", fiscal_year=fiscal_year,
    )


@functools.lru_cache(maxsize=1)
def reference_score() -> ScoreResult:
    """A REAL ScoreResult: the existing scoring engine run over the shared
    fixture statements. The Phase 2A engine under test must interpret exactly
    this object — a hand-typed result would let the test and the engine drift
    apart about what a ScoreResult contains."""
    fin = make_financials()
    years = list(fin.fiscal_years)
    inputs = ScoringInputs(
        company_id="c1", ticker=TICKER, name=NAME,
        incomes=[build_income_statement(fin, y) for y in years],
        balances=[build_balance_sheet(fin, y) for y in years],
        cash_flows=[build_cash_flow(fin, y) for y in years],
        current_price=1200.0,
        wacc=0.114, cost_of_equity=0.131,
        intrinsic_value=1390.0, upside=0.158,
        ev_ebitda=9.5, pe_ratio=24.0,
    )
    return compute_score(inputs, DEFAULT_PROFILE)


def full_context(**drop: bool) -> GroundedContext:
    """The richest context the ContextBuilder can carry for these intents.

    The scoring citations mirror exactly what
    ``ContextBuilder._add_scoring`` publishes for the result this context
    also carries in ``score`` — so the Phase 2A intents are exercised the
    way production assembles them.
    """
    score = reference_score()
    citations = [
        # Market
        cite("price", 1200.0, unit="₹", kind=EvidenceKind.MARKET, fiscal_year=None),
        cite("market_cap", 300000.0, unit="₹ cr", kind=EvidenceKind.MARKET, fiscal_year=None),
        # Statements
        cite("revenue", 10000.0, unit="₹ cr"),
        cite("pat", 1500.0, unit="₹ cr"),
        cite("eps", 12.5, unit="₹"),
        cite("equity", 9000.0, unit="₹ cr"),
        cite("total_assets", 15000.0, unit="₹ cr"),
        cite("gross_debt", 2000.0, unit="₹ cr"),
        cite("net_debt", 800.0, unit="₹ cr"),
        # Ratios
        cite("roe_avg", 0.167, unit="%", kind=EvidenceKind.RATIO),
        cite("roce", 0.125, unit="%", kind=EvidenceKind.RATIO),
        cite("net_debt_ebitda", 0.62, unit="x", kind=EvidenceKind.RATIO),
        # Growth (from the canonical financial engine)
        cite("revenue_growth", 0.19, unit="%"),
        cite("pat_growth", 0.22, unit="%"),
        # Valuation
        cite("wacc", 0.114, unit="%", kind=EvidenceKind.VALUATION, fiscal_year=None),
        cite("cost_of_equity", 0.131, unit="%", kind=EvidenceKind.VALUATION, fiscal_year=None),
        cite("dcf_value", 1350.0, unit="₹", kind=EvidenceKind.VALUATION, fiscal_year=None),
        cite("dcf_upside", 0.125, unit="%", kind=EvidenceKind.VALUATION, fiscal_year=None),
        cite("relative_target", 1420.0, unit="₹", kind=EvidenceKind.VALUATION, fiscal_year=None),
        cite("pe_ratio", 24.0, unit="x", kind=EvidenceKind.VALUATION, fiscal_year=None),
        cite("ev_ebitda", 9.5, unit="x", kind=EvidenceKind.VALUATION, fiscal_year=None),
        cite("weighted_value", 1390.0, unit="₹", kind=EvidenceKind.VALUATION, fiscal_year=None),
        cite("valuation_upside", 0.158, unit="%", kind=EvidenceKind.VALUATION, fiscal_year=None),
        cite("valuation_recommendation", "Accumulate", kind=EvidenceKind.VALUATION, fiscal_year=None),
        cite("data_quality", "illustrative", kind=EvidenceKind.VALUATION, fiscal_year=None),
        # Scoring — the same keys ContextBuilder._add_scoring publishes
        cite("overall_score", score.overall_score, unit="/100",
             kind=EvidenceKind.SCORING, fiscal_year=None),
        cite("grade", score.grade, kind=EvidenceKind.SCORING, fiscal_year=None),
        cite("recommendation", score.recommendation,
             kind=EvidenceKind.SCORING, fiscal_year=None),
        cite("confidence", score.confidence.confidence, unit="%",
             kind=EvidenceKind.SCORING, fiscal_year=None),
    ]
    for category in score.categories:
        citations.append(cite(f"score_{category.key}", category.raw_score,
                              unit="/10", kind=EvidenceKind.SCORING,
                              fiscal_year=None))
    keep = [c for c in citations if not drop.get(c.key, False)]
    return GroundedContext(
        company_id="c1", ticker=TICKER, name=NAME, citations=keep,
        score=score,
    )


#: The analyst's dispatch rule, mirrored here: the Phase 1 engine answers the
#: canonical figure intents, the Phase 2A engine the investment intents.
def engine_for(intent: FinancialIntent):
    if intent in INVESTMENT_INTENTS:
        return InvestmentAnswerEngine()
    return FinancialAnswerEngine()


PHASE1_ENGINE = FinancialAnswerEngine()
RESOLVER = FinancialIntentResolver()


def audited_answer(intent: FinancialIntent, context: GroundedContext) -> DeterministicAnswer:
    answer = engine_for(intent).answer(intent, context)
    a = audit(answer.content, context.citations)
    assert a.unknown_keys == [], f"unresolvable citations: {a.unknown_keys}"
    assert a.uncited_numbers == [], (
        f"figures with no platform evidence: {a.uncited_numbers}"
    )
    assert a.is_supported, a.summary
    return answer


# ---------------------------------------------------------------------------
# Resolver: one intent per question, or none
# ---------------------------------------------------------------------------
class TestFinancialIntentResolver:
    @pytest.mark.parametrize("question,intent", [
        ("What is the current P/E of the company?", FinancialIntent.PE),
        ("What is the P/E ratio?", FinancialIntent.PE),
        ("What is the price to earnings ratio?", FinancialIntent.PE),
        ("What is the P/B?", FinancialIntent.PB),
        ("What is the price to book ratio?", FinancialIntent.PB),
        ("What is the total debt?", FinancialIntent.DEBT),
        ("How leveraged is the company?", FinancialIntent.DEBT),
        ("What are its borrowings?", FinancialIntent.DEBT),
        ("What is the ROE?", FinancialIntent.ROE),
        ("What is the return on equity?", FinancialIntent.ROE),
        ("What is the ROCE?", FinancialIntent.ROCE),
        ("What is the return on capital employed?", FinancialIntent.ROCE),
        ("What is the profit growth?", FinancialIntent.PROFIT_GROWTH),
        ("How fast is profit growing?", FinancialIntent.PROFIT_GROWTH),
        ("What is the EPS growth?", FinancialIntent.PROFIT_GROWTH),
        ("What is the revenue growth?", FinancialIntent.REVENUE_GROWTH),
        ("How is sales growth looking?", FinancialIntent.REVENUE_GROWTH),
        ("What is the EPS?", FinancialIntent.EPS),
        ("What is the earnings per share?", FinancialIntent.EPS),
        ("What is the current market price?", FinancialIntent.MARKET_PRICE),
        ("What is the share price?", FinancialIntent.MARKET_PRICE),
        ("What is the valuation?", FinancialIntent.VALUATION),
        ("What is the DCF value?", FinancialIntent.VALUATION),
        ("Is the stock overvalued?", FinancialIntent.VALUATION),
        ("What is the target price?", FinancialIntent.VALUATION),
        ("How is the stock valued?", FinancialIntent.VALUATION),
    ])
    def test_single_intent_recognised(self, question, intent):
        assert RESOLVER.resolve(question) is intent

    @pytest.mark.parametrize("question", [
        # Two financial intents: a deterministic answer would be a partial
        # answer to a multi-part question, so it must fall through.
        "What is the P/E and the ROE?",
        "What is the debt and the revenue growth?",
        "What is the revenue growth and the profit growth?",
        "What is the current price and the P/E?",
        "How leveraged is it, and what is the ROCE?",
        # No supported financial intent at all.
        "Tell me about the company's business model.",
        "What are the main risks?",
        "Who is the CEO?",
        "",
        "   ",
    ])
    def test_ambiguous_or_unsupported_resolves_to_none(self, question):
        assert RESOLVER.resolve(question) is None

    def test_price_to_earnings_is_not_a_price_question(self):
        # "price" here is part of the ratio's name, not the share price.
        assert RESOLVER.resolve("What is the price to earnings ratio?") is FinancialIntent.PE
        assert RESOLVER.resolve("What is the current price to earnings ratio?") is FinancialIntent.PE

    def test_price_to_book_is_not_a_price_question(self):
        assert RESOLVER.resolve("What is the current price to book ratio?") is FinancialIntent.PB

    def test_case_and_whitespace_insensitive(self):
        assert RESOLVER.resolve("  WHAT   IS   THE   p/e  ?") is FinancialIntent.PE


# ---------------------------------------------------------------------------
# Engine: every supported intent
# ---------------------------------------------------------------------------
class TestPE:
    def test_uses_pe_ratio_and_explains_it(self):
        answer = audited_answer(FinancialIntent.PE, full_context())
        assert "[pe_ratio]" in answer.content
        assert "24.00x" in answer.content
        # The formula is shown against the underlying evidence.
        assert "[price]" in answer.content and "[eps]" in answer.content
        # The platform never declares cheap/expensive from P/E alone.
        low = answer.content.lower()
        assert "cheap" in low and "expensive" in low  # the hedge, stated plainly
        assert "not a verdict" in low

    def test_missing_pe_ratio_is_said_not_invented(self):
        context = full_context(pe_ratio=True)
        answer = audited_answer(FinancialIntent.PE, context)
        assert answer.missing == ["pe_ratio"]
        assert "not available" in answer.content.lower()
        assert "[pe_ratio]" not in answer.content


class TestPB:
    def test_unavailable_without_a_canonical_pb_citation(self):
        # The ContextBuilder publishes no `pb` citation today; the engine must
        # say P/B is unavailable, not derive it from price and book value.
        context = full_context()
        assert not any(c.key == "pb" for c in context.citations)
        answer = audited_answer(FinancialIntent.PB, context)
        assert answer.missing == ["pb"]
        assert "not available from the current canonical evidence" in answer.content
        assert "[pb]" not in answer.content
        # And it did not compute one: no price/book division appears.
        assert "1,200.00" not in answer.content
        assert "9,000.00" not in answer.content

    def test_used_when_a_canonical_pb_citation_exists(self):
        context = full_context()
        context.add(cite("pb", 3.2, unit="x", kind=EvidenceKind.VALUATION, fiscal_year=None))
        answer = audited_answer(FinancialIntent.PB, context)
        assert answer.missing == []
        assert "[pb]" in answer.content
        assert "3.20x" in answer.content


class TestDebt:
    def test_uses_the_debt_citations_that_exist(self):
        answer = audited_answer(FinancialIntent.DEBT, full_context())
        assert "[gross_debt]" in answer.content
        assert "[net_debt]" in answer.content
        assert "[net_debt_ebitda]" in answer.content
        assert "2,000.00" in answer.content
        assert "800.00" in answer.content
        assert "0.62" in answer.content
        assert answer.missing == []

    def test_partial_evidence_reports_the_rest(self):
        context = full_context(net_debt_ebitda=True)
        answer = audited_answer(FinancialIntent.DEBT, context)
        assert "[gross_debt]" in answer.content
        assert "[net_debt]" in answer.content
        assert "[net_debt_ebitda]" not in answer.content
        assert "net_debt_ebitda" in answer.missing

    def test_no_debt_figures_says_unavailable(self):
        context = full_context(gross_debt=True, net_debt=True, net_debt_ebitda=True)
        answer = audited_answer(FinancialIntent.DEBT, context)
        assert answer.missing == ["gross_debt", "net_debt", "net_debt_ebitda"]
        assert "not available" in answer.content.lower()

    def test_does_not_invent_debt_to_equity(self):
        answer = audited_answer(FinancialIntent.DEBT, full_context())
        low = answer.content.lower()
        assert "debt-to-equity" not in low
        assert "debt to equity" not in low
        assert "debt/equity" not in low


class TestROE:
    def test_uses_roe_avg_and_is_not_a_recommendation(self):
        answer = audited_answer(FinancialIntent.ROE, full_context())
        assert "[roe_avg]" in answer.content
        assert "16.70 %" in answer.content
        # The underlying equity figure is cited in the formula sentence.
        assert "[equity]" in answer.content
        low = answer.content.lower()
        assert "not an investment recommendation" in low
        assert "buy" not in low

    def test_missing_roe_is_said(self):
        answer = audited_answer(FinancialIntent.ROE, full_context(roe_avg=True))
        assert answer.missing == ["roe_avg"]
        assert "not available" in answer.content.lower()
        assert "[roe_avg]" not in answer.content


class TestROCE:
    def test_uses_roce_and_explains_it(self):
        answer = audited_answer(FinancialIntent.ROCE, full_context())
        assert "[roce]" in answer.content
        assert "12.50 %" in answer.content
        low = answer.content.lower()
        assert "not an investment recommendation" in low

    def test_missing_roce_is_said(self):
        answer = audited_answer(FinancialIntent.ROCE, full_context(roce=True))
        assert answer.missing == ["roce"]
        assert "not available" in answer.content.lower()


class TestGrowth:
    def test_revenue_growth_states_the_period(self):
        answer = audited_answer(FinancialIntent.REVENUE_GROWTH, full_context())
        assert "[revenue_growth]" in answer.content
        assert "19.00 %" in answer.content
        assert "FY25" in answer.content
        # The level it grew from is the platform's own citation.
        assert "[revenue]" in answer.content
        assert answer.missing == []

    def test_profit_growth_states_the_period(self):
        answer = audited_answer(FinancialIntent.PROFIT_GROWTH, full_context())
        assert "[pat_growth]" in answer.content
        assert "22.00 %" in answer.content
        assert "FY25" in answer.content
        assert "[pat]" in answer.content

    def test_missing_growth_is_said(self):
        context = full_context(revenue_growth=True, pat_growth=True)
        r = audited_answer(FinancialIntent.REVENUE_GROWTH, context)
        p = audited_answer(FinancialIntent.PROFIT_GROWTH, context)
        assert r.missing == ["revenue_growth"]
        assert p.missing == ["pat_growth"]
        assert "not available" in r.content.lower()
        assert "not available" in p.content.lower()


class TestEPS:
    def test_uses_eps_without_recomputing_it(self):
        answer = audited_answer(FinancialIntent.EPS, full_context())
        assert "[eps]" in answer.content
        assert "12.50" in answer.content
        # The engine does not divide PAT by shares itself.
        assert "[pat]" not in answer.content

    def test_missing_eps_is_said(self):
        answer = audited_answer(FinancialIntent.EPS, full_context(eps=True))
        assert answer.missing == ["eps"]
        assert "not available" in answer.content.lower()


class TestMarketPrice:
    def test_uses_price_and_market_cap(self):
        answer = audited_answer(FinancialIntent.MARKET_PRICE, full_context())
        assert "[price]" in answer.content
        assert "1,200.00" in answer.content
        assert "[market_cap]" in answer.content
        assert "300,000.00" in answer.content

    def test_missing_price_is_said(self):
        answer = audited_answer(FinancialIntent.MARKET_PRICE, full_context(price=True))
        assert answer.missing == ["price"]
        assert "not available" in answer.content.lower()
        assert "[price]" not in answer.content


class TestValuation:
    def test_uses_multiple_available_valuation_fields(self):
        answer = audited_answer(FinancialIntent.VALUATION, full_context())
        for key in (
            "wacc", "cost_of_equity", "dcf_value", "dcf_upside",
            "relative_target", "pe_ratio", "ev_ebitda", "weighted_value",
            "valuation_upside", "valuation_recommendation", "data_quality",
        ):
            assert f"[{key}]" in answer.content, f"valuation answer omitted [{key}]"
        assert answer.missing == []

    def test_never_declares_cheap_or_expensive(self):
        answer = audited_answer(FinancialIntent.VALUATION, full_context())
        low = answer.content.lower()
        for word in ("undervalued", "overvalued"):
            assert word not in low
        assert "not treated, by itself, as a verdict" in low

    def test_pe_alone_is_not_a_valuation_picture(self):
        # Only the relative multiple is present: no DCF, no weighted value.
        # The answer must say the core outputs are unavailable and must not
        # turn the lone P/E into a verdict.
        context = full_context(
            wacc=True, cost_of_equity=True, dcf_value=True, dcf_upside=True,
            relative_target=True, ev_ebitda=True, weighted_value=True,
            valuation_upside=True, valuation_recommendation=True,
            data_quality=True,
        )
        answer = audited_answer(FinancialIntent.VALUATION, context)
        assert "[pe_ratio]" in answer.content
        assert "[dcf_value]" not in answer.content
        assert "[weighted_value]" not in answer.content
        low = answer.content.lower()
        assert "not available in this evidence set" in low
        assert "core valuation outputs are unavailable" in low
        for word in ("undervalued", "overvalued", "cheap", "expensive"):
            assert word not in low

    def test_partial_valuation_lists_what_is_missing(self):
        context = full_context(dcf_upside=True, ev_ebitda=True,
                               valuation_upside=True, data_quality=True)
        answer = audited_answer(FinancialIntent.VALUATION, context)
        # What exists is cited…
        assert "[dcf_value]" in answer.content
        assert "[relative_target]" in answer.content
        # …and what is missing is named, rather than silently dropped.
        assert "[dcf_upside]" not in answer.content
        assert "[ev_ebitda]" not in answer.content
        assert "the DCF upside" in answer.content
        assert "the EV/EBITDA multiple" in answer.content

    def test_no_valuation_evidence_says_unavailable(self):
        context = full_context(
            wacc=True, cost_of_equity=True, dcf_value=True, dcf_upside=True,
            relative_target=True, pe_ratio=True, ev_ebitda=True,
            weighted_value=True, valuation_upside=True,
            valuation_recommendation=True, data_quality=True,
        )
        answer = audited_answer(FinancialIntent.VALUATION, context)
        assert "not available" in answer.content.lower()
        assert len(answer.missing) == 10

    def test_recommends_only_what_the_platform_computed(self):
        answer = audited_answer(FinancialIntent.VALUATION, full_context())
        assert "'Accumulate'" in answer.content
        assert "[valuation_recommendation]" in answer.content


# ---------------------------------------------------------------------------
# Cross-cutting properties
# ---------------------------------------------------------------------------
class TestDeterminismAndFabrication:
    @pytest.mark.parametrize("intent", list(FinancialIntent))
    def test_output_is_deterministic(self, intent):
        context = full_context()
        first = engine_for(intent).answer(intent, context).content
        second = engine_for(intent).answer(intent, context).content
        assert first == second

    def _context(self, intent: FinancialIntent) -> GroundedContext:
        # PB is the one intent whose canonical citation the ContextBuilder
        # does not publish today; give it a canonical one here so every
        # intent is exercised in its available-evidence form.
        context = full_context()
        if intent is FinancialIntent.PB:
            context.add(cite("pb", 3.2, unit="x",
                             kind=EvidenceKind.VALUATION, fiscal_year=None))
        return context

    @pytest.mark.parametrize("intent", list(FinancialIntent))
    def test_every_number_has_platform_evidence(self, intent):
        """The citation audit finds no figure the platform did not compute."""
        answer = audited_answer(intent, self._context(intent))
        assert answer.used_citations
        assert answer.missing == []

    @pytest.mark.parametrize("intent", list(FinancialIntent))
    def test_only_existing_citation_keys_are_cited(self, intent):
        context = full_context()
        answer = engine_for(intent).answer(intent, context)
        import re
        markers = re.findall(r"\[([a-z][a-z0-9_.]*)\]", answer.content)
        assert set(markers) <= {c.key for c in context.citations}

    def test_citations_reused_across_answers(self):
        """An engine instance is stateless: answers do not leak between calls."""
        a1 = PHASE1_ENGINE.answer(FinancialIntent.PE, full_context())
        a2 = PHASE1_ENGINE.answer(FinancialIntent.ROE, full_context())
        assert [c.key for c in a1.used_citations] != [c.key for c in a2.used_citations]
