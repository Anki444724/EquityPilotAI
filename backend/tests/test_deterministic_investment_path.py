"""The deterministic Phase 2A path inside ResearchAnalyst.

Pins the integration contract for the investment-intelligence intents, the
same way test_deterministic_analyst_path.py pins it for the Phase 1 intents:

* a single supported Phase 2A intent is answered deterministically — the
  ProviderRouter is never called and RAG retrieval never runs;
* the answer still passes through the existing citation audit, guardrail
  check, annotation and memory pipeline, with canonical-English content;
* accounting is honest: provider "deterministic", model "none", zero prompt
  tokens, zero completion tokens, zero cost, real latency;
* an unsupported or multi-intent question (including a Phase 2A intent
  mixed with a Phase 1 intent) falls through to the existing provider/RAG
  path unchanged;
* a source-restricted question keeps its existing fail-closed behaviour.

The spies record provider and retrieval calls instead of silencing them: a
call that must not happen raises, so the test fails loudly if the path
drifts.
"""
from __future__ import annotations

import asyncio

import pytest

from app.services.ai.analyst import ResearchAnalyst
from app.services.ai.context_builder import ContextBuilder
from app.services.ai.financial_intent import (
    INVESTMENT_INTENTS, FinancialIntentResolver,
)
from app.services.ai.memory import ConversationMemory
from app.services.ai.prompt_builder import PromptBuilder
from app.services.analysis_service import AnalysisService
from app.services.forecast.service import ForecastService
from app.services.scoring.service import ScoringService
from app.services.valuation.service import ValuationService
from tests.test_deterministic_analyst_path import (
    SpyDocumentService, SpyRouter, _analyst, _memory,
)

REF = "BHARATCP"


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


@pytest.fixture()
def db_session():
    from tests.conftest import TestingSession

    session = TestingSession()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


#: One question per Phase 2A intent. Hinglish on purpose: the resolver runs
#: on the raw question here (no language requested), and the patterns must
#: cover both the raw form and the adapter-normalised form.
INTENT_QUESTIONS = {
    "overall_assessment": "BEL kaisi company hai?",
    "financial_quality": "BEL ki financial quality kaisi hai?",
    "growth_quality": "BEL ki growth kaisi hai?",
    "financial_risk": "BEL me financial risk kitna hai?",
    "strengths": "BEL ki strengths kya hain?",
    "weaknesses": "BEL ki weaknesses kya hain?",
    "investment_case": "BEL me invest karne ka case kya hai?",
    "recommendation": "BEL buy hai ya hold?",
}


def _assert_intent(intent: str, question: str) -> None:
    """The test would be vacuous if the resolver did not agree this is a
    single Phase 2A intent."""
    resolved = FinancialIntentResolver().resolve(question)
    assert resolved is not None
    assert resolved.value == intent
    assert resolved in INVESTMENT_INTENTS


class TestPhase2ADeterministicPath:
    @pytest.mark.parametrize("intent,question", list(INTENT_QUESTIONS.items()))
    def test_supported_intent_is_provider_and_rag_free(self, db_session, intent,
                                                       question):
        _assert_intent(intent, question)

        analyst, router, docs, _ = _analyst(db_session, mode="forbid")
        result = _run(analyst.chat(question, _memory()))

        # No provider, no RAG — the whole point of the path.
        assert router.complete_calls == []
        assert docs.search_calls == []

        # Honest accounting: same deterministic metadata as Phase 1.
        assert result.provider == "deterministic"
        assert result.model == "none"
        assert result.prompt_tokens == 0
        assert result.completion_tokens == 0
        assert result.cost_usd == 0.0
        assert result.latency_ms >= 0.0
        assert result.cached is False
        assert result.fell_back_from is None

        # The existing verification funnel ran, on the canonical-English
        # answer, and the answer is supported by platform evidence.
        assert result.citation_audit is not None
        assert result.citation_audit.unknown_keys == []
        assert result.citation_audit.uncited_numbers == []
        assert result.citation_audit.is_supported
        assert result.guardrails is not None
        assert result.guardrails.passed
        assert "not investment advice" in result.content
        # No prompt was built: the deterministic metadata is exact.
        assert result.prompt_key == "chat"
        assert result.prompt_version == 0

        # Every cited figure is platform evidence; annotation worked.
        assert result.citations
        for citation in result.citations:
            assert f"[{citation.key}]" in result.content
            assert f"[{citation.label}]" in result.display_content

    @pytest.mark.parametrize("intent,question", list(INTENT_QUESTIONS.items()))
    def test_answer_is_deterministic(self, db_session, intent, question):
        analyst, _, _, _ = _analyst(db_session, mode="forbid")
        first = _run(analyst.chat(question, _memory())).content
        second = _run(analyst.chat(question, _memory())).content
        assert first == second

    @pytest.mark.parametrize("intent,question", list(INTENT_QUESTIONS.items()))
    def test_answer_is_built_from_the_existing_score_result(self, db_session,
                                                             intent, question):
        """The interpretation must read the SAME ScoreResult the citations
        came from: the cited figures are the scoring engine's own."""
        _assert_intent(intent, question)
        analyst, _, _, analysis = _analyst(db_session, mode="forbid")
        result = _run(analyst.chat(question, _memory()))

        context = analyst.context()
        assert context.score is not None
        for citation in result.citations:
            if citation.key in ("overall_score", "grade", "recommendation",
                                "confidence") or citation.key.startswith("score_"):
                # Each scoring figure the answer cites must exist, unchanged,
                # on the ScoreResult the ContextBuilder computed.
                assert citation.key in context.keys()
        if intent in ("overall_assessment", "investment_case", "recommendation"):
            assert f"'{context.score.recommendation}'" in result.content

    def test_memory_pipeline_records_the_turn(self, db_session):
        analyst, _, _, _ = _analyst(db_session, mode="forbid")
        memory = _memory()
        question = INTENT_QUESTIONS["recommendation"]
        result = _run(analyst.chat(question, memory))
        assert len(memory.turns) == 2
        assert memory.turns[0].role.value == "user"
        assert memory.turns[1].role.value == "assistant"
        stored = memory.turns[1].content
        assert result.content.startswith(stored)
        assert "not investment advice" not in stored
        assert "recommendation" in memory.turns[1].citations

    def test_multilingual_hinglish_runs_the_same_deterministic_path(self,
                                                                    db_session,
                                                                    monkeypatch):
        """With a language requested, the question is normalised first and
        the SAME deterministic path must serve it, rendered afterwards.

        A deterministic fake translator stands in for a real model (the
        same pattern test_multilingual_chat_flow.py uses): no network, and
        the full protect → translate → restore → verify path still runs.
        """
        from app.domain.language.types import Language
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

        analyst, router, docs, _ = _analyst(db_session, mode="forbid")
        question = "BEL kaisi company hai?"
        result = _run(analyst.chat(question, _memory(), language=Language.HINGLISH))

        assert router.complete_calls == []
        assert docs.search_calls == []
        assert result.provider == "deterministic"
        assert result.prompt_tokens == 0
        assert result.citation_audit.is_supported
        # The language pipeline ran on the deterministic answer.
        assert result.language is not None
        assert result.language["language"] == "hinglish"
        assert result.language["translation"]["translated"] is True
        # Canonical content stays English; display is rendered.
        assert "scores" in result.content
        assert result.display_content.startswith("(Hinglish)")


class TestPhase2AFallThrough:
    def test_unsupported_intent_uses_the_provider_path(self, db_session):
        analyst, router, docs, _ = _analyst(
            db_session, mode="offline", fail_on_search=False,
        )
        question = "What are the main risks for the company?"
        assert FinancialIntentResolver().resolve(question) is None

        result = _run(analyst.chat(question, _memory()))

        assert len(router.complete_calls) == 1
        assert result.provider == SpyRouter.NAME
        assert len(docs.search_calls) == 1
        assert result.citation_audit is not None
        assert result.guardrails is not None

    @pytest.mark.parametrize("question", [
        # Phase 2A + Phase 2A
        "BEL ki financial quality aur growth kaisi hai?",
        "What is the overall assessment and the recommendation?",
        # Phase 1 + Phase 2A — mixing families is multi-intent too.
        "What is the P/E and what is the overall assessment?",
        # Phase 1 + Phase 1 (regression: unchanged).
        "What is the P/E and the ROE?",
    ])
    def test_multi_intent_question_falls_through(self, db_session, question):
        analyst, router, docs, _ = _analyst(
            db_session, mode="offline", fail_on_search=False,
        )
        assert FinancialIntentResolver().resolve(question) is None

        result = _run(analyst.chat(question, _memory()))

        # No partial deterministic answer: the whole question goes to the
        # provider, and only the provider.
        assert len(router.complete_calls) == 1
        assert result.provider != "deterministic"

    def test_source_restricted_question_keeps_existing_behaviour(self,
                                                                 db_session):
        """A "documents only" question about the overall assessment must not
        be answered from the scoring engine deterministically."""
        analyst, router, docs, _ = _analyst(
            db_session, mode="offline", fail_on_search=False,
        )
        question = ("What is the overall assessment? "
                    "Answer only from the uploaded documents.")

        result = _run(analyst.chat(question, _memory()))

        # No deterministic answer, no provider call either: with no ingested
        # documents the source router declines, exactly as before.
        assert result.provider == "source-router"
        assert router.complete_calls == []
        assert "deterministic" not in result.provider

    def test_phase1_intent_still_uses_the_phase1_engine(self, db_session):
        """A Phase 1 canonical question must keep working end to end — the
        Phase 2A dispatch must not have captured it."""
        analyst, router, docs, _ = _analyst(db_session, mode="forbid")
        result = _run(analyst.chat("What is the current P/E?", _memory()))
        assert router.complete_calls == []
        assert result.provider == "deterministic"
        assert "[pe_ratio]" in result.content
        # And a Phase 1 question mixed with a Phase 2A intent declines.
        assert FinancialIntentResolver().resolve(
            "What is the P/E and the overall assessment?"
        ) is None
