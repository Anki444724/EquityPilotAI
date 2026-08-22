"""Yahoo Finance — fallback market-data provider.

Wraps the platform's existing `app.data.yahoo_source` rather than adding
`yfinance`. That client's throttling, backoff and circuit breaker are already
tuned to what Yahoo tolerates from one IP, learned the hard way during the
rc2 ingestion sprint; a second HTTP client would have to relearn all of it,
and the two would then compete for the same per-IP budget.

Yahoo serves a quote and a price history. It has no news or earnings-calendar
endpoint in the form used here, and those are reported as unavailable rather
than silently omitted.
"""
from __future__ import annotations

from typing import Any

import structlog

from app.data.providers.base import (
    BaseMarketProvider, CompanyProfile, MarketSnapshot, ProviderError,
    Quote, RetryPolicy, normalise_symbol, to_float,
)

log = structlog.get_logger(__name__)


class YahooProvider(BaseMarketProvider):
    name = "Yahoo Finance (Fallback)"
    priority = 20
    #: Not a live Indian quote source. The router must not call ``fetch()``
    #: for a live price: ``yahoo_source._fetch_quote`` waits up to 20–30s
    #: and is what made RELIANCE take ~22s after NSE returned 403.
    live_quote = False

    def __init__(self, policy: RetryPolicy | None = None) -> None:
        # Yahoo rate-limits a single IP aggressively; the underlying client
        # widens its own interval on 429, so this one stays out of the way.
        super().__init__(policy or RetryPolicy(
            attempts=2, backoff_base=2.0, timeout_seconds=20.0,
            min_interval=1.0, circuit_threshold=6,
        ))

    def configured(self) -> bool:
        return True          # no credentials required

    @staticmethod
    def to_symbol(ticker: str) -> str:
        return normalise_symbol(ticker)

    def fetch_quote(self, ticker: str) -> Quote | None:
        """Yahoo is not used for live Indian quotes. History still works."""
        return None

    def fetch_history(self, ticker: str, days: int = 365) -> list[dict[str, Any]] | None:
        """Daily bars via the existing yahoo_source client — not a live quote."""
        from app.data import yahoo_source

        symbol = self.to_symbol(ticker)
        try:
            history = yahoo_source.fetch_price_history(
                symbol.split(".")[0], days=days,
            )
        except Exception:  # noqa: BLE001 - history is best-effort
            return None
        rows = []
        for row in list(history or []):
            close = to_float(
                getattr(row, "close", None)
                if not isinstance(row, dict) else row.get("close")
            )
            rows.append({
                "date": getattr(row, "date", None)
                if not isinstance(row, dict) else row.get("date"),
                "close": close,
            })
        return rows or None

    def fetch(self, ticker: str, **kwargs) -> tuple[MarketSnapshot, dict[str, Any]]:
        from app.data import yahoo_source

        symbol = self.to_symbol(ticker)
        snapshot = MarketSnapshot(ticker=ticker.upper(), source=self.name)
        raw: dict[str, Any] = {}

        try:
            holder = yahoo_source.CompanyFinancials(ticker=symbol.split(".")[0])
            price, market_cap, shares = yahoo_source._fetch_quote(  # noqa: SLF001
                symbol, holder,
            )
            raw["quote"] = {
                "symbol": symbol, "price": price,
                "market_cap": market_cap, "shares": shares,
            }
            snapshot.quote = Quote(price=to_float(price, zero_is_absent=True))
            snapshot.profile = CompanyProfile(
                name=getattr(holder, "long_name", None),
                exchange="NSE" if symbol.endswith(".NS") else None,
                currency="INR" if symbol.endswith(".NS") else None,
                market_cap=to_float(market_cap, zero_is_absent=True),
                shares_outstanding=to_float(shares, zero_is_absent=True),
            )
            if not snapshot.has_quote:
                snapshot.unavailable.append("quote: Yahoo returned no price")
        except Exception as exc:  # noqa: BLE001
            raw["quote"] = {"_error": str(exc)[:160]}
            snapshot.unavailable.append(f"quote: {str(exc)[:80]}")

        if kwargs.get("include_history", True):
            try:
                history = yahoo_source.fetch_price_history(
                    symbol.split(".")[0], days=kwargs.get("history_days", 90),
                )
                rows = list(history or [])
                raw["price_history"] = {"rows": len(rows)}
                snapshot.price_history = [
                    {
                        "date": getattr(row, "date", None) or row.get("date"),
                        "close": to_float(
                            getattr(row, "close", None)
                            if not isinstance(row, dict) else row.get("close")
                        ),
                    }
                    for row in rows[:120]
                ]
                if not snapshot.price_history:
                    snapshot.unavailable.append("historical price data")
            except Exception as exc:  # noqa: BLE001
                raw["price_history"] = {"_error": str(exc)[:160]}
                snapshot.unavailable.append(f"historical price data: {str(exc)[:60]}")

        # Endpoints Yahoo does not serve through this client. Named so a
        # caller can tell "not offered" from "tried and empty".
        snapshot.unavailable.extend([
            "key metrics", "financial ratios", "income statement",
            "balance sheet", "cash flow", "company news", "earnings",
        ])

        if not snapshot.has_quote:
            raise ProviderError(f"{self.name}: no usable quote for {symbol}")

        return snapshot, raw
