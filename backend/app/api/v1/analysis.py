"""Module 2 analysis endpoints.

URL scheme is ticker-based per the specification:

    GET /api/v1/company/{ticker}/income-statement
    GET /api/v1/company/{ticker}/balance-sheet
    GET /api/v1/company/{ticker}/cash-flow
    GET /api/v1/company/{ticker}/ratios
    GET /api/v1/company/{ticker}/working-capital
    GET /api/v1/company/{ticker}/debt
    GET /api/v1/company/{ticker}/capex
    GET /api/v1/company/{ticker}/shareholding

Every handler is a thin adapter: resolve → delegate to a service → serialise.
No financial logic lives in this layer.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.security import CurrentUser, get_current_user
from app.db.base import get_db
from app.schemas.analysis import (
    CapexResponse, CovenantRow, DebtInstrumentRow, DebtReconciliation,
    DebtResponse, FinancialsOverview, MaturityBucket, OwnershipSignal,
    RatioResponse, ShareholdingResponse, StatementResponse, StatementSummary,
    WorkingCapitalResponse,
)
from app.schemas.common import Unit
from app.services.analysis_service import AnalysisService
from app.services.ratios.service import RatioService

router = APIRouter(prefix="/company", tags=["analysis"])


def get_analysis(
    ticker: str,
    db: Session = Depends(get_db),
    _: CurrentUser = Depends(get_current_user),
) -> AnalysisService:
    """Resolve a ticker to a fully loaded analysis context (single query)."""
    svc = AnalysisService.for_ticker(db, ticker)
    if svc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"company '{ticker}' not found")
    return svc


# --------------------------------------------------------------- statements
@router.get("/{ticker}/income-statement", response_model=StatementResponse,
            summary="Historical income statement")
def income_statement(svc: AnalysisService = Depends(get_analysis)) -> StatementResponse:
    return StatementResponse(
        statement="income_statement",
        company=svc.company_ref(),
        periods=svc.periods(),
        sections=svc.statements.income_statement_sections(),
        has_data=svc.has_data,
    )


@router.get("/{ticker}/balance-sheet", response_model=StatementResponse,
            summary="Historical balance sheet")
def balance_sheet(svc: AnalysisService = Depends(get_analysis)) -> StatementResponse:
    return StatementResponse(
        statement="balance_sheet",
        company=svc.company_ref(),
        periods=svc.periods(),
        sections=svc.statements.balance_sheet_sections(),
        has_data=svc.has_data,
        warnings=svc.statements.balance_warnings(),
    )


@router.get("/{ticker}/cash-flow", response_model=StatementResponse,
            summary="Historical cash-flow statement")
def cash_flow(svc: AnalysisService = Depends(get_analysis)) -> StatementResponse:
    return StatementResponse(
        statement="cash_flow",
        company=svc.company_ref(),
        periods=svc.periods(),
        sections=svc.statements.cash_flow_sections(),
        has_data=svc.has_data,
    )


@router.get("/{ticker}/financials", response_model=FinancialsOverview,
            summary="Headline financial summary across all periods")
def financials_overview(svc: AnalysisService = Depends(get_analysis)) -> FinancialsOverview:
    ratios = RatioService(svc.incomes, svc.balances, svc.cash_flows)
    roe = next(r for r in ratios.return_ratios().rows if r.key == "roe_avg").values
    roce = next(r for r in ratios.return_ratios().rows if r.key == "roce").values

    summary = [
        StatementSummary(
            fiscal_year=inc.fiscal_year,
            revenue=inc.total_revenue,
            ebitda=inc.ebitda,
            ebitda_margin=inc.ebitda_margin,
            pat=inc.pat,
            pat_margin=inc.pat_margin,
            eps=inc.eps_basic,
            cfo=cf.cfo,
            free_cash_flow=cf.free_cash_flow,
            net_debt=bal.net_debt,
            total_assets=bal.total_assets,
            roe=roe[i],
            roce=roce[i],
            balance_sheet_ties=bal.balances,
        )
        for i, (inc, bal, cf) in enumerate(zip(svc.incomes, svc.balances, svc.cash_flows))
    ]
    return FinancialsOverview(
        company=svc.company_ref(),
        periods=svc.periods(),
        summary=summary,
        revenue_cagr_5y=svc.statements.revenue_cagr(5),
        revenue_cagr_full=svc.statements.revenue_cagr(),
        has_data=svc.has_data,
        warnings=svc.statements.balance_warnings(),
    )


# ------------------------------------------------------------------- ratios
@router.get("/{ticker}/ratios", response_model=RatioResponse,
            summary="Full ratio suite (45+ ratios, six families)")
def ratios(
    svc: AnalysisService = Depends(get_analysis),
    wacc: float | None = Query(
        None, ge=0, le=1,
        description="WACC as a fraction, e.g. 0.12. Enables ROIC spread and EVA.",
    ),
) -> RatioResponse:
    return RatioResponse(
        company=svc.company_ref(),
        periods=svc.periods(unit=Unit.RATIO),
        sections=svc.ratios(wacc).all_sections(),
        has_data=svc.has_data,
        wacc_assumption=wacc,
    )


# ---------------------------------------------------------- working capital
@router.get("/{ticker}/working-capital", response_model=WorkingCapitalResponse,
            summary="Working-capital components, cycle days and intensity")
def working_capital(svc: AnalysisService = Depends(get_analysis)) -> WorkingCapitalResponse:
    wc = svc.working_capital()
    return WorkingCapitalResponse(
        company=svc.company_ref(),
        periods=svc.periods(),
        sections=wc.all_sections(),
        has_data=svc.has_data,
        flags=wc.flags(),
        cost_of_debt_assumption=svc.implied_cost_of_debt,
    )


# -------------------------------------------------------------------- capex
@router.get("/{ticker}/capex", response_model=CapexResponse,
            summary="Capex split, intensity and efficiency")
def capex(svc: AnalysisService = Depends(get_analysis)) -> CapexResponse:
    return CapexResponse(
        company=svc.company_ref(),
        periods=svc.periods(),
        sections=svc.capex().all_sections(),
        has_data=svc.has_data,
    )


# --------------------------------------------------------------------- debt
@router.get("/{ticker}/debt", response_model=DebtResponse,
            summary="Debt profile, maturity ladder and covenant headroom")
def debt(svc: AnalysisService = Depends(get_analysis)) -> DebtResponse:
    d = svc.debt()
    return DebtResponse(
        company=svc.company_ref(),
        periods=svc.periods(),
        sections=d.all_sections(),
        has_data=svc.has_data,
        instruments=[DebtInstrumentRow(**row) for row in d.instrument_schedule()],
        maturity_ladder=[MaturityBucket(**b) for b in d.maturity_ladder()],
        covenants=[
            CovenantRow(
                key=c.key, label=c.label, threshold=c.threshold, actual=c.actual,
                direction=c.direction, unit=c.unit,
                compliant=c.compliant, headroom=c.headroom,
            )
            for c in d.covenants()
        ],
        reconciliation=DebtReconciliation(**d.reconciliation()),
        blended_rate=d.blended_rate(),
        floating_rate_share=d.floating_share(),
        foreign_currency_share=d.foreign_currency_share(),
        flags=d.flags(),
    )


# ------------------------------------------------------------- shareholding
@router.get("/{ticker}/shareholding", response_model=ShareholdingResponse,
            summary="Shareholding pattern, pledge and ownership trend")
def shareholding(svc: AnalysisService = Depends(get_analysis)) -> ShareholdingResponse:
    sh = svc.shareholding()
    labels = sh.labels()
    periods = svc.periods(unit=Unit.PERCENT)
    periods.labels = labels
    periods.fiscal_years = [s.fiscal_year for s in sh.snaps]

    return ShareholdingResponse(
        company=svc.company_ref(),
        periods=periods,
        sections=sh.all_sections(),
        has_data=bool(sh.snaps),
        signal=OwnershipSignal(**sh.ownership_signal()) if sh.snaps else None,
        flags=sh.flags(),
    )
