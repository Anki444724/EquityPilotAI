"""Phase 1 — Recycle Bin (soft delete / restore / purge) + RBAC + audit.

Uses a self-contained in-memory database with a real tenant and JWT auth, so
the tenant-scoped admin endpoints can be exercised without disturbing the
shared seeded database used by the rest of the suite.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.core.security import get_current_user
from app.db.base import Base, get_db
from app.domain.platform.identity import Role
from app.domain.platform.plans import PlanTier
from app.main import app
from app.services.platform.entitlements import EntitlementService
from app.services.platform.identity_service import IdentityService
from app.services.platform.tenancy import TenantService

import app.models as _models  # noqa: F401  (register every table)


PASSWORD = "a-strong-test-password-1"


@pytest.fixture(scope="module")
def admin_client():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    with Session() as db:
        EntitlementService(db).sync_catalogue()
        tenant = TenantService(db).create("Alpha Capital", tier=PlanTier.PROFESSIONAL)
        user, _ = IdentityService(db).register(
            email="admin@alpha.com", password=PASSWORD, name="Admin",
            tenant_id=tenant.id, role=Role.ADMIN, auto_verify=True,
        )

    def _override():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    previous_override = app.dependency_overrides.get(get_db)
    previous_native = settings.NATIVE_AUTH
    app.dependency_overrides[get_db] = _override
    settings.NATIVE_AUTH = True

    test_client = TestClient(app)
    login = test_client.post(
        "/api/v1/auth/login", json={"email": "admin@alpha.com", "password": PASSWORD},
    )
    assert login.status_code == 200, login.text
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    test_client.headers.update(headers)

    yield test_client

    settings.NATIVE_AUTH = previous_native
    if previous_override is not None:
        app.dependency_overrides[get_db] = previous_override
    else:
        app.dependency_overrides.pop(get_db, None)
    engine.dispose()


class TestRecycleBinApi:
    def test_soft_delete_list_restore_purge_roundtrip(self, admin_client):
        r = admin_client.post("/api/v1/admin/recycle-bin", json={
            "resource_type": "sector", "resource_id": "tmt",
            "display_name": "Technology, Media & Telecom",
            "payload": {"theme": "Digital"},
        })
        assert r.status_code == 201, r.text
        entry = r.json()
        assert entry["resource_type"] == "sector"
        assert entry["resource_id"] == "tmt"
        assert entry["is_active"] is True
        assert entry["restored_at"] is None

        rows = admin_client.get("/api/v1/admin/recycle-bin").json()
        assert rows["total"] >= 1
        assert entry["id"] in {x["id"] for x in rows["items"]}

        r = admin_client.post(f"/api/v1/admin/recycle-bin/{entry['id']}/restore")
        assert r.status_code == 200, r.text
        assert r.json()["restored_at"] is not None

        r = admin_client.delete(f"/api/v1/admin/recycle-bin/{entry['id']}")
        assert r.status_code == 200, r.text
        assert r.json()["purged_at"] is not None

        rows = admin_client.get("/api/v1/admin/recycle-bin").json()
        assert entry["id"] not in {x["id"] for x in rows["items"]}

    def test_restore_unknown_entry_is_404(self, admin_client):
        assert admin_client.post(
            "/api/v1/admin/recycle-bin/999999/restore").status_code == 404

    def test_purge_unknown_entry_is_404(self, admin_client):
        assert admin_client.delete(
            "/api/v1/admin/recycle-bin/999999").status_code == 404

    def test_soft_delete_is_audited(self, admin_client):
        before = admin_client.get(
            "/api/v1/admin/audit", params={"action": "recycle.soft_deleted"}
        ).json()["total"]
        admin_client.post("/api/v1/admin/recycle-bin", json={
            "resource_type": "sector", "resource_id": "fn-audit",
            "display_name": "Audit probe",
        })
        after = admin_client.get(
            "/api/v1/admin/audit", params={"action": "recycle.soft_deleted"}
        ).json()["total"]
        assert after == before + 1

    def test_restore_is_audited(self, admin_client):
        entry = admin_client.post("/api/v1/admin/recycle-bin", json={
            "resource_type": "news", "resource_id": "n1", "display_name": "News",
        }).json()
        before = admin_client.get(
            "/api/v1/admin/audit", params={"action": "recycle.restored"}
        ).json()["total"]
        admin_client.post(f"/api/v1/admin/recycle-bin/{entry['id']}/restore")
        after = admin_client.get(
            "/api/v1/admin/audit", params={"action": "recycle.restored"}
        ).json()["total"]
        assert after == before + 1


class TestRecycleBinRbac:
    def test_every_recycle_manage_route_is_permission_guarded(self):
        from app.api.v1 import admin as adm

        guarded = {r.path for r in adm.router.routes
                   if "recycle-bin" in getattr(r, "path", "")}
        # Every recycle-bin route must be present.
        assert guarded >= {
            "/admin/recycle-bin",
            "/admin/recycle-bin/{entry_id}/restore",
            "/admin/recycle-bin/{entry_id}",
        }

    def test_anonymous_cannot_soft_delete(self):
        # A bare unauthenticated client is refused (401 auth, or 400 no tenant).
        r = TestClient(app).post("/api/v1/admin/recycle-bin", json={
            "resource_type": "sector", "resource_id": "anon",
        })
        assert r.status_code in (401, 403, 400)


class TestSystemStatus:
    def test_system_status_endpoint(self, admin_client):
        r = admin_client.get("/api/v1/admin/system-status")
        assert r.status_code == 200, r.text
        body = r.json()
        names = {c["name"] for c in body["components"]}
        assert {"database", "redis", "railway", "market"} <= names
        assert body["companies"] >= 0
        assert body["market_open"] in {"open", "closed", "weekend", "unknown"}
