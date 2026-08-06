"""Phase 6 — document approval workflow.

Revision ID: 7f8091a2b3c4
Revises: 6e7f8091a2b3
Create Date: 2026-08-06

Adds an ``approval_status`` column to ``documents`` implementing the Phase 6
approval workflow (uploaded → ai_extracted → pending_review → approved →
published) as a parallel track to the existing ingestion ``status`` column, so
the two lifecycles do not interfere.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "7f8091a2b3c4"
down_revision = "6e7f8091a2b3"
branch_labels = None
depends_on = None


def _has_column(table: str, column: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return column in {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    if not _has_column("documents", "approval_status"):
        op.add_column(
            "documents",
            sa.Column("approval_status", sa.String(20),
                      server_default="uploaded", nullable=False),
        )
    if not _has_column("documents", "approval_reviewer"):
        op.add_column("documents", sa.Column("approval_reviewer", sa.String(254), nullable=True))
    if not _has_column("documents", "approved_at"):
        op.add_column("documents", sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True))
    if not _has_column("documents", "approval_note"):
        op.add_column("documents", sa.Column("approval_note", sa.Text(), nullable=True))


def downgrade() -> None:
    for column in ("approval_status", "approval_reviewer", "approved_at", "approval_note"):
        if _has_column("documents", column):
            op.drop_column("documents", column)
