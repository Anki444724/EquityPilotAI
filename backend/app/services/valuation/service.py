"""Valuation orchestration.

Runs every methodology from one resolved company context, assembles the
cross-method summary, and grades the result. The engines themselves are pure
functions in ``app.domain.valuation``; this layer supplies their inputs from
the forecast and the reported financials, and does no valuation arithmetic of
its own.

Ordering matters: WACC is needed before the DCFs, the FCFF DCF is needed
before relative valuation (it contributes a method to the blend), and every
output is needed before the data-quality grade can be assigned.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.calc import safe_div
from app.domain.forecast.assumptions import Scenario
from app.domain.forecast.engine import ForecastResult
from app.domain.valuation.data_quality import DataQualityReport, assess_data_quality
from app.domain.valuation.dcf import (
    DCFInputs, DCFResult, DiscountConvention, TerminalMethod, run_dcf,
)
from app.domain.valuation.ddm import DDMInputs, DDMResult, DDMVariant, run_ddm
from app.domain.valuation.relative import (
    RelativeInputs, RelativeValuationResult, run_relative_valuation,
)
from app.domain.valuation.sensitivity import (
    SensitivityGrid, StochasticVariable, build_grid, run_simulation, SimulationResult,
)
from app.domain.valuation.sotp import (
    ReplacementValueResult, SOTPResult, run_replacement_value,
)
from app.domain.valuation.wacc import (
    BetaSource, WACCInputs, WACCResult, compute_wacc, dynamic_wacc_schedule,
)
from app.models.company import FinancialFact
from app.services.analysis_service import AnalysisService
from app.services.forecast.service import ForecastService


@dataclass(frozen=True, slots=True)
class MethodValue:
    """One methodology's contribution to the consolidated view."""

    key: str
    label: str
    value_per_share: float | None
    upside: float | None
    weight: float
    applicable: bool
    note: str | None = None


@dataclass(frozen=True, slots=True)
class ValuationSummary:
    """The consolidated cross-method conclusion."""

    methods: list[MethodValue]
    weighted_value: float | None
    median_value: float | None
    low: float | None
    high: float | None
    current_price: float | None
    upside: float | None
    margin_of_safety: float
    maximum_buy_price: float | None
    in_buy_zone: bool | None
    recommendation: str


@dataclass(frozen=True, slots=True)
class ValuationBundle:
    """Everything the valuation endpoints return."""

    wacc: WACCResult
    wacc_schedule: list[WACCResult]
    dcf_fcff: DCFResult
    dcf_fcfe: DCFResult
    relative: RelativeValuationResult
    ddm: DDMResult
    replacement: ReplacementValueResult
    sotp: SOTPResult | None
    summary: ValuationSummary
    quality: DataQualityReport
    scenario_values: dict[str, float | None] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


#: Default cross-method weights. Cash-flow methods dominate; asset-based
#: methods act as a floor rather than a primary driver.
DEFAULT_METHOD_WEIGHTS = {
    "dcf_fcff": 0.40,
    "dcf_fcfe": 0.15,
    "relative": 0.30,
    "ddm": 0.10,
    "replacement": 0.05,
    "sotp": 0.0,
}


class ValuationService:
    def __init__(self, db: Session) -> None:
        self.db = db

    # ------------------------------------------------------------- inputs
    def build_wacc(
        self,
        analysis: AnalysisService,
        forecast: ForecastResult,
        overrides: dict[str, float] | None = None,
    ) -> WACCInputs:
        """Assemble WACC inputs from market data and reported financials."""
        company = analysis.company
        balance = analysis.balances[-1] if analysis.balances else None
        assumptions = forecast.assumptions
        overrides = overrides or {}

        market_cap = company.market_cap or 0.0
        gross_debt = balance.gross_debt if balance else 0.0

        return WACCInputs(
            risk_free_rate=overrides.get("risk_free_rate", 0.0695),
            mature_erp=overrides.get("mature_erp", 0.055),
            country_risk_premium=overrides.get("country_risk_premium", 0.0243),
            unlevered_beta=overrides.get("unlevered_beta", 0.85),
            regression_beta=overrides.get("regression_beta"),
            beta_source=BetaSource(overrides.get("beta_source", BetaSource.BOTTOM_UP))
            if isinstance(overrides.get("beta_source"), str) else BetaSource.BOTTOM_UP,
            size_premium=overrides.get("size_premium", 0.01),
            specific_premium=overrides.get("specific_premium", 0.005),
            cost_of_debt=overrides.get("cost_of_debt", assumptions.interest_rate.value),
            marginal_tax_rate=overrides.get(
                "marginal_tax_rate", assumptions.effective_tax_rate.value
            ),
            market_value_equity=overrides.get("market_value_equity", market_cap),
            market_value_debt=overrides.get("market_value_debt", gross_debt),
        )

    # --------------------------------------------------------------- DCFs
    def run_fcff(
        self,
        analysis: AnalysisService,
        forecast: ForecastResult,
        wacc: WACCResult,
        *,
        convention: DiscountConvention,
        terminal_method: TerminalMethod,
        terminal_growth: float,
        exit_multiple: float,
        margin_of_safety: float,
        rate_schedule: tuple[float, ...] | None = None,
    ) -> DCFResult:
        base = forecast.base
        balance = analysis.balances[-1] if analysis.balances else None
        terminal = forecast.terminal_year

        return run_dcf(DCFInputs(
            cash_flows=tuple(y.fcff for y in forecast.years),
            discount_rate=wacc.wacc,
            discount_rate_schedule=rate_schedule,
            terminal_growth=terminal_growth,
            terminal_method=terminal_method,
            exit_multiple=exit_multiple,
            terminal_ebitda=terminal.ebitda if terminal else None,
            convention=convention,
            gross_debt=base.gross_debt,
            cash_and_equivalents=base.cash,
            minority_interest=balance.minority_interest if balance else 0.0,
            associate_investments=balance.lt_investments_associates if balance else 0.0,
            shares_outstanding=base.shares_outstanding,
            current_price=analysis.company.current_price,
            margin_of_safety=margin_of_safety,
        ))

    def run_fcfe(
        self,
        analysis: AnalysisService,
        forecast: ForecastResult,
        wacc: WACCResult,
        *,
        convention: DiscountConvention,
        terminal_growth: float,
        margin_of_safety: float,
    ) -> DCFResult:
        """FCFE discounted at the cost of equity — no enterprise bridge."""
        base = forecast.base
        return run_dcf(
            DCFInputs(
                cash_flows=tuple(y.fcfe for y in forecast.years),
                discount_rate=wacc.cost_of_equity,
                terminal_growth=terminal_growth,
                terminal_method=TerminalMethod.PERPETUAL_GROWTH,
                convention=convention,
                shares_outstanding=base.shares_outstanding,
                current_price=analysis.company.current_price,
                margin_of_safety=margin_of_safety,
            ),
            equity_model=True,
        )

    # ------------------------------------------------------ relative & DDM
    def run_relative(
        self,
        analysis: AnalysisService,
        forecast: ForecastResult,
        wacc: WACCResult,
        dcf_value: float | None,
        overrides: dict[str, float] | None = None,
    ) -> RelativeValuationResult:
        company = analysis.company
        income = analysis.incomes[-1] if analysis.incomes else None
        balance = analysis.balances[-1] if analysis.balances else None
        cash_flow = analysis.cash_flows[-1] if analysis.cash_flows else None
        base = forecast.base
        overrides = overrides or {}
        terminal = forecast.terminal_year

        bvps = safe_div(
            balance.shareholders_equity if balance else None, base.shares_outstanding
        )
        forward_bvps = tuple(
            safe_div(y.equity, base.shares_outstanding) for y in forecast.years[:3]
        )

        return run_relative_valuation(RelativeInputs(
            current_price=company.current_price,
            shares_outstanding=base.shares_outstanding,
            market_cap=company.market_cap or 0.0,
            gross_debt=base.gross_debt,
            cash_and_equivalents=base.cash,
            trailing_eps=income.eps_basic if income else None,
            trailing_bvps=bvps,
            trailing_ebitda=income.ebitda if income else None,
            trailing_revenue=income.total_revenue if income else None,
            trailing_ebit=income.ebit if income else None,
            trailing_fcfe=cash_flow.fcf_to_equity if cash_flow else None,
            trailing_dividend_per_share=income.dividend_per_share if income else None,
            forward_eps=tuple(y.eps for y in forecast.years[:3]),
            forward_bvps=forward_bvps,
            forward_ebitda=tuple(y.ebitda for y in forecast.years[:3]),
            forward_revenue=tuple(y.revenue for y in forecast.years[:3]),
            forward_ebit=tuple(y.ebit for y in forecast.years[:3]),
            forward_fcfe=tuple(y.fcfe for y in forecast.years[:3]),
            target_pe=overrides.get("target_pe", forecast.assumptions.target_pe.value),
            target_pb=overrides.get("target_pb", 3.0),
            target_ev_ebitda=overrides.get(
                "target_ev_ebitda", forecast.assumptions.exit_ev_ebitda.value
            ),
            target_ev_sales=overrides.get("target_ev_sales", 2.5),
            cost_of_equity=wacc.cost_of_equity,
            wacc=wacc.wacc,
            terminal_growth=overrides.get(
                "terminal_growth", forecast.assumptions.terminal_growth.value
            ),
            payout_ratio=forecast.assumptions.dividend_payout.value,
            roe=terminal.roe if terminal else None,
            reinvestment_rate=(
                forecast.cash_flow_rows[-1].reinvestment_rate
                if forecast.cash_flow_rows else None
            ),
            tax_rate=forecast.assumptions.effective_tax_rate.value,
            eps_cagr=forecast.eps_cagr,
            dcf_value_per_share=dcf_value,
        ))

    def run_ddm_model(
        self,
        analysis: AnalysisService,
        forecast: ForecastResult,
        wacc: WACCResult,
        variant: DDMVariant = DDMVariant.H_MODEL,
    ) -> DDMResult:
        income = analysis.incomes[-1] if analysis.incomes else None
        return run_ddm(DDMInputs(
            current_dividend_per_share=(income.dividend_per_share or 0.0) if income else 0.0,
            cost_of_equity=wacc.cost_of_equity,
            stable_growth=forecast.assumptions.terminal_growth.value,
            variant=variant,
            high_growth=forecast.assumptions.revenue_growth.value,
            high_growth_years=min(5, len(forecast.years)),
            half_life_years=max(1, len(forecast.years) // 2),
            current_price=analysis.company.current_price,
        ))

    def run_replacement(
        self, analysis: AnalysisService, forecast: ForecastResult
    ) -> ReplacementValueResult:
        base = forecast.base
        return run_replacement_value(
            net_block=base.net_block,
            net_working_capital=base.net_working_capital,
            net_debt=base.gross_debt - base.cash,
            shares_outstanding=base.shares_outstanding,
            market_cap=analysis.company.market_cap,
            current_price=analysis.company.current_price,
        )

    # ------------------------------------------------------------ summary
    def summarise(
        self,
        *,
        dcf_fcff: DCFResult,
        dcf_fcfe: DCFResult,
        relative: RelativeValuationResult,
        ddm: DDMResult,
        replacement: ReplacementValueResult,
        sotp: SOTPResult | None,
        current_price: float | None,
        margin_of_safety: float,
        weights: dict[str, float] | None = None,
    ) -> ValuationSummary:
        w = {**DEFAULT_METHOD_WEIGHTS, **(weights or {})}

        candidates = [
            ("dcf_fcff", "DCF — FCFF", dcf_fcff.intrinsic_value_per_share, None),
            ("dcf_fcfe", "DCF — FCFE", dcf_fcfe.intrinsic_value_per_share, None),
            ("relative", "Relative (blended)", relative.blended_target_price, None),
            ("ddm", "Dividend discount", ddm.value_per_share,
             ddm.warnings[0] if ddm.warnings else None),
            ("replacement", "Replacement value", replacement.value_per_share,
             "Asset-based floor."),
            ("sotp", "Sum of the parts", sotp.value_per_share if sotp else None,
             None if sotp else "No segment data supplied."),
        ]

        methods: list[MethodValue] = []
        for key, label, value, note in candidates:
            applicable = value is not None and value > 0
            methods.append(MethodValue(
                key=key, label=label, value_per_share=value,
                upside=safe_div(value, current_price) - 1
                if applicable and current_price else None,
                weight=w.get(key, 0.0) if applicable else 0.0,
                applicable=applicable, note=note,
            ))

        usable = [m for m in methods if m.applicable and m.weight > 0]
        total_weight = sum(m.weight for m in usable)
        weighted = (
            sum(m.value_per_share * m.weight for m in usable) / total_weight
            if total_weight > 0 else None
        )
        values = [m.value_per_share for m in methods if m.applicable]

        upside = safe_div(weighted, current_price) - 1 if weighted and current_price else None
        max_buy = weighted * (1 - margin_of_safety) if weighted else None
        in_zone = current_price <= max_buy if (max_buy and current_price) else None

        if upside is None:
            recommendation = "Not rated — no market price"
        elif upside > 0.35:
            recommendation = "Strong Buy"
        elif upside > 0.20:
            recommendation = "Buy"
        elif upside > 0.08:
            recommendation = "Accumulate"
        elif upside > -0.08:
            recommendation = "Hold"
        elif upside > -0.20:
            recommendation = "Reduce"
        else:
            recommendation = "Sell"

        return ValuationSummary(
            methods=methods,
            weighted_value=weighted,
            median_value=median(values) if values else None,
            low=min(values) if values else None,
            high=max(values) if values else None,
            current_price=current_price,
            upside=upside,
            margin_of_safety=margin_of_safety,
            maximum_buy_price=max_buy,
            in_buy_zone=in_zone,
            recommendation=recommendation,
        )

    # ------------------------------------------------------- data quality
    def grade(
        self,
        analysis: AnalysisService,
        forecast: ForecastResult,
        summary: ValuationSummary,
        relative: RelativeValuationResult,
        dcf: DCFResult,
    ) -> DataQualityReport:
        sources = set(
            self.db.execute(
                select(FinancialFact.source)
                .where(FinancialFact.company_id == analysis.company.id)
                .distinct()
            ).scalars().all()
        )
        balances = analysis.balances
        return assess_data_quality(
            fact_sources={s for s in sources if s},
            coverage=analysis.fin.coverage(),
            history_years=len(analysis.fin.fiscal_years),
            upside=summary.upside,
            ev_ebitda=relative.current.ev_ebitda,
            terminal_value_pct=dcf.terminal_value_pct,
            assumption_provenance=forecast.assumptions.provenance_summary(),
            balance_sheet_ties=all(b.balances for b in balances) if balances else True,
            forecast_converged=forecast.debt_converged and forecast.all_reconciled,
        )

    # --------------------------------------------------------- sensitivity
    def sensitivity(
        self,
        analysis: AnalysisService,
        forecast: ForecastResult,
        wacc: WACCResult,
        *,
        row_key: str,
        col_key: str,
        convention: DiscountConvention,
        terminal_method: TerminalMethod,
        terminal_growth: float,
        exit_multiple: float,
        steps: int = 2,
    ) -> SensitivityGrid:
        """Two-way sensitivity, revaluing the FCFF DCF at each intersection."""
        base_map = {
            "wacc": wacc.wacc,
            "terminal_growth": terminal_growth,
            "exit_multiple": exit_multiple,
            "revenue_cagr": forecast.revenue_cagr or 0.10,
            "ebit_margin": (
                forecast.terminal_year.ebit_margin if forecast.terminal_year else 0.15
            ) or 0.15,
        }
        flows = [y.fcff for y in forecast.years]
        terminal = forecast.terminal_year
        base = forecast.base
        balance = analysis.balances[-1] if analysis.balances else None

        def revalue(row_value: float, col_value: float) -> float | None:
            params = dict(base_map)
            params[row_key] = row_value
            params[col_key] = col_value

            # Growth and margin axes rescale the cash-flow stream. This is a
            # first-order approximation: a full re-run of the forecast engine
            # per cell would be exact but is not needed for a 5x5 grid.
            adjusted = list(flows)
            if row_key == "revenue_cagr" or col_key == "revenue_cagr":
                shift = params["revenue_cagr"] - (base_map["revenue_cagr"] or 0.0)
                adjusted = [f * ((1 + shift) ** (i + 1)) for i, f in enumerate(adjusted)]
            if row_key == "ebit_margin" or col_key == "ebit_margin":
                ratio = safe_div(params["ebit_margin"], base_map["ebit_margin"])
                if ratio:
                    adjusted = [f * ratio for f in adjusted]

            result = run_dcf(DCFInputs(
                cash_flows=tuple(adjusted),
                discount_rate=params["wacc"],
                terminal_growth=params["terminal_growth"],
                terminal_method=terminal_method,
                exit_multiple=params["exit_multiple"],
                terminal_ebitda=terminal.ebitda if terminal else None,
                convention=convention,
                gross_debt=base.gross_debt,
                cash_and_equivalents=base.cash,
                minority_interest=balance.minority_interest if balance else 0.0,
                shares_outstanding=base.shares_outstanding,
                current_price=analysis.company.current_price,
            ))
            return result.intrinsic_value_per_share

        return build_grid(
            row_key=row_key, col_key=col_key,
            row_base=base_map[row_key], col_base=base_map[col_key],
            revalue=revalue, steps=steps,
            current_price=analysis.company.current_price,
        )

    def monte_carlo(
        self,
        analysis: AnalysisService,
        forecast: ForecastResult,
        wacc: WACCResult,
        *,
        convention: DiscountConvention,
        terminal_growth: float,
        trials: int = 1000,
        seed: int | None = 42,
    ) -> SimulationResult:
        """Monte Carlo over WACC, terminal growth and cash-flow level."""
        flows = [y.fcff for y in forecast.years]
        terminal = forecast.terminal_year
        base = forecast.base

        variables = [
            StochasticVariable("wacc", wacc.wacc, spread=0.02, minimum=0.05, maximum=0.30),
            StochasticVariable("terminal_growth", terminal_growth, spread=0.015,
                               minimum=-0.01, maximum=0.08),
            StochasticVariable("cash_flow_factor", 1.0, spread=0.20, minimum=0.4, maximum=1.8),
        ]

        def revalue(draw: dict[str, float]) -> float | None:
            factor = draw["cash_flow_factor"]
            result = run_dcf(DCFInputs(
                cash_flows=tuple(f * factor for f in flows),
                discount_rate=draw["wacc"],
                terminal_growth=draw["terminal_growth"],
                terminal_ebitda=terminal.ebitda if terminal else None,
                convention=convention,
                gross_debt=base.gross_debt,
                cash_and_equivalents=base.cash,
                shares_outstanding=base.shares_outstanding,
            ))
            return result.intrinsic_value_per_share

        return run_simulation(
            variables, revalue, trials=trials, seed=seed,
            current_price=analysis.company.current_price,
        )

    # ------------------------------------------------------------- bundle
    def value_company(
        self,
        analysis: AnalysisService,
        forecast_service: ForecastService,
        *,
        horizon: int = 5,
        convention: DiscountConvention = DiscountConvention.MID_YEAR,
        terminal_method: TerminalMethod = TerminalMethod.PERPETUAL_GROWTH,
        terminal_growth: float | None = None,
        exit_multiple: float | None = None,
        margin_of_safety: float = 0.20,
        dynamic_wacc: bool = False,
        wacc_overrides: dict[str, float] | None = None,
        scenario: Scenario = Scenario.BASE,
    ) -> ValuationBundle:
        """Run every methodology and consolidate."""
        context = forecast_service.build_context(
            analysis.company, analysis.statements, years=horizon
        )
        saved = forecast_service.active_for_company(analysis.company.id)
        forecast = forecast_service.run(context, saved, scenario)

        assumptions = forecast.assumptions
        tg = terminal_growth if terminal_growth is not None else assumptions.terminal_growth.value
        exit_mult = exit_multiple if exit_multiple is not None else assumptions.exit_ev_ebitda.value

        wacc_inputs = self.build_wacc(analysis, forecast, wacc_overrides)
        wacc = compute_wacc(wacc_inputs)

        schedule: list[WACCResult] = []
        rate_schedule: tuple[float, ...] | None = None
        if dynamic_wacc:
            equity_path = [y.equity for y in forecast.years]
            debt_path = [y.gross_debt for y in forecast.years]
            schedule = dynamic_wacc_schedule(wacc_inputs, equity_path, debt_path)
            rate_schedule = tuple(s.wacc for s in schedule)

        fcff = self.run_fcff(
            analysis, forecast, wacc, convention=convention,
            terminal_method=terminal_method, terminal_growth=tg,
            exit_multiple=exit_mult, margin_of_safety=margin_of_safety,
            rate_schedule=rate_schedule,
        )
        fcfe = self.run_fcfe(
            analysis, forecast, wacc, convention=convention,
            terminal_growth=tg, margin_of_safety=margin_of_safety,
        )
        relative = self.run_relative(
            analysis, forecast, wacc, fcff.intrinsic_value_per_share
        )
        ddm = self.run_ddm_model(analysis, forecast, wacc)
        replacement = self.run_replacement(analysis, forecast)

        summary = self.summarise(
            dcf_fcff=fcff, dcf_fcfe=fcfe, relative=relative, ddm=ddm,
            replacement=replacement, sotp=None,
            current_price=analysis.company.current_price,
            margin_of_safety=margin_of_safety,
        )
        quality = self.grade(analysis, forecast, summary, relative, fcff)

        # Scenario valuations, using the same FCFF machinery for each case.
        scenario_values: dict[str, float | None] = {}
        for case in (Scenario.BEAR, Scenario.BASE, Scenario.BULL):
            case_forecast = forecast_service.run(context, saved, case)
            case_result = self.run_fcff(
                analysis, case_forecast, wacc, convention=convention,
                terminal_method=terminal_method, terminal_growth=tg,
                exit_multiple=exit_mult, margin_of_safety=margin_of_safety,
            )
            scenario_values[case.value] = case_result.intrinsic_value_per_share

        warnings = [*fcff.warnings, *fcfe.warnings, *relative.warnings, *ddm.warnings]

        return ValuationBundle(
            wacc=wacc,
            wacc_schedule=schedule,
            dcf_fcff=fcff,
            dcf_fcfe=fcfe,
            relative=relative,
            ddm=ddm,
            replacement=replacement,
            sotp=None,
            summary=summary,
            quality=quality,
            scenario_values=scenario_values,
            warnings=warnings,
        )
