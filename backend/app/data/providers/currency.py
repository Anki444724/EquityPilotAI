"""Currency-aware money formatting.

The platform grew up Indian: every large figure was divided by a crore and
labelled "cr". That is right for an NSE listing and quietly wrong for anything
else — Apple's market capitalisation rendered as "489,721 cr" is arithmetically
defensible and semantically nonsense, because nobody quotes a US company in
crore and a reader skimming the number has no way to know it is dollars.

So the provider's own currency is preserved and the scale convention follows
the currency rather than the platform's origin: Indian figures in lakh crore
and crore, Western ones in trillion/billion/million, yen in 兆/億.

No conversion happens anywhere in this module. Converting would require a
rate, a rate has a timestamp, and an unlabelled converted figure is worse than
an honestly labelled foreign one.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: Indian numbering. One crore is 10^7, one lakh crore is 10^12.
_CRORE = 1e7
_LAKH_CRORE = 1e12

#: Currencies that use the Indian crore/lakh convention.
INDIAN_CURRENCIES = frozenset({"INR"})

SYMBOLS: dict[str, str] = {
    "INR": "₹", "USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥",
    "CNY": "¥", "HKD": "HK$", "AUD": "A$", "CAD": "C$", "CHF": "CHF",
    "SGD": "S$", "KRW": "₩", "BRL": "R$", "ZAR": "R",
}

#: Where a listing trades, derived from the symbol suffix. Used to fill in a
#: currency when a provider omits it, and to report the market and timezone
#: the brief asks for.
EXCHANGE_BY_SUFFIX: dict[str, tuple[str, str, str, str]] = {
    # suffix: (exchange, market, currency, IANA timezone)
    ".NS": ("NSE", "India", "INR", "Asia/Kolkata"),
    ".BO": ("BSE", "India", "INR", "Asia/Kolkata"),
    ".L": ("LSE", "United Kingdom", "GBP", "Europe/London"),
    ".DE": ("XETRA", "Germany", "EUR", "Europe/Berlin"),
    ".PA": ("Euronext Paris", "France", "EUR", "Europe/Paris"),
    ".AS": ("Euronext Amsterdam", "Netherlands", "EUR", "Europe/Amsterdam"),
    ".MI": ("Borsa Italiana", "Italy", "EUR", "Europe/Rome"),
    ".SW": ("SIX", "Switzerland", "CHF", "Europe/Zurich"),
    ".T": ("TSE", "Japan", "JPY", "Asia/Tokyo"),
    ".HK": ("HKEX", "Hong Kong", "HKD", "Asia/Hong_Kong"),
    ".SS": ("SSE", "China", "CNY", "Asia/Shanghai"),
    ".SZ": ("SZSE", "China", "CNY", "Asia/Shanghai"),
    ".AX": ("ASX", "Australia", "AUD", "Australia/Sydney"),
    ".TO": ("TSX", "Canada", "CAD", "America/Toronto"),
    ".KS": ("KRX", "South Korea", "KRW", "Asia/Seoul"),
    ".SA": ("B3", "Brazil", "BRL", "America/Sao_Paulo"),
}

#: A bare symbol with no suffix is a US listing.
US_DEFAULT = ("NASDAQ/NYSE", "United States", "USD", "America/New_York")


@dataclass(frozen=True, slots=True)
class Money:
    """An amount that knows what it is denominated in."""

    amount: float | None
    currency: str = "USD"

    @property
    def symbol(self) -> str:
        return SYMBOLS.get(self.currency.upper(), "")

    def formatted(self) -> str | None:
        """Human-readable, scaled by the convention of its own currency."""
        if self.amount is None:
            return None
        return format_money(self.amount, self.currency)

    def as_dict(self) -> dict[str, Any]:
        return {
            "amount": self.amount,
            "currency": self.currency,
            "symbol": self.symbol,
            "display": self.formatted(),
            "scale": scale_name(self.amount, self.currency),
        }


def scale_name(amount: float | None, currency: str) -> str | None:
    """Which unit the display uses, so a caller can label an axis."""
    if amount is None:
        return None
    value = abs(amount)
    if currency.upper() in INDIAN_CURRENCIES:
        if value >= _LAKH_CRORE:
            return "lakh crore"
        if value >= _CRORE:
            return "crore"
        return "units"
    if value >= 1e12:
        return "trillion"
    if value >= 1e9:
        return "billion"
    if value >= 1e6:
        return "million"
    return "units"


def format_money(amount: float | None, currency: str = "USD") -> str | None:
    """Format in the scale a reader of that currency actually uses.

    Indian currencies take crore and lakh crore; everything else takes the
    short scale. Japanese yen additionally uses 兆 and 億, which is what a
    Japanese filing quotes.
    """
    if amount is None:
        return None

    code = (currency or "USD").upper()
    symbol = SYMBOLS.get(code, "")
    value = float(amount)
    sign = "-" if value < 0 else ""
    value = abs(value)

    if code in INDIAN_CURRENCIES:
        if value >= _LAKH_CRORE:
            return f"{sign}{symbol}{value / _LAKH_CRORE:,.2f} lakh crore"
        if value >= _CRORE:
            return f"{sign}{symbol}{value / _CRORE:,.2f} crore"
        return f"{sign}{symbol}{value:,.2f}"

    if code == "JPY":
        if value >= 1e12:
            return f"{sign}{symbol}{value / 1e12:,.2f}兆"
        if value >= 1e8:
            return f"{sign}{symbol}{value / 1e8:,.2f}億"
        return f"{sign}{symbol}{value:,.0f}"

    if value >= 1e12:
        return f"{sign}{symbol}{value / 1e12:,.2f}T"
    if value >= 1e9:
        return f"{sign}{symbol}{value / 1e9:,.2f}B"
    if value >= 1e6:
        return f"{sign}{symbol}{value / 1e6:,.2f}M"
    return f"{sign}{symbol}{value:,.2f}"


def to_crore(amount: float | None, currency: str) -> float | None:
    """Crore, but only for a currency that uses crore.

    Retained because the platform's own database, valuation engine and
    reports are denominated in ₹ crore throughout. Returns None for a foreign
    currency rather than a misleading number: the caller should be forced to
    notice, not handed a figure that looks Indian and is not.
    """
    if amount is None or (currency or "").upper() not in INDIAN_CURRENCIES:
        return None
    return round(amount / _CRORE, 2)


def resolve_market(symbol: str, *, provider_currency: str | None = None
                   ) -> tuple[str, str, str, str]:
    """(exchange, market, currency, timezone) for a fully-qualified symbol.

    The provider's own currency wins when it supplies one — it knows the
    listing better than a suffix table does. The table fills the gap when it
    does not, which is common on free tiers.
    """
    upper = (symbol or "").strip().upper()
    for suffix, meta in EXCHANGE_BY_SUFFIX.items():
        if upper.endswith(suffix):
            exchange, market, currency, timezone = meta
            return exchange, market, (provider_currency or currency).upper(), timezone
    exchange, market, currency, timezone = US_DEFAULT
    return exchange, market, (provider_currency or currency).upper(), timezone
