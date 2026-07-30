"""Return measurement and performance attribution.

Two return measures, and the distinction between them is the single most
misreported number in retail portfolio software:

* **Time-weighted return (TWR)** neutralises the effect of deposits and
  withdrawals. It measures the *manager*. This is what you compare against an
  index, because an index has no cash flows.
* **Money-weighted return (MWR / IRR)** is the return the investor's actual
  rupees earned. It measures the *investor*, timing included.

A portfolio that doubled and then received a large deposit just before a fall
has a good TWR and a poor MWR. Both are true. Reporting only one — and most
tools report whichever flatters — is the error.

Attribution uses Brinson-Fachler, decomposing active return into allocation,
selection and interaction.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Sequence

from app.domain.calc import safe_div
from app.domain.portfolio.types import ReturnPoint

#: IRR solver bounds. Rates beyond these are not meaningful for a portfolio and
#: usually indicate a malformed cash-flow series.
_IRR_LOW = -0.9999
_IRR_HIGH = 100.0
_IRR_TOLERANCE = 1e-7
_IRR_MAX_ITERATIONS = 200
DAYS_PER_YEAR = 365.0


def time_weighted_return(points: Sequence[ReturnPoint]) -> float | None:
    """Modified Dietz chain-linked TWR.

    Each sub-period return is measured against the opening value *plus* the
    flow, so money that arrived during the period is not credited with the
    return it was not present for. Sub-period returns are then chain-linked
    geometrically.

    Returns ``None`` when there is no usable history rather than 0.0, because
    "flat" and "unknown" are different answers.
    """
    if len(points) < 2:
        return None
    growth = 1.0
    usable = 0
    for previous, current in zip(points, points[1:]):
        base = previous.value + current.net_flow
        if base <= 0:
            # The portfolio was empty or fully withdrawn; no return is defined
            # for this sub-period, so it is skipped rather than treated as zero.
            continue
        growth *= current.value / base
        usable += 1
    return (growth - 1.0) if usable else None


def annualise(total_return: float | None, days: int) -> float | None:
    """Convert a cumulative return over `days` into an annual rate.

    Periods under a year are **not** extrapolated. Annualising a 4% gain over
    three weeks into 96% is arithmetically valid and analytically worthless,
    and it is how short-lived funds advertise themselves.
    """
    if total_return is None or days <= 0:
        return None
    years = days / DAYS_PER_YEAR
    if years < 1.0:
        return None
    base = 1.0 + total_return
    if base <= 0:
        return -1.0
    return base ** (1.0 / years) - 1.0


@dataclass(frozen=True, slots=True)
class CashFlow:
    """A dated external flow. Positive is money in, negative is money out."""

    when: date
    amount: float


def xnpv(rate: float, flows: Sequence[CashFlow]) -> float:
    """Net present value of irregularly dated flows."""
    if not flows:
        return 0.0
    start = flows[0].when
    total = 0.0
    for flow in flows:
        years = (flow.when - start).days / DAYS_PER_YEAR
        total += flow.amount / ((1.0 + rate) ** years)
    return total


def xirr(flows: Sequence[CashFlow]) -> float | None:
    """Money-weighted return, solved by bisection.

    Bisection rather than Newton-Raphson: Newton is faster but diverges on the
    sign-alternating flow series a real portfolio produces, and a silently
    divergent IRR returns a plausible wrong number. Bisection is slower and
    always right when a root is bracketed.

    Returns ``None`` when no sign change exists — a series of only deposits has
    no rate of return, and inventing one would be fiction.
    """
    if len(flows) < 2:
        return None
    ordered = sorted(flows, key=lambda f: f.when)
    if not (any(f.amount > 0 for f in ordered) and any(f.amount < 0 for f in ordered)):
        return None

    low, high = _IRR_LOW, _IRR_HIGH
    npv_low, npv_high = xnpv(low, ordered), xnpv(high, ordered)
    if npv_low * npv_high > 0:
        return None  # no root bracketed

    for _ in range(_IRR_MAX_ITERATIONS):
        mid = (low + high) / 2.0
        npv_mid = xnpv(mid, ordered)
        if abs(npv_mid) < _IRR_TOLERANCE:
            return mid
        if npv_low * npv_mid < 0:
            high, npv_high = mid, npv_mid
        else:
            low, npv_low = mid, npv_mid
    return (low + high) / 2.0


def money_weighted_return(flows: Sequence[CashFlow]) -> float | None:
    """Annualised IRR of the investor's own cash flows."""
    return xirr(flows)


# ---------------------------------------------------------------------------
# Attribution
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class AttributionRow:
    """Brinson-Fachler attribution for one segment."""

    key: str
    label: str
    portfolio_weight: float
    benchmark_weight: float
    portfolio_return: float
    benchmark_return: float
    allocation: float = 0.0
    selection: float = 0.0
    interaction: float = 0.0

    @property
    def total(self) -> float:
        return self.allocation + self.selection + self.interaction

    @property
    def active_weight(self) -> float:
        return self.portfolio_weight - self.benchmark_weight


@dataclass(slots=True)
class Attribution:
    """Complete decomposition of active return."""

    rows: list[AttributionRow] = field(default_factory=list)
    portfolio_return: float = 0.0
    benchmark_return: float = 0.0

    @property
    def active_return(self) -> float:
        return self.portfolio_return - self.benchmark_return

    @property
    def total_allocation(self) -> float:
        return sum(r.allocation for r in self.rows)

    @property
    def total_selection(self) -> float:
        return sum(r.selection for r in self.rows)

    @property
    def total_interaction(self) -> float:
        return sum(r.interaction for r in self.rows)

    @property
    def residual(self) -> float:
        """Active return the decomposition did not explain.

        Should be ~0 for a single period. A material residual means the weights
        or returns supplied are inconsistent, and surfacing it is what makes
        the attribution auditable rather than merely plausible.
        """
        return self.active_return - (
            self.total_allocation + self.total_selection + self.total_interaction
        )


def brinson_attribution(
    segments: Sequence[tuple[str, str, float, float, float, float]],
) -> Attribution:
    """Brinson-Fachler attribution.

    Each segment is `(key, label, portfolio_weight, benchmark_weight,
    portfolio_return, benchmark_return)`.

    * **Allocation** = (wp − wb) × (rb − total_rb) — the benefit of over- or
      under-weighting a segment relative to how that segment did *versus the
      whole benchmark*. The Fachler refinement (subtracting the total benchmark
      return) is what stops an overweight in any rising segment from scoring
      positively even when it lagged the index.
    * **Selection** = wb × (rp − rb) — picking better names inside a segment.
    * **Interaction** = (wp − wb) × (rp − rb) — the cross term, reported
      separately rather than folded into selection, because merging them
      flatters a manager who was overweight *and* right.
    """
    total_benchmark_return = sum(wb * rb for _, _, _, wb, _, rb in segments)
    total_portfolio_return = sum(wp * rp for _, _, wp, _, rp, _ in segments)

    rows: list[AttributionRow] = []
    for key, label, wp, wb, rp, rb in segments:
        row = AttributionRow(
            key=key, label=label,
            portfolio_weight=wp, benchmark_weight=wb,
            portfolio_return=rp, benchmark_return=rb,
        )
        row.allocation = (wp - wb) * (rb - total_benchmark_return)
        row.selection = wb * (rp - rb)
        row.interaction = (wp - wb) * (rp - rb)
        rows.append(row)

    rows.sort(key=lambda r: -abs(r.total))
    return Attribution(
        rows=rows,
        portfolio_return=total_portfolio_return,
        benchmark_return=total_benchmark_return,
    )


@dataclass(slots=True)
class ContributionRow:
    """A position's contribution to portfolio return."""

    ticker: str
    name: str
    weight: float
    position_return: float
    contribution: float

    @property
    def share_of_total(self) -> float | None:
        return None


def contribution_analysis(
    holdings: Sequence[tuple[str, str, float, float]],
) -> list[ContributionRow]:
    """Weight × return for each holding, ranked by impact.

    Each holding is `(ticker, name, weight, return)`. This answers "what
    actually moved the portfolio", which a table of individual returns does
    not: a 60% gain on a 1% position is a rounding error, and a 5% gain on a
    30% position is the quarter.
    """
    rows = [
        ContributionRow(
            ticker=ticker, name=name, weight=weight,
            position_return=ret, contribution=weight * ret,
        )
        for ticker, name, weight, ret in holdings
    ]
    rows.sort(key=lambda r: -r.contribution)
    return rows


def rolling_returns(
    points: Sequence[ReturnPoint], window: int
) -> list[tuple[date, float]]:
    """Rolling total return over a fixed observation window.

    Rolling windows are how a strategy's *consistency* becomes visible. A
    single trailing number hides whether a five-year record came from five
    good years or one extraordinary one.
    """
    if window <= 0 or len(points) <= window:
        return []
    out: list[tuple[date, float]] = []
    for index in range(window, len(points)):
        start = points[index - window]
        end = points[index]
        if start.value <= 0:
            continue
        out.append((end.as_of, end.value / start.value - 1.0))
    return out


def drawdown_series(points: Sequence[ReturnPoint]) -> list[tuple[date, float]]:
    """Underwater curve — the decline from the running peak at each date."""
    out: list[tuple[date, float]] = []
    peak = None
    for point in points:
        if peak is None or point.value > peak:
            peak = point.value
        if peak and peak > 0:
            out.append((point.as_of, point.value / peak - 1.0))
    return out
