"""Storage health: the dashboard, the alerts, and the promotion assessment.

One service produces all three so they cannot disagree. A dashboard that says
"healthy" while an alert fires is worse than either alone, because it teaches
an operator to distrust both.

Alerts are deduplicated on a topic-and-level basis within a cooldown window.
Replication runs every ten minutes, and a volume that is 85% full will still
be 85% full on the next pass; notifying every ten minutes would train the
recipient to filter the channel, which is the failure mode that makes alerting
worse than useless.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog
from sqlalchemy import desc, func, select

from app.domain.documents.replication import (
    HealthLevel, HealthSignal, PROMOTION_MIN_DAYS, ReplicationState,
    assess_promotion, assess_replication, assess_volume,
)
from app.models.document import Document
from app.models.replication import DocumentReplica

log = structlog.get_logger(__name__)

#: How long the same alert stays suppressed after being raised.
ALERT_COOLDOWN = timedelta(hours=6)

#: Notification topic prefix, so these can be filtered as a family.
ALERT_TOPIC = "storage.health"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class StorageHealth:
    signals: list[HealthSignal]
    volume: dict[str, Any]
    object_storage: dict[str, Any]
    replication: dict[str, Any]
    generated_at: str

    @property
    def level(self) -> HealthLevel:
        if any(s.level is HealthLevel.CRITICAL for s in self.signals):
            return HealthLevel.CRITICAL
        if any(s.level is HealthLevel.WARNING for s in self.signals):
            return HealthLevel.WARNING
        return HealthLevel.OK

    @property
    def alerting(self) -> list[HealthSignal]:
        return [s for s in self.signals if s.is_alerting]

    def as_dict(self) -> dict[str, Any]:
        return {
            "level": self.level.value,
            "generated_at": self.generated_at,
            "volume": self.volume,
            "object_storage": self.object_storage,
            "replication": self.replication,
            "signals": [s.as_dict() for s in self.signals],
            "alerting": [s.name for s in self.alerting],
        }


class StorageHealthService:
    """Assembles the storage health picture and raises alerts from it."""

    def __init__(self, db: Any) -> None:
        self.db = db

    # -------------------------------------------------------------- volume
    def volume_stats(self) -> dict[str, Any]:
        from app.core.config import settings
        from app.services.documents.storage import free_disk_bytes

        path = settings.DOCUMENT_STORAGE_PATH
        used_bytes = int(self.db.scalar(
            select(func.coalesce(func.sum(Document.size_bytes), 0))
        ) or 0)

        free = 0
        total = 0
        try:
            import shutil
            from pathlib import Path

            usage = shutil.disk_usage(Path(path).expanduser().resolve())
            free, total = usage.free, usage.total
        except Exception:  # noqa: BLE001 — absent on a dev machine
            log.debug("volume stat unavailable", path=path)

        return {
            "path": path,
            "authoritative": True,
            "documents_bytes": used_bytes,
            "documents_mb": round(used_bytes / (1024 * 1024), 2),
            "disk_total_bytes": total,
            "disk_free_bytes": free,
            "disk_used_bytes": max(0, total - free) if total else 0,
            "disk_used_percent": (
                round((total - free) / total * 100, 1) if total else None
            ),
        }

    # ------------------------------------------------------ object storage
    def object_storage_stats(self) -> dict[str, Any]:
        from app.services.documents.replication import ReplicationService

        service = ReplicationService(self.db)
        configured = service.enabled
        reachable: bool | None = None
        detail = "object storage is not configured"

        if configured:
            # A cheap liveness probe. `exists` on a key that will never be
            # present exercises auth and networking without writing anything.
            try:
                service.secondary.exists("_healthcheck/probe")
                reachable, detail = True, "reachable"
            except Exception as exc:  # noqa: BLE001
                reachable, detail = False, f"unreachable: {exc}"[:200]

        replicated = service.replicated_bytes()
        return {
            "configured": configured,
            "reachable": reachable,
            "detail": detail,
            "role": "secondary replica",
            "replicated_bytes": replicated,
            "replicated_mb": round(replicated / (1024 * 1024), 2),
        }

    # --------------------------------------------------------- replication
    def replication_stats(self) -> dict[str, Any]:
        from app.services.documents.replication import ReplicationService

        service = ReplicationService(self.db)
        counts = service.counts()
        last = service.last_success()
        total = counts.get("total_documents", 0)
        verified = counts.get(ReplicationState.VERIFIED.value, 0)
        return {
            "queue_depth": counts.get(ReplicationState.PENDING.value, 0),
            "in_flight": counts.get(ReplicationState.REPLICATING.value, 0),
            "verified": verified,
            "failures": counts.get(ReplicationState.FAILED.value, 0),
            "mismatches": counts.get(ReplicationState.MISMATCH.value, 0),
            "skipped": counts.get(ReplicationState.SKIPPED.value, 0),
            "total_documents": total,
            "coverage_percent": (
                round(verified / total * 100, 1) if total else 0.0
            ),
            "last_successful_replication": last.isoformat() if last else None,
        }

    # -------------------------------------------------------------- health
    def health(self) -> StorageHealth:
        volume = self.volume_stats()
        objects = self.object_storage_stats()
        replication = self.replication_stats()

        signals: list[HealthSignal] = []
        if volume["disk_total_bytes"]:
            signals.append(assess_volume(volume["disk_used_bytes"],
                                         volume["disk_total_bytes"]))

        # Assess replication whenever there is anything to assess. Gating this
        # on `configured` was wrong: a mismatch already recorded in the
        # database is a correctness incident regardless of whether a bucket
        # is reachable *now*, and suppressing it meant a CRITICAL signal
        # silently downgraded to OK the moment credentials were removed.
        has_history = any((
            replication["verified"], replication["failures"],
            replication["mismatches"], replication["skipped"],
        ))
        if objects["configured"] or has_history:
            signals.extend(assess_replication(
                pending=replication["queue_depth"],
                failed=replication["failures"],
                mismatched=replication["mismatches"],
                last_success=_parse(replication["last_successful_replication"]),
            ))
        else:
            # With no bucket there is nothing to replicate to, so "no
            # successful replication yet" is a statement of configuration,
            # not a fault. Warning on it would put the platform permanently in
            # a degraded state for a condition nobody can act on, which is how
            # an alerting channel stops being read.
            signals.append(HealthSignal(
                "replication_queue", HealthLevel.OK,
                f"{replication['queue_depth']} document(s) awaiting a "
                f"replication target",
                replication["queue_depth"],
            ))

        if objects["configured"]:
            signals.append(HealthSignal(
                "object_storage",
                HealthLevel.OK if objects["reachable"] else HealthLevel.CRITICAL,
                objects["detail"], objects["reachable"],
            ))
        else:
            # Not an alert. The platform is designed to run without a bucket,
            # and it is the current, expected state.
            signals.append(HealthSignal(
                "object_storage", HealthLevel.OK,
                "not configured — volume-only operation", False,
            ))

        return StorageHealth(
            signals=signals, volume=volume, object_storage=objects,
            replication=replication, generated_at=_utcnow().isoformat(),
        )

    # -------------------------------------------------------------- alerts
    def raise_alerts(self) -> int:
        """Notify admins of anything alerting, respecting the cooldown."""
        from app.models.platform import Notification

        health = self.health()
        raised = 0
        for signal in health.alerting:
            topic = f"{ALERT_TOPIC}.{signal.name}"
            if self._recently_alerted(topic, signal.level):
                continue
            for user_id in self._admins():
                self.db.add(Notification(
                    user_id=user_id, channel="in_app", topic=topic,
                    subject=f"[{signal.level.value.upper()}] storage: "
                            f"{signal.name.replace('_', ' ')}"[:240],
                    body=(
                        f"{signal.detail}\n\n"
                        f"Volume: {health.volume['disk_used_percent']}% used, "
                        f"{health.volume['documents_mb']} MB of documents.\n"
                        f"Replication: {health.replication['verified']} verified, "
                        f"{health.replication['queue_depth']} queued, "
                        f"{health.replication['failures']} failed, "
                        f"{health.replication['mismatches']} mismatched.\n"
                        f"The volume remains the authoritative copy."
                    ),
                    link="/admin/storage",
                ))
                raised += 1
        if raised:
            self.db.commit()
            log.warning("storage alerts raised", count=raised,
                        signals=[s.name for s in health.alerting])
        return raised

    def _recently_alerted(self, topic: str, level: HealthLevel) -> bool:
        from app.models.platform import Notification

        cutoff = _utcnow() - ALERT_COOLDOWN
        recent = self.db.scalar(
            select(Notification)
            .where(Notification.topic == topic,
                   Notification.created_at >= cutoff)
            .order_by(desc(Notification.created_at))
            .limit(1)
        )
        if recent is None:
            return False
        # An escalation from warning to critical always notifies, cooldown or
        # not: the situation has materially changed.
        return level.value.upper() in (recent.subject or "").upper()

    def _admins(self) -> list[str]:
        try:
            from app.models.platform import User

            rows = self.db.execute(
                select(User.id).where(
                    User.role.in_(("super_admin", "admin", "tenant_admin"))
                )
            ).all()
            return [r[0] for r in rows]
        except Exception:  # noqa: BLE001
            log.debug("admin lookup unavailable for storage alerts")
            return []

    # ----------------------------------------------------------- promotion
    def promotion_readiness(self) -> dict[str, Any]:
        """Is object storage ready to become the primary?

        Purely mechanical. The decision to trust a beta service with the only
        copy of a research corpus should not rest on anybody's impression that
        it has been fine.
        """
        from app.services.documents.replication import ReplicationService

        service = ReplicationService(self.db)
        replication = self.replication_stats()
        since = service.clean_since()
        clean_days = 0.0
        if since is not None:
            if since.tzinfo is None:
                since = since.replace(tzinfo=timezone.utc)
            clean_days = (_utcnow() - since).total_seconds() / 86400.0

        total = replication["total_documents"]
        unreplicated = max(0, total - replication["verified"])
        ready, criteria = assess_promotion(
            clean_days=clean_days,
            mismatches=replication["mismatches"],
            failures=replication["failures"],
            unreplicated=unreplicated,
            total=total,
        )
        return {
            "ready": ready,
            "required_days": PROMOTION_MIN_DAYS,
            "clean_days_observed": round(clean_days, 2),
            "clean_since": since.isoformat() if since else None,
            "criteria": [c.as_dict() for c in criteria],
            "replication": replication,
            "recommendation": (
                "All criteria met. Object storage may be promoted to primary."
                if ready else
                "Not ready. The volume must remain authoritative."
            ),
        }


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
