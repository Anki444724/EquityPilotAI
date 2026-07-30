"""Financial quality.

Returns, margins and the structural soundness of the earnings themselves.
Where business quality asks whether the franchise is good, this asks whether
the reported numbers are strong and internally coherent.
"""
from __future__ import annotations

from app.domain.calc import safe_div
from app.domain.scoring.base import (
    DataOrigin, MetricScore, band_score, build_category, trend_score,
)
from app.domain.scoring.inputs import ScoringInputs
from app.domain.scoring.weights import Category, CATEGORY_LABELS

KEY = Category.FINANCIAL_QUALITY
SOURCE = "06 Historical IS, 07 Historical BS"


def score(inputs: ScoringInputs, weight: float):
    metrics: list[MetricScore] = []
    income = inputs.latest_income

    # --- return on equity -------------------------------------------------
    roe = safe_div(income.pat, inputs.avg_balance("shareholders_equity")) if income else None
    if roe is not None:
        metrics.append(MetricScore(
            key="roe", label="Return on equity", weight=0.24,
            score=band_score(roe, [(0.25, 10), (0.18, 8.5), (0.14, 7), (0.10, 5), (0.05, 3)]),
            origin=DataOrigin.VERIFIED, value=roe, unit="%",
            explanation=(
                f"ROE of {roe:.1%} on average equity is "
                f"{'excellent' if roe > 0.20 else 'healthy' if roe > 0.14 else 'modest'}."
            ),
            source=SOURCE,
        ))

    # --- return on capital employed ----------------------------------------
    roce = safe_div(income.ebit, inputs.avg_balance("capital_employed")) if income else None
    if roce is not None:
        metrics.append(MetricScore(
            key="roce", label="Return on capital employed", weight=0.22,
            score=band_score(roce, [(0.22, 10), (0.16, 8.5), (0.12, 7), (0.08, 5), (0.04, 3)]),
            origin=DataOrigin.VERIFIED, value=roce, unit="%",
            explanation=f"Pre-tax ROCE of {roce:.1%} on average capital employed.",
            source=SOURCE,
        ))

    # --- EBITDA margin -------------------------------------------------------
    if income and income.ebitda_margin is not None:
        metrics.append(MetricScore(
            key="ebitda_margin", label="EBITDA margin", weight=0.16,
            score=band_score(income.ebitda_margin,
                             [(0.25, 10), (0.18, 8.5), (0.13, 7), (0.08, 5), (0.04, 3)]),
            origin=DataOrigin.VERIFIED, value=income.ebitda_margin, unit="%",
            explanation=f"EBITDA margin of {income.ebitda_margin:.1%}.",
            source=SOURCE,
        ))

    # --- margin trajectory ---------------------------------------------------
    margin_series = inputs.series("income", "ebitda_margin", 5)
    if len([m for m in margin_series if m is not None]) >= 3:
        score_value, slope = trend_score(margin_series)
        metrics.append(MetricScore(
            key="margin_trend", label="Margin trajectory", weight=0.14,
            score=score_value, origin=DataOrigin.VERIFIED,
            value=slope, unit="%",
            explanation=(
                "EBITDA margin has been essentially flat over the period."
                if abs(slope or 0) < 1e-4 else
                f"EBITDA margin has {'expanded' if (slope or 0) > 0 else 'compressed'} by "
                f"{abs(slope or 0) * 10000:.0f} bps a year on average."
            ),
            source=SOURCE,
        ))

    # --- earnings quality: is PAT backed by cash? -----------------------------
    cash_flow = inputs.latest_cash_flow
    if cash_flow and income and income.pat:
        accrual = safe_div(cash_flow.cfo, income.pat)
        if accrual is not None:
            metrics.append(MetricScore(
                key="accrual_quality", label="CFO / PAT", weight=0.14,
                score=band_score(accrual, [(1.10, 10), (0.90, 8.5), (0.75, 6.5), (0.55, 4), (0.30, 2)]),
                origin=DataOrigin.VERIFIED, value=accrual, unit="x",
                explanation=(
                    f"Operating cash flow covers {accrual:.2f}x reported profit — "
                    f"{'earnings are cash-backed' if accrual >= 0.9 else 'a material accrual gap'}."
                ),
                source="08 Historical CF",
            ))

    # --- effective tax rate plausibility ---------------------------------------
    if income and income.effective_tax_rate is not None:
        rate = income.effective_tax_rate
        # Both an implausibly low and an implausibly high rate signal a problem.
        plausible = 0.15 <= rate <= 0.35
        metrics.append(MetricScore(
            key="tax_normality", label="Effective tax rate", weight=0.10,
            score=9.0 if plausible else 4.0,
            origin=DataOrigin.VERIFIED, value=rate, unit="%",
            explanation=(
                f"Effective tax rate of {rate:.1%} is "
                f"{'within the normal statutory band' if plausible else 'outside the normal band, which may indicate one-offs or reporting issues'}."
            ),
            source=SOURCE,
        ))

    return build_category(KEY.value, CATEGORY_LABELS[KEY], metrics, weight,
                          [SOURCE, "08 Historical CF"])
