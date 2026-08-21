"""Task 5 regression tests: refresh cooldown + batch-retry skip.

Plainly:
- Cooldown — a company we fetched recently is left alone for a while, even
  if it is still waiting for new results. An old one is still picked up, and
  a company becomes pickable again once the cooldown has passed.
- Batch-retry skip — when a refresh batch partly succeeds and then hits a
  temporary failure, the automatic retry re-fetches ONLY the companies that
  still need it, never the ones that already succeeded.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 — register every table
from app.data.ingest import ingest_company
from app.data.screener_source import ScreenerFinancials
from app.db.base import Base
from app.domain.platform.jobs import JobKind, JobStatus
from app.models.company import Company, FinancialFact
from app.models.platform import BackgroundJob
from app.services.platform.jobs.queue import JobQueue
from app.services.platform.jobs.worker import Worker
from app.services.universe.financials_backfill import (
    FinancialsBackfillService, current_fiscal_year,
)

STALE_YEARS = (2022, 2023)          # always older than the current Indian FY


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


def _payload(ticker, years=STALE_YEARS):
    return ScreenerFinancials(
        ticker=ticker, fiscal_years=list(years),
        profit_loss={
            "Sales +": {y: 1000.0 + i * 10 for i, y in enumerate(years)},
            "Net Profit +": {y: 100.0 for y in years},
        },
        balance_sheet={"Equity Capital": {y: 100.0 for y in years}},
        cash_flow={}, price=50.0,
    )


def _ingest_old(db, ticker, *, fetched_hours_ago):
    """Ingest through the real pipeline, then age the freshness stamp."""
    with patch("app.data.ingest.fetch_screener", return_value=_payload(ticker)):
        result = ingest_company(db, ticker, f"{ticker} Ltd", "T", "T",
                                with_yahoo=False)
    assert result.ok
    company = db.scalar(select(Company).where(Company.ticker == ticker))
    stamp = datetime.now(timezone.utc) - timedelta(hours=fetched_hours_ago)
    db.query(FinancialFact).filter(
        FinancialFact.company_id == company.id,
    ).update({FinancialFact.fetched_at: stamp})
    db.commit()
    return company


class TestRefreshCooldown:
    def test_recently_fetched_company_is_excluded(self, db):
        _ingest_old(db, "JUSTDONE", fetched_hours_ago=1)      # stale FY, fresh fetch
        _ingest_old(db, "LONGAGO", fetched_hours_ago=25)      # stale FY, old fetch
        service = FinancialsBackfillService(db, delay_seconds=0.0)

        picked = [t.ticker for t in service.companies_with_stale_financials()]
        assert "JUSTDONE" not in picked, "company fetched 1h ago must be skipped"
        assert "LONGAGO" in picked

    def test_old_and_unknown_fetch_times_stay_eligible(self, db):
        old = _ingest_old(db, "OLDFETCH", fetched_hours_ago=25)
        service = FinancialsBackfillService(db, delay_seconds=0.0)
        assert [t.ticker for t in service.companies_with_stale_financials()] == ["OLDFETCH"]

        # Rows written before Phase 1 carry no fetched_at: absence means
        # "not fetched recently", never "fresh".
        db.query(FinancialFact).filter(
            FinancialFact.company_id == old.id,
        ).update({FinancialFact.fetched_at: None})
        db.commit()
        assert [t.ticker for t in service.companies_with_stale_financials()] == ["OLDFETCH"]

    def test_company_becomes_eligible_after_cooldown(self, db):
        company = _ingest_old(db, "WAITING", fetched_hours_ago=30)
        service = FinancialsBackfillService(db, delay_seconds=0.0)
        assert [t.ticker for t in service.companies_with_stale_financials()] == ["WAITING"]

        # Fetched 1h ago: skipped under the default 20h window…
        db.query(FinancialFact).filter(
            FinancialFact.company_id == company.id,
        ).update({FinancialFact.fetched_at: datetime.now(timezone.utc) - timedelta(hours=1)})
        db.commit()
        assert service.companies_with_stale_financials() == []

        # …and picked up again once the window is shortened past that age,
        # or disabled entirely (cooldown_hours=0 / payload override).
        assert [t.ticker for t in service.companies_with_stale_financials(
            cooldown_hours=0.5)] == ["WAITING"]
        assert [t.ticker for t in service.companies_with_stale_financials(
            cooldown_hours=0)] == ["WAITING"]

    def test_default_comes_from_settings(self, db, monkeypatch):
        _ingest_old(db, "SETME", fetched_hours_ago=3)
        service = FinancialsBackfillService(db, delay_seconds=0.0)

        monkeypatch.setattr(
            "app.core.config.settings.FINANCIAL_REFRESH_COOLDOWN_HOURS", 20.0)
        assert service.companies_with_stale_financials() == []
        monkeypatch.setattr(
            "app.core.config.settings.FINANCIAL_REFRESH_COOLDOWN_HOURS", 0.0)
        assert [t.ticker for t in service.companies_with_stale_financials()] == ["SETME"]


class TestBatchRetrySkip:
    def test_retry_does_not_refetch_succeeded_company(self, db, monkeypatch):
        """A refresh batch: GOODCO succeeds, FLAKYCO hits a 429. The job
        fails, backs off, retries — and the retry fetches ONLY FLAKYCO.
        GOODCO's success stamped fetched_at=now (even though its facts were
        unchanged), which the cooldown excludes on the retry."""
        monkeypatch.setattr("app.core.config.settings.DATA_PROVIDER", "real")
        _ingest_old(db, "GOODCO", fetched_hours_ago=30)
        _ingest_old(db, "FLAKYCO", fetched_hours_ago=30)

        calls = {"GOODCO": 0, "FLAKYCO": 0}

        def feed(ticker):
            calls[ticker] += 1
            if ticker == "FLAKYCO" and calls["FLAKYCO"] == 1:
                from app.data.screener_source import ScreenerError
                raise ScreenerError("HTTP Error 429: Too Many Requests")
            return _payload(ticker)

        from app.services.platform.jobs.handlers import handler_for

        with patch("app.data.ingest.fetch_screener", side_effect=feed):
            JobQueue(db).enqueue(
                JobKind.FINANCIALS_BACKFILL,
                payload={"mode": "refresh", "limit": 10},
            )
            db.commit()
            factory = sessionmaker(bind=db.get_bind())
            worker = Worker(factory, worker_id="t5-worker")

            assert worker.run_once()                     # attempt 1 → FAILED
            job = db.scalar(select(BackgroundJob).where(
                BackgroundJob.kind == JobKind.FINANCIALS_BACKFILL.value))
            assert job.status == JobStatus.FAILED.value
            assert job.run_after is not None             # backoff scheduled
            assert calls == {"GOODCO": 1, "FLAKYCO": 1}

            job.run_after = datetime.now(timezone.utc) - timedelta(seconds=1)
            db.commit()
            JobQueue(db).requeue_ready()
            assert worker.run_once()                     # attempt 2 → SUCCEEDED

        job = db.get(BackgroundJob, job.id)
        assert job.status == JobStatus.SUCCEEDED.value
        assert job.attempts == 2
        # THE assertion: the succeeded company fetched exactly once across
        # both attempts; only the failed one was re-fetched.
        assert calls == {"GOODCO": 1, "FLAKYCO": 2}

    def test_handler_still_raises_on_transient_failure(self, db, monkeypatch):
        """The batch-retry skip must not weaken failure escalation: a batch
        with a transient provider error still fails the job for a bounded
        retry (that retry is what the skip then narrows)."""
        monkeypatch.setattr("app.core.config.settings.DATA_PROVIDER", "real")
        _ingest_old(db, "SOLOCO", fetched_hours_ago=30)

        from app.data.screener_source import ScreenerError
        from app.services.platform.jobs.handlers import handler_for

        def flaky(ticker):
            raise ScreenerError("HTTP Error 429: Too Many Requests")

        with patch("app.data.ingest.fetch_screener", side_effect=flaky):
            with pytest.raises(Exception) as excinfo:
                handler_for(JobKind.FINANCIALS_BACKFILL)(
                    db, {"mode": "refresh", "limit": 5},
                )
        assert "transient" in str(excinfo.value).lower()

    def test_existing_dead_letter_behaviour_unchanged(self, db, monkeypatch):
        """A job whose kind retry policy is exhausted still dead-letters."""
        monkeypatch.setattr("app.core.config.settings.DATA_PROVIDER", "real")
        from app.services.platform.jobs.handlers import handler_for

        def always_fails(payload_dict):
            from app.services.universe.financials_backfill import (
                TransientIngestionFailure,
            )
            raise TransientIngestionFailure(transient=1, attempted=1)

        with patch.object(
            __import__("app.services.universe.financials_backfill",
                       fromlist=["FinancialsBackfillService"]),
            "FinancialsBackfillService", "run", always_fails,
        ), patch("app.data.ingest.fetch_screener"):
            JobQueue(db).enqueue(JobKind.FINANCIALS_BACKFILL, payload={"limit": 1})
            db.commit()
            worker = Worker(sessionmaker(bind=db.get_bind()), worker_id="t5-dl")
            from app.domain.platform.jobs import policy_for
            policy = policy_for(JobKind.FINANCIALS_BACKFILL)
            for attempt in range(policy.max_attempts):
                assert worker.run_once()
                job = db.scalar(select(BackgroundJob).where(
                    BackgroundJob.kind == JobKind.FINANCIALS_BACKFILL.value))
                if attempt < policy.max_attempts - 1:
                    assert job.status == JobStatus.FAILED.value
                    job.run_after = datetime.now(timezone.utc) - timedelta(seconds=1)
                    db.commit()
                    JobQueue(db).requeue_ready()
            assert job.status == JobStatus.DEAD_LETTER.value
            assert job.attempts == policy.max_attempts
