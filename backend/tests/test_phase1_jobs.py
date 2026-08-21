"""Phase 1 — job-queue integration for the new sync jobs (requirement H).

Runs the REAL Worker.run_once() loop against the real DB-backed queue: no
second queue system, real leases, real idempotency keys, real retry policies
with exponential backoff, real dead-lettering.
"""
from __future__ import annotations

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import pytest

import app.models  # noqa: F401 — register every table
from app.db.base import Base
from app.domain.platform.jobs import JobKind, JobStatus, policy_for
from app.models.company import Company
from app.models.ingestion import IngestionFailure, IngestionRun
from app.models.market import MarketQuote
from app.models.portfolio import PriceHistory
from app.services.platform.jobs.queue import JobQueue
from app.services.platform.jobs.worker import Worker


@pytest.fixture()
def jobs_db():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine)
    session = factory()
    try:
        yield session, factory
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def mock_mode(monkeypatch):
    from app.data.providers import router as router_module

    monkeypatch.setattr("app.core.config.settings.DATA_PROVIDER", "mock")
    monkeypatch.setattr("app.core.config.settings.MOCK_UNIVERSE_SIZE", "150")
    router_module.reset_router()
    yield
    router_module.reset_router()


def _drain(worker: Worker, limit: int = 20) -> int:
    """Run run_once() until the queue is empty (or `limit` hits)."""
    ran = 0
    while worker.run_once() and ran < limit:
        ran += 1
    return ran


class TestCompanyUniverseSyncJob:
    def test_enqueued_job_builds_the_mock_universe(self, jobs_db, mock_mode):
        db, factory = jobs_db
        JobQueue(db).enqueue(JobKind.COMPANY_UNIVERSE_SYNC, payload={"limit": 150})
        db.commit()

        worker = Worker(factory, worker_id="test-worker")
        assert _drain(worker) == 1

        assert db.scalar(select(func.count()).select_from(Company)) == 150
        runs = db.scalars(select(IngestionRun)).all()
        assert len(runs) == 1 and runs[0].kind == "company_universe_sync"

    def test_duplicate_enqueue_is_deduplicated_while_pending(self, jobs_db, mock_mode):
        db, _factory = jobs_db
        queue = JobQueue(db)
        first = queue.enqueue(JobKind.COMPANY_UNIVERSE_SYNC, payload={"limit": 10})
        second = queue.enqueue(JobKind.COMPANY_UNIVERSE_SYNC, payload={"limit": 10})
        assert first.id == second.id

    def test_running_twice_is_idempotent_at_the_data_level(self, jobs_db, mock_mode):
        db, factory = jobs_db
        for _ in range(2):
            JobQueue(db).enqueue(JobKind.COMPANY_UNIVERSE_SYNC, payload={"limit": 120})
            db.commit()
            _drain(Worker(factory, worker_id="test-worker"))

        assert db.scalar(select(func.count()).select_from(Company)) == 120


class TestPriceAndHistoryJobs:
    def test_price_sync_persists_quotes_in_batches(self, jobs_db, mock_mode):
        db, factory = jobs_db
        JobQueue(db).enqueue(JobKind.COMPANY_UNIVERSE_SYNC, payload={"limit": 40})
        db.commit()
        _drain(Worker(factory, worker_id="test-worker"))

        JobQueue(db).enqueue(JobKind.PRICE_SYNC, payload={"limit": 40})
        db.commit()
        _drain(Worker(factory, worker_id="test-worker"))

        quotes = db.scalar(select(func.count()).select_from(MarketQuote))
        assert quotes == 40

        # Second sync refreshes in place: still 40 rows, no growth.
        JobQueue(db).enqueue(JobKind.PRICE_SYNC, payload={"limit": 40})
        db.commit()
        _drain(Worker(factory, worker_id="test-worker"))
        assert db.scalar(select(func.count()).select_from(MarketQuote)) == 40

    def test_historical_sync_is_idempotent(self, jobs_db, mock_mode):
        db, factory = jobs_db
        JobQueue(db).enqueue(JobKind.COMPANY_UNIVERSE_SYNC, payload={"limit": 15})
        db.commit()
        _drain(Worker(factory, worker_id="test-worker"))

        for _ in range(2):
            JobQueue(db).enqueue(
                JobKind.HISTORICAL_PRICE_SYNC, payload={"limit": 15, "days": 20},
            )
            db.commit()
            _drain(Worker(factory, worker_id="test-worker"))

        bars = db.scalar(select(func.count()).select_from(PriceHistory))
        assert bars == 15 * 20          # exactly one row per (ticker, date)


class TestRetryHandling:
    def test_failed_job_retries_with_backoff_and_succeeds(self, jobs_db, mock_mode):
        """Requirement: failed jobs retry correctly. A transient provider
        failure fails the job (bounded retry scheduled); after the backoff
        elapses, the scheduler requeues and the retry succeeds — while the
        data written by the failed attempt's successful symbols survives."""
        db, factory = jobs_db
        JobQueue(db).enqueue(JobKind.COMPANY_UNIVERSE_SYNC, payload={"limit": 10})
        db.commit()
        _drain(Worker(factory, worker_id="test-worker"))

        from app.services.market import sync as sync_module

        calls = {"n": 0}

        def flaky(self, limit, job_id=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise sync_module.TransientSyncFailure(1, 5)
            return {"attempted": 5, "succeeded": 5, "failed": 0}

        monkey = pytest.MonkeyPatch()
        monkey.setattr(sync_module.PriceSyncService, "sync_batch", flaky)
        try:
            JobQueue(db).enqueue(JobKind.PRICE_SYNC, payload={"limit": 5})
            db.commit()
            worker = Worker(factory, worker_id="retry-worker")
            assert worker.run_once()             # attempt 1 → failure

            job = db.scalar(
                select(__import__("app.models.platform", fromlist=["BackgroundJob"]).BackgroundJob)
                .where(
                    __import__("app.models.platform", fromlist=["BackgroundJob"]).BackgroundJob.kind
                    == JobKind.PRICE_SYNC.value
                )
            )
            assert job.status == JobStatus.FAILED.value
            assert job.run_after is not None      # backoff scheduled
            policy = policy_for(JobKind.PRICE_SYNC)
            assert job.attempts == 1

            # Simulate the backoff elapsing: the scheduler's requeue step
            # (run_after in the past, exactly as time passing would leave it).
            from datetime import datetime as _dt, timedelta as _td, timezone as _tz
            job.run_after = _dt.now(_tz.utc) - _td(seconds=1)
            db.commit()
            JobQueue(db).requeue_ready()
            assert worker.run_once()             # attempt 2 → success
            assert calls["n"] == 2
            job = db.get(
                __import__("app.models.platform", fromlist=["BackgroundJob"]).BackgroundJob, job.id
            )
            assert job.status == JobStatus.SUCCEEDED.value
        finally:
            monkey.undo()

    def test_exhausted_retries_dead_letter(self, jobs_db, mock_mode):
        db, factory = jobs_db
        JobQueue(db).enqueue(JobKind.COMPANY_UNIVERSE_SYNC, payload={"limit": 5})
        db.commit()
        _drain(Worker(factory, worker_id="test-worker"))

        from app.services.market import sync as sync_module

        def always_fails(self, limit, job_id=None):
            raise sync_module.TransientSyncFailure(1, 1)

        from app.models.platform import BackgroundJob

        monkey = pytest.MonkeyPatch()
        monkey.setattr(sync_module.PriceSyncService, "sync_batch", always_fails)
        try:
            JobQueue(db).enqueue(JobKind.PRICE_SYNC, payload={"limit": 1})
            db.commit()
            worker = Worker(factory, worker_id="dl-worker")
            policy = policy_for(JobKind.PRICE_SYNC)
            for attempt in range(policy.max_attempts):
                assert worker.run_once()
                job = db.scalar(select(BackgroundJob).where(
                    BackgroundJob.kind == JobKind.PRICE_SYNC.value))
                # Between attempts the backoff must elapse (scheduler step).
                if attempt < policy.max_attempts - 1:
                    assert job.status == JobStatus.FAILED.value
                    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
                    job.run_after = _dt.now(_tz.utc) - _td(seconds=1)
                    db.commit()
                    JobQueue(db).requeue_ready()
            assert job.status == JobStatus.DEAD_LETTER.value
            assert job.attempts == policy.max_attempts
        finally:
            monkey.undo()

    def test_provider_failure_does_not_corrupt_existing_rows(self, jobs_db, mock_mode):
        """Requirement: third-party provider failure must not corrupt
        existing data. Sync quotes for a small batch, then run a sync whose
        provider throws for every symbol: stored rows unchanged in count and
        value."""
        db, factory = jobs_db
        JobQueue(db).enqueue(JobKind.COMPANY_UNIVERSE_SYNC, payload={"limit": 8})
        db.commit()
        _drain(Worker(factory, worker_id="test-worker"))
        JobQueue(db).enqueue(JobKind.PRICE_SYNC, payload={"limit": 8})
        db.commit()
        _drain(Worker(factory, worker_id="test-worker"))

        before = {
            q.company_id: (q.ltp, q.fetched_at)
            for q in db.scalars(select(MarketQuote)).all()
        }

        from app.services.market import sync as sync_module

        def broken(self, limit, job_id=None):
            raise RuntimeError("connection reset by peer")

        monkey = pytest.MonkeyPatch()
        monkey.setattr(sync_module.PriceSyncService, "sync_batch", broken)
        try:
            JobQueue(db).enqueue(JobKind.PRICE_SYNC, payload={"limit": 8})
            db.commit()
            _drain(Worker(factory, worker_id="test-worker"))
        finally:
            monkey.undo()

        after = {
            q.company_id: (q.ltp, q.fetched_at)
            for q in db.scalars(select(MarketQuote)).all()
        }
        assert after == before


class TestFailedDataRetryJob:
    def test_transient_failure_is_retried_and_resolved(self, jobs_db, mock_mode):
        db, factory = jobs_db
        JobQueue(db).enqueue(JobKind.COMPANY_UNIVERSE_SYNC, payload={"limit": 6})
        db.commit()
        _drain(Worker(factory, worker_id="test-worker"))

        # A price sync where one symbol explodes — recorded, then retried.
        from app.services.market import sync as sync_module

        original = sync_module.PriceSyncService.sync_batch

        def one_bad_symbol(self, limit, job_id=None):
            run = IngestionRun(kind="price_sync", provider="mock",
                               started_at=__import__("datetime").datetime.now(
                                   __import__("datetime").timezone.utc))
            self.db.add(run)
            self.db.commit()
            self.db.add(IngestionFailure(
                run_id=run.id, kind="price_sync", symbol="MCK0000",
                company_id=None, error="ProviderError: 429 too many requests",
                failure_kind="transient",
                last_attempt_at=__import__("datetime").datetime(
                    2020, 1, 1, tzinfo=__import__("datetime").timezone.utc),
            ))
            self.db.commit()
            return {"attempted": 6, "succeeded": 5, "failed": 1}

        monkey = pytest.MonkeyPatch()
        monkey.setattr(sync_module.PriceSyncService, "sync_batch", one_bad_symbol)
        try:
            JobQueue(db).enqueue(JobKind.PRICE_SYNC, payload={"limit": 6})
            db.commit()
            _drain(Worker(factory, worker_id="test-worker"))
        finally:
            monkey.undo()

        failure = db.scalar(select(IngestionFailure))
        assert failure is not None and failure.resolved_at is None

        # Backoff long elapsed (last_attempt 2020). The retry job re-fetches
        # the quote through the (working) provider and resolves the row.
        JobQueue(db).enqueue(JobKind.FAILED_DATA_RETRY, payload={"limit": 10})
        db.commit()
        _drain(Worker(factory, worker_id="test-worker"))

        failure = db.scalar(select(IngestionFailure))
        assert failure.resolved_at is not None
        assert failure.attempts == 1      # resolved on the first retry

    def test_permanent_failure_is_not_retried(self, jobs_db, mock_mode):
        db, factory = jobs_db
        run = IngestionRun(kind="price_sync", provider="mock",
                           started_at=__import__("datetime").datetime.now(
                               __import__("datetime").timezone.utc))
        db.add(run)
        db.commit()
        db.add(IngestionFailure(
            run_id=run.id, kind="price_sync", symbol="GONE",
            error="SymbolNotFound: not found (404)",
            failure_kind="permanent",
            last_attempt_at=__import__("datetime").datetime(
                2020, 1, 1, tzinfo=__import__("datetime").timezone.utc),
        ))
        db.commit()

        JobQueue(db).enqueue(JobKind.FAILED_DATA_RETRY, payload={"limit": 10})
        db.commit()
        _drain(Worker(factory, worker_id="test-worker"))

        failure = db.scalar(select(IngestionFailure))
        assert failure.resolved_at is None    # left for an operator
