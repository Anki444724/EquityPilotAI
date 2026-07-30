"""Ratio analysis service.

Implements the full ratio suite from the specification, organised into the same
six families: returns, Du Pont, profitability, liquidity, leverage/solvency and
efficiency.

Two conventions matter and are applied consistently:

* **Average balances.** Every ratio pairing a flow (revenue, PAT, EBIT) with a
  stock (equity, assets, capital) uses the average of opening and closing
  balances. Using closing balances would overstate returns in a growing firm.
* **Undefined, not zero.** A ratio with a zero or absent denominator returns
  ``None`` and renders as an em dash.

All statement values are read from the Module 1 engines; nothing is recomputed.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.domain.calc import DAYS_IN_YEAR, avg_balance, safe_div
from app.domain.financials.statements import (
    BalanceSheet,
    CashFlowStatement,
    IncomeStatement,
)
from app.schemas.common import MetricRow, MetricSection, Unit


@dataclass(frozen=True, slots=True)
class RatioInputs:
    """Everything a single period's ratios need, including the prior balance sheet."""

    income: IncomeStatement
    balance: BalanceSheet
    cash_flow: CashFlowStatement
    prior_balance: BalanceSheet | None
    prior_income: IncomeStatement | None
    wacc: float | None = None


# Altman Z-score coefficients (manufacturing variant), per the specification.
_ALTMAN = {
    "working_capital": 1.2,
    "retained_earnings": 1.4,
    "ebit": 3.3,
    "equity_to_liabilities": 0.6,
    "asset_turnover": 1.0,
}


class RatioService:
    """Computes every ratio for every reported period."""

    def __init__(
        self,
        incomes: list[IncomeStatement],
        balances: list[BalanceSheet],
        cash_flows: list[CashFlowStatement],
        wacc: float | None = None,
    ) -> None:
        self.incomes = incomes
        self.balances = balances
        self.cash_flows = cash_flows
        self.wacc = wacc

    # ------------------------------------------------------------- helpers
    def _inputs(self, i: int) -> RatioInputs:
        return RatioInputs(
            income=self.incomes[i],
            balance=self.balances[i],
            cash_flow=self.cash_flows[i],
            prior_balance=self.balances[i - 1] if i > 0 else None,
            prior_income=self.incomes[i - 1] if i > 0 else None,
            wacc=self.wacc,
        )

    def _avg(self, i: int, attr: str) -> float | None:
        """Average opening/closing balance, via the shared primitive."""
        return avg_balance(
            getattr(self.balances[i], attr, None),
            getattr(self.balances[i - 1], attr, None) if i > 0 else None,
        )

    def _series(self, fn) -> list[float | None]:
        return [fn(self._inputs(i), i) for i in range(len(self.incomes))]

    # ------------------------------------------------------- ratio families
    def return_ratios(self) -> MetricSection:
        rows = [
            MetricRow(key="roe_avg", label="Return on equity (avg equity)", unit=Unit.PERCENT,
                      values=self._series(lambda x, i: safe_div(x.income.pat, self._avg(i, "shareholders_equity")))),
            MetricRow(key="roe_closing", label="Return on equity (closing equity)", unit=Unit.PERCENT,
                      values=self._series(lambda x, i: safe_div(x.income.pat, x.balance.shareholders_equity))),
            MetricRow(key="roce", label="Return on capital employed (pre-tax)", unit=Unit.PERCENT,
                      values=self._series(lambda x, i: safe_div(x.income.ebit, self._avg(i, "capital_employed")))),
            MetricRow(key="roic", label="Return on invested capital (post-tax)", unit=Unit.PERCENT,
                      values=self._series(self._roic)),
            MetricRow(key="roa", label="Return on assets", unit=Unit.PERCENT,
                      values=self._series(lambda x, i: safe_div(x.income.pat, self._avg(i, "total_assets")))),
            MetricRow(key="rofa", label="Return on fixed assets", unit=Unit.PERCENT,
                      values=self._series(lambda x, i: safe_div(x.income.ebit, self._avg(i, "net_block_ppe")))),
            MetricRow(key="croic", label="Cash return on invested capital", unit=Unit.PERCENT,
                      values=self._series(lambda x, i: safe_div(x.cash_flow.free_cash_flow, self._avg(i, "invested_capital")))),
            MetricRow(key="roic_wacc_spread", label="ROIC − WACC spread", unit=Unit.PERCENT,
                      values=self._series(self._roic_spread),
                      note="Economic profit margin. Requires a WACC assumption."),
            MetricRow(key="eva", label="Economic value added", unit=Unit.CRORE,
                      values=self._series(self._eva)),
        ]
        return MetricSection(key="returns", title="A. Return ratios", rows=rows)

    def _nopat(self, x: RatioInputs) -> float | None:
        tax_rate = x.income.effective_tax_rate
        if tax_rate is None:
            return None
        return x.income.ebit * (1 - tax_rate)

    def _roic(self, x: RatioInputs, i: int) -> float | None:
        return safe_div(self._nopat(x), self._avg(i, "invested_capital"))

    def _roic_spread(self, x: RatioInputs, i: int) -> float | None:
        roic = self._roic(x, i)
        if roic is None or x.wacc is None:
            return None
        return roic - x.wacc

    def _eva(self, x: RatioInputs, i: int) -> float | None:
        nopat = self._nopat(x)
        capital = self._avg(i, "invested_capital")
        if nopat is None or capital is None or x.wacc is None:
            return None
        return nopat - x.wacc * capital

    def dupont(self) -> MetricSection:
        rows = [
            MetricRow(key="net_margin", label="Net profit margin", unit=Unit.PERCENT,
                      values=self._series(lambda x, i: x.income.pat_margin)),
            MetricRow(key="asset_turnover", label="Asset turnover", unit=Unit.MULTIPLE,
                      values=self._series(lambda x, i: safe_div(x.income.total_revenue, self._avg(i, "total_assets")))),
            MetricRow(key="equity_multiplier", label="Equity multiplier", unit=Unit.MULTIPLE,
                      values=self._series(lambda x, i: safe_div(self._avg(i, "total_assets"), self._avg(i, "shareholders_equity")))),
            MetricRow(key="dupont_roe", label="Du Pont reconstructed ROE", unit=Unit.PERCENT,
                      values=self._series(self._dupont_roe),
                      note="Margin × turnover × leverage. Should reconcile to ROE (avg equity)."),
            MetricRow(key="tax_burden", label="Tax burden (PAT / PBT)", unit=Unit.MULTIPLE,
                      values=self._series(lambda x, i: safe_div(x.income.pat, x.income.pbt))),
            MetricRow(key="interest_burden", label="Interest burden (PBT / EBIT)", unit=Unit.MULTIPLE,
                      values=self._series(lambda x, i: safe_div(x.income.pbt, x.income.ebit))),
            MetricRow(key="operating_margin_dupont", label="Operating margin (EBIT / revenue)", unit=Unit.PERCENT,
                      values=self._series(lambda x, i: x.income.ebit_margin)),
        ]
        return MetricSection(key="dupont", title="B. Du Pont decomposition", rows=rows)

    def _dupont_roe(self, x: RatioInputs, i: int) -> float | None:
        margin = x.income.pat_margin
        turnover = safe_div(x.income.total_revenue, self._avg(i, "total_assets"))
        leverage = safe_div(self._avg(i, "total_assets"), self._avg(i, "shareholders_equity"))
        if margin is None or turnover is None or leverage is None:
            return None
        return margin * turnover * leverage

    def profitability(self) -> MetricSection:
        rows = [
            MetricRow(key="gross_margin", label="Gross margin", unit=Unit.PERCENT,
                      values=self._series(lambda x, i: x.income.gross_margin)),
            MetricRow(key="ebitda_margin", label="EBITDA margin", unit=Unit.PERCENT,
                      values=self._series(lambda x, i: x.income.ebitda_margin)),
            MetricRow(key="ebit_margin", label="EBIT margin", unit=Unit.PERCENT,
                      values=self._series(lambda x, i: x.income.ebit_margin)),
            MetricRow(key="pretax_margin", label="Pre-tax margin", unit=Unit.PERCENT,
                      values=self._series(lambda x, i: safe_div(x.income.pbt, x.income.total_revenue))),
            MetricRow(key="net_margin_p", label="Net profit margin", unit=Unit.PERCENT,
                      values=self._series(lambda x, i: x.income.pat_margin)),
            MetricRow(key="cash_profit_margin", label="Cash profit margin (PAT + D&A)", unit=Unit.PERCENT,
                      values=self._series(lambda x, i: safe_div(x.income.pat + x.income.depreciation, x.income.total_revenue))),
            MetricRow(key="effective_tax_rate", label="Effective tax rate", unit=Unit.PERCENT,
                      values=self._series(lambda x, i: x.income.effective_tax_rate)),
            MetricRow(key="operating_leverage", label="Operating leverage (ΔEBIT% / ΔRevenue%)", unit=Unit.MULTIPLE,
                      values=self._series(self._operating_leverage)),
        ]
        return MetricSection(key="profitability", title="C. Profitability & margins", rows=rows)

    def _operating_leverage(self, x: RatioInputs, i: int) -> float | None:
        if x.prior_income is None:
            return None
        ebit_growth = safe_div(x.income.ebit, x.prior_income.ebit)
        rev_growth = safe_div(x.income.total_revenue, x.prior_income.total_revenue)
        if ebit_growth is None or rev_growth is None:
            return None
        return safe_div(ebit_growth - 1, rev_growth - 1)

    def liquidity(self) -> MetricSection:
        rows = [
            MetricRow(key="current_ratio", label="Current ratio", unit=Unit.MULTIPLE,
                      values=self._series(lambda x, i: safe_div(x.balance.total_current_assets, x.balance.total_current_liabilities))),
            MetricRow(key="quick_ratio", label="Quick ratio (acid test)", unit=Unit.MULTIPLE,
                      values=self._series(lambda x, i: safe_div(
                          x.balance.total_current_assets - x.balance.inventories,
                          x.balance.total_current_liabilities))),
            MetricRow(key="cash_ratio", label="Cash ratio", unit=Unit.MULTIPLE,
                      values=self._series(lambda x, i: safe_div(
                          x.balance.cash_and_bank + x.balance.current_investments,
                          x.balance.total_current_liabilities))),
            MetricRow(key="defensive_interval", label="Defensive interval", unit=Unit.DAYS,
                      values=self._series(self._defensive_interval)),
            MetricRow(key="nwc_to_revenue", label="Net working capital / revenue", unit=Unit.PERCENT,
                      values=self._series(lambda x, i: safe_div(x.balance.net_working_capital, x.income.total_revenue))),
            MetricRow(key="ocf_ratio", label="Operating cash flow ratio", unit=Unit.MULTIPLE,
                      values=self._series(lambda x, i: safe_div(x.cash_flow.cfo, x.balance.total_current_liabilities))),
        ]
        return MetricSection(key="liquidity", title="D. Liquidity", rows=rows)

    def _defensive_interval(self, x: RatioInputs, i: int) -> float | None:
        liquid = (
            x.balance.cash_and_bank
            + x.balance.current_investments
            + x.balance.trade_receivables
        )
        daily_opex = safe_div(x.income.total_cogs + x.income.total_opex, DAYS_IN_YEAR)
        return safe_div(liquid, daily_opex)

    def leverage(self) -> MetricSection:
        rows = [
            MetricRow(key="debt_equity", label="Debt / equity (gross)", unit=Unit.MULTIPLE,
                      values=self._series(lambda x, i: safe_div(x.balance.gross_debt, x.balance.shareholders_equity))),
            MetricRow(key="net_debt_equity", label="Net debt / equity", unit=Unit.MULTIPLE,
                      values=self._series(lambda x, i: safe_div(x.balance.net_debt, x.balance.shareholders_equity))),
            MetricRow(key="debt_assets", label="Debt / total assets", unit=Unit.MULTIPLE,
                      values=self._series(lambda x, i: safe_div(x.balance.gross_debt, x.balance.total_assets))),
            MetricRow(key="debt_capital_employed", label="Debt / capital employed", unit=Unit.MULTIPLE,
                      values=self._series(lambda x, i: safe_div(x.balance.gross_debt, x.balance.capital_employed))),
            MetricRow(key="net_debt_ebitda", label="Net debt / EBITDA", unit=Unit.MULTIPLE,
                      values=self._series(lambda x, i: safe_div(x.balance.net_debt, x.income.ebitda))),
            MetricRow(key="gross_debt_ebitda", label="Gross debt / EBITDA", unit=Unit.MULTIPLE,
                      values=self._series(lambda x, i: safe_div(x.balance.gross_debt, x.income.ebitda))),
            MetricRow(key="interest_coverage", label="Interest coverage (EBIT / interest)", unit=Unit.MULTIPLE,
                      values=self._series(lambda x, i: safe_div(x.income.ebit, x.income.finance_costs))),
            MetricRow(key="ebitda_interest_coverage", label="EBITDA interest coverage", unit=Unit.MULTIPLE,
                      values=self._series(lambda x, i: safe_div(x.income.ebitda, x.income.finance_costs))),
            MetricRow(key="cash_interest_coverage", label="Cash interest coverage", unit=Unit.MULTIPLE,
                      values=self._series(lambda x, i: safe_div(
                          x.cash_flow.cfo + x.income.finance_costs, x.income.finance_costs))),
            MetricRow(key="dscr", label="Debt service coverage ratio", unit=Unit.MULTIPLE,
                      values=self._series(lambda x, i: safe_div(
                          x.income.ebitda,
                          x.income.finance_costs + x.balance.current_maturities_ltd))),
            MetricRow(key="financial_leverage", label="Financial leverage (assets / equity)", unit=Unit.MULTIPLE,
                      values=self._series(lambda x, i: safe_div(x.balance.total_assets, x.balance.shareholders_equity))),
            MetricRow(key="equity_ratio", label="Equity ratio (equity / assets)", unit=Unit.PERCENT,
                      values=self._series(lambda x, i: safe_div(x.balance.shareholders_equity, x.balance.total_assets))),
            MetricRow(key="altman_z", label="Altman Z-score (manufacturing)", unit=Unit.RATIO,
                      values=self._series(self._altman_z),
                      note="Above 2.99 safe · 1.81–2.99 grey · below 1.81 distress"),
        ]
        return MetricSection(key="leverage", title="E. Leverage & solvency", rows=rows)

    def _altman_z(self, x: RatioInputs, i: int) -> float | None:
        b, inc = x.balance, x.income
        assets = b.total_assets
        if assets == 0:
            return None
        components = [
            _ALTMAN["working_capital"] * safe_div(b.net_working_capital, assets),
            _ALTMAN["retained_earnings"] * safe_div(b.reserves_surplus, assets),
            _ALTMAN["ebit"] * safe_div(inc.ebit, assets),
            _ALTMAN["equity_to_liabilities"] * safe_div(b.shareholders_equity, b.total_liabilities),
            _ALTMAN["asset_turnover"] * safe_div(inc.total_revenue, assets),
        ]
        if any(c is None for c in components):
            return None
        return sum(components)  # type: ignore[arg-type]

    def efficiency(self) -> MetricSection:
        rows = [
            MetricRow(key="asset_turnover_e", label="Asset turnover", unit=Unit.MULTIPLE,
                      values=self._series(lambda x, i: safe_div(x.income.total_revenue, self._avg(i, "total_assets")))),
            MetricRow(key="fixed_asset_turnover", label="Fixed-asset turnover", unit=Unit.MULTIPLE,
                      values=self._series(lambda x, i: safe_div(x.income.total_revenue, self._avg(i, "net_block_ppe")))),
            MetricRow(key="inventory_turnover", label="Inventory turnover", unit=Unit.MULTIPLE,
                      values=self._series(lambda x, i: safe_div(x.income.total_cogs, self._avg(i, "inventories")))),
            MetricRow(key="receivables_turnover", label="Receivables turnover", unit=Unit.MULTIPLE,
                      values=self._series(lambda x, i: safe_div(x.income.total_revenue, self._avg(i, "trade_receivables")))),
            MetricRow(key="payables_turnover", label="Payables turnover", unit=Unit.MULTIPLE,
                      values=self._series(lambda x, i: safe_div(x.income.total_cogs, self._avg(i, "trade_payables")))),
            MetricRow(key="working_capital_turnover", label="Working-capital turnover", unit=Unit.MULTIPLE,
                      values=self._series(lambda x, i: safe_div(x.income.total_revenue, self._avg(i, "net_working_capital")))),
            MetricRow(key="capital_employed_turnover", label="Capital-employed turnover", unit=Unit.MULTIPLE,
                      values=self._series(lambda x, i: safe_div(x.income.total_revenue, self._avg(i, "capital_employed")))),
        ]
        return MetricSection(key="efficiency", title="F. Efficiency & turnover", rows=rows)

    def all_sections(self) -> list[MetricSection]:
        return [
            self.return_ratios(),
            self.dupont(),
            self.profitability(),
            self.liquidity(),
            self.leverage(),
            self.efficiency(),
        ]
