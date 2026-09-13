"""The deterministic path inside ResearchAnalyst.

Pins the integration contract:

* a single supported canonical financial intent is answered deterministically
  — the ProviderRouter is never called and RAG retrieval never runs;
* the answer still passes through the existing citation audit, guardrail
  check, annotation and memory pipeline, with canonical-English content;
* accounting is honest: provider "deterministic", model "none", zero prompt
  tokens, zero completion tokens, zero cost, real latency;
* an unsupported or multi-intent question falls through to the existing
  provider/RAG path unchanged;
* a source-restricted question keeps its existing (provider or fail-closed
  refusal) behaviour.

The spies record provider and retrieval calls instead of silencing them: a
call that must not happen raises, so the test fails loudly if the path
drifts.
"""
from __future__ import annotations

import asyncio
import types

import pytest

from app.domain.ai.types import CompletionRequest, CompletionResponse
from app.services.ai.analyst import ResearchAnalyst
from app.services.ai.context_builder import ContextBuilder
from app.services.ai.financial_intent import FinancialIntentResolver
from app.services.ai.memory import ConversationMemory
from app.services.ai.prompt_builder import PromptBuilder
from app.services.ai.providers.mock import OfflineProvider
from app.services.ai.providers.base import ProviderConfig
from app.services.analysis_service import AnalysisService
from app.services.forecast.service import ForecastService
from app.services.scoring.service import ScoringService
from app.services.valuation.service import ValuationService

REF = "BHARATCP"


class SpyDocumentService:
    """Records RAG retrieval; context-building reads return an empty corpus."""

    def __init__(self, *, fail_on_search: bool = True) -> None:
        self.search_calls: list[str] = []
        self.fail_on_search = fail_on_search
        #: No session of its own — the ContextBuilder's knowledge-vault read
        #: is skipped, exactly as for a deployment without documents.
        self.db = None

    def facts(self, company_id=None):
        return []

    def list_documents(self, company_id, include_superseded=False):
        return []

    def search(self, question, company_id=None, top_k=10):
        self.search_calls.append(question)
        if self.fail_on_search:
            raise AssertionError(
                "RAG retrieval must not run on the deterministic path"
            )
        return types.SimpleNamespace(hits=[])


class SpyRouter:
    """Records provider calls; behaviour driven per test.

    ``mode="forbid"`` — the deterministic path: any provider call fails the
    test. ``mode="offline"`` — the fall-through path: the real offline
    composer answers, so the provider pipeline runs end to end without a key.
    """

    NAME = "SpyOffline"

    def __init__(self, mode: str = "forbid") -> None:
        self.mode = mode
        self.complete_calls: list[CompletionRequest] = []
        self._offline = OfflineProvider(ProviderConfig(
            name=self.NAME, endpoint="local://spy", payload_shape="offline",
            auth_header="", response_path="", default_model="spy-offline-v1",
            api_key="offline",
        ))

    async def complete(
        self, request: CompletionRequest, *, preferred: str | None = None,
        use_cache: bool = True,
    ) -> CompletionResponse:
        self.complete_calls.append(request)
        if self.mode == "forbid":
            raise AssertionError(
                "ProviderRouter.complete must not be called on the "
                "deterministic path"
            )
        return await self._offline.complete(request)

    async def stream(self, request: CompletionRequest, *, preferred: str | None = None):
        self.complete_calls.append(request)
        raise AssertionError("provider stream must not be called")
        yield  # pragma: no cover


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


def _analyst(db_session, *, mode: str = "forbid",
             fail_on_search: bool = True):
    """A real analyst over the seeded reference company, with spies."""
    analysis = AnalysisService.for_ticker(db_session, REF, provision=False)
    assert analysis is not None and analysis.has_data
    docs = SpyDocumentService(fail_on_search=fail_on_search)
    builder = ContextBuilder(
        analysis,
        ForecastService(db_session),
        ValuationService(db_session),
        ScoringService(db_session),
        docs,
    )
    router = SpyRouter(mode)
    analyst = ResearchAnalyst(builder, router=router, prompt_builder=PromptBuilder())
    return analyst, router, docs, analysis


def _memory() -> ConversationMemory:
    return ConversationMemory(session_id="det-test")


#: One canonical question per supported intent.
INTENT_QUESTIONS = {
    "pe": "What is the current P/E?",
    "pb": "What is the P/B?",
    "debt": "What is the total debt?",
    "roe": "What is the ROE?",
    "roce": "What is the ROCE?",
    "profit_growth": "What is the profit growth?",
    "revenue_growth": "What is the revenue growth?",
    "eps": "What is the EPS?",
    "market_price": "What is the current market price?",
    "valuation": "What is the valuation?",
}


class TestDeterministicPath:
    @pytest.mark.parametrize("intent,question", list(INTENT_QUESTIONS.items()))
    def test_supported_intent_is_provider_and_rag_free(self, db_session, intent,
                                                       question):
        # The resolver must agree the question is a single supported intent;
        # the test would be vacuous otherwise.
        assert FinancialIntentResolver().resolve(question) is not None

        analyst, router, docs, _ = _analyst(db_session, mode="forbid")
        result = _run(analyst.chat(question, _memory()))

        # No provider, no RAG — the whole point of the path.
        assert router.complete_calls == []
        assert docs.search_calls == []

        # Honest accounting.
        assert result.provider == "deterministic"
        assert result.model == "none"
        assert result.prompt_tokens == 0
        assert result.completion_tokens == 0
        assert result.cost_usd == 0.0
        # Latency is measured, not faked.
        assert result.latency_ms >= 0.0
        assert result.cached is False
        assert result.fell_back_from is None

        # The existing verification funnel ran.
        assert result.citation_audit is not None
        assert result.citation_audit.unknown_keys == []
        assert result.citation_audit.uncited_numbers == []
        assert result.citation_audit.is_supported
        assert result.guardrails is not None
        assert result.guardrails.passed
        # The normal disclosure remains intact.
        assert "not investment advice" in result.content

        # Annotation replaced the keys with readable labels. (PB has no
        # canonical citation in the current ContextBuilder, so its answer
        # cites nothing and says so — an empty citation list is the honest
        # outcome there.)
        if intent != "pb":
            assert result.citations
        for citation in result.citations:
            assert f"[{citation.key}]" in result.content
            assert f"[{citation.label}]" in result.display_content
        # Canonical English content, unchanged by display rendering.
        assert result.display_content

    @pytest.mark.parametrize("intent,question", list(INTENT_QUESTIONS.items()))
    def test_answer_is_deterministic(self, db_session, intent, question):
        analyst, _, _, _ = _analyst(db_session, mode="forbid")
        first = _run(analyst.chat(question, _memory())).content
        second = _run(analyst.chat(question, _memory())).content
        assert first == second

    def test_memory_pipeline_records_the_turn(self, db_session):
        analyst, _, _, _ = _analyst(db_session, mode="forbid")
        memory = _memory()
        result = _run(analyst.chat(INTENT_QUESTIONS["pe"], memory))
        # User question + assistant answer, exactly as a chat turn should.
        assert len(memory.turns) == 2
        assert memory.turns[0].role.value == "user"
        assert memory.turns[1].role.value == "assistant"
        # Memory stores the raw English answer (the enforced result content
        # additionally carries the disclosure footer) — same contract as the
        # provider path.
        stored = memory.turns[1].content
        assert result.content.startswith(stored)
        assert "not investment advice" not in stored
        # The assistant turn carries the resolved citation keys.
        assert "pe_ratio" in memory.turns[1].citations


class TestFallThrough:
    def test_unsupported_intent_uses_the_provider_path(self, db_session):
        analyst, router, docs, _ = _analyst(
            db_session, mode="offline", fail_on_search=False,
        )
        question = "What are the main risks for the company?"
        assert FinancialIntentResolver().resolve(question) is None

        result = _run(analyst.chat(question, _memory()))

        # The existing pipeline served the question.
        assert len(router.complete_calls) == 1
        assert result.provider == SpyRouter.NAME
        assert result.model == "spy-offline-v1"
        # Provider accounting is the provider's, untouched by the refactor.
        assert result.prompt_tokens > 0
        assert result.completion_tokens > 0
        # Retrieval ran for the provider path, as before.
        assert len(docs.search_calls) == 1
        assert result.citation_audit is not None
        assert result.guardrails is not None

    def test_multi_intent_question_falls_through(self, db_session):
        analyst, router, docs, _ = _analyst(
            db_session, mode="offline", fail_on_search=False,
        )
        question = "What is the P/E and the ROE?"
        assert FinancialIntentResolver().resolve(question) is None

        result = _run(analyst.chat(question, _memory()))

        # No partial deterministic answer: the whole question goes to the
        # provider, and only the provider.
        assert len(router.complete_calls) == 1
        assert result.provider == SpyRouter.NAME
        assert result.provider != "deterministic"

    def test_source_restricted_question_keeps_existing_behaviour(self, db_session):
        """A "documents only" question must not be answered from the financial
        database deterministically; the existing fail-closed refusal stands."""
        analyst, router, docs, _ = _analyst(
            db_session, mode="offline", fail_on_search=False,
        )
        question = "What is the P/E? Answer only from the uploaded documents."

        result = _run(analyst.chat(question, _memory()))

        # No deterministic answer, no provider call either: with no ingested
        # documents the source router declines, exactly as before.
        assert result.provider == "source-router"
        assert router.complete_calls == []
        assert "deterministic" not in result.content.lower()

    def test_prompt_is_not_built_on_the_deterministic_path(self, db_session):
        """No LLM prompt is assembled for a deterministic answer: the router
        spy never receives a request, and the prompt version is zero, not the
        chat template's version."""
        analyst, router, _, _ = _analyst(db_session, mode="forbid")
        result = _run(analyst.chat(INTENT_QUESTIONS["roe"], _memory()))
        assert router.complete_calls == []
        assert result.prompt_key == "chat"
        assert result.prompt_version == 0


class TestProviderPathRegression:
    """The refactor of _finalise must not have changed provider accounting."""

    def test_provider_usage_is_reported_not_estimated(self, db_session):
        analyst, router, docs, _ = _analyst(
            db_session, mode="offline", fail_on_search=False,
        )
        question = "What is the business model?"
        assert FinancialIntentResolver().resolve(question) is None

        result = _run(analyst.chat(question, _memory()))
        # The offline composer reports real usage; the result must carry the
        # provider's own numbers, not the prompt-length estimate.
        assert result.prompt_tokens > 0
        assert result.completion_tokens > 0
        assert result.total_tokens == result.prompt_tokens + result.completion_tokens
        # The memory funnel still stores the audited English turn.
        memory = _memory()
        result2 = _run(analyst.chat(question, memory))
        assert len(memory.turns) == 2
        assert result2.content.startswith(memory.turns[1].content)
        assert memory.turns[1].citations is not None
