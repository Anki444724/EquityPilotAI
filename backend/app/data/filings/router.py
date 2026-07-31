"""Filing routing, and the evidence chain beneath it.

Order differs by market, as the brief specifies:

    India: uploaded annual reports (RAG) → NSE → BSE → screener pipeline
           → Finnhub → FMP → Yahoo
    US:    SEC EDGAR → uploaded annual reports → Finnhub → FMP → Yahoo

Uploaded reports lead for India because the platform has already parsed them
into citable passages — page, chunk and quotation — which is stronger evidence
than an announcement title. For the US, EDGAR leads because it is the
regulator's own copy and is exhaustive.

Every answer carries the source *category*, not just the provider name. "FMP"
tells a reader which vendor was called; "Annual Report" tells them what kind
of thing they are being asked to believe.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import structlog

from app.data.filings.base import (
    CATEGORY_CONFIDENCE, Filing, FilingProvider, FilingResult, FilingType,
    SourceCategory, confidence_for,
)
from app.data.filings.indian import BSEFilingProvider, NSEFilingProvider
from app.data.filings.sec import SECFilingProvider
from app.data.providers.symbols import resolve

log = structlog.get_logger(__name__)


class UploadedReportProvider(FilingProvider):
    """Annual reports the platform has already ingested.

    The strongest evidence available, because ingestion has turned the
    document into passages that can be quoted with a page and a chunk id.
    Everything else in this layer can cite a document; only this can cite a
    sentence.
    """

    name = "Uploaded Annual Reports (RAG)"
    category = SourceCategory.ANNUAL_REPORT
    markets = frozenset({"India", "United States"})

    def __init__(self, db: Any = None) -> None:
        self.db = db

    def available(self) -> bool:
        return self.db is not None

    def fetch(
        self,
        ticker: str,
        *,
        filing_types: list[FilingType] | None = None,
        limit: int = 10,
        **kwargs: Any,
    ) -> FilingResult:
        started = time.perf_counter()
        if self.db is None:
            return FilingResult(filings=[], source=self.name,
                                category=self.category, error="no database session")

        try:
            from sqlalchemy import select

            from app.models.company import Company
            from app.services.documents.service import DocumentService

            base = (ticker or "").upper().split(".")[0]
            company = self.db.scalar(select(Company).where(Company.ticker == base))
            if company is None:
                return FilingResult(
                    filings=[], source=self.name, category=self.category,
                    latency_ms=(time.perf_counter() - started) * 1000,
                    error=f"{base} is not in the coverage universe",
                )

            documents = DocumentService(self.db).list_documents(
                company.id, include_superseded=False,
            )
            from app.data.filings.base import classify_filing, parse_date

            filings = [
                Filing(
                    category=self.category,
                    filing_type=classify_filing(
                        f"{document.title or ''} {document.filename}"
                    ),
                    title=document.title or document.filename,
                    reference=f"document:{document.id}",
                    filed_on=parse_date(document.processed_at),
                    period=document.period,
                    document_id=document.id,
                    summary=(
                        f"{document.page_count} pages, {document.chunk_count} "
                        f"indexed passages, {document.fact_count} extracted fields"
                    ),
                    extra={"status": document.status,
                           "fiscal_year": document.fiscal_year},
                )
                for document in documents
                if document.status in {"completed", "ready"}
            ][:limit]

            if filing_types:
                wanted = set(filing_types)
                filings = [f for f in filings if f.filing_type in wanted]

            return FilingResult(
                filings=filings, source=self.name, category=self.category,
                latency_ms=(time.perf_counter() - started) * 1000,
            )
        except Exception as exc:  # noqa: BLE001 - never break the chain
            return FilingResult(
                filings=[], source=self.name, category=self.category,
                latency_ms=(time.perf_counter() - started) * 1000,
                error=f"{type(exc).__name__}: {exc}"[:140],
            )


@dataclass(slots=True)
class EvidenceResult:
    """What the chain found, with everything needed to judge it."""

    ticker: str
    market: str
    filings: list[Filing] = field(default_factory=list)
    source_category: SourceCategory | None = None
    provider: str | None = None
    confidence: float = 0.0
    attempted: list[dict[str, Any]] = field(default_factory=list)
    latency_ms: float = 0.0

    @property
    def found(self) -> bool:
        return bool(self.filings)

    def citations(self) -> list[str]:
        return [f.citation() for f in self.filings]

    def as_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "market": self.market,
            "source_category": (
                self.source_category.value if self.source_category else None
            ),
            "provider": self.provider,
            "confidence_score": round(self.confidence, 3),
            "filing_count": len(self.filings),
            "filings": [f.as_dict() for f in self.filings],
            "citations": self.citations(),
            "providers_attempted": self.attempted,
            "latency_ms": round(self.latency_ms, 1),
        }


class FilingRouter:
    """Walks the filing chain in the order the market deserves."""

    def __init__(self, db: Any = None) -> None:
        self.db = db
        self.sec = SECFilingProvider()
        self.nse = NSEFilingProvider()
        self.bse = BSEFilingProvider()
        self.rag = UploadedReportProvider(db)

    def chain_for(self, market: str) -> list[FilingProvider]:
        if market == "India":
            return [self.rag, self.nse, self.bse]
        return [self.sec, self.rag]

    def fetch(
        self,
        ticker: str,
        *,
        filing_types: list[FilingType] | None = None,
        limit: int = 10,
        all_sources: bool = False,
    ) -> EvidenceResult:
        """First provider with anything wins, unless `all_sources`.

        Stopping at the first hit is deliberate: the chain is ordered by
        authority, so continuing past a hit can only add weaker evidence,
        and mixing an annual report with an aggregator's summary invites a
        reader to average two things that should not be averaged.
        """
        resolved = resolve(ticker)
        started = time.perf_counter()
        attempted: list[dict[str, Any]] = []
        collected: list[Filing] = []
        best_category: SourceCategory | None = None
        best_provider: str | None = None

        for provider in self.chain_for(resolved.market):
            if not provider.available():
                attempted.append({"provider": provider.name, "outcome": "skipped",
                                  "reason": "not available"})
                continue

            result = provider.fetch(
                resolved.canonical, filing_types=filing_types, limit=limit,
            )
            if result.found:
                attempted.append({
                    "provider": provider.name, "outcome": "served",
                    "category": provider.category.value,
                    "count": len(result.filings),
                    "ms": round(result.latency_ms, 1),
                })
                collected.extend(result.filings)
                if best_category is None:
                    best_category, best_provider = provider.category, provider.name
                if not all_sources:
                    break
            else:
                attempted.append({
                    "provider": provider.name, "outcome": "no_filings",
                    "reason": (result.error or "none found")[:120],
                    "ms": round(result.latency_ms, 1),
                })

        confidence = max((f.confidence for f in collected), default=0.0)
        elapsed = (time.perf_counter() - started) * 1000
        log.info("filing chain complete", ticker=resolved.canonical,
                 market=resolved.market, provider=best_provider,
                 filings=len(collected), ms=round(elapsed, 1))

        return EvidenceResult(
            ticker=resolved.canonical, market=resolved.market,
            filings=collected, source_category=best_category,
            provider=best_provider, confidence=confidence,
            attempted=attempted, latency_ms=elapsed,
        )


def category_for_source(source: str) -> SourceCategory:
    """Map a market-data provider name onto a citable category.

    Lets an AI answer cite "Market Data" or "Internal Database" in the same
    vocabulary as a filing, so every claim carries a category regardless of
    which layer produced it.
    """
    lowered = (source or "").lower()
    if "internal" in lowered or "database" in lowered:
        return SourceCategory.INTERNAL_DATABASE
    if "document" in lowered or "rag" in lowered or "annual report" in lowered:
        return SourceCategory.ANNUAL_REPORT
    if "sec" in lowered or "edgar" in lowered:
        return SourceCategory.SEC_FILING
    if "nse" in lowered:
        return SourceCategory.NSE_FILING
    if "bse" in lowered:
        return SourceCategory.BSE_FILING
    return SourceCategory.MARKET_DATA
