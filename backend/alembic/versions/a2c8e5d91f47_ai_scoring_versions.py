"""AI Scoring Engine 3.0 — permanent score version history

Revision ID: a2c8e5d91f47
Revises: f7b2d94e15c8
Create Date: 2026-08-03

One table, append-only. Every run of the scoring engine that saw new evidence
is retained permanently, so "what did we say about this company in March, and
on what basis?" stays answerable after the evidence itself has moved on.

The unique constraint on (company_id, version) is the enforcement mechanism for
the brief's "historical scores must never be overwritten": an UPDATE path
simply does not exist in the service, and an accidental one would collide here
rather than silently replacing a row.

`created_at` and `updated_at` are declared explicitly. `Base` declares them on
every table, and a migration that omits them produces a schema Alembic thinks
is current and SQLAlchemy cannot insert into — the MIG-001 defect from the
filing-collection work, repeated here deliberately as a reminder not to.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "a2c8e5d91f47"
down_revision: str | None = "f7b2d94e15c8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_score_versions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("company_id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=12), nullable=False,
                  server_default="current"),
        sa.Column("framework_version", sa.String(length=16), nullable=False),
        sa.Column("overall_score", sa.Float(), nullable=False),
        sa.Column("rating", sa.String(length=4), nullable=False),
        sa.Column("recommendation", sa.String(length=16), nullable=False),
        sa.Column("coverage", sa.Float(), nullable=False, server_default="0"),
        sa.Column("module_scores", sa.JSON(), nullable=False),
        sa.Column("probabilities", sa.JSON(), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("recommendation_reason", sa.Text(), nullable=True),
        sa.Column("input_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("total_citations", sa.Integer(), nullable=False,
                  server_default="0"),
        sa.Column("trigger", sa.String(length=24), nullable=False,
                  server_default="manual"),
        sa.Column("trigger_document_id", sa.Integer(), nullable=True),
        sa.Column("supersedes_version", sa.Integer(), nullable=True),
        sa.Column("score_delta", sa.Float(), nullable=True),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"],
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["trigger_document_id"], ["documents.id"],
                                ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("company_id", "version",
                            name="uq_ai_score_version"),
    )
    op.create_index("ix_ai_score_versions_company_id", "ai_score_versions",
                    ["company_id"])
    op.create_index("ix_ai_score_versions_status", "ai_score_versions",
                    ["status"])
    op.create_index("ix_ai_score_versions_rating", "ai_score_versions",
                    ["rating"])
    op.create_index("ix_ai_score_versions_recommendation", "ai_score_versions",
                    ["recommendation"])
    op.create_index("ix_ai_score_versions_overall_score", "ai_score_versions",
                    ["overall_score"])
    op.create_index("ix_ai_score_versions_input_fingerprint",
                    "ai_score_versions", ["input_fingerprint"])
    op.create_index("ix_ai_score_versions_trigger", "ai_score_versions",
                    ["trigger"])
    op.create_index("ix_ai_score_versions_computed_at", "ai_score_versions",
                    ["computed_at"])
    op.create_index("ix_ai_score_current", "ai_score_versions",
                    ["company_id", "status"])
    op.create_index("ix_ai_score_history", "ai_score_versions",
                    ["company_id", "computed_at"])


def downgrade() -> None:
    op.drop_index("ix_ai_score_history", table_name="ai_score_versions")
    op.drop_index("ix_ai_score_current", table_name="ai_score_versions")
    op.drop_index("ix_ai_score_versions_computed_at",
                  table_name="ai_score_versions")
    op.drop_index("ix_ai_score_versions_trigger",
                  table_name="ai_score_versions")
    op.drop_index("ix_ai_score_versions_input_fingerprint",
                  table_name="ai_score_versions")
    op.drop_index("ix_ai_score_versions_overall_score",
                  table_name="ai_score_versions")
    op.drop_index("ix_ai_score_versions_recommendation",
                  table_name="ai_score_versions")
    op.drop_index("ix_ai_score_versions_rating",
                  table_name="ai_score_versions")
    op.drop_index("ix_ai_score_versions_status",
                  table_name="ai_score_versions")
    op.drop_index("ix_ai_score_versions_company_id",
                  table_name="ai_score_versions")
    op.drop_table("ai_score_versions")
