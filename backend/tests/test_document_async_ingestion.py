"""Asynchronous document ingestion — storage, worker, status, re-index.

Covers the redesign that replaced the synchronous upload path. The properties
under test are the ones whose absence caused the production failures:

* the original bytes survive the request, a failure, and a restart
* the request returns immediately, having parsed nothing
* the worker does the work and moves the document through the statuses
* re-index re-reads the stored source rather than asking for a re-upload
"""
from __future__ import annotations

import io

import pytest
from reportlab.pdfgen import canvas

from app.domain.documents.types import DocumentStatus
from app.models.document import Document, DocumentJob
from app.services.documents.ingestion import (
    DocumentIngestionService, IngestionError,
)
from app.services.documents.storage import (
    DocumentStorage, LocalFileStorage, StorageError,
)
from app.services.documents.worker import DocumentWorker


def _pdf(lines: list[str]) -> bytes:
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer)
    text = pdf.beginText(60, 780)
    for line in lines:
        text.textLine(line)
    pdf.drawText(text)
    pdf.showPage()
    pdf.save()
    return buffer.getvalue()


REPORT = _pdf([
    "Acme Industries Limited",
    "Integrated Annual Report FY2026",
    "Consolidated revenue for FY2026 stood at 84,200 crore rupees.",
    "Headcount at 31 March 2026 was 41,300 employees.",
    "Client concentration in the retail segment remains a principal risk.",
])


@pytest.fixture()
def storage(tmp_path) -> LocalFileStorage:
    return LocalFileStorage(tmp_path / "documents")


@pytest.fixture()
def db_session():
    """A session on the suite's shared seeded database.

    `accept()` commits — it must, since the worker runs in another
    transaction — so a rollback is not enough to isolate these tests. Each
    one starts and ends by deleting every document and job, otherwise a
    worker in a later test claims a stale job whose bytes were written into
    an earlier test's tmp_path and now do not exist. That is exactly the
    "object not found" the first run produced: a harness fault, not a
    product one.
    """
    from tests.conftest import TestingSession

    session = TestingSession()

    def purge() -> None:
        session.rollback()
        session.query(DocumentJob).delete()
        session.query(Document).delete()
        session.commit()

    purge()
    try:
        yield session
    finally:
        purge()
        session.close()


@pytest.fixture()
def company_id(db_session) -> str:
    from sqlalchemy import select

    from app.models.company import Company

    return db_session.scalars(select(Company).limit(1)).first().id


class TestStorage:
    def test_put_and_read_round_trip(self, storage):
        stored = storage.put("documents/x/abc.pdf", REPORT)
        assert stored.size_bytes == len(REPORT)
        assert storage.read("documents/x/abc.pdf") == REPORT
        assert storage.exists("documents/x/abc.pdf")

    def test_content_hash_is_reported(self, storage):
        import hashlib

        stored = storage.put("documents/x/abc.pdf", REPORT)
        assert stored.content_hash == hashlib.sha256(REPORT).hexdigest()

    def test_streams_are_accepted_without_loading_into_memory(self, storage):
        stored = storage.put("documents/x/s.pdf", io.BytesIO(REPORT))
        assert stored.size_bytes == len(REPORT)
        assert storage.read("documents/x/s.pdf") == REPORT

    def test_key_traversal_is_refused(self, storage):
        with pytest.raises(StorageError):
            storage.put("../../etc/passwd", b"x")

    def test_keys_are_content_addressed(self):
        key = DocumentStorage.build_key("c1", "d" * 64, "Annual Report.PDF")
        assert key == f"documents/c1/{'d' * 64}.pdf"

    def test_missing_object_raises(self, storage):
        with pytest.raises(StorageError):
            storage.read("documents/x/nope.pdf")


class TestAcceptIsFastAndDurable:
    def test_upload_stores_bytes_and_queues_without_parsing(
        self, db_session, storage, company_id,
    ):
        service = DocumentIngestionService(db_session, storage=storage)
        accepted = service.accept(company_id, REPORT, "annual.pdf")

        document = accepted.document
        # Queued, not processed: nothing has been parsed yet.
        assert document.status == DocumentStatus.QUEUED.value
        assert document.chunk_count == 0
        assert document.page_count == 0
        assert accepted.job_id is not None

        # ...but the bytes are already safe.
        assert document.storage_key
        assert storage.exists(document.storage_key)
        assert storage.read(document.storage_key) == REPORT

    def test_empty_upload_is_refused_and_leaves_nothing_behind(
        self, db_session, storage, company_id,
    ):
        service = DocumentIngestionService(db_session, storage=storage)
        with pytest.raises(IngestionError, match="empty"):
            service.accept(company_id, b"", "empty.pdf")
        assert not list(storage.root.rglob("*.pdf"))

    def test_duplicate_upload_returns_the_existing_document(
        self, db_session, storage, company_id,
    ):
        service = DocumentIngestionService(db_session, storage=storage)
        first = service.accept(company_id, REPORT, "annual.pdf")
        second = service.accept(company_id, REPORT, "annual.pdf")
        assert second.action == "duplicate"
        assert second.duplicate_of == first.document.id
        assert second.job_id is None


class TestWorkerCompletesTheWork:
    def test_worker_ingests_a_queued_document_end_to_end(
        self, db_session, storage, company_id, monkeypatch,
    ):
        monkeypatch.setattr(
            "app.services.documents.ingestion.get_storage", lambda: storage,
        )
        service = DocumentIngestionService(db_session, storage=storage)
        accepted = service.accept(company_id, REPORT, "annual.pdf")
        document_id = accepted.document.id

        worker = DocumentWorker(lambda: db_session, storage=storage)
        assert worker.run_once() is True

        document = db_session.get(Document, document_id)
        assert document.status == DocumentStatus.COMPLETED.value
        assert document.progress == 1.0
        assert document.page_count > 0
        assert document.chunk_count > 0, "the whole point: chunks must exist"
        assert document.error is None

        # The source is still there afterwards, for re-indexing.
        assert storage.exists(document.storage_key)

    def test_processing_log_records_every_stage(
        self, db_session, storage, company_id, monkeypatch,
    ):
        monkeypatch.setattr(
            "app.services.documents.ingestion.get_storage", lambda: storage,
        )
        service = DocumentIngestionService(db_session, storage=storage)
        accepted = service.accept(company_id, REPORT, "logged.pdf")
        DocumentWorker(lambda: db_session, storage=storage).run_once()

        document = db_session.get(Document, accepted.document.id)
        stages = [entry["stage"] for entry in document.processing_log]
        assert "queued" in stages
        assert "done" in stages
        assert any(e["progress"] > 0 for e in document.processing_log)

    def test_worker_returns_false_when_the_queue_is_empty(
        self, db_session, storage,
    ):
        db_session.query(DocumentJob).delete()
        db_session.commit()
        assert DocumentWorker(lambda: db_session, storage=storage).run_once() is False


class TestFailureKeepsTheSource:
    def test_a_failed_run_leaves_the_original_stored(
        self, db_session, storage, company_id, monkeypatch,
    ):
        """Requirement 7.

        Before the redesign a failure lost the document outright: the bytes
        only ever existed inside the request that failed.
        """
        monkeypatch.setattr(
            "app.services.documents.ingestion.get_storage", lambda: storage,
        )
        service = DocumentIngestionService(db_session, storage=storage)
        accepted = service.accept(company_id, REPORT, "boom.pdf")
        key = accepted.document.storage_key

        def explode(*args, **kwargs):
            raise RuntimeError("simulated parser crash")

        monkeypatch.setattr(
            "app.services.documents.pipeline.orchestrator.IngestionPipeline.run",
            explode,
        )
        worker = DocumentWorker(lambda: db_session, storage=storage)
        worker.run_once()

        document = db_session.get(Document, accepted.document.id)
        assert "simulated parser crash" in (document.error or "")
        # The document survives and can be retried.
        assert storage.exists(key)
        assert storage.read(key) == REPORT


class TestReindexUsesStoredSource:
    def test_reprocess_requeues_without_a_new_upload(
        self, db_session, storage, company_id, monkeypatch,
    ):
        """Requirement 6."""
        monkeypatch.setattr(
            "app.services.documents.ingestion.get_storage", lambda: storage,
        )
        service = DocumentIngestionService(db_session, storage=storage)
        accepted = service.accept(company_id, REPORT, "reindex.pdf")
        DocumentWorker(lambda: db_session, storage=storage).run_once()

        document_id = accepted.document.id
        first_chunks = db_session.get(Document, document_id).chunk_count
        assert first_chunks > 0

        # No bytes supplied — it must find them itself.
        job_id = service.reprocess(document_id)
        assert job_id
        assert db_session.get(Document, document_id).status == (
            DocumentStatus.QUEUED.value
        )

        DocumentWorker(lambda: db_session, storage=storage).run_once()
        document = db_session.get(Document, document_id)
        assert document.status == DocumentStatus.COMPLETED.value
        assert document.chunk_count == first_chunks

    def test_reprocess_refuses_a_document_with_no_stored_source(
        self, db_session, storage, company_id, monkeypatch,
    ):
        """Rows predating retention say so rather than failing obscurely."""
        monkeypatch.setattr(
            "app.services.documents.ingestion.get_storage", lambda: storage,
        )
        service = DocumentIngestionService(db_session, storage=storage)
        accepted = service.accept(company_id, REPORT, "legacy.pdf")
        document = db_session.get(Document, accepted.document.id)
        document.storage_key = None
        db_session.commit()

        with pytest.raises(IngestionError, match="no stored original"):
            service.reprocess(document.id)


class TestStatusVocabulary:
    def test_every_required_status_exists(self):
        """Requirement 5."""
        for name in ("UPLOADED", "QUEUED", "PROCESSING", "OCR_COMPLETE",
                     "CHUNKED", "EMBEDDED", "COMPLETED", "FAILED"):
            assert hasattr(DocumentStatus, name)

    def test_progress_is_monotonic_through_the_lifecycle(self):
        from app.domain.documents.types import STATUS_PROGRESS

        order = [
            DocumentStatus.UPLOADED, DocumentStatus.QUEUED,
            DocumentStatus.PROCESSING, DocumentStatus.OCR_COMPLETE,
            DocumentStatus.CHUNKED, DocumentStatus.EMBEDDED,
            DocumentStatus.COMPLETED,
        ]
        values = [STATUS_PROGRESS[s] for s in order]
        assert values == sorted(values)
        assert values[0] == 0.0 and values[-1] == 1.0

    def test_only_completed_counts_as_indexed(self):
        assert DocumentStatus.COMPLETED.is_indexed
        assert not DocumentStatus.EMBEDDED.is_indexed
        assert not DocumentStatus.FAILED.is_indexed
        assert DocumentStatus.FAILED.is_terminal
