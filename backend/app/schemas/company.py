"""API contracts for companies. Typed end to end — these generate the OpenAPI
schema the frontend client is built from."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class LiveMarket(BaseModel):
    """The single, shared market view every surface renders a price from.

    `live_price` is the best figure the market router could produce (from a
    live provider when one answered, else the internal/database tier). The
    stored `current_price` column is carried separately and only ever used as
    an explicit fallback when no live figure is available — never presented as
    a live quote. `price_source` and `market_status` make the provenance
    visible instead of silently blending a stale figure into a live one.
    """

    live_price: float | None = Field(None, description="Best current price (₹ per share)")
    current_price: float | None = Field(None, description="Stored fallback, ₹ per share")
    price_source: str | None = Field(None, description="Tier that served the price")
    last_updated: str | None = Field(None, description="ISO-8601 timestamp from the source")
    market_status: str = Field("unknown", description="open | closed | weekend | unknown")
    change: float | None = None
    change_percent: float | None = None
    volume: float | None = None


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
    market: LiveMarket | None = Field(None, description="Live market view")


class CompanyDetail(CompanySummary):
    isin: str | None = None
    description: str | None = None
    website: str | None = None
    incorporated_year: int | None = None
    shares_outstanding: float | None = Field(None, description="crore")
    data_version: int = 1
    # Phase 2 enterprise fields
    bse_code: str | None = None
    listing_status: str = "active"
    index_membership: str | None = None
    currency: str = "INR"
    reporting_scale: str = "crore"
    face_value: float | None = None
    listing_date: datetime | None = None
    ceo: str | None = None
    employees: int | None = None
    headquarters: str | None = None
    logo_url: str | None = None
    favicon_url: str | None = None
    deleted_at: datetime | None = None


class CompanyCreate(BaseModel):
    """Admin-created company. All fields optional except the essentials."""

    name: str = Field(min_length=1, max_length=200)
    ticker: str = Field(min_length=1, max_length=32)
    exchange: str = "NSE"
    isin: str | None = Field(default=None, max_length=16)
    bse_code: str | None = Field(default=None, max_length=16)
    sector: str | None = Field(default=None, max_length=120)
    industry: str | None = Field(default=None, max_length=120)
    market_cap: float | None = None
    current_price: float | None = None
    shares_outstanding: float | None = None
    face_value: float | None = None
    listing_date: str | None = None
    website: str | None = None
    description: str | None = None
    ceo: str | None = None
    employees: int | None = None
    headquarters: str | None = None
    listing_status: str = "active"
    index_membership: str | None = None


class CompanyUpdate(BaseModel):
    """Partial update; only the provided fields change."""

    name: str | None = Field(default=None, max_length=200)
    ticker: str | None = Field(default=None, max_length=32)
    exchange: str | None = None
    isin: str | None = Field(default=None, max_length=16)
    bse_code: str | None = Field(default=None, max_length=16)
    sector: str | None = Field(default=None, max_length=120)
    industry: str | None = Field(default=None, max_length=120)
    market_cap: float | None = None
    current_price: float | None = None
    shares_outstanding: float | None = None
    face_value: float | None = None
    listing_date: str | None = None
    website: str | None = None
    description: str | None = None
    ceo: str | None = None
    employees: int | None = None
    headquarters: str | None = None
    listing_status: str | None = None
    index_membership: str | None = None
    logo_url: str | None = None
    favicon_url: str | None = None


class CompanyVersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    company_id: str
    version: int
    actor_id: str | None = None
    actor_email: str | None = None
    changes: dict[str, Any] | None = None
    change_type: str
    summary: str
    created_at: datetime


class CompanyBulkEditItem(BaseModel):
    """One row of the bulk spreadsheet editor."""

    id: str | None = None
    name: str | None = None
    ticker: str | None = None
    exchange: str | None = None
    isin: str | None = None
    bse_code: str | None = None
    sector: str | None = None
    industry: str | None = None
    market_cap: float | None = None
    face_value: float | None = None
    website: str | None = None
    description: str | None = None


class CompanyBulkEditRequest(BaseModel):
    """Apply a set of in-place edits (by ticker or id)."""

    items: list[CompanyBulkEditItem]


class CompanyBulkEditResult(BaseModel):
    updated: int
    created: int
    errors: list[dict[str, str]]


class ImportResult(BaseModel):
    imported: int
    updated: int
    skipped: int
    errors: list[dict[str, str]]


class MergeResult(BaseModel):
    kept_id: str
    kept_ticker: str
    merged_ids: list[str]
    removed_count: int


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
    market: LiveMarket | None = Field(None, description="Live market view")


class SearchResponse(BaseModel):
    query: str
    total: int
    results: list[CompanySummary]


class PaginatedCompanies(BaseModel):
    total: int
    page: int
    page_size: int
    results: list[CompanySummary]
