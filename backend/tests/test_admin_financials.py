"""Phase 3 — Enterprise Financial Statements: edit, validate, version, import."""
from __future__ import annotations

import io
import json

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

    # Create a company to edit financials for
    c = client.post("/api/v1/admin/companies", json={
        "name": "Fin Co", "ticker": "FIN1", "isin": "INE909090909",
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


def _facts(client, cid, **overrides):
    body = {
        "fiscal_year": 2025,
        "line_item": "revenue",
        "value": 1000.0,
        "precedence": 2,
        "source": "admin-test",
    }
    body.update(overrides)
    return client.put(f"/api/v1/admin/financials/{cid}/facts", json=[body])


class TestAnnualFacts:
    def test_upsert_and_statements(self, ctx):
        client, cid = ctx
        r = _facts(client, cid)
        assert r.status_code == 200, r.text
        assert r.json()["created"] == 1

        # Add balance + cashflow so statements build
        _facts(client, cid, line_item="cash_and_bank", value=500)
        _facts(client, cid, line_item="equity_share_capital", value=100)

        st = client.get(f"/api/v1/admin/financials/{cid}/statements").json()
        assert 2025 in st["years"]

    def test_duplicate_year_rejected(self, ctx):
        client, cid = ctx
        r = _facts(client, cid, line_item="revenue", value=2000.0)
        assert r.status_code == 200
        assert r.json()["updated"] == 1  # upsert updates, does not duplicate

    def test_unknown_line_item_error(self, ctx):
        client, cid = ctx
        r = _facts(client, cid, line_item="nonsense", value=1)
        assert r.status_code == 200
        assert r.json()["errors"]


class TestQuarterly:
    def test_upsert_quarterly(self, ctx):
        client, cid = ctx
        r = client.put(f"/api/v1/admin/financials/{cid}/quarterly", json=[{
            "fiscal_year": 2025, "quarter": 1, "revenue": 250, "net_profit": 40,
        }])
        assert r.status_code == 200, r.text
        assert r.json()["created"] == 1
        q = client.get(f"/api/v1/admin/financials/{cid}/quarterly").json()["items"]
        assert len(q) == 1 and q[0]["quarter"] == 1

    def test_invalid_quarter(self, ctx):
        client, cid = ctx
        r = client.put(f"/api/v1/admin/financials/{cid}/quarterly", json=[{
            "fiscal_year": 2025, "quarter": 9, "revenue": 1,
        }])
        assert r.status_code == 422  # schema bounds


class TestShareholding:
    def test_upsert_shareholding(self, ctx):
        client, cid = ctx
        r = client.put(f"/api/v1/admin/financials/{cid}/shareholding", json=[{
            "fiscal_year": 2025, "quarter": 1,
            "promoter_indian": 0.52, "fii_fpi": 0.2, "others_custodians": 0.28,
        }])
        assert r.status_code == 200, r.text
        items = client.get(f"/api/v1/admin/financials/{cid}/shareholding").json()["items"]
        assert len(items) == 1 and items[0]["promoter_indian"] == 0.52


class TestCorporateActions:
    def test_crud_action(self, ctx):
        client, cid = ctx
        r = client.post(f"/api/v1/admin/financials/{cid}/corporate-actions", json={
            "action_type": "dividend", "ex_date": "2025-07-10", "value": 8.0,
            "description": "Interim dividend",
        })
        assert r.status_code == 200, r.text
        aid = r.json()["id"]
        actions = client.get(f"/api/v1/admin/financials/{cid}/corporate-actions").json()["items"]
        assert len(actions) == 1 and actions[0]["action_type"] == "dividend"

        r = client.patch(f"/api/v1/admin/financials/{cid}/corporate-actions/{aid}", json={
            "value": 10.0,
        })
        assert r.status_code == 200 and r.json()["value"] == 10.0

        r = client.delete(f"/api/v1/admin/financials/{cid}/corporate-actions/{aid}")
        assert r.status_code == 204


class TestBulkImport:
    def test_csv_import(self, ctx):
        client, cid = ctx
        csv_text = "fiscal_year,line_item,value\n2024,revenue,900\n2024,net_block_ppe,300\n"
        r = client.post(
            f"/api/v1/admin/financials/{cid}/bulk-import",
            params={"kind": "facts"},
            files={"file": ("facts.csv", csv_text, "text/csv")},
        )
        assert r.status_code == 200, r.text
        assert r.json()["created"] == 2

    def test_json_import(self, ctx):
        client, cid = ctx
        payload = json.dumps([
            {"fiscal_year": 2023, "line_item": "revenue", "value": 700},
            {"fiscal_year": 2023, "line_item": "tax_expense", "value": 100},
        ])
        r = client.post(
            f"/api/v1/admin/financials/{cid}/bulk-import",
            params={"kind": "facts"},
            files={"file": ("facts.json", payload, "application/json")},
        )
        assert r.status_code == 200, r.text
        assert r.json()["created"] == 2


class TestVersions:
    def test_versions_and_rollback(self, ctx):
        client, cid = ctx
        _facts(client, cid, line_item="revenue", value=5000.0)
        versions = client.get(f"/api/v1/admin/financials/{cid}/versions").json()
        assert len(versions) >= 2

        # Roll back to version 1 (earliest snapshot)
        v1 = versions[-1]["version"]
        r = client.post(f"/api/v1/admin/financials/{cid}/rollback", params={"version": v1})
        assert r.status_code == 200, r.text
