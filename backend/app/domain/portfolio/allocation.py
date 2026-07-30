"""Allocation across the five dimensions the brief requires.

Sector, industry, market cap, country and style. The first four are lookups;
style is derived, and that is where the design decision sits.

**Style is computed from platform scores, never hand-tagged.** A holding is
"value" because its valuation score is strong and its growth score is not, and
those numbers already exist in Module 5. Letting a user tag a position as
growth would make style allocation an opinion survey rather than a measurement.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Callable, Sequence

from app.domain.calc import safe_div
from app.domain.portfolio.types import (
    Allocation, AllocationDimension, AllocationSlice, MarketCapBand, Position,
    StyleBucket,
)

# ---------------------------------------------------------------------------
# Market-cap bands
# ---------------------------------------------------------------------------
#: SEBI's classification is rank-based (top 100 large, next 150 mid), which
#: needs the whole listed universe. These absolute thresholds in ₹ crore are
#: the conventional Indian market approximation and are declared here rather
#: than inlined so a caller can override them for a different market.
LARGE_CAP_FLOOR = 50_000.0
MID_CAP_FLOOR = 17_000.0
SMALL_CAP_FLOOR = 1_000.0


def market_cap_band(
    market_cap: float | None,
    *,
    large_floor: float = LARGE_CAP_FLOOR,
    mid_floor: float = MID_CAP_FLOOR,
    small_floor: float = SMALL_CAP_FLOOR,
) -> MarketCapBand:
    """Classify a market capitalisation in ₹ crore."""
    if market_cap is None or market_cap <= 0:
        return MarketCapBand.UNKNOWN
    if market_cap >= large_floor:
        return MarketCapBand.LARGE
    if market_cap >= mid_floor:
        return MarketCapBand.MID
    if market_cap >= small_floor:
        return MarketCapBand.SMALL
    return MarketCapBand.MICRO


#: Score margin below which two style signals are treated as tied.
STYLE_TIE_MARGIN = 5.0


def style_bucket(
    valuation_score: float | None,
    growth_score: float | None,
    quality_score: float | None,
    *,
    threshold: float = 60.0,
    tie_margin: float = STYLE_TIE_MARGIN,
) -> StyleBucket:
    """Derive a style from Module 5's category scores.

    All three scores are on the platform's 0–100 scale. A holding qualifies for
    a style only if that score clears `threshold`; where two clear it within
    `tie_margin` of each other the holding is BLEND, because calling a
    genuinely balanced position "growth" on a two-point margin is noise
    presented as signal.
    """
    candidates = {
        StyleBucket.VALUE: valuation_score,
        StyleBucket.GROWTH: growth_score,
        StyleBucket.QUALITY: quality_score,
    }
    qualifying = {
        bucket: score for bucket, score in candidates.items()
        if score is not None and score >= threshold
    }
    if not qualifying:
        # Nothing clears the bar. If we have scores at all this is a genuine
        # blend; if we have none we do not know.
        return (
            StyleBucket.BLEND
            if any(score is not None for score in candidates.values())
            else StyleBucket.UNKNOWN
        )
    if len(qualifying) == 1:
        return next(iter(qualifying))
    ranked = sorted(qualifying.items(), key=lambda item: -item[1])
    if ranked[0][1] - ranked[1][1] < tie_margin:
        return StyleBucket.BLEND
    return ranked[0][0]


# ---------------------------------------------------------------------------
# Allocation
# ---------------------------------------------------------------------------
_LABELS = {
    MarketCapBand.LARGE: "Large cap",
    MarketCapBand.MID: "Mid cap",
    MarketCapBand.SMALL: "Small cap",
    MarketCapBand.MICRO: "Micro cap",
    MarketCapBand.UNKNOWN: "Unclassified",
    StyleBucket.QUALITY: "Quality",
    StyleBucket.GROWTH: "Growth",
    StyleBucket.VALUE: "Value",
    StyleBucket.BLEND: "Blend",
    StyleBucket.UNKNOWN: "Unclassified",
}

UNCLASSIFIED = "unclassified"


@dataclass(frozen=True, slots=True)
class StyleInputs:
    """Per-ticker Module 5 scores used to derive style."""

    valuation: float | None = None
    growth: float | None = None
    quality: float | None = None


def build_allocation(
    dimension: AllocationDimension,
    positions: Sequence[Position],
    *,
    key_of: Callable[[Position], tuple[str, str]],
    targets: dict[str, float] | None = None,
) -> Allocation:
    """Group positions into an allocation cut.

    Weights are computed against the total *classified and unclassified* market
    value, so a portfolio with an unclassified holding does not show its
    remaining weights summing to more than one.
    """
    buckets: dict[str, dict] = defaultdict(
        lambda: {"value": 0.0, "count": 0, "pnl": 0.0, "label": ""}
    )
    total = 0.0
    unclassified = 0.0

    for position in positions:
        value = position.market_value
        if value is None or not position.is_open:
            continue
        total += value
        key, label = key_of(position)
        if not key or key == UNCLASSIFIED:
            unclassified += value
            key, label = UNCLASSIFIED, "Unclassified"
        bucket = buckets[key]
        bucket["value"] += value
        bucket["count"] += 1
        bucket["label"] = label
        pnl = position.unrealised_pnl
        if pnl is not None:
            bucket["pnl"] += pnl

    target_map = targets or {}
    slices = [
        AllocationSlice(
            key=key,
            label=data["label"] or key,
            market_value=round(data["value"], 4),
            weight=round(safe_div(data["value"], total) or 0.0, 6),
            position_count=data["count"],
            target_weight=target_map.get(key),
            unrealised_pnl=round(data["pnl"], 4),
        )
        for key, data in buckets.items()
    ]
    slices.sort(key=lambda s: (-s.market_value, s.label))
    return Allocation(
        dimension=dimension, slices=slices,
        unclassified_value=round(unclassified, 4),
    )


def by_sector(positions, targets=None) -> Allocation:
    return build_allocation(
        AllocationDimension.SECTOR, positions,
        key_of=lambda p: (p.sector or UNCLASSIFIED, p.sector or "Unclassified"),
        targets=targets,
    )


def by_industry(positions, targets=None) -> Allocation:
    return build_allocation(
        AllocationDimension.INDUSTRY, positions,
        key_of=lambda p: (p.industry or UNCLASSIFIED, p.industry or "Unclassified"),
        targets=targets,
    )


def by_market_cap(positions, targets=None) -> Allocation:
    def key_of(position: Position) -> tuple[str, str]:
        band = market_cap_band(position.market_cap)
        return band.value, _LABELS[band]

    return build_allocation(
        AllocationDimension.MARKET_CAP, positions, key_of=key_of, targets=targets
    )


def by_country(positions, targets=None) -> Allocation:
    return build_allocation(
        AllocationDimension.COUNTRY, positions,
        key_of=lambda p: (p.country or UNCLASSIFIED, p.country or "Unclassified"),
        targets=targets,
    )


def by_style(
    positions, style_inputs: dict[str, StyleInputs], targets=None
) -> Allocation:
    def key_of(position: Position) -> tuple[str, str]:
        scores = style_inputs.get(position.ticker)
        bucket = (
            style_bucket(scores.valuation, scores.growth, scores.quality)
            if scores else StyleBucket.UNKNOWN
        )
        return bucket.value, _LABELS[bucket]

    return build_allocation(
        AllocationDimension.STYLE, positions, key_of=key_of, targets=targets
    )
