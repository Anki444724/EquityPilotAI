"""Add the audit timestamps missing from the filing-collection tables.

Revision ID: d5f83c1e6a27
Revises: c4e2a91b7d38
Create Date: 2026-08-01

`Base` declares `created_at` and `updated_at` on every model, so they are part
of each table's contract rather than optional bookkeeping. The hand-written
migration `c4e2a91b7d38` omitted them.

This was invisible locally: the test suite builds its schema with
`create_all()` from the models, which supplies the inherited columns
automatically, so 58 new tests and 2,253 total passed against a schema the
migration would never produce. On Postgres, where the migration *is* the
schema, every query against these tables failed with

    UndefinedColumn: column discovered_filings.created_at does not exist

and the three new endpoints returned 500.

`c4e2a91b7d38` has been corrected too, so a fresh database gets the columns in
one step; this migration exists for databases that already ran the broken
version. Guarded by an existence check so it is safe either way.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d5f83c1e6a27"
down_revision = "c4e2a91b7d38"
branch_labels = None
depends_on = None

_TABLES = ("discovered_filings", "company_crawl_state")
_COLUMNS = ("created_at", "updated_at")


def _existing(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    for table in _TABLES:
        present = _existing(table)
        for column in _COLUMNS:
            if column in present:
                # A database created from the corrected c4e2a91b7d38 already
                # has them. Adding again would abort the whole migration.
                continue
            op.add_column(
                table,
                sa.Column(column, sa.DateTime(timezone=True),
                          server_default=sa.func.now(), nullable=False),
            )


def downgrade() -> None:
    for table in _TABLES:
        present = _existing(table)
        for column in _COLUMNS:
            if column in present:
                op.drop_column(table, column)
