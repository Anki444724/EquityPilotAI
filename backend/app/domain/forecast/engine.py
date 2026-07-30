"""Forecast engine — orchestrates every projection service.

Runs the schedules in dependency order and resolves the one genuine circularity
(interest ↔ debt ↔ cash) explicitly rather than by iteration side-effects:

    revenue → capex/depreciation → margins → working capital
            → provisional taxes → debt solve → final taxes → cash flow

Taxes are computed twice by design. The first pass has no interest figure yet;
once the debt solver returns, PBT is restated with actual interest and the tax
charge is recomputed. Without this the model would tax a PBT that never existed.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.domain.calc import cagr, safe_div
from .assumptions import ForecastAssumptions
from .capex import CapexForecast, CapexYear
from .cashflow import CashFlowYear, build_cash_flows
from .debt import DebtSolution, DebtYear, solve_debt_schedule
from .depreciation import DepreciationYear, build_schedule
from .margins import MarginForecast, MarginYear
from .revenue import RevenueForecast, RevenueYear
from .taxes import TaxForecast, TaxYear
from .working_capital import WorkingCapitalForecast, WorkingCapitalYear


@dataclass(frozen=True, slots=True)
class ForecastBase:
    """The last reported position the forecast grows from."""

    fiscal_year: int
    revenue: float
    ebitda: float
    net_block: float
    net_working_capital: float
    gross_debt: float
    cash: float
    shares_outstanding: float
    equity: float
    invested_capital: float


@dataclass(frozen=True, slots=True)
class ForecastYear:
    """A single projected period across all statements."""

    period: int
    fiscal_year: int

    revenue: float
    revenue_growth: float | None
    ebitda: float
    ebitda_margin: float
    depreciation: float
    ebit: float
    ebit_margin: float | None
    other_income: float
    interest_expense: float
    pbt: float
    tax_expense: float
    effective_tax_rate: float
    pat: float
    pat_margin: float | None
    eps: float | None

    net_working_capital: float
    change_in_nwc: float
    capex: float
    net_block: float
    gross_debt: float
    cash: float
    net_debt: float
    equity: float

    cfo: float
    cfi: float
    cff: float
    fcff: float
    fcfe: float
    free_cash_flow: float

    roe: float | None
    roce: float | None
    roic: float | None
    net_debt_ebitda: float | None
    interest_coverage: float | None
    reconciled: bool


@dataclass(frozen=True, slots=True)
class ForecastResult:
    """Complete forecast output for one scenario."""

    scenario: str
    base: ForecastBase
    years: list[ForecastYear]
    assumptions: ForecastAssumptions

    revenue_rows: list[RevenueYear]
    margin_rows: list[MarginYear]
    capex_rows: list[CapexYear]
    depreciation_rows: list[DepreciationYear]
    wc_rows: list[WorkingCapitalYear]
    tax_rows: list[TaxYear]
    debt_rows: list[DebtYear]
    cash_flow_rows: list[CashFlowYear]

    debt_converged: bool
    debt_iterations: int
    all_reconciled: bool

    # ------------------------------------------------------------ summary
    @property
    def revenue_cagr(self) -> float | None:
        if not self.years:
            return None
        return cagr(self.base.revenue, self.years[-1].revenue, len(self.years))

    @property
    def ebitda_cagr(self) -> float | None:
        if not self.years:
            return None
        return cagr(self.base.ebitda, self.years[-1].ebitda, len(self.years))

    @property
    def eps_cagr(self) -> float | None:
        if not self.years or self.years[-1].eps is None:
            return None
        base_eps = safe_div(self.base.ebitda * 0.5, self.base.shares_outstanding)
        if not base_eps:
            return None
        return cagr(base_eps, self.years[-1].eps, len(self.years))

    @property
    def terminal_year(self) -> ForecastYear | None:
        return self.years[-1] if self.years else None


class ForecastEngine:
    """Builds an integrated forecast from a base position and assumptions."""

    def __init__(self, base: ForecastBase, assumptions: ForecastAssumptions) -> None:
        self.base = base
        self.a = assumptions

    def run(self) -> ForecastResult:
        a, base = self.a, self.base

        # 1. Top line ----------------------------------------------------
        revenue_fc = RevenueForecast(base.revenue, base.fiscal_year, a)
        revenue_rows = revenue_fc.project()

        # 2. Capex and the asset base (drives depreciation) ---------------
        capex_fc = CapexForecast(base.net_block, a)
        capex_rows = capex_fc.project(revenue_rows)
        depreciation = [c.depreciation for c in capex_rows]

        # 3. Margins, using the asset-derived depreciation -----------------
        margin_rows = MarginForecast(a).project(revenue_rows, depreciation)

        # 4. Working capital ----------------------------------------------
        wc_rows = WorkingCapitalForecast(base.net_working_capital, a).project(margin_rows)

        # 5. Provisional tax, pre-interest, to seed the debt solver --------
        tax_fc = TaxForecast(a)
        provisional = [
            tax_fc.compute(m.period, m.fiscal_year, m.ebit + m.other_income, m.ebit, 0.0)
            for m in margin_rows
        ]

        # Cash generated before any financing decision.
        cash_before_financing = [
            provisional[i].pat + margin_rows[i].depreciation
            + wc_rows[i].change_in_nwc - capex_rows[i].capex
            for i in range(len(margin_rows))
        ]
        dividends = [
            provisional[i].pat * a.dividend_payout.at(margin_rows[i].period)
            for i in range(len(margin_rows))
        ]

        # 6. Resolve the debt/interest/cash circularity --------------------
        solution: DebtSolution = solve_debt_schedule(
            opening_debt=base.gross_debt,
            opening_cash=base.cash,
            assumptions=a,
            revenue=[m.revenue for m in margin_rows],
            cash_before_financing=cash_before_financing,
            dividends=dividends,
        )
        # slots dataclasses have no __dict__; rebuild with the fiscal year
        debt_rows = [
            DebtYear(
                period=d.period,
                fiscal_year=margin_rows[i].fiscal_year,
                opening_debt=d.opening_debt,
                scheduled_repayment=d.scheduled_repayment,
                new_borrowing=d.new_borrowing,
                cash_sweep_repayment=d.cash_sweep_repayment,
                closing_debt=d.closing_debt,
                average_debt=d.average_debt,
                interest_expense=d.interest_expense,
                opening_cash=d.opening_cash,
                closing_cash=d.closing_cash,
                interest_income=d.interest_income,
                net_debt=d.net_debt,
                effective_rate=d.effective_rate,
            )
            for i, d in enumerate(solution.years)
        ]

        # 7. Final tax, now that interest is known -------------------------
        tax_rows = [
            tax_fc.compute(
                m.period,
                m.fiscal_year,
                m.ebit + m.other_income + debt_rows[i].interest_income
                - debt_rows[i].interest_expense,
                m.ebit,
                debt_rows[i].interest_expense,
            )
            for i, m in enumerate(margin_rows)
        ]

        # 8. Equity roll-forward and invested capital ----------------------
        equity_levels: list[float] = []
        equity = base.equity
        for i, t in enumerate(tax_rows):
            equity += t.pat * (1 - a.dividend_payout.at(margin_rows[i].period))
            equity_levels.append(equity)

        invested_capital = [
            equity_levels[i] + debt_rows[i].closing_debt - debt_rows[i].closing_cash
            for i in range(len(tax_rows))
        ]

        # 9. Cash flow, FCFF and FCFE --------------------------------------
        cash_flow_rows = build_cash_flows(
            assumptions=a,
            margins=margin_rows,
            capex_rows=capex_rows,
            wc_rows=wc_rows,
            tax_rows=tax_rows,
            debt_rows=debt_rows,
            invested_capital=invested_capital,
        )

        # 10. Flatten into the presentation view ---------------------------
        years: list[ForecastYear] = []
        prior_equity = base.equity
        for i, m in enumerate(margin_rows):
            t, d, w, c, cf = tax_rows[i], debt_rows[i], wc_rows[i], capex_rows[i], cash_flow_rows[i]
            eps = safe_div(t.pat, base.shares_outstanding)
            avg_equity = (prior_equity + equity_levels[i]) / 2
            capital_employed = equity_levels[i] + d.closing_debt

            years.append(
                ForecastYear(
                    period=m.period,
                    fiscal_year=m.fiscal_year,
                    revenue=m.revenue,
                    revenue_growth=revenue_rows[i].growth,
                    ebitda=m.ebitda,
                    ebitda_margin=m.ebitda_margin,
                    depreciation=m.depreciation,
                    ebit=m.ebit,
                    ebit_margin=m.ebit_margin,
                    other_income=m.other_income,
                    interest_expense=d.interest_expense,
                    pbt=t.pbt,
                    tax_expense=t.tax_expense,
                    effective_tax_rate=t.effective_rate,
                    pat=t.pat,
                    pat_margin=safe_div(t.pat, m.revenue),
                    eps=eps,
                    net_working_capital=w.net_working_capital,
                    change_in_nwc=w.change_in_nwc,
                    capex=c.capex,
                    net_block=c.closing_net_block,
                    gross_debt=d.closing_debt,
                    cash=d.closing_cash,
                    net_debt=d.net_debt,
                    equity=equity_levels[i],
                    cfo=cf.cfo,
                    cfi=cf.cfi,
                    cff=cf.cff,
                    fcff=cf.fcff,
                    fcfe=cf.fcfe,
                    free_cash_flow=cf.free_cash_flow,
                    roe=safe_div(t.pat, avg_equity),
                    roce=safe_div(m.ebit, capital_employed),
                    roic=cf.roic,
                    net_debt_ebitda=safe_div(d.net_debt, m.ebitda),
                    interest_coverage=safe_div(m.ebit, d.interest_expense),
                    reconciled=cf.reconciled,
                )
            )
            prior_equity = equity_levels[i]

        return ForecastResult(
            scenario=a.scenario.value,
            base=base,
            years=years,
            assumptions=a,
            revenue_rows=revenue_rows,
            margin_rows=margin_rows,
            capex_rows=capex_rows,
            depreciation_rows=build_schedule(capex_rows),
            wc_rows=wc_rows,
            tax_rows=tax_rows,
            debt_rows=debt_rows,
            cash_flow_rows=cash_flow_rows,
            debt_converged=solution.converged,
            debt_iterations=solution.iterations,
            all_reconciled=all(c.reconciled for c in cash_flow_rows),
        )
