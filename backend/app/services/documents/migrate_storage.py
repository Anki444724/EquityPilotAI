"""Migrate stored document bytes between storage backends.

Written for the Railway Volume → Object Storage move, but the direction is not
hard-coded: it copies from any `DocumentStorage` to any other.

The rules that make this safe to run against production:

* **Nothing is deleted.** The source object is left exactly where it is. A
  migration that removes the original has no rollback, and the whole point of
  moving 283 MB of irreplaceable filings is that they remain readable if the
  destination turns out to be misconfigured.
* **Every copy is verified before the database is updated.** The bytes are
  re-read from the destination and their SHA256 compared with the source. Only
  then is `storage_backend`/`storage_location` repointed. A row that claims an
  object exists somewhere it does not is worse than an unmigrated row, because
  the failure surfaces later, to a user, on download.
* **Idempotent and resumable.** A document already present and verified at the
  destination is skipped, so an interrupted run is simply re-run. This matters
  when the source is a 500 MB volume being copied over the public network.
* **Per-document failure isolation.** One unreadable object must not abandon
  the remaining hundreds; it is recorded and the run continues.

The database is the index of record: `Document.storage_key` is unchanged by a
migration, because the key is the same in both backends. Only the backend name
and the human-readable location move.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any

import structlog
from sqlalchemy import select

from app.models.document import Document
from app.services.documents.storage import DocumentStorage, StorageError

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class DocumentOutcome:
    document_id: int
    key: str
    status: str          # migrated | already_present | missing_source | failed
    size_bytes: int = 0
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id, "key": self.key,
            "status": self.status, "size_bytes": self.size_bytes,
            "detail": self.detail,
        }


@dataclass(slots=True)
class MigrationReport:
    total: int = 0
    migrated: int = 0
    already_present: int = 0
    missing_source: int = 0
    failed: int = 0
    bytes_copied: int = 0
    latency_ms: float = 0.0
    outcomes: list[DocumentOutcome] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """A run is only clean if nothing failed and nothing was unreadable."""
        return self.failed == 0 and self.missing_source == 0

    @property
    def throughput_mbps(self) -> float:
        seconds = self.latency_ms / 1000.0
        if seconds <= 0:
            return 0.0
        return round((self.bytes_copied / (1024 * 1024)) / seconds, 2)

    def as_dict(self) -> dict[str, Any]:
        return {
            "total": self.total, "migrated": self.migrated,
            "already_present": self.already_present,
            "missing_source": self.missing_source, "failed": self.failed,
            "bytes_copied": self.bytes_copied,
            "megabytes_copied": round(self.bytes_copied / (1024 * 1024), 2),
            "latency_ms": round(self.latency_ms, 1),
            "throughput_mbps": self.throughput_mbps,
            "ok": self.ok,
            "outcomes": [o.as_dict() for o in self.outcomes],
        }


class StorageMigrator:
    """Copies document objects from one backend to another, verifying each."""

    def __init__(
        self,
        db: Any,
        source: DocumentStorage,
        destination: DocumentStorage,
        *,
        dry_run: bool = False,
    ) -> None:
        self.db = db
        self.source = source
        self.destination = destination
        self.dry_run = dry_run

    def pending(self, *, limit: int | None = None) -> list[Document]:
        """Documents whose bytes are not yet recorded at the destination."""
        query = select(Document).where(Document.storage_key.is_not(None))
        query = query.order_by(Document.id)
        if limit:
            query = query.limit(limit)
        return list(self.db.execute(query).scalars().all())

    def migrate_one(self, document: Document) -> DocumentOutcome:
        key = document.storage_key
        if not key:
            return DocumentOutcome(document.id, "", "failed",
                                   detail="no storage key")

        # Already at the destination and readable? Then this is a re-run.
        try:
            if self.destination.exists(key):
                remote = self.destination.read(key)
                if document.content_hash and (
                    hashlib.sha256(remote).hexdigest() == document.content_hash
                ):
                    self._repoint(document)
                    return DocumentOutcome(
                        document.id, key, "already_present", len(remote),
                    )
        except Exception:  # noqa: BLE001 — treat as not present and copy again
            log.debug("destination probe failed, will copy", key=key)

        try:
            payload = self.source.read(key)
        except (StorageError, FileNotFoundError, OSError) as exc:
            # The row references bytes the source cannot produce. Reported, not
            # swallowed: it means the volume lost an object, which the operator
            # must know about before anything is decommissioned.
            log.warning("source object unreadable", document_id=document.id,
                        key=key, error=str(exc)[:160])
            return DocumentOutcome(document.id, key, "missing_source",
                                   detail=str(exc)[:200])

        if self.dry_run:
            return DocumentOutcome(document.id, key, "migrated", len(payload),
                                   detail="dry run — nothing written")

        try:
            stored = self.destination.put(key, payload)
        except Exception as exc:  # noqa: BLE001
            return DocumentOutcome(document.id, key, "failed", len(payload),
                                   detail=f"write failed: {exc}"[:200])

        # Verify by reading back, not by trusting the write. An S3 put that
        # returns success and stores nothing is rare; discovering it months
        # later when a user downloads a filing is not acceptable.
        try:
            verify = self.destination.read(key)
        except Exception as exc:  # noqa: BLE001
            return DocumentOutcome(document.id, key, "failed", len(payload),
                                   detail=f"verify read failed: {exc}"[:200])

        digest = hashlib.sha256(verify).hexdigest()
        if digest != stored.content_hash or len(verify) != len(payload):
            return DocumentOutcome(
                document.id, key, "failed", len(payload),
                detail=(
                    f"checksum mismatch after copy: source "
                    f"{stored.content_hash[:12]} vs readback {digest[:12]}"
                ),
            )
        if document.content_hash and digest != document.content_hash:
            # The object is intact but disagrees with what the database
            # recorded at upload. Surfaced rather than silently accepted.
            return DocumentOutcome(
                document.id, key, "failed", len(payload),
                detail=(
                    f"copied object does not match the recorded content hash "
                    f"({digest[:12]} vs {document.content_hash[:12]})"
                ),
            )

        self._repoint(document)
        return DocumentOutcome(document.id, key, "migrated", len(payload))

    def _repoint(self, document: Document) -> None:
        """Point the row at the destination. The key itself does not change."""
        document.storage_backend = self.destination.backend
        try:
            document.storage_location = self.destination.location(
                document.storage_key
            )
        except Exception:  # noqa: BLE001 — location is cosmetic
            pass

    def run(self, *, limit: int | None = None,
            commit_every: int = 25) -> MigrationReport:
        started = time.perf_counter()
        report = MigrationReport()
        documents = self.pending(limit=limit)
        report.total = len(documents)

        for index, document in enumerate(documents, start=1):
            outcome = self.migrate_one(document)
            report.outcomes.append(outcome)
            if outcome.status == "migrated":
                report.migrated += 1
                report.bytes_copied += outcome.size_bytes
            elif outcome.status == "already_present":
                report.already_present += 1
            elif outcome.status == "missing_source":
                report.missing_source += 1
            else:
                report.failed += 1

            # Commit in batches so an interrupted run keeps its progress.
            if not self.dry_run and index % commit_every == 0:
                self.db.commit()

        if not self.dry_run:
            self.db.commit()

        report.latency_ms = (time.perf_counter() - started) * 1000
        log.info("storage migration complete", **{
            k: v for k, v in report.as_dict().items() if k != "outcomes"
        })
        return report
