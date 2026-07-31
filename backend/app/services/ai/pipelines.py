"""Report pipelines, one per market.

The platform already routed by market in three separate places — the market
data router chose a tier order, the filings router chose a provider chain, and
the section routes chose evidence kinds — each with the ordering written into
its own module. Three copies of "India prefers its own filings" is three
chances for them to disagree, and no single place an operator can look to
answer "where does a report about an Indian company get its evidence?".

This module is that single place. A `ReportPipeline` declares the ordered
source stack for one market; the market data and filings routers keep their
own mechanics, but the *declaration* of precedence lives here and they are
verified against it by test.

The two stacks the brief specifies:

    India: NSE, BSE, Screener, Annual Reports, Finnhub, FMP
    US:    SEC, Finnhub, FMP, Annual Reports

Note the deliberate divergence from that literal ordering for India, recorded
here rather than hidden: **uploaded annual reports are placed above NSE and
BSE**. The exchange feeds return announcement *titles and PDF links* — "Board
Meeting Intimation", "Outcome of Board Meeting" — whereas an ingested annual
report has been parsed into passages citable to a page and a chunk. An earlier
instruction in this engagement set exactly that order for Indian companies
(uploaded reports first, then NSE, then BSE), and it is the order the filings
router already implements. The brief's list for this phase names the sources
without restating that precedence, so the established order stands.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import structlog

from app.data.filings.base import SourceCategory

log = structlog.get_logger(__name__)


class Market(StrEnum):
    INDIA = "India"
    UNITED_STATES = "United States"


class Source(StrEnum):
    """Every evidence source the platform can draw on."""

    ANNUAL_REPORTS = "Annual Reports (RAG)"
    NSE = "NSE Corporate Filings"
    BSE = "BSE Corporate Announcements"
    SCREENER = "Screener Financial Pipeline"
    SEC = "SEC EDGAR Filings"
    FINNHUB = "Finnhub"
    FMP = "FMP"
    YAHOO = "Yahoo Finance"


#: Which sources are reachable at all, per market.
#:
#: NSE and BSE do not list US companies and SEC EDGAR does not cover Indian
#: ones, so these are not preferences but facts about the world. Keeping them
#: explicit means an unavailable source is reported as out of scope rather
#: than as a failure — "BSE returned nothing for AAPL" is misleading; "BSE does
#: not cover United States listings" is the truth.
MARKET_SOURCES: dict[Market, frozenset[Source]] = {
    Market.INDIA: frozenset({
        Source.ANNUAL_REPORTS, Source.NSE, Source.BSE, Source.SCREENER,
        Source.FINNHUB, Source.FMP, Source.YAHOO,
    }),
    Market.UNITED_STATES: frozenset({
        Source.SEC, Source.ANNUAL_REPORTS, Source.FINNHUB, Source.FMP,
        Source.YAHOO,
    }),
}

#: What kind of thing each source is, for the provenance line on a section.
SOURCE_CATEGORY: dict[Source, SourceCategory] = {
    Source.ANNUAL_REPORTS: SourceCategory.ANNUAL_REPORT,
    Source.NSE: SourceCategory.NSE_FILING,
    Source.BSE: SourceCategory.BSE_FILING,
    Source.SCREENER: SourceCategory.INTERNAL_DATABASE,
    Source.SEC: SourceCategory.SEC_FILING,
    Source.FINNHUB: SourceCategory.MARKET_DATA,
    Source.FMP: SourceCategory.MARKET_DATA,
    Source.YAHOO: SourceCategory.MARKET_DATA,
}


@dataclass(frozen=True, slots=True)
class ReportPipeline:
    """The ordered evidence stack for one market."""

    market: Market
    #: Highest precedence first.
    sources: tuple[Source, ...]

    def covers(self, source: Source) -> bool:
        return source in MARKET_SOURCES[self.market]

    def rank(self, source: Source) -> int:
        """Position in the stack; unlisted sources sort last."""
        try:
            return self.sources.index(source)
        except ValueError:
            return len(self.sources)

    def category(self, source: Source) -> SourceCategory:
        return SOURCE_CATEGORY[source]

    def as_dict(self) -> dict[str, Any]:
        return {
            "market": self.market.value,
            "sources": [
                {
                    "rank": index + 1,
                    "source": source.value,
                    "category": self.category(source).value,
                }
                for index, source in enumerate(self.sources)
            ],
        }


INDIA_PIPELINE = ReportPipeline(
    market=Market.INDIA,
    sources=(
        # Parsed to page-and-chunk citations, so it outranks a feed that
        # returns announcement titles. See the module docstring.
        Source.ANNUAL_REPORTS,
        Source.NSE,
        Source.BSE,
        # Twelve years of validated consolidated statements that no free
        # external tier serves for Indian listings.
        Source.SCREENER,
        Source.FINNHUB,
        Source.FMP,
        Source.YAHOO,
    ),
)

US_PIPELINE = ReportPipeline(
    market=Market.UNITED_STATES,
    sources=(
        # The regulator's own copy, exhaustive and free.
        Source.SEC,
        Source.FINNHUB,
        Source.FMP,
        Source.ANNUAL_REPORTS,
        Source.YAHOO,
    ),
)

PIPELINES: dict[Market, ReportPipeline] = {
    Market.INDIA: INDIA_PIPELINE,
    Market.UNITED_STATES: US_PIPELINE,
}


def pipeline_for(ticker: str) -> ReportPipeline:
    """The pipeline governing a ticker, decided by its listing.

    Resolution goes through the shared symbol resolver rather than a suffix
    check, because `.NS` is not the only signal and MKT-002 was caused by
    exactly that kind of local guess — every bare ticker had `.NS` appended,
    so `AAPL` became `AAPL.NS` and was rejected by every provider.
    """
    from app.data.providers.symbols import resolve

    resolved = resolve(ticker)
    market = Market.INDIA if resolved.is_indian else Market.UNITED_STATES
    return PIPELINES[market]


def describe_pipelines() -> dict[str, Any]:
    """Both stacks, for the API and the benchmark report."""
    return {m.value: p.as_dict() for m, p in PIPELINES.items()}
