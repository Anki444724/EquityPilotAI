"""AI Scoring Engine 3.0 endpoints.

    GET  /ai-score/framework                     the framework definition
    GET  /ai-score/dashboard                     universe-level summary
    GET  /company/{ticker}/ai-score              compute a fresh score
    POST /company/{ticker}/ai-score/recalculate  compute and record a version
    GET  /company/{ticker}/ai-score/history      every retained version
    GET  /company/{ticker}/ai-score/version/{n}  one historical version, whole

`/ai-score/framework` is registered as a static path and the router is
included before `market.router` for the same reason the filings admin router
is: a sibling declaring `/{ticker}` captures a literal segment registered after
it, which is the ROUTE-001 defect. Here both live in this module, so the static
routes are simply declared first.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.security import CurrentUser, get_current_user
from app.db.base import get_db
from app.domain.ai_scoring.framework import (
    FRAMEWORK_VERSION, GUARDRAILS, MIN_COVERAGE_FOR_DIRECTION, MODULE_CRITERIA,
    MODULE_LABELS, MODULE_ORDER, MODULE_WEIGHTS, PROVISIONAL_COVERAGE,
)
from app.domain.ai_scoring.probability import PROBABILITY_SPECS
from app.domain.ai_scoring.types import (
    AIScoreResult, RATING_BANDS, RECOMMENDATION_BANDS,
)
from app.models.company import Company
from app.schemas.common import CompanyRef
from app.schemas.scoring import (
    AIFrameworkResponse, AIScoreHistoryResponse, AIScoreResponse,
    AIScoreVersionOut, ModuleCriterionOut,
)
from app.services.ai_scoring.service import AIScoringError, AIScoringService

router = APIRouter(tags=["ai-scoring"])


def _service(db: Session = Depends(get_db)) -> AIScoringService:
    return AIScoringService(db)


def _company(db: Session, ticker: str) -> Company:
    company = db.execute(
        select(Company).where(func.upper(Company.ticker) == ticker.upper())
    ).scalars().first()
    if company is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            f"company '{ticker}' not found")
    return company


def _response(result: AIScoreResult, **extra) -> AIScoreResponse:
    payload = result.as_dict()
    payload.update(extra)
    return AIScoreResponse(**payload)


# --------------------------------------------------------------- framework
# Declared before the /company/{ticker}/... routes purely for readability;
# they do not collide, because the prefixes differ at the first segment.
@router.get("/ai-score/framework", response_model=AIFrameworkResponse,
            summary="The scoring framework: modules, weights and guardrails")
def get_framework(
    _: CurrentUser = Depends(get_current_user),
) -> AIFrameworkResponse:
    """Publish the framework so a score can be argued with.

    A rating nobody can inspect the derivation of is a black box regardless of
    how much explanation accompanies an individual number, so the weights, the
    bands and the guardrails are all served as data.
    """
    return AIFrameworkResponse(
        version=FRAMEWORK_VERSION,
        modules=[
            ModuleCriterionOut(
                key=module.value,
                label=MODULE_LABELS[module],
                weight=MODULE_WEIGHTS[module],
                criteria=list(MODULE_CRITERIA[module]),
            )
            for module in MODULE_ORDER
        ],
        total_weight=sum(MODULE_WEIGHTS.values()),
        rating_bands=[
            {"at_or_above": threshold, "rating": rating.value,
             "description": description}
            for threshold, rating, description in RATING_BANDS
        ],
        recommendation_bands=[
            {"at_or_above": threshold, "recommendation": rec.value}
            for threshold, rec in RECOMMENDATION_BANDS
        ],
        guardrails=[
            {"key": rail.key, "module": rail.module.value,
             "fires_at_or_below": rail.at_or_below, "caps_at": rail.cap.value}
            for rail in GUARDRAILS
        ] + [
            {"key": "thin_evidence", "module": "(all)",
             "fires_at_or_below": MIN_COVERAGE_FOR_DIRECTION,
             "caps_at": "Hold"}
        ],
        probability_specs=[
            {"key": spec.key, "label": spec.label,
             "drivers": [{"module": m.value, "coefficient": c}
                         for m, c in spec.drivers]}
            for spec in PROBABILITY_SPECS
        ],
        notes=[
            "No rating is generated from a prompt or a model opinion. Every "
            "figure is arithmetic over observed inputs.",
            "The Risk module and the Valuation module both score 10 = "
            "favourable: low risk and cheap respectively.",
            "A missing input scores the neutral midpoint and reduces "
            "coverage; it is never scored as zero.",
            f"Coverage below {PROVISIONAL_COVERAGE:.0%} marks a score "
            "provisional; below "
            f"{MIN_COVERAGE_FOR_DIRECTION:.0%} the recommendation is capped "
            "at Hold.",
            "Historical score versions are never overwritten.",
        ],
    )


@router.get("/ai-score/dashboard",
            summary="Universe-level summary of current AI scores")
def get_dashboard(
    service: AIScoringService = Depends(_service),
    _: CurrentUser = Depends(get_current_user),
) -> dict:
    return service.dashboard()


# ------------------------------------------------------------------ scoring
@router.get("/company/{ticker}/ai-score", response_model=AIScoreResponse,
            summary="Explainable AI score across the ten framework modules")
def get_ai_score(
    ticker: str,
    db: Session = Depends(get_db),
    service: AIScoringService = Depends(_service),
    _: CurrentUser = Depends(get_current_user),
) -> AIScoreResponse:
    """Compute a fresh score. Read-only — nothing is recorded.

    Deliberately does not write a version: a GET that mutates the permanent
    history would mean the act of looking at a company changed its record, and
    a dashboard polling fifty companies would manufacture fifty versions an
    hour.
    """
    company = _company(db, ticker)
    try:
        result = service.score_company(company)
    except AIScoringError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    current = service.current(company.id)
    return _response(
        result,
        version=current.version if current else None,
        version_created=False,
        version_note=(
            "Live computation. Not recorded — use POST "
            f"/company/{ticker}/ai-score/recalculate to append a version."
        ),
    )


@router.post("/company/{ticker}/ai-score/recalculate",
             response_model=AIScoreResponse,
             summary="Recalculate and permanently record a new score version")
def recalculate(
    ticker: str,
    force: bool = Query(
        False,
        description=("record a version even when the inputs are unchanged; "
                     "normally an unchanged fingerprint writes nothing"),
    ),
    db: Session = Depends(get_db),
    service: AIScoringService = Depends(_service),
    _: CurrentUser = Depends(get_current_user),
) -> AIScoreResponse:
    company = _company(db, ticker)
    try:
        result, outcome = service.score_and_record(
            company, trigger="manual", force=force,
        )
    except AIScoringError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    return _response(
        result,
        version=outcome.version.version if outcome.version else None,
        version_created=outcome.created,
        version_note=outcome.reason,
    )


# ------------------------------------------------------------------ history
@router.get("/company/{ticker}/ai-score/history",
            response_model=AIScoreHistoryResponse,
            summary="Every retained score version — nothing is ever deleted")
def get_history(
    ticker: str,
    limit: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
    service: AIScoringService = Depends(_service),
    _: CurrentUser = Depends(get_current_user),
) -> AIScoreHistoryResponse:
    company = _company(db, ticker)
    versions = service.history(company.id, limit=limit)
    frameworks = {v.framework_version for v in versions}

    return AIScoreHistoryResponse(
        company=CompanyRef(
            id=company.id, name=company.name, ticker=company.ticker,
            sector=company.sector, industry=company.industry,
        ),
        framework_version=FRAMEWORK_VERSION,
        versions_retained=len(versions),
        versions=[
            AIScoreVersionOut(
                version=v.version, status=v.status,
                framework_version=v.framework_version,
                overall_score=v.overall_score, rating=v.rating,
                recommendation=v.recommendation, coverage=v.coverage,
                module_scores=v.module_scores or {},
                probabilities=v.probabilities or {},
                summary=v.summary, input_fingerprint=v.input_fingerprint,
                total_citations=v.total_citations, trigger=v.trigger,
                trigger_document_id=v.trigger_document_id,
                supersedes_version=v.supersedes_version,
                score_delta=v.score_delta, computed_at=v.computed_at,
            )
            for v in versions
        ],
        # Surfaced rather than left for the caller to notice: a trend line
        # drawn across two framework versions compares two different
        # questions, and the chart cannot tell on its own.
        spans_framework_versions=len(frameworks) > 1,
    )


@router.get("/company/{ticker}/ai-score/version/{version}",
            summary="One historical version in full, exactly as recorded")
def get_version(
    ticker: str,
    version: int,
    db: Session = Depends(get_db),
    service: AIScoringService = Depends(_service),
    _: CurrentUser = Depends(get_current_user),
) -> dict:
    """Return a stored version verbatim.

    The stored `detail` payload is returned as recorded rather than
    re-rendered through the current schema. A version written under an older
    framework must read back as it was written; passing it through today's
    response model would silently reshape history.
    """
    company = _company(db, ticker)
    row = service.version(company.id, version)
    if row is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"no version {version} recorded for '{ticker}'",
        )
    return {
        "version": row.version,
        "status": row.status,
        "framework_version": row.framework_version,
        "trigger": row.trigger,
        "trigger_document_id": row.trigger_document_id,
        "supersedes_version": row.supersedes_version,
        "score_delta": row.score_delta,
        "computed_at": row.computed_at,
        "input_fingerprint": row.input_fingerprint,
        "detail": row.detail,
    }
