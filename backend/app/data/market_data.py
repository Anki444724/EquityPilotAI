"""Market-data routing: Finnhub first, Yahoo Finance as the fallback.

One entry point, `fetch_market_data`, which tries providers in order and
reports which one answered. The source is returned with *every* response, not
only when the fallback fires: a figure whose provenance is only stated on the
unhappy path is a figure whose provenance cannot be relied on.

Falling back is deliberately narrow. A provider is abandoned when it is
unreachable, unauthenticated, rate-limited or returns no usable quote — not
when it merely omits a field. Partial data from the primary is better than a
complete answer from a second source silently substituted, which is the
failure this platform already fixed once in the AI layer.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any

import structlog

from app.data import finnhub_source as finnhub
from app.data.finnhub_source import FinnhubError, FinnhubNotConfigured, MarketSnapshot

log = structlog.get_logger(__name__)

#: Display strings the brief specifies. Rendered verbatim by the API.
SOURCE_FINNHUB = "Finnhub"
SOURCE_YAHOO = "Yahoo Finance (Fallback)"
SOURCE_NONE = "Unavailable"


@dataclass(slots=True)
class MarketDataResult:
    """A snapshot plus an honest account of how it was obtained."""

    snapshot: MarketSnapshot
    source: str
    #: Provider that was tried and failed, if the fallback was used.
    fell_back_from: str | None = None
    reason: str | None = None
    latency_ms: float = 0.0
    #: Raw provider payloads, for the verification harness and for auditing a
    #: parsed figure back to what the provider actually said.
    raw: dict[str, Any] = field(default_factory=dict)

    def as_dict(self, *, include_raw: bool = False) -> dict[str, Any]:
        payload = asdict(self.snapshot)
        payload["source"] = self.source
        payload["source_label"] = f"✓ {self.source}"
        if self.fell_back_from:
            payload["fell_back_from"] = self.fell_back_from
            payload["fallback_reason"] = self.reason
        payload["latency_ms"] = round(self.latency_ms, 1)
        if include_raw:
            payload["raw"] = self.raw
        return payload


def _yahoo_snapshot(ticker: str) -> tuple[MarketSnapshot, dict]:
    """Fallback via the platform's existing Yahoo client.

    Reuses `yahoo_source` rather than adding `yfinance`: the throttling,
    backoff and circuit breaker there are already tuned to what Yahoo tolerates
    from a single IP, and a second HTTP client would have to relearn it.
    """
    from app.data import yahoo_source

    symbol = ticker.upper()
    if "." not in symbol:
        symbol = f"{symbol}.NS"

    data = yahoo_source.CompanyFinancials(ticker=symbol.split(".")[0])
    price, market_cap, shares = yahoo_source._fetch_quote(symbol, data)  # noqa: SLF001

    snapshot = MarketSnapshot(
        ticker=ticker.upper(),
        source=SOURCE_YAHOO,
        name=getattr(data, "long_name", None),
        exchange="NSE" if symbol.endswith(".NS") else None,
        currency="INR" if symbol.endswith(".NS") else None,
        current_price=price,
        market_cap=market_cap,
        shares_outstanding=shares,
    )
    if not snapshot.has_quote:
        snapshot.unavailable.append("quote")
    # Yahoo's timeseries endpoint carries no news or earnings calendar.
    snapshot.unavailable.extend(["company news", "earnings calendar"])
    return snapshot, {"yahoo_quote": {"price": price, "market_cap": market_cap,
                                      "shares": shares, "symbol": symbol}}


def fetch_market_data(
    ticker: str,
    *,
    include_news: bool = True,
    include_earnings: bool = True,
    allow_fallback: bool = True,
) -> MarketDataResult:
    """Fetch market data, preferring Finnhub and falling back to Yahoo."""
    started = time.perf_counter()

    reason: str | None = None
    try:
        if not finnhub.provider_available():
            raise FinnhubError("circuit open after repeated rate limits")

        snapshot, raw = finnhub.fetch_snapshot(
            ticker, include_news=include_news, include_earnings=include_earnings,
        )
        if snapshot.has_quote:
            elapsed = (time.perf_counter() - started) * 1000
            log.info("market data served", ticker=ticker, source=SOURCE_FINNHUB,
                     ms=round(elapsed, 1), missing=len(snapshot.unavailable))
            return MarketDataResult(
                snapshot=snapshot, source=SOURCE_FINNHUB,
                latency_ms=elapsed, raw=raw,
            )
        # A profile with no price is not a usable market snapshot. Common for
        # a symbol Finnhub does not cover on the free tier.
        reason = "Finnhub returned no quote for this symbol"
    except FinnhubNotConfigured as exc:
        reason = str(exc)
    except FinnhubError as exc:
        reason = str(exc)

    log.warning("finnhub unusable", ticker=ticker, reason=(reason or "")[:160])

    if not allow_fallback:
        return MarketDataResult(
            snapshot=MarketSnapshot(ticker=ticker.upper(), source=SOURCE_NONE),
            source=SOURCE_NONE, fell_back_from=SOURCE_FINNHUB, reason=reason,
            latency_ms=(time.perf_counter() - started) * 1000,
        )

    try:
        snapshot, raw = _yahoo_snapshot(ticker)
        elapsed = (time.perf_counter() - started) * 1000
        log.info("market data served", ticker=ticker, source=SOURCE_YAHOO,
                 ms=round(elapsed, 1), fell_back_from=SOURCE_FINNHUB)
        return MarketDataResult(
            snapshot=snapshot, source=SOURCE_YAHOO,
            fell_back_from=SOURCE_FINNHUB, reason=reason,
            latency_ms=elapsed, raw=raw,
        )
    except Exception as exc:  # noqa: BLE001
        elapsed = (time.perf_counter() - started) * 1000
        log.error("all market data providers failed", ticker=ticker,
                  error=str(exc)[:160])
        return MarketDataResult(
            snapshot=MarketSnapshot(ticker=ticker.upper(), source=SOURCE_NONE),
            source=SOURCE_NONE, fell_back_from=SOURCE_FINNHUB,
            reason=f"{reason}; Yahoo also failed: {exc}",
            latency_ms=elapsed,
        )
