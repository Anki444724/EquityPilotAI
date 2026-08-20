"""The company deduplication migration, replayed on a real PostgreSQL server.

The SQLite suite in ``test_company_dedup_migration.py`` pins behaviour. It
cannot pin the two things that actually broke production, because both are
PostgreSQL semantics that SQLite does not share:

* a UNIQUE constraint is a unique *index*, maintained on write, so an UPDATE
  that changes an unrelated column still re-inserts the row's key into it and
  can collide with another live row;
* the PostgreSQL-only branches of the migration — catalogue discovery of
  referencing tables from ``pg_catalog``, ``ALTER TABLE … ADD CONSTRAINT``, the
  functional unique index on ``upper(ticker)``, ``UPDATE … FROM`` and
  ``IS NOT DISTINCT FROM`` — never execute at all under SQLite.

This module runs the migration against a genuine server, seeded with the exact
incident rows, in the exact state production was in.

It is skipped unless an embedded PostgreSQL is available. To run it::

    pip install pgserver
    pytest tests/test_company_dedup_migration_postgres.py -v

``pgserver`` is deliberately *not* in ``requirements.txt``: it is a test-time
convenience, not a runtime dependency.
"""
from __future__ import annotations

import importlib
import pathlib
import pkgutil
from datetime import datetime, timezone

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

pgserver = pytest.importorskip(
    "pgserver", reason="embedded PostgreSQL not installed (pip install pgserver)",
)

import app.models as models_pkg  # noqa: E402

for _module in pkgutil.iter_modules(models_pkg.__path__):
    importlib.import_module(f"app.models.{_module.name}")

from app.models.company import Company, FinancialFact  # noqa: E402
from app.models.document import Document  # noqa: E402

BACKEND = pathlib.Path(__file__).resolve().parent.parent
PREV_REVISION = "164253079db3"
HEAD_REVISION = "9f0b5e8c2d71"

#: The incident rows, verbatim.
CANONICAL_ID = "dff1781c-00be-4237-b545-4df26a58b2e0"
DUPLICATE_ID = "5868f82a-0195-4414-aabf-fc40fc2e1f37"
THIRD_ID = "9a2c4f10-7b31-4e08-9d55-6c1f2a83be47"
ISIN = "INE101A01026"

LINE_ITEMS = [
    "revenue", "ebitda", "ebit", "pat", "eps", "equity", "debt", "cash",
    "operating_cash_flow", "free_cash_flow",
]
CANONICAL_FACTS = 300      # 30 years x 10 line items
REDUNDANT_FACTS = 100      # 10 years x 10 line items, identical values


def _value(year: int, item: str) -> float:
    return round(1000 + year + len(item) * 7.5, 4)


@pytest.fixture(scope="module")
def pg_url(tmp_path_factory) -> str:
    server = pgserver.get_server(str(tmp_path_factory.mktemp("pgdata")))
    return server.get_uri().replace("postgresql://", "postgresql+psycopg://")


def _config(url: str) -> Config:
    config = Config(str(BACKEND / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    return config


@pytest.fixture(scope="module")
def migrated(pg_url):
    """Seed production's exact state, then run the upgrade against it."""
    from app.core.config import settings
    from app.db.base import Base

    engine = sa.create_engine(pg_url)
    with engine.begin() as conn:
        conn.execute(sa.text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
    Base.metadata.create_all(bind=engine)

    with engine.begin() as conn:
        # The migration under test creates this one.
        conn.execute(sa.text("DROP INDEX IF EXISTS uq_companies_exchange_ticker_ci"))
        # Production holds rows the identity constraint should have prevented,
        # so the constraint is removed here in order to seed them.
        conn.execute(sa.text(
            "ALTER TABLE companies DROP CONSTRAINT uq_company_ticker_exchange"
        ))

    can_t = datetime(2026, 8, 19, tzinfo=timezone.utc)
    dup_t = datetime(2026, 8, 17, tzinfo=timezone.utc)
    third_t = datetime(2026, 8, 15, tzinfo=timezone.utc)
    with sa.orm.sessionmaker(bind=engine)() as db:
        db.add(Company(
            id=CANONICAL_ID, name="Mahindra & Mahindra Ltd", ticker="M&M",
            exchange="NSE", isin=None, listing_status="active", data_version=2,
            created_at=can_t, updated_at=can_t,
        ))
        db.add(Company(
            id=DUPLICATE_ID, name="Mahindra and Mahindra", ticker="M&M",
            exchange="NSE", isin=ISIN, industry="Passenger Vehicles",
            listing_status="active", data_version=1,
            created_at=dup_t, updated_at=dup_t,
        ))
        db.add(Company(
            id=THIRD_ID, name="Mahindra & Mahindra", ticker="M&M",
            exchange="BSE", isin=None, listing_status="active", data_version=1,
            created_at=third_t, updated_at=third_t,
        ))
        db.flush()
        for year in range(1996, 2026):
            for item in LINE_ITEMS:
                db.add(FinancialFact(
                    company_id=CANONICAL_ID, fiscal_year=year, line_item=item,
                    value=_value(year, item), precedence=2,
                    source="screener.in", created_at=can_t, updated_at=can_t,
                ))
        for year in range(2016, 2026):
            for item in LINE_ITEMS:
                db.add(FinancialFact(
                    company_id=THIRD_ID, fiscal_year=year, line_item=item,
                    value=_value(year, item), precedence=2,
                    source="screener.in", created_at=third_t, updated_at=third_t,
                ))
        db.add(Document(
            company_id=DUPLICATE_ID, filename="ar.pdf", doc_type="annual_report",
            file_format="pdf", content_hash="dup-h1",
            created_at=dup_t, updated_at=dup_t,
        ))
        db.commit()

    with engine.begin() as conn:
        # Restore the index in the state production had it: enforcing writes,
        # never validated against the rows already present. This is what a
        # failed CREATE INDEX CONCURRENTLY leaves behind.
        conn.execute(sa.text(
            "CREATE INDEX uq_company_ticker_exchange ON companies (ticker, exchange)"
        ))
        conn.execute(sa.text(
            "UPDATE pg_index SET indisunique = true "
            "WHERE indexrelid = 'uq_company_ticker_exchange'::regclass"
        ))
    engine.dispose()
    engine = sa.create_engine(pg_url)

    original = settings.DATABASE_URL
    try:
        settings.DATABASE_URL = pg_url
        command.stamp(_config(pg_url), PREV_REVISION)
        command.upgrade(_config(pg_url), "head")
    finally:
        settings.DATABASE_URL = original
    return engine


class TestPostgresIncidentReplay:
    """Every failure mode the three production runs hit, on a real server."""

    def test_the_failing_statement_is_reproducible(self, pg_url):
        """The precise error, before the migration runs.

        Seeded independently of `migrated` so the assertion stands on its own:
        an UPDATE of `isin` is rejected by a constraint on (ticker, exchange).

        The unique index on `isin` is what makes this deterministic. An UPDATE
        that touches an indexed column cannot be applied as a heap-only tuple,
        so PostgreSQL must insert a fresh entry into *every* index on the
        table — including the one on (ticker, exchange), where the key already
        belongs to the canonical row. On production's `companies` table `isin`
        carries `companies_isin_key`, so the ISIN release could never be HOT
        and the collision was certain.
        """
        engine = sa.create_engine(pg_url)
        with engine.begin() as conn:
            conn.execute(sa.text("DROP TABLE IF EXISTS probe_companies"))
            conn.execute(sa.text(
                "CREATE TABLE probe_companies "
                "(id text PRIMARY KEY, ticker text, exchange text, isin text)"
            ))
            conn.execute(sa.text(
                "INSERT INTO probe_companies VALUES "
                "(:can,'M&M','NSE',NULL), (:dup,'M&M','NSE',:isin)"
            ), {"can": CANONICAL_ID, "dup": DUPLICATE_ID, "isin": ISIN})
            # Mirrors companies_isin_key: updating isin is therefore not HOT.
            conn.execute(sa.text(
                "CREATE UNIQUE INDEX uq_probe_isin ON probe_companies (isin)"
            ))
            conn.execute(sa.text(
                "CREATE INDEX uq_probe_ticker_exchange "
                "ON probe_companies (ticker, exchange)"
            ))
            conn.execute(sa.text(
                "UPDATE pg_index SET indisunique = true "
                "WHERE indexrelid = 'uq_probe_ticker_exchange'::regclass"
            ))
        engine.dispose()
        engine = sa.create_engine(pg_url)

        with pytest.raises(sa.exc.IntegrityError) as excinfo:
            with engine.begin() as conn:
                conn.execute(sa.text(
                    "UPDATE probe_companies SET isin = NULL WHERE id = :dup"
                ), {"dup": DUPLICATE_ID})
        message = str(excinfo.value)
        assert "uq_probe_ticker_exchange" in message
        assert "(ticker, exchange)=(M&M, NSE)" in message

    def test_the_upgrade_reaches_head(self, migrated):
        with migrated.connect() as conn:
            assert conn.execute(sa.text(
                "SELECT version_num FROM alembic_version"
            )).scalar() == HEAD_REVISION

    def test_one_company_survives_with_its_identity_and_isin(self, migrated):
        with migrated.connect() as conn:
            rows = conn.execute(sa.text(
                "SELECT id, ticker, exchange, isin, industry FROM companies"
            )).all()
            assert len(rows) == 1
            row = rows[0]
            assert row.id == CANONICAL_ID
            assert (row.ticker, row.exchange) == ("M&M", "NSE")
            assert row.isin == ISIN
            assert row.industry == "Passenger Vehicles"

    def test_every_distinct_fact_survives_untouched(self, migrated):
        with migrated.connect() as conn:
            assert conn.execute(sa.text(
                "SELECT count(*) FROM financial_facts"
            )).scalar() == CANONICAL_FACTS
            assert float(conn.execute(sa.text(
                "SELECT value FROM financial_facts "
                "WHERE fiscal_year = 2020 AND line_item = 'revenue'"
            )).scalar()) == _value(2020, "revenue")

    def test_redundant_copies_are_backed_up_and_logged(self, migrated):
        with migrated.connect() as conn:
            assert conn.execute(sa.text(
                "SELECT count(*) FROM company_merge_backup_financial_facts "
                "WHERE company_id = :cid"
            ), {"cid": THIRD_ID}).scalar() == REDUNDANT_FACTS
            assert conn.execute(sa.text(
                "SELECT conflicting FROM company_merge_log "
                "WHERE subject = 'redundant:financial_facts' AND dup_id = :cid"
            ), {"cid": THIRD_ID}).scalar() == REDUNDANT_FACTS

    def test_dependents_moved_and_backups_hold_the_real_tickers(self, migrated):
        with migrated.connect() as conn:
            assert conn.execute(sa.text(
                "SELECT count(*) FROM documents WHERE company_id = :cid"
            ), {"cid": CANONICAL_ID}).scalar() == 1
            assert conn.execute(sa.text(
                "SELECT count(*) FROM companies_pre_merge_backup "
                "WHERE ticker = 'M&M'"
            )).scalar() == 3
            assert conn.execute(sa.text(
                "SELECT count(*) FROM companies WHERE ticker LIKE '~DUP~%'"
            )).scalar() == 0

    def test_all_three_constraints_exist_afterwards(self, migrated):
        with migrated.connect() as conn:
            constraints = set(conn.execute(sa.text(
                "SELECT conname FROM pg_constraint "
                "WHERE conrelid = 'companies'::regclass"
            )).scalars())
            assert "uq_company_ticker_exchange" in constraints
            assert "companies_isin_key" in constraints
            assert conn.execute(sa.text(
                "SELECT count(*) FROM pg_indexes "
                "WHERE indexname = 'uq_companies_exchange_ticker_ci'"
            )).scalar() == 1

    @pytest.mark.parametrize(
        "columns,values,constraint",
        [
            ("ticker, exchange", "'M&M', 'NSE'", "uq_company_ticker_exchange"),
            ("ticker, exchange, isin", f"'CLONE', 'NSE', '{ISIN}'", "companies_isin_key"),
            ("ticker, exchange", "'m&m', 'NSE'", "uq_companies_exchange_ticker_ci"),
        ],
    )
    def test_the_constraints_actually_enforce(self, migrated, columns, values,
                                              constraint):
        with pytest.raises(sa.exc.IntegrityError) as excinfo:
            with migrated.begin() as conn:
                conn.execute(sa.text(
                    f"INSERT INTO companies (id, name, {columns}, "
                    " listing_status, data_version, created_at, updated_at) "
                    f"VALUES (gen_random_uuid()::text, 'Impostor', {values}, "
                    "        'active', 1, now(), now())"
                ))
        assert constraint in str(excinfo.value)
