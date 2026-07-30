"""Unit tests for the ratio service.

Ratios are verified by independent recomputation from the underlying statement
values — never by comparing the service against itself.
"""
from __future__ import annotations

import pytest

from app.services.ratios.service import RatioService


@pytest.fixture(scope="module")
def svc(incomes, balances, cash_flows):
    return RatioService(incomes, balances, cash_flows, wacc=0.12)


def value(section, key, index=-1):
    for row in section.rows:
        if row.key == key:
            return row.values[index]
    raise KeyError(key)


class TestStructure:
    def test_six_families(self, svc):
        keys = [s.key for s in svc.all_sections()]
        assert keys == ["returns", "dupont", "profitability", "liquidity", "leverage", "efficiency"]

    def test_at_least_45_ratios(self, svc):
        n = sum(len(s.rows) for s in svc.all_sections())
        assert n >= 45, f"specification requires 45+ ratios, found {n}"

    def test_every_row_covers_every_period(self, svc, incomes):
        for section in svc.all_sections():
            for row in section.rows:
                assert len(row.values) == len(incomes), f"{row.key} has ragged periods"

    def test_row_keys_are_unique_within_a_section(self, svc):
        for section in svc.all_sections():
            keys = [r.key for r in section.rows]
            assert len(keys) == len(set(keys)), f"duplicate keys in {section.key}"


class TestReturnRatios:
    def test_roe_uses_average_equity(self, svc, incomes, balances):
        """ROE must divide by average, not closing, equity."""
        expected = incomes[-1].pat / (
            (balances[-1].shareholders_equity + balances[-2].shareholders_equity) / 2
        )
        assert value(svc.return_ratios(), "roe_avg") == pytest.approx(expected)

    def test_roe_closing_differs_from_average(self, svc):
        """A growing balance sheet makes the two measures diverge."""
        sec = svc.return_ratios()
        assert value(sec, "roe_avg") != pytest.approx(value(sec, "roe_closing"))

    def test_roce_uses_ebit_over_average_capital_employed(self, svc, incomes, balances):
        expected = incomes[-1].ebit / (
            (balances[-1].capital_employed + balances[-2].capital_employed) / 2
        )
        assert value(svc.return_ratios(), "roce") == pytest.approx(expected)

    def test_roic_is_post_tax(self, svc, incomes):
        """ROIC must apply the effective tax rate to EBIT."""
        roic = value(svc.return_ratios(), "roic")
        roce = value(svc.return_ratios(), "roce")
        assert roic < roce  # NOPAT < EBIT whenever tax is positive
        assert incomes[-1].effective_tax_rate > 0

    def test_roic_wacc_spread(self, svc):
        sec = svc.return_ratios()
        assert value(sec, "roic_wacc_spread") == pytest.approx(value(sec, "roic") - 0.12)

    def test_spread_and_eva_are_none_without_wacc(self, incomes, balances, cash_flows):
        no_wacc = RatioService(incomes, balances, cash_flows, wacc=None)
        assert value(no_wacc.return_ratios(), "roic_wacc_spread") is None
        assert value(no_wacc.return_ratios(), "eva") is None

    def test_eva_sign_matches_spread(self, svc):
        sec = svc.return_ratios()
        assert (value(sec, "eva") < 0) == (value(sec, "roic_wacc_spread") < 0)


class TestDuPont:
    def test_reconstructed_roe_matches_roe(self, svc):
        """The decomposition must multiply back to ROE on average equity."""
        assert value(svc.dupont(), "dupont_roe") == pytest.approx(
            value(svc.return_ratios(), "roe_avg"), rel=1e-9
        )

    def test_five_step_components_multiply_to_roe(self, svc):
        d = svc.dupont()
        product = (
            value(d, "tax_burden")
            * value(d, "interest_burden")
            * value(d, "operating_margin_dupont")
            * value(d, "asset_turnover")
            * value(d, "equity_multiplier")
        )
        assert product == pytest.approx(value(svc.return_ratios(), "roe_avg"), rel=1e-9)


class TestProfitability:
    def test_margins_match_statement_values(self, svc, incomes):
        p = svc.profitability()
        assert value(p, "gross_margin") == pytest.approx(incomes[-1].gross_margin)
        assert value(p, "ebitda_margin") == pytest.approx(incomes[-1].ebitda_margin)
        assert value(p, "net_margin_p") == pytest.approx(incomes[-1].pat_margin)

    def test_margin_ordering_is_coherent(self, svc):
        p = svc.profitability()
        assert value(p, "gross_margin") > value(p, "ebitda_margin")
        assert value(p, "ebitda_margin") > value(p, "ebit_margin")
        assert value(p, "ebit_margin") > value(p, "net_margin_p")

    def test_operating_leverage_undefined_in_first_period(self, svc):
        assert value(svc.profitability(), "operating_leverage", 0) is None


class TestLiquidity:
    def test_quick_ratio_excludes_inventory(self, svc):
        liq = svc.liquidity()
        assert value(liq, "quick_ratio") < value(liq, "current_ratio")

    def test_cash_ratio_is_the_strictest(self, svc):
        liq = svc.liquidity()
        assert value(liq, "cash_ratio") < value(liq, "quick_ratio")

    def test_current_ratio_recomputed(self, svc, balances):
        expected = balances[-1].total_current_assets / balances[-1].total_current_liabilities
        assert value(svc.liquidity(), "current_ratio") == pytest.approx(expected)


class TestLeverage:
    def test_net_debt_ratios_are_below_gross(self, svc):
        lev = svc.leverage()
        assert value(lev, "net_debt_ebitda") < value(lev, "gross_debt_ebitda")

    def test_ebitda_coverage_exceeds_ebit_coverage(self, svc):
        lev = svc.leverage()
        assert value(lev, "ebitda_interest_coverage") > value(lev, "interest_coverage")

    def test_altman_z_is_computed(self, svc):
        z = value(svc.leverage(), "altman_z")
        assert z is not None and 0 < z < 20

    def test_equity_ratio_is_inverse_of_leverage(self, svc):
        lev = svc.leverage()
        assert value(lev, "equity_ratio") == pytest.approx(
            1 / value(lev, "financial_leverage"), rel=1e-9
        )


class TestEfficiency:
    def test_turnover_uses_average_balances(self, svc, incomes, balances):
        expected = incomes[-1].total_revenue / (
            (balances[-1].total_assets + balances[-2].total_assets) / 2
        )
        assert value(svc.efficiency(), "asset_turnover_e") == pytest.approx(expected)

    def test_inventory_turnover_uses_cogs(self, svc, incomes, balances):
        expected = incomes[-1].total_cogs / (
            (balances[-1].inventories + balances[-2].inventories) / 2
        )
        assert value(svc.efficiency(), "inventory_turnover") == pytest.approx(expected)


class TestDegenerateInputs:
    def test_empty_company_yields_none_not_crash(self):
        from tests.conftest import make_financials
        from app.domain.financials.statements import (
            build_balance_sheet, build_cash_flow, build_income_statement,
        )
        from app.domain.financials.canonical import CanonicalFinancialsBuilder

        empty = CanonicalFinancialsBuilder("empty", [2025]).build()
        svc = RatioService(
            [build_income_statement(empty, 2025)],
            [build_balance_sheet(empty, 2025)],
            [build_cash_flow(empty, 2025)],
        )
        for section in svc.all_sections():
            for row in section.rows:
                assert all(v is None for v in row.values), f"{row.key} fabricated a value"
