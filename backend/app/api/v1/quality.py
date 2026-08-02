"""Data Quality Score API.

Read paths are open to any authenticated user: knowing how good the data is
must never be harder than reading the data itself. The refresh-all sweep is
operator-only because it walks the universe.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import CurrentUser, get_current_user
from app.db.base import get_db
from app.models.company import Company

router = APIRouter(tags=["quality"])


def _company(db: Session, ticker: str) -> Company:
    company = db.scalar(
        select(Company).where(Company.ticker == ticker.upper())
    )
    if company is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            f"unknown ticker {ticker}")
    return company


def _require_operator(user: CurrentUser) -> None:
    role = str(getattr(user, "role", "") or "").lower()
    if role not in ("admin", "super_admin", "tenant_admin", "operator"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "operator role required")


@router.get("/company/{ticker}/quality",
            summary="Data Quality Score for one company")
def company_quality(
    ticker: str,
    refresh: bool = Query(
        default=False,
        description="Recompute and persist rather than serving the snapshot.",
    ),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Score, grade, per-dimension breakdown, missing items and freshness.

    Computed live rather than served from the snapshot by default. A score is
    ~0.7s, and a user asking "how good is this data" deserves the answer as it
    stands now rather than as it stood at the last sweep — the score exists
    precisely to be trusted about currency.
    """
    from app.services.quality.service import QualitySnapshotService

    company = _company(db, ticker)
    service = QualitySnapshotService(db)
    # `refresh` also persists; the default path scores without writing, so a
    # read cannot be a write for an ordinary user.
    result = (
        service.refresh(company) if refresh
        else service.scorer.score_company(company)
    )
    return result.as_dict()


@router.get("/quality/scheme",
            summary="The scoring scheme: dimensions, weights and checks")
def scheme(
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Published so a score can be audited rather than taken on trust."""
    from app.domain.quality.score import (
        CHECKS, GRADE_BANDS, WARN_BELOW, WEIGHTS,
    )

    return {
        "total": sum(WEIGHTS.values()),
        "warn_below": WARN_BELOW,
        "dimensions": [
            {
                "dimension": dimension.value,
                "weight": WEIGHTS[dimension],
                "checks": list(CHECKS[dimension]),
            }
            for dimension in WEIGHTS
        ],
        "grades": [
            {"min_score": floor, "grade": grade.value}
            for floor, grade in GRADE_BANDS
        ],
    }


@router.get("/quality/dashboard", summary="Universe-wide data quality")
def dashboard(
    top: int = Query(default=10, ge=1, le=50),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Average score, leaderboards, grade distribution and threshold counts.

    Served from persisted snapshots: scoring 500 companies on request would
    take minutes.
    """
    from app.services.quality.service import QualitySnapshotService

    return QualitySnapshotService(db).dashboard(top=top)


@router.post("/quality/refresh", summary="Rescore every company")
def refresh_all(
    limit: int | None = Query(default=None, ge=1, le=1000),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Recompute and persist the universe. The nightly sweep does this too."""
    _require_operator(user)
    from app.services.quality.service import QualitySnapshotService

    return QualitySnapshotService(db).refresh_all(limit=limit)
