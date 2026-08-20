"""Regression tests for the company deduplication migration.

These replay the production scenario the bug report describes — a duplicate
``companies`` row for one ticker, with the financial history on exactly one
of the pair — against a real Alembic chain: the database is migrated to the
revision *before* the fix, seeded with duplicates and dependent rows, then
upgraded through the deduplication migration and verified.

What is pinned here:

* the canonical row is the one that owns the financial history;
* every dependent foreign key is migrated (documents, watchlists, quarters,
  versions, quality snapshots) and conflicting redundant twins are backed
  up, never silently lost;
* financial history is a hard invariant: no fact is deleted, overwritten or
  stranded — a conflicting pair aborts the migration instead;
* the duplicate company row is removed only after everything else moved;
* the case-insensitive (exchange, upper(ticker)) guard index exists.
"""

from __future__ import annotations

import importlib
import pathlib
import pkgutil
import uuid
from datetime import datetime, timezone

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

import app.models as models_pkg

for module in pkgutil.iter_modules(models_pkg.__path__):
    importlib.import_module(f"app.models.{module.name}")

from app.models.analysis import QuarterlyResult  # noqa: E402
from app.models.company import (  # noqa: E402
    Company, CompanyVersion, FinancialFact,
)
from app.models.document import Document  # noqa: E402
from app.models.financials import FinancialFactVersion  # noqa: E402
from app.models.portfolio import Watchlist, WatchlistEntry  # noqa: E402
from app.models.scoring import DataQualitySnapshot  # noqa: E402

BACKEND = pathlib.Path(__file__).resolve().parent.parent
PREV_REVISION = "164253079db3"  # head before the deduplication migration

#: (table, fk column) — mirrors the migration's SQLite fallback list; the
#: test asserts zero dangling references across all of them.
REFERENCING_TABLES = {
    "ai_analyses", "ai_overrides", "ai_score_versions",
    "company_crawl_state", "company_versions", "corporate_actions",
    "credit_ratings", "data_quality_snapshots", "debt_instruments",
    "discovered_filings", "document_summaries", "documents",
    "financial_fact_versions", "financial_facts", "forecasts",
    "knowledge_entries", "market_overrides", "portfolio_transactions",
    "quarterly_results", "reports", "score_snapshots",
    "shareholding_snapshots", "watchlist_entries", "yearly_observations",
}


def _config(url: str) -> Config:
    config = Config(str(BACKEND / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    return config


def _prev_schema_engine(url: str, settings) -> sa.Engine:
    """A database with the full pre-fix schema and the previous revision
    stamped.

    The historical chain itself cannot be replayed on SQLite — migrations
    like 3b4c7d9e0f1a call create_unique_constraint without batch mode, which
    is why TestMigrationsMatchModels skips on SQLite (production runs
    Postgres). Instead this builds the schema the project asserts the
    migrations produce (`Base.metadata.create_all`, the same guarantee the
    test suite relies on), stamps the previous revision, and then lets
    Alembic run *only* the deduplication migration — exactly what a
    production upgrade to head executes.
    """
    from app.db.base import Base

    engine = sa.create_engine(url)
    Base.metadata.create_all(bind=engine)
    # The case-insensitive guard index is added by the migration under test;
    # drop the model-created copy so the pre-fix schema matches production
    # and the migration's own CREATE UNIQUE INDEX is what gets verified.
    with engine.begin() as conn:
        conn.execute(sa.text(
            "DROP INDEX IF EXISTS uq_companies_exchange_ticker_ci"
        ))
    original = settings.DATABASE_URL
    try:
        settings.DATABASE_URL = url
        command.stamp(_config(url), PREV_REVISION)
    finally:
        settings.DATABASE_URL = original
    return engine


def _upgrade(config: Config, revision: str, settings) -> None:
    original = settings.DATABASE_URL
    try:
        settings.DATABASE_URL = config.get_main_option("sqlalchemy.url")
        command.upgrade(config, revision)
    finally:
        settings.DATABASE_URL = original


def _dangling(db, company_id: str) -> list[str]:
    """Any row in any referencing table that still points at company_id."""
    hits = []
    for table in sorted(REFERENCING_TABLES):
        try:
            n = db.execute(sa.text(
                f'SELECT COUNT(*) FROM "{table}" WHERE company_id = :cid'
            ), {"cid": company_id}).scalar()
        except sa.exc.OperationalError as exc:  # table absent at this revision
            if "no such table" in str(exc).lower():
                continue
            raise
        if n:
            hits.append(f"{table}={n}")
    return hits


def _seed_confirmed_shape(db) -> tuple[str, str]:
    """The bug report's exact shape: one ticker, two rows, facts on one.

    The pair is seeded on different Indian venues (NSE/BSE) because the
    (ticker, exchange) unique constraint already exists at the previous
    revision — on the production database the constraint was not effective
    for these rows, and the migration must merge both cases.
    """
    canonical = str(uuid.uuid4())
    dup = str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    db.add(Company(
        id=canonical, name="Mahindra & Mahindra Ltd", ticker="M&M",
        exchange="NSE", sector="Automobile", industry=None,
        listing_status="active", data_version=3,
        created_at=now, updated_at=now,
    ))
    db.add(Company(
        id=dup, name="Mahindra & Mahindra", ticker="M&M",
        exchange="BSE", sector=None, industry="Automotive",
        listing_status="active", data_version=1,
        created_at=now, updated_at=now,
    ))
    db.flush()

    # 12 fiscal years of history on the canonical row, none on the duplicate.
    for offset in range(12):
        year = 2014 + offset
        db.add(FinancialFact(
            company_id=canonical, fiscal_year=year, line_item="revenue",
            value=1_000.0 + offset, precedence=2, source="screener.in",
            created_at=now, updated_at=now,
        ))
        db.add(FinancialFact(
            company_id=canonical, fiscal_year=year, line_item="net_profit",
            value=100.0 + offset, precedence=2, source="screener.in",
            created_at=now, updated_at=now,
        ))

    # Version history on both rows — must merge without version collisions.
    db.add(CompanyVersion(
        company_id=canonical, version=1, change_type="create",
        summary="created", created_at=now, updated_at=now,
    ))
    db.add(CompanyVersion(
        company_id=dup, version=1, change_type="create",
        summary="created", created_at=now, updated_at=now,
    ))
    db.add(FinancialFactVersion(
        company_id=dup, version=1, change_type="import",
        summary="imported", created_at=now, updated_at=now,
    ))

    # Documents: dup has h1 (moves) and h2 (conflicts with canonical's h2 →
    # redundant twin: backed up, then removed with the duplicate).
    db.add(Document(
        company_id=canonical, filename="a.pdf", doc_type="annual_report",
        file_format="pdf", content_hash="h2", created_at=now, updated_at=now,
    ))
    db.add(Document(
        company_id=dup, filename="a.pdf", doc_type="annual_report",
        file_format="pdf", content_hash="h2", created_at=now, updated_at=now,
    ))
    db.add(Document(
        company_id=dup, filename="b.pdf", doc_type="annual_report",
        file_format="pdf", content_hash="h1", created_at=now, updated_at=now,
    ))

    # A user's watchlist entry pointing at the duplicate → repoint to canonical.
    watchlist = Watchlist(owner_id="user-1", name="Candidates",
                          created_at=now, updated_at=now)
    db.add(watchlist)
    db.flush()
    db.add(WatchlistEntry(
        watchlist_id=watchlist.id, company_id=dup, ticker="M&M",
        added_on=None, created_at=now, updated_at=now,
    ))

    # Quarters: dup's Q1 moves; canonical's Q2 is untouched.
    db.add(QuarterlyResult(
        company_id=dup, fiscal_year=2024, quarter=1, revenue=1.0,
        created_at=now, updated_at=now,
    ))
    db.add(QuarterlyResult(
        company_id=canonical, fiscal_year=2024, quarter=2, revenue=2.0,
        created_at=now, updated_at=now,
    ))

    # One-per-company snapshots on both → dup's is a redundant twin.
    db.add(DataQualitySnapshot(
        company_id=canonical, score=88.0, grade="A", computed_at=now,
        created_at=now, updated_at=now,
    ))
    db.add(DataQualitySnapshot(
        company_id=dup, score=12.0, grade="F", computed_at=now,
        created_at=now, updated_at=now,
    ))

    db.commit()
    return canonical, dup


class TestConfirmedShapeMerge:
    """The M&M scenario: one canonical row with 12 years, one empty twin."""

    @pytest.fixture()
    def migrated(self, tmp_path):
        from app.core.config import settings

        target = tmp_path / "confirmed.db"
        url = f"sqlite:///{target}"
        engine = _prev_schema_engine(url, settings)
        with engine.begin() as conn:
            session_factory = sa.orm.sessionmaker(bind=conn)
            with session_factory() as db:
                canonical, dup = _seed_confirmed_shape(db)
        _upgrade(_config(url), "head", settings)
        return engine, canonical, dup

    def test_financial_history_owner_survives_alone(self, migrated):
        engine, canonical, dup = migrated
        with engine.connect() as conn:
            rows = conn.execute(sa.text(
                "SELECT id, exchange, ticker, industry FROM companies "
                "WHERE ticker = 'M&M'"
            )).all()
            assert len(rows) == 1
            row = rows[0]
            assert row.id == canonical
            assert dup not in [r.id for r in rows]
            assert row.exchange == "NSE"          # Indian family normalised
            assert row.industry == "Automotive"   # metadata merged from dup

    def test_no_financial_history_is_lost_or_moved_to_the_wrong_row(
        self, migrated,
    ):
        engine, canonical, dup = migrated
        with engine.connect() as conn:
            total = conn.execute(sa.text(
                "SELECT COUNT(*) FROM financial_facts"
            )).scalar()
            on_canonical = conn.execute(
                sa.text("SELECT COUNT(*) FROM financial_facts "
                        "WHERE company_id = :c"),
                {"c": canonical},
            ).scalar()
            assert total == 24, "12 years x 2 items — every fact preserved"
            assert on_canonical == 24
            assert _dangling(conn, dup) == []

    def test_dependent_rows_migrated_and_redundant_twins_backed_up(
        self, migrated,
    ):
        engine, canonical, dup = migrated
        with engine.connect() as conn:
            # Documents: h1 moved over, h2 twin removed but backed up.
            docs = conn.execute(
                sa.text("SELECT content_hash FROM documents "
                        "WHERE company_id = :c ORDER BY content_hash"),
                {"c": canonical},
            ).scalars().all()
            assert docs == ["h1", "h2"]
            backed = conn.execute(
                sa.text("SELECT content_hash FROM company_merge_backup_documents "
                        "WHERE company_id = :d ORDER BY content_hash"),
                {"d": dup},
            ).scalars().all()
            assert backed == ["h1", "h2"]

            # Watchlist entry repointed to the canonical company.
            assert conn.execute(
                sa.text("SELECT COUNT(*) FROM watchlist_entries "
                        "WHERE company_id = :c AND ticker = 'M&M'"),
                {"c": canonical},
            ).scalar() == 1

            # Quarters: both rows now belong to the canonical company.
            quarters = conn.execute(
                sa.text("SELECT fiscal_year, quarter FROM quarterly_results "
                        "WHERE company_id = :c ORDER BY quarter"),
                {"c": canonical},
            ).all()
            assert quarters == [(2024, 1), (2024, 2)]

            # One-per-company snapshot: canonical kept its own; the dup's
            # twin was backed up rather than destroyed.
            assert conn.execute(
                sa.text("SELECT COUNT(*) FROM data_quality_snapshots "
                        "WHERE company_id = :c"),
                {"c": canonical},
            ).scalar() == 1
            assert conn.execute(
                sa.text("SELECT COUNT(*) FROM "
                        "company_merge_backup_data_quality_snapshots "
                        "WHERE company_id = :d"),
                {"d": dup},
            ).scalar() == 1

            # Version histories merged without collisions.
            versions = conn.execute(
                sa.text("SELECT version FROM company_versions "
                        "WHERE company_id = :c ORDER BY version"),
                {"c": canonical},
            ).scalars().all()
            assert versions == [1, 2]
            assert conn.execute(
                sa.text("SELECT COUNT(*) FROM financial_fact_versions "
                        "WHERE company_id = :c"),
                {"c": canonical},
            ).scalar() == 1

    def test_backup_and_audit_log_record_the_merge(self, migrated):
        engine, canonical, dup = migrated
        with engine.connect() as conn:
            assert conn.execute(
                sa.text("SELECT COUNT(*) FROM companies_pre_merge_backup "
                        "WHERE id = :d"),
                {"d": dup},
            ).scalar() == 1
            log_rows = conn.execute(
                sa.text("SELECT subject FROM company_merge_log "
                        "WHERE dup_id = :d ORDER BY subject"),
                {"d": dup},
            ).scalars().all()
            assert "companies" in log_rows
            assert "table:documents" in log_rows
            assert "table:watchlist_entries" in log_rows

    def test_case_insensitive_guard_index_exists(self, migrated):
        engine, _, _ = migrated
        with engine.connect() as conn:
            names = [r[1] for r in conn.execute(
                sa.text("PRAGMA index_list(companies)")
            ).all()]
            assert "uq_companies_exchange_ticker_ci" in names


class TestCaseVariantMerge:
    """'CASE1' and 'case1' on the same venue are the same company."""

    def test_case_variant_pair_merges_into_history_owner(self, tmp_path):
        from app.core.config import settings

        target = tmp_path / "case.db"
        url = f"sqlite:///{target}"
        engine = _prev_schema_engine(url, settings)
        canonical = str(uuid.uuid4())
        dup = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        with engine.begin() as conn:
            session_factory = sa.orm.sessionmaker(bind=conn)
            with session_factory() as db:
                db.add(Company(
                    id=canonical, name="Case Co", ticker="CASE1",
                    exchange="NSE", listing_status="active",
                    created_at=now, updated_at=now,
                ))
                db.add(FinancialFact(
                    company_id=canonical, fiscal_year=2020,
                    line_item="revenue", value=1.0, precedence=2,
                    created_at=now, updated_at=now,
                ))
                db.add(Company(
                    id=dup, name="case co", ticker="case1", exchange="NSE",
                    listing_status="active", created_at=now, updated_at=now,
                ))
                db.commit()
        _upgrade(_config(url), "head", settings)

        with engine.connect() as conn:
            rows = conn.execute(sa.text(
                "SELECT id FROM companies WHERE upper(ticker) = 'CASE1'"
            )).all()
            assert [r.id for r in rows] == [canonical]
            assert _dangling(conn, dup) == []


class TestFinancialHistoryInvariant:
    """If merging would strand a fact, the migration aborts — and the state
    stays exactly as it was, with the facts intact on both rows."""

    def test_conflicting_facts_abort_instead_of_delete(self, tmp_path):
        from app.core.config import settings

        target = tmp_path / "conflict.db"
        url = f"sqlite:///{target}"
        engine = _prev_schema_engine(url, settings)
        canonical = str(uuid.uuid4())
        dup = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        with engine.begin() as conn:
            session_factory = sa.orm.sessionmaker(bind=conn)
            with session_factory() as db:
                for cid in (canonical, dup):
                    db.add(Company(
                        id=cid, name="Clash Co", ticker="CLASH",
                        exchange="NSE" if cid == canonical else "BSE",
                        listing_status="active",
                        created_at=now, updated_at=now,
                    ))
                    db.add(FinancialFact(
                        company_id=cid, fiscal_year=2020, line_item="revenue",
                        value=999.0, precedence=2,
                        created_at=now, updated_at=now,
                    ))
                db.commit()
        original = settings.DATABASE_URL
        try:
            settings.DATABASE_URL = url
            with pytest.raises(RuntimeError, match="refusing to merge"):
                command.upgrade(_config(url), "head")
        finally:
            settings.DATABASE_URL = original

        with engine.connect() as conn:
            # Both rows and both facts are intact: nothing was deleted.
            assert conn.execute(sa.text(
                "SELECT COUNT(*) FROM companies WHERE ticker = 'CLASH'"
            )).scalar() == 2
            assert conn.execute(sa.text(
                "SELECT COUNT(*) FROM financial_facts"
            )).scalar() == 2
