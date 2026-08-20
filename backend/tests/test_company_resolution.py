"""Canonical company resolution and the duplicate-company regressions.

These tests pin the root-cause fix for the financial backfill duplicate bug:

* one ticker must never produce a second ``companies`` row, no matter how
  many times the sync/ingest runs;
* when legacy duplicates still exist (pre-dedup-migration data), every lookup
  resolves to the row that owns the financial history — never an arbitrary
  twin;
* a creation race is arbitrated by the database constraint, and the loser
  merges into the winner instead of failing or duplicating.

The network is never touched: the providers are stubbed with in-memory
fixtures shaped like real screener.in responses.
"""

from __future__ import annotations

import importlib
import pkgutil
import uuid

import pytest

from app.data.screener_source import ScreenerFinancials
from app.data.yahoo_source import CompanyFinancials
from app.models.company import Company, FinancialFact
from app.services.company_service import CompanyService
from app.services.universe.resolution import resolve_company


@pytest.fixture()
def db():
    """A scratch database per test (same reasoning as the backfill suite)."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    import app.models as models_pkg
    from app.db.base import Base

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


def _screener(ticker: str, years: int = 12) -> ScreenerFinancials:
    fy = [2020 + i for i in range(years)]
    return ScreenerFinancials(
        ticker=ticker,
        fiscal_years=fy,
        profit_loss={
            "Sales": {y: 1_000.0 + i for i, y in enumerate(fy)},
            "Expenses": {y: 600.0 for y in fy},
            "Net Profit": {y: 200.0 for y in fy},
            "EPS in Rs": {y: 20.0 for y in fy},
            "Dividend Payout %": {y: 20.0 for y in fy},
            "Profit before tax": {y: 260.0 for y in fy},
            "Tax %": {y: 23.0 for y in fy},
        },
        balance_sheet={
            "Total Assets": {y: 5_000.0 for y in fy},
            "Total Liabilities": {y: 4_000.0 for y in fy},
            "Equity Capital": {y: 100.0 for y in fy},
            "Reserves": {y: 900.0 for y in fy},
            "Borrowings": {y: 500.0 for y in fy},
        },
        cash_flow={"Cash from Operating Activity": {y: 300.0 for y in fy}},
        price=1_000.0,
        market_cap=100_000.0,
    )


def _yahoo(ticker: str, years: int = 12) -> CompanyFinancials:
    fy = [2020 + i for i in range(years)]
    return CompanyFinancials(ticker=ticker, fiscal_years=fy, facts={},
                             context={}, price=1_000.0)


def _add_company(db, ticker, *, exchange="NSE", years=0) -> str:
    cid = str(uuid.uuid4())
    db.add(Company(
        id=cid, name=f"{ticker} Ltd.", ticker=ticker, exchange=exchange,
        sector="Testing", listing_status="active",
    ))
    for offset in range(years):
        db.add(FinancialFact(
            company_id=cid, fiscal_year=2020 + offset,
            line_item="revenue", value=100.0 + offset, precedence=2,
        ))
    db.commit()
    return cid


def _ingest(db, ticker, monkeypatch, *, years=12, with_yahoo=False):
    from app.data import ingest as ingest_module

    monkeypatch.setattr(
        ingest_module, "fetch_screener", lambda t: _screener(t, years=years),
    )
    if with_yahoo:
        monkeypatch.setattr(
            ingest_module, "fetch_financials",
            lambda t: _yahoo(t, years=years),
        )
    return ingest_module.ingest_company(
        db, ticker, f"{ticker} Ltd.", "Testing", "",
        with_yahoo=with_yahoo,
    )


def _count(db, ticker):
    from sqlalchemy import func, select

    return db.execute(
        select(func.count()).select_from(Company)
        .where(Company.ticker == ticker)
    ).scalar_one()


# ------------------------------------------------------------------ resolution

def test_resolver_picks_the_financial_history_owner(db):
    """When two rows share a ticker, the canonical row is the one that owns
    the facts — deterministic, never an arbitrary planner pick."""
    empty = _add_company(db, "M&M", exchange="NSE", years=0)
    owner = _add_company(db, "M&M", exchange="BSE", years=12)

    assert resolve_company(db, "M&M", exchange="NSE").id == owner
    assert resolve_company(db, "M&M").id == owner
    assert resolve_company(db, "M&M", exchange="BSE").id == owner
    assert empty != owner


def test_resolver_is_venue_scoped(db):
    """An NSE ingest must never adopt a foreign listing that shares a symbol."""
    us_id = _add_company(db, "TCS", exchange="NASDAQ", years=3)

    assert resolve_company(db, "TCS", exchange="NSE") is None
    assert resolve_company(db, "TCS", exchange="NASDAQ").id == us_id


def test_get_by_ticker_returns_history_owner_not_arbitrary(db):
    _add_company(db, "J&KBANK", exchange="NSE", years=0)
    owner = _add_company(db, "J&KBANK", exchange="BSE", years=12)

    assert CompanyService(db).get_by_ticker("j&kbank").id == owner


# ------------------------------------------------------------------ ingestion

def test_ingest_company_twice_keeps_one_row(db, monkeypatch):
    """Regression: running the company sync twice does not create another
    company row, and the second run refreshes the same row in place."""
    first = _ingest(db, "MINDACORP", monkeypatch)
    assert first.ok
    cid = first.company_id
    assert _count(db, "MINDACORP") == 1

    second = _ingest(db, "MINDACORP", monkeypatch)
    assert second.ok
    assert second.company_id == cid
    assert _count(db, "MINDACORP") == 1

    facts = db.query(FinancialFact).filter_by(company_id=cid).count()
    assert facts > 0
    assert facts == second.fact_count


def test_ingest_writes_into_the_history_owner_not_a_new_row(db, monkeypatch):
    """With a legacy duplicate pair already present, ingest must refresh the
    canonical (facts-owning) row and create nothing new."""
    _add_company(db, "M&MFIN", exchange="NSE", years=0)
    owner = _add_company(db, "M&MFIN", exchange="BSE", years=12)

    result = _ingest(db, "M&MFIN", monkeypatch)
    assert result.ok
    assert result.company_id == owner
    assert _count(db, "M&MFIN") == 2  # legacy pair untouched; nothing added
    assert db.query(FinancialFact).filter_by(company_id=owner).count() > 0


def test_ingest_losing_a_creation_race_merges_into_the_winner(db, monkeypatch):
    """The database constraint arbitrates a concurrent ingest: the loser's
    insert fails, the loser re-reads the winner and refreshes it, and exactly
    one company row exists."""
    from app.data import ingest as ingest_module
    from app.services.universe import resolution as resolution_module

    winner_id = _add_company(db, "RACE1", exchange="NSE", years=0)
    assert _count(db, "RACE1") == 1

    calls = {"n": 0}
    real_resolve = resolution_module.resolve_company

    def racing_resolve(session, ticker, *, exchange=None):
        # First call simulates the pre-insert lookup seeing an empty table
        # (the winner commits between our SELECT and our INSERT).
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return real_resolve(session, ticker, exchange=exchange)

    monkeypatch.setattr(ingest_module, "resolve_company", racing_resolve)
    monkeypatch.setattr(
        ingest_module, "fetch_screener", lambda t: _screener(t),
    )
    monkeypatch.setattr(
        ingest_module, "fetch_financials", lambda t: _yahoo(t),
    )

    result = ingest_module.ingest_company(
        db, "RACE1", "Race One Ltd.", "Testing", "", with_yahoo=False,
    )

    assert result.ok
    assert result.company_id == winner_id
    assert _count(db, "RACE1") == 1
    assert db.query(FinancialFact).filter_by(company_id=winner_id).count() > 0


# ------------------------------------------------------------ universe import

def test_nifty500_import_losing_a_creation_race_merges_into_the_winner(
    db, monkeypatch,
):
    """Same race protocol for the universe sync: the losing import updates
    the winner instead of creating a second identity row."""
    from app.services.universe.nifty500 import (
        INDEX_NAME, Constituent, Nifty500Importer,
    )

    winner_id = _add_company(db, "RACE2", exchange="NSE", years=0)

    item = Constituent(
        symbol="RACE2", name="Race Two Ltd.", sector="Capital Goods",
        isin="INE000R02001", category="midcap", bse_code="500020",
    )
    monkeypatch.setattr(
        "app.services.universe.nifty500.build_universe", lambda: [item],
    )

    importer = Nifty500Importer(db)
    real_existing = importer._existing
    calls = {"n": 0}

    def racing_existing(symbol, isin):
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return real_existing(symbol, isin)

    monkeypatch.setattr(importer, "_existing", racing_existing)

    report = importer.run()
    assert report.created == 0
    assert report.updated == 1
    assert report.failed == 0

    rows = db.query(Company).filter_by(ticker="RACE2").all()
    assert len(rows) == 1
    assert rows[0].id == winner_id
    assert rows[0].name == "Race Two Ltd."
    assert rows[0].index_membership == INDEX_NAME


# ---------------------------------------------------------------- admin guard

def test_admin_create_losing_a_creation_race_is_a_friendly_error(db, monkeypatch):
    """The admin create path also relies on the constraint as arbiter: a race
    becomes a CompanyAdminError, never a second row."""
    from app.schemas.company import CompanyCreate
    from app.services.company_admin_service import (
        CompanyAdminError, CompanyAdminService,
    )

    service = CompanyAdminService(db)
    service.create(CompanyCreate(name="Race Three Ltd.", ticker="RACE3"))
    db.commit()  # the first request commits; the second one is the racer

    # Simulate the race: the pre-check passes (sees nothing) but a concurrent
    # request inserted the row before our flush.
    def blind_check(*_a, **_k):
        return None

    monkeypatch.setattr(service, "_check_unique", blind_check)
    with pytest.raises(CompanyAdminError, match="already exists"):
        service.create(CompanyCreate(name="Race Three Ltd.", ticker="RACE3"))

    assert _count(db, "RACE3") == 1


def test_duplicate_tickers_are_blocked_by_the_database(db):
    """The last line of defence: even bypassing the service layer, the schema
    refuses a second (ticker, exchange) pair — case-insensitively."""
    from sqlalchemy.exc import IntegrityError

    _add_company(db, "GUARD1", exchange="NSE")

    with pytest.raises(IntegrityError):
        db.add(Company(
            id=str(uuid.uuid4()), name="Guard One Ltd.", ticker="guard1",
            exchange="NSE",
        ))
        db.flush()
    db.rollback()
    assert _count(db, "GUARD1") == 1
