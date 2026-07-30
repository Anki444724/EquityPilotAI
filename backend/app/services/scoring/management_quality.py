"""Management quality.

Judged primarily on evidence rather than impression: what returns has
management earned on the capital it chose to reinvest, has it kept margins and
returns stable, and does it pledge its own shares?

Incremental ROIC is the sharpest available test. Average ROIC reflects
decisions made over decades; incremental ROIC reflects the decisions this
management team is making now.
"""
from __future__ import annotations

from app.domain.calc import safe_div
from app.domain.scoring.base import (
    DataOrigin, MetricScore, band_score, build_category, trend_score,
)
from app.domain.scoring.inputs import ScoringInputs
from app.domain.scoring.weights import Category, CATEGORY_LABELS

KEY = Category.MANAGEMENT_QUALITY
SOURCE = "06 Historical IS, 07 Historical BS"


def incremental_roic(inputs: ScoringInputs, lookback: int = 3) -> float | None:
    """Change in NOPAT over change in invested capital.

    Measures the return on capital deployed during the period, which is what
    management actually controls.
    """
    if len(inputs.incomes) <= lookback or len(inputs.balances) <= lookback:
        return None
    now, then = inputs.incomes[-1], inputs.incomes[-1 - lookback]
    bal_now, bal_then = inputs.balances[-1], inputs.balances[-1 - lookback]

    if now.effective_tax_rate is None or then.effective_tax_rate is None:
        return None
    nopat_delta = (
        now.ebit * (1 - now.effective_tax_rate)
        - then.ebit * (1 - then.effective_tax_rate)
    )
    capital_delta = bal_now.invested_capital - bal_then.invested_capital
    if capital_delta <= 0:
        return None
    return nopat_delta / capital_delta


def score(inputs: ScoringInputs, weight: float):
    metrics: list[MetricScore] = []
    q = inputs.qualitative

    # --- incremental return on reinvested capital -------------------------
    incremental = incremental_roic(inputs)
    if incremental is not None and inputs.wacc:
        spread = incremental - inputs.wacc
        metrics.append(MetricScore(
            key="incremental_roic", label="Incremental ROIC vs WACC", weight=0.32,
            score=band_score(spread, [(0.10, 10), (0.05, 8.5), (0.0, 6.5), (-0.05, 4), (-0.10, 2)]),
            origin=DataOrigin.VERIFIED, value=spread, unit="%",
            explanation=(
                f"Capital deployed over the last three years earned {incremental:.1%}, "
                f"a {spread:+.1%} spread over the {inputs.wacc:.1%} cost of capital — "
                f"management is {'creating' if spread > 0 else 'destroying'} value with new investment."
            ),
            source=SOURCE,
        ))
    else:
        metrics.append(MetricScore(
            key="incremental_roic", label="Incremental ROIC vs WACC", weight=0.32,
            score=5.0, origin=DataOrigin.MISSING,
            explanation=(
                "Incremental ROIC not measurable — insufficient history or no net "
                "capital was deployed over the period."
            ),
        ))

    # --- consistency of returns --------------------------------------------
    roe_series: list[float | None] = []
    for i in range(min(5, len(inputs.incomes))):
        offset = min(5, len(inputs.incomes)) - 1 - i
        idx = len(inputs.incomes) - 1 - offset
        equity = inputs.avg_balance("shareholders_equity", offset)
        roe_series.append(safe_div(inputs.incomes[idx].pat, equity))

    present = [r for r in roe_series if r is not None]
    if len(present) >= 3:
        score_value, slope = trend_score(roe_series)
        metrics.append(MetricScore(
            key="roe_trend", label="ROE trajectory", weight=0.20,
            score=score_value, origin=DataOrigin.VERIFIED, value=slope, unit="%",
            explanation=(
                "Return on equity has held steady over the period."
                if abs(slope or 0) < 1e-4 else
                f"Return on equity has {'improved' if (slope or 0) > 0 else 'deteriorated'} "
                f"by {abs(slope or 0) * 10000:.0f} bps a year on average."
            ),
            source=SOURCE,
        ))

    # --- capital-allocation record (qualitative) -----------------------------
    if q.capital_allocation_record is not None:
        metrics.append(MetricScore(
            key="allocation_record", label="Capital-allocation record", weight=0.18,
            score=q.capital_allocation_record, origin=DataOrigin.ANALYST,
            value=q.capital_allocation_record, unit="/10",
            explanation=f"Analyst rates the capital-allocation record {q.capital_allocation_record:.0f}/10.",
            source="Analyst input",
        ))
    else:
        metrics.append(MetricScore(
            key="allocation_record", label="Capital-allocation record", weight=0.18,
            score=5.0, origin=DataOrigin.MISSING,
            explanation="No analyst assessment of the capital-allocation record on file.",
        ))

    # --- guidance credibility ------------------------------------------------
    if q.guidance_credibility is not None:
        metrics.append(MetricScore(
            key="guidance", label="Guidance credibility", weight=0.15,
            score=q.guidance_credibility, origin=DataOrigin.ANALYST,
            value=q.guidance_credibility, unit="/10",
            explanation=f"Guidance credibility rated {q.guidance_credibility:.0f}/10.",
            source="Analyst input",
        ))
    else:
        metrics.append(MetricScore(
            key="guidance", label="Guidance credibility", weight=0.15,
            score=5.0, origin=DataOrigin.MISSING,
            explanation="Management guidance has not been assessed against outcomes.",
        ))

    # --- promoter pledge: a direct alignment signal ---------------------------
    if q.promoter_pledge is not None:
        metrics.append(MetricScore(
            key="promoter_pledge", label="Promoter pledge", weight=0.15,
            score=band_score(q.promoter_pledge, [(0.0, 10), (0.05, 8), (0.15, 5.5), (0.30, 3), (0.50, 1)],
                             higher_is_better=False),
            origin=DataOrigin.VERIFIED, value=q.promoter_pledge, unit="%",
            explanation=(
                f"{q.promoter_pledge:.0%} of the promoter holding is pledged — "
                f"{'no encumbrance risk' if q.promoter_pledge == 0 else 'a financing-stress signal'}."
            ),
            source="14 Shareholding",
        ))
    else:
        metrics.append(MetricScore(
            key="promoter_pledge", label="Promoter pledge", weight=0.15,
            score=5.0, origin=DataOrigin.MISSING,
            explanation="Promoter pledge data not available.",
        ))

    return build_category(KEY.value, CATEGORY_LABELS[KEY], metrics, weight,
                          [SOURCE, "14 Shareholding", "Analyst input"])
