"""Recycle Bin — generic soft-delete ledger.

A row here records that a resource was soft-deleted and holds a redacted
snapshot of it so an admin can review, restore or permanently purge it. It is
deliberately generic over ``resource_type`` so any entity (a company, a sector,
a news item, a user) can be recycled without a schema change per entity.

Semantics:
* ``deleted_at`` is set when the resource is moved to the bin.
* ``restored_at`` is set when an admin restores it (the entry is kept for the
  audit trail, but is no longer an active item).
* ``purged_at`` is set when an admin permanently deletes it; purged entries are
  excluded from every list query.

The snapshot payload is redacted (credentials removed) before it is stored —
the same discipline the audit trail enforces — so nothing sensitive leaks into
a field that an admin panel renders back verbatim.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RecycleBin(Base):
    """One soft-deleted resource awaiting review, restore or purge."""

    __tablename__ = "recycle_bin"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    #: The kind of resource, e.g. ``"company"``, ``"news"``, ``"sector"``.
    resource_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    #: The resource's own identifier (a UUID, a numeric id, a slug).
    resource_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    #: Human-readable label for the list view (e.g. a company ticker).
    display_name: Mapped[str | None] = mapped_column(String(200))
    #: Redacted snapshot of the deleted resource, for restore and review.
    payload: Mapped[dict | None] = mapped_column(JSON)

    deleted_by: Mapped[str | None] = mapped_column(String(36))
    deleted_by_email: Mapped[str | None] = mapped_column(String(254))
    deleted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False, index=True,
    )

    restored_by: Mapped[str | None] = mapped_column(String(36))
    restored_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None,
    )

    purged_by: Mapped[str | None] = mapped_column(String(36))
    purged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None,
    )

    @property
    def is_active(self) -> bool:
        """Still in the bin (not restored, not purged)."""
        return self.restored_at is None and self.purged_at is None

    @property
    def is_restored(self) -> bool:
        return self.restored_at is not None

    @property
    def is_purged(self) -> bool:
        return self.purged_at is not None
