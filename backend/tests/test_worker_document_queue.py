"""The worker process a deployment runs must drain BOTH queues.

Regression suite for a production fault in which every document ingested by
the Blogger sync sat forever at::

    documents.status = queued      documents.stage = queued
    document_jobs.status = queued  attempts = 0  started_at = NULL

with no `BackgroundJob` row anywhere — correctly, because `DocumentJob` is its
own queue — and no process claiming it.

The cause was not a missing queue, a missing worker class or a broken Blogger
integration. `DocumentIngestionService.claim_next`/`run_job` and the
`DocumentWorker` wrapped around them all existed and worked; what was missing
is that only the *API* process started a document worker, and a production
deployment runs the API with `WORKER_ENABLED=false` and its jobs in
`python -m app.worker` — a process built around the generic `BackgroundJob`
queue alone. So the properties asserted here are about the platform `Worker`,
not about the document pipeline:

* the worker reaches the document queue and runs the existing pipeline;
* the generic queue behaves exactly as it did before;
* neither queue can starve the other;
* a claimed document cannot be taken twice;
* Blogger-synced documents, the ones that were stuck, now reach completion.

Each test gets its own database. Two of the tests deliberately do not: they
build the worker through `SessionLocal`, the way `app.worker.main` does,
because the entry point's own wiring is what failed.
"""
from __future__ import annotations

import io
import threading
import time
import uuid

import pytest
from reportlab.pdfgen import canvas
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.db.base import Base, SessionLocal
from app.domain.documents.types import DocumentStatus
from app.domain.platform.jobs import JobKind, JobStatus
from app.models.company import Company
from app.models.document import Document, DocumentChunk, DocumentJob
from app.models.platform import BackgroundJob
from app.services.blogger.sync import BloggerSyncService
from app.services.documents.ingestion import DocumentIngestionService
from app.services.documents.storage import LocalFileStorage
from app.services.platform.jobs import handlers
from app.services.platform.jobs.queue import JobQueue
from app.services.platform.jobs.worker import Worker
from tests.fixtures.blogger_feed import (
    FEED_URL, FakeBloggerServer, client_for, one_post,
)


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


def _report(index: int = 0) -> bytes:
    """A parseable report whose bytes differ per index.

    The bytes must differ: `accept()` deduplicates on content hash, so three
    copies of one PDF would produce one document and two `duplicate` results,
    and a test of queue fairness would be measuring nothing.
    """
    return _pdf([
        f"Acme Industries Limited — report {index}",
        "Integrated Annual Report FY2026",
        f"Consolidated revenue for FY2026 stood at {84200 + index} crore rupees.",
        f"Headcount at 31 March 2026 was {41300 + index} employees.",
        "Client concentration in the retail segment remains a principal risk.",
    ])


def _explode(*args, **kwargs):
    raise RuntimeError("simulated parser crash")


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------
@pytest.fixture()
def db_factory():
    """An empty database of the test's own.

    Empty matters. These tests assert on queue *emptiness* — that a pass found
    nothing, that a second worker found no job left — and a suite-wide shared
    database cannot promise that, because any other module may have left a job
    queued behind it.
    """
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        yield factory
    finally:
        engine.dispose()


@pytest.fixture()
def db(db_factory):
    session = db_factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def session_factory(db):
    """A factory returning the same session — the worker takes a factory."""
    return lambda: db


@pytest.fixture()
def storage(tmp_path) -> LocalFileStorage:
    return LocalFileStorage(tmp_path / "documents")


@pytest.fixture()
def company(db) -> Company:
    row = Company(
        id=str(uuid.uuid4()), ticker="ACME", name="Acme Industries Ltd",
        exchange="NSE", listing_status="active",
    )
    db.add(row)
    db.commit()
    return row


@pytest.fixture(autouse=True)
def configured_storage(storage, monkeypatch):
    """Point the configured backend at the temporary volume.

    The worker built the way production builds it passes no storage, so
    ingestion resolves one through `get_storage()`. Patching that, rather than
    always injecting, keeps these tests on the production code path.
    """
    monkeypatch.setattr(
        "app.services.documents.ingestion.get_storage", lambda: storage,
    )


def _accept(db, storage, company, index: int = 0, payload: bytes | None = None):
    """An upload accepted by the existing request path.

    `payload` is for the tests that need the exact bytes back afterwards: a
    freshly rendered PDF is not byte-identical to an earlier one, because
    ReportLab stamps a creation date into it.
    """
    return DocumentIngestionService(db, storage=storage).accept(
        company.id,
        payload if payload is not None else _report(index),
        f"annual-{index}.pdf",
    )


def _queued_generic_jobs(db) -> int:
    return db.scalar(
        select(func.count())
        .select_from(BackgroundJob)
        .where(
            BackgroundJob.kind == JobKind.EMBEDDING.value,
            BackgroundJob.status == JobStatus.QUEUED.value,
        )
    )


def _job(db, job_id: int) -> DocumentJob:
    db.expire_all()
    return db.get(DocumentJob, job_id)


# ---------------------------------------------------------------------------
# 1–4. The worker reaches the document queue and finishes the work
# ---------------------------------------------------------------------------
class TestTheWorkerProcessesDocumentJobs:
    def test_a_queued_document_job_is_processed_by_the_worker(
        self, db, session_factory, storage, company,
    ):
        """The fault in one line: this call used to find nothing, forever."""
        accepted = _accept(db, storage, company)
        worker = Worker(session_factory, worker_id="prod", document_storage=storage)

        assert worker.run_pass() is True

        assert _job(db, accepted.job_id).status == "succeeded"

    def test_a_claimed_job_leaves_the_queued_state(
        self, db, storage, company,
    ):
        """`queued` → `running` on claim, with the attempt and start recorded.

        This is the transition production never made: `started_at` stayed NULL
        and `attempts` stayed 0 because no worker ever called `claim_next`.
        """
        accepted = _accept(db, storage, company)

        job = DocumentIngestionService(db, storage=storage).claim_next("under-test")

        assert job is not None
        assert job.id == accepted.job_id
        assert job.status == "running"
        assert job.attempts == 1
        assert job.started_at is not None

    def test_the_existing_pipeline_runs_and_produces_chunks(
        self, db, session_factory, storage, company,
    ):
        """The worker must run the real pipeline, not a stand-in for it."""
        accepted = _accept(db, storage, company)
        Worker(session_factory, worker_id="prod", document_storage=storage).run_pass()

        db.expire_all()
        document = db.get(Document, accepted.document.id)
        assert document.chunk_count > 0, "the whole point: chunks must exist"
        chunks = db.scalars(
            select(DocumentChunk).where(DocumentChunk.document_id == document.id)
        ).all()
        assert len(chunks) == document.chunk_count
        assert any("Acme Industries" in chunk.text for chunk in chunks)

    def test_a_successful_document_reaches_the_expected_final_status(
        self, db, session_factory, storage, company,
    ):
        accepted = _accept(db, storage, company)
        worker = Worker(session_factory, worker_id="prod", document_storage=storage)
        worker.run_pass()

        db.expire_all()
        document = db.get(Document, accepted.document.id)
        job = db.get(DocumentJob, accepted.job_id)

        assert document.status == DocumentStatus.COMPLETED.value
        assert document.stage == "done"
        assert document.progress == 1.0
        assert document.error is None
        assert document.processed_at is not None
        assert job.status == "succeeded"
        assert job.finished_at is not None
        # The source is retained, so the document stays re-indexable.
        assert storage.exists(document.storage_key)

    def test_the_document_path_can_be_switched_off(
        self, db, session_factory, storage, company,
    ):
        """For a deployment that runs the standalone document worker instead."""
        accepted = _accept(db, storage, company)
        worker = Worker(
            session_factory, worker_id="prod",
            documents_enabled=False, document_storage=storage,
        )

        assert worker.run_document_once() is False
        assert _job(db, accepted.job_id).status == "queued"

    def test_the_entry_point_builds_a_worker_with_documents_enabled(self):
        """`python -m app.worker` constructs `Worker(SessionLocal)` with no
        keyword arguments, so the default *is* the production configuration.
        This is the guard on the default that caused the outage.
        """
        assert Worker(SessionLocal).documents_enabled is True


# ---------------------------------------------------------------------------
# 5. Failure and retry semantics stay the ingestion service's
# ---------------------------------------------------------------------------
class TestFailureAndRetryAreUnchanged:
    def test_a_failure_is_retryable_until_max_attempts(
        self, db, session_factory, storage, company, monkeypatch,
    ):
        """The worker must not add, replace or shorten the retry policy."""
        monkeypatch.setattr(
            "app.services.documents.pipeline.orchestrator.IngestionPipeline.run",
            _explode,
        )
        payload = _report(0)
        accepted = _accept(db, storage, company, payload=payload)
        worker = Worker(session_factory, worker_id="prod", document_storage=storage)

        assert worker.run_pass() is True

        job = _job(db, accepted.job_id)
        document = db.get(Document, accepted.document.id)
        # Attempt 1 of 3: requeued, not failed, and the source is intact.
        assert job.status == "queued"
        assert job.attempts == 1
        assert job.finished_at is None
        assert "simulated parser crash" in (job.error or "")
        assert document.status == DocumentStatus.QUEUED.value
        assert storage.exists(document.storage_key)
        assert storage.read(document.storage_key) == payload

        worker.run_pass()
        job = _job(db, accepted.job_id)
        assert (job.status, job.attempts) == ("queued", 2)

        # Attempt 3 of 3: terminal.
        worker.run_pass()
        job = _job(db, accepted.job_id)
        assert job.status == "failed"
        assert job.attempts == job.max_attempts == 3
        assert job.finished_at is not None
        assert db.get(Document, accepted.document.id).status == (
            DocumentStatus.FAILED.value
        )

    def test_a_failed_document_does_not_stop_the_worker(
        self, db, session_factory, storage, company, monkeypatch,
    ):
        monkeypatch.setattr(
            "app.services.documents.pipeline.orchestrator.IngestionPipeline.run",
            _explode,
        )
        _accept(db, storage, company, index=1)
        worker = Worker(session_factory, worker_id="prod", document_storage=storage)

        worker.run_pass()
        assert worker.document_queue.failed == 1
        # ...and it is still able to take the next unit of work.
        assert worker.run_pass() is True

    def test_a_retry_after_a_failure_completes(
        self, db, session_factory, storage, company, monkeypatch,
    ):
        """The stored source is what makes the retry worth anything."""
        from app.services.documents.pipeline.orchestrator import IngestionPipeline

        original = IngestionPipeline.run
        monkeypatch.setattr(IngestionPipeline, "run", _explode)
        accepted = _accept(db, storage, company, index=2)
        worker = Worker(session_factory, worker_id="prod", document_storage=storage)
        worker.run_pass()
        monkeypatch.setattr(IngestionPipeline, "run", original)

        assert worker.run_pass() is True

        db.expire_all()
        document = db.get(Document, accepted.document.id)
        assert document.status == DocumentStatus.COMPLETED.value
        assert document.chunk_count > 0
        assert db.get(DocumentJob, accepted.job_id).status == "succeeded"


# ---------------------------------------------------------------------------
# 6. The generic queue is untouched
# ---------------------------------------------------------------------------
class TestTheGenericQueueIsUnchanged:
    def test_run_once_still_claims_only_generic_jobs(
        self, db, session_factory, storage, company,
    ):
        """`run_once` keeps its contract, so every existing caller and test of
        it is unaffected — a queued document must not make it return True.
        """
        accepted = _accept(db, storage, company)
        worker = Worker(session_factory, worker_id="prod", document_storage=storage)

        assert worker.run_once() is False
        assert worker.processed == 0

        job = _job(db, accepted.job_id)
        assert job.status == "queued"
        assert job.attempts == 0

    def test_a_generic_job_still_runs_to_success(
        self, db, session_factory, storage, monkeypatch,
    ):
        ran = []
        monkeypatch.setitem(
            handlers.HANDLERS, JobKind.EMBEDDING,
            lambda _db, payload: ran.append(payload) or {"ran": payload},
        )
        queue = JobQueue(db)
        job = queue.enqueue(JobKind.EMBEDDING, payload={"company_id": "x"})
        worker = Worker(session_factory, worker_id="prod", document_storage=storage)

        assert worker.run_once() is True
        assert queue.get(job.id).status == JobStatus.SUCCEEDED.value
        assert worker.processed == 1
        assert ran == [{"company_id": "x"}]

    def test_a_handler_exception_still_fails_the_job_not_the_worker(
        self, db, session_factory, storage, monkeypatch,
    ):
        monkeypatch.setitem(handlers.HANDLERS, JobKind.EMBEDDING, _explode)
        queue = JobQueue(db)
        job = queue.enqueue(JobKind.EMBEDDING, payload={"x": 1})
        worker = Worker(session_factory, worker_id="prod", document_storage=storage)

        assert worker.run_pass() is True

        assert worker.failed == 1
        row = queue.get(job.id)
        assert row.status in (JobStatus.FAILED.value, JobStatus.DEAD_LETTER.value)
        assert "simulated parser crash" in row.error


# ---------------------------------------------------------------------------
# 7. Two workers cannot take the same document
# ---------------------------------------------------------------------------
class TestTheClaimIsStillAtomic:
    def test_only_one_worker_processes_a_document(
        self, db, session_factory, storage, company,
    ):
        accepted = _accept(db, storage, company)
        first = Worker(session_factory, worker_id="w1", document_storage=storage)
        second = Worker(session_factory, worker_id="w2", document_storage=storage)

        assert first.run_document_once() is True
        assert second.run_document_once() is False

        job = _job(db, accepted.job_id)
        assert job.status == "succeeded"
        # One attempt, one pipeline run: the second worker never touched it.
        assert job.attempts == 1
        document = db.get(Document, accepted.document.id)
        assert [e["stage"] for e in document.processing_log].count("done") == 1

    def test_a_job_already_running_is_not_claimed_again(
        self, db, storage, company,
    ):
        """The other half of the race: a worker that finds the row already
        claimed must walk away rather than re-run the pipeline.
        """
        accepted = _accept(db, storage, company)
        winner = DocumentIngestionService(db, storage=storage)
        loser = DocumentIngestionService(db, storage=storage)

        assert winner.claim_next("w1") is not None
        assert loser.claim_next("w2") is None

        assert _job(db, accepted.job_id).attempts == 1


# ---------------------------------------------------------------------------
# 8. Fairness: neither queue starves the other
# ---------------------------------------------------------------------------
class TestNeitherQueueStarvesTheOther:
    def test_a_deep_generic_queue_cannot_push_documents_behind_it(
        self, db, session_factory, storage, company, monkeypatch,
    ):
        """Twelve generic jobs and three documents: the generic drain is
        capped, so the very first pass must already have served a document.
        """
        monkeypatch.setitem(
            handlers.HANDLERS, JobKind.EMBEDDING, lambda _db, _payload: {"ok": True},
        )
        queue = JobQueue(db)
        for i in range(12):
            queue.enqueue(JobKind.EMBEDDING, payload={"i": i})
        for i in range(3):
            _accept(db, storage, company, index=i)

        worker = Worker(session_factory, worker_id="fair", document_storage=storage)
        assert worker.run_pass() is True

        assert worker.processed == worker.BACKGROUND_JOBS_PER_PASS
        assert worker.document_queue.processed == 1, (
            "a document must be served in the same pass as the generic jobs"
        )
        assert _queued_generic_jobs(db) == 2

    def test_a_deep_document_queue_cannot_push_generic_jobs_behind_it(
        self, db, session_factory, storage, company, monkeypatch,
    ):
        """The mirror image: three documents and one generic job, which must
        finish in the first pass rather than after the documents.
        """
        ran = []
        monkeypatch.setitem(
            handlers.HANDLERS, JobKind.EMBEDDING,
            lambda _db, payload: ran.append(payload["i"]) or {"ok": True},
        )
        for i in range(3):
            _accept(db, storage, company, index=i)
        JobQueue(db).enqueue(JobKind.EMBEDDING, payload={"i": 99})

        worker = Worker(session_factory, worker_id="fair", document_storage=storage)
        assert worker.run_pass() is True

        assert ran == [99]
        assert worker.document_queue.processed == 1

    def test_both_queues_drain_completely(
        self, db, session_factory, storage, company, monkeypatch,
    ):
        monkeypatch.setitem(
            handlers.HANDLERS, JobKind.EMBEDDING, lambda _db, _payload: {"ok": True},
        )
        queue = JobQueue(db)
        for i in range(12):
            queue.enqueue(JobKind.EMBEDDING, payload={"i": i})
        for i in range(3):
            _accept(db, storage, company, index=i)

        worker = Worker(session_factory, worker_id="fair", document_storage=storage)
        passes = 0
        while passes < 20 and worker.run_pass():
            passes += 1

        assert worker.processed == 12
        assert worker.document_queue.processed == 3
        assert _queued_generic_jobs(db) == 0
        assert db.scalar(
            select(func.count()).select_from(DocumentJob)
            .where(DocumentJob.status == "queued")
        ) == 0
        assert passes <= 4, "neither queue should wait for the other to empty"


# ---------------------------------------------------------------------------
# 9–10. The Blogger path, end to end, through the worker production runs
# ---------------------------------------------------------------------------
@pytest.fixture()
def blogger_settings(monkeypatch):
    monkeypatch.setattr(settings, "BLOGGER_ENABLED", True)
    monkeypatch.setattr(settings, "BLOGGER_FEED_URL", FEED_URL)
    monkeypatch.setattr(settings, "BLOGGER_MAX_POSTS", 100)
    monkeypatch.setattr(settings, "BLOGGER_DEFAULT_TICKER", "")
    monkeypatch.setattr(settings, "BLOGGER_SYNC_TIMEOUT_SECONDS", 5.0)


@pytest.fixture()
def shared_db():
    """The suite's seeded database, for the tests that must go through
    `SessionLocal` — the factory `app.worker.main` is handed.
    """
    from tests.conftest import TestingSession

    session = TestingSession()
    _purge(session)
    try:
        yield session
    finally:
        _purge(session)
        session.close()


def _purge(session) -> None:
    """Remove every document, chunk and job.

    The suite shares one seeded database and `accept()` commits, so a rollback
    cannot isolate these tests. Without the purge a later test's worker claims
    an earlier test's job, whose bytes lived in a tmp_path that is now gone.
    """
    session.rollback()
    session.query(DocumentChunk).delete()
    session.query(DocumentJob).delete()
    session.query(Document).delete()
    session.commit()


@pytest.fixture()
def shriramfin(shared_db) -> Company:
    company = Company(
        id=str(uuid.uuid4()), ticker="SHRIRAMFIN", name="Shriram Finance Ltd",
        exchange="NSE", listing_status="active",
    )
    shared_db.add(company)
    shared_db.commit()
    yield company
    shared_db.query(Document).filter(
        Document.company_id == company.id
    ).delete(synchronize_session=False)
    shared_db.query(Company).filter(
        Company.id == company.id
    ).delete(synchronize_session=False)
    shared_db.commit()


@pytest.fixture()
def blogger_sync(shared_db, storage, blogger_settings) -> BloggerSyncService:
    """The real sync service against the fixture feed and the temp volume."""
    server = FakeBloggerServer(posts=(one_post(),))
    return BloggerSyncService(
        shared_db, feed=client_for(server), storage=storage,
    )


def _blogger_document(db, company) -> tuple[Document, DocumentJob]:
    document = db.scalars(
        select(Document).where(Document.company_id == company.id)
    ).one()
    job = db.scalar(
        select(DocumentJob).where(DocumentJob.document_id == document.id)
    )
    return document, job


class TestTheBloggerPathReachesAProcessedDocument:
    def test_the_sync_creates_a_document_job_and_no_background_job(
        self, blogger_sync, shared_db, shriramfin,
    ):
        """The production shape, asserted rather than assumed: a queued
        document, a queued job, and deliberately no `BackgroundJob` row,
        because `DocumentJob` is its own queue.

        Counted from the high-water mark rather than absolutely: the suite
        shares a database, and other modules leave document-referencing jobs
        behind that this sync had nothing to do with.
        """
        high_water = shared_db.scalar(select(func.max(BackgroundJob.id))) or 0

        blogger_sync.sync()

        document, job = _blogger_document(shared_db, shriramfin)
        assert job is not None
        assert job.status == "queued"
        assert job.attempts == 0
        assert job.started_at is None
        # Nothing was enqueued on the generic queue for this document — or at
        # all. Ingestion is `DocumentJob`'s job, which is why a worker that
        # only drained `BackgroundJob` left it stuck.
        assert shared_db.scalar(
            select(func.count()).select_from(BackgroundJob)
            .where(BackgroundJob.id > high_water)
        ) == 0

    def test_the_worker_processes_what_the_sync_queued(
        self, blogger_sync, shared_db, storage, shriramfin,
    ):
        """Requirement 9: the DocumentJob a Blogger sync creates is one the
        production worker can claim and complete.
        """
        blogger_sync.sync()
        document, _ = _blogger_document(shared_db, shriramfin)

        worker = Worker(
            lambda: shared_db, worker_id="prod", document_storage=storage,
        )
        assert worker.run_document_once() is True

        shared_db.expire_all()
        document = shared_db.get(Document, document.id)
        assert document.status == DocumentStatus.COMPLETED.value
        assert document.chunk_count > 0
        chunks = shared_db.scalars(
            select(DocumentChunk).where(DocumentChunk.document_id == document.id)
        ).all()
        assert any("Shriram Finance" in chunk.text for chunk in chunks)

    def test_a_queued_blogger_document_does_not_stay_stuck(
        self, blogger_sync, shared_db, storage, shriramfin,
    ):
        """Requirement 10: the outage, reproduced and then cleared.

        The worker is built through `SessionLocal` with no keyword arguments,
        exactly as `app.worker.main` builds it, so this test fails if the entry
        point's worker ever stops reaching the document queue again.
        """
        blogger_sync.sync()
        document, job = _blogger_document(shared_db, shriramfin)
        job_id = job.id

        # The state production was found in.
        assert document.status == DocumentStatus.QUEUED.value
        assert document.stage == "queued"
        assert (job.status, job.attempts, job.started_at) == ("queued", 0, None)

        worker = Worker(SessionLocal, worker_id="python -m app.worker",
                        document_storage=storage)
        passes = 0
        while passes < 5 and worker.run_pass():
            passes += 1

        shared_db.expire_all()
        document = shared_db.get(Document, document.id)
        job = shared_db.get(DocumentJob, job_id)
        assert document.status == DocumentStatus.COMPLETED.value
        assert document.stage == "done"
        assert document.chunk_count > 0
        assert job.status == "succeeded"
        assert job.attempts == 1
        assert job.started_at is not None
        assert job.finished_at is not None


# ---------------------------------------------------------------------------
# Loop behaviour: shutdown and idle backoff
# ---------------------------------------------------------------------------
class TestTheLoop:
    def test_run_pass_is_false_when_both_queues_are_empty(
        self, db_factory, storage,
    ):
        worker = Worker(db_factory, worker_id="idle", document_storage=storage)
        assert worker.run_pass() is False

    def test_a_stopped_worker_takes_no_work(
        self, db, session_factory, storage, company,
    ):
        accepted = _accept(db, storage, company)
        worker = Worker(session_factory, worker_id="idle", document_storage=storage)
        worker.stop()

        assert worker.run_pass() is False
        assert _job(db, accepted.job_id).status == "queued"

    def test_run_forever_stops_when_signalled(
        self, db, db_factory, monkeypatch,
    ):
        """Graceful shutdown: the loop leaves after the pass it is in."""
        worker = Worker(
            db_factory, worker_id="loop", poll_seconds=0.01,
        )
        monkeypatch.setitem(
            handlers.HANDLERS, JobKind.EMBEDDING,
            lambda _db, _payload: worker.stop() or {"ok": True},
        )
        JobQueue(db).enqueue(JobKind.EMBEDDING, payload={})

        thread = threading.Thread(target=worker.run_forever, daemon=True)
        thread.start()
        thread.join(timeout=20)

        assert not thread.is_alive(), "the loop must honour stop()"
        assert worker.processed == 1

    def test_an_idle_worker_backs_off_and_stays_capped(
        self, db_factory, storage, monkeypatch,
    ):
        """The existing polling behaviour, kept: the wait grows while idle and
        stops growing at five polls.
        """
        worker = Worker(
            db_factory, worker_id="idle", poll_seconds=0.01,
            document_storage=storage,
        )
        waits: list[float] = []
        real_wait = worker._stop.wait
        monkeypatch.setattr(
            worker._stop, "wait",
            lambda seconds: waits.append(seconds) or real_wait(seconds),
        )

        thread = threading.Thread(target=worker.run_forever, daemon=True)
        thread.start()
        deadline = time.time() + 10
        while time.time() < deadline and len(waits) < 5:
            time.sleep(0.01)
        worker.stop()
        thread.join(timeout=20)

        assert not thread.is_alive()
        assert waits, "an empty pair of queues must poll rather than spin"
        assert waits == sorted(waits), "the backoff must grow while idle"
        assert max(waits) == pytest.approx(worker.poll_seconds * 5)
