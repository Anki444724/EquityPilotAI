"""Forecast endpoints.

    GET  /company/{ticker}/forecast              run the active (or default) forecast
    POST /company/{ticker}/forecast              create a saved forecast
    PUT  /company/{ticker}/forecast/assumptions  edit assumption drivers
    GET  /company/{ticker}/forecast/scenarios    bull / base / bear comparison
    GET  /company/{ticker}/forecast/list         saved forecasts

Handlers resolve, delegate and serialise. No projection arithmetic lives here.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.v1.analysis import get_analysis
from app.core.security import CurrentUser, get_current_user
from app.db.base import get_db
from app.domain.forecast.assumptions import (
    ForecastAssumptions, Provenance, RevenueMethod, Scenario,
)
from app.domain.forecast.engine import ForecastResult
from app.schemas.common import MetricRow, MetricSection, PeriodMeta, Unit
from app.schemas.forecast import (
    AssumptionSet, AssumptionUpdateRequest, DriverOut, ForecastCreateRequest,
    ForecastListItem, ForecastListResponse, ForecastResponse, ForecastSummary,
    ForecastYearOut, HistoricalYearOut, ScenarioComparisonRow, ScenarioOutcomeOut,
    ScenarioResponse,
)
from app.services.analysis_service import AnalysisService, fiscal_label
from app.services.forecast.metadata import GROUP_ORDER, meta_for
from app.services.forecast.service import ForecastError, ForecastService
from app.services.live_market import LiveMarketService

router = APIRouter(prefix="/company", tags=["forecast"])


def _service(db: Session = Depends(get_db)) -> ForecastService:
    return ForecastService(db)


def _assumption_set(a: ForecastAssumptions) -> AssumptionSet:
    drivers: list[DriverOut] = []
    for name in a.driver_names():
        d = a.get(name)
        label, unit, group = meta_for(name)
        drivers.append(
            DriverOut(
                name=name, label=label, value=d.value, unit=unit, group=group,
                source=d.source.value, citation=d.citation, note=d.note,
                by_year=dict(d.by_year),
            )
        )
    drivers.sort(key=lambda d: (GROUP_ORDER.index(d.group) if d.group in GROUP_ORDER else 99,
                                d.label))
    return AssumptionSet(
        scenario=a.scenario.value,
        horizon_years=a.years,
        revenue_method=a.revenue_method.value,
        drivers=drivers,
        provenance=a.provenance_summary(),
    )


def _sections(result: ForecastResult) -> list[MetricSection]:
    """Grid view of the projection, reusing the Module 2 row contract."""
    years = result.years

    def row(key, label, fn, unit=Unit.CRORE, subtotal=False, indent=0, note=None):
        return MetricRow(
            key=key, label=label, unit=unit, values=[fn(y) for y in years],
            is_subtotal=subtotal, indent=indent, note=note,
        )

    return [
        MetricSection(key="income", title="Projected income statement", rows=[
            row("revenue", "Revenue", lambda y: y.revenue, subtotal=True),
            row("revenue_growth", "Revenue growth", lambda y: y.revenue_growth, Unit.PERCENT),
            row("ebitda", "EBITDA", lambda y: y.ebitda, subtotal=True),
            row("ebitda_margin", "EBITDA margin", lambda y: y.ebitda_margin, Unit.PERCENT),
            row("depreciation", "Depreciation & amortisation", lambda y: y.depreciation, indent=1),
            row("ebit", "EBIT", lambda y: y.ebit, subtotal=True),
            row("ebit_margin", "EBIT margin", lambda y: y.ebit_margin, Unit.PERCENT),
            row("other_income", "Other income", lambda y: y.other_income, indent=1),
            row("interest_expense", "Interest expense", lambda y: y.interest_expense, indent=1),
            row("pbt", "Profit before tax", lambda y: y.pbt, subtotal=True),
            row("tax_expense", "Tax expense", lambda y: y.tax_expense, indent=1),
            row("pat", "Profit after tax", lambda y: y.pat, subtotal=True),
            row("pat_margin", "PAT margin", lambda y: y.pat_margin, Unit.PERCENT),
            row("eps", "EPS", lambda y: y.eps, Unit.RUPEES, subtotal=True),
        ]),
        MetricSection(key="capital", title="Capital & balance sheet", rows=[
            row("capex", "Capital expenditure", lambda y: y.capex),
            row("net_block", "Net block", lambda y: y.net_block),
            row("net_working_capital", "Net working capital", lambda y: y.net_working_capital),
            row("change_in_nwc", "Change in NWC", lambda y: y.change_in_nwc,
                note="Positive releases cash; negative absorbs it."),
            row("gross_debt", "Gross debt", lambda y: y.gross_debt),
            row("cash", "Cash & equivalents", lambda y: y.cash),
            row("net_debt", "Net debt", lambda y: y.net_debt, subtotal=True),
            row("equity", "Shareholders' equity", lambda y: y.equity),
        ]),
        MetricSection(key="cashflow", title="Projected cash flow", rows=[
            row("cfo", "Cash flow from operations", lambda y: y.cfo, subtotal=True),
            row("cfi", "Cash flow from investing", lambda y: y.cfi, subtotal=True),
            row("cff", "Cash flow from financing", lambda y: y.cff, subtotal=True),
            row("free_cash_flow", "Free cash flow (CFO − capex)", lambda y: y.free_cash_flow),
            row("fcff", "Free cash flow to firm (FCFF)", lambda y: y.fcff, subtotal=True),
            row("fcfe", "Free cash flow to equity (FCFE)", lambda y: y.fcfe, subtotal=True),
        ]),
        MetricSection(key="returns", title="Returns & leverage", rows=[
            row("roe", "Return on equity", lambda y: y.roe, Unit.PERCENT),
            row("roce", "Return on capital employed", lambda y: y.roce, Unit.PERCENT),
            row("roic", "Return on invested capital", lambda y: y.roic, Unit.PERCENT),
            row("net_debt_ebitda", "Net debt / EBITDA", lambda y: y.net_debt_ebitda, Unit.MULTIPLE),
            row("interest_coverage", "Interest coverage", lambda y: y.interest_coverage, Unit.MULTIPLE),
        ]),
    ]


def _history(analysis: AnalysisService, limit: int = 10) -> list[HistoricalYearOut]:
    out = []
    for inc, cf in list(zip(analysis.incomes, analysis.cash_flows))[-limit:]:
        out.append(HistoricalYearOut(
            fiscal_year=inc.fiscal_year, revenue=inc.total_revenue, ebitda=inc.ebitda,
            ebitda_margin=inc.ebitda_margin, pat=inc.pat, eps=inc.eps_basic,
            free_cash_flow=cf.free_cash_flow,
        ))
    return out


def _periods(result: ForecastResult) -> PeriodMeta:
    years = [y.fiscal_year for y in result.years]
    return PeriodMeta(
        fiscal_years=years,
        labels=[fiscal_label(y) for y in years],
        latest_fiscal_year=years[-1] if years else None,
    )


def _summary(result: ForecastResult) -> ForecastSummary:
    t = result.terminal_year
    return ForecastSummary(
        revenue_cagr=result.revenue_cagr,
        ebitda_cagr=result.ebitda_cagr,
        terminal_revenue=t.revenue if t else None,
        terminal_ebitda=t.ebitda if t else None,
        terminal_eps=t.eps if t else None,
        terminal_fcff=t.fcff if t else None,
        debt_converged=result.debt_converged,
        debt_iterations=result.debt_iterations,
        all_reconciled=result.all_reconciled,
    )


def _warnings(result: ForecastResult) -> list[str]:
    out: list[str] = []
    if not result.debt_converged:
        out.append("Debt schedule did not converge; interest figures are approximate.")
    if not result.all_reconciled:
        out.append("FCFF builds disagree — a projection schedule is inconsistent.")
    return out


# --------------------------------------------------------------------- GET
@router.get("/{ticker}/forecast", response_model=ForecastResponse,
            summary="Run a forecast")
def get_forecast(
    scenario: Scenario = Query(Scenario.BASE),
    horizon: int | None = Query(None, description="3, 5 or 10; overrides the saved horizon"),
    method: RevenueMethod | None = Query(None),
    forecast_id: str | None = Query(None),
    analysis: AnalysisService = Depends(get_analysis),
    svc: ForecastService = Depends(_service),
) -> ForecastResponse:
    if not analysis.has_data:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "no financial history; cannot build a forecast")
    saved = svc.get(forecast_id) if forecast_id else svc.active_for_company(analysis.company.id)
    years = horizon or (saved.horizon_years if saved else 5)
    if years not in (3, 5, 10):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "horizon must be 3, 5 or 10")

    ctx = svc.build_context(
        analysis.company, analysis.statements, years=years,
        method=method or (RevenueMethod(saved.revenue_method) if saved else RevenueMethod.CAGR),
    )
    if horizon or method:
        saved = None  # ad-hoc run; do not apply the saved horizon/method
    result = svc.run(ctx, saved, scenario)

    return ForecastResponse(
        company=analysis.company_ref(),
        forecast_id=saved.id if saved else None,
        name=saved.name if saved else "Ad-hoc forecast",
        scenario=scenario.value,
        periods=_periods(result),
        base_fiscal_year=ctx.base.fiscal_year,
        years=[ForecastYearOut.model_validate(y, from_attributes=True) for y in result.years],
        history=_history(analysis),
        assumptions=_assumption_set(result.assumptions),
        summary=_summary(result),
        sections=_sections(result),
        warnings=_warnings(result),
    )


@router.get("/{ticker}/forecast/scenarios", response_model=ScenarioResponse,
            summary="Bull / base / bear comparison")
def get_scenarios(
    horizon: int | None = Query(None),
    forecast_id: str | None = Query(None),
    analysis: AnalysisService = Depends(get_analysis),
    svc: ForecastService = Depends(_service),
    db: Session = Depends(get_db),
) -> ScenarioResponse:
    if not analysis.has_data:
        raise HTTPException(status.HTTP_409_CONFLICT, "no financial history")
    saved = svc.get(forecast_id) if forecast_id else svc.active_for_company(analysis.company.id)
    years = horizon or (saved.horizon_years if saved else 5)
    if years not in (3, 5, 10):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "horizon must be 3, 5 or 10")

    ctx = svc.build_context(analysis.company, analysis.statements, years=years)
    if horizon:
        saved = None
    # The scenario upside / verdict is computed against the current market
    # price from the shared LiveMarketService, so it can never diverge from the
    # live price shown on every other page.
    cmp_price = LiveMarketService(db).price_for(analysis.company)
    result = svc.run_all_scenarios(ctx, saved, cmp_price=cmp_price)

    base = result.results["base"]
    series = {
        name: {
            "revenue": [y.revenue for y in res.years],
            "ebitda": [y.ebitda for y in res.years],
            "pat": [y.pat for y in res.years],
            "eps": [y.eps for y in res.years],
            "fcff": [y.fcff for y in res.years],
        }
        for name, res in result.results.items()
    }
    comparison = [
        ScenarioComparisonRow(
            key=key, label=label, unit=unit,
            bear=series["bear"][key], base=series["base"][key], bull=series["bull"][key],
        )
        for key, label, unit in (
            ("revenue", "Revenue", Unit.CRORE),
            ("ebitda", "EBITDA", Unit.CRORE),
            ("pat", "PAT", Unit.CRORE),
            ("eps", "EPS", Unit.RUPEES),
            ("fcff", "FCFF", Unit.CRORE),
        )
    ]

    return ScenarioResponse(
        company=analysis.company_ref(),
        forecast_id=saved.id if saved else None,
        periods=_periods(base),
        outcomes=[
            ScenarioOutcomeOut(
                scenario=o.scenario, probability=o.probability,
                terminal_revenue=o.terminal_revenue, terminal_ebitda=o.terminal_ebitda,
                terminal_eps=o.terminal_eps, revenue_cagr=o.revenue_cagr,
                terminal_fcff=o.terminal_fcff, value_per_share=o.value_per_share,
                upside=o.upside,
            )
            for o in result.outcomes
        ],
        comparison=comparison,
        expected_value=result.expected_value,
        expected_upside=result.expected_upside,
        bull_upside=result.bull_upside,
        bear_downside=result.bear_downside,
        risk_reward=result.risk_reward,
        standard_deviation=result.standard_deviation,
        coefficient_of_variation=result.coefficient_of_variation,
        verdict=result.verdict,
        current_price=cmp_price,
    )


@router.get("/{ticker}/forecast/list", response_model=ForecastListResponse,
            summary="Saved forecasts")
def list_forecasts(
    analysis: AnalysisService = Depends(get_analysis),
    svc: ForecastService = Depends(_service),
) -> ForecastListResponse:
    return ForecastListResponse(
        company=analysis.company_ref(),
        forecasts=[
            ForecastListItem(
                id=f.id, name=f.name, horizon_years=f.horizon_years,
                revenue_method=f.revenue_method, status=f.status, revision=f.revision,
            )
            for f in svc.list_for_company(analysis.company.id)
        ],
    )


# -------------------------------------------------------------------- POST
@router.post("/{ticker}/forecast", response_model=ForecastResponse,
             status_code=status.HTTP_201_CREATED, summary="Create a saved forecast")
def create_forecast(
    body: ForecastCreateRequest,
    analysis: AnalysisService = Depends(get_analysis),
    svc: ForecastService = Depends(_service),
    user: CurrentUser = Depends(get_current_user),
) -> ForecastResponse:
    if not analysis.has_data:
        raise HTTPException(status.HTTP_409_CONFLICT, "no financial history")
    try:
        saved = svc.create(
            company_id=analysis.company.id, name=body.name,
            horizon_years=body.horizon_years, revenue_method=body.revenue_method,
            segments=body.segments, created_by=user.id, notes=body.notes,
        )
        if body.drivers:
            svc.update_assumptions(saved, body.drivers, scenario=None)
    except ForecastError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    ctx = svc.build_context(
        analysis.company, analysis.statements,
        years=saved.horizon_years, method=RevenueMethod(saved.revenue_method),
    )
    result = svc.run(ctx, saved, Scenario.BASE)
    return ForecastResponse(
        company=analysis.company_ref(), forecast_id=saved.id, name=saved.name,
        scenario="base", periods=_periods(result), base_fiscal_year=ctx.base.fiscal_year,
        years=[ForecastYearOut.model_validate(y, from_attributes=True) for y in result.years],
        history=_history(analysis), assumptions=_assumption_set(result.assumptions),
        summary=_summary(result), sections=_sections(result), warnings=_warnings(result),
    )


# --------------------------------------------------------------------- PUT
@router.put("/{ticker}/forecast/assumptions", response_model=ForecastResponse,
            summary="Update assumption drivers")
def update_assumptions(
    body: AssumptionUpdateRequest,
    forecast_id: str | None = Query(None),
    analysis: AnalysisService = Depends(get_analysis),
    svc: ForecastService = Depends(_service),
    user: CurrentUser = Depends(get_current_user),
) -> ForecastResponse:
    if not analysis.has_data:
        raise HTTPException(status.HTTP_409_CONFLICT, "no financial history")

    saved = svc.get(forecast_id) if forecast_id else svc.active_for_company(analysis.company.id)
    if saved is None:
        # First edit implicitly creates a forecast to hold it.
        saved = svc.create(analysis.company.id, "Base forecast", created_by=user.id)

    if body.horizon_years or body.revenue_method:
        if body.horizon_years:
            saved.horizon_years = body.horizon_years
        if body.revenue_method:
            saved.revenue_method = body.revenue_method
        svc.db.commit()

    try:
        source = Provenance(body.source)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            f"unknown source '{body.source}'") from exc

    try:
        svc.update_assumptions(
            saved, body.drivers,
            scenario=Scenario(body.scenario) if body.scenario else None,
            source=source, citation=body.citation,
            requires_review=body.requires_review,
            by_year={k: {int(p): v for p, v in d.items()} for k, d in body.by_year.items()},
        )
    except ForecastError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    ctx = svc.build_context(
        analysis.company, analysis.statements,
        years=saved.horizon_years, method=RevenueMethod(saved.revenue_method),
    )
    scenario = Scenario(body.scenario) if body.scenario else Scenario.BASE
    result = svc.run(ctx, saved, scenario)
    return ForecastResponse(
        company=analysis.company_ref(), forecast_id=saved.id, name=saved.name,
        scenario=scenario.value, periods=_periods(result),
        base_fiscal_year=ctx.base.fiscal_year,
        years=[ForecastYearOut.model_validate(y, from_attributes=True) for y in result.years],
        history=_history(analysis), assumptions=_assumption_set(result.assumptions),
        summary=_summary(result), sections=_sections(result), warnings=_warnings(result),
    )
