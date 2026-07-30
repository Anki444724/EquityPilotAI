"""Weight profiles.

Weights encode an investment philosophy, so they are data, not code. A value
investor and a growth investor looking at the same company should reach
different conclusions, and the engine makes that difference explicit rather
than burying one house view in the source.

Profiles are normalised on construction, so a caller may supply relative
weights in any scale — 3/2/1 and 0.5/0.33/0.17 mean the same thing.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum


class Category(StrEnum):
    """The thirteen scoring categories."""

    BUSINESS_QUALITY = "business_quality"
    FINANCIAL_QUALITY = "financial_quality"
    MANAGEMENT_QUALITY = "management_quality"
    CAPITAL_ALLOCATION = "capital_allocation"
    COMPETITIVE_MOAT = "competitive_moat"
    GOVERNANCE = "governance"
    FINANCIAL_RISK = "financial_risk"
    BUSINESS_RISK = "business_risk"
    VALUATION = "valuation"
    GROWTH_QUALITY = "growth_quality"
    CASH_FLOW_QUALITY = "cash_flow_quality"
    ESG = "esg"
    MOMENTUM = "momentum"


CATEGORY_LABELS: dict[Category, str] = {
    Category.BUSINESS_QUALITY: "Business Quality",
    Category.FINANCIAL_QUALITY: "Financial Quality",
    Category.MANAGEMENT_QUALITY: "Management Quality",
    Category.CAPITAL_ALLOCATION: "Capital Allocation",
    Category.COMPETITIVE_MOAT: "Competitive Moat",
    Category.GOVERNANCE: "Corporate Governance",
    Category.FINANCIAL_RISK: "Financial Risk",
    Category.BUSINESS_RISK: "Business Risk",
    Category.VALUATION: "Valuation",
    Category.GROWTH_QUALITY: "Growth Quality",
    Category.CASH_FLOW_QUALITY: "Cash Flow Quality",
    Category.ESG: "ESG",
    Category.MOMENTUM: "Momentum",
}


@dataclass(frozen=True, slots=True)
class WeightProfile:
    """A named set of category weights, normalised to sum to 1."""

    key: str
    label: str
    description: str
    weights: dict[str, float]
    is_builtin: bool = True

    def __post_init__(self) -> None:
        total = sum(self.weights.values())
        if total <= 0:
            raise ValueError(f"profile '{self.key}' has non-positive total weight")
        # frozen dataclass: normalise via object.__setattr__
        object.__setattr__(
            self, "weights", {k: v / total for k, v in self.weights.items()}
        )

    def weight_for(self, category: Category | str) -> float:
        key = category.value if isinstance(category, Category) else category
        return self.weights.get(key, 0.0)

    def with_overrides(self, overrides: dict[str, float], key: str = "custom",
                       label: str = "Custom") -> "WeightProfile":
        """Derive a custom profile by overriding selected categories."""
        unknown = set(overrides) - {c.value for c in Category}
        if unknown:
            raise ValueError(f"unknown categories: {sorted(unknown)}")
        merged = {**self.weights, **overrides}
        return WeightProfile(
            key=key, label=label,
            description=f"Derived from {self.label} with {len(overrides)} override(s).",
            weights=merged, is_builtin=False,
        )

    def top_categories(self, n: int = 3) -> list[tuple[str, float]]:
        return sorted(self.weights.items(), key=lambda kv: -kv[1])[:n]


def _profile(key: str, label: str, description: str, **weights: float) -> WeightProfile:
    full = {c.value: weights.get(c.value, 0.0) for c in Category}
    return WeightProfile(key=key, label=label, description=description, weights=full)


# ---------------------------------------------------------------------------
# Built-in profiles.
#
# Relative weights are expressed on a 0–100 scale for readability and are
# normalised on construction. Each reflects a coherent, recognisable philosophy
# rather than an arbitrary spread.
# ---------------------------------------------------------------------------

BALANCED = _profile(
    "balanced", "Balanced",
    "Even-handed across quality, risk and valuation. The default house view.",
    business_quality=12, financial_quality=13, management_quality=8,
    capital_allocation=8, competitive_moat=10, governance=7,
    financial_risk=9, business_risk=6, valuation=12,
    growth_quality=7, cash_flow_quality=8, esg=3, momentum=2,
)

CONSERVATIVE = _profile(
    "conservative", "Conservative",
    "Capital preservation first: balance-sheet strength, governance and cash "
    "generation outweigh growth and momentum.",
    business_quality=11, financial_quality=14, management_quality=8,
    capital_allocation=7, competitive_moat=9, governance=12,
    financial_risk=16, business_risk=9, valuation=8,
    growth_quality=2, cash_flow_quality=12, esg=4, momentum=0,
)

GROWTH = _profile(
    "growth", "Growth",
    "Prioritises durable compounding: growth quality, moat and reinvestment "
    "runway, tolerating a fuller valuation.",
    business_quality=13, financial_quality=10, management_quality=10,
    capital_allocation=11, competitive_moat=14, governance=5,
    financial_risk=4, business_risk=4, valuation=4,
    growth_quality=17, cash_flow_quality=4, esg=1, momentum=6,
)

VALUE = _profile(
    "value", "Value",
    "Price paid dominates. Demands a margin of safety and a sound balance "
    "sheet; discounts growth narratives.",
    business_quality=8, financial_quality=12, management_quality=6,
    capital_allocation=9, competitive_moat=7, governance=8,
    financial_risk=13, business_risk=6, valuation=25,
    growth_quality=2, cash_flow_quality=11, esg=1, momentum=0,
)

QUALITY = _profile(
    "quality", "Quality",
    "Franchise durability above all: moat, returns on capital, management and "
    "governance. Valuation matters least.",
    business_quality=16, financial_quality=14, management_quality=13,
    capital_allocation=12, competitive_moat=18, governance=10,
    financial_risk=5, business_risk=3, valuation=3,
    growth_quality=3, cash_flow_quality=8, esg=3, momentum=0,
)

BUILTIN_PROFILES: dict[str, WeightProfile] = {
    p.key: p for p in (BALANCED, CONSERVATIVE, GROWTH, VALUE, QUALITY)
}

DEFAULT_PROFILE = BALANCED


def get_profile(key: str | None) -> WeightProfile:
    """Look up a built-in profile, falling back to the default."""
    if not key:
        return DEFAULT_PROFILE
    profile = BUILTIN_PROFILES.get(key.lower())
    if profile is None:
        raise KeyError(f"unknown weight profile '{key}'")
    return profile
