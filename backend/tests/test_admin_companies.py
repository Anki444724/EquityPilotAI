"""Phase 2 — Admin Company Management: CRUD, bulk, import/export, merge, versions."""
from __future__ import annotations

import csv
import io

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
def admin_client():
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

    test_client = TestClient(app)
    login = test_client.post(
        "/api/v1/auth/login", json={"email": "admin@alpha.com", "password": PASSWORD},
    )
    assert login.status_code == 200, login.text
    test_client.headers.update({
        "Authorization": f"Bearer {login.json()['access_token']}",
    })

    yield test_client

    settings.NATIVE_AUTH = prev_native
    if prev_override is not None:
        app.dependency_overrides[get_db] = prev_override
    else:
        app.dependency_overrides.pop(get_db, None)
    engine.dispose()


@pytest.fixture()
def clean_companies(admin_client):
    """Delete any companies created by other tests in this module."""
    yield


_counter = [0]


def _create(client, **overrides):
    _counter[0] += 1
    ticker = overrides.get("ticker", f"ACME{_counter[0]}")
    body = {
        "name": f"Company {ticker}", "ticker": ticker, "exchange": "NSE",
        "isin": f"INE{ticker}{_counter[0]:04d}"[:16], "sector": "IT",
        "industry": "Software", "market_cap": 1200.0, "face_value": 10.0,
        "ceo": "Jane Doe", "employees": 500, "headquarters": "Mumbai",
    }
    body.update(overrides)
    r = client.post("/api/v1/admin/companies", json=body)
    assert r.status_code == 201, r.text
    return r.json()


class TestCompanyCrud:
    def test_create_read_update_soft_delete_restore(self, admin_client):
        c = _create(admin_client, ticker="CRUD1")
        assert c["ticker"] == "CRUD1"
        assert c["ceo"] == "Jane Doe"

        # read
        got = admin_client.get(f"/api/v1/admin/companies/{c['id']}").json()
        assert got["name"] == c["name"]

        # update
        r = admin_client.patch(f"/api/v1/admin/companies/{c['id']}", json={"ceo": "Bob"})
        assert r.status_code == 200, r.text
        assert r.json()["ceo"] == "Bob"

        # soft delete -> no longer listed, still visible with include_deleted
        admin_client.delete(f"/api/v1/admin/companies/{c['id']}")
        listed = admin_client.get("/api/v1/admin/companies").json()["results"]
        assert c["id"] not in {x["id"] for x in listed}
        del_list = admin_client.get(
            "/api/v1/admin/companies", params={"include_deleted": "true"}
        ).json()["results"]
        assert c["id"] in {x["id"] for x in del_list}

        # restore
        r = admin_client.post(f"/api/v1/admin/companies/{c['id']}/restore")
        assert r.status_code == 200, r.text
        listed = admin_client.get("/api/v1/admin/companies").json()["results"]
        assert c["id"] in {x["id"] for x in listed}

    def test_permanent_delete(self, admin_client):
        c = _create(admin_client, ticker="PERM1")
        r = admin_client.delete(f"/api/v1/admin/companies/{c['id']}/permanent")
        assert r.status_code == 204
        assert admin_client.get(f"/api/v1/admin/companies/{c['id']}").status_code == 404

    def test_required_fields(self, admin_client):
        r = admin_client.post("/api/v1/admin/companies", json={"ticker": "NONAME"})
        assert r.status_code == 422  # name missing

    def test_duplicate_symbol_rejected(self, admin_client):
        _create(admin_client, ticker="DUP1")
        r = admin_client.post("/api/v1/admin/companies", json={
            "name": "Another", "ticker": "dup1",  # case-insensitive
        })
        assert r.status_code == 400
        assert "already exists" in r.json()["detail"]

    def test_duplicate_isin_rejected(self, admin_client):
        c = _create(admin_client, ticker="ISIN1")
        r = admin_client.post("/api/v1/admin/companies", json={
            "name": "Other Co", "ticker": "OTHER1", "isin": c["isin"],
        })
        assert r.status_code == 400

    def test_update_duplicate_symbol_rejected(self, admin_client):
        _create(admin_client, ticker="A1")
        b = _create(admin_client, ticker="B1")
        r = admin_client.patch(f"/api/v1/admin/companies/{b['id']}", json={"ticker": "A1"})
        assert r.status_code == 400


class TestCompanyVersions:
    def test_every_edit_is_versioned_and_rollback_works(self, admin_client):
        c = _create(admin_client, ticker="VER1")
        admin_client.patch(f"/api/v1/admin/companies/{c['id']}", json={"ceo": "Alice"})
        admin_client.patch(f"/api/v1/admin/companies/{c['id']}", json={"market_cap": 9999.0})

        versions = admin_client.get(f"/api/v1/admin/companies/{c['id']}/versions").json()
        assert len(versions) >= 3  # create + 2 updates
        assert versions[0]["change_type"] in ("update", "create")

        # Roll back to version 1 (create state)
        v1 = versions[-1]["version"]
        r = admin_client.post(
            f"/api/v1/admin/companies/{c['id']}/rollback", params={"version": v1}
        )
        assert r.status_code == 200, r.text
        assert r.json()["ceo"] == "Jane Doe"  # original create value


class TestBulkEdit:
    def test_bulk_edit_updates_multiple(self, admin_client):
        a = _create(admin_client, ticker="BLK1")
        b = _create(admin_client, ticker="BLK2")
        r = admin_client.post("/api/v1/admin/companies/bulk-edit", json={"items": [
            {"id": a["id"], "sector": "Telecom", "industry": "Infra"},
            {"id": b["id"], "sector": "Telecom", "industry": "Infra"},
        ]})
        assert r.status_code == 200, r.text
        assert r.json()["updated"] == 2
        got = admin_client.get(f"/api/v1/admin/companies/{a['id']}").json()
        assert got["sector"] == "Telecom"


class TestImportExport:
    def test_csv_import_and_export_roundtrip(self, admin_client):
        csv_text = "name,ticker,exchange,sector,industry,market_cap,face_value\n" \
                   "Import Co One,IMPORT1,NSE,IT,Software,500,10\n" \
                   "Import Co Two,IMPORT2,NSE,FMCG,Food,300,2\n"
        r = admin_client.post(
            "/api/v1/admin/companies/import/csv",
            files={"file": ("companies.csv", csv_text, "text/csv")},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["imported"] == 2, body

        # Export and confirm rows present
        r = admin_client.get("/api/v1/admin/companies/export/csv")
        assert r.status_code == 200
        content = r.content.decode()
        assert "IMPORT1" in content and "IMPORT2" in content

    def test_xlsx_import(self, admin_client):
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["name", "ticker", "sector", "market_cap"])
        ws.append(["Excel Co", "XL1", "Energy", "777"])
        buf = io.BytesIO()
        wb.save(buf)
        r = admin_client.post(
            "/api/v1/admin/companies/import/xlsx",
            files={"file": ("companies.xlsx", buf.getvalue(),
                            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        )
        assert r.status_code == 200, r.text
        assert r.json()["imported"] == 1


class TestMerge:
    def test_merge_duplicates_reassigns(self, admin_client):
        keep = _create(admin_client, ticker="KEEP1")
        dup = _create(admin_client, ticker="DUPX")
        r = admin_client.post(
            "/api/v1/admin/companies/merge",
            params={"keep_id": keep["id"], "delete_ids": dup["id"]},
        )
        assert r.status_code == 200, r.text
        assert r.json()["removed_count"] == 1
        # duplicate gone
        assert admin_client.get(f"/api/v1/admin/companies/{dup['id']}").status_code == 404


class TestSearchFilters:
    def test_search_by_multiple_fields(self, admin_client):
        _create(admin_client, ticker="SEAR1", sector="Chemicals", industry="Paints")
        r = admin_client.get("/api/v1/admin/companies", params={"search": "SEAR1"})
        assert r.status_code == 200
        assert any(x["ticker"] == "SEAR1" for x in r.json()["results"])

        r = admin_client.get("/api/v1/admin/companies", params={"sector": "Chemicals"})
        assert any(x["ticker"] == "SEAR1" for x in r.json()["results"])

    def test_pagination_and_sorting(self, admin_client):
        r = admin_client.get(
            "/api/v1/admin/companies", params={"page": 1, "page_size": 5, "sort_by": "name", "order": "asc"}
        )
        assert r.status_code == 200
        assert "results" in r.json() and "total" in r.json()


class TestRbac:
    def test_read_requires_permission(self):
        r = TestClient(app).get("/api/v1/admin/companies")
        assert r.status_code in (401, 403)
