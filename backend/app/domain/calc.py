"""Shared calculation primitives.

Every arithmetic convention used anywhere in the platform is defined here
exactly once. Services import these rather than re-implementing them, which is
how the "each calculation must exist only once" rule is enforced mechanically
rather than by discipline.

Conventions are taken from the workbook specification:

* Undefined ratios return ``None`` ("n.m."), never 0 — a missing ratio and a
  genuine zero are different facts and must render differently.
* Balance-sheet-derived ratios use **average** opening/closing balances where
  the specification does (turnover and cycle-day metrics).
* Day-count uses a 365-day year.
"""
from __future__ import annotations

from typing import Iterable, Sequence

#: Day-count convention for all cycle metrics.
DAYS_IN_YEAR = 365


def safe_div(numerator: float | None, denominator: float | None) -> float | None:
    """Division that yields ``None`` when undefined.

    Replaces the workbook's ``IFERROR(a/b, "n.m.")`` guard.
    """
    if numerator is None or denominator is None:
        return None
    if denominator == 0:
        return None
    return numerator / denominator


def average(*values: float | None) -> float | None:
    """Mean of the supplied values, ignoring ``None``."""
    present = [v for v in values if v is not None]
    if not present:
        return None
    return sum(present) / len(present)


def avg_balance(closing: float | None, opening: float | None) -> float | None:
    """Average of opening and closing balances.

    The specification computes turnover and cycle metrics on average balances.
    When no opening balance exists (the first year on file) the closing balance
    is used, which is the standard treatment.
    """
    if closing is None and opening is None:
        return None
    if opening is None:
        return closing
    if closing is None:
        return opening
    return (closing + opening) / 2


def days(numerator: float | None, denominator: float | None) -> float | None:
    """Convert a balance/flow ratio into days."""
    ratio = safe_div(numerator, denominator)
    return None if ratio is None else ratio * DAYS_IN_YEAR


def growth(current: float | None, prior: float | None) -> float | None:
    """Period-on-period growth rate.

    Undefined when the prior period is zero or negative — a growth rate off a
    negative base is not meaningful and must not be rendered as a number.
    """
    if current is None or prior is None or prior <= 0:
        return None
    return current / prior - 1


def cagr(first: float | None, last: float | None, periods: int) -> float | None:
    """Compound annual growth rate over ``periods`` intervals.

    Undefined for non-positive endpoints, which cannot support a compound rate.
    """
    if first is None or last is None or periods <= 0:
        return None
    if first <= 0 or last <= 0:
        return None
    return (last / first) ** (1 / periods) - 1


def delta(current: float | None, prior: float | None) -> float | None:
    """Absolute change between two periods."""
    if current is None or prior is None:
        return None
    return current - prior


def basis_points(current: float | None, prior: float | None) -> float | None:
    """Change between two rates, expressed in basis points."""
    d = delta(current, prior)
    return None if d is None else d * 10_000


def pct_of(part: float | None, whole: float | None) -> float | None:
    """Share of a total, as a fraction."""
    return safe_div(part, whole)


def total(*values: float | None) -> float:
    """Sum treating ``None`` as zero.

    Used only for subtotals where absent components are genuinely nil.
    """
    return sum(v for v in values if v is not None)


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def series_cagr(values: Sequence[float | None]) -> float | None:
    """CAGR across an ordered series, using its first and last present points."""
    present = [(i, v) for i, v in enumerate(values) if v is not None]
    if len(present) < 2:
        return None
    (i0, first), (i1, last) = present[0], present[-1]
    return cagr(first, last, i1 - i0)


def consecutive_run(flags: Iterable[bool]) -> int:
    """Length of the trailing run of ``True`` values.

    Used by diagnostics such as "deteriorated for N consecutive years".
    """
    run = 0
    for flag in flags:
        run = run + 1 if flag else 0
    return run
