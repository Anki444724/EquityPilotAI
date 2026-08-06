"""Phase 5 — AI operations tables.

Revision ID: 6e7f8091a2b3
Revises: 5d6e7f8091a2
Create Date: 2026-08-06

Adds ``ai_overrides`` for the manual AI-score override control center.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "6e7f8091a2b3"
down_revision = "5d6e7f8091a2"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return name in inspector.get_table_names()


def upgrade() -> None:
    if _has_table("ai_overrides"):
        return
    op.create_table(
        "ai_overrides",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("company_id", sa.String(36),
                  sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("ticker", sa.String(32), nullable=False),
        sa.Column("mode", sa.String(12), nullable=False, server_default="auto"),
        sa.Column("manual_score", sa.Float(), nullable=True),
        sa.Column("manual_confidence", sa.Float(), nullable=True),
        sa.Column("manual_risk", sa.Float(), nullable=True),
        sa.Column("manual_summary", sa.Text(), nullable=True),
        sa.Column("manual_bull_case", sa.Text(), nullable=True),
        sa.Column("manual_bear_case", sa.Text(), nullable=True),
        sa.Column("manual_recommendation", sa.String(32), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(36), nullable=True),
        sa.Column("created_by_email", sa.String(254), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_ai_override_active", "ai_overrides", ["company_id", "expires_at"])
    op.create_index("ix_ai_override_ticker", "ai_overrides", ["ticker"])


def downgrade() -> None:
    if _has_table("ai_overrides"):
        op.drop_table("ai_overrides")
