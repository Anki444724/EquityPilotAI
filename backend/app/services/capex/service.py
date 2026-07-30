"""Capex analysis service.

Splits capital expenditure into maintenance and growth, then measures intensity
and the return the growth spend is generating.

Specification conventions:
  * Gross capex is taken as the absolute value of the cash-flow capex line.
  * Maintenance capex is proxied by D&A, capped at gross capex — a business
    cannot be spending less than nothing on upkeep.
  * Growth capex is the residual.
  * Asset turn on growth capex lags the spend by one year, since capital
    deployed this year produces revenue next year.
"""
from __future__ import annotations

from app.domain.calc import safe_div
from app.domain.financials.statements import (
    BalanceSheet, CashFlowStatement, IncomeStatement,
)
from app.schemas.common import MetricRow, MetricSection, Unit


class CapexService:
    def __init__(
        self,
        incomes: list[IncomeStatement],
        balances: list[BalanceSheet],
        cash_flows: list[CashFlowStatement],
    ) -> None:
        self.incomes = incomes
        self.balances = balances
        self.cash_flows = cash_flows
        self.n = len(incomes)

    # --------------------------------------------------------------- pieces
    def gross_capex(self, i: int) -> float:
        return abs(self.cash_flows[i].capex)

    def maintenance_capex(self, i: int) -> float:
        """Proxied by D&A, capped at gross capex."""
        return min(self.gross_capex(i), self.incomes[i].depreciation)

    def growth_capex(self, i: int) -> float:
        return self.gross_capex(i) - self.maintenance_capex(i)

    def net_capex(self, i: int) -> float:
        return self.gross_capex(i) - self.cash_flows[i].sale_fixed_assets

    # ------------------------------------------------------------- sections
    def historical_section(self) -> MetricSection:
        idx = range(self.n)
        return MetricSection(key="historical", title="A. Historical capex", rows=[
            MetricRow(key="gross_capex", label="Gross capex", values=[self.gross_capex(i) for i in idx], is_subtotal=True),
            MetricRow(key="depreciation", label="Depreciation & amortisation", values=[self.incomes[i].depreciation for i in idx]),
            MetricRow(key="maintenance_capex", label="Maintenance capex (proxy = D&A, capped)",
                      values=[self.maintenance_capex(i) for i in idx], indent=1),
            MetricRow(key="growth_capex", label="Growth capex", values=[self.growth_capex(i) for i in idx], indent=1),
            MetricRow(key="asset_sales", label="Proceeds from asset sales", values=[self.cash_flows[i].sale_fixed_assets for i in idx]),
            MetricRow(key="net_capex", label="Net capex", values=[self.net_capex(i) for i in idx], is_subtotal=True),
        ])

    def intensity_section(self) -> MetricSection:
        idx = range(self.n)

        def gross_block_growth(i: int) -> float | None:
            if i == 0:
                return None
            ratio = safe_div(self.balances[i].net_block_ppe, self.balances[i - 1].net_block_ppe)
            return None if ratio is None else ratio - 1

        def icor(i: int) -> float | None:
            """Incremental capital-output ratio: growth capex per ₹1 of new revenue."""
            if i == 0:
                return None
            return safe_div(self.growth_capex(i),
                            self.incomes[i].total_revenue - self.incomes[i - 1].total_revenue)

        def asset_turn_on_growth(i: int) -> float | None:
            """Revenue added this year per ₹1 of growth capex spent last year."""
            if i == 0:
                return None
            return safe_div(self.incomes[i].total_revenue - self.incomes[i - 1].total_revenue,
                            self.growth_capex(i - 1))

        return MetricSection(key="intensity", title="B. Capex intensity & efficiency", rows=[
            MetricRow(key="capex_to_revenue", label="Capex / revenue", unit=Unit.PERCENT,
                      values=[safe_div(self.gross_capex(i), self.incomes[i].total_revenue) for i in idx]),
            MetricRow(key="capex_to_da", label="Capex / D&A", unit=Unit.MULTIPLE,
                      values=[safe_div(self.gross_capex(i), self.incomes[i].depreciation) for i in idx],
                      note="Above 1.0x implies expansion; below implies under-investment."),
            MetricRow(key="capex_to_cfo", label="Capex / CFO", unit=Unit.PERCENT,
                      values=[safe_div(self.gross_capex(i), self.cash_flows[i].cfo) for i in idx]),
            MetricRow(key="growth_share", label="Growth capex as % of total", unit=Unit.PERCENT,
                      values=[safe_div(self.growth_capex(i), self.gross_capex(i)) for i in idx]),
            MetricRow(key="gross_block_growth", label="Net block growth", unit=Unit.PERCENT,
                      values=[gross_block_growth(i) for i in idx]),
            MetricRow(key="icor", label="Incremental capital-output ratio", unit=Unit.MULTIPLE,
                      values=[icor(i) for i in idx]),
            MetricRow(key="asset_turn_growth", label="Asset turn on prior-year growth capex", unit=Unit.MULTIPLE,
                      values=[asset_turn_on_growth(i) for i in idx]),
            MetricRow(key="cwip", label="Capital work-in-progress", values=[self.balances[i].cwip for i in idx]),
            MetricRow(key="cwip_to_net_block", label="CWIP as % of net block", unit=Unit.PERCENT,
                      values=[safe_div(self.balances[i].cwip, self.balances[i].net_block_ppe) for i in idx],
                      note="Elevated CWIP signals capital tied up in incomplete projects."),
        ])

    def all_sections(self) -> list[MetricSection]:
        return [self.historical_section(), self.intensity_section()]
