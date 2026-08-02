"""Tests for the universe-wide financials backfill.

The network is never touched: `ingest_company` is injected, so these assert
the service's own behaviour — selection, ordering, reason capture, coverage
arithmetic and the no-placeholder guarantee — rather than screener's uptime.
"""

from __future__ import annotations

import uuid

import pytest

from app.data.ingest import IngestResult
from app.models.company import Company, FinancialFact
from app.services.universe.financials_backfill import (
    MIN_USEFUL_YEARS, FinancialsBackfillService,
)


@pytest.fixture()
def db():
    """A scratch database per test.

    The shared `TestingSession` was the obvious choice and the wrong one:
    these tests `commit()` (the service under test commits), and the module
    fixture is also pre-seeded with reference companies. Rolling back at
    teardown therefore discarded nothing, so the first test's rows were
    visible to the second and `coverage_snapshot` counted 38 companies where
    the test had created one. That was a harness defect, not a product one —
    but it would have made every coverage assertion meaningless.

    An in-memory engine per test gives real isolation, which is what these
    assertions actually require.
    """
    import importlib
    import pkgutil

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    import app.models as models_pkg
    from app.db.base import Base

    # Same reasoning as conftest (CONFTEST-001): `create_all` only builds
    # tables for models that have been imported.
    for module in pkgutil.iter_modules(models_pkg.__path__):
        importlib.import_module(f"app.models.{module.name}")

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _company(db, ticker, *, category="midcap", years=0, status="active",
             isin=None, bse=None):
    cid = str(uuid.uuid4())
    db.add(Company(
        id=cid, name=f"{ticker} Ltd.", ticker=ticker, exchange="NSE",
        sector="Testing", market_cap_category=category,
        listing_status=status, isin=isin, bse_code=bse,
    ))
    for offset in range(years):
        db.add(FinancialFact(
            company_id=cid, fiscal_year=2020 + offset,
            line_item="revenue", value=100.0 + offset, precedence=2,
        ))
    db.commit()
    return cid


# ------------------------------------------------------------------ selection

def test_selects_only_companies_without_usable_history(db):
    _company(db, "COVERED", years=5)
    _company(db, "EMPTY", years=0)
    service = FinancialsBackfillService(db, delay_seconds=0)

    tickers = {t.ticker for t in service.companies_without_financials()}
    assert "EMPTY" in tickers
    assert "COVERED" not in tickers


def test_a_single_stray_year_does_not_count_as_coverage(db):
    """One fiscal year is not a financial history.

    Counting it would inflate the headline coverage figure while leaving the
    statements unusable, and — worse — would exclude the company from the
    retry set, stranding it permanently.
    """
    _company(db, "STRAY", years=1)
    assert MIN_USEFUL_YEARS > 1

    service = FinancialsBackfillService(db, delay_seconds=0)
    assert "STRAY" in {t.ticker for t in service.companies_without_financials()}


def test_delisted_companies_are_excluded_by_default(db):
    _company(db, "GONE", years=0, status="delisted")
    service = FinancialsBackfillService(db, delay_seconds=0)

    assert "GONE" not in {t.ticker for t in service.companies_without_financials()}
    assert "GONE" in {
        t.ticker for t in service.companies_without_financials(only_active=False)
    }


def test_largecaps_are_attempted_first(db):
    """If a long sweep is interrupted, the coverage that exists should be the
    coverage users are most likely to look for."""
    _company(db, "SMALL", category="smallcap")
    _company(db, "LARGE", category="largecap")
    _company(db, "MID", category="midcap")

    order = [t.ticker for t in
             FinancialsBackfillService(db, delay_seconds=0)
             .companies_without_financials()]
    assert order.index("LARGE") < order.index("MID") < order.index("SMALL")


# -------------------------------------------------------------------- running

def test_failure_reason_is_preserved_verbatim(db):
    """"Reason for every missing company" is a deliverable, so a generic
    "failed" here would be a defect."""
    _company(db, "BAD")
    reason = "screener: HTTP 404 for https://www.screener.in/company/BAD/"

    def failing(*_a, **_k):
        return IngestResult(ticker="BAD", ok=False, error=reason)

    report = FinancialsBackfillService(
        db, delay_seconds=0, ingest=failing,
    ).run(progress=False)

    assert report.failed[0].reason == reason


def test_one_bad_company_does_not_stop_the_sweep(db):
    _company(db, "AAA")
    _company(db, "BBB")
    _company(db, "CCC")
    seen: list[str] = []

    def flaky(_db, ticker, *_a, **_k):
        seen.append(ticker)
        if ticker == "BBB":
            raise RuntimeError("connection reset by peer")
        return IngestResult(ticker=ticker, ok=True,
                            fiscal_years=[2023, 2024], fact_count=40)

    report = FinancialsBackfillService(
        db, delay_seconds=0, ingest=flaky,
    ).run(progress=False)

    assert len(seen) == 3, "sweep stopped early"
    assert len(report.succeeded) == 2
    assert "RuntimeError" in report.failed[0].reason


def test_no_placeholder_rows_are_written_for_a_failed_company(db):
    """The central guarantee: a company the source has no data for keeps
    zero facts, so the UI's "no financial data" message stays truthful."""
    cid = _company(db, "NODATA")

    def no_data(*_a, **_k):
        return IngestResult(ticker="NODATA", ok=False,
                            error="no canonical facts derived")

    FinancialsBackfillService(db, delay_seconds=0, ingest=no_data).run(
        progress=False)

    assert db.query(FinancialFact).filter_by(company_id=cid).count() == 0


def test_reasons_are_grouped_for_the_summary(db):
    for ticker in ("A1", "A2", "B1"):
        _company(db, ticker)

    def mixed(_db, ticker, *_a, **_k):
        error = ("screener: HTTP 404 not found" if ticker.startswith("A")
                 else "no canonical facts derived")
        return IngestResult(ticker=ticker, ok=False, error=error)

    report = FinancialsBackfillService(
        db, delay_seconds=0, ingest=mixed,
    ).run(progress=False)

    assert report.reasons()["screener"] == 2


# ------------------------------------------------------------------- coverage

def test_coverage_snapshot_is_read_back_from_the_database(db):
    """Deliberately re-queries rather than trusting the run's own tally: the
    question is what the platform holds now."""
    _company(db, "HAS", category="largecap", years=4)
    _company(db, "HASNT", category="largecap", years=0)

    snapshot = FinancialsBackfillService(db, delay_seconds=0).coverage_snapshot()

    assert snapshot["with_financials"] == 1
    assert snapshot["without_financials"] == 1
    assert snapshot["coverage_pct"] == 50.0
    assert snapshot["by_category"]["largecap"] == {"total": 2, "covered": 1}


def test_delisted_companies_are_not_counted_against_coverage(db):
    """A delisted company has no current filings by definition; counting it
    as a coverage miss would make 100% unreachable for the wrong reason."""
    _company(db, "LIVE", years=4)
    _company(db, "DEAD", years=0, status="delisted")

    snapshot = FinancialsBackfillService(db, delay_seconds=0).coverage_snapshot()
    assert snapshot["companies"] == 1
    assert snapshot["coverage_pct"] == 100.0


def test_identity_columns_are_passed_through_not_invented(db):
    """The Nifty 500 import set isin, bse_code and market_cap_category.
    Backfill must address the existing row, never create a second one."""
    _company(db, "IDENT", isin="INE000A01001", bse="500001", category="largecap")

    captured: dict[str, object] = {}

    def capture(_db, ticker, name, sector, industry, **_k):
        captured.update(ticker=ticker, name=name, sector=sector,
                        industry=industry)
        return IngestResult(ticker=ticker, ok=True,
                            fiscal_years=[2024], fact_count=1)

    FinancialsBackfillService(db, delay_seconds=0, ingest=capture).run(
        progress=False)

    assert captured["ticker"] == "IDENT"
    assert captured["name"] == "IDENT Ltd."
    assert captured["sector"] == "Testing"
    assert db.query(Company).filter_by(ticker="IDENT").count() == 1
