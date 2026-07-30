"""Unit tests for the scoring primitives, weight engine and category scorers."""
from __future__ import annotations

import pytest

from app.domain.scoring.base import (
    NEUTRAL_SCORE, ConfidenceBreakdown, DataOrigin, MetricScore, aggregate,
    band_score, build_category, build_confidence, clamp_score, linear_score,
    narrate, trend_score,
)
from app.domain.scoring.inputs import QualitativeInputs, ScoringInputs
from app.domain.scoring.weights import (
    BALANCED, BUILTIN_PROFILES, CONSERVATIVE, GROWTH, QUALITY, VALUE,
    Category, WeightProfile, get_profile,
)
from app.services.scoring import (
    business_quality, capital_allocation, cash_flow_quality,
    competitive_advantage, esg, financial_quality, financial_risk, governance,
    growth_quality, management_quality, momentum, risk, valuation,
)
from app.services.scoring.overall_score import (
    GRADE_BANDS, SCORERS, compute_score, grade_for, stars_for,
)
from tests.conftest import make_financials
from app.domain.financials.statements import (
    build_balance_sheet, build_cash_flow, build_income_statement,
)


def build_inputs(**kw) -> ScoringInputs:
    """Scoring inputs from the shared Titan-equivalent fixture."""
    fin = make_financials(**{
        k: v for k, v in kw.items()
        if k in {"scale", "growth", "margin", "shares", "years"}
    })
    years = list(fin.fiscal_years)
    defaults = dict(
        company_id="c1", ticker="TEST", name="Test Company Ltd",
        incomes=[build_income_statement(fin, y) for y in years],
        balances=[build_balance_sheet(fin, y) for y in years],
        cash_flows=[build_cash_flow(fin, y) for y in years],
        wacc=0.12, cost_of_equity=0.145, current_price=100.0,
    )
    defaults.update({k: v for k, v in kw.items() if k not in
                     {"scale", "growth", "margin", "shares", "years"}})
    return ScoringInputs(**defaults)


# ===================================================================== base
class TestBandScore:
    def test_first_matching_band_wins(self):
        bands = [(0.20, 10), (0.15, 8), (0.10, 5)]
        assert band_score(0.25, bands) == 10
        assert band_score(0.16, bands) == 8
        assert band_score(0.11, bands) == 5

    def test_no_match_scores_zero(self):
        assert band_score(0.01, [(0.20, 10), (0.10, 5)]) == 0.0

    def test_lower_is_better_direction(self):
        bands = [(1.0, 10), (2.0, 7), (3.0, 4)]
        assert band_score(0.5, bands, higher_is_better=False) == 10
        assert band_score(2.5, bands, higher_is_better=False) == 4

    def test_missing_value_is_neutral(self):
        assert band_score(None, [(0.2, 10)]) == NEUTRAL_SCORE


class TestLinearScore:
    def test_interpolates(self):
        assert linear_score(0.5, 0.0, 1.0) == pytest.approx(5.0)

    def test_reversed_anchors_for_lower_is_better(self):
        assert linear_score(1.0, 4.0, 0.0) == pytest.approx(7.5)

    def test_clamped_at_both_ends(self):
        assert linear_score(2.0, 0.0, 1.0) == 10.0
        assert linear_score(-1.0, 0.0, 1.0) == 0.0

    def test_missing_is_neutral(self):
        assert linear_score(None, 0.0, 1.0) == NEUTRAL_SCORE


class TestTrendScore:
    def test_steady_improvement_scores_top(self):
        score, slope = trend_score([0.10, 0.12, 0.14, 0.16])
        assert score == 10.0
        assert slope == pytest.approx(0.02)

    def test_steady_decline_scores_bottom(self):
        score, _ = trend_score([0.16, 0.14, 0.12, 0.10])
        assert score == 0.0

    def test_flat_series_is_neutral_not_a_decline(self):
        """A stable series must not be punished as deterioration."""
        score, slope = trend_score([0.1, 0.1, 0.1, 0.1])
        assert score == NEUTRAL_SCORE
        assert slope == pytest.approx(0.0)

    def test_volatile_path_scores_below_steady(self):
        steady, _ = trend_score([0.10, 0.13, 0.16, 0.19])
        volatile, _ = trend_score([0.10, 0.05, 0.20, 0.19])
        assert volatile < steady

    def test_direction_inverts_when_lower_is_better(self):
        rising, _ = trend_score([1.0, 2.0, 3.0], higher_is_better=False)
        assert rising < NEUTRAL_SCORE

    def test_single_point_is_neutral(self):
        assert trend_score([0.1]) == (NEUTRAL_SCORE, None)


class TestConfidence:
    def test_weighted_not_counted(self):
        """A heavily weighted gap must hurt more than a trivial one."""
        heavy = build_confidence([
            MetricScore("a", "A", 5, 0.9, DataOrigin.MISSING),
            MetricScore("b", "B", 5, 0.1, DataOrigin.VERIFIED),
        ])
        light = build_confidence([
            MetricScore("a", "A", 5, 0.1, DataOrigin.MISSING),
            MetricScore("b", "B", 5, 0.9, DataOrigin.VERIFIED),
        ])
        assert heavy.confidence < light.confidence

    def test_all_verified_is_full_confidence(self):
        c = build_confidence([MetricScore("a", "A", 5, 1.0, DataOrigin.VERIFIED)])
        assert c.confidence == pytest.approx(1.0)
        assert c.verified_pct == pytest.approx(1.0)

    def test_all_missing_is_zero_confidence(self):
        c = build_confidence([MetricScore("a", "A", 5, 1.0, DataOrigin.MISSING)])
        assert c.confidence == pytest.approx(0.0)
        assert c.missing_pct == pytest.approx(1.0)

    def test_shares_sum_to_one(self):
        c = build_confidence([
            MetricScore("a", "A", 5, 0.4, DataOrigin.VERIFIED),
            MetricScore("b", "B", 5, 0.3, DataOrigin.ESTIMATED),
            MetricScore("c", "C", 5, 0.2, DataOrigin.ANALYST),
            MetricScore("d", "D", 5, 0.1, DataOrigin.MISSING),
        ])
        total = c.verified_pct + c.estimated_pct + c.analyst_pct + c.missing_pct
        assert total == pytest.approx(1.0)

    def test_recomputed_by_hand(self):
        c = build_confidence([
            MetricScore("a", "A", 5, 0.4, DataOrigin.VERIFIED),
            MetricScore("b", "B", 5, 0.3, DataOrigin.ESTIMATED),
            MetricScore("c", "C", 5, 0.3, DataOrigin.MISSING),
        ])
        assert c.confidence == pytest.approx(0.4 * 1.0 + 0.3 * 0.65 + 0.3 * 0.0)

    def test_labels(self):
        assert build_confidence([MetricScore("a", "A", 5, 1, DataOrigin.VERIFIED)]).label == "High"
        assert build_confidence([MetricScore("a", "A", 5, 1, DataOrigin.MISSING)]).label == "Very low"


class TestAggregate:
    def test_weighted_mean(self):
        metrics = [
            MetricScore("a", "A", 8.0, 0.5, DataOrigin.VERIFIED),
            MetricScore("b", "B", 4.0, 0.5, DataOrigin.VERIFIED),
        ]
        assert aggregate(metrics) == pytest.approx(6.0)

    def test_unequal_weights(self):
        metrics = [
            MetricScore("a", "A", 10.0, 0.8, DataOrigin.VERIFIED),
            MetricScore("b", "B", 0.0, 0.2, DataOrigin.VERIFIED),
        ]
        assert aggregate(metrics) == pytest.approx(8.0)

    def test_empty_is_neutral(self):
        assert aggregate([]) == NEUTRAL_SCORE


class TestNarrate:
    def test_mentions_strongest_and_weakest(self):
        metrics = [
            MetricScore("a", "A", 9.0, 0.5, DataOrigin.VERIFIED, explanation="strong bit"),
            MetricScore("b", "B", 2.0, 0.5, DataOrigin.VERIFIED, explanation="weak bit"),
        ]
        text = narrate("Test", metrics, 5.5)
        assert "strong bit" in text and "weak bit" in text

    def test_lists_missing_inputs(self):
        metrics = [MetricScore("a", "A", 5, 1.0, DataOrigin.MISSING)]
        assert "could not be assessed" in narrate("Test", metrics, 5.0)


# ================================================================== weights
class TestWeightProfiles:
    @pytest.mark.parametrize("key", list(BUILTIN_PROFILES))
    def test_normalised_to_one(self, key):
        assert sum(BUILTIN_PROFILES[key].weights.values()) == pytest.approx(1.0)

    @pytest.mark.parametrize("key", list(BUILTIN_PROFILES))
    def test_covers_all_thirteen_categories(self, key):
        assert set(BUILTIN_PROFILES[key].weights) == {c.value for c in Category}

    def test_five_builtin_profiles(self):
        assert set(BUILTIN_PROFILES) == {
            "balanced", "conservative", "growth", "value", "quality"
        }

    def test_value_prioritises_valuation_over_growth(self):
        assert VALUE.weight_for(Category.VALUATION) > GROWTH.weight_for(Category.VALUATION)

    def test_growth_prioritises_growth_over_value(self):
        assert (GROWTH.weight_for(Category.GROWTH_QUALITY)
                > VALUE.weight_for(Category.GROWTH_QUALITY))

    def test_conservative_prioritises_risk_and_governance(self):
        assert (CONSERVATIVE.weight_for(Category.FINANCIAL_RISK)
                > BALANCED.weight_for(Category.FINANCIAL_RISK))
        assert (CONSERVATIVE.weight_for(Category.GOVERNANCE)
                > GROWTH.weight_for(Category.GOVERNANCE))

    def test_quality_prioritises_moat(self):
        assert QUALITY.weight_for(Category.COMPETITIVE_MOAT) >= 0.15

    def test_momentum_is_never_dominant(self):
        for profile in BUILTIN_PROFILES.values():
            assert profile.weight_for(Category.MOMENTUM) <= 0.10

    def test_relative_weights_are_scale_invariant(self):
        a = WeightProfile("a", "A", "", {"valuation": 3, "governance": 1})
        b = WeightProfile("b", "B", "", {"valuation": 30, "governance": 10})
        assert a.weights == pytest.approx(b.weights)

    def test_overrides_renormalise(self):
        custom = BALANCED.with_overrides({"valuation": 0.5})
        assert sum(custom.weights.values()) == pytest.approx(1.0)
        assert custom.is_builtin is False

    def test_unknown_category_rejected(self):
        with pytest.raises(ValueError):
            BALANCED.with_overrides({"not_a_category": 1.0})

    def test_zero_total_rejected(self):
        with pytest.raises(ValueError):
            WeightProfile("z", "Z", "", {"valuation": 0.0})

    def test_get_profile_defaults_and_raises(self):
        assert get_profile(None) is BALANCED
        with pytest.raises(KeyError):
            get_profile("nonsense")


# ================================================================ categories
class TestCategoryScorers:
    @pytest.fixture(scope="class")
    def inputs(self):
        return build_inputs()

    @pytest.mark.parametrize("module", [
        business_quality, financial_quality, management_quality,
        capital_allocation, competitive_advantage, governance, financial_risk,
        risk, valuation, growth_quality, cash_flow_quality, esg, momentum,
    ])
    def test_every_scorer_returns_a_valid_category(self, module, inputs):
        result = module.score(inputs, 0.1)
        assert 0.0 <= result.raw_score <= 10.0
        assert result.weighted_score == pytest.approx(result.raw_score * 0.1)
        assert result.metrics
        assert result.explanation

    @pytest.mark.parametrize("module", [
        business_quality, financial_quality, management_quality,
        capital_allocation, competitive_advantage, governance, financial_risk,
        risk, valuation, growth_quality, cash_flow_quality, esg, momentum,
    ])
    def test_every_metric_has_an_explanation_or_is_missing(self, module, inputs):
        for metric in module.score(inputs, 0.1).metrics:
            assert metric.explanation, f"{module.__name__}.{metric.key} has no explanation"

    @pytest.mark.parametrize("module", [
        business_quality, financial_quality, financial_risk, cash_flow_quality,
    ])
    def test_quantitative_categories_are_mostly_verified(self, module, inputs):
        """Categories fed by statements should not be guessing."""
        assert module.score(inputs, 0.1).confidence.verified_pct >= 0.5

    def test_esg_reports_missing_when_nothing_supplied(self, inputs):
        result = esg.score(inputs, 0.05)
        assert result.confidence.confidence == pytest.approx(0.0)
        assert result.confidence.missing_pct == pytest.approx(1.0)

    def test_momentum_reports_missing_without_price_data(self, inputs):
        assert momentum.score(inputs, 0.02).confidence.missing_pct == pytest.approx(1.0)

    def test_qualitative_inputs_lift_confidence(self, inputs):
        bare = governance.score(inputs, 0.1)
        informed = governance.score(
            build_inputs(qualitative=QualitativeInputs(
                board_independence=0.6, audit_qualifications=0, promoter_pledge=0.0,
                related_party_intensity=0.01, disclosure_quality=8.0,
                auditor_is_big_four=True,
            )), 0.1,
        )
        assert informed.confidence.confidence > bare.confidence.confidence
        assert informed.raw_score > bare.raw_score

    def test_financial_risk_rewards_low_leverage(self):
        """A net-cash company must score higher than a levered one."""
        low = financial_risk.score(build_inputs(), 0.1)
        assert low.raw_score >= 5.0

    def test_valuation_scores_upside(self):
        cheap = valuation.score(build_inputs(upside=0.45, pe_ratio=11.0), 0.1)
        rich = valuation.score(build_inputs(upside=-0.30, pe_ratio=48.0), 0.1)
        assert cheap.raw_score > rich.raw_score

    def test_valuation_missing_without_intrinsic_value(self, inputs):
        result = valuation.score(inputs, 0.1)
        upside = next(m for m in result.metrics if m.key == "upside")
        assert upside.origin is DataOrigin.MISSING

    def test_capital_allocation_judges_reinvestment_conditionally(self):
        """Identical reinvestment scores differently above and below WACC.

        The fixture's own ROIC is low, so the WACC is set either side of it to
        exercise both branches rather than assuming the fixture straddles them.
        """
        high_return = build_inputs(margin=0.45, wacc=0.02)   # ROIC comfortably above WACC
        low_return = build_inputs(margin=0.45, wacc=0.60)    # same business, WACC above ROIC

        above = next(m for m in capital_allocation.score(high_return, 0.1).metrics
                     if m.key == "reinvestment_rate")
        below = next(m for m in capital_allocation.score(low_return, 0.1).metrics
                     if m.key == "reinvestment_rate")

        assert above.score != below.score
        assert "accretive" in above.explanation
        assert "dilutive" in below.explanation

    def test_moat_requires_financial_corroboration(self, inputs):
        """A moat claim with no excess returns must not score top marks."""
        story_only = competitive_advantage.score(
            build_inputs(wacc=0.40, qualitative=QualitativeInputs(
                brand_strength=10, switching_costs=10, network_effects=10,
                cost_advantage=10, intangible_assets=10, efficient_scale=10,
            )), 0.1,
        )
        assert story_only.raw_score < 9.0


# =================================================================== overall
class TestOverallScore:
    @pytest.fixture(scope="class")
    def result(self):
        return compute_score(build_inputs(), BALANCED)

    def test_thirteen_scorers_registered(self):
        assert len(SCORERS) == 13
        assert len({c for c, _ in SCORERS}) == 13

    def test_all_categories_present(self, result):
        assert len(result.categories) == 13
        assert {c.key for c in result.categories} == {c.value for c in Category}

    def test_composite_in_range(self, result):
        assert 0.0 <= result.overall_score <= 100.0

    def test_composite_is_the_weighted_mean(self, result):
        total_weight = sum(c.weight for c in result.categories)
        expected = sum(c.weighted_score for c in result.categories) / total_weight * 10
        assert result.overall_score == pytest.approx(expected)

    def test_grade_matches_score(self, result):
        letter, _ = grade_for(result.overall_score)
        assert result.grade == letter

    def test_grade_bands_ordered_and_complete(self):
        thresholds = [t for t, _, _ in GRADE_BANDS]
        assert thresholds == sorted(thresholds, reverse=True)
        assert thresholds[-1] == 0.0

    @pytest.mark.parametrize("score,expected", [
        (92, "AAA"), (80, "AA"), (70, "A"), (60, "BBB"), (50, "BB"), (40, "B"), (20, "C"),
    ])
    def test_grade_boundaries(self, score, expected):
        assert grade_for(score)[0] == expected

    def test_stars_track_score(self):
        assert stars_for(100) == 5.0
        assert stars_for(50) == 2.5
        assert stars_for(0) == 0.5  # floor

    def test_stars_are_half_steps(self):
        for score in range(0, 101, 7):
            assert (stars_for(score) * 2) % 1 == 0

    def test_recommendation_is_valid(self, result):
        assert result.recommendation in {
            "BUY", "ACCUMULATE", "HOLD", "REDUCE", "SELL"
        }

    def test_rationale_explains_the_recommendation(self, result):
        assert "Composite score" in result.recommendation_rationale

    def test_summary_is_ai_consumable(self, result):
        assert result.name in result.summary
        assert f"{result.overall_score:.1f}" in result.summary

    def test_strongest_and_weakest_reported(self, result):
        assert len(result.strongest) == 3
        assert len(result.weakest) == 3

    def test_profiles_produce_different_scores(self):
        scores = {
            key: compute_score(build_inputs(upside=-0.4, pe_ratio=45.0), profile).overall_score
            for key, profile in BUILTIN_PROFILES.items()
        }
        assert len(set(round(s, 3) for s in scores.values())) > 1

    def test_value_profile_punishes_expensive_stock_hardest(self):
        expensive = build_inputs(upside=-0.45, pe_ratio=50.0, ev_ebitda=32.0,
                                 pb_ratio=9.0, justified_premium=1.2)
        value = compute_score(expensive, VALUE).overall_score
        growth = compute_score(expensive, GROWTH).overall_score
        assert value < growth


class TestRecommendationOverrides:
    def test_expensive_valuation_caps_at_hold(self):
        result = compute_score(
            build_inputs(upside=-0.60, pe_ratio=80.0, ev_ebitda=45.0,
                         pb_ratio=15.0, justified_premium=2.0),
            BALANCED,
        )
        assert result.recommendation in {"HOLD", "REDUCE", "SELL"}
        if result.overall_score >= 62:
            assert "Capped at HOLD" in result.recommendation_rationale

    def test_low_confidence_caps_the_call(self):
        """With almost no data the engine must not make a directional call."""
        bare = ScoringInputs(
            company_id="c", ticker="X", name="Sparse Ltd",
            incomes=[], balances=[], cash_flows=[],
        )
        result = compute_score(bare, BALANCED)
        assert result.recommendation in {"HOLD", "REDUCE", "SELL"}
        assert result.confidence.confidence < 0.55

    def test_conviction_reflects_confidence(self):
        rich = compute_score(build_inputs(upside=0.3), BALANCED)
        bare = compute_score(
            ScoringInputs(company_id="c", ticker="X", name="X",
                          incomes=[], balances=[], cash_flows=[]),
            BALANCED,
        )
        assert bare.conviction == "Low"
        assert rich.conviction in {"Medium", "High"}

    def test_illustrative_data_surfaces_a_warning(self):
        from app.domain.valuation.data_quality import assess_data_quality
        result = compute_score(
            build_inputs(quality_report=assess_data_quality(fact_sources={"seed"})),
            BALANCED,
        )
        assert any("Illustrative" in w for w in result.warnings)


class TestDegenerateInputs:
    def test_empty_company_does_not_crash(self):
        result = compute_score(
            ScoringInputs(company_id="c", ticker="X", name="Empty Ltd",
                          incomes=[], balances=[], cash_flows=[]),
            BALANCED,
        )
        assert len(result.categories) == 13
        assert result.confidence.confidence < 0.3

    def test_single_year_of_history(self):
        result = compute_score(build_inputs(years=(2025,)), BALANCED)
        assert 0 <= result.overall_score <= 100

    def test_zero_weight_category_contributes_nothing(self):
        profile = WeightProfile("t", "T", "", {"valuation": 1.0})
        result = compute_score(build_inputs(), profile)
        momentum_category = result.category(Category.MOMENTUM)
        assert momentum_category.weight == 0.0
        assert momentum_category.weighted_score == 0.0
