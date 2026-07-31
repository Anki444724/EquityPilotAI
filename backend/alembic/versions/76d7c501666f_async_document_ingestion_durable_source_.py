"""async document ingestion: durable source storage, status and log

Adds the columns the asynchronous pipeline needs:

* `storage_key` / `storage_backend` / `storage_location` — where the original
  upload is kept. Previously the bytes were discarded once the request ended,
  which made re-indexing impossible and lost the document if ingestion failed.
* `processing_log` — the per-stage log, persisted with the document rather
  than only written to stdout, because "why does this report have no chunks?"
  is asked days later, long after the container has gone.
* `attempts` — retries made, mirrored from the job for display.

Existing rows are migrated to the new status vocabulary. They keep a NULL
`storage_key` because their source bytes genuinely were not kept; the API
reports that honestly and asks for a re-upload rather than pretending a
re-index is possible.

Revision ID: 76d7c501666f
Revises: 33573f2f567a
Create Date: 2026-07-31

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "76d7c501666f"
down_revision = "33573f2f567a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # batch_alter_table so the same migration runs on SQLite, which cannot
    # ALTER a column in place.
    with op.batch_alter_table("documents") as batch:
        batch.add_column(sa.Column("storage_key", sa.String(512), nullable=True))
        batch.add_column(sa.Column("storage_backend", sa.String(16), nullable=True))
        batch.add_column(sa.Column("storage_location", sa.String(1024), nullable=True))
        batch.add_column(sa.Column("processing_log", sa.JSON(), nullable=True))
        batch.add_column(
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0")
        )

    op.create_index("ix_documents_storage_key", "documents", ["storage_key"])

    # Map the old vocabulary onto the new one. "ready" became "completed";
    # everything else already matches or is terminal.
    documents = sa.table(
        "documents",
        sa.column("status", sa.String),
        sa.column("progress", sa.Float),
    )
    op.execute(
        documents.update()
        .where(documents.c.status == op.inline_literal("ready"))
        .values(status="completed", progress=1.0)
    )
    op.execute(
        documents.update()
        .where(documents.c.status == op.inline_literal("error"))
        .values(status="failed")
    )


def downgrade() -> None:
    op.execute(
        sa.table("documents", sa.column("status", sa.String))
        .update()
        .where(sa.column("status") == op.inline_literal("completed"))
        .values(status="ready")
    )
    op.drop_index("ix_documents_storage_key", table_name="documents")
    with op.batch_alter_table("documents") as batch:
        batch.drop_column("attempts")
        batch.drop_column("processing_log")
        batch.drop_column("storage_location")
        batch.drop_column("storage_backend")
        batch.drop_column("storage_key")
