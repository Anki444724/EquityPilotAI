"""Admin document intelligence service (Phase 6).

Approval workflow, OCR/ingestion actions, version comparison, RAG refresh and
document search for the Document Intelligence Center. Reuses the existing
ingestion and search machinery in :class:`DocumentService`.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models.company import Company
from app.models.document import (
    Document, DocumentChunk, DocumentFact, DocumentSection,
)


class DocumentAdminError(Exception):
    """Raised when a document admin action cannot be honoured."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


#: Valid approval workflow states.
APPROVAL_STATES = ("uploaded", "ai_extracted", "pending_review", "approved", "published")


class DocumentAdminService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def _get(self, document_id: int) -> Document:
        doc = self.db.get(Document, document_id)
        if doc is None:
            raise DocumentAdminError(f"document {document_id} not found")
        return doc

    # ==================================================================
    # List
    # ==================================================================
    def list_documents(
        self, *, company_id: str | None = None, doc_type: str | None = None,
        approval_status: str | None = None, search: str | None = None,
        page: int = 1, page_size: int = 25,
    ) -> tuple[list[Document], int]:
        stmt = select(Document)
        if company_id:
            stmt = stmt.where(Document.company_id == company_id)
        if doc_type:
            stmt = stmt.where(Document.doc_type == doc_type)
        if approval_status:
            stmt = stmt.where(Document.approval_status == approval_status)
        if search:
            pattern = f"%{search.strip().lower()}%"
            stmt = stmt.where(or_(
                func.lower(Document.title).like(pattern),
                func.lower(Document.filename).like(pattern),
            ))
        total = self.db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
        rows = self.db.execute(
            stmt.order_by(Document.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
        ).scalars().all()
        return list(rows), total

    # ==================================================================
    # Approval workflow
    # ==================================================================
    def set_approval(
        self, document_id: int, state: str, *, reviewer: str | None = None,
        note: str | None = None,
    ) -> Document:
        if state not in APPROVAL_STATES:
            raise DocumentAdminError(f"invalid approval state '{state}'")
        doc = self._get(document_id)
        doc.approval_status = state
        if state in ("approved", "published"):
            doc.approval_reviewer = reviewer
            doc.approved_at = _utcnow()
        if note is not None:
            doc.approval_note = note
        self.db.flush()
        return doc

    def approve(self, document_id: int, *, reviewer: str | None = None, note: str | None = None) -> Document:
        return self.set_approval(document_id, "approved", reviewer=reviewer, note=note)

    def publish(self, document_id: int, *, reviewer: str | None = None, note: str | None = None) -> Document:
        return self.set_approval(document_id, "published", reviewer=reviewer, note=note)

    def reject(self, document_id: int, *, reviewer: str | None = None, note: str | None = None) -> Document:
        doc = self._get(document_id)
        doc.approval_status = "pending_review"
        doc.approval_note = note or doc.approval_note
        self.db.flush()
        return doc

    # ==================================================================
    # Detail / compare versions
    # ==================================================================
    def detail(self, document_id: int) -> Document:
        return self._get(document_id)

    def version_history(self, company_id: str, title: str | None = None) -> list[Document]:
        """Documents of the same company, ordered newest first (version lineage)."""
        stmt = select(Document).where(Document.company_id == company_id)
        if title:
            stmt = stmt.where(Document.title == title)
        return list(self.db.execute(stmt.order_by(Document.version.desc())).scalars())

    def compare(self, doc_id: int, other_id: int) -> dict[str, Any]:
        """Compare two document versions and report what changed."""
        a = self._get(doc_id)
        b = self._get(other_id)
        a_facts = self._fact_map(a.id)
        b_facts = self._fact_map(b.id)
        changed = []
        for key in sorted(set(a_facts) | set(b_facts)):
            if a_facts.get(key) != b_facts.get(key):
                changed.append({
                    "field": key,
                    "old": a_facts.get(key),
                    "new": b_facts.get(key),
                })
        return {
            "old": {"id": a.id, "version": a.version, "filename": a.filename,
                    "processed_at": a.processed_at.isoformat() if a.processed_at else None},
            "new": {"id": b.id, "version": b.version, "filename": b.filename,
                    "processed_at": b.processed_at.isoformat() if b.processed_at else None},
            "changed_fields": changed,
            "changed_count": len(changed),
            "old_fact_count": len(a_facts),
            "new_fact_count": len(b_facts),
        }

    def _fact_map(self, document_id: int) -> dict[str, float | str | None]:
        rows = self.db.execute(
            select(DocumentFact).where(DocumentFact.document_id == document_id)
        ).scalars().all()
        out: dict[str, float | str | None] = {}
        for f in rows:
            out[f.field_key] = f.value if f.value is not None else f.text_value
        return out

    # ==================================================================
    # RAG / search
    # ==================================================================
    def rag_stats(self, company_id: str | None = None) -> dict[str, Any]:
        docs = select(Document)
        chunks = select(DocumentChunk)
        if company_id:
            docs = docs.where(Document.company_id == company_id)
            chunks = chunks.where(DocumentChunk.document_id.in_(
                select(Document.id).where(Document.company_id == company_id)
            ))
        doc_count = self.db.execute(select(func.count()).select_from(docs.subquery())).scalar_one()
        chunk_count = self.db.execute(select(func.count()).select_from(chunks.subquery())).scalar_one()
        return {
            "documents": doc_count, "chunks": chunk_count,
            "embeddings": chunk_count, "vector_count": chunk_count,
        }

    def search_documents(
        self, *, company_id: str | None = None, query: str, limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Lexical search over document chunks with matched-text highlights."""
        pattern = f"%{query.strip().lower()}%"
        stmt = select(DocumentChunk, Document).join(
            Document, Document.id == DocumentChunk.document_id
        ).where(func.lower(DocumentChunk.text).like(pattern))
        if company_id:
            stmt = stmt.where(Document.company_id == company_id)
        rows = self.db.execute(stmt.limit(limit)).all()
        results = []
        for chunk, doc in rows:
            results.append({
                "document_id": doc.id, "title": doc.title or doc.filename,
                "chunk_id": chunk.id, "page": chunk.page,
                "text": _highlight(chunk.text, query),
                "score": _score(chunk.text, query),
            })
        return results

    # ==================================================================
    # Actions
    # ==================================================================
    def delete(self, document_id: int) -> None:
        doc = self._get(document_id)
        self.db.delete(doc)
        self.db.flush()


def _highlight(text: str, query: str) -> str:
    """Return the matched text with the query terms wrapped in <mark>."""
    lowered = text.lower()
    query_l = query.lower().strip()
    if not query_l:
        return text[:300]
    start = lowered.find(query_l)
    if start == -1:
        return text[:300]
    begin = max(0, start - 120)
    end = min(len(text), start + len(query_l) + 180)
    snippet = text[begin:end]
    # Mark the first occurrence, case-insensitively.
    match = re.search(re.escape(query_l), snippet, re.IGNORECASE)
    if match:
        snippet = (snippet[:match.start()]
                   + f"<mark>{match.group()}</mark>"
                   + snippet[match.end():])
    return snippet


def _score(text: str, query: str) -> float:
    ql = query.lower().strip()
    return text.lower().count(ql)
