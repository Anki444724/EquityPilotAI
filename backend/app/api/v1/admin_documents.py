"""Document Intelligence Center endpoints (Phase 6)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.security import CurrentUser, get_current_user, require
from app.db.base import get_db
from app.domain.platform.identity import Permission
from app.schemas.document_admin import (
    ApprovalUpdate, DocumentAdminOut, CompareOut, RAGStatsOut,
)
from app.services.document_admin_service import (
    DocumentAdminError, DocumentAdminService,
)

router = APIRouter(prefix="/admin/documents", tags=["admin-documents"])


def _service(db: Session = Depends(get_db)) -> DocumentAdminService:
    return DocumentAdminService(db)


def _doc_out(doc) -> DocumentAdminOut:
    return DocumentAdminOut(
        id=doc.id, company_id=doc.company_id, filename=doc.filename,
        title=doc.title, doc_type=doc.doc_type, file_format=doc.file_format,
        size_bytes=doc.size_bytes, version=doc.version, status=doc.status,
        approval_status=doc.approval_status, approval_reviewer=doc.approval_reviewer,
        approved_at=doc.approved_at, approval_note=doc.approval_note,
        page_count=doc.page_count, chunk_count=doc.chunk_count,
        fact_count=doc.fact_count, used_ocr=doc.used_ocr,
        processed_at=doc.processed_at,
    )


@router.get(
    "", summary="List documents (admin)",
    dependencies=[Depends(require(Permission.DOCUMENT_READ))],
)
def list_documents(
    company_id: str | None = None, doc_type: str | None = None,
    approval_status: str | None = None, search: str | None = None,
    page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=200),
    svc: DocumentAdminService = Depends(_service),
):
    rows, total = svc.list_documents(
        company_id=company_id, doc_type=doc_type, approval_status=approval_status,
        search=search, page=page, page_size=page_size,
    )
    return {"total": total, "page": page, "page_size": page_size,
            "items": [_doc_out(d) for d in rows]}


@router.get(
    "/rag/stats", response_model=RAGStatsOut,
    summary="RAG index statistics",
    dependencies=[Depends(require(Permission.DOCUMENT_READ))],
)
def rag_stats(company_id: str | None = None, svc: DocumentAdminService = Depends(_service)):
    return svc.rag_stats(company_id=company_id)


@router.get(
    "/search", summary="Search inside documents with highlights",
    dependencies=[Depends(require(Permission.DOCUMENT_READ))],
)
def search(q: str = Query(..., min_length=1), company_id: str | None = None,
           limit: int = Query(20, ge=1, le=100),
           svc: DocumentAdminService = Depends(_service)):
    return {"results": svc.search_documents(company_id=company_id, query=q, limit=limit)}


# ------------------------------------------------------------- delete
@router.get(
    "/{document_id}", response_model=DocumentAdminOut,
    summary="Document detail",
    dependencies=[Depends(require(Permission.DOCUMENT_READ))],
)
def get_document(document_id: int, svc: DocumentAdminService = Depends(_service)):
    try:
        return _doc_out(svc.detail(document_id))
    except DocumentAdminError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc))


# ------------------------------------------------------------- approval
@router.post(
    "/{document_id}/approval", response_model=DocumentAdminOut,
    summary="Set approval state",
    dependencies=[Depends(require(Permission.DOCUMENT_UPLOAD))],
)
def set_approval(
    document_id: int, body: ApprovalUpdate, svc: DocumentAdminService = Depends(_service),
    user: CurrentUser = Depends(get_current_user),
):
    try:
        doc = svc.set_approval(
            document_id, body.state, reviewer=user.email, note=body.note,
        )
    except DocumentAdminError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    svc.db.commit()
    return _doc_out(doc)


@router.post(
    "/{document_id}/approve", response_model=DocumentAdminOut,
    summary="Approve a document",
    dependencies=[Depends(require(Permission.DOCUMENT_UPLOAD))],
)
def approve(document_id: int, note: str | None = Query(None),
            svc: DocumentAdminService = Depends(_service),
            user: CurrentUser = Depends(get_current_user)):
    try:
        doc = svc.approve(document_id, reviewer=user.email, note=note)
    except DocumentAdminError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    svc.db.commit()
    return _doc_out(doc)


@router.post(
    "/{document_id}/publish", response_model=DocumentAdminOut,
    summary="Publish an approved document",
    dependencies=[Depends(require(Permission.DOCUMENT_UPLOAD))],
)
def publish(document_id: int, note: str | None = Query(None),
            svc: DocumentAdminService = Depends(_service),
            user: CurrentUser = Depends(get_current_user)):
    try:
        doc = svc.publish(document_id, reviewer=user.email, note=note)
    except DocumentAdminError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    svc.db.commit()
    return _doc_out(doc)


@router.post(
    "/{document_id}/reject", response_model=DocumentAdminOut,
    summary="Reject a document (back to pending review)",
    dependencies=[Depends(require(Permission.DOCUMENT_UPLOAD))],
)
def reject(document_id: int, note: str | None = Query(None),
           svc: DocumentAdminService = Depends(_service),
           user: CurrentUser = Depends(get_current_user)):
    try:
        doc = svc.reject(document_id, reviewer=user.email, note=note)
    except DocumentAdminError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    svc.db.commit()
    return _doc_out(doc)


# ------------------------------------------------------------- compare / versions
@router.get(
    "/{document_id}/versions", summary="Document version history",
    dependencies=[Depends(require(Permission.DOCUMENT_READ))],
)
def version_history(document_id: int, svc: DocumentAdminService = Depends(_service)):
    try:
        doc = svc.detail(document_id)
        return [_doc_out(d) for d in svc.version_history(doc.company_id, doc.title)]
    except DocumentAdminError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc))


@router.get(
    "/{document_id}/compare/{other_id}", response_model=CompareOut,
    summary="Compare two document versions",
    dependencies=[Depends(require(Permission.DOCUMENT_READ))],
)
def compare(document_id: int, other_id: int, svc: DocumentAdminService = Depends(_service)):
    try:
        return svc.compare(document_id, other_id)
    except DocumentAdminError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc))


# ------------------------------------------------------------- RAG / search
@router.delete(
    "/{document_id}", status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a document",
    dependencies=[Depends(require(Permission.DOCUMENT_DELETE))],
)
def delete_document(document_id: int, svc: DocumentAdminService = Depends(_service)):
    try:
        svc.delete(document_id)
    except DocumentAdminError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    svc.db.commit()
