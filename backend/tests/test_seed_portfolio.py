"""Integration tests for seed_portfolio.py.

Verifies seeding of demonstration portfolios, dated transaction ledgers,
geometric random walks for price histories, benchmarks, and watchlists.
"""
from __future__ import annotations

import importlib
import pkgutil
import pytest
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.models.company import Company
from app.models.portfolio import (
    PriceHistory, BenchmarkLevel, Portfolio, PortfolioSnapshot,
    PortfolioTransaction, Watchlist, WatchlistEntry
)
from app.db.seed_portfolio import seed_module8, DEMO_HOLDINGS, DEMO_WATCHLIST

@pytest.fixture
def db_session_seeded_companies():
    """Isolated SQLite session with the necessary companies populated for the demo portfolio."""
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    
    # Load all models
    import app.models as _models
    for _module in pkgutil.iter_modules(_models.__path__):
        importlib.import_module(f"app.models.{_module.name}")

    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with Session() as session:
        # Seed companies that demo portfolio looks up
        tickers = {t[0] for t in DEMO_HOLDINGS} | {t[0] for t in DEMO_WATCHLIST}
        for ticker in sorted(tickers):
            session.add(Company(
                id=f"comp-{ticker.lower()}",
                name=f"{ticker} Name",
                ticker=ticker,
                current_price=1000.0,
                market_cap=50000.0,
                listing_status="active"
            ))
        session.commit()
        yield session


def test_seed_portfolio_complete_pipeline(db_session_seeded_companies):
    """Executes full seed_module8 and asserts correctness of all seeded tables."""
    # Run seed
    result = seed_module8(db_session_seeded_companies)

    # 1. Assert return keys and counts
    assert "price_rows" in result
    assert "benchmark_rows" in result
    assert "portfolio_id" in result
    assert "snapshots" in result
    assert "watchlist_id" in result

    assert result["price_rows"] > 0
    assert result["benchmark_rows"] > 0
    assert result["snapshots"] > 0

    # 2. Assert on PriceHistory
    histories = db_session_seeded_companies.scalars(select(PriceHistory)).all()
    assert len(histories) == result["price_rows"]
    # Check that closing prices are non-zero and walk exists
    assert histories[0].close > 0.0
    assert histories[0].volume > 0

    # 3. Assert on BenchmarkLevel
    benchmarks = db_session_seeded_companies.scalars(select(BenchmarkLevel)).all()
    assert len(benchmarks) == result["benchmark_rows"]
    assert benchmarks[0].close > 0.0

    # 4. Assert on Portfolio
    portfolio = db_session_seeded_companies.get(Portfolio, result["portfolio_id"])
    assert portfolio is not None
    assert portfolio.name == "Core Equity"
    assert portfolio.owner_id == "dev-user"

    # 5. Assert on PortfolioTransaction
    transactions = db_session_seeded_companies.scalars(
        select(PortfolioTransaction).where(PortfolioTransaction.portfolio_id == portfolio.id)
    ).all()
    # At least the DEMO_HOLDINGS purchases plus cash deposit, dividend, bonus etc.
    assert len(transactions) >= len(DEMO_HOLDINGS) + 1

    # Check bonus and sell transactions
    tx_types = {t.txn_type for t in transactions}
    assert "buy" in tx_types
    assert "deposit" in tx_types
    assert "bonus" in tx_types or "dividend" in tx_types or "sell" in tx_types

    # 6. Assert on PortfolioSnapshot
    snapshots = db_session_seeded_companies.scalars(
        select(PortfolioSnapshot).where(PortfolioSnapshot.portfolio_id == portfolio.id)
    ).all()
    assert len(snapshots) == result["snapshots"]
    assert snapshots[0].market_value >= 0.0
    assert snapshots[0].cash > 0.0

    # 7. Assert on Watchlist
    watchlist = db_session_seeded_companies.get(Watchlist, result["watchlist_id"])
    assert watchlist is not None
    assert watchlist.name == "Candidates"

    entries = db_session_seeded_companies.scalars(
        select(WatchlistEntry).where(WatchlistEntry.watchlist_id == watchlist.id)
    ).all()
    assert len(entries) == len(DEMO_WATCHLIST)
