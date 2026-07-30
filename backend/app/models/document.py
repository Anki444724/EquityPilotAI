"""Persistence for the document-intelligence layer.

Seven tables, and the shape of them follows from one requirement: every fact
the platform learns must remain traceable to a page of a specific version of a
specific file, indefinitely. That rules out storing derived data alone.

* **Document** — the uploaded file, its content hash, and its lifecycle state.
* **DocumentVersion-by-hash** — a re-upload of identical bytes is recognised;
  a changed file of the same name supersedes its predecessor rather than
  overwriting it, so an old citation still resolves.
* **DocumentPage** — per-page text and OCR provenance.
* **DocumentChunk** — retrievable units plus their embedding.
* **DocumentTable** — recovered tables as JSON, with units preserved.
* **DocumentEntity** / **DocumentFact** — the extracted knowledge.
* **DocumentJob** — the ingestion queue.

Embeddings are stored as JSON rather than a native vector column because the
platform must run on SQLite with no extensions as readily as on Postgres with
pgvector. The vector store abstraction is where a pgvector implementation would
slot in; nothing above it would change.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    DateTime, Float, ForeignKey, Index, Integer, JSON, String, Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class Document(Base):
    """An uploaded document and its processing state."""

    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )

    filename: Mapped[str] = mapped_column(String(400), nullable=False)
    title: Mapped[str | None] = mapped_column(String(400))
    doc_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    file_format: Mapped[str] = mapped_column(String(16), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)

    #: SHA-256 of the raw upload. Identity for duplicate and version detection.
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    #: Set when a newer version of the same logical document arrives. A
    #: superseded document is excluded from search but never deleted, so a
    #: citation issued months ago still resolves to the text it quoted.
    superseded_by: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("documents.id", ondelete="SET NULL")
    )

    period: Mapped[str | None] = mapped_column(String(16))
    fiscal_year: Mapped[int | None] = mapped_column(Integer)

    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    stage: Mapped[str] = mapped_column(String(20), default="queued")
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    error: Mapped[str | None] = mapped_column(Text)

    page_count: Mapped[int] = mapped_column(Integer, default=0)
    char_count: Mapped[int] = mapped_column(Integer, default=0)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    table_count: Mapped[int] = mapped_column(Integer, default=0)
    entity_count: Mapped[int] = mapped_column(Integer, default=0)
    fact_count: Mapped[int] = mapped_column(Integer, default=0)

    used_ocr: Mapped[bool] = mapped_column(default=False)
    ocr_pages: Mapped[int] = mapped_column(Integer, default=0)
    #: Share of the 73 spec fields this document supplied.
    coverage: Mapped[float] = mapped_column(Float, default=0.0)
    avg_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    duplicate_ratio: Mapped[float] = mapped_column(Float, default=0.0)

    embedding_spec: Mapped[str | None] = mapped_column(String(80))
    processing_ms: Mapped[float] = mapped_column(Float, default=0.0)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    uploaded_by: Mapped[str | None] = mapped_column(String(64))
    doc_metadata: Mapped[dict | None] = mapped_column(JSON)

    pages: Mapped[list["DocumentPage"]] = relationship(
        back_populates="document", cascade="all, delete-orphan",
    )
    chunks: Mapped[list["DocumentChunk"]] = relationship(
        back_populates="document", cascade="all, delete-orphan",
    )
    tables: Mapped[list["DocumentTable"]] = relationship(
        back_populates="document", cascade="all, delete-orphan",
    )
    entities: Mapped[list["DocumentEntity"]] = relationship(
        back_populates="document", cascade="all, delete-orphan",
    )
    facts: Mapped[list["DocumentFact"]] = relationship(
        back_populates="document", cascade="all, delete-orphan",
    )
    sections: Mapped[list["DocumentSection"]] = relationship(
        back_populates="document", cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index("ix_document_company_type", "company_id", "doc_type"),
        Index("ix_document_company_status", "company_id", "status"),
        # The same bytes for the same company are one document, not two.
        UniqueConstraint("company_id", "content_hash", name="uq_document_company_hash"),
    )

    @property
    def is_current(self) -> bool:
        return self.superseded_by is None


class DocumentPage(Base):
    """One page: its text and how that text was obtained."""

    __tablename__ = "document_pages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, default="")
    text_source: Mapped[str] = mapped_column(String(16), default="native")
    ocr_confidence: Mapped[float | None] = mapped_column(Float)
    char_count: Mapped[int] = mapped_column(Integer, default=0)

    document: Mapped[Document] = relationship(back_populates="pages")

    __table_args__ = (
        UniqueConstraint("document_id", "page_number", name="uq_page_document_number"),
    )


class DocumentSection(Base):
    """A detected section span."""

    __tablename__ = "document_sections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    kind: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(200), default="")
    start_page: Mapped[int] = mapped_column(Integer, default=1)
    end_page: Mapped[int] = mapped_column(Integer, default=1)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)

    document: Mapped[Document] = relationship(back_populates="sections")


class DocumentChunk(Base):
    """A retrievable passage, its provenance and its embedding."""

    __tablename__ = "document_chunks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    chunk_index: Mapped[int] = mapped_column(Integer, default=0)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    page: Mapped[int] = mapped_column(Integer, default=1, index=True)
    paragraph: Mapped[int] = mapped_column(Integer, default=0)
    section: Mapped[str] = mapped_column(String(40), default="unknown", index=True)
    section_title: Mapped[str | None] = mapped_column(String(200))
    token_estimate: Mapped[int] = mapped_column(Integer, default=0)
    #: SHA-1 of the normalised text — the deduplication key.
    fingerprint: Mapped[str] = mapped_column(String(40), index=True)
    #: The vector, as JSON. Portable across SQLite and Postgres alike.
    embedding: Mapped[list | None] = mapped_column(JSON)

    document: Mapped[Document] = relationship(back_populates="chunks")

    __table_args__ = (
        Index("ix_chunk_document_page", "document_id", "page"),
    )


class DocumentTable(Base):
    """A recovered table, with headers, units and merged spans preserved."""

    __tablename__ = "document_tables"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    page: Mapped[int] = mapped_column(Integer, default=1)
    table_index: Mapped[int] = mapped_column(Integer, default=0)
    caption: Mapped[str | None] = mapped_column(String(400))
    unit: Mapped[str] = mapped_column(String(20), default="unknown")
    header: Mapped[list | None] = mapped_column(JSON)
    rows: Mapped[list | None] = mapped_column(JSON)
    #: Merged spans as a list of [row, col, rowspan, colspan]; JSON has no
    #: tuple keys, so the dict is flattened rather than stringified.
    merged: Mapped[list | None] = mapped_column(JSON)
    n_rows: Mapped[int] = mapped_column(Integer, default=0)
    n_cols: Mapped[int] = mapped_column(Integer, default=0)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)

    document: Mapped[Document] = relationship(back_populates="tables")


class DocumentEntity(Base):
    """A named entity with the page and sentence that evidenced it."""

    __tablename__ = "document_entities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    company_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(400), nullable=False)
    normalised: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    page: Mapped[int] = mapped_column(Integer, default=1)
    context: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    mentions: Mapped[int] = mapped_column(Integer, default=1)
    attributes: Mapped[dict | None] = mapped_column(JSON)

    document: Mapped[Document] = relationship(back_populates="entities")

    __table_args__ = (
        Index("ix_entity_company_kind", "company_id", "kind"),
    )


class DocumentFact(Base):
    """One row of the workbook's AI-2 extraction store."""

    __tablename__ = "document_facts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    company_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)

    category: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    field_key: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    value: Mapped[float | None] = mapped_column(Float)
    text_value: Mapped[str | None] = mapped_column(Text)
    unit: Mapped[str] = mapped_column(String(20), default="unknown")
    period: Mapped[str | None] = mapped_column(String(16), index=True)
    fiscal_year: Mapped[int | None] = mapped_column(Integer)

    page: Mapped[int] = mapped_column(Integer, default=0)
    section: Mapped[str] = mapped_column(String(40), default="unknown")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    #: Verbatim source span, so an extraction can be checked by eye.
    evidence: Mapped[str | None] = mapped_column(Text)
    #: Set once the fact has been promoted into the canonical financial store.
    promoted: Mapped[bool] = mapped_column(default=False)

    document: Mapped[Document] = relationship(back_populates="facts")

    __table_args__ = (
        Index("ix_fact_company_field", "company_id", "field_key"),
        Index("ix_fact_company_period", "company_id", "period"),
    )


class DocumentRelation(Base):
    """A knowledge-graph edge, persisted with the pages that evidence it."""

    __tablename__ = "document_relations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    document_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    source_key: Mapped[str] = mapped_column(String(220), nullable=False, index=True)
    target_key: Mapped[str] = mapped_column(String(220), nullable=False, index=True)
    source_label: Mapped[str] = mapped_column(String(400), default="")
    target_label: Mapped[str] = mapped_column(String(400), default="")
    source_kind: Mapped[str] = mapped_column(String(30), default="company")
    target_kind: Mapped[str] = mapped_column(String(30), default="company")
    relation: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    weight: Mapped[float] = mapped_column(Float, default=1.0)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    pages: Mapped[list | None] = mapped_column(JSON)

    __table_args__ = (
        Index("ix_relation_company_rel", "company_id", "relation"),
        UniqueConstraint(
            "company_id", "source_key", "target_key", "relation",
            name="uq_relation_edge",
        ),
    )


class DocumentJob(Base):
    """A queued ingestion task.

    The queue is a table rather than Redis so the platform keeps its promise of
    running with zero infrastructure. The claim is done with a conditional
    update, which is atomic in both SQLite and Postgres, so two workers cannot
    take the same job.
    """

    __tablename__ = "document_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    company_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    stage: Mapped[str] = mapped_column(String(20), default="queued")
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[float] = mapped_column(Float, default=0.0)
    #: Per-stage timings, for the performance panel.
    timings: Mapped[dict | None] = mapped_column(JSON)

    __table_args__ = (
        Index("ix_job_status_priority", "status", "priority"),
    )
