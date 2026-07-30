"""ESG.

Environmental, social and governance factors. Governance is scored in depth by
its own category; here it contributes only through disclosure quality, so the
two do not double-count.

ESG data is almost entirely analyst- or disclosure-sourced in the Indian
market. When nothing is on file the category is honest about it: every metric
is marked missing and confidence collapses, rather than a neutral 5/10 being
presented as if it meant something.
"""
from __future__ import annotations

from app.domain.scoring.base import (
    DataOrigin, MetricScore, build_category,
)
from app.domain.scoring.inputs import ScoringInputs
from app.domain.scoring.weights import Category, CATEGORY_LABELS

KEY = Category.ESG


def score(inputs: ScoringInputs, weight: float):
    metrics: list[MetricScore] = []
    q = inputs.qualitative

    definitions = [
        ("environmental", "Environmental", q.environmental_score, 0.35,
         "Emissions intensity, energy mix, water and waste."),
        ("social", "Social", q.social_score, 0.30,
         "Workforce safety, attrition, community and supply chain."),
        ("esg_disclosure", "ESG disclosure", q.esg_disclosure, 0.20,
         "BRSR/GRI completeness, assurance level and comparability."),
    ]

    for key, label, value, metric_weight, description in definitions:
        if value is not None:
            metrics.append(MetricScore(
                key=key, label=label, weight=metric_weight, score=value,
                origin=DataOrigin.ANALYST, value=value, unit="/10",
                explanation=f"{label} rated {value:.0f}/10. {description}",
                source="BRSR / analyst input",
            ))
        else:
            metrics.append(MetricScore(
                key=key, label=label, weight=metric_weight, score=5.0,
                origin=DataOrigin.MISSING,
                explanation=f"{label} not assessed — no BRSR or analyst data on file.",
            ))

    # Governance contributes through disclosure only; the governance category
    # scores it properly and double-counting would distort the composite.
    if q.disclosure_quality is not None:
        metrics.append(MetricScore(
            key="governance_link", label="Governance disclosure", weight=0.15,
            score=q.disclosure_quality, origin=DataOrigin.ANALYST,
            value=q.disclosure_quality, unit="/10",
            explanation=(
                f"Disclosure quality of {q.disclosure_quality:.0f}/10 feeds the ESG "
                "composite; governance itself is scored separately."
            ),
            source="Analyst input",
        ))
    else:
        metrics.append(MetricScore(
            key="governance_link", label="Governance disclosure", weight=0.15,
            score=5.0, origin=DataOrigin.MISSING,
            explanation="Disclosure quality not assessed.",
        ))

    return build_category(KEY.value, CATEGORY_LABELS[KEY], metrics, weight,
                          ["BRSR / analyst input"])
