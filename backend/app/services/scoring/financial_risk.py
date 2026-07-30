"""Financial risk.

Balance-sheet fragility: leverage, coverage, liquidity and distress proximity.
Scored inversely — low leverage and high coverage produce a high score, so that
across the framework a higher number is always better.
"""
from __future__ import annotations

from app.domain.calc import safe_div
from app.domain.scoring.base import (
    DataOrigin, MetricScore, band_score, build_category,
)
from app.domain.scoring.inputs import ScoringInputs
from app.domain.scoring.weights import Category, CATEGORY_LABELS

KEY = Category.FINANCIAL_RISK
SOURCE = "07 Historical BS, 06 Historical IS"

#: Altman Z-score coefficients (manufacturing variant), as used in Module 2.
ALTMAN = {"wc": 1.2, "re": 1.4, "ebit": 3.3, "eq": 0.6, "turnover": 1.0}


def score(inputs: ScoringInputs, weight: float):
    metrics: list[MetricScore] = []
    balance, income = inputs.latest_balance, inputs.latest_income

    # --- net leverage ------------------------------------------------------
    if balance and income and income.ebitda:
        net_debt_ebitda = safe_div(balance.net_debt, income.ebitda)
        if net_debt_ebitda is not None:
            metrics.append(MetricScore(
                key="net_debt_ebitda", label="Net debt / EBITDA", weight=0.28,
                score=band_score(net_debt_ebitda,
                                 [(0.0, 10), (1.0, 8.5), (2.0, 7), (3.0, 5), (4.0, 2.5)],
                                 higher_is_better=False),
                origin=DataOrigin.VERIFIED, value=net_debt_ebitda, unit="x",
                explanation=(
                    f"Net {'cash' if net_debt_ebitda < 0 else 'debt'} at "
                    f"{abs(net_debt_ebitda):.2f}x EBITDA — "
                    f"{'a fortress balance sheet' if net_debt_ebitda < 0 else 'comfortable' if net_debt_ebitda < 2 else 'stretched'}."
                ),
                source=SOURCE,
            ))

    # --- interest coverage --------------------------------------------------
    if income and income.finance_costs:
        coverage = safe_div(income.ebit, income.finance_costs)
        if coverage is not None:
            metrics.append(MetricScore(
                key="interest_coverage", label="Interest coverage", weight=0.24,
                score=band_score(coverage, [(10, 10), (6, 8.5), (4, 7), (2.5, 5), (1.5, 2.5)]),
                origin=DataOrigin.VERIFIED, value=coverage, unit="x",
                explanation=(
                    f"EBIT covers interest {coverage:.1f}x — "
                    f"{'ample headroom' if coverage > 6 else 'thin cover' if coverage < 2.5 else 'adequate'}."
                ),
                source=SOURCE,
            ))
    elif income:
        metrics.append(MetricScore(
            key="interest_coverage", label="Interest coverage", weight=0.24,
            score=10.0, origin=DataOrigin.VERIFIED, value=None, unit="x",
            explanation="No finance costs — the company carries no meaningful interest burden.",
            source=SOURCE,
        ))

    # --- gearing ------------------------------------------------------------
    if balance:
        gearing = safe_div(balance.gross_debt, balance.shareholders_equity)
        if gearing is not None:
            metrics.append(MetricScore(
                key="debt_equity", label="Debt / equity", weight=0.18,
                score=band_score(gearing, [(0.10, 10), (0.35, 8.5), (0.65, 7), (1.0, 5), (1.75, 2.5)],
                                 higher_is_better=False),
                origin=DataOrigin.VERIFIED, value=gearing, unit="x",
                explanation=f"Gross debt is {gearing:.2f}x shareholders' equity.",
                source=SOURCE,
            ))

    # --- liquidity ----------------------------------------------------------
    if balance:
        current = safe_div(balance.total_current_assets, balance.total_current_liabilities)
        if current is not None:
            metrics.append(MetricScore(
                key="current_ratio", label="Current ratio", weight=0.14,
                score=band_score(current, [(2.0, 9.5), (1.5, 8.5), (1.2, 7), (1.0, 5), (0.8, 2.5)]),
                origin=DataOrigin.VERIFIED, value=current, unit="x",
                explanation=(
                    f"Current assets cover current liabilities {current:.2f}x."
                ),
                source=SOURCE,
            ))

    # --- distress proximity --------------------------------------------------
    if balance and income and balance.total_assets:
        parts = [
            ALTMAN["wc"] * safe_div(balance.net_working_capital, balance.total_assets),
            ALTMAN["re"] * safe_div(balance.reserves_surplus, balance.total_assets),
            ALTMAN["ebit"] * safe_div(income.ebit, balance.total_assets),
            ALTMAN["eq"] * safe_div(balance.shareholders_equity, balance.total_liabilities),
            ALTMAN["turnover"] * safe_div(income.total_revenue, balance.total_assets),
        ]
        if all(p is not None for p in parts):
            z = sum(parts)
            metrics.append(MetricScore(
                key="altman_z", label="Altman Z-score", weight=0.16,
                score=band_score(z, [(3.0, 10), (2.6, 8), (2.0, 6), (1.8, 4), (1.1, 2)]),
                origin=DataOrigin.VERIFIED, value=z, unit="ratio",
                explanation=(
                    f"Altman Z of {z:.2f} places the company in the "
                    f"{'safe' if z > 2.99 else 'grey' if z > 1.81 else 'distress'} zone."
                ),
                source=SOURCE,
            ))

    return build_category(KEY.value, CATEGORY_LABELS[KEY], metrics, weight, [SOURCE])
