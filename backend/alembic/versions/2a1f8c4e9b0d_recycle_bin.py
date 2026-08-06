"""Add the recycle bin table (generic soft delete).

Revision ID: 2a1f8c4e9b0d
Revises: f7b2d94e15c8
Create Date: 2026-08-06

The recycle bin records soft-deleted resources generically (``resource_type`` +
``resource_id`` + a redacted ``payload`` snapshot) so any entity can be moved to
the bin, reviewed, restored or permanently purged without a per-entity schema
change.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "2a1f8c4e9b0d"
down_revision = "a2c8e5d91f47"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return name in inspector.get_table_names()


def upgrade() -> None:
    if _has_table("recycle_bin"):
        return
    op.create_table(
        "recycle_bin",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("resource_type", sa.String(length=32), nullable=False),
        sa.Column("resource_id", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("deleted_by", sa.String(length=36), nullable=True),
        sa.Column("deleted_by_email", sa.String(length=254), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("restored_by", sa.String(length=36), nullable=True),
        sa.Column("restored_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("purged_by", sa.String(length=36), nullable=True),
        sa.Column("purged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_recycle_bin_resource_type", "recycle_bin", ["resource_type"])
    op.create_index("ix_recycle_bin_resource_id", "recycle_bin", ["resource_id"])
    op.create_index("ix_recycle_bin_deleted_at", "recycle_bin", ["deleted_at"])


def downgrade() -> None:
    if not _has_table("recycle_bin"):
        return
    op.drop_index("ix_recycle_bin_deleted_at", table_name="recycle_bin")
    op.drop_index("ix_recycle_bin_resource_id", table_name="recycle_bin")
    op.drop_index("ix_recycle_bin_resource_type", table_name="recycle_bin")
    op.drop_table("recycle_bin")
