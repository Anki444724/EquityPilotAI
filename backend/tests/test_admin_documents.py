"""Phase 6 — Document Intelligence Center: approval workflow, compare, search, RAG."""
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
        "name": "Doc Co", "ticker": "DOCC", "isin": "INE929292929",
    })
    assert c.status_code == 201, c.text
    company_id = c.json()["id"]

    yield client, company_id, Session

    settings.NATIVE_AUTH = prev_native
    if prev_override is not None:
        app.dependency_overrides[get_db] = prev_override
    else:
        app.dependency_overrides.pop(get_db, None)
    engine.dispose()


def _make_doc(client, cid, Session, filename="annual.pdf"):
    # Insert a document directly so we control the approval_status.
    from app.models.document import Document, DocumentChunk, DocumentFact
    db = Session()
    try:
        doc = Document(
            company_id=cid, filename=filename, title="Annual report",
            doc_type="annual", file_format="PDF", size_bytes=100,
            content_hash=filename, status="completed",
            approval_status="ai_extracted", page_count=10, chunk_count=1,
        )
        db.add(doc)
        db.flush()
        db.add(DocumentChunk(document_id=doc.id, chunk_index=0, text="Revenue grew strongly. Debt reduced.",
                             page=1, paragraph=0, section="MD&A", section_title="MD&A",
                             token_estimate=10, fingerprint=f"fp-{filename}"))
        db.add(DocumentFact(document_id=doc.id, company_id=cid, category="financial",
                            field_key="revenue", label="Revenue", value=100.0,
                            unit="crore", page=3, section="P&L", confidence=0.9))
        db.commit()
        return doc.id
    finally:
        db.close()


class TestApprovalWorkflow:
    def test_full_workflow(self, ctx):
        client, cid, session = ctx
        did = _make_doc(client, cid, session)
        # ai_extracted -> pending_review -> approved -> published
        r = client.post(f"/api/v1/admin/documents/{did}/approval", json={"state": "pending_review"})
        assert r.status_code == 200, r.text
        assert r.json()["approval_status"] == "pending_review"

        r = client.post(f"/api/v1/admin/documents/{did}/approve")
        assert r.json()["approval_status"] == "approved"
        assert r.json()["approval_reviewer"] == "admin@alpha.com"
        assert r.json()["approved_at"] is not None

        r = client.post(f"/api/v1/admin/documents/{did}/publish")
        assert r.json()["approval_status"] == "published"

    def test_reject(self, ctx):
        client, cid, session = ctx
        did = _make_doc(client, cid, session, "reject.pdf")
        r = client.post(f"/api/v1/admin/documents/{did}/reject", params={"note": "needs more"})
        assert r.status_code == 200
        assert r.json()["approval_status"] == "pending_review"
        assert r.json()["approval_note"] == "needs more"

    def test_invalid_state(self, ctx):
        client, cid, session = ctx
        did = _make_doc(client, cid, session, "invalid.pdf")
        r = client.post(f"/api/v1/admin/documents/{did}/approval", json={"state": "bogus"})
        assert r.status_code == 422

    def test_list_filter_by_status(self, ctx):
        client, cid, session = ctx
        _make_doc(client, cid, session, "list1.pdf")
        r = client.get("/api/v1/admin/documents", params={"approval_status": "ai_extracted"})
        assert r.status_code == 200
        assert all(d["approval_status"] == "ai_extracted" for d in r.json()["items"])


class TestCompareAndVersion:
    def test_compare_documents(self, ctx):
        client, cid, session = ctx
        a = _make_doc(client, cid, session, "cmp_a.pdf")
        b = _make_doc(client, cid, session, "cmp_b.pdf")
        r = client.get(f"/api/v1/admin/documents/{a}/compare/{b}")
        assert r.status_code == 200, r.text
        body = r.json()
        assert "changed_fields" in body
        assert "old" in body and "new" in body

    def test_version_history(self, ctx):
        client, cid, session = ctx
        a = _make_doc(client, cid, session, "ver_a.pdf")
        r = client.get(f"/api/v1/admin/documents/{a}/versions")
        assert r.status_code == 200
        assert isinstance(r.json(), list)


class TestSearchAndRag:
    def test_search_highlights(self, ctx):
        client, cid, session = ctx
        _make_doc(client, cid, session, "search.pdf")
        r = client.get("/api/v1/admin/documents/search", params={"q": "Revenue"})
        assert r.status_code == 200, r.text
        results = r.json()["results"]
        assert len(results) >= 1
        assert "<mark>" in results[0]["text"]

    def test_rag_stats(self, ctx):
        client, cid, session = ctx
        _make_doc(client, cid, session, "rag.pdf")
        r = client.get("/api/v1/admin/documents/rag/stats", params={"company_id": cid})
        assert r.status_code == 200
        assert r.json()["documents"] >= 1
        assert r.json()["chunks"] >= 1


class TestDelete:
    def test_delete_document(self, ctx):
        client, cid, session = ctx
        did = _make_doc(client, cid, session, "del.pdf")
        r = client.delete(f"/api/v1/admin/documents/{did}")
        assert r.status_code == 204
        assert client.get(f"/api/v1/admin/documents/{did}").status_code == 404
