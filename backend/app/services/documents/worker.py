"""Background worker for document ingestion.

Runs the pipeline off the request path. Deliberately a separate loop from the
platform's generic `Worker`: document jobs are long (minutes for a 1000-page
scanned report) and have their own claim table, so mixing them with
second-scale jobs like notifications would let one report block the queue.

It can run in-process alongside the API (`WORKER_ENABLED=true`) or as its own
Railway service (`python -m app.services.documents.worker`). The claim is a
conditional UPDATE, so running several is safe.
"""
from __future__ import annotations

import os
import signal
import threading
import time

import structlog

from app.services.documents.ingestion import DocumentIngestionService

log = structlog.get_logger(__name__)


class DocumentWorker:
    """Polls for queued documents and ingests them one at a time."""

    def __init__(
        self,
        session_factory,
        *,
        worker_id: str | None = None,
        poll_seconds: float = 2.0,
        storage=None,
    ) -> None:
        self.session_factory = session_factory
        self.worker_id = worker_id or f"docworker-{os.getpid()}"
        self.poll_seconds = poll_seconds
        #: Injectable so a test can point the worker at a tmp_path instead of
        #: the configured volume. None means "use the configured backend".
        self.storage = storage
        self._stop = threading.Event()
        self.processed = 0
        self.failed = 0

    def stop(self) -> None:
        self._stop.set()

    def run_once(self) -> bool:
        """Claim and ingest at most one document. True if one was handled.

        Returns rather than looping so tests can drive it deterministically.
        """
        db = self.session_factory()
        try:
            service = DocumentIngestionService(db, storage=self.storage)
            job = service.claim_next(self.worker_id)
            if job is None:
                return False

            started = time.perf_counter()
            log.info(
                "document job claimed", job_id=job.id,
                document_id=job.document_id, worker=self.worker_id,
                attempt=job.attempts,
            )
            try:
                service.run_job(job)
                self.processed += 1
            except Exception as exc:  # noqa: BLE001
                # run_job has already recorded the failure and preserved the
                # stored source; the worker must survive to take the next job.
                self.failed += 1
                log.error(
                    "document job failed", job_id=job.id,
                    document_id=job.document_id, error=str(exc)[:300],
                    ms=round((time.perf_counter() - started) * 1000, 1),
                )
            return True
        finally:
            db.close()

    def run_forever(self) -> None:
        log.info("document worker started", worker=self.worker_id)
        while not self._stop.is_set():
            try:
                if not self.run_once():
                    self._stop.wait(self.poll_seconds)
            except Exception:  # noqa: BLE001
                log.exception("document worker loop error")
                self._stop.wait(self.poll_seconds)
        log.info(
            "document worker stopped", worker=self.worker_id,
            processed=self.processed, failed=self.failed,
        )


_THREAD: threading.Thread | None = None
_WORKER: DocumentWorker | None = None


def start_in_process(session_factory) -> None:
    """Start the worker in a daemon thread beside the API."""
    global _THREAD, _WORKER
    if _THREAD is not None and _THREAD.is_alive():
        return
    _WORKER = DocumentWorker(session_factory)
    _THREAD = threading.Thread(
        target=_WORKER.run_forever, name="document-worker", daemon=True,
    )
    _THREAD.start()


def stop_in_process(timeout: float = 5.0) -> None:
    global _THREAD, _WORKER
    if _WORKER is not None:
        _WORKER.stop()
    if _THREAD is not None:
        _THREAD.join(timeout=timeout)
    _THREAD, _WORKER = None, None


def main() -> None:  # pragma: no cover - process entry point
    from app.db.base import SessionLocal

    worker = DocumentWorker(SessionLocal)

    def _shutdown(signum, _frame):
        log.info("shutdown signal", signal=signum)
        worker.stop()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    worker.run_forever()


if __name__ == "__main__":  # pragma: no cover
    main()
