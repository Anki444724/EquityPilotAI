"""Official filings layer.

A filing is not the same kind of evidence as a market-data quote, and the
platform should not pretend otherwise. A company's own annual report, lodged
with a regulator and signed by its auditors, carries weight that a
third-party aggregator's convenience endpoint does not — and when the two
disagree, the filing is right.

So filings get their own provider abstraction, their own source categories,
and a confidence model that ranks a regulator-lodged document above an API
response. The categories are the vocabulary every AI answer must cite, which
is why they are an enum here rather than strings scattered across callers.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Any

import structlog

log = structlog.get_logger(__name__)


class SourceCategory(StrEnum):
    """Where a piece of evidence came from, as cited to the reader.

    Ordered from most to least authoritative. An answer that cannot name its
    category is an answer whose provenance cannot be checked.
    """

    ANNUAL_REPORT = "Annual Report"
    NSE_FILING = "NSE Filing"
    BSE_FILING = "BSE Filing"
    SEC_FILING = "SEC Filing"
    INTERNAL_DATABASE = "Internal Database"
    MARKET_DATA = "Market Data"


class FilingType(StrEnum):
    """What kind of document it is."""

    ANNUAL_REPORT = "annual_report"
    QUARTERLY_RESULTS = "quarterly_results"
    INVESTOR_PRESENTATION = "investor_presentation"
    PRESS_RELEASE = "press_release"
    CORPORATE_ANNOUNCEMENT = "corporate_announcement"
    OTHER = "other"


#: How much a category is trusted, 0–1. Official filings outrank third-party
#: APIs deliberately: an aggregator can be stale, can mis-map a ticker, and
#: has no legal exposure if it is wrong. A regulator-lodged document has all
#: three the other way round.
CATEGORY_CONFIDENCE: dict[SourceCategory, float] = {
    SourceCategory.ANNUAL_REPORT: 1.00,
    SourceCategory.SEC_FILING: 0.98,
    SourceCategory.NSE_FILING: 0.95,
    SourceCategory.BSE_FILING: 0.94,
    SourceCategory.INTERNAL_DATABASE: 0.75,
    SourceCategory.MARKET_DATA: 0.60,
}

#: Age discount. A filing does not become wrong with age — a 2019 annual
#: report is still exactly what the company said in 2019 — but it becomes
#: less relevant to a question about today, and the score should say so.
def recency_factor(filed: date | None, *, today: date | None = None) -> float:
    if filed is None:
        return 0.85            # unknown date: mildly discounted, not punished
    reference = today or date.today()
    days = max((reference - filed).days, 0)
    if days <= 90:
        return 1.00
    if days <= 365:
        return 0.95
    if days <= 730:
        return 0.88
    if days <= 1825:
        return 0.80
    return 0.70


def confidence_for(
    category: SourceCategory,
    *,
    filed: date | None = None,
    completeness: float = 1.0,
) -> float:
    """Score one piece of evidence.

    Three factors, multiplied: how authoritative the category is, how recent
    the document is, and how much of the question it actually answers. A
    complete, recent annual report scores 1.0; a five-year-old fragment from
    a market API scores near the floor.
    """
    base = CATEGORY_CONFIDENCE.get(category, 0.5)
    return round(base * recency_factor(filed) * max(0.0, min(completeness, 1.0)), 3)


@dataclass(slots=True)
class Filing:
    """One official document."""

    category: SourceCategory
    filing_type: FilingType
    title: str
    #: Regulator-issued identifier where one exists — an SEC accession
    #: number, an NSE sequence id. This is what makes a citation checkable.
    reference: str | None = None
    filed_on: date | None = None
    period: str | None = None
    url: str | None = None
    summary: str | None = None
    #: Set when the filing came from a document the platform has ingested,
    #: so an AI answer can cite the page and chunk rather than the document.
    document_id: int | None = None
    exchange: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def confidence(self) -> float:
        return confidence_for(self.category, filed=self.filed_on)

    def citation(self) -> str:
        """A reference a reader can act on.

        Names the category, the document and its regulator identifier: enough
        to find the original without trusting this platform's rendering of it.
        """
        parts = [f"{self.category.value}: {self.title}"]
        if self.period:
            parts.append(f"({self.period})")
        if self.filed_on:
            parts.append(f"filed {self.filed_on.isoformat()}")
        if self.reference:
            parts.append(f"ref {self.reference}")
        return " · ".join(parts)

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["category"] = self.category.value
        payload["filing_type"] = self.filing_type.value
        payload["filed_on"] = self.filed_on.isoformat() if self.filed_on else None
        payload["confidence"] = self.confidence
        payload["citation"] = self.citation()
        return payload


@dataclass(slots=True)
class FilingResult:
    """What a filing provider returned, and how it went."""

    filings: list[Filing]
    source: str
    category: SourceCategory | None = None
    latency_ms: float = 0.0
    error: str | None = None

    @property
    def found(self) -> bool:
        return bool(self.filings)

    @property
    def confidence(self) -> float:
        return max((f.confidence for f in self.filings), default=0.0)


class FilingProvider(ABC):
    """One source of official documents."""

    name: str = "abstract"
    category: SourceCategory = SourceCategory.MARKET_DATA
    #: Which market this provider covers. The router uses it to skip
    #: providers that cannot possibly help — asking SEC about an NSE listing
    #: wastes a request and muddies the audit trail.
    markets: frozenset[str] = frozenset()

    @abstractmethod
    def available(self) -> bool:
        """Is this provider usable right now?"""

    @abstractmethod
    def fetch(
        self,
        ticker: str,
        *,
        filing_types: list[FilingType] | None = None,
        limit: int = 10,
        **kwargs: Any,
    ) -> FilingResult:
        """Return the most recent matching filings."""


#: Keyword patterns that classify a filing title. Deliberately ordered: an
#: "Annual Report presentation" is a presentation, so the more specific
#: patterns are tested first.
_TYPE_PATTERNS: tuple[tuple[FilingType, tuple[str, ...]], ...] = (
    (FilingType.INVESTOR_PRESENTATION,
     ("investor presentation", "earnings presentation", "analyst presentation",
      "investor deck", "earnings call presentation", "presentation")),
    (FilingType.QUARTERLY_RESULTS,
     ("quarterly result", "unaudited financial result", "financial results",
      "10-q", "q1 ", "q2 ", "q3 ", "q4 ", "quarter ended", "interim results")),
    (FilingType.ANNUAL_REPORT,
     ("annual report", "10-k", "20-f", "integrated report",
      "annual financial statement", "audited financial result")),
    (FilingType.PRESS_RELEASE,
     ("press release", "media release", "news release", "8-k")),
)


def classify_filing(title: str, *, form: str | None = None) -> FilingType:
    """Best-guess document type from its title and regulator form code.

    The form code wins when present: "10-K" is unambiguous in a way that a
    free-text title never is.
    """
    code = (form or "").strip().upper()
    if code in {"10-K", "20-F", "40-F"}:
        return FilingType.ANNUAL_REPORT
    if code in {"10-Q"}:
        return FilingType.QUARTERLY_RESULTS
    if code in {"8-K", "6-K"}:
        return FilingType.PRESS_RELEASE

    text = (title or "").lower()
    for filing_type, patterns in _TYPE_PATTERNS:
        if any(pattern in text for pattern in patterns):
            return filing_type
    return FilingType.CORPORATE_ANNOUNCEMENT if text else FilingType.OTHER


def parse_date(value: Any) -> date | None:
    """Parse the several date shapes these sources use."""
    if isinstance(value, date):
        return value
    if isinstance(value, datetime):
        return value.date()
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%d-%b-%Y %H:%M:%S", "%d-%b-%Y", "%d/%m/%Y",
                "%Y-%m-%dT%H:%M:%S", "%d %b %Y"):
        try:
            return datetime.strptime(text[:len(fmt) + 8], fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None
