"""Phase 2 — company management fields and version history.

Revision ID: 3b4c7d9e0f1a
Revises: 2a1f8c4e9b0d
Create Date: 2026-08-06

Adds the enterprise company-management columns (face value, listing date, CEO,
employees, headquarters, logo/favicon, soft-delete flag) and the
``company_versions`` table that records every edit for rollback and audit.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "3b4c7d9e0f1a"
down_revision = "2a1f8c4e9b0d"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return name in inspector.get_table_names()


def _columns(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    present = _columns("companies")
    additions = [
        ("face_value", sa.Float(), True),
        ("listing_date", sa.DateTime(timezone=True), True),
        ("ceo", sa.String(160), True),
        ("employees", sa.Integer(), True),
        ("headquarters", sa.String(200), True),
        ("logo_url", sa.String(500), True),
        ("favicon_url", sa.String(500), True),
        ("deleted_at", sa.DateTime(timezone=True), True),
    ]
    for name, coltype, nullable in additions:
        if name in present:
            continue
        op.add_column("companies", sa.Column(name, coltype, nullable=nullable))
    if "deleted_at" in present:
        pass

    if not _has_table("company_versions"):
        op.create_table(
            "company_versions",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("company_id", sa.String(36),
                      sa.ForeignKey("companies.id", ondelete="CASCADE"),
                      nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("actor_id", sa.String(36), nullable=True),
            sa.Column("actor_email", sa.String(254), nullable=True),
            sa.Column("changes", sa.JSON(), nullable=True),
            sa.Column("snapshot", sa.JSON(), nullable=True),
            sa.Column("change_type", sa.String(16), nullable=False, server_default="update"),
            sa.Column("summary", sa.String(400), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(timezone=True),
                      server_default=sa.func.now(), nullable=False),
        )
        op.create_index("ix_company_versions_company_id",
                        "company_versions", ["company_id"])
        op.create_index("ix_company_versions_created_at",
                        "company_versions", ["created_at"])
        op.create_unique_constraint("uq_company_version",
                                    "company_versions", ["company_id", "version"])


def downgrade() -> None:
    if _has_table("company_versions"):
        op.drop_table("company_versions")
    cols = ["face_value", "listing_date", "ceo", "employees", "headquarters",
            "logo_url", "favicon_url", "deleted_at"]
    present = _columns("companies")
    for name in cols:
        if name in present:
            op.drop_column("companies", name)
