"""API contracts for the Document Intelligence Center (Phase 6)."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class DocumentAdminOut(BaseModel):
    id: int
    company_id: str
    filename: str
    title: str | None = None
    doc_type: str
    file_format: str
    size_bytes: int = 0
    version: int = 1
    status: str
    approval_status: str
    approval_reviewer: str | None = None
    approved_at: datetime | None = None
    approval_note: str | None = None
    page_count: int = 0
    chunk_count: int = 0
    fact_count: int = 0
    used_ocr: bool = False
    processed_at: datetime | None = None


class ApprovalUpdate(BaseModel):
    state: str = Field(pattern="^(uploaded|ai_extracted|pending_review|approved|published)$")
    note: str | None = None


class CompareOut(BaseModel):
    old: dict[str, Any]
    new: dict[str, Any]
    changed_fields: list[dict[str, Any]]
    changed_count: int
    old_fact_count: int
    new_fact_count: int


class RAGStatsOut(BaseModel):
    documents: int
    chunks: int
    embeddings: int
    vector_count: int
