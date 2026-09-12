"""Hybrid retrieval failure falls back to the legacy index without a NameError.

Latent bug: `DocumentService._hybrid_answer` referenced `log`, but the module
defines `logger`. A hybrid retrieval failure therefore raised `NameError`
instead of gracefully returning `None` so the caller falls back to the legacy
in-memory index. This pins the intended behaviour: when the hybrid engine
raises, retrieval still answers from the legacy index and never 500s.
"""
from __future__ import annotations

import uuid

import pytest

from app.core.config import settings
from app.models.company import Company
from app.models.document import Document, DocumentChunk, DocumentJob
from app.services.blogger.sync import BloggerSyncService
from app.services.documents.storage import LocalFileStorage
from app.services.documents.worker import DocumentWorker
from tests.fixtures.blogger_feed import (
    FEED_URL, FakeBloggerServer, FakePost, client_for,
)

PUBLISHED = "HYBFALL"
POST_TEXT = (
    "Rural distribution reached 62% of addressable outlets, and the Maggi "
    "noodle line ran at 78% capacity utilisation through the year."
)


def _purge(session) -> None:
    session.rollback()
    session.query(DocumentChunk).delete()
    session.query(DocumentJob).delete()
    session.query(Document).delete()
    session.commit()


@pytest.fixture()
def db_session():
    from tests.conftest import TestingSession

    session = TestingSession()
    _purge(session)
    try:
        yield session
    finally:
        _purge(session)
        session.close()


@pytest.fixture(autouse=True)
def blogger_settings(monkeypatch, db_session):
    monkeypatch.setattr(settings, "BLOGGER_ENABLED", True)
    monkeypatch.setattr(settings, "BLOGGER_FEED_URL", FEED_URL)
    monkeypatch.setattr(settings, "BLOGGER_PUBLIC_TICKERS", PUBLISHED)
    monkeypatch.setattr(settings, "BLOGGER_DEFAULT_TICKER", "")
    monkeypatch.setattr(settings, "BLOGGER_SYNC_SECRET", "")


@pytest.fixture()
def storage(tmp_path) -> LocalFileStorage:
    return LocalFileStorage(tmp_path / "documents")


@pytest.fixture()
def indexed_company(db_session, storage, monkeypatch):
    """A company with one ingested, indexed post (so legacy retrieval has
    something to find)."""
    row = Company(
        id=str(uuid.uuid4()), ticker=PUBLISHED,
        name="Hybrid Fallback Test Co", exchange="NSE",
    )
    db_session.add(row)
    db_session.commit()

    monkeypatch.setattr(
        "app.services.documents.ingestion.get_storage", lambda: storage,
    )
    server = FakeBloggerServer(posts=(FakePost(
        post_id="8000000000000000001",
        title="Rural Distribution Review",
        labels=("FMCG", "HYBFALL"),
        slug="rural-distribution-review",
        text=POST_TEXT,
    ),))
    result = BloggerSyncService(
        db_session, feed=client_for(server), storage=storage,
    ).sync()
    assert result.ingested == 1, result.as_dict()
    worker = DocumentWorker(lambda: db_session, storage=storage)
    while worker.run_once():
        pass

    yield row

    db_session.query(Document).filter(Document.company_id == row.id).delete(
        synchronize_session=False,
    )
    db_session.delete(row)
    db_session.commit()


class TestHybridFallback:
    def test_hybrid_failure_returns_none_not_name_error(
        self, db_session, indexed_company, monkeypatch,
    ):
        """A raising hybrid engine must degrade to None, not a NameError."""
        from app.services.documents.service import DocumentService
        from app.services.retrieval.engine import HybridRetrievalEngine

        def boom(*args, **kwargs):
            raise RuntimeError("pgvector unavailable")

        monkeypatch.setattr(HybridRetrievalEngine, "retrieve", boom)

        service = DocumentService(db_session)
        # `_hybrid_answer` returns None to signal fallback; it must not raise.
        assert service._hybrid_answer(
            "capacity utilisation", indexed_company.id, 5, None,
        ) is None

    def test_search_falls_back_to_legacy_index(
        self, db_session, indexed_company, monkeypatch,
    ):
        """End to end: with the hybrid engine raising, search still answers
        from the legacy in-memory index."""
        from app.services.documents.service import DocumentService
        from app.services.retrieval.engine import HybridRetrievalEngine

        monkeypatch.setattr(
            HybridRetrievalEngine, "retrieve",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )

        from app.services.platform.cache import Namespace, cache

        cache.invalidate(Namespace.RAG)

        answer = DocumentService(db_session).search(
            "capacity utilisation", company_id=indexed_company.id, top_k=5,
        )
        assert answer.hits, "legacy retrieval must still return results"
