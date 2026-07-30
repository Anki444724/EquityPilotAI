"""The job queue: enqueue, claim, complete, retry, reap.

A database-backed queue rather than Redis or Celery, for the same reason the
rest of the platform runs on SQLite by default: it must work with no
infrastructure, and it must work on Railway with one Postgres instance. When
throughput demands a real broker, the interface here is what gets
reimplemented — not every call site.

The correctness question for any queue is *what happens when a worker dies
mid-job*. The answer here is leases: claiming a job sets `locked_by` and
`lease_expires_at`, and a job whose lease has expired is reclaimable. A dead
worker's job returns to the queue after the lease elapses rather than being
stuck `running` forever.

The claim itself is a conditional update — `UPDATE … WHERE id = ? AND status =
'queued'` — and the row is only claimed if the update reports one row changed.
Two workers racing for the same job therefore produce one winner and one
retry, on SQLite and on Postgres alike, without `SELECT … FOR UPDATE`.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.orm import Session

from app.domain.platform.jobs import (
    ACTIVE_STATUSES, DEFAULT_PRIORITY, JobKind, JobPriority, JobStatus,
    QueueDepth, assert_transition, idempotency_key, policy_for,
)
from app.models.platform import BackgroundJob


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class QueueError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Claim:
    """A leased job. The worker must complete or fail it before the lease
    expires, or another worker will pick it up."""

    job: BackgroundJob
    worker_id: str
    lease_expires_at: datetime


class JobQueue:
    def __init__(self, db: Session) -> None:
        self.db = db

    # ==================================================================
    # Enqueue
    # ==================================================================
    def enqueue(
        self,
        kind: JobKind,
        *,
        payload: dict[str, Any] | None = None,
        tenant_id: int | None = None,
        priority: JobPriority | None = None,
        run_after: datetime | None = None,
        resource_type: str | None = None,
        resource_id: str | int | None = None,
        max_attempts: int | None = None,
        deduplicate: bool = True,
    ) -> BackgroundJob:
        """Add work to the queue.

        With `deduplicate`, an identical pending job is returned instead of a
        second copy. "Identical" means same kind, same tenant, same payload —
        so a user clicking Generate twice gets one report, while the same
        report requested again tomorrow is genuinely new work.
        """
        body = payload or {}
        key = idempotency_key(kind, tenant_id, body)

        if deduplicate:
            existing = self.db.scalar(
                select(BackgroundJob).where(
                    BackgroundJob.idempotency_key == key,
                    BackgroundJob.status.in_([s.value for s in ACTIVE_STATUSES]),
                )
            )
            if existing is not None:
                return existing

        policy = policy_for(kind)
        job = BackgroundJob(
            tenant_id=tenant_id,
            kind=kind.value,
            status=JobStatus.QUEUED.value,
            priority=int(priority or DEFAULT_PRIORITY[kind]),
            payload=body,
            idempotency_key=key,
            resource_type=resource_type,
            resource_id=str(resource_id) if resource_id is not None else None,
            max_attempts=max_attempts or policy.max_attempts,
            run_after=run_after or _utcnow(),
        )
        self.db.add(job)
        self.db.commit()
        self.db.refresh(job)
        return job

    # ==================================================================
    # Claim
    # ==================================================================
    def claim(
        self,
        worker_id: str,
        *,
        kinds: list[JobKind] | None = None,
        lease_seconds: int = 300,
    ) -> Claim | None:
        """Take the next eligible job, or None.

        Eligible means queued (or leased-but-expired), due, and of a kind this
        worker handles. Ordered by priority then age, so interactive work goes
        first and nothing starves.
        """
        now = _utcnow()

        candidates = select(BackgroundJob).where(
            or_(
                and_(
                    BackgroundJob.status == JobStatus.QUEUED.value,
                    or_(
                        BackgroundJob.run_after.is_(None),
                        BackgroundJob.run_after <= now,
                    ),
                ),
                # Reclaim: a worker that died still holds the row.
                and_(
                    BackgroundJob.status == JobStatus.RUNNING.value,
                    BackgroundJob.lease_expires_at.isnot(None),
                    BackgroundJob.lease_expires_at <= now,
                ),
            )
        )
        if kinds:
            candidates = candidates.where(
                BackgroundJob.kind.in_([k.value for k in kinds])
            )
        candidates = candidates.order_by(
            BackgroundJob.priority.asc(), BackgroundJob.id.asc()
        ).limit(10)

        lease_until = now + timedelta(seconds=lease_seconds)

        for job in self.db.scalars(candidates):
            # Conditional update: only the worker whose UPDATE actually
            # changes the row wins. `status` is part of the predicate, so a
            # concurrent claim invalidates the loser's attempt.
            result = self.db.execute(
                update(BackgroundJob)
                .where(
                    BackgroundJob.id == job.id,
                    BackgroundJob.status == job.status,
                    (
                        BackgroundJob.lease_expires_at.is_(None)
                        if job.status == JobStatus.QUEUED.value
                        else BackgroundJob.lease_expires_at <= now
                    ),
                )
                .values(
                    status=JobStatus.RUNNING.value,
                    locked_by=worker_id,
                    lease_expires_at=lease_until,
                    started_at=job.started_at or now,
                    attempts=BackgroundJob.attempts + 1,
                    stage="running",
                )
            )
            self.db.commit()
            if result.rowcount:
                self.db.refresh(job)
                return Claim(job=job, worker_id=worker_id, lease_expires_at=lease_until)

        return None

    def extend_lease(self, job_id: int, worker_id: str, seconds: int = 300) -> bool:
        """Renew a lease on a long-running job.

        A document with four hundred pages takes longer than any sensible
        default lease. Rather than setting the lease to the worst case — which
        would leave a genuinely dead worker's job stuck for that long — the
        handler renews as it makes progress.
        """
        result = self.db.execute(
            update(BackgroundJob)
            .where(BackgroundJob.id == job_id, BackgroundJob.locked_by == worker_id)
            .values(lease_expires_at=_utcnow() + timedelta(seconds=seconds))
        )
        self.db.commit()
        return bool(result.rowcount)

    def progress(self, job_id: int, fraction: float, stage: str | None = None) -> None:
        job = self.db.get(BackgroundJob, job_id)
        if job is None:
            return
        job.progress = max(0.0, min(1.0, fraction))
        if stage:
            job.stage = stage[:32]
        self.db.commit()

    # ==================================================================
    # Completion
    # ==================================================================
    def succeed(self, job_id: int, result: dict[str, Any] | None = None) -> BackgroundJob:
        job = self._require(job_id)
        assert_transition(JobStatus(job.status), JobStatus.SUCCEEDED)

        now = _utcnow()
        job.status = JobStatus.SUCCEEDED.value
        job.finished_at = now
        job.progress = 1.0
        job.stage = "done"
        job.result = result
        job.error = None
        job.locked_by = None
        job.lease_expires_at = None
        started = _aware(job.started_at)
        job.duration_ms = round((now - started).total_seconds() * 1000, 2) if started else 0.0
        self.db.commit()
        self.db.refresh(job)
        return job

    def fail(self, job_id: int, error: str) -> BackgroundJob:
        """Record a failed attempt and schedule the retry, or dead-letter it.

        The backoff comes from the kind's policy and is seeded with the job id,
        so a thousand jobs failing at once retry across a spread rather than
        in a thundering herd.
        """
        job = self._require(job_id)
        policy = policy_for(JobKind(job.kind))
        target = policy.outcome_after_failure(job.attempts)

        assert_transition(JobStatus(job.status), target)

        now = _utcnow()
        job.status = target.value
        job.error = (error or "")[:4000]
        job.locked_by = None
        job.lease_expires_at = None
        job.finished_at = now if target is JobStatus.DEAD_LETTER else None
        started = _aware(job.started_at)
        job.duration_ms = round((now - started).total_seconds() * 1000, 2) if started else 0.0

        if target is JobStatus.FAILED:
            job.run_after = policy.next_run_at(now, job.attempts, seed=str(job.id))
            job.stage = "retry_scheduled"
        else:
            job.stage = "dead_letter"

        self.db.commit()
        self.db.refresh(job)
        return job

    def cancel(self, job_id: int) -> BackgroundJob:
        job = self._require(job_id)
        assert_transition(JobStatus(job.status), JobStatus.CANCELLED)
        job.status = JobStatus.CANCELLED.value
        job.finished_at = _utcnow()
        job.locked_by = None
        job.lease_expires_at = None
        self.db.commit()
        self.db.refresh(job)
        return job

    def retry(self, job_id: int) -> BackgroundJob:
        """Manual replay of a dead-lettered job.

        The attempt counter is reset: an operator replaying a job after fixing
        the underlying cause wants a fresh set of attempts, not the one
        remaining from the original burst.
        """
        job = self._require(job_id)
        assert_transition(JobStatus(job.status), JobStatus.QUEUED)
        job.status = JobStatus.QUEUED.value
        job.attempts = 0
        job.error = None
        job.run_after = _utcnow()
        job.stage = "queued"
        job.finished_at = None
        self.db.commit()
        self.db.refresh(job)
        return job

    def requeue_ready(self) -> int:
        """Move failed jobs whose backoff has elapsed back to queued.

        Called by the scheduler. `claim()` only looks at queued jobs, so
        without this a failed job would never be retried.
        """
        result = self.db.execute(
            update(BackgroundJob)
            .where(
                BackgroundJob.status == JobStatus.FAILED.value,
                BackgroundJob.run_after.isnot(None),
                BackgroundJob.run_after <= _utcnow(),
            )
            .values(status=JobStatus.QUEUED.value, stage="queued")
        )
        self.db.commit()
        return int(result.rowcount or 0)

    def reap_expired_leases(self) -> int:
        """Return abandoned jobs to the queue.

        `claim()` already reclaims them opportunistically; this makes the
        state correct in the admin panel even when no worker is polling, so a
        stalled queue looks stalled rather than busy.
        """
        result = self.db.execute(
            update(BackgroundJob)
            .where(
                BackgroundJob.status == JobStatus.RUNNING.value,
                BackgroundJob.lease_expires_at.isnot(None),
                BackgroundJob.lease_expires_at <= _utcnow(),
            )
            .values(
                status=JobStatus.QUEUED.value, locked_by=None,
                lease_expires_at=None, stage="reclaimed",
            )
        )
        self.db.commit()
        return int(result.rowcount or 0)

    # ==================================================================
    # Reading
    # ==================================================================
    def get(self, job_id: int) -> BackgroundJob | None:
        return self.db.get(BackgroundJob, job_id)

    def _require(self, job_id: int) -> BackgroundJob:
        job = self.get(job_id)
        if job is None:
            raise QueueError(f"no job {job_id}")
        return job

    def list(
        self,
        *,
        tenant_id: int | None = None,
        kind: JobKind | None = None,
        status: JobStatus | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[list[BackgroundJob], int]:
        stmt = select(BackgroundJob)
        if tenant_id is not None:
            stmt = stmt.where(BackgroundJob.tenant_id == tenant_id)
        if kind:
            stmt = stmt.where(BackgroundJob.kind == kind.value)
        if status:
            stmt = stmt.where(BackgroundJob.status == status.value)
        total = self.db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        rows = list(self.db.scalars(
            stmt.order_by(BackgroundJob.id.desc()).offset(offset).limit(limit)
        ))
        return rows, total

    def depth(self) -> QueueDepth:
        """The monitoring snapshot."""
        now = _utcnow()
        counts = {
            status: int(count) for status, count in self.db.execute(
                select(BackgroundJob.status, func.count(BackgroundJob.id))
                .group_by(BackgroundJob.status)
            )
        }

        oldest = self.db.scalar(
            select(func.min(BackgroundJob.run_after)).where(
                BackgroundJob.status == JobStatus.QUEUED.value
            )
        )
        oldest_seconds = 0.0
        oldest_aware = _aware(oldest)
        if oldest_aware is not None:
            oldest_seconds = max(0.0, (now - oldest_aware).total_seconds())

        since = now - timedelta(hours=24)
        durations = sorted(
            float(d) for d in self.db.scalars(
                select(BackgroundJob.duration_ms).where(
                    BackgroundJob.status == JobStatus.SUCCEEDED.value,
                    BackgroundJob.finished_at >= since,
                    BackgroundJob.duration_ms > 0,
                )
            )
        )

        by_kind = {
            kind: int(count) for kind, count in self.db.execute(
                select(BackgroundJob.kind, func.count(BackgroundJob.id))
                .where(BackgroundJob.status.in_([s.value for s in ACTIVE_STATUSES]))
                .group_by(BackgroundJob.kind)
            )
        }

        return QueueDepth(
            queued=counts.get(JobStatus.QUEUED.value, 0),
            running=counts.get(JobStatus.RUNNING.value, 0),
            failed=counts.get(JobStatus.FAILED.value, 0),
            dead_letter=counts.get(JobStatus.DEAD_LETTER.value, 0),
            succeeded_24h=len(durations),
            oldest_queued_seconds=round(oldest_seconds, 1),
            p50_duration_ms=_percentile(durations, 0.50),
            p95_duration_ms=_percentile(durations, 0.95),
            by_kind=by_kind,
        )

    def purge_completed(self, *, older_than_days: int = 7) -> int:
        """Delete old successes. Failures and dead letters are kept — they are
        the ones somebody may still need to look at."""
        cutoff = _utcnow() - timedelta(days=older_than_days)
        count = self.db.query(BackgroundJob).filter(
            BackgroundJob.status == JobStatus.SUCCEEDED.value,
            BackgroundJob.finished_at < cutoff,
        ).delete(synchronize_session=False)
        self.db.commit()
        return int(count)


def _percentile(sorted_values: list[float], p: float) -> float:
    """Nearest-rank percentile of an already-sorted list."""
    if not sorted_values:
        return 0.0
    index = min(len(sorted_values) - 1, max(0, int(round(p * len(sorted_values))) - 1))
    return round(sorted_values[index], 2)
