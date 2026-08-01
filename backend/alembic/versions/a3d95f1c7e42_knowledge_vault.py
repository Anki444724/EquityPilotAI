"""Company Knowledge Vault: versioned entries and permanent summaries.

Revision ID: a3d95f1c7e42
Revises: f2b71c4e9a08
Create Date: 2026-08-01

Additive only. Both tables are append-only in practice: a new filing inserts a
new version and flips the previous row's status, so no existing data is
touched and nothing is ever deleted.

`created_at`/`updated_at` are spelled out because `Base` declares them on
every model — omitting them is invisible on SQLite, where the test suite
builds the schema from the models, and fails only on Postgres where the
migration *is* the schema. That is MIG-001.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a3d95f1c7e42"
down_revision = "f2b71c4e9a08"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "knowledge_entries",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("company_id", sa.String(length=36), nullable=False),
        sa.Column("section", sa.String(length=32), nullable=False),
        sa.Column("key", sa.String(length=80), nullable=False),
        sa.Column("label", sa.String(length=240), server_default="",
                  nullable=False),
        sa.Column("value_text", sa.Text(), nullable=True),
        sa.Column("value_number", sa.Float(), nullable=True),
        sa.Column("unit", sa.String(length=24), nullable=True),
        sa.Column("confidence", sa.Float(), server_default="0", nullable=False),
        sa.Column("authority", sa.Float(), server_default="0.4", nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=True),
        sa.Column("page", sa.Integer(), nullable=True),
        sa.Column("paragraph", sa.Integer(), nullable=True),
        sa.Column("fiscal_year", sa.Integer(), nullable=True),
        sa.Column("quarter", sa.String(length=16), nullable=True),
        sa.Column("doc_type", sa.String(length=40), nullable=True),
        sa.Column("evidence", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("status", sa.String(length=12), server_default="current",
                  nullable=False),
        sa.Column("superseded_by", sa.Integer(), nullable=True),
        sa.Column("generated_by", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"],
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"],
                                ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["superseded_by"], ["knowledge_entries.id"],
                                ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("company_id", "section", "key", "version",
                            name="uq_knowledge_version"),
    )
    op.create_index("ix_knowledge_entries_company_id", "knowledge_entries",
                    ["company_id"])
    op.create_index("ix_knowledge_entries_section", "knowledge_entries",
                    ["section"])
    op.create_index("ix_knowledge_entries_key", "knowledge_entries", ["key"])
    op.create_index("ix_knowledge_entries_status", "knowledge_entries",
                    ["status"])
    op.create_index("ix_knowledge_entries_document_id", "knowledge_entries",
                    ["document_id"])
    op.create_index("ix_knowledge_entries_fiscal_year", "knowledge_entries",
                    ["fiscal_year"])
    op.create_index("ix_knowledge_current", "knowledge_entries",
                    ["company_id", "section", "status"])
    op.create_index("ix_knowledge_lineage", "knowledge_entries",
                    ["company_id", "section", "key", "version"])

    op.create_table(
        "document_summaries",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("word_count", sa.Integer(), server_default="0",
                  nullable=False),
        sa.Column("fiscal_year", sa.Integer(), nullable=True),
        sa.Column("quarter", sa.String(length=16), nullable=True),
        sa.Column("doc_type", sa.String(length=40), nullable=True),
        sa.Column("provider", sa.String(length=32), nullable=True),
        sa.Column("model", sa.String(length=64), nullable=True),
        sa.Column("prompt_version", sa.Integer(), server_default="1",
                  nullable=False),
        sa.Column("is_fallback", sa.Boolean(), server_default=sa.false(),
                  nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), server_default="0",
                  nullable=False),
        sa.Column("completion_tokens", sa.Integer(), server_default="0",
                  nullable=False),
        sa.Column("cost_usd", sa.Float(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"],
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"],
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_id", "kind", "prompt_version",
                            name="uq_summary_doc_kind_version"),
    )
    op.create_index("ix_document_summaries_document_id", "document_summaries",
                    ["document_id"])
    op.create_index("ix_document_summaries_company_id", "document_summaries",
                    ["company_id"])
    op.create_index("ix_document_summaries_kind", "document_summaries", ["kind"])
    op.create_index("ix_document_summaries_fiscal_year", "document_summaries",
                    ["fiscal_year"])
    op.create_index("ix_document_summaries_doc_type", "document_summaries",
                    ["doc_type"])
    op.create_index("ix_summary_company_kind", "document_summaries",
                    ["company_id", "kind"])
    op.create_index("ix_summary_temporal", "document_summaries",
                    ["company_id", "fiscal_year", "kind"])


def downgrade() -> None:
    op.drop_table("document_summaries")
    op.drop_table("knowledge_entries")
