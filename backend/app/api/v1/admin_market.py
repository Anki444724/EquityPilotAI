"""Market Operations Center endpoints (Phase 4).

Provider registry & health, manual overrides, realtime dashboard, cache
manager, scheduler, websocket monitor, historical sync and logs.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from app.core.security import CurrentUser, get_current_user, require
from app.db.base import get_db
from app.domain.platform.identity import Permission
from app.schemas.market_ops import (
    MarketOverrideIn, MarketOverrideOut, ProviderHealthOut, ProviderInfoOut,
)
from app.services.market_ops import MarketOpsError, MarketOpsService

router = APIRouter(prefix="/admin/market", tags=["admin-market"])


def _service(db: Session = Depends(get_db)) -> MarketOpsService:
    return MarketOpsService(db)


def _override_out(ov) -> MarketOverrideOut:
    return MarketOverrideOut(
        id=ov.id, company_id=ov.company_id, ticker=ov.ticker,
        manual_price=ov.manual_price, manual_volume=ov.manual_volume,
        manual_market_cap=ov.manual_market_cap, manual_pe=ov.manual_pe,
        manual_pb=ov.manual_pb, reason=ov.reason, expires_at=ov.expires_at,
        auto_revert=ov.auto_revert, created_by_email=ov.created_by_email,
        created_at=ov.created_at, is_active=ov.is_active,
    )


# ---------------------------------------------------------------- providers
@router.get(
    "/providers", response_model=list[ProviderInfoOut],
    summary="Provider registry & health",
    dependencies=[Depends(require(Permission.COMPANY_READ))],
)
def list_providers(svc: MarketOpsService = Depends(_service)):
    return svc.provider_registry()


@router.get(
    "/providers/health", response_model=list[ProviderHealthOut],
    summary="Provider health snapshot",
    dependencies=[Depends(require(Permission.COMPANY_READ))],
)
def provider_health(svc: MarketOpsService = Depends(_service)):
    return svc.provider_health()


# --------------------------------------------------------------- overrides
@router.get(
    "/overrides", response_model=list[MarketOverrideOut],
    summary="List market overrides",
    dependencies=[Depends(require(Permission.COMPANY_READ))],
)
def list_overrides(
    active_only: bool = Query(True), svc: MarketOpsService = Depends(_service),
):
    return [_override_out(o) for o in svc.list_overrides(active_only=active_only)]


@router.post(
    "/overrides/{company_id}", response_model=MarketOverrideOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a manual override for a company",
    dependencies=[Depends(require(Permission.COMPANY_WRITE))],
)
def create_override(
    company_id: str, body: MarketOverrideIn, svc: MarketOpsService = Depends(_service),
    user: CurrentUser = Depends(get_current_user),
):
    try:
        ov = svc.create_override(
            company_id, body, actor_id=user.user_id, actor_email=user.email,
        )
    except MarketOpsError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    svc.db.commit()
    return _override_out(ov)


@router.delete(
    "/overrides/{override_id}", summary="Clear a manual override (auto-revert)",
    dependencies=[Depends(require(Permission.COMPANY_WRITE))],
)
def clear_override(override_id: int, svc: MarketOpsService = Depends(_service)):
    try:
        svc.clear_override(override_id)
    except MarketOpsError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    svc.db.commit()
    return {"status": "cleared"}


@router.delete(
    "/overrides", summary="Clear all active overrides",
    dependencies=[Depends(require(Permission.COMPANY_WRITE))],
)
def clear_all_overrides(svc: MarketOpsService = Depends(_service)):
    count = svc.clear_all()
    svc.db.commit()
    return {"status": "cleared", "cleared": count}


# ---------------------------------------------------------------- dashboard
@router.get(
    "/dashboard", summary="Realtime market-operations dashboard",
    dependencies=[Depends(require(Permission.COMPANY_READ))],
)
def dashboard(svc: MarketOpsService = Depends(_service)):
    return svc.dashboard()


# ------------------------------------------------------------ cache manager
@router.post(
    "/cache/clear", summary="Clear the market cache",
    dependencies=[Depends(require(Permission.COMPANY_WRITE))],
)
def clear_cache(svc: MarketOpsService = Depends(_service)):
    svc.clear_cache()
    return {"status": "cleared"}


@router.post(
    "/cache/refresh", summary="Refresh the market cache",
    dependencies=[Depends(require(Permission.COMPANY_WRITE))],
)
def refresh_cache(svc: MarketOpsService = Depends(_service)):
    svc.refresh_cache()
    return {"status": "refreshed"}


# ------------------------------------------------- scheduler / sync / ws / logs
@router.get(
    "/scheduler", summary="Scheduler status",
    dependencies=[Depends(require(Permission.COMPANY_READ))],
)
def scheduler(svc: MarketOpsService = Depends(_service)):
    return svc.scheduler_status()


@router.get(
    "/sync", summary="Historical sync status",
    dependencies=[Depends(require(Permission.COMPANY_READ))],
)
def sync_status(svc: MarketOpsService = Depends(_service)):
    return svc.sync_status()


@router.get(
    "/websocket", summary="WebSocket monitor status",
    dependencies=[Depends(require(Permission.COMPANY_READ))],
)
def websocket(svc: MarketOpsService = Depends(_service)):
    return svc.websocket_status()


@router.get(
    "/logs", summary="Market & provider logs",
    dependencies=[Depends(require(Permission.COMPANY_READ))],
)
def logs(level: str | None = None, limit: int = Query(100, ge=1, le=500),
         svc: MarketOpsService = Depends(_service)):
    return svc.logs(level=level, limit=limit)
