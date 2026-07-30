"""Cash-flow quality.

Cash is the hardest number in a set of accounts to manipulate, so this category
carries real forensic weight. It tests conversion, consistency, free cash
generation and how much of operating cash is consumed by maintenance capex.
"""
from __future__ import annotations

from app.domain.calc import safe_div
from app.domain.scoring.base import (
    DataOrigin, MetricScore, band_score, build_category,
)
from app.domain.scoring.inputs import ScoringInputs
from app.domain.scoring.weights import Category, CATEGORY_LABELS

KEY = Category.CASH_FLOW_QUALITY
SOURCE = "08 Historical CF"


def score(inputs: ScoringInputs, weight: float):
    metrics: list[MetricScore] = []
    cash_flow, income = inputs.latest_cash_flow, inputs.latest_income

    # --- cash conversion --------------------------------------------------
    if cash_flow and income and income.ebitda:
        conversion = safe_div(cash_flow.cfo, income.ebitda)
        if conversion is not None:
            metrics.append(MetricScore(
                key="cfo_ebitda", label="CFO / EBITDA", weight=0.24,
                score=band_score(conversion, [(0.85, 10), (0.70, 8.5), (0.55, 7), (0.40, 5), (0.20, 3)]),
                origin=DataOrigin.VERIFIED, value=conversion, unit="x",
                explanation=(
                    f"{conversion:.0%} of EBITDA converts to operating cash — "
                    f"{'excellent conversion' if conversion > 0.8 else 'material leakage into working capital or tax'}."
                ),
                source=SOURCE,
            ))

    # --- free cash flow generation ------------------------------------------
    if cash_flow and income and income.total_revenue:
        fcf_margin = safe_div(cash_flow.free_cash_flow, income.total_revenue)
        if fcf_margin is not None:
            metrics.append(MetricScore(
                key="fcf_margin", label="Free cash flow margin", weight=0.22,
                score=band_score(fcf_margin, [(0.15, 10), (0.10, 8.5), (0.05, 7), (0.0, 5), (-0.05, 2.5)]),
                origin=DataOrigin.VERIFIED, value=fcf_margin, unit="%",
                explanation=(
                    f"Free cash flow is {fcf_margin:.1%} of revenue — "
                    f"{'strongly self-funding' if fcf_margin > 0.08 else 'cash consumptive' if fcf_margin < 0 else 'modest'}."
                ),
                source=SOURCE,
            ))

    # --- consistency of free cash flow ---------------------------------------
    fcf_series = [f for f in inputs.series("cash_flow", "free_cash_flow", 6) if f is not None]
    if len(fcf_series) >= 3:
        positive = sum(1 for f in fcf_series if f > 0)
        ratio = positive / len(fcf_series)
        metrics.append(MetricScore(
            key="fcf_consistency", label="FCF consistency", weight=0.20,
            score=band_score(ratio, [(1.0, 10), (0.80, 8.5), (0.60, 6.5), (0.40, 4), (0.20, 2)]),
            origin=DataOrigin.VERIFIED, value=ratio, unit="%",
            explanation=(
                f"Free cash flow was positive in {positive} of {len(fcf_series)} years."
            ),
            source=SOURCE,
        ))

    # --- capex intensity against operating cash --------------------------------
    if cash_flow and cash_flow.cfo:
        intensity = safe_div(abs(cash_flow.capex), cash_flow.cfo)
        if intensity is not None:
            metrics.append(MetricScore(
                key="capex_intensity", label="Capex / CFO", weight=0.18,
                score=band_score(intensity, [(0.30, 10), (0.50, 8), (0.70, 6), (0.95, 4), (1.20, 2)],
                                 higher_is_better=False),
                origin=DataOrigin.VERIFIED, value=intensity, unit="%",
                explanation=(
                    f"Capex absorbs {intensity:.0%} of operating cash flow — "
                    f"{'light' if intensity < 0.4 else 'heavy' if intensity > 0.8 else 'moderate'} reinvestment burden."
                ),
                source=SOURCE,
            ))

    # --- working capital discipline ----------------------------------------------
    wc_series = [w for w in inputs.series("cash_flow", "working_capital_change", 5) if w is not None]
    if len(wc_series) >= 3 and income and income.total_revenue:
        drag = safe_div(sum(wc_series), sum(
            r for r in inputs.series("income", "total_revenue", 5) if r is not None
        ))
        if drag is not None:
            metrics.append(MetricScore(
                key="wc_discipline", label="Working-capital drag", weight=0.16,
                score=band_score(drag, [(0.0, 9.5), (-0.01, 8), (-0.02, 6.5), (-0.04, 4.5), (-0.07, 2.5)]),
                origin=DataOrigin.VERIFIED, value=drag, unit="%",
                explanation=(
                    f"Working capital {'released' if drag > 0 else 'absorbed'} "
                    f"{abs(drag):.1%} of cumulative revenue over the period."
                ),
                source=SOURCE,
            ))

    return build_category(KEY.value, CATEGORY_LABELS[KEY], metrics, weight, [SOURCE])
