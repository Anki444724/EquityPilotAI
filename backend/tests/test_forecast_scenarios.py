"""Unit tests for scenario derivation and probability weighting."""
from __future__ import annotations

import pytest

from app.domain.forecast.assumptions import (
    ForecastAssumptions, Provenance, Scenario, driver,
)
from app.domain.forecast.engine import ForecastBase
from app.domain.forecast.scenarios import (
    BOUNDS, DEFAULT_PROBABILITIES, DEFAULT_SHIFTS, DriverShift,
    derive_scenario, run_scenarios,
)


def base_position() -> ForecastBase:
    return ForecastBase(
        fiscal_year=2025, revenue=10_000.0, ebitda=1_800.0, net_block=3_100.0,
        net_working_capital=1_500.0, gross_debt=1_600.0, cash=900.0,
        shares_outstanding=120.0, equity=5_000.0, invested_capital=5_700.0,
    )


class TestDerivation:
    def test_base_case_is_unshifted(self):
        a = ForecastAssumptions(ebitda_margin=driver(0.18))
        assert derive_scenario(a, Scenario.BASE).ebitda_margin.value == pytest.approx(0.18)

    def test_bear_lowers_margin_bull_raises_it(self):
        a = ForecastAssumptions(ebitda_margin=driver(0.18))
        assert derive_scenario(a, Scenario.BEAR).ebitda_margin.value == pytest.approx(0.16)
        assert derive_scenario(a, Scenario.BULL).ebitda_margin.value == pytest.approx(0.20)

    def test_capex_moves_the_unfavourable_way_in_the_bear_case(self):
        """Bear means spending MORE capital for the same revenue."""
        a = ForecastAssumptions(capex_pct_revenue=driver(0.05))
        assert derive_scenario(a, Scenario.BEAR).capex_pct_revenue.value > 0.05
        assert derive_scenario(a, Scenario.BULL).capex_pct_revenue.value < 0.05

    def test_receivable_days_worsen_in_the_bear_case(self):
        a = ForecastAssumptions(receivable_days=driver(45.0))
        assert derive_scenario(a, Scenario.BEAR).receivable_days.value > 45.0
        assert derive_scenario(a, Scenario.BULL).receivable_days.value < 45.0

    def test_wacc_rises_in_the_bear_case(self):
        a = ForecastAssumptions(wacc=driver(0.115))
        assert derive_scenario(a, Scenario.BEAR).wacc.value > 0.115
        assert derive_scenario(a, Scenario.BULL).wacc.value < 0.115

    def test_multiples_shift_proportionally(self):
        a = ForecastAssumptions(exit_ev_ebitda=driver(12.0))
        assert derive_scenario(a, Scenario.BULL).exit_ev_ebitda.value == pytest.approx(15.0)
        assert derive_scenario(a, Scenario.BEAR).exit_ev_ebitda.value == pytest.approx(9.0)

    def test_bounds_prevent_nonsensical_values(self):
        """A 3% base margin must not shift to a negative margin."""
        a = ForecastAssumptions(ebitda_margin=driver(0.03))
        shifted = derive_scenario(a, Scenario.BEAR).ebitda_margin.value
        assert shifted >= BOUNDS["ebitda_margin"][0]
        assert shifted > 0

    def test_tax_rate_cannot_exceed_its_cap(self):
        a = ForecastAssumptions(effective_tax_rate=driver(0.59))
        assert derive_scenario(a, Scenario.BEAR).effective_tax_rate.value <= 0.60

    def test_scenarios_derive_from_edited_base(self):
        """An analyst edit must propagate into bull and bear."""
        edited = ForecastAssumptions().with_drivers({"ebitda_margin": 0.30})
        assert derive_scenario(edited, Scenario.BULL).ebitda_margin.value == pytest.approx(0.32)
        assert derive_scenario(edited, Scenario.BEAR).ebitda_margin.value == pytest.approx(0.28)

    def test_provenance_is_preserved_through_derivation(self):
        a = ForecastAssumptions().with_drivers(
            {"ebitda_margin": 0.22}, source=Provenance.AI_EXTRACTED
        )
        assert derive_scenario(a, Scenario.BULL).ebitda_margin.source is Provenance.AI_EXTRACTED

    def test_custom_shifts_are_honoured(self):
        a = ForecastAssumptions(ebitda_margin=driver(0.18))
        custom = (DriverShift("ebitda_margin", bear=-0.10, bull=0.10),)
        assert derive_scenario(a, Scenario.BULL, custom).ebitda_margin.value == pytest.approx(0.28)

    def test_default_probabilities_sum_to_one(self):
        assert sum(DEFAULT_PROBABILITIES.values()) == pytest.approx(1.0)


class TestScenarioAnalysis:
    @pytest.fixture(scope="class")
    def analysis(self):
        return run_scenarios(base_position(), ForecastAssumptions(years=5), cmp_price=250.0)

    def test_three_scenarios_produced(self, analysis):
        assert set(analysis.results) == {"bear", "base", "bull"}
        assert len(analysis.outcomes) == 3

    def test_every_scenario_converges_and_reconciles(self, analysis):
        for result in analysis.results.values():
            assert result.debt_converged
            assert result.all_reconciled

    def test_outcomes_are_ordered_bear_base_bull(self, analysis):
        values = [o.value_per_share for o in analysis.outcomes]
        assert values == sorted(values)

    def test_bull_grows_faster_than_bear(self, analysis):
        by = {o.scenario: o for o in analysis.outcomes}
        assert by["bull"].revenue_cagr > by["base"].revenue_cagr > by["bear"].revenue_cagr

    def test_expected_value_is_probability_weighted(self, analysis):
        expected = sum(
            o.value_per_share * o.probability for o in analysis.outcomes
        ) / sum(o.probability for o in analysis.outcomes)
        assert analysis.expected_value == pytest.approx(expected)

    def test_expected_value_lies_between_bear_and_bull(self, analysis):
        by = {o.scenario: o.value_per_share for o in analysis.outcomes}
        assert by["bear"] < analysis.expected_value < by["bull"]

    def test_risk_reward_is_upside_over_downside(self, analysis):
        assert analysis.risk_reward == pytest.approx(
            abs(analysis.bull_upside) / abs(analysis.bear_downside)
        )

    def test_dispersion_is_positive(self, analysis):
        assert analysis.standard_deviation > 0
        assert analysis.coefficient_of_variation > 0

    def test_verdict_is_populated(self, analysis):
        assert analysis.verdict and "risk" in analysis.verdict.lower()

    def test_no_price_means_no_upside_claim(self):
        """Without a market price the engine must not invent an upside."""
        a = run_scenarios(base_position(), ForecastAssumptions(years=3), cmp_price=None)
        assert a.expected_upside is None
        assert all(o.upside is None for o in a.outcomes)
        assert "market price" in a.verdict.lower()

    def test_custom_probabilities_shift_the_expected_value(self):
        bullish = run_scenarios(
            base_position(), ForecastAssumptions(years=5), cmp_price=250.0,
            probabilities={Scenario.BEAR: 0.1, Scenario.BASE: 0.4, Scenario.BULL: 0.5},
        )
        neutral = run_scenarios(
            base_position(), ForecastAssumptions(years=5), cmp_price=250.0
        )
        assert bullish.expected_value > neutral.expected_value

    def test_value_per_share_uses_the_exit_multiple(self, analysis):
        result = analysis.results["base"]
        terminal = result.terminal_year
        multiple = result.assumptions.exit_ev_ebitda.at(terminal.period)
        expected = (terminal.ebitda * multiple - terminal.net_debt) / 120.0
        by = {o.scenario: o.value_per_share for o in analysis.outcomes}
        assert by["base"] == pytest.approx(expected)
