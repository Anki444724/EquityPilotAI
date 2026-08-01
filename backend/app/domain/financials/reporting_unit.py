"""How a company's figures are denominated, and how they are labelled.

The platform was built for Indian listings, so "₹ crore" was not a variable —
it was a constant written into twenty-four evidence lines, the valuation
engine's display strings and every report template. That is correct for TCS
and a fabrication for Apple: the same code path would have told the language
model that Apple earned "416,161 ₹ cr", and the model would have written it up
faithfully, because every figure it was given was real and every citation
resolved.

This module makes the unit a property of the company rather than of the
codebase. A `ReportingUnit` carries the currency, the scale the statements are
stored in, and the symbol used to label them, so a US report says "$416,161 M"
and an Indian one says "₹267,021 cr" from one code path.

**The scale is part of the stored value's meaning, not a display choice.**
Indian statements are stored in crore (10^7); US statements are stored in
millions (10^6). Getting that wrong is a factor-of-ten error that no test of
formatting would catch, so the scale lives beside the currency and both travel
with the company record.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Scale(StrEnum):
    """The multiple that stored statement figures are expressed in."""

    CRORE = "crore"          # 1e7 — Indian reporting convention
    MILLION = "million"      # 1e6 — US reporting convention
    UNIT = "unit"            # absolute, no scaling


#: Multiplier from a stored figure back to absolute currency units.
SCALE_FACTOR: dict[Scale, float] = {
    Scale.CRORE: 1e7,
    Scale.MILLION: 1e6,
    Scale.UNIT: 1.0,
}

#: Short suffix appended to a labelled figure.
SCALE_SUFFIX: dict[Scale, str] = {
    Scale.CRORE: "cr",
    Scale.MILLION: "M",
    Scale.UNIT: "",
}

CURRENCY_SYMBOL: dict[str, str] = {
    "INR": "₹", "USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥",
}


@dataclass(frozen=True, slots=True)
class ReportingUnit:
    """Currency and scale for one company's financial statements."""

    currency: str = "INR"
    scale: Scale = Scale.CRORE

    @property
    def symbol(self) -> str:
        return CURRENCY_SYMBOL.get(self.currency.upper(), self.currency.upper())

    @property
    def money(self) -> str:
        """Unit string for a monetary evidence line: '₹ cr', '$ M'."""
        suffix = SCALE_SUFFIX[self.scale]
        return f"{self.symbol} {suffix}".strip()

    @property
    def per_share(self) -> str:
        """Unit for a per-share figure, which carries no scale.

        EPS is quoted per share in whole currency, never in crore or millions.
        Labelling it '₹ cr' — which the platform did, by using one unit string
        for every monetary row — overstates it by seven orders of magnitude.
        """
        return self.symbol

    def to_absolute(self, value: float | None) -> float | None:
        """Stored figure back to absolute currency units."""
        if value is None:
            return None
        return value * SCALE_FACTOR[self.scale]

    def from_absolute(self, value: float | None) -> float | None:
        """Absolute currency units into the stored scale."""
        if value is None:
            return None
        return value / SCALE_FACTOR[self.scale]

    def label(self, value: float | None, *, per_share: bool = False) -> str:
        if value is None:
            return "unavailable"
        unit = self.per_share if per_share else self.money
        return f"{value:,.2f} {unit}".strip()

    def as_dict(self) -> dict[str, str]:
        return {
            "currency": self.currency.upper(),
            "scale": self.scale.value,
            "symbol": self.symbol,
            "money_unit": self.money,
        }


#: The two conventions the platform serves.
INR_CRORE = ReportingUnit("INR", Scale.CRORE)
USD_MILLION = ReportingUnit("USD", Scale.MILLION)


def for_exchange(exchange: str | None, currency: str | None = None) -> ReportingUnit:
    """The reporting unit implied by where a company is listed.

    Currency wins when supplied, because a provider that names the reported
    currency knows the listing better than an exchange table does — the same
    precedence `resolve_market` already applies.
    """
    code = (currency or "").strip().upper()
    venue = (exchange or "").strip().upper()

    if code == "INR" or venue in {"NSE", "BSE"}:
        return INR_CRORE
    if code and code != "USD":
        # A currency the platform has no scale convention for. Millions is the
        # international filing norm; the currency is preserved so the label is
        # at least honest about what the figures are denominated in.
        return ReportingUnit(code, Scale.MILLION)
    return USD_MILLION
