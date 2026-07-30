"""Dividend discount models.

Three variants, chosen by the maturity of the dividend stream:

* **Gordon growth** — one perpetual growth rate. Suitable only for stable,
  mature payers.
* **Two-stage** — a high-growth phase, then an abrupt step down to a stable
  rate. The standard textbook treatment.
* **H-model** — growth fades *linearly* from a high initial rate to a stable
  rate over 2H years. More realistic than the two-stage step, and closed-form.

A DDM is only meaningful for a company that actually pays dividends, so the
engine refuses to produce a value when the payout is nil and says why, rather
than returning a misleading zero.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from app.domain.calc import safe_div

MIN_SPREAD = 0.005


class DDMVariant(StrEnum):
    GORDON = "gordon"
    TWO_STAGE = "two_stage"
    H_MODEL = "h_model"


@dataclass(frozen=True, slots=True)
class DDMInputs:
    current_dividend_per_share: float
    cost_of_equity: float
    stable_growth: float = 0.05

    variant: DDMVariant = DDMVariant.GORDON

    # two-stage
    high_growth: float = 0.12
    high_growth_years: int = 5

    # H-model: growth fades linearly across 2 x half_life years
    half_life_years: int = 5

    current_price: float | None = None


@dataclass(frozen=True, slots=True)
class DDMResult:
    variant: str
    value_per_share: float | None
    terminal_value: float | None
    pv_explicit: float | None
    implied_dividend_yield: float | None
    upside: float | None
    warnings: list[str] = field(default_factory=list)


def run_ddm(inputs: DDMInputs) -> DDMResult:
    warnings: list[str] = []
    d0 = inputs.current_dividend_per_share
    ke = inputs.cost_of_equity
    g = inputs.stable_growth

    if d0 <= 0:
        return DDMResult(
            variant=inputs.variant.value, value_per_share=None, terminal_value=None,
            pv_explicit=None, implied_dividend_yield=None, upside=None,
            warnings=["Company pays no dividend; a dividend discount model does not apply."],
        )

    if ke - g <= MIN_SPREAD:
        warnings.append(
            f"Cost of equity ({ke:.2%}) is too close to stable growth ({g:.2%}); "
            "the perpetuity is unstable."
        )
        g = ke - MIN_SPREAD

    pv_explicit = 0.0
    terminal = None

    if inputs.variant is DDMVariant.GORDON:
        value = d0 * (1 + g) / (ke - g)
        terminal = value

    elif inputs.variant is DDMVariant.TWO_STAGE:
        dividend = d0
        for year in range(1, inputs.high_growth_years + 1):
            dividend *= 1 + inputs.high_growth
            pv_explicit += dividend / ((1 + ke) ** year)
        terminal_dividend = dividend * (1 + g)
        terminal = terminal_dividend / (ke - g)
        value = pv_explicit + terminal / ((1 + ke) ** inputs.high_growth_years)

    else:  # H-model
        # P0 = D0[(1+gs) + H(gh - gs)] / (Ke - gs), where H = half-life
        h = inputs.half_life_years
        value = d0 * ((1 + g) + h * (inputs.high_growth - g)) / (ke - g)
        terminal = value

    return DDMResult(
        variant=inputs.variant.value,
        value_per_share=value,
        terminal_value=terminal,
        pv_explicit=pv_explicit or None,
        implied_dividend_yield=safe_div(d0, value),
        upside=safe_div(value, inputs.current_price) - 1 if inputs.current_price else None,
        warnings=warnings,
    )
