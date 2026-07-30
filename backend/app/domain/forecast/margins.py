"""Margin and operating-profit projection.

EBITDA is driven by a margin assumption rather than by forecasting each cost
line, which is how sell-side and buy-side models are actually built: the margin
is the judgement, and the cost stack follows from it.

``margin_expansion`` applies cumulatively — 50 bps of annual expansion means
the margin in year 3 is the base plus 150 bps, not plus 50.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.domain.calc import safe_div
from .assumptions import ForecastAssumptions
from .revenue import RevenueYear


@dataclass(frozen=True, slots=True)
class MarginYear:
    period: int
    fiscal_year: int
    revenue: float
    gross_profit: float | None
    gross_margin: float | None
    ebitda: float
    ebitda_margin: float
    depreciation: float
    ebit: float
    ebit_margin: float | None
    other_income: float


class MarginForecast:
    """Turns projected revenue into EBITDA and EBIT."""

    def __init__(self, assumptions: ForecastAssumptions) -> None:
        self.a = assumptions

    def ebitda_margin_at(self, period: int) -> float:
        """Base margin plus cumulative expansion to this period."""
        a = self.a
        base = a.ebitda_margin.at(period)
        # expansion accrues from the first forecast year onward
        cumulative = sum(a.margin_expansion.at(p) for p in range(1, period + 1))
        return base + cumulative

    def project(
        self,
        revenue_rows: list[RevenueYear],
        depreciation: list[float],
    ) -> list[MarginYear]:
        """Depreciation is supplied by the capex/depreciation schedule.

        EBIT is therefore consistent with the asset base rather than assumed
        independently — the two schedules cannot drift apart.
        """
        a = self.a
        out: list[MarginYear] = []

        for i, row in enumerate(revenue_rows):
            margin = self.ebitda_margin_at(row.period)
            ebitda = row.revenue * margin
            dep = depreciation[i] if i < len(depreciation) else 0.0
            ebit = ebitda - dep

            gm = a.gross_margin.at(row.period) if a.gross_margin else None
            gross_profit = row.revenue * gm if gm is not None else None

            out.append(
                MarginYear(
                    period=row.period,
                    fiscal_year=row.fiscal_year,
                    revenue=row.revenue,
                    gross_profit=gross_profit,
                    gross_margin=gm,
                    ebitda=ebitda,
                    ebitda_margin=margin,
                    depreciation=dep,
                    ebit=ebit,
                    ebit_margin=safe_div(ebit, row.revenue),
                    other_income=row.revenue * a.other_income_pct_revenue.at(row.period),
                )
            )
        return out
