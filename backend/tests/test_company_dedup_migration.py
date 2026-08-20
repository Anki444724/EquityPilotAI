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


# ==========================================================================
# The production ISIN conflict
# ==========================================================================

#: The exact rows from the production incident report.
PROD_CANONICAL_ID = "dff1781c-00be-4237-b545-4df26a58b2e0"
PROD_DUPLICATE_ID = "5868f82a-0195-4414-aabf-fc40fc2e1f37"
PROD_ISIN = "INE101A01026"


class TestProductionIsinTransfer:
    """The duplicate owns the unique ISIN; the canonical row owns the history.

    This is the shape that broke the production upgrade::

        sqlalchemy.exc.IntegrityError: duplicate key value violates unique
        constraint "companies_isin_key"
        DETAIL:  Key (isin)=(INE101A01026) already exists.

    ``companies.isin`` is UNIQUE table-wide. The merge wanted to copy the
    duplicate's ISIN onto the canonical row, but the duplicate still held it,
    so the UPDATE collided with the constraint the instant it ran. The fix
    releases the ISIN from the duplicate first, then assigns it — which is
    what these tests pin.

    Note on the seeded exchanges: production held both rows on NSE because
    ``uq_company_ticker_exchange`` was not effective for them. The pre-fix
    test schema *does* enforce it, so the pair is seeded NSE/BSE, exactly as
    ``_seed_confirmed_shape`` does and for the same reason. The ISIN
    dimension under test is reproduced verbatim: same two ids, same ISIN, on
    the same sides of the pair, and the newer row is the history owner just
    as it was in production.
    """

    @staticmethod
    def _seed(db) -> None:
        canonical_created = datetime(2026, 8, 19, tzinfo=timezone.utc)
        duplicate_created = datetime(2026, 8, 17, tzinfo=timezone.utc)

        # Canonical: owns the financial history, holds no ISIN.
        db.add(Company(
            id=PROD_CANONICAL_ID, name="Mahindra & Mahindra Ltd", ticker="M&M",
            exchange="NSE", isin=None, sector="Automobile",
            listing_status="active", data_version=2,
            created_at=canonical_created, updated_at=canonical_created,
        ))
        # Duplicate: older, no history, owns the unique ISIN.
        db.add(Company(
            id=PROD_DUPLICATE_ID, name="Mahindra and Mahindra", ticker="M&M",
            exchange="BSE", isin=PROD_ISIN, industry="Passenger Vehicles",
            listing_status="active", data_version=1,
            created_at=duplicate_created, updated_at=duplicate_created,
        ))
        db.flush()

        for offset in range(9):
            year = 2017 + offset
            db.add(FinancialFact(
                company_id=PROD_CANONICAL_ID, fiscal_year=year,
                line_item="revenue", value=100_000.0 + offset, precedence=2,
                source="screener.in",
                created_at=canonical_created, updated_at=canonical_created,
            ))
        db.commit()

    @pytest.fixture()
    def migrated(self, tmp_path):
        from app.core.config import settings

        url = f"sqlite:///{tmp_path / 'prod_isin.db'}"
        engine = _prev_schema_engine(url, settings)
        with engine.begin() as conn:
            session_factory = sa.orm.sessionmaker(bind=conn)
            with session_factory() as db:
                self._seed(db)
        _upgrade(_config(url), "head", settings)
        return engine

    def test_the_upgrade_completes_instead_of_violating_companies_isin_key(
        self, migrated,
    ):
        """Before the fix this raised IntegrityError on companies_isin_key."""
        with migrated.connect() as conn:
            assert conn.execute(sa.text(
                "SELECT COUNT(*) FROM companies WHERE ticker = 'M&M'"
            )).scalar() == 1

    def test_the_canonical_company_survives_and_owns_the_isin(self, migrated):
        with migrated.connect() as conn:
            row = conn.execute(sa.text(
                "SELECT id, isin, exchange, industry FROM companies "
                "WHERE ticker = 'M&M'"
            )).one()
            assert row.id == PROD_CANONICAL_ID
            assert row.isin == PROD_ISIN
            # Indian family normalised, and the duplicate's metadata merged.
            assert row.exchange == "NSE"
            assert row.industry == "Passenger Vehicles"

    def test_the_duplicate_row_is_gone_and_leaves_no_references(
        self, migrated,
    ):
        with migrated.connect() as conn:
            assert conn.execute(sa.text(
                "SELECT COUNT(*) FROM companies WHERE id = :dup"
            ), {"dup": PROD_DUPLICATE_ID}).scalar() == 0
            session_factory = sa.orm.sessionmaker(bind=conn)
            with session_factory() as db:
                assert _dangling(db, PROD_DUPLICATE_ID) == []

    def test_the_isin_belongs_to_exactly_one_company(self, migrated):
        with migrated.connect() as conn:
            owners = conn.execute(sa.text(
                "SELECT id FROM companies WHERE isin = :isin"
            ), {"isin": PROD_ISIN}).scalars().all()
            assert owners == [PROD_CANONICAL_ID]

    def test_the_unique_isin_constraint_is_still_enforced(self, migrated):
        """The constraint is never dropped or deferred to get the merge through."""
        with migrated.connect() as conn:
            with pytest.raises(sa.exc.IntegrityError):
                conn.execute(sa.text(
                    "INSERT INTO companies "
                    "(id, name, ticker, exchange, isin, listing_status, "
                    " data_version, created_at, updated_at) "
                    "VALUES (:id, 'Impostor Ltd', 'IMPOST', 'NSE', :isin, "
                    "        'active', 1, :now, :now)"
                ), {
                    "id": str(uuid.uuid4()), "isin": PROD_ISIN,
                    "now": datetime.now(timezone.utc),
                })

    def test_no_duplicate_ticker_exchange_identity_remains(self, migrated):
        with migrated.connect() as conn:
            clashes = conn.execute(sa.text(
                "SELECT upper(ticker) AS t, exchange, COUNT(*) AS n "
                "FROM companies GROUP BY upper(ticker), exchange HAVING n > 1"
            )).all()
            assert clashes == []

    def test_the_financial_history_stayed_on_the_canonical_row(self, migrated):
        with migrated.connect() as conn:
            assert conn.execute(sa.text(
                "SELECT COUNT(*) FROM financial_facts WHERE company_id = :cid"
            ), {"cid": PROD_CANONICAL_ID}).scalar() == 9


class TestIsinTransferRules:
    """`_transfer_isin` in isolation: ordering, idempotency, and the guards.

    The migration module is loaded by path because ``alembic/versions`` is not
    an importable package.
    """

    @staticmethod
    def _migration_module():
        import importlib.util

        path = (BACKEND / "alembic" / "versions"
                / "9f0b5e8c2d71_company_deduplication_and_unique_identity.py")
        spec = importlib.util.spec_from_file_location("dedup_migration", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    @staticmethod
    def _table(engine, unique_isin: bool) -> None:
        constraint = " UNIQUE" if unique_isin else ""
        with engine.begin() as conn:
            conn.execute(sa.text(
                f"CREATE TABLE companies (id TEXT PRIMARY KEY, "
                f"isin TEXT{constraint})"
            ))

    def _rows(self, conn) -> dict[str, str | None]:
        return {
            r.id: r.isin
            for r in conn.execute(sa.text("SELECT id, isin FROM companies"))
        }

    def test_transfer_releases_before_assigning(self):
        """The whole point: no UNIQUE violation while both rows exist."""
        module = self._migration_module()
        engine = sa.create_engine("sqlite://")
        self._table(engine, unique_isin=True)
        with engine.begin() as conn:
            conn.execute(sa.text(
                "INSERT INTO companies VALUES ('can', NULL), ('dup', :isin)"
            ), {"isin": PROD_ISIN})
            module._transfer_isin(conn, "can", "dup")
            assert self._rows(conn) == {"can": PROD_ISIN, "dup": None}

    def test_transfer_is_idempotent(self):
        module = self._migration_module()
        engine = sa.create_engine("sqlite://")
        self._table(engine, unique_isin=True)
        with engine.begin() as conn:
            conn.execute(sa.text(
                "INSERT INTO companies VALUES ('can', NULL), ('dup', :isin)"
            ), {"isin": PROD_ISIN})
            module._transfer_isin(conn, "can", "dup")
            module._transfer_isin(conn, "can", "dup")
            module._transfer_isin(conn, "can", "dup")
            assert self._rows(conn) == {"can": PROD_ISIN, "dup": None}

    def test_canonical_keeps_the_isin_it_already_has(self):
        module = self._migration_module()
        engine = sa.create_engine("sqlite://")
        self._table(engine, unique_isin=True)
        with engine.begin() as conn:
            conn.execute(sa.text(
                "INSERT INTO companies VALUES ('can', 'INE000A01001'), "
                "('dup', 'INE999Z01009')"
            ))
            module._transfer_isin(conn, "can", "dup")
            assert self._rows(conn) == {
                "can": "INE000A01001", "dup": "INE999Z01009",
            }

    def test_nothing_to_transfer_when_the_duplicate_has_no_isin(self):
        module = self._migration_module()
        engine = sa.create_engine("sqlite://")
        self._table(engine, unique_isin=True)
        with engine.begin() as conn:
            conn.execute(sa.text(
                "INSERT INTO companies VALUES ('can', NULL), ('dup', NULL)"
            ))
            module._transfer_isin(conn, "can", "dup")
            assert self._rows(conn) == {"can": None, "dup": None}

    def test_a_third_owner_of_the_isin_leaves_both_rows_untouched(self):
        """Defensive: only reachable on a database where UNIQUE was lost."""
        module = self._migration_module()
        engine = sa.create_engine("sqlite://")
        self._table(engine, unique_isin=False)
        with engine.begin() as conn:
            conn.execute(sa.text(
                "INSERT INTO companies VALUES ('can', NULL), ('dup', :isin), "
                "('other', :isin)"
            ), {"isin": PROD_ISIN})
            module._transfer_isin(conn, "can", "dup")
            assert self._rows(conn) == {
                "can": None, "dup": PROD_ISIN, "other": PROD_ISIN,
            }
