"""The worker and the scheduler.

Both are deliberately simple loops over the queue rather than a framework.
The platform must run as a single container on Railway with no broker, and it
must also scale out to several replicas — both work here, because the queue's
claim is atomic and the scheduler's due-check is a conditional update.

Deployment shapes:

* `WORKER_ENABLED=true` — a background thread inside the API process. One
  container, no extra cost, adequate for a single instance.
* `python -m app.worker` — a separate process. What a production deployment
  runs, so a heavy report render cannot starve the API's event loop.

Neither is required for the product to function: every operation the queue
performs is also available synchronously. The queue makes slow work
asynchronous; it is not a hidden dependency of correctness.
"""
from __future__ import annotations

import signal
import threading
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.domain.platform.jobs import JobKind, SCHEDULES
from app.models.platform import ScheduleState
from app.services.platform.jobs.handlers import handler_for
from app.services.platform.jobs.queue import JobQueue
from app.services.platform.observability import get_logger

log = get_logger("ierp.worker")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class Worker:
    """Claims jobs and runs their handlers.

    One session per job, not one per worker. A long-lived session accumulates
    identity-map state across unrelated jobs and, worse, one job's failed
    flush poisons the next job's commit.
    """

    def __init__(
        self,
        session_factory,
        *,
        worker_id: str | None = None,
        kinds: list[JobKind] | None = None,
        poll_seconds: float | None = None,
        lease_seconds: int | None = None,
    ) -> None:
        import socket
        import os

        self.session_factory = session_factory
        self.worker_id = worker_id or f"{socket.gethostname()}-{os.getpid()}"
        self.kinds = kinds
        self.poll_seconds = poll_seconds or settings.WORKER_POLL_SECONDS
        self.lease_seconds = lease_seconds or settings.WORKER_LEASE_SECONDS
        self._stop = threading.Event()
        self.processed = 0
        self.failed = 0

    def stop(self) -> None:
        self._stop.set()

    # -- one unit of work ---------------------------------------------
    def run_once(self) -> bool:
        """Claim and run at most one job. True if one was processed.

        Returns rather than looping so a test can drive the worker
        deterministically — no sleeps, no threads, no waiting for a poll
        interval to elapse.
        """
        db: Session = self.session_factory()
        try:
            claim = JobQueue(db).claim(
                self.worker_id, kinds=self.kinds, lease_seconds=self.lease_seconds,
            )
            if claim is None:
                return False

            job = claim.job
            queue = JobQueue(db)
            started = time.perf_counter()
            log.info(
                "job started", job_id=job.id, kind=job.kind,
                attempt=job.attempts, worker=self.worker_id,
            )

            try:
                handler = handler_for(JobKind(job.kind))
                result = handler(db, job.payload or {})
                queue.succeed(job.id, result)
                self.processed += 1
                log.info(
                    "job succeeded", job_id=job.id, kind=job.kind,
                    ms=round((time.perf_counter() - started) * 1000, 1),
                )
            except Exception as exc:  # noqa: BLE001 — a bad job must not kill the worker
                # The handler may have left the session dirty; roll back
                # before the queue writes, or the failure record fails too.
                try:
                    db.rollback()
                except Exception:  # noqa: BLE001
                    pass
                outcome = queue.fail(job.id, f"{type(exc).__name__}: {exc}")
                self.failed += 1
                log.error(
                    "job failed", job_id=job.id, kind=job.kind,
                    attempt=job.attempts, status=outcome.status,
                    error=str(exc)[:300],
                )
                self._capture(db, exc, job)
            return True
        finally:
            db.close()

    def _capture(self, db: Session, exc: BaseException, job) -> None:
        from app.services.platform.observability import ErrorTracker

        try:
            ErrorTracker(db).capture(
                exc, route=f"job:{job.kind}", method="JOB",
                tenant_id=job.tenant_id,
            )
        except Exception:  # noqa: BLE001
            pass

    # -- the loop ------------------------------------------------------
    def run_forever(self) -> None:
        log.info(
            "worker starting", worker=self.worker_id,
            kinds=[k.value for k in self.kinds] if self.kinds else "all",
        )
        idle = 0
        while not self._stop.is_set():
            try:
                did_work = self.run_once()
            except Exception as exc:  # noqa: BLE001
                # A failure in the queue itself, not in a handler. Back off
                # rather than spinning: the database is probably unwell.
                log.error("worker loop error", error=str(exc))
                did_work = False

            if did_work:
                idle = 0
                continue

            # Gentle backoff while idle, capped, so an empty queue costs
            # roughly one query every few seconds instead of one per poll.
            idle = min(idle + 1, 5)
            self._stop.wait(self.poll_seconds * idle)

        log.info(
            "worker stopped", worker=self.worker_id,
            processed=self.processed, failed=self.failed,
        )


class Scheduler:
    """Fires recurring jobs and performs queue maintenance.

    Enqueues rather than executes: the scheduler decides *when*, the worker
    decides *how*. Keeping them apart means a slow nightly backup cannot delay
    the next hourly alert sweep.
    """

    def __init__(self, session_factory, *, tick_seconds: float = 30.0) -> None:
        self.session_factory = session_factory
        self.tick_seconds = tick_seconds
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def sync_schedules(self, db: Session) -> int:
        """Reconcile the stored schedule with the declared one.

        SCHED-001. This previously only INSERTED missing rows, so changing an
        interval in code had no effect on a database that already had the row:
        the filing crawl was moved from 24h to 12h in `SCHEDULES` and
        production kept running it at 86400s, because `sync_schedules` never
        looked at the existing value. The declared schedule is the source of
        truth, so a drifted interval is corrected here.

        An operator's `enabled` flag is deliberately NOT overwritten — pausing
        a schedule from the admin console must survive a deploy.
        """
        written = 0
        for spec in SCHEDULES:
            row = db.scalar(
                select(ScheduleState).where(ScheduleState.kind == spec.kind.value)
            )
            if row is None:
                db.add(ScheduleState(
                    kind=spec.kind.value,
                    enabled=spec.enabled,
                    every_seconds=spec.every_seconds,
                    next_run_at=_utcnow(),
                ))
                written += 1
            elif row.every_seconds != spec.every_seconds:
                log.info("schedule interval reconciled", kind=spec.kind.value,
                         was=row.every_seconds, now=spec.every_seconds)
                row.every_seconds = spec.every_seconds
                # Re-base the next run so a shortened interval takes effect on
                # this tick rather than after the old, longer wait.
                row.next_run_at = (
                    (row.last_run_at or _utcnow())
                    + timedelta(seconds=spec.every_seconds)
                )
                written += 1
        if written:
            db.commit()
        return written

    def tick(self) -> dict[str, int]:
        """One scheduling pass. Returns what it did, for the tests."""
        db: Session = self.session_factory()
        try:
            self.sync_schedules(db)
            queue = JobQueue(db)
            now = _utcnow()

            enqueued = 0
            for spec in SCHEDULES:
                row = db.scalar(
                    select(ScheduleState).where(ScheduleState.kind == spec.kind.value)
                )
                if row is None or not row.enabled:
                    continue
                if not spec.due(_aware(row.last_run_at), now):
                    continue

                queue.enqueue(spec.kind, payload={"scheduled": True})
                row.last_run_at = now
                row.next_run_at = now + timedelta(seconds=row.every_seconds)
                row.run_count = (row.run_count or 0) + 1
                row.last_status = "enqueued"
                enqueued += 1

            if enqueued:
                db.commit()

            # Maintenance: promote due retries and reclaim dead leases. Both
            # are cheap conditional updates and both are what stops a queue
            # from silently wedging.
            return {
                "enqueued": enqueued,
                "requeued": queue.requeue_ready(),
                "reaped": queue.reap_expired_leases(),
            }
        finally:
            db.close()

    def run_forever(self) -> None:
        log.info("scheduler starting", tick_seconds=self.tick_seconds)
        while not self._stop.is_set():
            try:
                outcome = self.tick()
                if any(outcome.values()):
                    log.info("scheduler tick", **outcome)
            except Exception as exc:  # noqa: BLE001
                log.error("scheduler error", error=str(exc))
            self._stop.wait(self.tick_seconds)
        log.info("scheduler stopped")


# ---------------------------------------------------------------------------
# In-process supervision
# ---------------------------------------------------------------------------
_threads: list[threading.Thread] = []
_runners: list[Worker | Scheduler] = []


def start_in_process(session_factory) -> None:
    """Start workers and the scheduler as daemon threads inside the API.

    Daemon threads so an interpreter shutdown is never blocked by a worker
    waiting on a poll. Work in flight is protected by the lease, not by a
    graceful join: whatever a killed worker was doing is reclaimed after the
    lease expires. That is the same guarantee as a container being evicted,
    so it is the one worth relying on.
    """
    if settings.WORKER_ENABLED:
        for i in range(max(1, settings.WORKER_CONCURRENCY)):
            worker = Worker(session_factory, worker_id=f"inproc-{i}")
            thread = threading.Thread(
                target=worker.run_forever, name=f"ierp-worker-{i}", daemon=True,
            )
            thread.start()
            _threads.append(thread)
            _runners.append(worker)

    if settings.SCHEDULER_ENABLED:
        scheduler = Scheduler(session_factory)
        thread = threading.Thread(
            target=scheduler.run_forever, name="ierp-scheduler", daemon=True,
        )
        thread.start()
        _threads.append(thread)
        _runners.append(scheduler)


def stop_in_process(timeout: float = 5.0) -> None:
    for runner in _runners:
        runner.stop()
    for thread in _threads:
        thread.join(timeout=timeout)
    _threads.clear()
    _runners.clear()


def main() -> None:
    """Entry point for `python -m app.worker`."""
    from app.db.base import SessionLocal

    from app.services.documents.worker import DocumentWorker
    from app.services.platform.observability import configure_logging

    configure_logging()

    worker = Worker(SessionLocal)
    scheduler = Scheduler(SessionLocal)
    # Document ingestion has its own claim table and can run for minutes.
    # It is NOT handled by the platform JobQueue — without this loop,
    # uploaded PDFs stay at pages=0 / chunks=0 forever.
    document_worker = DocumentWorker(SessionLocal)

    def _shutdown(signum, _frame):
        log.info("shutdown signal received", signal=signum)
        worker.stop()
        scheduler.stop()
        document_worker.stop()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    threading.Thread(target=scheduler.run_forever, daemon=True).start()
    threading.Thread(
        target=document_worker.run_forever, name="document-worker", daemon=True,
    ).start()
    worker.run_forever()
