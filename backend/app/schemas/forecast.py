"""API contracts for the forecast engine."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.common import CompanyRef, MetricSection, PeriodMeta

Horizon = Literal[3, 5, 10]
ScenarioName = Literal["bear", "base", "bull"]
RevenueMethodName = Literal["cagr", "volume_price", "segment", "organic_acquisition"]


class DriverOut(BaseModel):
    """One assumption, with the provenance that makes it auditable."""

    name: str
    label: str
    value: float
    unit: str
    group: str
    source: str
    citation: str | None = None
    note: str | None = None
    by_year: dict[int, float] = Field(default_factory=dict)


class AssumptionSet(BaseModel):
    scenario: ScenarioName
    horizon_years: int
    revenue_method: RevenueMethodName
    drivers: list[DriverOut]
    provenance: dict[str, int] = Field(default_factory=dict)


class ForecastYearOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    period: int
    fiscal_year: int
    revenue: float
    revenue_growth: float | None = None
    ebitda: float
    ebitda_margin: float
    depreciation: float
    ebit: float
    ebit_margin: float | None = None
    other_income: float
    interest_expense: float
    pbt: float
    tax_expense: float
    effective_tax_rate: float
    pat: float
    pat_margin: float | None = None
    eps: float | None = None
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
    roe: float | None = None
    roce: float | None = None
    roic: float | None = None
    net_debt_ebitda: float | None = None
    interest_coverage: float | None = None
    reconciled: bool = True


class HistoricalYearOut(BaseModel):
    """Reported history, so charts can show history versus forecast."""

    fiscal_year: int
    revenue: float
    ebitda: float
    ebitda_margin: float | None = None
    pat: float
    eps: float | None = None
    free_cash_flow: float


class ForecastSummary(BaseModel):
    revenue_cagr: float | None = None
    ebitda_cagr: float | None = None
    terminal_revenue: float | None = None
    terminal_ebitda: float | None = None
    terminal_eps: float | None = None
    terminal_fcff: float | None = None
    debt_converged: bool = True
    debt_iterations: int = 0
    all_reconciled: bool = True


class ForecastResponse(BaseModel):
    company: CompanyRef
    forecast_id: str | None = None
    name: str | None = None
    scenario: ScenarioName
    periods: PeriodMeta
    base_fiscal_year: int
    years: list[ForecastYearOut]
    history: list[HistoricalYearOut] = Field(default_factory=list)
    assumptions: AssumptionSet
    summary: ForecastSummary
    sections: list[MetricSection] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ScenarioOutcomeOut(BaseModel):
    scenario: ScenarioName
    probability: float
    terminal_revenue: float
    terminal_ebitda: float
    terminal_eps: float | None = None
    revenue_cagr: float | None = None
    terminal_fcff: float
    value_per_share: float | None = None
    upside: float | None = None


class ScenarioComparisonRow(BaseModel):
    """One metric across the three cases, for the comparison chart."""

    key: str
    label: str
    unit: str
    bear: list[float | None]
    base: list[float | None]
    bull: list[float | None]


class ScenarioResponse(BaseModel):
    company: CompanyRef
    forecast_id: str | None = None
    periods: PeriodMeta
    outcomes: list[ScenarioOutcomeOut]
    comparison: list[ScenarioComparisonRow]
    expected_value: float | None = None
    expected_upside: float | None = None
    bull_upside: float | None = None
    bear_downside: float | None = None
    risk_reward: float | None = None
    standard_deviation: float | None = None
    coefficient_of_variation: float | None = None
    verdict: str
    current_price: float | None = None


# ------------------------------------------------------------------ requests
class ForecastCreateRequest(BaseModel):
    name: str = "Base forecast"
    horizon_years: Horizon = 5
    revenue_method: RevenueMethodName = "cagr"
    segments: list[dict] | None = None
    notes: str | None = None
    #: Optional initial driver values, applied to the base case.
    drivers: dict[str, float] = Field(default_factory=dict)

    @field_validator("segments")
    @classmethod
    def _validate_segments(cls, v: list[dict] | None) -> list[dict] | None:
        if not v:
            return v
        for seg in v:
            if "name" not in seg or "base_revenue" not in seg:
                raise ValueError("each segment needs 'name' and 'base_revenue'")
        return v


class AssumptionUpdateRequest(BaseModel):
    """Analyst (or, later, AI) edits to assumption drivers."""

    drivers: dict[str, float]
    scenario: ScenarioName | None = Field(
        None, description="null updates the shared base assumptions"
    )
    by_year: dict[str, dict[int, float]] = Field(default_factory=dict)
    source: str = "analyst"
    citation: str | None = None
    requires_review: bool = False
    horizon_years: Horizon | None = None
    revenue_method: RevenueMethodName | None = None


class ForecastListItem(BaseModel):
    id: str
    name: str
    horizon_years: int
    revenue_method: str
    status: str
    revision: int


class ForecastListResponse(BaseModel):
    company: CompanyRef
    forecasts: list[ForecastListItem]
