"""data quality snapshots

Revision ID: e4a9c62d7b13
Revises: d1e7b3c85f96
Create Date: 2026-08-02

Persists the Data Quality Score per company so the dashboard can aggregate
across 500 companies in one query. Scoring a single company takes ~0.7s,
which is fine on demand and far too slow for a leaderboard.

One row per company, updated in place — this is a derived measurement of
current state, not an assertion about the company, so a version history would
grow without ever being read. The score is always recomputable from the
underlying rows, which are themselves versioned.

`created_at` and `updated_at` are declared explicitly: `Base` defines them on
every model, and omitting them produces `UndefinedColumn` in production while
staying invisible locally, because the test schema is built by `create_all()`
from the models rather than by replaying migrations (MIG-001).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "e4a9c62d7b13"
down_revision: str | None = "d1e7b3c85f96"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "data_quality_snapshots",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("company_id", sa.String(length=36), nullable=False),
        sa.Column("score", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("grade", sa.String(length=2), nullable=False,
                  server_default="F"),
        sa.Column("identity_points", sa.Float(), nullable=True),
        sa.Column("financials_points", sa.Float(), nullable=True),
        sa.Column("documents_points", sa.Float(), nullable=True),
        sa.Column("vault_points", sa.Float(), nullable=True),
        sa.Column("ai_points", sa.Float(), nullable=True),
        sa.Column("freshness_points", sa.Float(), nullable=True),
        sa.Column("source_points", sa.Float(), nullable=True),
        sa.Column("health_points", sa.Float(), nullable=True),
        sa.Column("missing_count", sa.Integer(), nullable=False,
                  server_default=sa.text("0")),
        sa.Column("missing_items", sa.JSON(), nullable=True),
        sa.Column("last_updated_days", sa.Integer(), nullable=True),
        sa.Column("knowledge_freshness_days", sa.Integer(), nullable=True),
        sa.Column("computed_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"],
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("company_id", name="uq_quality_company"),
    )
    op.create_index("ix_data_quality_snapshots_company_id",
                    "data_quality_snapshots", ["company_id"])
    op.create_index("ix_data_quality_snapshots_score",
                    "data_quality_snapshots", ["score"])
    op.create_index("ix_data_quality_snapshots_grade",
                    "data_quality_snapshots", ["grade"])
    op.create_index("ix_quality_leaderboard", "data_quality_snapshots",
                    ["score", "grade"])


def downgrade() -> None:
    op.drop_index("ix_quality_leaderboard", table_name="data_quality_snapshots")
    op.drop_index("ix_data_quality_snapshots_grade",
                  table_name="data_quality_snapshots")
    op.drop_index("ix_data_quality_snapshots_score",
                  table_name="data_quality_snapshots")
    op.drop_index("ix_data_quality_snapshots_company_id",
                  table_name="data_quality_snapshots")
    op.drop_table("data_quality_snapshots")
