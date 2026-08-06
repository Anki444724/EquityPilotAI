"""API contracts for the admin financial-statements module (Phase 3)."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, Field


class FinancialBulkResult(BaseModel):
    updated: int = 0
    created: int = 0
    errors: list[dict[str, str]] = Field(default_factory=list)


class QuarterlyResultIn(BaseModel):
    fiscal_year: int
    quarter: int = Field(ge=1, le=4)
    revenue: float | None = None
    expenses: float | None = None
    operating_profit: float | None = None
    operating_margin: float | None = None
    other_income: float | None = None
    interest: float | None = None
    depreciation: float | None = None
    profit_before_tax: float | None = None
    tax_rate: float | None = None
    net_profit: float | None = None
    eps: float | None = None
    source: str | None = None


class ShareholdingIn(BaseModel):
    fiscal_year: int
    quarter: int = Field(ge=1, le=4)
    promoter_indian: float | None = None
    promoter_foreign: float | None = None
    fii_fpi: float | None = None
    mutual_funds: float | None = None
    insurance: float | None = None
    banks_fis_aif: float | None = None
    government: float | None = None
    others_custodians: float | None = None
    promoter_pledged: float | None = None


class CorporateActionIn(BaseModel):
    action_type: str = Field(min_length=1, max_length=24)
    ex_date: date | None = None
    record_date: date | None = None
    value: float | None = None
    description: str | None = None
    source: str | None = None


class CorporateActionUpdate(BaseModel):
    """Partial update — all fields optional."""

    action_type: str | None = Field(default=None, min_length=1, max_length=24)
    ex_date: date | None = None
    record_date: date | None = None
    value: float | None = None
    description: str | None = None
    source: str | None = None


class FinancialVersionOut(BaseModel):
    id: int
    company_id: str
    version: int
    actor_email: str | None = None
    change_type: str
    summary: str
    created_at: str


class FinancialStatementsOut(BaseModel):
    """Annual statements, ratios and metadata for a company."""

    years: list[int]
    statements: dict[str, Any]
    ratios: dict[str, Any]
    fiscal_years: list[int]
