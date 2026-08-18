"""Platform-operator endpoints to run and monitor the universe financials backfill.

The backfill itself lives in `FinancialsBackfillService`; these routes wire it
into the production pipeline by enqueuing it on the platform job queue, where
the worker runs it in the background rather than inside a request. That is the
same path `embedding_backfill` and the other scheduled sweeps use, so the
scheduled daily pass and an on-demand operator run share one implementation.

    -- operator console ----------------------------------------------
    GET  /platform/financials/backfill    coverage + most recent run
    POST /platform/financials/backfill    enqueue a run

The POST body either targets specific tickers (``{"tickers": ["NHPC"]}``) or,
with no tickers, runs the next bounded universe sweep (default 25 companies).
All routes are operator-only. Reads require `SYSTEM_READ`; writes additionally
require `JOB_MANAGE` — the same guard the generic job endpoints use.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.orm import Session

from app.core.security import (
    CurrentUser, get_current_user, require, require_operator,
)
from app.db.base import get_db
from app.domain.platform.audit import AuditAction
from app.domain.platform.identity import Permission
from app.domain.platform.jobs import JobKind, JOB_LABELS
from app.models.platform import BackgroundJob
from app.schemas.platform import (
    FinancialsBackfillCoverage, FinancialsBackfillStatus,
    FinancialsBackfillTrigger, JobOut,
)
from app.services.platform.audit_service import AuditService, RequestContext
from app.services.platform.jobs.queue import JobQueue
from app.services.universe.financials_backfill import FinancialsBackfillService

router = APIRouter(prefix="/platform/financials", tags=["admin-backfill"])


def _context(request: Request) -> RequestContext:
    from app.core.security import _client_ip

    return RequestContext(
        ip_address=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
        request_id=getattr(request.state, "request_id", None),
    )


def _latest_job(db: Session) -> BackgroundJob | None:
    rows, _ = JobQueue(db).list(kind=JobKind.FINANCIALS_BACKFILL, limit=1)
    return rows[0] if rows else None


@router.get(
    "/backfill", response_model=FinancialsBackfillStatus,
    summary="Financial coverage and the most recent backfill run",
    dependencies=[Depends(require_operator),
                  Depends(require(Permission.SYSTEM_READ))],
)
def financials_backfill_status(db: Session = Depends(get_db)) -> FinancialsBackfillStatus:
    coverage = FinancialsBackfillService(db).coverage_snapshot()
    latest = _latest_job(db)
    return FinancialsBackfillStatus(
        coverage=FinancialsBackfillCoverage(**coverage),
        latest_job=JobOut.model_validate(latest) if latest else None,
    )


@router.post(
    "/backfill", response_model=JobOut, status_code=status.HTTP_201_CREATED,
    summary="Enqueue a universe financials backfill sweep",
    dependencies=[Depends(require_operator)],
)
def enqueue_financials_backfill(
    body: FinancialsBackfillTrigger,
    request: Request,
    user: CurrentUser = Depends(require(Permission.JOB_MANAGE)),
    db: Session = Depends(get_db),
) -> JobOut:
    payload: dict[str, object] = {}
    if body.limit is not None:
        payload["limit"] = body.limit
    if body.tickers:
        payload["tickers"] = body.tickers
    job = JobQueue(db).enqueue(
        JobKind.FINANCIALS_BACKFILL, payload=payload, tenant_id=user.tenant_id,
    )
    AuditService(db).record(
        AuditAction.JOB_ENQUEUED, principal=user,
        resource_type="job", resource_id=job.id,
        summary=f"{JOB_LABELS[JobKind.FINANCIALS_BACKFILL]} enqueued manually",
        context=_context(request),
    )
    return JobOut.model_validate(job)
