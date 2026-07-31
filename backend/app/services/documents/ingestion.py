"""Asynchronous document ingestion.

Replaces the synchronous upload path, in which parsing, OCR, chunking and
embedding all ran inside the HTTP request. That design failed in three ways
that only appear in production:

* A 500–1000 page report takes minutes. The platform's edge proxy closes the
  connection long before, so the client sees a failure for work that may still
  be running, and the browser has no way to learn the outcome.
* The uploaded bytes were discarded when the request ended, so a re-index had
  nothing to re-parse and a failure lost the document.
* One slow upload occupied a request worker for the whole duration.

The request now does the minimum that must be transactional — validate, store
the bytes, insert the row, enqueue — and returns **202 Accepted**. Everything
expensive happens in the background worker, which reads the bytes back from
storage. Because the source is durable, a failed run can be retried without
the user re-uploading, and re-index is a re-run rather than a re-upload.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, BinaryIO

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.documents.types import (
    STATUS_PROGRESS, DocumentStatus, DocumentType, ProcessingStage,
)
from app.models.company import Company
from app.models.document import Document, DocumentJob
from app.services.documents.storage import (
    DocumentStorage, StorageError, free_disk_bytes, get_storage,
)
from app.services.platform.cache import Namespace, cache

log = structlog.get_logger(__name__)

#: Cap on retained log entries. A 1000-page report emits a handful per stage;
#: this stops a pathological retry loop growing the row without bound.
MAX_LOG_ENTRIES = 200


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class IngestionError(Exception):
    """Raised when an upload cannot be accepted."""


@dataclass(frozen=True, slots=True)
class AcceptedUpload:
    """What the 202 response reports back."""

    document: Document
    job_id: int | None
    action: str                      # created | duplicate | new_version
    duplicate_of: int | None = None
    superseded: int | None = None


class DocumentIngestionService:
    """Accepts uploads and drives the background pipeline."""

    def __init__(self, db: Session, storage: DocumentStorage | None = None) -> None:
        self.db = db
        self.storage = storage or get_storage()

    # ==================================================================
    # Request path — must stay fast and must not parse anything
    # ==================================================================
    def accept(
        self,
        company_id: str,
        source: BinaryIO | bytes,
        filename: str,
        *,
        doc_type: DocumentType | None = None,
        uploaded_by: str | None = None,
        declared_size: int | None = None,
    ) -> AcceptedUpload:
        """Validate, persist the bytes, create the row, enqueue. No parsing.

        The order matters. Bytes are written **before** the row is committed
        so a row can never reference an object that does not exist; the
        reverse ordering leaves a document pointing at nothing when the
        process dies between the two.
        """
        from app.core.config import settings
        from app.services.documents.extractors.base import DocumentParser

        company = self.db.get(Company, company_id)
        if company is None:
            raise IngestionError(f"unknown company '{company_id}'")

        # Extension is checked before a single byte is written, so an
        # unsupported file never occupies storage or a row.
        file_format = DocumentParser.format_for(filename)

        max_bytes = settings.DOCUMENT_MAX_UPLOAD_MB * 1024 * 1024
        if declared_size is not None and declared_size > max_bytes:
            raise IngestionError(
                f"file exceeds the {settings.DOCUMENT_MAX_UPLOAD_MB} MB limit"
            )

        if self.storage.backend == "local":
            free = free_disk_bytes(settings.DOCUMENT_STORAGE_PATH)
            floor = settings.DOCUMENT_MIN_FREE_DISK_MB * 1024 * 1024
            if free >= 0 and declared_size and free - declared_size < floor:
                raise IngestionError(
                    "insufficient storage on the document volume; "
                    "free space or increase the volume before retrying"
                )

        # Written to a provisional key, then moved to the content-addressed
        # key once the hash is known. Hashing requires reading the stream, and
        # a 200 MB upload must not be held in memory to hash it twice.
        provisional = f"documents/{company_id}/_incoming/{int(time.time()*1000)}"
        stored = self.storage.put(provisional, source)

        if stored.size_bytes == 0:
            self.storage.delete(provisional)
            raise IngestionError("uploaded file is empty")
        if stored.size_bytes > max_bytes:
            self.storage.delete(provisional)
            raise IngestionError(
                f"file exceeds the {settings.DOCUMENT_MAX_UPLOAD_MB} MB limit"
            )

        digest = stored.content_hash
        final_key = DocumentStorage.build_key(company_id, digest, filename)

        existing = self.db.scalar(
            select(Document).where(
                Document.company_id == company_id,
                Document.content_hash == digest,
            )
        )
        if existing is not None:
            # Byte-identical re-upload. Keep whichever copy is already
            # referenced and drop the provisional one.
            self.storage.delete(provisional)
            if not existing.storage_key:
                # An older row from before durable storage: adopt these bytes
                # so it becomes re-indexable.
                self._adopt(existing, final_key, source_key=None)
            return AcceptedUpload(
                existing, job_id=None, action="duplicate", duplicate_of=existing.id,
            )

        # Promote to the final key by re-writing from the provisional object.
        with self.storage.open(provisional) as handle:
            final = self.storage.put(final_key, handle)
        self.storage.delete(provisional)

        predecessor = self._find_predecessor(company_id, filename)
        version = (predecessor.version + 1) if predecessor else 1

        document = Document(
            company_id=company_id,
            filename=filename,
            title=filename,
            doc_type=(doc_type or DocumentType.OTHER).value,
            file_format=file_format.value,
            size_bytes=final.size_bytes,
            content_hash=digest,
            version=version,
            storage_key=final.key,
            storage_backend=final.backend,
            storage_location=final.location,
            status=DocumentStatus.UPLOADED.value,
            stage=ProcessingStage.QUEUED.value,
            progress=STATUS_PROGRESS[DocumentStatus.UPLOADED],
            uploaded_by=uploaded_by,
            processing_log=[],
        )
        self.db.add(document)
        self.db.flush()

        superseded: int | None = None
        if predecessor is not None:
            predecessor.superseded_by = document.id
            superseded = predecessor.id

        self._log(document, ProcessingStage.QUEUED,
                  f"stored {final.size_bytes:,} bytes at {final.key}")

        job = DocumentJob(
            document_id=document.id, company_id=company_id, status="queued",
        )
        self.db.add(job)
        self.db.flush()

        self._set_status(document, DocumentStatus.QUEUED)
        self.db.commit()

        log.info(
            "upload accepted", document_id=document.id, job_id=job.id,
            company_id=company_id, size_bytes=final.size_bytes,
            filename=filename, backend=final.backend,
            version=version, superseded=superseded,
        )
        # Distinguished so the caller can tell the user whether earlier
        # citations still resolve: a new version supersedes its predecessor
        # rather than replacing it, and the old text is retained.
        return AcceptedUpload(
            document, job_id=job.id,
            action="new_version" if superseded is not None else "created",
            superseded=superseded,
        )

    def _adopt(self, document: Document, key: str, source_key: str | None) -> None:
        document.storage_key = key
        document.storage_backend = self.storage.backend
        document.storage_location = self.storage.location(key)
        self.db.commit()

    def _find_predecessor(self, company_id: str, filename: str) -> Document | None:
        return self.db.scalar(
            select(Document)
            .where(
                Document.company_id == company_id,
                Document.filename == filename,
                Document.superseded_by.is_(None),
            )
            .order_by(Document.version.desc())
        )

    # ==================================================================
    # Re-index — reuses the stored original (requirement 6)
    # ==================================================================
    def reprocess(self, document_id: int, *, force: bool = False) -> int:
        """Queue a document to be parsed again from its stored bytes.

        This is what makes the original worth keeping. Changing the chunker,
        the OCR settings or the embedding model re-runs the pipeline against
        the source rather than asking anyone to find the file again.
        """
        document = self.db.get(Document, document_id)
        if document is None:
            raise IngestionError(f"unknown document {document_id}")
        if not document.storage_key:
            raise IngestionError(
                f"document {document_id} was uploaded before source retention "
                "and has no stored original; re-upload it to enable re-indexing"
            )
        if not self.storage.exists(document.storage_key):
            raise IngestionError(
                f"stored object missing for document {document_id} "
                f"({document.storage_key})"
            )
        if document.status == DocumentStatus.PROCESSING.value and not force:
            raise IngestionError(f"document {document_id} is already processing")

        job = self.db.scalar(
            select(DocumentJob).where(DocumentJob.document_id == document_id)
        )
        if job is None:
            job = DocumentJob(
                document_id=document.id, company_id=document.company_id,
                status="queued",
            )
            self.db.add(job)
        else:
            job.status = "queued"
            job.attempts = 0
            job.error = None
            job.started_at = None
            job.finished_at = None

        document.error = None
        self._set_status(document, DocumentStatus.QUEUED)
        self._log(document, ProcessingStage.QUEUED, "re-index requested")
        self.db.flush()
        self.db.commit()
        log.info("reindex queued", document_id=document_id, job_id=job.id)
        return job.id

    # ==================================================================
    # Worker path
    # ==================================================================
    def claim_next(self, worker_id: str) -> DocumentJob | None:
        """Atomically take the oldest queued job.

        A conditional UPDATE, which is atomic on both SQLite and Postgres, so
        two workers cannot claim the same document.
        """
        from sqlalchemy import update

        candidate = self.db.scalar(
            select(DocumentJob)
            .where(DocumentJob.status == "queued")
            .order_by(DocumentJob.priority.desc(), DocumentJob.id)
            .limit(1)
        )
        if candidate is None:
            return None

        claimed = self.db.execute(
            update(DocumentJob)
            .where(DocumentJob.id == candidate.id, DocumentJob.status == "queued")
            .values(status="running", started_at=_utcnow(),
                    attempts=DocumentJob.attempts + 1)
        )
        if claimed.rowcount != 1:
            self.db.rollback()
            return None       # another worker won the race
        self.db.commit()
        self.db.refresh(candidate)
        return candidate

    def run_job(self, job: DocumentJob) -> Document:
        """Execute the full pipeline for one claimed job."""
        document = self.db.get(Document, job.document_id)
        if document is None:
            raise IngestionError(f"job {job.id} references a missing document")

        if not document.storage_key:
            self._fail(document, job, "no stored original to process")
            raise IngestionError("no stored original")

        started = time.perf_counter()
        document.attempts = job.attempts
        self._set_status(document, DocumentStatus.PROCESSING)
        self._log(document, ProcessingStage.PARSE, "reading stored source")
        self.db.commit()

        try:
            payload = self.storage.read(document.storage_key)
        except StorageError as exc:
            self._fail(document, job, f"stored object unreadable: {exc}")
            self.db.commit()
            raise

        company = self.db.get(Company, document.company_id)
        try:
            result = self._run_pipeline(document, payload, company)
        except Exception as exc:  # noqa: BLE001
            # The bytes stay exactly where they are: the document can be
            # retried or inspected, which was impossible before.
            log.exception(
                "ingestion failed", document_id=document.id, job_id=job.id,
                attempt=job.attempts, storage_key=document.storage_key,
            )
            self._fail(document, job, f"{type(exc).__name__}: {exc}")
            self.db.commit()
            raise

        from app.services.documents.service import DocumentService

        service = DocumentService(self.db)
        service._persist(document, result)          # noqa: SLF001 — same package

        elapsed = (time.perf_counter() - started) * 1000.0
        document.processing_ms = round(elapsed, 3)
        document.processed_at = _utcnow()
        self._set_status(document, DocumentStatus.COMPLETED)
        self._log(
            document, ProcessingStage.DONE,
            f"completed: {document.chunk_count} chunks, "
            f"{document.fact_count} fields, {document.entity_count} entities",
            ms=elapsed,
        )
        job.status = "succeeded"
        job.stage = ProcessingStage.DONE.value
        job.progress = 1.0
        job.finished_at = _utcnow()
        job.duration_ms = round(elapsed, 3)
        job.timings = result.timing_map()
        self.db.commit()

        # A new document has entered the corpus, so every cached retrieval
        # answer was computed against an index that is now incomplete. Without
        # this, a user who uploads an annual report and immediately asks about
        # it is told there is no evidence — for up to the TTL, and with no
        # indication why. Explicit invalidation is what lets the RAG cache have
        # a long TTL safely.
        cache.invalidate(Namespace.RAG)

        log.info(
            "ingestion complete", document_id=document.id, job_id=job.id,
            pages=document.page_count, chunks=document.chunk_count,
            facts=document.fact_count, ms=round(elapsed, 1),
        )
        return document

    def _run_pipeline(self, document: Document, payload: bytes, company: Company | None):
        """Run the pipeline, advancing status as each milestone is reached."""
        from app.services.documents.pipeline.orchestrator import IngestionPipeline

        pipeline = IngestionPipeline()
        declared = (
            DocumentType(document.doc_type)
            if document.doc_type and document.doc_type != DocumentType.OTHER.value
            else None
        )

        stage_started = time.perf_counter()

        def on_stage(stage: str, fraction: float, detail: str = "") -> None:
            """Progress callback invoked by the pipeline between stages."""
            nonlocal stage_started
            elapsed = (time.perf_counter() - stage_started) * 1000.0
            stage_started = time.perf_counter()
            try:
                mapped = _STAGE_STATUS.get(stage)
                if mapped is not None:
                    self._set_status(document, mapped)
                else:
                    document.progress = max(document.progress, fraction)
                    document.stage = stage
                self._log(document, stage, detail or stage, ms=elapsed)
                self.db.commit()
            except Exception:  # noqa: BLE001 — telemetry must never fail a run
                self.db.rollback()

        return pipeline.run(
            payload, document.filename,
            company_name=company.name if company else document.company_id,
            company_ticker=company.ticker if company else None,
            doc_type=declared,
            progress=on_stage,
        )

    # ==================================================================
    # Status, progress and the processing log
    # ==================================================================
    def _set_status(self, document: Document, status: DocumentStatus) -> None:
        document.status = status.value
        document.progress = STATUS_PROGRESS[status]
        if status is DocumentStatus.COMPLETED:
            document.stage = ProcessingStage.DONE.value
        elif status is DocumentStatus.FAILED:
            document.stage = ProcessingStage.FAILED.value

    def _log(
        self, document: Document, stage: Any, message: str, *, ms: float = 0.0,
    ) -> None:
        entries = list(document.processing_log or [])
        entries.append({
            "at": _utcnow().isoformat(),
            "stage": str(getattr(stage, "value", stage)),
            "status": document.status,
            "progress": round(document.progress, 4),
            "message": message[:500],
            "ms": round(ms, 2),
        })
        document.processing_log = entries[-MAX_LOG_ENTRIES:]

    def _fail(self, document: Document, job: DocumentJob, error: str) -> None:
        self._set_status(document, DocumentStatus.FAILED)
        document.error = error[:2000]
        self._log(document, ProcessingStage.FAILED, error)
        job.status = (
            "failed" if job.attempts >= job.max_attempts else "queued"
        )
        job.error = error[:2000]
        job.finished_at = _utcnow() if job.status == "failed" else None
        if job.status == "queued":
            # Retryable: the source is still stored, so this costs nothing.
            self._set_status(document, DocumentStatus.QUEUED)
            document.error = error[:2000]


#: Pipeline stage → coarse document status, for requirement 5's vocabulary.
_STAGE_STATUS: dict[str, DocumentStatus] = {
    ProcessingStage.OCR.value: DocumentStatus.OCR_COMPLETE,
    ProcessingStage.CHUNKING.value: DocumentStatus.CHUNKED,
    ProcessingStage.EMBEDDING.value: DocumentStatus.EMBEDDED,
}
