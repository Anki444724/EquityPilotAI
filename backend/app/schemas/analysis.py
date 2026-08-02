"""Response models for the Module 2 analysis endpoints."""
from __future__ import annotations

from pydantic import BaseModel, Field

from app.schemas.common import (
    AnalysisResponse, CompanyRef, Flag, MetricSection, PeriodMeta, Unit,
)


class StatementResponse(AnalysisResponse):
    """Income statement / balance sheet / cash flow."""

    statement: str = Field(description="income_statement | balance_sheet | cash_flow")


class RatioResponse(AnalysisResponse):
    wacc_assumption: float | None = Field(
        None, description="WACC used for ROIC spread and EVA; null when not supplied"
    )


class WorkingCapitalResponse(AnalysisResponse):
    flags: list[Flag] = Field(default_factory=list)
    cost_of_debt_assumption: float | None = None


class CapexResponse(AnalysisResponse):
    pass


class DebtInstrumentRow(BaseModel):
    instrument: str
    lender: str | None = None
    security: str
    rate_type: str
    amount: float = Field(description="₹ crore")
    share_of_debt: float | None = None
    interest_rate: float | None = None
    maturity_year: int | None = None
    currency: str = "INR"


class MaturityBucket(BaseModel):
    year: int
    amount: float
    share_of_debt: float | None = None
    cumulative: float
    ebitda_coverage: float | None = None


class CovenantRow(BaseModel):
    key: str
    label: str
    threshold: float
    actual: float | None
    direction: str
    unit: str = Unit.MULTIPLE
    compliant: bool | None = None
    headroom: float | None = None


class DebtReconciliation(BaseModel):
    instrument_total: float
    balance_sheet_gross_debt: float
    difference: float
    reconciled: bool


class DebtResponse(AnalysisResponse):
    instruments: list[DebtInstrumentRow] = Field(default_factory=list)
    maturity_ladder: list[MaturityBucket] = Field(default_factory=list)
    covenants: list[CovenantRow] = Field(default_factory=list)
    reconciliation: DebtReconciliation | None = None
    blended_rate: float | None = None
    floating_rate_share: float | None = None
    foreign_currency_share: float | None = None
    flags: list[Flag] = Field(default_factory=list)


class OwnershipSignal(BaseModel):
    signal: str
    score: int | None = None
    detail: str | None = None


class ShareholdingResponse(AnalysisResponse):
    signal: OwnershipSignal | None = None
    flags: list[Flag] = Field(default_factory=list)


class QuarterRowOut(BaseModel):
    """One reported quarter. All money in ₹ crore; all rates are fractions."""

    fiscal_year: int
    quarter: int = Field(ge=1, le=4)
    label: str = Field(description='e.g. "Q2 FY26"')
    revenue: float | None = None
    expenses: float | None = None
    operating_profit: float | None = None
    operating_margin: float | None = Field(
        None, description="Fraction, not percent (0.145 == 14.5%)")
    other_income: float | None = None
    interest: float | None = None
    depreciation: float | None = None
    profit_before_tax: float | None = None
    tax_rate: float | None = Field(None, description="Fraction, not percent")
    net_profit: float | None = None
    eps: float | None = Field(None, description="₹ per share")
    revenue_qoq: float | None = None
    profit_qoq: float | None = None
    revenue_yoy: float | None = None
    profit_yoy: float | None = None
    source: str | None = None


class QuarterlyResponse(BaseModel):
    """Quarterly results as filed.

    `has_data=False` means the source genuinely publishes no quarters for this
    company — a recent listing, or a company screener does not cover. It never
    means the platform declined to store them: a period with no reported
    figure is not written at all, so an empty list here is a fact about the
    filing history, not about the pipeline.
    """

    company: CompanyRef
    quarters: list[QuarterRowOut] = Field(default_factory=list)
    has_data: bool = False
    #: Why the list is empty, when it is. Null when data is present.
    unavailable_reason: str | None = None


class StatementSummary(BaseModel):
    """Compact headline block used by the financials overview tab."""

    fiscal_year: int
    revenue: float | None = None
    ebitda: float | None = None
    ebitda_margin: float | None = None
    pat: float | None = None
    pat_margin: float | None = None
    eps: float | None = None
    cfo: float | None = None
    free_cash_flow: float | None = None
    net_debt: float | None = None
    total_assets: float | None = None
    roe: float | None = None
    roce: float | None = None
    balance_sheet_ties: bool = True


class FinancialsOverview(BaseModel):
    company: CompanyRef
    periods: PeriodMeta
    summary: list[StatementSummary]
    revenue_cagr_5y: float | None = None
    revenue_cagr_full: float | None = None
    has_data: bool = True
    warnings: list[str] = Field(default_factory=list)
