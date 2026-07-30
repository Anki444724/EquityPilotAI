"""Business risk.

Operating fragility rather than balance-sheet fragility: earnings volatility,
customer and revenue concentration, cyclicality and industry pressure. Scored
inversely, so a low-risk business scores high.

Financial risk is a separate category (``financial_risk.py``); the two are kept
apart because a debt-free company can still run a violently cyclical business,
and conflating them hides that.
"""
from __future__ import annotations

from statistics import pstdev

from app.domain.calc import safe_div
from app.domain.scoring.base import (
    DataOrigin, MetricScore, band_score, build_category,
)
from app.domain.scoring.inputs import ScoringInputs
from app.domain.scoring.weights import Category, CATEGORY_LABELS

KEY = Category.BUSINESS_RISK
SOURCE = "06 Historical IS"


def score(inputs: ScoringInputs, weight: float):
    metrics: list[MetricScore] = []
    q = inputs.qualitative

    # --- earnings volatility -------------------------------------------------
    pats = [p for p in inputs.series("income", "pat", 6) if p is not None]
    if len(pats) >= 3:
        mean = sum(pats) / len(pats)
        cv = safe_div(pstdev(pats), abs(mean)) if mean else None
        if cv is not None:
            metrics.append(MetricScore(
                key="earnings_volatility", label="Earnings volatility", weight=0.26,
                score=band_score(cv, [(0.15, 10), (0.30, 8), (0.50, 6), (0.80, 4), (1.20, 2)],
                                 higher_is_better=False),
                origin=DataOrigin.VERIFIED, value=cv, unit="x",
                explanation=(
                    f"Profit has a {cv:.0%} coefficient of variation over {len(pats)} years — "
                    f"{'highly predictable' if cv < 0.25 else 'volatile' if cv > 0.6 else 'moderately variable'}."
                ),
                source=SOURCE,
            ))

    # --- revenue cyclicality ----------------------------------------------------
    revenues = [r for r in inputs.series("income", "total_revenue", 6) if r is not None]
    if len(revenues) >= 3:
        declines = [
            (revenues[i] / revenues[i - 1] - 1)
            for i in range(1, len(revenues)) if revenues[i] < revenues[i - 1]
        ]
        worst = min(declines) if declines else 0.0
        metrics.append(MetricScore(
            key="revenue_cyclicality", label="Worst revenue decline", weight=0.22,
            score=band_score(worst, [(0.0, 10), (-0.05, 8), (-0.12, 6), (-0.25, 3.5), (-0.40, 1.5)]),
            origin=DataOrigin.VERIFIED, value=worst, unit="%",
            explanation=(
                "Revenue never declined year on year over the period." if not declines
                else f"The worst annual revenue decline was {worst:.1%}, indicating "
                     f"{'mild' if worst > -0.1 else 'material'} cyclicality."
            ),
            source=SOURCE,
        ))

    # --- operating leverage risk --------------------------------------------------
    income = inputs.latest_income
    if income and income.total_revenue:
        fixed_cost_ratio = safe_div(income.employee_benefit + income.depreciation,
                                    income.total_revenue)
        if fixed_cost_ratio is not None:
            metrics.append(MetricScore(
                key="fixed_cost_base", label="Fixed-cost intensity", weight=0.18,
                score=band_score(fixed_cost_ratio,
                                 [(0.08, 9.5), (0.15, 8), (0.25, 6), (0.35, 4), (0.50, 2)],
                                 higher_is_better=False),
                origin=DataOrigin.VERIFIED, value=fixed_cost_ratio, unit="%",
                explanation=(
                    f"Employee costs and depreciation are {fixed_cost_ratio:.1%} of revenue — "
                    f"{'a flexible cost base' if fixed_cost_ratio < 0.2 else 'high operating leverage, which amplifies a downturn'}."
                ),
                source=SOURCE,
            ))

    # --- customer concentration ------------------------------------------------------
    if q.customer_concentration is not None:
        metrics.append(MetricScore(
            key="concentration_risk", label="Customer concentration risk", weight=0.18,
            score=band_score(q.customer_concentration,
                             [(0.15, 9.5), (0.30, 8), (0.45, 5.5), (0.60, 3), (0.75, 1.5)],
                             higher_is_better=False),
            origin=DataOrigin.ANALYST, value=q.customer_concentration, unit="%",
            explanation=f"Top-5 customers represent {q.customer_concentration:.0%} of revenue.",
            source="Analyst input",
        ))
    else:
        metrics.append(MetricScore(
            key="concentration_risk", label="Customer concentration risk", weight=0.18,
            score=5.0, origin=DataOrigin.MISSING,
            explanation="Customer concentration not disclosed.",
        ))

    # --- data integrity as a risk signal ------------------------------------------
    if inputs.quality_report is not None:
        report = inputs.quality_report
        critical = len(report.blocking)
        metrics.append(MetricScore(
            key="data_integrity", label="Data integrity", weight=0.16,
            score=band_score(critical, [(0, 9.5), (1, 5), (2, 2.5)], higher_is_better=False),
            origin=DataOrigin.VERIFIED, value=float(critical), unit="count",
            explanation=(
                f"Data quality graded '{report.grade.value}'"
                + (f" with {critical} critical issue(s) that undermine confidence in the analysis."
                   if critical else " with no critical integrity issues.")
            ),
            source="Data-quality engine",
        ))

    return build_category(KEY.value, CATEGORY_LABELS[KEY], metrics, weight,
                          [SOURCE, "Analyst input", "Data-quality engine"])
