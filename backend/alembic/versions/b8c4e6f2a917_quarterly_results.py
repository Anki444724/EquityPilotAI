"""quarterly results

Revision ID: b8c4e6f2a917
Revises: a3d95f1c7e42
Create Date: 2026-08-02

Adds `quarterly_results`. Quarterly figures are separately disclosed facts,
not a decomposition of the annual statements — four quarters do not reconcile
to the annual figure in Indian reporting, because Q4 routinely absorbs audit
adjustments. Deriving them would be inventing numbers, so they are stored.

`created_at` and `updated_at` are declared explicitly. `Base` defines them on
every model, and a migration that omits them produces `UndefinedColumn` in
production while remaining invisible locally, because the test schema is built
by `create_all()` from the models rather than by replaying migrations. That is
MIG-001, and `tests/test_migrations.py` exists to catch a repeat.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "b8c4e6f2a917"
down_revision: str | None = "a3d95f1c7e42"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "quarterly_results",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("company_id", sa.String(length=36), nullable=False),
        sa.Column("fiscal_year", sa.Integer(), nullable=False),
        sa.Column("quarter", sa.Integer(), nullable=False),
        sa.Column("revenue", sa.Float(), nullable=True),
        sa.Column("expenses", sa.Float(), nullable=True),
        sa.Column("operating_profit", sa.Float(), nullable=True),
        sa.Column("operating_margin", sa.Float(), nullable=True),
        sa.Column("other_income", sa.Float(), nullable=True),
        sa.Column("interest", sa.Float(), nullable=True),
        sa.Column("depreciation", sa.Float(), nullable=True),
        sa.Column("profit_before_tax", sa.Float(), nullable=True),
        sa.Column("tax_rate", sa.Float(), nullable=True),
        sa.Column("net_profit", sa.Float(), nullable=True),
        sa.Column("eps", sa.Float(), nullable=True),
        sa.Column("source", sa.String(length=120), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "company_id", "fiscal_year", "quarter", name="uq_quarterly_period",
        ),
    )
    op.create_index(
        "ix_quarterly_company_period",
        "quarterly_results",
        ["company_id", "fiscal_year", "quarter"],
    )


def downgrade() -> None:
    op.drop_index("ix_quarterly_company_period", table_name="quarterly_results")
    op.drop_table("quarterly_results")
