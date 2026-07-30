"""Workbook-equivalence tests for the statements engine.

The fixtures below reproduce the synthetic companies used by the workbook's own
`v7_engine_test.py`, whose evaluated results are recorded in QA_Report_v7.md.
Reproducing those exact figures proves the Python engine implements the same
arithmetic as the spreadsheet.
"""
from __future__ import annotations

import pytest

from app.domain.financials.canonical import CanonicalFinancialsBuilder, Precedence
from app.domain.financials.line_items import LineItem as LI
from app.domain.financials.statements import (
    build_balance_sheet,
    build_cash_flow,
    build_income_statement,
)

YEARS = tuple(range(2016, 2026))


def _series(base: float, growth: float) -> list[float]:
    """Same generator as builder/v7_engine_test.py."""
    return [round(base * (1 + growth) ** i, 1) for i in range(10)]


def _company(scale: float, growth: float, margin: float):
    """Rebuilds the engine-test fixture, including the reserves plug."""
    rev = _series(scale, growth)
    d: dict[LI, list[float]] = {}
    d[LI.REVENUE] = rev
    d[LI.OTHER_OPERATING_INCOME] = [round(x * 0.012, 1) for x in rev]
    d[LI.RAW_MATERIALS] = [round(x * (1 - margin - 0.19), 1) for x in rev]
    d[LI.PURCHASE_STOCK_IN_TRADE] = [round(x * 0.05, 1) for x in rev]
    d[LI.CHANGE_INVENTORIES] = [0.0] * 10
    d[LI.EMPLOYEE_BENEFIT] = [round(x * 0.09, 1) for x in rev]
    d[LI.OTHER_EXPENSES] = [round(x * 0.10, 1) for x in rev]
    d[LI.DEPRECIATION] = [round(x * 0.035, 1) for x in rev]
    d[LI.OTHER_INCOME] = [round(x * 0.012, 1) for x in rev]
    d[LI.FINANCE_COSTS] = [round(x * 0.008, 1) for x in rev]
    d[LI.EXCEPTIONAL_ITEMS] = [0.0] * 10
    d[LI.TAX_EXPENSE] = [round(x * margin * 0.25, 1) for x in rev]
    d[LI.MINORITY_INTEREST] = [round(x * 0.001, 1) for x in rev]
    d[LI.OCI] = [0.0] * 10
    d[LI.DIVIDEND_PAID] = [round(x * margin * 0.18, 1) for x in rev]
    d[LI.WEIGHTED_SHARES] = [120.0] * 10

    d[LI.CASH_AND_BANK] = [round(x * 0.10, 1) for x in rev]
    d[LI.CURRENT_INVESTMENTS] = [round(x * 0.05, 1) for x in rev]
    d[LI.TRADE_RECEIVABLES] = [round(x * 0.12, 1) for x in rev]
    d[LI.INVENTORIES] = [round(x * 0.14, 1) for x in rev]
    d[LI.OTHER_CURRENT_ASSETS] = [round(x * 0.04, 1) for x in rev]
    d[LI.NET_BLOCK_PPE] = [round(x * 0.31, 1) for x in rev]
    d[LI.CWIP] = [round(x * 0.03, 1) for x in rev]
    d[LI.GOODWILL] = [round(x * 0.01, 1) for x in rev]
    d[LI.OTHER_INTANGIBLES] = [round(x * 0.008, 1) for x in rev]
    d[LI.LT_INVESTMENTS_ASSOCIATES] = [round(x * 0.02, 1) for x in rev]
    d[LI.OTHER_NCA] = [round(x * 0.014, 1) for x in rev]
    d[LI.DEFERRED_TAX_ASSET] = [round(x * 0.004, 1) for x in rev]
    d[LI.TRADE_PAYABLES] = [round(x * 0.10, 1) for x in rev]
    d[LI.SHORT_TERM_BORROWINGS] = [round(x * 0.03, 1) for x in rev]
    d[LI.CURRENT_MATURITIES_LTD] = [round(x * 0.011, 1) for x in rev]
    d[LI.OTHER_CURRENT_LIABILITIES] = [round(x * 0.035, 1) for x in rev]
    d[LI.SHORT_TERM_PROVISIONS] = [round(x * 0.014, 1) for x in rev]
    d[LI.LONG_TERM_BORROWINGS] = [round(x * 0.07, 1) for x in rev]
    d[LI.DEFERRED_TAX_LIABILITY] = [round(x * 0.017, 1) for x in rev]
    d[LI.OTHER_NCL] = [round(x * 0.012, 1) for x in rev]
    d[LI.EQUITY_SHARE_CAPITAL] = [120.0] * 10
    d[LI.MINORITY_INTEREST_BS] = [round(x * 0.006, 1) for x in rev]

    assets_keys = [
        LI.CASH_AND_BANK, LI.CURRENT_INVESTMENTS, LI.TRADE_RECEIVABLES,
        LI.INVENTORIES, LI.OTHER_CURRENT_ASSETS, LI.NET_BLOCK_PPE, LI.CWIP,
        LI.GOODWILL, LI.OTHER_INTANGIBLES, LI.LT_INVESTMENTS_ASSOCIATES,
        LI.OTHER_NCA, LI.DEFERRED_TAX_ASSET,
    ]
    liab_keys = [
        LI.TRADE_PAYABLES, LI.SHORT_TERM_BORROWINGS, LI.CURRENT_MATURITIES_LTD,
        LI.OTHER_CURRENT_LIABILITIES, LI.SHORT_TERM_PROVISIONS,
        LI.LONG_TERM_BORROWINGS, LI.DEFERRED_TAX_LIABILITY, LI.OTHER_NCL,
    ]
    reserves = []
    for i in range(10):
        assets = sum(d[k][i] for k in assets_keys)
        liab = sum(d[k][i] for k in liab_keys)
        reserves.append(
            round(assets - liab - d[LI.EQUITY_SHARE_CAPITAL][i] - d[LI.MINORITY_INTEREST_BS][i], 1)
        )
    d[LI.RESERVES_SURPLUS] = reserves
    return d


def _build(scale: float, growth: float, margin: float):
    data = _company(scale, growth, margin)
    b = CanonicalFinancialsBuilder("test", YEARS)
    for item, values in data.items():
        for year, value in zip(YEARS, values):
            b.add(item, year, value, Precedence.STORE, "fixture")
    return b.build()


# Fixture A == "Reliance Industries Ltd" in the workbook engine test
FIN_A = _build(9000, 0.11, 0.155)
# Fixture B == "Titan Company Ltd"
FIN_B = _build(3000, 0.19, 0.105)


class TestWorkbookEquivalence:
    """Assert the exact figures recorded in QA_Report_v7.md section 9.1."""

    def test_revenue_matches_workbook(self):
        assert build_income_statement(FIN_A, 2025).total_revenue == pytest.approx(23298.6, abs=0.05)
        assert build_income_statement(FIN_B, 2025).total_revenue == pytest.approx(14528.6, abs=0.05)

    def test_ebitda_matches_workbook(self):
        assert build_income_statement(FIN_A, 2025).ebitda == pytest.approx(2693.7, abs=0.05)
        assert build_income_statement(FIN_B, 2025).ebitda == pytest.approx(961.9, abs=0.05)

    def test_pat_matches_workbook(self):
        assert build_income_statement(FIN_A, 2025).pat == pytest.approx(1064.9, abs=0.05)
        assert build_income_statement(FIN_B, 2025).pat == pytest.approx(125.5, abs=0.05)

    def test_eps_matches_workbook(self):
        assert build_income_statement(FIN_A, 2025).eps_basic == pytest.approx(8.874, abs=0.001)
        assert build_income_statement(FIN_B, 2025).eps_basic == pytest.approx(1.0458, abs=0.001)

    @pytest.mark.parametrize("fin", [FIN_A, FIN_B], ids=["A", "B"])
    def test_balance_sheet_ties_every_year(self, fin):
        """Workbook probe: 'BS balance = ALL YEARS BALANCE' for both companies."""
        for year in YEARS:
            bs = build_balance_sheet(fin, year)
            assert bs.balances, f"{year} out by {bs.balance_check}"


class TestStatementIntegrity:
    def test_income_statement_subtotals_are_consistent(self):
        s = build_income_statement(FIN_A, 2025)
        assert s.total_revenue == pytest.approx(s.revenue_operations + s.other_operating_income)
        assert s.gross_profit == pytest.approx(s.total_revenue - s.total_cogs)
        assert s.ebitda == pytest.approx(s.gross_profit - s.total_opex)
        assert s.ebit == pytest.approx(s.ebitda - s.depreciation)
        assert s.pbt == pytest.approx(s.pbt_before_exceptional + s.exceptional_items)
        assert s.pat == pytest.approx(s.pat_before_minority - s.minority_interest)

    def test_balance_sheet_derived_aggregates(self):
        bs = build_balance_sheet(FIN_A, 2025)
        assert bs.gross_debt == pytest.approx(
            bs.short_term_borrowings + bs.current_maturities_ltd + bs.long_term_borrowings
        )
        assert bs.net_debt == pytest.approx(
            bs.gross_debt - bs.cash_and_bank - bs.current_investments
        )
        assert bs.net_working_capital == pytest.approx(
            bs.total_current_assets - bs.total_current_liabilities
        )

    def test_cash_flow_reconciles_to_closing_cash(self):
        cf = build_cash_flow(FIN_A, 2025)
        assert cf.closing_cash == pytest.approx(cf.opening_cash + cf.net_cash_flow)
        assert cf.net_cash_flow == pytest.approx(cf.cfo + cf.cfi + cf.cff)
        assert cf.free_cash_flow == pytest.approx(cf.cfo - cf.capex)

    def test_margins_are_ratios_of_total_revenue(self):
        s = build_income_statement(FIN_A, 2025)
        assert s.ebitda_margin == pytest.approx(s.ebitda / s.total_revenue)
        assert s.pat_margin == pytest.approx(s.pat / s.total_revenue)


class TestPrecedenceChain:
    """The `0C Data Map` 4-tier rule."""

    def test_override_beats_store(self):
        b = CanonicalFinancialsBuilder("c", [2025])
        b.add(LI.REVENUE, 2025, 100.0, Precedence.STORE)
        b.add(LI.REVENUE, 2025, 999.0, Precedence.OVERRIDE)
        assert b.build().get(LI.REVENUE, 2025) == 999.0

    def test_store_beats_alias(self):
        b = CanonicalFinancialsBuilder("c", [2025])
        b.add(LI.REVENUE, 2025, 50.0, Precedence.ALIAS)
        b.add(LI.REVENUE, 2025, 100.0, Precedence.STORE)
        assert b.build().get(LI.REVENUE, 2025) == 100.0

    def test_missing_resolves_to_none_not_a_fabricated_number(self):
        """Deliberate divergence from the workbook's sample-constant fallback."""
        fin = CanonicalFinancialsBuilder("c", [2025]).build()
        assert fin.get(LI.REVENUE, 2025) is None
        assert fin.at(LI.REVENUE, 2025) == 0.0
        assert not fin.has_data()

    def test_coverage_reports_real_density(self):
        b = CanonicalFinancialsBuilder("c", [2025])
        b.add(LI.REVENUE, 2025, 1.0, Precedence.STORE)
        fin = b.build()
        assert fin.coverage() == pytest.approx(1 / 54)
        assert fin.has_data()


class TestUndefinedRatios:
    def test_zero_revenue_yields_none_not_zero(self):
        """A margin on zero revenue is undefined — the workbook's 'n.m.'."""
        fin = CanonicalFinancialsBuilder("c", [2025]).build()
        s = build_income_statement(fin, 2025)
        assert s.ebitda_margin is None
        assert s.eps_basic is None
