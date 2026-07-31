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

    def __init__(self, *, timeout: float = 20.0) -> None:
        self.timeout = timeout
        self._opener: urllib.request.OpenerDirector | None = None

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

        try:
            url = self._ANNOUNCEMENTS.format(symbol=urllib.parse.quote(symbol))
            with self._session().open(url, timeout=self.timeout) as response:
                payload = json.load(response)
        except Exception as exc:  # noqa: BLE001
            return FilingResult(
                filings=[], source=self.name, category=self.category,
                latency_ms=(time.perf_counter() - started) * 1000,
                error=f"NSE unreachable: {str(exc)[:120]}",
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
        scrip = self.SCRIP_CODES.get(symbol)
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
