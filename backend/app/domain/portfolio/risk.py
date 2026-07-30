"""Portfolio risk metrics.

Every function here takes a return series and returns a scalar or ``None``.
``None`` means *the input could not support the statistic* — too few
observations, zero variance, no downside — and never zero. A Sortino ratio of
zero and an undefined Sortino ratio are opposite statements, and conflating
them is how a portfolio with no losing days ends up looking like one that never
made money.

Two conventions are fixed throughout and stated once here:

* **Returns are simple periodic fractions**, not log returns and not
  percentages. 1% is 0.01.
* **Annualisation uses 252 trading days.** Volatility scales with the square
  root of time; mean return scales linearly.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

#: Indian equity markets trade roughly 250 days a year; 252 is the standard
#: convention and the one every published Sharpe ratio assumes.
TRADING_DAYS = 252
#: Minimum observations before a dispersion statistic is meaningful. Below
#: this the estimate is noise, so the functions decline rather than mislead.
MIN_OBSERVATIONS = 3
#: Default risk-free rate, annual. Indian 10-year G-Sec, overridable.
DEFAULT_RISK_FREE = 0.07


def to_returns(values: Sequence[float]) -> list[float]:
    """Convert a value series into simple periodic returns.

    Periods where the prior value is non-positive are skipped: a return
    computed against a zero base is infinite, and against a negative base is
    meaningless.
    """
    out: list[float] = []
    for prior, current in zip(values, values[1:]):
        if prior is None or current is None or prior <= 0:
            continue
        out.append(current / prior - 1.0)
    return out


def mean(values: Sequence[float]) -> float | None:
    return (sum(values) / len(values)) if values else None


def stdev(values: Sequence[float], *, sample: bool = True) -> float | None:
    """Standard deviation. Sample (n−1) by default.

    Sample, not population: a return series is a sample of the process that
    generated it, and the population form understates dispersion on the short
    histories a portfolio typically has.
    """
    n = len(values)
    if n < MIN_OBSERVATIONS:
        return None
    average = sum(values) / n
    divisor = (n - 1) if sample else n
    if divisor <= 0:
        return None
    variance = sum((v - average) ** 2 for v in values) / divisor
    return math.sqrt(variance)


def annualised_return(returns: Sequence[float], periods: int = TRADING_DAYS) -> float | None:
    """Geometric annualised return — the rate actually compounded.

    Geometric rather than arithmetic: a series of +50% then −50% has an
    arithmetic mean of zero and has in fact lost a quarter of its capital.
    """
    if not returns:
        return None
    growth = 1.0
    for r in returns:
        growth *= (1.0 + r)
    if growth <= 0:
        # Total loss. Annualising a non-positive multiple has no real root.
        return -1.0
    years = len(returns) / periods
    if years <= 0:
        return None
    return growth ** (1.0 / years) - 1.0


def annualised_volatility(
    returns: Sequence[float], periods: int = TRADING_DAYS
) -> float | None:
    sigma = stdev(returns)
    return None if sigma is None else sigma * math.sqrt(periods)


def downside_deviation(
    returns: Sequence[float], target: float = 0.0, periods: int = TRADING_DAYS
) -> float | None:
    """Annualised deviation of returns below `target`.

    The divisor is the *full* observation count, not the count of downside
    periods. That is the Sortino convention and it matters: dividing by the
    downside count alone would make a portfolio with one bad day in a thousand
    look as risky as one with a hundred.
    """
    if len(returns) < MIN_OBSERVATIONS:
        return None
    shortfalls = [min(0.0, r - target) for r in returns]
    if not any(shortfalls):
        return None  # no downside observed — the ratio is undefined, not infinite
    variance = sum(s ** 2 for s in shortfalls) / len(returns)
    return math.sqrt(variance) * math.sqrt(periods)


def sharpe_ratio(
    returns: Sequence[float],
    risk_free: float = DEFAULT_RISK_FREE,
    periods: int = TRADING_DAYS,
) -> float | None:
    """Excess return per unit of total volatility."""
    volatility = annualised_volatility(returns, periods)
    if volatility is None or volatility == 0:
        return None
    annual = annualised_return(returns, periods)
    return None if annual is None else (annual - risk_free) / volatility


def sortino_ratio(
    returns: Sequence[float],
    risk_free: float = DEFAULT_RISK_FREE,
    periods: int = TRADING_DAYS,
) -> float | None:
    """Excess return per unit of downside volatility.

    The target is the *periodic* risk-free rate, so the numerator and the
    denominator are measured against the same hurdle. Using zero as the target
    while subtracting an annual risk-free rate in the numerator is a common and
    silent inconsistency.
    """
    periodic_rf = risk_free / periods
    downside = downside_deviation(returns, target=periodic_rf, periods=periods)
    if downside is None or downside == 0:
        return None
    annual = annualised_return(returns, periods)
    return None if annual is None else (annual - risk_free) / downside


@dataclass(frozen=True, slots=True)
class Drawdown:
    """A peak-to-trough decline."""

    depth: float              # negative fraction, e.g. -0.32
    peak_index: int
    trough_index: int
    recovery_index: int | None = None

    @property
    def length(self) -> int:
        return self.trough_index - self.peak_index

    @property
    def recovered(self) -> bool:
        return self.recovery_index is not None


def max_drawdown(values: Sequence[float]) -> Drawdown | None:
    """Deepest peak-to-trough decline in a value series.

    Computed on the value series rather than reconstructed from returns, so it
    is exact rather than accumulating floating-point drift over long histories.
    """
    if len(values) < 2:
        return None
    peak = values[0]
    peak_index = 0
    worst = Drawdown(0.0, 0, 0)
    for index, value in enumerate(values):
        if value > peak:
            peak, peak_index = value, index
        if peak > 0:
            decline = value / peak - 1.0
            if decline < worst.depth:
                worst = Drawdown(decline, peak_index, index)
    if worst.depth == 0.0:
        return Drawdown(0.0, 0, 0, recovery_index=0)

    recovery = None
    peak_value = values[worst.peak_index]
    for index in range(worst.trough_index + 1, len(values)):
        if values[index] >= peak_value:
            recovery = index
            break
    return Drawdown(worst.depth, worst.peak_index, worst.trough_index, recovery)


def value_at_risk(
    returns: Sequence[float], confidence: float = 0.95, *, horizon: int = 1
) -> float | None:
    """Historical VaR — the loss exceeded (1−confidence) of the time.

    Historical rather than parametric, deliberately. Equity returns have fatter
    tails than a normal distribution, and a Gaussian VaR systematically
    understates exactly the losses the measure exists to warn about.

    Returned as a **negative** fraction, so the sign convention matches the
    return series it came from.
    """
    if len(returns) < MIN_OBSERVATIONS or not 0 < confidence < 1:
        return None
    ordered = sorted(returns)
    # Nearest-rank on the lower tail; clamped so a short series still resolves.
    rank = int(math.floor((1.0 - confidence) * len(ordered)))
    rank = min(max(rank, 0), len(ordered) - 1)
    single = ordered[rank]
    return single * math.sqrt(horizon) if horizon > 1 else single


def conditional_value_at_risk(
    returns: Sequence[float], confidence: float = 0.95
) -> float | None:
    """Expected loss *given* the VaR threshold is breached.

    CVaR is the more honest of the pair: VaR says how bad a day has to be
    before it counts as bad, CVaR says how bad those days actually are.
    """
    if len(returns) < MIN_OBSERVATIONS or not 0 < confidence < 1:
        return None
    ordered = sorted(returns)
    cutoff = max(1, int(math.floor((1.0 - confidence) * len(ordered))))
    tail = ordered[:cutoff]
    return (sum(tail) / len(tail)) if tail else None


def covariance(a: Sequence[float], b: Sequence[float]) -> float | None:
    n = min(len(a), len(b))
    if n < MIN_OBSERVATIONS:
        return None
    x, y = list(a[:n]), list(b[:n])
    mx, my = sum(x) / n, sum(y) / n
    return sum((xi - mx) * (yi - my) for xi, yi in zip(x, y)) / (n - 1)


def beta(
    portfolio_returns: Sequence[float], benchmark_returns: Sequence[float]
) -> float | None:
    """Regression beta against a benchmark: cov(p, b) / var(b)."""
    cov = covariance(portfolio_returns, benchmark_returns)
    if cov is None:
        return None
    n = min(len(portfolio_returns), len(benchmark_returns))
    bench_var = covariance(benchmark_returns[:n], benchmark_returns[:n])
    if bench_var is None or bench_var == 0:
        return None
    return cov / bench_var


def alpha(
    portfolio_returns: Sequence[float],
    benchmark_returns: Sequence[float],
    risk_free: float = DEFAULT_RISK_FREE,
    periods: int = TRADING_DAYS,
) -> float | None:
    """Jensen's alpha — return beyond what beta alone would have delivered."""
    b = beta(portfolio_returns, benchmark_returns)
    if b is None:
        return None
    rp = annualised_return(portfolio_returns, periods)
    rm = annualised_return(benchmark_returns, periods)
    if rp is None or rm is None:
        return None
    return rp - (risk_free + b * (rm - risk_free))


def tracking_error(
    portfolio_returns: Sequence[float],
    benchmark_returns: Sequence[float],
    periods: int = TRADING_DAYS,
) -> float | None:
    """Annualised standard deviation of active return."""
    n = min(len(portfolio_returns), len(benchmark_returns))
    if n < MIN_OBSERVATIONS:
        return None
    active = [portfolio_returns[i] - benchmark_returns[i] for i in range(n)]
    sigma = stdev(active)
    return None if sigma is None else sigma * math.sqrt(periods)


def information_ratio(
    portfolio_returns: Sequence[float],
    benchmark_returns: Sequence[float],
    periods: int = TRADING_DAYS,
) -> float | None:
    """Active return per unit of tracking error."""
    te = tracking_error(portfolio_returns, benchmark_returns, periods)
    if te is None or te == 0:
        return None
    rp = annualised_return(portfolio_returns, periods)
    rm = annualised_return(benchmark_returns, periods)
    if rp is None or rm is None:
        return None
    return (rp - rm) / te


def up_down_capture(
    portfolio_returns: Sequence[float], benchmark_returns: Sequence[float]
) -> tuple[float | None, float | None]:
    """How much of the benchmark's up and down moves the portfolio captured."""
    n = min(len(portfolio_returns), len(benchmark_returns))
    if n < MIN_OBSERVATIONS:
        return None, None
    up_p = [portfolio_returns[i] for i in range(n) if benchmark_returns[i] > 0]
    up_b = [benchmark_returns[i] for i in range(n) if benchmark_returns[i] > 0]
    down_p = [portfolio_returns[i] for i in range(n) if benchmark_returns[i] < 0]
    down_b = [benchmark_returns[i] for i in range(n) if benchmark_returns[i] < 0]
    up = (sum(up_p) / sum(up_b)) if up_b and sum(up_b) else None
    down = (sum(down_p) / sum(down_b)) if down_b and sum(down_b) else None
    return up, down


# ---------------------------------------------------------------------------
# Concentration and liquidity
# ---------------------------------------------------------------------------
def herfindahl(weights: Sequence[float]) -> float:
    """HHI of position weights. 1.0 is a single holding."""
    return sum(w * w for w in weights)


def effective_positions(weights: Sequence[float]) -> float:
    """1/HHI — the workbook's measure of genuine breadth.

    Ten positions with one at 90% is not ten positions. This reports roughly
    1.2, which is the honest number.
    """
    hhi = herfindahl(weights)
    return (1.0 / hhi) if hhi > 0 else 0.0


def top_n_concentration(weights: Sequence[float], n: int = 5) -> float:
    return sum(sorted(weights, reverse=True)[:n])


def diversification_score(weights: Sequence[float], target_names: int = 15) -> float:
    """The workbook's 0–100 diversification score.

    `100 × (1 − HHI) × min(1, names/target)`. Two independent penalties: weight
    concentration, and simply not holding enough names. A five-stock portfolio
    equally weighted scores well on the first and poorly on the second, which
    is correct.
    """
    if not weights:
        return 0.0
    breadth = min(1.0, len(weights) / target_names) if target_names > 0 else 1.0
    return round(min(100.0, 100.0 * (1.0 - herfindahl(weights)) * breadth), 2)


def liquidity_days(
    position_value: float,
    average_daily_value: float | None,
    participation: float = 0.20,
) -> float | None:
    """Days to liquidate at a given share of average daily traded value.

    Twenty per cent participation is the conventional institutional assumption:
    trading more than a fifth of a day's volume moves the price against you,
    so a position needing thirty days to exit is genuinely illiquid however
    good the business is.
    """
    if not average_daily_value or average_daily_value <= 0 or participation <= 0:
        return None
    return position_value / (average_daily_value * participation)


@dataclass(slots=True)
class RiskProfile:
    """The complete risk picture. Every field may legitimately be ``None``."""

    observations: int = 0
    annualised_return: float | None = None
    annualised_volatility: float | None = None
    sharpe: float | None = None
    sortino: float | None = None
    max_drawdown: float | None = None
    drawdown_recovered: bool | None = None
    var_95: float | None = None
    cvar_95: float | None = None
    var_99: float | None = None
    beta: float | None = None
    alpha: float | None = None
    tracking_error: float | None = None
    information_ratio: float | None = None
    up_capture: float | None = None
    down_capture: float | None = None
    herfindahl: float | None = None
    effective_positions: float | None = None
    top_5_concentration: float | None = None
    diversification_score: float | None = None
    largest_position_weight: float | None = None
    illiquid_positions: int = 0
    #: Populated when a statistic could not be computed, so the UI can explain
    #: a blank cell rather than showing a dash the user has to interpret.
    unavailable: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.unavailable is None:
            self.unavailable = []


def build_risk_profile(
    portfolio_values: Sequence[float],
    benchmark_values: Sequence[float] | None = None,
    weights: Sequence[float] | None = None,
    *,
    risk_free: float = DEFAULT_RISK_FREE,
    periods: int = TRADING_DAYS,
    illiquid_count: int = 0,
) -> RiskProfile:
    """Assemble every risk statistic, recording what could not be computed."""
    returns = to_returns(portfolio_values)
    profile = RiskProfile(observations=len(returns))
    gaps: list[str] = []

    if len(returns) < MIN_OBSERVATIONS:
        gaps.append(
            f"Return statistics need at least {MIN_OBSERVATIONS} observations; "
            f"{len(returns)} available"
        )
    else:
        profile.annualised_return = annualised_return(returns, periods)
        profile.annualised_volatility = annualised_volatility(returns, periods)
        profile.sharpe = sharpe_ratio(returns, risk_free, periods)
        profile.sortino = sortino_ratio(returns, risk_free, periods)
        profile.var_95 = value_at_risk(returns, 0.95)
        profile.cvar_95 = conditional_value_at_risk(returns, 0.95)
        profile.var_99 = value_at_risk(returns, 0.99)
        if profile.sortino is None:
            gaps.append("Sortino undefined — no returns below the risk-free hurdle")

    drawdown = max_drawdown(portfolio_values)
    if drawdown is not None:
        profile.max_drawdown = drawdown.depth
        profile.drawdown_recovered = drawdown.recovered

    if benchmark_values:
        bench_returns = to_returns(benchmark_values)
        if len(bench_returns) >= MIN_OBSERVATIONS:
            profile.beta = beta(returns, bench_returns)
            profile.alpha = alpha(returns, bench_returns, risk_free, periods)
            profile.tracking_error = tracking_error(returns, bench_returns, periods)
            profile.information_ratio = information_ratio(
                returns, bench_returns, periods
            )
            profile.up_capture, profile.down_capture = up_down_capture(
                returns, bench_returns
            )
        else:
            gaps.append("Benchmark history too short for beta and alpha")
    else:
        gaps.append("No benchmark series supplied — beta and alpha unavailable")

    if weights:
        profile.herfindahl = herfindahl(weights)
        profile.effective_positions = effective_positions(weights)
        profile.top_5_concentration = top_n_concentration(weights, 5)
        profile.diversification_score = diversification_score(weights)
        profile.largest_position_weight = max(weights, default=None)

    profile.illiquid_positions = illiquid_count
    profile.unavailable = gaps
    return profile
