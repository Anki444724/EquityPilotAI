"""The automated filing collector.

One company at a time: discover what each source is publishing, decide what is
new, download it, and hand it to the existing ingestion pipeline. The daily
scheduler calls `crawl_due()`; everything below that is deterministic given
its inputs and can be tested without a network.

**Dedup runs at two levels, and both are necessary.** The URL/reference check
happens before any bytes move, so the nightly re-scan of a company that has
published nothing costs one indexed lookup per announcement rather than a
download. The SHA256 check happens after, because NSE and BSE publish the
*same* PDF at different URLs — URL-level dedup alone would store every annual
report twice and retrieve it twice in every RAG answer.

**A collected document is not special.** It goes through
`DocumentIngestionService.accept()` exactly as an uploaded one does: same
persistent storage, same OCR decision, same chunking, same embeddings, same
vector index. There is no parallel pipeline to keep in step, which is the
single most important structural decision here.

**Failure is per-source and per-document.** One company's IR site being down
must not stop the exchange crawl; one corrupt PDF must not abandon the other
forty documents in the batch. Every failure is recorded against the row it
belongs to and the run continues.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import structlog
from sqlalchemy import func, select

from app.data.filings.base import Filing, FilingType
from app.data.filings.indian import BSEFilingProvider, NSEFilingProvider
from app.data.filings.investor_relations import InvestorRelationsProvider
from app.domain.documents.types import DocumentType
from app.domain.filings.collection import (
    MAX_AUTO_DOWNLOAD_BYTES, CollectionStatus, CollectionTier, classify,
    due_for_crawl, fiscal_year_for, is_noise, quarter_for,
)
from app.models.company import Company
from app.models.filing_collection import CompanyCrawlState, DiscoveredFiling
from app.services.filings.downloader import DownloadError, FilingDownloader

log = structlog.get_logger(__name__)

#: Free space the volume must retain when it is only transit/scratch space.
#:
#: Enough for one maximum-sized download plus room for OCR to rasterise it,
#: which is the largest transient the pipeline creates. Far below the durable
#: floor because nothing is being retained here.
TRANSIT_HEADROOM_BYTES = 150 * 1024 * 1024


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class SourceOutcome:
    source: str
    discovered: int = 0
    new: int = 0
    error: str | None = None
    latency_ms: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source, "discovered": self.discovered,
            "new": self.new, "error": self.error,
            "latency_ms": round(self.latency_ms, 1),
        }


@dataclass(slots=True)
class CompanyCrawlResult:
    ticker: str
    company_id: str
    sources: list[SourceOutcome] = field(default_factory=list)
    downloaded: int = 0
    ingested: int = 0
    duplicates: int = 0
    skipped: int = 0
    failed: int = 0
    latency_ms: float = 0.0

    @property
    def discovered(self) -> int:
        return sum(s.discovered for s in self.sources)

    @property
    def new_documents(self) -> int:
        return sum(s.new for s in self.sources)

    @property
    def succeeded(self) -> bool:
        """At least one source answered without erroring."""
        return any(s.error is None for s in self.sources)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker, "company_id": self.company_id,
            "discovered": self.discovered, "new": self.new_documents,
            "downloaded": self.downloaded, "ingested": self.ingested,
            "duplicates": self.duplicates, "skipped": self.skipped,
            "failed": self.failed, "latency_ms": round(self.latency_ms, 1),
            "sources": [s.as_dict() for s in self.sources],
        }


class FilingCollector:
    """Discovers, downloads and ingests filings for Indian companies."""

    def __init__(
        self,
        db: Any,
        *,
        downloader: FilingDownloader | None = None,
        nse: Any = None,
        bse: Any = None,
        ir: Any = None,
        polite_delay: float = 0.0,
    ) -> None:
        self.db = db
        self.downloader = downloader or FilingDownloader()
        self.nse = nse if nse is not None else NSEFilingProvider()
        self.bse = bse if bse is not None else BSEFilingProvider()
        self.ir = ir if ir is not None else InvestorRelationsProvider()
        #: Pause between companies. Exchanges rate-limit, and a burst of 135
        #: requests is the fastest way to be blocked for the rest of the day.
        self.polite_delay = polite_delay

    # ------------------------------------------------------------- state
    def state_for(self, company: Company) -> CompanyCrawlState:
        state = self.db.scalar(
            select(CompanyCrawlState).where(
                CompanyCrawlState.company_id == company.id
            )
        )
        if state is None:
            state = CompanyCrawlState(
                company_id=company.id, tier=CollectionTier.WEEKLY.value,
            )
            self.db.add(state)
            self.db.flush()

        # BSE-001. The scrip code lives on the company after the Nifty 500
        # import, but crawl state was only ever populated by hand — so 498
        # freshly imported codes sat unused and every BSE fetch reported "no
        # scrip code mapped". Copy it across on first sight, without ever
        # overwriting a value an operator set deliberately.
        if not state.bse_scrip_code and getattr(company, "bse_code", None):
            state.bse_scrip_code = company.bse_code
            self.db.flush()
        return state

    def due_companies(self, *, limit: int | None = None) -> list[Company]:
        """Companies whose tier says they should be visited now."""
        rows = self.db.execute(
            select(Company, CompanyCrawlState)
            .outerjoin(CompanyCrawlState,
                       CompanyCrawlState.company_id == Company.id)
            .where(
                Company.exchange.in_(("NSE", "BSE", "NSE/BSE")),
                # DELIST-001. A company marked delisted still had a row and
                # was therefore still crawled every night. Tata Motors has
                # demerged and Zomato has renamed; their old symbols resolve
                # to nothing at NSE, so each one burned a request and a
                # failure counter on every pass. History is retained, but the
                # active universe is what gets collected.
                Company.listing_status == "active",
            )
        ).all()

        now = _utcnow()
        due: list[Company] = []
        for company, state in rows:
            if state is None:
                due.append(company)
                continue
            if not state.enabled:
                continue
            try:
                tier = CollectionTier(state.tier)
            except ValueError:
                tier = CollectionTier.WEEKLY
            if due_for_crawl(tier, state.last_crawled_at, now):
                due.append(company)

        # Longest-waiting first, so a backlog drains fairly rather than the
        # same alphabetical prefix being served every night.
        due.sort(key=lambda c: (
            getattr(self._state_cache(c.id), "last_crawled_at", None) or
            datetime.min.replace(tzinfo=timezone.utc)
        ))
        return due[:limit] if limit else due

    def _state_cache(self, company_id: str) -> Any:
        return self.db.scalar(
            select(CompanyCrawlState).where(
                CompanyCrawlState.company_id == company_id
            )
        )

    # ---------------------------------------------------------- discovery
    def discover(self, company: Company, state: CompanyCrawlState,
                 *, limit_per_source: int = 25) -> tuple[list[tuple[str, Filing]], list[SourceOutcome]]:
        """Ask every source what it is publishing for this company.

        Sources are tried in the brief's priority order and their results are
        *combined* rather than short-circuited: an annual report on the IR
        site and a results announcement on NSE are both wanted, so unlike the
        filings router — which answers one question from the best source —
        collection takes everything.
        """
        found: list[tuple[str, Filing]] = []
        outcomes: list[SourceOutcome] = []

        attempts = [
            (self.ir, {"ir_url": state.ir_url}),
            (self.nse, {}),
            (self.bse, {"scrip_code": state.bse_scrip_code}),
        ]

        for provider, extra in attempts:
            started = time.perf_counter()
            outcome = SourceOutcome(source=getattr(provider, "name", "unknown"))
            try:
                if not provider.available():
                    outcome.error = "provider unavailable"
                else:
                    result = provider.fetch(
                        company.ticker, limit=limit_per_source, **extra,
                    )
                    outcome.error = result.error
                    for filing in result.filings:
                        outcome.discovered += 1
                        found.append((outcome.source, filing))
            except Exception as exc:  # noqa: BLE001 — one source must not stop the rest
                outcome.error = f"{type(exc).__name__}: {exc}"[:200]
                log.warning("source failed", source=outcome.source,
                            ticker=company.ticker, error=outcome.error)
            outcome.latency_ms = (time.perf_counter() - started) * 1000
            outcomes.append(outcome)

        return found, outcomes

    def record(self, company: Company, source: str,
               filing: Filing) -> DiscoveredFiling | None:
        """Insert a discovery row, or return None when already known.

        This is the first dedup gate and the reason a nightly re-scan is
        cheap: an announcement seen yesterday is matched on
        (source, reference) and never downloaded again.
        """
        reference = (filing.reference or filing.url or filing.title or "")[:500]
        if not reference:
            return None

        existing = self.db.scalar(
            select(DiscoveredFiling).where(
                DiscoveredFiling.source == source,
                DiscoveredFiling.source_reference == reference,
            )
        )
        if existing is not None:
            return None

        title = filing.title or ""
        if is_noise(title):
            # Recorded as skipped rather than dropped, so the dashboard can
            # show what was filtered and an operator can audit the filter.
            row = DiscoveredFiling(
                company_id=company.id, source=source,
                source_reference=reference, source_url=filing.url,
                title=title[:480], status=CollectionStatus.SKIPPED.value,
                error="filtered as procedural noise",
            )
            self.db.add(row)
            return row

        classification = classify(title, url=filing.url)
        published = filing.filed_on
        row = DiscoveredFiling(
            company_id=company.id,
            source=source,
            source_reference=reference,
            source_url=filing.url,
            title=title[:480],
            filing_type=classification.filing_type.value,
            doc_type=classification.doc_type.value,
            classification_confidence=classification.confidence,
            published_on=(
                datetime.combine(published, datetime.min.time(), timezone.utc)
                if published else None
            ),
            fiscal_year=fiscal_year_for(published, title),
            quarter=quarter_for(published, title),
            status=CollectionStatus.DISCOVERED.value,
        )
        self.db.add(row)
        return row

    # ------------------------------------------------------------ storage
    def _has_headroom(self) -> bool:
        """Is there enough free disk to take another document safely?

        Deliberately stricter than the ingestion service's own floor: that
        floor protects the *platform* from running out of disk, and by the
        time it trips a user upload is already failing. The crawler reserves a
        margin above it so automated collection stops first and leaves the
        remaining space for interactive work.
        """
        try:
            from app.core.config import settings
            from app.services.documents.storage import free_disk_bytes

            path = getattr(settings, "DOCUMENT_STORAGE_PATH", None)
            if not path:
                return True

            # STORAGE-002. With object storage primary, a collected document
            # does not stay on the volume — it is streamed through and the
            # durable copy lives in R2. The original reservation (512 MB floor
            # plus a 60 MB download) was sized for a volume that had to *hold*
            # the corpus, and against a 500 MB volume already 56% full by
            # migrated copies it refuses every download forever: collection
            # would report "deferred: insufficient free storage" for all 500
            # companies and never recover.
            #
            # The volume is still scratch space during parsing and OCR, so a
            # working margin is kept — just one sized for transit rather than
            # for permanent residency.
            backend = (settings.DOCUMENT_STORAGE_BACKEND or "local").lower()
            if backend in {"s3", "r2", "minio"}:
                required = TRANSIT_HEADROOM_BYTES
            else:
                # Volume is the durable store: reserve the platform floor plus
                # one maximum-sized download, so the check cannot pass and then
                # be invalidated by the very file it just authorised.
                floor_mb = int(getattr(settings, "DOCUMENT_MIN_FREE_DISK_MB", 512))
                required = floor_mb * 1024 * 1024 + MAX_AUTO_DOWNLOAD_BYTES
            return free_disk_bytes(path) > required
        except Exception:  # noqa: BLE001 - never block collection on telemetry
            log.debug("storage headroom check unavailable")
            return True

    # ----------------------------------------------------------- download
    def collect_one(self, row: DiscoveredFiling) -> str:
        """Download and ingest one discovered filing. Returns its status."""
        from app.services.documents.ingestion import (
            DocumentIngestionService, IngestionError,
        )

        if not row.source_url:
            row.status = CollectionStatus.FAILED.value
            row.error = "no download url"
            return row.status

        # STORAGE-001. Stop before the volume fills.
        #
        # Observed in production on the first real run: 229 MB of a 500 MB
        # volume consumed within minutes, with individual shareholder-meeting
        # PDFs above 20 MB. Left unchecked the crawler exhausts the disk, and
        # the failure lands on whatever writes next — quite possibly a user's
        # upload rather than the crawl that caused it. Automated collection is
        # the lowest-priority consumer of shared storage, so it is the thing
        # that must yield.
        if not self._has_headroom():
            row.status = CollectionStatus.DISCOVERED.value
            row.error = (
                "deferred: insufficient free storage for automatic download"
            )
            log.warning("collection deferred, low storage", filing_id=row.id)
            return row.status

        row.status = CollectionStatus.DOWNLOADING.value
        row.attempts = (row.attempts or 0) + 1

        try:
            downloaded = self.downloader.fetch(row.source_url)
        except DownloadError as exc:
            row.status = CollectionStatus.FAILED.value
            row.error = str(exc)[:500]
            log.info("filing download failed", filing_id=row.id,
                     retryable=exc.retryable, error=row.error)
            return row.status

        row.content_sha256 = downloaded.sha256
        row.file_size = downloaded.size
        row.downloaded_at = _utcnow()

        # Second dedup gate: the same bytes under a different URL. NSE and BSE
        # both carry the same PDF, so without this every report is stored and
        # retrieved twice.
        twin = self.db.scalar(
            select(DiscoveredFiling).where(
                DiscoveredFiling.content_sha256 == downloaded.sha256,
                DiscoveredFiling.id != row.id,
                DiscoveredFiling.document_id.is_not(None),
            )
        )
        if twin is not None:
            row.status = CollectionStatus.DUPLICATE.value
            row.document_id = twin.document_id
            row.completed_at = _utcnow()
            return row.status

        row.status = CollectionStatus.PROCESSING.value
        try:
            doc_type = DocumentType(row.doc_type) if row.doc_type else None
        except ValueError:
            doc_type = None

        try:
            accepted = DocumentIngestionService(self.db).accept(
                row.company_id,
                downloaded.content,
                _filename_for(row),
                doc_type=doc_type,
                uploaded_by="filing-collector",
            )
        except IngestionError as exc:
            row.status = CollectionStatus.FAILED.value
            row.error = f"ingestion rejected: {exc}"[:500]
            return row.status
        except Exception as exc:  # noqa: BLE001
            row.status = CollectionStatus.FAILED.value
            row.error = f"{type(exc).__name__}: {exc}"[:500]
            log.exception("ingestion failed", filing_id=row.id)
            return row.status

        row.document_id = accepted.document.id
        if accepted.action == "duplicate":
            # The ingestion service found the same content hash among
            # uploaded documents. Also a success, and also not a new document.
            row.status = CollectionStatus.DUPLICATE.value
        else:
            # Queued for parsing, OCR, chunking and embedding by the existing
            # document worker. `EMBEDDING` is the honest status: the bytes are
            # stored, the row exists, the vector index does not have it yet.
            row.status = CollectionStatus.EMBEDDING.value
        row.completed_at = _utcnow()
        return row.status

    # -------------------------------------------------------------- crawl
    def crawl_company(
        self, company: Company, *, download: bool = True,
        max_downloads: int = 10,
    ) -> CompanyCrawlResult:
        """One company end to end."""
        started = time.perf_counter()
        state = self.state_for(company)
        result = CompanyCrawlResult(ticker=company.ticker, company_id=company.id)

        found, outcomes = self.discover(company, state)
        result.sources = outcomes

        fresh: list[DiscoveredFiling] = []
        for source, filing in found:
            row = self.record(company, source, filing)
            if row is None:
                continue
            for outcome in outcomes:
                if outcome.source == source:
                    outcome.new += 1
            if row.status == CollectionStatus.SKIPPED.value:
                result.skipped += 1
            else:
                fresh.append(row)
        self.db.flush()

        if download:
            for row in fresh[:max_downloads]:
                status = self.collect_one(row)
                if status == CollectionStatus.EMBEDDING.value:
                    result.downloaded += 1
                    result.ingested += 1
                elif status == CollectionStatus.DUPLICATE.value:
                    result.duplicates += 1
                elif status == CollectionStatus.FAILED.value:
                    result.failed += 1
                self.db.flush()

        # Health bookkeeping. A company whose sources all failed accumulates
        # consecutive failures and is eventually demoted, so a permanently
        # broken source cannot consume the nightly budget indefinitely.
        now = _utcnow()
        state.last_crawled_at = now
        state.documents_found = (state.documents_found or 0) + result.new_documents
        state.documents_ingested = (state.documents_ingested or 0) + result.ingested
        if result.succeeded:
            state.last_success_at = now
            state.last_status = "ok"
            state.last_error = None
            state.consecutive_failures = 0
        else:
            state.consecutive_failures = (state.consecutive_failures or 0) + 1
            state.last_status = "failed"
            state.last_error = "; ".join(
                f"{o.source}: {o.error}" for o in outcomes if o.error
            )[:500]
            if state.consecutive_failures >= 10:
                state.tier = CollectionTier.PAUSED.value
                log.warning("company paused after repeated failures",
                            ticker=company.ticker,
                            failures=state.consecutive_failures)

        self.db.commit()
        result.latency_ms = (time.perf_counter() - started) * 1000
        log.info("company crawled", ticker=company.ticker,
                 discovered=result.discovered, new=result.new_documents,
                 ingested=result.ingested, duplicates=result.duplicates,
                 failed=result.failed, ms=round(result.latency_ms, 1))
        return result

    def crawl_due(
        self, *, max_companies: int = 25, download: bool = True,
        max_downloads_per_company: int = 5,
    ) -> dict[str, Any]:
        """The nightly pass."""
        started = time.perf_counter()
        companies = self.due_companies(limit=max_companies)
        results: list[CompanyCrawlResult] = []

        for index, company in enumerate(companies):
            if index and self.polite_delay:
                time.sleep(self.polite_delay)
            try:
                results.append(self.crawl_company(
                    company, download=download,
                    max_downloads=max_downloads_per_company,
                ))
            except Exception as exc:  # noqa: BLE001 — one company must not stop the run
                log.exception("company crawl failed", ticker=company.ticker)
                self.db.rollback()
                results.append(CompanyCrawlResult(
                    ticker=company.ticker, company_id=company.id,
                    sources=[SourceOutcome("crawler",
                                           error=f"{type(exc).__name__}: {exc}"[:200])],
                ))

        elapsed = (time.perf_counter() - started) * 1000
        summary = {
            "companies": len(results),
            "discovered": sum(r.discovered for r in results),
            "new": sum(r.new_documents for r in results),
            "ingested": sum(r.ingested for r in results),
            "duplicates": sum(r.duplicates for r in results),
            "skipped": sum(r.skipped for r in results),
            "failed": sum(r.failed for r in results),
            "latency_ms": round(elapsed, 1),
            "results": [r.as_dict() for r in results],
        }
        log.info("crawl pass complete", **{
            k: v for k, v in summary.items() if k != "results"
        })
        return summary

    # ---------------------------------------------------------- dashboard
    def dashboard(self) -> dict[str, Any]:
        """Counts by status, plus storage and freshness — the admin panel."""
        counts = dict(
            self.db.execute(
                select(DiscoveredFiling.status, func.count())
                .group_by(DiscoveredFiling.status)
            ).all()
        )
        total_bytes = self.db.scalar(
            select(func.coalesce(func.sum(DiscoveredFiling.file_size), 0))
        ) or 0
        last_update = self.db.scalar(
            select(func.max(DiscoveredFiling.discovered_at))
        )
        tiers = dict(
            self.db.execute(
                select(CompanyCrawlState.tier, func.count())
                .group_by(CompanyCrawlState.tier)
            ).all()
        )
        failing = self.db.scalar(
            select(func.count()).select_from(CompanyCrawlState)
            .where(CompanyCrawlState.consecutive_failures > 0)
        ) or 0

        return {
            "by_status": {s.value: counts.get(s.value, 0) for s in CollectionStatus},
            "total_documents": sum(counts.values()),
            "storage_bytes": int(total_bytes),
            "storage_mb": round(total_bytes / (1024 * 1024), 2),
            "last_discovery_at": last_update.isoformat() if last_update else None,
            "companies_by_tier": tiers,
            "companies_failing": failing,
        }


def _filename_for(row: DiscoveredFiling) -> str:
    """A stable, readable filename.

    Derived from the metadata rather than the URL, because NSE archive URLs
    end in opaque sequence ids that tell a reader nothing.
    """
    parts = [row.doc_type or "filing"]
    if row.fiscal_year:
        parts.append(f"FY{row.fiscal_year}")
    if row.quarter:
        parts.append(row.quarter)
    parts.append(str(row.id or "new"))
    stem = "_".join(str(p) for p in parts)
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in stem)
    return f"{safe[:120]}.pdf"
