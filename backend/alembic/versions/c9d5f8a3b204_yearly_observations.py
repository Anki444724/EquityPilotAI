"""yearly observations — temporal memory

Revision ID: c9d5f8a3b204
Revises: b8c4e6f2a917
Create Date: 2026-08-02

One dated AI observation per company per fiscal year (§8, §12), versioned so
regenerating a year with a better model does not rewrite a judgement that a
later year has already been scored against.

`created_at` and `updated_at` are declared explicitly — `Base` defines them on
every model, and omitting them yields `UndefinedColumn` in production while
staying invisible locally, because the test schema is built by `create_all()`
from the models rather than by replaying migrations. That is MIG-001;
`tests/test_migrations.py` guards against a repeat.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "c9d5f8a3b204"
down_revision: str | None = "b8c4e6f2a917"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "yearly_observations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("company_id", sa.String(length=36), nullable=False),
        sa.Column("fiscal_year", sa.Integer(), nullable=False),
        sa.Column("findings", sa.Text(), nullable=True),
        sa.Column("dimensions", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False,
                  server_default=sa.text("0")),
        sa.Column("guidance", sa.Text(), nullable=True),
        sa.Column("prior_verdict", sa.String(length=24), nullable=False,
                  server_default="not_assessable"),
        sa.Column("verdict_reasoning", sa.Text(), nullable=True),
        sa.Column("citations", sa.Text(), nullable=True),
        sa.Column("source_document_ids", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False,
                  server_default=sa.text("1")),
        sa.Column("status", sa.String(length=12), nullable=False,
                  server_default="current"),
        sa.Column("superseded_by", sa.Integer(), nullable=True),
        sa.Column("generated_by", sa.String(length=64), nullable=True),
        sa.Column("is_fallback", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("prompt_version", sa.String(length=16), nullable=False,
                  server_default="v1"),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"],
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["superseded_by"], ["yearly_observations.id"],
                                ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("company_id", "fiscal_year", "version",
                            name="uq_observation_version"),
    )
    op.create_index("ix_yearly_observations_company_id",
                    "yearly_observations", ["company_id"])
    op.create_index("ix_yearly_observations_fiscal_year",
                    "yearly_observations", ["fiscal_year"])
    op.create_index("ix_yearly_observations_prior_verdict",
                    "yearly_observations", ["prior_verdict"])
    op.create_index("ix_yearly_observations_status",
                    "yearly_observations", ["status"])
    op.create_index("ix_observation_timeline", "yearly_observations",
                    ["company_id", "fiscal_year", "status"])


def downgrade() -> None:
    op.drop_index("ix_observation_timeline", table_name="yearly_observations")
    op.drop_index("ix_yearly_observations_status",
                  table_name="yearly_observations")
    op.drop_index("ix_yearly_observations_prior_verdict",
                  table_name="yearly_observations")
    op.drop_index("ix_yearly_observations_fiscal_year",
                  table_name="yearly_observations")
    op.drop_index("ix_yearly_observations_company_id",
                  table_name="yearly_observations")
    op.drop_table("yearly_observations")
