"""Business quality.

Asks whether this is a good business, independent of price: does it earn
returns above its cost of capital, are those returns stable, and is the revenue
base durable?

Return on invested capital is the anchor. A business earning below its WACC
destroys value however fast it grows, so ROIC is scored against the company's
own cost of capital rather than an absolute threshold.
"""
from __future__ import annotations

from app.domain.calc import safe_div
from app.domain.scoring.base import (
    DataOrigin, MetricScore, band_score, build_category, linear_score,
)
from app.domain.scoring.inputs import ScoringInputs
from app.domain.scoring.weights import Category, CATEGORY_LABELS

KEY = Category.BUSINESS_QUALITY
SOURCE_IS = "06 Historical IS"
SOURCE_BS = "07 Historical BS"


def score(inputs: ScoringInputs, weight: float):
    metrics: list[MetricScore] = []
    income, balance = inputs.latest_income, inputs.latest_balance

    # --- ROIC versus cost of capital -----------------------------------
    roic = None
    if income and income.effective_tax_rate is not None:
        nopat = income.ebit * (1 - income.effective_tax_rate)
        roic = safe_div(nopat, inputs.avg_balance("invested_capital"))

    if roic is not None and inputs.wacc:
        spread = roic - inputs.wacc
        metrics.append(MetricScore(
            key="roic_spread", label="ROIC vs WACC", weight=0.30,
            score=band_score(spread, [(0.10, 10), (0.05, 8.5), (0.02, 7), (0.0, 5.5), (-0.05, 3)]),
            origin=DataOrigin.VERIFIED, value=spread, unit="%",
            explanation=(
                f"ROIC of {roic:.1%} versus a {inputs.wacc:.1%} cost of capital is a "
                f"{spread:+.1%} spread — {'value-creating' if spread > 0 else 'value-destroying'}."
            ),
            source=f"{SOURCE_IS}, {SOURCE_BS}",
        ))
    else:
        metrics.append(MetricScore(
            key="roic_spread", label="ROIC vs WACC", weight=0.30, score=5.0,
            origin=DataOrigin.MISSING,
            explanation="ROIC spread not measurable without a tax rate and cost of capital.",
        ))

    # --- gross margin: pricing power -------------------------------------
    if income and income.gross_margin is not None:
        metrics.append(MetricScore(
            key="gross_margin", label="Gross margin", weight=0.18,
            score=band_score(income.gross_margin,
                             [(0.50, 10), (0.35, 8), (0.25, 6.5), (0.15, 4.5), (0.08, 2.5)]),
            origin=DataOrigin.VERIFIED, value=income.gross_margin, unit="%",
            explanation=(
                f"Gross margin of {income.gross_margin:.1%} indicates "
                f"{'strong' if income.gross_margin > 0.35 else 'limited'} pricing power."
            ),
            source=SOURCE_IS,
        ))

    # --- margin stability -------------------------------------------------
    margins = [m for m in inputs.series("income", "ebitda_margin", 5) if m is not None]
    if len(margins) >= 3:
        mean = sum(margins) / len(margins)
        spread = max(margins) - min(margins)
        volatility = safe_div(spread, mean)
        metrics.append(MetricScore(
            key="margin_stability", label="EBITDA margin stability", weight=0.17,
            score=linear_score(volatility, 0.60, 0.05),
            origin=DataOrigin.VERIFIED, value=volatility, unit="x",
            explanation=(
                f"EBITDA margin ranged {min(margins):.1%}–{max(margins):.1%} over "
                f"{len(margins)} years, a {volatility:.0%} swing around the mean — "
                f"{'stable' if (volatility or 1) < 0.25 else 'volatile'}."
            ),
            source=SOURCE_IS,
        ))

    # --- asset efficiency --------------------------------------------------
    if income:
        turnover = safe_div(income.total_revenue, inputs.avg_balance("total_assets"))
        if turnover is not None:
            metrics.append(MetricScore(
                key="asset_turnover", label="Asset turnover", weight=0.12,
                score=band_score(turnover, [(1.5, 9), (1.0, 7.5), (0.7, 6), (0.4, 4.5), (0.2, 3)]),
                origin=DataOrigin.VERIFIED, value=turnover, unit="x",
                explanation=f"Each rupee of assets generates {turnover:.2f} of revenue.",
                source=f"{SOURCE_IS}, {SOURCE_BS}",
            ))

    # --- customer concentration (qualitative) -------------------------------
    concentration = inputs.qualitative.customer_concentration
    if concentration is not None:
        metrics.append(MetricScore(
            key="customer_concentration", label="Customer concentration", weight=0.13,
            score=band_score(concentration, [(0.15, 9), (0.30, 7), (0.45, 5), (0.60, 3)],
                             higher_is_better=False),
            origin=DataOrigin.ANALYST, value=concentration, unit="%",
            explanation=(
                f"Top-5 customers account for {concentration:.0%} of revenue — "
                f"{'well diversified' if concentration < 0.3 else 'concentrated'}."
            ),
            source="Analyst input",
        ))
    else:
        metrics.append(MetricScore(
            key="customer_concentration", label="Customer concentration", weight=0.13,
            score=5.0, origin=DataOrigin.MISSING,
            explanation="Customer concentration not disclosed.",
        ))

    # --- revenue durability --------------------------------------------------
    revenues = [r for r in inputs.series("income", "total_revenue", 6) if r is not None]
    if len(revenues) >= 3:
        declines = sum(1 for i in range(1, len(revenues)) if revenues[i] < revenues[i - 1])
        metrics.append(MetricScore(
            key="revenue_durability", label="Revenue durability", weight=0.10,
            score=band_score(declines, [(0, 10), (1, 7.5), (2, 5), (3, 3)],
                             higher_is_better=False),
            origin=DataOrigin.VERIFIED, value=float(declines), unit="count",
            explanation=(
                f"Revenue declined in {declines} of the last {len(revenues) - 1} years."
                if declines else
                f"Revenue grew in every one of the last {len(revenues) - 1} years."
            ),
            source=SOURCE_IS,
        ))

    return build_category(
        KEY.value, CATEGORY_LABELS[KEY], metrics, weight,
        [SOURCE_IS, SOURCE_BS, "Analyst input"],
    )
