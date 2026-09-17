"""Web evidence provenance on documents

Revision ID: c7a13e5b9d24
Revises: b10a01b0a101
Create Date: 2026-09-18

A web page ingested as evidence is a document like any other — it goes through
the same chunking, indexing and citation path — but it arrives from a URL
rather than from an upload, and that difference has to survive persistence:

``source_url``
    Where it came from. Without it a cited page cannot be re-checked, and a
    duplicate cannot be recognised before its bytes are fetched again.
``source_class``
    Which allowlist rule admitted the host (company site, verified IR,
    exchange, regulator, media, unknown). Stored as text rather than an enum
    so adding a class later is not a migration.
``published_at``
    The page's own publication date, when it genuinely states one. Nullable
    and left null when the page does not — the extractor never invents it.
``retrieved_at``
    When this platform fetched it. Distinct from ``published_at`` and never
    inferred FROM it: a 2019 report fetched today was retrieved today.

Indexes: ``ix_document_company_url`` backs the URL lookup and the dedup probe
(has this company's page already been ingested?); ``ix_document_source_class``
backs provenance queries. Neither is unique — the same URL legitimately
appears more than once when a page changes and is re-ingested as a new
version, and byte-identical repeats are already collapsed by
``uq_document_company_hash``.

Nullable throughout: the existing rows predate web ingestion and must not be
labelled as fetched from anywhere.

Additive and reversible: the downgrade drops the indexes and the columns.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c7a13e5b9d24"
down_revision = "b10a01b0a101"
branch_labels = None
depends_on = None

_TABLE = "documents"

_COLUMNS: tuple[tuple[str, sa.types.TypeEngine], ...] = (
    ("source_url", sa.String(length=2048)),
    ("source_class", sa.String(length=32)),
    ("published_at", sa.DateTime(timezone=True)),
    ("retrieved_at", sa.DateTime(timezone=True)),
)


def _existing_columns() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {column["name"] for column in inspector.get_columns(_TABLE)}


def _existing_indexes() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {index["name"] for index in inspector.get_indexes(_TABLE)}


def upgrade() -> None:
    present = _existing_columns()
    for name, column_type in _COLUMNS:
        if name not in present:
            op.add_column(_TABLE, sa.Column(name, column_type, nullable=True))

    indexed = _existing_indexes()
    if "ix_document_company_url" not in indexed:
        op.create_index(
            "ix_document_company_url", _TABLE, ["company_id", "source_url"],
        )
    if "ix_document_source_class" not in indexed:
        op.create_index("ix_document_source_class", _TABLE, ["source_class"])


def downgrade() -> None:
    indexed = _existing_indexes()
    if "ix_document_source_class" in indexed:
        op.drop_index("ix_document_source_class", table_name=_TABLE)
    if "ix_document_company_url" in indexed:
        op.drop_index("ix_document_company_url", table_name=_TABLE)

    present = _existing_columns()
    for name, _ in reversed(_COLUMNS):
        if name in present:
            op.drop_column(_TABLE, name)
