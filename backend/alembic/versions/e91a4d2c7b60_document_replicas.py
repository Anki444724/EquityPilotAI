"""Hybrid storage: per-document replication state.

Revision ID: e91a4d2c7b60
Revises: d5f83c1e6a27
Create Date: 2026-08-01

Additive only. No existing table is touched and no data moves, so this is safe
to apply while the volume remains the authoritative store.

Note the `created_at`/`updated_at` columns: `Base` declares them on every
model, so they are part of the table's contract rather than optional
bookkeeping. Omitting them from a hand-written migration is invisible on
SQLite — where the test suite builds the schema from the models — and fails
only on Postgres. That is MIG-001, and `tests/test_migrations.py` now catches
it, but the columns are spelled out here rather than relied upon.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e91a4d2c7b60"
down_revision = "d5f83c1e6a27"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "document_replicas",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("backend", sa.String(length=16), server_default="s3",
                  nullable=False),
        sa.Column("storage_key", sa.String(length=512), nullable=True),
        sa.Column("state", sa.String(length=16), server_default="pending",
                  nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("verified_sha256", sa.String(length=64), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("replicated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"],
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_id", "backend",
                            name="uq_replica_doc_backend"),
    )
    op.create_index("ix_document_replicas_document_id", "document_replicas",
                    ["document_id"])
    op.create_index("ix_document_replicas_state", "document_replicas", ["state"])
    op.create_index("ix_replica_state_attempts", "document_replicas",
                    ["state", "attempts"])


def downgrade() -> None:
    op.drop_table("document_replicas")
