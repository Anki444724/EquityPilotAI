"""Scoring endpoints.

    GET /company/{ticker}/scoring              full score across 13 categories
    GET /company/{ticker}/scoring/history      trend from stored snapshots
    GET /company/{ticker}/scoring/explanation  AI-ready narratives
    GET /company/{ticker}/scoring/peers        peer comparison
    GET /scoring/weights                       available weight profiles
    PUT /scoring/weights                       create or update a custom profile
"""
from __future__ import annotations

from statistics import median

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.v1.analysis import get_analysis
from app.core.security import CurrentUser, get_current_user
from app.db.base import get_db
from app.domain.scoring.base import CategoryScore, DataOrigin
from app.domain.scoring.weights import Category, WeightProfile
from app.models.company import Company
from app.schemas.common import CompanyRef
from app.schemas.scoring import (
    CategoryScoreOut, ConfidenceOut, ExplanationItem, ExplanationResponse,
    HistoryPoint, HistoryResponse, MetricScoreOut, PeerComparisonResponse,
    PeerScoreRow, ScoreResponse, WeightProfileListResponse, WeightProfileOut,
    WeightUpdateRequest,
)
from app.services.analysis_service import AnalysisService
from app.services.forecast.service import ForecastService
from app.services.scoring.overall_score import ScoreResult
from app.services.scoring.service import ScoringError, ScoringService
from app.services.valuation.service import ValuationService

router = APIRouter(tags=["scoring"])


def _services(db: Session = Depends(get_db)):
    return ScoringService(db), ForecastService(db), ValuationService(db)


def _require_data(analysis: AnalysisService) -> None:
    if not analysis.has_data:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "no financial history; the company cannot be scored",
        )


def _confidence_out(breakdown) -> ConfidenceOut:
    return ConfidenceOut(
        confidence=breakdown.confidence, label=breakdown.label,
        verified_pct=breakdown.verified_pct, estimated_pct=breakdown.estimated_pct,
        analyst_pct=breakdown.analyst_pct, missing_pct=breakdown.missing_pct,
        metrics_total=breakdown.metrics_total, metrics_missing=breakdown.metrics_missing,
    )


def _category_out(category: CategoryScore) -> CategoryScoreOut:
    return CategoryScoreOut(
        key=category.key, label=category.label,
        raw_score=category.raw_score, weighted_score=category.weighted_score,
        weight=category.weight, score_pct=category.score_pct,
        grade_hint=category.grade_hint,
        confidence=_confidence_out(category.confidence),
        explanation=category.explanation, data_sources=category.data_sources,
        metrics=[
            MetricScoreOut(
                key=m.key, label=m.label, score=m.score, weight=m.weight,
                origin=m.origin.value, confidence=m.confidence, value=m.value,
                unit=m.unit, explanation=m.explanation, source=m.source,
            )
            for m in category.metrics
        ],
    )


def _score_response(analysis: AnalysisService, result: ScoreResult) -> ScoreResponse:
    return ScoreResponse(
        company=analysis.company_ref(),
        overall_score=result.overall_score, grade=result.grade,
        grade_description=result.grade_description, stars=result.stars,
        recommendation=result.recommendation,
        recommendation_rationale=result.recommendation_rationale,
        conviction=result.conviction,
        profile_key=result.profile_key, profile_label=result.profile_label,
        confidence=_confidence_out(result.confidence),
        categories=[_category_out(c) for c in result.categories],
        strongest=result.strongest, weakest=result.weakest,
        warnings=result.warnings, summary=result.summary,
    )


def _profile_out(profile: WeightProfile) -> WeightProfileOut:
    from app.domain.scoring.weights import CATEGORY_LABELS
    return WeightProfileOut(
        key=profile.key, label=profile.label, description=profile.description,
        is_builtin=profile.is_builtin, weights=profile.weights,
        top_categories=[
            CATEGORY_LABELS[Category(k)] for k, _ in profile.top_categories(3)
        ],
    )


# ------------------------------------------------------------------- scoring
@router.get("/company/{ticker}/scoring", response_model=ScoreResponse,
            summary="Institutional score across 13 categories")
def get_scoring(
    profile: str | None = Query(None, description="balanced | conservative | growth | value | quality | custom key"),
    horizon: int = Query(5),
    save: bool = Query(False, description="persist this run as a history snapshot"),
    analysis: AnalysisService = Depends(get_analysis),
    services=Depends(_services),
    user: CurrentUser = Depends(get_current_user),
) -> ScoreResponse:
    _require_data(analysis)
    scoring, forecast, valuation = services
    try:
        result = scoring.score_company(
            analysis, forecast, valuation, profile_key=profile, horizon=horizon,
        )
    except ScoringError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    if save:
        scoring.save_snapshot(result)
    return _score_response(analysis, result)


@router.get("/company/{ticker}/scoring/explanation", response_model=ExplanationResponse,
            summary="AI-ready score explanations")
def get_explanation(
    profile: str | None = Query(None),
    horizon: int = Query(5),
    analysis: AnalysisService = Depends(get_analysis),
    services=Depends(_services),
) -> ExplanationResponse:
    _require_data(analysis)
    scoring, forecast, valuation = services
    try:
        result = scoring.score_company(
            analysis, forecast, valuation, profile_key=profile, horizon=horizon
        )
    except ScoringError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    categories = [
        ExplanationItem(
            category=c.key, category_label=c.label, score=c.raw_score,
            weight=c.weight, origin="aggregate", explanation=c.explanation,
            source=", ".join(c.data_sources),
        )
        for c in result.categories
    ]

    metrics: list[ExplanationItem] = []
    for c in result.categories:
        for m in c.metrics:
            if not m.explanation:
                continue
            metrics.append(ExplanationItem(
                category=c.key, category_label=c.label,
                metric=m.key, metric_label=m.label, score=m.score,
                weight=m.weight * c.weight, origin=m.origin.value,
                explanation=m.explanation, source=m.source,
            ))

    scored = [m for m in metrics if m.origin != DataOrigin.MISSING.value]
    ranked = sorted(scored, key=lambda m: -m.score)

    return ExplanationResponse(
        company=analysis.company_ref(),
        overall_score=result.overall_score, grade=result.grade,
        recommendation=result.recommendation, summary=result.summary,
        recommendation_rationale=result.recommendation_rationale,
        categories=categories, metrics=metrics,
        key_positives=ranked[:5],
        key_negatives=list(reversed(ranked[-5:])) if len(ranked) >= 5 else [],
        data_gaps=[m for m in metrics if m.origin == DataOrigin.MISSING.value],
        warnings=result.warnings,
    )


@router.get("/company/{ticker}/scoring/history", response_model=HistoryResponse,
            summary="Score trend from stored snapshots")
def get_history(
    profile: str | None = Query(None),
    limit: int = Query(24, ge=1, le=120),
    analysis: AnalysisService = Depends(get_analysis),
    services=Depends(_services),
) -> HistoryResponse:
    scoring, _, _ = services
    key = profile or "balanced"
    snapshots = scoring.history(analysis.company.id, key, limit)

    points = [
        HistoryPoint(
            as_of=str(s.as_of), overall_score=s.overall_score, grade=s.grade,
            stars=s.stars, recommendation=s.recommendation,
            confidence=s.confidence, category_scores=s.category_scores or {},
        )
        for s in snapshots
    ]
    change = (
        points[-1].overall_score - points[0].overall_score
        if len(points) >= 2 else None
    )
    trend = (
        "improving" if change is not None and change > 1
        else "deteriorating" if change is not None and change < -1
        else "flat"
    )
    return HistoryResponse(
        company=analysis.company_ref(), profile_key=key,
        points=points, score_change=change, trend=trend,
    )


@router.get("/company/{ticker}/scoring/peers", response_model=PeerComparisonResponse,
            summary="Peer score comparison")
def get_peers(
    profile: str | None = Query(None),
    limit: int = Query(5, ge=1, le=10),
    horizon: int = Query(5),
    analysis: AnalysisService = Depends(get_analysis),
    services=Depends(_services),
    db: Session = Depends(get_db),
) -> PeerComparisonResponse:
    _require_data(analysis)
    scoring, forecast, valuation = services

    # Same-sector peers, largest first, with the subject company included.
    rows = db.execute(
        select(Company)
        .where(Company.sector == analysis.company.sector)
        .where(Company.id != analysis.company.id)
        .order_by(Company.market_cap.desc().nullslast())
        .limit(limit)
    ).scalars().all()

    peers = [analysis]
    for company in rows:
        peer = AnalysisService.for_ticker(db, company.ticker)
        if peer and peer.has_data:
            peers.append(peer)

    try:
        results = scoring.peer_scores(
            peers, forecast, valuation, profile_key=profile, horizon=horizon
        )
    except ScoringError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    by_category: dict[str, list[float]] = {}
    out_rows: list[PeerScoreRow] = []
    for result in results:
        scores = {c.key: round(c.raw_score, 4) for c in result.categories}
        for key, value in scores.items():
            by_category.setdefault(key, []).append(value)
        company = db.get(Company, result.company_id)
        out_rows.append(PeerScoreRow(
            company=CompanyRef.model_validate(company),
            overall_score=result.overall_score, grade=result.grade,
            stars=result.stars, recommendation=result.recommendation,
            confidence=result.confidence.confidence, category_scores=scores,
        ))

    return PeerComparisonResponse(
        profile_key=results[0].profile_key if results else (profile or "balanced"),
        peers=out_rows,
        category_medians={k: median(v) for k, v in by_category.items()},
    )


# ------------------------------------------------------------------- weights
@router.get("/scoring/weights", response_model=WeightProfileListResponse,
            summary="Available weight profiles")
def list_weights(
    active: str | None = Query(None),
    db: Session = Depends(get_db),
    _: CurrentUser = Depends(get_current_user),
) -> WeightProfileListResponse:
    scoring = ScoringService(db)
    return WeightProfileListResponse(
        profiles=[_profile_out(p) for p in scoring.list_profiles()],
        active=active or "balanced",
    )


@router.put("/scoring/weights", response_model=WeightProfileOut,
            summary="Create or update a custom weight profile")
def put_weights(
    body: WeightUpdateRequest,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> WeightProfileOut:
    scoring = ScoringService(db)
    try:
        profile = scoring.save_profile(
            key=body.key, label=body.label, weights=body.weights,
            description=body.description, derived_from=body.derived_from,
        )
    except ScoringError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    return _profile_out(profile)
