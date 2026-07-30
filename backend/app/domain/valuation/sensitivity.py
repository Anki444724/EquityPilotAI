"""Sensitivity analysis and Monte Carlo simulation.

Both rest on the same property: :func:`~app.domain.valuation.dcf.run_dcf` is a
pure function. A sensitivity grid is a deterministic sweep over two axes; a
Monte Carlo run is a random sweep over many. Neither needs the DCF engine to
change.

Sensitivity variables are declared as *revaluation functions* — each knows how
to rebuild the valuation inputs for a given parameter value. Adding a new axis
means adding one entry, not editing the grid code.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from enum import StrEnum
from statistics import mean, median, pstdev
from typing import Callable, Sequence

from app.domain.calc import safe_div


class SensitivityAxis(StrEnum):
    WACC = "wacc"
    TERMINAL_GROWTH = "terminal_growth"
    REVENUE_CAGR = "revenue_cagr"
    EBIT_MARGIN = "ebit_margin"
    EXIT_MULTIPLE = "exit_multiple"


AXIS_META: dict[str, tuple[str, str, float]] = {
    # key -> (label, unit, default step)
    SensitivityAxis.WACC: ("WACC", "%", 0.005),
    SensitivityAxis.TERMINAL_GROWTH: ("Terminal growth", "%", 0.005),
    SensitivityAxis.REVENUE_CAGR: ("Revenue CAGR", "%", 0.01),
    SensitivityAxis.EBIT_MARGIN: ("EBIT margin", "%", 0.01),
    SensitivityAxis.EXIT_MULTIPLE: ("Exit EV/EBITDA", "x", 1.0),
}

#: Valuation function signature: (row_value, col_value) -> value per share.
Revaluer = Callable[[float, float], float | None]


@dataclass(frozen=True, slots=True)
class SensitivityGrid:
    """A two-dimensional sensitivity table."""

    row_key: str
    row_label: str
    row_unit: str
    row_values: list[float]

    col_key: str
    col_label: str
    col_unit: str
    col_values: list[float]

    #: cells[row][col] — value per share, or None where undefined.
    cells: list[list[float | None]]
    base_row: float
    base_col: float
    base_value: float | None
    current_price: float | None = None

    def upside_cells(self) -> list[list[float | None]]:
        """The same grid expressed as upside against the market price."""
        if not self.current_price:
            return [[None for _ in row] for row in self.cells]
        return [
            [None if v is None else v / self.current_price - 1 for v in row]
            for row in self.cells
        ]

    @property
    def minimum(self) -> float | None:
        vals = [v for row in self.cells for v in row if v is not None]
        return min(vals) if vals else None

    @property
    def maximum(self) -> float | None:
        vals = [v for row in self.cells for v in row if v is not None]
        return max(vals) if vals else None


def build_axis(base: float, steps: int = 2, step_size: float | None = None,
               key: str = SensitivityAxis.WACC) -> list[float]:
    """Symmetric axis around a base value: base ± steps × step_size."""
    if step_size is None:
        step_size = AXIS_META.get(key, ("", "", 0.005))[2]
    return [round(base + (i - steps) * step_size, 10) for i in range(steps * 2 + 1)]


def build_grid(
    *,
    row_key: str,
    col_key: str,
    row_base: float,
    col_base: float,
    revalue: Revaluer,
    steps: int = 2,
    row_step: float | None = None,
    col_step: float | None = None,
    current_price: float | None = None,
) -> SensitivityGrid:
    """Sweep two parameters and revalue at each intersection."""
    row_values = build_axis(row_base, steps, row_step, row_key)
    col_values = build_axis(col_base, steps, col_step, col_key)

    cells = [[revalue(r, c) for c in col_values] for r in row_values]

    row_label, row_unit, _ = AXIS_META.get(row_key, (row_key, "", 0.0))
    col_label, col_unit, _ = AXIS_META.get(col_key, (col_key, "", 0.0))

    return SensitivityGrid(
        row_key=row_key, row_label=row_label, row_unit=row_unit, row_values=row_values,
        col_key=col_key, col_label=col_label, col_unit=col_unit, col_values=col_values,
        cells=cells,
        base_row=row_base, base_col=col_base,
        base_value=revalue(row_base, col_base),
        current_price=current_price,
    )


# ---------------------------------------------------------------------------
# Monte Carlo
# ---------------------------------------------------------------------------

class Distribution(StrEnum):
    NORMAL = "normal"
    TRIANGULAR = "triangular"
    UNIFORM = "uniform"


@dataclass(frozen=True, slots=True)
class StochasticVariable:
    """One uncertain input in a simulation."""

    key: str
    base: float
    distribution: Distribution = Distribution.TRIANGULAR
    #: Normal: standard deviation. Triangular/uniform: half-width of the range.
    spread: float = 0.01
    minimum: float | None = None
    maximum: float | None = None

    def draw(self, rng: random.Random) -> float:
        if self.distribution is Distribution.NORMAL:
            value = rng.gauss(self.base, self.spread)
        elif self.distribution is Distribution.UNIFORM:
            value = rng.uniform(self.base - self.spread, self.base + self.spread)
        else:
            value = rng.triangular(self.base - self.spread, self.base + self.spread, self.base)
        if self.minimum is not None:
            value = max(self.minimum, value)
        if self.maximum is not None:
            value = min(self.maximum, value)
        return value


@dataclass(frozen=True, slots=True)
class SimulationResult:
    trials: int
    values: list[float] = field(repr=False, default_factory=list)
    mean_value: float | None = None
    median_value: float | None = None
    std_dev: float | None = None
    percentiles: dict[int, float] = field(default_factory=dict)
    probability_above_price: float | None = None
    current_price: float | None = None
    #: Bucketed distribution for charting: (lower, upper, count).
    histogram: list[tuple[float, float, int]] = field(default_factory=list)
    failed_trials: int = 0


def run_simulation(
    variables: Sequence[StochasticVariable],
    revalue: Callable[[dict[str, float]], float | None],
    *,
    trials: int = 1000,
    seed: int | None = 42,
    current_price: float | None = None,
    buckets: int = 20,
) -> SimulationResult:
    """Monte Carlo over an arbitrary set of uncertain inputs.

    ``revalue`` receives a dict of drawn values and returns a value per share.
    Because the DCF engine is pure, this needs no locking or state management.
    A fixed ``seed`` makes runs reproducible, which matters when a valuation
    has to be defended.
    """
    rng = random.Random(seed)
    values: list[float] = []
    failed = 0

    for _ in range(trials):
        draw = {v.key: v.draw(rng) for v in variables}
        try:
            result = revalue(draw)
        except (ZeroDivisionError, ValueError, OverflowError):
            result = None
        if result is None or result != result:  # NaN check
            failed += 1
        else:
            values.append(result)

    if not values:
        return SimulationResult(trials=trials, failed_trials=failed, current_price=current_price)

    ordered = sorted(values)

    def pct(p: int) -> float:
        idx = min(len(ordered) - 1, max(0, int(round(p / 100 * (len(ordered) - 1)))))
        return ordered[idx]

    lo, hi = ordered[0], ordered[-1]
    width = (hi - lo) / buckets if hi > lo else 1.0
    histogram: list[tuple[float, float, int]] = []
    for i in range(buckets):
        low = lo + i * width
        high = low + width
        count = sum(1 for v in ordered if (low <= v < high or (i == buckets - 1 and v == hi)))
        histogram.append((low, high, count))

    return SimulationResult(
        trials=trials,
        values=values,
        mean_value=mean(values),
        median_value=median(values),
        std_dev=pstdev(values) if len(values) > 1 else 0.0,
        percentiles={p: pct(p) for p in (5, 10, 25, 50, 75, 90, 95)},
        probability_above_price=(
            sum(1 for v in values if v > current_price) / len(values)
            if current_price else None
        ),
        current_price=current_price,
        histogram=histogram,
        failed_trials=failed,
    )
