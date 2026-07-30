"""Overall score — the composite and the investment decision.

Runs all thirteen category scorers, applies the active weight profile, and
produces the outputs the rest of the platform consumes: a 0–100 score, a letter
grade, a star rating and a recommendation.

Two design decisions matter here.

**Confidence gates the recommendation.** A composite of 82 built on 35%
confidence must not read "BUY" with the same authority as one built on 90%.
When confidence is low the recommendation is deliberately pulled toward HOLD
and the reason is stated. The engine should be less certain when it knows less.

**Valuation is separated from quality.** A superb business at an absurd price
and a mediocre business at a fair one can produce the same composite, which
would be useless. The recommendation therefore reads the valuation category
explicitly rather than relying on the blend alone.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from app.domain.scoring.base import (
    CategoryScore, ConfidenceBreakdown, DataOrigin, MetricScore,
)
from app.domain.scoring.inputs import ScoringInputs
from app.domain.scoring.weights import Category, WeightProfile
from app.services.scoring import (
    business_quality, capital_allocation, cash_flow_quality,
    competitive_advantage, esg, financial_quality, financial_risk, governance,
    growth_quality, management_quality, momentum, risk, valuation,
)


class Recommendation(StrEnum):
    BUY = "BUY"
    ACCUMULATE = "ACCUMULATE"
    HOLD = "HOLD"
    REDUCE = "REDUCE"
    SELL = "SELL"


#: Letter grades by composite score. Mirrors the workbook's AAA–C scale.
GRADE_BANDS: tuple[tuple[float, str, str], ...] = (
    (85.0, "AAA", "Exceptional franchise with pristine fundamentals"),
    (75.0, "AA", "High-quality compounder with a durable advantage"),
    (65.0, "A", "Good business with some identifiable weaknesses"),
    (55.0, "BBB", "Average quality; the thesis depends on execution"),
    (45.0, "BB", "Below-average quality or a stretched balance sheet"),
    (35.0, "B", "Weak fundamentals with material concerns"),
    (0.0, "C", "Distressed or governance-impaired"),
)

#: Recommendation thresholds on the composite, before adjustments.
RECOMMENDATION_BANDS: tuple[tuple[float, Recommendation], ...] = (
    (72.0, Recommendation.BUY),
    (62.0, Recommendation.ACCUMULATE),
    (48.0, Recommendation.HOLD),
    (38.0, Recommendation.REDUCE),
    (0.0, Recommendation.SELL),
)

#: Below this confidence the recommendation is pulled toward HOLD.
LOW_CONFIDENCE_THRESHOLD = 0.55
#: A valuation category this weak caps the recommendation regardless of quality.
EXPENSIVE_VALUATION_SCORE = 3.5
#: A financial-risk category this weak caps it too.
FRAGILE_BALANCE_SHEET_SCORE = 3.0

#: Every scorer, in presentation order.
SCORERS = (
    (Category.BUSINESS_QUALITY, business_quality.score),
    (Category.FINANCIAL_QUALITY, financial_quality.score),
    (Category.MANAGEMENT_QUALITY, management_quality.score),
    (Category.CAPITAL_ALLOCATION, capital_allocation.score),
    (Category.COMPETITIVE_MOAT, competitive_advantage.score),
    (Category.GOVERNANCE, governance.score),
    (Category.FINANCIAL_RISK, financial_risk.score),
    (Category.BUSINESS_RISK, risk.score),
    (Category.VALUATION, valuation.score),
    (Category.GROWTH_QUALITY, growth_quality.score),
    (Category.CASH_FLOW_QUALITY, cash_flow_quality.score),
    (Category.ESG, esg.score),
    (Category.MOMENTUM, momentum.score),
)


@dataclass(frozen=True, slots=True)
class ScoreResult:
    """The complete scoring output."""

    company_id: str
    ticker: str
    name: str

    #: 0–100.
    overall_score: float
    grade: str
    grade_description: str
    #: 0.5–5.0 in half-star steps.
    stars: float
    recommendation: str
    recommendation_rationale: str
    conviction: str

    categories: list[CategoryScore]
    confidence: ConfidenceBreakdown
    profile_key: str
    profile_label: str

    strongest: list[str] = field(default_factory=list)
    weakest: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: Narrative assembled for the AI Analyst.
    summary: str = ""

    def category(self, key: Category | str) -> CategoryScore | None:
        target = key.value if isinstance(key, Category) else key
        return next((c for c in self.categories if c.key == target), None)


def grade_for(score: float) -> tuple[str, str]:
    for threshold, letter, description in GRADE_BANDS:
        if score >= threshold:
            return letter, description
    return "C", GRADE_BANDS[-1][2]


def stars_for(score: float) -> float:
    """Half-star rating on a 0–100 composite."""
    raw = score / 100 * 5
    return max(0.5, round(raw * 2) / 2)


def _aggregate_confidence(categories: list[CategoryScore]) -> ConfidenceBreakdown:
    """Weight category confidences by their profile weights."""
    all_metrics: list[MetricScore] = []
    for category in categories:
        # Rescale each metric's weight by its category weight so the composite
        # reflects what actually drives the score.
        metric_total = sum(m.weight for m in category.metrics) or 1.0
        for metric in category.metrics:
            all_metrics.append(
                MetricScore(
                    key=f"{category.key}.{metric.key}", label=metric.label,
                    score=metric.score,
                    weight=metric.weight / metric_total * category.weight,
                    origin=metric.origin, value=metric.value, unit=metric.unit,
                    explanation=metric.explanation, source=metric.source,
                )
            )
    from app.domain.scoring.base import build_confidence
    return build_confidence(all_metrics)


def _recommend(
    composite: float,
    categories: list[CategoryScore],
    confidence: ConfidenceBreakdown,
) -> tuple[Recommendation, str, list[str]]:
    """Derive the recommendation, with explicit overrides and their reasons."""
    warnings: list[str] = []

    base = next(
        (rec for threshold, rec in RECOMMENDATION_BANDS if composite >= threshold),
        Recommendation.SELL,
    )
    reasons = [f"Composite score of {composite:.1f}/100 maps to {base.value}."]
    order = [
        Recommendation.SELL, Recommendation.REDUCE, Recommendation.HOLD,
        Recommendation.ACCUMULATE, Recommendation.BUY,
    ]
    index = order.index(base)

    def cap(at: Recommendation, reason: str) -> None:
        nonlocal index
        ceiling = order.index(at)
        if index > ceiling:
            index = ceiling
            reasons.append(reason)

    # Valuation override: quality never justifies any price.
    val = next((c for c in categories if c.key == Category.VALUATION.value), None)
    if val and val.raw_score <= EXPENSIVE_VALUATION_SCORE and val.weight > 0:
        cap(Recommendation.HOLD,
            f"Capped at HOLD: valuation scores {val.raw_score:.1f}/10, so the "
            "shares are expensive regardless of business quality.")
        warnings.append("Valuation is the binding constraint on this recommendation.")

    # Balance-sheet override: fragility outranks everything.
    fin_risk = next((c for c in categories if c.key == Category.FINANCIAL_RISK.value), None)
    if fin_risk and fin_risk.raw_score <= FRAGILE_BALANCE_SHEET_SCORE:
        cap(Recommendation.REDUCE,
            f"Capped at REDUCE: financial risk scores {fin_risk.raw_score:.1f}/10, "
            "indicating balance-sheet fragility.")
        warnings.append("Balance-sheet risk overrides the quality assessment.")

    # Confidence override: be less decisive when the data is thin.
    if confidence.confidence < LOW_CONFIDENCE_THRESHOLD:
        cap(Recommendation.HOLD,
            f"Capped at HOLD: confidence is only {confidence.confidence:.0%} "
            f"({confidence.missing_pct:.0%} of weighted inputs are missing), which "
            "does not support a directional call.")
        warnings.append(
            f"Low confidence — {confidence.missing_pct:.0%} of weighted inputs are missing."
        )

    return order[index], " ".join(reasons), warnings


def _conviction(confidence: ConfidenceBreakdown, composite: float) -> str:
    if confidence.confidence >= 0.80 and (composite >= 70 or composite <= 40):
        return "High"
    if confidence.confidence >= 0.60:
        return "Medium"
    return "Low"


def _summarise(
    name: str, composite: float, grade: str, recommendation: str,
    categories: list[CategoryScore], confidence: ConfidenceBreakdown,
) -> str:
    """Narrative for the AI Analyst and the research report."""
    ranked = sorted(
        [c for c in categories if c.weight > 0], key=lambda c: -c.raw_score
    )
    strongest = ranked[:2]
    weakest = ranked[-2:]

    parts = [
        f"{name} scores {composite:.1f}/100 (grade {grade}), supporting a "
        f"{recommendation} recommendation at {confidence.confidence:.0%} confidence.",
    ]
    if strongest:
        parts.append(
            "Strengths: "
            + "; ".join(f"{c.label} {c.raw_score:.1f}/10" for c in strongest) + "."
        )
    if weakest:
        parts.append(
            "Weaknesses: "
            + "; ".join(f"{c.label} {c.raw_score:.1f}/10" for c in weakest) + "."
        )
    if confidence.missing_pct > 0.20:
        parts.append(
            f"{confidence.missing_pct:.0%} of weighted inputs are unavailable, so the "
            "score should be read as provisional."
        )
    return " ".join(parts)


def compute_score(inputs: ScoringInputs, profile: WeightProfile) -> ScoreResult:
    """Run every category scorer and assemble the composite."""
    categories: list[CategoryScore] = []
    for category, scorer in SCORERS:
        weight = profile.weight_for(category)
        categories.append(scorer(inputs, weight))

    # Composite: weighted mean of category scores, scaled to 0–100.
    total_weight = sum(c.weight for c in categories)
    composite = (
        sum(c.weighted_score for c in categories) / total_weight * 10
        if total_weight > 0 else 50.0
    )
    composite = max(0.0, min(100.0, composite))

    confidence = _aggregate_confidence(categories)
    grade, description = grade_for(composite)
    recommendation, rationale, warnings = _recommend(composite, categories, confidence)

    contributing = sorted(
        [c for c in categories if c.weight > 0], key=lambda c: -c.raw_score
    )

    if inputs.quality_report and inputs.quality_report.is_illustrative:
        warnings.insert(0, inputs.quality_report.disclosure or
                        "Underlying data is not sourced from filings.")

    return ScoreResult(
        company_id=inputs.company_id, ticker=inputs.ticker, name=inputs.name,
        overall_score=composite,
        grade=grade, grade_description=description,
        stars=stars_for(composite),
        recommendation=recommendation.value,
        recommendation_rationale=rationale,
        conviction=_conviction(confidence, composite),
        categories=categories, confidence=confidence,
        profile_key=profile.key, profile_label=profile.label,
        strongest=[c.label for c in contributing[:3]],
        weakest=[c.label for c in contributing[-3:]],
        warnings=warnings,
        summary=_summarise(inputs.name, composite, grade,
                           recommendation.value, categories, confidence),
    )
