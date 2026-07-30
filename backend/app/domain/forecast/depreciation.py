"""Depreciation schedule.

Depreciation is derived from the asset base, not assumed as a percentage of
revenue. Charging a rate against the opening net block keeps three things
consistent that a revenue-linked assumption would let drift apart: the capex
spend, the balance-sheet net block, and the D&A in the income statement.

The schedule itself is produced by :class:`~.capex.CapexForecast`, which owns
the roll-forward. This module exposes the analytical view of it — there is one
implementation, not two.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.domain.calc import safe_div
from .capex import CapexYear


@dataclass(frozen=True, slots=True)
class DepreciationYear:
    period: int
    fiscal_year: int
    opening_net_block: float
    depreciation: float
    capex: float
    closing_net_block: float
    effective_rate: float | None
    #: Capex / depreciation. Above 1.0x the asset base is expanding.
    reinvestment_ratio: float | None


def build_schedule(capex_rows: list[CapexYear]) -> list[DepreciationYear]:
    """Analytical view of the capex roll-forward."""
    return [
        DepreciationYear(
            period=c.period,
            fiscal_year=c.fiscal_year,
            opening_net_block=c.opening_net_block,
            depreciation=c.depreciation,
            capex=c.capex,
            closing_net_block=c.closing_net_block,
            effective_rate=safe_div(c.depreciation, c.opening_net_block),
            reinvestment_ratio=safe_div(c.capex, c.depreciation),
        )
        for c in capex_rows
    ]
