"""Sum-of-the-parts and replacement value.

SOTP values a conglomerate segment by segment, each on the multiple
appropriate to *its* industry, then applies a holding-company discount. This
matters when a group's segments would trade at very different multiples: a
blended multiple on consolidated EBITDA systematically misprices them.

Each segment may be valued on EV/EBITDA, EV/Sales, P/E or a supplied DCF value,
so a cash-generative core and a loss-making growth arm can be valued on the
bases that actually suit them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from app.domain.calc import safe_div


class SegmentBasis(StrEnum):
    EV_EBITDA = "ev_ebitda"
    EV_SALES = "ev_sales"
    PE = "pe"
    BOOK = "book"
    DCF = "dcf"


@dataclass(frozen=True, slots=True)
class SOTPSegment:
    name: str
    basis: SegmentBasis
    multiple: float | None = None
    #: EBITDA, revenue, PAT or book value, per the basis.
    metric: float | None = None
    #: Supplied directly when basis is DCF.
    direct_value: float | None = None
    #: Debt attributable to this segment, if tracked separately.
    attributed_debt: float = 0.0
    stake: float = 1.0
    note: str | None = None


@dataclass(frozen=True, slots=True)
class SOTPSegmentResult:
    name: str
    basis: str
    multiple: float | None
    metric: float | None
    gross_value: float | None
    attributable_value: float | None
    stake: float
    share_of_total: float | None
    note: str | None


@dataclass(frozen=True, slots=True)
class SOTPResult:
    segments: list[SOTPSegmentResult]
    gross_asset_value: float
    net_debt: float
    holding_discount: float
    discount_amount: float
    equity_value: float
    shares_outstanding: float
    value_per_share: float | None
    current_price: float | None
    upside: float | None
    warnings: list[str] = field(default_factory=list)


def run_sotp(
    segments: list[SOTPSegment],
    *,
    net_debt: float = 0.0,
    holding_discount: float = 0.15,
    shares_outstanding: float = 0.0,
    current_price: float | None = None,
    unallocated_assets: float = 0.0,
) -> SOTPResult:
    """Value each segment on its own basis, then consolidate."""
    warnings: list[str] = []
    results: list[SOTPSegmentResult] = []
    gross = 0.0

    for seg in segments:
        if seg.basis is SegmentBasis.DCF:
            value = seg.direct_value
        elif seg.multiple is not None and seg.metric is not None:
            value = seg.multiple * seg.metric
        else:
            value = None
            warnings.append(f"Segment '{seg.name}' lacks the inputs for a {seg.basis.value} valuation.")

        if value is not None and seg.basis in (SegmentBasis.EV_EBITDA, SegmentBasis.EV_SALES):
            # EV-based segments carry their own debt to an equity contribution.
            value -= seg.attributed_debt

        attributable = value * seg.stake if value is not None else None
        if attributable is not None:
            gross += attributable

        results.append(
            SOTPSegmentResult(
                name=seg.name, basis=seg.basis.value, multiple=seg.multiple,
                metric=seg.metric, gross_value=value, attributable_value=attributable,
                stake=seg.stake, share_of_total=None, note=seg.note,
            )
        )

    gross += unallocated_assets

    # Recompute shares now that the total is known.
    results = [
        SOTPSegmentResult(
            name=r.name, basis=r.basis, multiple=r.multiple, metric=r.metric,
            gross_value=r.gross_value, attributable_value=r.attributable_value,
            stake=r.stake, share_of_total=safe_div(r.attributable_value, gross),
            note=r.note,
        )
        for r in results
    ]

    discount_amount = gross * holding_discount
    equity_value = gross - discount_amount - net_debt
    per_share = safe_div(equity_value, shares_outstanding)

    if equity_value < 0:
        warnings.append("Sum-of-the-parts equity value is negative after net debt.")

    return SOTPResult(
        segments=results,
        gross_asset_value=gross,
        net_debt=net_debt,
        holding_discount=holding_discount,
        discount_amount=discount_amount,
        equity_value=equity_value,
        shares_outstanding=shares_outstanding,
        value_per_share=per_share,
        current_price=current_price,
        upside=safe_div(per_share, current_price) - 1 if per_share and current_price else None,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Replacement value (Tobin's Q framing)
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ReplacementValueResult:
    """What it would cost to rebuild the business from scratch.

    A floor valuation for asset-heavy businesses, and the denominator of
    Tobin's Q. Marked future-ready: the inflation-adjusted gross-block estimate
    below is a first-order approximation, and a rigorous version needs an
    asset-register vintage profile, which arrives with document ingestion.
    """

    net_block: float
    inflation_adjustment: float
    adjusted_fixed_assets: float
    net_working_capital: float
    intangible_replacement: float
    total_replacement_cost: float
    net_debt: float
    equity_replacement_value: float
    shares_outstanding: float
    value_per_share: float | None
    tobins_q: float | None
    current_price: float | None
    upside: float | None
    warnings: list[str] = field(default_factory=list)


def run_replacement_value(
    *,
    net_block: float,
    net_working_capital: float,
    net_debt: float,
    shares_outstanding: float,
    market_cap: float | None = None,
    asset_age_years: float = 7.0,
    inflation_rate: float = 0.05,
    intangible_replacement: float = 0.0,
    current_price: float | None = None,
) -> ReplacementValueResult:
    """Estimate replacement cost and Tobin's Q."""
    uplift = (1 + inflation_rate) ** asset_age_years
    adjusted = net_block * uplift
    total = adjusted + net_working_capital + intangible_replacement
    equity_value = total - net_debt
    per_share = safe_div(equity_value, shares_outstanding)

    return ReplacementValueResult(
        net_block=net_block,
        inflation_adjustment=uplift - 1,
        adjusted_fixed_assets=adjusted,
        net_working_capital=net_working_capital,
        intangible_replacement=intangible_replacement,
        total_replacement_cost=total,
        net_debt=net_debt,
        equity_replacement_value=equity_value,
        shares_outstanding=shares_outstanding,
        value_per_share=per_share,
        tobins_q=safe_div(market_cap, equity_value) if market_cap else None,
        current_price=current_price,
        upside=safe_div(per_share, current_price) - 1 if per_share and current_price else None,
        warnings=[
            "Replacement cost uses an inflation-indexed gross-block proxy. A rigorous "
            "estimate requires an asset-register vintage profile."
        ],
    )
