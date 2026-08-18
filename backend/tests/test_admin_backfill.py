"""API tests for the platform-operator financials backfill endpoints.

Builds its own application with native auth switched on and a real operator
(`SUPER_ADMIN`) plus an ordinary tenant admin, so it can assert both the happy
path and the isolation guarantee: the backfill is operator-only, and an
unprivileged caller is refused rather than seeing the console at all.
"""
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

PASSWORD = "CorrectHorseBattery1"
PREFIX = "/api/v1"


@pytest.fixture(scope="module")
def secured():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    with Session() as db:
        EntitlementService(db).sync_catalogue()
        tenants = TenantService(db)
        identity = IdentityService(db)

        alpha = tenants.create("Alpha Capital", tier=PlanTier.PROFESSIONAL)

        admin, _ = identity.register(
            email="admin@alpha.com", password=PASSWORD, name="Admin",
            tenant_id=alpha.id, role=Role.ADMIN, auto_verify=True,
        )
        operator, _ = identity.register(
            email="operator@ierp.io", password=PASSWORD, name="Operator",
            tenant_id=alpha.id, role=Role.SUPER_ADMIN, auto_verify=True,
        )
        _admin_id, operator_id = admin.id, operator.id

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

    def _login(email: str) -> dict[str, str]:
        response = test_client.post(
            f"{PREFIX}/auth/login",
            json={"email": email, "password": PASSWORD},
        )
        assert response.status_code == 200, response.text
        return {"Authorization": f"Bearer {response.json()['access_token']}"}

    yield {
        "client": test_client,
        "login": _login,
        "admin_headers": _login("admin@alpha.com"),
        "operator_headers": _login("operator@ierp.io"),
        "operator_id": operator_id,
        "session": Session,
    }

    settings.NATIVE_AUTH = previous_native
    if previous_override is not None:
        app.dependency_overrides[get_db] = previous_override
    else:
        app.dependency_overrides.pop(get_db, None)
    engine.dispose()


def test_status_returns_coverage_for_operator(secured):
    response = secured["client"].get(
        f"{PREFIX}/platform/financials/backfill",
        headers=secured["operator_headers"],
    )
    assert response.status_code == 200, response.text
    body = response.json()
    coverage = body["coverage"]
    assert set(coverage) == {
        "companies", "with_financials", "without_financials",
        "coverage_pct", "by_category",
    }
    # latest_job is null until a sweep has been enqueued and run.
    assert body["latest_job"] is None


def test_status_is_refused_anonymously(secured):
    response = secured["client"].get(f"{PREFIX}/platform/financials/backfill")
    assert response.status_code in (401, 403)


def test_enqueue_creates_a_financials_backfill_job(secured):
    from app.models.platform import BackgroundJob

    response = secured["client"].post(
        f"{PREFIX}/platform/financials/backfill",
        headers=secured["operator_headers"], json={"limit": 25},
    )
    assert response.status_code == 201, response.text
    job = response.json()
    assert job["kind"] == "financials_backfill"
    assert job["status"] == "queued"

    # JobOut does not carry the payload, so assert it from the database.
    with secured["session"]() as db:
        row = db.get(BackgroundJob, job["id"])
        assert row is not None
        assert row.kind == "financials_backfill"
        assert row.payload == {"limit": 25}


def test_enqueue_without_limit_bounds_nothing(secured):
    from app.models.platform import BackgroundJob

    response = secured["client"].post(
        f"{PREFIX}/platform/financials/backfill",
        headers=secured["operator_headers"], json={},
    )
    assert response.status_code == 201, response.text
    job = response.json()

    with secured["session"]() as db:
        row = db.get(BackgroundJob, job["id"])
        assert row.payload == {}


def test_enqueue_targets_tickers(secured):
    from app.models.platform import BackgroundJob

    response = secured["client"].post(
        f"{PREFIX}/platform/financials/backfill",
        headers=secured["operator_headers"], json={"tickers": ["NHPC"]},
    )
    assert response.status_code == 201, response.text
    job = response.json()
    assert job["kind"] == "financials_backfill"
    assert job["status"] == "queued"

    with secured["session"]() as db:
        row = db.get(BackgroundJob, job["id"])
        assert row.payload == {"tickers": ["NHPC"]}


def test_enqueue_normalises_and_dedupes_tickers(secured):
    from app.models.platform import BackgroundJob

    response = secured["client"].post(
        f"{PREFIX}/platform/financials/backfill",
        headers=secured["operator_headers"],
        json={"tickers": ["  nhpc  ", "NHPC", "tcs"]},
    )
    assert response.status_code == 201, response.text

    with secured["session"]() as db:
        row = db.get(BackgroundJob, response.json()["id"])
        assert row.payload == {"tickers": ["NHPC", "TCS"]}


def test_enqueue_rejects_a_zero_limit(secured):
    response = secured["client"].post(
        f"{PREFIX}/platform/financials/backfill",
        headers=secured["operator_headers"], json={"limit": 0},
    )
    assert response.status_code == 422


def test_enqueue_is_refused_for_a_tenant_admin(secured):
    """require_operator answers 404, not 403, so the operator console does not
    confirm its own existence to a customer who probes for it."""
    response = secured["client"].post(
        f"{PREFIX}/platform/financials/backfill",
        headers=secured["admin_headers"], json={},
    )
    assert response.status_code == 404
