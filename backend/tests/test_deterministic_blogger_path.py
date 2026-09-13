"""The public Blogger chat endpoint over the deterministic path.

The endpoint's public contract — fields, grounding honesty, citations,
disclosure — must not change when a canonical financial question is answered
by the provider-free path instead of a model. These tests drive the real
endpoint with a spy router so that any provider call is a loud failure, and
pin:

* a supported intent is answered with no provider call and no RAG;
* the response shape and grounding fields are exactly the existing contract;
* an unsupported question still goes to the existing provider path;
* nothing about the internal provider is leaked in the public payload.
"""
from __future__ import annotations

import uuid

import pytest

from app.core.config import settings
from app.domain.ai.types import CompletionRequest, CompletionResponse
from app.services.ai import service as ai_service
from app.services.ai.providers.base import ProviderConfig
from app.services.ai.providers.mock import OfflineProvider
from tests.test_deterministic_analyst_path import (
    INTENT_QUESTIONS, SpyRouter,
)

TICKER = "BHARATCP"

#: The public contract: exactly these fields, no provider internals.
CONTRACT_FIELDS = {
    "ticker", "company", "answer", "grounded", "citations", "warnings",
    "disclosure", "language", "session_id", "turn_count",
}


@pytest.fixture(autouse=True)
def blogger_settings(monkeypatch):
    monkeypatch.setattr(settings, "BLOGGER_ENABLED", True)
    monkeypatch.setattr(settings, "BLOGGER_PUBLIC_TICKERS", TICKER)
    monkeypatch.setattr(settings, "BLOGGER_DEFAULT_TICKER", "")
    monkeypatch.setattr(settings, "BLOGGER_SYNC_SECRET", "")
    monkeypatch.setattr(settings, "AI_PREFERRED_PROVIDER", None)


@pytest.fixture()
def db_session():
    from tests.conftest import TestingSession

    session = TestingSession()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture()
def client(api_client):
    return api_client


@pytest.fixture()
def router(monkeypatch) -> SpyRouter:
    """The provider chain the endpoint will use — with call recording."""
    spy = SpyRouter(mode="offline")
    monkeypatch.setattr(ai_service, "_router", spy)
    return spy


def _ask(client, question: str, session_id: str | None = None) -> dict:
    response = client.post(
        "/api/v1/blogger/chat",
        json={
            "ticker": TICKER, "question": question,
            "session_id": session_id or f"det-{uuid.uuid4().hex[:12]}",
            "language": "auto",
        },
    )
    assert response.status_code == 200, response.text[:400]
    return response.json()


class TestBloggerDeterministicPath:
    @pytest.mark.parametrize("intent,question", list(INTENT_QUESTIONS.items()))
    def test_supported_intent_never_reaches_a_provider(self, client, router,
                                                       intent, question):
        body = _ask(client, question)

        # The whole point: no model served this answer.
        assert router.complete_calls == []

        # The public contract is intact.
        assert body["ticker"] == TICKER
        assert body["answer"]
        assert body["grounded"] is (intent != "pb")
        assert body["turn_count"] >= 1
        # Every cited figure is reported with its provenance — by label, the
        # public contract's unit of citation (internal keys never leak).
        if intent != "pb":
            assert body["citations"]
            labels = {c["label"] for c in body["citations"]}
            expected = {
                "pe": {"Trailing P/E"},
                "debt": {"Gross debt", "Net debt"},
                "roe": {"Return on equity"},
                "roce": {"Return on capital employed"},
                "eps": {"EPS (basic)"},
                "market_price": {"Current market price"},
            }.get(intent)
            if expected:
                assert expected <= labels
        # The normal disclosure remains.
        assert body["disclosure"]

    def test_answer_cites_platform_evidence_in_display_text(self, client,
                                                            router):
        body = _ask(client, "What is the current P/E?")

        # The display answer carries the annotated citation, not the raw key.
        assert "[Trailing P/E]" in body["answer"]
        assert "[pe_ratio]" not in body["answer"]
        # And the cited evidence is the platform's own figure, reported in
        # the citation list the widget renders with its provenance.
        pe = next(c for c in body["citations"] if c["label"] == "Trailing P/E")
        assert pe["kind"] == "valuation"
        assert pe["source"]

    def test_response_shape_is_the_existing_contract(self, client, router):
        body = _ask(client, "What is the total debt?")
        assert set(body.keys()) == CONTRACT_FIELDS
        # No provider internals leak into the public payload.
        for field in ("provider", "model", "prompt_tokens", "completion_tokens",
                      "cost_usd"):
            assert field not in body

    def test_unsupported_question_still_uses_the_provider_path(self, client,
                                                               router):
        body = _ask(client, "What are the main risks for the company?")

        # The existing provider pipeline served it — the deterministic path
        # declined because no single supported intent is named.
        assert len(router.complete_calls) == 1
        assert body["answer"]
        assert body["disclosure"]

    def test_multi_intent_question_goes_to_the_provider(self, client, router):
        body = _ask(client, "What is the P/E and the ROE?")
        # No partial deterministic answer for a multi-part question.
        assert len(router.complete_calls) == 1
        assert body["answer"]

    def test_usage_is_recorded_as_deterministic(self, client, router,
                                                db_session):
        """The operator's usage ledger must show a zero-cost deterministic
        call, not a phantom provider bill."""
        from app.models.ai import AIUsageRecord
        from sqlalchemy import func, select

        before = db_session.execute(
            select(func.count()).select_from(AIUsageRecord)
        ).scalar() or 0

        _ask(client, "What is the current market price?")
        db_session.commit()

        rows = db_session.execute(
            select(AIUsageRecord).order_by(AIUsageRecord.id.desc()).limit(1)
        ).scalars().all()
        assert len(rows) >= 1
        latest = rows[0]
        assert latest.provider == "deterministic"
        assert latest.model == "none"
        assert latest.prompt_tokens == 0
        assert latest.completion_tokens == 0
        assert latest.cost_usd == 0.0
        assert db_session.execute(
            select(func.count()).select_from(AIUsageRecord)
        ).scalar() >= before + 1
