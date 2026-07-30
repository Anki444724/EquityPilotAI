"""Working-capital analysis service.

Implements the specification's three blocks: components, cycle days and
intensity/funding cost, plus the diagnostic flags.

Cycle-day conventions from the specification:
  * DIO and DPO are computed on **COGS**, not revenue.
  * DSO is computed on **revenue**.
  * All three use **average** opening/closing balances.
  * Operating cycle = DIO + DSO; cash conversion cycle = DIO + DSO − DPO.

Working capital here is the *operating* measure — it excludes cash and debt, so
it reflects the capital the business ties up to trade.
"""
from __future__ import annotations

from app.domain.calc import (
    DAYS_IN_YEAR, avg_balance, consecutive_run, delta, safe_div,
)
from app.domain.financials.statements import BalanceSheet, IncomeStatement
from app.schemas.common import Flag, MetricRow, MetricSection, Unit


class WorkingCapitalService:
    def __init__(
        self,
        incomes: list[IncomeStatement],
        balances: list[BalanceSheet],
        cost_of_debt: float | None = None,
    ) -> None:
        self.incomes = incomes
        self.balances = balances
        self.cost_of_debt = cost_of_debt
        self.n = len(incomes)

    # --------------------------------------------------------------- helpers
    def _avg(self, i: int, attr: str) -> float | None:
        """Average opening/closing balance, via the shared primitive."""
        return avg_balance(
            getattr(self.balances[i], attr),
            getattr(self.balances[i - 1], attr) if i > 0 else None,
        )

    def gross_wc_assets(self, i: int) -> float:
        b = self.balances[i]
        return b.inventories + b.trade_receivables + b.other_current_assets

    def operating_current_liabilities(self, i: int) -> float:
        b = self.balances[i]
        return b.trade_payables + b.other_current_liabilities + b.short_term_provisions

    def net_working_capital(self, i: int) -> float:
        """Operating NWC — excludes cash and all borrowings."""
        return self.gross_wc_assets(i) - self.operating_current_liabilities(i)

    # ----------------------------------------------------------- cycle days
    def dio(self, i: int) -> float | None:
        """Days inventory outstanding, on COGS."""
        r = safe_div(self._avg(i, "inventories"), self.incomes[i].total_cogs)
        return None if r is None else r * DAYS_IN_YEAR

    def dso(self, i: int) -> float | None:
        """Days sales outstanding, on revenue."""
        r = safe_div(self._avg(i, "trade_receivables"), self.incomes[i].total_revenue)
        return None if r is None else r * DAYS_IN_YEAR

    def dpo(self, i: int) -> float | None:
        """Days payables outstanding, on COGS."""
        r = safe_div(self._avg(i, "trade_payables"), self.incomes[i].total_cogs)
        return None if r is None else r * DAYS_IN_YEAR

    def operating_cycle(self, i: int) -> float | None:
        dio, dso = self.dio(i), self.dso(i)
        return None if dio is None or dso is None else dio + dso
    
    def ccc(self, i: int) -> float | None:
        oc, dpo = self.operating_cycle(i), self.dpo(i)
        return None if oc is None or dpo is None else oc - dpo

    # ------------------------------------------------------------- sections
    def components_section(self) -> MetricSection:
        idx = range(self.n)
        nwc = [self.net_working_capital(i) for i in idx]
        return MetricSection(key="components", title="A. Working-capital components", rows=[
            MetricRow(key="inventories", label="Inventories", values=[self.balances[i].inventories for i in idx], indent=1),
            MetricRow(key="receivables", label="Trade receivables", values=[self.balances[i].trade_receivables for i in idx], indent=1),
            MetricRow(key="other_ca", label="Other current assets", values=[self.balances[i].other_current_assets for i in idx], indent=1),
            MetricRow(key="gross_wc", label="Gross working-capital assets", values=[self.gross_wc_assets(i) for i in idx], is_subtotal=True),
            MetricRow(key="payables", label="Trade payables", values=[self.balances[i].trade_payables for i in idx], indent=1),
            MetricRow(key="other_cl", label="Other current liabilities & provisions",
                      values=[self.balances[i].other_current_liabilities + self.balances[i].short_term_provisions for i in idx], indent=1),
            MetricRow(key="total_ocl", label="Total operating current liabilities",
                      values=[self.operating_current_liabilities(i) for i in idx], is_subtotal=True),
            MetricRow(key="nwc", label="Net working capital (ex-cash, ex-debt)", values=nwc, is_subtotal=True),
            MetricRow(key="nwc_change", label="Change in NWC (cash released/(absorbed))",
                      values=[None] + [-(nwc[i] - nwc[i - 1]) for i in range(1, self.n)]),
        ])

    def cycle_section(self) -> MetricSection:
        idx = range(self.n)
        ccc = [self.ccc(i) for i in idx]
        return MetricSection(key="cycle", title="B. Cycle days", rows=[
            MetricRow(key="dio", label="Inventory days (DIO) — on COGS", unit=Unit.DAYS, values=[self.dio(i) for i in idx]),
            MetricRow(key="dso", label="Receivable days (DSO) — on revenue", unit=Unit.DAYS, values=[self.dso(i) for i in idx]),
            MetricRow(key="dpo", label="Payable days (DPO) — on COGS", unit=Unit.DAYS, values=[self.dpo(i) for i in idx]),
            MetricRow(key="operating_cycle", label="Operating cycle (DIO + DSO)", unit=Unit.DAYS,
                      values=[self.operating_cycle(i) for i in idx], is_subtotal=True),
            MetricRow(key="ccc", label="Cash conversion cycle (DIO + DSO − DPO)", unit=Unit.DAYS,
                      values=ccc, is_subtotal=True),
            MetricRow(key="ccc_change", label="Change in CCC", unit=Unit.DAYS,
                      values=[None] + [delta(ccc[i], ccc[i - 1]) for i in range(1, self.n)]),
        ])

    def intensity_section(self) -> MetricSection:
        idx = range(self.n)
        nwc = [self.net_working_capital(i) for i in idx]

        def funding_cost(i: int) -> float | None:
            return None if self.cost_of_debt is None else nwc[i] * self.cost_of_debt

        def incremental(i: int) -> float | None:
            if i == 0:
                return None
            return safe_div(nwc[i] - nwc[i - 1],
                            self.incomes[i].total_revenue - self.incomes[i - 1].total_revenue)

        return MetricSection(key="intensity", title="C. Intensity & funding cost", rows=[
            MetricRow(key="nwc_to_revenue", label="NWC as % of revenue", unit=Unit.PERCENT,
                      values=[safe_div(nwc[i], self.incomes[i].total_revenue) for i in idx]),
            MetricRow(key="nwc_to_capital", label="NWC as % of capital employed", unit=Unit.PERCENT,
                      values=[safe_div(nwc[i], self.balances[i].capital_employed) for i in idx]),
            MetricRow(key="incremental_nwc", label="Incremental NWC / incremental revenue", unit=Unit.MULTIPLE,
                      values=[incremental(i) for i in idx]),
            MetricRow(key="funding_cost", label="Implied annual funding cost of NWC", unit=Unit.CRORE,
                      values=[funding_cost(i) for i in idx],
                      note="Requires a pre-tax cost-of-debt assumption."),
            MetricRow(key="funding_cost_ebit", label="Funding cost as % of EBIT", unit=Unit.PERCENT,
                      values=[safe_div(funding_cost(i), self.incomes[i].ebit) for i in idx]),
            MetricRow(key="revenue_per_nwc", label="Revenue supported per ₹1 of NWC", unit=Unit.MULTIPLE,
                      values=[safe_div(self.incomes[i].total_revenue, nwc[i]) for i in idx]),
        ])

    # ----------------------------------------------------------- diagnostics
    def flags(self) -> list[Flag]:
        out: list[Flag] = []
        if self.n < 2:
            return out

        dso = [self.dso(i) for i in range(self.n)]
        dio = [self.dio(i) for i in range(self.n)]
        ccc = [self.ccc(i) for i in range(self.n)]

        # Receivable days rising faster than revenue
        rev_growth = safe_div(self.incomes[-1].total_revenue, self.incomes[-2].total_revenue)
        dso_growth = safe_div(dso[-1], dso[-2]) if dso[-1] and dso[-2] else None
        rising = bool(dso_growth and rev_growth and dso_growth > rev_growth)
        out.append(Flag(
            key="dso_outpacing_revenue",
            label="Receivable days rising faster than revenue",
            triggered=rising, severity="warn" if rising else "info",
            detail="Collections are lagging growth — check revenue quality." if rising else None,
        ))

        # Inventory days at a multi-year high
        window = [d for d in dio[-5:] if d is not None]
        at_high = bool(window and dio[-1] is not None and dio[-1] >= max(window))
        out.append(Flag(
            key="inventory_days_high",
            label="Inventory days at a 5-year high",
            triggered=at_high, severity="warn" if at_high else "info",
        ))

        # CCC deteriorating for 3 consecutive years
        worsening = [
            ccc[i] is not None and ccc[i - 1] is not None and ccc[i] > ccc[i - 1]
            for i in range(1, self.n)
        ]
        run = consecutive_run(worsening)
        out.append(Flag(
            key="ccc_deteriorating",
            label="Cash conversion cycle deteriorated 3 consecutive years",
            triggered=run >= 3, severity="alert" if run >= 3 else "info",
            detail=f"{run} consecutive years of deterioration" if run else None,
        ))
        return out

    def all_sections(self) -> list[MetricSection]:
        return [self.components_section(), self.cycle_section(), self.intensity_section()]
