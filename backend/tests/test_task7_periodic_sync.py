"""Task 7 regression tests: scheduled quarterly/shareholding sync.

Plainly: the quarterly-results + shareholding update, which previously ran
only when an operator typed a command, now runs itself once a night on a
small batch, always picking the most out-of-date companies first — and it
reuses the exact same code path the operator command used.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 — register every table
from app.data.screener_source import ScreenerFinancials
from app.db.base import Base
from app.domain.platform.jobs import JobKind, JobStatus, policy_for
from app.models.analysis import QuarterlyResult, ShareholdingSnapshot
from app.models.company import Company
from app.models.ingestion import IngestionFailure, IngestionRun
from app.models.platform import BackgroundJob
from app.services.platform.jobs.handlers import handler_for
from app.services.platform.jobs.queue import JobQueue
from app.services.platform.jobs.worker import Worker
from app.services.universe.periodic_backfill import PeriodicBackfillService


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _company(db, ticker, *, category=None):
    row = Company(id=f"cid-{ticker}", ticker=ticker, name=f"{ticker} Ltd",
                  exchange="NSE", listing_status="active",
                  market_cap_category=category)
    db.add(row)
    db.commit()
    return row


def _quarters_payload(ticker, period=(2025, 1)):
    fy, q = period
    return ScreenerFinancials(
        ticker=ticker,
        quarters={
            "Sales": {period: 250.0},
            "OPM %": {period: 20.0},
            "Net Profit": {period: 40.0},
        },
        shareholding={
            "Promoters": {period: 70.0},
            "FIIs": {period: 12.0},
            "DIIs": {period: 10.0},
            "Public": {period: 8.0},
        },
    )


class TestStalestFirstSelection:
    def test_no_quarters_first_then_oldest_newest_period(self, db):
        never = _company(db, "NEVERCO")
        old = _company(db, "OLDCO")
        fresh = _company(db, "FRESHCO")
        db.add_all([
            QuarterlyResult(company_id=old.id, fiscal_year=2023, quarter=4,
                            revenue=100.0),
            QuarterlyResult(company_id=fresh.id, fiscal_year=2025, quarter=4,
                            revenue=100.0),
        ])
        db.commit()

        picked = [c.ticker for c in PeriodicBackfillService(db).targets()]
        assert picked == ["NEVERCO", "OLDCO", "FRESHCO"], (
            "never-covered first, then stalest-first by newest period"
        )

    def test_batch_walks_forward_not_the_same_head(self, db):
        """A bounded batch must advance: after refreshing the stalest
        company, the NEXT batch starts with a different company."""
        for ticker in ("AAA", "BBB", "CCC"):
            c = _company(db, ticker)
            db.add(QuarterlyResult(company_id=c.id, fiscal_year=2022,
                                   quarter=4, revenue=100.0))
        db.commit()

        service = PeriodicBackfillService(db, delay_seconds=0.0,
                                          fetch=lambda t: _quarters_payload(t))
        first = [c.ticker for c in service.targets(limit=1)]
        service.run(companies=service.targets(limit=1), progress=False)
        second = [c.ticker for c in service.targets(limit=1)]

        assert first == ["AAA"]                     # alphabetical tie-break
        assert second == ["BBB"], (
            "the refreshed company must sink to the tail so the next batch "
            "advances through the universe"
        )


class TestHandler:
    def test_end_to_end_through_the_real_worker(self, db, monkeypatch):
        monkeypatch.setattr("app.core.config.settings.DATA_PROVIDER", "real")
        monkeypatch.setattr("app.core.config.settings.PERIODIC_SYNC_BATCH_SIZE", 2)
        _company(db, "AAA")
        _company(db, "BBB")

        monkeypatch.setattr(
            "app.services.universe.periodic_backfill.fetch_screener",
            lambda ticker: _quarters_payload(ticker),
        )
        JobQueue(db).enqueue(JobKind.PERIODIC_SYNC, payload={})
        db.commit()
        worker = Worker(sessionmaker(bind=db.get_bind()), worker_id="t7-worker")
        assert worker.run_once()

        job = db.scalar(select(BackgroundJob).where(
            BackgroundJob.kind == JobKind.PERIODIC_SYNC.value))
        assert job.status == JobStatus.SUCCEEDED.value
        result = job.result
        assert result["attempted"] == 2 and result["succeeded"] == 2
        assert result["quarters_written"] == 2
        assert result["shareholding_written"] == 2
        assert db.scalar(select(func.count()).select_from(QuarterlyResult)) == 2
        assert db.scalar(select(func.count()).select_from(ShareholdingSnapshot)) == 2
        # Failure recording is on (Task 2) — nothing failed, nothing filed.
        assert db.scalar(select(func.count()).select_from(IngestionFailure)) == 0

    def test_failures_are_filed_and_job_backs_off(self, db, monkeypatch):
        monkeypatch.setattr("app.core.config.settings.DATA_PROVIDER", "real")
        _company(db, "BADCO")

        def not_listed(ticker):
            from app.data.screener_source import ScreenerError
            raise ScreenerError("HTTP Error 429: Too Many Requests")

        monkeypatch.setattr(
            "app.services.universe.periodic_backfill.fetch_screener", not_listed)
        JobQueue(db).enqueue(JobKind.PERIODIC_SYNC, payload={})
        db.commit()
        worker = Worker(sessionmaker(bind=db.get_bind()), worker_id="t7-worker")
        assert worker.run_once()

        job = db.scalar(select(BackgroundJob).where(
            BackgroundJob.kind == JobKind.PERIODIC_SYNC.value))
        assert job.status == JobStatus.FAILED.value      # transient → retry
        assert job.run_after is not None                 # bounded backoff set

        failure = db.scalar(select(IngestionFailure))
        assert failure is not None
        assert failure.kind == "periodic_sync"
        assert failure.symbol == "BADCO"
        assert failure.failure_kind == "transient"
        run = db.scalar(select(IngestionRun).where(
            IngestionRun.kind == "periodic_sync"))
        assert run is not None and run.failed == 1

    def test_retry_policy_and_dead_letter_unchanged(self, db, monkeypatch):
        """Exhausting the kind's policy still dead-letters (2 attempts)."""
        monkeypatch.setattr("app.core.config.settings.DATA_PROVIDER", "real")
        _company(db, "DEADCO")

        def always_429(ticker):
            from app.data.screener_source import ScreenerError
            raise ScreenerError("HTTP Error 429: Too Many Requests")

        monkeypatch.setattr(
            "app.services.universe.periodic_backfill.fetch_screener", always_429)
        JobQueue(db).enqueue(JobKind.PERIODIC_SYNC, payload={})
        db.commit()
        worker = Worker(sessionmaker(bind=db.get_bind()), worker_id="t7-dl")
        policy = policy_for(JobKind.PERIODIC_SYNC)
        assert policy.max_attempts == 2

        for attempt in range(policy.max_attempts):
            assert worker.run_once()
            job = db.scalar(select(BackgroundJob).where(
                BackgroundJob.kind == JobKind.PERIODIC_SYNC.value))
            if attempt < policy.max_attempts - 1:
                assert job.status == JobStatus.FAILED.value
                job.run_after = datetime.now(timezone.utc)
                db.commit()
                JobQueue(db).requeue_ready()
        assert job.status == JobStatus.DEAD_LETTER.value

    def test_mock_mode_skips_honestly(self, db, monkeypatch):
        monkeypatch.setattr("app.core.config.settings.DATA_PROVIDER", "mock")
        result = handler_for(JobKind.PERIODIC_SYNC)(db, {})
        assert result["skipped"] is True
        assert "mock" in result["reason"]

    def test_targeted_tickers_mode(self, db, monkeypatch):
        monkeypatch.setattr("app.core.config.settings.DATA_PROVIDER", "real")
        _company(db, "AAA")
        _company(db, "BBB")
        monkeypatch.setattr(
            "app.services.universe.periodic_backfill.fetch_screener",
            lambda ticker: _quarters_payload(ticker),
        )
        result = handler_for(JobKind.PERIODIC_SYNC)(
            db, {"tickers": ["BBB"]})
        assert result["targeted"] is True
        assert result["attempted"] == 1 and result["succeeded"] == 1
        assert db.scalar(select(func.count()).select_from(QuarterlyResult)) == 1


class TestScheduleRegistration:
    def test_env_gated_schedule_registered_and_disableable(self, monkeypatch):
        from app.domain.platform.jobs import JobKind as K
        import app.services.platform.jobs.worker as worker_module

        specs = {s.kind: s for s in worker_module.ALL_SCHEDULES}
        assert K.PERIODIC_SYNC in specs
        assert specs[K.PERIODIC_SYNC].every_seconds == 86_400
        assert specs[K.PERIODIC_SYNC].enabled is True

        # Enabled=False is decided at import from settings; verify the gate
        # logic directly rather than re-importing the module.
        monkeypatch.setattr(
            "app.core.config.settings.PERIODIC_SYNC_INTERVAL_SECONDS", 0)
        assert worker_module._phase1_schedules()[-1].kind == K.PERIODIC_SYNC or \
            any(s.kind == K.PERIODIC_SYNC
                for s in worker_module._phase1_schedules())
        gated = [s for s in worker_module._phase1_schedules()
                 if s.kind == K.PERIODIC_SYNC][0]
        assert gated.enabled is False, "interval 0 must disable the schedule"

    def test_idempotent_enqueue_deduplicates(self, db, monkeypatch):
        monkeypatch.setattr("app.core.config.settings.DATA_PROVIDER", "real")
        queue = JobQueue(db)
        first = queue.enqueue(JobKind.PERIODIC_SYNC, payload={})
        second = queue.enqueue(JobKind.PERIODIC_SYNC, payload={})
        assert first.id == second.id
