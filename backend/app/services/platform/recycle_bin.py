"""Recycle Bin service — generic soft-delete, restore and purge.

Every operation is audited. The stored ``payload`` is redacted before it is
persisted, so an admin panel rendering it back can never leak a credential.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.domain.platform.audit import AuditAction, redact
from app.models.recycle_bin import RecycleBin
from app.services.platform.audit_service import AuditService, RequestContext


class RecycleBinError(Exception):
    """Raised when a recycle-bin operation cannot be honoured."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RecycleBinService:
    """Moves resources into, and out of, the recycle bin."""

    def __init__(self, db: Session) -> None:
        self.db = db
        self.audit = AuditService(db)

    # ---------------------------------------------------------------- write
    def soft_delete(
        self,
        *,
        resource_type: str,
        resource_id: str,
        display_name: str | None,
        payload: dict[str, Any] | None = None,
        principal=None,
        context: RequestContext | None = None,
    ) -> RecycleBin:
        """Move a resource to the bin and audit the action."""
        existing = self.db.execute(
            select(RecycleBin)
            .where(RecycleBin.resource_type == resource_type)
            .where(RecycleBin.resource_id == resource_id)
            .where(RecycleBin.purged_at.is_(None))
        ).scalars().first()
        if existing is not None and existing.is_active:
            # Idempotent: a resource already soft-deleted stays soft-deleted.
            return existing

        safe_payload = redact(payload) if payload else None
        entry = RecycleBin(
            resource_type=resource_type,
            resource_id=resource_id,
            display_name=display_name,
            payload=safe_payload,
            deleted_by=principal.user_id if principal else None,
            deleted_by_email=principal.email if principal else None,
            deleted_at=_utcnow(),
        )
        self.db.add(entry)
        self.audit.record(
            AuditAction.RECYCLE_SOFT_DELETED,
            principal=principal,
            resource_type=resource_type,
            resource_id=resource_id,
            summary=f"Soft-deleted {resource_type} '{display_name or resource_id}'",
            context=context,
        )
        self.db.flush()
        return entry

    # ---------------------------------------------------------------- read
    def list(
        self,
        *,
        status: str | None = None,
        resource_type: str | None = None,
        search: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[list[RecycleBin], int]:
        """Active (not purged) bin entries, most recently deleted first."""
        base = select(RecycleBin).where(RecycleBin.purged_at.is_(None))
        count = select(func.count()).select_from(base.subquery())

        filters: list[Any] = []
        if status == "restored":
            filters.append(RecycleBin.restored_at.is_not(None))
        elif status == "active":
            filters.append(RecycleBin.restored_at.is_(None))
        if resource_type:
            filters.append(RecycleBin.resource_type == resource_type)
        if search:
            pattern = f"%{search.strip().lower()}%"
            filters.append(
                or_(
                    func.lower(RecycleBin.display_name).like(pattern),
                    func.lower(RecycleBin.resource_id).like(pattern),
                )
            )

        base = base.where(*filters) if filters else base
        total = self.db.execute(count.where(*filters)).scalar_one()
        rows = (
            self.db.execute(
                base.order_by(RecycleBin.deleted_at.desc())
                .offset(offset).limit(limit)
            )
            .scalars()
            .all()
        )
        return list(rows), total

    def get(self, entry_id: int) -> RecycleBin:
        entry = self.db.get(RecycleBin, entry_id)
        if entry is None:
            raise RecycleBinError(f"recycle bin entry {entry_id} not found")
        return entry

    # --------------------------------------------------------------- mutate
    def restore(
        self, entry_id: int, *, principal=None,
        context: RequestContext | None = None,
    ) -> RecycleBin:
        """Mark an entry restored and audit it."""
        entry = self.get(entry_id)
        if entry.is_purged:
            raise RecycleBinError("a purged entry cannot be restored")
        if entry.is_restored:
            return entry
        entry.restored_at = _utcnow()
        entry.restored_by = principal.user_id if principal else None
        self.audit.record(
            AuditAction.RECYCLE_RESTORED,
            principal=principal,
            resource_type=entry.resource_type,
            resource_id=entry.resource_id,
            summary=f"Restored {entry.resource_type} '{entry.display_name or entry.resource_id}' from the recycle bin",
            context=context,
        )
        self.db.flush()
        return entry

    def purge(
        self, entry_id: int, *, principal=None,
        context: RequestContext | None = None,
    ) -> RecycleBin:
        """Permanently delete an entry and audit it."""
        entry = self.get(entry_id)
        if entry.is_purged:
            return entry
        entry.purged_at = _utcnow()
        entry.purged_by = principal.user_id if principal else None
        self.audit.record(
            AuditAction.RECYCLE_PURGED,
            principal=principal,
            resource_type=entry.resource_type,
            resource_id=entry.resource_id,
            summary=f"Permanently purged {entry.resource_type} '{entry.display_name or entry.resource_id}'",
            context=context,
        )
        self.db.flush()
        return entry

    def purge_all(
        self, *, resource_type: str | None = None, principal=None,
        context: RequestContext | None = None,
    ) -> int:
        """Purge every active entry (optionally of one resource type)."""
        stmt = select(RecycleBin).where(RecycleBin.purged_at.is_(None))
        if resource_type:
            stmt = stmt.where(RecycleBin.resource_type == resource_type)
        rows = self.db.execute(stmt).scalars().all()
        now = _utcnow()
        for entry in rows:
            entry.purged_at = now
            entry.purged_by = principal.user_id if principal else None
        self.audit.record(
            AuditAction.RECYCLE_PURGED_ALL,
            principal=principal,
            resource_type=resource_type,
            summary=f"Purged {len(rows)} item(s) from the recycle bin"
                    + (f" ({resource_type})" if resource_type else ""),
            context=context,
        )
        self.db.flush()
        return len(rows)
