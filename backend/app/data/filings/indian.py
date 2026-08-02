"""NSE and BSE corporate filings.

Neither exchange publishes a documented public API, so both are read through
the endpoints their own websites call. That is a real fragility and is
treated as one: every failure degrades to the next tier rather than raising,
and the reason is recorded so an operator can see *why* a tier was skipped
rather than guessing.

NSE additionally requires a session cookie obtained by first requesting the
home page; a bare API call returns an anti-bot page. BSE keys on a numeric
scrip code rather than a ticker.
"""
from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from http.cookiejar import CookieJar
from typing import Any

import structlog

from app.data.filings.base import (
    Filing, FilingProvider, FilingResult, FilingType, SourceCategory,
    classify_filing, parse_date,
)

log = structlog.get_logger(__name__)

_BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


class NSEFilingProvider(FilingProvider):
    """Corporate announcements from the National Stock Exchange."""

    name = "NSE Corporate Filings"
    category = SourceCategory.NSE_FILING
    markets = frozenset({"India"})

    _HOME = "https://www.nseindia.com"
    _ANNOUNCEMENTS = (
        "https://www.nseindia.com/api/corporate-announcements"
        "?index=equities&symbol={symbol}"
    )

    #: Retry schedule for a transient NSE failure.
    #:
    #: Measured, not guessed: 44 of 501 companies recorded
    #: "NSE unreachable: The read operation timed out" on the nightly crawl,
    #: and the previous implementation made exactly ONE attempt with a bare
    #: `except`, so a single slow response cost that company a whole day.
    #:
    #: Exponential with jitter. The jitter matters more than the growth: a
    #: crawl walks companies in a loop, so a fixed backoff would align every
    #: retry into a burst and reproduce the overload that caused the timeout.
    _RETRY_ATTEMPTS = 3
    _RETRY_BASE_SECONDS = 1.5
    _RETRY_FACTOR = 3.0
    _RETRY_JITTER = 0.3

    def __init__(self, *, timeout: float = 20.0,
                 attempts: int | None = None) -> None:
        self.timeout = timeout
        #: Injectable so a test can assert the retry loop without sleeping.
        self.attempts = attempts if attempts is not None else self._RETRY_ATTEMPTS
        self._opener: urllib.request.OpenerDirector | None = None

    def _backoff_seconds(self, attempt: int) -> float:
        """Delay before retry `attempt` (1-based), with jitter."""
        base = self._RETRY_BASE_SECONDS * (self._RETRY_FACTOR ** (attempt - 1))
        return base * (1.0 + random.uniform(-self._RETRY_JITTER,
                                            self._RETRY_JITTER))

    @staticmethod
    def _is_retryable(exc: Exception) -> bool:
        """Whether another attempt could plausibly succeed.

        A timeout, a reset connection or a 5xx is worth retrying. A 404 is
        not — the symbol does not exist, and two more requests just spend the
        rate-limit budget confirming it.
        """
        if isinstance(exc, urllib.error.HTTPError):
            return exc.code in (408, 425, 429, 500, 502, 503, 504)
        return isinstance(exc, (TimeoutError, urllib.error.URLError, OSError))

    def available(self) -> bool:
        return True

    def _session(self) -> urllib.request.OpenerDirector:
        """An opener holding NSE's cookies.

        The API refuses a request that has not first visited the site, so the
        home page is fetched once to seed the jar. Reused across calls: doing
        it per request would triple the traffic for no benefit.
        """
        if self._opener is not None:
            return self._opener
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar())
        )
        opener.addheaders = [
            ("User-Agent", _BROWSER_UA),
            ("Accept", "application/json, text/plain, */*"),
            ("Accept-Language", "en-GB,en;q=0.9"),
            ("Referer", f"{self._HOME}/companies-listing/corporate-filings-announcements"),
        ]
        try:
            opener.open(self._HOME, timeout=self.timeout).read(1024)
        except Exception as exc:  # noqa: BLE001 - the call may still succeed
            log.debug("nse session priming failed", error=str(exc)[:120])
        self._opener = opener
        return opener

    def fetch(
        self,
        ticker: str,
        *,
        filing_types: list[FilingType] | None = None,
        limit: int = 10,
        **kwargs: Any,
    ) -> FilingResult:
        started = time.perf_counter()
        symbol = (ticker or "").strip().upper().split(".")[0]
        wanted = set(filing_types or [])

        url = self._ANNOUNCEMENTS.format(symbol=urllib.parse.quote(symbol))
        payload = None
        last_error: Exception | None = None

        for attempt in range(1, max(1, self.attempts) + 1):
            try:
                with self._session().open(url, timeout=self.timeout) as response:
                    payload = json.load(response)
                break
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt >= self.attempts or not self._is_retryable(exc):
                    break
                # Drop the cookie jar between attempts. NSE expires a session
                # aggressively, and a stale jar is a common cause of the
                # second attempt failing the same way as the first.
                self._opener = None
                delay = self._backoff_seconds(attempt)
                log.info("nse retry", symbol=symbol, attempt=attempt,
                         delay=round(delay, 2), error=str(exc)[:100])
                time.sleep(delay)

        if payload is None:
            return FilingResult(
                filings=[], source=self.name, category=self.category,
                latency_ms=(time.perf_counter() - started) * 1000,
                error=(
                    f"NSE unreachable after {self.attempts} attempts: "
                    f"{str(last_error)[:100]}"
                ),
            )

        rows = payload if isinstance(payload, list) else payload.get("data") or []
        filings: list[Filing] = []
        for row in rows:
            # The feed is the whole market unless the symbol filter bites, so
            # it is applied here as well as in the query string.
            if str(row.get("symbol", "")).upper() not in {symbol, ""}:
                continue
            title = (row.get("desc") or row.get("attchmntText") or "").strip()
            filing_type = classify_filing(
                f"{title} {row.get('attchmntText') or ''}"
            )
            if wanted and filing_type not in wanted:
                continue
            filings.append(Filing(
                category=self.category,
                filing_type=filing_type,
                title=title[:200] or "Corporate announcement",
                reference=str(row.get("seq_id") or "") or None,
                filed_on=parse_date(row.get("an_dt") or row.get("sort_date")),
                url=row.get("attchmntFile") or None,
                summary=(row.get("attchmntText") or "")[:280] or None,
                exchange="NSE",
                extra={"industry": row.get("smIndustry"),
                       "isin": row.get("sm_isin")},
            ))
            if len(filings) >= limit:
                break

        return FilingResult(
            filings=filings, source=self.name, category=self.category,
            latency_ms=(time.perf_counter() - started) * 1000,
        )


class BSEFilingProvider(FilingProvider):
    """Corporate announcements from the Bombay Stock Exchange."""

    name = "BSE Corporate Announcements"
    category = SourceCategory.BSE_FILING
    markets = frozenset({"India"})

    _API = "https://api.bseindia.com/BseIndiaAPI/api/AnnGetData/w"

    #: BSE keys on a numeric scrip code, not a ticker. A full mapping needs
    #: their master list; the majors are carried here so the tier is usable,
    #: and an unmapped symbol degrades to the next tier rather than guessing.
    SCRIP_CODES: dict[str, str] = {
        "RELIANCE": "500325", "TCS": "532540", "HDFCBANK": "500180",
        "INFY": "500209", "SBIN": "500112", "ICICIBANK": "532174",
        "BHARTIARTL": "532454", "ITC": "500875", "LT": "500510",
        "HINDUNILVR": "500696", "AXISBANK": "532215", "MARUTI": "532500",
        "SUNPHARMA": "524715", "TITAN": "500114", "WIPRO": "507685",
    }

    def __init__(self, *, timeout: float = 20.0) -> None:
        self.timeout = timeout

    def available(self) -> bool:
        return True

    def fetch(
        self,
        ticker: str,
        *,
        filing_types: list[FilingType] | None = None,
        limit: int = 10,
        **kwargs: Any,
    ) -> FilingResult:
        started = time.perf_counter()
        symbol = (ticker or "").strip().upper().split(".")[0]
        # BSE-002. The caller supplies the scrip code from the company record —
        # 498 of them arrived with the Nifty 500 import — and this ignored it
        # in favour of a hardcoded fifteen-entry table, so every other company
        # reported "no BSE scrip code mapped" however well populated the
        # database was. The passed value wins; the table is the fallback for
        # callers that have none.
        scrip = kwargs.get("scrip_code") or self.SCRIP_CODES.get(symbol)
        if scrip is None:
            return FilingResult(
                filings=[], source=self.name, category=self.category,
                latency_ms=(time.perf_counter() - started) * 1000,
                error=f"no BSE scrip code mapped for {symbol}",
            )

        today = date.today()
        params = urllib.parse.urlencode({
            "strCat": "-1", "strPrevDate": (today - timedelta(days=180)).strftime("%Y%m%d"),
            "strScrip": scrip, "strSearch": "P",
            "strToDate": today.strftime("%Y%m%d"), "strType": "C",
        })
        request = urllib.request.Request(f"{self._API}?{params}", headers={
            "User-Agent": _BROWSER_UA,
            "Accept": "application/json",
            "Referer": "https://www.bseindia.com/",
            "Origin": "https://www.bseindia.com",
        })

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.load(response)
        except Exception as exc:  # noqa: BLE001
            return FilingResult(
                filings=[], source=self.name, category=self.category,
                latency_ms=(time.perf_counter() - started) * 1000,
                error=f"BSE unreachable: {str(exc)[:120]}",
            )

        if isinstance(payload, str):
            # BSE answers 200 with the string "No Record Found!" rather than
            # an empty list, so this is a normal outcome, not a fault.
            return FilingResult(
                filings=[], source=self.name, category=self.category,
                latency_ms=(time.perf_counter() - started) * 1000,
                error=payload[:120],
            )

        rows = (payload or {}).get("Table") or []
        filings: list[Filing] = []
        for row in rows:
            title = (row.get("NEWSSUB") or row.get("HEADLINE") or "").strip()
            filing_type = classify_filing(title)
            if filing_types and filing_type not in set(filing_types):
                continue
            attachment = row.get("ATTACHMENTNAME") or ""
            filings.append(Filing(
                category=self.category,
                filing_type=filing_type,
                title=title[:200] or "Corporate announcement",
                reference=str(row.get("NEWSID") or "") or None,
                filed_on=parse_date(row.get("NEWS_DT") or row.get("DT_TM")),
                url=(f"https://www.bseindia.com/xml-data/corpfiling/AttachLive/{attachment}"
                     if attachment else None),
                summary=(row.get("MORE") or "")[:280] or None,
                exchange="BSE",
                extra={"scrip_code": scrip},
            ))
            if len(filings) >= limit:
                break

        return FilingResult(
            filings=filings, source=self.name, category=self.category,
            latency_ms=(time.perf_counter() - started) * 1000,
        )
