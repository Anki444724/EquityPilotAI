"""Unit tests for the forecast engine.

Every projection is verified by independent recomputation from first
principles, never by comparing the engine against itself.
"""
from __future__ import annotations

import pytest

from app.domain.forecast.assumptions import (
    Driver, ForecastAssumptions, Provenance, RevenueMethod, Scenario,
    SegmentAssumption, driver,
)
from app.domain.forecast.capex import CapexForecast
from app.domain.forecast.debt import (
    CONVERGENCE_TOLERANCE, MAX_ITERATIONS, solve_debt_schedule,
)
from app.domain.forecast.depreciation import build_schedule
from app.domain.forecast.engine import ForecastBase, ForecastEngine
from app.domain.forecast.margins import MarginForecast
from app.domain.forecast.revenue import RevenueForecast, faded_growth
from app.domain.forecast.taxes import TaxForecast
from app.domain.forecast.working_capital import WorkingCapitalForecast


def base_position(**kw) -> ForecastBase:
    defaults = dict(
        fiscal_year=2025, revenue=10_000.0, ebitda=1_800.0, net_block=3_100.0,
        net_working_capital=1_500.0, gross_debt=1_600.0, cash=900.0,
        shares_outstanding=120.0, equity=5_000.0, invested_capital=5_700.0,
    )
    return ForecastBase(**{**defaults, **kw})


# ===================================================================== drivers
class TestDriver:
    def test_scalar_default_applies_to_every_period(self):
        d = driver(0.12)
        assert d.at(1) == d.at(7) == 0.12

    def test_per_year_override_wins(self):
        d = Driver(value=0.10, by_year={2: 0.25})
        assert d.at(1) == 0.10
        assert d.at(2) == 0.25

    def test_shifted_is_additive(self):
        """Point shifts, for rates an analyst thinks about in bps."""
        assert driver(0.18).shifted(0.02).value == pytest.approx(0.20)

    def test_scaled_is_proportional(self):
        assert driver(12.0).scaled(1.25).value == pytest.approx(15.0)

    def test_shift_propagates_into_per_year_values(self):
        d = Driver(value=0.10, by_year={1: 0.20}).shifted(0.05)
        assert d.at(1) == pytest.approx(0.25)
        assert d.at(2) == pytest.approx(0.15)


class TestAssumptions:
    def test_thirty_drivers_exposed(self):
        assert len(ForecastAssumptions().driver_names()) == 30

    def test_nothing_is_hardcoded_all_drivers_overridable(self):
        a = ForecastAssumptions()
        for name in a.driver_names():
            updated = a.with_drivers({name: 0.4242})
            assert updated.get(name).value == pytest.approx(0.4242)

    def test_updates_record_provenance(self):
        a = ForecastAssumptions().with_drivers(
            {"ebitda_margin": 0.25}, source=Provenance.AI_EXTRACTED
        )
        assert a.ebitda_margin.source is Provenance.AI_EXTRACTED

    def test_unknown_driver_is_ignored_not_crashing(self):
        a = ForecastAssumptions().with_drivers({"not_a_driver": 1.0})
        assert isinstance(a, ForecastAssumptions)

    def test_assumptions_are_immutable(self):
        a = ForecastAssumptions()
        b = a.with_drivers({"wacc": 0.13})
        assert a.wacc.value != b.wacc.value


# ===================================================================== revenue
class TestRevenue:
    def test_flat_cagr_compounds(self):
        a = ForecastAssumptions(years=3, revenue_growth=driver(0.10))
        rows = RevenueForecast(1_000, 2025, a).project()
        assert [r.revenue for r in rows] == pytest.approx([1100, 1210, 1331])

    def test_fiscal_years_advance_from_the_base(self):
        rows = RevenueForecast(1_000, 2025, ForecastAssumptions(years=3)).project()
        assert [r.fiscal_year for r in rows] == [2026, 2027, 2028]

    def test_volume_and_price_compound_not_add(self):
        """6% volume with 4% price is 10.24%, not 10%."""
        a = ForecastAssumptions(
            years=1, revenue_method=RevenueMethod.VOLUME_PRICE,
            volume_growth=driver(0.06), price_growth=driver(0.04),
        )
        row = RevenueForecast(1_000, 2025, a).project()[0]
        assert row.growth == pytest.approx(0.1024)
        assert row.growth != pytest.approx(0.10)

    def test_fade_decays_to_the_terminal_rate(self):
        a = ForecastAssumptions(
            years=5, revenue_growth=driver(0.20),
            terminal_revenue_growth=driver(0.05), growth_fade=driver(1.0),
        )
        growths = [r.growth for r in RevenueForecast(1_000, 2025, a).project()]
        assert growths[0] == pytest.approx(0.20)
        assert growths[-1] == pytest.approx(0.05)
        assert growths == sorted(growths, reverse=True)

    def test_zero_fade_holds_the_rate_flat(self):
        a = ForecastAssumptions(
            years=5, revenue_growth=driver(0.20),
            terminal_revenue_growth=driver(0.05), growth_fade=driver(0.0),
        )
        assert all(
            r.growth == pytest.approx(0.20)
            for r in RevenueForecast(1_000, 2025, a).project()
        )

    def test_partial_fade_lands_between(self):
        a = ForecastAssumptions(
            years=5, revenue_growth=driver(0.20),
            terminal_revenue_growth=driver(0.00), growth_fade=driver(0.5),
        )
        assert faded_growth(a, 5, 0.20) == pytest.approx(0.10)

    def test_segment_build_sums_the_parts(self):
        segs = (
            SegmentAssumption("A", 600.0, driver(0.10)),
            SegmentAssumption("B", 400.0, driver(0.20)),
        )
        a = ForecastAssumptions(years=1, revenue_method=RevenueMethod.SEGMENT, segments=segs)
        row = RevenueForecast(1_000, 2025, a).project()[0]
        assert row.revenue == pytest.approx(600 * 1.10 + 400 * 1.20)
        assert dict(row.segment_revenue) == pytest.approx({"A": 660.0, "B": 480.0})

    def test_segment_mix_shift_lifts_blended_growth(self):
        segs = (
            SegmentAssumption("Slow", 900.0, driver(0.02)),
            SegmentAssumption("Fast", 100.0, driver(0.50)),
        )
        a = ForecastAssumptions(years=3, revenue_method=RevenueMethod.SEGMENT, segments=segs)
        growths = [r.growth for r in RevenueForecast(1_000, 2025, a).project()]
        assert growths[-1] > growths[0]  # fast segment compounds into the mix

    def test_organic_and_acquired_split_reconciles(self):
        a = ForecastAssumptions(
            years=3, revenue_method=RevenueMethod.ORGANIC_ACQUISITION,
            organic_growth=driver(0.09), acquisition_growth=driver(0.03),
        )
        for row in RevenueForecast(1_000, 2025, a).project():
            assert row.organic_revenue + row.acquired_revenue == pytest.approx(row.revenue)
            assert row.acquired_revenue > 0

    def test_segment_method_falls_back_when_no_segments_defined(self):
        a = ForecastAssumptions(
            years=2, revenue_method=RevenueMethod.SEGMENT,
            segments=(), revenue_growth=driver(0.10),
        )
        rows = RevenueForecast(1_000, 2025, a).project()
        assert rows[0].revenue == pytest.approx(1100)


# ===================================================================== margins
class TestMargins:
    def test_margin_expansion_is_cumulative(self):
        a = ForecastAssumptions(
            years=3, ebitda_margin=driver(0.18), margin_expansion=driver(0.01)
        )
        mf = MarginForecast(a)
        assert mf.ebitda_margin_at(1) == pytest.approx(0.19)
        assert mf.ebitda_margin_at(2) == pytest.approx(0.20)
        assert mf.ebitda_margin_at(3) == pytest.approx(0.21)

    def test_ebit_is_ebitda_less_supplied_depreciation(self):
        a = ForecastAssumptions(years=2, ebitda_margin=driver(0.20))
        rows = RevenueForecast(1_000, 2025, a).project()
        margins = MarginForecast(a).project(rows, [50.0, 60.0])
        assert margins[0].ebit == pytest.approx(margins[0].ebitda - 50.0)
        assert margins[1].ebit == pytest.approx(margins[1].ebitda - 60.0)


# ======================================================================= capex
class TestCapexAndDepreciation:
    def test_net_block_rolls_forward(self):
        a = ForecastAssumptions(
            years=3, capex_pct_revenue=driver(0.05), depreciation_rate=driver(0.10)
        )
        rows = RevenueForecast(1_000, 2025, a).project()
        capex = CapexForecast(500.0, a).project(rows)
        assert capex[0].opening_net_block == 500.0
        for i, c in enumerate(capex):
            assert c.closing_net_block == pytest.approx(
                c.opening_net_block + c.capex - c.depreciation
            )
            if i:
                assert c.opening_net_block == pytest.approx(capex[i - 1].closing_net_block)

    def test_depreciation_charged_on_opening_block(self):
        """A year's own capex must not depreciate before commissioning."""
        a = ForecastAssumptions(years=1, depreciation_rate=driver(0.10))
        rows = RevenueForecast(1_000, 2025, a).project()
        c = CapexForecast(500.0, a).project(rows)[0]
        assert c.depreciation == pytest.approx(50.0)

    def test_maintenance_and_growth_split_sums_to_capex(self):
        a = ForecastAssumptions(years=3, maintenance_capex_pct=driver(0.6))
        rows = RevenueForecast(1_000, 2025, a).project()
        for c in CapexForecast(500.0, a).project(rows):
            assert c.maintenance_capex + c.growth_capex == pytest.approx(c.capex)
            assert c.maintenance_capex == pytest.approx(c.capex * 0.6)

    def test_schedule_view_matches_the_rollforward(self):
        a = ForecastAssumptions(years=3)
        rows = RevenueForecast(1_000, 2025, a).project()
        capex = CapexForecast(500.0, a).project(rows)
        for s, c in zip(build_schedule(capex), capex):
            assert s.depreciation == c.depreciation
            assert s.reinvestment_ratio == pytest.approx(c.capex / c.depreciation)


# ============================================================ working capital
class TestWorkingCapital:
    def test_days_drive_the_balances(self):
        a = ForecastAssumptions(
            years=1, ebitda_margin=driver(0.20),
            inventory_days=driver(73.0), receivable_days=driver(36.5),
            payable_days=driver(36.5),
            other_ca_pct_revenue=driver(0.0), other_cl_pct_revenue=driver(0.0),
        )
        rows = RevenueForecast(1_000, 2025, a).project()
        margins = MarginForecast(a).project(rows, [0.0])
        wc = WorkingCapitalForecast(0.0, a).project(margins)[0]
        cogs = margins[0].revenue * 0.80
        assert wc.inventories == pytest.approx(cogs * 73.0 / 365)
        assert wc.receivables == pytest.approx(margins[0].revenue * 36.5 / 365)
        assert wc.payables == pytest.approx(cogs * 36.5 / 365)

    def test_cash_conversion_cycle_identity(self):
        a = ForecastAssumptions(years=2)
        rows = RevenueForecast(1_000, 2025, a).project()
        margins = MarginForecast(a).project(rows, [0.0, 0.0])
        for w in WorkingCapitalForecast(100.0, a).project(margins):
            assert w.cash_conversion_cycle == pytest.approx(
                w.inventory_days + w.receivable_days - w.payable_days
            )

    def test_growth_absorbs_cash(self):
        """Rising working capital is a cash outflow, so the change is negative."""
        a = ForecastAssumptions(years=3, revenue_growth=driver(0.20))
        rows = RevenueForecast(1_000, 2025, a).project()
        margins = MarginForecast(a).project(rows, [0.0] * 3)
        changes = [w.change_in_nwc for w in WorkingCapitalForecast(100.0, a).project(margins)]
        assert all(c < 0 for c in changes)

    def test_shrinking_days_release_cash(self):
        a = ForecastAssumptions(
            years=2, revenue_growth=driver(0.0),
            receivable_days=Driver(value=60.0, by_year={2: 30.0}),
        )
        rows = RevenueForecast(1_000, 2025, a).project()
        margins = MarginForecast(a).project(rows, [0.0, 0.0])
        wc = WorkingCapitalForecast(0.0, a).project(margins)
        assert wc[1].change_in_nwc > 0


# ======================================================================== debt
class TestDebtSolver:
    def _solve(self, **kw):
        defaults = dict(
            opening_debt=1_000.0, opening_cash=200.0,
            assumptions=ForecastAssumptions(years=3),
            revenue=[1_000.0] * 3,
            cash_before_financing=[300.0] * 3,
            dividends=[50.0] * 3,
        )
        return solve_debt_schedule(**{**defaults, **kw})

    def test_converges(self):
        s = self._solve()
        assert s.converged
        assert s.residual < CONVERGENCE_TOLERANCE
        assert s.iterations <= MAX_ITERATIONS

    def test_interest_charged_on_average_balance(self):
        s = self._solve()
        a = ForecastAssumptions(years=3)
        for row in s.years:
            expected_avg = (row.opening_debt + row.closing_debt) / 2
            assert row.average_debt == pytest.approx(expected_avg)
            assert row.interest_expense == pytest.approx(
                expected_avg * a.interest_rate.at(row.period)
            )

    def test_balances_roll_forward(self):
        s = self._solve()
        for i, row in enumerate(s.years[1:], start=1):
            assert row.opening_debt == pytest.approx(s.years[i - 1].closing_debt)
            assert row.opening_cash == pytest.approx(s.years[i - 1].closing_cash)

    def test_debt_never_goes_negative(self):
        s = self._solve(cash_before_financing=[5_000.0] * 3)
        assert all(row.closing_debt >= 0 for row in s.years)

    def test_cash_sweep_retains_the_minimum_buffer(self):
        a = ForecastAssumptions(years=3, min_cash_pct_revenue=driver(0.10))
        s = self._solve(assumptions=a, cash_before_financing=[2_000.0] * 3)
        # once debt is repaid the sweep stops and cash accumulates
        assert s.years[-1].closing_debt == pytest.approx(0.0)
        assert s.years[-1].closing_cash > 100.0

    def test_no_debt_means_no_interest(self):
        s = self._solve(opening_debt=0.0)
        assert all(row.interest_expense == pytest.approx(0.0) for row in s.years)

    def test_solution_is_a_true_fixed_point(self):
        """Re-running with the converged interest must reproduce it."""
        s = self._solve()
        again = self._solve()
        for a_row, b_row in zip(s.years, again.years):
            assert a_row.interest_expense == pytest.approx(b_row.interest_expense)
            assert a_row.closing_debt == pytest.approx(b_row.closing_debt)


# ======================================================================= taxes
class TestTaxes:
    def test_nopat_uses_ebit_not_pbt(self):
        t = TaxForecast(ForecastAssumptions(effective_tax_rate=driver(0.25)))
        row = t.compute(1, 2026, pbt=800.0, ebit=1_000.0, interest=200.0)
        assert row.nopat == pytest.approx(750.0)
        assert row.pat == pytest.approx(600.0)

    def test_interest_tax_shield(self):
        t = TaxForecast(ForecastAssumptions(effective_tax_rate=driver(0.30)))
        row = t.compute(1, 2026, pbt=800.0, ebit=1_000.0, interest=200.0)
        assert row.interest_tax_shield == pytest.approx(60.0)


# ====================================================== integrated engine
class TestIntegratedEngine:
    @pytest.fixture(scope="class")
    def result(self):
        return ForecastEngine(base_position(), ForecastAssumptions(years=5)).run()

    def test_horizon_respected(self, result):
        assert len(result.years) == 5

    @pytest.mark.parametrize("years", [3, 5, 10])
    def test_all_supported_horizons(self, years):
        r = ForecastEngine(base_position(), ForecastAssumptions(years=years)).run()
        assert len(r.years) == years
        assert r.debt_converged and r.all_reconciled

    def test_debt_schedule_converges(self, result):
        assert result.debt_converged

    def test_both_fcff_builds_agree(self, result):
        """Top-down and bottom-up FCFF must reconcile, or a schedule is wrong."""
        assert result.all_reconciled
        for cf in result.cash_flow_rows:
            assert cf.fcff == pytest.approx(cf.fcff_reconciled, abs=1e-6)

    def test_fcff_identity(self, result):
        for i, cf in enumerate(result.cash_flow_rows):
            y = result.years[i]
            expected = (
                result.tax_rows[i].nopat + y.depreciation + y.change_in_nwc - y.capex
            )
            assert cf.fcff == pytest.approx(expected)

    def test_cash_flow_statement_articulates(self, result):
        for y in result.years:
            assert y.cfo + y.cfi + y.cff == pytest.approx(
                result.cash_flow_rows[y.period - 1].net_cash_flow
            )

    def test_pat_derived_from_pbt_and_tax(self, result):
        for y in result.years:
            assert y.pat == pytest.approx(y.pbt - y.tax_expense)
            assert y.tax_expense == pytest.approx(y.pbt * y.effective_tax_rate)

    def test_pbt_includes_interest(self, result):
        """Tax must be charged on post-interest profit, not on EBIT."""
        for i, y in enumerate(result.years):
            expected = (
                y.ebit + y.other_income
                + result.debt_rows[i].interest_income - y.interest_expense
            )
            assert y.pbt == pytest.approx(expected)

    def test_eps_from_pat_and_share_count(self, result):
        for y in result.years:
            assert y.eps == pytest.approx(y.pat / 120.0)

    def test_equity_rolls_forward_with_retained_earnings(self, result):
        a = ForecastAssumptions(years=5)
        equity = 5_000.0
        for y in result.years:
            equity += y.pat * (1 - a.dividend_payout.at(y.period))
            assert y.equity == pytest.approx(equity)

    def test_net_debt_identity(self, result):
        for y in result.years:
            assert y.net_debt == pytest.approx(y.gross_debt - y.cash)

    def test_revenue_cagr_matches_the_projection(self, result):
        implied = (result.years[-1].revenue / 10_000.0) ** (1 / 5) - 1
        assert result.revenue_cagr == pytest.approx(implied)

    def test_zero_growth_produces_flat_revenue(self):
        a = ForecastAssumptions(years=3, revenue_growth=driver(0.0))
        r = ForecastEngine(base_position(), a).run()
        assert all(y.revenue == pytest.approx(10_000.0) for y in r.years)

    def test_debt_free_company_has_no_interest(self):
        r = ForecastEngine(
            base_position(gross_debt=0.0), ForecastAssumptions(years=3)
        ).run()
        assert all(y.interest_expense == pytest.approx(0.0) for y in r.years)

    def test_engine_is_deterministic(self):
        a = ForecastAssumptions(years=5)
        first = ForecastEngine(base_position(), a).run()
        second = ForecastEngine(base_position(), a).run()
        assert [y.fcff for y in first.years] == pytest.approx([y.fcff for y in second.years])
