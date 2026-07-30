"""API contracts for the valuation framework."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.common import CompanyRef

ConventionName = Literal["mid_year", "year_end"]
TerminalMethodName = Literal["perpetual_growth", "exit_multiple"]
ScenarioName = Literal["bear", "base", "bull"]


class WACCOut(BaseModel):
    risk_free_rate: float
    total_erp: float
    unlevered_beta: float
    levered_beta: float
    regression_beta: float | None = None
    beta_used: float
    beta_source: str
    size_premium: float
    specific_premium: float
    cost_of_equity: float
    pre_tax_cost_of_debt: float
    marginal_tax_rate: float
    after_tax_cost_of_debt: float
    market_value_equity: float
    market_value_debt: float
    total_capital: float
    weight_equity: float
    weight_debt: float
    debt_to_equity: float
    wacc: float
    bounded: bool = False


class WACCScheduleRow(BaseModel):
    period: int
    debt_to_equity: float
    levered_beta: float
    cost_of_equity: float
    wacc: float


class DCFYearOut(BaseModel):
    period: int
    cash_flow: float
    discount_period: float
    discount_rate: float
    discount_factor: float
    present_value: float


class DCFOut(BaseModel):
    model: str
    convention: str
    terminal_method: str
    years: list[DCFYearOut]
    sum_pv_explicit: float
    terminal_value: float
    pv_terminal_value: float
    terminal_value_pct: float | None = None
    enterprise_value: float
    net_debt: float | None = None
    equity_value: float
    shares_outstanding: float
    intrinsic_value_per_share: float | None = None
    current_price: float | None = None
    upside: float | None = None
    margin_of_safety: float
    maximum_buy_price: float | None = None
    in_buy_zone: bool | None = None
    discount_rate: float
    terminal_growth: float
    implied_exit_multiple: float | None = None
    implied_perpetual_growth: float | None = None
    warnings: list[str] = Field(default_factory=list)


class MultipleSetOut(BaseModel):
    label: str
    pe: float | None = None
    pb: float | None = None
    ev_ebitda: float | None = None
    ev_sales: float | None = None
    ev_ebit: float | None = None
    p_fcfe: float | None = None
    dividend_yield: float | None = None
    peg: float | None = None


class TargetPriceOut(BaseModel):
    key: str
    label: str
    basis: str
    target_multiple: float | None = None
    metric: float | None = None
    metric_label: str
    implied_value: float | None = None
    target_price: float | None = None
    weight: float
    rationale: str


class JustifiedMultipleOut(BaseModel):
    key: str
    label: str
    formula: str
    justified: float | None = None
    actual: float | None = None
    premium_discount: float | None = None
    verdict: str


class RelativeOut(BaseModel):
    current: MultipleSetOut
    forward: list[MultipleSetOut]
    methods: list[TargetPriceOut]
    justified: list[JustifiedMultipleOut]
    blended_target_price: float | None = None
    simple_average_target: float | None = None
    median_target: float | None = None
    target_low: float | None = None
    target_high: float | None = None
    upside: float | None = None
    current_price: float | None = None
    warnings: list[str] = Field(default_factory=list)


class DDMOut(BaseModel):
    variant: str
    value_per_share: float | None = None
    terminal_value: float | None = None
    pv_explicit: float | None = None
    implied_dividend_yield: float | None = None
    upside: float | None = None
    warnings: list[str] = Field(default_factory=list)


class ReplacementOut(BaseModel):
    net_block: float
    inflation_adjustment: float
    adjusted_fixed_assets: float
    net_working_capital: float
    intangible_replacement: float
    total_replacement_cost: float
    net_debt: float
    equity_replacement_value: float
    value_per_share: float | None = None
    tobins_q: float | None = None
    upside: float | None = None
    warnings: list[str] = Field(default_factory=list)


class SOTPSegmentOut(BaseModel):
    name: str
    basis: str
    multiple: float | None = None
    metric: float | None = None
    gross_value: float | None = None
    attributable_value: float | None = None
    stake: float
    share_of_total: float | None = None
    note: str | None = None


class SOTPOut(BaseModel):
    segments: list[SOTPSegmentOut]
    gross_asset_value: float
    net_debt: float
    holding_discount: float
    discount_amount: float
    equity_value: float
    value_per_share: float | None = None
    upside: float | None = None
    warnings: list[str] = Field(default_factory=list)


class MethodValueOut(BaseModel):
    key: str
    label: str
    value_per_share: float | None = None
    upside: float | None = None
    weight: float
    applicable: bool
    note: str | None = None


class SummaryOut(BaseModel):
    methods: list[MethodValueOut]
    weighted_value: float | None = None
    median_value: float | None = None
    low: float | None = None
    high: float | None = None
    current_price: float | None = None
    upside: float | None = None
    margin_of_safety: float
    maximum_buy_price: float | None = None
    in_buy_zone: bool | None = None
    recommendation: str


class QualityIssueOut(BaseModel):
    key: str
    message: str
    severity: str
    detail: str | None = None


class DataQualityOut(BaseModel):
    grade: str
    is_illustrative: bool
    disclosure: str | None = None
    headline: str
    issues: list[QualityIssueOut] = Field(default_factory=list)
    coverage: float | None = None
    history_years: int | None = None
    synthetic_sources: list[str] = Field(default_factory=list)


class ValuationResponse(BaseModel):
    company: CompanyRef
    scenario: ScenarioName
    horizon_years: int
    convention: ConventionName
    terminal_method: TerminalMethodName
    wacc: WACCOut
    wacc_schedule: list[WACCScheduleRow] = Field(default_factory=list)
    dcf_fcff: DCFOut
    dcf_fcfe: DCFOut
    relative: RelativeOut
    ddm: DDMOut
    replacement: ReplacementOut
    sotp: SOTPOut | None = None
    summary: SummaryOut
    quality: DataQualityOut
    scenario_values: dict[str, float | None] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class SensitivityCellOut(BaseModel):
    row: float
    col: float
    value: float | None = None
    upside: float | None = None


class SensitivityOut(BaseModel):
    company: CompanyRef
    row_key: str
    row_label: str
    row_unit: str
    row_values: list[float]
    col_key: str
    col_label: str
    col_unit: str
    col_values: list[float]
    cells: list[list[float | None]]
    upside_cells: list[list[float | None]]
    base_row: float
    base_col: float
    base_value: float | None = None
    minimum: float | None = None
    maximum: float | None = None
    current_price: float | None = None
    quality: DataQualityOut


class HistogramBucket(BaseModel):
    lower: float
    upper: float
    count: int


class SimulationOut(BaseModel):
    company: CompanyRef
    trials: int
    failed_trials: int
    mean_value: float | None = None
    median_value: float | None = None
    std_dev: float | None = None
    percentiles: dict[int, float] = Field(default_factory=dict)
    probability_above_price: float | None = None
    current_price: float | None = None
    histogram: list[HistogramBucket] = Field(default_factory=list)
    quality: DataQualityOut


class SOTPSegmentRequest(BaseModel):
    name: str
    basis: Literal["ev_ebitda", "ev_sales", "pe", "book", "dcf"]
    multiple: float | None = None
    metric: float | None = None
    direct_value: float | None = None
    attributed_debt: float = 0.0
    stake: float = 1.0
    note: str | None = None


class SOTPRequest(BaseModel):
    segments: list[SOTPSegmentRequest]
    holding_discount: float = Field(0.15, ge=0, le=0.6)
    unallocated_assets: float = 0.0
