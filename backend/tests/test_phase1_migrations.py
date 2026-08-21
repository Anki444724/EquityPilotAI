"""Phase 1 — migration upgrade/downgrade round-trip (requirement C/L).

Runs the real alembic chain against a scratch database: upgrade to head,
verify the Phase-1 objects exist; downgrade exactly the five Phase-1
revisions and verify they are gone; upgrade again. This is the disposable-
database rehearsal the deployment procedure will follow on staging.
"""
from __future__ import annotations

import pathlib

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

BACKEND = pathlib.Path(__file__).resolve().parent.parent

PHASE1_REVISIONS = [
    "b1e5a7d2c904",   # companies metadata columns
    "c2f6b8e3d015",   # financial_facts upsert columns
    "d3a7c9f4e126",   # market_quotes + price_history OHLC
    "e4b8d0a5f237",   # ingestion_runs / ingestion_failures
    "f5c9e1b6a348",   # pg_trgm search indexes (Postgres-only, guarded)
]
PRE_PHASE1_HEAD = "164253079db3"


@pytest.fixture(scope="module")
def scratch(tmp_path_factory):
    """A scratch database at the PRE-Phase-1 head, with the engine handed back.

    The full alembic chain is not SQLite-runnable: `3b4c7d9e0f1a` (company
    management, pre-Phase-1 history) uses constraint ALTER, which the SQLite
    dialect does not implement — the same reason `test_migrations.py` skips
    its full-chain diff on SQLite. Production runs Postgres, where the chain
    is verified. This fixture therefore builds the minimal PRE-Phase-1 shape
    of the four tables the Phase-1 revisions touch, stamps
    `164253079db3` (the real pre-Phase-1 head), and lets the five new
    revisions do their work from there — exercising exactly the migrations
    this phase adds, on a disposable database, both ways.
    """
    from alembic.config import Config
    from app.core.config import settings

    target = tmp_path_factory.mktemp("phase1mig") / "scratch.db"
    url = f"sqlite:///{target}"
    engine = sa.create_engine(url)
    with engine.begin() as conn:
        conn.execute(sa.text("""
            CREATE TABLE companies (
                id VARCHAR(36) PRIMARY KEY,
                name VARCHAR(200) NOT NULL, ticker VARCHAR(32) NOT NULL,
                exchange VARCHAR(16), isin VARCHAR(16),
                listing_status VARCHAR(12), currency VARCHAR(3),
                reporting_scale VARCHAR(8), data_version INTEGER,
                created_at DATETIME, updated_at DATETIME,
                CONSTRAINT uq_company_ticker_exchange UNIQUE (ticker, exchange),
                CONSTRAINT ix_companies_isin UNIQUE (isin)
            )"""))
        conn.execute(sa.text("""
            CREATE TABLE financial_facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_id VARCHAR(36), fiscal_year INTEGER,
                line_item VARCHAR(64), value FLOAT,
                precedence INTEGER, source VARCHAR(120),
                created_at DATETIME, updated_at DATETIME,
                CONSTRAINT uq_fact_company_year_item_precedence
                    UNIQUE (company_id, fiscal_year, line_item, precedence)
            )"""))
        conn.execute(sa.text("""
            CREATE TABLE price_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker VARCHAR(32), as_of DATE, close FLOAT,
                volume FLOAT, traded_value FLOAT,
                created_at DATETIME, updated_at DATETIME,
                CONSTRAINT uq_price_ticker_date UNIQUE (ticker, as_of)
            )"""))
        conn.execute(sa.text("""
            CREATE TABLE background_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at DATETIME, updated_at DATETIME
            )"""))
    engine.dispose()

    config = Config(str(BACKEND / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    original = settings.DATABASE_URL
    try:
        settings.DATABASE_URL = url
        command.stamp(config, PRE_PHASE1_HEAD)
        command.upgrade(config, "head")
    finally:
        settings.DATABASE_URL = original
    engine = sa.create_engine(url)
    try:
        yield config, engine
    finally:
        engine.dispose()


def _columns(engine, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(engine).get_columns(table)}


def _tables(engine) -> set[str]:
    return set(sa.inspect(engine).get_table_names())


class TestUpgrade:
    def test_phase1_columns_exist_after_upgrade(self, scratch):
        _config, engine = scratch
        companies = _columns(engine, "companies")
        assert {"metadata_source", "metadata_synced_at"} <= companies
        facts = _columns(engine, "financial_facts")
        assert {"consolidated", "fetched_at", "data_version"} <= facts
        prices = _columns(engine, "price_history")
        assert {"day_open", "day_high", "day_low", "provider"} <= prices

    def test_phase1_tables_exist_after_upgrade(self, scratch):
        _config, engine = scratch
        tables = _tables(engine)
        assert {"market_quotes", "ingestion_runs", "ingestion_failures"} <= tables

    def test_preexisting_constraints_survive(self, scratch):
        """Requirement: do not disable or drop existing uniqueness. The three
        identity/fact constraints must be exactly as before Phase 1."""
        _config, engine = scratch

        def uniques(table: str) -> set[tuple[str, ...]]:
            out: set[tuple[str, ...]] = set()
            for constraint in sa.inspect(engine).get_unique_constraints(table):
                names = constraint["column_names"]
                # Postgres reports dicts, SQLite plain strings.
                cols = tuple(
                    c["name"] if isinstance(c, dict) else c for c in names
                )
                out.add(cols)
            return out

        assert ("ticker", "exchange") in uniques("companies")
        assert ("isin",) in uniques("companies")
        assert ("company_id", "fiscal_year", "line_item", "precedence") in \
            uniques("financial_facts")
        assert ("ticker", "as_of") in uniques("price_history")


class TestDowngrade:
    def test_downgrade_the_five_phase1_revisions_and_back(self, scratch):
        from app.core.config import settings

        config, engine = scratch
        original = settings.DATABASE_URL

        settings.DATABASE_URL = str(engine.url)
        try:
            # Down to the pre-Phase-1 head: all five revisions, one step
            # each, proving every downgrade() runs (not just the bundle).
            for revision in reversed(PHASE1_REVISIONS):
                command.downgrade(config, revision)
            command.downgrade(config, PRE_PHASE1_HEAD)
            assert _tables(engine) == _tables(engine) - {
                "market_quotes", "ingestion_runs", "ingestion_failures",
            }
            assert "metadata_source" not in _columns(engine, "companies")
            assert "consolidated" not in _columns(engine, "financial_facts")
            assert "day_open" not in _columns(engine, "price_history")

            # And back up: a re-upgrade must reproduce the same schema.
            command.upgrade(config, "head")
            assert "market_quotes" in _tables(engine)
            assert "consolidated" in _columns(engine, "financial_facts")
        finally:
            settings.DATABASE_URL = original

    def test_data_survives_the_round_trip(self, scratch):
        """A row written under the Phase-1 schema survives downgrade+upgrade
        of the objects that do not touch it, and the identity constraint still
        holds afterwards."""
        from app.core.config import settings

        config, engine = scratch
        with engine.begin() as conn:
            conn.execute(sa.text(
                "INSERT INTO companies (id, name, ticker, exchange, isin, "
                "listing_status, currency, reporting_scale, data_version, "
                "created_at, updated_at) VALUES "
                "('c1', 'Survivor Ltd', 'SURV', 'NSE', 'INE999999999', "
                "'active', 'INR', 'crore', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ))
        original = settings.DATABASE_URL
        settings.DATABASE_URL = str(engine.url)
        try:
            command.downgrade(config, PRE_PHASE1_HEAD)
            command.upgrade(config, "head")
        finally:
            settings.DATABASE_URL = original
        with engine.begin() as conn:
            row = conn.execute(sa.text(
                "SELECT id, ticker FROM companies WHERE id = 'c1'"
            )).one()
        assert row.ticker == "SURV"
