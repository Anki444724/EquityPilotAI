"""Task 2 regression tests: refresh + cache correctness + failure recording.

A. successful financial ingest invalidates the statements cache
B. a stale, covered company is selected and genuinely re-ingested (refresh)
C. a failed financial ingest files an ingestion_failures record
D. a failed quarterly/shareholding pass files an ingestion_failures record
E. existing retry behaviour (job backoff → requeue → success; failed-data
   retry dispatches the new failure kinds) still works
F. an unrelated company's cache entry survives another company's ingest
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 — register every table
from app.data.ingest import ingest_company
from app.data.screener_source import ScreenerError, ScreenerFinancials
from app.db.base import Base
from app.domain.platform.jobs import JobKind, JobStatus
from app.models.analysis import QuarterlyResult, ShareholdingSnapshot
from app.models.company import Company, FinancialFact
from app.models.ingestion import IngestionFailure, IngestionRun
from app.models.platform import BackgroundJob
from app.services.company_service import CompanyService
from app.services.platform.cache import Namespace, cache
from app.services.platform.jobs.queue import JobQueue
from app.services.platform.jobs.worker import Worker
from app.services.universe.financials_backfill import (
    FinancialsBackfillService, current_fiscal_year,
)
from app.services.universe.periodic_backfill import PeriodicBackfillService


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    # The cache service is a process singleton; each test starts cold so a
    # previous test's entries can never satisfy (or fail) an assertion here.
    cache.invalidate(Namespace.STATEMENTS)
    cache.invalidate(Namespace.SEARCH)
    try:
        yield session
    finally:
        session.close()
        engine.dispose()
        cache.invalidate(Namespace.STATEMENTS)
        cache.invalidate(Namespace.SEARCH)


def _screener_financials(ticker="TESTCO", years=(2022, 2023), sales=None):
    """A minimal screener payload; `sales` overrides the Sales series."""
    def series(*values):
        return dict(zip(years, values))

    return ScreenerFinancials(
        ticker=ticker,
        fiscal_years=list(years),
        profit_loss={
            "Sales +": sales or series(1000.0, 1200.0),
            "Expenses +": series(800.0, 950.0),
            "Operating Profit": series(200.0, 250.0),
            "Net Profit +": series(112.5, 146.25),
            "EPS in Rs": series(11.25, 14.6),
        },
        balance_sheet={
            "Equity Capital": series(100.0, 100.0),
            "Reserves": series(500.0, 600.0),
        },
        cash_flow={},
        price=50.0,
    )


def _ingest(db, ticker, payload):
    from unittest.mock import patch

    with patch("app.data.ingest.fetch_screener", return_value=payload):
        return ingest_company(db, ticker, f"{ticker} Ltd", "Test", "Testing",
                              with_yahoo=False)


def _revenue(db, company_id, year):
    return db.scalar(
        select(FinancialFact.value).where(
            FinancialFact.company_id == company_id,
            FinancialFact.line_item == "revenue",
            FinancialFact.fiscal_year == year,
        )
    )


# ---------------------------------------------------------------- A + F
class TestStatementsCacheInvalidation:
    def test_A_reingest_invalidates_and_reloads(self, db):
        first = _ingest(db, "TESTCO", _screener_financials())
        assert first.ok
        company = db.scalar(select(Company).where(Company.ticker == "TESTCO"))

        from app.domain.financials.line_items import LineItem as LI

        svc = CompanyService(db)
        original = svc.load_financials(company.id)
        assert cache.get(Namespace.STATEMENTS, company.id) is not None
        old_revenue = original.get(LI.REVENUE, 2023)

        # New filing arrives: 2023 revenue changes.
        changed = _screener_financials(
            years=(2022, 2023), sales={2022: 1000.0, 2023: 1500.0},
        )
        second = _ingest(db, "TESTCO", changed)
        assert second.ok

        # Cache entry is gone; the next read reflects the new figure.
        assert cache.get(Namespace.STATEMENTS, company.id) is None
        refreshed = svc.load_financials(company.id)
        new_revenue = refreshed.get(LI.REVENUE, 2023)
        assert new_revenue != old_revenue
        assert new_revenue == 1500.0

    def test_F_unrelated_company_cache_survives(self, db):
        _ingest(db, "ALPHA", _screener_financials("ALPHA", years=(2022, 2023)))
        _ingest(db, "BETA", _screener_financials("BETA", years=(2022, 2023)))
        alpha = db.scalar(select(Company).where(Company.ticker == "ALPHA"))
        beta = db.scalar(select(Company).where(Company.ticker == "BETA"))

        svc = CompanyService(db)
        svc.load_financials(alpha.id)
        svc.load_financials(beta.id)
        assert cache.get(Namespace.STATEMENTS, alpha.id) is not None
        assert cache.get(Namespace.STATEMENTS, beta.id) is not None

        # Only ALPHA changes; BETA's warm entry must survive.
        _ingest(db, "ALPHA", _screener_financials(
            "ALPHA", years=(2022, 2023), sales={2022: 1000.0, 2023: 1800.0},
        ))
        assert cache.get(Namespace.STATEMENTS, alpha.id) is None
        assert cache.get(Namespace.STATEMENTS, beta.id) is not None

    def test_enrich_invalidates_too(self, db, monkeypatch):
        from app.data import enrich as enrich_module
        from app.domain.financials.line_items import LineItem as LI

        _ingest(db, "GAMMA", _screener_financials("GAMMA", years=(2022, 2023)))
        gamma = db.scalar(select(Company).where(Company.ticker == "GAMMA"))
        svc = CompanyService(db)
        svc.load_financials(gamma.id)
        assert cache.get(Namespace.STATEMENTS, gamma.id) is not None

        class _FakeYahoo:
            # CASH_AND_BANK is Yahoo-only detail: canonicalise() derives
            # employee_benefit et al from screener's "Expenses +" aggregate,
            # so that item would already exist and enrich would add nothing.
            facts = {LI.CASH_AND_BANK: {2023: 75.0}}

        monkeypatch.setattr(
            enrich_module, "fetch_financials", lambda ticker: _FakeYahoo(),
        )
        result = enrich_module.enrich_company(db, "GAMMA")
        assert result.ok and result.added > 0
        assert cache.get(Namespace.STATEMENTS, gamma.id) is None


# ---------------------------------------------------------------- B
class TestRefreshExistingCompanies:
    def test_B_stale_company_selected_and_reingested(self, db):
        stale = _ingest(db, "STALE", _screener_financials(
            "STALE", years=(2022, 2023),          # current FY is far newer
        ))
        assert stale.ok
        fresh_years = (current_fiscal_year() - 1, current_fiscal_year())
        _ingest(db, "FRESH", _screener_financials("FRESH", years=fresh_years))
        # One stray year: owned by the coverage sweep, not refresh.
        _ingest(db, "STRAY", _screener_financials("STRAY", years=(2023,)))

        stale_co = db.scalar(select(Company).where(Company.ticker == "STALE"))
        service = FinancialsBackfillService(db, delay_seconds=0.0)
        targets = service.companies_with_stale_financials()
        assert [t.ticker for t in targets] == ["STALE"], (
            "refresh must select exactly the covered-but-stale company"
        )
        # And the coverage sweep must NOT pick the covered stale company up.
        assert "STALE" not in [t.ticker for t in service.companies_without_financials()]

        # The company reports its newest year: refresh re-ingests through the
        # SAME ingest path, and the new year lands.
        newer = ScreenerFinancials(
            ticker="STALE", fiscal_years=[2022, 2023, current_fiscal_year()],
            profit_loss={
                "Sales +": {2022: 1000.0, 2023: 1200.0,
                            current_fiscal_year(): 2000.0},
                "Net Profit +": {2022: 112.5, 2023: 146.25,
                                 current_fiscal_year(): 300.0},
            },
            balance_sheet={
                "Equity Capital": {2022: 100.0, 2023: 100.0,
                                   current_fiscal_year(): 100.0},
            },
            cash_flow={}, price=60.0,
        )
        from unittest.mock import patch

        with patch("app.data.ingest.fetch_screener", return_value=newer):
            report = service.run(targets=targets, progress=False)
        assert len(report.succeeded) == 1

        assert _revenue(db, stale_co.id, current_fiscal_year()) == 2000.0
        # Unchanged older rows were left alone (row revision still 1).
        row = db.scalar(select(FinancialFact).where(
            FinancialFact.company_id == stale_co.id,
            FinancialFact.line_item == "revenue",
            FinancialFact.fiscal_year == 2022,
        ))
        assert row.data_version == 1
        # Having reported the current FY, the company leaves the refresh set.
        assert service.companies_with_stale_financials() == []

    def test_refresh_selection_is_bounded(self, db):
        for i in range(40):
            _ingest(db, f"CO{i:02d}", _screener_financials(
                f"CO{i:02d}", years=(2022, 2023),
            ))
        service = FinancialsBackfillService(db, delay_seconds=0.0)
        assert len(service.companies_with_stale_financials(limit=25)) == 25


# ---------------------------------------------------------------- C + D
class TestFailureRecording:
    def test_C_failed_financial_ingest_is_filed(self, db, monkeypatch):
        db.add(Company(id="cid-x", ticker="DEADCO", name="Dead Ltd",
                       exchange="NSE", listing_status="active"))
        db.commit()

        def not_listed(ticker):
            raise ScreenerError("not listed: https://www.screener.in/company/DEADCO/")

        monkeypatch.setattr("app.data.ingest.fetch_screener", not_listed)
        service = FinancialsBackfillService(db, delay_seconds=0.0)
        report = service.run(progress=False)

        assert len(report.failed) == 1
        failure = db.scalar(select(IngestionFailure))
        assert failure is not None
        assert failure.kind == "financials_sync"
        assert failure.symbol == "DEADCO"
        assert failure.company_id == "cid-x"
        assert failure.failure_kind == "permanent"
        assert "not listed" in failure.error
        assert failure.payload["operation"] == "annual"
        assert failure.payload["source"] == "screener.in"

        run = db.scalar(select(IngestionRun).where(
            IngestionRun.kind == "financials_sync"))
        assert run is not None and run.failed == 1 and run.finished_at is not None

    def test_D_failed_periodic_pass_is_filed(self, db, monkeypatch):
        db.add(Company(id="cid-y", ticker="PERCO", name="Periodic Ltd",
                       exchange="NSE", listing_status="active"))
        db.commit()

        def not_listed(ticker):
            raise ScreenerError("429 too many requests")

        # `fetch=` is the service's own injection seam (same pattern as the
        # financials service's `ingest=`); no network is touched.
        report = PeriodicBackfillService(
            db, delay_seconds=0.0, fetch=not_listed,
        ).run(progress=False)
        assert len(report.failed) == 1

        failure = db.scalar(select(IngestionFailure))
        assert failure.kind == "periodic_sync"
        assert failure.symbol == "PERCO"
        assert failure.company_id == "cid-y"
        assert failure.failure_kind == "transient"     # 429 is retryable
        assert failure.payload["operation"] == "quarterly_and_shareholding"

    def test_successful_runs_write_no_failures(self, db):
        _ingest(db, "OKCO", _screener_financials("OKCO", years=(2022, 2023)))
        FinancialsBackfillService(db, delay_seconds=0.0).run(
            targets=[], progress=False)
        assert db.scalar(select(func.count()).select_from(IngestionFailure)) == 0

    def test_periodic_success_path_still_writes(self, db, monkeypatch):
        company = Company(id="cid-z", ticker="QCO", name="Quarterly Ltd",
                          exchange="NSE", listing_status="active")
        db.add(company)
        db.commit()

        payload = ScreenerFinancials(
            ticker="QCO",
            quarters={
                "Sales": {(2024, 1): 250.0, (2024, 2): 260.0},
                "OPM %": {(2024, 1): 20.0, (2024, 2): 21.0},
                "Net Profit": {(2024, 1): 40.0, (2024, 2): 42.0},
            },
            shareholding={
                "Promoters": {(2024, 2): 70.0},
                "FIIs": {(2024, 2): 12.0},
                "DIIs": {(2024, 2): 10.0},
                "Public": {(2024, 2): 8.0},
            },
        )
        report = PeriodicBackfillService(
            db, delay_seconds=0.0, fetch=lambda ticker: payload,
        ).run(progress=False)
        assert len(report.succeeded) == 1
        assert db.scalar(select(func.count()).select_from(QuarterlyResult)) == 2
        snap = db.scalars(select(ShareholdingSnapshot)).all()
        assert len(snap) == 1
        assert abs(snap[0].promoter_indian - 0.70) < 1e-9   # fraction, not %
        assert db.scalar(select(func.count()).select_from(IngestionFailure)) == 0


# ---------------------------------------------------------------- E
class TestRetryBehaviourPreserved:
    def test_E_job_backoff_then_success(self, db, monkeypatch):
        _ingest(db, "RETRY", _screener_financials("RETRY", years=(2022, 2023)))

        from app.services.universe.financials_backfill import (
            TransientIngestionFailure,
        )

        calls = {"n": 0}
        original = FinancialsBackfillService.run

        def flaky(self, targets=None, *, limit=None, progress=True):
            calls["n"] += 1
            if calls["n"] == 1:
                raise TransientIngestionFailure(transient=2, attempted=5)
            return original(self, targets=targets, limit=limit, progress=progress)

        monkeypatch.setattr(FinancialsBackfillService, "run", flaky)

        JobQueue(db).enqueue(JobKind.FINANCIALS_BACKFILL, payload={"limit": 5})
        db.commit()
        worker = Worker(sessionmaker(bind=db.get_bind()), worker_id="t2-worker")
        assert worker.run_once()                    # attempt 1 → failure
        job = db.scalar(select(BackgroundJob).where(
            BackgroundJob.kind == JobKind.FINANCIALS_BACKFILL.value))
        assert job.status == JobStatus.FAILED.value
        assert job.run_after is not None            # backoff scheduled

        job.run_after = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
        JobQueue(db).requeue_ready()
        assert worker.run_once()                    # attempt 2 → success
        job = db.get(BackgroundJob, job.id)
        assert job.status == JobStatus.SUCCEEDED.value
        assert calls["n"] == 2

    def test_E_failed_data_retry_dispatches_financials(self, db):
        run = IngestionRun(kind="financials_sync", provider="screener.in",
                           started_at=datetime.now(timezone.utc))
        db.add(run)
        db.commit()
        db.add(IngestionFailure(
            run_id=run.id, kind="financials_sync", symbol="RETRY",
            company_id=None, error="HTTPError: 429 too many requests",
            failure_kind="transient",
            last_attempt_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
        ))
        db.commit()

        from app.services.market.sync import FailedRetryService
        out = FailedRetryService(db).run(limit=10, max_attempts=5)
        assert out["resolved"] == 1

        failure = db.scalar(select(IngestionFailure))
        assert failure.resolved_at is not None
        enqueued = db.scalar(select(BackgroundJob).where(
            BackgroundJob.kind == JobKind.FINANCIALS_BACKFILL.value))
        assert enqueued is not None
        assert enqueued.payload["tickers"] == ["RETRY"]

    def test_E_failed_data_retry_dispatches_periodic(self, db, monkeypatch):
        db.add(Company(id="cid-p", ticker="PCO", name="P Ltd", exchange="NSE",
                       listing_status="active"))
        run = IngestionRun(kind="periodic_sync", provider="screener.in",
                           started_at=datetime.now(timezone.utc))
        db.add(run)
        db.commit()
        db.add(IngestionFailure(
            run_id=run.id, kind="periodic_sync", symbol="PCO",
            company_id="cid-p", error="HTTPError: 429 too many requests",
            failure_kind="transient",
            last_attempt_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
        ))
        db.commit()

        from app.services.universe.periodic_backfill import PeriodicReport

        def fake_run(self, companies=None, *, limit=None, progress=True):
            return PeriodicReport(outcomes=[
                type("Outcome", (), {"ok": True})(),
            ])

        monkeypatch.setattr(PeriodicBackfillService, "run", fake_run)
        from app.services.market.sync import FailedRetryService
        out = FailedRetryService(db).run(limit=10, max_attempts=5)
        assert out["resolved"] == 1
        assert db.scalar(select(IngestionFailure)).resolved_at is not None


# ---------------------------------------------------------------- handler
class TestHandlerRefreshMode:
    def test_refresh_mode_in_mock_mode_skips_honestly(self, db, monkeypatch):
        from app.services.platform.jobs.handlers import handler_for

        monkeypatch.setattr("app.core.config.settings.DATA_PROVIDER", "mock")
        result = handler_for(JobKind.FINANCIALS_BACKFILL)(db, {"mode": "refresh"})
        assert result["skipped"] is True

    def test_unknown_mode_is_rejected(self, db, monkeypatch):
        from app.services.platform.jobs.handlers import handler_for

        monkeypatch.setattr("app.core.config.settings.DATA_PROVIDER", "mock")
        with pytest.raises(ValueError):
            handler_for(JobKind.FINANCIALS_BACKFILL)(db, {"mode": "everything"})
