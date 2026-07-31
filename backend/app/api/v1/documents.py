"""Document intelligence endpoints.

    POST /documents/upload              upload and ingest a document
    GET  /documents                     list documents
    GET  /documents/search              semantic search with citations
    GET  /documents/capabilities        engine self-description
    GET  /documents/statistics          corpus counters
    GET  /documents/coverage            extraction coverage vs the 73 fields
    GET  /documents/chunks              indexed passages
    GET  /documents/entities            extracted entities
    GET  /documents/tables              recovered tables
    GET  /documents/facts               the structured extraction store
    GET  /documents/knowledge           the knowledge graph
    GET  /documents/jobs                the ingestion queue
    POST /documents/reindex             re-embed without re-parsing
    GET  /documents/{id}                one document with its structure
    GET  /documents/{id}/pages/{n}      page text
    POST /documents/{id}/reprocess      re-run the pipeline
    DELETE /documents/{id}              remove a document

The literal paths are declared before ``/documents/{document_id}`` because
FastAPI matches in declaration order; without that, a request for
``/documents/search`` would be routed to the detail handler with
``document_id="search"`` and fail on the integer coercion.
"""
from __future__ import annotations

import time

from fastapi import (
    APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status,
)
from fastapi.responses import Response, StreamingResponse
from sqlalchemy.orm import Session

from app.core.security import CurrentUser, get_current_user
from app.db.base import get_db
from app.domain.documents.fields import FIELD_COUNT, FIELD_SPECS, FieldCategory
from app.domain.documents.types import (
    DocumentError, DocumentStatus, DocumentType, EntityKind, PIPELINE_STAGES,
    ParseFailure, ProcessingStage, RelationKind, SectionKind, UnsupportedFormat,
)
from app.models.document import Document, DocumentJob
from app.schemas.document import (
    CapabilitiesResponse, CategoryCoverage, ChunkOut, CitationOut,
    CoverageResponse, DocumentDetail, DocumentSummary, EntityOut, FactOut,
    FieldSpecOut, GraphResponse, JobOut, PageOut, ReindexResponse,
    DocumentStatusResponse, ProcessingLogEntry, SearchHitOut, SearchResponse,
    SectionOut, StatisticsResponse, TableOut, UploadResponse,
)
from app.services.documents.extractors.base import registered_formats
from app.services.documents.extractors.ocr import OcrEngine
from app.services.documents.pipeline.search import verify_answer_citations
from app.services.documents.ingestion import (
    DocumentIngestionService, IngestionError,
)
from app.services.documents.service import MAX_UPLOAD_BYTES, DocumentService
from app.services.documents.storage import StorageError, get_storage, iter_stream

router = APIRouter(tags=["documents"])


def _ingestion(db: Session = Depends(get_db)) -> DocumentIngestionService:
    """Async ingestion service. Sync dependency: the auth and DB layers are
    synchronous, and an async dependency doing sync DB work blocks the loop."""
    return DocumentIngestionService(db)


def _service(db: Session = Depends(get_db)) -> DocumentService:
    return DocumentService(db)


def _detail(service: DocumentService, document: Document) -> DocumentDetail:
    detail = DocumentDetail.model_validate(document)
    detail.sections = [
        SectionOut.model_validate(s) for s in service.sections(document.id)
    ]
    detail.pages = [PageOut.model_validate(p) for p in service.pages(document.id)]
    detail.doc_metadata = {
        k: str(v) for k, v in (document.doc_metadata or {}).items()
    }
    return detail


# ===========================================================================
# Upload
# ===========================================================================
@router.post(
    "/documents/upload",
    response_model=UploadResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upload a document and queue it for ingestion",
)
async def upload_document(
    company_id: str = Form(...),
    file: UploadFile = File(...),
    doc_type: DocumentType | None = Form(default=None),
    ingestion: DocumentIngestionService = Depends(_ingestion),
    user: CurrentUser = Depends(get_current_user),
) -> UploadResponse:
    """Validate, store the bytes, create the row, enqueue — then return.

    **202 Accepted, not 201 Created.** Nothing has been parsed when this
    returns. Ingesting a 500–1000 page annual report takes minutes and cannot
    happen inside an HTTP request: the edge proxy times out long before, and
    the client is left unable to discover the outcome of work that may still
    be running. The response carries `status_url`; poll it until `pending`
    is false.

    The upload is streamed to storage rather than read into memory, so a
    200 MB report costs a buffer, not 200 MB of resident set.
    """
    declared = None
    length = file.headers.get("content-length") if file.headers else None
    if length and length.isdigit():
        declared = int(length)

    try:
        accepted = ingestion.accept(
            company_id, file.file, file.filename or "upload",
            doc_type=doc_type, uploaded_by=user.id, declared_size=declared,
        )
    except UnsupportedFormat as exc:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, str(exc))
    except StorageError as exc:
        raise HTTPException(status.HTTP_507_INSUFFICIENT_STORAGE, str(exc))
    except (IngestionError, DocumentError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))

    messages = {
        "duplicate": (
            "This file has already been ingested; the existing document is "
            "returned unchanged."
        ),
        "new_version": (
            "A new version of an existing document. The previous version is "
            "retained and marked superseded so earlier citations still resolve."
        ),
        "created": (
            "Upload stored and queued for ingestion. Poll status_url for "
            "progress."
        ),
    }
    return UploadResponse(
        document=DocumentSummary.model_validate(accepted.document),
        action=accepted.action,
        duplicate_of=accepted.duplicate_of,
        superseded=accepted.superseded,
        message=messages[accepted.action],
        job_id=accepted.job_id,
        status_url=f"/api/v1/documents/{accepted.document.id}/status",
    )


@router.get(
    "/documents/{document_id}/status",
    response_model=DocumentStatusResponse,
    summary="Ingestion progress for one document",
)
def document_status(
    document_id: int,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> DocumentStatusResponse:
    """Poll target for the 202. Cheap enough to call every second."""
    document = db.get(Document, document_id)
    if document is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown document")

    state = DocumentStatus(document.status) if document.status in {
        s.value for s in DocumentStatus
    } else None
    return DocumentStatusResponse(
        document_id=document.id,
        filename=document.filename,
        status=document.status,
        stage=document.stage,
        progress=round(document.progress, 4),
        progress_percent=int(round(document.progress * 100)),
        attempts=document.attempts,
        error=document.error,
        page_count=document.page_count,
        chunk_count=document.chunk_count,
        fact_count=document.fact_count,
        entity_count=document.entity_count,
        processing_ms=document.processing_ms,
        indexed=bool(state and state.is_indexed),
        pending=not (state.is_terminal if state else False),
        storage_key=document.storage_key,
        storage_backend=document.storage_backend,
        log=[ProcessingLogEntry(**e) for e in (document.processing_log or [])],
    )


@router.post(
    "/documents/{document_id}/reprocess",
    response_model=UploadResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Re-run ingestion from the stored original",
)
def reprocess_document(
    document_id: int,
    force: bool = Query(default=False),
    ingestion: DocumentIngestionService = Depends(_ingestion),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> UploadResponse:
    """Re-parse a document without asking anyone to upload it again.

    This is what retaining the original buys. Changing the chunker, the OCR
    settings or the embedding model, or recovering a failed run, re-reads the
    stored bytes.
    """
    try:
        job_id = ingestion.reprocess(document_id, force=force)
    except IngestionError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc))

    document = db.get(Document, document_id)
    return UploadResponse(
        document=DocumentSummary.model_validate(document),
        action="created",
        message="Re-ingestion queued from the stored original.",
        job_id=job_id,
        status_url=f"/api/v1/documents/{document_id}/status",
    )


@router.get(
    "/documents/{document_id}/source",
    summary="Download the stored original",
)
def download_source(
    document_id: int,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> StreamingResponse:
    """Stream the original file back.

    Streamed in blocks so serving a 200 MB report does not materialise it in
    memory, and so an auditor can verify a citation against the actual page.
    """
    document = db.get(Document, document_id)
    if document is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown document")
    if not document.storage_key:
        raise HTTPException(
            status.HTTP_410_GONE,
            "this document predates source retention; no original is stored",
        )
    storage = get_storage()
    try:
        handle = storage.open(document.storage_key)
    except StorageError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc))

    return StreamingResponse(
        iter_stream(handle),
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{document.filename}"',
            "Content-Length": str(document.size_bytes),
        },
    )


# ===========================================================================
# Collection reads — declared before /{document_id}
# ===========================================================================
@router.get("/documents", response_model=list[DocumentSummary])
def list_documents(
    company_id: str | None = Query(default=None),
    doc_type: DocumentType | None = Query(default=None),
    doc_status: str | None = Query(default=None, alias="status"),
    include_superseded: bool = Query(default=True),
    service: DocumentService = Depends(_service),
    user: CurrentUser = Depends(get_current_user),
) -> list[DocumentSummary]:
    documents = service.list_documents(
        company_id, doc_type=doc_type, status=doc_status,
        include_superseded=include_superseded,
    )
    return [DocumentSummary.model_validate(d) for d in documents]


@router.get(
    "/documents/search",
    response_model=SearchResponse,
    summary="Semantic search returning answer, passages, pages and confidence",
)
def search_documents(
    q: str = Query(min_length=1, max_length=1000),
    company_id: str | None = Query(default=None),
    top_k: int = Query(default=8, ge=1, le=50),
    document_id: list[int] | None = Query(default=None),
    section: list[SectionKind] | None = Query(default=None),
    service: DocumentService = Depends(_service),
    user: CurrentUser = Depends(get_current_user),
) -> SearchResponse:
    started = time.perf_counter()
    answer = service.search(
        q, company_id=company_id, top_k=top_k,
        document_ids=document_id, sections=section,
    )
    citations = service.citations_for(answer)
    took = (time.perf_counter() - started) * 1000.0
    return SearchResponse(
        query=answer.query,
        answer=answer.answer,
        confidence=answer.confidence,
        unavailable_reason=answer.unavailable_reason,
        hits=[
            SearchHitOut(
                chunk_id=h.chunk_id, document_id=h.document_id,
                document_title=h.document_title, page=h.page,
                paragraph=h.paragraph, section=h.section.value, text=h.text,
                score=h.score, lexical_score=h.lexical_score,
                semantic_score=h.semantic_score,
            )
            for h in answer.hits
        ],
        citations=[CitationOut(**c.to_dict()) for c in citations],
        citation_audit=verify_answer_citations(answer.answer, citations),
        took_ms=round(took, 3),
    )


@router.get("/documents/capabilities", response_model=CapabilitiesResponse)
def capabilities(
    service: DocumentService = Depends(_service),
    user: CurrentUser = Depends(get_current_user),
) -> CapabilitiesResponse:
    """What the engine supports. Derived from the registries, never hard-coded."""
    return CapabilitiesResponse(
        document_types=[t.value for t in DocumentType],
        file_formats=[f.value for f in registered_formats()],
        sections=[s.value for s in SectionKind],
        entity_kinds=[e.value for e in EntityKind],
        relation_kinds=[r.value for r in RelationKind],
        pipeline_stages=[s.value for s in PIPELINE_STAGES],
        fields=[
            FieldSpecOut(
                key=f.key, label=f.label, category=f.category.value,
                unit=f.unit.value, target=f.target,
            )
            for f in FIELD_SPECS
        ],
        field_count=FIELD_COUNT,
        ocr=OcrEngine().describe(),
        embedding={
            "provider": service.embedder.spec.provider,
            "model": service.embedder.spec.model,
            "dimension": service.embedder.spec.dimension,
        },
    )


@router.get("/documents/statistics", response_model=StatisticsResponse)
def statistics(
    company_id: str | None = Query(default=None),
    service: DocumentService = Depends(_service),
    user: CurrentUser = Depends(get_current_user),
) -> StatisticsResponse:
    return StatisticsResponse(**service.statistics(company_id))


@router.get("/documents/coverage", response_model=CoverageResponse)
def coverage(
    company_id: str = Query(...),
    service: DocumentService = Depends(_service),
    user: CurrentUser = Depends(get_current_user),
) -> CoverageResponse:
    try:
        payload = service.coverage(company_id)
    except DocumentError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    payload["categories"] = [CategoryCoverage(**c) for c in payload["categories"]]
    return CoverageResponse(**payload)


@router.get("/documents/chunks", response_model=list[ChunkOut])
def list_chunks(
    document_id: int = Query(...),
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    service: DocumentService = Depends(_service),
    user: CurrentUser = Depends(get_current_user),
) -> list[ChunkOut]:
    return [
        ChunkOut.model_validate(c)
        for c in service.chunks(document_id, limit=limit, offset=offset)
    ]


@router.get("/documents/tables", response_model=list[TableOut])
def list_tables(
    document_id: int = Query(...),
    service: DocumentService = Depends(_service),
    user: CurrentUser = Depends(get_current_user),
) -> list[TableOut]:
    return [TableOut.model_validate(t) for t in service.tables(document_id)]


@router.get("/documents/entities", response_model=list[EntityOut])
def list_entities(
    company_id: str | None = Query(default=None),
    document_id: int | None = Query(default=None),
    kind: EntityKind | None = Query(default=None),
    min_confidence: float = Query(default=0.0, ge=0.0, le=1.0),
    service: DocumentService = Depends(_service),
    user: CurrentUser = Depends(get_current_user),
) -> list[EntityOut]:
    if company_id is None and document_id is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "either company_id or document_id is required",
        )
    return [
        EntityOut.model_validate(e)
        for e in service.entities(
            company_id=company_id, document_id=document_id,
            kind=kind, min_confidence=min_confidence,
        )
    ]


@router.get("/documents/facts", response_model=list[FactOut])
def list_facts(
    company_id: str | None = Query(default=None),
    document_id: int | None = Query(default=None),
    category: FieldCategory | None = Query(default=None),
    field_key: str | None = Query(default=None),
    period: str | None = Query(default=None),
    service: DocumentService = Depends(_service),
    user: CurrentUser = Depends(get_current_user),
) -> list[FactOut]:
    if company_id is None and document_id is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "either company_id or document_id is required",
        )
    return [
        FactOut.model_validate(f)
        for f in service.facts(
            company_id=company_id, document_id=document_id,
            category=category, field_key=field_key, period=period,
        )
    ]


@router.get("/documents/knowledge", response_model=GraphResponse)
def knowledge_graph(
    company_id: str = Query(...),
    min_confidence: float = Query(default=0.0, ge=0.0, le=1.0),
    service: DocumentService = Depends(_service),
    user: CurrentUser = Depends(get_current_user),
) -> GraphResponse:
    try:
        return GraphResponse(**service.graph(company_id, min_confidence=min_confidence))
    except DocumentError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc))


@router.get("/documents/jobs", response_model=list[JobOut])
def list_jobs(
    company_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> list[JobOut]:
    from sqlalchemy import select

    query = select(DocumentJob).order_by(DocumentJob.id.desc())
    if company_id is not None:
        query = query.where(DocumentJob.company_id == company_id)
    return [JobOut.model_validate(j) for j in db.scalars(query).all()]


@router.post("/documents/reindex", response_model=ReindexResponse)
def reindex(
    company_id: str | None = Query(default=None),
    service: DocumentService = Depends(_service),
    user: CurrentUser = Depends(get_current_user),
) -> ReindexResponse:
    """Re-embed stored chunks without re-parsing the source files."""
    started = time.perf_counter()
    # Re-embeds stored chunks without re-parsing — the cheap path, correct
    # when only the embedding model changed. To re-parse from the original
    # bytes (new chunker, new OCR settings, or recovering a failed run) use
    # POST /documents/{id}/reprocess, which re-reads the stored source.
    count = service.reindex(company_id)
    return ReindexResponse(
        reindexed_chunks=count,
        embedding={
            "provider": service.embedder.spec.provider,
            "model": service.embedder.spec.model,
            "dimension": service.embedder.spec.dimension,
        },
        took_ms=round((time.perf_counter() - started) * 1000.0, 3),
    )


# ===========================================================================
# Item reads — declared last so literal paths win
# ===========================================================================
@router.get("/documents/{document_id}", response_model=DocumentDetail)
def get_document(
    document_id: int,
    service: DocumentService = Depends(_service),
    user: CurrentUser = Depends(get_current_user),
) -> DocumentDetail:
    document = service.get(document_id)
    if document is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")
    return _detail(service, document)


@router.get("/documents/{document_id}/pages/{page_number}")
def get_page(
    document_id: int,
    page_number: int,
    service: DocumentService = Depends(_service),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    for page in service.pages(document_id):
        if page.page_number == page_number:
            return {
                "document_id": document_id,
                "page_number": page.page_number,
                "text": page.text,
                "text_source": page.text_source,
                "ocr_confidence": page.ocr_confidence,
                "char_count": page.char_count,
            }
    raise HTTPException(status.HTTP_404_NOT_FOUND, "page not found")


@router.delete(
    "/documents/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
def delete_document(
    document_id: int,
    db: Session = Depends(get_db),
    service: DocumentService = Depends(_service),
    user: CurrentUser = Depends(get_current_user),
) -> Response:
    document = service.get(document_id)
    if document is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")
    db.delete(document)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
