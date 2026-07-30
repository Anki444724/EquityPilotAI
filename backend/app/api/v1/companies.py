"""Company endpoints — search, list, detail, profile."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.security import CurrentUser, get_current_user
from app.db.base import get_db
from app.schemas.company import (
    CompanyDetail, CompanyProfile, PaginatedCompanies, SearchResponse,
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
    company = svc.get(company_id)
    if company is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "company not found")
    return CompanyDetail.model_validate(company)


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
