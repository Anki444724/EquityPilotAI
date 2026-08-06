"""AI Operations Center endpoints (Phase 5).

AI score overrides, model registry, prompt catalog, cost dashboard, queue,
learning, RAG and logs.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.security import CurrentUser, get_current_user, require
from app.db.base import get_db
from app.domain.platform.identity import Permission
from app.schemas.ai_ops import AIOverrideIn, AIOverrideOut
from app.services.ai_ops import AIOpsError, AIOpsService

router = APIRouter(prefix="/admin/ai", tags=["admin-ai"])


def _service(db: Session = Depends(get_db)) -> AIOpsService:
    return AIOpsService(db)


def _override_out(ov) -> AIOverrideOut:
    return AIOverrideOut(
        id=ov.id, company_id=ov.company_id, ticker=ov.ticker, mode=ov.mode,
        manual_score=ov.manual_score, manual_confidence=ov.manual_confidence,
        manual_risk=ov.manual_risk, manual_summary=ov.manual_summary,
        manual_bull_case=ov.manual_bull_case, manual_bear_case=ov.manual_bear_case,
        manual_recommendation=ov.manual_recommendation, reason=ov.reason,
        expires_at=ov.expires_at, created_by_email=ov.created_by_email,
        created_at=ov.created_at, is_active=ov.is_active,
    )


# -------------------------------------------------------------- overrides
@router.get(
    "/overrides", response_model=list[AIOverrideOut],
    summary="List AI score overrides",
    dependencies=[Depends(require(Permission.COMPANY_READ))],
)
def list_overrides(active_only: bool = Query(True), svc: AIOpsService = Depends(_service)):
    return [_override_out(o) for o in svc.list_overrides(active_only=active_only)]


@router.post(
    "/overrides/{company_id}", response_model=AIOverrideOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a manual AI score override",
    dependencies=[Depends(require(Permission.COMPANY_WRITE))],
)
def create_override(
    company_id: str, body: AIOverrideIn, svc: AIOpsService = Depends(_service),
    user: CurrentUser = Depends(get_current_user),
):
    try:
        ov = svc.create_override(
            company_id, body.model_dump(exclude_none=True),
            actor_id=user.user_id, actor_email=user.email,
        )
    except AIOpsError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    svc.db.commit()
    return _override_out(ov)


@router.delete(
    "/overrides/{override_id}", summary="Clear an AI override (revert to auto)",
    dependencies=[Depends(require(Permission.COMPANY_WRITE))],
)
def clear_override(override_id: int, svc: AIOpsService = Depends(_service)):
    try:
        svc.clear_override(override_id)
    except AIOpsError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    svc.db.commit()
    return {"status": "cleared", "mode": "auto"}


# -------------------------------------------------------------- models
@router.get(
    "/models", summary="AI model registry",
    dependencies=[Depends(require(Permission.COMPANY_READ))],
)
def models(svc: AIOpsService = Depends(_service)):
    return svc.models()


# -------------------------------------------------------------- prompts
@router.get(
    "/prompts", summary="Prompt catalog",
    dependencies=[Depends(require(Permission.COMPANY_READ))],
)
def prompts(svc: AIOpsService = Depends(_service)):
    return svc.prompts()


# -------------------------------------------------------------- cost
@router.get(
    "/cost", summary="AI cost dashboard",
    dependencies=[Depends(require(Permission.COMPANY_READ))],
)
def cost(days: int = Query(30, ge=1, le=365), svc: AIOpsService = Depends(_service)):
    return svc.cost_dashboard(days=days)


# ------------------------------------------------- queue / learning / rag / logs
@router.get(
    "/queue", summary="AI queue status",
    dependencies=[Depends(require(Permission.COMPANY_READ))],
)
def queue(svc: AIOpsService = Depends(_service)):
    return svc.queue_status()


@router.get(
    "/learning", summary="Learning & feedback status",
    dependencies=[Depends(require(Permission.COMPANY_READ))],
)
def learning(svc: AIOpsService = Depends(_service)):
    return svc.learning_status()


@router.get(
    "/rag", summary="RAG / retrieval status",
    dependencies=[Depends(require(Permission.COMPANY_READ))],
)
def rag(svc: AIOpsService = Depends(_service)):
    return svc.rag_status()


@router.get(
    "/logs", summary="AI logs (prompt/response/latency/errors)",
    dependencies=[Depends(require(Permission.COMPANY_READ))],
)
def logs(limit: int = Query(100, ge=1, le=500), svc: AIOpsService = Depends(_service)):
    return svc.logs(limit=limit)
