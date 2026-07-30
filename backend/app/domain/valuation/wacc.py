"""WACC engine.

Cost of equity is built from CAPM with the adjustments institutional practice
requires in an emerging market:

    Ke = Rf + β × (mature ERP + country risk premium) + size premium
         + company-specific premium

Beta can be sourced three ways — a bottom-up unlevered sector beta relevered to
the company's own capital structure (Hamada), a regression beta, or the average
of both. Bottom-up is the default because a single-stock regression beta is
statistically noisy and unstable across estimation windows.

    βL = βU × [1 + (1 − t) × D/E]

Cost of debt is observed where possible (finance costs over average debt) and
falls back to a synthetic spread over the risk-free rate when it is not.

**Dynamic WACC.** Weights are market-value based, so as a forecast delevers the
capital structure changes and the discount rate should change with it. The
engine can produce a per-period WACC schedule rather than a single constant.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.domain.calc import safe_div


class BetaSource(StrEnum):
    BOTTOM_UP = "bottom_up"          # unlevered sector beta, relevered
    REGRESSION = "regression"        # observed regression beta
    AVERAGE = "average"              # mean of the two


#: Floor on after-tax cost of debt. A company cannot borrow below the
#: short-term risk-free rate in practice; this prevents an absurd WACC when a
#: near-debt-free company shows a tiny observed interest cost.
MIN_COST_OF_DEBT = 0.03

#: WACC is bounded to a plausible band. Outside this range the inputs are
#: wrong, and a valuation built on them would be misleading rather than useful.
WACC_FLOOR = 0.04
WACC_CEILING = 0.35


@dataclass(frozen=True, slots=True)
class WACCInputs:
    """Every WACC driver. Nothing is hard-coded downstream."""

    risk_free_rate: float = 0.0695
    mature_erp: float = 0.055
    country_risk_premium: float = 0.0243

    unlevered_beta: float = 0.85
    regression_beta: float | None = None
    beta_source: BetaSource = BetaSource.BOTTOM_UP

    size_premium: float = 0.01
    specific_premium: float = 0.005

    #: Pre-tax cost of debt. When None the engine derives it from a spread.
    cost_of_debt: float | None = None
    credit_spread: float = 0.025
    marginal_tax_rate: float = 0.25

    market_value_equity: float = 0.0
    market_value_debt: float = 0.0

    @property
    def total_equity_risk_premium(self) -> float:
        """Mature-market ERP plus the country premium."""
        return self.mature_erp + self.country_risk_premium


@dataclass(frozen=True, slots=True)
class WACCResult:
    """A fully decomposed WACC, so every component is auditable."""

    # cost of equity
    risk_free_rate: float
    total_erp: float
    unlevered_beta: float
    levered_beta: float
    regression_beta: float | None
    beta_used: float
    beta_source: str
    size_premium: float
    specific_premium: float
    cost_of_equity: float

    # cost of debt
    pre_tax_cost_of_debt: float
    marginal_tax_rate: float
    after_tax_cost_of_debt: float

    # structure
    market_value_equity: float
    market_value_debt: float
    total_capital: float
    weight_equity: float
    weight_debt: float
    debt_to_equity: float

    wacc: float
    #: True when the raw computation fell outside the plausible band.
    bounded: bool = False


def relever_beta(unlevered: float, debt_to_equity: float, tax_rate: float) -> float:
    """Hamada relevering: βL = βU × [1 + (1 − t) × D/E]."""
    return unlevered * (1 + (1 - tax_rate) * debt_to_equity)


def unlever_beta(levered: float, debt_to_equity: float, tax_rate: float) -> float:
    """Inverse of :func:`relever_beta`, for deriving a sector asset beta."""
    return levered / (1 + (1 - tax_rate) * debt_to_equity)


def compute_wacc(inputs: WACCInputs) -> WACCResult:
    """Build the weighted average cost of capital from its components."""
    equity = max(0.0, inputs.market_value_equity)
    debt = max(0.0, inputs.market_value_debt)
    total = equity + debt

    # Capital-structure weights. With no capital at all the model is
    # all-equity by convention rather than undefined.
    weight_equity = safe_div(equity, total)
    weight_equity = 1.0 if weight_equity is None else weight_equity
    weight_debt = 1.0 - weight_equity
    de_ratio = safe_div(debt, equity) or 0.0

    # ---- cost of equity -------------------------------------------------
    levered = relever_beta(inputs.unlevered_beta, de_ratio, inputs.marginal_tax_rate)

    if inputs.beta_source is BetaSource.REGRESSION and inputs.regression_beta:
        beta_used = inputs.regression_beta
    elif inputs.beta_source is BetaSource.AVERAGE and inputs.regression_beta:
        beta_used = (levered + inputs.regression_beta) / 2
    else:
        beta_used = levered

    terp = inputs.total_equity_risk_premium
    cost_of_equity = (
        inputs.risk_free_rate
        + beta_used * terp
        + inputs.size_premium
        + inputs.specific_premium
    )

    # ---- cost of debt ---------------------------------------------------
    pre_tax_kd = inputs.cost_of_debt
    if pre_tax_kd is None or pre_tax_kd <= 0:
        # Synthetic: risk-free plus a credit spread.
        pre_tax_kd = inputs.risk_free_rate + inputs.credit_spread
    pre_tax_kd = max(MIN_COST_OF_DEBT, pre_tax_kd)
    after_tax_kd = pre_tax_kd * (1 - inputs.marginal_tax_rate)

    # ---- weighted result -------------------------------------------------
    raw = weight_equity * cost_of_equity + weight_debt * after_tax_kd
    wacc = min(WACC_CEILING, max(WACC_FLOOR, raw))

    return WACCResult(
        risk_free_rate=inputs.risk_free_rate,
        total_erp=terp,
        unlevered_beta=inputs.unlevered_beta,
        levered_beta=levered,
        regression_beta=inputs.regression_beta,
        beta_used=beta_used,
        beta_source=inputs.beta_source.value,
        size_premium=inputs.size_premium,
        specific_premium=inputs.specific_premium,
        cost_of_equity=cost_of_equity,
        pre_tax_cost_of_debt=pre_tax_kd,
        marginal_tax_rate=inputs.marginal_tax_rate,
        after_tax_cost_of_debt=after_tax_kd,
        market_value_equity=equity,
        market_value_debt=debt,
        total_capital=total,
        weight_equity=weight_equity,
        weight_debt=weight_debt,
        debt_to_equity=de_ratio,
        wacc=wacc,
        bounded=abs(raw - wacc) > 1e-12,
    )


def dynamic_wacc_schedule(
    inputs: WACCInputs,
    equity_values: list[float],
    debt_values: list[float],
) -> list[WACCResult]:
    """Per-period WACC as the capital structure evolves.

    A forecast that repays debt becomes progressively less levered, so holding
    WACC constant across the horizon understates the cost of equity early and
    overstates it late. This recomputes the full build for each period.
    """
    out: list[WACCResult] = []
    for equity, debt in zip(equity_values, debt_values):
        out.append(
            compute_wacc(
                WACCInputs(
                    risk_free_rate=inputs.risk_free_rate,
                    mature_erp=inputs.mature_erp,
                    country_risk_premium=inputs.country_risk_premium,
                    unlevered_beta=inputs.unlevered_beta,
                    regression_beta=inputs.regression_beta,
                    beta_source=inputs.beta_source,
                    size_premium=inputs.size_premium,
                    specific_premium=inputs.specific_premium,
                    cost_of_debt=inputs.cost_of_debt,
                    credit_spread=inputs.credit_spread,
                    marginal_tax_rate=inputs.marginal_tax_rate,
                    market_value_equity=equity,
                    market_value_debt=debt,
                )
            )
        )
    return out
