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


# ---------------------------------------------------------------------------
# AI Scoring Engine 3.0
# ---------------------------------------------------------------------------

class CitationOut(BaseModel):
    kind: str
    label: str
    reference: str = ""
    document_id: int | None = None
    page: int | None = None
    fiscal_year: int | None = None
    url: str | None = None
    excerpt: str | None = None


class FactorScoreOut(BaseModel):
    """One factor, with everything the brief requires it to carry."""

    key: str
    label: str
    score: float = Field(ge=0, le=10)
    weight: float
    origin: str
    coverage: float
    reason: str
    value: float | None = None
    unit: str = ""
    evidence: str = ""
    citations: list[CitationOut] = Field(default_factory=list)
    computed_by: str = ""


class ModuleScoreOut(BaseModel):
    key: str
    label: str
    weight: float
    score: float = Field(ge=0, le=10)
    contribution: float
    coverage: float
    reason: str
    ai_commentary: str | None = None
    missing_factors: list[str] = Field(default_factory=list)
    citation_count: int = 0
    factors: list[FactorScoreOut] = Field(default_factory=list)


class ProbabilityDriverOut(BaseModel):
    module: str
    influence: float


class ProbabilityOut(BaseModel):
    key: str
    label: str
    probability: float = Field(ge=0, le=1)
    percent: float
    drivers: list[ProbabilityDriverOut] = Field(default_factory=list)
    reason: str
    shrinkage: float = 0.0


class AIScoreResponse(BaseModel):
    company_id: str
    ticker: str
    name: str
    overall_score: float = Field(ge=0, le=100)
    rating: str
    rating_description: str
    recommendation: str
    recommendation_reason: str
    coverage: float
    summary: str
    warnings: list[str] = Field(default_factory=list)
    guardrails: list[str] = Field(default_factory=list)
    framework_version: str
    input_fingerprint: str
    total_citations: int
    modules: list[ModuleScoreOut] = Field(default_factory=list)
    probabilities: list[ProbabilityOut] = Field(default_factory=list)
    #: Present when the run was recorded, so the caller knows which permanent
    #: version this response corresponds to.
    version: int | None = None
    version_created: bool | None = None
    version_note: str | None = None


class AIScoreVersionOut(BaseModel):
    version: int
    status: str
    framework_version: str
    overall_score: float
    rating: str
    recommendation: str
    coverage: float
    module_scores: dict[str, float] = Field(default_factory=dict)
    probabilities: dict[str, float] = Field(default_factory=dict)
    summary: str | None = None
    input_fingerprint: str
    total_citations: int
    trigger: str
    trigger_document_id: int | None = None
    supersedes_version: int | None = None
    score_delta: float | None = None
    computed_at: object


class AIScoreHistoryResponse(BaseModel):
    company: CompanyRef
    framework_version: str
    versions_retained: int
    versions: list[AIScoreVersionOut] = Field(default_factory=list)
    #: True when the history spans more than one framework version, in which
    #: case a trend line across it is comparing two different questions.
    spans_framework_versions: bool = False


class ModuleCriterionOut(BaseModel):
    key: str
    label: str
    weight: float
    criteria: list[str] = Field(default_factory=list)


class AIFrameworkResponse(BaseModel):
    """The framework definition, published so the score is inspectable."""

    version: str
    modules: list[ModuleCriterionOut] = Field(default_factory=list)
    total_weight: float
    rating_bands: list[dict] = Field(default_factory=list)
    recommendation_bands: list[dict] = Field(default_factory=list)
    guardrails: list[dict] = Field(default_factory=list)
    probability_specs: list[dict] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
