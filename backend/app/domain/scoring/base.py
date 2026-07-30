"""Scoring primitives — the contract every category implements.

The engine's central idea is that a score is worthless without knowing how much
of it rests on real data. Every category therefore returns not just a number
but a **provenance-weighted confidence**: what share of the inputs was verified
from reported figures, what share was estimated, and what was simply missing.

A 78/100 built on 40% missing data is a different claim from a 78/100 built on
complete filings, and the engine refuses to conflate them.

Each metric also carries a plain-English explanation stating *why* it scored
where it did. Those strings are the AI Analyst's raw material — they are
written to be read by a person or a model, not to be parsed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Callable, Sequence

from app.domain.calc import safe_div


class DataOrigin(StrEnum):
    """Where a metric's inputs came from. Drives the confidence calculation."""

    #: Computed from reported financial statements.
    VERIFIED = "verified"
    #: Derived from a forecast or a calibrated assumption.
    ESTIMATED = "estimated"
    #: Supplied by an analyst as a qualitative judgement.
    ANALYST = "analyst"
    #: Input unavailable; the metric scored on a neutral default.
    MISSING = "missing"


#: Confidence weight for each origin. Verified data is worth full confidence;
#: a missing input contributes none.
ORIGIN_CONFIDENCE: dict[DataOrigin, float] = {
    DataOrigin.VERIFIED: 1.00,
    DataOrigin.ESTIMATED: 0.65,
    DataOrigin.ANALYST: 0.50,
    DataOrigin.MISSING: 0.00,
}

#: Score awarded when an input is missing — the midpoint, so an absent metric
#: neither flatters nor punishes. The confidence penalty carries the signal.
NEUTRAL_SCORE = 5.0

#: Every metric and category is scored 0–10 before weighting.
SCORE_MIN = 0.0
SCORE_MAX = 10.0


@dataclass(frozen=True, slots=True)
class MetricScore:
    """One measurable input inside a category."""

    key: str
    label: str
    #: 0–10.
    score: float
    #: Relative importance within its category. Need not sum to 1.
    weight: float
    origin: DataOrigin
    #: The underlying figure, for display and audit.
    value: float | None = None
    unit: str = ""
    #: Why this scored where it did. Written for humans and language models.
    explanation: str = ""
    #: Which sheet, statement or engine produced the input.
    source: str = ""

    @property
    def confidence(self) -> float:
        return ORIGIN_CONFIDENCE[self.origin]

    @property
    def weighted(self) -> float:
        return self.score * self.weight


@dataclass(frozen=True, slots=True)
class ConfidenceBreakdown:
    """How much of a score rests on real data."""

    confidence: float          # 0–1, weight-adjusted
    verified_pct: float
    estimated_pct: float
    analyst_pct: float
    missing_pct: float
    metrics_total: int
    metrics_missing: int

    @property
    def label(self) -> str:
        if self.confidence >= 0.85:
            return "High"
        if self.confidence >= 0.65:
            return "Moderate"
        if self.confidence >= 0.40:
            return "Low"
        return "Very low"


def build_confidence(metrics: Sequence[MetricScore]) -> ConfidenceBreakdown:
    """Weight-adjusted confidence across a set of metrics.

    Shares are weighted, not counted: a heavily weighted missing input damages
    confidence more than a trivial one, which a simple count would miss.
    """
    if not metrics:
        return ConfidenceBreakdown(0.0, 0.0, 0.0, 0.0, 1.0, 0, 0)

    total_weight = sum(m.weight for m in metrics) or 1.0
    share: dict[DataOrigin, float] = {o: 0.0 for o in DataOrigin}
    for m in metrics:
        share[m.origin] += m.weight / total_weight

    confidence = sum(share[o] * ORIGIN_CONFIDENCE[o] for o in DataOrigin)

    return ConfidenceBreakdown(
        confidence=confidence,
        verified_pct=share[DataOrigin.VERIFIED],
        estimated_pct=share[DataOrigin.ESTIMATED],
        analyst_pct=share[DataOrigin.ANALYST],
        missing_pct=share[DataOrigin.MISSING],
        metrics_total=len(metrics),
        metrics_missing=sum(1 for m in metrics if m.origin is DataOrigin.MISSING),
    )


@dataclass(frozen=True, slots=True)
class CategoryScore:
    """A scoring category's full result."""

    key: str
    label: str
    #: 0–10, weighted mean of its metrics.
    raw_score: float
    #: raw_score x the category's profile weight.
    weighted_score: float
    #: The profile weight applied.
    weight: float
    confidence: ConfidenceBreakdown
    metrics: list[MetricScore]
    #: Category-level narrative, assembled from the metric explanations.
    explanation: str
    data_sources: list[str] = field(default_factory=list)

    @property
    def score_pct(self) -> float:
        """Score as a percentage of the maximum, for radar charts."""
        return self.raw_score / SCORE_MAX

    @property
    def grade_hint(self) -> str:
        if self.raw_score >= 8.0:
            return "Strong"
        if self.raw_score >= 6.5:
            return "Good"
        if self.raw_score >= 5.0:
            return "Adequate"
        if self.raw_score >= 3.5:
            return "Weak"
        return "Poor"


def clamp_score(value: float) -> float:
    return max(SCORE_MIN, min(SCORE_MAX, value))


def band_score(
    value: float | None,
    bands: Sequence[tuple[float, float]],
    *,
    higher_is_better: bool = True,
) -> float:
    """Map a value onto a 0–10 score using threshold bands.

    ``bands`` is ordered best-first as ``(threshold, score)``. With
    ``higher_is_better`` the value must be at or above the threshold; otherwise
    at or below it. A value matching no band scores zero.

    Bands are used rather than a continuous function because analysts reason in
    thresholds — "ROE above 20% is excellent, above 15% is good" — and a band
    table is inspectable and adjustable in a way a fitted curve is not.
    """
    if value is None:
        return NEUTRAL_SCORE
    for threshold, score in bands:
        if (higher_is_better and value >= threshold) or (
            not higher_is_better and value <= threshold
        ):
            return clamp_score(score)
    return SCORE_MIN


def linear_score(
    value: float | None, worst: float, best: float, *, cap: bool = True
) -> float:
    """Interpolate a 0–10 score linearly between two anchors.

    Works in either direction: ``worst`` may exceed ``best`` for metrics where
    lower is better (leverage, cycle days).
    """
    if value is None:
        return NEUTRAL_SCORE
    if best == worst:
        return NEUTRAL_SCORE
    ratio = (value - worst) / (best - worst)
    score = ratio * SCORE_MAX
    return clamp_score(score) if cap else score


#: Period-on-period changes smaller than this are treated as flat rather than
#: as a direction. Without it a perfectly stable series scores as a decline,
#: because no period "improved".
TREND_FLAT_EPSILON = 1e-4


def trend_score(
    values: Sequence[float | None], *, higher_is_better: bool = True
) -> tuple[float, float | None]:
    """Score the direction and consistency of a series.

    Returns ``(score, slope)`` where slope is the average period-on-period
    change. Rewards steady improvement over a volatile path to the same place,
    because consistency is itself evidence of control.

    A genuinely flat series scores neutral: stability is neither a strength nor
    a failing, and treating it as deterioration would penalise exactly the
    predictable businesses this framework is meant to favour.
    """
    present = [v for v in values if v is not None]
    if len(present) < 2:
        return NEUTRAL_SCORE, None

    deltas = [present[i] - present[i - 1] for i in range(1, len(present))]
    slope = sum(deltas) / len(deltas)

    if abs(slope) < TREND_FLAT_EPSILON:
        return NEUTRAL_SCORE, slope

    # Direction is set by the slope; consistency measures how much of the path
    # moved that way, ignoring periods that barely changed.
    moved = [d for d in deltas if abs(d) >= TREND_FLAT_EPSILON]
    improving = sum(1 for d in moved if (d > 0) == higher_is_better)
    consistency = improving / len(moved) if moved else 0.5

    direction = slope if higher_is_better else -slope
    base = NEUTRAL_SCORE + (2.5 if direction > 0 else -2.5)
    return clamp_score(base + (consistency - 0.5) * 5.0), slope


def aggregate(metrics: Sequence[MetricScore]) -> float:
    """Weighted mean of metric scores, on the same 0–10 scale."""
    if not metrics:
        return NEUTRAL_SCORE
    total_weight = sum(m.weight for m in metrics)
    if total_weight <= 0:
        return NEUTRAL_SCORE
    return clamp_score(sum(m.weighted for m in metrics) / total_weight)


def narrate(category: str, metrics: Sequence[MetricScore], score: float) -> str:
    """Assemble a category narrative from its strongest and weakest metrics.

    Deliberately concise: the AI Analyst consumes these, and a wall of text is
    harder to reason over than a pointed observation.
    """
    scored = [m for m in metrics if m.origin is not DataOrigin.MISSING]
    if not scored:
        return f"{category} could not be assessed — no supporting data available."

    best = max(scored, key=lambda m: m.score)
    worst = min(scored, key=lambda m: m.score)
    verdict = (
        "strong" if score >= 8 else "good" if score >= 6.5
        else "adequate" if score >= 5 else "weak" if score >= 3.5 else "poor"
    )

    parts = [f"{category} is {verdict} at {score:.1f}/10."]
    if best.explanation:
        parts.append(f"Strongest: {best.explanation}")
    if worst.key != best.key and worst.explanation:
        parts.append(f"Weakest: {worst.explanation}")

    missing = [m.label for m in metrics if m.origin is DataOrigin.MISSING]
    if missing:
        shown = ", ".join(missing[:3])
        more = f" and {len(missing) - 3} other inputs" if len(missing) > 3 else ""
        parts.append(f"Not assessed: {shown}{more}.")
    return " ".join(parts)


def build_category(
    key: str,
    label: str,
    metrics: Sequence[MetricScore],
    weight: float,
    sources: Sequence[str] = (),
) -> CategoryScore:
    """Assemble a category result from its metrics."""
    raw = aggregate(metrics)
    return CategoryScore(
        key=key,
        label=label,
        raw_score=raw,
        weighted_score=raw * weight,
        weight=weight,
        confidence=build_confidence(metrics),
        metrics=list(metrics),
        explanation=narrate(label, metrics, raw),
        data_sources=list(dict.fromkeys(sources)),
    )


#: Signature every category scorer implements.
CategoryScorer = Callable[..., CategoryScore]
