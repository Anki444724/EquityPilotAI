"""What each job kind actually does.

A handler is a plain function of `(db, payload) -> dict`. It returns a result
dictionary or raises; the worker translates that into a queue transition. No
handler touches the queue itself, which is what lets every one of them be
tested by calling it directly.

Two rules the handlers all follow.

**They call the existing services.** Report generation calls
`ReportService.generate`, exactly as the synchronous endpoint does. The
background path must not become a second implementation that drifts from the
foreground one — that is how "it works in the UI but not in the queue" starts.

**They are idempotent where they can be.** A retried job re-checks the state
it was going to create. Re-running a completed document ingestion should be a
no-op, not a duplicate.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.domain.platform.jobs import JobKind
from app.services.platform.observability import get_logger

log = get_logger("ierp.jobs")

Handler = Callable[[Session, dict[str, Any]], dict[str, Any]]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ===========================================================================
# Report generation
# ===========================================================================
def handle_report_generation(db: Session, payload: dict[str, Any]) -> dict[str, Any]:
    """Generate a report off the request path.

    Module 9 already builds reports synchronously in roughly two seconds. That
    is tolerable for a quick report and not for a deep research report with
    every format rendered, which is what this exists for.
    """
    from app.domain.reports.blocks import ReportType, Theme
    from app.services.reports.renderers.base import OutputFormat
    from app.services.reports.service import ReportService

    company_id = payload["company_id"]
    report_type = ReportType(payload.get("report_type", "institutional"))
    formats = [OutputFormat(f) for f in payload.get("formats", ["html", "pdf"])]

    result = ReportService(db).generate(
        company_id,
        report_type,
        owner_id=payload["owner_id"],
        formats=formats,
        theme=Theme(payload.get("theme", "light")),
        analyst=payload.get("analyst", ""),
        portfolio_id=payload.get("portfolio_id"),
        include_ai=payload.get("include_ai", True),
        use_cache=payload.get("use_cache", True),
    )

    # Meter it only now that it exists — see `EntitlementService.consume`.
    _consume(db, payload, "reports_generated", resource_type="report",
             resource_id=result.report.id)

    return {
        "report_id": result.report.id,
        "version": result.report.version,
        "cached": result.cached,
        "sections": result.report.section_count,
        "build_ms": result.report.build_ms,
    }


# ===========================================================================
# Document processing
# ===========================================================================
def handle_document_processing(db: Session, payload: dict[str, Any]) -> dict[str, Any]:
    """Run Module 7's ingestion pipeline for one document.

    `DocumentService.process(document_id, payload_bytes)` needs the raw file,
    and Module 7 deliberately does not store it — only the extracted text,
    tables, entities and facts. So the upload endpoint spools the bytes to a
    temporary file and the job carries the path; the spool is deleted once the
    pipeline has consumed it.

    Idempotent: a document already `ready` is skipped rather than
    re-extracted, so a retry after a worker crash does not double the facts.
    """
    from pathlib import Path

    from app.models.document import Document
    from app.services.documents.service import DocumentService

    document_id = int(payload["document_id"])
    document = db.get(Document, document_id)
    if document is None:
        return {"skipped": True, "reason": "document no longer exists"}
    if document.status in {"completed", "ready"} and not payload.get("force"):
        return {"skipped": True, "reason": "already processed"}

    spool = Path(payload.get("spool_path") or "")
    if not spool.is_file():
        # The bytes are gone and cannot be reconstructed. Fail loudly rather
        # than marking the document processed with nothing in it.
        raise FileNotFoundError(
            f"spooled upload for document {document_id} is missing: {spool}"
        )

    raw = spool.read_bytes()
    processed = DocumentService(db).process(
        document_id,
        raw,
        company_name=payload.get("company_name"),
        company_ticker=payload.get("company_ticker"),
    )
    spool.unlink(missing_ok=True)

    _consume(db, payload, "documents_processed",
             resource_type="document", resource_id=document_id)
    pages = int(getattr(processed, "page_count", 0) or 0)
    if pages:
        _consume(db, payload, "document_pages", quantity=pages,
                 resource_type="document", resource_id=document_id)

    return {
        "document_id": document_id,
        "status": getattr(processed, "status", "ready"),
        "pages": pages,
        "chunks": int(getattr(processed, "chunk_count", 0) or 0),
    }


# ===========================================================================
# Embeddings
# ===========================================================================
def handle_embedding(db: Session, payload: dict[str, Any]) -> dict[str, Any]:
    """Re-embed stored chunks.

    Split from ingestion because extraction is the part a user waits for and
    embedding is the part that makes search work a few seconds later. Failing
    the embedding must not fail the upload.

    Module 7 already exposes exactly this operation as
    `DocumentService.reindex(company_id)` — re-embedding from stored chunks
    with no re-parsing. Calling it rather than reimplementing the loop keeps
    the embedding path single-sourced.
    """
    from app.services.documents.service import DocumentService

    company_id = payload.get("company_id")
    embedded = DocumentService(db).reindex(company_id)
    return {"company_id": company_id, "chunks_embedded": embedded}


# ===========================================================================
# Notifications
# ===========================================================================
def handle_notification(db: Session, payload: dict[str, Any]) -> dict[str, Any]:
    """Deliver a queued notification.

    With no SMTP host configured the console transport records the message and
    logs it, so every flow that depends on an email — verification, reset,
    magic link — works end to end in development without a mail server.
    """
    from app.models.platform import Notification
    from app.services.platform.email import EmailService

    notification_id = int(payload["notification_id"])
    notification = db.get(Notification, notification_id)
    if notification is None:
        return {"skipped": True, "reason": "notification no longer exists"}
    if notification.sent_at is not None:
        return {"skipped": True, "reason": "already sent"}

    if notification.channel == "email":
        EmailService().send(
            to=payload.get("to") or "",
            subject=notification.subject,
            body=notification.body,
        )

    notification.sent_at = _utcnow()
    notification.delivery_status = "sent"
    db.commit()
    return {"notification_id": notification_id, "channel": notification.channel}


# ===========================================================================
# Scheduled portfolio update
# ===========================================================================
def handle_portfolio_refresh(db: Session, payload: dict[str, Any]) -> dict[str, Any]:
    """Revalue portfolios and write a dated snapshot.

    Module 8 makes the point that a price history cannot be reconstructed
    after the fact. A daily snapshot is what turns a book into a track record,
    and it only exists if something writes it every day.
    """
    from app.models.portfolio import Portfolio
    from app.services.portfolio.service import PortfolioService

    service = PortfolioService(db)
    ids = payload.get("portfolio_ids")
    if ids:
        portfolios = [db.get(Portfolio, int(i)) for i in ids]
    else:
        portfolios = list(db.scalars(
            select(Portfolio).where(Portfolio.is_active.is_(True))
        ))

    snapshotted, failed = 0, 0
    for portfolio in portfolios:
        if portfolio is None:
            continue
        try:
            # `record_snapshot` is idempotent per date, so a job that runs
            # twice in a day updates one row rather than writing two.
            service.record_snapshot(portfolio.id, as_of=date.today())
            snapshotted += 1
        except Exception as exc:  # noqa: BLE001 — one bad book must not stop the run
            failed += 1
            log.warning(
                "portfolio snapshot failed",
                portfolio_id=portfolio.id, error=str(exc),
            )

    return {"portfolios": len(portfolios), "snapshotted": snapshotted, "failed": failed}


# ===========================================================================
# Alert evaluation
# ===========================================================================
def handle_alert_evaluation(db: Session, payload: dict[str, Any]) -> dict[str, Any]:
    """Run Module 8's alert rules across every active portfolio."""
    from app.models.portfolio import Portfolio
    from app.services.portfolio.service import PortfolioService

    service = PortfolioService(db)
    triggered, evaluated, failed = 0, 0, 0

    for portfolio in db.scalars(
        select(Portfolio).where(Portfolio.is_active.is_(True))
    ):
        try:
            # Returns a list of AlertEvaluation, one per rule per subject.
            # Only those that actually fired are of interest here.
            evaluations = service.evaluate_alerts(portfolio.id)
            evaluated += 1
            triggered += sum(1 for e in evaluations if e.is_triggered)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            log.warning(
                "alert evaluation failed",
                portfolio_id=portfolio.id, error=str(exc),
            )

    return {"portfolios": evaluated, "alerts": triggered, "failed": failed}


# ===========================================================================
# Usage roll-up
# ===========================================================================
def handle_usage_rollup(db: Session, payload: dict[str, Any]) -> dict[str, Any]:
    """Reconcile the usage counters against the raw events.

    The counters are incremented in line with each consumption, so they should
    already be right. This recomputes them from the events and reports any
    drift — a counter that disagrees with its own evidence is a billing
    dispute waiting to happen, and it is far better to find it here.
    """
    from sqlalchemy import func

    from app.models.platform import Subscription, UsageCounter, UsageEvent

    repaired, checked = 0, 0
    for sub in db.scalars(select(Subscription)):
        for counter in db.scalars(
            select(UsageCounter).where(
                UsageCounter.tenant_id == sub.tenant_id,
                UsageCounter.period_start == sub.period_start,
            )
        ):
            actual = db.scalar(
                select(func.coalesce(func.sum(UsageEvent.quantity), 0)).where(
                    UsageEvent.tenant_id == counter.tenant_id,
                    UsageEvent.quota == counter.quota,
                    func.date(UsageEvent.occurred_at) >= counter.period_start,
                    func.date(UsageEvent.occurred_at) < counter.period_end,
                )
            ) or 0
            checked += 1
            if int(actual) != counter.used:
                log.warning(
                    "usage counter drift",
                    tenant_id=counter.tenant_id, quota=counter.quota,
                    counter=counter.used, events=int(actual),
                )
                counter.used = int(actual)
                repaired += 1
    if repaired:
        db.commit()
    return {"counters_checked": checked, "counters_repaired": repaired}


# ===========================================================================
# Backup
# ===========================================================================
def handle_backup(db: Session, payload: dict[str, Any]) -> dict[str, Any]:
    from app.services.platform.backup import BackupService

    record = BackupService(db).create()
    return {
        "backup_id": record.id,
        "location": record.location,
        "size_bytes": record.size_bytes,
        "checksum": record.checksum,
    }


# ===========================================================================
# Retention sweep
# ===========================================================================
def handle_retention_sweep(db: Session, payload: dict[str, Any]) -> dict[str, Any]:
    """Delete data past its retention window.

    Three separate windows, because they answer to different masters: metrics
    are operational, audit rows are compliance, and completed jobs are
    housekeeping.
    """
    from app.services.platform.audit_service import AuditService
    from app.services.platform.identity_service import IdentityService
    from app.services.platform.jobs.queue import JobQueue
    from app.services.platform.observability import MetricsService

    metrics = MetricsService(db).purge(older_than_days=settings.METRICS_RETENTION_DAYS)
    audit = AuditService(db).purge(older_than_days=settings.AUDIT_RETENTION_DAYS)
    jobs = JobQueue(db).purge_completed(older_than_days=7)
    tokens = IdentityService(db).purge_expired_tokens()

    return {
        "metrics_deleted": metrics,
        "audit_deleted": audit,
        "jobs_deleted": jobs,
        **{f"{k}_deleted": v for k, v in tokens.items()},
    }


# ===========================================================================
# Registry
# ===========================================================================
# ===========================================================================
# Automated Indian filing collection
# ===========================================================================
def handle_filing_crawl(db: Session, payload: dict[str, Any]) -> dict[str, Any]:
    """Crawl due companies for new filings and ingest what is new.

    Bounded per run. The nightly budget exists because exchanges rate-limit
    and because an unbounded crawl would hold a worker for the better part of
    an hour; companies not reached tonight are simply the longest-waiting
    tomorrow, which is why `due_companies` sorts by staleness.
    """
    from app.services.filings.collector import FilingCollector

    # Coverage budget.
    #
    # The audit measured 161 of 501 companies ever crawled: at 25/day against
    # a 7-day WEEKLY tier, the tail was never reached. Raising the number
    # alone would not fix it — crawl job 528 took 5h20m for 25 companies
    # (~12.8 min each), and that time is almost entirely DOWNLOADS, not
    # discovery. Discovery is one HTTP call per source.
    #
    # So the two are separated. `discover_only` sweeps every due company for
    # new filings and stores the discovery rows; the download budget stays
    # bounded and drains the resulting queue over subsequent passes. That
    # gives full daily COVERAGE without a proportional increase in provider
    # load or worker time.
    max_companies = int(payload.get("max_companies", 260))
    max_downloads = int(payload.get("max_downloads_per_company", 2))
    download = bool(payload.get("download", True))
    tickers = payload.get("tickers") or None

    collector = FilingCollector(db, polite_delay=float(payload.get("delay", 1.0)))

    if tickers:
        # Targeted run, used by the admin panel and by verification.
        from app.models.company import Company

        results = []
        for ticker in tickers:
            company = db.scalar(
                select(Company).where(Company.ticker == str(ticker).upper())
            )
            if company is None:
                continue
            results.append(collector.crawl_company(
                company, download=download, max_downloads=max_downloads,
            ).as_dict())
        return {"companies": len(results), "results": results}

    return collector.crawl_due(
        max_companies=max_companies, download=download,
        max_downloads_per_company=max_downloads,
    )


def handle_filing_post_process(db: Session, payload: dict[str, Any]) -> dict[str, Any]:
    """Rescore and notify for filings that have finished indexing.

    Separate from the crawl because ingestion is asynchronous: a document
    collected at 02:00 may not finish OCR and embedding until 02:20, and the
    rescore has to follow the document rather than the crawl that fetched it.
    """
    from app.domain.filings.collection import CollectionStatus
    from app.services.filings.post_filing import (
        PostFilingProcessor, documents_awaiting_post_processing,
    )

    limit = int(payload.get("limit", 20))
    rows = documents_awaiting_post_processing(db, limit=limit)
    processor = PostFilingProcessor(db)

    processed: list[dict[str, Any]] = []
    for row in rows:
        try:
            # The 'before' snapshot is computed from the database as it stands
            # with the new document already indexed, so it is not a true
            # pre-filing baseline. Recorded honestly as such: the delta this
            # produces is against the last stored score, which is what the
            # notification claims.
            outcome = processor.run(row.company_id, document_id=row.document_id)
            row.status = CollectionStatus.COMPLETED.value
            processed.append(outcome.as_dict())
        except Exception as exc:  # noqa: BLE001 - one filing must not stop the batch
            row.status = CollectionStatus.FAILED.value
            row.error = f"post-processing failed: {exc}"[:500]
            log.exception("post-filing processing failed", filing_id=row.id)
    db.commit()
    return {"processed": len(processed), "results": processed}


# ===========================================================================
# AI score refresh (Scoring Engine 3.0 learning loop)
# ===========================================================================
def handle_ai_score_refresh(db: Session, payload: dict[str, Any]) -> dict[str, Any]:
    """Recalculate the ten-module AI score and append versions where evidence moved.

    Two modes, one code path.

    With ``company_ids`` the handler rescores exactly those companies — this is
    what the post-filing processor enqueues when a document finishes indexing,
    so a new annual report rescores its own company within minutes rather than
    waiting for the nightly sweep.

    Without it, the scheduled sweep walks the active universe. Batched because
    a 500-company pass in one job would hold a worker for minutes in a 1 GB
    container, which is exactly how production was crashed three times before.

    The service declines to write when the input fingerprint is unchanged, so
    a quiet day produces a full scoring pass and zero rows. That is the
    intended behaviour: a version that says nothing new buries the ones that do.
    """
    from app.models.company import Company
    from app.services.ai_scoring.service import AIScoringService

    company_ids = payload.get("company_ids") or []
    trigger = str(payload.get("trigger") or "scheduled")
    limit = int(payload.get("limit", 60))
    document_id = payload.get("document_id")

    service = AIScoringService(db)

    if company_ids:
        companies = list(db.execute(
            select(Company).where(Company.id.in_(list(company_ids)))
        ).scalars().all())
        results = []
        for company in companies:
            try:
                result, outcome = service.score_and_record(
                    company, trigger=trigger,
                    trigger_document_id=document_id,
                )
                results.append({
                    "ticker": company.ticker,
                    "score": round(result.overall_score, 2),
                    "rating": result.rating.value,
                    "version": outcome.version.version if outcome.version else None,
                    "created": outcome.created,
                    "delta": outcome.delta,
                })
            except Exception as exc:  # noqa: BLE001 — one company must not
                # abort the batch
                db.rollback()
                log.warning("ai score refresh failed", ticker=company.ticker,
                            error=str(exc))
                results.append({"ticker": company.ticker, "error": str(exc)[:200]})
        return {"mode": "targeted", "companies": len(companies),
                "results": results}

    # Scheduled sweep. Ordered by the company that has gone longest without a
    # current score, so a run truncated by the batch limit makes progress
    # through the universe rather than rescoring the same head of the list
    # every night — the defect that left 340 of 501 companies never crawled.
    from app.models.scoring import AIScoreVersion

    current = (
        select(AIScoreVersion.company_id, AIScoreVersion.computed_at)
        .where(AIScoreVersion.status == "current")
        .subquery()
    )
    stmt = (
        select(Company)
        .outerjoin(current, current.c.company_id == Company.id)
        .where(Company.listing_status == "active")
        .order_by(current.c.computed_at.asc().nullsfirst())
        .limit(limit)
    )
    companies = list(db.execute(stmt).scalars().all())
    summary = service.score_universe(companies=companies, trigger=trigger)
    summary["mode"] = "sweep"
    return summary


# ===========================================================================
# Embedding backfill
# ===========================================================================
def handle_embedding_backfill(db: Session, payload: dict[str, Any]) -> dict[str, Any]:
    """Embed chunks lacking a semantic vector.

    Scheduled rather than triggered because the event it waits for — a
    provider becoming reachable — produces no signal. A key is added, or an
    exhausted quota resets overnight, and nothing tells the application.
    Polling costs one indexed COUNT and removes the manual step: the corpus
    starts embedding itself within half an hour of credentials appearing.
    """
    from app.services.retrieval.backfill import (
        DEFAULT_LIMIT, EmbeddingBackfillService,
    )

    limit = int(payload.get("limit", DEFAULT_LIMIT))
    return EmbeddingBackfillService(db).run(limit=limit).as_dict()


# ===========================================================================
# Data quality refresh
# ===========================================================================
def handle_quality_refresh(db: Session, payload: dict[str, Any]) -> dict[str, Any]:
    """Rescore data quality across the universe.

    The per-company refresh runs inside memory enrichment, so a document
    arriving updates its company's score immediately. This sweep exists for
    the other direction: a score falls through the passage of time alone as a
    filing ages past its freshness horizon, and nothing would otherwise
    notice.
    """
    from app.services.quality.service import QualitySnapshotService

    limit = payload.get("limit")
    return QualitySnapshotService(db).refresh_all(
        limit=int(limit) if limit else None,
    )


# ===========================================================================
# Investor-relations URL discovery
# ===========================================================================
def handle_ir_discovery(db: Session, payload: dict[str, Any]) -> dict[str, Any]:
    """Find and store IR URLs for companies that have none.

    The audit found `ir_url` NULL for all 501 rows, so the brief's Priority-1
    source contributed nothing and 100% of discovered filings came from NSE.

    Bounded per run because each company costs up to a handful of HTTP probes
    against a third-party site; the remainder is picked up tomorrow, and a
    company whose probe fails is retried only after `ir_url_checked_at` ages,
    not on every pass.
    """
    from app.services.filings.ir_discovery import IRDiscoveryService

    limit = int(payload.get("limit", 40))
    overwrite = bool(payload.get("overwrite", False))
    report = IRDiscoveryService(db).run(limit=limit, overwrite=overwrite)
    return report.as_dict()


# ===========================================================================
# Automatic memory enrichment
# ===========================================================================
def handle_memory_enrichment(db: Session, payload: dict[str, Any]) -> dict[str, Any]:
    """Turn a newly ingested document into permanent company memory.

    This is the job that makes the platform a memory system rather than a
    retrieval system. Before it existed, every memory service had to be
    triggered by hand, and the audit found 163 documents that had been
    ingested since the vault was last built without producing a single vault
    entry.

    Runs here, in the background worker, rather than in the document worker.
    That worker has crashed production three times in a 1 GB container while
    holding a large PDF; LLM summarisation and observation generation on the
    same loop would guarantee a fourth.

    With no `company_id` the job sweeps companies whose documents have outrun
    their memory — the same comparison the audit used — so a lost enqueue is
    self-healing rather than a permanent hole.
    """
    from app.services.knowledge.enrichment import (
        MemoryEnrichmentService, companies_needing_enrichment,
    )

    company_id = payload.get("company_id")
    document_id = payload.get("document_id")
    allow_llm = bool(payload.get("allow_llm", True))

    service = MemoryEnrichmentService(db, allow_llm=allow_llm)

    if company_id:
        result = service.run(company_id, trigger_document_id=document_id)
        return {"companies": 1, "results": [result.as_dict()]}

    limit = int(payload.get("limit", 5))
    targets = companies_needing_enrichment(db, limit=limit)
    results = []
    for target in targets:
        try:
            results.append(service.run(target).as_dict())
        except Exception as exc:  # noqa: BLE001 — one company must not stop the sweep
            db.rollback()
            log.warning("enrichment sweep entry failed",
                        company_id=target, error=str(exc)[:200])
            results.append({"company_id": target, "error": str(exc)[:200]})
    return {"companies": len(results), "results": results}


# ===========================================================================
# Financials backfill
# ===========================================================================
def handle_financials_backfill(db: Session, payload: dict[str, Any]) -> dict[str, Any]:
    """Ingest canonical annual financials for companies that lack them.

    Runs the same `FinancialsBackfillService` that `deploy/backfill_financials.py`
    drives manually, so the scheduled sweep and an on-demand admin run share one
    implementation instead of a second one drifting from the first.

    Two modes:

    * **Targeted** (`payload["tickers"]`) — resolves those exact tickers (e.g.
      ``["NHPC"]``) from the database and ingests only them, so a specific
      company can be ingested before or independently of the universe sweep.
      A targeted run is *not* bounded by the sweep batching limit.
    * **Sweep** (no tickers) — ingests the next uncovered batch. The run is
      bounded by ``payload["limit"]`` or, when absent, the safe default of 25,
      so a scheduled job can never perform an unbounded full-universe sweep.
      The target set is recomputed from the database each run, so a truncated
      run makes progress and the next run picks up the next uncovered batch.

    If any company hit a *transient* provider failure (429/timeout/connection),
    this raises :class:`TransientIngestionFailure` so the worker fails the job
    and the kind's bounded retry policy runs — per-company failures are no
    longer swallowed. Permanent failures (404, no data) never trip the retry.
    Coverage is read back from the database before and after, so the job result
    records real progress rather than the run's own tally.
    """
    from app.services.universe.financials_backfill import (
        DEFAULT_REFRESH_LIMIT, DEFAULT_SWEEP_LIMIT, FinancialsBackfillService,
        TransientIngestionFailure,
    )

    # DATA_PROVIDER=mock swaps the provider, not the pipeline: the same job
    # ingests deterministic synthetic facts through the same upsert path, so
    # the full universe loop is exercisable offline. Real mode is unchanged.
    from app.core.config import settings

    mode = str(payload.get("mode", "coverage")).lower()
    if mode not in ("coverage", "refresh"):
        raise ValueError(f"unknown financials mode '{mode}'")

    if settings.DATA_PROVIDER.lower() == "mock":
        if mode == "refresh":
            # Refresh is a real-provider operation; the mock universe is
            # deterministic, so there is nothing fresher to fetch. Report the
            # no-op honestly rather than simulating a refresh.
            return {
                "skipped": True,
                "reason": "refresh is a real-provider operation; "
                          "DATA_PROVIDER=mock serves deterministic data",
            }
        return _mock_financials_sweep(db, payload)

    tickers = [str(t) for t in (payload.get("tickers") or []) if str(t).strip()]

    service = FinancialsBackfillService(db)
    before = service.coverage_snapshot()

    if tickers:
        targets = service.companies_by_tickers(tickers)
        found = {t.ticker for t in targets}
        missing = [t for t in tickers if t not in found]
        report = service.run(targets=targets, progress=False)
    elif mode == "refresh":
        # Conservative refresh of already-covered companies (Task 2): the
        # same bounded batch, the same throttled ingest — only the target
        # selector differs (stale latest-fiscal-year instead of uncovered).
        # The cooldown (Task 5) is the batch-retry skip: a company that
        # SUCCEEDED in an earlier attempt carries fetched_at = "now", so the
        # job-level retry's re-selection naturally excludes it and fetches
        # only the companies that still need processing. `cooldown_hours` in
        # the payload lets an operator override the setting per run
        # (0 disables the skip for a deliberate force-refresh).
        limit = int(payload.get("limit") or DEFAULT_REFRESH_LIMIT)
        targets = service.companies_with_stale_financials(
            limit=limit,
            cooldown_hours=(
                float(payload["cooldown_hours"])
                if payload.get("cooldown_hours") is not None else None
            ),
        )
        missing = []
        report = service.run(targets=targets, progress=False)
    else:
        limit = int(payload.get("limit") or DEFAULT_SWEEP_LIMIT)
        report = service.run(limit=limit, progress=False)

    after = service.coverage_snapshot()

    result: dict[str, Any] = {
        "mode": mode,
        "attempted": len(report.outcomes),
        "succeeded": len(report.succeeded),
        "failed": len(report.failed),
        "coverage_before": before,
        "coverage_after": after,
        "failure_reasons": report.reasons(),
    }
    if tickers:
        result["targeted"] = True
        result["tickers"] = tickers
        result["missing_tickers"] = missing
    else:
        result["targeted"] = False
        result["limit"] = limit

    if report.had_transient_failures:
        raise TransientIngestionFailure(
            transient=len(report.transient_failures),
            attempted=len(report.outcomes),
        )

    return result


# ===========================================================================
# Hybrid storage replication
# ===========================================================================
def handle_storage_replication(db: Session, payload: dict[str, Any]) -> dict[str, Any]:
    """Copy unreplicated documents to object storage, then assess health.

    The volume remains authoritative throughout: this only ever reads from it.
    Alerts are raised from the same pass that produces the numbers, so the
    dashboard and the notification can never disagree about the state.
    """
    from app.services.documents.replication import ReplicationService
    from app.services.documents.storage_health import StorageHealthService

    service = ReplicationService(db)
    if not service.enabled:
        # Expected until Railway provisions the bucket. Reported rather than
        # treated as a failure, so the job does not dead-letter every ten
        # minutes on a perfectly healthy system.
        return {"skipped": True, "reason": "object storage is not configured"}

    run = service.run(limit=int(payload.get("limit", 25)))
    alerts = StorageHealthService(db).raise_alerts()
    summary = {k: v for k, v in run.as_dict().items() if k != "outcomes"}
    summary["alerts_raised"] = alerts
    return summary


# ===========================================================================
# Phase 1: the 5,000-company universe jobs
# ===========================================================================
def _mock_financials_sweep(db: Session, payload: dict[str, Any]) -> dict[str, Any]:
    """The mock-mode twin of the financials sweep (see handler above).

    Targeted (payload["tickers"]) or the next uncovered batch, exactly like
    the real sweep's shape, but sourcing from the deterministic generator.
    """
    from sqlalchemy import func, select

    from app.data.mock_financials import upsert_mock_financials
    from app.models.company import Company, FinancialFact
    from app.models.ingestion import IngestionRun

    tickers = [str(t) for t in (payload.get("tickers") or []) if str(t).strip()]
    limit = int(payload.get("limit") or 50)

    run = IngestionRun(kind="financials_sync", provider="mock",
                       started_at=datetime.now(timezone.utc))
    db.add(run)
    db.commit()

    covered = select(FinancialFact.company_id).distinct()
    stmt = (
        select(Company)
        .where(
            Company.deleted_at.is_(None),
            Company.listing_status == "active",
            Company.id.not_in(covered),
        )
        .order_by(Company.ticker.asc())
        .limit(limit)
    )
    if tickers:
        companies = db.scalars(
            select(Company).where(Company.ticker.in_(tickers))
        ).all()
    else:
        companies = db.scalars(stmt).all()

    inserted = updated = unchanged = failed = 0
    for company in companies:
        result = upsert_mock_financials(db, company.ticker)
        if result.get("ok"):
            inserted += int(result.get("inserted", 0))
            updated += int(result.get("updated", 0))
            unchanged += int(result.get("unchanged", 0))
        else:
            failed += 1

    remaining = int(db.scalar(
        select(func.count()).select_from(Company).where(
            Company.deleted_at.is_(None),
            Company.listing_status == "active",
            Company.id.not_in(covered),
        )
    ) or 0)

    run.succeeded = len(companies) - failed
    run.failed = failed
    run.finished_at = datetime.now(timezone.utc)
    run.stats = {
        "attempted": len(companies), "inserted": inserted, "updated": updated,
        "unchanged": unchanged, "universe_without_financials": remaining,
    }
    db.commit()
    return {
        "provider": "mock", "attempted": len(companies), "failed": failed,
        "inserted": inserted, "updated": updated, "unchanged": unchanged,
        "universe_without_financials": remaining,
    }


def handle_company_universe_sync(db: Session, payload: dict[str, Any]) -> dict[str, Any]:
    """Upsert the company master in resumable batches.

    Source resolution follows DATA_PROVIDER unless the payload names one, so
    mock mode generates its deterministic universe and never touches an
    exchange master, while real mode walks the NSE+BSE masters. A run resumes
    from the last unfinished run's `next_index`, is bounded by
    `max_batches` × batch size per invocation, and records every failure in
    `ingestion_failures` for the retry job.
    """
    from app.core.config import settings
    from app.services.universe.company_universe import (
        CompanyUniverseService, records_for_source, resolve_source,
    )

    source = resolve_source(payload.get("source"))
    records = records_for_source(source, limit=payload.get("limit"))
    batch_size = int(payload.get("batch_size") or settings.UNIVERSE_SYNC_BATCH_SIZE)
    max_batches = payload.get("max_batches")

    service = CompanyUniverseService(db)
    start_index = payload.get("start_index")
    if start_index is None:
        start_index = service.resume_position()

    report = service.sync(
        records,
        source=source,
        batch_size=batch_size,
        max_batches=int(max_batches) if max_batches is not None else None,
        start_index=int(start_index),
    )
    result = report.as_dict()
    result["completed"] = report.next_index >= len(records)
    if transient := result.get("failed"):
        # A failed row is recorded and skipped, so the job still succeeds;
        # surfacing the count is reporting, not failure.
        result["note"] = f"{transient} row(s) failed and are queued for retry"
    return result


def handle_price_sync(db: Session, payload: dict[str, Any]) -> dict[str, Any]:
    """Refresh persisted quotes for the stalest bounded batch of companies."""
    from app.core.config import settings
    from app.services.market.sync import PriceSyncService, TransientSyncFailure

    limit = int(payload.get("limit") or settings.PRICE_SYNC_BATCH_SIZE)
    try:
        return PriceSyncService(db).sync_batch(limit=limit)
    except TransientSyncFailure:
        # The per-symbol results are already recorded; re-raise so the kind's
        # bounded retry policy runs the remainder sooner than the schedule.
        raise


def handle_historical_price_sync(db: Session, payload: dict[str, Any]) -> dict[str, Any]:
    """Backfill daily OHLCV bars for the next bounded batch."""
    from app.core.config import settings
    from app.services.market.sync import HistoricalPriceSyncService, TransientSyncFailure

    limit = int(payload.get("limit") or settings.HISTORICAL_PRICE_SYNC_BATCH_SIZE)
    days = int(payload.get("days") or settings.PRICE_HISTORY_BACKFILL_DAYS)
    try:
        return HistoricalPriceSyncService(db).sync_batch(limit=limit, days=days)
    except TransientSyncFailure:
        raise


def handle_failed_data_retry(db: Session, payload: dict[str, Any]) -> dict[str, Any]:
    """Re-drive ingestion failures whose backoff has elapsed.

    Permanent failures are skipped (retrying a 404 burns provider quota and
    changes nothing); transient failures and never-attempted rows are re-run
    through the same service that failed them. A row whose attempts exceed
    the budget is left for an operator — visible in the admin console, not
    silently retried forever.
    """
    from app.core.config import settings
    from app.services.market.sync import FailedRetryService

    limit = int(payload.get("limit") or 200)
    max_attempts = int(
        payload.get("max_attempts") or settings.FAILED_RETRY_MAX_ATTEMPTS
    )
    return FailedRetryService(db).run(limit=limit, max_attempts=max_attempts)


HANDLERS: dict[JobKind, Handler] = {
    JobKind.REPORT_GENERATION: handle_report_generation,
    JobKind.DOCUMENT_PROCESSING: handle_document_processing,
    JobKind.EMBEDDING: handle_embedding,
    JobKind.NOTIFICATION: handle_notification,
    JobKind.PORTFOLIO_REFRESH: handle_portfolio_refresh,
    JobKind.STORAGE_REPLICATION: handle_storage_replication,
    JobKind.FILING_CRAWL: handle_filing_crawl,
    JobKind.FILING_POST_PROCESS: handle_filing_post_process,
    JobKind.ALERT_EVALUATION: handle_alert_evaluation,
    JobKind.USAGE_ROLLUP: handle_usage_rollup,
    JobKind.BACKUP: handle_backup,
    JobKind.RETENTION_SWEEP: handle_retention_sweep,
    JobKind.MEMORY_ENRICHMENT: handle_memory_enrichment,
    JobKind.IR_DISCOVERY: handle_ir_discovery,
    JobKind.QUALITY_REFRESH: handle_quality_refresh,
    JobKind.EMBEDDING_BACKFILL: handle_embedding_backfill,
    JobKind.AI_SCORE_REFRESH: handle_ai_score_refresh,
    JobKind.FINANCIALS_BACKFILL: handle_financials_backfill,
    JobKind.COMPANY_UNIVERSE_SYNC: handle_company_universe_sync,
    JobKind.PRICE_SYNC: handle_price_sync,
    JobKind.HISTORICAL_PRICE_SYNC: handle_historical_price_sync,
    JobKind.FAILED_DATA_RETRY: handle_failed_data_retry,
}


def handler_for(kind: JobKind) -> Handler:
    handler = HANDLERS.get(kind)
    if handler is None:
        raise KeyError(f"no handler registered for job kind '{kind}'")
    return handler


def _consume(
    db: Session,
    payload: dict[str, Any],
    quota_key: str,
    *,
    quantity: int = 1,
    resource_type: str | None = None,
    resource_id: Any = None,
) -> None:
    """Meter a completed unit of work, if the job carries a tenant.

    Wrapped in a try: a metering failure must not fail work the customer has
    already received. The roll-up job reconciles any counter that drifts.
    """
    tenant_id = payload.get("tenant_id")
    if not tenant_id:
        return
    try:
        from app.domain.platform.plans import Quota
        from app.services.platform.entitlements import EntitlementService

        EntitlementService(db).consume(
            int(tenant_id), Quota(quota_key), quantity,
            user_id=payload.get("owner_id"),
            resource_type=resource_type,
            resource_id=str(resource_id) if resource_id is not None else None,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("usage metering failed", quota=quota_key, error=str(exc))
