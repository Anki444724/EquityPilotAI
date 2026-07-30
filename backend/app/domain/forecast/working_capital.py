"""Working-capital projection.

Driven by cycle days rather than a flat percentage of revenue, because days are
the unit an analyst actually negotiates over: "we think receivable days go from
45 to 40" is a defensible claim; "working capital falls to 14.2% of sales" is a
consequence of it.

Inventory and payables are computed on COGS, receivables on revenue — the same
convention Module 2 uses for historical cycle days, so history and forecast are
directly comparable.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.domain.calc import DAYS_IN_YEAR, safe_div
from .assumptions import ForecastAssumptions
from .margins import MarginYear


@dataclass(frozen=True, slots=True)
class WorkingCapitalYear:
    period: int
    fiscal_year: int
    inventories: float
    receivables: float
    other_current_assets: float
    payables: float
    other_current_liabilities: float
    net_working_capital: float
    #: Positive = cash released, negative = cash absorbed.
    change_in_nwc: float
    nwc_pct_revenue: float | None
    inventory_days: float
    receivable_days: float
    payable_days: float
    cash_conversion_cycle: float


class WorkingCapitalForecast:
    """Projects the operating working-capital position."""

    def __init__(self, opening_nwc: float, assumptions: ForecastAssumptions) -> None:
        self.opening_nwc = opening_nwc
        self.a = assumptions

    def project(self, margin_rows: list[MarginYear]) -> list[WorkingCapitalYear]:
        a = self.a
        out: list[WorkingCapitalYear] = []
        prior_nwc = self.opening_nwc

        for row in margin_rows:
            # COGS is inferred from the EBITDA margin: the non-margin share of
            # revenue is the operating cost base that inventory and payables
            # are carried against.
            cogs = row.revenue * (1 - row.ebitda_margin)

            dio = a.inventory_days.at(row.period)
            dso = a.receivable_days.at(row.period)
            dpo = a.payable_days.at(row.period)

            inventories = cogs * dio / DAYS_IN_YEAR
            receivables = row.revenue * dso / DAYS_IN_YEAR
            payables = cogs * dpo / DAYS_IN_YEAR
            other_ca = row.revenue * a.other_ca_pct_revenue.at(row.period)
            other_cl = row.revenue * a.other_cl_pct_revenue.at(row.period)

            nwc = inventories + receivables + other_ca - payables - other_cl

            out.append(
                WorkingCapitalYear(
                    period=row.period,
                    fiscal_year=row.fiscal_year,
                    inventories=inventories,
                    receivables=receivables,
                    other_current_assets=other_ca,
                    payables=payables,
                    other_current_liabilities=other_cl,
                    net_working_capital=nwc,
                    change_in_nwc=-(nwc - prior_nwc),
                    nwc_pct_revenue=safe_div(nwc, row.revenue),
                    inventory_days=dio,
                    receivable_days=dso,
                    payable_days=dpo,
                    cash_conversion_cycle=dio + dso - dpo,
                )
            )
            prior_nwc = nwc
        return out
