"""Nifty 500 expansion: BSE code, market-cap category, listing status.

Revision ID: f2b71c4e9a08
Revises: e91a4d2c7b60
Create Date: 2026-08-01

Additive only. Existing rows keep their data; `listing_status` defaults to
"active", which is true of all 135 companies already present.

`bse_code` is a string rather than an integer because it is an identifier, not
a quantity — several are zero-padded and arithmetic on one is always a bug.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f2b71c4e9a08"
down_revision = "e91a4d2c7b60"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("companies", sa.Column("bse_code", sa.String(length=16),
                                         nullable=True))
    op.add_column("companies", sa.Column("market_cap_category",
                                         sa.String(length=12), nullable=True))
    op.add_column(
        "companies",
        sa.Column("listing_status", sa.String(length=12), nullable=False,
                  server_default="active"),
    )
    op.add_column("companies", sa.Column("index_membership",
                                         sa.String(length=64), nullable=True))

    op.create_index("ix_companies_bse_code", "companies", ["bse_code"])
    op.create_index("ix_companies_market_cap_category", "companies",
                    ["market_cap_category"])
    op.create_index("ix_companies_listing_status", "companies",
                    ["listing_status"])
    op.create_index("ix_companies_index_membership", "companies",
                    ["index_membership"])


def downgrade() -> None:
    op.drop_index("ix_companies_index_membership", table_name="companies")
    op.drop_index("ix_companies_listing_status", table_name="companies")
    op.drop_index("ix_companies_market_cap_category", table_name="companies")
    op.drop_index("ix_companies_bse_code", table_name="companies")
    op.drop_column("companies", "index_membership")
    op.drop_column("companies", "listing_status")
    op.drop_column("companies", "market_cap_category")
    op.drop_column("companies", "bse_code")
