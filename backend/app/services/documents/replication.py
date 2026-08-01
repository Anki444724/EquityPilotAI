"""Replicate documents from the authoritative volume to object storage.

The write path is deliberately one-directional: bytes are read from the
primary, written to the secondary, read back, and the SHA256 compared against
what the database recorded at upload. The primary is never written to and
never deleted from by anything in this module.

Three properties are worth stating because they are easy to get wrong.

**The expected hash comes from the database, not from the copy.** Hashing the
bytes we just uploaded and comparing them with themselves proves only that
memory is working. The comparison that means something is against
`Document.content_hash`, recorded when the upload was first accepted.

**A mismatch is terminal.** It is not retried, because a retry that happens to
succeed would paper over the fact that two copies of a financial filing
disagreed. It alerts and stays visible.

**Replication failure never fails the caller.** Upload succeeds on the volume
alone; replication is best-effort and reported. That is the whole point of
keeping the volume authoritative during this phase.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog
from sqlalchemy import func, select

from app.domain.documents.replication import (
    MAX_REPLICATION_ATTEMPTS, ReplicationState, should_retry,
)
from app.models.document import Document
from app.models.replication import DocumentReplica
from app.services.documents.storage import DocumentStorage, StorageError

log = structlog.get_logger(__name__)

SECONDARY_BACKEND = "s3"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class ReplicationOutcome:
    document_id: int
    state: ReplicationState
    size_bytes: int = 0
    detail: str = ""
    latency_ms: float = 0.0

    @property
    def ok(self) -> bool:
        return self.state is ReplicationState.VERIFIED

    def as_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id, "state": self.state.value,
            "size_bytes": self.size_bytes, "detail": self.detail,
            "latency_ms": round(self.latency_ms, 1), "ok": self.ok,
        }


@dataclass(slots=True)
class ReplicationRun:
    attempted: int = 0
    verified: int = 0
    failed: int = 0
    mismatched: int = 0
    skipped: int = 0
    bytes_copied: int = 0
    latency_ms: float = 0.0
    outcomes: list[ReplicationOutcome] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.failed == 0 and self.mismatched == 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "attempted": self.attempted, "verified": self.verified,
            "failed": self.failed, "mismatched": self.mismatched,
            "skipped": self.skipped, "bytes_copied": self.bytes_copied,
            "megabytes_copied": round(self.bytes_copied / (1024 * 1024), 2),
            "latency_ms": round(self.latency_ms, 1), "ok": self.ok,
            "outcomes": [o.as_dict() for o in self.outcomes],
        }


class ReplicationService:
    """Copies verified documents to the secondary backend."""

    def __init__(
        self,
        db: Any,
        *,
        primary: DocumentStorage | None = None,
        secondary: DocumentStorage | None = None,
    ) -> None:
        self.db = db
        self._primary = primary
        self._secondary = secondary

    # ------------------------------------------------------------ backends
    @property
    def primary(self) -> DocumentStorage:
        if self._primary is None:
            from app.services.documents.storage import get_storage

            self._primary = get_storage()
        return self._primary

    @property
    def secondary(self) -> DocumentStorage | None:
        """The *other* backend — whichever one the primary is not.

        REPL-002. This originally always built the S3 client, which was
        correct while the volume was primary and became a silent no-op the
        moment `DOCUMENT_STORAGE_BACKEND=r2`: primary and secondary both
        resolved to R2, so the "fallback" read retried the backend that had
        just failed, and replication would have copied R2 onto itself.

        The pairing is therefore derived from the configured primary:

        * volume primary  -> secondary is object storage (the pre-cutover
          arrangement: replicate outwards to R2);
        * object primary  -> secondary is the volume (the post-cutover
          arrangement: the volume is the read fallback the brief requires
          until migration is fully verified).

        Returning None is still valid and means "no second backend", which is
        how the platform runs with no bucket configured at all.
        """
        if self._secondary is not None:
            return self._secondary
        from app.core.config import settings
        from app.services.documents.storage import (
            LocalFileStorage, S3CompatibleStorage,
        )

        backend = (settings.DOCUMENT_STORAGE_BACKEND or "local").lower()
        if backend in {"s3", "r2", "minio"}:
            # Object storage is primary; the volume is the fallback copy.
            try:
                self._secondary = LocalFileStorage(settings.DOCUMENT_STORAGE_PATH)
            except Exception as exc:  # noqa: BLE001 - no volume mounted
                log.warning("volume fallback unavailable", error=str(exc)[:160])
                return None
            return self._secondary

        if not settings.DOCUMENT_S3_BUCKET:
            return None
        try:
            self._secondary = S3CompatibleStorage(
                settings.DOCUMENT_S3_BUCKET,
                endpoint_url=settings.DOCUMENT_S3_ENDPOINT,
                region=settings.DOCUMENT_S3_REGION,
                access_key=settings.DOCUMENT_S3_ACCESS_KEY,
                secret_key=settings.DOCUMENT_S3_SECRET_KEY,
            )
        except StorageError as exc:
            log.warning("secondary storage unavailable", error=str(exc)[:160])
            return None
        return self._secondary

    @property
    def enabled(self) -> bool:
        return self.secondary is not None

    # --------------------------------------------------------------- state
    def replica_for(self, document: Document) -> DocumentReplica:
        replica = self.db.scalar(
            select(DocumentReplica).where(
                DocumentReplica.document_id == document.id,
                DocumentReplica.backend == SECONDARY_BACKEND,
            )
        )
        if replica is None:
            replica = DocumentReplica(
                document_id=document.id, backend=SECONDARY_BACKEND,
                storage_key=document.storage_key,
                state=ReplicationState.PENDING.value,
            )
            self.db.add(replica)
            self.db.flush()
        return replica

    def enrol(self, document: Document) -> DocumentReplica:
        """Register a freshly-uploaded document as awaiting replication.

        Called on the upload path. It only writes a row; the copy itself
        happens in the background so the user is never waiting on S3.
        """
        return self.replica_for(document)

    # --------------------------------------------------------- replication
    def replicate_one(self, document: Document) -> ReplicationOutcome:
        started = time.perf_counter()
        secondary = self.secondary
        if secondary is None:
            return ReplicationOutcome(
                document.id, ReplicationState.PENDING,
                detail="object storage is not configured",
            )

        replica = self.replica_for(document)
        key = document.storage_key
        if not key:
            replica.state = ReplicationState.SKIPPED.value
            replica.error = "document has no storage key"
            return ReplicationOutcome(document.id, ReplicationState.SKIPPED,
                                      detail=replica.error)

        replica.state = ReplicationState.REPLICATING.value
        replica.attempts = (replica.attempts or 0) + 1
        replica.last_attempt_at = _utcnow()
        replica.storage_key = key

        # --- read the authoritative copy --------------------------------
        try:
            payload = self.primary.read(key)
        except (StorageError, FileNotFoundError, OSError) as exc:
            # The primary cannot produce the bytes. Skipped rather than
            # failed: retrying will not conjure them, and it must be visible
            # as a primary-side problem, not a replication one.
            replica.state = ReplicationState.SKIPPED.value
            replica.error = f"primary unreadable: {exc}"[:400]
            log.warning("primary object unreadable", document_id=document.id,
                        key=key, error=str(exc)[:160])
            return ReplicationOutcome(
                document.id, ReplicationState.SKIPPED, detail=replica.error,
                latency_ms=(time.perf_counter() - started) * 1000,
            )

        # --- write the replica ------------------------------------------
        try:
            secondary.put(key, payload)
        except Exception as exc:  # noqa: BLE001 — any S3 error is a failure
            replica.state = ReplicationState.FAILED.value
            replica.error = f"write failed: {exc}"[:400]
            log.warning("replication write failed", document_id=document.id,
                        error=str(exc)[:160])
            return ReplicationOutcome(
                document.id, ReplicationState.FAILED, len(payload),
                replica.error, (time.perf_counter() - started) * 1000,
            )

        replica.replicated_at = _utcnow()
        replica.size_bytes = len(payload)

        # --- verify by reading back --------------------------------------
        try:
            readback = secondary.read(key)
        except Exception as exc:  # noqa: BLE001
            replica.state = ReplicationState.FAILED.value
            replica.error = f"verification read failed: {exc}"[:400]
            return ReplicationOutcome(
                document.id, ReplicationState.FAILED, len(payload),
                replica.error, (time.perf_counter() - started) * 1000,
            )

        digest = hashlib.sha256(readback).hexdigest()
        replica.verified_sha256 = digest
        expected = document.content_hash

        if expected and digest != expected:
            # Terminal. Two copies of a filing disagree; an automatic retry
            # that happened to succeed would hide that it ever occurred.
            replica.state = ReplicationState.MISMATCH.value
            replica.error = (
                f"SHA256 mismatch: expected {expected[:16]}…, "
                f"replica returned {digest[:16]}…"
            )
            log.error("replication checksum mismatch", document_id=document.id,
                      expected=expected[:16], observed=digest[:16])
            return ReplicationOutcome(
                document.id, ReplicationState.MISMATCH, len(payload),
                replica.error, (time.perf_counter() - started) * 1000,
            )

        replica.state = ReplicationState.VERIFIED.value
        replica.verified_at = _utcnow()
        replica.error = None
        elapsed = (time.perf_counter() - started) * 1000
        log.info("document replicated", document_id=document.id,
                 size=len(payload), ms=round(elapsed, 1))
        return ReplicationOutcome(document.id, ReplicationState.VERIFIED,
                                  len(payload), latency_ms=elapsed)

    def pending_documents(self, *, limit: int = 25) -> list[Document]:
        """Documents needing a replication attempt, oldest first.

        A left join rather than a subquery so a document with no replica row
        at all — every pre-existing document — is picked up on the first pass.
        """
        rows = self.db.execute(
            select(Document, DocumentReplica)
            .outerjoin(
                DocumentReplica,
                (DocumentReplica.document_id == Document.id)
                & (DocumentReplica.backend == SECONDARY_BACKEND),
            )
            .where(Document.storage_key.is_not(None))
            .order_by(Document.id)
        ).all()

        out: list[Document] = []
        for document, replica in rows:
            if replica is None:
                out.append(document)
            elif should_retry(replica.state, replica.attempts or 0):
                out.append(document)
            if len(out) >= limit:
                break
        return out

    def run(self, *, limit: int = 25) -> ReplicationRun:
        """One background replication pass."""
        started = time.perf_counter()
        run = ReplicationRun()

        if not self.enabled:
            run.latency_ms = (time.perf_counter() - started) * 1000
            return run

        for document in self.pending_documents(limit=limit):
            outcome = self.replicate_one(document)
            run.attempted += 1
            run.outcomes.append(outcome)
            if outcome.state is ReplicationState.VERIFIED:
                run.verified += 1
                run.bytes_copied += outcome.size_bytes
            elif outcome.state is ReplicationState.MISMATCH:
                run.mismatched += 1
            elif outcome.state is ReplicationState.SKIPPED:
                run.skipped += 1
            else:
                run.failed += 1
            self.db.flush()

        self.db.commit()
        run.latency_ms = (time.perf_counter() - started) * 1000
        log.info("replication pass complete", **{
            k: v for k, v in run.as_dict().items() if k != "outcomes"
        })
        return run

    # ------------------------------------------------------------- reading
    def read_document(self, document: Document) -> bytes:
        """Read a document, falling back to the replica.

        The volume is authoritative and tried first. If it cannot serve the
        bytes — a lost object, an unmounted volume — the verified replica is
        used rather than failing the user's download. The fallback is logged
        loudly, because reading from the secondary means the primary has a
        problem somebody must look at.
        """
        key = document.storage_key
        if not key:
            raise StorageError(f"document {document.id} has no storage key")

        try:
            return self.primary.read(key)
        except (StorageError, FileNotFoundError, OSError) as primary_error:
            secondary = self.secondary
            if secondary is None:
                raise
            log.error(
                "primary read failed, falling back to replica",
                document_id=document.id, key=key,
                error=str(primary_error)[:160],
            )
            try:
                payload = secondary.read(key)
            except Exception as exc:  # noqa: BLE001
                raise StorageError(
                    f"document {document.id} unreadable from both the "
                    f"primary backend ({primary_error}) and the fallback "
                    f"({exc})"
                ) from exc

            # A fallback read is still a read of a financial document, so it
            # is checked. Serving bytes that do not match the recorded hash
            # would be worse than serving an error.
            if document.content_hash:
                digest = hashlib.sha256(payload).hexdigest()
                if digest != document.content_hash:
                    raise StorageError(
                        f"replica for document {document.id} failed checksum "
                        f"verification on fallback read"
                    )
            log.warning("served from replica", document_id=document.id,
                        size=len(payload))
            return payload

    # -------------------------------------------------------------- health
    def counts(self) -> dict[str, int]:
        rows = dict(
            self.db.execute(
                select(DocumentReplica.state, func.count())
                .where(DocumentReplica.backend == SECONDARY_BACKEND)
                .group_by(DocumentReplica.state)
            ).all()
        )
        total_documents = self.db.scalar(
            select(func.count()).select_from(Document)
            .where(Document.storage_key.is_not(None))
        ) or 0
        tracked = sum(rows.values())
        counts = {s.value: rows.get(s.value, 0) for s in ReplicationState}
        # Documents with no replica row are pending by definition.
        counts["pending"] = counts.get("pending", 0) + max(
            0, total_documents - tracked
        )
        counts["total_documents"] = total_documents
        return counts

    def last_success(self) -> datetime | None:
        return self.db.scalar(
            select(func.max(DocumentReplica.verified_at))
            .where(DocumentReplica.backend == SECONDARY_BACKEND)
        )

    def replicated_bytes(self) -> int:
        return int(self.db.scalar(
            select(func.coalesce(func.sum(DocumentReplica.size_bytes), 0))
            .where(
                DocumentReplica.backend == SECONDARY_BACKEND,
                DocumentReplica.state == ReplicationState.VERIFIED.value,
            )
        ) or 0)

    def clean_since(self) -> datetime | None:
        """When the current unbroken run of clean replication began.

        Used by the promotion assessment. Defined as the earliest verification
        that is not preceded by a mismatch, which is the only reading of
        "consecutive days without a checksum failure" that cannot be gamed by
        a quiet period.
        """
        latest_mismatch = self.db.scalar(
            select(func.max(DocumentReplica.last_attempt_at)).where(
                DocumentReplica.backend == SECONDARY_BACKEND,
                DocumentReplica.state == ReplicationState.MISMATCH.value,
            )
        )
        earliest_verified = self.db.scalar(
            select(func.min(DocumentReplica.verified_at)).where(
                DocumentReplica.backend == SECONDARY_BACKEND,
                DocumentReplica.state == ReplicationState.VERIFIED.value,
            )
        )
        if latest_mismatch is None:
            return earliest_verified
        if earliest_verified is None:
            return None
        if earliest_verified.tzinfo is None:
            earliest_verified = earliest_verified.replace(tzinfo=timezone.utc)
        if latest_mismatch.tzinfo is None:
            latest_mismatch = latest_mismatch.replace(tzinfo=timezone.utc)
        # A mismatch resets the clock.
        return max(earliest_verified, latest_mismatch)
