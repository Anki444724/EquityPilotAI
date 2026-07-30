"""Momentum.

Price and earnings momentum. Deliberately the smallest weight in every built-in
profile, and zero in Conservative and Value — momentum is a timing input, not a
statement about business quality.

Price data is not yet ingested by the platform, so these metrics are usually
missing. The category is built now because the framework must be complete and
because momentum will matter to the alerting engine in Module 8; the confidence
engine reports the gap honestly in the meantime.
"""
from __future__ import annotations

from app.domain.scoring.base import (
    DataOrigin, MetricScore, band_score, build_category,
)
from app.domain.scoring.inputs import ScoringInputs
from app.domain.scoring.weights import Category, CATEGORY_LABELS

KEY = Category.MOMENTUM


def score(inputs: ScoringInputs, weight: float):
    metrics: list[MetricScore] = []
    q = inputs.qualitative

    if q.price_return_12m is not None:
        metrics.append(MetricScore(
            key="return_12m", label="12-month price return", weight=0.32,
            score=band_score(q.price_return_12m,
                             [(0.35, 9.5), (0.18, 8), (0.05, 6.5), (-0.10, 4.5), (-0.25, 2.5)]),
            origin=DataOrigin.VERIFIED, value=q.price_return_12m, unit="%",
            explanation=f"The shares returned {q.price_return_12m:+.1%} over twelve months.",
            source="Market data",
        ))
    else:
        metrics.append(MetricScore(
            key="return_12m", label="12-month price return", weight=0.32,
            score=5.0, origin=DataOrigin.MISSING,
            explanation="Price history is not yet ingested by the platform.",
        ))

    if q.price_return_3m is not None:
        metrics.append(MetricScore(
            key="return_3m", label="3-month price return", weight=0.22,
            score=band_score(q.price_return_3m,
                             [(0.15, 9), (0.07, 7.5), (0.0, 6), (-0.08, 4), (-0.18, 2)]),
            origin=DataOrigin.VERIFIED, value=q.price_return_3m, unit="%",
            explanation=f"Three-month return of {q.price_return_3m:+.1%}.",
            source="Market data",
        ))
    else:
        metrics.append(MetricScore(
            key="return_3m", label="3-month price return", weight=0.22,
            score=5.0, origin=DataOrigin.MISSING,
            explanation="Short-term price history unavailable.",
        ))

    if q.earnings_revision is not None:
        metrics.append(MetricScore(
            key="earnings_revision", label="Earnings revision", weight=0.28,
            score=band_score(q.earnings_revision,
                             [(0.08, 9.5), (0.03, 8), (0.0, 6), (-0.04, 4), (-0.10, 2)]),
            origin=DataOrigin.ESTIMATED, value=q.earnings_revision, unit="%",
            explanation=(
                f"Consensus earnings estimates have been revised {q.earnings_revision:+.1%} — "
                f"{'upgrades' if q.earnings_revision > 0 else 'downgrades'}."
            ),
            source="Consensus data",
        ))
    else:
        metrics.append(MetricScore(
            key="earnings_revision", label="Earnings revision", weight=0.28,
            score=5.0, origin=DataOrigin.MISSING,
            explanation="No consensus estimate history available.",
        ))

    if q.relative_strength is not None:
        metrics.append(MetricScore(
            key="relative_strength", label="Relative strength vs index", weight=0.18,
            score=band_score(q.relative_strength,
                             [(0.20, 9.5), (0.08, 8), (0.0, 6), (-0.08, 4), (-0.20, 2)]),
            origin=DataOrigin.VERIFIED, value=q.relative_strength, unit="%",
            explanation=f"Outperformance against the index of {q.relative_strength:+.1%}.",
            source="Market data",
        ))
    else:
        metrics.append(MetricScore(
            key="relative_strength", label="Relative strength vs index", weight=0.18,
            score=5.0, origin=DataOrigin.MISSING,
            explanation="Index-relative performance unavailable.",
        ))

    return build_category(KEY.value, CATEGORY_LABELS[KEY], metrics, weight,
                          ["Market data", "Consensus data"])
