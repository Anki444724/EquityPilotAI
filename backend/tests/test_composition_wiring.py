"""Part 2C — the analyst wiring for internal composition.

`test_internal_composer.py` proves the composer joins existing engine output
correctly. `test_question_planner_architecture.py` proves the planner plans and
computes nothing. This module proves the two are wired into the answer path
*only* where they are entitled to be, and that everything the platform already
guarantees about an answer still holds for a composed one.

Six properties, each of which can fail silently in production if untested:

* **opt-in isolation** — composition is off unless a caller asks for it, and
  exactly one caller (authenticated, non-streaming chat) asks. The blogger
  publisher, the report builders, the analyse endpoint, streaming and the
  batch runner must keep the analyst they had before Part 2C existed.
* **eligibility** — only a multi-intent plan reaches the composer. A single
  intent is still answered by the engine that has always answered it, and
  every other route (source router, decline, internal reasoning, comparison)
  is left entirely alone.
* **company identity safety** — the composed answer is only ever rendered
  from the context bound to this analysis. A plan naming another company, or
  several, is refused; a resolver that fails cannot retarget an answer.
* **the existing funnel** — a composed answer passes through the same
  `_deterministic` → `_verify_and_record` pipeline as a single-intent one:
  citation audit, guardrails, annotation, memory, language rendering.
* **no provider, no retrieval** — a composed answer costs zero tokens and
  zero retrieval calls, asserted with spies that raise if either happens.
* **fail-closed fallback** — a composer that returns nothing, raises, or
  fails to plan at all leaves the whole question to the existing provider
  path. No partial deterministic answer escapes.

The spies raise rather than record where a call must not happen, so a drift
is a failure rather than a different number in a list.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.domain.language.types import Language
from app.main import app
from app.services.ai.analyst import (
    ResearchAnalyst, _company_identity_is_safe, _normalise_identity,
)
from app.services.ai.context_builder import ContextBuilder
from app.services.ai.financial_intent import FinancialIntent
from app.services.ai.internal_composer import ComposedAnswer, InternalComposer
from app.services.ai.memory import ConversationMemory
from app.services.ai.planner import (
    EntityResolution, EntityStatus, ExecutionRoute, QuestionPlanner,
)
from app.services.ai.prompt_builder import PromptBuilder
from app.services.ai.service import AIService
from app.services.analysis_service import AnalysisService
from app.services.company_service import CompanyService
from app.services.forecast.service import ForecastService
from app.services.scoring.service import ScoringService
from app.services.valuation.service import ValuationService
from tests.test_deterministic_analyst_path import (
    SpyDocumentService, SpyRouter,
)

REF = "BHARATCP"
client = TestClient(app)


def _run(coro):
    return asyncio.run(coro)


# ===========================================================================
# Collaborators
# ===========================================================================
class SpyComposer:
    """Records composition calls; the failure modes are switchable.

    ``inner`` is the real composer unless a test replaces the behaviour, so
    the happy path exercises the production implementation rather than a
    stand-in for it.
    """

    def __init__(self, *, returns_none: bool = False, raises: bool = False,
                 inner: InternalComposer | None = None,
                 partial_text: str = "(partial composition text)") -> None:
        self.returns_none = returns_none
        self.raises = raises
        self.inner = inner or InternalComposer()
        self.partial_text = partial_text
        self.calls: list[tuple] = []
        self.results: list[ComposedAnswer | None] = []

    def compose_answer(self, plan, context):
        self.calls.append((plan, context))
        if self.raises:
            raise RuntimeError("composer exploded")
        if self.returns_none:
            # A composer that built some text and then declined. The text must
            # not appear anywhere in the response.
            self.results.append(None)
            return None
        result = self.inner.compose_answer(plan, context)
        self.results.append(result)
        return result


class StubPlanner:
    """Returns a fixed plan, or raises — for routes the planner declines."""

    def __init__(self, plan=None, error: Exception | None = None) -> None:
        self._plan = plan
        self.error = error
        self.calls: list[str] = []

    def plan(self, question, *, language=None, source=None):
        self.calls.append(question)
        if self.error is not None:
            raise self.error
        return self._plan


class Company:
    """The three attributes the planner reads, and nothing else."""

    def __init__(self, id, ticker, name):
        self.id, self.ticker, self.name = id, ticker, name


# ===========================================================================
# Fixtures
# ===========================================================================
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
def analysis(db_session):
    service = AnalysisService.for_ticker(db_session, REF, provision=False)
    assert service is not None and service.has_data
    return service


def _real_planner(db_session, resolver=None) -> QuestionPlanner:
    """The planner as production builds it: the platform's company resolver."""
    return QuestionPlanner(
        company_resolver=resolver or CompanyService(db_session).named_in,
    )


class StreamingSpyRouter(SpyRouter):
    """`SpyRouter`, but the streaming endpoint is allowed to stream."""

    async def stream(self, request, *, preferred=None):
        self.complete_calls.append(request)
        yield "streamed "
        yield "answer"


def _analyst(db_session, analysis, *, mode="forbid", fail_on_search=True,
             planner=None, composer=None, router=None):
    """A real analyst over the seeded company, with provider/RAG spies.

    `planner` and `composer` are injected exactly as given. Passing neither
    reproduces every pre-Part-2C caller in the codebase.
    """
    docs = SpyDocumentService(fail_on_search=fail_on_search)
    builder = ContextBuilder(
        analysis,
        ForecastService(db_session),
        ValuationService(db_session),
        ScoringService(db_session),
        docs,
    )
    router = router or SpyRouter(mode)
    analyst = ResearchAnalyst(
        builder, router=router, prompt_builder=PromptBuilder(),
        planner=planner, composer=composer,
    )
    return analyst, router, docs


def _composed_analyst(db_session, analysis, *, mode="forbid",
                      fail_on_search=True, planner=None, composer=None):
    """An analyst wired for composition, with the real collaborators by
    default — the shape `analyst_for(..., enable_composition=True)` builds."""
    return _analyst(
        db_session, analysis, mode=mode, fail_on_search=fail_on_search,
        planner=planner if planner is not None else _real_planner(db_session),
        composer=composer if composer is not None else SpyComposer(),
    )


def _memory() -> ConversationMemory:
    memory = ConversationMemory(session_id="compose-test")
    return memory


def _prime(memory: ConversationMemory, analysis) -> ConversationMemory:
    memory.set_company(analysis.company.id, analysis.company.ticker,
                       analysis.company.name)
    return memory


#: Multi-intent questions the planner routes to composition. Each mixes
#: intents from the two deterministic engine families, which is exactly what
#: the resolver refuses to answer partially.
MULTI_INTENT = (
    "What is the P/E and the ROE?",
    "What is the P/E and the financial quality?",
    "What are the strengths and weaknesses?",
    "What is the P/E and the valuation?",
)

#: Single-intent questions: one supported intent, answered as before.
SINGLE_INTENT = (
    "What is the P/E?",
    "What is the ROE?",
    "What is the debt?",
    "What is the financial quality?",
)


# ===========================================================================
class TestOptInIsolation:
    """Composition is opt-in, and exactly one caller opts in."""

    def test_analyst_for_defaults_to_no_composition(self, db_session, analysis):
        analyst = AIService(db_session).analyst_for(analysis)
        assert analyst.planner is None
        assert analyst.composer is None

    def test_analyst_for_enables_composition_only_when_asked(
        self, db_session, analysis,
    ):
        analyst = AIService(db_session).analyst_for(
            analysis, enable_composition=True,
        )
        assert isinstance(analyst.planner, QuestionPlanner)
        assert isinstance(analyst.composer, InternalComposer)

    def test_the_planner_is_given_the_platform_resolver(
        self, db_session, analysis,
    ):
        """One company-resolution architecture: `CompanyService.named_in`.

        Proven behaviourally — the analyst's own plan resolves the seeded
        company by its stored name, which only the real resolver can do.
        """
        analyst = AIService(db_session).analyst_for(
            analysis, enable_composition=True,
        )
        plan = analyst.planner.plan(
            f"What is the P/E and the ROE of {analysis.company.name}?",
        )
        assert plan.entity.status is EntityStatus.RESOLVED
        assert plan.entity.company_id == analysis.company.id
        assert plan.entity.ticker == analysis.company.ticker

    def test_a_default_analyst_serves_a_multi_intent_question_as_before(
        self, db_session, analysis,
    ):
        """No planner, no composer: the multi-intent path does not exist.

        This is the contract every pre-Part-2C caller depends on — the blogger
        publisher, the report builders, the batch runner.
        """
        analyst, router, docs = _analyst(
            db_session, analysis, mode="offline", fail_on_search=False,
            planner=None, composer=None,
        )
        assert analyst.planner is None and analyst.composer is None

        result = _run(analyst.chat(MULTI_INTENT[0], _prime(_memory(), analysis)))

        assert len(router.complete_calls) == 1
        assert result.provider == SpyRouter.NAME
        assert result.provider != "deterministic"
        # The existing provider path still retrieves, exactly as before.
        assert len(docs.search_calls) == 1

    def test_a_composer_without_a_planner_cannot_compose(
        self, db_session, analysis,
    ):
        """Half the wiring is not a license to compose."""
        composer = SpyComposer()
        analyst, router, _ = _analyst(
            db_session, analysis, mode="offline", fail_on_search=False,
            planner=None, composer=composer,
        )
        result = _run(analyst.chat(MULTI_INTENT[0], _prime(_memory(), analysis)))

        assert composer.calls == []
        assert result.provider == SpyRouter.NAME


class TestApiOptInIsIsolated:
    """The HTTP surface: chat opts in, every other endpoint does not."""

    @staticmethod
    def _record(monkeypatch) -> list[bool]:
        """Record what each caller passes, then run the real thing."""
        calls: list[bool] = []
        original = AIService.analyst_for

        def spy(self, analysis, *, enable_composition=False):
            calls.append(enable_composition)
            return original(self, analysis,
                            enable_composition=enable_composition)

        monkeypatch.setattr(AIService, "analyst_for", spy)
        return calls

    def test_authenticated_chat_opts_in(self, monkeypatch):
        calls = self._record(monkeypatch)
        r = client.post(
            f"/api/v1/company/{REF}/ai/chat",
            json={"question": "How leveraged is it?", "session_id": "optin"},
        )
        assert r.status_code == 200, r.text[:300]
        assert calls == [True]

    def test_analyse_does_not_opt_in(self, monkeypatch):
        calls = self._record(monkeypatch)
        r = client.post(
            f"/api/v1/company/{REF}/ai/analyse",
            json={"capability": "swot", "save": False},
        )
        assert r.status_code == 200, r.text[:300]
        assert calls == [False]

    def test_report_does_not_opt_in(self, monkeypatch):
        calls = self._record(monkeypatch)
        r = client.post(
            f"/api/v1/company/{REF}/ai/report",
            json={"capabilities": ["swot"]},
        )
        assert r.status_code == 200, r.text[:300]
        assert calls == [False]

    def test_streaming_does_not_opt_in(self, monkeypatch):
        calls = self._record(monkeypatch)
        with client.stream(
            "POST", f"/api/v1/company/{REF}/ai/chat/stream",
            json={"question": "Summarise", "session_id": "optin-stream"},
        ) as r:
            assert r.status_code == 200
            "".join(r.iter_text())
        assert calls == [False]

    def test_research_report_does_not_opt_in(self, monkeypatch):
        from app.services.ai import report_orchestrator

        class _StubReport:
            def as_dict(self):
                return {"sections": []}

        async def _run_orchestrator(self, sections=None):
            return _StubReport()

        monkeypatch.setattr(
            report_orchestrator.ReportOrchestrator, "run", _run_orchestrator,
        )
        calls = self._record(monkeypatch)
        r = client.post(f"/api/v1/company/{REF}/ai/research-report")
        assert r.status_code == 200, r.text[:300]
        assert calls == [False]

    def test_context_endpoint_does_not_opt_in(self, monkeypatch):
        calls = self._record(monkeypatch)
        r = client.get(f"/api/v1/company/{REF}/ai/context")
        assert r.status_code == 200, r.text[:300]
        assert calls == [False]

    def test_multi_intent_chat_is_answered_deterministically_over_http(self):
        """The opt-in is real end to end: no provider, no tokens, audited."""
        r = client.post(
            f"/api/v1/company/{REF}/ai/chat",
            json={"question": MULTI_INTENT[0], "session_id": "compose-http"},
        )
        assert r.status_code == 200, r.text[:300]
        body = r.json()

        assert body["provider"] == "deterministic"
        assert body["model"] == "none"
        assert body["prompt_tokens"] == 0
        assert body["completion_tokens"] == 0
        assert body["cost_usd"] == 0.0
        # Both intents are answered, in the order they were asked.
        assert "[pe_ratio]" in body["content"]
        assert "[roe_avg]" in body["content"]
        assert body["content"].index("[pe_ratio]") < body["content"].index("[roe_avg]")
        # The existing verification pipeline still ran.
        assert body["citation_audit"]["is_supported"] is True
        assert body["citation_audit"]["unknown_keys"] == []
        assert body["guardrails"]["passed"] is True

    def test_a_retargeted_chat_composes_about_the_company_the_question_named(self):
        """The existing retarget rule and the identity gate agree.

        A chat scoped to one company whose question names another is answered
        from the named company's record — that is the endpoint's existing
        behaviour, and composition must follow it rather than refuse, or the
        gate would turn a working feature off.
        """
        body = client.post(
            "/api/v1/company/RELIANCE/ai/chat",
            json={
                "question": "What is the P/E and the ROE of "
                            "Tata Consultancy Services Ltd?",
                "session_id": "compose-retarget",
            },
        ).json()

        assert body["provider"] == "deterministic"
        assert body["prompt_tokens"] == 0
        # The answer is about the company the question named, not the URL's.
        assert "Tata Consultancy Services Ltd" in body["content"]
        assert "[roe_avg]" in body["content"]
        assert any("TCS" in w for w in body["warnings"])

    def test_english_chat_payload_is_unchanged_for_a_single_intent(self):
        """A canonical question is answered by the same engine as before."""
        body = client.post(
            f"/api/v1/company/{REF}/ai/chat",
            json={"question": "What is the P/E?", "session_id": "single-http"},
        ).json()
        assert body["provider"] == "deterministic"
        assert body["prompt_tokens"] == 0
        assert "[roe_avg]" not in body["content"]


# ===========================================================================
class TestDependencyInjection:
    """The planner and composer are collaborators of the analyst."""

    def test_collaborators_are_constructor_injected(self):
        import inspect

        params = inspect.signature(ResearchAnalyst.__init__).parameters
        assert "planner" in params and "composer" in params
        assert params["planner"].kind is inspect.Parameter.KEYWORD_ONLY
        assert params["composer"].kind is inspect.Parameter.KEYWORD_ONLY
        # Both default to absent: composition off unless asked for.
        assert params["planner"].default is None
        assert params["composer"].default is None

    def test_one_planner_serves_the_whole_analyst(self, db_session, analysis):
        """Built once, not once per question."""
        composer = SpyComposer()
        analyst, _, _ = _composed_analyst(
            db_session, analysis, mode="offline", fail_on_search=False,
            composer=composer,
        )
        planner = analyst.planner

        for question in MULTI_INTENT[:2]:
            _run(analyst.chat(question, _prime(_memory(), analysis)))
        assert analyst.planner is planner
        assert len(composer.calls) == 2


# ===========================================================================
class TestEligibility:
    """Which questions may be composed, and which may not."""

    @pytest.mark.parametrize("question", MULTI_INTENT)
    def test_multi_intent_question_reaches_the_composer(
        self, db_session, analysis, question,
    ):
        composer = SpyComposer()
        analyst, router, docs = _composed_analyst(
            db_session, analysis, composer=composer,
        )
        result = _run(analyst.chat(question, _prime(_memory(), analysis)))

        assert len(composer.calls) == 1
        plan, context = composer.calls[0]
        assert plan.execution_route is ExecutionRoute.COMPOSITION_REQUIRED
        assert len(plan.intents) > 1
        assert context is not None

        # Provider-free and retrieval-free: the spies would have raised.
        assert router.complete_calls == []
        assert docs.search_calls == []
        assert result.provider == "deterministic"
        assert result.prompt_tokens == 0
        assert result.completion_tokens == 0

    @pytest.mark.parametrize("question", SINGLE_INTENT)
    def test_single_intent_never_reaches_the_composer(
        self, db_session, analysis, question,
    ):
        composer = SpyComposer()
        analyst, router, docs = _composed_analyst(
            db_session, analysis, composer=composer,
        )
        result = _run(analyst.chat(question, _prime(_memory(), analysis)))

        assert composer.calls == []
        assert analyst.planner is not None  # wired, simply not needed
        assert router.complete_calls == []
        assert docs.search_calls == []
        assert result.provider == "deterministic"

    @pytest.mark.parametrize(
        "question",
        [
            "Compare Reliance with TCS.",          # INTERNAL_REASONING
            "Tell me about the company",           # OPEN_ENDED
            "Good company hai?",                   # DECLINE (vague)
            "What is the revenue?",                # DECLINE (unsupported)
            "What is the P/E? Answer only from the uploaded documents.",
        ],
    )
    def test_non_composable_routes_never_reach_the_composer(
        self, db_session, analysis, question,
    ):
        composer = SpyComposer()
        analyst, router, _ = _composed_analyst(
            db_session, analysis, mode="offline", fail_on_search=False,
            composer=composer,
        )
        result = _run(analyst.chat(question, _prime(_memory(), analysis)))

        assert composer.calls == []
        assert result.provider != "deterministic"
        # Every non-composable route still reaches its existing owner.
        assert len(router.complete_calls) <= 1

    def test_a_single_intent_question_is_not_rerouted_through_the_planner(
        self, db_session, analysis,
    ):
        """The resolver answers first; the planner is not even consulted.

        Order is the regression protection: composition can only ever see a
        question the resolver declined, so it cannot intercept one that has a
        single deterministic answer.
        """
        plan = _real_planner(db_session).plan(SINGLE_INTENT[0])
        stub = StubPlanner(plan=replace(
            plan,
            execution_route=ExecutionRoute.COMPOSITION_REQUIRED,
            intents=plan.intents + plan.intents,
        ))
        composer = SpyComposer()
        analyst, router, _ = _composed_analyst(
            db_session, analysis, mode="forbid", planner=stub, composer=composer,
        )
        result = _run(analyst.chat(SINGLE_INTENT[0], _prime(_memory(), analysis)))

        assert stub.calls == []
        assert composer.calls == []
        assert router.complete_calls == []
        assert result.provider == "deterministic"

    def test_source_directed_question_bypasses_composition(
        self, db_session, analysis,
    ):
        composer = SpyComposer()
        analyst, router, _ = _composed_analyst(
            db_session, analysis, mode="offline", fail_on_search=False,
            composer=composer,
        )
        result = _run(analyst.chat(
            f"{MULTI_INTENT[0]} Answer only from the uploaded documents.",
            _prime(_memory(), analysis),
        ))

        assert composer.calls == []
        # The existing fail-closed source router still owns the question.
        assert result.provider == "source-router"
        assert router.complete_calls == []

    def test_directive_parsing_precedes_the_composition_gate(
        self, db_session, analysis,
    ):
        """Order: directive → classification → gate → source routing.

        The planner sees the restriction as well, so a restricted question is
        classified SOURCE_ROUTER rather than composed — independently of the
        analyst declining it earlier.
        """
        question = f"{MULTI_INTENT[0]} Answer only from the uploaded documents."
        plan = _real_planner(db_session).plan(question)

        assert plan.source_directive is not None
        assert plan.source_directive.scope.is_restricted
        assert plan.execution_route is ExecutionRoute.SOURCE_ROUTER
        assert plan.query_type.value == "source_directed"

    def test_context_override_bypasses_composition(self, db_session, analysis):
        """A caller-supplied context is not composed from.

        `context_override` exists so a caller can restrict the evidence (the
        report orchestrator does), and internal composition must not read a
        context the caller narrowed — nor silently substitute the analyst's
        own broader one.
        """
        composer = SpyComposer()
        analyst, router, _ = _composed_analyst(
            db_session, analysis, mode="offline", fail_on_search=False,
            composer=composer,
        )
        override = analyst.context()
        result = _run(analyst.run(
            "chat", question=MULTI_INTENT[0], memory=_prime(_memory(), analysis),
            context_override=override,
        ))

        assert composer.calls == []
        assert result.provider == SpyRouter.NAME
        assert len(router.complete_calls) == 1

    def test_an_empty_question_never_reaches_the_composer(
        self, db_session, analysis,
    ):
        composer = SpyComposer()
        analyst, router, _ = _composed_analyst(
            db_session, analysis, mode="offline", fail_on_search=False,
            composer=composer,
        )
        result = _run(analyst.chat("   ", _prime(_memory(), analysis)))

        assert composer.calls == []
        assert result.provider == SpyRouter.NAME

    def test_non_chat_capabilities_never_reach_the_composer(
        self, db_session, analysis,
    ):
        """Composition is a chat capability, as the deterministic path is."""
        composer = SpyComposer()
        analyst, router, _ = _composed_analyst(
            db_session, analysis, mode="offline", fail_on_search=False,
            composer=composer,
        )
        for capability in ("swot", "risk_analysis", "bull_case"):
            _run(analyst.run(
                capability, question=MULTI_INTENT[0],
                memory=_prime(_memory(), analysis),
            ))
        assert composer.calls == []


# ===========================================================================
class TestVerificationPipeline:
    """A composed answer is verified, remembered and rendered as any other."""

    def test_composed_answer_passes_the_existing_funnel(
        self, db_session, analysis,
    ):
        analyst, _, _ = _composed_analyst(db_session, analysis)
        result = _run(analyst.chat(MULTI_INTENT[0], _prime(_memory(), analysis)))

        # Citation audit.
        assert result.citation_audit is not None
        assert result.citation_audit.unknown_keys == []
        assert result.citation_audit.uncited_numbers == []
        assert result.citation_audit.is_supported is True
        assert result.citations == result.citation_audit.resolved
        # Guardrails, including the disclosure every answer carries.
        assert result.guardrails is not None
        assert result.guardrails.passed is True
        assert "not investment advice" in result.content
        # Annotation: keys became labels in the display copy only.
        for citation in result.citations:
            assert f"[{citation.key}]" in result.content
            assert f"[{citation.label}]" in result.display_content
        # Honest accounting, exactly as for a single-intent answer.
        assert (result.provider, result.model) == ("deterministic", "none")
        assert result.prompt_tokens == result.completion_tokens == 0
        assert result.cost_usd == 0.0
        assert result.cached is False
        assert result.fell_back_from is None
        assert result.latency_ms >= 0.0

    def test_the_composers_own_object_reaches_the_funnel(
        self, db_session, analysis,
    ):
        """No re-composition, no second copy, nothing recalculated.

        The object the composer returned is the object the funnel verifies,
        so its `used_citations` and `missing` are preserved by identity
        rather than reconstructed.
        """
        composer = SpyComposer()
        analyst, _, _ = _composed_analyst(
            db_session, analysis, composer=composer,
        )
        captured: dict = {}
        original = analyst._deterministic

        async def spy(capability, answer, context, elapsed_ms, memory,
                      question, *, language=None):
            captured["answer"] = answer
            return await original(capability, answer, context, elapsed_ms,
                                  memory, question, language=language)

        analyst._deterministic = spy  # type: ignore[method-assign]
        result = _run(analyst.chat(MULTI_INTENT[0], _prime(_memory(), analysis)))

        composed = composer.results[0]
        assert composed is not None
        assert captured["answer"] is composed
        assert composed.used_citations  # the engines cited their own evidence
        assert result.content.startswith(composed.content)
        assert [c.key for c in result.citations] == [
            c.key for c in composed.used_citations
        ]

    def test_memory_records_the_composed_turn(self, db_session, analysis):
        analyst, _, _ = _composed_analyst(db_session, analysis)
        memory = _prime(_memory(), analysis)
        result = _run(analyst.chat(MULTI_INTENT[0], memory))

        assert len(memory.turns) == 2
        assert memory.turns[0].role.value == "user"
        assert memory.turns[1].role.value == "assistant"
        # Memory keeps the canonical English answer, without the footer —
        # the same contract the provider and single-intent paths follow.
        stored = memory.turns[1].content
        assert result.content.startswith(stored)
        assert "not investment advice" not in stored
        assert "pe_ratio" in memory.turns[1].citations

    def test_language_adaptation_still_runs(self, db_session, analysis,
                                            monkeypatch):
        """Composition answers in English; rendering is the adapter's job."""
        from app.services.language.translators import TranslationResult

        class _FakeTranslator:
            name = "fake"

            def supports(self, language):
                return True

            async def translate(self, text, language, *, entities=None):
                marker = ("(हिन्दी) " if language is Language.HINDI
                          else "(Hinglish) ")
                return TranslationResult(
                    text=f"{marker}{text}", language=language,
                    translated=True, provider=self.name,
                )

        monkeypatch.setattr(
            "app.services.language.adapter.build_translator",
            lambda *args, **kwargs: _FakeTranslator(),
        )
        analyst, router, _ = _composed_analyst(db_session, analysis)
        result = _run(analyst.chat(
            MULTI_INTENT[0], _prime(_memory(), analysis),
            language=Language.HINGLISH,
        ))

        # Audited English content, rendered display copy.
        assert result.content.startswith("The trailing price-to-earnings")
        assert result.display_content.startswith("(Hinglish) ")
        assert result.language is not None
        assert result.language["language"] == "hinglish"
        assert result.language["translation"]["translated"] is True
        # No provider request was made to produce or translate it here.
        assert router.complete_calls == []

    @pytest.mark.parametrize(
        "question,language",
        [("P/E aur ROE kya hai?", Language.HINGLISH),
         ("P/E और ROE क्या है?", Language.HINDI)],
    )
    def test_multilingual_multi_intent_questions_compose(
        self, db_session, analysis, question, language,
    ):
        """Hindi and Hinglish reach the same composed English answer."""
        composer = SpyComposer()
        analyst, router, docs = _composed_analyst(
            db_session, analysis, composer=composer,
        )
        result = _run(analyst.chat(
            question, _prime(_memory(), analysis), language=language,
        ))

        assert len(composer.calls) == 1
        assert result.provider == "deterministic"
        assert router.complete_calls == []
        assert docs.search_calls == []
        assert result.citation_audit is not None
        assert result.citation_audit.unknown_keys == []
        # The canonical answer is English in both cases — one knowledge base,
        # rendered per language, never a second answer.
        assert result.content.startswith("The trailing price-to-earnings")


# ===========================================================================
class TestCompanyIdentitySafety:
    """Composition is refused whenever the subject cannot be established."""

    # ---------------------------------------------------------- the predicate
    def test_normalise_identity_is_conservative(self):
        assert _normalise_identity("  Bharat   Consumer Products Ltd.  ") == (
            "bharat consumer products ltd"
        )
        # Two names that differ by a word are NOT the same company.
        assert _normalise_identity("Reliance Industries") != _normalise_identity(
            "Reliance Industries Ltd"
        )
        assert _normalise_identity(None) == ""
        assert _normalise_identity("") == ""

    @staticmethod
    def _plan_for(db_session, question, entity=None, status=None):
        plan = _real_planner(db_session).plan(question)
        if entity is not None:
            plan = replace(plan, entity=entity)
        elif status is not None:
            plan = replace(plan, entity=replace(plan.entity, status=status))
        return plan

    def test_company_id_match_is_safe(self, db_session, analysis):
        context = _context_for(db_session, analysis)
        plan = self._plan_for(db_session, MULTI_INTENT[0], entity=EntityResolution(
            status=EntityStatus.RESOLVED, company_id=analysis.company.id,
            ticker="WRONG", name="Wrong Company Ltd", basis="test",
        ))
        safe, reason = _company_identity_is_safe(plan, context)
        assert safe is True
        assert "company id" in reason

    def test_ticker_match_is_safe(self, db_session, analysis):
        context = _context_for(db_session, analysis)
        plan = self._plan_for(db_session, MULTI_INTENT[0], entity=EntityResolution(
            status=EntityStatus.RESOLVED, company_id="other-id",
            ticker=analysis.company.ticker, name="Wrong Company Ltd",
            basis="test",
        ))
        safe, reason = _company_identity_is_safe(plan, context)
        assert safe is True
        assert "ticker" in reason

    def test_normalised_name_match_is_safe(self, db_session, analysis):
        context = _context_for(db_session, analysis)
        plan = self._plan_for(db_session, MULTI_INTENT[0], entity=EntityResolution(
            status=EntityStatus.RESOLVED, company_id="other-id",
            ticker="OTHER", name=f"  {analysis.company.name.upper()}. ",
            basis="test",
        ))
        safe, reason = _company_identity_is_safe(plan, context)
        assert safe is True
        assert "company name" in reason

    def test_bare_name_prefix_is_not_a_match(self, db_session, analysis):
        context = _context_for(db_session, analysis)
        plan = self._plan_for(db_session, MULTI_INTENT[0], entity=EntityResolution(
            status=EntityStatus.RESOLVED, company_id="other-id", ticker="OTHER",
            name=analysis.company.name.split()[0], basis="test",
        ))
        safe, reason = _company_identity_is_safe(plan, context)
        assert safe is False
        assert "does not match" in reason

    def test_ambiguous_entity_is_never_safe(self, db_session, analysis):
        context = _context_for(db_session, analysis)
        plan = self._plan_for(db_session, MULTI_INTENT[0], entity=EntityResolution(
            status=EntityStatus.AMBIGUOUS, candidates=("A", "B"),
            basis="two companies named",
        ))
        safe, reason = _company_identity_is_safe(plan, context)
        assert safe is False
        assert "more than one company" in reason

    def test_unresolved_entity_uses_the_bound_company(self, db_session, analysis):
        context = _context_for(db_session, analysis)
        plan = self._plan_for(db_session, MULTI_INTENT[0], entity=EntityResolution(
            status=EntityStatus.UNRESOLVED, basis="nothing named",
        ))
        safe, reason = _company_identity_is_safe(plan, context)
        assert safe is True
        assert "authoritative" in reason

    def test_context_only_entity_must_match(self, db_session, analysis):
        context = _context_for(db_session, analysis)
        matching = self._plan_for(
            db_session, MULTI_INTENT[0],
            entity=EntityResolution(
                status=EntityStatus.CONTEXT_ONLY, ticker=analysis.company.ticker,
                basis="pinned",
            ),
        )
        assert _company_identity_is_safe(matching, context)[0] is True

        other = self._plan_for(
            db_session, MULTI_INTENT[0],
            entity=EntityResolution(
                status=EntityStatus.CONTEXT_ONLY, ticker="SOMETHINGELSE",
                basis="pinned",
            ),
        )
        assert _company_identity_is_safe(other, context)[0] is False

    def test_a_context_without_identity_is_never_safe(self, db_session, analysis):
        context = _context_for(db_session, analysis)
        anonymous = replace(context, company_id="", ticker="")
        plan = self._plan_for(db_session, MULTI_INTENT[0])
        safe, reason = _company_identity_is_safe(plan, anonymous)
        assert safe is False
        assert "does not identify a company" in reason

    # ------------------------------------------------------- the analyst path
    def test_matching_company_id_composes(self, db_session, analysis):
        resolver = lambda _q: [Company(  # noqa: E731 - a resolver, not a def
            analysis.company.id, "WRONG", "Wrong Company Ltd",
        )]
        composer = SpyComposer()
        analyst, router, _ = _composed_analyst(
            db_session, analysis, planner=_real_planner(db_session, resolver),
            composer=composer,
        )
        result = _run(analyst.chat(MULTI_INTENT[0], _prime(_memory(), analysis)))

        assert len(composer.calls) == 1
        assert result.provider == "deterministic"
        assert router.complete_calls == []

    def test_matching_ticker_composes(self, db_session, analysis):
        resolver = lambda _q: [Company(  # noqa: E731
            "other-id", analysis.company.ticker, "Wrong Company Ltd",
        )]
        composer = SpyComposer()
        analyst, router, _ = _composed_analyst(
            db_session, analysis, planner=_real_planner(db_session, resolver),
            composer=composer,
        )
        result = _run(analyst.chat(MULTI_INTENT[0], _prime(_memory(), analysis)))

        assert len(composer.calls) == 1
        assert result.provider == "deterministic"
        assert router.complete_calls == []

    def test_matching_normalised_name_composes(self, db_session, analysis):
        resolver = lambda _q: [Company(  # noqa: E731
            "other-id", "OTHER", f"{analysis.company.name.upper()}.",
        )]
        composer = SpyComposer()
        analyst, _, _ = _composed_analyst(
            db_session, analysis, planner=_real_planner(db_session, resolver),
            composer=composer,
        )
        result = _run(analyst.chat(MULTI_INTENT[0], _prime(_memory(), analysis)))

        assert len(composer.calls) == 1
        assert result.provider == "deterministic"

    def test_explicit_mismatch_is_refused(self, db_session, analysis):
        """A plan about another company never composes from this context."""
        resolver = lambda _q: [Company(  # noqa: E731
            "other-id", "OTHERCO", "Other Company Ltd",
        )]
        composer = SpyComposer()
        analyst, router, _ = _composed_analyst(
            db_session, analysis, mode="offline", fail_on_search=False,
            planner=_real_planner(db_session, resolver), composer=composer,
        )
        result = _run(analyst.chat(MULTI_INTENT[0], _prime(_memory(), analysis)))

        assert composer.calls == []
        assert result.provider == SpyRouter.NAME
        assert len(router.complete_calls) == 1
        # Nothing was composed, so no composed text can be present: the
        # deterministic sentences the engines would have produced are absent.
        assert "[pe_ratio]" not in result.content
        assert "[roe_avg]" not in result.content

    def test_ambiguous_entity_is_refused_by_the_analyst(
        self, db_session, analysis,
    ):
        """Two plans named → the planner declines to choose → no composition."""
        plan = _real_planner(db_session).plan(MULTI_INTENT[0])
        ambiguous = replace(plan, entity=EntityResolution(
            status=EntityStatus.AMBIGUOUS, candidates=("A", "B"),
            basis="two companies named",
        ))
        composer = SpyComposer()
        analyst, router, _ = _composed_analyst(
            db_session, analysis, mode="offline", fail_on_search=False,
            planner=StubPlanner(plan=ambiguous), composer=composer,
        )
        result = _run(analyst.chat(MULTI_INTENT[0], _prime(_memory(), analysis)))

        assert composer.calls == []
        assert result.provider == SpyRouter.NAME

    def test_two_companies_named_never_composes(self, db_session, analysis):
        """The real planner's own ambiguity, through the real resolver."""
        composer = SpyComposer()
        analyst, router, _ = _composed_analyst(
            db_session, analysis, mode="offline", fail_on_search=False,
            composer=composer,
        )
        result = _run(analyst.chat(
            "What is the P/E and the ROE of Reliance and TCS?",
            _prime(_memory(), analysis),
        ))

        assert composer.calls == []
        assert result.provider != "deterministic"

    def test_unresolved_entity_composes_from_the_bound_company(
        self, db_session, analysis,
    ):
        composer = SpyComposer()
        analyst, router, _ = _composed_analyst(
            db_session, analysis,
            planner=_real_planner(db_session, lambda _q: []), composer=composer,
        )
        result = _run(analyst.chat(MULTI_INTENT[0], _prime(_memory(), analysis)))

        assert len(composer.calls) == 1
        _, used_context = composer.calls[0]
        # The context composed from is the analyst's own, and the answer names
        # the company bound to this analysis.
        assert used_context.ticker == analysis.company.ticker
        assert analysis.company.name in result.content
        assert result.provider == "deterministic"
        assert router.complete_calls == []

    def test_resolver_failure_cannot_substitute_another_company(
        self, db_session, analysis,
    ):
        """A resolver that raises reports nobody, so nobody is substituted."""
        def _boom(_question):
            raise RuntimeError("resolver down")

        composer = SpyComposer()
        planner = _real_planner(db_session, _boom)
        analyst, router, _ = _composed_analyst(
            db_session, analysis, planner=planner, composer=composer,
        )

        plan = planner.plan(MULTI_INTENT[0])
        assert plan.entity.status is EntityStatus.UNRESOLVED
        assert plan.entity.company_id is None and plan.entity.ticker is None

        result = _run(analyst.chat(MULTI_INTENT[0], _prime(_memory(), analysis)))

        assert len(composer.calls) == 1
        _, used_context = composer.calls[0]
        assert used_context is analyst.context()
        assert analysis.company.name in result.content
        assert router.complete_calls == []


def _context_for(db_session, analysis):
    return _analyst(db_session, analysis)[0].context()


# ===========================================================================
class TestFailureHandling:
    """Nothing partial escapes, and the fallback is the existing path."""

    def test_composer_returning_none_falls_back(self, db_session, analysis):
        composer = SpyComposer(returns_none=True)
        analyst, router, docs = _composed_analyst(
            db_session, analysis, mode="offline", fail_on_search=False,
            composer=composer,
        )
        memory = _prime(_memory(), analysis)
        result = _run(analyst.chat(MULTI_INTENT[0], memory))

        assert len(composer.calls) == 1  # it was consulted, and declined
        assert result.provider == SpyRouter.NAME
        assert result.provider != "deterministic"
        assert composer.partial_text not in result.content
        assert result.citation_audit is not None

    def test_composer_raising_falls_back(self, db_session, analysis):
        composer = SpyComposer(raises=True)
        analyst, router, _ = _composed_analyst(
            db_session, analysis, mode="offline", fail_on_search=False,
            composer=composer,
        )
        result = _run(analyst.chat(MULTI_INTENT[0], _prime(_memory(), analysis)))

        assert len(composer.calls) == 1
        assert result.provider == SpyRouter.NAME
        assert composer.partial_text not in result.content

    def test_planner_raising_falls_back(self, db_session, analysis):
        planner = StubPlanner(error=RuntimeError("planner exploded"))
        composer = SpyComposer()
        analyst, router, _ = _composed_analyst(
            db_session, analysis, mode="offline", fail_on_search=False,
            planner=planner, composer=composer,
        )
        result = _run(analyst.chat(MULTI_INTENT[0], _prime(_memory(), analysis)))

        assert planner.calls == [MULTI_INTENT[0]]
        assert composer.calls == []
        assert result.provider == SpyRouter.NAME

    def test_a_non_composable_plan_falls_back_without_composing(
        self, db_session, analysis,
    ):
        """`compose_answer` returning None is not a partial answer.

        The composer is *asked* and declines; nothing it might have built is
        allowed into the response.
        """
        composer = SpyComposer(returns_none=True)
        analyst, router, _ = _composed_analyst(
            db_session, analysis, mode="offline", fail_on_search=False,
            composer=composer,
        )
        result = _run(analyst.chat(MULTI_INTENT[0], _prime(_memory(), analysis)))

        assert result.content
        assert "partial" not in result.content.lower()
        assert result.provider == SpyRouter.NAME
        assert len(router.complete_calls) == 1

    def test_no_partial_deterministic_answer_is_recorded_in_memory(
        self, db_session, analysis,
    ):
        """The fallback owns the turn, including what memory stores."""
        composer = SpyComposer(raises=True)
        analyst, _, _ = _composed_analyst(
            db_session, analysis, mode="offline", fail_on_search=False,
            composer=composer,
        )
        memory = _prime(_memory(), analysis)
        result = _run(analyst.chat(MULTI_INTENT[0], memory))

        assert len(memory.turns) == 2
        stored = memory.turns[1].content
        assert result.content.startswith(stored)
        assert composer.partial_text not in stored


# ===========================================================================
class TestUnchangedPaths:
    """Streaming, batch and the single-intent path keep their behaviour."""

    def test_streaming_never_plans_or_composes(self, db_session, analysis):
        planner = StubPlanner(error=AssertionError("streaming must not plan"))
        composer = SpyComposer()
        router = StreamingSpyRouter()
        analyst, _, _ = _analyst(
            db_session, analysis, fail_on_search=False,
            planner=planner, composer=composer, router=router,
        )
        memory = _prime(_memory(), analysis)

        async def collect():
            return [token async for token in analyst.stream_chat(
                MULTI_INTENT[0], memory,
            )]

        tokens = _run(collect())

        assert planner.calls == []
        assert composer.calls == []
        assert len(router.complete_calls) == 1
        assert "".join(tokens).strip()

    def test_run_many_never_plans_or_composes(self, db_session, analysis):
        planner = StubPlanner(error=AssertionError("batch must not plan"))
        composer = SpyComposer()
        analyst, router, _ = _analyst(
            db_session, analysis, mode="offline", fail_on_search=False,
            planner=planner, composer=composer,
        )
        results = _run(analyst.run_many(["swot", "bull_case"]))

        assert planner.calls == []
        assert composer.calls == []
        assert [r.capability for r in results] == ["swot", "bull_case"]
        assert all(r.content for r in results)

    @pytest.mark.parametrize("question", SINGLE_INTENT)
    def test_single_intent_answers_are_identical_with_composition_on_and_off(
        self, db_session, analysis, question,
    ):
        """Byte-for-byte: opting in changes nothing for a single intent."""
        off, router_off, docs_off = _analyst(db_session, analysis)
        on, router_on, docs_on = _composed_analyst(db_session, analysis)

        first = _run(off.chat(question, _prime(_memory(), analysis)))
        second = _run(on.chat(question, _prime(_memory(), analysis)))

        assert first.content == second.content
        assert first.provider == second.provider == "deterministic"
        assert router_off.complete_calls == router_on.complete_calls == []
        assert docs_off.search_calls == docs_on.search_calls == []

    def test_provider_path_metadata_is_unchanged_for_other_questions(
        self, db_session, analysis,
    ):
        from app.services.ai.financial_intent import FinancialIntentResolver

        question = "What are the main risks for the company?"
        assert FinancialIntentResolver().resolve(question) is None

        off, _, _ = _analyst(db_session, analysis, mode="offline",
                             fail_on_search=False)
        on, _, _ = _composed_analyst(db_session, analysis, mode="offline",
                                     fail_on_search=False)

        first = _run(off.chat(question, _prime(_memory(), analysis)))
        second = _run(on.chat(question, _prime(_memory(), analysis)))

        assert first.provider == second.provider == SpyRouter.NAME
        assert first.content == second.content
        assert second.prompt_tokens > 0 and second.completion_tokens > 0


# ===========================================================================
class TestComposedAnswerContract:
    """The composed answer keeps the deterministic metadata convention."""

    def test_metadata_is_the_deterministic_convention(self, db_session, analysis):
        composer = SpyComposer()
        analyst, _, _ = _composed_analyst(db_session, analysis, composer=composer)
        result = _run(analyst.chat(MULTI_INTENT[0], _prime(_memory(), analysis)))

        composed = composer.results[0]
        assert isinstance(composed, ComposedAnswer)
        assert result.provider == "deterministic"
        assert result.model == "none"
        assert result.prompt_tokens == 0
        assert result.completion_tokens == 0
        assert result.total_tokens == 0
        assert result.cost_usd == 0.0
        assert result.prompt_version == 0
        assert result.prompt_key == "chat"

    def test_composed_content_is_the_engines_own_text_in_plan_order(
        self, db_session, analysis,
    ):
        from app.services.ai.financial_answer_engine import FinancialAnswerEngine
        from app.services.ai.investment_answer_engine import InvestmentAnswerEngine

        composer = SpyComposer()
        analyst, _, _ = _composed_analyst(db_session, analysis, composer=composer)
        context = analyst.context()
        question = "What is the P/E and the financial quality?"
        _run(analyst.chat(question, _prime(_memory(), analysis)))

        plan, _ = composer.calls[0]
        engines = {
            FinancialIntent.PE: FinancialAnswerEngine(),
            FinancialIntent.FINANCIAL_QUALITY: InvestmentAnswerEngine(),
        }
        expected = " ".join(
            engines[match.intent].answer(match.intent, context).content
            for match in plan.intents
        )
        assert composer.results[0].content == expected
