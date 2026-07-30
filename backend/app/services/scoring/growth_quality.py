"""Growth quality.

Not how fast, but how good. Growth funded by dilution or leverage, or that
outruns cash generation, is worth less than growth funded from operations at
stable margins.
"""
from __future__ import annotations

from app.domain.calc import cagr, safe_div
from app.domain.scoring.base import (
    DataOrigin, MetricScore, band_score, build_category,
)
from app.domain.scoring.inputs import ScoringInputs
from app.domain.scoring.weights import Category, CATEGORY_LABELS

KEY = Category.GROWTH_QUALITY
SOURCE = "06 Historical IS"


def score(inputs: ScoringInputs, weight: float):
    metrics: list[MetricScore] = []
    revenues = [r for r in inputs.series("income", "total_revenue", 6) if r is not None]

    # --- historical revenue growth -----------------------------------------
    if len(revenues) >= 3:
        historical = cagr(revenues[0], revenues[-1], len(revenues) - 1)
        if historical is not None:
            metrics.append(MetricScore(
                key="revenue_cagr", label="Historical revenue CAGR", weight=0.24,
                score=band_score(historical, [(0.18, 10), (0.12, 8.5), (0.08, 7), (0.04, 5), (0.0, 3)]),
                origin=DataOrigin.VERIFIED, value=historical, unit="%",
                explanation=f"Revenue compounded at {historical:.1%} over {len(revenues) - 1} years.",
                source=SOURCE,
            ))

    # --- profit growth outpacing revenue -------------------------------------
    pats = [p for p in inputs.series("income", "pat", 6) if p is not None]
    if len(pats) >= 3 and len(revenues) >= 3 and pats[0] > 0:
        pat_cagr = cagr(pats[0], pats[-1], len(pats) - 1)
        rev_cagr = cagr(revenues[0], revenues[-1], len(revenues) - 1)
        if pat_cagr is not None and rev_cagr is not None:
            leverage = pat_cagr - rev_cagr
            metrics.append(MetricScore(
                key="operating_leverage", label="Profit vs revenue growth", weight=0.22,
                score=band_score(leverage, [(0.05, 10), (0.02, 8.5), (0.0, 7), (-0.03, 5), (-0.08, 2.5)]),
                origin=DataOrigin.VERIFIED, value=leverage, unit="%",
                explanation=(
                    f"Profit compounded at {pat_cagr:.1%} against revenue at {rev_cagr:.1%} — "
                    f"{'positive operating leverage' if leverage > 0 else 'margin erosion is offsetting top-line growth'}."
                ),
                source=SOURCE,
            ))

    # --- growth funded from operations ------------------------------------------
    cfos = [c for c in inputs.series("cash_flow", "cfo", 5) if c is not None]
    capexes = [abs(c) for c in inputs.series("cash_flow", "capex", 5) if c is not None]
    if cfos and capexes:
        self_funding = safe_div(sum(cfos), sum(capexes))
        if self_funding is not None:
            metrics.append(MetricScore(
                key="self_funded", label="Self-funded growth", weight=0.22,
                score=band_score(self_funding, [(2.5, 10), (1.6, 8.5), (1.1, 7), (0.8, 4.5), (0.5, 2)]),
                origin=DataOrigin.VERIFIED, value=self_funding, unit="x",
                explanation=(
                    f"Cumulative operating cash flow covers capex {self_funding:.2f}x — growth is "
                    f"{'comfortably self-funded' if self_funding > 1.5 else 'dependent on external funding'}."
                ),
                source="08 Historical CF",
            ))

    # --- forecast growth --------------------------------------------------------
    if inputs.forecast and inputs.forecast.revenue_cagr is not None:
        forecast_cagr = inputs.forecast.revenue_cagr
        metrics.append(MetricScore(
            key="forecast_cagr", label="Forecast revenue CAGR", weight=0.18,
            score=band_score(forecast_cagr, [(0.16, 10), (0.11, 8.5), (0.07, 7), (0.03, 5), (0.0, 3)]),
            origin=DataOrigin.ESTIMATED, value=forecast_cagr, unit="%",
            explanation=f"The forecast projects {forecast_cagr:.1%} compound revenue growth.",
            source="Forecast engine",
        ))

    # --- growth without dilution --------------------------------------------------
    # Both endpoints must be positive, not just the first.
    #
    # The original guard checked `shares[0] > 0` and then divided by
    # `shares[-1]`, which is a different element. LTIMindtree reports a zero
    # weighted-share count in its latest year — a real artefact of the
    # LTI/Mindtree merger, where the aggregator has the merged entity's
    # revenue but not yet its share count — and the whole scoring engine died
    # with ZeroDivisionError. One missing datum in one year took down every
    # category for that company, including the twelve that had nothing to do
    # with shares.
    shares = [s for s in inputs.series("income", "weighted_shares", 6) if s is not None]
    if (
        len(shares) >= 3 and len(revenues) >= 3
        and shares[0] > 0 and shares[-1] > 0
    ):
        per_share_growth = cagr(
            revenues[0] / shares[0], revenues[-1] / shares[-1], len(revenues) - 1
        )
        if per_share_growth is not None:
            metrics.append(MetricScore(
                key="per_share_growth", label="Revenue per share growth", weight=0.14,
                score=band_score(per_share_growth,
                                 [(0.15, 10), (0.10, 8.5), (0.06, 7), (0.02, 5), (0.0, 3)]),
                origin=DataOrigin.VERIFIED, value=per_share_growth, unit="%",
                explanation=(
                    f"Revenue per share compounded at {per_share_growth:.1%}, capturing the "
                    "effect of any share issuance on growth actually delivered to holders."
                ),
                source=SOURCE,
            ))

    return build_category(KEY.value, CATEGORY_LABELS[KEY], metrics, weight,
                          [SOURCE, "08 Historical CF", "Forecast engine"])
