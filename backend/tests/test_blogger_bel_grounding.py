"""Blogger chatbot stays scoped to BEL and cites the BEL post for valuation.

These guard the public path end-to-end: the blogger endpoint resolves the
requested ticker from the allowlist first and never runs the shared
question-text company resolution, so a Hindi "par" in a valuation question
must not retarget the answer away from BEL, and a valuation question must
still retrieve and cite the BEL Blogger post.

The shared resolution fix (CompanyService.named_in) is what protects the
authenticated `/company/{ticker}/ai/chat` path; these tests pin the public
surface so both entry points are covered.
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

PUBLISHED = "BEL"
BEL_NAME = "Bharat Electronics Ltd"

#: The valuation section of the live BEL post, with the exact figures.
POST_TEXT = (
    "Bharat Electronics Ltd (BEL) is a defence electronics company.\n\n"
    "Valuation: at the current price BEL trades at a P/E of 48.2x, a return "
    "on equity (ROE) of 27.4% and a return on capital employed (ROCE) of "
    "36.4%. A strong company does not automatically mean a cheap stock, and "
    "the intrinsic value figure is model-dependent."
)

BEL_SLUG = "bharat-electronics-bel-stock-analysis"


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
def client(api_client):
    return api_client


@pytest.fixture()
def storage(tmp_path) -> LocalFileStorage:
    return LocalFileStorage(tmp_path / "documents")


@pytest.fixture()
def bel(db_session):
    """A BEL company row, created here rather than in the seed."""
    row = Company(
        id=str(uuid.uuid4()), ticker="BEL", name=BEL_NAME, exchange="NSE",
    )
    db_session.add(row)
    db_session.commit()
    yield row
    db_session.query(Document).filter(Document.company_id == row.id).delete(
        synchronize_session=False,
    )
    db_session.delete(row)
    db_session.commit()


@pytest.fixture()
def indexed_bel_post(db_session, storage, monkeypatch, bel):
    monkeypatch.setattr(
        "app.services.documents.ingestion.get_storage", lambda: storage,
    )
    server = FakeBloggerServer(posts=(FakePost(
        post_id="9000000000000000001",
        title="Bharat Electronics Ltd (BEL) Stock Analysis",
        labels=("Defence Stocks", "BEL", "Stock Analysis"),
        slug=BEL_SLUG,
        text=POST_TEXT,
    ),))
    result = BloggerSyncService(
        db_session, feed=client_for(server), storage=storage,
    ).sync()
    assert result.ingested == 1, result.as_dict()
    worker = DocumentWorker(lambda: db_session, storage=storage)
    while worker.run_once():
        pass
    return server.posts[0]


def _ask(client, *, question, session_id=None):
    return client.post(
        "/api/v1/blogger/chat",
        json={
            "ticker": PUBLISHED, "question": question,
            "session_id": session_id or f"t-{uuid.uuid4().hex[:12]}",
            "language": "auto",
        },
    )


class TestBloggerBELGrounding:
    def test_valuation_question_with_par_stays_scoped_to_bel(
        self, client, indexed_bel_post,
    ):
        body = _ask(
            client,
            question="BEL ki valuation expensive hai ya reasonable? "
                     "P/E, ROE aur ROCE ke basis par explain karo.",
        ).json()
        # The public answer is about BEL, never PAR.
        assert body["ticker"] == PUBLISHED
        assert body["company"] == BEL_NAME

    def test_valuation_question_returns_blogger_citations(
        self, client, indexed_bel_post,
    ):
        body = _ask(
            client,
            question="BEL ki valuation expensive hai ya reasonable? "
                     "P/E, ROE aur ROCE ke basis par explain karo.",
        ).json()

        blog_citations = [
            c for c in body["citations"]
            if c.get("url") and "blogspot.com" in c["url"]
        ]
        assert blog_citations, body["citations"]
        assert "Blogger post" in blog_citations[0]["label"]

        # The retrieved evidence carries the post's valuation figures.
        evidence = " ".join(c.get("snippet") or "" for c in body["citations"])
        assert "P/E" in evidence
        assert "48.2" in evidence

    def test_explicit_all_caps_ticker_mention_is_not_this_path(
        self, client, indexed_bel_post,
    ):
        """The blogger path is scoped by the request ticker, not by question
        text — naming PAR in the question must not change the scoped company."""
        body = _ask(
            client,
            question="PAR ka debt kya hai?",
        ).json()
        assert body["ticker"] == PUBLISHED
