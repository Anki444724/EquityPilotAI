"""Competitive moat.

A moat claim is only credible if the financials corroborate it. This category
therefore blends two evidence types: qualitative moat sources (brand, switching
costs, network effects, cost advantage, intangibles, efficient scale) and the
quantitative footprint a real moat leaves — sustained excess returns and
defended margins.

A company scoring highly on qualitative sources but showing no excess return
over a decade does not have a moat; it has a story.
"""
from __future__ import annotations

from app.domain.calc import safe_div
from app.domain.scoring.base import (
    DataOrigin, MetricScore, band_score, build_category,
)
from app.domain.scoring.inputs import ScoringInputs
from app.domain.scoring.weights import Category, CATEGORY_LABELS

KEY = Category.COMPETITIVE_MOAT

#: The six recognised sources of durable advantage.
MOAT_SOURCES = (
    ("brand_strength", "Brand & intangible pricing power"),
    ("switching_costs", "Customer switching costs"),
    ("network_effects", "Network effects"),
    ("cost_advantage", "Structural cost advantage"),
    ("intangible_assets", "Patents, licences & regulatory"),
    ("efficient_scale", "Efficient scale"),
)


def excess_return_years(inputs: ScoringInputs) -> tuple[int, int]:
    """Years in which ROIC exceeded WACC, and years measurable."""
    if not inputs.wacc:
        return 0, 0
    hits = measurable = 0
    for offset in range(min(6, len(inputs.incomes))):
        idx = len(inputs.incomes) - 1 - offset
        income = inputs.incomes[idx]
        if income.effective_tax_rate is None:
            continue
        capital = inputs.avg_balance("invested_capital", offset)
        roic = safe_div(income.ebit * (1 - income.effective_tax_rate), capital)
        if roic is None:
            continue
        measurable += 1
        if roic > inputs.wacc:
            hits += 1
    return hits, measurable


def score(inputs: ScoringInputs, weight: float):
    metrics: list[MetricScore] = []
    q = inputs.qualitative

    # --- qualitative moat sources -------------------------------------------
    present = [(k, label, getattr(q, k)) for k, label in MOAT_SOURCES if getattr(q, k) is not None]
    if present:
        mean = sum(v for _, _, v in present) / len(present)
        strong = [label for _, label, v in present if v >= 7]
        metrics.append(MetricScore(
            key="moat_sources", label="Moat sources", weight=0.34,
            score=mean, origin=DataOrigin.ANALYST, value=mean, unit="/10",
            explanation=(
                f"{len(present)} of 6 moat sources assessed, averaging {mean:.1f}/10"
                + (f"; strongest are {', '.join(strong[:2])}." if strong
                   else " with no individually strong source.")
            ),
            source="Analyst input",
        ))
    else:
        metrics.append(MetricScore(
            key="moat_sources", label="Moat sources", weight=0.34, score=5.0,
            origin=DataOrigin.MISSING,
            explanation="No qualitative moat assessment on file; moat rests on financial evidence alone.",
        ))

    # --- quantitative corroboration: sustained excess returns ------------------
    hits, measurable = excess_return_years(inputs)
    if measurable >= 3:
        ratio = hits / measurable
        metrics.append(MetricScore(
            key="excess_return_persistence", label="Excess-return persistence", weight=0.30,
            score=band_score(ratio, [(1.0, 10), (0.80, 8.5), (0.60, 6.5), (0.35, 4), (0.15, 2)]),
            origin=DataOrigin.VERIFIED, value=ratio, unit="%",
            explanation=(
                f"ROIC exceeded the cost of capital in {hits} of {measurable} measurable years — "
                + ("persistent excess returns, the financial signature of a real moat."
                   if ratio >= 0.8 else
                   "excess returns are intermittent, which argues against a durable moat.")
            ),
            source="06 Historical IS, 07 Historical BS",
        ))
    else:
        metrics.append(MetricScore(
            key="excess_return_persistence", label="Excess-return persistence", weight=0.30,
            score=5.0, origin=DataOrigin.MISSING,
            explanation="Insufficient history to test whether excess returns persist.",
        ))

    # --- margin defence -------------------------------------------------------
    margins = [m for m in inputs.series("income", "gross_margin", 6) if m is not None]
    if len(margins) >= 3:
        erosion = margins[-1] - margins[0]
        metrics.append(MetricScore(
            key="margin_defence", label="Gross-margin defence", weight=0.20,
            score=band_score(erosion, [(0.02, 10), (0.0, 8), (-0.02, 6), (-0.05, 4), (-0.10, 2)]),
            origin=DataOrigin.VERIFIED, value=erosion, unit="%",
            explanation=(
                f"Gross margin has {'held or expanded' if erosion >= 0 else 'eroded'} by "
                f"{abs(erosion) * 10000:.0f} bps over {len(margins)} years — "
                f"{'pricing power is intact' if erosion >= -0.01 else 'competitive pressure is visible in pricing'}."
            ),
            source="06 Historical IS",
        ))

    # --- industry structure ----------------------------------------------------
    if q.porters_five_forces is not None:
        # Scale runs 1 (attractive) to 5 (hostile), so invert it.
        metrics.append(MetricScore(
            key="industry_structure", label="Industry structure", weight=0.16,
            score=band_score(q.porters_five_forces,
                             [(2.0, 9.5), (2.5, 8), (3.0, 6.5), (3.5, 5), (4.0, 3)],
                             higher_is_better=False),
            origin=DataOrigin.ANALYST, value=q.porters_five_forces, unit="/5",
            explanation=(
                f"Porter's Five Forces composite of {q.porters_five_forces:.1f}/5 — "
                f"{'a benign competitive structure' if q.porters_five_forces < 2.5 else 'a demanding competitive structure'}."
            ),
            source="Analyst input",
        ))
    else:
        metrics.append(MetricScore(
            key="industry_structure", label="Industry structure", weight=0.16,
            score=5.0, origin=DataOrigin.MISSING,
            explanation="Industry structure has not been assessed.",
        ))

    return build_category(KEY.value, CATEGORY_LABELS[KEY], metrics, weight,
                          ["Analyst input", "06 Historical IS", "07 Historical BS"])
