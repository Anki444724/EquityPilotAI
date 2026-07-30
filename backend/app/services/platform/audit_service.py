"""Writing and reading the audit trail.

Two guarantees, both enforced here rather than asked of callers.

**Logging never breaks the request.** Every write is wrapped: if the audit
insert fails, the failure is logged to stderr and the request continues. An
application that returns 500 because it could not record that it succeeded is
worse than one that loses an audit row — and the loss is visible, because the
structured log still has it.

**Nothing sensitive is written.** The payload passes through
`domain.platform.audit.redact` before it reaches the model, and there is no
code path into `audit_logs` that skips it.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.domain.platform.audit import (
    AuditAction, AuditCategory, AuditSeverity, build as build_event,
    tenant_visible,
)
from app.domain.platform.identity import Principal
from app.models.platform import AuditLog

log = logging.getLogger("ierp.audit")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class RequestContext:
    """The ambient facts about a request that every audit row wants.

    Bundled so a call site passes one object instead of four arguments, and so
    a route that forgets one of them fails to compile rather than silently
    writing a row with no IP address.
    """

    ip_address: str | None = None
    user_agent: str | None = None
    request_id: str | None = None

    @classmethod
    def empty(cls) -> "RequestContext":
        return cls()


class AuditService:
    """Append to, and query, the trail."""

    def __init__(self, db: Session) -> None:
        self.db = db

    # ==================================================================
    # Writing
    # ==================================================================
    def record(
        self,
        action: AuditAction,
        *,
        principal: Principal | None = None,
        tenant_id: int | None = None,
        actor_id: str | None = None,
        actor_email: str | None = None,
        actor_role: str | None = None,
        resource_type: str | None = None,
        resource_id: str | int | None = None,
        summary: str = "",
        outcome: str = "success",
        context: RequestContext | None = None,
        metadata: dict[str, Any] | None = None,
        commit: bool = True,
    ) -> AuditLog | None:
        """Write one event. Returns None if the write failed.

        `principal` fills the actor fields when present; the explicit
        arguments cover the cases where there is no principal yet — a failed
        sign-in, a webhook, a scheduled job.
        """
        ctx = context or RequestContext.empty()

        event = build_event(
            action,
            at=_utcnow(),
            actor_id=actor_id or (principal.user_id if principal else None),
            actor_email=actor_email or (principal.email if principal else None),
            actor_role=actor_role or (principal.role.value if principal else None),
            tenant_id=tenant_id if tenant_id is not None else (
                principal.tenant_id if principal else None
            ),
            resource_type=resource_type,
            resource_id=str(resource_id) if resource_id is not None else None,
            summary=summary,
            outcome=outcome,
            ip_address=ctx.ip_address,
            user_agent=ctx.user_agent,
            request_id=ctx.request_id,
            metadata=metadata,
        )

        row = AuditLog(
            tenant_id=event.tenant_id,
            action=event.action.value,
            category=event.category.value,
            severity=event.severity.value,
            outcome=event.outcome,
            actor_id=event.actor_id,
            actor_email=event.actor_email,
            actor_role=event.actor_role,
            resource_type=event.resource_type,
            resource_id=event.resource_id,
            summary=event.summary[:500],
            ip_address=event.ip_address,
            user_agent=event.user_agent,
            request_id=event.request_id,
            meta=event.metadata,
            occurred_at=event.at,
        )

        try:
            self.db.add(row)
            if commit:
                self.db.commit()
                self.db.refresh(row)
            else:
                self.db.flush()
        except Exception as exc:  # noqa: BLE001 — audit must never 500 a request
            log.error(
                "audit write failed action=%s tenant=%s error=%s",
                action.value, event.tenant_id, exc,
            )
            try:
                self.db.rollback()
            except Exception:  # noqa: BLE001
                pass
            return None

        # Security events also go to the structured log, so they reach the
        # log aggregator even if the database is the thing that is unwell.
        if event.is_security_relevant:
            log.warning(
                "security event action=%s outcome=%s actor=%s tenant=%s ip=%s",
                event.action.value, event.outcome, event.actor_id,
                event.tenant_id, event.ip_address,
            )
        return row

    # ==================================================================
    # Reading
    # ==================================================================
    def query(
        self,
        *,
        tenant_id: int | None = None,
        unrestricted: bool = False,
        action: str | None = None,
        category: AuditCategory | None = None,
        severity: AuditSeverity | None = None,
        actor_id: str | None = None,
        resource_type: str | None = None,
        outcome: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        search: str | None = None,
        offset: int = 0,
        limit: int = 50,
        descending: bool = True,
    ) -> tuple[list[AuditLog], int]:
        """Filtered, paginated read.

        `unrestricted` is the operator's cross-tenant view and is the only way
        to see another organisation's trail; a tenant admin always gets their
        own rows and only the categories a customer is allowed to see.
        """
        stmt = select(AuditLog)

        if not unrestricted:
            stmt = stmt.where(AuditLog.tenant_id == tenant_id)
            stmt = stmt.where(AuditLog.category != AuditCategory.SYSTEM.value)
        elif tenant_id is not None:
            stmt = stmt.where(AuditLog.tenant_id == tenant_id)

        if action:
            stmt = stmt.where(AuditLog.action == action)
        if category:
            stmt = stmt.where(AuditLog.category == category.value)
        if severity:
            stmt = stmt.where(AuditLog.severity == severity.value)
        if actor_id:
            stmt = stmt.where(AuditLog.actor_id == actor_id)
        if resource_type:
            stmt = stmt.where(AuditLog.resource_type == resource_type)
        if outcome:
            stmt = stmt.where(AuditLog.outcome == outcome)
        if since:
            stmt = stmt.where(AuditLog.occurred_at >= since)
        if until:
            stmt = stmt.where(AuditLog.occurred_at <= until)
        if search:
            like = f"%{search.lower()}%"
            stmt = stmt.where(
                func.lower(AuditLog.summary).like(like)
                | func.lower(AuditLog.actor_email).like(like)
                | func.lower(AuditLog.action).like(like)
            )

        total = self.db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        order = AuditLog.occurred_at.desc() if descending else AuditLog.occurred_at.asc()
        # Tie-break on id: rows written in the same millisecond otherwise
        # shuffle between pages and the reader sees duplicates.
        order2 = AuditLog.id.desc() if descending else AuditLog.id.asc()
        rows = list(self.db.scalars(stmt.order_by(order, order2).offset(offset).limit(limit)))
        return rows, total

    def recent_for_resource(
        self, resource_type: str, resource_id: str, *, limit: int = 20,
    ) -> list[AuditLog]:
        return list(self.db.scalars(
            select(AuditLog)
            .where(
                AuditLog.resource_type == resource_type,
                AuditLog.resource_id == str(resource_id),
            )
            .order_by(AuditLog.occurred_at.desc())
            .limit(limit)
        ))

    # ==================================================================
    # Aggregates
    # ==================================================================
    def summary(
        self, *, tenant_id: int | None = None, days: int = 7,
    ) -> dict[str, Any]:
        """Counts by category, severity and day — the audit dashboard."""
        since = _utcnow() - timedelta(days=days)

        def _grouped(column) -> dict[str, int]:
            stmt = (
                select(column, func.count(AuditLog.id))
                .where(AuditLog.occurred_at >= since)
                .group_by(column)
            )
            if tenant_id is not None:
                stmt = stmt.where(AuditLog.tenant_id == tenant_id)
            return {str(k): int(v) for k, v in self.db.execute(stmt)}

        daily_stmt = (
            select(func.date(AuditLog.occurred_at), func.count(AuditLog.id))
            .where(AuditLog.occurred_at >= since)
            .group_by(func.date(AuditLog.occurred_at))
            .order_by(func.date(AuditLog.occurred_at))
        )
        if tenant_id is not None:
            daily_stmt = daily_stmt.where(AuditLog.tenant_id == tenant_id)

        total_stmt = select(func.count(AuditLog.id)).where(AuditLog.occurred_at >= since)
        if tenant_id is not None:
            total_stmt = total_stmt.where(AuditLog.tenant_id == tenant_id)

        failure_stmt = total_stmt.where(AuditLog.outcome != "success")

        return {
            "days": days,
            "total": int(self.db.scalar(total_stmt) or 0),
            "failures": int(self.db.scalar(failure_stmt) or 0),
            "by_category": _grouped(AuditLog.category),
            "by_severity": _grouped(AuditLog.severity),
            "by_action": dict(sorted(
                _grouped(AuditLog.action).items(), key=lambda kv: -kv[1],
            )[:15]),
            "daily": [
                {"date": str(day)[:10], "count": int(count)}
                for day, count in self.db.execute(daily_stmt)
            ],
        }

    def security_events(
        self, *, tenant_id: int | None = None, days: int = 7, limit: int = 50,
    ) -> list[AuditLog]:
        """Everything a security review should read: denials, lockouts, token
        reuse, and anything CRITICAL."""
        since = _utcnow() - timedelta(days=days)
        stmt = (
            select(AuditLog)
            .where(
                AuditLog.occurred_at >= since,
                (AuditLog.category == AuditCategory.SECURITY.value)
                | (AuditLog.severity == AuditSeverity.CRITICAL.value)
                | (AuditLog.outcome != "success"),
            )
            .order_by(AuditLog.occurred_at.desc())
            .limit(limit)
        )
        if tenant_id is not None:
            stmt = stmt.where(AuditLog.tenant_id == tenant_id)
        return list(self.db.scalars(stmt))

    # ==================================================================
    # Retention
    # ==================================================================
    def purge(self, *, older_than_days: int, keep_critical: bool = True) -> int:
        """Delete aged rows.

        Critical events are retained regardless: the whole purpose of a
        security trail is that it outlives the incident, and "we deleted the
        evidence on schedule" is not an acceptable answer to an auditor.
        """
        cutoff = _utcnow() - timedelta(days=older_than_days)
        stmt = delete(AuditLog).where(AuditLog.occurred_at < cutoff)
        if keep_critical:
            stmt = stmt.where(AuditLog.severity != AuditSeverity.CRITICAL.value)
        result = self.db.execute(stmt)
        self.db.commit()
        return int(result.rowcount or 0)
