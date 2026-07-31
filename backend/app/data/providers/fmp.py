"""Financial Modeling Prep — primary market-data provider.

Covers every endpoint the brief lists: profile, quote, key metrics, the three
statements, ratios, news, historical prices and earnings.

FMP authenticates with an `apikey` query parameter, which means the key ends
up in the URL. Every log line and error message therefore passes through the
`redact` argument on the shared fetcher, so the key cannot reach a log the way
the Gemini key once did (PD-003).

Free-plan reality, established by testing rather than assumed: several
endpoints are premium-only and answer 403 for a free key. Those are recorded
as unavailable, with the reason, rather than treated as faults — a plan
limitation is not an outage, and reporting it as one would send the router
falling back when the primary is working exactly as licensed.
"""
from __future__ import annotations

from typing import Any

import structlog

from app.data.providers.base import (
    BaseMarketProvider, CompanyProfile, MarketSnapshot, ProviderAuthError,
    ProviderError, ProviderNotConfigured, ProviderRateLimited, Quote,
    RetryPolicy, SymbolNotFound, normalise_symbol, to_float,
)

log = structlog.get_logger(__name__)

_BASE_V3 = "https://financialmodelingprep.com/api/v3"
_BASE_STABLE = "https://financialmodelingprep.com/stable"

#: One crore is ten million. FMP reports absolute currency units, so a market
#: capitalisation arrives as e.g. 17_496_210_000_000 and must be divided.
_CRORE = 1e7


class FMPProvider(BaseMarketProvider):
    name = "Financial Modeling Prep"
    priority = 20

    #: Endpoints the provider serves. On the free plan most are restricted to
    #: US symbols; a non-US symbol returns HTTP 402 and the router falls
    #: through, which is verified behaviour rather than an assumption.
    supports = frozenset({
        "profile", "quote", "key_metrics", "ratios", "income_statement",
        "balance_sheet", "cash_flow", "news", "price_history", "earnings",
    })

    def __init__(self, policy: RetryPolicy | None = None) -> None:
        # FMP's free plan allows 250 calls/day. The interval matters less than
        # the daily budget, so it is modest; the budget is protected by the
        # cache in the router rather than by sleeping here.
        super().__init__(policy or RetryPolicy(
            attempts=3, backoff_base=1.6, timeout_seconds=15.0,
            min_interval=0.3, circuit_threshold=6,
        ))

    # -- credentials ------------------------------------------------------
    def configured(self) -> bool:
        from app.core.config import settings

        return bool((settings.FMP_API_KEY or "").strip())

    def _key(self) -> str:
        from app.core.config import settings

        key = (settings.FMP_API_KEY or "").strip()
        if not key:
            raise ProviderNotConfigured(
                "FMP_API_KEY is not set. It belongs in the environment; the "
                "platform carries no default key."
            )
        return key

    def _call(self, path: str, *, base: str = _BASE_STABLE, **params: Any) -> Any:
        """One call against FMP's `/stable` API.

        The older `/api/v3` paths now answer 403 "Legacy Endpoint ... only
        available for legacy users who have valid subscriptions prior to
        August 31 2025", so every call goes to `/stable`.
        """
        import urllib.parse

        key = self._key()
        query = urllib.parse.urlencode({**params, "apikey": key})
        payload = self._get_json(f"{base}{path}?{query}", redact=key)

        # FMP answers 200 with an error object for some plan violations,
        # rather than an HTTP status. Detected here so a premium endpoint is
        # not mistaken for an empty result.
        if isinstance(payload, dict):
            message = str(
                payload.get("Error Message") or payload.get("error") or ""
            )
            if message:
                lowered = message.lower()
                if ("premium" in lowered or "subscription" in lowered
                        or "upgrade" in lowered or "restricted" in lowered
                        or "legacy" in lowered):
                    # A plan restriction, not a bad key. The provider is
                    # healthy and correctly licensed; it simply does not
                    # cover this symbol or endpoint for us.
                    raise ProviderError(f"{self.name}: not on this plan — {message[:120]}")
                if "limit" in lowered:
                    raise ProviderRateLimited(f"{self.name}: {message[:160]}")
                if "invalid api key" in lowered or "unauthorized" in lowered:
                    raise ProviderAuthError(f"{self.name}: {message[:160]}")
                raise ProviderError(f"{self.name}: {message[:160]}")
        return payload

    # -- symbols ----------------------------------------------------------
    @staticmethod
    def to_symbol(ticker: str) -> str:
        return normalise_symbol(ticker)

    # -- endpoints --------------------------------------------------------
    def company_profile(self, ticker: str) -> list[dict]:
        return self._call("/profile", symbol=self.to_symbol(ticker))

    def quote(self, ticker: str) -> list[dict]:
        return self._call("/quote", symbol=self.to_symbol(ticker))

    def key_metrics(self, ticker: str, *, limit: int = 5) -> list[dict]:
        return self._call("/key-metrics", symbol=self.to_symbol(ticker), limit=limit)

    def ratios(self, ticker: str, *, limit: int = 5) -> list[dict]:
        return self._call("/ratios", symbol=self.to_symbol(ticker), limit=limit)

    def income_statement(self, ticker: str, *, limit: int = 5) -> list[dict]:
        return self._call("/income-statement", symbol=self.to_symbol(ticker), limit=limit)

    def balance_sheet(self, ticker: str, *, limit: int = 5) -> list[dict]:
        return self._call(
            "/balance-sheet-statement", symbol=self.to_symbol(ticker), limit=limit,
        )

    def cash_flow(self, ticker: str, *, limit: int = 5) -> list[dict]:
        return self._call(
            "/cash-flow-statement", symbol=self.to_symbol(ticker), limit=limit,
        )

    def news(self, ticker: str, *, limit: int = 10) -> list[dict]:
        return self._call("/news/stock", symbols=self.to_symbol(ticker), limit=limit)

    def price_history(self, ticker: str, *, days: int = 90) -> dict:
        return self._call(
            "/historical-price-eod/light", symbol=self.to_symbol(ticker),
        )

    def earnings(self, ticker: str, *, limit: int = 6) -> list[dict]:
        return self._call("/earnings", symbol=self.to_symbol(ticker), limit=limit)

    # -- parsing ----------------------------------------------------------
    @staticmethod
    def parse(ticker: str, raw: dict[str, Any]) -> MarketSnapshot:
        """Normalise FMP's payloads. Pure, so it is testable without a key."""
        snapshot = MarketSnapshot(
            ticker=ticker.upper(), source=FMPProvider.name,
        )

        def first(key: str) -> dict | None:
            value = raw.get(key)
            if isinstance(value, list) and value and isinstance(value[0], dict):
                return value[0]
            return None

        def note(section: str, key: str) -> None:
            entry = raw.get(key)
            reason = (
                entry.get("_error") if isinstance(entry, dict) and "_error" in entry
                else None
            )
            snapshot.unavailable.append(
                f"{section}: {reason}" if reason else section
            )

        profile = first("profile")
        if profile:
            # `/stable` renamed this from v3's `mktCap`. Both are read so a
            # cached v3 payload still parses.
            cap = to_float(
                profile.get("marketCap") or profile.get("mktCap"),
                zero_is_absent=True,
            )
            snapshot.profile = CompanyProfile(
                name=profile.get("companyName") or None,
                exchange=profile.get("exchange") or profile.get("exchangeFullName"),
                currency=profile.get("currency") or None,
                industry=profile.get("industry") or None,
                sector=profile.get("sector") or None,
                description=(profile.get("description") or "")[:600] or None,
                website=profile.get("website") or None,
                market_cap=round(cap / _CRORE, 2) if cap else None,
            )
            # `/stable/profile` carries the last price, change and volume.
            # That matters on the free plan, where `/stable/quote` is
            # premium-gated for non-US symbols: without this a usable quote
            # would be discarded and the router would fall through
            # unnecessarily.
            price = to_float(profile.get("price"), zero_is_absent=True)
            if price is not None:
                snapshot.quote = Quote(
                    price=price,
                    change=to_float(profile.get("change")),
                    percent_change=to_float(profile.get("changePercentage")),
                    volume=to_float(profile.get("volume")),
                )
            band = str(profile.get("range") or "")
            if "-" in band:
                low, _, high = band.partition("-")
                snapshot.key_metrics["week52_low"] = to_float(low)
                snapshot.key_metrics["week52_high"] = to_float(high)
        else:
            note("company profile", "profile")

        quote = first("quote")
        if quote:
            # A real /quote response supersedes the profile-derived one.
            shares = to_float(quote.get("sharesOutstanding"), zero_is_absent=True)
            if shares and snapshot.profile.shares_outstanding is None:
                snapshot.profile.shares_outstanding = round(shares / _CRORE, 4)
            if snapshot.profile.market_cap is None:
                cap = to_float(quote.get("marketCap"), zero_is_absent=True)
                snapshot.profile.market_cap = round(cap / _CRORE, 2) if cap else None
            snapshot.quote = Quote(
                price=to_float(quote.get("price"), zero_is_absent=True),
                change=to_float(quote.get("change")),
                percent_change=to_float(quote.get("changesPercentage")),
                day_open=to_float(quote.get("open"), zero_is_absent=True),
                day_high=to_float(quote.get("dayHigh"), zero_is_absent=True),
                day_low=to_float(quote.get("dayLow"), zero_is_absent=True),
                previous_close=to_float(quote.get("previousClose"), zero_is_absent=True),
                volume=to_float(quote.get("volume")),
            )
            snapshot.key_metrics.setdefault(
                "pe_ratio", to_float(quote.get("pe"), zero_is_absent=True),
            )
            snapshot.key_metrics.setdefault(
                "eps", to_float(quote.get("eps"), zero_is_absent=True),
            )
            snapshot.key_metrics.setdefault(
                "week52_high", to_float(quote.get("yearHigh"), zero_is_absent=True),
            )
            snapshot.key_metrics.setdefault(
                "week52_low", to_float(quote.get("yearLow"), zero_is_absent=True),
            )
        else:
            note("quote", "quote")

        metrics = first("key_metrics")
        if metrics:
            snapshot.key_metrics.update({
                "revenue_per_share": to_float(metrics.get("revenuePerShare")),
                "net_income_per_share": to_float(metrics.get("netIncomePerShare")),
                "book_value_per_share": to_float(metrics.get("bookValuePerShare")),
                "roe": to_float(metrics.get("roe")),
                "debt_to_equity": to_float(metrics.get("debtToEquity")),
                "period": metrics.get("date"),
            })
        else:
            note("key metrics", "key_metrics")

        ratios = first("ratios")
        if ratios:
            snapshot.ratios = {
                "current_ratio": to_float(ratios.get("currentRatio")),
                "quick_ratio": to_float(ratios.get("quickRatio")),
                "gross_margin": to_float(ratios.get("grossProfitMargin")),
                "operating_margin": to_float(ratios.get("operatingProfitMargin")),
                "net_margin": to_float(ratios.get("netProfitMargin")),
                "return_on_equity": to_float(ratios.get("returnOnEquity")),
                "return_on_assets": to_float(ratios.get("returnOnAssets")),
                "period": ratios.get("date"),
            }
        else:
            note("financial ratios", "ratios")

        for section, key in (
            ("income_statement", "income_statement"),
            ("balance_sheet", "balance_sheet"),
            ("cash_flow", "cash_flow"),
        ):
            rows = raw.get(key)
            if isinstance(rows, list) and rows:
                setattr(snapshot, section, [
                    {
                        "period": row.get("date"),
                        "fiscal_year": row.get("calendarYear"),
                        "currency": row.get("reportedCurrency"),
                        **{
                            field_name: to_float(row.get(field_name))
                            for field_name in _STATEMENT_FIELDS.get(key, ())
                            if row.get(field_name) is not None
                        },
                    }
                    for row in rows[:5]
                ])
            else:
                note(section.replace("_", " "), key)

        news = raw.get("news")
        if isinstance(news, list) and news:
            snapshot.news = [
                {
                    "headline": item.get("title"),
                    "source": item.get("site"),
                    "url": item.get("url"),
                    "published": item.get("publishedDate"),
                    "summary": (item.get("text") or "")[:280],
                }
                for item in news[:10]
            ]
        else:
            note("company news", "news")

        history = raw.get("price_history")
        rows = history.get("historical") if isinstance(history, dict) else None
        if rows:
            snapshot.price_history = [
                {
                    "date": row.get("date"),
                    "open": to_float(row.get("open")),
                    "high": to_float(row.get("high")),
                    "low": to_float(row.get("low")),
                    "close": to_float(row.get("close")),
                    "volume": to_float(row.get("volume")),
                }
                for row in rows[:120]
            ]
        else:
            note("historical price data", "price_history")

        earnings = raw.get("earnings")
        if isinstance(earnings, list) and earnings:
            snapshot.earnings = [
                {
                    "date": row.get("date"),
                    "eps_actual": to_float(row.get("eps")),
                    "eps_estimated": to_float(row.get("epsEstimated")),
                    "revenue_actual": to_float(row.get("revenue")),
                    "revenue_estimated": to_float(row.get("revenueEstimated")),
                }
                for row in earnings[:6]
            ]
        else:
            note("earnings", "earnings")

        return snapshot

    # -- orchestration ----------------------------------------------------
    def fetch(self, ticker: str, **kwargs) -> tuple[MarketSnapshot, dict[str, Any]]:
        """Every endpoint for one ticker.

        A failure in one endpoint does not fail the rest — a profile without
        news is still worth having — except for an authentication failure,
        which is fatal for the provider and re-raised so the router falls back
        immediately rather than repeating a guaranteed rejection nine times.
        """
        raw: dict[str, Any] = {}

        def attempt(key: str, fn) -> None:
            try:
                raw[key] = fn()
            except ProviderAuthError:
                raise
            except ProviderError as exc:
                raw[key] = {"_error": str(exc)[:160]}
                log.warning("fmp endpoint unavailable", endpoint=key,
                            ticker=ticker, reason=str(exc)[:120])

        attempt("profile", lambda: self.company_profile(ticker))
        attempt("quote", lambda: self.quote(ticker))
        attempt("key_metrics", lambda: self.key_metrics(ticker))
        attempt("ratios", lambda: self.ratios(ticker))
        attempt("income_statement", lambda: self.income_statement(ticker))
        attempt("balance_sheet", lambda: self.balance_sheet(ticker))
        attempt("cash_flow", lambda: self.cash_flow(ticker))
        if kwargs.get("include_news", True):
            attempt("news", lambda: self.news(ticker))
        if kwargs.get("include_history", True):
            attempt("price_history", lambda: self.price_history(ticker))
        if kwargs.get("include_earnings", True):
            attempt("earnings", lambda: self.earnings(ticker))

        return self.parse(ticker, raw), raw


#: Fields lifted from each statement. A deliberate subset: the platform has
#: its own 54-item canonical model, and copying FMP's full schema would
#: create a second, competing definition of what a line item means.
_STATEMENT_FIELDS: dict[str, tuple[str, ...]] = {
    "income_statement": (
        "revenue", "costOfRevenue", "grossProfit", "operatingIncome",
        "ebitda", "netIncome", "eps", "epsdiluted",
    ),
    "balance_sheet": (
        "totalAssets", "totalLiabilities", "totalEquity", "cashAndCashEquivalents",
        "totalDebt", "netDebt", "totalCurrentAssets", "totalCurrentLiabilities",
    ),
    "cash_flow": (
        "operatingCashFlow", "capitalExpenditure", "freeCashFlow",
        "netCashUsedForInvestingActivites", "dividendsPaid",
    ),
}
