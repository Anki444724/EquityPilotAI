"""Capital expenditure projection and the resulting asset base.

Capex is projected as a share of revenue, then split into maintenance and
growth. The net block is rolled forward — opening block, plus capex, less
depreciation — so the balance sheet and the depreciation schedule stay
consistent with the spend.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.domain.calc import safe_div
from .assumptions import ForecastAssumptions
from .revenue import RevenueYear


@dataclass(frozen=True, slots=True)
class CapexYear:
    period: int
    fiscal_year: int
    capex: float
    maintenance_capex: float
    growth_capex: float
    capex_pct_revenue: float
    opening_net_block: float
    depreciation: float
    closing_net_block: float


class CapexForecast:
    """Projects capex and rolls the net block forward."""

    def __init__(self, opening_net_block: float, assumptions: ForecastAssumptions) -> None:
        self.opening_net_block = opening_net_block
        self.a = assumptions

    def project(self, revenue_rows: list[RevenueYear]) -> list[CapexYear]:
        a = self.a
        out: list[CapexYear] = []
        block = self.opening_net_block

        for row in revenue_rows:
            pct = a.capex_pct_revenue.at(row.period)
            capex = row.revenue * pct
            maint_share = a.maintenance_capex_pct.at(row.period)
            maintenance = capex * maint_share

            # Depreciation is charged on the OPENING block, so a year's own
            # capex does not depreciate before it is commissioned.
            dep = block * a.depreciation_rate.at(row.period)
            closing = block + capex - dep

            out.append(
                CapexYear(
                    period=row.period,
                    fiscal_year=row.fiscal_year,
                    capex=capex,
                    maintenance_capex=maintenance,
                    growth_capex=capex - maintenance,
                    capex_pct_revenue=pct,
                    opening_net_block=block,
                    depreciation=dep,
                    closing_net_block=closing,
                )
            )
            block = closing
        return out

    def depreciation_series(self, revenue_rows: list[RevenueYear]) -> list[float]:
        return [y.depreciation for y in self.project(revenue_rows)]
