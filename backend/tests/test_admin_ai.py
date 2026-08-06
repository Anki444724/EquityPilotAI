"""Phase 5 — AI Operations Center: overrides, models, cost."""
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
        "name": "AI Co", "ticker": "AICO", "isin": "INE818181818",
    })
    assert c.status_code == 201, c.text
    company_id = c.json()["id"]

    # Seed financial facts so the scoring endpoint has data to score.
    import app.models as _m
    from app.domain.financials.line_items import LineItem
    facts = [
        {"fiscal_year": 2024, "line_item": "revenue", "value": 5000.0},
        {"fiscal_year": 2024, "line_item": "net_block_ppe", "value": 3000.0},
        {"fiscal_year": 2024, "line_item": "cash_and_bank", "value": 1000.0},
        {"fiscal_year": 2024, "line_item": "equity_share_capital", "value": 500.0},
        {"fiscal_year": 2024, "line_item": "long_term_borrowings", "value": 800.0},
        {"fiscal_year": 2024, "line_item": "trade_receivables", "value": 600.0},
        {"fiscal_year": 2024, "line_item": "inventories", "value": 400.0},
        {"fiscal_year": 2024, "line_item": "trade_payables", "value": 500.0},
        {"fiscal_year": 2024, "line_item": "raw_materials", "value": 2000.0},
        {"fiscal_year": 2024, "line_item": "employee_benefit", "value": 800.0},
    ]
    fr = client.put(f"/api/v1/admin/financials/{company_id}/facts", json=facts)
    assert fr.status_code == 200, fr.text

    yield client, company_id

    settings.NATIVE_AUTH = prev_native
    if prev_override is not None:
        app.dependency_overrides[get_db] = prev_override
    else:
        app.dependency_overrides.pop(get_db, None)
    engine.dispose()


class TestAIOverrides:
    def test_create_and_apply(self, ctx):
        client, cid = ctx
        r = client.post(f"/api/v1/admin/ai/overrides/{cid}", json={
            "manual_score": 95.0, "manual_recommendation": "Strong Buy",
            "manual_summary": "Manually overridden thesis",
            "reason": "analyst override", "mode": "manual",
        })
        assert r.status_code == 201, r.text
        ov = r.json()
        assert ov["ticker"] == "AICO"
        assert ov["is_active"] is True

        # The scoring endpoint reflects the override.
        sc = client.get(f"/api/v1/company/AICO/scoring").json()
        assert sc["overall_score"] == 95.0
        assert sc["recommendation"] == "Strong Buy"
        assert sc["summary"] == "Manually overridden thesis"

    def test_clear_reverts(self, ctx):
        client, cid = ctx
        ov = client.post(f"/api/v1/admin/ai/overrides/{cid}", json={
            "manual_score": 88.0, "mode": "manual",
        }).json()
        client.delete(f"/api/v1/admin/ai/overrides/{ov['id']}")
        sc = client.get(f"/api/v1/company/AICO/scoring").json()
        assert sc["overall_score"] != 88.0

    def test_auto_mode_no_override(self, ctx):
        client, cid = ctx
        client.post(f"/api/v1/admin/ai/overrides/{cid}", json={"mode": "auto"})
        sc = client.get(f"/api/v1/company/AICO/scoring").json()
        # Auto mode must not pin a manual score.
        assert "overridden" not in sc or not sc.get("warnings")


class TestModels:
    def test_model_registry(self, ctx):
        client, _ = ctx
        models = client.get("/api/v1/admin/ai/models").json()
        names = {m["name"] for m in models}
        assert {"Gemini", "OpenRouter", "Claude", "OpenAI", "Local LLM"} <= names
        assert all("priority" in m and "configured" in m for m in models)


class TestCost:
    def test_cost_dashboard(self, ctx):
        client, _ = ctx
        cost = client.get("/api/v1/admin/ai/cost", params={"days": 30}).json()
        assert "total_tokens" in cost
        assert "requests" in cost
        assert "total_cost_usd" in cost
        assert "by_provider" in cost


class TestPromptCatalog:
    def test_prompts(self, ctx):
        client, _ = ctx
        prompts = client.get("/api/v1/admin/ai/prompts").json()
        assert isinstance(prompts, list)
        assert all("key" in p and "template" in p for p in prompts)


class TestMiscState:
    def test_queue_learning_rag_logs(self, ctx):
        client, _ = ctx
        for path in ("queue", "learning", "rag", "logs"):
            r = client.get(f"/api/v1/admin/ai/{path}")
            assert r.status_code == 200, path
