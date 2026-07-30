"""Integrated cash-flow projection, FCFF and FCFE.

This module is the integrator: it consumes the revenue, margin, capex, working
capital, tax and debt schedules and produces a coherent three-statement view.

Two independent FCFF builds are produced and reconciled:

* **Top-down**  NOPAT + D&A - ΔNWC - capex
* **Bottom-up** CFO - interest tax shield + tax on non-operating income - capex

They must agree. Any divergence means a schedule is inconsistent, so the
reconciliation is reported rather than assumed — the same discipline the
workbook applied, kept because it catches real modelling errors.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.domain.calc import safe_div
from .assumptions import ForecastAssumptions
from .capex import CapexYear
from .debt import DebtYear
from .margins import MarginYear
from .taxes import TaxYear
from .working_capital import WorkingCapitalYear

#: Tolerance for the two FCFF builds agreeing, in ₹ crore.
RECONCILIATION_TOLERANCE = 0.01


@dataclass(frozen=True, slots=True)
class CashFlowYear:
    period: int
    fiscal_year: int

    # operating
    pat: float
    depreciation: float
    interest_expense: float
    change_in_nwc: float
    cfo: float

    # investing
    capex: float
    cfi: float

    # financing
    new_borrowing: float
    debt_repayment: float
    dividends_paid: float
    interest_paid: float
    cff: float

    net_cash_flow: float
    opening_cash: float
    closing_cash: float

    # free cash flow
    nopat: float
    fcff: float
    fcff_reconciled: float
    fcff_reconciliation_gap: float
    reconciled: bool
    fcfe: float
    free_cash_flow: float
    fcff_margin: float | None

    # value creation
    reinvestment: float
    reinvestment_rate: float | None
    roic: float | None
    implied_growth: float | None
    economic_profit: float | None


def build_cash_flows(
    *,
    assumptions: ForecastAssumptions,
    margins: list[MarginYear],
    capex_rows: list[CapexYear],
    wc_rows: list[WorkingCapitalYear],
    tax_rows: list[TaxYear],
    debt_rows: list[DebtYear],
    invested_capital: list[float],
) -> list[CashFlowYear]:
    """Assemble the cash-flow statement and both FCFF builds."""
    a = assumptions
    out: list[CashFlowYear] = []

    for i, m in enumerate(margins):
        cx, wc, tx, dbt = capex_rows[i], wc_rows[i], tax_rows[i], debt_rows[i]

        # ---- operating -------------------------------------------------
        cfo = tx.pat + m.depreciation + wc.change_in_nwc

        # ---- investing -------------------------------------------------
        cfi = -cx.capex

        # ---- financing -------------------------------------------------
        repayment = dbt.scheduled_repayment + dbt.cash_sweep_repayment
        dividends = tx.pat * a.dividend_payout.at(m.period)
        cff = dbt.new_borrowing - repayment - dividends

        net_cf = cfo + cfi + cff

        # ---- FCFF, top-down --------------------------------------------
        fcff = tx.nopat + m.depreciation + wc.change_in_nwc - cx.capex

        # ---- FCFF, bottom-up from CFO ----------------------------------
        # PAT carries non-operating items net of tax; FCFF must not. The exact
        # identity is:
        #   PAT  = (EBIT + other income + interest income - interest) x (1-t)
        #   CFO  = PAT + D&A + dNWC
        #   FCFF = EBIT x (1-t) + D&A + dNWC - capex
        # so the two builds differ by the after-tax non-operating flows.
        non_operating = (
            m.other_income + dbt.interest_income - dbt.interest_expense
        ) * (1 - tx.effective_rate)
        fcff_recon = cfo - non_operating - cx.capex
        gap = fcff - fcff_recon

        # ---- FCFE -------------------------------------------------------
        fcfe = cfo - cx.capex + dbt.new_borrowing - repayment

        # ---- value creation ---------------------------------------------
        reinvestment = cx.capex - m.depreciation - wc.change_in_nwc
        ic = invested_capital[i] if i < len(invested_capital) else None
        roic = safe_div(tx.nopat, ic)
        rr = safe_div(reinvestment, tx.nopat)
        implied = rr * roic if (rr is not None and roic is not None) else None
        econ = tx.nopat - a.wacc.at(m.period) * ic if ic is not None else None

        out.append(
            CashFlowYear(
                period=m.period,
                fiscal_year=m.fiscal_year,
                pat=tx.pat,
                depreciation=m.depreciation,
                interest_expense=dbt.interest_expense,
                change_in_nwc=wc.change_in_nwc,
                cfo=cfo,
                capex=cx.capex,
                cfi=cfi,
                new_borrowing=dbt.new_borrowing,
                debt_repayment=repayment,
                dividends_paid=dividends,
                interest_paid=dbt.interest_expense,
                cff=cff,
                net_cash_flow=net_cf,
                opening_cash=dbt.opening_cash,
                closing_cash=dbt.closing_cash,
                nopat=tx.nopat,
                fcff=fcff,
                fcff_reconciled=fcff_recon,
                fcff_reconciliation_gap=gap,
                reconciled=abs(gap) < RECONCILIATION_TOLERANCE,
                fcfe=fcfe,
                free_cash_flow=cfo - cx.capex,
                fcff_margin=safe_div(fcff, m.revenue),
                reinvestment=reinvestment,
                reinvestment_rate=rr,
                roic=roic,
                implied_growth=implied,
                economic_profit=econ,
            )
        )
    return out
