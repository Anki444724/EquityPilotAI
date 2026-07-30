"""API contracts for companies. Typed end to end — these generate the OpenAPI
schema the frontend client is built from."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class CompanySummary(BaseModel):
    """Lightweight projection for search results and lists."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    ticker: str
    exchange: str
    sector: str | None = None
    industry: str | None = None
    market_cap: float | None = Field(None, description="₹ crore")
    current_price: float | None = Field(None, description="₹ per share")


class CompanyDetail(CompanySummary):
    isin: str | None = None
    description: str | None = None
    website: str | None = None
    incorporated_year: int | None = None
    shares_outstanding: float | None = Field(None, description="crore")
    data_version: int = 1


class DataCoverage(BaseModel):
    """How much of the 54-item canonical grid is populated."""

    has_data: bool
    coverage: float = Field(ge=0, le=1)
    fiscal_years: list[int]
    items_total: int = 54
    items_populated: int


class CompanyProfile(BaseModel):
    """Company header plus headline financials for the profile page."""

    company: CompanyDetail
    coverage: DataCoverage
    latest_fiscal_year: int | None = None
    revenue: float | None = Field(None, description="₹ crore")
    ebitda: float | None = None
    pat: float | None = None
    eps: float | None = None
    ebitda_margin: float | None = None
    pat_margin: float | None = None
    net_debt: float | None = None
    total_assets: float | None = None
    balance_sheet_ties: bool | None = None


class SearchResponse(BaseModel):
    query: str
    total: int
    results: list[CompanySummary]


class PaginatedCompanies(BaseModel):
    total: int
    page: int
    page_size: int
    results: list[CompanySummary]
