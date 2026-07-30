"""Capital allocation.

Where management quality asks whether the team is competent, this asks how the
cash is actually being spent: reinvested, returned, or hoarded — and whether
reinvestment is earning its keep.

The reinvestment rate is judged *conditionally*. Heavy reinvestment is
excellent when returns are above the cost of capital and value-destroying when
they are not, so the same number scores differently depending on ROIC.
"""
from __future__ import annotations

from app.domain.calc import safe_div
from app.domain.scoring.base import (
    DataOrigin, MetricScore, band_score, build_category,
)
from app.domain.scoring.inputs import ScoringInputs
from app.domain.scoring.weights import Category, CATEGORY_LABELS

KEY = Category.CAPITAL_ALLOCATION
SOURCE = "08 Historical CF, 06 Historical IS"


def score(inputs: ScoringInputs, weight: float):
    metrics: list[MetricScore] = []
    income, cash_flow, balance = (
        inputs.latest_income, inputs.latest_cash_flow, inputs.latest_balance
    )

    # --- reinvestment rate, judged against returns -------------------------
    roic = None
    if income and income.effective_tax_rate is not None:
        nopat = income.ebit * (1 - income.effective_tax_rate)
        roic = safe_div(nopat, inputs.avg_balance("invested_capital"))

    if cash_flow and cash_flow.cfo:
        reinvestment = safe_div(abs(cash_flow.capex), cash_flow.cfo)
        if reinvestment is not None:
            creates_value = roic is not None and inputs.wacc is not None and roic > inputs.wacc
            if creates_value:
                # reinvesting at above-WACC returns is good; more is better
                metric_score = band_score(
                    reinvestment, [(0.60, 10), (0.40, 9), (0.25, 7.5), (0.10, 6), (0.0, 5)]
                )
                verdict = (
                    f"reinvesting {reinvestment:.0%} of operating cash at a "
                    f"{roic:.1%} return, above the {inputs.wacc:.1%} cost of capital — value accretive"
                )
            else:
                # reinvesting below WACC destroys value; restraint is better
                metric_score = band_score(
                    reinvestment, [(0.15, 9), (0.30, 7), (0.50, 5), (0.75, 3), (1.0, 1.5)],
                    higher_is_better=False,
                )
                verdict = (
                    f"reinvesting {reinvestment:.0%} of operating cash while returns sit at "
                    f"{roic:.1%}" if roic is not None else
                    f"reinvesting {reinvestment:.0%} of operating cash with returns unmeasured"
                )
                if roic is not None and inputs.wacc:
                    verdict += f", below the {inputs.wacc:.1%} cost of capital — value dilutive"

            metrics.append(MetricScore(
                key="reinvestment_rate", label="Reinvestment rate", weight=0.30,
                score=metric_score, origin=DataOrigin.VERIFIED,
                value=reinvestment, unit="%",
                explanation=f"Management is {verdict}.",
                source=SOURCE,
            ))

    # --- shareholder returns ------------------------------------------------
    if income and income.pat and income.pat > 0:
        payout = safe_div(income.dividend_paid, income.pat)
        if payout is not None:
            # A sensible payout is neither zero nor unsustainable.
            balanced = 0.15 <= payout <= 0.70
            metrics.append(MetricScore(
                key="payout_discipline", label="Dividend payout", weight=0.20,
                score=9.0 if balanced else (6.0 if payout < 0.15 else 3.5),
                origin=DataOrigin.VERIFIED, value=payout, unit="%",
                explanation=(
                    f"Payout ratio of {payout:.0%} is "
                    + ("balanced against reinvestment needs." if balanced
                       else "minimal — retained cash must earn its keep." if payout < 0.15
                       else "above sustainable levels given reinvestment needs.")
                ),
                source=SOURCE,
            ))

    # --- balance-sheet management ---------------------------------------------
    if balance and income and income.ebitda:
        leverage = safe_div(balance.net_debt, income.ebitda)
        if leverage is not None:
            # Both over-leverage and a lazy, over-capitalised balance sheet are faults.
            if leverage < -2.0:
                metric_score, note = 6.0, "an unusually large net cash pile that is not being deployed"
            elif leverage <= 2.0:
                metric_score, note = 9.0, "leverage kept within a prudent policy range"
            elif leverage <= 3.5:
                metric_score, note = 6.0, "leverage at the upper end of prudence"
            else:
                metric_score, note = 3.0, "leverage beyond a comfortable policy range"
            metrics.append(MetricScore(
                key="balance_sheet_policy", label="Balance-sheet management", weight=0.20,
                score=metric_score, origin=DataOrigin.VERIFIED, value=leverage, unit="x",
                explanation=f"Net debt/EBITDA of {leverage:.2f}x reflects {note}.",
                source=SOURCE,
            ))

    # --- dilution discipline ----------------------------------------------------
    share_series = [s for s in inputs.series("income", "weighted_shares", 5) if s is not None]
    if len(share_series) >= 3 and share_series[0] > 0:
        dilution = share_series[-1] / share_series[0] - 1
        metrics.append(MetricScore(
            key="dilution", label="Share-count discipline", weight=0.16,
            score=band_score(dilution, [(0.0, 10), (0.02, 8), (0.05, 6), (0.12, 3.5), (0.25, 1.5)],
                             higher_is_better=False),
            origin=DataOrigin.VERIFIED, value=dilution, unit="%",
            explanation=(
                f"Share count {'unchanged' if abs(dilution) < 0.005 else f'{dilution:+.1%}'} "
                f"over {len(share_series)} years — "
                f"{'no dilution of existing holders' if dilution <= 0.005 else 'existing holders have been diluted'}."
            ),
            source="06 Historical IS",
        ))

    # --- growth capex efficiency -------------------------------------------------
    if len(inputs.incomes) >= 4 and len(inputs.cash_flows) >= 4:
        revenue_delta = inputs.incomes[-1].total_revenue - inputs.incomes[-4].total_revenue
        capex_total = sum(abs(c.capex) for c in inputs.cash_flows[-4:-1])
        sales_to_capital = safe_div(revenue_delta, capex_total)
        if sales_to_capital is not None:
            metrics.append(MetricScore(
                key="sales_to_capital", label="Sales-to-capital ratio", weight=0.14,
                score=band_score(sales_to_capital, [(2.5, 10), (1.5, 8.5), (1.0, 7), (0.5, 5), (0.2, 3)]),
                origin=DataOrigin.VERIFIED, value=sales_to_capital, unit="x",
                explanation=(
                    f"Every rupee of capex over the last three years generated "
                    f"{sales_to_capital:.2f} of incremental annual revenue."
                ),
                source=SOURCE,
            ))

    return build_category(KEY.value, CATEGORY_LABELS[KEY], metrics, weight, [SOURCE])
