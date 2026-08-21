"""Company endpoints — search, list, detail, profile."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.security import CurrentUser, get_current_user
from app.db.base import get_db
from app.schemas.company import (
    CompanyDataStatus, CompanyDetail, CompanyPrices, CompanyProfile,
    CompanyQuote, PaginatedCompanies, PriceBar, SearchResponse,
)
from app.services.company_service import CompanyService

router = APIRouter(prefix="/companies", tags=["companies"])


def _service(db: Session = Depends(get_db)) -> CompanyService:
    return CompanyService(db)


@router.get("/search", response_model=SearchResponse, summary="Fast company search")
def search_companies(
    q: str = Query("", description="Name, ticker or sector"),
    limit: int = Query(20, ge=1, le=50),
    svc: CompanyService = Depends(_service),
    _: CurrentUser = Depends(get_current_user),
) -> SearchResponse:
    results = svc.search(q, limit)
    return SearchResponse(query=q, total=len(results), results=results)


@router.get("/sectors", response_model=list[str], summary="Distinct sectors")
def list_sectors(
    svc: CompanyService = Depends(_service),
    _: CurrentUser = Depends(get_current_user),
) -> list[str]:
    return svc.sectors()


@router.get("", response_model=PaginatedCompanies, summary="List companies")
def list_companies(
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    sector: str | None = None,
    svc: CompanyService = Depends(_service),
    _: CurrentUser = Depends(get_current_user),
) -> PaginatedCompanies:
    total, results = svc.list_companies(page, page_size, sector)
    return PaginatedCompanies(
        total=total, page=page, page_size=page_size, results=results
    )


@router.get("/{company_id}", response_model=CompanyDetail, summary="Company detail")
def get_company(
    company_id: str,
    svc: CompanyService = Depends(_service),
    _: CurrentUser = Depends(get_current_user),
) -> CompanyDetail:
    detail = svc.get_detail(company_id)
    if detail is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "company not found")
    return detail


@router.get(
    "/{company_id}/profile",
    response_model=CompanyProfile,
    summary="Company profile with headline financials",
)
def get_company_profile(
    company_id: str,
    svc: CompanyService = Depends(_service),
    _: CurrentUser = Depends(get_current_user),
) -> CompanyProfile:
    profile = svc.profile(company_id)
    if profile is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "company not found")
    return profile


# =========================================================================
# Phase 1: market data + availability (additive; existing routes untouched)
# =========================================================================
@router.get(
    "/{company_id}/quote", response_model=CompanyQuote,
    summary="Latest persisted market quote",
)
def get_company_quote(
    company_id: str,
    db: Session = Depends(get_db),
    _: CurrentUser = Depends(get_current_user),
) -> CompanyQuote:
    """The durable quote written by the `price_sync` job — OHLC, 52-week
    range, market status and the provider that produced it. Redis stays the
    fast path for pages that read the embedded `market` block; this endpoint
    answers from the database and labels its provenance ('mock' rows come
    from the deterministic mock provider and say so)."""
    from sqlalchemy import select as _select

    from app.models.company import Company
    from app.services.market.persistence import latest_quote

    company = db.scalar(_select(Company).where(Company.id == company_id))
    if company is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "company not found")
    row = latest_quote(db, company.id)
    if row is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "no persisted quote yet — the price sync job has not reached "
            "this company",
        )
    data_kind = "mock" if (row.provider or "").startswith("mock") else "real"
    return CompanyQuote(
        company_id=company.id, ticker=company.ticker,
        exchange=row.exchange or company.exchange,
        ltp=row.ltp, previous_close=row.previous_close,
        day_open=row.day_open, day_high=row.day_high, day_low=row.day_low,
        volume=row.volume, change=row.change_amt,
        change_percent=row.change_percent,
        week_52_high=row.week_52_high, week_52_low=row.week_52_low,
        market_status=row.market_status, provider=row.provider,
        data_kind=data_kind, fetched_at=row.fetched_at,
    )


@router.get(
    "/{company_id}/prices", response_model=CompanyPrices,
    summary="Historical daily prices",
)
def get_company_prices(
    company_id: str,
    range: str = Query("1M", description="1D|1W|1M|3M|6M|1Y|3Y|5Y|MAX"),
    db: Session = Depends(get_db),
    _: CurrentUser = Depends(get_current_user),
) -> CompanyPrices:
    """Daily OHLCV bars from `price_history` (written idempotently by the
    `historical_price_sync` job). Granularity is daily — intraday candles
    arrive only with a licensed feed, and are not implied here. `1D` returns
    the most recent trading day's bar."""
    from sqlalchemy import select as _select

    from app.models.company import Company
    from app.services.market.persistence import bars_for_range

    company = db.scalar(_select(Company).where(Company.id == company_id))
    if company is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "company not found")
    range_name = (range or "1M").upper()
    if range_name not in {"1D", "1W", "1M", "3M", "6M", "1Y", "3Y", "5Y", "MAX"}:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"unknown range '{range_name}'",
        )
    bars = bars_for_range(db, company.ticker, range_name)
    provider = bars[-1].provider if bars else None
    data_kind = "mock" if (provider or "").startswith("mock") else "real"
    return CompanyPrices(
        company_id=company.id, ticker=company.ticker,
        exchange=company.exchange, range=range_name,
        provider=provider, data_kind=data_kind,
        bars=[
            PriceBar(
                date=bar.as_of.isoformat(), open=bar.day_open,
                high=bar.day_high, low=bar.day_low, close=bar.close,
                volume=bar.volume,
            )
            for bar in bars
        ],
    )


@router.get(
    "/{company_id}/data-status", response_model=CompanyDataStatus,
    summary="What data exists for a company, and its provenance",
)
def get_company_data_status(
    company_id: str,
    db: Session = Depends(get_db),
    _: CurrentUser = Depends(get_current_user),
) -> CompanyDataStatus:
    """Availability surface for the 5,000-company universe: financials,
    quarterlies, shareholding, quote and price history — each counted, with
    the sources that supplied them, so 'no data' is a truthful labelled state
    rather than an empty page."""
    from sqlalchemy import func, select as _select

    from app.models.analysis import QuarterlyResult, ShareholdingSnapshot
    from app.models.company import Company, FinancialFact
    from app.models.market import MarketQuote
    from app.models.portfolio import PriceHistory

    company = db.scalar(_select(Company).where(Company.id == company_id))
    if company is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "company not found")

    fact_rows = db.execute(
        _select(
            FinancialFact.line_item, FinancialFact.fiscal_year,
            FinancialFact.source,
        ).where(FinancialFact.company_id == company.id)
    ).all()
    quarterly_count = int(db.scalar(
        _select(func.count()).select_from(QuarterlyResult)
        .where(QuarterlyResult.company_id == company.id)
    ) or 0)
    shareholding_count = int(db.scalar(
        _select(func.count()).select_from(ShareholdingSnapshot)
        .where(ShareholdingSnapshot.company_id == company.id)
    ) or 0)
    quote = db.get(MarketQuote, company.id)
    price_bars = int(db.scalar(
        _select(func.count()).select_from(PriceHistory)
        .where(PriceHistory.ticker == company.ticker)
    ) or 0)

    years = {row.fiscal_year for row in fact_rows}
    sources = sorted({row.source for row in fact_rows if row.source})
    has_mock_source = any(s and s.startswith("mock") for s in sources)

    return CompanyDataStatus(
        company_id=company.id, ticker=company.ticker,
        has_financials=bool(fact_rows),
        fact_count=len(fact_rows),
        fiscal_years=len(years),
        latest_fiscal_year=max(years) if years else None,
        quarterly_count=quarterly_count,
        shareholding_count=shareholding_count,
        financial_sources=sources,
        has_quote=quote is not None,
        quote_provider=(quote.provider if quote else None)
        or ("mock" if has_mock_source else None),
        price_bars=price_bars,
        metadata_source=company.metadata_source,
        metadata_synced_at=company.metadata_synced_at,
        data_version=company.data_version or 1,
    )
