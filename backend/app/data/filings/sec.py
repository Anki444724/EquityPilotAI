"""SEC EDGAR — official filings for US listings.

Free, keyless and authoritative: 10-K, 10-Q, 8-K and the rest, straight from
the regulator. EDGAR's only requirement is a User-Agent naming the caller,
which its fair-access policy treats as a contact address; requests without
one are refused.

Two-step lookup, because EDGAR keys on the CIK rather than the ticker: the
ticker map is fetched once and cached for the process, then the submissions
feed gives every filing the company has lodged.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from functools import lru_cache
from typing import Any

import structlog

from app.data.filings.base import (
    Filing, FilingProvider, FilingResult, FilingType, SourceCategory,
    classify_filing, parse_date,
)

log = structlog.get_logger(__name__)

_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
_ARCHIVE = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{document}"

#: EDGAR's fair-access policy asks for an identifying User-Agent. A generic
#: one is refused, so this names the platform and a contact route.
_HEADERS = {
    "User-Agent": "IERP Institutional Equity Research Platform (contact: research@ierp.local)",
    "Accept": "application/json",
    # Deliberately NOT advertising gzip: urllib does not decompress
    # transparently, so EDGAR replied with a gzip stream that json.load
    # choked on ("codec can't decode byte 0x8b"), and every US filing
    # silently returned nothing. Identity keeps the payload readable.
    "Accept-Encoding": "identity",
}


class SECFilingProvider(FilingProvider):
    name = "SEC EDGAR"
    category = SourceCategory.SEC_FILING
    markets = frozenset({"United States"})

    def __init__(self, *, timeout: float = 20.0) -> None:
        self.timeout = timeout
        self._last_error: str | None = None

    def available(self) -> bool:
        # No credentials to check. Reachability is proven by use rather than
        # by a probe, so a health check does not spend a request on every call.
        return True

    # -- lookup -----------------------------------------------------------
    def _get(self, url: str) -> Any:
        request = urllib.request.Request(url, headers=_HEADERS)
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.load(response)

    @lru_cache(maxsize=1)
    def _ticker_map(self) -> dict[str, tuple[int, str]]:
        """ticker -> (cik, company name). Fetched once per process."""
        try:
            payload = self._get(_TICKERS_URL)
        except Exception as exc:  # noqa: BLE001
            log.warning("sec ticker map unavailable", error=str(exc)[:160])
            return {}
        return {
            str(row["ticker"]).upper(): (int(row["cik_str"]), row["title"])
            for row in payload.values()
            if row.get("ticker")
        }

    def cik_for(self, ticker: str) -> tuple[int, str] | None:
        base = (ticker or "").strip().upper().split(".")[0]
        return self._ticker_map().get(base)

    # -- filings ----------------------------------------------------------
    def fetch(
        self,
        ticker: str,
        *,
        filing_types: list[FilingType] | None = None,
        limit: int = 10,
        **kwargs: Any,
    ) -> FilingResult:
        started = time.perf_counter()
        wanted = set(filing_types or [])

        resolved = self.cik_for(ticker)
        if resolved is None:
            return FilingResult(
                filings=[], source=self.name, category=self.category,
                latency_ms=(time.perf_counter() - started) * 1000,
                error=f"{ticker} is not in EDGAR's ticker map",
            )

        cik, company = resolved
        try:
            payload = self._get(_SUBMISSIONS.format(cik=cik))
        except urllib.error.HTTPError as exc:
            return FilingResult(
                filings=[], source=self.name, category=self.category,
                latency_ms=(time.perf_counter() - started) * 1000,
                error=f"EDGAR HTTP {exc.code}",
            )
        except Exception as exc:  # noqa: BLE001
            return FilingResult(
                filings=[], source=self.name, category=self.category,
                latency_ms=(time.perf_counter() - started) * 1000,
                error=f"EDGAR unreachable: {str(exc)[:120]}",
            )

        recent = (payload.get("filings") or {}).get("recent") or {}
        forms = recent.get("form") or []
        filings: list[Filing] = []

        for index, form in enumerate(forms):
            filing_type = classify_filing(
                recent.get("primaryDocDescription", [""] * len(forms))[index]
                if index < len(recent.get("primaryDocDescription", [])) else "",
                form=form,
            )
            if wanted and filing_type not in wanted:
                continue

            accession = (recent.get("accessionNumber") or [""])[index]
            document = (recent.get("primaryDocument") or [""])[index]
            filings.append(Filing(
                category=self.category,
                filing_type=filing_type,
                title=f"{form} — {company}",
                # The accession number *is* the citation: it identifies the
                # filing uniquely and permanently in EDGAR.
                reference=accession,
                filed_on=parse_date((recent.get("filingDate") or [None])[index]),
                period=(recent.get("reportDate") or [None])[index] or None,
                url=_ARCHIVE.format(
                    cik=cik, accession=accession.replace("-", ""), document=document,
                ) if accession and document else None,
                exchange=(payload.get("exchanges") or [None])[0],
                extra={"form": form, "cik": cik},
            ))
            if len(filings) >= limit:
                break

        return FilingResult(
            filings=filings, source=self.name, category=self.category,
            latency_ms=(time.perf_counter() - started) * 1000,
        )
