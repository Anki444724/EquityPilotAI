"""Phase 1: company-master provenance columns.

Revision ID: b1e5a7d2c904
Revises: 164253079db3
Create Date: 2026-08-21

Additive only. `companies.metadata_source` records which master supplied the
identity row and `companies.metadata_synced_at` when it last refreshed, so
`company_universe_sync` can upsert without guessing where a row came from, and
`company_metadata_sync` can pick the stalest rows first.

Identity is NOT touched: `ticker` remains the NSE symbol, `bse_code` the BSE
scrip code, `isin` unique, `(ticker, exchange)` unique. No existing company
changes id, ticker or exchange. Both new columns are nullable, so every
pre-existing row is valid without a backfill (NULL means "written before
provenance was tracked", which is the truth).

Rollback: drop the two columns. No data outside them is lost.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b1e5a7d2c904"
down_revision = "164253079db3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "companies",
        sa.Column("metadata_source", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "companies",
        sa.Column("metadata_synced_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("companies", "metadata_synced_at")
    op.drop_column("companies", "metadata_source")
