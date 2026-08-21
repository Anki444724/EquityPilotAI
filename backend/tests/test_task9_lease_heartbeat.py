"""Task 9 regression tests: lease heartbeat for long-running sync jobs.

Plainly: a big quarterly/shareholding or financial-refresh batch can take
longer than the job system's 5-minute "is this worker still alive?" lease.
Previously the system could then hand the SAME job to another worker while
the first was still working. Now the worker re-news ("heartbeats") the
lease while those two kinds of job run, and stops heartbeating the moment
the job finishes or fails.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401 — register every table
from app.db.base import Base
from app.domain.platform.jobs import JobKind, JobStatus, policy_for
from app.models.company import Company
from app.models.platform import BackgroundJob
from app.services.platform.jobs.queue import JobQueue
from app.services.platform.jobs.worker import LONG_RUNNING_KINDS, Worker


@pytest.fixture()
def db(tmp_path):
    # File-backed on purpose: the heartbeat thread, the handler and the test
    # probes each open their OWN connection, which a shared StaticPool
    # connection would illegally interleave. SQLite file locking plus a
    # generous busy timeout gives the real cross-connection semantics.
    path = Path(tmp_path) / "heartbeat.db"
    engine = create_engine(
        f"sqlite+pysqlite:///{path}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _job(db, kind: JobKind, payload: dict | None = None) -> BackgroundJob:
    job = JobQueue(db).enqueue(kind, payload=payload or {})
    db.commit()
    return job


def _make_worker(db, *, lease_seconds: int, interval: float) -> Worker:
    return Worker(
        sessionmaker(bind=db.get_bind()),
        worker_id="t9-worker",
        lease_seconds=lease_seconds,
        heartbeat_interval=interval,
    )



def _fresh(db):
    """A fresh session for asserting PERSISTED state — the test's own session
    may hold a cached identity-map object from enqueue time."""
    return sessionmaker(bind=db.get_bind())()

class TestLeaseRenewal:
    def test_long_periodic_job_renews_its_lease(self, db, monkeypatch):
        """PERIODIC_SYNC runs longer than the initial lease; the lease is
        renewed while it runs and the job is NOT reaped mid-flight."""
        monkeypatch.setattr("app.core.config.settings.DATA_PROVIDER", "real")
        db.add(Company(id="c1", ticker="SLOWCO", name="Slow Ltd",
                       exchange="NSE", listing_status="active"))
        db.commit()
        job = _job(db, JobKind.PERIODIC_SYNC, payload={"tickers": ["SLOWCO"]})

        snapshots = []

        def slow_fetch(ticker):
            # Capture the live lease at entry and again after the initial
            # lease has elapsed — proving renewal happened mid-run.
            probe = sessionmaker(bind=db.get_bind())()
            snapshots.append(probe.get(BackgroundJob, job.id).lease_expires_at)
            time.sleep(1.0)
            probe = sessionmaker(bind=db.get_bind())()
            snapshots.append(probe.get(BackgroundJob, job.id).lease_expires_at)
            probe.close()
            from app.data.screener_source import ScreenerFinancials
            return ScreenerFinancials(ticker=ticker)

        monkeypatch.setattr(
            "app.services.universe.periodic_backfill.fetch_screener", slow_fetch)

        worker = _make_worker(db, lease_seconds=2, interval=0.2)
        assert worker.run_once()

        assert snapshots[1] > snapshots[0], "lease must be renewed mid-run"
        row = _fresh(db).get(BackgroundJob, job.id)
        assert row.status == JobStatus.SUCCEEDED.value
        assert row.attempts == 1, "nobody else should have claimed it"

    def test_long_financial_job_renews_its_lease(self, db, monkeypatch):
        """FINANCIALS_BACKFILL runs longer than the initial lease and is
        renewed rather than reaped."""
        monkeypatch.setattr("app.core.config.settings.DATA_PROVIDER", "real")
        job = _job(db, JobKind.FINANCIALS_BACKFILL, payload={"limit": 1})

        def slow_run(self, targets=None, *, limit=None, progress=True):
            probe = sessionmaker(bind=self.db.get_bind())()
            before = probe.get(BackgroundJob, job.id).lease_expires_at
            time.sleep(0.8)
            after = probe.get(BackgroundJob, job.id).lease_expires_at
            probe.close()
            assert after > before, "lease must be renewed mid-run"

            class _Report:
                outcomes = []
                @property
                def succeeded(self):
                    return []
                @property
                def failed(self):
                    return []
                @property
                def had_transient_failures(self):
                    return False
                def reasons(self):
                    return {}
            return _Report()

        from app.services.universe.financials_backfill import (
            FinancialsBackfillService,
        )
        monkeypatch.setattr(FinancialsBackfillService, "run", slow_run)

        worker = _make_worker(db, lease_seconds=2, interval=0.2)
        assert worker.run_once()
        assert _fresh(db).get(BackgroundJob, job.id).status == \
            JobStatus.SUCCEEDED.value

    def test_expiring_lease_is_not_reaped_while_heartbeating(self, db, monkeypatch):
        """The exact Task-8 risk: a run longer than the initial lease must
        not be reclaimed by the reaper."""
        monkeypatch.setattr("app.core.config.settings.DATA_PROVIDER", "real")
        db.add(Company(id="c2", ticker="SLOW2", name="Slow2 Ltd",
                       exchange="NSE", listing_status="active"))
        db.commit()
        job = _job(db, JobKind.PERIODIC_SYNC, payload={"tickers": ["SLOW2"]})

        def slow_fetch(ticker):
            time.sleep(1.4)   # > initial lease of 1s
            from app.data.screener_source import ScreenerFinancials
            return ScreenerFinancials(ticker=ticker)

        monkeypatch.setattr(
            "app.services.universe.periodic_backfill.fetch_screener", slow_fetch)

        worker = _make_worker(db, lease_seconds=1, interval=0.2)

        # Reaper races the handler from the "outside" mid-run.
        reaped = []

        def reaper():
            time.sleep(0.7)
            probe = sessionmaker(bind=db.get_bind())()
            reaped.append(JobQueue(probe).reap_expired_leases())
            probe.close()

        thread = threading.Thread(target=reaper)
        thread.start()
        assert worker.run_once()
        thread.join(timeout=5)

        assert reaped == [0], "the live job must not be reclaimed"
        row = _fresh(db).get(BackgroundJob, job.id)
        assert row.status == JobStatus.SUCCEEDED.value
        assert row.attempts == 1


class TestCleanStop:
    def test_no_heartbeat_thread_left_after_success_or_failure(self, db, monkeypatch):
        monkeypatch.setattr("app.core.config.settings.DATA_PROVIDER", "real")
        db.add(Company(id="c3", ticker="DONE", name="Done Ltd",
                       exchange="NSE", listing_status="active"))
        db.commit()
        ok_job = _job(db, JobKind.PERIODIC_SYNC, payload={"tickers": ["DONE"]})

        monkeypatch.setattr(
            "app.services.universe.periodic_backfill.fetch_screener",
            lambda ticker: __import__("app.data.screener_source",
                                      fromlist=["ScreenerFinancials"]).ScreenerFinancials(ticker=ticker),
        )
        worker = _make_worker(db, lease_seconds=600, interval=0.05)
        assert worker.run_once()
        assert not any(t.name == f"lease-heartbeat-{ok_job.id}"
                       for t in threading.enumerate()), "success must stop the heartbeat"

        # A failing handler must stop it too. The failure must be TRANSIENT
        # (a 429): permanent errors are recorded per-company and the job
        # legitimately succeeds with failed=1 — only transient errors
        # escalate to job failure via TransientIngestionFailure.
        def boom(ticker):
            from app.data.screener_source import ScreenerError
            raise ScreenerError("HTTP Error 429: Too Many Requests")

        monkeypatch.setattr(
            "app.services.universe.periodic_backfill.fetch_screener", boom)
        bad_job = _job(db, JobKind.PERIODIC_SYNC, payload={"tickers": ["DONE"]})
        assert worker.run_once()
        assert not any(t.name == f"lease-heartbeat-{bad_job.id}"
                       for t in threading.enumerate()), "failure must stop the heartbeat"
        assert _fresh(db).get(BackgroundJob, bad_job.id).status == \
            JobStatus.FAILED.value

    def test_completed_job_cannot_be_renewed_afterwards(self, db, monkeypatch):
        """After the outcome is written, extend_lease refuses — renewals are
        dead the moment the job stops running."""
        monkeypatch.setattr("app.core.config.settings.DATA_PROVIDER", "real")
        job = _job(db, JobKind.FINANCIALS_BACKFILL, payload={"limit": 1})

        from app.services.universe.financials_backfill import (
            FinancialsBackfillService,
        )

        class _Report:
            outcomes = []
            succeeded = []
            failed = []
            had_transient_failures = False
            def reasons(self):
                return {}

        monkeypatch.setattr(
            FinancialsBackfillService, "run",
            lambda self, targets=None, *, limit=None, progress=True: _Report())
        worker = _make_worker(db, lease_seconds=600, interval=0.05)
        assert worker.run_once()
        assert JobQueue(db).extend_lease(job.id, "t9-worker") is False


class TestPoliciesUnchanged:
    def test_retry_dead_letter_unchanged_for_heartbeat_kinds(self, db, monkeypatch):
        """Exhausting the kind's policy still dead-letters; the heartbeat
        neither weakens nor strengthens retries."""
        monkeypatch.setattr("app.core.config.settings.DATA_PROVIDER", "real")

        def always_429(payload_dict):
            from app.services.universe.financials_backfill import (
                TransientIngestionFailure,
            )
            raise TransientIngestionFailure(transient=1, attempted=1)

        job = _job(db, JobKind.FINANCIALS_BACKFILL, payload={"limit": 1})
        worker = _make_worker(db, lease_seconds=600, interval=0.05)

        from app.services.universe.financials_backfill import (
            FinancialsBackfillService,
        )
        with patch.object(FinancialsBackfillService, "run", always_429):
            policy = policy_for(JobKind.FINANCIALS_BACKFILL)
            for attempt in range(policy.max_attempts):
                assert worker.run_once()
                row = db.get(BackgroundJob, job.id)
                if attempt < policy.max_attempts - 1:
                    assert row.status == JobStatus.FAILED.value
                    row.run_after = datetime.now(timezone.utc) - timedelta(seconds=1)
                    db.commit()
                    JobQueue(db).requeue_ready()
        row = _fresh(db).get(BackgroundJob, job.id)
        assert row.status == JobStatus.DEAD_LETTER.value
        assert row.attempts == policy.max_attempts

    def test_deduplication_unchanged(self, db):
        queue = JobQueue(db)
        first = queue.enqueue(JobKind.PERIODIC_SYNC, payload={"scheduled": True})
        second = queue.enqueue(JobKind.PERIODIC_SYNC, payload={"scheduled": True})
        third = queue.enqueue(JobKind.FINANCIALS_BACKFILL, payload={"limit": 5})
        fourth = queue.enqueue(JobKind.FINANCIALS_BACKFILL, payload={"limit": 5})
        assert first.id == second.id
        assert third.id == fourth.id
        assert first.id != third.id

    def test_short_jobs_do_not_start_a_heartbeat(self, db, monkeypatch):
        """Non-long-running kinds keep the plain claim lease — the allowlist
        stays exactly PERIODIC_SYNC and FINANCIALS_BACKFILL."""
        assert LONG_RUNNING_KINDS == frozenset(
            {JobKind.PERIODIC_SYNC, JobKind.FINANCIALS_BACKFILL})
