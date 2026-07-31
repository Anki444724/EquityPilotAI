"""Finnhub — primary market-data provider.

Restored as tier 1. Covers profile, quote, key metrics (via `stock/metric`),
company news and earnings; it does not serve the three statements in a form
this platform uses, so those fall through to the next tier — which is the
point of the chain: a provider that cannot answer *this* endpoint should not
block one that can.

Authenticates with an `X-Finnhub-Token` header rather than a query parameter,
so the key never enters a URL, an access log or an error message.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import structlog

from app.data.providers.currency import to_crore
from app.data.providers.base import (
    BaseMarketProvider, CompanyProfile, MarketSnapshot, ProviderAuthError,
    ProviderError, ProviderNotConfigured, Quote, RetryPolicy, SymbolNotFound,
    normalise_symbol, to_float,
)

log = structlog.get_logger(__name__)

_BASE = "https://finnhub.io/api/v1"

#: Finnhub reports market capitalisation in millions of the listing currency.
#: One crore is ten million, so a million is 0.1 crore.
_MILLIONS_TO_CRORE = 0.1


class FinnhubProvider(BaseMarketProvider):
    name = "Finnhub"
    priority = 10

    #: Endpoints this provider genuinely serves. The router consults this so
    #: an unsupported endpoint is a fall-through rather than a failed call —
    #: spending a request to be told "no" wastes the free-tier budget.
    supports = frozenset({
        "profile", "quote", "key_metrics", "news", "earnings",
    })

    def __init__(self, policy: RetryPolicy | None = None) -> None:
        # Free tier: 60 calls/minute.
        super().__init__(policy or RetryPolicy(
            attempts=3, backoff_base=1.6, timeout_seconds=15.0,
            min_interval=1.05, circuit_threshold=8,
        ))

    def configured(self) -> bool:
        from app.core.config import settings

        return bool((settings.FINNHUB_API_KEY or "").strip())

    def _key(self) -> str:
        from app.core.config import settings

        key = (settings.FINNHUB_API_KEY or "").strip()
        if not key:
            raise ProviderNotConfigured("FINNHUB_API_KEY is not set.")
        return key

    def _call(self, path: str, **params: Any) -> Any:
        import urllib.parse

        key = self._key()
        query = urllib.parse.urlencode(
            {k: v for k, v in params.items() if v is not None}
        )
        return self._get_json(
            f"{_BASE}{path}?{query}",
            headers={"X-Finnhub-Token": key},
            redact=key,
        )

    @staticmethod
    def to_symbol(ticker: str) -> str:
        return normalise_symbol(ticker)

    # -- endpoints --------------------------------------------------------
    def company_profile(self, ticker: str) -> dict:
        return self._call("/stock/profile2", symbol=self.to_symbol(ticker))

    def quote(self, ticker: str) -> dict:
        return self._call("/quote", symbol=self.to_symbol(ticker))

    def key_metrics(self, ticker: str) -> dict:
        return self._call("/stock/metric", symbol=self.to_symbol(ticker), metric="all")

    def news(self, ticker: str, *, days: int = 30) -> list:
        today = date.today()
        payload = self._call(
            "/company-news", symbol=self.to_symbol(ticker),
            **{"from": (today - timedelta(days=days)).isoformat(),
               "to": today.isoformat()},
        )
        return payload if isinstance(payload, list) else []

    def earnings(self, ticker: str) -> dict:
        today = date.today()
        return self._call(
            "/calendar/earnings", symbol=self.to_symbol(ticker),
            **{"from": today.isoformat(),
               "to": (today + timedelta(days=180)).isoformat()},
        )

    # -- parsing ----------------------------------------------------------
    @staticmethod
    def parse(ticker: str, raw: dict[str, Any]) -> MarketSnapshot:
        snapshot = MarketSnapshot(ticker=ticker.upper(), source=FinnhubProvider.name)

        def usable(key: str) -> Any:
            value = raw.get(key)
            if isinstance(value, dict) and "_error" in value:
                return None
            return value or None

        profile = usable("profile")
        if profile:
            # Finnhub reports both in millions of the listing currency.
            # Scaled to absolute units and left in that currency: the caller
            # formats it, because only the currency knows whether "crore" or
            # "billion" is the right word.
            cap = to_float(profile.get("marketCapitalization"), zero_is_absent=True)
            shares = to_float(profile.get("shareOutstanding"), zero_is_absent=True)
            currency = (profile.get("currency") or "").upper() or None
            absolute_cap = cap * 1e6 if cap else None
            snapshot.profile = CompanyProfile(
                name=profile.get("name") or None,
                exchange=profile.get("exchange") or None,
                currency=currency,
                industry=profile.get("finnhubIndustry") or None,
                website=profile.get("weburl") or None,
                market_cap=absolute_cap,
                market_cap_crore=to_crore(absolute_cap, currency or ""),
                shares_outstanding=shares * 1e6 if shares else None,
            )
        else:
            snapshot.unavailable.append("company profile")

        quote = usable("quote")
        if quote:
            # Finnhub returns 0, not null, for a symbol it does not cover.
            snapshot.quote = Quote(
                price=to_float(quote.get("c"), zero_is_absent=True),
                change=to_float(quote.get("d")),
                percent_change=to_float(quote.get("dp")),
                day_high=to_float(quote.get("h"), zero_is_absent=True),
                day_low=to_float(quote.get("l"), zero_is_absent=True),
                day_open=to_float(quote.get("o"), zero_is_absent=True),
                previous_close=to_float(quote.get("pc"), zero_is_absent=True),
            )
        else:
            snapshot.unavailable.append("quote")

        metrics = usable("key_metrics")
        series = (metrics or {}).get("metric") or {}
        if series:
            snapshot.key_metrics = {
                "pe_ratio": to_float(series.get("peTTM"), zero_is_absent=True),
                "eps": to_float(series.get("epsTTM"), zero_is_absent=True),
                "week52_high": to_float(series.get("52WeekHigh"), zero_is_absent=True),
                "week52_low": to_float(series.get("52WeekLow"), zero_is_absent=True),
                "roe": to_float(series.get("roeTTM")),
                "net_margin": to_float(series.get("netProfitMarginTTM")),
            }
        else:
            snapshot.unavailable.append("key metrics")

        news = usable("news")
        if news:
            snapshot.news = [
                {
                    "headline": item.get("headline"),
                    "source": item.get("source"),
                    "url": item.get("url"),
                    "published": item.get("datetime"),
                    "summary": (item.get("summary") or "")[:280],
                }
                for item in news[:10]
            ]
        else:
            snapshot.unavailable.append("company news")

        rows = (usable("earnings") or {}).get("earningsCalendar") or []
        if rows:
            snapshot.earnings = [
                {
                    "date": row.get("date"),
                    "period": row.get("period"),
                    "eps_estimated": to_float(row.get("epsEstimate")),
                    "revenue_estimated": to_float(row.get("revenueEstimate")),
                }
                for row in rows[:6]
            ]
        else:
            snapshot.unavailable.append("earnings")

        # Never served by this provider — named so the router can tell
        # "not offered" from "tried and empty".
        snapshot.unavailable.extend([
            "financial ratios", "income statement", "balance sheet",
            "cash flow", "historical price data",
        ])
        return snapshot

    def fetch(self, ticker: str, **kwargs) -> tuple[MarketSnapshot, dict[str, Any]]:
        raw: dict[str, Any] = {}

        def attempt(key: str, fn) -> None:
            try:
                raw[key] = fn()
            except ProviderAuthError:
                # Fatal for the provider: a rejected key is rejected by every
                # endpoint, and retrying the other four wastes the budget and
                # delays the fallback that would have worked.
                raise
            except ProviderError as exc:
                raw[key] = {"_error": str(exc)[:160]}
                log.warning("finnhub endpoint unavailable", endpoint=key,
                            ticker=ticker, reason=str(exc)[:120])

        attempt("profile", lambda: self.company_profile(ticker))
        attempt("quote", lambda: self.quote(ticker))
        attempt("key_metrics", lambda: self.key_metrics(ticker))
        if kwargs.get("include_news", True):
            attempt("news", lambda: self.news(ticker))
        if kwargs.get("include_earnings", True):
            attempt("earnings", lambda: self.earnings(ticker))

        return self.parse(ticker, raw), raw
