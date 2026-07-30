"""API contracts for the scoring engine."""
from __future__ import annotations

from pydantic import BaseModel, Field

from app.schemas.common import CompanyRef


class MetricScoreOut(BaseModel):
    key: str
    label: str
    score: float = Field(ge=0, le=10)
    weight: float
    origin: str
    confidence: float
    value: float | None = None
    unit: str = ""
    explanation: str = ""
    source: str = ""


class ConfidenceOut(BaseModel):
    confidence: float
    label: str
    verified_pct: float
    estimated_pct: float
    analyst_pct: float
    missing_pct: float
    metrics_total: int
    metrics_missing: int


class CategoryScoreOut(BaseModel):
    key: str
    label: str
    raw_score: float
    weighted_score: float
    weight: float
    score_pct: float
    grade_hint: str
    confidence: ConfidenceOut
    explanation: str
    data_sources: list[str] = Field(default_factory=list)
    metrics: list[MetricScoreOut] = Field(default_factory=list)


class ScoreResponse(BaseModel):
    company: CompanyRef
    overall_score: float
    grade: str
    grade_description: str
    stars: float
    recommendation: str
    recommendation_rationale: str
    conviction: str
    profile_key: str
    profile_label: str
    confidence: ConfidenceOut
    categories: list[CategoryScoreOut]
    strongest: list[str] = Field(default_factory=list)
    weakest: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    summary: str = ""


class ExplanationItem(BaseModel):
    """One narrative element, structured for the future AI Analyst."""

    category: str
    category_label: str
    metric: str | None = None
    metric_label: str | None = None
    score: float
    weight: float
    origin: str
    explanation: str
    source: str = ""


class ExplanationResponse(BaseModel):
    company: CompanyRef
    overall_score: float
    grade: str
    recommendation: str
    summary: str
    recommendation_rationale: str
    #: Category-level narratives.
    categories: list[ExplanationItem] = Field(default_factory=list)
    #: Metric-level narratives, the AI Analyst's raw material.
    metrics: list[ExplanationItem] = Field(default_factory=list)
    #: Highest and lowest scoring drivers, pre-sorted for convenience.
    key_positives: list[ExplanationItem] = Field(default_factory=list)
    key_negatives: list[ExplanationItem] = Field(default_factory=list)
    data_gaps: list[ExplanationItem] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class HistoryPoint(BaseModel):
    as_of: str
    overall_score: float
    grade: str
    stars: float
    recommendation: str
    confidence: float
    category_scores: dict[str, float] = Field(default_factory=dict)


class HistoryResponse(BaseModel):
    company: CompanyRef
    profile_key: str
    points: list[HistoryPoint] = Field(default_factory=list)
    #: Change over the retained window.
    score_change: float | None = None
    trend: str = "flat"


class WeightProfileOut(BaseModel):
    key: str
    label: str
    description: str
    is_builtin: bool
    weights: dict[str, float]
    top_categories: list[str] = Field(default_factory=list)


class WeightProfileListResponse(BaseModel):
    profiles: list[WeightProfileOut]
    active: str


class WeightUpdateRequest(BaseModel):
    key: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=120)
    weights: dict[str, float]
    description: str | None = None
    derived_from: str | None = None


class PeerScoreRow(BaseModel):
    company: CompanyRef
    overall_score: float
    grade: str
    stars: float
    recommendation: str
    confidence: float
    category_scores: dict[str, float] = Field(default_factory=dict)


class PeerComparisonResponse(BaseModel):
    profile_key: str
    peers: list[PeerScoreRow] = Field(default_factory=list)
    #: Median score per category across the peer set, for radar overlay.
    category_medians: dict[str, float] = Field(default_factory=dict)
