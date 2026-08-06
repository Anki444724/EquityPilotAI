"""Phase 3 — financial statement admin tables.

Revision ID: 4c5d6e7f8091
Revises: 3b4c7d9e0f1a
Create Date: 2026-08-06

Adds ``corporate_actions`` and ``financial_fact_versions`` to support the
enterprise financial-statements module (editable statements, corporate actions,
and fact-level version history with rollback).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "4c5d6e7f8091"
down_revision = "3b4c7d9e0f1a"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return name in inspector.get_table_names()


def upgrade() -> None:
    if not _has_table("corporate_actions"):
        op.create_table(
            "corporate_actions",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("company_id", sa.String(36),
                      sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
            sa.Column("action_type", sa.String(24), nullable=False),
            sa.Column("ex_date", sa.Date(), nullable=True),
            sa.Column("record_date", sa.Date(), nullable=True),
            sa.Column("value", sa.Float(), nullable=True),
            sa.Column("description", sa.String(400), nullable=True),
            sa.Column("source", sa.String(120), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True),
                      server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True),
                      server_default=sa.func.now(), nullable=False),
        )
        op.create_index("ix_corporate_action_company_date",
                        "corporate_actions", ["company_id", "ex_date"])

    if not _has_table("financial_fact_versions"):
        op.create_table(
            "financial_fact_versions",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("company_id", sa.String(36),
                      sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("actor_id", sa.String(36), nullable=True),
            sa.Column("actor_email", sa.String(254), nullable=True),
            sa.Column("snapshot", sa.JSON(), nullable=True),
            sa.Column("change_type", sa.String(16), nullable=False, server_default="update"),
            sa.Column("summary", sa.String(400), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(timezone=True),
                      server_default=sa.func.now(), nullable=False),
        )
        op.create_index("ix_financial_fact_versions_company_id",
                        "financial_fact_versions", ["company_id"])
        op.create_index("ix_financial_fact_versions_created_at",
                        "financial_fact_versions", ["created_at"])
        op.create_unique_constraint("uq_financial_fact_version",
                                    "financial_fact_versions", ["company_id", "version"])


def downgrade() -> None:
    if _has_table("financial_fact_versions"):
        op.drop_table("financial_fact_versions")
    if _has_table("corporate_actions"):
        op.drop_table("corporate_actions")
