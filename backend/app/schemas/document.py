"""Typed contracts for the document-intelligence API.

Every response is a declared model. Nothing returns a bare dict, so the OpenAPI
schema is a real description of the surface rather than a promise that it might
be JSON.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.documents.types import (
    DocumentType, EntityKind, FileFormat, RelationKind, SectionKind,
)


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------
class DocumentSummary(ORMModel):
    """A document as it appears in a list."""

    id: int
    company_id: str
    filename: str
    title: str | None = None
    doc_type: str
    file_format: str
    size_bytes: int = 0
    version: int = 1
    superseded_by: int | None = None
    period: str | None = None
    fiscal_year: int | None = None

    status: str
    stage: str
    progress: float = 0.0
    error: str | None = None

    page_count: int = 0
    chunk_count: int = 0
    table_count: int = 0
    entity_count: int = 0
    fact_count: int = 0
    used_ocr: bool = False
    ocr_pages: int = 0
    coverage: float = 0.0
    avg_confidence: float = 0.0
    duplicate_ratio: float = 0.0
    processing_ms: float = 0.0
    processed_at: datetime | None = None
    created_at: datetime | None = None

    @property
    def is_current(self) -> bool:
        return self.superseded_by is None


class SectionOut(ORMModel):
    kind: str
    title: str
    start_page: int
    end_page: int
    confidence: float


class PageOut(ORMModel):
    page_number: int
    text_source: str
    ocr_confidence: float | None = None
    char_count: int = 0


class DocumentDetail(DocumentSummary):
    """A single document with its structure."""

    sections: list[SectionOut] = Field(default_factory=list)
    pages: list[PageOut] = Field(default_factory=list)
    #: Serialised as "metadata", but read from `doc_metadata` on the model.
    #: `Base.metadata` is SQLAlchemy's own MetaData object, so a field simply
    #: named `metadata` with from_attributes=True picks that up instead and
    #: fails validation on every request.
    #: Nullable on the model: under asynchronous ingestion the row is created
    #: when the upload is accepted, before anything has been parsed, so a
    #: queued document genuinely has no metadata yet. A plain default is not
    #: enough — Pydantic only applies it when the attribute is absent, not
    #: when it is present and None — so the None is coerced explicitly.
    doc_metadata: dict[str, str] = Field(
        default_factory=dict, serialization_alias="metadata",
    )

    @field_validator("doc_metadata", mode="before")
    @classmethod
    def _default_metadata(cls, value: object) -> object:
        return {} if value is None else value


class UploadResponse(BaseModel):
    """Result of an upload.

    Returned with 202 Accepted, not 201: the bytes are stored and the work is
    queued, but parsing, OCR, chunking and embedding have not run yet. The
    client polls `status_url` until `status` reaches "completed" or "failed".
    """

    document: DocumentSummary
    #: "created", "duplicate" or "new_version".
    action: str
    duplicate_of: int | None = None
    superseded: int | None = None
    message: str
    #: Ingestion job, absent for a byte-identical duplicate (nothing requeued).
    job_id: int | None = None
    #: Where to poll for progress.
    status_url: str | None = None


class ProcessingLogEntry(BaseModel):
    """One line of the persisted processing log."""

    at: str
    stage: str
    status: str
    progress: float
    message: str
    ms: float = 0.0


class DocumentStatusResponse(BaseModel):
    """Live ingestion state, for polling after a 202."""

    document_id: int
    filename: str
    status: str
    stage: str
    #: 0.0–1.0.
    progress: float
    #: Whole percent, for a progress bar.
    progress_percent: int
    attempts: int = 0
    error: str | None = None
    page_count: int = 0
    chunk_count: int = 0
    fact_count: int = 0
    entity_count: int = 0
    processing_ms: float = 0.0
    #: True once search and the AI layer can use this document.
    indexed: bool = False
    #: True while the client should keep polling.
    pending: bool = True
    storage_key: str | None = None
    storage_backend: str | None = None
    log: list[ProcessingLogEntry] = []


# ---------------------------------------------------------------------------
# Chunks, tables, entities, facts
# ---------------------------------------------------------------------------
class ChunkOut(ORMModel):
    id: int
    document_id: int
    chunk_index: int
    text: str
    page: int
    paragraph: int
    section: str
    section_title: str | None = None
    token_estimate: int = 0
    fingerprint: str


class TableOut(ORMModel):
    id: int
    document_id: int
    page: int
    table_index: int
    caption: str | None = None
    unit: str
    header: list[str] = Field(default_factory=list)
    rows: list[list[str]] = Field(default_factory=list)
    #: Merged spans as [row, col, rowspan, colspan].
    merged: list[list[int]] = Field(default_factory=list)
    n_rows: int = 0
    n_cols: int = 0
    confidence: float = 0.0


class EntityOut(ORMModel):
    id: int
    document_id: int
    kind: str
    name: str
    normalised: str
    page: int
    context: str | None = None
    confidence: float
    mentions: int = 1


class FactOut(ORMModel):
    id: int
    document_id: int
    category: str
    field_key: str
    label: str
    value: float | None = None
    text_value: str | None = None
    unit: str
    period: str | None = None
    fiscal_year: int | None = None
    page: int
    section: str
    confidence: float
    evidence: str | None = None


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------
class SearchHitOut(BaseModel):
    chunk_id: int
    document_id: int
    document_title: str
    page: int
    paragraph: int
    section: str
    text: str
    score: float
    lexical_score: float = 0.0
    semantic_score: float = 0.0


class CitationOut(BaseModel):
    """Document · page · section · paragraph — the brief's four fields."""

    document_id: int
    document_title: str
    page: int
    section: str
    paragraph: int
    chunk_id: int
    quote: str
    reference: str


class SearchResponse(BaseModel):
    """Answer, supporting paragraphs, page numbers, confidence."""

    query: str
    answer: str
    confidence: float
    #: Populated when the corpus cannot support an answer. Never a guess.
    unavailable_reason: str | None = None
    hits: list[SearchHitOut] = Field(default_factory=list)
    citations: list[CitationOut] = Field(default_factory=list)
    #: Verification that every page the answer cites was actually retrieved.
    citation_audit: dict = Field(default_factory=dict)
    took_ms: float = 0.0


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=1000)
    company_id: str | None = None
    document_ids: list[int] | None = None
    sections: list[SectionKind] | None = None
    top_k: int = Field(default=8, ge=1, le=50)


# ---------------------------------------------------------------------------
# Knowledge graph
# ---------------------------------------------------------------------------
class GraphNodeOut(BaseModel):
    key: str
    kind: str
    label: str
    weight: float
    degree: int
    attributes: dict[str, str] = Field(default_factory=dict)


class GraphEdgeOut(BaseModel):
    source: str
    target: str
    relation: str
    weight: float
    pages: list[int] = Field(default_factory=list)
    confidence: float


class GraphResponse(BaseModel):
    company: dict
    nodes: list[GraphNodeOut] = Field(default_factory=list)
    edges: list[GraphEdgeOut] = Field(default_factory=list)
    stats: dict


# ---------------------------------------------------------------------------
# Coverage and statistics
# ---------------------------------------------------------------------------
class CategoryCoverage(BaseModel):
    category: str
    defined: int
    extracted: int
    coverage: float
    avg_confidence: float
    #: Fields the platform looked for and did not find — reported, not hidden.
    missing: list[str] = Field(default_factory=list)


class CoverageResponse(BaseModel):
    company_id: str
    fields_defined: int
    fields_extracted: int
    coverage: float
    avg_confidence: float
    documents: int
    documents_ready: int
    categories: list[CategoryCoverage] = Field(default_factory=list)


class StatisticsResponse(BaseModel):
    documents: int
    current_documents: int
    superseded: int
    pages: int
    chunks: int
    tables: int
    entities: int
    facts: int
    ocr_documents: int
    by_type: dict[str, int] = Field(default_factory=dict)
    by_status: dict[str, int] = Field(default_factory=dict)
    queue: dict[str, int] = Field(default_factory=dict)
    embedding: dict = Field(default_factory=dict)
    ocr: dict = Field(default_factory=dict)
    supported_formats: list[str] = Field(default_factory=list)


class FieldSpecOut(BaseModel):
    key: str
    label: str
    category: str
    unit: str
    target: str | None = None


class CapabilitiesResponse(BaseModel):
    """What the engine can do — for the UI and for honest self-description."""

    document_types: list[str]
    file_formats: list[str]
    sections: list[str]
    entity_kinds: list[str]
    relation_kinds: list[str]
    pipeline_stages: list[str]
    fields: list[FieldSpecOut]
    field_count: int
    ocr: dict
    embedding: dict


class JobOut(ORMModel):
    id: int
    document_id: int
    company_id: str
    status: str
    stage: str
    progress: float
    attempts: int
    error: str | None = None
    duration_ms: float = 0.0
    timings: dict | None = None


class ReindexResponse(BaseModel):
    reindexed_chunks: int
    embedding: dict
    took_ms: float
