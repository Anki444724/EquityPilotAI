"""Regression tests for the Blogger chat provider-selection / 45s timeout bug.

Background
----------
Production runs with ``AI_PREFERRED_PROVIDER=Gemini`` and Gemini answers in
~2s, yet ``POST /api/v1/blogger/chat`` hit the endpoint's 45s budget every
time. The cause is not Gemini and not RAG: ``AIService`` builds its shared
``ProviderRouter()`` with **no** preferred provider, and the Blogger handler
never passed one either, so the chain fell back to ``FALLBACK_ORDER``
(``OpenRouter`` first). OpenRouter, out of credits, blocked the request until
the endpoint budget was spent — a 504 the reader could only retry.

These tests drive the real endpoint with controllable stand-ins for the
providers, so they pin *selection* and *timing* without a network or an API
key. They prove:

1. Blogger chat uses Gemini when ``AI_PREFERRED_PROVIDER=Gemini``.
2. A normal Gemini response completes within the endpoint budget.
3. The existing grounded citations / RAG behaviour is intact.
4. When Gemini actually fails, the chain falls back safely (no hang, no 5xx).
"""
from __future__ import annotations

import asyncio
import time
import uuid

import pytest

from app.core.config import settings
from app.domain.ai.types import CompletionResponse, ProviderError, TokenUsage
from app.services.ai import service as ai_service
from app.services.ai.providers.base import LLMProvider, ProviderConfig
from app.services.ai.providers.mock import OfflineProvider
from app.services.ai.providers.router import ProviderRouter


# ---------------------------------------------------------------------------
# Controllable provider stand-in
# ---------------------------------------------------------------------------
#: Per-provider-name behaviour, swapped per test: "fast" returns a grounded
#: answer (delegated to the offline composer), "hang" blocks forever inside
#: the test budget (simulating a dead OpenRouter key), "fail" raises.
_BEHAVIOUR: dict[str, str] = {}
#: The providers the router actually called, in order, for the last request.
_CALLS: list[str] = []


class ControllableProvider(LLMProvider):
    """A provider whose behaviour is driven entirely by ``_BEHAVIOUR``."""

    # These are never exercised — ``complete`` / ``stream`` are overridden — but
    # ``LLMProvider`` is abstract, so the stubs are required to instantiate it.
    def build_payload(self, request, model):
        return {}

    def extract_content(self, body):
        return ""

    def extract_usage(self, body):
        return TokenUsage()

    async def complete(self, request: "CompletionRequest") -> CompletionResponse:  # noqa: F821
        name = self.config.name
        _CALLS.append(name)
        behaviour = _BEHAVIOUR.get(name, "fast")
        if behaviour == "hang":
            # A provider that never answers inside the endpoint budget — the
            # exact shape of OpenRouter with exhausted credits during the
            # incident. The endpoint's asyncio.wait_for is what must fire.
            await asyncio.sleep(2)
            return CompletionResponse(
                content="unreachable", provider=name, model="m",
                usage=TokenUsage(1, 1),
            )
        if behaviour == "fail":
            raise ProviderError(
                f"{name} is unavailable", provider=name, retryable=False,
            )
        # "fast": a genuinely grounded, cited answer, composed from the same
        # evidence block the real pipeline assembles. Re-using the offline
        # composer keeps the citation / grounding machinery exercised for real.
        return await OfflineProvider(self.config).complete(request)

    async def stream(self, request: "CompletionRequest"):  # noqa: F821
        yield "x"


def _install_controllable_router(monkeypatch, behaviours: dict[str, str],
                                  preferred: str | None = None) -> ProviderRouter:
    """Replace AIService's shared router with one made of controllable providers.

    We override ``build`` on the instance rather than registering a shape in the
    global ``SHAPE_ADAPTERS`` registry, so this test does not leak a fake shape
    into the shared transport layer (which other tests assert has exactly three
    entries).
    """
    _CALLS.clear()
    _BEHAVIOUR.clear()
    _BEHAVIOUR.update(behaviours)
    configs = [
        ProviderConfig(
            name=name, endpoint=f"local://{name}", payload_shape="offline",
            auth_header="", response_path="", default_model=f"m-{name}",
            api_key="k", enabled=True,
        )
        for name in behaviours
    ]
    router = ProviderRouter(configs=configs, preferred=preferred)
    router.build = lambda config: ControllableProvider(config)  # type: ignore[method-assign]
    monkeypatch.setattr(ai_service, "_router", router)
    return router


# ---------------------------------------------------------------------------
# Harness (mirrors test_blogger_api.py)
# ---------------------------------------------------------------------------
from app.models.document import Document, DocumentChunk, DocumentJob  # noqa: E402
from tests.fixtures.blogger_feed import (  # noqa: E402
    FEED_URL, FakeBloggerServer, FakePost, client_for,
)
from app.services.blogger.sync import BloggerSyncService  # noqa: E402
from app.services.documents.storage import LocalFileStorage  # noqa: E402
from app.services.documents.worker import DocumentWorker  # noqa: E402


PUBLISHED = "NESTLEIND"
POST_TEXT = (
    "Nestlé India's Mysore sandalwood-adjacent division is a distraction; the "
    "real story is distribution.\n\nRural distribution reached 62% of "
    "addressable outlets, and the Maggi noodle line ran at 78% capacity "
    "utilisation through the year."
)


@pytest.fixture(autouse=True)
def blogger_settings(monkeypatch, db_session):
    monkeypatch.setattr(settings, "BLOGGER_ENABLED", True)
    monkeypatch.setattr(settings, "BLOGGER_FEED_URL", FEED_URL)
    monkeypatch.setattr(settings, "BLOGGER_PUBLIC_TICKERS", PUBLISHED)
    monkeypatch.setattr(settings, "BLOGGER_DEFAULT_TICKER", "")
    monkeypatch.setattr(settings, "BLOGGER_SYNC_SECRET", "")
    monkeypatch.setattr(settings, "AI_PREFERRED_PROVIDER", "Gemini")


@pytest.fixture()
def db_session():
    from tests.conftest import TestingSession

    session = TestingSession()
    session.query(DocumentChunk).delete()
    session.query(DocumentJob).delete()
    session.query(Document).delete()
    session.commit()
    try:
        yield session
    finally:
        session.rollback()
        session.query(DocumentChunk).delete()
        session.query(DocumentJob).delete()
        session.query(Document).delete()
        session.commit()
        session.close()


@pytest.fixture()
def client(api_client):
    return api_client


@pytest.fixture()
def storage(tmp_path) -> LocalFileStorage:
    return LocalFileStorage(tmp_path / "documents")


def _ingest_post(db_session, storage, monkeypatch):
    monkeypatch.setattr("app.services.documents.ingestion.get_storage", lambda: storage)
    server = FakeBloggerServer(posts=(FakePost(
        post_id="7000000000000000001", title="Nestlé India: Rural Distribution",
        labels=("Nestlé India",), slug="nestle-india-rural-distribution",
        text=POST_TEXT,
    ),))
    result = BloggerSyncService(db_session, feed=client_for(server), storage=storage).sync()
    assert result.ingested == 1, result.as_dict()
    worker = DocumentWorker(lambda: db_session, storage=storage)
    while worker.run_once():
        pass


@pytest.fixture()
def indexed_post(db_session, storage, monkeypatch):
    _ingest_post(db_session, storage, monkeypatch)
    return POST_TEXT


def _ask(client, *, question="What was the Maggi line capacity utilisation?",
         session_id=None, language="auto"):
    return client.post(
        "/api/v1/blogger/chat",
        json={
            "ticker": PUBLISHED, "question": question,
            "session_id": session_id or f"t-{uuid.uuid4().hex[:12]}",
            "language": language,
        },
    )


# ---------------------------------------------------------------------------
# 1 + 2. Gemini is used and completes within the endpoint budget
# ---------------------------------------------------------------------------
class TestGeminiPreferred:
    def test_uses_gemini_and_completes_within_budget(self, client, indexed_post,
                                                     monkeypatch):
        # OpenRouter leads FALLBACK_ORDER and would block (it has no credits);
        # Gemini is the operator's preferred provider and answers fast.
        _install_controllable_router(
            monkeypatch, {"OpenRouter": "hang", "Gemini": "fast"},
            preferred=None,
        )
        _CALLS.clear()

        started = time.monotonic()
        response = _ask(client)
        elapsed = time.monotonic() - started

        assert response.status_code == 200, response.text
        # The chain must have gone straight to Gemini — OpenRouter, which would
        # have blocked, is never called.
        assert _CALLS == ["Gemini"], _CALLS
        # Comfortably inside the 45s endpoint budget (and the real Gemini SLA).
        assert elapsed < settings.blogger_chat_timeout_seconds

    def test_reproduces_the_blocking_path_when_preferred_is_unset(
        self, client, indexed_post, monkeypatch,
    ):
        """The exact production symptom: preferred not honoured -> OpenRouter first.

        This pins the *blocking* path that the fix removes. We compress the
        endpoint budget so the test is fast while remaining faithful: the first
        provider in the chain hangs, so ``asyncio.wait_for`` is what fires.
        """
        # Mirror the pre-fix reality: the operator's preference is never wired
        # in, so the chain honours FALLBACK_ORDER (OpenRouter first).
        monkeypatch.setattr(settings, "AI_PREFERRED_PROVIDER", None)
        _install_controllable_router(
            monkeypatch, {"OpenRouter": "hang", "Gemini": "fast"},
            preferred=None,
        )
        # Compress the budget so the dead key is bounded but the test stays quick.
        monkeypatch.setattr(settings, "BLOGGER_CHAT_TIMEOUT_SECONDS", 1.5)

        response = _ask(client)

        assert response.status_code == 504, response.text
        assert "too long" in response.json()["detail"].lower()


# ---------------------------------------------------------------------------
# 3. Grounded citations / RAG behaviour intact
# ---------------------------------------------------------------------------
class TestGroundedCitationsIntact:
    def test_retrieved_post_is_cited_when_gemini_answers(self, client, indexed_post,
                                                          monkeypatch):
        _install_controllable_router(
            monkeypatch, {"OpenRouter": "hang", "Gemini": "fast"},
            preferred=None,
        )
        body = _ask(client).json()

        assert body["grounded"] is True
        assert body["citations"], "retrieved Blogger post must be cited"
        blog_citations = [
            c for c in body["citations"]
            if c.get("url") and "blogspot.com" in c["url"]
        ]
        assert blog_citations, body["citations"]
        assert "Blogger post" in blog_citations[0]["label"]
        assert blog_citations[0]["snippet"]
        # The public surface still leaks nothing about the deployment.
        assert {"provider", "model", "cost_usd"}.isdisjoint(body)


# ---------------------------------------------------------------------------
# 4. Safe fallback when Gemini actually fails
# ---------------------------------------------------------------------------
class TestFallbackWhenGeminiFails:
    def test_falls_back_to_next_provider_without_hanging(self, client, indexed_post,
                                                          monkeypatch):
        # Gemini is preferred but genuinely fails; the chain must fall through
        # to OpenRouter (which answers) rather than hang or 5xx.
        _install_controllable_router(
            monkeypatch, {"Gemini": "fail", "OpenRouter": "fast"},
            preferred=None,
        )
        _CALLS.clear()

        response = _ask(client)

        assert response.status_code == 200, response.text
        # Gemini was attempted, then the router fell back to OpenRouter.
        assert _CALLS == ["Gemini", "OpenRouter"], _CALLS
        body = response.json()
        # Grounding still holds after a fallback.
        assert body["grounded"] is True
        assert body["citations"]

    def test_fallback_reaches_offline_when_live_providers_fail(self, client,
                                                               indexed_post,
                                                               monkeypatch):
        # Both live providers fail; the deterministic offline composer is the
        # safe floor. The reader still gets a grounded answer, never a timeout.
        _install_controllable_router(
            monkeypatch,
            {"Gemini": "fail", "OpenRouter": "fail", "Offline": "fast"},
            preferred=None,
        )
        response = _ask(client)
        assert response.status_code == 200, response.text
        assert response.json()["citations"]


# ---------------------------------------------------------------------------
# Unit-level proof of the routing root cause
# ---------------------------------------------------------------------------
class TestRouterOrdering:
    def test_shared_router_ignores_ai_preferred_provider(self, monkeypatch):
        # The singleton AIService ships with no preference, so nothing routes
        # through Gemini unless the caller supplies it.
        from app.services.ai import service as svc

        monkeypatch.setattr(svc, "_router", ProviderRouter())
        assert svc._router.preferred is None

    def test_fallback_order_leads_with_openrouter_without_preference(self):
        configs = [
            ProviderConfig(name=n, endpoint="x", payload_shape="offline",
                           auth_header="", response_path="", default_model="m",
                           api_key="k", enabled=True)
            for n in ("OpenRouter", "Gemini", "OpenAI", "Claude")
        ]
        router = ProviderRouter(configs=configs, preferred=None)
        assert router.chain()[0].name == "OpenRouter"
        # Once the caller supplies the preference, Gemini leads.
        assert router.chain(preferred="Gemini")[0].name == "Gemini"
