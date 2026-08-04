"""Storage-health service contracts using only injected storage boundaries."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.domain.documents.replication import HealthLevel, HealthSignal
from app.models.platform import Notification, User
from app.services.documents import storage_health as health_module
from app.services.documents.storage_health import StorageHealth, StorageHealthService


def signal(name: str, level: HealthLevel, alerting: bool = False) -> HealthSignal:
    # HealthSignal derives alerting from level; use real domain objects so the
    # dashboard serializer is exercised exactly as production uses it.
    return HealthSignal(name, level, f"{name} detail", 1 if alerting else 0)


class FakeReplication:
    enabled = False
    secondary = None
    counts_value = {"total_documents": 0}
    last_value = None
    bytes_value = 0
    clean_value = None

    def __init__(self, db):
        self.db = db

    def counts(self): return dict(self.counts_value)
    def last_success(self): return self.last_value
    def replicated_bytes(self): return self.bytes_value
    def clean_since(self): return self.clean_value


@pytest.fixture()
def service(monkeypatch):
    engine = create_engine("sqlite://")
    from app.db.base import Base
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    monkeypatch.setattr("app.services.documents.replication.ReplicationService", FakeReplication)
    yield StorageHealthService(db)
    db.close()
    engine.dispose()


def test_storage_health_serialises_severity_and_alerts():
    warning = signal("queue", HealthLevel.WARNING, True)
    critical = signal("storage", HealthLevel.CRITICAL, True)
    result = StorageHealth([warning, critical], {"x": 1}, {}, {}, "now")
    assert result.level is HealthLevel.CRITICAL
    assert result.alerting == [warning, critical]
    assert result.as_dict()["alerting"] == ["queue", "storage"]
    assert result.as_dict()["level"] == "critical"


def test_volume_stats_handles_real_disk_probe_failure(service, monkeypatch):
    monkeypatch.setattr("shutil.disk_usage", lambda path: (_ for _ in ()).throw(OSError("unavailable")))
    stats = service.volume_stats()
    assert stats["authoritative"] is True
    assert stats["disk_total_bytes"] == 0
    assert stats["disk_used_percent"] is None


def test_object_storage_disabled_and_unreachable_paths(service):
    FakeReplication.enabled = False
    FakeReplication.bytes_value = 2 * 1024 * 1024
    disabled = service.object_storage_stats()
    assert disabled["reachable"] is None
    assert disabled["replicated_mb"] == 2.0

    class BrokenSecondary:
        def exists(self, key): raise RuntimeError("credentials rejected")
    FakeReplication.enabled = True
    FakeReplication.secondary = BrokenSecondary()
    failed = service.object_storage_stats()
    assert failed["reachable"] is False
    assert "credentials rejected" in failed["detail"]


def test_replication_stats_handles_empty_and_populated_totals(service):
    FakeReplication.counts_value = {"total_documents": 0}
    assert service.replication_stats()["coverage_percent"] == 0.0
    FakeReplication.counts_value = {"total_documents": 4, "verified": 3, "pending": 1, "failed": 0, "mismatch": 0, "skipped": 0, "replicating": 0}
    FakeReplication.last_value = datetime(2026, 1, 1, tzinfo=timezone.utc)
    stats = service.replication_stats()
    assert stats["coverage_percent"] == 75.0
    assert stats["last_successful_replication"].startswith("2026-01-01")


def test_health_marks_unreachable_configured_storage_critical(service, monkeypatch):
    service.volume_stats = lambda: {"disk_total_bytes": 100, "disk_used_bytes": 90}
    service.object_storage_stats = lambda: {"configured": True, "reachable": False, "detail": "down"}
    service.replication_stats = lambda: {"queue_depth": 0, "verified": 0, "failures": 0, "mismatches": 0, "skipped": 0, "last_successful_replication": None}
    result = service.health()
    assert result.level is HealthLevel.CRITICAL
    assert any(s.name == "object_storage" and s.level is HealthLevel.CRITICAL for s in result.signals)


def test_health_without_target_reports_non_alerting_queue(service):
    service.volume_stats = lambda: {"disk_total_bytes": 0, "disk_used_bytes": 0}
    service.object_storage_stats = lambda: {"configured": False, "reachable": None, "detail": "not configured"}
    service.replication_stats = lambda: {"queue_depth": 2, "verified": 0, "failures": 0, "mismatches": 0, "skipped": 0, "last_successful_replication": None}
    result = service.health()
    assert result.level is HealthLevel.OK
    assert result.alerting == []


def test_alerts_notify_admins_once_and_respect_cooldown(service):
    service.db.add_all([
        User(id="admin", tenant_id="tenant", email="admin@example.com", name="Admin", role="admin", status="active"),
        User(id="reader", tenant_id="tenant", email="reader@example.com", name="Reader", role="read_only", status="active"),
    ])
    service.db.commit()
    alert = signal("object_storage", HealthLevel.CRITICAL, True)
    service.health = lambda: StorageHealth([alert], {"disk_used_percent": 90, "documents_mb": 1}, {}, {"verified": 0, "queue_depth": 1, "failures": 0, "mismatches": 0}, "now")
    assert service.raise_alerts() == 1
    assert service.db.query(Notification).count() == 1
    assert service.raise_alerts() == 0


def test_promotion_readiness_normalises_naive_timestamp_and_reports_not_ready(service):
    FakeReplication.clean_value = datetime.now() - timedelta(days=2)
    service.replication_stats = lambda: {"total_documents": 10, "verified": 8, "mismatches": 1, "failures": 0}
    report = service.promotion_readiness()
    assert report["clean_since"].endswith("+00:00")
    assert report["ready"] is False
    assert "volume must remain authoritative" in report["recommendation"]
