"""Revenue projection.

Supports four build methods, selected per forecast:

* ``CAGR``               — a single growth rate, optionally fading toward a
                           long-run rate across the horizon.
* ``VOLUME_PRICE``       — (1+volume) x (1+price) - 1, the correct compounding
                           of the two effects rather than their sum.
* ``SEGMENT``            — bottom-up per business line.
* ``ORGANIC_ACQUISITION``— organic growth plus a separately tracked inorganic
                           contribution, so the quality of growth is visible.

Growth fade matters. A flat CAGR held for ten years implies a company grows
faster than its economy forever; institutional models decay the near-term rate
toward a sustainable long-run rate. ``growth_fade`` controls how completely
that decay happens (0 = flat, 1 = full linear convergence).
"""
from __future__ import annotations

from dataclasses import dataclass

from app.domain.calc import cagr
from .assumptions import ForecastAssumptions, RevenueMethod


@dataclass(frozen=True, slots=True)
class RevenueYear:
    """One projected period of the top line."""

    period: int
    fiscal_year: int
    revenue: float
    growth: float | None
    #: Populated only when the method distinguishes the components.
    volume_growth: float | None = None
    price_growth: float | None = None
    organic_growth: float | None = None
    acquisition_growth: float | None = None
    organic_revenue: float | None = None
    acquired_revenue: float | None = None
    segment_revenue: tuple[tuple[str, float], ...] = ()


def faded_growth(a: ForecastAssumptions, period: int, start: float) -> float:
    """Growth for ``period``, linearly decayed toward the terminal rate.

    With ``growth_fade`` = 0 this returns ``start`` unchanged, so the simple
    CAGR case costs nothing.
    """
    fade = a.growth_fade.at(period)
    if fade <= 0 or a.years <= 1:
        return start
    terminal = a.terminal_revenue_growth.at(period)
    # progress runs 0 -> 1 across the horizon
    progress = (period - 1) / (a.years - 1)
    return start + (terminal - start) * fade * progress


class RevenueForecast:
    """Projects revenue for a horizon from a base-year figure."""

    def __init__(
        self,
        base_revenue: float,
        base_fiscal_year: int,
        assumptions: ForecastAssumptions,
    ) -> None:
        self.base_revenue = base_revenue
        self.base_fiscal_year = base_fiscal_year
        self.a = assumptions

    # ------------------------------------------------------------ methods
    def _growth_for(self, period: int) -> tuple[float, dict[str, float | None]]:
        """Growth rate for a period plus its component decomposition."""
        a = self.a
        detail: dict[str, float | None] = {}

        if a.revenue_method is RevenueMethod.VOLUME_PRICE:
            vol = faded_growth(a, period, a.volume_growth.at(period))
            price = a.price_growth.at(period)
            # multiplicative, not additive: 6% volume and 4% price is 10.24%
            growth = (1 + vol) * (1 + price) - 1
            detail = {"volume_growth": vol, "price_growth": price}

        elif a.revenue_method is RevenueMethod.ORGANIC_ACQUISITION:
            organic = faded_growth(a, period, a.organic_growth.at(period))
            acquired = a.acquisition_growth.at(period)
            growth = organic + acquired
            detail = {"organic_growth": organic, "acquisition_growth": acquired}

        else:  # CAGR — also the fallback when a segment build is unavailable
            growth = faded_growth(a, period, a.revenue_growth.at(period))

        return growth, detail

    def _segment_build(self) -> list[RevenueYear]:
        """Bottom-up projection, summing independently grown segments."""
        a = self.a
        levels = {s.name: s.base_revenue for s in a.segments}
        prior_total = sum(levels.values())
        out: list[RevenueYear] = []

        for period in a.periods():
            for seg in a.segments:
                levels[seg.name] *= 1 + faded_growth(a, period, seg.growth.at(period))
            total = sum(levels.values())
            out.append(
                RevenueYear(
                    period=period,
                    fiscal_year=self.base_fiscal_year + period,
                    revenue=total,
                    growth=(total / prior_total - 1) if prior_total else None,
                    segment_revenue=tuple(sorted(levels.items())),
                )
            )
            prior_total = total
        return out

    # -------------------------------------------------------------- build
    def project(self) -> list[RevenueYear]:
        a = self.a
        if a.revenue_method is RevenueMethod.SEGMENT and a.segments:
            return self._segment_build()

        out: list[RevenueYear] = []
        level = self.base_revenue
        organic_level = self.base_revenue
        split = a.revenue_method is RevenueMethod.ORGANIC_ACQUISITION

        for period in a.periods():
            growth, detail = self._growth_for(period)
            level = level * (1 + growth)
            if split:
                organic_level *= 1 + (detail.get("organic_growth") or 0.0)

            out.append(
                RevenueYear(
                    period=period,
                    fiscal_year=self.base_fiscal_year + period,
                    revenue=level,
                    growth=growth,
                    volume_growth=detail.get("volume_growth"),
                    price_growth=detail.get("price_growth"),
                    organic_growth=detail.get("organic_growth"),
                    acquisition_growth=detail.get("acquisition_growth"),
                    organic_revenue=organic_level if split else None,
                    acquired_revenue=(level - organic_level) if split else None,
                )
            )
        return out

    # --------------------------------------------------------- diagnostics
    def implied_cagr(self, rows: list[RevenueYear] | None = None) -> float | None:
        rows = rows or self.project()
        if not rows:
            return None
        return cagr(self.base_revenue, rows[-1].revenue, len(rows))
