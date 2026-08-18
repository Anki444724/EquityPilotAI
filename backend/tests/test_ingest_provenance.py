"""Tests that real-data ingestion records provenance.

Every successful `ingest_company` call must persist an immutable
`FinancialFactVersion` snapshot carrying source provenance, while a failed or
no-facts ingest must write zero facts and zero version snapshots. The network
is never touched: `fetch_screener` is patched, so these assert the ingestion
persistence contract rather than screener's uptime.
"""
from __future__ import annotations

import importlib
import pkgutil

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.data.ingest import ingest_company
from app.data.screener_source import ScreenerFinancials, ScreenerError
from app.db.base import Base
from app.models.company import Company, FinancialFact
from app.models.financials import FinancialFactVersion


@pytest.fixture()
def db():
    """A scratch in-memory database, isolated per test (same reasoning as the
    backfill suite's fixture: the service under test commits)."""
    import app.models as models_pkg

    for module in pkgutil.iter_modules(models_pkg.__path__):
        importlib.import_module(f"app.models.{module.name}")

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


def _operating_company(ticker="NHPC", years=(2024, 2025, 2026)) -> ScreenerFinancials:
    def series(*values):
        return dict(zip(years, values))

    return ScreenerFinancials(
        ticker=ticker,
        fiscal_years=list(years),
        profit_loss={
            "Sales +": series(1000.0, 1200.0, 1400.0),
            "Expenses +": series(800.0, 950.0, 1100.0),
            "Operating Profit": series(200.0, 250.0, 300.0),
            "Other Income +": series(10.0, 12.0, 15.0),
            "Interest": series(20.0, 22.0, 25.0),
            "Depreciation": series(40.0, 45.0, 50.0),
            "Profit before tax": series(150.0, 195.0, 240.0),
            "Tax %": series(25.0, 25.0, 25.0),
            "Net Profit +": series(112.5, 146.25, 180.0),
            "EPS in Rs": series(11.25, 14.625, 18.0),
            "Dividend Payout %": series(20.0, 20.0, 20.0),
        },
        balance_sheet={
            "Equity Capital": series(100.0, 100.0, 100.0),
            "Reserves": series(500.0, 600.0, 720.0),
            "Borrowings +": series(300.0, 320.0, 340.0),
            "Total Liabilities": series(1100.0, 1250.0, 1420.0),
            "Fixed Assets +": series(600.0, 680.0, 780.0),
            "CWIP": series(50.0, 60.0, 70.0),
            "Investments": series(100.0, 110.0, 120.0),
            "Total Assets": series(1100.0, 1250.0, 1420.0),
        },
        cash_flow={
            "Cash from Operating Activity +": series(180.0, 200.0, 240.0),
        },
        price=50.0,
    )


def test_successful_ingest_writes_a_version_snapshot_with_provenance(db, monkeypatch):
    """Requirement A: a successful ingest persists a FinancialFactVersion with
    source provenance, matching the editor's snapshot convention."""
    monkeypatch.setattr(
        "app.data.ingest.fetch_screener", lambda ticker: _operating_company(ticker))

    result = ingest_company(
        db, "NHPC", "NHPC Ltd", "Power & Utilities", "Hydro Power",
        with_yahoo=False,
    )
    assert result.ok is True
    assert result.fact_count > 0

    company = db.scalar(select(Company).where(Company.ticker == "NHPC"))
    versions = db.execute(
        select(FinancialFactVersion).where(
            FinancialFactVersion.company_id == company.id,
        )
    ).scalars().all()
    assert len(versions) == 1
    version = versions[0]
    assert version.version == 1
    assert version.change_type == "import"
    assert version.actor_id is None  # automated ingestion, not a human edit

    snap = version.snapshot
    # Follows the FinancialAdminService._snapshot shape.
    assert set(snap.keys()) == {"facts", "quarterly", "shareholding", "actions"}
    assert snap["quarterly"] == [] and snap["shareholding"] == [] and snap["actions"] == []
    assert len(snap["facts"]) == result.fact_count
    # Every snapshot fact carries source provenance.
    assert all(f["source"] == "screener.in" for f in snap["facts"])
    assert all({"fiscal_year", "line_item", "value", "precedence", "source"}
               <= set(f) for f in snap["facts"])

    # The facts rows themselves carry provenance too.
    facts = db.execute(select(FinancialFact)).scalars().all()
    assert all(f.source == "screener.in" for f in facts)


def test_a_second_ingest_bumps_the_version(db, monkeypatch):
    """Re-ingesting the same company records a new version (v2), and the new
    snapshot reflects the freshly persisted facts."""
    monkeypatch.setattr(
        "app.data.ingest.fetch_screener", lambda ticker: _operating_company(ticker))
    ingest_company(db, "NHPC", "NHPC Ltd", "Power", "Hydro", with_yahoo=False)
    ingest_company(db, "NHPC", "NHPC Ltd", "Power", "Hydro", with_yahoo=False)

    company = db.scalar(select(Company).where(Company.ticker == "NHPC"))
    versions = db.execute(
        select(FinancialFactVersion).where(
            FinancialFactVersion.company_id == company.id,
        )
    ).scalars().all()
    assert [v.version for v in versions] == [1, 2]


def test_failed_ingest_writes_zero_facts_and_zero_versions(db, monkeypatch):
    """Requirement B: a 404/not-listed ingest writes no facts and no version."""
    def not_listed(ticker):
        raise ScreenerError(f"not listed: https://www.screener.in/company/{ticker}/")

    monkeypatch.setattr("app.data.ingest.fetch_screener", not_listed)

    result = ingest_company(db, "NOPE", "Nope Ltd", "Testing", "Testing",
                            with_yahoo=False)
    assert result.ok is False
    assert "not listed" in (result.error or "")

    assert db.execute(select(FinancialFact)).scalars().all() == []
    assert db.execute(select(FinancialFactVersion)).scalars().all() == []


def test_no_facts_ingest_writes_zero_facts_and_zero_versions(db, monkeypatch):
    """Requirement B: a company with no derivable facts (empty tables) writes
    no facts and no successful-ingestion version snapshot."""
    empty = ScreenerFinancials(ticker="EMPTY", fiscal_years=[])
    monkeypatch.setattr("app.data.ingest.fetch_screener", lambda ticker: empty)

    result = ingest_company(db, "EMPTY", "Empty Ltd", "Testing", "Testing",
                            with_yahoo=False)
    assert result.ok is False
    assert "no canonical facts derived" in (result.error or "")

    assert db.execute(select(FinancialFact)).scalars().all() == []
    assert db.execute(select(FinancialFactVersion)).scalars().all() == []
