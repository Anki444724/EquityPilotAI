"""Unit tests for the valuation engines.

Every figure is verified by independent recomputation from first principles.
"""
from __future__ import annotations

import pytest

from app.domain.valuation.data_quality import (
    DataGrade, ILLUSTRATIVE_NOTICE, Severity, assess_data_quality,
)
from app.domain.valuation.dcf import (
    DCFInputs, DiscountConvention, TerminalMethod, discount_periods, run_dcf,
)
from app.domain.valuation.ddm import DDMInputs, DDMVariant, run_ddm
from app.domain.valuation.relative import (
    RelativeInputs, run_relative_valuation, sustainable_payout,
)
from app.domain.valuation.sensitivity import (
    Distribution, StochasticVariable, build_axis, build_grid, run_simulation,
)
from app.domain.valuation.sotp import (
    SOTPSegment, SegmentBasis, run_replacement_value, run_sotp,
)
from app.domain.valuation.wacc import (
    BetaSource, WACC_CEILING, WACC_FLOOR, WACCInputs, compute_wacc,
    dynamic_wacc_schedule, relever_beta, unlever_beta,
)

FLOWS = (100.0, 110.0, 121.0, 133.1, 146.41)


# ======================================================================= WACC
class TestWACC:
    def test_capm_build_recomputed(self):
        w = compute_wacc(WACCInputs(
            market_value_equity=8000, market_value_debt=2000, cost_of_debt=0.085
        ))
        expected_ke = 0.0695 + w.beta_used * (0.055 + 0.0243) + 0.01 + 0.005
        assert w.cost_of_equity == pytest.approx(expected_ke)

    def test_weighted_average_recomputed(self):
        w = compute_wacc(WACCInputs(
            market_value_equity=8000, market_value_debt=2000, cost_of_debt=0.085
        ))
        assert w.wacc == pytest.approx(
            0.8 * w.cost_of_equity + 0.2 * w.after_tax_cost_of_debt
        )

    def test_hamada_relevering(self):
        assert relever_beta(0.85, 0.25, 0.25) == pytest.approx(0.85 * (1 + 0.75 * 0.25))

    def test_unlever_is_the_inverse(self):
        levered = relever_beta(0.9, 0.4, 0.3)
        assert unlever_beta(levered, 0.4, 0.3) == pytest.approx(0.9)

    def test_leverage_raises_beta_and_cost_of_equity(self):
        low = compute_wacc(WACCInputs(market_value_equity=9000, market_value_debt=1000))
        high = compute_wacc(WACCInputs(market_value_equity=5000, market_value_debt=5000))
        assert high.levered_beta > low.levered_beta
        assert high.cost_of_equity > low.cost_of_equity

    def test_tax_shield_lowers_cost_of_debt(self):
        w = compute_wacc(WACCInputs(cost_of_debt=0.10, marginal_tax_rate=0.30))
        assert w.after_tax_cost_of_debt == pytest.approx(0.07)

    def test_country_premium_enters_the_erp(self):
        w = compute_wacc(WACCInputs(mature_erp=0.05, country_risk_premium=0.02))
        assert w.total_erp == pytest.approx(0.07)

    def test_regression_beta_source(self):
        w = compute_wacc(WACCInputs(
            regression_beta=1.4, beta_source=BetaSource.REGRESSION,
            market_value_equity=8000, market_value_debt=2000,
        ))
        assert w.beta_used == 1.4

    def test_average_beta_source(self):
        w = compute_wacc(WACCInputs(
            regression_beta=1.4, beta_source=BetaSource.AVERAGE,
            market_value_equity=8000, market_value_debt=2000,
        ))
        assert w.beta_used == pytest.approx((w.levered_beta + 1.4) / 2)

    def test_synthetic_cost_of_debt_when_unobserved(self):
        w = compute_wacc(WACCInputs(cost_of_debt=None, credit_spread=0.03))
        assert w.pre_tax_cost_of_debt == pytest.approx(0.0695 + 0.03)

    def test_cost_of_debt_floor(self):
        w = compute_wacc(WACCInputs(cost_of_debt=0.001))
        assert w.pre_tax_cost_of_debt >= 0.03

    def test_wacc_is_bounded(self):
        w = compute_wacc(WACCInputs(
            unlevered_beta=8.0, market_value_equity=1000, market_value_debt=0
        ))
        assert w.wacc <= WACC_CEILING
        assert w.bounded is True

    def test_all_equity_when_no_capital(self):
        w = compute_wacc(WACCInputs(market_value_equity=0, market_value_debt=0))
        assert w.weight_equity == 1.0
        assert w.wacc == pytest.approx(w.cost_of_equity)

    def test_dynamic_schedule_tracks_delevering(self):
        rows = dynamic_wacc_schedule(
            WACCInputs(cost_of_debt=0.085), [8000, 9000, 10000], [2000, 1000, 0]
        )
        assert rows[0].debt_to_equity > rows[1].debt_to_equity > rows[2].debt_to_equity
        assert rows[0].levered_beta > rows[2].levered_beta


# ======================================================================== DCF
class TestDCF:
    def _base(self, **kw):
        defaults = dict(
            cash_flows=FLOWS, discount_rate=0.12, terminal_growth=0.05,
            shares_outstanding=100.0, gross_debt=500.0, cash_and_equivalents=200.0,
            current_price=15.0, terminal_ebitda=300.0,
        )
        return DCFInputs(**{**defaults, **kw})

    def test_year_end_discount_periods(self):
        assert discount_periods(3, DiscountConvention.YEAR_END) == [1.0, 2.0, 3.0]

    def test_mid_year_discount_periods(self):
        assert discount_periods(3, DiscountConvention.MID_YEAR) == [0.5, 1.5, 2.5]

    def test_year_end_present_value_recomputed(self):
        r = run_dcf(self._base(convention=DiscountConvention.YEAR_END))
        assert r.years[0].present_value == pytest.approx(100 / 1.12)
        assert r.years[2].present_value == pytest.approx(121 / 1.12 ** 3)

    def test_mid_year_present_value_recomputed(self):
        r = run_dcf(self._base(convention=DiscountConvention.MID_YEAR))
        assert r.years[0].present_value == pytest.approx(100 / 1.12 ** 0.5)

    def test_mid_year_uplift_is_sqrt_of_one_plus_rate(self):
        ye = run_dcf(self._base(convention=DiscountConvention.YEAR_END))
        my = run_dcf(self._base(convention=DiscountConvention.MID_YEAR))
        assert my.sum_pv_explicit / ye.sum_pv_explicit == pytest.approx(1.12 ** 0.5)

    def test_gordon_terminal_value(self):
        r = run_dcf(self._base())
        assert r.terminal_value == pytest.approx(146.41 * 1.05 / (0.12 - 0.05))

    def test_exit_multiple_terminal_value(self):
        r = run_dcf(self._base(terminal_method=TerminalMethod.EXIT_MULTIPLE,
                               exit_multiple=12.0))
        assert r.terminal_value == pytest.approx(300.0 * 12.0)

    def test_exit_multiple_falls_back_without_ebitda(self):
        r = run_dcf(self._base(terminal_method=TerminalMethod.EXIT_MULTIPLE,
                               terminal_ebitda=None))
        assert any("fell back" in w for w in r.warnings)

    def test_growth_near_discount_rate_is_bounded_and_warned(self):
        r = run_dcf(self._base(discount_rate=0.10, terminal_growth=0.099))
        assert r.terminal_value < float("inf")
        assert any("too close" in w for w in r.warnings)

    def test_enterprise_to_equity_bridge(self):
        r = run_dcf(self._base(minority_interest=50.0, associate_investments=80.0))
        assert r.equity_value == pytest.approx(
            r.enterprise_value - 500 + 200 - 50 + 80
        )

    def test_intrinsic_value_per_share(self):
        r = run_dcf(self._base())
        assert r.intrinsic_value_per_share == pytest.approx(r.equity_value / 100)

    def test_upside_and_buy_zone(self):
        r = run_dcf(self._base(current_price=10.0, margin_of_safety=0.25))
        assert r.upside == pytest.approx(r.intrinsic_value_per_share / 10.0 - 1)
        assert r.maximum_buy_price == pytest.approx(r.intrinsic_value_per_share * 0.75)
        assert r.in_buy_zone == (10.0 <= r.maximum_buy_price)

    def test_equity_model_skips_the_bridge(self):
        r = run_dcf(self._base(), equity_model=True)
        assert r.equity_value == pytest.approx(r.enterprise_value)
        assert r.net_debt is None

    def test_dynamic_rate_schedule_applied(self):
        flat = run_dcf(self._base(discount_rate=0.12,
                                  convention=DiscountConvention.YEAR_END))
        rising = run_dcf(self._base(
            discount_rate=0.12, discount_rate_schedule=(0.10, 0.11, 0.12, 0.13, 0.14),
            convention=DiscountConvention.YEAR_END,
        ))
        assert rising.years[0].present_value > flat.years[0].present_value

    def test_terminal_dominance_warning(self):
        r = run_dcf(self._base(discount_rate=0.09, terminal_growth=0.08))
        assert r.terminal_value_pct > 0.85
        assert any("Terminal value is" in w for w in r.warnings)

    def test_higher_discount_rate_lowers_value(self):
        low = run_dcf(self._base(discount_rate=0.10))
        high = run_dcf(self._base(discount_rate=0.15))
        assert high.intrinsic_value_per_share < low.intrinsic_value_per_share

    def test_higher_growth_raises_value(self):
        low = run_dcf(self._base(terminal_growth=0.03))
        high = run_dcf(self._base(terminal_growth=0.06))
        assert high.intrinsic_value_per_share > low.intrinsic_value_per_share

    def test_purity_inputs_unchanged(self):
        """Monte Carlo depends on run_dcf not mutating its arguments."""
        inputs = self._base()
        before = inputs.cash_flows
        run_dcf(inputs)
        assert inputs.cash_flows is before


# ======================================================== relative valuation
class TestRelative:
    def _inputs(self, **kw):
        defaults = dict(
            current_price=250.0, shares_outstanding=100.0, market_cap=25000.0,
            gross_debt=5000.0, cash_and_equivalents=2000.0,
            trailing_eps=12.5, trailing_bvps=80.0, trailing_ebitda=3000.0,
            trailing_revenue=15000.0, trailing_ebit=2400.0, trailing_fcfe=1100.0,
            trailing_dividend_per_share=3.0,
            forward_eps=(14.0, 16.0, 18.0), forward_bvps=(90.0, 102.0, 116.0),
            forward_ebitda=(3400.0, 3800.0, 4300.0),
            forward_revenue=(16500.0, 18200.0, 20000.0),
            forward_ebit=(2750.0, 3100.0, 3500.0),
            forward_fcfe=(1250.0, 1400.0, 1600.0),
            cost_of_equity=0.145, wacc=0.115, terminal_growth=0.05,
            payout_ratio=0.72, roe=0.18, reinvestment_rate=0.40, tax_rate=0.25,
            eps_cagr=0.13, dcf_value_per_share=290.0,
        )
        return RelativeInputs(**{**defaults, **kw})

    def test_trailing_pe_recomputed(self):
        r = run_relative_valuation(self._inputs())
        assert r.current.pe == pytest.approx(250 / 12.5)

    def test_ev_multiples_use_enterprise_value(self):
        r = run_relative_valuation(self._inputs())
        ev = 25000 + 5000 - 2000
        assert r.current.ev_ebitda == pytest.approx(ev / 3000)
        assert r.current.ev_sales == pytest.approx(ev / 15000)

    def test_ev_based_target_bridges_to_equity(self):
        r = run_relative_valuation(self._inputs(target_ev_ebitda=12.0))
        method = next(m for m in r.methods if m.key == "ev_ebitda")
        assert method.target_price == pytest.approx((12.0 * 3400 - 3000) / 100)

    def test_equity_based_target_is_direct(self):
        r = run_relative_valuation(self._inputs(target_pe=20.0))
        method = next(m for m in r.methods if m.key == "pe")
        assert method.target_price == pytest.approx(20.0 * 14.0)

    def test_blended_target_is_weighted(self):
        r = run_relative_valuation(self._inputs())
        priced = [m for m in r.methods if m.target_price and m.target_price > 0]
        expected = sum(m.target_price * m.weight for m in priced) / sum(m.weight for m in priced)
        assert r.blended_target_price == pytest.approx(expected)

    def test_dcf_participates_in_the_blend(self):
        r = run_relative_valuation(self._inputs())
        assert any(m.key == "dcf" and m.target_price == 290.0 for m in r.methods)

    def test_sustainable_payout_identity(self):
        """g = ROE x retention, so payout = 1 - g/ROE."""
        assert sustainable_payout(0.05, 0.18) == pytest.approx(1 - 0.05 / 0.18)

    def test_sustainable_payout_undefined_when_growth_exceeds_roe(self):
        assert sustainable_payout(0.20, 0.15) is None

    def test_justified_forward_pe_uses_coherent_payout(self):
        r = run_relative_valuation(self._inputs())
        j = next(x for x in r.justified if x.key == "forward_pe")
        implied_payout = 1 - 0.05 / 0.18
        assert j.justified == pytest.approx(implied_payout / (0.145 - 0.05))

    def test_justified_pb_formula(self):
        r = run_relative_valuation(self._inputs())
        j = next(x for x in r.justified if x.key == "pb")
        assert j.justified == pytest.approx((0.18 - 0.05) / (0.145 - 0.05))

    def test_justified_ev_ebitda_formula(self):
        r = run_relative_valuation(self._inputs())
        j = next(x for x in r.justified if x.key == "ev_ebitda")
        assert j.justified == pytest.approx((1 - 0.25) * (1 - 0.40) / (0.115 - 0.05))

    def test_incoherent_payout_is_flagged(self):
        """Payout of 25% with 5% growth at 18% ROE cannot both be true."""
        r = run_relative_valuation(self._inputs(payout_ratio=0.25))
        assert any("inconsistent" in w for w in r.warnings)

    def test_growth_above_roe_is_flagged(self):
        r = run_relative_valuation(self._inputs(terminal_growth=0.20, roe=0.15))
        assert any("cannot be self-funded" in w for w in r.warnings)

    def test_wide_target_dispersion_is_flagged(self):
        r = run_relative_valuation(self._inputs(target_pe=2.0, target_ev_ebitda=60.0))
        assert any("5x range" in w for w in r.warnings)

    def test_verdict_reflects_premium(self):
        r = run_relative_valuation(self._inputs())
        j = next(x for x in r.justified if x.key == "pb")
        assert j.premium_discount > 0
        assert "Overvalued" in j.verdict


# ======================================================================== DDM
class TestDDM:
    def test_gordon_recomputed(self):
        r = run_ddm(DDMInputs(current_dividend_per_share=5.0, cost_of_equity=0.13,
                              stable_growth=0.06))
        assert r.value_per_share == pytest.approx(5 * 1.06 / 0.07)

    def test_h_model_recomputed(self):
        r = run_ddm(DDMInputs(
            current_dividend_per_share=5.0, cost_of_equity=0.13, stable_growth=0.06,
            variant=DDMVariant.H_MODEL, high_growth=0.15, half_life_years=5,
        ))
        assert r.value_per_share == pytest.approx(5 * (1.06 + 5 * 0.09) / 0.07)

    def test_two_stage_exceeds_gordon_when_growth_is_higher(self):
        gordon = run_ddm(DDMInputs(current_dividend_per_share=5.0, cost_of_equity=0.13,
                                   stable_growth=0.06))
        two = run_ddm(DDMInputs(
            current_dividend_per_share=5.0, cost_of_equity=0.13, stable_growth=0.06,
            variant=DDMVariant.TWO_STAGE, high_growth=0.15, high_growth_years=5,
        ))
        assert two.value_per_share > gordon.value_per_share

    def test_h_model_sits_between_the_two(self):
        args = dict(current_dividend_per_share=5.0, cost_of_equity=0.13, stable_growth=0.06)
        gordon = run_ddm(DDMInputs(**args))
        h = run_ddm(DDMInputs(**args, variant=DDMVariant.H_MODEL,
                              high_growth=0.15, half_life_years=5))
        two = run_ddm(DDMInputs(**args, variant=DDMVariant.TWO_STAGE,
                                high_growth=0.15, high_growth_years=5))
        assert gordon.value_per_share < h.value_per_share < two.value_per_share

    def test_no_dividend_declines_to_value(self):
        r = run_ddm(DDMInputs(current_dividend_per_share=0.0, cost_of_equity=0.13))
        assert r.value_per_share is None
        assert "no dividend" in r.warnings[0]

    def test_growth_near_ke_is_bounded(self):
        r = run_ddm(DDMInputs(current_dividend_per_share=5.0, cost_of_equity=0.10,
                              stable_growth=0.099))
        assert r.value_per_share is not None
        assert any("too close" in w for w in r.warnings)


# ======================================================== SOTP & replacement
class TestSOTP:
    def _segments(self):
        return [
            SOTPSegment("Core", SegmentBasis.EV_EBITDA, multiple=11.0, metric=1800.0,
                        attributed_debt=900.0),
            SOTPSegment("Brands", SegmentBasis.EV_EBITDA, multiple=22.0, metric=600.0),
            SOTPSegment("Digital", SegmentBasis.EV_SALES, multiple=4.5, metric=400.0),
            SOTPSegment("Stake", SegmentBasis.DCF, direct_value=5200.0, stake=0.62),
        ]

    def test_segment_values_recomputed(self):
        r = run_sotp(self._segments(), shares_outstanding=100.0)
        by = {s.name: s.attributable_value for s in r.segments}
        assert by["Core"] == pytest.approx(11 * 1800 - 900)
        assert by["Brands"] == pytest.approx(22 * 600)
        assert by["Digital"] == pytest.approx(4.5 * 400)

    def test_minority_stake_is_pro_rated(self):
        r = run_sotp(self._segments(), shares_outstanding=100.0)
        by = {s.name: s.attributable_value for s in r.segments}
        assert by["Stake"] == pytest.approx(5200 * 0.62)

    def test_shares_of_total_sum_to_one(self):
        r = run_sotp(self._segments(), shares_outstanding=100.0)
        assert sum(s.share_of_total for s in r.segments) == pytest.approx(1.0)

    def test_holding_discount_and_net_debt_applied(self):
        r = run_sotp(self._segments(), net_debt=1500.0, holding_discount=0.15,
                     shares_outstanding=100.0)
        assert r.equity_value == pytest.approx(
            r.gross_asset_value * 0.85 - 1500.0
        )

    def test_segment_missing_inputs_is_warned(self):
        r = run_sotp([SOTPSegment("Broken", SegmentBasis.EV_EBITDA)],
                     shares_outstanding=100.0)
        assert any("lacks the inputs" in w for w in r.warnings)

    def test_replacement_inflation_uplift(self):
        r = run_replacement_value(
            net_block=4500.0, net_working_capital=1200.0, net_debt=1500.0,
            shares_outstanding=100.0, asset_age_years=7, inflation_rate=0.05,
        )
        assert r.adjusted_fixed_assets == pytest.approx(4500 * 1.05 ** 7)

    def test_tobins_q(self):
        r = run_replacement_value(
            net_block=4500.0, net_working_capital=1200.0, net_debt=1500.0,
            shares_outstanding=100.0, market_cap=18000.0,
        )
        assert r.tobins_q == pytest.approx(18000 / r.equity_replacement_value)


# =========================================== sensitivity and Monte Carlo
class TestSensitivity:
    def _revalue(self, wacc, growth):
        return run_dcf(DCFInputs(
            cash_flows=FLOWS, discount_rate=wacc, terminal_growth=growth,
            shares_outstanding=100.0, gross_debt=500.0, cash_and_equivalents=200.0,
        )).intrinsic_value_per_share

    def test_axis_is_symmetric_around_base(self):
        axis = build_axis(0.12, steps=2, step_size=0.005)
        assert axis == pytest.approx([0.11, 0.115, 0.12, 0.125, 0.13])

    def test_grid_dimensions(self):
        grid = build_grid(row_key="wacc", col_key="terminal_growth",
                          row_base=0.12, col_base=0.05, revalue=self._revalue, steps=2)
        assert len(grid.cells) == 5
        assert all(len(row) == 5 for row in grid.cells)

    def test_value_falls_as_wacc_rises(self):
        grid = build_grid(row_key="wacc", col_key="terminal_growth",
                          row_base=0.12, col_base=0.05, revalue=self._revalue, steps=2)
        column = [row[2] for row in grid.cells]
        assert column == sorted(column, reverse=True)

    def test_value_rises_with_terminal_growth(self):
        grid = build_grid(row_key="wacc", col_key="terminal_growth",
                          row_base=0.12, col_base=0.05, revalue=self._revalue, steps=2)
        assert grid.cells[2] == sorted(grid.cells[2])

    def test_centre_cell_equals_base_value(self):
        grid = build_grid(row_key="wacc", col_key="terminal_growth",
                          row_base=0.12, col_base=0.05, revalue=self._revalue, steps=2)
        assert grid.cells[2][2] == pytest.approx(grid.base_value)

    def test_upside_view_uses_the_market_price(self):
        grid = build_grid(row_key="wacc", col_key="terminal_growth",
                          row_base=0.12, col_base=0.05, revalue=self._revalue,
                          steps=1, current_price=15.0)
        assert grid.upside_cells()[1][1] == pytest.approx(grid.cells[1][1] / 15.0 - 1)


class TestMonteCarlo:
    def _revalue(self, draw):
        return run_dcf(DCFInputs(
            cash_flows=FLOWS, discount_rate=draw["wacc"],
            terminal_growth=draw["terminal_growth"], shares_outstanding=100.0,
        )).intrinsic_value_per_share

    def _variables(self):
        return [
            StochasticVariable("wacc", 0.12, spread=0.02, minimum=0.05),
            StochasticVariable("terminal_growth", 0.05, spread=0.015,
                               minimum=0.0, maximum=0.08),
        ]

    def test_runs_all_trials(self):
        sim = run_simulation(self._variables(), self._revalue, trials=500)
        assert len(sim.values) + sim.failed_trials == 500

    def test_reproducible_with_a_seed(self):
        a = run_simulation(self._variables(), self._revalue, trials=200, seed=7)
        b = run_simulation(self._variables(), self._revalue, trials=200, seed=7)
        assert a.mean_value == pytest.approx(b.mean_value)

    def test_different_seeds_differ(self):
        a = run_simulation(self._variables(), self._revalue, trials=200, seed=1)
        b = run_simulation(self._variables(), self._revalue, trials=200, seed=2)
        assert a.mean_value != pytest.approx(b.mean_value)

    def test_percentiles_are_ordered(self):
        sim = run_simulation(self._variables(), self._revalue, trials=500)
        values = [sim.percentiles[p] for p in (5, 10, 25, 50, 75, 90, 95)]
        assert values == sorted(values)

    def test_probability_above_price(self):
        sim = run_simulation(self._variables(), self._revalue, trials=500,
                             current_price=14.0)
        assert 0.0 <= sim.probability_above_price <= 1.0

    def test_histogram_counts_every_value(self):
        sim = run_simulation(self._variables(), self._revalue, trials=400)
        assert sum(b[2] for b in sim.histogram) == len(sim.values)

    def test_bounds_are_respected(self):
        variable = StochasticVariable("x", 0.10, Distribution.NORMAL, spread=0.5,
                                      minimum=0.05, maximum=0.15)
        import random
        rng = random.Random(1)
        assert all(0.05 <= variable.draw(rng) <= 0.15 for _ in range(200))

    def test_failed_trials_are_counted_not_raised(self):
        def explode(draw):
            raise ZeroDivisionError

        sim = run_simulation(self._variables(), explode, trials=50)
        assert sim.failed_trials == 50
        assert sim.mean_value is None


# ============================================================= data quality
class TestDataQuality:
    def test_real_filings_are_investment_grade(self):
        r = assess_data_quality(fact_sources={"annual_report"}, coverage=1.0,
                                history_years=10, upside=0.24, ev_ebitda=11.0)
        assert r.grade is DataGrade.INVESTMENT_GRADE
        assert r.is_illustrative is False
        assert r.disclosure is None

    def test_synthetic_data_forces_the_disclosure(self):
        r = assess_data_quality(fact_sources={"seed"}, coverage=1.0, history_years=10)
        assert r.is_illustrative is True
        assert r.disclosure == ILLUSTRATIVE_NOTICE

    def test_unknown_source_is_not_trusted(self):
        """Allowlist, not blocklist: an unrecognised source cannot be certified."""
        r = assess_data_quality(fact_sources={"reference_model"}, coverage=1.0,
                                history_years=10)
        assert r.is_illustrative is True

    def test_missing_source_is_critical(self):
        r = assess_data_quality(fact_sources=set(), coverage=1.0, history_years=10)
        assert any(i.key == "unknown_provenance" for i in r.issues)

    def test_implausible_upside_is_unreliable(self):
        r = assess_data_quality(fact_sources={"annual_report"}, coverage=1.0,
                                history_years=10, upside=42.0)
        assert r.grade is DataGrade.UNRELIABLE
        assert any(i.key == "implausible_upside" for i in r.blocking)

    def test_implausible_downside_is_unreliable(self):
        r = assess_data_quality(fact_sources={"annual_report"}, coverage=1.0,
                                history_years=10, upside=-0.95)
        assert r.grade is DataGrade.UNRELIABLE

    def test_absurd_multiple_is_flagged(self):
        r = assess_data_quality(fact_sources={"annual_report"}, coverage=1.0,
                                history_years=10, ev_ebitda=307.0)
        assert any(i.key == "implausible_multiple" for i in r.issues)

    def test_low_coverage_downgrades(self):
        r = assess_data_quality(fact_sources={"annual_report"}, coverage=0.20,
                                history_years=10)
        assert r.is_illustrative is True

    def test_broken_balance_sheet_is_unreliable(self):
        r = assess_data_quality(fact_sources={"annual_report"}, coverage=1.0,
                                history_years=10, balance_sheet_ties=False)
        assert r.grade is DataGrade.UNRELIABLE

    def test_non_convergence_is_unreliable(self):
        r = assess_data_quality(fact_sources={"annual_report"}, coverage=1.0,
                                history_years=10, forecast_converged=False)
        assert r.grade is DataGrade.UNRELIABLE

    def test_large_upside_warns_without_blocking(self):
        r = assess_data_quality(fact_sources={"annual_report"}, coverage=1.0,
                                history_years=10, upside=1.5)
        assert r.grade is DataGrade.INDICATIVE
        assert any(i.severity is Severity.WARN for i in r.issues)

    def test_ungrounded_assumptions_warn(self):
        r = assess_data_quality(
            fact_sources={"annual_report"}, coverage=1.0, history_years=10,
            assumption_provenance={"historical": 2, "default": 28},
        )
        assert any(i.key == "ungrounded_assumptions" for i in r.issues)

    def test_headline_always_present(self):
        for sources in ({"annual_report"}, {"seed"}, set()):
            assert assess_data_quality(fact_sources=sources).headline
