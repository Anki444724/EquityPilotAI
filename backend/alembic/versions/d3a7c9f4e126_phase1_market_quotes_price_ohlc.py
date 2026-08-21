"""Phase 1: persistent quotes + OHLC price history.

Revision ID: d3a7c9f4e126
Revises: c2f6b8e3d015
Create Date: 2026-08-21

Two additive changes:

1. `market_quotes` — one row per company holding the latest persisted quote
   (LTP, previous close, OHLC, volume, change, 52-week range, market status,
   provider, fetched_at). Redis stays the fast serving cache; this table is
   the durable record the cache expires back to.

2. `price_history` gains day_open/day_high/day_low/provider, all nullable:
   the pre-existing demo rows were close-only and are preserved as-is, and a
   figure a source does not report is stored as absent, never fabricated.
   The existing UNIQUE (ticker, as_of) is untouched — it is exactly the
   conflict key that makes historical upsert idempotent.

No existing table or constraint is dropped or altered. Rollback drops the new
table and columns; `price_history.close`-only data survives either way.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d3a7c9f4e126"
down_revision = "c2f6b8e3d015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "market_quotes",
        sa.Column("company_id", sa.String(length=36), primary_key=True),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("exchange", sa.String(length=16), nullable=False),
        sa.Column("ltp", sa.Float(), nullable=True),
        sa.Column("previous_close", sa.Float(), nullable=True),
        sa.Column("day_open", sa.Float(), nullable=True),
        sa.Column("day_high", sa.Float(), nullable=True),
        sa.Column("day_low", sa.Float(), nullable=True),
        sa.Column("volume", sa.Float(), nullable=True),
        sa.Column("change_amt", sa.Float(), nullable=True),
        sa.Column("change_percent", sa.Float(), nullable=True),
        sa.Column("week_52_high", sa.Float(), nullable=True),
        sa.Column("week_52_low", sa.Float(), nullable=True),
        sa.Column("market_status", sa.String(length=12), nullable=False,
                  server_default="unknown"),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("meta", sa.JSON(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        # Audit columns every table carries (see tests/test_migrations.py —
        # the defect that suite exists for).
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
    )
    op.create_index(
        "ix_market_quotes_stalest", "market_quotes", ["fetched_at"],
    )

    with op.batch_alter_table("price_history") as batch:
        batch.add_column(sa.Column("day_open", sa.Float(), nullable=True))
        batch.add_column(sa.Column("day_high", sa.Float(), nullable=True))
        batch.add_column(sa.Column("day_low", sa.Float(), nullable=True))
        batch.add_column(sa.Column("provider", sa.String(length=64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("price_history") as batch:
        batch.drop_column("provider")
        batch.drop_column("day_low")
        batch.drop_column("day_high")
        batch.drop_column("day_open")
    op.drop_index("ix_market_quotes_stalest", table_name="market_quotes")
    op.drop_table("market_quotes")
