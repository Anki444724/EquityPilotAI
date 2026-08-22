"""NSE India — primary live market-data provider for Indian listings.

EquityPilotAI is India-only. Finnhub serves US symbols perfectly but answers a
plan restriction for `RELIANCE.NS`; FMP and Yahoo have both been observed
returning HTTP 429 or "no usable quote" for Indian symbols. This provider is
the platform's own reliable live source for NSE/BSE: it reads the same public
quote endpoint the National Stock Exchange's website uses, so it needs no API
key and its coverage is exactly the Indian universe this platform serves.

The NSE endpoint requires a session cookie obtained by first requesting the
home page — a bare API call returns an anti-bot page — so the request is sent
through a cookie-jar opener that primes the session once and reuses it, in
exactly the same way ``NSEFilingProvider`` does for corporate filings.

Like every other provider here it is deliberately best-effort: an NSE outage
raises a ``ProviderError`` and the router falls through to the next tier
rather than failing the request. What it never does is return an empty or
zero price as if it were a real quote.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from typing import Any

import structlog

from app.data.providers.base import (
    BaseMarketProvider, CompanyProfile, MarketSnapshot, ProviderError,
    ProviderRateLimited, Quote, RetryPolicy, SymbolNotFound, to_float,
)
from app.data.providers.currency import to_crore

log = structlog.get_logger(__name__)

_BASE = "https://www.nseindia.com/api/quote-equity"
_HOME = "https://www.nseindia.com"

_BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

#: NSE reports market capitalisation in absolute units of the listing currency
#: (INR). The internal pipeline is denominated in crore, so the convenience
#: field is derived the same way every other provider does it.
_CRORE = 1e7

#: How long to skip NSE after Akamai/WAF returns HTTP 403. The block is
#: origin-wide (not per-symbol), so retrying RELIANCE then TCS then INFY
#: would each burn a 15s timeout. Five minutes is long enough that a page
#: refresh does not re-wait, and short enough that a transient WAF blip
#: recovers without an operator restart.
_BLOCKED_COOLDOWN_SECONDS = 300.0


class NSEIndiaProvider(BaseMarketProvider):
    """Live NSE/BSE quotes from the exchange's own public endpoint."""

    name = "NSE India (Live)"
    priority = 5

    #: The quote endpoint carries the profile and quote; the platform's own
    #: stored financials cover the rest, so this provider does not try to
    #: duplicate the statements it does not serve.
    supports = frozenset({"profile", "quote"})

    def __init__(self, policy: RetryPolicy | None = None) -> None:
        # NSE throttles aggressively and expires sessions fast; the underlying
        # opener is given a couple of attempts with modest backoff and the
        # router's own cache does the real work of protecting the budget.
        # A 403 (Akamai/WAF block from typical cloud IPs) is *not* retried:
        # it is treated as provider-unavailable and opens a cooldown so the
        # next request fails closed in milliseconds instead of ~20s.
        super().__init__(policy or RetryPolicy(
            attempts=2, backoff_base=1.5, timeout_seconds=8.0,
            min_interval=0.6, circuit_threshold=6,
        ))
        self._opener: urllib.request.OpenerDirector | None = None
        self.blocked_cooldown_seconds = _BLOCKED_COOLDOWN_SECONDS

    def configured(self) -> bool:
        # No credentials required — this is the exchange's own public endpoint —
        # but an operator can disable it entirely in a network-restricted
        # deployment via NSE_MARKET_ENABLED=false.
        from app.core.config import settings

        return settings.NSE_MARKET_ENABLED

    @staticmethod
    def to_symbol(ticker: str) -> str:
        """The bare symbol NSE keys on — no `.NS`/`.BO` suffix."""
        symbol = (ticker or "").strip().upper()
        for suffix in (".NS", ".BO"):
            if symbol.endswith(suffix):
                return symbol[: -len(suffix)]
        return symbol

    # -- transport -------------------------------------------------------
    def _raise_if_blocked(self) -> None:
        """Fail immediately when a previous 403 opened the cooldown."""
        if not self.available:
            raise ProviderRateLimited(
                f"{self.name}: circuit open (provider unavailable)"
            )

    def _mark_blocked(self, status: int) -> None:
        """Treat an origin-level block as provider-unavailable.

        HTTP 403 from NSE/Akamai is not a per-symbol miss and is not worth
        retrying: the next request to the same origin will get the same
        answer, typically after another 8–15s timeout. Opening the circuit
        lets the router fall through (and skip NSE on the next call).
        """
        self._opener = None
        self.mark_unavailable(self.blocked_cooldown_seconds)
        log.warning("nse provider blocked", status=status,
                    cooldown_s=self.blocked_cooldown_seconds)

    def _session(self) -> urllib.request.OpenerDirector:
        """An opener holding NSE's session cookies, primed against the home page.

        Mirrors ``NSEFilingProvider._session``: the quote API refuses a
        request that has not first visited the site, so the home page is
        fetched once to seed the jar and reused across calls.
        """
        if self._opener is not None:
            return self._opener
        self._raise_if_blocked()
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar())
        )
        opener.addheaders = [
            ("User-Agent", _BROWSER_UA),
            ("Accept", "application/json, text/plain, */*"),
            ("Accept-Language", "en-GB,en;q=0.9"),
            ("Referer", f"{_HOME}/market-data/live-equity-market"),
        ]
        try:
            opener.open(_HOME, timeout=self.policy.timeout_seconds).read(1024)
        except urllib.error.HTTPError as exc:
            if exc.code == 403:
                self._mark_blocked(exc.code)
                raise ProviderError(
                    f"{self.name}: HTTP 403 (provider unavailable)"
                ) from exc
            log.debug("nse quote session priming failed", error=str(exc)[:120])
        except Exception as exc:  # noqa: BLE001 - the call may still succeed
            log.debug("nse quote session priming failed", error=str(exc)[:120])
        self._opener = opener
        return opener

    def _fetch_payload(self, symbol: str) -> dict[str, Any]:
        """One quote payload, raising provider exceptions on failure."""
        self._raise_if_blocked()
        url = f"{_BASE}?symbol={urllib.parse.quote(symbol)}"
        last: Exception | None = None
        for attempt in range(self.policy.attempts):
            self._throttle()
            try:
                with self._session().open(url, timeout=self.policy.timeout_seconds) as response:
                    payload = json.load(response)
                self._consecutive_rate_limits = 0
                return payload
            except urllib.error.HTTPError as exc:
                last = exc
                if exc.code == 403:
                    # Origin-level WAF/Akamai block. Do not retry, do not
                    # wait for the timeout budget: trip the cooldown and
                    # let the router try the next configured provider.
                    self._mark_blocked(exc.code)
                    raise ProviderError(
                        f"{self.name}: HTTP 403 (provider unavailable)"
                    ) from exc
                if exc.code == 404:
                    raise SymbolNotFound(
                        f"{self.name}: no NSE/BSE listing for {symbol}"
                    ) from exc
                if exc.code == 429:
                    self._consecutive_rate_limits += 1
                    time_delay = self.policy.delay(attempt)
                    log.info("nse quote rate limited", symbol=symbol,
                             attempt=attempt, delay=round(time_delay, 2))
                    time.sleep(time_delay)
                    continue
                # Other 4xx/5xx: fall through so the next provider gets a
                # turn rather than burning the whole chain.
                raise ProviderError(
                    f"{self.name}: HTTP {exc.code} for {symbol}"
                ) from exc
            except (TimeoutError, urllib.error.URLError, OSError) as exc:
                last = exc
                log.info("nse quote transient failure", symbol=symbol,
                         attempt=attempt, error=str(exc)[:100])
                time.sleep(self.policy.delay(attempt))
            except json.JSONDecodeError as exc:
                raise ProviderError(
                    f"{self.name}: malformed JSON for {symbol}"
                ) from exc

        raise ProviderError(
            f"{self.name}: failed after {self.policy.attempts} attempts: "
            f"{str(last)[:160]}"
        )

    # -- parsing ----------------------------------------------------------
    @staticmethod
    def parse(symbol: str, raw: dict[str, Any]) -> MarketSnapshot:
        """Normalise NSE's quote payload. Pure, so it is testable without a key.

        NSE's ``/api/quote-equity`` shape::

            {
              "symbol": "RELIANCE", "companyName": "Reliance Industries Limited",
              "marketCap": 17697782146242,
              "industry": "Refining & Petrochemicals",
              "priceInfo": {"lastPrice": 1307.8, "change": 14.9, "pctChange": 1.15,
                            "open": 1288.0, "dayHigh": 1315.0, "dayLow": 1280.0,
                            "previousClose": 1292.9, "totalTradedVolume": 8622452},
              ...
            }
        """
        snapshot = MarketSnapshot(ticker=symbol.upper(), source=NSEIndiaProvider.name)
        profile = raw.get("companyInfo") or {}
        if isinstance(profile, dict) and profile.get("companyName"):
            snapshot.profile = CompanyProfile(
                name=profile.get("companyName") or None,
                industry=(raw.get("industry") or profile.get("industry") or None),
                currency="INR",
                exchange="NSE",
                market_cap=to_float(raw.get("marketCap"), zero_is_absent=True),
                market_cap_crore=to_crore(
                    to_float(raw.get("marketCap"), zero_is_absent=True), "INR"
                ),
            )
        elif raw.get("companyName"):
            snapshot.profile = CompanyProfile(
                name=raw.get("companyName") or None,
                industry=raw.get("industry") or None,
                currency="INR",
                exchange="NSE",
                market_cap=to_float(raw.get("marketCap"), zero_is_absent=True),
                market_cap_crore=to_crore(
                    to_float(raw.get("marketCap"), zero_is_absent=True), "INR"
                ),
            )
        else:
            snapshot.unavailable.append("company profile")

        price_info = raw.get("priceInfo")
        if isinstance(price_info, dict):
            snapshot.quote = Quote(
                price=to_float(price_info.get("lastPrice"), zero_is_absent=True),
                change=to_float(price_info.get("change")),
                percent_change=to_float(price_info.get("pctChange")),
                day_open=to_float(price_info.get("open"), zero_is_absent=True),
                day_high=to_float(price_info.get("dayHigh"), zero_is_absent=True),
                day_low=to_float(price_info.get("dayLow"), zero_is_absent=True),
                previous_close=to_float(price_info.get("previousClose"),
                                        zero_is_absent=True),
                volume=to_float(price_info.get("totalTradedVolume"),
                                zero_is_absent=True),
            )
        else:
            snapshot.unavailable.append("quote")

        # Endpoints this provider does not serve; named so a caller can tell
        # "not offered" from "tried and empty".
        snapshot.unavailable.extend([
            "key metrics", "financial ratios", "income statement",
            "balance sheet", "cash flow", "company news", "earnings",
            "historical price data",
        ])
        return snapshot

    @staticmethod
    def _raise_if_error(payload: Any, symbol: str) -> None:
        """NSE answers 200 with an error object for a symbol it does not know,
        rather than an HTTP 404. Recognise and classify it."""
        if isinstance(payload, dict) and payload.get("error"):
            raise SymbolNotFound(
                f"{NSEIndiaProvider.name}: no NSE/BSE listing for {symbol}"
            )

    def fetch(self, ticker: str, **kwargs) -> tuple[MarketSnapshot, dict[str, Any]]:
        symbol = self.to_symbol(ticker)
        payload = self._fetch_payload(symbol)
        self._raise_if_error(payload, symbol)

        snapshot = self.parse(symbol, payload)
        if not snapshot.has_quote:
            raise ProviderError(
                f"{self.name}: no usable quote for {symbol}"
            )
        return snapshot, {"quote": payload}

    def fetch_quote(self, ticker: str) -> Quote:
        """Current quote only — the live-price path uses this, not get_quote()."""
        snapshot, _ = self.fetch(ticker)
        return snapshot.quote
