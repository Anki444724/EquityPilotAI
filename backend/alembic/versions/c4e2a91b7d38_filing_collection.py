"""Automated Indian filing collection.

Revision ID: c4e2a91b7d38
Revises: b7c31f0a2d54
Create Date: 2026-08-01

Two tables. `discovered_filings` is the ledger of everything the crawler has
seen — the unique constraint on (source, source_reference) is what makes a
nightly re-scan an indexed lookup rather than a re-download, and the index on
content_sha256 is the second dedup gate for the same PDF published by both
exchanges. `company_crawl_state` holds per-company scheduling and health.

Additive only: no existing table is touched, so the migration is safe to apply
to production with the collector switched off.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c4e2a91b7d38"
down_revision = "b7c31f0a2d54"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "discovered_filings",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("company_id", sa.String(length=36), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("source_reference", sa.String(length=500), nullable=False),
        sa.Column("source_url", sa.String(length=1000), nullable=True),
        sa.Column("title", sa.String(length=500), server_default="", nullable=False),
        sa.Column("filing_type", sa.String(length=40), nullable=True),
        sa.Column("doc_type", sa.String(length=40), nullable=True),
        sa.Column("classification_confidence", sa.Float(), nullable=True),
        sa.Column("published_on", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fiscal_year", sa.Integer(), nullable=True),
        sa.Column("quarter", sa.String(length=4), nullable=True),
        sa.Column("language", sa.String(length=8), server_default="en", nullable=False),
        sa.Column("status", sa.String(length=16), server_default="discovered", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("content_sha256", sa.String(length=64), nullable=True),
        sa.Column("file_size", sa.Integer(), nullable=True),
        sa.Column("document_id", sa.Integer(), nullable=True),
        sa.Column("discovered_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("downloaded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source", "source_reference",
                            name="uq_discovered_source_ref"),
    )
    op.create_index("ix_discovered_filings_company_id", "discovered_filings",
                    ["company_id"])
    op.create_index("ix_discovered_filings_source", "discovered_filings", ["source"])
    op.create_index("ix_discovered_filings_status", "discovered_filings", ["status"])
    op.create_index("ix_discovered_filings_filing_type", "discovered_filings",
                    ["filing_type"])
    op.create_index("ix_discovered_filings_fiscal_year", "discovered_filings",
                    ["fiscal_year"])
    op.create_index("ix_discovered_filings_content_sha256", "discovered_filings",
                    ["content_sha256"])
    op.create_index("ix_discovered_filings_document_id", "discovered_filings",
                    ["document_id"])
    op.create_index("ix_discovered_company_status", "discovered_filings",
                    ["company_id", "status"])
    op.create_index("ix_discovered_status_discovered", "discovered_filings",
                    ["status", "discovered_at"])

    op.create_table(
        "company_crawl_state",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("company_id", sa.String(length=36), nullable=False),
        sa.Column("tier", sa.String(length=8), server_default="weekly", nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("ir_url", sa.String(length=500), nullable=True),
        sa.Column("bse_scrip_code", sa.String(length=16), nullable=True),
        sa.Column("last_crawled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_status", sa.String(length=16), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), server_default="0", nullable=False),
        sa.Column("documents_found", sa.Integer(), server_default="0", nullable=False),
        sa.Column("documents_ingested", sa.Integer(), server_default="0", nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("company_id"),
    )
    op.create_index("ix_company_crawl_state_company_id", "company_crawl_state",
                    ["company_id"])
    op.create_index("ix_company_crawl_state_tier", "company_crawl_state", ["tier"])
    op.create_index("ix_crawl_due", "company_crawl_state",
                    ["enabled", "tier", "last_crawled_at"])


def downgrade() -> None:
    op.drop_table("company_crawl_state")
    op.drop_table("discovered_filings")
