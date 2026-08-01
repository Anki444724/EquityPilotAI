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

    max_companies = int(payload.get("max_companies", 25))
    max_downloads = int(payload.get("max_downloads_per_company", 5))
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


HANDLERS: dict[JobKind, Handler] = {
    JobKind.REPORT_GENERATION: handle_report_generation,
    JobKind.DOCUMENT_PROCESSING: handle_document_processing,
    JobKind.EMBEDDING: handle_embedding,
    JobKind.NOTIFICATION: handle_notification,
    JobKind.PORTFOLIO_REFRESH: handle_portfolio_refresh,
    JobKind.FILING_CRAWL: handle_filing_crawl,
    JobKind.FILING_POST_PROCESS: handle_filing_post_process,
    JobKind.ALERT_EVALUATION: handle_alert_evaluation,
    JobKind.USAGE_ROLLUP: handle_usage_rollup,
    JobKind.BACKUP: handle_backup,
    JobKind.RETENTION_SWEEP: handle_retention_sweep,
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
