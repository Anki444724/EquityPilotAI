"""Phase 1: financial_facts upsert/provenance columns.

Revision ID: c2f6b8e3d015
Revises: b1e5a7d2c904
Create Date: 2026-08-21

Additive only — and deliberately so. The audited plan proposed replacing the
unique constraint `uq_fact_company_year_item_precedence`
(company_id, fiscal_year, line_item, precedence) with one that also carries the
reporting basis. That swap is DEFERRED to Phase 2 and needs explicit approval,
because Phase 1 writes only consolidated rows and the existing constraint is
exactly the upsert conflict key for them:

    old constraint : UNIQUE (company_id, fiscal_year, line_item, precedence)
    new constraint : unchanged in this revision
    reason         : Phase 1 ingests consolidated data only; the existing key
                     already guarantees no duplicate facts for that basis.
                     Standalone (consolidated=false) rows are not written
                     until Phase 2, and when they are, the constraint change
                     ships as its own reviewed migration.
    rollback       : n/a (nothing changed); this migration's own rollback is
                     to drop the three added columns.

The three added columns make the row-level upsert honest:
  consolidated — True for every existing row (server_default '1'), because
                 consolidated screener presentation is the only basis the
                 platform has ever ingested.
  fetched_at   — when the figure was last fetched from `source`.
  data_version — row revision, bumped only when the value actually changes,
                 so repeated identical syncs are provably no-ops.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c2f6b8e3d015"
down_revision = "b1e5a7d2c904"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "financial_facts",
        sa.Column(
            "consolidated", sa.Boolean(), nullable=False, server_default="1",
        ),
    )
    op.add_column(
        "financial_facts",
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "financial_facts",
        sa.Column("data_version", sa.Integer(), nullable=False, server_default="1"),
    )


def downgrade() -> None:
    op.drop_column("financial_facts", "data_version")
    op.drop_column("financial_facts", "fetched_at")
    op.drop_column("financial_facts", "consolidated")
