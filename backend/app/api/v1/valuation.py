"""Valuation endpoints.

    GET  /company/{ticker}/valuation              all methodologies + summary
    GET  /company/{ticker}/valuation/wacc         WACC build and schedule
    GET  /company/{ticker}/valuation/dcf          FCFF or FCFE detail
    GET  /company/{ticker}/valuation/relative     multiples and justified multiples
    GET  /company/{ticker}/valuation/sensitivity  two-way grid
    GET  /company/{ticker}/valuation/simulation   Monte Carlo
    POST /company/{ticker}/valuation/sotp         sum-of-the-parts

Every response carries a data-quality block. When the underlying data is not
sourced from filings the response includes an explicit disclosure, so no caller
can present an unqualified figure by accident.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.v1.analysis import get_analysis
from app.core.security import CurrentUser, get_current_user
from app.db.base import get_db
from app.domain.forecast.assumptions import Scenario
from app.domain.valuation.data_quality import DataQualityReport
from app.domain.valuation.dcf import DCFResult, DiscountConvention, TerminalMethod
from app.domain.valuation.sotp import SOTPSegment, SegmentBasis, run_sotp
from app.schemas.valuation import (
    DataQualityOut, DCFOut, DCFYearOut, DDMOut, HistogramBucket,
    JustifiedMultipleOut, MethodValueOut, MultipleSetOut, QualityIssueOut,
    RelativeOut, ReplacementOut, SensitivityOut, SimulationOut, SOTPOut,
    SOTPRequest, SOTPSegmentOut, SummaryOut, TargetPriceOut, ValuationResponse,
    WACCOut, WACCScheduleRow,
)
from app.services.analysis_service import AnalysisService
from app.services.forecast.service import ForecastService
from app.services.valuation.service import ValuationBundle, ValuationService

router = APIRouter(prefix="/company", tags=["valuation"])


def _services(db: Session = Depends(get_db)) -> tuple[ValuationService, ForecastService]:
    return ValuationService(db), ForecastService(db)


def _require_data(analysis: AnalysisService) -> None:
    if not analysis.has_data:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "no financial history; a valuation cannot be produced",
        )


def _quality_out(report: DataQualityReport) -> DataQualityOut:
    return DataQualityOut(
        grade=report.grade.value,
        is_illustrative=report.is_illustrative,
        disclosure=report.disclosure,
        headline=report.headline,
        issues=[
            QualityIssueOut(key=i.key, message=i.message, severity=i.severity.value,
                            detail=i.detail)
            for i in report.issues
        ],
        coverage=report.coverage,
        history_years=report.history_years,
        synthetic_sources=report.synthetic_sources,
    )


def _dcf_out(result: DCFResult, model: str) -> DCFOut:
    return DCFOut(
        model=model,
        convention=result.convention,
        terminal_method=result.terminal_method,
        years=[DCFYearOut(**y.__dict__) if hasattr(y, "__dict__") else DCFYearOut(
            period=y.period, cash_flow=y.cash_flow, discount_period=y.discount_period,
            discount_rate=y.discount_rate, discount_factor=y.discount_factor,
            present_value=y.present_value,
        ) for y in result.years],
        sum_pv_explicit=result.sum_pv_explicit,
        terminal_value=result.terminal_value,
        pv_terminal_value=result.pv_terminal_value,
        terminal_value_pct=result.terminal_value_pct,
        enterprise_value=result.enterprise_value,
        net_debt=result.net_debt,
        equity_value=result.equity_value,
        shares_outstanding=result.shares_outstanding,
        intrinsic_value_per_share=result.intrinsic_value_per_share,
        current_price=result.current_price,
        upside=result.upside,
        margin_of_safety=result.margin_of_safety,
        maximum_buy_price=result.maximum_buy_price,
        in_buy_zone=result.in_buy_zone,
        discount_rate=result.discount_rate,
        terminal_growth=result.terminal_growth,
        implied_exit_multiple=result.implied_exit_multiple,
        implied_perpetual_growth=result.implied_perpetual_growth,
        warnings=result.warnings,
    )


def _bundle_response(
    analysis: AnalysisService, bundle: ValuationBundle,
    scenario: Scenario, horizon: int,
    convention: DiscountConvention, terminal_method: TerminalMethod,
) -> ValuationResponse:
    rel = bundle.relative
    return ValuationResponse(
        company=analysis.company_ref(),
        scenario=scenario.value,
        horizon_years=horizon,
        convention=convention.value,
        terminal_method=terminal_method.value,
        wacc=WACCOut(**bundle.wacc.__dict__) if hasattr(bundle.wacc, "__dict__")
        else WACCOut(
            risk_free_rate=bundle.wacc.risk_free_rate, total_erp=bundle.wacc.total_erp,
            unlevered_beta=bundle.wacc.unlevered_beta, levered_beta=bundle.wacc.levered_beta,
            regression_beta=bundle.wacc.regression_beta, beta_used=bundle.wacc.beta_used,
            beta_source=bundle.wacc.beta_source, size_premium=bundle.wacc.size_premium,
            specific_premium=bundle.wacc.specific_premium,
            cost_of_equity=bundle.wacc.cost_of_equity,
            pre_tax_cost_of_debt=bundle.wacc.pre_tax_cost_of_debt,
            marginal_tax_rate=bundle.wacc.marginal_tax_rate,
            after_tax_cost_of_debt=bundle.wacc.after_tax_cost_of_debt,
            market_value_equity=bundle.wacc.market_value_equity,
            market_value_debt=bundle.wacc.market_value_debt,
            total_capital=bundle.wacc.total_capital,
            weight_equity=bundle.wacc.weight_equity, weight_debt=bundle.wacc.weight_debt,
            debt_to_equity=bundle.wacc.debt_to_equity, wacc=bundle.wacc.wacc,
            bounded=bundle.wacc.bounded,
        ),
        wacc_schedule=[
            WACCScheduleRow(
                period=i + 1, debt_to_equity=s.debt_to_equity,
                levered_beta=s.levered_beta, cost_of_equity=s.cost_of_equity, wacc=s.wacc,
            )
            for i, s in enumerate(bundle.wacc_schedule)
        ],
        dcf_fcff=_dcf_out(bundle.dcf_fcff, "fcff"),
        dcf_fcfe=_dcf_out(bundle.dcf_fcfe, "fcfe"),
        relative=RelativeOut(
            current=MultipleSetOut(**rel.current.__dict__) if hasattr(rel.current, "__dict__")
            else MultipleSetOut(label=rel.current.label, pe=rel.current.pe, pb=rel.current.pb,
                                ev_ebitda=rel.current.ev_ebitda, ev_sales=rel.current.ev_sales,
                                ev_ebit=rel.current.ev_ebit, p_fcfe=rel.current.p_fcfe,
                                dividend_yield=rel.current.dividend_yield, peg=rel.current.peg),
            forward=[
                MultipleSetOut(label=f.label, pe=f.pe, pb=f.pb, ev_ebitda=f.ev_ebitda,
                               ev_sales=f.ev_sales, ev_ebit=f.ev_ebit, p_fcfe=f.p_fcfe)
                for f in rel.forward
            ],
            methods=[
                TargetPriceOut(
                    key=m.key, label=m.label, basis=m.basis,
                    target_multiple=m.target_multiple, metric=m.metric,
                    metric_label=m.metric_label, implied_value=m.implied_value,
                    target_price=m.target_price, weight=m.weight, rationale=m.rationale,
                )
                for m in rel.methods
            ],
            justified=[
                JustifiedMultipleOut(
                    key=j.key, label=j.label, formula=j.formula, justified=j.justified,
                    actual=j.actual, premium_discount=j.premium_discount, verdict=j.verdict,
                )
                for j in rel.justified
            ],
            blended_target_price=rel.blended_target_price,
            simple_average_target=rel.simple_average_target,
            median_target=rel.median_target,
            target_low=rel.target_low, target_high=rel.target_high,
            upside=rel.upside, current_price=rel.current_price, warnings=rel.warnings,
        ),
        ddm=DDMOut(
            variant=bundle.ddm.variant, value_per_share=bundle.ddm.value_per_share,
            terminal_value=bundle.ddm.terminal_value, pv_explicit=bundle.ddm.pv_explicit,
            implied_dividend_yield=bundle.ddm.implied_dividend_yield,
            upside=bundle.ddm.upside, warnings=bundle.ddm.warnings,
        ),
        replacement=ReplacementOut(
            net_block=bundle.replacement.net_block,
            inflation_adjustment=bundle.replacement.inflation_adjustment,
            adjusted_fixed_assets=bundle.replacement.adjusted_fixed_assets,
            net_working_capital=bundle.replacement.net_working_capital,
            intangible_replacement=bundle.replacement.intangible_replacement,
            total_replacement_cost=bundle.replacement.total_replacement_cost,
            net_debt=bundle.replacement.net_debt,
            equity_replacement_value=bundle.replacement.equity_replacement_value,
            value_per_share=bundle.replacement.value_per_share,
            tobins_q=bundle.replacement.tobins_q, upside=bundle.replacement.upside,
            warnings=bundle.replacement.warnings,
        ),
        summary=SummaryOut(
            methods=[
                MethodValueOut(key=m.key, label=m.label, value_per_share=m.value_per_share,
                               upside=m.upside, weight=m.weight, applicable=m.applicable,
                               note=m.note)
                for m in bundle.summary.methods
            ],
            weighted_value=bundle.summary.weighted_value,
            median_value=bundle.summary.median_value,
            low=bundle.summary.low, high=bundle.summary.high,
            current_price=bundle.summary.current_price, upside=bundle.summary.upside,
            margin_of_safety=bundle.summary.margin_of_safety,
            maximum_buy_price=bundle.summary.maximum_buy_price,
            in_buy_zone=bundle.summary.in_buy_zone,
            recommendation=bundle.summary.recommendation,
        ),
        quality=_quality_out(bundle.quality),
        scenario_values=bundle.scenario_values,
        warnings=bundle.warnings,
    )


# ---------------------------------------------------------------------- main
@router.get("/{ticker}/valuation", response_model=ValuationResponse,
            summary="Full valuation across all methodologies")
def get_valuation(
    scenario: Scenario = Query(Scenario.BASE),
    horizon: int = Query(5, description="3, 5 or 10"),
    convention: DiscountConvention = Query(DiscountConvention.MID_YEAR),
    terminal_method: TerminalMethod = Query(TerminalMethod.PERPETUAL_GROWTH),
    terminal_growth: float | None = Query(None, ge=-0.02, le=0.10),
    exit_multiple: float | None = Query(None, gt=0, le=60),
    margin_of_safety: float = Query(0.20, ge=0, le=0.6),
    dynamic_wacc: bool = Query(False),
    analysis: AnalysisService = Depends(get_analysis),
    services: tuple[ValuationService, ForecastService] = Depends(_services),
) -> ValuationResponse:
    _require_data(analysis)
    if horizon not in (3, 5, 10):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "horizon must be 3, 5 or 10")

    valuation, forecast = services
    bundle = valuation.value_company(
        analysis, forecast, horizon=horizon, convention=convention,
        terminal_method=terminal_method, terminal_growth=terminal_growth,
        exit_multiple=exit_multiple, margin_of_safety=margin_of_safety,
        dynamic_wacc=dynamic_wacc, scenario=scenario,
    )
    return _bundle_response(analysis, bundle, scenario, horizon, convention, terminal_method)


@router.get("/{ticker}/valuation/wacc", response_model=ValuationResponse,
            summary="WACC build (full bundle, WACC-focused)")
def get_wacc(
    horizon: int = Query(5),
    dynamic_wacc: bool = Query(True),
    analysis: AnalysisService = Depends(get_analysis),
    services: tuple[ValuationService, ForecastService] = Depends(_services),
) -> ValuationResponse:
    _require_data(analysis)
    valuation, forecast = services
    bundle = valuation.value_company(
        analysis, forecast, horizon=horizon, dynamic_wacc=dynamic_wacc
    )
    return _bundle_response(
        analysis, bundle, Scenario.BASE, horizon,
        DiscountConvention.MID_YEAR, TerminalMethod.PERPETUAL_GROWTH,
    )


# --------------------------------------------------------------- sensitivity
@router.get("/{ticker}/valuation/sensitivity", response_model=SensitivityOut,
            summary="Two-way sensitivity grid")
def get_sensitivity(
    row: str = Query("wacc", description="wacc | terminal_growth | revenue_cagr | ebit_margin | exit_multiple"),
    col: str = Query("terminal_growth"),
    horizon: int = Query(5),
    steps: int = Query(2, ge=1, le=4),
    convention: DiscountConvention = Query(DiscountConvention.MID_YEAR),
    terminal_method: TerminalMethod = Query(TerminalMethod.PERPETUAL_GROWTH),
    analysis: AnalysisService = Depends(get_analysis),
    services: tuple[ValuationService, ForecastService] = Depends(_services),
) -> SensitivityOut:
    _require_data(analysis)
    valid = {"wacc", "terminal_growth", "revenue_cagr", "ebit_margin", "exit_multiple"}
    if row not in valid or col not in valid:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            f"axes must be among {sorted(valid)}")
    if row == col:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "row and column axes must differ")

    valuation, forecast_service = services
    context = forecast_service.build_context(
        analysis.company, analysis.statements, years=horizon
    )
    saved = forecast_service.active_for_company(analysis.company.id)
    forecast = forecast_service.run(context, saved, Scenario.BASE)

    wacc = valuation.build_wacc(analysis, forecast)
    from app.domain.valuation.wacc import compute_wacc
    wacc_result = compute_wacc(wacc)

    grid = valuation.sensitivity(
        analysis, forecast, wacc_result, row_key=row, col_key=col,
        convention=convention, terminal_method=terminal_method,
        terminal_growth=forecast.assumptions.terminal_growth.value,
        exit_multiple=forecast.assumptions.exit_ev_ebitda.value,
        steps=steps,
    )

    dcf = valuation.run_fcff(
        analysis, forecast, wacc_result, convention=convention,
        terminal_method=terminal_method,
        terminal_growth=forecast.assumptions.terminal_growth.value,
        exit_multiple=forecast.assumptions.exit_ev_ebitda.value,
        margin_of_safety=0.20,
    )
    relative = valuation.run_relative(
        analysis, forecast, wacc_result, dcf.intrinsic_value_per_share
    )
    summary = valuation.summarise(
        dcf_fcff=dcf, dcf_fcfe=dcf, relative=relative,
        ddm=valuation.run_ddm_model(analysis, forecast, wacc_result),
        replacement=valuation.run_replacement(analysis, forecast), sotp=None,
        current_price=analysis.company.current_price, margin_of_safety=0.20,
    )
    quality = valuation.grade(analysis, forecast, summary, relative, dcf)

    return SensitivityOut(
        company=analysis.company_ref(),
        row_key=grid.row_key, row_label=grid.row_label, row_unit=grid.row_unit,
        row_values=grid.row_values,
        col_key=grid.col_key, col_label=grid.col_label, col_unit=grid.col_unit,
        col_values=grid.col_values,
        cells=grid.cells, upside_cells=grid.upside_cells(),
        base_row=grid.base_row, base_col=grid.base_col, base_value=grid.base_value,
        minimum=grid.minimum, maximum=grid.maximum,
        current_price=grid.current_price,
        quality=_quality_out(quality),
    )


# ---------------------------------------------------------------- simulation
@router.get("/{ticker}/valuation/simulation", response_model=SimulationOut,
            summary="Monte Carlo valuation distribution")
def get_simulation(
    trials: int = Query(1000, ge=100, le=20000),
    horizon: int = Query(5),
    seed: int | None = Query(42),
    convention: DiscountConvention = Query(DiscountConvention.MID_YEAR),
    analysis: AnalysisService = Depends(get_analysis),
    services: tuple[ValuationService, ForecastService] = Depends(_services),
) -> SimulationOut:
    _require_data(analysis)
    valuation, forecast_service = services
    context = forecast_service.build_context(
        analysis.company, analysis.statements, years=horizon
    )
    saved = forecast_service.active_for_company(analysis.company.id)
    forecast = forecast_service.run(context, saved, Scenario.BASE)

    from app.domain.valuation.wacc import compute_wacc
    wacc_result = compute_wacc(valuation.build_wacc(analysis, forecast))

    sim = valuation.monte_carlo(
        analysis, forecast, wacc_result, convention=convention,
        terminal_growth=forecast.assumptions.terminal_growth.value,
        trials=trials, seed=seed,
    )

    dcf = valuation.run_fcff(
        analysis, forecast, wacc_result, convention=convention,
        terminal_method=TerminalMethod.PERPETUAL_GROWTH,
        terminal_growth=forecast.assumptions.terminal_growth.value,
        exit_multiple=forecast.assumptions.exit_ev_ebitda.value,
        margin_of_safety=0.20,
    )
    relative = valuation.run_relative(
        analysis, forecast, wacc_result, dcf.intrinsic_value_per_share
    )
    summary = valuation.summarise(
        dcf_fcff=dcf, dcf_fcfe=dcf, relative=relative,
        ddm=valuation.run_ddm_model(analysis, forecast, wacc_result),
        replacement=valuation.run_replacement(analysis, forecast), sotp=None,
        current_price=analysis.company.current_price, margin_of_safety=0.20,
    )
    quality = valuation.grade(analysis, forecast, summary, relative, dcf)

    return SimulationOut(
        company=analysis.company_ref(),
        trials=sim.trials, failed_trials=sim.failed_trials,
        mean_value=sim.mean_value, median_value=sim.median_value, std_dev=sim.std_dev,
        percentiles=sim.percentiles,
        probability_above_price=sim.probability_above_price,
        current_price=sim.current_price,
        histogram=[HistogramBucket(lower=a, upper=b, count=c) for a, b, c in sim.histogram],
        quality=_quality_out(quality),
    )


# ---------------------------------------------------------------------- SOTP
@router.post("/{ticker}/valuation/sotp", response_model=SOTPOut,
             summary="Sum-of-the-parts valuation")
def post_sotp(
    body: SOTPRequest,
    analysis: AnalysisService = Depends(get_analysis),
    services: tuple[ValuationService, ForecastService] = Depends(_services),
    _: CurrentUser = Depends(get_current_user),
) -> SOTPOut:
    _require_data(analysis)
    if not body.segments:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "at least one segment is required")

    balance = analysis.balances[-1] if analysis.balances else None
    income = analysis.incomes[-1] if analysis.incomes else None

    result = run_sotp(
        [
            SOTPSegment(
                name=s.name, basis=SegmentBasis(s.basis), multiple=s.multiple,
                metric=s.metric, direct_value=s.direct_value,
                attributed_debt=s.attributed_debt, stake=s.stake, note=s.note,
            )
            for s in body.segments
        ],
        net_debt=balance.net_debt if balance else 0.0,
        holding_discount=body.holding_discount,
        shares_outstanding=income.weighted_shares if income else 0.0,
        current_price=analysis.company.current_price,
        unallocated_assets=body.unallocated_assets,
    )

    return SOTPOut(
        segments=[
            SOTPSegmentOut(
                name=s.name, basis=s.basis, multiple=s.multiple, metric=s.metric,
                gross_value=s.gross_value, attributable_value=s.attributable_value,
                stake=s.stake, share_of_total=s.share_of_total, note=s.note,
            )
            for s in result.segments
        ],
        gross_asset_value=result.gross_asset_value,
        net_debt=result.net_debt,
        holding_discount=result.holding_discount,
        discount_amount=result.discount_amount,
        equity_value=result.equity_value,
        value_per_share=result.value_per_share,
        upside=result.upside,
        warnings=result.warnings,
    )
