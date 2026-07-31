"""add username to users

A second login identifier, unique when set and nullable so accounts created
by invitation or OAuth are unaffected. Stored lower-cased by the service
layer: a case-sensitive unique index would let "AnkitSingh" and "ankitsingh"
both be claimed, which is an impersonation vector rather than a convenience.

Revision ID: a6b4a4d5e3ea
Revises: 76d7c501666f
Create Date: 2026-07-31

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a6b4a4d5e3ea"
down_revision = "76d7c501666f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("username", sa.String(64), nullable=True))
    # A plain unique index, not a constraint, so SQLite's batch mode and
    # Postgres agree. NULLs do not collide in either, so accounts without a
    # username remain valid.
    op.create_index("ix_users_username", "users", ["username"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_users_username", table_name="users")
    with op.batch_alter_table("users") as batch:
        batch.drop_column("username")
