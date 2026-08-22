"""Deterministic mock market provider — Phase 1.

`DATA_PROVIDER=mock` swaps the entire external tier for this provider. It
makes three guarantees the real tiers cannot:

1. **Deterministic.** Every figure is a pure function of (symbol[, date]).
   The same symbol always produces the same quote, and the same (symbol,
   date) always produces the same bar — across processes, restarts and
   machines — because the "randomness" is a sha256-derived PRNG seeded by the
   symbol, and each bar is derived from the day index rather than from a
   walk that depends on when the run happened. Re-running a sync therefore
   reproduces byte-identical data, which is what makes idempotency testable.
2. **Offline.** No network, no credentials, no filesystem. The 5,000-company
   pipeline runs on a laptop or in CI.
3. **Unmistakably synthetic.** Provider name is "Mock (synthetic)", row
   provenance is `provider='mock'`, and the mock universe uses ISINs with the
   reserved-ish prefix `INM` and tickers with the reserved prefix `MCK`, so a
   mock row cannot be confused with, or collide with, a real security. The
   mock chain and the real chain are mutually exclusive (see
   `app/data/providers/router.py`) — mock data can never mix into a real
   record, because no real provider is consulted while it is selected.

Prices are realistic in *shape* (₹ tens…₹ thousands, ~1-3% daily moves,
weekends closed) and false in *fact*. That is the point: the pipeline is real,
the numbers are not, and every response says so.
"""
from __future__ import annotations

import hashlib
import math
from datetime import date, datetime, timedelta, timezone
from typing import Any

from app.data.providers.base import (
    BaseMarketProvider, CompanyProfile, MarketSnapshot, ProviderMetadata, Quote,
)

_EPOCH = date(2015, 1, 1)  # stable day-index origin; never changes


def _seed(*parts: str) -> int:
    """A stable 64-bit seed from the identity of what is being generated."""
    digest = hashlib.sha256("|".join(parts).encode()).digest()
    return int.from_bytes(digest[:8], "big")


def _unit(seed: int) -> float:
    """Deterministic float in [0, 1)."""
    return (seed & 0xFFFFFFFF) / 0x100000000


def _trading_day(day: date) -> bool:
    """NSE/BSE cash market is shut Saturday and Sunday."""
    return day.weekday() < 5


def _base_price(symbol: str) -> float:
    """Anchor price in ₹: 20 … ~2,020, log-distributed for realism."""
    unit = _unit(_seed("base", symbol))
    return round(20.0 * (100.0 ** unit), 2)


def _daily_factor(symbol: str, day: date) -> float:
    """The bar-to-bar move for (symbol, day), roughly ±3%.

    Two symbol-phased sine waves give the series visible trend and mean
    reversion; a hashed per-day jitter keeps consecutive bars from feeling
    looped. Pure function of (symbol, day) — no walk, no run-time state.
    """
    index = (day - _EPOCH).days
    u = _unit(_seed("move", symbol, str(index)))
    wave = 0.018 * math.sin(
        2 * math.pi * index / (37 + 40 * _unit(_seed("p1", symbol)))
        + 6.283 * _unit(_seed("ph1", symbol)),
    )
    wave += 0.011 * math.sin(
        2 * math.pi * index / (127 + 200 * _unit(_seed("p2", symbol)))
        + 6.283 * _unit(_seed("ph2", symbol)),
    )
    jitter = (u - 0.5) * 0.03
    return wave + jitter


def mock_close(symbol: str, day: date) -> float:
    """Deterministic close for (symbol, day) — O(1), no cumulative walk.

    The anchor price drifts by a gentle long-period exponential trend so the
    5-year history looks like a stock rather than noise around a flat line,
    and each day applies its own factor. Both terms are pure functions of the
    day index, so any process computing the same (symbol, day) agrees.
    """
    index = (day - _EPOCH).days
    drift = 0.06 / 365.0 * index            # ~6%/yr upward bias
    seasonal = _daily_factor(symbol, day)
    value = _base_price(symbol) * math.exp(drift + seasonal)
    return round(value, 2)


def mock_bar(symbol: str, day: date) -> dict[str, Any]:
    """One deterministic OHLCV bar for (symbol, day)."""
    close = mock_close(symbol, day)
    seed = _seed("bar", symbol, str((day - _EPOCH).days))
    u1, u2, u3 = _unit(seed), _unit(seed ^ 0x9E3779B97F4A7C15), _unit(seed ^ 0xC2B2AE3D27D4EB4F)
    spread = 0.004 + 0.012 * u1
    day_open = round(close * (1 - spread * u2), 2)
    day_high = round(max(close, day_open) * (1 + spread * 0.6), 2)
    day_low = round(min(close, day_open) * (1 - spread * 0.6), 2)
    volume = float(10_000 + int(990_000 * u3))
    return {
        "date": day.isoformat(),
        "open": day_open,
        "high": day_high,
        "low": day_low,
        "close": close,
        "volume": volume,
    }


def mock_quote(symbol: str) -> Quote:
    """Deterministic current quote; 'today' is the last trading day."""
    today = datetime.now(timezone.utc).date()
    day = today
    while not _trading_day(day):
        day -= timedelta(days=1)
    prev = day - timedelta(days=1)
    while not _trading_day(prev):
        prev -= timedelta(days=1)

    close = mock_close(symbol, day)
    prev_close = mock_close(symbol, prev)
    bar = mock_bar(symbol, day)
    change = round(close - prev_close, 2)
    pct = round((close / prev_close - 1) * 100, 2) if prev_close else None

    # 52-week window, deterministic scan of the same pure function.
    highs, lows = [], []
    scan = day
    for _ in range(365):
        if _trading_day(scan):
            c = mock_close(symbol, scan)
            highs.append(c)
            lows.append(c)
        scan -= timedelta(days=1)

    status = "weekend" if today.weekday() >= 5 else (
        "open" if _market_open() else "closed"
    )
    return Quote(
        price=close,
        previous_close=prev_close,
        day_open=bar["open"],
        day_high=bar["high"],
        day_low=bar["low"],
        volume=bar["volume"],
        change=change,
        percent_change=pct,
        week_52_high=max(highs) if highs else None,
        week_52_low=min(lows) if lows else None,
        market_status=status,
    )


def _market_open(now_utc: datetime | None = None) -> bool:
    """IST cash-market hours 09:15–15:30, Mon–Fri (mirrors live_market)."""
    now = now_utc or datetime.now(timezone.utc)
    ist = datetime.fromtimestamp(now.timestamp() + 5 * 3600 + 30 * 60, timezone.utc)
    if ist.weekday() >= 5:
        return False
    seconds = ist.hour * 3600 + ist.minute * 60 + ist.second
    return (9 * 3600 + 15 * 60) <= seconds <= (15 * 3600 + 30 * 60)


def mock_history(symbol: str, days: int) -> list[dict[str, Any]]:
    """Deterministic daily bars, newest last, weekends excluded."""
    today = datetime.now(timezone.utc).date()
    bars: list[dict[str, Any]] = []
    scan = today
    while len(bars) < days:
        if _trading_day(scan):
            bars.append(mock_bar(symbol, scan))
        scan -= timedelta(days=1)
    bars.reverse()
    return bars


class MockMarketProvider(BaseMarketProvider):
    """The `DATA_PROVIDER=mock` tier. Deterministic, offline, labelled."""

    name = "Mock (synthetic)"
    priority = 5  # the only external-tier provider when selected

    def __init__(self, policy=None) -> None:  # type: ignore[no-untyped-def]
        from app.data.providers.base import RetryPolicy as _Policy

        # Nothing here can fail — no network, no credentials — so no retry,
        # no throttle floor and a tiny timeout keep accidental loops cheap.
        super().__init__(policy or _Policy(
            attempts=1, timeout_seconds=1.0, min_interval=0.0,
        ))

    def configured(self) -> bool:
        return True  # no credentials by construction

    # -- narrow Phase-1 fetches --------------------------------------------
    def fetch_quote(self, ticker: str) -> Quote:
        return mock_quote(self._clean(ticker))

    def fetch_history(self, ticker: str, days: int = 365) -> list[dict[str, Any]]:
        return mock_history(self._clean(ticker), days)

    # -- the full snapshot, same shape as every other provider -------------
    def fetch(self, ticker: str, **kwargs) -> tuple[MarketSnapshot, dict[str, Any]]:
        symbol = self._clean(ticker)
        quote = mock_quote(symbol)
        snapshot = MarketSnapshot(
            ticker=symbol.upper(),
            source=self.name,
            meta=ProviderMetadata(
                provider=self.name, currency="INR", exchange="NSE",
                market="India", confidence=1.0,
                last_updated=datetime.now(timezone.utc).isoformat(),
            ),
            profile=CompanyProfile(
                name=f"{symbol} Synthetic Ltd",
                exchange="NSE", currency="INR",
                sector=self._sector(symbol), industry=None,
                description="Synthetic instrument generated by the mock "
                            "provider for pipeline testing. Not a real company.",
                market_cap=quote.price * self._shares(symbol),
            ),
            quote=quote,
        )
        if kwargs.get("include_history", True):
            days = int(kwargs.get("history_days", 90))
            snapshot.price_history = mock_history(symbol, days)
        # News/earnings/statements are reported unavailable rather than faked:
        # the mock provider exercises the market pipeline, not the AI stack.
        snapshot.unavailable.extend([
            "key metrics", "financial ratios", "income statement",
            "balance sheet", "cash flow", "company news", "earnings",
            "reason: mock provider serves market data only",
        ])
        raw = {"mock": {"symbol": symbol, "deterministic": True}}
        return snapshot, raw

    # -- helpers ------------------------------------------------------------
    @staticmethod
    def _clean(ticker: str) -> str:
        symbol = (ticker or "").strip().upper()
        if "." in symbol:  # MCK123.NS → MCK123
            symbol = symbol.split(".")[0]
        return symbol

    @staticmethod
    def _shares(symbol: str) -> float:
        return float(1_000_000 + (_seed("shares", symbol) % 900_000_000))

    @staticmethod
    def _sector(symbol: str) -> str:
        sectors = (
            "Financial Services", "Information Technology", "Healthcare",
            "Fast Moving Consumer Goods", "Automobile and Auto Components",
            "Capital Goods", "Construction", "Energy", "Metals and Mining",
            "Consumer Durables", "Telecommunication", "Textiles",
            "Chemicals", "Realty",
        )
        return sectors[_seed("sector", symbol) % len(sectors)]
