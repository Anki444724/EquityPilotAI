"""Dashboard read models — aggregated market and coverage overview."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import distinct, func, select
from sqlalchemy.orm import Session

from app.core.security import CurrentUser, get_current_user
from app.db.base import get_db
from app.models.company import Company, FinancialFact
from app.schemas.company import CompanySummary
from app.services.live_market import LiveMarketService

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


class CoverageStats(BaseModel):
    companies: int
    companies_with_financials: int
    sectors: int
    fact_rows: int
    fiscal_years: list[int]


class SectorBreakdown(BaseModel):
    sector: str
    count: int
    market_cap: float = Field(0, description="₹ crore")


class DashboardOverview(BaseModel):
    coverage: CoverageStats
    sectors: list[SectorBreakdown]
    largest: list[CompanySummary]
    recently_added: list[CompanySummary]


@router.get("/overview", response_model=DashboardOverview, summary="Dashboard overview")
def overview(
    db: Session = Depends(get_db),
    _: CurrentUser = Depends(get_current_user),
) -> DashboardOverview:
    companies = db.execute(select(func.count(Company.id))).scalar_one()
    with_fin = db.execute(
        select(func.count(distinct(FinancialFact.company_id)))
    ).scalar_one()
    n_sectors = db.execute(
        select(func.count(distinct(Company.sector))).where(Company.sector.is_not(None))
    ).scalar_one()
    fact_rows = db.execute(select(func.count(FinancialFact.id))).scalar_one()
    years = [
        y for (y,) in db.execute(
            select(distinct(FinancialFact.fiscal_year)).order_by(FinancialFact.fiscal_year)
        ).all()
    ]

    sector_rows = db.execute(
        select(
            Company.sector,
            func.count(Company.id),
            func.coalesce(func.sum(Company.market_cap), 0.0),
        )
        .where(Company.sector.is_not(None))
        .group_by(Company.sector)
        .order_by(func.count(Company.id).desc())
    ).all()

    largest = db.execute(
        select(Company).order_by(Company.market_cap.desc().nullslast()).limit(8)
    ).scalars().all()
    recent = db.execute(
        select(Company).order_by(Company.created_at.desc()).limit(8)
    ).scalars().all()

    market = LiveMarketService(db).bulk_quotes(list(largest) + list(recent))
    largest_out = [
        CompanySummary.model_validate(c).model_copy(
            update={"market": market.get(c.ticker)}
        )
        for c in largest
    ]
    recent_out = [
        CompanySummary.model_validate(c).model_copy(
            update={"market": market.get(c.ticker)}
        )
        for c in recent
    ]

    return DashboardOverview(
        coverage=CoverageStats(
            companies=companies,
            companies_with_financials=with_fin,
            sectors=n_sectors,
            fact_rows=fact_rows,
            fiscal_years=years,
        ),
        sectors=[
            SectorBreakdown(sector=s, count=c, market_cap=float(m))
            for s, c, m in sector_rows
        ],
        largest=largest_out,
        recently_added=recent_out,
    )
