"""Phase 4 — market operations tables.

Revision ID: 5d6e7f8091a2
Revises: 4c5d6e7f8091
Create Date: 2026-08-06

Adds ``market_overrides`` for the manual market override control center.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "5d6e7f8091a2"
down_revision = "4c5d6e7f8091"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return name in inspector.get_table_names()


def upgrade() -> None:
    if _has_table("market_overrides"):
        return
    op.create_table(
        "market_overrides",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("company_id", sa.String(36),
                  sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("ticker", sa.String(32), nullable=False),
        sa.Column("manual_price", sa.Float(), nullable=True),
        sa.Column("manual_volume", sa.Float(), nullable=True),
        sa.Column("manual_market_cap", sa.Float(), nullable=True),
        sa.Column("manual_pe", sa.Float(), nullable=True),
        sa.Column("manual_pb", sa.Float(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("auto_revert", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("created_by", sa.String(36), nullable=True),
        sa.Column("created_by_email", sa.String(254), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_market_override_active", "market_overrides",
                    ["company_id", "expires_at"])
    op.create_index("ix_market_override_ticker", "market_overrides", ["ticker"])


def downgrade() -> None:
    if _has_table("market_overrides"):
        op.drop_table("market_overrides")
