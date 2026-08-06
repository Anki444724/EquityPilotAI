"""Phase 7 — User Management & Subscription Center."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.db.base import Base, get_db
from app.domain.platform.identity import Role
from app.domain.platform.plans import PlanTier
from app.main import app
from app.services.platform.entitlements import EntitlementService
from app.services.platform.identity_service import IdentityService
from app.services.platform.tenancy import TenantService

import app.models as _models  # noqa: F401


PASSWORD = "a-strong-test-password-1"


@pytest.fixture(scope="module")
def ctx():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    with Session() as db:
        EntitlementService(db).sync_catalogue()
        tenant = TenantService(db).create("Alpha Capital", tier=PlanTier.PROFESSIONAL)
        IdentityService(db).register(
            email="admin@alpha.com", password=PASSWORD, name="Admin",
            tenant_id=tenant.id, role=Role.SUPER_ADMIN, auto_verify=True,
        )
        target = IdentityService(db).register(
            email="target@alpha.com", password=PASSWORD, name="Target",
            tenant_id=tenant.id, role=Role.ANALYST, auto_verify=True,
        )
        target_id = target[0].id
        tenant_id = tenant.id

    def _override():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    prev_override = app.dependency_overrides.get(get_db)
    prev_native = settings.NATIVE_AUTH
    app.dependency_overrides[get_db] = _override
    settings.NATIVE_AUTH = True

    client = TestClient(app)
    login = client.post(
        "/api/v1/auth/login", json={"email": "admin@alpha.com", "password": PASSWORD},
    )
    assert login.status_code == 200, login.text
    client.headers.update({"Authorization": f"Bearer {login.json()['access_token']}"})

    yield client, target_id, tenant_id

    settings.NATIVE_AUTH = prev_native
    if prev_override is not None:
        app.dependency_overrides[get_db] = prev_override
    else:
        app.dependency_overrides.pop(get_db, None)
    engine.dispose()


class TestUsers:
    def test_list_users(self, ctx):
        client, _, _ = ctx
        r = client.get("/api/v1/admin/users")
        assert r.status_code == 200, r.text
        assert r.json()["total"] >= 2

    def test_user_detail(self, ctx):
        client, uid, _ = ctx
        r = client.get(f"/api/v1/admin/users/{uid}")
        assert r.status_code == 200, r.text
        assert r.json()["email"] == "target@alpha.com"
        assert "permissions" in r.json()

    def test_suspend_restore(self, ctx):
        client, uid, _ = ctx
        r = client.post(f"/api/v1/admin/users/{uid}/suspend")
        assert r.json()["status"] == "suspended"
        r = client.post(f"/api/v1/admin/users/{uid}/restore")
        assert r.json()["status"] == "active"

    def test_roles_endpoint(self, ctx):
        client, _, _ = ctx
        r = client.get("/api/v1/admin/users/roles")
        assert r.status_code == 200
        assert {"super_admin", "admin", "analyst"} <= {x["key"] for x in r.json()["roles"]}


class TestSubscriptions:
    def test_upgrade_downgrade(self, ctx):
        client, uid, tid = ctx
        r = client.post(f"/api/v1/admin/users/{uid}/subscription",
                        params={"tier": "enterprise"})
        assert r.status_code == 200, r.text
        assert r.json()["plan_tier"] == "enterprise"
        r = client.post(f"/api/v1/admin/users/{uid}/subscription",
                        params={"tier": "free"})
        assert r.json()["plan_tier"] == "free"

    def test_extend(self, ctx):
        client, uid, _ = ctx
        r = client.post(f"/api/v1/admin/users/{uid}/subscription/extend", params={"days": 30})
        assert r.status_code == 200, r.text


class TestPayments:
    def test_issue_pay_refund_invoice(self, ctx):
        client, uid, _ = ctx
        r = client.post(f"/api/v1/admin/users/{uid}/invoices",
                        params={"plan_tier": "pro", "amount_paise": 99900})
        assert r.status_code == 200, r.text
        inv_id = r.json()["id"]
        assert r.json()["total_paise"] == 99900

        r = client.post(f"/api/v1/admin/users/{uid}/invoices/{inv_id}/pay")
        assert r.json()["status"] == "paid"

        r = client.post(f"/api/v1/admin/users/{uid}/invoices/{inv_id}/refund")
        assert r.json()["status"] == "refunded"


class TestSecurity:
    def test_security_status(self, ctx):
        client, uid, _ = ctx
        r = client.get(f"/api/v1/admin/users/{uid}/security")
        assert r.status_code == 200
        assert "mfa_ready" in r.json()
        assert "email_verified" in r.json()

    def test_login_history(self, ctx):
        client, uid, _ = ctx
        r = client.get(f"/api/v1/admin/users/{uid}/login-history")
        assert r.status_code == 200
        assert isinstance(r.json()["items"], list)


class TestNotifications:
    def test_notify_user(self, ctx):
        client, uid, _ = ctx
        r = client.post(f"/api/v1/admin/users/{uid}/notify",
                        params={"channel": "email", "subject": "Hello", "body": "World"})
        assert r.status_code == 200, r.text
        assert r.json()["subject"] == "Hello"

    def test_announce(self, ctx):
        client, _, _ = ctx
        r = client.post("/api/v1/admin/users/announce",
                        params={"subject": "New feature", "body": "Check it out"})
        assert r.status_code == 200


class TestAnalytics:
    def test_analytics(self, ctx):
        client, _, _ = ctx
        r = client.get("/api/v1/admin/users/analytics/summary", params={"days": 30})
        assert r.status_code == 200, r.text
        body = r.json()
        assert "total_users" in body
        assert "revenue_inr" in body
        assert "retention_pct" in body
