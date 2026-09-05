"""The public Blogger endpoints: what an anonymous caller may reach.

These tests are mostly about refusal, because that is what the endpoint is for.
A signed-in user's chat is bounded by their account; this one is bounded only by
the checks below, and every check that is missing is a way for a stranger to
spend the platform's money, read another company's corpus, or enumerate its
database.

The properties under test:

* only the tickers the operator published are discussable, and the refusal
  happens before anything is looked up;
* an unknown ticker is not provisioned — no outbound call, no row written;
* the response carries an answer and its provenance, and nothing about the
  deployment: no provider, model, prompt, token count, cost or internal id;
* two readers who both send `session_id: "public"` do not share a conversation;
* the rate limit reaches an anonymous caller;
* the widget can find out what it may offer, and a disabled feature advertises
  nothing;
* a sync can be triggered by an operator's credential or by the platform's own
  document permission, by neither otherwise, and an empty credential does not
  accept an empty header;
* the authenticated company chat is untouched.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.core.config import settings
from app.core.security import get_optional_user
from app.domain.platform.jobs import JobKind
from app.models.company import Company
from app.models.document import Document, DocumentChunk, DocumentJob
from app.models.platform import AuditLog, BackgroundJob
from app.services.blogger.sync import BloggerSyncService
from app.services.documents.storage import LocalFileStorage
from app.services.documents.worker import DocumentWorker
from tests.fixtures.blogger_feed import (
    FEED_URL, FakeBloggerServer, FakePost, client_for,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
WIDGET = REPO_ROOT / "frontend" / "public" / "blogger-chat-widget.js"
COMPOSE = REPO_ROOT / "docker-compose.yml"

PUBLISHED = "NESTLEIND"

#: Vocabulary chosen so a retrieval has something distinctive to find.
POST_TEXT = (
    "Nestlé India's Mysore sandalwood-adjacent division is a distraction; the "
    "real story is distribution.\n\nRural distribution reached 62% of "
    "addressable outlets, and the Maggi noodle line ran at 78% capacity "
    "utilisation through the year."
)


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------
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
    """The feature is on, publishing exactly one seeded company."""
    monkeypatch.setattr(settings, "BLOGGER_ENABLED", True)
    monkeypatch.setattr(settings, "BLOGGER_FEED_URL", FEED_URL)
    monkeypatch.setattr(settings, "BLOGGER_PUBLIC_TICKERS", PUBLISHED)
    monkeypatch.setattr(settings, "BLOGGER_DEFAULT_TICKER", "")
    monkeypatch.setattr(settings, "BLOGGER_SYNC_SECRET", "")


@pytest.fixture()
def client(api_client):
    return api_client


def _ask(client, *, ticker=PUBLISHED, question="What does the company do?",
         session_id=None, language="auto", headers=None):
    """One request. A fresh conversation unless the test asks for a named one.

    The memory store is process-global and keyed by address and session id, and
    every test here arrives from the same test client address — so a shared
    default id would make one test's turns appear in the next test's count.
    """
    return client.post(
        "/api/v1/blogger/chat",
        json={
            "ticker": ticker, "question": question,
            "session_id": session_id or f"t-{uuid.uuid4().hex[:12]}",
            "language": language,
        },
        headers=headers or {},
    )


def _ingest_post(db_session, storage, monkeypatch, *, text=POST_TEXT,
                 content=None, labels=("Nestlé India",),
                 post_id="7000000000000000001",
                 title="Nestlé India: Rural Distribution",
                 slug="nestle-india-rural-distribution",
                 published=datetime(2026, 9, 3, 8, 0, tzinfo=timezone.utc)):
    """Put one Blogger post through the real sync and the real worker.

    `text` is prose the fixture wraps in paragraphs; `content` is markup passed
    through untouched, which is what a real post is.
    """
    monkeypatch.setattr(
        "app.services.documents.ingestion.get_storage", lambda: storage,
    )
    server = FakeBloggerServer(posts=(
        FakePost(
            post_id=post_id, title=title, labels=labels, slug=slug,
            published=published,
            **({"content": content} if content is not None else {"text": text}),
        ),
    ))
    result = BloggerSyncService(
        db_session, feed=client_for(server), storage=storage,
    ).sync()
    assert result.ingested == 1, result.as_dict()
    worker = DocumentWorker(lambda: db_session, storage=storage)
    while worker.run_once():
        pass
    return server.posts[0]


@pytest.fixture()
def storage(tmp_path) -> LocalFileStorage:
    return LocalFileStorage(tmp_path / "documents")


@pytest.fixture()
def indexed_post(db_session, storage, monkeypatch):
    """A post that is ingested, parsed, chunked and searchable."""
    return _ingest_post(db_session, storage, monkeypatch)


# ---------------------------------------------------------------------------
# Who may ask, and about what
# ---------------------------------------------------------------------------
class TestRestrictions:
    def test_an_unpublished_ticker_is_refused(self, client, db_session):
        response = _ask(client, ticker="RELIANCE")
        assert response.status_code == 403
        assert "RELIANCE" in response.json()["detail"]

    def test_the_refusal_happens_before_any_work_is_done(self, client, db_session):
        # No company lookup, no retrieval, no provider call, no usage row: the
        # allowlist is checked first, so an unlisted ticker cannot be used to
        # spend anything or to learn whether a company exists here.
        from app.models.ai import AIAnalysis

        before = db_session.query(AIAnalysis).count()
        assert _ask(client, ticker="RELIANCE").status_code == 403
        assert db_session.query(AIAnalysis).count() == before

    def test_a_ticker_is_matched_regardless_of_case(self, client):
        assert _ask(client, ticker="nestleind").status_code == 200

    def test_surrounding_whitespace_does_not_break_the_ticker(self, client):
        assert _ask(client, ticker="  nestleind  ").status_code == 200

    def test_an_unlisted_ticker_is_not_provisioned(self, client, db_session,
                                                  monkeypatch):
        """Publishing a ticker must not become a way to create companies.

        The authenticated path will fetch an unknown US listing from a vendor
        and write the row. An anonymous caller who could reach that code would
        be able to make this platform perform outbound requests and grow its
        database by guessing symbols.
        """
        monkeypatch.setattr(settings, "BLOGGER_PUBLIC_TICKERS", "AAPL")
        before = db_session.query(Company).count()

        response = _ask(client, ticker="AAPL")

        assert response.status_code == 404
        assert db_session.query(Company).count() == before

    def test_a_question_is_required(self, client):
        assert _ask(client, question="").status_code == 422

    def test_a_question_is_bounded(self, client):
        assert _ask(client, question="x" * 2001).status_code == 422

    def test_a_ticker_is_required(self, client):
        response = client.post(
            "/api/v1/blogger/chat", json={"question": "What does it do?"},
        )
        assert response.status_code == 422

    def test_a_disabled_feature_refuses_to_answer(self, client, monkeypatch):
        monkeypatch.setattr(settings, "BLOGGER_ENABLED", False)
        response = _ask(client)
        assert response.status_code == 503
        assert "not enabled" in response.json()["detail"]

    def test_the_rate_limit_reaches_an_anonymous_caller(self, client):
        statuses = [
            _ask(client, session_id=f"rl-{n}").status_code for n in range(30)
        ]
        assert 429 in statuses
        # A reader who asks a handful of questions is not affected: the budget
        # is per address per minute, not per day.
        assert statuses[0] == 200
        assert statuses.count(200) >= 4

    def test_a_rate_limited_caller_is_told_when_to_retry(self, client):
        for _ in range(30):
            response = _ask(client)
            if response.status_code == 429:
                break
        assert response.status_code == 429
        assert response.headers.get("retry-after") or response.headers.get(
            "x-ratelimit-reset"
        )


# ---------------------------------------------------------------------------
# What comes back
# ---------------------------------------------------------------------------
class TestTheResponse:
    def test_an_answer_is_returned_with_its_provenance(self, client):
        body = _ask(client, question="How is distribution performing?",
                    session_id="widget-thread").json()
        assert body["answer"]
        assert body["ticker"] == PUBLISHED
        assert body["company"]
        # Echoed unchanged: the widget relies on it to keep a thread together.
        assert body["session_id"] == "widget-thread"
        assert body["turn_count"] == 2
        assert isinstance(body["grounded"], bool)
        assert isinstance(body["citations"], list)
        assert "not investment advice" in body["disclosure"].lower()

    def test_the_response_says_nothing_about_the_deployment(self, client):
        """The field list is the security control.

        Every one of these is useful to an operator and useless to a reader —
        and together they describe the platform's configuration, its economics
        and its internal identifiers to anybody who can reach the URL.
        """
        body = _ask(client).json()
        forbidden = {
            "provider", "model", "prompt_key", "prompt_version", "cost_usd",
            "prompt_tokens", "completion_tokens", "total_tokens", "data_quality",
            "fell_back_from", "session_state", "content", "citation_audit",
            "guardrails",
        }
        assert forbidden.isdisjoint(body)

        # ...including inside the citations, where the authenticated schema
        # carries internal primary keys.
        for citation in body["citations"]:
            assert {"document_id", "chunk_id"}.isdisjoint(citation)

    def test_the_response_leaks_no_paths_or_connection_strings(self, client):
        text = _ask(client).text.lower()
        for needle in ("postgresql", "sqlite", "redis", "/app/", "ierp",
                       "encryption_key", "api_key", "bearer "):
            assert needle not in text, needle

    def test_an_answer_with_no_document_evidence_says_so(self, client):
        # Nothing is indexed for this company in this test, so the answer comes
        # from computed figures — and a reader of a research blog is entitled to
        # be told that.
        body = _ask(client, question="Summarise the business.").json()
        if not body["citations"]:
            assert body["grounded"] is False
            assert any(
                "no indexed document" in warning.lower()
                for warning in body["warnings"]
            )

    def test_a_refusal_for_want_of_evidence_is_not_called_grounded(self, client):
        """The one misleading thing this response could say.

        The citation audit's verdict is "no uncited number, no invented key",
        and an answer that declines because nothing was retrieved satisfies it
        vacuously — it cites nothing and claims nothing. A blog widget that
        rendered `grounded: true` beside "I found no evidence" would be telling
        the reader the opposite of the truth, so the public field also requires
        evidence to have been retrieved.
        """
        body = _ask(
            client,
            question="What was the capacity utilisation of the steel line?",
        ).json()
        if not body["citations"]:
            assert body["grounded"] is False

    def test_an_indexed_post_is_cited_with_a_link_back_to_it(
        self, client, indexed_post,
    ):
        body = _ask(
            client, question="What was the capacity utilisation of the Maggi line?",
        ).json()
        assert body["citations"], "the post should be retrieved as evidence"

        blog_citations = [
            citation for citation in body["citations"]
            if citation.get("url") and "blogspot.com" in citation["url"]
        ]
        assert blog_citations, body["citations"]
        citation = blog_citations[0]
        assert citation["url"].startswith("https://equitypilot.blogspot.com/")
        assert citation["document_type"] == "research_note"
        # Named as what it is: the model is told this is a blog post, so it
        # cannot describe the author's commentary as a company filing.
        assert "Blogger post" in citation["label"]
        assert citation["snippet"]

    def test_a_hindi_question_is_answered_without_an_error(self, client):
        body = _ask(
            client, question="कंपनी की बिक्री कितनी बढ़ी?", language="auto",
        ).json()
        assert body["answer"]
        if body["language"]:
            # The reader-facing subset only: no provider, no cost, no latency.
            assert {"provider", "cost_usd", "latency_ms"}.isdisjoint(body["language"])
            assert body["language"]["language"]

    def test_an_explicit_language_request_is_accepted(self, client):
        # The request is honoured as far as this deployment can: with no
        # translation provider configured the adapter records that it answered
        # in English rather than pretending otherwise. What must hold is that
        # asking for a language is never an error, and the block always says
        # what actually happened.
        body = _ask(client, question="How is distribution?", language="hindi").json()
        assert body["answer"]
        if body["language"]:
            assert body["language"]["language"]
            assert body["language"]["resolved_from"]

    def test_two_readers_do_not_share_a_session(self, client):
        """The session id is caller-chosen, so it is not an identity.

        Both readers below send `session_id: "public"`. Without the address in
        the memory key, the second would continue the first's conversation and
        be answered from a context neither of them chose — which on a public
        endpoint means one reader's question appearing in another's answer.
        """
        shared_id = f"shared-{uuid.uuid4().hex[:8]}"
        first = _ask(client, question="First reader's question?", session_id=shared_id,
                     headers={"X-Forwarded-For": "203.0.113.7"}).json()
        second = _ask(client, question="Second reader's question?", session_id=shared_id,
                      headers={"X-Forwarded-For": "198.51.100.9"}).json()

        assert first["turn_count"] == 2
        assert second["turn_count"] == 2

    def test_one_reader_does_accumulate_a_conversation(self, client):
        headers = {"X-Forwarded-For": "203.0.113.8"}
        thread = f"thread-{uuid.uuid4().hex[:8]}"
        first = _ask(client, question="What does the company sell?",
                     session_id=thread, headers=headers).json()
        second = _ask(client, question="And how is demand?",
                      session_id=thread, headers=headers).json()
        assert first["turn_count"] == 2
        assert second["turn_count"] == 4

    def test_the_authenticated_company_chat_is_unchanged(self, client):
        """The existing chat keeps its full surface.

        Worth asserting explicitly: the temptation when adding a public variant
        is to generalise the authenticated one, and a reader of the diff should
        not have to trust that it was not.
        """
        response = client.post(
            f"/api/v1/company/{PUBLISHED}/ai/chat",
            json={"question": "How leveraged is it?", "session_id": "compat"},
        )
        assert response.status_code == 200
        body = response.json()
        for field in ("provider", "model", "content", "display_content",
                      "citation_audit", "guardrails", "data_quality",
                      "prompt_key", "prompt_version"):
            assert field in body, field

    def test_the_openapi_surface_names_no_configuration(self, client):
        schema = json.dumps(client.get("/openapi.json").json()).lower()
        for needle in ("blogger_sync_secret", "encryption_key", "database_url"):
            assert needle not in schema


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------
class TestStatus:
    def test_the_widget_can_find_out_what_it_may_offer(self, client, indexed_post):
        body = client.get("/api/v1/blogger/status").json()
        assert body["enabled"] is True
        assert [t["ticker"] for t in body["tickers"]] == [PUBLISHED]
        assert body["tickers"][0]["name"]
        assert body["tickers"][0]["posts"] == 1
        assert body["last_synced_at"]
        assert body["disclosure"]

    def test_only_published_companies_are_advertised(self, client, monkeypatch):
        monkeypatch.setattr(settings, "BLOGGER_PUBLIC_TICKERS", "NESTLEIND,LT")
        tickers = {t["ticker"] for t in client.get("/api/v1/blogger/status").json()["tickers"]}
        assert tickers == {"NESTLEIND", "LT"}

    def test_a_disabled_feature_advertises_nothing(self, client, monkeypatch):
        monkeypatch.setattr(settings, "BLOGGER_ENABLED", False)
        body = client.get("/api/v1/blogger/status").json()
        assert body["enabled"] is False
        assert body["tickers"] == []
        assert body["reason"]

    def test_an_empty_allowlist_advertises_nothing(self, client, monkeypatch):
        monkeypatch.setattr(settings, "BLOGGER_PUBLIC_TICKERS", "")
        body = client.get("/api/v1/blogger/status").json()
        assert body["enabled"] is False
        assert body["tickers"] == []

    def test_a_published_ticker_with_no_company_row_is_not_advertised(
        self, client, monkeypatch,
    ):
        # An operator can publish a ticker the register does not hold. The
        # widget should not offer it, and the endpoint should not fail.
        monkeypatch.setattr(settings, "BLOGGER_PUBLIC_TICKERS", "NESTLEIND,NOSUCHCO")
        body = client.get("/api/v1/blogger/status").json()
        assert [t["ticker"] for t in body["tickers"]] == [PUBLISHED]

    def test_status_is_a_read(self, client):
        assert client.post("/api/v1/blogger/status").status_code == 405


# ---------------------------------------------------------------------------
# Sync trigger
# ---------------------------------------------------------------------------
class TestSyncTrigger:
    @pytest.fixture()
    def anonymous(self):
        """No session at all — the case the credential exists for.

        Overridden as a dependency, because that is what FastAPI resolves: the
        endpoint captured the callable when it was declared, so replacing the
        module attribute afterwards would change nothing and the test would
        quietly run as an operator instead.
        """
        from tests.conftest import app

        app.dependency_overrides[get_optional_user] = lambda: None
        try:
            yield
        finally:
            app.dependency_overrides.pop(get_optional_user, None)

    def test_an_anonymous_caller_with_no_credential_is_refused(self, client, anonymous):
        assert client.post("/api/v1/blogger/sync", json={}).status_code == 403

    def test_the_refusal_does_not_reveal_whether_a_credential_exists(
        self, client, anonymous, monkeypatch,
    ):
        monkeypatch.setattr(settings, "BLOGGER_SYNC_SECRET", "")
        without = client.post("/api/v1/blogger/sync", json={}).json()["detail"]
        monkeypatch.setattr(settings, "BLOGGER_SYNC_SECRET", "configured-value")
        with_secret = client.post("/api/v1/blogger/sync", json={}).json()["detail"]
        assert without == with_secret

    def test_an_empty_credential_does_not_accept_an_empty_header(
        self, client, anonymous, monkeypatch,
    ):
        # The trap: comparing an absent header against an unset configuration
        # value succeeds, and the endpoint becomes public.
        monkeypatch.setattr(settings, "BLOGGER_SYNC_SECRET", "")
        response = client.post(
            "/api/v1/blogger/sync", json={},
            headers={"X-Blogger-Sync-Secret": ""},
        )
        assert response.status_code == 403

    def test_a_configured_credential_queues_a_sync(self, client, anonymous,
                                                   db_session, monkeypatch):
        monkeypatch.setattr(settings, "BLOGGER_SYNC_SECRET", "configured-value")
        response = client.post(
            "/api/v1/blogger/sync", json={"max_posts": 10},
            headers={"X-Blogger-Sync-Secret": "configured-value"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["queued"] is True
        assert body["job_id"]

        job = db_session.get(BackgroundJob, body["job_id"])
        assert job is not None
        assert job.kind == JobKind.BLOGGER_SYNC.value
        assert job.payload["max_posts"] == 10

    def test_a_wrong_credential_is_refused(self, client, anonymous, monkeypatch):
        monkeypatch.setattr(settings, "BLOGGER_SYNC_SECRET", "configured-value")
        response = client.post(
            "/api/v1/blogger/sync", json={},
            headers={"X-Blogger-Sync-Secret": "not-the-value"},
        )
        assert response.status_code == 403

    def test_a_credential_is_never_echoed_back(self, client, anonymous, monkeypatch):
        monkeypatch.setattr(settings, "BLOGGER_SYNC_SECRET", "configured-value")
        response = client.post(
            "/api/v1/blogger/sync", json={},
            headers={"X-Blogger-Sync-Secret": "not-the-value"},
        )
        assert "configured-value" not in response.text

    def test_an_operator_may_trigger_it_without_a_credential(self, client, db_session):
        # The test harness runs in development identity mode, where the caller
        # holds the platform's document permission — the same permission that
        # governs an upload.
        response = client.post("/api/v1/blogger/sync", json={})
        assert response.status_code == 200
        assert response.json()["queued"] is True

    def test_the_sync_is_queued_rather_than_run(self, client, db_session):
        # A full pass reads a paged feed and enqueues a job per changed post.
        # Holding an HTTP request open for that is how a deployment ends up with
        # a proxy timeout in front of work that succeeded.
        before = db_session.query(Document).count()
        assert client.post("/api/v1/blogger/sync", json={}).status_code == 200
        assert db_session.query(Document).count() == before

    def test_a_second_trigger_does_not_queue_a_second_job(self, client, db_session):
        first = client.post("/api/v1/blogger/sync", json={}).json()["job_id"]
        second = client.post("/api/v1/blogger/sync", json={}).json()["job_id"]
        assert first == second

    def test_a_dry_run_is_passed_through_to_the_job(self, client, db_session):
        job_id = client.post(
            "/api/v1/blogger/sync", json={"dry_run": True},
        ).json()["job_id"]
        assert db_session.get(BackgroundJob, job_id).payload["dry_run"] is True

    def test_no_feed_url_is_a_conflict_not_a_silent_success(self, client, monkeypatch):
        monkeypatch.setattr(settings, "BLOGGER_FEED_URL", "  ")
        response = client.post("/api/v1/blogger/sync", json={})
        assert response.status_code == 409

    def test_the_trigger_is_audited(self, client, db_session):
        client.post("/api/v1/blogger/sync", json={"force": True})
        rows = db_session.query(AuditLog).filter(
            AuditLog.action == "system.job.enqueued"
        ).all()
        assert rows
        assert any("Blogger sync queued" in (row.summary or "") for row in rows)
        # The credential is not in the audit row either.
        assert all(
            "configured-value" not in json.dumps(row.meta or {})
            for row in rows
        )

    def test_the_trigger_is_rate_limited(self, client):
        statuses = [
            client.post("/api/v1/blogger/sync", json={}).status_code
            for _ in range(12)
        ]
        assert 429 in statuses


#: The live Shriram Finance post, as the feed served it on 2026-09-05: its own
#: post id, title, labels, URL slug and timestamps, with the markup reduced to
#: the sections a reader would ask about. The `<style>` block and the leading
#: HTML comment are real too — they are what the parser has to drop.
LIVE_POST_ID = "4850132895787140759"
LIVE_SLUG = "shriram-finance-share-price-fundamental-analysis"
LIVE_URL = f"https://equitypilot.blogspot.com/2026/09/{LIVE_SLUG}.html"
LIVE_TITLE = (
    "Shriram Finance Share Price, Fundamental Analysis, Future Growth "
    "& Investment Outlook"
)
LIVE_LABELS = (
    "Finance Stocks", "Fundamental Analysis", "NBFC", "Share Price",
    "Shriram Finance", "Stock Analysis",
)
LIVE_HTML = """<!--=========================================================
SHRIRAM FINANCE LTD — COMPLETE BLOGGER STOCK ANALYSIS
Ready to paste into Blogger HTML view
=========================================================-->

<style>
.sf-post{
  font-family:Arial,Helvetica,sans-serif;
  line-height:1.7;
  color:#202124;
  max-width:100%;
  margin:auto;
}
.sf-card strong{
  display:block;
  font-size:20px;
  margin-top:5px;
}
</style>

<div class="sf-post">

<h2 id="overview">Quick Overview</h2>

<div class="sf-card-grid">
<div class="sf-card"><span>Company</span><strong>Shriram Finance Ltd</strong></div>
<div class="sf-card"><span>NSE</span><strong>SHRIRAMFIN</strong></div>
<div class="sf-card"><span>Current Price</span><strong>\u20b91,041</strong></div>
<div class="sf-card"><span>P/E</span><strong>21.5x</strong></div>
<div class="sf-card"><span>ROE</span><strong>16.4%</strong></div>
<div class="sf-card"><span>Debt / Equity</span><strong>3.80x</strong></div>
</div>

<h2 id="scale">Business Scale &amp; Operational Growth</h2>

<div class="sf-table-wrap">
<table class="sf-table">
<tr><th>Operational Metric</th><th>Mar 2024</th><th>Mar 2025</th><th>Mar 2026</th></tr>
<tr><td>Total AUM</td><td>\u20b92.25 Lakh Cr</td><td>\u20b92.63 Lakh Cr</td><td><strong>\u20b93.02 Lakh Cr</strong></td></tr>
<tr><td>Gross Stage 3 Assets</td><td>~4.61%</td><td>4.55%</td><td><strong>4.58%</strong></td></tr>
<tr><td>Branches</td><td>~2,925</td><td>3,120</td><td><strong>3,225</strong></td></tr>
<tr><td>Customers</td><td>~7.2 Million</td><td>8.4 Million</td><td><strong>8.9 Million</strong></td></tr>
</table>
</div>

<h2 id="business">What Does Shriram Finance Do?</h2>

<p>Shriram Finance borrows money from different sources and lends that money to
individuals, small businesses and vehicle owners. It earns interest and fees from
those loans while managing credit losses, borrowing costs and operating expenses.</p>

<p class="sf-warning">This article is an educational stock analysis, not a buy or
sell recommendation.</p>

</div>
"""


class TestTheLiveShriramFinancePost:
    """The checklist question, asked of the feed's own post.

    Everything here is the real thing: the post id, title, labels, slug and
    timestamps the live feed served on 2026-09-05, its markup including the
    stylesheet and the leading comment, the sync, the document worker, the
    retrieval engine and the public endpoint. Only the language model is the
    offline stand-in this repository's tests always use — so what is asserted is
    the part retrieval owns: that the post was found, that the answer is marked
    grounded because of it, and that the citation points back at the post.
    """

    @pytest.fixture()
    def shriramfin(self, db_session):
        # Not in the seeded register, and created here rather than added to the
        # seed: the seed is the platform's, not this feature's.
        row = Company(
            id=str(uuid.uuid4()), ticker="SHRIRAMFIN",
            name="Shriram Finance Ltd", exchange="NSE",
        )
        db_session.add(row)
        db_session.commit()
        yield row
        db_session.query(Document).filter(
            Document.company_id == row.id
        ).delete(synchronize_session=False)
        db_session.delete(row)
        db_session.commit()

    def test_a_safe_question_is_answered_from_the_post(
        self, client, db_session, storage, monkeypatch, shriramfin,
    ):
        monkeypatch.setattr(settings, "BLOGGER_PUBLIC_TICKERS", "SHRIRAMFIN")
        post = _ingest_post(
            db_session, storage, monkeypatch,
            content=LIVE_HTML, labels=LIVE_LABELS, post_id=LIVE_POST_ID,
            title=LIVE_TITLE, slug=LIVE_SLUG,
            published=datetime(2026, 9, 5, 12, 35, 6, 786000, tzinfo=timezone.utc),
        )
        assert post.url == LIVE_URL

        body = _ask(
            client, ticker="SHRIRAMFIN",
            question="What was Shriram Finance's total AUM in March 2026?",
        ).json()

        assert body["ticker"] == "SHRIRAMFIN"
        assert body["company"]
        assert body["answer"]
        # Grounded because a document was retrieved, not because the model said so.
        assert body["grounded"] is True

        urls = [c["url"] for c in body["citations"] if c.get("url")]
        assert LIVE_URL in urls, body["citations"]
        cited = next(c for c in body["citations"] if c.get("url") == LIVE_URL)
        assert "Blogger post" in cited["label"]
        assert cited["document_type"] == "research_note"
        assert cited["document_title"] == LIVE_TITLE

        # The figure the question asks about is in the evidence that was
        # retrieved — the table survived the parser, stylesheet and all.
        evidence = " ".join(c.get("snippet") or "" for c in body["citations"])
        assert "AUM" in evidence
        assert "3.02" in evidence

        # And the template did not become knowledge.
        assert "font-family" not in evidence
        assert "Ready to paste into Blogger" not in evidence

    def test_the_same_post_in_hinglish_is_still_grounded(
        self, client, db_session, storage, monkeypatch, shriramfin,
    ):
        monkeypatch.setattr(settings, "BLOGGER_PUBLIC_TICKERS", "SHRIRAMFIN")
        _ingest_post(
            db_session, storage, monkeypatch,
            content=LIVE_HTML, labels=LIVE_LABELS, post_id=LIVE_POST_ID,
            title=LIVE_TITLE, slug=LIVE_SLUG,
        )
        body = _ask(
            client, ticker="SHRIRAMFIN",
            question="Shriram Finance ka total AUM March 2026 mein kitna tha?",
        ).json()
        assert body["answer"]
        assert body["grounded"] is True
        assert any(c.get("url") == LIVE_URL for c in body["citations"])


class TestThePublicRequestCompletes:
    """The request has to *finish*, and finish inside a bound.

    Everything upstream of the model is fast — sync, worker, retrieval,
    grounding and the citation audit complete in well under a second. What is
    not fast is the provider chain behind `analyst.chat`: three attempts at a
    sixty-second HTTP timeout for each of four providers, so 182 seconds with
    one provider configured and 726 with all four. A public endpoint that
    inherits that patience hands the reader a hung request: the work is done,
    the answer exists, and the proxy in front gave up a minute ago.

    These tests drive the real ASGI application over a real event loop rather
    than through `TestClient`, and bound every call with `asyncio.wait_for`, so
    a regression here fails with a message instead of hanging the suite — and
    cannot pass without an actual completed response.
    """

    #: Generous against a measured ~0.15 s, tight enough that a hang is a
    #: failure rather than a slow test.
    BUDGET = 25.0

    @staticmethod
    def _drive(coro_factory):
        """Run one coroutine to completion, or fail with the elapsed time."""
        async def runner():
            started = time.monotonic()
            try:
                return await asyncio.wait_for(coro_factory(), timeout=25.0), \
                    time.monotonic() - started
            except (asyncio.TimeoutError, TimeoutError):  # pragma: no cover
                pytest.fail(
                    "the public chat endpoint did not return a response within "
                    f"{time.monotonic() - started:.1f} s"
                )
        return asyncio.run(runner())

    def test_the_live_shriramfin_question_returns_a_completed_response(
        self, db_session, storage, monkeypatch, shriramfin,
    ):
        """The checklist request, over HTTP, to completion.

        `POST /api/v1/blogger/chat` with `ticker=SHRIRAMFIN` and the question a
        reader would actually ask, asserted on the response that arrived rather
        than on anything observed inside the pipeline.
        """
        from httpx import ASGITransport, AsyncClient

        from app.main import app

        monkeypatch.setattr(settings, "BLOGGER_PUBLIC_TICKERS", "SHRIRAMFIN")
        company = shriramfin
        # The live post, through the real sync and the real document worker, so
        # the question below has something to be answered from.
        _ingest_post(
            db_session, storage, monkeypatch,
            content=LIVE_HTML, labels=LIVE_LABELS, post_id=LIVE_POST_ID,
            title=LIVE_TITLE, slug=LIVE_SLUG,
        )
        payload = {
            "ticker": "SHRIRAMFIN",
            "question": "What was Shriram Finance's total AUM in March 2026?",
            "session_id": "integration",
            "language": "auto",
        }

        async def call():
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://blogger.test", timeout=20.0,
            ) as client:
                return await client.post("/api/v1/blogger/chat", json=payload)

        response, elapsed = self._drive(call)

        assert response.status_code == 200, response.text[:400]
        body = response.json()
        assert body["ticker"] == "SHRIRAMFIN"
        assert body["company"] == "Shriram Finance Ltd"
        assert body["answer"]
        assert body["grounded"] is True
        assert body["citations"], "a grounded answer carries its evidence"
        assert any(
            citation.get("url") == LIVE_URL for citation in body["citations"]
        ), body["citations"]
        evidence = " ".join(c.get("snippet") or "" for c in body["citations"])
        assert "3.02" in evidence, "the figure asked for is in the evidence"
        assert elapsed < self.BUDGET
        # Recorded, so the operator can see what the blog cost — the write
        # happens after the answer and must not be what the reader waits on.
        from app.models.ai import AIAnalysis
        assert db_session.query(AIAnalysis).filter(
            AIAnalysis.company_id == company.id
        ).count() == 1

    def test_a_generation_that_outlives_its_budget_returns_504(
        self, client, db_session, monkeypatch, indexed_post,
    ):
        """Bounded, not hung: the reader gets a retryable answer about failure.

        The model is made slower than the budget. What must happen is a 504
        inside that budget — not a 500, not a partial answer, and not a request
        that is still running when the caller has gone.
        """
        from app.services.ai.service import AIService

        budget = 1.0     # the floor: below it the setting is read as a typo
        monkeypatch.setattr(settings, "BLOGGER_CHAT_TIMEOUT_SECONDS", budget)
        monkeypatch.setattr(
            "app.api.v1.blogger.AIService", _slowed(AIService, delay=8.0),
        )

        from app.models.ai import AIAnalysis

        def recorded():
            return db_session.query(AIAnalysis).count()

        before = recorded()
        started = time.perf_counter()
        response = _ask(client, question="What was the AUM in March 2026?")
        elapsed = time.perf_counter() - started

        assert response.status_code == 504, response.text[:300]
        assert "too long" in response.json()["detail"]
        # Bounded by the budget, not by the model's patience.
        assert elapsed < budget + 2.0, elapsed
        # And nothing half-written: an answer that never arrived is not
        # recorded as though it had. The count is taken before the call because
        # the shared in-memory database carries rows from earlier tests.
        assert recorded() == before

    def test_the_budget_is_read_from_configuration(self, client, monkeypatch,
                                                   indexed_post):
        # A deployment behind a thirty-second proxy can say so, and the
        # endpoint obeys rather than applying a number baked into the code.
        monkeypatch.setattr(settings, "BLOGGER_CHAT_TIMEOUT_SECONDS", 30.0)
        assert settings.blogger_chat_timeout_seconds == 30.0
        # A typo cannot close the endpoint or unbound it.
        monkeypatch.setattr(settings, "BLOGGER_CHAT_TIMEOUT_SECONDS", 0)
        assert settings.blogger_chat_timeout_seconds == 45.0
        monkeypatch.setattr(settings, "BLOGGER_CHAT_TIMEOUT_SECONDS", 99999)
        assert settings.blogger_chat_timeout_seconds == 300.0
        assert _ask(client, question="What does the company do?").status_code == 200

    def test_other_readers_are_not_held_up_by_one_slow_answer(
        self, db_session, monkeypatch, indexed_post,
    ):
        """The loop stays free while one answer is being generated.

        The handler is `async def` over blocking database work. Run on the event
        loop, that work stalls every other request for as long as it takes; in a
        thread, a reader waiting on a slow model costs the others nothing. This
        holds a chat open for most of a second and asks for the status in the
        middle of it.
        """
        from httpx import ASGITransport, AsyncClient

        from app.main import app
        from app.services.ai.service import AIService

        monkeypatch.setattr(
            "app.api.v1.blogger.AIService", _slowed(AIService, delay=0.9),
        )

        async def scenario():
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://blogger.test", timeout=20.0,
            ) as http:
                started = time.monotonic()
                chat = asyncio.create_task(
                    http.post("/api/v1/blogger/chat", json={
                        "ticker": PUBLISHED, "question": "How is distribution?",
                        "session_id": "slow", "language": "auto",
                    })
                )
                await asyncio.sleep(0.05)          # let the chat get going
                probe_started = time.monotonic()
                status_response = await http.get("/api/v1/blogger/status")
                probe = time.monotonic() - probe_started
                chat_response = await chat
                return chat_response, status_response, probe, \
                    time.monotonic() - started

        chat_response, status_response, probe, total = asyncio.run(
            asyncio.wait_for(scenario(), timeout=self.BUDGET)
        )

        assert chat_response.status_code == 200, chat_response.text[:300]
        assert chat_response.json()["grounded"] is True
        assert status_response.status_code == 200
        # Served while the chat was still generating, not after it.
        assert probe < 0.4, f"the status call waited {probe:.2f}s behind the chat"
        assert total >= 0.9, "the chat really was still running"

    @pytest.fixture()
    def shriramfin(self, db_session):
        # Not in the seeded register, and created here rather than added to the
        # seed: the seed is the platform's, not this feature's. The database is
        # shared for the whole session, so anything this test writes it also
        # takes back — `test_blogger_sync.py` inserts the same ticker and would
        # otherwise meet a UNIQUE constraint.
        from app.models.ai import AIAnalysis

        row = Company(
            id=str(uuid.uuid4()), ticker="SHRIRAMFIN",
            name="Shriram Finance Ltd", exchange="NSE",
        )
        db_session.add(row)
        db_session.commit()
        yield row
        db_session.query(AIAnalysis).filter(
            AIAnalysis.company_id == row.id
        ).delete(synchronize_session=False)
        db_session.query(Document).filter(
            Document.company_id == row.id
        ).delete(synchronize_session=False)
        db_session.delete(row)
        db_session.commit()


def _slowed(service_class, *, delay: float):
    """`service_class` with an analyst whose generation takes `delay` seconds.

    A subclass rather than a stand-in, so memory, recording and every other
    collaborator stay the real thing: what is being measured is the endpoint's
    handling of a slow model, not a fake service's behaviour.
    """

    class _Slowed(service_class):  # type: ignore[valid-type,misc]
        def analyst_for(self, analysis):
            analyst = super().analyst_for(analysis)

            class _SlowAnalyst:
                async def chat(self, question, memory, **kwargs):
                    await asyncio.sleep(delay)
                    return await analyst.chat(question, memory, **kwargs)

            return _SlowAnalyst()

    return _Slowed


# ---------------------------------------------------------------------------
# The deployment around the endpoint
# ---------------------------------------------------------------------------
class TestDeploymentSurface:
    def test_the_blog_origin_is_allowed_explicitly(self):
        text = COMPOSE.read_text()
        assert "https://equitypilot.blogspot.com" in text
        # A wildcard with credentials enabled is either ignored by the browser
        # or an open door; neither is acceptable here.
        cors_lines = [
            line for line in text.splitlines() if "CORS_ORIGINS" in line
        ]
        assert cors_lines
        assert all('"*"' not in line for line in cors_lines)

    def test_the_blog_origin_is_https(self):
        text = COMPOSE.read_text()
        assert "http://equitypilot.blogspot.com" not in text

    def test_the_compose_file_carries_the_blogger_settings(self):
        text = COMPOSE.read_text()
        for name in ("BLOGGER_ENABLED", "BLOGGER_FEED_URL", "BLOGGER_MAX_POSTS",
                     "BLOGGER_SYNC_TIMEOUT_SECONDS", "BLOGGER_CHAT_TIMEOUT_SECONDS",
                     "BLOGGER_PUBLIC_TICKERS", "BLOGGER_SYNC_SECRET",
                     "BLOGGER_DEFAULT_TICKER"):
            assert name in text, name
        # The credential is injected, not committed.
        assert 'BLOGGER_SYNC_SECRET: ""' in text

    def test_the_widget_exists_and_holds_no_credentials(self):
        assert WIDGET.exists(), "the embeddable widget is part of the deliverable"
        text = WIDGET.read_text()
        for needle in ("api_key", "apikey", "Bearer ", "SECRET_KEY",
                       "ENCRYPTION_KEY", "password", "postgres"):
            assert needle.lower() not in text.lower(), needle
        # It calls the public API by relative-or-configured URL, never a
        # hard-coded internal host.
        assert "localhost" not in text
        assert "127.0.0.1" not in text

    def test_the_widget_points_at_the_public_endpoint(self):
        text = WIDGET.read_text()
        # The base URL is configurable and defaults to the platform's v1 API;
        # the two paths it calls are the only two public endpoints.
        assert "https://equitypilot.in/api/v1" in text
        assert "\"/blogger/status\"" in text
        assert "\"/blogger/chat\"" in text
        # And it calls nothing else on the platform: no admin, no company
        # workspace, no upload route.
        for forbidden in ("/admin", "/company/", "/documents", "/auth", "/platform"):
            assert forbidden not in text, forbidden

    def test_the_widget_does_not_claim_to_be_an_iframe_of_the_app(self):
        # The frontend sends X-Frame-Options: DENY, so an iframe embed cannot
        # work; a widget that tried would render as a blank box.
        text = WIDGET.read_text().lower()
        assert "<iframe" not in text
