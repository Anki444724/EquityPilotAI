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
def resolve(ticker: str, exchange: str | None = None) -> ResolvedSymbol:
    """Resolve any spelling into one canonical listing.

    ``exchange`` is authoritative when it comes from a Company row. The
    original resolver only knew the repository's old 120-symbol seed tuple,
    so most of the imported Nifty 500 was incorrectly treated as US-listed
    and sent to Yahoo without ``.NS``. Callers without company metadata keep
    the conservative bare-symbol behaviour that protects AAPL.
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
        elif (exchange or "").upper() in {"NSE", "NSE/BSE"}:
            base, suffix, canonical = upper, ".NS", f"{upper}.NS"
        elif (exchange or "").upper() == "BSE":
            base, suffix, canonical = upper, ".BO", f"{upper}.BO"
        elif upper in _indian_universe():
            base, suffix, canonical = upper, ".NS", f"{upper}.NS"
        else:
            # A bare symbol without exchange metadata and outside the bundled
            # seed universe is a US listing. This preserves the AAPL fix.
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
