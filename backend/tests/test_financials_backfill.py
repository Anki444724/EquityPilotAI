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
    DEFAULT_SWEEP_LIMIT, MIN_USEFUL_YEARS, FailureKind,
    FinancialsBackfillService, classify_ingest_failure,
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


# ===================================================== error classification
class TestFailureClassification:
    def test_http_429_is_transient(self):
        assert classify_ingest_failure("screener: HTTP Error 429: Too Many Requests") \
            is FailureKind.TRANSIENT

    def test_timeout_is_transient(self):
        assert classify_ingest_failure(
            "screener: TimeoutError: timed out"
        ) is FailureKind.TRANSIENT

    def test_connection_reset_is_transient(self):
        assert classify_ingest_failure(
            "screener: ConnectionResetError: [Errno 104] Connection reset by peer"
        ) is FailureKind.TRANSIENT

    def test_http_5xx_is_transient(self):
        assert classify_ingest_failure(
            "screener: HTTP Error 503: Service Unavailable"
        ) is FailureKind.TRANSIENT

    def test_http_404_is_permanent(self):
        assert classify_ingest_failure(
            "screener: not listed: https://www.screener.in/company/NOPE/"
        ) is FailureKind.PERMANENT

    def test_no_canonical_facts_is_permanent(self):
        assert classify_ingest_failure(
            "no canonical facts derived"
        ) is FailureKind.PERMANENT

    def test_unknown_error_defaults_to_permanent(self):
        assert classify_ingest_failure("weird: something unexpected") \
            is FailureKind.PERMANENT

    def test_none_defaults_to_permanent(self):
        assert classify_ingest_failure(None) is FailureKind.PERMANENT


# ===================================================== transient/permanent run
def test_run_classifies_transient_and_permanent_failures(db):
    _company(db, "T429")
    _company(db, "T404")
    _company(db, "NODATA")

    def classify(_db, ticker, *_a, **_k):
        if ticker == "T429":
            return IngestResult(
                ticker=ticker, ok=False, error="screener: HTTP Error 429: Too Many Requests")
        if ticker == "T404":
            return IngestResult(
                ticker=ticker, ok=False, error="screener: not listed: ...")
        return IngestResult(ticker=ticker, ok=False, error="no canonical facts derived")

    report = FinancialsBackfillService(db, delay_seconds=0, ingest=classify).run(progress=False)

    assert len(report.transient_failures) == 1
    assert report.transient_failures[0].ticker == "T429"
    assert len(report.permanent_failures) == 2
    assert report.had_transient_failures is True


def test_run_with_only_permanent_failures_has_no_transients(db):
    _company(db, "ONLY404")
    report = FinancialsBackfillService(
        db, delay_seconds=0,
        ingest=lambda _db, ticker, *_a, **_k: IngestResult(
            ticker=ticker, ok=False, error="screener: not listed: ..."),
    ).run(progress=False)
    assert report.had_transient_failures is False
    assert len(report.transient_failures) == 0


# ===================================================== targeted ticker ingest
def test_companies_by_tickers_resolves_from_the_database(db):
    """Targeted ingest reads the companies table, not NSE_UNIVERSE."""
    _company(db, "NHPC", category="largecap")
    _company(db, "OTHER")

    service = FinancialsBackfillService(db, delay_seconds=0)
    targets = service.companies_by_tickers(["NHPC"])

    assert [t.ticker for t in targets] == ["NHPC"]
    assert targets[0].name == "NHPC Ltd."
    assert targets[0].market_cap_category == "largecap"


def test_companies_by_tickers_is_case_and_whitespace_insensitive(db):
    _company(db, "NHPC")
    targets = FinancialsBackfillService(db, delay_seconds=0).companies_by_tickers(
        ["  nhpc  "])
    assert [t.ticker for t in targets] == ["NHPC"]


def test_companies_by_tickers_returns_empty_for_unknown_tickers(db):
    _company(db, "NHPC")
    targets = FinancialsBackfillService(db, delay_seconds=0).companies_by_tickers(
        ["ZZZZ"])
    assert targets == []


def test_targeted_run_ingests_only_the_requested_ticker(db):
    """A targeted run passes explicit targets to `run`, so it is independent of
    the 25-company sweep limit and touches only the requested company."""
    _company(db, "NHPC")
    _company(db, "SOMETHINGELSE")
    seen: list[str] = []

    def ingest(_db, ticker, *_a, **_k):
        seen.append(ticker)
        return IngestResult(ticker=ticker, ok=True,
                            fiscal_years=[2023, 2024], fact_count=40)

    service = FinancialsBackfillService(db, delay_seconds=0, ingest=ingest)
    targets = service.companies_by_tickers(["NHPC"])
    report = service.run(targets=targets, progress=False)

    assert seen == ["NHPC"]
    assert len(report.succeeded) == 1
    assert report.succeeded[0].ticker == "NHPC"


# ===================================================== resumable batching
def test_batched_runs_resume_with_the_next_uncovered_companies(db):
    """Run 1 covers the first batch, run 2 the next — because selection is
    recomputed from the database and covered companies are skipped."""
    for ticker in ("A", "B", "C", "D", "E"):
        _company(db, ticker)

    def ingest(_db, ticker, *_a, **_k):
        # Persist real facts so the company counts as covered and is excluded
        # from the next run's selection — what `ingest_company` does for real.
        company = _db.query(Company).filter_by(ticker=ticker).one()
        for year in (2023, 2024):
            _db.add(FinancialFact(
                company_id=company.id, fiscal_year=year,
                line_item="revenue", value=100.0, precedence=2,
            ))
        _db.commit()
        return IngestResult(ticker=ticker, ok=True,
                            fiscal_years=[2023, 2024], fact_count=40)

    service = FinancialsBackfillService(db, delay_seconds=0, ingest=ingest)
    first = service.run(limit=2, progress=False)
    second = service.run(limit=2, progress=False)
    third = service.run(limit=2, progress=False)

    # Run 1 → first 2, run 2 → next 2, run 3 → last 1. Selection is recomputed
    # from the database each run and covered companies are skipped.
    assert [o.ticker for o in first.succeeded] == ["A", "B"]
    assert [o.ticker for o in second.succeeded] == ["C", "D"]
    assert [o.ticker for o in third.succeeded] == ["E"]
    # Everything is now covered and excluded from future selection.
    assert service.companies_without_financials() == []


def test_default_sweep_limit_is_twenty_five():
    """The safe default for a scheduled sweep is exactly 25 companies."""
    assert DEFAULT_SWEEP_LIMIT == 25


# ------------------------------------------------------ canonical-company regressions
def test_company_with_twelve_fiscal_years_is_not_reported_missing(db):
    """Regression: an M&M-style company with 12 fiscal years of history must
    never appear in companies_without_financials().

    The duplicate-company defect surfaced exactly this way: a legacy duplicate
    row with zero facts was (correctly, per the LEFT JOIN) reported as
    uncovered, while its twin owned the full history. After the deduplication
    migration there is one row, and it must be treated as covered.
    """
    _company(db, "M&M", years=12)
    service = FinancialsBackfillService(db, delay_seconds=0)

    selected = service.companies_without_financials()
    assert [t.ticker for t in selected] == []


def test_only_genuinely_uncovered_companies_are_selected(db):
    """Selection returns exactly the companies with no usable history — no
    more, no fewer — which is the invariant the duplicate rows broke."""
    _company(db, "COVERED", years=12)
    _company(db, "COVERED2", years=5)
    _company(db, "EMPTY1", years=0)
    _company(db, "EMPTY2", years=0)

    service = FinancialsBackfillService(db, delay_seconds=0)
    selected = {t.ticker for t in service.companies_without_financials()}

    assert selected == {"EMPTY1", "EMPTY2"}


def test_coverage_snapshot_counts_each_company_once(db):
    """A duplicate row inflated `without_financials` even when the ticker was
    fully covered. Post-dedup, the snapshot must count one row per company."""
    _company(db, "DUP1", years=12)
    _company(db, "DUP2", years=12)
    _company(db, "MISS1", years=0)

    snapshot = FinancialsBackfillService(db, delay_seconds=0).coverage_snapshot()

    assert snapshot["companies"] == 3
    assert snapshot["with_financials"] == 2
    assert snapshot["without_financials"] == 1
