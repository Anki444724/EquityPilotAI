"""Scenario construction and probability-weighted analysis.

Bull and bear cases are derived from the base case by shifting drivers, not by
maintaining three unrelated assumption sets. That matters for two reasons: the
cases stay internally comparable, and an analyst who changes one base
assumption sees it propagate into all three.

Each driver is shifted in the direction that is *unfavourable* in the bear case
and *favourable* in the bull case. Rates that an analyst thinks about in
percentage points (margins, tax, WACC) are shifted additively; levels and
multiples are shifted proportionally. Applying a relative change to a margin
would be meaningless — "10% worse than an 18% margin" is not a claim anyone
makes.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import sqrt

from app.domain.calc import safe_div
from .assumptions import Driver, ForecastAssumptions, Scenario
from .engine import ForecastBase, ForecastEngine, ForecastResult


@dataclass(frozen=True, slots=True)
class DriverShift:
    """How one driver moves between scenarios.

    ``bear`` and ``bull`` are the shift applied in each case. ``additive``
    selects point-shift versus proportional-shift semantics.
    """

    name: str
    bear: float
    bull: float
    additive: bool = True


#: Default scenario calibration. Every value is overridable per company; these
#: are starting points, not embedded truths.
DEFAULT_SHIFTS: tuple[DriverShift, ...] = (
    # growth: 300 bps either side
    DriverShift("revenue_growth", bear=-0.03, bull=0.03),
    DriverShift("organic_growth", bear=-0.03, bull=0.03),
    DriverShift("volume_growth", bear=-0.02, bull=0.02),
    DriverShift("price_growth", bear=-0.01, bull=0.01),
    DriverShift("terminal_revenue_growth", bear=-0.01, bull=0.01),
    # margins: 200 bps either side
    DriverShift("ebitda_margin", bear=-0.02, bull=0.02),
    DriverShift("margin_expansion", bear=-0.005, bull=0.005),
    # capital intensity: worse means spending more for the same revenue
    DriverShift("capex_pct_revenue", bear=0.015, bull=-0.01),
    # working capital: worse means slower collection, faster payment
    DriverShift("receivable_days", bear=8.0, bull=-5.0),
    DriverShift("inventory_days", bear=10.0, bull=-6.0),
    DriverShift("payable_days", bear=-5.0, bull=4.0),
    # financing and discounting
    DriverShift("interest_rate", bear=0.015, bull=-0.005),
    DriverShift("effective_tax_rate", bear=0.03, bull=-0.02),
    DriverShift("wacc", bear=0.02, bull=-0.015),
    DriverShift("terminal_growth", bear=-0.01, bull=0.0075),
    # exit multiples move proportionally
    DriverShift("exit_ev_ebitda", bear=-0.25, bull=0.25, additive=False),
    DriverShift("target_pe", bear=-0.25, bull=0.25, additive=False),
)

#: Default probabilities. Must sum to 1.0.
DEFAULT_PROBABILITIES = {Scenario.BEAR: 0.25, Scenario.BASE: 0.55, Scenario.BULL: 0.20}

#: Floors and caps that keep a shifted driver economically sensible.
BOUNDS: dict[str, tuple[float, float]] = {
    "ebitda_margin": (0.01, 0.90),
    "effective_tax_rate": (0.0, 0.60),
    "wacc": (0.04, 0.35),
    "terminal_growth": (-0.02, 0.08),
    "interest_rate": (0.0, 0.30),
    "capex_pct_revenue": (0.0, 0.50),
    "inventory_days": (0.0, 400.0),
    "receivable_days": (0.0, 400.0),
    "payable_days": (0.0, 400.0),
    "exit_ev_ebitda": (1.0, 60.0),
    "target_pe": (1.0, 100.0),
}


def _bounded(name: str, value: float) -> float:
    lo, hi = BOUNDS.get(name, (float("-inf"), float("inf")))
    return max(lo, min(hi, value))


def derive_scenario(
    base: ForecastAssumptions,
    scenario: Scenario,
    shifts: tuple[DriverShift, ...] = DEFAULT_SHIFTS,
    probability: float | None = None,
) -> ForecastAssumptions:
    """Build a bull or bear assumption set from the base case."""
    if scenario is Scenario.BASE:
        prob = probability if probability is not None else DEFAULT_PROBABILITIES[Scenario.BASE]
        return base.override(
            scenario=Scenario.BASE, probability=Driver(value=prob, source=base.probability.source)
        )

    patch: dict[str, object] = {}
    for shift in shifts:
        current = getattr(base, shift.name, None)
        if not isinstance(current, Driver):
            continue
        amount = shift.bear if scenario is Scenario.BEAR else shift.bull
        moved = current.shifted(amount) if shift.additive else current.scaled(1 + amount)
        patch[shift.name] = Driver(
            value=_bounded(shift.name, moved.value),
            by_year={k: _bounded(shift.name, v) for k, v in moved.by_year.items()},
            source=current.source,
            citation=current.citation,
            note=current.note,
        )

    prob = probability if probability is not None else DEFAULT_PROBABILITIES[scenario]
    patch["scenario"] = scenario
    patch["probability"] = Driver(value=prob)
    return base.override(**patch)


@dataclass(frozen=True, slots=True)
class ScenarioOutcome:
    """Headline outputs for one scenario."""

    scenario: str
    probability: float
    terminal_revenue: float
    terminal_ebitda: float
    terminal_eps: float | None
    revenue_cagr: float | None
    terminal_fcff: float
    #: Simple exit-multiple value per share, used for scenario comparison.
    value_per_share: float | None
    upside: float | None


@dataclass(frozen=True, slots=True)
class ScenarioAnalysis:
    """Three scenarios plus the probability-weighted conclusion."""

    results: dict[str, ForecastResult]
    outcomes: list[ScenarioOutcome]
    expected_value: float | None
    expected_upside: float | None
    bull_upside: float | None
    bear_downside: float | None
    risk_reward: float | None
    standard_deviation: float | None
    coefficient_of_variation: float | None
    verdict: str


def _value_per_share(result: ForecastResult, cmp_price: float | None) -> float | None:
    """Exit-multiple equity value per share at the terminal year.

    A full DCF belongs to Module 4; this is the scenario-comparison measure,
    computed identically across cases so the comparison is fair.
    """
    terminal = result.terminal_year
    if terminal is None or not result.base.shares_outstanding:
        return None
    multiple = result.assumptions.exit_ev_ebitda.at(terminal.period)
    enterprise_value = terminal.ebitda * multiple
    equity_value = enterprise_value - terminal.net_debt
    return equity_value / result.base.shares_outstanding


def run_scenarios(
    base_position: ForecastBase,
    base_assumptions: ForecastAssumptions,
    cmp_price: float | None = None,
    shifts: tuple[DriverShift, ...] = DEFAULT_SHIFTS,
    probabilities: dict[Scenario, float] | None = None,
) -> ScenarioAnalysis:
    """Run all three cases and produce the weighted conclusion."""
    probs = probabilities or DEFAULT_PROBABILITIES
    results: dict[str, ForecastResult] = {}
    outcomes: list[ScenarioOutcome] = []

    for scenario in (Scenario.BEAR, Scenario.BASE, Scenario.BULL):
        assumptions = derive_scenario(
            base_assumptions, scenario, shifts, probs.get(scenario)
        )
        result = ForecastEngine(base_position, assumptions).run()
        results[scenario.value] = result

        terminal = result.terminal_year
        value = _value_per_share(result, cmp_price)
        outcomes.append(
            ScenarioOutcome(
                scenario=scenario.value,
                probability=probs.get(scenario, 0.0),
                terminal_revenue=terminal.revenue if terminal else 0.0,
                terminal_ebitda=terminal.ebitda if terminal else 0.0,
                terminal_eps=terminal.eps if terminal else None,
                revenue_cagr=result.revenue_cagr,
                terminal_fcff=terminal.fcff if terminal else 0.0,
                value_per_share=value,
                upside=safe_div(value, cmp_price) - 1
                if value is not None and cmp_price else None,
            )
        )

    # ---- probability-weighted conclusion -------------------------------
    valued = [o for o in outcomes if o.value_per_share is not None]
    weight = sum(o.probability for o in valued)
    expected = (
        sum(o.value_per_share * o.probability for o in valued) / weight  # type: ignore[misc]
        if valued and weight else None
    )

    variance = (
        sum(o.probability * (o.value_per_share - expected) ** 2 for o in valued) / weight  # type: ignore[operator]
        if expected is not None and weight else None
    )
    stdev = sqrt(variance) if variance is not None and variance >= 0 else None

    by_name = {o.scenario: o for o in outcomes}
    bull_upside = by_name[Scenario.BULL.value].upside
    bear_downside = by_name[Scenario.BEAR.value].upside
    expected_upside = (
        safe_div(expected, cmp_price) - 1 if expected is not None and cmp_price else None
    )

    risk_reward = (
        abs(bull_upside) / abs(bear_downside)
        if bull_upside is not None and bear_downside not in (None, 0)
        and bear_downside < 0
        else None
    )

    if risk_reward is not None and expected_upside is not None:
        if risk_reward > 2 and expected_upside > 0.15:
            verdict = "Asymmetric — strong risk/reward"
        elif risk_reward > 1.5:
            verdict = "Favourable — acceptable risk/reward"
        elif risk_reward > 1:
            verdict = "Balanced — limited edge"
        else:
            verdict = "Unfavourable — downside outweighs upside"
    else:
        verdict = "Not assessable without a market price"

    return ScenarioAnalysis(
        results=results,
        outcomes=outcomes,
        expected_value=expected,
        expected_upside=expected_upside,
        bull_upside=bull_upside,
        bear_downside=bear_downside,
        risk_reward=risk_reward,
        standard_deviation=stdev,
        coefficient_of_variation=safe_div(stdev, expected),
        verdict=verdict,
    )
