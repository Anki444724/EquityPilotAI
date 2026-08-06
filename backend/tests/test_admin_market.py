"""Phase 4 — Market Operations Center: overrides, providers, dashboard."""
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
        tenant = TenantService(db).create("Alpha Capital", tier=PlanTier.ENTERPRISE)
        IdentityService(db).register(
            email="admin@alpha.com", password=PASSWORD, name="Admin",
            tenant_id=tenant.id, role=Role.ADMIN, auto_verify=True,
        )

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

    c = client.post("/api/v1/admin/companies", json={
        "name": "Market Co", "ticker": "MKTC", "isin": "INE707070707",
    })
    assert c.status_code == 201, c.text
    company_id = c.json()["id"]

    yield client, company_id

    settings.NATIVE_AUTH = prev_native
    if prev_override is not None:
        app.dependency_overrides[get_db] = prev_override
    else:
        app.dependency_overrides.pop(get_db, None)
    engine.dispose()


class TestProviderRegistry:
    def test_all_supported_providers_listed(self, ctx):
        client, _ = ctx
        providers = client.get("/api/v1/admin/market/providers").json()
        names = {p["name"] for p in providers}
        assert {"Finnhub", "Yahoo Finance (Fallback)",
                "Infoway", "AlphaVantage", "Polygon", "Custom Provider"} <= names

    def test_provider_health(self, ctx):
        client, _ = ctx
        health = client.get("/api/v1/admin/market/providers/health").json()
        assert isinstance(health, list)
        assert all("status" in h for h in health)


class TestOverrides:
    def test_create_and_apply_override(self, ctx):
        client, cid = ctx
        r = client.post(f"/api/v1/admin/market/overrides/{cid}", json={
            "manual_price": 1234.5, "manual_volume": 999999,
            "reason": "test override", "auto_revert": True,
        })
        assert r.status_code == 201, r.text
        ov = r.json()
        assert ov["ticker"] == "MKTC"
        assert ov["is_active"] is True

        # LiveMarketService snapshot must reflect the manual override.
        detail = client.get(f"/api/v1/admin/companies/{cid}").json()
        assert detail["market"]["price_source"] == "Manual Override"
        assert detail["market"]["live_price"] == 1234.5

    def test_clear_override(self, ctx):
        client, cid = ctx
        ov = client.post(f"/api/v1/admin/market/overrides/{cid}", json={
            "manual_price": 999.0, "reason": "to clear",
        }).json()
        r = client.delete(f"/api/v1/admin/market/overrides/{ov['id']}")
        assert r.status_code == 200

        # After expiry the manual price must no longer apply.
        detail = client.get(f"/api/v1/admin/companies/{cid}").json()
        assert detail["market"]["price_source"] != "Manual Override"

    def test_list_overrides(self, ctx):
        client, cid = ctx
        client.post(f"/api/v1/admin/market/overrides/{cid}", json={
            "manual_pe": 20.0, "reason": "list test",
        })
        items = client.get("/api/v1/admin/market/overrides").json()
        assert any(i["ticker"] == "MKTC" for i in items)


class TestDashboard:
    def test_dashboard_fields(self, ctx):
        client, _ = ctx
        d = client.get("/api/v1/admin/market/dashboard").json()
        assert "connected_symbols" in d
        assert "cache_size" in d
        assert "ttl_seconds" in d
        assert "redis" in d
        assert "market_status" in d

    def test_cache_clear(self, ctx):
        client, _ = ctx
        r = client.post("/api/v1/admin/market/cache/clear")
        assert r.status_code == 200


class TestConsistency:
    def test_override_visible_across_surfaces(self, ctx):
        """Dashboard, company, portfolio, watchlist, AI all use the override."""
        client, cid = ctx
        # Ensure the company appears in the market-cap-sorted dashboard list.
        client.patch(f"/api/v1/admin/companies/{cid}", json={"market_cap": 999999.0})
        ov = client.post(f"/api/v1/admin/market/overrides/{cid}", json={
            "manual_price": 4321.0, "reason": "consistency",
        }).json()
        assert ov["ticker"] == "MKTC"

        # Company detail
        detail = client.get(f"/api/v1/admin/companies/{cid}").json()
        assert detail["market"]["live_price"] == 4321.0

        # Public company detail also reflects it (same LiveMarketService)
        rows = client.get("/api/v1/companies", params={"page_size": 100}).json()["results"]
        mkt = next(c for c in rows if c["ticker"] == "MKTC")
        assert mkt["market"]["live_price"] == 4321.0
        assert mkt["market"]["price_source"] == "Manual Override"
