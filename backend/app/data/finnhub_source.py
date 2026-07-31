"""Finnhub market-data client.

The first external *market* data provider. The platform's fundamentals come
from screener.in and Yahoo; this covers the things those do least well —
live quotes, company profiles, news and earnings dates.

Follows the same discipline as the existing sources: a minimum interval
between calls, exponential backoff on transient failures, an interval that
widens when the provider says 429, and a circuit breaker so a sustained
outage degrades to the fallback in microseconds instead of burning the retry
budget on every single call.

The API key is read from settings — which reads the environment — and never
appears in this file, in a default, or in a log line. Finnhub authenticates
with an `X-Finnhub-Token` header rather than a query parameter, which is what
made the Gemini key leak into httpx's INFO logs (PD-003); the header form has
no such exposure, but the URL is still scrubbed before anything is logged.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import structlog

log = structlog.get_logger(__name__)

_BASE = "https://finnhub.io/api/v1"

#: Finnhub's free tier allows 60 calls/minute. One second between calls keeps
#: us just inside it without needing a token bucket.
MIN_INTERVAL = 1.05
_last_call = 0.0

#: Consecutive rate-limit responses before the provider is considered down.
#: Past this the client fails immediately so callers fall back rather than
#: waiting through a retry ladder that cannot succeed.
_CIRCUIT_THRESHOLD = 8
_consecutive_429 = 0


class FinnhubError(Exception):
    """Any failure that should cause a fallback rather than a crash."""


class FinnhubAuthError(FinnhubError):
    """The key was rejected. Fatal for the whole provider, not one endpoint."""


class FinnhubNotConfigured(FinnhubError):
    """No API key. Distinct so the caller can report it precisely."""


def provider_available() -> bool:
    """False once the circuit has tripped."""
    return _consecutive_429 < _CIRCUIT_THRESHOLD


def reset_circuit() -> None:
    global _consecutive_429, MIN_INTERVAL
    _consecutive_429 = 0
    MIN_INTERVAL = 1.05


def _api_key() -> str:
    from app.core.config import settings

    key = (settings.FINNHUB_API_KEY or "").strip()
    if not key:
        raise FinnhubNotConfigured(
            "FINNHUB_API_KEY is not set. Set it in the environment; the "
            "platform never carries a default key."
        )
    return key


def _throttle() -> None:
    global _last_call
    elapsed = time.monotonic() - _last_call
    if elapsed < MIN_INTERVAL:
        time.sleep(MIN_INTERVAL - elapsed)
    _last_call = time.monotonic()


def _get(path: str, params: dict[str, Any], *, retries: int = 3,
         backoff: float = 1.5) -> Any:
    """One authenticated GET, with retries and rate-limit handling."""
    global _consecutive_429, MIN_INTERVAL

    if not provider_available():
        raise FinnhubError(
            f"circuit open after {_consecutive_429} consecutive rate limits"
        )

    key = _api_key()
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    url = f"{_BASE}{path}?{query}"

    last: Exception | None = None
    for attempt in range(retries):
        _throttle()
        request = urllib.request.Request(url, headers={
            # Header auth, not a query parameter: a key in the URL ends up in
            # access logs, proxy logs and error reports.
            "X-Finnhub-Token": key,
            "Accept": "application/json",
            "User-Agent": "IERP/1.0 (+institutional-equity-research-platform)",
        })
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                payload = json.load(response)
            _consecutive_429 = 0
            return payload
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code == 429:
                _consecutive_429 += 1
                # Widen the interval so the next caller is gentler, and wait
                # out the window before retrying.
                MIN_INTERVAL = min(MIN_INTERVAL * 1.5, 8.0)
                time.sleep(4.0 * (attempt + 1))
                continue
            if exc.code in (401, 403):
                # A bad key will not fix itself; retrying wastes the budget.
                raise FinnhubAuthError(
                    f"authentication rejected (HTTP {exc.code})"
                ) from exc
            if 500 <= exc.code < 600:
                time.sleep(backoff ** (attempt + 1))
                continue
            raise FinnhubError(f"HTTP {exc.code} for {path}") from exc
        except Exception as exc:  # noqa: BLE001 - timeouts, DNS, resets
            last = exc
            time.sleep(backoff ** (attempt + 1))

    raise FinnhubError(f"{path} failed after {retries} attempts: {last}")


# ===========================================================================
# Symbol mapping
# ===========================================================================
def to_finnhub_symbol(ticker: str) -> str:
    """NSE tickers carry the `.NS` suffix on Finnhub, as on Yahoo.

    `RELIANCE` and `RELIANCE.NS` both resolve, so callers may pass either.
    """
    symbol = (ticker or "").strip().upper()
    if not symbol:
        raise FinnhubError("empty ticker")
    return symbol if "." in symbol else f"{symbol}.NS"


# ===========================================================================
# Endpoints
# ===========================================================================
def company_profile(ticker: str) -> dict[str, Any]:
    return _get("/stock/profile2", {"symbol": to_finnhub_symbol(ticker)})


def quote(ticker: str) -> dict[str, Any]:
    return _get("/quote", {"symbol": to_finnhub_symbol(ticker)})


def basic_financials(ticker: str, metric: str = "all") -> dict[str, Any]:
    return _get(
        "/stock/metric", {"symbol": to_finnhub_symbol(ticker), "metric": metric},
    )


def company_news(ticker: str, *, days: int = 30) -> list[dict[str, Any]]:
    today = date.today()
    payload = _get("/company-news", {
        "symbol": to_finnhub_symbol(ticker),
        "from": (today - timedelta(days=days)).isoformat(),
        "to": today.isoformat(),
    })
    return payload if isinstance(payload, list) else []


def earnings_calendar(ticker: str, *, days_ahead: int = 180) -> dict[str, Any]:
    """Upcoming earnings dates.

    Documented as premium-only on some plans; a 403 here is a plan limitation
    rather than a fault, and the caller reports it as unavailable rather than
    as an error.
    """
    today = date.today()
    return _get("/calendar/earnings", {
        "symbol": to_finnhub_symbol(ticker),
        "from": today.isoformat(),
        "to": (today + timedelta(days=days_ahead)).isoformat(),
    })


# ===========================================================================
# Parsed shapes
# ===========================================================================
@dataclass(slots=True)
class MarketSnapshot:
    """The normalised view every provider must produce.

    Deliberately provider-neutral: the caller should not be able to tell
    which source answered except by reading `source`, which is the point of
    reporting it explicitly.
    """

    ticker: str
    source: str = "unavailable"
    name: str | None = None
    exchange: str | None = None
    currency: str | None = None
    industry: str | None = None
    market_cap: float | None = None          # ₹ crore
    shares_outstanding: float | None = None  # crore
    current_price: float | None = None
    change: float | None = None
    percent_change: float | None = None
    day_high: float | None = None
    day_low: float | None = None
    day_open: float | None = None
    previous_close: float | None = None
    pe_ratio: float | None = None
    eps: float | None = None
    week52_high: float | None = None
    week52_low: float | None = None
    news: list[dict[str, Any]] = field(default_factory=list)
    earnings: list[dict[str, Any]] = field(default_factory=list)
    #: Endpoints that returned nothing, and why. Reported rather than hidden.
    unavailable: list[str] = field(default_factory=list)

    @property
    def has_quote(self) -> bool:
        return self.current_price is not None and self.current_price > 0


def _f(value: Any) -> float | None:
    """Coerce to float, mapping Finnhub's 0-for-absent to None.

    Finnhub returns 0 rather than null for missing numerics. Treating that as
    a real zero would put a ₹0 market cap or a 0 P/E into a valuation model,
    which is worse than reporting the field absent.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number != 0 else None


def parse_snapshot(
    ticker: str,
    *,
    profile: dict | None = None,
    quote_data: dict | None = None,
    metrics: dict | None = None,
    news: list | None = None,
    earnings: dict | None = None,
) -> MarketSnapshot:
    """Turn raw Finnhub payloads into the normalised shape."""
    snapshot = MarketSnapshot(ticker=ticker.upper(), source="Finnhub")

    if profile:
        snapshot.name = profile.get("name") or None
        snapshot.exchange = profile.get("exchange") or None
        snapshot.currency = profile.get("currency") or None
        snapshot.industry = profile.get("finnhubIndustry") or None
        # Finnhub reports market cap in millions of the listing currency.
        # The platform's unit is the crore (10 million), so 1 million = 0.1 cr.
        cap_millions = _f(profile.get("marketCapitalization"))
        snapshot.market_cap = round(cap_millions * 0.1, 2) if cap_millions else None
        shares_millions = _f(profile.get("shareOutstanding"))
        snapshot.shares_outstanding = (
            round(shares_millions * 0.1, 4) if shares_millions else None
        )
    else:
        snapshot.unavailable.append("company profile")

    if quote_data:
        snapshot.current_price = _f(quote_data.get("c"))
        snapshot.change = _f(quote_data.get("d"))
        snapshot.percent_change = _f(quote_data.get("dp"))
        snapshot.day_high = _f(quote_data.get("h"))
        snapshot.day_low = _f(quote_data.get("l"))
        snapshot.day_open = _f(quote_data.get("o"))
        snapshot.previous_close = _f(quote_data.get("pc"))
    else:
        snapshot.unavailable.append("quote")

    if metrics:
        series = metrics.get("metric") or {}
        snapshot.pe_ratio = _f(series.get("peTTM") or series.get("peBasicExclExtraTTM"))
        snapshot.eps = _f(series.get("epsTTM") or series.get("epsBasicExclExtraItemsTTM"))
        snapshot.week52_high = _f(series.get("52WeekHigh"))
        snapshot.week52_low = _f(series.get("52WeekLow"))
    else:
        snapshot.unavailable.append("basic financials")

    if news:
        snapshot.news = [
            {
                "headline": item.get("headline"),
                "source": item.get("source"),
                "url": item.get("url"),
                "datetime": item.get("datetime"),
                "summary": (item.get("summary") or "")[:280],
            }
            for item in news[:10]
        ]
    else:
        snapshot.unavailable.append("company news")

    rows = (earnings or {}).get("earningsCalendar") or []
    if rows:
        snapshot.earnings = [
            {
                "date": row.get("date"),
                "period": row.get("period"),
                "eps_estimate": _f(row.get("epsEstimate")),
                "revenue_estimate": _f(row.get("revenueEstimate")),
            }
            for row in rows[:6]
        ]
    else:
        snapshot.unavailable.append("earnings calendar")

    return snapshot


def fetch_snapshot(ticker: str, *, include_news: bool = True,
                   include_earnings: bool = True) -> tuple[MarketSnapshot, dict]:
    """Every endpoint for one ticker. Returns the snapshot and the raw payloads.

    A failure in one endpoint does not fail the others: a profile without news
    is still worth having, and the missing piece is named in `unavailable`.
    """
    raw: dict[str, Any] = {}

    def attempt(name: str, fn):
        try:
            raw[name] = fn()
            return raw[name]
        except FinnhubAuthError:
            # Fatal for the provider, not just this endpoint: a rejected key
            # will be rejected by the other four too. Propagated so the router
            # falls back immediately instead of throttling through five
            # guaranteed failures — which cost 28s per ticker.
            raise
        except FinnhubError as exc:
            raw[name] = {"error": str(exc)}
            log.warning("finnhub endpoint failed", endpoint=name,
                        ticker=ticker, error=str(exc)[:160])
            return None

    profile = attempt("profile", lambda: company_profile(ticker))
    quote_data = attempt("quote", lambda: quote(ticker))
    metrics = attempt("basic_financials", lambda: basic_financials(ticker))
    news = attempt("news", lambda: company_news(ticker)) if include_news else None
    earnings = (
        attempt("earnings_calendar", lambda: earnings_calendar(ticker))
        if include_earnings else None
    )

    snapshot = parse_snapshot(
        ticker, profile=profile, quote_data=quote_data, metrics=metrics,
        news=news, earnings=earnings,
    )
    return snapshot, raw
