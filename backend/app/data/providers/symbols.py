"""Universal symbol resolver.

One ticker has several spellings: a user types `RELIANCE`, Yahoo and FMP want
`RELIANCE.NS`, a display wants `NSE:RELIANCE`, and Finnhub wants whatever its
own coverage uses. Leaving each provider to guess produced the AAPL bug —
`.NS` appended to a US ticker, which every provider then rejected, so a symbol
the primary served perfectly was reported unsupported by all five tiers.

Resolution is deliberately explicit rather than heuristic. A bare symbol is
Indian only if it is in the platform's own NSE universe; anything else is
assumed to be a US listing, because that is the other market these providers
cover and guessing wrong is cheap to detect and expensive to debug.

Updated to handle broader Indian universe (Nifty500, BSE codes) without
hardcoding individual symbols, while preserving the AAPL fix.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from app.data.providers.currency import EXCHANGE_BY_SUFFIX, resolve_market

#: Explicit prefixes a user or a chart library may supply: "NASDAQ:AAPL".
_PREFIXED = re.compile(r"^(?P<venue>[A-Z]{2,6}):(?P<symbol>[A-Z0-9._-]{1,20})$")

#: Venue prefix to the suffix a data provider expects.
_VENUE_SUFFIX: dict[str, str] = {
    "NSE": ".NS", "NASDAQ": "", "NYSE": "", "AMEX": "", "ARCA": "",
    "BSE": ".BO", "LSE": ".L", "TSE": ".T", "HKEX": ".HK", "ASX": ".AX",
    "TSX": ".TO", "XETRA": ".DE", "EPA": ".PA", "KRX": ".KS",
}


@dataclass(frozen=True, slots=True)
class ResolvedSymbol:
    """Every spelling of one listing, plus where it trades."""

    #: As the user supplied it.
    raw: str
    #: Canonical, provider-facing: "AAPL", "RELIANCE.NS".
    canonical: str
    #: Bare, no venue: "AAPL", "RELIANCE".
    base: str
    #: Display form: "NASDAQ:AAPL", "NSE:RELIANCE".
    display: str
    exchange: str
    market: str
    currency: str
    timezone: str

    @property
    def is_indian(self) -> bool:
        return self.market == "India"

    @property
    def is_us(self) -> bool:
        return self.market == "United States"

    # Every provider currently wants the canonical form. Kept as separate
    # properties so a provider that diverges can be accommodated without the
    # callers having to learn about it.
    @property
    def finnhub(self) -> str:
        return self.canonical

    @property
    def fmp(self) -> str:
        return self.canonical

    @property
    def yahoo(self) -> str:
        return self.canonical

    def as_dict(self) -> dict[str, str]:
        return {
            "input": self.raw, "canonical": self.canonical, "base": self.base,
            "display": self.display, "exchange": self.exchange,
            "market": self.market, "currency": self.currency,
            "timezone": self.timezone,
        }


@lru_cache(maxsize=2048)
def _indian_universe() -> frozenset[str]:
    """Tickers the platform actually covers, used to decide the default."""
    try:
        from app.data.nse_universe import NSE_UNIVERSE

        return frozenset(row[0].upper() for row in NSE_UNIVERSE)
    except Exception:  # noqa: BLE001 - the resolver must not depend on it
        return frozenset()


@lru_cache(maxsize=4096)
def resolve(ticker: str) -> ResolvedSymbol:
    """Resolve any spelling into one canonical listing.

    Authoritative Indian identification uses the Company's exchange/bse_code/ISIN
    where available (see LiveMarketService._canonical_for_company). This resolver
    is the fallback for bare ticker strings without company context and is
    intentionally explicit to preserve the AAPL bug fix while expanding coverage:

    - Numeric tickers (BSE scrip codes) -> BSE .BO, India
    - Ticklers in NSE_UNIVERSE -> NSE .NS, India
    - Bare symbols containing digits, '-' or '&', or longer than 5 chars -> NSE .NS, India
      (covers 20MICRONS, 21STCENMGM, BHARATCP etc. without hardcoding)
    - Otherwise bare short alphabetic -> US (preserves AAPL fix)
    """
    raw = (ticker or "").strip()
    if not raw:
        raise ValueError("empty ticker")

    upper = raw.upper()
    suffix = ""

    prefixed = _PREFIXED.match(upper)
    if prefixed:
        venue = prefixed.group("venue")
        symbol = prefixed.group("symbol")
        suffix = _VENUE_SUFFIX.get(venue, "")
        base = symbol.split(".")[0]
        canonical = f"{base}{suffix}"
    else:
        known = [s for s in EXCHANGE_BY_SUFFIX if upper.endswith(s)]
        if known:
            suffix = known[0]
            base = upper[: -len(suffix)]
            canonical = upper
        elif "." in upper:
            # An unrecognised suffix. Left alone: inventing one would be
            # worse than passing through what the user meant.
            base, _, _ = upper.partition(".")
            canonical = upper
        elif upper.isdigit():
            # BSE scrip code: numeric, Yahoo uses <code>.BO, market India.
            # Do NOT blindly append .NS to BSE numeric codes.
            base, suffix, canonical = upper, ".BO", f"{upper}.BO"
        elif upper in _indian_universe():
            base, suffix, canonical = upper, ".NS", f"{upper}.NS"
        else:
            # Broader Indian heuristic without hardcoding the six symbols.
            # - Contains digit (20MICRONS, 21STCENMGM) => NSE Indian
            # - Contains '-' or '&' (BAJAJ-AUTO, M&M) => NSE Indian
            # - Length >5 (BHARATCP etc.) => likely NSE Indian, not US
            # Pure short alphabetic not in universe stays US (AAPL fix).
            has_digit = any(ch.isdigit() for ch in upper)
            has_special = ("-" in upper) or ("&" in upper)
            is_long = len(upper) > 5
            if has_digit or has_special or is_long:
                base, suffix, canonical = upper, ".NS", f"{upper}.NS"
            else:
                # A bare symbol not in the Indian universe is a US listing.
                # This is the AAPL fix: never append ".NS" by default.
                base, canonical = upper, upper

    exchange, market, currency, timezone = resolve_market(canonical)
    venue_label = (
        prefixed.group("venue") if prefixed
        else ("NASDAQ" if market == "United States" else exchange)
    )
    return ResolvedSymbol(
        raw=raw, canonical=canonical, base=base,
        display=f"{venue_label}:{base}", exchange=exchange, market=market,
        currency=currency, timezone=timezone,
    )
