"""Unit tests for the working-capital and capex services."""
from __future__ import annotations

import pytest

from app.domain.calc import DAYS_IN_YEAR
from app.services.capex.service import CapexService
from app.services.working_capital.service import WorkingCapitalService


@pytest.fixture(scope="module")
def wc(incomes, balances):
    return WorkingCapitalService(incomes, balances, cost_of_debt=0.09)


@pytest.fixture(scope="module")
def cx(incomes, balances, cash_flows):
    return CapexService(incomes, balances, cash_flows)


def value(section, key, index=-1):
    for row in section.rows:
        if row.key == key:
            return row.values[index]
    raise KeyError(key)


class TestWorkingCapitalComponents:
    def test_nwc_excludes_cash_and_debt(self, wc, balances):
        """Operating NWC must ignore cash and all borrowings."""
        i = len(balances) - 1
        b = balances[i]
        assert wc.net_working_capital(i) == pytest.approx(
            (b.inventories + b.trade_receivables + b.other_current_assets)
            - (b.trade_payables + b.other_current_liabilities + b.short_term_provisions)
        )
        # differs from the accounting NWC, which nets cash and short-term debt
        assert wc.net_working_capital(i) != pytest.approx(b.net_working_capital)

    def test_gross_assets_less_liabilities_equals_nwc(self, wc, balances):
        i = len(balances) - 1
        assert wc.gross_wc_assets(i) - wc.operating_current_liabilities(i) == pytest.approx(
            wc.net_working_capital(i)
        )

    def test_nwc_change_sign_convention(self, wc):
        """Rising NWC absorbs cash, so the reported change is negative."""
        row = next(r for r in wc.components_section().rows if r.key == "nwc_change")
        assert row.values[0] is None
        assert row.values[-1] < 0  # growing business ties up more capital


class TestCycleDays:
    def test_dio_uses_cogs_and_average_inventory(self, wc, incomes, balances):
        i = len(incomes) - 1
        avg_inv = (balances[i].inventories + balances[i - 1].inventories) / 2
        assert wc.dio(i) == pytest.approx(avg_inv / incomes[i].total_cogs * DAYS_IN_YEAR)

    def test_dso_uses_revenue(self, wc, incomes, balances):
        i = len(incomes) - 1
        avg_rec = (balances[i].trade_receivables + balances[i - 1].trade_receivables) / 2
        assert wc.dso(i) == pytest.approx(avg_rec / incomes[i].total_revenue * DAYS_IN_YEAR)

    def test_dpo_uses_cogs_not_revenue(self, wc, incomes, balances):
        i = len(incomes) - 1
        avg_pay = (balances[i].trade_payables + balances[i - 1].trade_payables) / 2
        assert wc.dpo(i) == pytest.approx(avg_pay / incomes[i].total_cogs * DAYS_IN_YEAR)
        # would be materially different on revenue
        assert wc.dpo(i) != pytest.approx(avg_pay / incomes[i].total_revenue * DAYS_IN_YEAR)

    def test_operating_cycle_is_dio_plus_dso(self, wc):
        i = 5
        assert wc.operating_cycle(i) == pytest.approx(wc.dio(i) + wc.dso(i))

    def test_ccc_deducts_payable_days(self, wc):
        i = 5
        assert wc.ccc(i) == pytest.approx(wc.dio(i) + wc.dso(i) - wc.dpo(i))
        assert wc.ccc(i) < wc.operating_cycle(i)


class TestWorkingCapitalIntensity:
    def test_funding_cost_applies_cost_of_debt(self, wc):
        i = len(wc.incomes) - 1
        assert value(wc.intensity_section(), "funding_cost") == pytest.approx(
            wc.net_working_capital(i) * 0.09
        )

    def test_funding_cost_absent_without_assumption(self, incomes, balances):
        bare = WorkingCapitalService(incomes, balances, cost_of_debt=None)
        assert value(bare.intensity_section(), "funding_cost") is None

    def test_revenue_per_nwc_is_reciprocal_of_intensity(self, wc):
        sec = wc.intensity_section()
        assert value(sec, "revenue_per_nwc") == pytest.approx(
            1 / value(sec, "nwc_to_revenue"), rel=1e-9
        )


class TestWorkingCapitalFlags:
    def test_flags_are_emitted(self, wc):
        keys = {f.key for f in wc.flags()}
        assert keys == {"dso_outpacing_revenue", "inventory_days_high", "ccc_deteriorating"}

    def test_stable_business_does_not_trigger_ccc_alert(self, wc):
        ccc = next(f for f in wc.flags() if f.key == "ccc_deteriorating")
        assert ccc.triggered is False  # constant ratios ⇒ flat cycle

    def test_no_flags_when_history_too_short(self, incomes, balances):
        short = WorkingCapitalService(incomes[:1], balances[:1])
        assert short.flags() == []


class TestCapexSplit:
    def test_gross_capex_is_absolute(self, cx, cash_flows):
        i = len(cash_flows) - 1
        assert cx.gross_capex(i) == abs(cash_flows[i].capex)
        assert cx.gross_capex(i) > 0

    def test_maintenance_capped_at_gross(self, cx, incomes):
        """Maintenance capex cannot exceed what was actually spent."""
        for i in range(len(incomes)):
            assert cx.maintenance_capex(i) <= cx.gross_capex(i)

    def test_maintenance_plus_growth_equals_gross(self, cx, incomes):
        for i in range(len(incomes)):
            assert cx.maintenance_capex(i) + cx.growth_capex(i) == pytest.approx(
                cx.gross_capex(i)
            )

    def test_growth_capex_never_negative(self, cx, incomes):
        assert all(cx.growth_capex(i) >= 0 for i in range(len(incomes)))

    def test_maintenance_uses_depreciation_when_below_capex(self, cx, incomes):
        i = len(incomes) - 1
        assert cx.gross_capex(i) > incomes[i].depreciation
        assert cx.maintenance_capex(i) == pytest.approx(incomes[i].depreciation)


class TestCapexIntensity:
    def test_capex_to_da_above_one_signals_expansion(self, cx):
        assert value(cx.intensity_section(), "capex_to_da") > 1.0

    def test_growth_share_is_a_fraction(self, cx):
        share = value(cx.intensity_section(), "growth_share")
        assert 0 <= share <= 1

    def test_first_period_lagged_metrics_are_none(self, cx):
        sec = cx.intensity_section()
        assert value(sec, "gross_block_growth", 0) is None
        assert value(sec, "icor", 0) is None
        assert value(sec, "asset_turn_growth", 0) is None

    def test_asset_turn_uses_prior_year_growth_capex(self, cx, incomes):
        """Capital deployed last year produces this year's revenue."""
        i = len(incomes) - 1
        expected = (incomes[i].total_revenue - incomes[i - 1].total_revenue) / cx.growth_capex(i - 1)
        assert value(cx.intensity_section(), "asset_turn_growth") == pytest.approx(expected)
