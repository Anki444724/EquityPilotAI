"""Phase 1: ingestion run + failure tracking.

Revision ID: e4b8d0a5f237
Revises: d3a7c9f4e126
Create Date: 2026-08-21

Two new tables, nothing touched:

* `ingestion_runs` — one row per execution of a sync job, with counters and a
  JSON `stats` block that carries `next_index` so a crashed batched sweep
  resumes instead of restarting (and re-billing) the provider.
* `ingestion_failures` — the durable failed-symbol list the
  `failed_data_retry` job works through, with the verbatim provider reason
  and the transient/permanent classification the financials backfill already
  uses. `background_jobs.id` is SET NULL on delete: job history may be
  retained-swept, the failure record must outlive it.

Rollback: drop both tables. They are new in Phase 1, so nothing pre-existing
depends on them.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e4b8d0a5f237"
down_revision = "d3a7c9f4e126"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ingestion_runs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("job_id", sa.Integer(), nullable=True),
        sa.Column("kind", sa.String(length=48), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("succeeded", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("stats", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["background_jobs.id"],
                                ondelete="SET NULL"),
    )
    op.create_index("ix_ingestion_runs_job_id", "ingestion_runs", ["job_id"])
    op.create_index("ix_ingestion_runs_kind", "ingestion_runs", ["kind"])
    op.create_index("ix_ingestion_runs_started_at", "ingestion_runs", ["started_at"])

    op.create_table(
        "ingestion_failures",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=48), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("company_id", sa.String(length=36), nullable=True),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("failure_kind", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["ingestion_runs.id"],
                                ondelete="CASCADE"),
        sa.UniqueConstraint("run_id", "symbol",
                            name="uq_ingestion_failure_run_symbol"),
    )
    op.create_index("ix_ingestion_failures_run_id", "ingestion_failures", ["run_id"])
    op.create_index("ix_ingestion_failures_kind", "ingestion_failures", ["kind"])
    op.create_index("ix_ingestion_failures_open", "ingestion_failures",
                    ["failure_kind", "last_attempt_at"])


def downgrade() -> None:
    op.drop_index("ix_ingestion_failures_open", table_name="ingestion_failures")
    op.drop_index("ix_ingestion_failures_kind", table_name="ingestion_failures")
    op.drop_index("ix_ingestion_failures_run_id", table_name="ingestion_failures")
    op.drop_table("ingestion_failures")
    op.drop_index("ix_ingestion_runs_started_at", table_name="ingestion_runs")
    op.drop_index("ix_ingestion_runs_kind", table_name="ingestion_runs")
    op.drop_index("ix_ingestion_runs_job_id", table_name="ingestion_runs")
    op.drop_table("ingestion_runs")
