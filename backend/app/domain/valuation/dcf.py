"""Discounted cash-flow engines — FCFF and FCFE.

Both are implemented because they answer different questions and are only
equivalent under assumptions that rarely hold:

* **FCFF** discounts unlevered cash flow at WACC and bridges enterprise value
  to equity. Robust when leverage changes over the forecast.
* **FCFE** discounts levered cash flow at the cost of equity directly. Closer
  to what an equity holder receives, but sensitive to the debt schedule.

Discounting conventions
-----------------------
* **Year-end**: cash arrives on the last day of the period, t = 1, 2, 3…
* **Mid-year**: cash arrives evenly, t = 0.5, 1.5, 2.5… This is the more
  realistic default and lifts value by roughly √(1+r) − 1.

The terminal value uses the *same* convention as the explicit period, which is
a detail many models get wrong: under mid-year discounting the terminal value
is still a perpetuity beginning at the end of the final year, so it is
discounted at the final year's mid-year factor plus a half period.

Monte Carlo readiness
---------------------
:func:`run_dcf` is a pure function of ``DCFInputs``. It performs no I/O, holds
no state and never mutates its arguments, so thousands of draws can be run in
parallel simply by varying the inputs. :mod:`app.domain.valuation.simulation`
does exactly that.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.domain.calc import safe_div


class DiscountConvention(StrEnum):
    YEAR_END = "year_end"
    MID_YEAR = "mid_year"


class TerminalMethod(StrEnum):
    PERPETUAL_GROWTH = "perpetual_growth"
    EXIT_MULTIPLE = "exit_multiple"


#: Terminal growth must stay meaningfully below the discount rate or the
#: Gordon denominator collapses and the value explodes.
MAX_GROWTH_SPREAD = 0.005


@dataclass(frozen=True, slots=True)
class DCFInputs:
    """Everything a DCF needs. Immutable, so it is safe to fan out."""

    cash_flows: tuple[float, ...]
    discount_rate: float
    #: Per-period rates for a dynamic WACC. Overrides ``discount_rate``.
    discount_rate_schedule: tuple[float, ...] | None = None

    terminal_growth: float = 0.05
    terminal_method: TerminalMethod = TerminalMethod.PERPETUAL_GROWTH
    exit_multiple: float = 12.0
    #: Terminal-year EBITDA, required for the exit-multiple method.
    terminal_ebitda: float | None = None

    convention: DiscountConvention = DiscountConvention.MID_YEAR

    # enterprise-to-equity bridge (FCFF only)
    gross_debt: float = 0.0
    cash_and_equivalents: float = 0.0
    minority_interest: float = 0.0
    associate_investments: float = 0.0
    contingent_liabilities: float = 0.0

    shares_outstanding: float = 0.0
    current_price: float | None = None
    margin_of_safety: float = 0.20


@dataclass(frozen=True, slots=True)
class DCFYear:
    period: int
    cash_flow: float
    discount_period: float
    discount_rate: float
    discount_factor: float
    present_value: float


@dataclass(frozen=True, slots=True)
class DCFResult:
    """A fully decomposed DCF."""

    years: list[DCFYear]
    convention: str
    terminal_method: str

    sum_pv_explicit: float
    terminal_value: float
    pv_terminal_value: float
    terminal_value_pct: float | None

    enterprise_value: float
    #: None for FCFE, where the model values equity directly.
    net_debt: float | None
    equity_value: float
    shares_outstanding: float
    intrinsic_value_per_share: float | None

    current_price: float | None
    upside: float | None
    margin_of_safety: float
    maximum_buy_price: float | None
    in_buy_zone: bool | None

    discount_rate: float
    terminal_growth: float
    #: Diagnostics
    implied_exit_multiple: float | None
    implied_perpetual_growth: float | None
    warnings: list[str]


def discount_periods(n: int, convention: DiscountConvention) -> list[float]:
    """Discount exponents for each explicit period."""
    if convention is DiscountConvention.MID_YEAR:
        return [i - 0.5 for i in range(1, n + 1)]
    return [float(i) for i in range(1, n + 1)]


def _cumulative_factor(rates: list[float], upto: int, exponent: float) -> float:
    """Discount factor honouring a per-period rate schedule.

    With a flat rate this reduces to 1/(1+r)^t. With a schedule it compounds
    each period's own rate, which is what a dynamic WACC requires.
    """
    if len({round(r, 12) for r in rates[:upto]}) <= 1:
        rate = rates[0] if rates else 0.0
        return 1.0 / ((1 + rate) ** exponent)

    factor = 1.0
    whole = int(exponent)
    for i in range(min(whole, len(rates))):
        factor /= 1 + rates[i]
    fraction = exponent - whole
    if fraction > 0:
        rate = rates[min(whole, len(rates) - 1)]
        factor /= (1 + rate) ** fraction
    return factor


def run_dcf(inputs: DCFInputs, *, equity_model: bool = False) -> DCFResult:
    """Value a stream of cash flows.

    ``equity_model=True`` treats the flows as FCFE discounted at the cost of
    equity, so no enterprise-to-equity bridge is applied.
    """
    warnings: list[str] = []
    flows = list(inputs.cash_flows)
    n = len(flows)

    rates = (
        list(inputs.discount_rate_schedule)
        if inputs.discount_rate_schedule
        else [inputs.discount_rate] * n
    )
    if len(rates) < n:
        rates += [rates[-1] if rates else inputs.discount_rate] * (n - len(rates))
    effective_rate = sum(rates[:n]) / n if n else inputs.discount_rate

    # ---- explicit period -------------------------------------------------
    exponents = discount_periods(n, inputs.convention)
    years: list[DCFYear] = []
    for i, (flow, exponent) in enumerate(zip(flows, exponents)):
        factor = _cumulative_factor(rates, i + 1, exponent)
        years.append(
            DCFYear(
                period=i + 1,
                cash_flow=flow,
                discount_period=exponent,
                discount_rate=rates[i],
                discount_factor=factor,
                present_value=flow * factor,
            )
        )
    sum_pv = sum(y.present_value for y in years)

    # ---- terminal value --------------------------------------------------
    terminal_rate = rates[-1] if rates else inputs.discount_rate
    growth = inputs.terminal_growth
    terminal_flow = flows[-1] if flows else 0.0

    gordon_tv: float | None = None
    if terminal_rate - growth <= MAX_GROWTH_SPREAD:
        warnings.append(
            f"Terminal growth ({growth:.2%}) is too close to the discount rate "
            f"({terminal_rate:.2%}); the perpetuity is unstable."
        )
        capped = terminal_rate - MAX_GROWTH_SPREAD
        gordon_tv = terminal_flow * (1 + capped) / MAX_GROWTH_SPREAD
    else:
        gordon_tv = terminal_flow * (1 + growth) / (terminal_rate - growth)

    exit_tv: float | None = None
    if inputs.terminal_ebitda is not None:
        exit_tv = inputs.terminal_ebitda * inputs.exit_multiple

    if inputs.terminal_method is TerminalMethod.EXIT_MULTIPLE:
        if exit_tv is None:
            warnings.append(
                "Exit-multiple terminal value requested without terminal EBITDA; "
                "fell back to perpetual growth."
            )
            terminal_value = gordon_tv
        else:
            terminal_value = exit_tv
    else:
        terminal_value = gordon_tv

    # The terminal value sits at the end of the final explicit year, so it is
    # discounted a full period beyond the last mid-year point.
    terminal_exponent = float(n) if n else 0.0
    terminal_factor = _cumulative_factor(rates, n, terminal_exponent)
    pv_terminal = terminal_value * terminal_factor

    # ---- bridge -----------------------------------------------------------
    enterprise_value = sum_pv + pv_terminal
    if equity_model:
        equity_value = enterprise_value
        net_debt = None
    else:
        net_debt = inputs.gross_debt - inputs.cash_and_equivalents
        equity_value = (
            enterprise_value
            - inputs.gross_debt
            + inputs.cash_and_equivalents
            - inputs.minority_interest
            + inputs.associate_investments
            - inputs.contingent_liabilities
        )

    per_share = safe_div(equity_value, inputs.shares_outstanding)

    # ---- conclusion --------------------------------------------------------
    upside = None
    max_buy = None
    in_zone = None
    if per_share is not None and inputs.current_price:
        upside = per_share / inputs.current_price - 1
        max_buy = per_share * (1 - inputs.margin_of_safety)
        in_zone = inputs.current_price <= max_buy

    if equity_value < 0:
        warnings.append("Equity value is negative; the bridge overwhelms enterprise value.")

    tv_pct = safe_div(pv_terminal, enterprise_value)
    if tv_pct is not None and tv_pct > 0.85:
        warnings.append(
            f"Terminal value is {tv_pct:.0%} of enterprise value; the result depends "
            "overwhelmingly on assumptions beyond the forecast horizon."
        )

    # ---- diagnostics --------------------------------------------------------
    implied_exit = safe_div(gordon_tv, inputs.terminal_ebitda) if inputs.terminal_ebitda else None
    implied_growth = None
    if inputs.terminal_method is TerminalMethod.EXIT_MULTIPLE and exit_tv and terminal_flow:
        # Invert Gordon: g = (TV × r − CF) / (TV + CF)
        implied_growth = (exit_tv * terminal_rate - terminal_flow) / (exit_tv + terminal_flow)

    return DCFResult(
        years=years,
        convention=inputs.convention.value,
        terminal_method=inputs.terminal_method.value,
        sum_pv_explicit=sum_pv,
        terminal_value=terminal_value,
        pv_terminal_value=pv_terminal,
        terminal_value_pct=tv_pct,
        enterprise_value=enterprise_value,
        net_debt=net_debt,
        equity_value=equity_value,
        shares_outstanding=inputs.shares_outstanding,
        intrinsic_value_per_share=per_share,
        current_price=inputs.current_price,
        upside=upside,
        margin_of_safety=inputs.margin_of_safety,
        maximum_buy_price=max_buy,
        in_buy_zone=in_zone,
        discount_rate=effective_rate,
        terminal_growth=growth,
        implied_exit_multiple=implied_exit,
        implied_perpetual_growth=implied_growth,
        warnings=warnings,
    )
