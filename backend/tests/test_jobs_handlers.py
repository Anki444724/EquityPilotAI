"""Integration and unit tests for background job handlers (handlers.py).

Uses mocked service boundaries to execute each handler directly, verifying
successful delegation, early returns, error isolation, and idempotency.
"""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch
from sqlalchemy.orm import Session
from pathlib import Path

from app.domain.platform.jobs import JobKind
from app.models.platform import Notification
from app.models.document import Document
from app.models.portfolio import Portfolio
from app.services.platform.jobs.handlers import (
    handler_for,
    handle_report_generation,
    handle_document_processing,
    handle_embedding,
    handle_notification,
    handle_portfolio_refresh,
    handle_alert_evaluation,
    handle_usage_rollup,
    handle_backup,
    handle_retention_sweep,
    handle_filing_crawl,
    handle_filing_post_process,
    handle_ai_score_refresh,
    handle_embedding_backfill,
    handle_quality_refresh,
    handle_ir_discovery,
    handle_memory_enrichment,
    handle_financials_backfill,
)

@pytest.fixture
def mock_db():
    return MagicMock(spec=Session)


def test_handler_for_raises_on_unknown():
    with pytest.raises(KeyError):
        handler_for("unknown-job-kind")
    assert handler_for(JobKind.BACKUP) == handle_backup


@patch("app.services.reports.service.ReportService")
def test_handle_report_generation(mock_report_service, mock_db):
    payload = {
        "company_id": "comp-1",
        "owner_id": "user-1",
        "formats": ["html"],
        "theme": "light",
        "tenant_id": 1,
        "report_type": "institutional",
    }
    
    mock_result = MagicMock()
    mock_result.report.id = 123
    mock_result.report.version = 1
    mock_result.cached = False
    mock_result.report.section_count = 10
    mock_result.report.build_ms = 450.0
    mock_report_service.return_value.generate.return_value = mock_result

    with patch("app.services.platform.entitlements.EntitlementService") as mock_entitlement:
        res = handle_report_generation(mock_db, payload)
        assert res["report_id"] == 123
        assert res["version"] == 1
        assert res["cached"] is False
        assert res["sections"] == 10
        assert res["build_ms"] == 450.0
        assert mock_entitlement.called


def test_handle_document_processing_early_returns(mock_db):
    # Case 1: Document missing
    mock_db.get.return_value = None
    res = handle_document_processing(mock_db, {"document_id": 999})
    assert res["skipped"] is True
    assert "no longer exists" in res["reason"]

    # Case 2: Already processed
    mock_doc = MagicMock(spec=Document)
    mock_doc.status = "completed"
    mock_db.get.return_value = mock_doc
    res = handle_document_processing(mock_db, {"document_id": 123})
    assert res["skipped"] is True
    assert "already processed" in res["reason"]


@patch("app.services.documents.service.DocumentService")
def test_handle_document_processing_executes(mock_doc_service, mock_db, tmp_path):
    mock_doc = MagicMock(spec=Document)
    mock_doc.status = "processing"
    mock_db.get.return_value = mock_doc

    spool_file = tmp_path / "upload.pdf"
    spool_file.write_bytes(b"pdf-content")

    payload = {
        "document_id": 123,
        "spool_path": str(spool_file),
        "company_name": "Test Co",
        "company_ticker": "TEST",
        "tenant_id": 1,
    }

    mock_processed = MagicMock()
    mock_processed.status = "ready"
    mock_processed.page_count = 5
    mock_processed.chunk_count = 15
    mock_doc_service.return_value.process.return_value = mock_processed

    with patch("app.services.platform.entitlements.EntitlementService") as mock_entitlement:
        res = handle_document_processing(mock_db, payload)
        assert res["document_id"] == 123
        assert res["status"] == "ready"
        assert res["pages"] == 5
        assert res["chunks"] == 15
        assert not spool_file.exists()


@patch("app.services.documents.service.DocumentService")
def test_handle_embedding(mock_doc_service, mock_db):
    mock_doc_service.return_value.reindex.return_value = 10
    res = handle_embedding(mock_db, {"company_id": "comp-1"})
    assert res["company_id"] == "comp-1"
    assert res["chunks_embedded"] == 10


def test_handle_notification_skips(mock_db):
    # Case 1: notification missing
    mock_db.get.return_value = None
    res = handle_notification(mock_db, {"notification_id": 1})
    assert res["skipped"] is True

    # Case 2: already sent
    mock_notif = MagicMock(spec=Notification)
    mock_notif.sent_at = "already-sent"
    mock_db.get.return_value = mock_notif
    res = handle_notification(mock_db, {"notification_id": 2})
    assert res["skipped"] is True


@patch("app.services.platform.email.EmailService")
def test_handle_notification_sends(mock_email, mock_db):
    mock_notif = MagicMock(spec=Notification)
    mock_notif.sent_at = None
    mock_notif.channel = "email"
    mock_notif.subject = "Sub"
    mock_notif.body = "Body"
    mock_db.get.return_value = mock_notif

    res = handle_notification(mock_db, {"notification_id": 12, "to": "test@test.com"})
    assert res["notification_id"] == 12
    assert res["channel"] == "email"
    assert mock_notif.delivery_status == "sent"
    assert mock_notif.sent_at is not None
    assert mock_email.return_value.send.called


@patch("app.services.portfolio.service.PortfolioService")
def test_handle_portfolio_refresh(mock_portfolio_service, mock_db):
    # Empty portfolios does nothing
    mock_db.get.return_value = None
    res = handle_portfolio_refresh(mock_db, {"portfolio_ids": [1]})
    assert res["snapshotted"] == 0

    # Successful snapshot
    mock_portfolio = MagicMock(spec=Portfolio)
    mock_portfolio.id = 5
    mock_db.get.return_value = mock_portfolio

    res = handle_portfolio_refresh(mock_db, {"portfolio_ids": [5]})
    assert res["snapshotted"] == 1
    assert mock_portfolio_service.return_value.record_snapshot.called


@patch("app.services.portfolio.service.PortfolioService")
def test_handle_alert_evaluation(mock_portfolio_service, mock_db):
    mock_portfolio = MagicMock(spec=Portfolio)
    mock_portfolio.id = 1
    mock_db.scalars.return_value = [mock_portfolio]

    mock_eval = MagicMock()
    mock_eval.is_triggered = True
    mock_portfolio_service.return_value.evaluate_alerts.return_value = [mock_eval]

    res = handle_alert_evaluation(mock_db, {})
    assert res["portfolios"] == 1
    assert res["alerts"] == 1


def test_handle_usage_rollup(mock_db):
    mock_sub = MagicMock()
    mock_sub.tenant_id = 1
    mock_sub.period_start = "2026-08-01"
    mock_db.scalars.side_effect = [
        [mock_sub], # subscriptions
        [] # counters (empty)
    ]
    res = handle_usage_rollup(mock_db, {})
    assert res["counters_checked"] == 0


@patch("app.services.platform.backup.BackupService")
def test_handle_backup(mock_backup_service, mock_db):
    mock_record = MagicMock()
    mock_record.id = 45
    mock_record.location = "S3"
    mock_record.size_bytes = 1024
    mock_record.checksum = "md5"
    mock_backup_service.return_value.create.return_value = mock_record

    res = handle_backup(mock_db, {})
    assert res["backup_id"] == 45
    assert res["location"] == "S3"


@patch("app.services.platform.audit_service.AuditService")
@patch("app.services.platform.identity_service.IdentityService")
@patch("app.services.platform.jobs.queue.JobQueue")
@patch("app.services.platform.observability.MetricsService")
def test_handle_retention_sweep(mock_metrics, mock_queue, mock_identity, mock_audit, mock_db):
    mock_metrics.return_value.purge.return_value = 5
    mock_audit.return_value.purge.return_value = 10
    mock_queue.return_value.purge_completed.return_value = 2
    mock_identity.return_value.purge_expired_tokens.return_value = {"tokens": 12}

    res = handle_retention_sweep(mock_db, {})
    assert res["metrics_deleted"] == 5
    assert res["audit_deleted"] == 10
    assert res["jobs_deleted"] == 2
    assert res["tokens_deleted"] == 12


@patch("app.services.filings.collector.FilingCollector")
def test_handle_filing_crawl(mock_collector, mock_db):
    mock_collector.return_value.crawl_due.return_value = {"crawled": 5}
    res = handle_filing_crawl(mock_db, {"max_companies": 10})
    assert res["crawled"] == 5

    # Target tickers — resolved through the canonical resolver, not
    # Session.scalar (which picks an arbitrary row when duplicates exist).
    mock_company = MagicMock()
    mock_company.ticker = "TCS"
    mock_collector.return_value.crawl_company.return_value.as_dict.return_value = {"ticker": "TCS"}

    with patch("app.services.universe.resolution.resolve_company",
               return_value=mock_company):
        res = handle_filing_crawl(mock_db, {"tickers": ["TCS"]})
    assert res["companies"] == 1
    assert res["results"][0]["ticker"] == "TCS"


@patch("app.services.filings.post_filing.documents_awaiting_post_processing")
@patch("app.services.filings.post_filing.PostFilingProcessor")
def test_handle_filing_post_process(mock_processor, mock_documents, mock_db):
    mock_row = MagicMock()
    mock_row.company_id = "comp-1"
    mock_row.document_id = 10
    mock_documents.return_value = [mock_row]

    mock_outcome = MagicMock()
    mock_outcome.as_dict.return_value = {"processed": True}
    mock_processor.return_value.run.return_value = mock_outcome

    res = handle_filing_post_process(mock_db, {})
    assert res["processed"] == 1
    assert mock_row.status == "completed"


@patch("app.services.ai_scoring.service.AIScoringService")
def test_handle_ai_score_refresh(mock_service, mock_db):
    mock_service.return_value.score_universe.return_value = {"mode": "sweep", "count": 4}
    res = handle_ai_score_refresh(mock_db, {})
    assert res["mode"] == "sweep"
    assert res["count"] == 4

    # Targeted
    mock_company = MagicMock()
    mock_company.ticker = "TCS"
    mock_db.execute.return_value.scalars.return_value.all.return_value = [mock_company]

    mock_result = MagicMock()
    mock_result.overall_score = 85.0
    mock_result.rating.value = "buy"
    mock_outcome = MagicMock()
    mock_outcome.version.version = 1
    mock_outcome.created = True
    mock_outcome.delta = 1.2
    mock_service.return_value.score_and_record.return_value = (mock_result, mock_outcome)

    res = handle_ai_score_refresh(mock_db, {"company_ids": ["comp-1"]})
    assert res["mode"] == "targeted"
    assert res["results"][0]["ticker"] == "TCS"
    assert res["results"][0]["score"] == 85.0


@patch("app.services.retrieval.backfill.EmbeddingBackfillService")
def test_handle_embedding_backfill(mock_service, mock_db):
    mock_service.return_value.run.return_value.as_dict.return_value = {"completed": True}
    res = handle_embedding_backfill(mock_db, {})
    assert res["completed"] is True


@patch("app.services.universe.financials_backfill.FinancialsBackfillService")
def test_handle_financials_backfill(mock_service, mock_db):
    """Delegates to the service (never reimplementing the sweep) and reports
    coverage read back from the database, with the run bounded by the payload
    limit and progress disabled inside the worker."""

    mock_coverage = {"companies": 500, "with_financials": 490,
                     "without_financials": 10, "coverage_pct": 98.0,
                     "by_category": {"largecap": {"total": 100, "covered": 99}}}

    mock_report = MagicMock()
    mock_report.outcomes = MagicMock()
    mock_report.succeeded = MagicMock()
    mock_report.failed = MagicMock()
    mock_report.reasons.return_value = {"screener: HTTP 404": 2}
    mock_report.outcomes.__len__ = lambda self: 10
    mock_report.succeeded.__len__ = lambda self: 8
    mock_report.failed.__len__ = lambda self: 2
    mock_report.had_transient_failures = False

    instance = mock_service.return_value
    instance.coverage_snapshot.side_effect = [mock_coverage, mock_coverage]
    instance.run.return_value = mock_report

    res = handle_financials_backfill(mock_db, {"limit": 25})

    instance.run.assert_called_once_with(limit=25, progress=False)
    assert res["attempted"] == 10
    assert res["succeeded"] == 8
    assert res["failed"] == 2
    assert res["coverage_before"]["companies"] == 500
    assert res["coverage_after"] is mock_coverage
    assert res["failure_reasons"] == {"screener: HTTP 404": 2}
    assert res["targeted"] is False
    assert res["limit"] == 25


@patch("app.services.universe.financials_backfill.FinancialsBackfillService")
def test_handle_financials_backfill_defaults_to_a_25_company_sweep(
        mock_service, mock_db):
    """The scheduled job (payload carries only {"scheduled": True}) must be
    bounded to 25 companies — never an unbounded full-universe sweep."""

    mock_report = MagicMock()
    mock_report.outcomes.__len__ = lambda self: 25
    mock_report.succeeded.__len__ = lambda self: 25
    mock_report.failed.__len__ = lambda self: 0
    mock_report.reasons.return_value = {}
    mock_report.had_transient_failures = False

    instance = mock_service.return_value
    instance.coverage_snapshot.return_value = {
        "companies": 0, "with_financials": 0, "without_financials": 0,
        "coverage_pct": 0.0, "by_category": {},
    }
    instance.run.return_value = mock_report

    res = handle_financials_backfill(mock_db, {"scheduled": True})

    instance.run.assert_called_once_with(limit=25, progress=False)
    assert res["limit"] == 25
    assert res["targeted"] is False


@patch("app.services.universe.financials_backfill.FinancialsBackfillService")
def test_handle_financials_backfill_targets_tickers_ignoring_the_batch_limit(
        mock_service, mock_db):
    """A targeted run resolves exact tickers from the database and is NOT
    bounded by the sweep's 25-company limit."""

    mock_target = MagicMock()
    mock_target.ticker = "NHPC"
    mock_service.return_value.companies_by_tickers.return_value = [mock_target]

    mock_report = MagicMock()
    mock_report.outcomes.__len__ = lambda self: 1
    mock_report.succeeded.__len__ = lambda self: 1
    mock_report.failed.__len__ = lambda self: 0
    mock_report.reasons.return_value = {}
    mock_report.had_transient_failures = False
    mock_service.return_value.run.return_value = mock_report
    mock_service.return_value.coverage_snapshot.return_value = {
        "companies": 0, "with_financials": 0, "without_financials": 0,
        "coverage_pct": 0.0, "by_category": {},
    }

    res = handle_financials_backfill(
        mock_db, {"tickers": ["NHPC"], "limit": 999})

    mock_service.return_value.companies_by_tickers.assert_called_once_with(["NHPC"])
    # `run` is called with the resolved targets, not with a limit — so a
    # targeted ingestion is never truncated by the 25-company batching.
    mock_service.return_value.run.assert_called_once_with(
        targets=[mock_target], progress=False)
    assert res["targeted"] is True
    assert res["tickers"] == ["NHPC"]
    assert res["missing_tickers"] == []


@patch("app.services.universe.financials_backfill.FinancialsBackfillService")
def test_handle_financials_backfill_reports_missing_targeted_tickers(
        mock_service, mock_db):
    """A ticker absent from the database is reported, not silently ignored."""

    mock_service.return_value.companies_by_tickers.return_value = []

    mock_report = MagicMock()
    mock_report.outcomes.__len__ = lambda self: 0
    mock_report.succeeded.__len__ = lambda self: 0
    mock_report.failed.__len__ = lambda self: 0
    mock_report.reasons.return_value = {}
    mock_report.had_transient_failures = False
    mock_service.return_value.run.return_value = mock_report
    mock_service.return_value.coverage_snapshot.return_value = {
        "companies": 0, "with_financials": 0, "without_financials": 0,
        "coverage_pct": 0.0, "by_category": {},
    }

    res = handle_financials_backfill(mock_db, {"tickers": ["ZZZZ"]})
    assert res["missing_tickers"] == ["ZZZZ"]
    mock_service.return_value.run.assert_called_once_with(targets=[], progress=False)


@patch("app.services.universe.financials_backfill.FinancialsBackfillService")
def test_handle_financials_backfill_raises_on_transient_failures(
        mock_service, mock_db):
    """Transient provider failures surface as a raised job failure so the
    worker's bounded RetryPolicy runs, instead of being swallowed inside the
    run."""

    mock_report = MagicMock()
    mock_report.outcomes.__len__ = lambda self: 25
    mock_report.succeeded.__len__ = lambda self: 24
    mock_report.failed.__len__ = lambda self: 1
    mock_report.reasons.return_value = {"screener: HTTP 429": 1}
    mock_report.had_transient_failures = True
    mock_report.transient_failures = [MagicMock()]
    mock_service.return_value.run.return_value = mock_report
    mock_service.return_value.coverage_snapshot.return_value = {
        "companies": 0, "with_financials": 0, "without_financials": 0,
        "coverage_pct": 0.0, "by_category": {},
    }

    from app.services.universe.financials_backfill import TransientIngestionFailure
    with pytest.raises(TransientIngestionFailure) as exc:
        handle_financials_backfill(mock_db, {"scheduled": True})
    assert exc.value.transient == 1
    assert exc.value.attempted == 25


def test_financials_backfill_is_registered_everywhere():
    """JOB-001 guard: a kind missing from any registry raises on enqueue."""
    from app.domain.platform.jobs import (
        DEFAULT_PRIORITY, JOB_LABELS, RETRY_POLICIES, SCHEDULES, JobKind,
    )
    from app.services.platform.jobs.handlers import handler_for

    kind = JobKind.FINANCIALS_BACKFILL
    assert kind in JOB_LABELS
    assert kind in DEFAULT_PRIORITY
    assert kind in RETRY_POLICIES
    assert handler_for(kind) is handle_financials_backfill
    assert any(s.kind == kind for s in SCHEDULES)


@patch("app.services.quality.service.QualitySnapshotService")
def test_handle_quality_refresh(mock_service, mock_db):
    mock_service.return_value.refresh_all.return_value = {"quality": 100}
    res = handle_quality_refresh(mock_db, {})
    assert res["quality"] == 100


@patch("app.services.filings.ir_discovery.IRDiscoveryService")
def test_handle_ir_discovery(mock_service, mock_db):
    mock_service.return_value.run.return_value.as_dict.return_value = {"discovered": 3}
    res = handle_ir_discovery(mock_db, {})
    assert res["discovered"] == 3


@patch("app.services.knowledge.enrichment.MemoryEnrichmentService")
def test_handle_memory_enrichment(mock_service, mock_db):
    # Single company
    mock_service.return_value.run.return_value.as_dict.return_value = {"enriched": "yes"}
    res = handle_memory_enrichment(mock_db, {"company_id": "comp-1"})
    assert res["companies"] == 1
    assert res["results"][0]["enriched"] == "yes"

    # Sweep
    with patch("app.services.knowledge.enrichment.companies_needing_enrichment") as mock_targets:
        mock_targets.return_value = ["comp-2"]
        res = handle_memory_enrichment(mock_db, {})
        assert res["companies"] == 1
