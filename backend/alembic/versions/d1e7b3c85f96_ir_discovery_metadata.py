"""IR URL discovery metadata

Revision ID: d1e7b3c85f96
Revises: c9d5f8a3b204
Create Date: 2026-08-02

Adds provenance for a discovered investor-relations URL. Discovery is a
heuristic — a domain derived from the company name plus a conventional IR
path — so the confidence and the method are stored beside the URL. Without
them a guessed URL is indistinguishable from one a human verified.

`ir_url_checked_at` records the last probe so the discoverer does not
re-attempt the same unresolvable domain on every pass.

Nullable throughout: the 501 existing rows predate discovery and must not be
claimed as checked.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "d1e7b3c85f96"
down_revision: str | None = "c9d5f8a3b204"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "company_crawl_state",
        sa.Column("ir_url_confidence", sa.Float(), nullable=True),
    )
    op.add_column(
        "company_crawl_state",
        sa.Column("ir_url_method", sa.String(length=24), nullable=True),
    )
    op.add_column(
        "company_crawl_state",
        sa.Column("ir_url_checked_at", sa.DateTime(timezone=True),
                  nullable=True),
    )


def downgrade() -> None:
    op.drop_column("company_crawl_state", "ir_url_checked_at")
    op.drop_column("company_crawl_state", "ir_url_method")
    op.drop_column("company_crawl_state", "ir_url_confidence")
