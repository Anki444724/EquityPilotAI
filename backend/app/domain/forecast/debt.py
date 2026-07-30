"""Debt schedule with cash sweep.

This is the circular part of any integrated model: interest depends on the debt
balance, the balance depends on free cash flow, and free cash flow depends on
interest. Spreadsheets resolve this with iterative calculation, which is fragile
and order-dependent.

We resolve it explicitly instead. :func:`solve_debt_schedule` iterates the
schedule to a fixed point with a convergence tolerance, so the result is
deterministic, inspectable, and cannot silently fail to converge — if it does
not settle, it says so.

Interest is charged on the **average** of opening and closing balances, which is
the correct treatment when debt moves materially during a year.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.domain.calc import safe_div
from .assumptions import ForecastAssumptions

#: Convergence tolerance in ₹ crore. Interest differences below this are noise.
CONVERGENCE_TOLERANCE = 1e-6
MAX_ITERATIONS = 50


@dataclass(frozen=True, slots=True)
class DebtYear:
    period: int
    fiscal_year: int
    opening_debt: float
    scheduled_repayment: float
    new_borrowing: float
    #: Additional voluntary prepayment funded by surplus cash.
    cash_sweep_repayment: float
    closing_debt: float
    average_debt: float
    interest_expense: float
    opening_cash: float
    closing_cash: float
    interest_income: float
    net_debt: float
    effective_rate: float | None


@dataclass(frozen=True, slots=True)
class DebtSolution:
    years: list[DebtYear]
    iterations: int
    converged: bool
    residual: float


def solve_debt_schedule(
    *,
    opening_debt: float,
    opening_cash: float,
    assumptions: ForecastAssumptions,
    revenue: list[float],
    cash_before_financing: list[float],
    dividends: list[float],
) -> DebtSolution:
    """Iterate the debt/cash schedule to a fixed point.

    ``cash_before_financing`` is cash generated after operations, tax and
    capex but *before* interest, debt movements and dividends. The solver
    supplies interest back into the loop until the balances stop moving.
    """
    a = assumptions
    n = len(revenue)
    interest_guess = [0.0] * n
    income_guess = [0.0] * n
    rows: list[DebtYear] = []
    residual = 0.0
    iteration = 0

    for iteration in range(1, MAX_ITERATIONS + 1):
        rows = []
        debt = opening_debt
        cash = opening_cash

        for i in range(n):
            period = i + 1
            rate = a.interest_rate.at(period)
            repay_pct = a.debt_repayment_pct.at(period)
            new_debt = a.new_debt.at(period)

            scheduled = min(debt, debt * repay_pct)

            # Cash available after operations, financing costs and dividends.
            available = (
                cash
                + cash_before_financing[i]
                - interest_guess[i]
                + income_guess[i]
                - dividends[i]
                - scheduled
                + new_debt
            )

            # Retain a minimum operating buffer, sweep the rest into debt.
            min_cash = revenue[i] * a.min_cash_pct_revenue.at(period)
            surplus = max(0.0, available - min_cash)
            remaining_debt = max(0.0, debt - scheduled + new_debt)
            sweep = min(surplus, remaining_debt)

            closing_debt = remaining_debt - sweep
            closing_cash = available - sweep

            avg_debt = (debt + closing_debt) / 2
            avg_cash = (cash + closing_cash) / 2
            interest = avg_debt * rate
            income = max(0.0, avg_cash) * a.cash_yield.at(period)

            rows.append(
                DebtYear(
                    period=period,
                    fiscal_year=0,  # assigned by the caller
                    opening_debt=debt,
                    scheduled_repayment=scheduled,
                    new_borrowing=new_debt,
                    cash_sweep_repayment=sweep,
                    closing_debt=closing_debt,
                    average_debt=avg_debt,
                    interest_expense=interest,
                    opening_cash=cash,
                    closing_cash=closing_cash,
                    interest_income=income,
                    net_debt=closing_debt - closing_cash,
                    effective_rate=safe_div(interest, avg_debt),
                )
            )
            debt = closing_debt
            cash = closing_cash

        new_interest = [r.interest_expense for r in rows]
        new_income = [r.interest_income for r in rows]
        residual = max(
            max((abs(a_ - b) for a_, b in zip(new_interest, interest_guess)), default=0.0),
            max((abs(a_ - b) for a_, b in zip(new_income, income_guess)), default=0.0),
        )
        interest_guess, income_guess = new_interest, new_income

        if residual < CONVERGENCE_TOLERANCE:
            return DebtSolution(rows, iteration, True, residual)

    return DebtSolution(rows, iteration, False, residual)
