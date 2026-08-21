"""Phase 1: trigram indexes for 5,000-company search.

Revision ID: f5c9e1b6a348
Revises: e4b8d0a5f237
Create Date: 2026-08-21

Search matches substrings (`LIKE '%q%'` semantics) across name, ticker and
ISIN. A btree index cannot serve a leading-wildcard pattern, so Postgres
would sequential-scan `companies` on every keystroke — invisible at 507 rows,
measurable at 5,000. pg_trgm GIN indexes are the justified fix: trigram
indexes exist precisely for substring similarity/LIKE queries.

Postgres-only. The migration is dialect-guarded so the SQLite test path (and
any non-PG deployment) skips it cleanly rather than failing; the query itself
is unchanged and correct on both engines — this revision only buys Postgres
an index plan. `EQUITY_L`-scale proof numbers are in docs/PHASE1_5000_UNIVERSE.md.

Rollback: drop the three GIN indexes (and the extension only if nothing else
uses it — dropping indexes is sufficient, so the extension is left in place;
`CREATE EXTENSION IF NOT EXISTS` is idempotent).
"""
from __future__ import annotations

from alembic import op

revision = "f5c9e1b6a348"
down_revision = "e4b8d0a5f237"
branch_labels = None
depends_on = None

#: GIN trigram indexes, matching the columns company search ORs across.
_TRGM_INDEXES: tuple[tuple[str, str], ...] = (
    ("ix_company_name_trgm", "name"),
    ("ix_company_ticker_trgm", "ticker"),
    ("ix_company_isin_trgm", "isin"),
)


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgres():
        return
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    for index_name, column in _TRGM_INDEXES:
        op.execute(
            f'CREATE INDEX IF NOT EXISTS "{index_name}" '
            f'ON companies USING gin ("{column}" gin_trgm_ops)'
        )


def downgrade() -> None:
    if not _is_postgres():
        return
    for index_name, _column in reversed(_TRGM_INDEXES):
        op.execute(f'DROP INDEX IF EXISTS "{index_name}"')
