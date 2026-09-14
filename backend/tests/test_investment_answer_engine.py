"""Unit tests for the Phase 2A investment-intelligence answer engine.

The engine is a consumer of two existing artefacts — the context's
citations and the context's ScoreResult — so the fixtures build a
ScoreResult by hand (the analyst-level tests exercise a real one) and
publish its figures under the SAME citation keys the ContextBuilder
publishes. Every answer is verified with the platform's own citation
audit and guardrail check, which is what "no fabricated data" means here:
a figure that is not in the evidence cannot pass, and a marker for a key
that is not in the evidence cannot pass.
"""
from __future__ import annotations

import re

import pytest

from app.domain.ai.types import Citation, EvidenceKind
from app.domain.scoring.base import CategoryScore, ConfidenceBreakdown
from app.services.ai.citation_engine import audit
from app.services.ai.context_builder import GroundedContext
from app.services.ai.financial_intent import (
    INVESTMENT_INTENTS, FinancialIntent,
)
from app.services.ai.guardrails import check
from app.services.ai.investment_answer_engine import InvestmentAnswerEngine
from app.services.scoring.overall_score import ScoreResult

NAME = "Testco Ltd"
TICKER = "TEST"

#: A figure, as the citation audit sees it.
_NUMBER = re.compile(
    r"(?<![\w.])(\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d{2,}(?:\.\d+)?|\d+\.\d+)"
)


def cite(key: str, value, *, unit: str = "", kind: EvidenceKind = EvidenceKind.SCORING,
         label: str | None = None, fiscal_year: int | None = None) -> Citation:
    return Citation(
        key=key, label=label or key.replace("_", " ").title(), kind=kind,
        value=value, unit=unit, source="test fixture", fiscal_year=fiscal_year,
    )


def cat(key: str, label: str, raw: float, *, explanation: str = "",
        confidence: float = 0.9, missing_pct: float = 0.0) -> CategoryScore:
    return CategoryScore(
        key=key, label=label, raw_score=raw,
        weighted_score=raw * 0.1, weight=0.1,
        confidence=ConfidenceBreakdown(
            confidence, max(0.0, 1.0 - missing_pct), 0.0, 0.0,
            missing_pct, 4, 0,
        ),
        metrics=[], explanation=explanation,
    )


def make_score(
    *,
    overall: float = 70.0,
    grade: str = "A",
    recommendation: str = "ACCUMULATE",
    rationale: str = "Composite score of 70.0/100 maps to ACCUMULATE.",
    confidence: float = 0.90,
    conviction: str = "High",
    strongest: tuple[str, ...] = ("Financial Quality", "Financial Risk", "Business Quality"),
    weakest: tuple[str, ...] = ("ESG", "Momentum", "Valuation"),
    warnings: tuple[str, ...] = (),
    categories: list[CategoryScore] | None = None,
) -> ScoreResult:
    if categories is None:
        categories = [
            cat("business_quality", "Business Quality", 8.0, confidence=0.8),
            cat("financial_quality", "Financial Quality", 8.15),
            cat("management_quality", "Management Quality", 6.0, confidence=0.6),
            cat("capital_allocation", "Capital Allocation", 7.5, confidence=0.8),
            cat("competitive_moat", "Competitive Moat", 7.0, confidence=0.7),
            cat("governance", "Corporate Governance", 5.0, confidence=0.0,
                missing_pct=1.0,
                explanation="Corporate Governance could not be assessed — "
                            "no supporting data available."),
            cat("financial_risk", "Financial Risk", 9.0),
            cat("business_risk", "Business Risk", 6.5, confidence=0.7),
            cat("valuation", "Valuation", 3.0, confidence=0.7),
            cat("growth_quality", "Growth Quality", 7.9, confidence=0.85),
            cat("cash_flow_quality", "Cash Flow Quality", 6.0, confidence=0.8),
            cat("esg", "ESG", 5.0, confidence=0.0, missing_pct=1.0,
                explanation="ESG could not be assessed — no supporting data available."),
            cat("momentum", "Momentum", 5.0, confidence=0.0, missing_pct=1.0,
                explanation="Momentum could not be assessed — no supporting data available."),
        ]
    return ScoreResult(
        company_id="c1", ticker=TICKER, name=NAME,
        overall_score=overall, grade=grade, grade_description="test",
        stars=3.5, recommendation=recommendation,
        recommendation_rationale=rationale, conviction=conviction,
        categories=categories,
        confidence=ConfidenceBreakdown(
            confidence, 1.0 - min(confidence, 1.0), 0.0, 0.0,
            max(0.0, 1.0 - confidence), 13, 0,
        ),
        profile_key="balanced", profile_label="Balanced",
        strongest=list(strongest), weakest=list(weakest),
        warnings=list(warnings), summary="",
    )


def make_context(score: ScoreResult | None = None, *, drop: tuple[str, ...] = (),
                 extra: tuple[Citation, ...] = ()) -> GroundedContext:
    """The context a ContextBuilder would carry: pre-existing evidence plus
    the scoring citations for `score` (the same keys `_add_scoring`
    publishes), plus the result itself."""
    citations: list[Citation] = [
        # Pre-existing market/statement/ratio/forecast/valuation evidence
        cite("price", 125.0, unit="₹", kind=EvidenceKind.MARKET),
        cite("market_cap", 25000.0, unit="₹ cr", kind=EvidenceKind.MARKET),
        cite("roe_avg", 0.167, unit="%", kind=EvidenceKind.RATIO, fiscal_year=2025),
        cite("ebitda_margin", 0.18, unit="%", kind=EvidenceKind.STATEMENT, fiscal_year=2025),
        cite("pat_margin", 0.10, unit="%", kind=EvidenceKind.STATEMENT, fiscal_year=2025),
        cite("revenue_growth", 0.12, unit="%", kind=EvidenceKind.STATEMENT, fiscal_year=2025),
        cite("pat_growth", 0.15, unit="%", kind=EvidenceKind.STATEMENT, fiscal_year=2025),
        cite("gross_debt", 500.0, unit="₹ cr", kind=EvidenceKind.STATEMENT, fiscal_year=2025),
        cite("net_debt", 100.0, unit="₹ cr", kind=EvidenceKind.STATEMENT, fiscal_year=2025),
        cite("net_debt_ebitda", 0.5, unit="x", kind=EvidenceKind.RATIO, fiscal_year=2025),
        cite("interest_coverage", 10.0, unit="x", kind=EvidenceKind.RATIO, fiscal_year=2025),
        cite("current_ratio", 2.0, unit="x", kind=EvidenceKind.RATIO, fiscal_year=2025),
        cite("altman_z", 4.5, unit="ratio", kind=EvidenceKind.RATIO, fiscal_year=2025),
        cite("forecast_revenue_cagr", 0.08, unit="%", kind=EvidenceKind.FORECAST),
        cite("weighted_value", 150.0, unit="₹", kind=EvidenceKind.VALUATION),
        cite("valuation_upside", 0.20, unit="%", kind=EvidenceKind.VALUATION),
        cite("valuation_recommendation", "Accumulate", kind=EvidenceKind.VALUATION),
    ]
    if score is not None:
        citations.append(cite("overall_score", score.overall_score, unit="/100"))
        citations.append(cite("grade", score.grade))
        citations.append(cite("recommendation", score.recommendation))
        citations.append(cite("confidence", score.confidence.confidence, unit="%"))
        for c in score.categories:
            citations.append(cite(f"score_{c.key}", c.raw_score, unit="/10"))
    keep = [c for c in [*citations, *extra] if c.key not in drop]
    return GroundedContext(
        company_id="c1", ticker=TICKER, name=NAME, citations=keep, score=score,
    )


ENGINE = InvestmentAnswerEngine()


def audited(intent: FinancialIntent, context: GroundedContext):
    """The answer must survive the platform's own audit AND guardrails —
    the same checks the analyst's funnel applies before it ships."""
    answer = ENGINE.answer(intent, context)
    a = audit(answer.content, context.citations)
    assert a.unknown_keys == [], f"unresolvable citations: {a.unknown_keys}"
    assert a.uncited_numbers == [], (
        f"figures with no platform evidence: {a.uncited_numbers}"
    )
    assert a.is_supported, a.summary
    g = check(answer.content, a)
    assert g.passed, f"guardrail violations: {g.violations}"
    return answer


def all_intents() -> list[FinancialIntent]:
    return [i for i in FinancialIntent if i in INVESTMENT_INTENTS]


# ---------------------------------------------------------------------------
# Complete ScoreResult: every intent, clean audit, correct keys
# ---------------------------------------------------------------------------
class TestFullScoreResult:
    @pytest.mark.parametrize("intent", all_intents())
    def test_answer_is_audit_and_guardrail_clean(self, intent):
        context = make_context(make_score())
        answer = audited(intent, context)
        # No synthetic citations: every marker resolves, every used
        # citation is one the context actually carries.
        keys = {c.key for c in context.citations}
        markers = set(re.findall(r"\[([a-z][a-z0-9_.]*)\]", answer.content))
        assert markers <= keys
        assert all(c.key in keys for c in answer.used_citations)

    @pytest.mark.parametrize("intent", all_intents())
    def test_answer_is_deterministic(self, intent):
        context = make_context(make_score())
        first = ENGINE.answer(intent, context).content
        second = ENGINE.answer(intent, context).content
        assert first == second

    def test_overall_assessment_uses_the_canonical_keys(self):
        answer = audited(FinancialIntent.OVERALL_ASSESSMENT,
                         make_context(make_score()))
        for key in ("overall_score", "grade", "recommendation", "confidence"):
            assert f"[{key}]" in answer.content
        # Strongest and weakest are the ScoreResult's own ranking, with the
        # category score under its own existing citation.
        assert "[score_financial_quality]" in answer.content
        assert "[score_valuation]" in answer.content
        assert "'ACCUMULATE'" in answer.content
        assert "70.00 /100" in answer.content
        assert "'A'" in answer.content

    def test_recommendation_is_the_scoring_output_not_fresh_advice(self):
        answer = audited(FinancialIntent.RECOMMENDATION,
                         make_context(make_score()))
        assert "'ACCUMULATE'" in answer.content
        assert "[recommendation]" in answer.content
        assert "not a fresh call" in answer.content.lower()
        low = answer.content.lower()
        for phrase in ("you should", "guaranteed", "must buy", "is a great buy"):
            assert phrase not in low

    def test_strengths_and_weaknesses_are_the_scored_ranking(self):
        context = make_context(make_score())
        s = audited(FinancialIntent.STRENGTHS, context)
        assert "Financial Quality" in s.content
        assert "Financial Risk" in s.content
        assert "[score_financial_quality]" in s.content
        w = audited(FinancialIntent.WEAKNESSES, context)
        assert "Valuation" in w.content
        assert "[score_valuation]" in w.content
        assert "ESG" in w.content

    def test_category_intents_cite_their_score_and_existing_evidence(self):
        context = make_context(make_score())
        fq = audited(FinancialIntent.FINANCIAL_QUALITY, context)
        assert "[score_financial_quality]" in fq.content
        assert "[roe_avg]" in fq.content
        gq = audited(FinancialIntent.GROWTH_QUALITY, context)
        assert "[score_growth_quality]" in gq.content
        assert "[revenue_growth]" in gq.content
        assert "[forecast_revenue_cagr]" in gq.content
        fr = audited(FinancialIntent.FINANCIAL_RISK, context)
        assert "[score_financial_risk]" in fr.content
        assert "[net_debt_ebitda]" in fr.content
        assert "[interest_coverage]" in fr.content

    def test_investment_case_has_the_required_structure(self):
        answer = audited(FinancialIntent.INVESTMENT_CASE,
                         make_context(make_score()))
        assert "Why consider:" in answer.content
        assert "Risks / what can go wrong:" in answer.content
        assert "Bottom line:" in answer.content
        assert "[overall_score]" in answer.content
        # The bottom line is the existing recommendation, confidence and
        # rationale — all under their existing citations.
        assert "'ACCUMULATE'" in answer.content
        assert "[recommendation]" in answer.content
        assert "[confidence]" in answer.content


# ---------------------------------------------------------------------------
# Missing evidence — said, never invented
# ---------------------------------------------------------------------------
class TestMissingEvidence:
    def test_no_score_at_all(self):
        context = make_context(None)
        for intent in all_intents():
            answer = audited(intent, context)
            low = answer.content.lower()
            assert "unavailable" in low or "cannot" in low, intent
            # No scoring figure is invented: no scoring citation is cited…
            assert not any(k.startswith("score_") or k in (
                "overall_score", "grade", "recommendation", "confidence"
            ) for k in re.findall(r"\[([a-z][a-z0-9_.]*)\]", answer.content)), (
                f"{intent} cited scoring evidence that does not exist: "
                f"{answer.content}"
            )
        # The intents whose answers carry no other evidence state nothing
        # numeric at all. (The category-score intents may still show the
        # platform's own cited growth/financial evidence, which is real
        # evidence — but never a score.)
        for intent in (FinancialIntent.OVERALL_ASSESSMENT, FinancialIntent.STRENGTHS,
                       FinancialIntent.WEAKNESSES, FinancialIntent.RECOMMENDATION,
                       FinancialIntent.INVESTMENT_CASE):
            answer = ENGINE.answer(intent, context)
            assert not _NUMBER.search(answer.content), (
                f"{intent} fabricated figures with no scoring evidence: "
                f"{answer.content}"
            )

    def test_overall_score_says_it_cannot_be_determined(self):
        answer = audited(FinancialIntent.OVERALL_ASSESSMENT, make_context(None))
        assert "cannot be fully determined" in answer.content
        assert answer.missing == ["overall_score"]

    def test_missing_category_citation(self):
        score = make_score()
        context = make_context(score, drop=("score_financial_quality",))
        answer = audited(FinancialIntent.FINANCIAL_QUALITY, context)
        assert "unavailable from the current scoring evidence" in answer.content
        assert answer.missing == ["score_financial_quality"]
        assert "[score_financial_quality]" not in answer.content

    def test_missing_confidence_is_not_a_guess(self):
        context = make_context(make_score(), drop=("confidence",))
        answer = audited(FinancialIntent.RECOMMENDATION, context)
        assert "[confidence]" not in answer.content
        assert "data confidence" not in answer.content.lower()
        # The recommendation itself is still the scoring output.
        assert "'ACCUMULATE'" in answer.content

    def test_missing_valuation_evidence(self):
        context = make_context(
            make_score(),
            drop=("weighted_value", "valuation_upside", "valuation_recommendation"),
        )
        o = audited(FinancialIntent.OVERALL_ASSESSMENT, context)
        assert "No valuation evidence is available" in o.content
        assert "[weighted_value]" not in o.content
        c = audited(FinancialIntent.INVESTMENT_CASE, context)
        assert "no price or upside is stated" in c.content

    def test_strengths_without_a_score(self):
        answer = audited(FinancialIntent.STRENGTHS, make_context(None))
        assert "cannot be listed" in answer.content

    def test_strengths_with_an_empty_ranking(self):
        score = make_score(strongest=(), weakest=())
        context = make_context(score)
        s = audited(FinancialIntent.STRENGTHS, context)
        assert "cannot be listed" in s.content

    def test_category_without_its_evidence_only_states_what_exists(self):
        context = make_context(make_score(),
                               drop=("net_debt_ebitda", "interest_coverage",
                                     "current_ratio", "altman_z",
                                     "gross_debt", "net_debt"))
        answer = audited(FinancialIntent.FINANCIAL_RISK, context)
        assert "[score_financial_risk]" in answer.content
        for key in ("net_debt_ebitda", "interest_coverage", "current_ratio",
                    "altman_z", "gross_debt", "net_debt"):
            assert f"[{key}]" not in answer.content


# ---------------------------------------------------------------------------
# Warnings and the recommendation rationale
# ---------------------------------------------------------------------------
class TestWarningsAndRationale:
    def test_numberless_warnings_are_quoted_numeric_ones_are_not_pasted(self):
        score = make_score(
            recommendation="HOLD",
            rationale="Composite score of 71.0/100 maps to ACCUMULATE. "
                      "Capped at HOLD: confidence is only 65% (35% of "
                      "weighted inputs are missing), which does not support "
                      "a directional call.",
            confidence=0.65,
            warnings=(
                "Valuation is the binding constraint on this recommendation.",
                "Low confidence — 35% of weighted inputs are missing.",
            ),
        )
        answer = audited(FinancialIntent.RECOMMENDATION, make_context(score))
        # The numberless warning is quoted…
        assert "Valuation is the binding constraint" in answer.content
        # …the numeric one is rendered from the citation, never pasted:
        assert "35%" not in answer.content
        assert "data confidence is only 65.00 % [confidence]" in answer.content

    def test_valuation_cap_is_stated_with_its_citation(self):
        score = make_score(
            recommendation="HOLD",
            rationale="Composite score of 71.0/100 maps to ACCUMULATE. "
                      "Capped at HOLD: valuation scores 2.7/10, so the "
                      "shares are expensive regardless of business quality.",
        )
        answer = audited(FinancialIntent.RECOMMENDATION, make_context(score))
        assert "initially mapped to ACCUMULATE" in answer.content
        assert "capped it at HOLD" in answer.content
        assert "the valuation category scores 3.00 /10 [score_valuation]" in answer.content

    def test_balance_sheet_cap_is_stated_with_its_citation(self):
        score = make_score(
            recommendation="REDUCE",
            rationale="Composite score of 40.0/100 maps to HOLD. "
                      "Capped at REDUCE: financial risk scores 2.9/10, "
                      "indicating balance-sheet fragility.",
        )
        answer = audited(FinancialIntent.RECOMMENDATION, make_context(score))
        assert "capped it at REDUCE" in answer.content
        assert "the financial risk category scores 9.00 /10 [score_financial_risk]" in answer.content

    def test_no_cap_means_no_invented_cap(self):
        answer = audited(FinancialIntent.RECOMMENDATION, make_context(make_score()))
        assert "capped" not in answer.content.lower()
        low = answer.content.lower()
        assert "p/e is low" not in low
        assert "because the pe" not in low

    def test_low_confidence_flag_renders_the_confidence_citation(self):
        score = make_score(
            recommendation="HOLD", confidence=0.45,
            rationale="Composite score of 55.0/100 maps to ACCUMULATE. "
                      "Capped at HOLD: confidence is only 45% (55% of "
                      "weighted inputs are missing), which does not "
                      "support a directional call.",
            warnings=("Low confidence — 55% of weighted inputs are missing.",),
        )
        answer = audited(FinancialIntent.WEAKNESSES, make_context(score))
        assert "45.00 % [confidence]" in answer.content
        assert "should be read as provisional" in answer.content
        assert "55%" not in answer.content


# ---------------------------------------------------------------------------
# Edge scores
# ---------------------------------------------------------------------------
class TestEdgeScores:
    def test_zero_score(self):
        score = make_score(overall=0.0, grade="C", recommendation="SELL",
                           rationale="Composite score of 0.0/100 maps to SELL.")
        answer = audited(FinancialIntent.OVERALL_ASSESSMENT, make_context(score))
        assert "0.00 /100 [overall_score]" in answer.content
        assert "'C'" in answer.content
        assert "'SELL'" in answer.content

    def test_perfect_score(self):
        score = make_score(overall=100.0, grade="AAA", recommendation="BUY",
                           rationale="Composite score of 100.0/100 maps to BUY.")
        answer = audited(FinancialIntent.OVERALL_ASSESSMENT, make_context(score))
        assert "100.00 /100 [overall_score]" in answer.content
        assert "'AAA'" in answer.content
        assert "'BUY'" in answer.content

    def test_zero_confidence(self):
        score = make_score(confidence=0.0)
        answer = audited(FinancialIntent.RECOMMENDATION, make_context(score))
        assert "0.00 % [confidence]" in answer.content


# ---------------------------------------------------------------------------
# Category narratives — quoted only when the audit backs every figure
# ---------------------------------------------------------------------------
class TestNarrativeGating:
    def test_backed_narrative_is_quoted_with_markers(self):
        score = make_score()
        fq = next(c for c in score.categories if c.key == "financial_quality")
        from dataclasses import replace
        score2 = replace(score, categories=[
            replace(fq, explanation=(
                "Financial Quality is strong at 8.15/10. Strongest: Return "
                "on equity is 16.7% — healthy returns."
            )),
            *score.categories[1:],
        ])
        context = make_context(score2)
        answer = audited(FinancialIntent.FINANCIAL_QUALITY, context)
        assert "Financial Quality is strong at 8.15/10 [score_financial_quality]." in answer.content
        assert "Return on equity is 16.7% — healthy returns [score_financial_quality]." in answer.content

    def test_unbacked_narrative_figure_drops_the_sentence_not_the_answer(self):
        score = make_score()
        fq = next(c for c in score.categories if c.key == "financial_quality")
        from dataclasses import replace
        score2 = replace(score, categories=[
            replace(fq, explanation=(
                "Financial Quality is strong at 8.15/10. Strongest: Debt "
                "turned over at 37.2x in the year."
            )),
            *score.categories[1:],
        ])
        context = make_context(score2)
        answer = audited(FinancialIntent.FINANCIAL_QUALITY, context)
        # The backed sentence stays…
        assert "Financial Quality is strong at 8.15/10 [score_financial_quality]." in answer.content
        # …the figure with no citation anywhere in the evidence does not.
        assert "37.2" not in answer.content

    def test_opinion_language_in_a_narrative_is_dropped(self):
        score = make_score()
        fq = next(c for c in score.categories if c.key == "financial_quality")
        from dataclasses import replace
        score2 = replace(score, categories=[
            replace(fq, explanation=(
                "Financial Quality is strong at 8.15/10. The margins are "
                "impressive across the board."
            )),
            *score.categories[1:],
        ])
        context = make_context(score2)
        answer = audited(FinancialIntent.FINANCIAL_QUALITY, context)
        assert "impressive" not in answer.content
        assert "Financial Quality is strong at 8.15/10 [score_financial_quality]." in answer.content


# ---------------------------------------------------------------------------
# No fabrication, no synthetic citations
# ---------------------------------------------------------------------------
class TestNoFabrication:
    @pytest.mark.parametrize("intent", all_intents())
    def test_only_existing_keys_are_cited(self, intent):
        context = make_context(make_score())
        answer = ENGINE.answer(intent, context)
        markers = set(re.findall(r"\[([a-z][a-z0-9_.]*)\]", answer.content))
        assert markers <= {c.key for c in context.citations}

    @pytest.mark.parametrize("intent", all_intents())
    def test_no_number_outside_the_evidence(self, intent):
        context = make_context(make_score())
        a = audit(ENGINE.answer(intent, context).content, context.citations)
        assert a.uncited_numbers == []

    def test_pe_is_never_invoked_as_the_reason_for_a_recommendation(self):
        # "BUY because P/E is low" must not appear unless the scoring result
        # says so — a low P/E in the evidence alone is not that.
        score = make_score(
            recommendation="BUY",
            rationale="Composite score of 80.0/100 maps to BUY.",
            overall=80.0,
        )
        context = make_context(score, extra=(cite("pe_ratio", 4.0, unit="x",
                                                  kind=EvidenceKind.VALUATION),))
        answer = audited(FinancialIntent.RECOMMENDATION, context)
        assert "[pe_ratio]" not in answer.content
        assert "p/e" not in answer.content.lower()
