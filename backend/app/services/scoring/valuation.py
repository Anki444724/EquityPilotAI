"""Valuation attractiveness.

Consumes Module 4's outputs — it does not revalue anything. Upside to intrinsic
value carries the most weight, tempered by the absolute multiples paid and by
how far the market price sits from the multiple fundamentals justify.
"""
from __future__ import annotations

from app.domain.scoring.base import (
    DataOrigin, MetricScore, band_score, build_category,
)
from app.domain.scoring.inputs import ScoringInputs
from app.domain.scoring.weights import Category, CATEGORY_LABELS

KEY = Category.VALUATION
SOURCE = "24 DCF Valuation, 25 Relative Valuation"


def score(inputs: ScoringInputs, weight: float):
    metrics: list[MetricScore] = []

    # --- upside to intrinsic value ------------------------------------------
    if inputs.upside is not None:
        metrics.append(MetricScore(
            key="upside", label="Upside to intrinsic value", weight=0.36,
            score=band_score(inputs.upside,
                             [(0.40, 10), (0.25, 8.5), (0.10, 7), (-0.05, 5), (-0.20, 2.5)]),
            origin=DataOrigin.ESTIMATED, value=inputs.upside, unit="%",
            explanation=(
                f"The blended intrinsic value implies {inputs.upside:+.0%} against the market price — "
                f"{'a substantial discount' if inputs.upside > 0.25 else 'broadly fair' if inputs.upside > -0.1 else 'a premium to fair value'}."
            ),
            source=SOURCE,
        ))
    else:
        metrics.append(MetricScore(
            key="upside", label="Upside to intrinsic value", weight=0.36,
            score=5.0, origin=DataOrigin.MISSING,
            explanation="No intrinsic value available; valuation cannot be scored on upside.",
        ))

    # --- earnings multiple ----------------------------------------------------
    if inputs.pe_ratio is not None and inputs.pe_ratio > 0:
        metrics.append(MetricScore(
            key="pe", label="Price / earnings", weight=0.20,
            score=band_score(inputs.pe_ratio, [(12, 10), (18, 8), (25, 6), (35, 4), (50, 2)],
                             higher_is_better=False),
            origin=DataOrigin.VERIFIED, value=inputs.pe_ratio, unit="x",
            explanation=f"Trading on {inputs.pe_ratio:.1f}x trailing earnings.",
            source=SOURCE,
        ))

    # --- enterprise multiple ---------------------------------------------------
    if inputs.ev_ebitda is not None and inputs.ev_ebitda > 0:
        metrics.append(MetricScore(
            key="ev_ebitda", label="EV / EBITDA", weight=0.18,
            score=band_score(inputs.ev_ebitda, [(7, 10), (11, 8), (15, 6), (22, 4), (30, 2)],
                             higher_is_better=False),
            origin=DataOrigin.VERIFIED, value=inputs.ev_ebitda, unit="x",
            explanation=f"Enterprise value is {inputs.ev_ebitda:.1f}x EBITDA.",
            source=SOURCE,
        ))

    # --- price against the justified multiple -----------------------------------
    if inputs.justified_premium is not None:
        metrics.append(MetricScore(
            key="justified_premium", label="Premium to justified multiple", weight=0.16,
            score=band_score(inputs.justified_premium,
                             [(-0.20, 10), (-0.05, 8.5), (0.10, 6.5), (0.35, 4), (0.75, 2)],
                             higher_is_better=False),
            origin=DataOrigin.ESTIMATED, value=inputs.justified_premium, unit="%",
            explanation=(
                f"The market pays a {inputs.justified_premium:+.0%} "
                f"{'premium' if inputs.justified_premium > 0 else 'discount'} to the multiple "
                "fundamentals justify on growth, payout and cost of equity."
            ),
            source="25 Relative Valuation",
        ))

    # --- book multiple -----------------------------------------------------------
    if inputs.pb_ratio is not None and inputs.pb_ratio > 0:
        metrics.append(MetricScore(
            key="pb", label="Price / book", weight=0.10,
            score=band_score(inputs.pb_ratio, [(1.5, 9.5), (2.5, 8), (4.0, 6), (6.5, 4), (10, 2)],
                             higher_is_better=False),
            origin=DataOrigin.VERIFIED, value=inputs.pb_ratio, unit="x",
            explanation=f"Trading at {inputs.pb_ratio:.1f}x book value.",
            source=SOURCE,
        ))

    return build_category(KEY.value, CATEGORY_LABELS[KEY], metrics, weight, [SOURCE])
