"""Authentication API failure contracts not covered by the happy-path suite."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.db.base import Base, get_db
from app.main import app
from app.models.platform import Tenant, User
from app.services.platform.identity_service import IdentityService

PASSWORD = "CorrectHorseBattery1"


@pytest.fixture()
def client():
    engine = create_engine("sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as db:
        tenant = Tenant(name="Auth Tests", slug="auth-tests")
        db.add(tenant)
        db.commit()
        IdentityService(db).register(email="active@example.com", password=PASSWORD, name="Active", tenant_id=tenant.id, auto_verify=True)
        disabled, _ = IdentityService(db).register(email="disabled@example.com", password=PASSWORD, name="Disabled", tenant_id=tenant.id, auto_verify=True)
        disabled.status = "suspended"
        db.commit()

    def override():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    old_native, old_override = settings.NATIVE_AUTH, app.dependency_overrides.get(get_db)
    settings.NATIVE_AUTH = True
    app.dependency_overrides[get_db] = override
    try:
        yield TestClient(app)
    finally:
        settings.NATIVE_AUTH = old_native
        if old_override is None:
            app.dependency_overrides.pop(get_db, None)
        else:
            app.dependency_overrides[get_db] = old_override
        engine.dispose()


def test_invalid_credentials_are_not_accepted(client):
    response = client.post("/api/v1/auth/login", json={"email": "active@example.com", "password": "wrong-password"})
    assert response.status_code == 401


def test_disabled_account_cannot_create_session(client):
    response = client.post("/api/v1/auth/login", json={"email": "disabled@example.com", "password": PASSWORD})
    assert response.status_code == 401


def test_refresh_requires_a_cookie_or_token(client):
    response = client.post("/api/v1/auth/refresh")
    assert response.status_code == 401
    assert "No session" in response.json()["detail"]


def test_invalid_verification_and_reset_tokens_are_rejected(client):
    verify = client.post("/api/v1/auth/verify-email", json={"token": "expired-or-invalid"})
    reset = client.post("/api/v1/auth/password-reset/confirm", json={"token": "expired-or-invalid", "password": "AnotherCorrectPassword1!"})
    assert verify.status_code == 400
    assert reset.status_code == 400


def test_unconfigured_oauth_provider_fails_before_redirect(client):
    response = client.get("/api/v1/auth/oauth/google")
    assert response.status_code == 400
    assert "not configured" in response.json()["detail"]
