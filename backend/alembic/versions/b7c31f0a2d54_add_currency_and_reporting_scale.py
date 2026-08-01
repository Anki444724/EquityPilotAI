"""Phase 3: currency and reporting scale on companies.

Revision ID: b7c31f0a2d54
Revises: a6b4a4d5e3ea
Create Date: 2026-08-01

The platform stored every company's figures in ₹ crore because every company
was Indian. Supporting US listings makes the unit a property of the company:
Apple's statements are in USD millions and labelling them "₹ cr" would be a
fabrication that no downstream check could catch, because every figure would
be real and every citation would resolve.

Both columns are NOT NULL with server defaults matching the existing
convention, so the 135 Indian companies already in production are unchanged by
this migration — they were crore-denominated before it and are explicitly
crore-denominated after.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b7c31f0a2d54"
down_revision = "a6b4a4d5e3ea"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "companies",
        sa.Column("currency", sa.String(length=3), nullable=False,
                  server_default="INR"),
    )
    op.add_column(
        "companies",
        sa.Column("reporting_scale", sa.String(length=8), nullable=False,
                  server_default="crore"),
    )
    # Belt and braces: the server default covers rows inserted without the
    # column, this covers rows that already exist. On Postgres `add_column`
    # with a default backfills, but being explicit costs nothing and makes the
    # intent legible to whoever reads this next.
    op.execute(
        "UPDATE companies SET currency = 'INR' WHERE currency IS NULL OR currency = ''"
    )
    op.execute(
        "UPDATE companies SET reporting_scale = 'crore' "
        "WHERE reporting_scale IS NULL OR reporting_scale = ''"
    )


def downgrade() -> None:
    op.drop_column("companies", "reporting_scale")
    op.drop_column("companies", "currency")
