"""Per-document replication state for hybrid storage.

A separate table rather than columns on `documents`, for three reasons that
matter operationally:

* **Replication is a property of a (document, backend) pair, not of a
  document.** When a third backend is added — an off-Railway cold copy, say —
  it is another row, not another five columns.
* **It carries its own history.** Attempt counts, error text and verification
  timestamps churn far more often than a document row, which is otherwise
  stable once processed.
* **The primary record stays clean.** `Document.storage_key` continues to
  describe the authoritative copy on the volume, and nothing about the replica
  can be mistaken for it.

The document's own `content_hash` remains the single source of truth for what
the bytes should be. `verified_sha256` records what was actually read back from
the replica, so a mismatch preserves both values rather than overwriting the
expectation with the observation.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
# Registers the FK target before the mapper is configured; without this the
# relationship resolves lazily and fails with NoReferencedTableError in
# whatever code happens to touch the ORM first.
from app.models.document import Document  # noqa: F401


class DocumentReplica(Base):
    """The state of one document's copy in a secondary backend."""

    __tablename__ = "document_replicas"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    #: "s3" today. Named rather than assumed so a second replica target does
    #: not require a schema change.
    backend: Mapped[str] = mapped_column(String(16), nullable=False, default="s3")

    #: The key in the secondary backend. Identical to the primary key in
    #: practice, but stored explicitly: a backend with different key rules
    #: must not silently break the mapping.
    storage_key: Mapped[str | None] = mapped_column(String(512))

    state: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", index=True,
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)

    #: What was actually read back from the replica. Kept alongside — never
    #: instead of — `Document.content_hash`, so a mismatch retains both the
    #: expected and the observed value.
    verified_sha256: Mapped[str | None] = mapped_column(String(64))
    size_bytes: Mapped[int | None] = mapped_column(Integer)

    replicated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )

    __table_args__ = (
        # One row per document per backend. Also what makes "already
        # replicated?" a single indexed lookup on every upload.
        UniqueConstraint("document_id", "backend", name="uq_replica_doc_backend"),
        Index("ix_replica_state_attempts", "state", "attempts"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<DocumentReplica doc={self.document_id} {self.backend} {self.state}>"
