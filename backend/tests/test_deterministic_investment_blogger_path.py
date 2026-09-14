"""The public Blogger chat endpoint over the Phase 2A deterministic path.

The endpoint's public contract — fields, grounding honesty, citations,
disclosure — must not change when an investment-intelligence question is
answered by the provider-free path instead of a model. These tests drive
the real endpoint with a spy router so that any provider call is a loud
failure, and pin:

* a supported Phase 2A intent (English and Hinglish) is answered with no
  provider call and no RAG, with grounded=True;
* the response shape and grounding fields are exactly the existing contract;
* a multi-intent or unsupported question still goes to the existing
  provider path;
* the usage ledger records the deterministic call at zero cost.
"""
from __future__ import annotations

import uuid

import pytest

from app.core.config import settings
from app.services.ai import service as ai_service
from tests.test_deterministic_analyst_path import SpyRouter

TICKER = "BHARATCP"

#: The public contract: exactly these fields, no provider internals.
CONTRACT_FIELDS = {
    "ticker", "company", "answer", "grounded", "citations", "warnings",
    "disclosure", "language", "session_id", "turn_count",
}

#: One English question per Phase 2A intent, plus Hinglish variants.
INTENT_QUESTIONS = {
    "overall_assessment": "What is the overall assessment?",
    "financial_quality": "What is the financial quality score?",
    "growth_quality": "What is the growth quality score?",
    "financial_risk": "How much financial risk is there?",
    "strengths": "What are the company's strengths?",
    "weaknesses": "What are the company's weaknesses?",
    "investment_case": "What is the investment case?",
    "recommendation": "What is the investment recommendation?",
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


@pytest.fixture(autouse=True)
def fake_translator(monkeypatch):
    """A deterministic stand-in for the outbound translation: no network,
    same protect → translate → restore → verify path (the pattern from
    test_multilingual_chat_flow.py)."""
    from app.services.language.translators import TranslationResult

    class FakeTranslator:
        name = "fake-hinglish"

        def supports(self, language):
            return True

        async def translate(self, text, language, *, entities=None):
            return TranslationResult(
                text=f"(Hinglish) {text}", language=language,
                translated=True, provider=self.name,
            )

    monkeypatch.setattr(
        "app.services.language.adapter.build_translator",
        lambda *args, **kwargs: FakeTranslator(),
    )


def _ask(client, question: str, language: str = "auto") -> dict:
    response = client.post(
        "/api/v1/blogger/chat",
        json={
            "ticker": TICKER, "question": question,
            "session_id": f"det2a-{uuid.uuid4().hex[:12]}",
            "language": language,
        },
    )
    assert response.status_code == 200, response.text[:400]
    return response.json()


class TestBloggerPhase2ADeterministicPath:
    @pytest.mark.parametrize("intent,question", list(INTENT_QUESTIONS.items()))
    def test_supported_intent_never_reaches_a_provider(self, client, router,
                                                       intent, question):
        body = _ask(client, question)

        # The whole point: no model served this answer.
        assert router.complete_calls == []

        # The public contract is intact, and the answer is grounded on
        # platform evidence.
        assert set(body.keys()) == CONTRACT_FIELDS
        assert body["ticker"] == TICKER
        assert body["answer"]
        assert body["grounded"] is True
        assert body["turn_count"] >= 1
        assert body["citations"]
        assert body["disclosure"]

    def test_hinglish_question_is_deterministic_too(self, client, router):
        body = _ask(client, "BEL kaisi company hai?")

        assert router.complete_calls == []
        assert body["grounded"] is True
        assert body["answer"]
        # The language block reports the detected language.
        assert body["language"] is not None

    def test_answer_cites_platform_evidence_in_display_text(self, client,
                                                            router):
        body = _ask(client, "What is the overall assessment?")

        # The display answer carries annotated citations, not raw keys.
        assert "[Institutional score]" in body["answer"]
        assert "[overall_score]" not in body["answer"]
        # And the cited evidence is the platform's own figure, with its
        # provenance in the public citation list.
        score = next(c for c in body["citations"]
                     if c["label"] == "Institutional score")
        assert score["kind"] == "scoring"
        assert score["source"]

    def test_no_provider_internals_leak(self, client, router):
        body = _ask(client, "What is the investment case?")
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

    @pytest.mark.parametrize("question", [
        # Phase 2A + Phase 2A
        "BEL ki financial quality aur growth kaisi hai?",
        # Phase 1 + Phase 2A
        "What is the P/E and what is the overall assessment?",
        # Phase 1 + Phase 1 (regression)
        "What is the P/E and the ROE?",
    ])
    def test_multi_intent_question_goes_to_the_provider(self, client, router,
                                                        question):
        body = _ask(client, question)
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

        _ask(client, "BEL buy hai ya hold?")
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
