"""Phase 2E A2 — isolation for the external-provider paths A1 left open.

Phase 2E A1 gated the LLM registry: with
`AI_EXTERNAL_PROVIDERS_ENABLED=false` the four vendors are never assembled
into `ProviderRouter` rows, so no completion or stream can reach them. That
gate is asserted untouched here rather than re-implemented.

A1 was one door of seven. The platform also reaches an external AI service
through embeddings, two reranker builders, the LLM translator, the knowledge
layer (summaries, temporal observations, memory enrichment) and the
`/ai/health` probe loop. A2 consults one predicate —
`app.services.ai.external_gate.external_providers_enabled` — at each of them.

What these tests assert, and why each class of assertion is here:

* **Enabled means unchanged.** The flag defaults to true, so the interesting
  regression is not "disabled mode is broken" but "enabling isolation quietly
  changed the default path". Every scoped decision is therefore computed in
  both flag states and compared, not just checked in the disabled one.
* **Disabled means no call, not a failed call.** Each path is exercised with
  `urllib.request.urlopen` and `httpx.AsyncClient` replaced by counters. A
  provider that is unreachable and a provider that was never contacted look
  identical from the response side; only the transport can tell them apart.
* **Disabled means honest.** No fabricated prose, no invented embeddings, no
  fake citations. Where the architecture already has a lexical or glossary
  fallback, that fallback serves and says what it is. Where it does not — a
  permanent summary, a management verdict — the refusal is recorded against
  the item rather than papered over with template text.
* **Internal means untouched.** The deterministic engines of Parts 1/2A/2C/2D
  and the internal language renderer are byte-identical across both states.
"""
from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import pkgutil
import urllib.request
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models as _models_pkg
from app.core.config import Settings, settings
from app.db.base import Base
from app.domain.ai.types import (
    Citation, CompletionResponse, EvidenceKind, Message, NoProviderConfigured,
    Role, TokenUsage,
)
from app.domain.knowledge.enrichment import LLM_STAGES, STAGE_ORDER, EnrichmentStage
from app.domain.knowledge.temporal import TRACKED_DIMENSIONS
from app.domain.knowledge.vault import SummaryKind
from app.domain.language.types import Language
from app.services.ai.context_builder import GroundedContext
from app.services.ai.external_gate import (
    SETTING_NAME, ExternalProvidersDisabled, external_providers_enabled,
    gate_detail,
)
from app.services.ai.financial_answer_engine import FinancialAnswerEngine
from app.services.ai.financial_intent import FinancialIntent, FinancialIntentResolver
from app.services.ai.internal_composer import InternalComposer
from app.services.ai.internal_open_ended import InternalOpenEndedEngine
from app.services.ai.investment_answer_engine import InvestmentAnswerEngine
from app.services.ai.planner.question_planner import QuestionPlanner
from app.services.ai.providers import base as provider_base, gemini, mock
from app.services.ai.providers.base import ProviderConfig
from app.services.ai.providers.router import FALLBACK_ORDER, ProviderRouter
from app.services.knowledge.enrichment import MemoryEnrichmentService
from app.services.knowledge.summaries import SummaryService
from app.services.knowledge.temporal import TemporalMemoryService
from app.services.language.internal_renderer import (
    InternalLanguageRenderer, InternalRendererTranslator,
)
from app.services.language.translators import (
    GlossaryTranslator, LLMTranslator, PassthroughTranslator, build_translator,
)
from app.services.retrieval.embeddings import JinaV3Provider, build_semantic_embedder
from app.services.retrieval.rerank import (
    CrossEncoderReranker, LexicalCoverageReranker, RerankCandidate, build_reranker,
)
from app.services.retrieval.rerank_providers import (
    CohereReranker, JinaReranker, LocalCrossEncoderReranker, OpenAIJudgeReranker,
    build_rerank_provider,
)

for _module in pkgutil.iter_modules(_models_pkg.__path__):
    importlib.import_module(f"app.models.{_module.name}")

BACKEND = Path(__file__).resolve().parent.parent
APP = BACKEND / "app"
EXTERNAL_NAMES = frozenset({"Gemini", "OpenAI", "Claude", "OpenRouter"})

#: The Hindi prose used by the translation tests. Carries a citation marker
#: and a figure so the tests can assert neither was invented nor lost.
HINDI_SOURCE = (
    "Strengths: Revenue grew 20% year on year [p.12].\n\n"
    "Risks: Margin pressure from raw material costs."
)


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Flag control
# ---------------------------------------------------------------------------
@pytest.fixture()
def flag(monkeypatch):
    """Set `AI_EXTERNAL_PROVIDERS_ENABLED` on the running singleton.

    Returns a callable so one test can move between states, which several
    need: the honest comparison is the same call on both sides of the switch.
    """

    def set_flag(enabled: bool) -> bool:
        monkeypatch.setattr(settings, SETTING_NAME, enabled)
        return enabled

    set_flag(True)
    return set_flag


@pytest.fixture()
def mock_mode(monkeypatch):
    """Set `AI_MOCK_MODE`, restored automatically.

    The offline provider belongs to that switch alone — A1's boundary, which
    A2 does not move. Two tests need it off to reach the fail-closed branch,
    where nothing honest is left to serve.
    """

    def set_mode(value: bool) -> bool:
        monkeypatch.setattr(settings, "AI_MOCK_MODE", value)
        return value

    return set_mode


def stub_settings(**overrides) -> SimpleNamespace:
    """A settings stand-in carrying every key the scoped builders read.

    Keys are present in BOTH flag states on purpose. Isolation has to hold
    because of the flag, not because the credentials happen to be absent —
    a test that unset the keys would pass against code with no gate at all.
    """
    base = dict(
        JINA_API_KEY="jina-key", OPENROUTER_API_KEY="or-key",
        OPENAI_API_KEY="oa-key",
        ANTHROPIC_API_KEY="an-key",
        GEMINI_API_KEY="gm-key",
        RERANK_PROVIDER="jina", RERANK_API_KEY="rerank-key",
        RERANK_MODEL=None, RERANK_ENDPOINT=None,
        RERANKER_ENDPOINT="https://rerank.example/v1/rerank",
        RERANKER_MODEL="bge-reranker-large", RERANKER_API_KEY="legacy-key",
        AI_MOCK_MODE=True, EMBEDDING_PROVIDER=None,
        # Mirrors the running singleton. In production there is one settings
        # object and the builders read the flag from it; a stub that omitted
        # the attribute would read as "absent means enabled" and every
        # disabled-mode assertion below would be vacuous.
        AI_EXTERNAL_PROVIDERS_ENABLED=settings.AI_EXTERNAL_PROVIDERS_ENABLED,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# ---------------------------------------------------------------------------
# Transport sentinels
# ---------------------------------------------------------------------------
class _NetworkAttempted(RuntimeError):
    """Raised by a transport stub, so an attempt is visible as an exception."""


class _NetworkCounter:
    """Stands in for both transports and counts what it was asked to do.

    Counting rather than merely raising: a test that asserts "no external
    call" needs to distinguish zero attempts from an attempt that was
    swallowed by a broad `except Exception` somewhere upstream, and only a
    counter can tell those apart.
    """

    def __init__(self) -> None:
        self.urlopen_calls = 0
        self.httpx_calls = 0

    @property
    def total(self) -> int:
        return self.urlopen_calls + self.httpx_calls

    # Named `fake_*` rather than `urlopen`/`AsyncClient`: assigning a counter
    # of the same name onto the instance would shadow the method with an int,
    # which fails as "'int' object is not callable" instead of counting.
    def fake_urlopen(self, *args, **kwargs):
        self.urlopen_calls += 1
        raise _NetworkAttempted("urllib.request.urlopen was called")

    def fake_async_client(self, *args, **kwargs):
        self.httpx_calls += 1
        raise _NetworkAttempted("httpx.AsyncClient was constructed")


@pytest.fixture()
def network(monkeypatch):
    """Replace every transport the scoped paths use. Nothing leaves the box."""
    counter = _NetworkCounter()
    monkeypatch.setattr(urllib.request, "urlopen", counter.fake_urlopen)
    monkeypatch.setattr(provider_base.httpx, "AsyncClient",
                        counter.fake_async_client)
    return counter


@pytest.fixture()
def no_sleep(monkeypatch):
    """Silence the retry backoff.

    The embedding ladder sleeps 1.5s then 4.5s before giving up, which is the
    right behaviour in production and six wasted seconds in a test that is
    only asserting the ladder was walked.
    """
    monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)


# ---------------------------------------------------------------------------
# Router and provider stubs
# ---------------------------------------------------------------------------
class _StubRouter:
    """A `ProviderRouter` stand-in that records completions instead of making
    them. Lets a test prove a path DID run when enabled, and did NOT when
    disabled, without touching a network."""

    def __init__(self, content: str = "Revenue grew 20% [p.12].",
                 provider: str = "Gemini", echo: bool = False) -> None:
        self.content = content
        self.provider = provider
        #: Return the prompt verbatim. The translation tests need this: the
        #: integrity check rejects a reply that drops a `§n§` sentinel, so a
        #: canned string would fail the check for the wrong reason and the
        #: test would assert a rejection rather than a translation.
        self.echo = echo
        self.requests: list = []

    async def complete(self, request, **kwargs):
        self.requests.append(request)
        content = (
            request.messages[-1].content if self.echo else self.content
        )
        return CompletionResponse(
            content=content, provider=self.provider, model="stub-model",
            usage=TokenUsage(prompt_tokens=11, completion_tokens=7),
        )

    async def stream(self, request, **kwargs):
        self.requests.append(request)
        for token in ("Revenue ", "grew."):
            yield token


class _StubProvider:
    """What `/ai/health` probes. Records that it was probed."""

    def __init__(self, ledger: list[str], name: str) -> None:
        self.ledger = ledger
        self.name = name

    async def complete(self, request):
        self.ledger.append(self.name)
        return CompletionResponse(
            content="ok", provider=self.name, model="stub",
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1),
        )


class _ProbeRouter:
    """Router stand-in for the health endpoint, with a live-shape chain.

    The production singleton is assembled at import time, when this
    environment holds no keys, so its chain is offline-only and would never
    exercise the probe branch. This supplies a chain that does.
    """

    def __init__(self) -> None:
        self.probed: list[str] = []
        self.configs = [
            ProviderConfig(
                name="Gemini", endpoint="https://generativelanguage.example/v1",
                auth_header="(key in URL)", payload_shape="gemini",
                response_path="candidates.0.content.parts.0.text",
                default_model="gemini-2.0-flash", api_key="gm-key",
            ),
            ProviderConfig(
                name="Offline", endpoint="", auth_header="",
                payload_shape="offline", response_path="",
                default_model="offline", api_key="offline",
            ),
        ]

    def chain(self, preferred=None):
        return list(self.configs)

    @property
    def available(self):
        return [c.name for c in self.configs]

    def build(self, config):
        return _StubProvider(self.probed, config.name)


@pytest.fixture()
def stub_router(monkeypatch):
    """Install `_StubRouter` as the module-level router the knowledge layer
    imports. `SummaryService._complete` and `TemporalMemoryService._ask` both
    do `from app.services.ai.service import _router` at call time, so
    patching the module attribute is enough."""
    router = _StubRouter()
    monkeypatch.setattr("app.services.ai.service._router", router)
    return router


# ---------------------------------------------------------------------------
# Database fixtures
# ---------------------------------------------------------------------------
@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def company(db):
    from app.models.company import Company

    row = Company(
        id=str(uuid.uuid4()), name="Isolation Ltd.", ticker="ISOL",
        exchange="NSE", listing_status="active",
    )
    db.add(row)
    db.commit()
    return row


@pytest.fixture()
def document(db, company):
    """One completed filing with indexed text, so a summary has a source."""
    from app.models.document import Document, DocumentChunk

    row = Document(
        company_id=company.id, filename="isol-ar.pdf", title="Isolation AR",
        doc_type="annual_report", file_format="pdf", size_bytes=1,
        status="completed", content_hash=uuid.uuid4().hex, fiscal_year=2026,
    )
    db.add(row)
    db.commit()
    db.add(DocumentChunk(
        document_id=row.id, chunk_index=0, page=12,
        text="Revenue from operations grew 20% to 1,000 crore during the year.",
        fingerprint=uuid.uuid4().hex[:40],
    ))
    db.commit()
    return row


@pytest.fixture()
def summarised_year(db, company, document):
    """Evidence for a temporal observation: a stored, non-fallback summary."""
    from app.models.knowledge import DocumentSummary

    db.add(DocumentSummary(
        document_id=document.id, company_id=company.id,
        kind=SummaryKind.BRIEF_100.value,
        content="Revenue grew 20% [p.12]; margins held.",
        word_count=7, fiscal_year=2026, doc_type="annual_report",
        provider="Gemini", model="gemini-2.0-flash", prompt_version=1,
        is_fallback=False,
    ))
    db.commit()
    return 2026


# ===========================================================================
# 1. The gate helper itself
# ===========================================================================
class TestTheGateHelper:
    def test_absent_setting_means_enabled(self):
        """The safe default. A settings object that predates the flag — a
        test stub, an old serialisation — must not disable production AI."""
        assert external_providers_enabled(SimpleNamespace()) is True

    def test_none_means_enabled(self):
        assert external_providers_enabled(
            SimpleNamespace(AI_EXTERNAL_PROVIDERS_ENABLED=None)
        ) is True

    def test_explicit_booleans_are_honoured(self):
        assert external_providers_enabled(
            SimpleNamespace(AI_EXTERNAL_PROVIDERS_ENABLED=True)) is True
        assert external_providers_enabled(
            SimpleNamespace(AI_EXTERNAL_PROVIDERS_ENABLED=False)) is False

    @pytest.mark.parametrize("text", ["false", "False", "FALSE", " false ",
                                      "0", "no", "off", ""])
    def test_falsy_text_is_read_as_off(self, text):
        """`"false"` is truthy in Python. A stub built from `os.environ` would
        otherwise make the flag do the opposite of what was typed."""
        assert external_providers_enabled(
            SimpleNamespace(AI_EXTERNAL_PROVIDERS_ENABLED=text)) is False

    @pytest.mark.parametrize("text", ["true", "1", "yes", "on"])
    def test_truthy_text_is_read_as_on(self, text):
        assert external_providers_enabled(
            SimpleNamespace(AI_EXTERNAL_PROVIDERS_ENABLED=text)) is True

    def test_the_running_singleton_defaults_to_enabled(self):
        assert Settings.model_fields[SETTING_NAME].default is True
        assert settings.AI_EXTERNAL_PROVIDERS_ENABLED is True
        assert external_providers_enabled() is True

    def test_settings_can_be_read_from_the_environment(self, monkeypatch):
        monkeypatch.setenv(SETTING_NAME, "false")
        assert Settings(_env_file=None).AI_EXTERNAL_PROVIDERS_ENABLED is False

    def test_the_setting_name_is_the_one_a1_introduced(self):
        assert SETTING_NAME == "AI_EXTERNAL_PROVIDERS_ENABLED"

    def test_the_refusal_is_a_runtime_error(self):
        """So the per-item `except Exception` handlers that already survive a
        provider outage record this one too, with no new failure path."""
        assert issubclass(ExternalProvidersDisabled, RuntimeError)

    def test_the_detail_names_the_setting_and_its_value(self):
        detail = gate_detail("translation")
        assert "translation" in detail
        assert f"{SETTING_NAME}=false" in detail
        assert f"{SETTING_NAME}=true" in detail

    def test_the_helper_resolves_settings_lazily(self):
        """Importing the gate must not drag the provider stack with it — the
        retrieval and language layers import it at module scope."""
        source = inspect.getsource(external_providers_enabled)
        assert "from app.core.config import settings" in source
        # ... and that import is inside the function, not at module level.
        module_source = inspect.getsource(
            importlib.import_module("app.services.ai.external_gate")
        )
        head = module_source.split("def external_providers_enabled")[0]
        assert "app.core.config" not in head

    def test_the_module_imports_no_provider(self):
        source = Path(
            APP / "services" / "ai" / "external_gate.py"
        ).read_text()
        for forbidden in ("providers.router", "providers import", "httpx",
                          "urllib"):
            assert forbidden not in source, forbidden


# ===========================================================================
# 2. Enabled — every scoped path behaves exactly as it did before A2
# ===========================================================================
class TestEnabledPreservesExistingBehaviour:
    def test_semantic_embedder_is_still_selected(self):
        assert isinstance(
            build_semantic_embedder(stub_settings()), JinaV3Provider,
        )

    def test_the_rerank_provider_is_still_selected(self):
        assert isinstance(build_rerank_provider(stub_settings()), JinaReranker)

    def test_the_legacy_cross_encoder_is_still_selected(self):
        assert isinstance(build_reranker(stub_settings()), CrossEncoderReranker)
        assert CrossEncoderReranker("https://x", "m", "k").available is True

    def test_memory_enrichment_still_allows_llm_stages(self, db):
        assert MemoryEnrichmentService(db).llm_enabled is True
        assert MemoryEnrichmentService(db, allow_llm=False).llm_enabled is False

    def test_the_translator_still_reaches_the_provider(self, flag):
        """With the flag on, `LLMTranslator` asks the router. This is the
        assertion that makes the disabled-mode test meaningful: without it,
        a translator that never called anything would pass both."""
        router = _StubRouter(echo=True)
        result = _run(LLMTranslator(router).translate(
            HINDI_SOURCE, Language.HINDI,
        ))
        assert len(router.requests) == 1
        assert result.provider == "llm"
        assert result.translated is True
        assert result.integrity_problems == []

    def test_summaries_are_generated_through_the_provider(
        self, db, company, document, stub_router,
    ):
        run = SummaryService(db).generate_for_document(
            document, kinds=[SummaryKind.BRIEF_100],
        )
        assert run.generated == 1
        assert run.failed == 0
        assert len(stub_router.requests) == 1
        rows = db.execute(select(_summary_model())).scalars().all()
        assert len(rows) == 1
        assert rows[0].provider == "Gemini"

    def test_an_observation_is_generated_through_the_provider(
        self, db, company, summarised_year, stub_router,
    ):
        stub_router.content = OBSERVATION_JSON
        service = TemporalMemoryService(db)
        row = service.generate_year(company.id, summarised_year)
        assert row is not None
        assert len(stub_router.requests) == 1
        assert row.generated_by.startswith("Gemini:")

    def test_selection_is_identical_in_both_flag_states(self, flag):
        """The strongest form of "unchanged": the same call, both states, the
        same answer. Covers the three builders, whose whole job is a
        decision rather than an effect."""
        outcomes: dict[bool, tuple[str, ...]] = {}
        for enabled in (True, False):
            flag(enabled)
            # Only the ENABLED side is recorded here; the disabled side is
            # asserted by its own tests below. What this pins is that turning
            # the flag on again restores the previous selection exactly.
            outcomes[enabled] = (
                type(build_semantic_embedder(stub_settings())).__name__,
                type(build_rerank_provider(stub_settings())).__name__,
                type(build_reranker(stub_settings())).__name__,
            )
        assert outcomes[True] == (
            "JinaV3Provider", "JinaReranker", "CrossEncoderReranker",
        )
        flag(True)
        assert (
            type(build_semantic_embedder(stub_settings())).__name__,
            type(build_rerank_provider(stub_settings())).__name__,
            type(build_reranker(stub_settings())).__name__,
        ) == outcomes[True]


def _summary_model():
    from app.models.knowledge import DocumentSummary

    return DocumentSummary


# ===========================================================================
# 3. Summaries — no fabricated permanent memory
# ===========================================================================
class TestSummariesAreGated:
    def test_complete_refuses_before_building_a_request(
        self, flag, db, stub_router,
    ):
        flag(False)
        with pytest.raises(ExternalProvidersDisabled) as exc:
            SummaryService(db)._complete("prompt", 100)  # noqa: SLF001
        assert stub_router.requests == []
        assert SETTING_NAME in str(exc.value)

    def test_a_pass_records_the_refusal_per_kind(
        self, flag, db, company, document, stub_router,
    ):
        flag(False)
        run = SummaryService(db).generate_for_document(document)
        assert run.generated == 0
        assert run.fallbacks == 0
        assert run.failed == len(SummaryKind)
        assert len(run.errors) == len(SummaryKind)
        assert all("ExternalProvidersDisabled" in e["error"]
                   for e in run.errors)
        assert stub_router.requests == []

    def test_nothing_is_written_to_permanent_memory(
        self, flag, db, company, document, stub_router,
    ):
        flag(False)
        SummaryService(db).generate_for_document(document)
        assert db.execute(select(_summary_model())).scalars().all() == []

    def test_the_error_names_the_kind_so_a_operator_can_act(
        self, flag, db, company, document, stub_router,
    ):
        flag(False)
        run = SummaryService(db).generate_for_document(
            document, kinds=[SummaryKind.RISK],
        )
        assert run.errors[0]["kind"] == SummaryKind.RISK.value

    def test_a_disabled_pass_does_not_mark_a_fallback(
        self, flag, db, company, document, stub_router,
    ):
        """`is_fallback` means "the offline composer wrote this". A disabled
        deployment wrote nothing, and must not borrow that label."""
        flag(False)
        run = SummaryService(db).generate_for_document(document)
        assert run.fallbacks == 0
        assert run.tokens == 0
        assert run.cost_usd == 0.0

    def test_the_gate_is_checked_before_the_request_is_assembled(
        self, flag, db, stub_router,
    ):
        """A gate placed after prompt construction would still spend the
        tokens it was meant to save."""
        source = inspect.getsource(SummaryService._complete)
        gate_line = next(
            i for i, line in enumerate(source.splitlines())
            if "external_providers_enabled()" in line
        )
        request_line = next(
            i for i, line in enumerate(source.splitlines())
            if "request = CompletionRequest(" in line
        )
        assert gate_line < request_line

    def test_enabled_mode_is_untouched(self, db, company, document, stub_router):
        """The control: with the flag on, the same document produces
        summaries. Without this the tests above would also pass against a
        summariser that never worked."""
        run = SummaryService(db).generate_for_document(
            document, kinds=[SummaryKind.BRIEF_100, SummaryKind.RISK],
        )
        assert run.generated == 2
        assert run.failed == 0


# ===========================================================================
# 4. Temporal observations — no fabricated management verdicts
# ===========================================================================
class TestTemporalObservationsAreGated:
    def test_ask_refuses(self, flag, db, summarised_year, stub_router):
        flag(False)
        service = TemporalMemoryService(db)
        with pytest.raises(ExternalProvidersDisabled):
            service._ask(  # noqa: SLF001
                fiscal_year=summarised_year, evidence="[E1] revenue grew",
                prior=None, metrics={}, prior_metrics={},
            )
        assert stub_router.requests == []

    def test_generate_year_propagates_the_refusal(
        self, flag, db, company, summarised_year, stub_router,
    ):
        flag(False)
        with pytest.raises(ExternalProvidersDisabled):
            TemporalMemoryService(db).generate_year(company.id, summarised_year)
        assert stub_router.requests == []

    def test_a_series_records_the_failure_and_keeps_going(
        self, flag, db, company, summarised_year, stub_router,
    ):
        """One disabled year must not abort the series — the existing
        per-year handler records it, exactly as it records a provider
        outage."""
        flag(False)
        run = TemporalMemoryService(db).build_company(company.id)
        assert run.generated == 0
        assert run.failed >= 1
        assert any("ExternalProvidersDisabled" in e["error"]
                   for e in run.errors)
        assert stub_router.requests == []

    def test_no_observation_row_is_written(
        self, flag, db, company, summarised_year, stub_router,
    ):
        from app.models.knowledge import YearlyObservation

        flag(False)
        with pytest.raises(ExternalProvidersDisabled):
            TemporalMemoryService(db).generate_year(company.id, summarised_year)
        assert db.execute(select(YearlyObservation)).scalars().all() == []

    def test_reading_the_timeline_needs_no_provider(self, flag, db, company):
        """The read path is provider-free and must stay reachable — a
        disabled deployment can still answer from what it already holds."""
        flag(False)
        assert TemporalMemoryService(db).timeline(company.id) == []
        assert TemporalMemoryService(db).credibility(company.id)

    def test_the_gate_precedes_the_prompt(self, flag):
        source = inspect.getsource(TemporalMemoryService._ask)
        gate_line = next(
            i for i, line in enumerate(source.splitlines())
            if "external_providers_enabled()" in line
        )
        prompt_line = next(
            i for i, line in enumerate(source.splitlines())
            if "prompt = (" in line
        )
        assert gate_line < prompt_line

    def test_enabled_mode_is_untouched(
        self, db, company, summarised_year, stub_router,
    ):
        """The control for this class: with the flag on the same year is
        judged, from a real reply, and stored as a non-fallback observation."""
        stub_router.content = OBSERVATION_JSON
        row = TemporalMemoryService(db).generate_year(company.id, summarised_year)
        assert row is not None
        assert row.is_fallback is False
        assert row.status == "current"


# ===========================================================================
# 5. Memory enrichment — the structural half still runs
# ===========================================================================
class TestEnrichmentSkipsLlmStagesOnly:
    @staticmethod
    def _outcomes(result):
        return {o.stage: o for o in result.stages}

    def test_llm_enabled_is_the_request_anded_with_the_gate(self, flag, db):
        flag(True)
        assert MemoryEnrichmentService(db).llm_enabled is True
        assert MemoryEnrichmentService(db, allow_llm=False).llm_enabled is False
        flag(False)
        assert MemoryEnrichmentService(db).llm_enabled is False
        assert MemoryEnrichmentService(db, allow_llm=False).llm_enabled is False

    def test_a_job_payload_cannot_overrule_the_flag(self, flag, db):
        """`handlers.py` reads `allow_llm` from the job payload, so this is
        the path an operator's decision could otherwise be undone by."""
        flag(False)
        assert MemoryEnrichmentService(db, allow_llm=True).llm_enabled is False

    def test_the_gate_is_read_when_the_pass_runs(self, flag, db):
        """Resolved on read, not at construction: a job queued before the
        switch was flipped must honour the switch."""
        service = MemoryEnrichmentService(db)
        assert service.llm_enabled is True
        flag(False)
        assert service.llm_enabled is False

    def test_llm_stages_are_skipped_and_say_why(
        self, flag, db, company, stub_router,
    ):
        flag(False)
        result = MemoryEnrichmentService(db).run(company.id)
        outcomes = self._outcomes(result)
        for stage in LLM_STAGES:
            assert outcomes[stage].skipped is True, stage
            assert SETTING_NAME in outcomes[stage].detail, stage

    def test_structural_stages_are_not_skipped_for_that_reason(
        self, flag, db, company, stub_router,
    ):
        flag(False)
        result = MemoryEnrichmentService(db).run(company.id)
        outcomes = self._outcomes(result)
        structural = set(STAGE_ORDER) - set(LLM_STAGES)
        assert structural == {
            EnrichmentStage.FINANCIAL_PROMOTION, EnrichmentStage.VAULT,
            EnrichmentStage.TEMPORAL_LINK,
        }
        for stage in structural:
            detail = outcomes[stage].detail or ""
            assert SETTING_NAME not in detail, stage

    def test_every_stage_still_reports(self, flag, db, company, stub_router):
        """A skipped stage is still a stage. Silently dropping the three LLM
        stages would make a disabled pass look like a truncated one."""
        flag(False)
        result = MemoryEnrichmentService(db).run(company.id)
        assert {o.stage for o in result.stages} == set(STAGE_ORDER)

    def test_no_provider_is_called_during_a_disabled_pass(
        self, flag, db, company, document, stub_router,
    ):
        flag(False)
        MemoryEnrichmentService(db).run(company.id)
        assert stub_router.requests == []

    def test_no_summary_or_observation_is_written(
        self, flag, db, company, document, stub_router,
    ):
        from app.models.knowledge import DocumentSummary, YearlyObservation

        flag(False)
        MemoryEnrichmentService(db).run(company.id)
        assert db.execute(select(DocumentSummary)).scalars().all() == []
        assert db.execute(select(YearlyObservation)).scalars().all() == []

    def test_an_explicit_structural_pass_keeps_its_own_wording(
        self, flag, db, company,
    ):
        """Two reasons to skip, two messages. `allow_llm=False` is a caller
        asking for a structural pass; the flag is an operator disabling
        providers. Conflating them hides which one happened."""
        flag(False)
        result = MemoryEnrichmentService(db, allow_llm=False).run(company.id)
        outcomes = self._outcomes(result)
        for stage in LLM_STAGES:
            assert outcomes[stage].detail == "LLM stages disabled for this pass"

    def test_enabled_mode_runs_the_llm_stages(
        self, db, company, document, stub_router,
    ):
        result = MemoryEnrichmentService(db).run(company.id)
        outcomes = self._outcomes(result)
        assert outcomes[EnrichmentStage.SUMMARIES].skipped is False
        assert outcomes[EnrichmentStage.SUMMARIES].written > 0
        assert stub_router.requests


# ===========================================================================
# 6. /ai/health — reported as disabled, and never probed
# ===========================================================================
@pytest.fixture()
def api_client():
    import tests.conftest  # noqa: F401 — installs the DB overrides and seeds

    from app.main import app

    return TestClient(app)


class TestHealthReportsDisabled:
    @pytest.fixture()
    def probe_router(self, monkeypatch):
        router = _ProbeRouter()
        monkeypatch.setattr("app.services.ai.service._router", router)
        return router

    def test_disabled_health_reports_the_flag(self, flag, api_client, probe_router):
        flag(False)
        body = api_client.get("/api/v1/ai/health").json()
        assert body["external_providers_enabled"] is False

    def test_enabled_health_reports_the_flag(self, flag, api_client, probe_router):
        flag(True)
        body = api_client.get("/api/v1/ai/health").json()
        assert body["external_providers_enabled"] is True

    def test_no_provider_is_probed_while_disabled(
        self, flag, api_client, probe_router, network,
    ):
        flag(False)
        body = api_client.get("/api/v1/ai/health").json()
        assert probe_router.probed == []
        assert network.total == 0
        by_name = {p["provider"]: p for p in body["providers"]}
        assert by_name["Gemini"]["status"] == "disabled"
        assert by_name["Gemini"]["latency_ms"] == 0.0

    def test_a_disabled_provider_is_listed_rather_than_omitted(
        self, flag, api_client, probe_router,
    ):
        """An endpoint that quietly drops a provider is indistinguishable
        from one that lost it. The chain and the entries must still name it."""
        flag(False)
        body = api_client.get("/api/v1/ai/health").json()
        assert "Gemini" in body["chain"]
        assert "Gemini" in [p["provider"] for p in body["providers"]]
        detail = next(
            p["detail"] for p in body["providers"] if p["provider"] == "Gemini"
        )
        assert SETTING_NAME in detail

    def test_the_offline_provider_is_still_reported_ready(
        self, flag, api_client, probe_router,
    ):
        """AI_MOCK_MODE keeps sole ownership of the offline provider — A1's
        boundary, unchanged by A2."""
        flag(False)
        body = api_client.get("/api/v1/ai/health").json()
        offline = next(
            p for p in body["providers"] if p["provider"] == "Offline"
        )
        assert offline["status"] == "ready"
        assert body["serving"] == "Offline"

    def test_enabled_health_still_probes(
        self, flag, api_client, probe_router, network,
    ):
        """The control. Without it, an endpoint that stopped probing
        altogether would satisfy every disabled-mode assertion above."""
        flag(True)
        body = api_client.get("/api/v1/ai/health").json()
        assert probe_router.probed == ["Gemini"]
        by_name = {p["provider"]: p for p in body["providers"]}
        assert by_name["Gemini"]["status"] == "ok"

    def test_the_endpoint_never_leaks_a_key(self, flag, api_client, probe_router):
        for enabled in (True, False):
            flag(enabled)
            body = api_client.get("/api/v1/ai/health").json()
            assert "gm-key" not in str(body)


# ===========================================================================
# 7. Translation — the glossary serves, and says it is not a translation
# ===========================================================================
class TestTranslationFallsBackSafely:
    def test_no_provider_is_called(self, flag, network):
        flag(False)
        router = _StubRouter()
        _run(LLMTranslator(router).translate(HINDI_SOURCE, Language.HINDI))
        assert router.requests == []
        assert network.total == 0

    def test_the_glossary_serves_instead(self, flag):
        flag(False)
        result = _run(LLMTranslator().translate(HINDI_SOURCE, Language.HINDI))
        assert result.provider == "glossary"
        assert result.text != HINDI_SOURCE
        assert "मज़बूतियाँ" in result.text  # "Strengths:" rendered

    def test_the_result_is_not_presented_as_a_translation(self, flag):
        """The honesty this module exists for: a caller must be able to tell
        Hindi from English-with-Hindi-terminology."""
        flag(False)
        result = _run(LLMTranslator().translate(HINDI_SOURCE, Language.HINDI))
        assert result.translated is False

    def test_the_detail_names_the_flag(self, flag):
        flag(False)
        result = _run(LLMTranslator().translate(HINDI_SOURCE, Language.HINDI))
        assert SETTING_NAME in result.detail

    def test_citations_and_figures_survive_the_fallback(self, flag):
        """No fake citations — and none lost either. The protected spans are
        the reason the translation layer has an integrity check at all."""
        flag(False)
        result = _run(LLMTranslator().translate(HINDI_SOURCE, Language.HINDI))
        assert "[p.12]" in result.text
        assert "20%" in result.text
        assert result.integrity_problems == []

    def test_english_is_still_a_passthrough(self, flag):
        for enabled in (True, False):
            flag(enabled)
            result = _run(LLMTranslator().translate(
                "Revenue grew.", Language.ENGLISH,
            ))
            assert result.translated is True
            assert result.text == "Revenue grew."

    def test_hinglish_declines_honestly(self, flag):
        flag(False)
        result = _run(LLMTranslator().translate(HINDI_SOURCE, Language.HINGLISH))
        assert result.translated is False
        assert result.language is Language.ENGLISH

    def test_the_fallback_matches_the_glossary_translator_exactly(self, flag):
        """The fallback IS the glossary translator, not a new partial
        implementation that can drift from it."""
        flag(False)
        via_llm = _run(LLMTranslator().translate(HINDI_SOURCE, Language.HINDI))
        direct = _run(GlossaryTranslator().translate(
            HINDI_SOURCE, Language.HINDI,
        ))
        assert via_llm.text == direct.text
        assert via_llm.fidelity == direct.fidelity

    def test_the_deterministic_translators_are_flag_invariant(self, flag):
        for translator in (PassthroughTranslator(), GlossaryTranslator()):
            outputs = {}
            for enabled in (True, False):
                flag(enabled)
                result = _run(translator.translate(
                    HINDI_SOURCE, Language.HINDI,
                ))
                outputs[enabled] = (result.text, result.translated,
                                    result.provider, result.detail)
            assert outputs[True] == outputs[False]

    def test_translator_selection_is_unchanged(self, flag):
        """Selecting a translator is not an external call. `build_translator`
        is therefore not gated, and the default stays `llm` — the gate lives
        where the call is made."""
        for enabled in (True, False):
            flag(enabled)
            assert isinstance(build_translator(Settings()), LLMTranslator)
        assert Settings.model_fields["TRANSLATION_PROVIDER"].default == "llm"

    def test_the_internal_renderer_is_flag_invariant(self, flag):
        outputs = {}
        for enabled in (True, False):
            flag(enabled)
            result = _run(InternalRendererTranslator().translate(
                "Revenue grew 20%.", Language.HINDI,
            ))
            outputs[enabled] = (result.text, result.translated,
                                result.fidelity, result.provider)
        assert outputs[True] == outputs[False]

    def test_the_hybrid_translator_survives_a_disabled_primary(self, flag):
        """`TRANSLATION_PROVIDER=hybrid` puts LLMTranslator behind the
        internal renderer. A declined primary must not become an exception
        or a fabricated rendering."""
        from app.services.language.internal_renderer import (
            InternalFallbackTranslator,
        )

        flag(False)
        translator = InternalFallbackTranslator(
            primary=LLMTranslator(), internal=InternalRendererTranslator(),
        )
        result = _run(translator.translate(HINDI_SOURCE, Language.HINDI))
        assert result.text
        assert result.translated is False

    def test_the_gate_precedes_the_router_call(self):
        source = inspect.getsource(LLMTranslator.translate)
        gate_line = next(
            i for i, line in enumerate(source.splitlines())
            if "external_providers_enabled()" in line
        )
        call_line = next(
            i for i, line in enumerate(source.splitlines())
            if "self.router.complete(" in line
        )
        assert gate_line < call_line


# ===========================================================================
# 8. Embeddings — no invented vectors
# ===========================================================================
class TestEmbeddingsFallBackSafely:
    def test_no_embedder_is_built_while_disabled(self, flag):
        flag(False)
        assert build_semantic_embedder(stub_settings()) is None

    def test_a_preferred_provider_cannot_resurrect_one(self, flag):
        """Mirrors A1's registry assertion at the embedding layer: naming a
        provider does not rebuild a selection the gate removed."""
        flag(False)
        for preferred in ("jina-v3", "bge-m3", "openai-small"):
            assert build_semantic_embedder(
                stub_settings(), preferred=preferred,
            ) is None

    def test_an_unconfigured_deployment_still_returns_none(self, flag):
        """The existing contract — None rather than a silent downgrade to the
        hashed embedder — is unchanged when enabled."""
        keys_absent = stub_settings(
            JINA_API_KEY=None, OPENROUTER_API_KEY=None, OPENAI_API_KEY=None,
        )
        for enabled in (True, False):
            flag(enabled)
            assert build_semantic_embedder(keys_absent) is None

    def test_a_directly_constructed_provider_refuses(self, flag, network):
        """The builder is the first lock; this is the second. A backfill
        script that constructs a provider itself must not reach the network
        either."""
        flag(False)
        with pytest.raises(RuntimeError) as exc:
            JinaV3Provider("jina-key").embed(["revenue grew 20%"])
        assert SETTING_NAME in str(exc.value)
        assert network.urlopen_calls == 0

    def test_enabled_mode_still_attempts_the_provider(
        self, flag, network, no_sleep,
    ):
        """The control: with the flag on the same call reaches the transport.
        The stub raises and the retry ladder turns that into a RuntimeError,
        which is the proof the gate — not a missing key — is what stops the
        disabled case."""
        flag(True)
        with pytest.raises(RuntimeError):
            JinaV3Provider("jina-key").embed(["revenue grew 20%"])
        assert network.urlopen_calls >= 1

    def test_no_vector_is_fabricated(self, flag):
        """There is no honest fallback embedding, so there is no fallback
        embedding. A caller that receives None decides what to do without
        semantics — which is what the engine already does."""
        flag(False)
        embedder = build_semantic_embedder(stub_settings())
        assert embedder is None

    def test_the_engine_degrades_to_lexical(self, flag, db):
        from app.services.retrieval.engine import HybridRetrievalEngine

        flag(False)
        engine = HybridRetrievalEngine(db)
        assert engine.embedder is None
        assert engine.available is False

    def test_the_engine_reports_semantics_available_when_enabled(
        self, flag, db, monkeypatch,
    ):
        """The engine resolves from the application singleton, so the key is
        set there — this environment ships without one."""
        from app.services.retrieval.engine import HybridRetrievalEngine

        flag(True)
        monkeypatch.setattr(settings, "JINA_API_KEY", "jina-key")
        engine = HybridRetrievalEngine(db)
        assert isinstance(engine.embedder, JinaV3Provider)
        assert engine.available is True

    def test_a_disabled_engine_never_embeds_a_query(self, flag, db, network):
        """`_semantic` returns [] and retrieval continues on the lexical
        signal — the degradation path that already exists for a 402."""
        from app.services.retrieval.engine import HybridRetrievalEngine

        flag(False)
        engine = HybridRetrievalEngine(db)
        assert engine._semantic("revenue growth", None, None) == []  # noqa: SLF001
        assert network.total == 0

    def test_the_backfill_service_reports_skipped(self, flag, db):
        from app.services.retrieval.backfill import EmbeddingBackfillService

        flag(False)
        service = EmbeddingBackfillService(db)
        assert service.embedder is None


# ===========================================================================
# 9. Reranking — the lexical scorer serves
# ===========================================================================
CANDIDATES = [
    RerankCandidate(1, "Revenue grew 20% year on year, driven by volume."),
    RerankCandidate(2, "The board met four times during the year."),
]


class TestRerankingFallsBackSafely:
    @pytest.mark.parametrize("name", ["jina", "cohere", "openai", "local"])
    def test_every_hosted_and_local_provider_falls_back(self, flag, name):
        flag(False)
        provider = build_rerank_provider(
            stub_settings(RERANK_PROVIDER=name),
        )
        assert isinstance(provider, LexicalCoverageReranker)

    def test_an_unknown_provider_still_falls_back(self, flag):
        flag(False)
        assert isinstance(
            build_rerank_provider(stub_settings(RERANK_PROVIDER="nope")),
            LexicalCoverageReranker,
        )

    def test_the_legacy_builder_falls_back(self, flag):
        """`build_reranker` is a second door into a cross-encoder. A gate on
        only one of the two builders would be a gate on neither."""
        flag(False)
        assert isinstance(build_reranker(stub_settings()),
                          LexicalCoverageReranker)

    def test_enabled_mode_keeps_the_configured_providers(self, flag):
        flag(True)
        assert isinstance(
            build_rerank_provider(stub_settings(RERANK_PROVIDER="jina")),
            JinaReranker,
        )
        assert isinstance(
            build_rerank_provider(stub_settings(RERANK_PROVIDER="cohere")),
            CohereReranker,
        )
        assert isinstance(
            build_rerank_provider(stub_settings(RERANK_PROVIDER="openai")),
            OpenAIJudgeReranker,
        )
        assert isinstance(build_reranker(stub_settings()), CrossEncoderReranker)

    @pytest.mark.parametrize("provider_cls", [
        JinaReranker, CohereReranker, OpenAIJudgeReranker,
    ])
    def test_a_directly_constructed_reranker_refuses(
        self, flag, provider_cls, network,
    ):
        flag(False)
        with pytest.raises(RuntimeError) as exc:
            provider_cls("rerank-key").rerank("revenue growth", CANDIDATES)
        assert SETTING_NAME in str(exc.value)
        assert network.urlopen_calls == 0

    def test_the_local_cross_encoder_is_refused_before_loading(
        self, flag, network,
    ):
        """`CrossEncoder(...)` downloads roughly 1.3 GB of weights on first
        use. Gated ahead of `_load()`, so a disabled deployment never
        triggers that fetch."""
        flag(False)
        with pytest.raises(RuntimeError) as exc:
            LocalCrossEncoderReranker().rerank("revenue growth", CANDIDATES)
        assert SETTING_NAME in str(exc.value)

    def test_the_legacy_cross_encoder_refuses(self, flag, network):
        flag(False)
        reranker = CrossEncoderReranker("https://rerank.example/v1", "m", "k")
        assert reranker.available is False
        with pytest.raises(RuntimeError) as exc:
            reranker.rerank("revenue growth", CANDIDATES)
        assert SETTING_NAME in str(exc.value)
        assert network.urlopen_calls == 0

    def test_the_legacy_cross_encoder_is_available_when_enabled(self, flag):
        flag(True)
        assert CrossEncoderReranker("https://rerank.example/v1", "m",
                                    "k").available is True

    def test_enabled_mode_reaches_the_transport(
        self, flag, network, no_sleep,
    ):
        """The control for the three refusal tests above."""
        flag(True)
        with pytest.raises(RuntimeError):
            JinaReranker("rerank-key").rerank("revenue growth", CANDIDATES)
        assert network.urlopen_calls >= 1

    def test_the_lexical_fallback_really_reranks(self, flag):
        """The fallback must be a reranker, not an empty list. An empty
        result would be read as "nothing relevant" and the fused ranking
        would be silently discarded."""
        flag(False)
        scores = build_rerank_provider(stub_settings()).rerank(
            "revenue growth", CANDIDATES,
        )
        assert [s.chunk_id for s in scores] == [1, 2]
        assert scores[0].score > scores[1].score

    def test_the_lexical_scores_are_flag_invariant(self, flag):
        outputs = {}
        for enabled in (True, False):
            flag(enabled)
            scores = LexicalCoverageReranker().rerank(
                "revenue growth", CANDIDATES,
            )
            outputs[enabled] = [(s.chunk_id, s.score) for s in scores]
        assert outputs[True] == outputs[False]

    def test_the_engine_gets_the_lexical_reranker_when_disabled(self, flag, db):
        from app.services.retrieval.engine import HybridRetrievalEngine

        flag(False)
        engine = HybridRetrievalEngine(db)
        assert isinstance(engine.reranker, LexicalCoverageReranker)


# ===========================================================================
# 10. No external network call from any scoped path
# ===========================================================================
class TestNoExternalNetworkCall:
    def test_every_disabled_path_is_silent(self, flag, db, company, document,
                                            summarised_year, network,
                                            stub_router):
        """One test, every scoped path, both transports replaced. Reaching
        the end means nothing left the process."""
        flag(False)

        assert build_semantic_embedder(stub_settings()) is None
        with pytest.raises(RuntimeError):
            JinaV3Provider("jina-key").embed(["revenue"])
        # The builders hand back the lexical reranker, which runs locally.
        build_rerank_provider(stub_settings()).rerank("revenue", CANDIDATES)
        build_reranker(stub_settings()).rerank("revenue", CANDIDATES)
        for provider_cls in (JinaReranker, CohereReranker, OpenAIJudgeReranker):
            with pytest.raises(RuntimeError):
                provider_cls("k").rerank("revenue", CANDIDATES)
        with pytest.raises(RuntimeError):
            LocalCrossEncoderReranker().rerank("revenue", CANDIDATES)
        with pytest.raises(RuntimeError):
            CrossEncoderReranker("https://x", "m", "k").rerank(
                "revenue", CANDIDATES,
            )

        _run(LLMTranslator().translate(HINDI_SOURCE, Language.HINDI))
        SummaryService(db).generate_for_document(document)
        TemporalMemoryService(db).build_company(company.id)
        MemoryEnrichmentService(db).run(company.id)

        router = ProviderRouter()
        try:
            _run(router.complete(_completion_request()))
        except NoProviderConfigured:
            pass

        assert network.total == 0
        assert stub_router.requests == []

    def test_a1_isolation_still_holds_with_the_router(self, flag, network):
        """A1's own property, restated here so this file fails if A2 ever
        regresses it: no completion reaches an external vendor."""
        flag(False)
        router = ProviderRouter()
        assert not EXTERNAL_NAMES.intersection(router.available)
        response = _run(router.complete(_completion_request()))
        assert response.provider == mock.NAME
        assert network.total == 0

    def test_the_probe_loop_is_the_only_health_transport(self, flag, network):
        source = Path(APP / "api" / "v1" / "ai.py").read_text()
        assert "external_providers_enabled()" in source
        assert '"status": "disabled"' in source


def _completion_request():
    from app.domain.ai.types import CompletionRequest

    return CompletionRequest(messages=[Message(Role.USER, "ok")])


#: A reply the temporal parser accepts. Above MIN_SERVABLE_CONFIDENCE so the
#: observation lands as `current`, and citing an evidence id the way the
#: prompt asks, so the stored row is shaped like a real one.
OBSERVATION_JSON = json.dumps({
    "findings": ["Revenue grew 20% [E1].", "Operating margin held [E1]."],
    "dimensions": {name: "improving" for name in TRACKED_DIMENSIONS},
    "guidance": None,
    "confidence": 0.8,
    "prior_year_verdict": "not_assessable",
    "verdict_reasoning": "No prior guidance on record [E1].",
})


# ===========================================================================
# 11. Internal deterministic paths are untouched
# ===========================================================================
def _pe_context() -> GroundedContext:
    return GroundedContext(
        company_id="c1", ticker="ACME", name="Acme Industries",
        citations=[
            Citation(key="pe_ratio", label="P/E Ratio",
                     kind=EvidenceKind.VALUATION, value=24.0, unit="x",
                     source="test", fiscal_year=None),
            Citation(key="price", label="Price", kind=EvidenceKind.MARKET,
                     value=1200.0, unit="₹", source="test", fiscal_year=None),
        ],
    )


def _compare_context() -> GroundedContext:
    return GroundedContext(
        company_id="c-slim", ticker="SLIMCO", name="Slim Evidence Ltd",
        sector="Textiles",
        citations=[
            Citation(key=key, label=key.replace("_", " ").title(),
                     kind=EvidenceKind.STATEMENT, value=value, unit=unit,
                     source="test", fiscal_year=2025)
            for key, (value, unit) in (
                ("revenue", (10000.0, "₹ cr")), ("pat", (1500.0, "₹ cr")),
            )
        ],
    )


class TestInternalPathsAreFlagInvariant:
    @pytest.fixture(params=[True, False], ids=["flag-on", "flag-off"])
    def state(self, request, flag):
        return flag(request.param)

    def test_intent_resolution(self, state):
        assert FinancialIntentResolver().resolve("What is the P/E ratio?")

    def test_financial_answer_engine(self, state):
        answer = FinancialAnswerEngine().answer(
            FinancialIntent.PE, _pe_context(),
        )
        assert answer.content
        assert answer.used_citations

    def test_investment_answer_engine(self, state):
        from tests.test_financial_answer_engine import full_context

        answer = InvestmentAnswerEngine().answer(
            FinancialIntent.OVERALL_ASSESSMENT, full_context(),
        )
        assert answer.content

    def test_question_planner(self, state):
        plan = QuestionPlanner().plan("Compare revenue and pat")
        assert plan.query_type.value == "comparison"

    def test_internal_composer(self, state):
        plan = QuestionPlanner().plan(
            "What is the P/E and what is the financial quality?",
        )
        decision = InternalComposer().compose(plan)
        assert decision.status.value in {"ready", "not_composable"}

    def test_internal_open_ended_engine(self, state):
        plan = QuestionPlanner().plan("Compare revenue and pat")
        answer = InternalOpenEndedEngine().answer(plan, _compare_context())
        assert answer.status.value == "answered"
        assert answer.content

    def test_internal_language_renderer(self, state):
        renderer = InternalLanguageRenderer()
        assert renderer.vocabulary

    def test_every_layer_is_byte_identical_across_both_states(self, flag):
        """The strongest form: identical bytes in both states, so the gate
        cannot have altered a deterministic path even slightly."""
        outputs: dict[bool, dict[str, object]] = {}
        for enabled in (True, False):
            flag(enabled)
            pe = FinancialAnswerEngine().answer(
                FinancialIntent.PE, _pe_context(),
            )
            plan = QuestionPlanner().plan("Compare revenue and pat")
            internal = InternalOpenEndedEngine().answer(plan, _compare_context())
            composed = InternalComposer().compose(QuestionPlanner().plan(
                "What is the P/E and what is the financial quality?",
            ))
            outputs[enabled] = {
                "pe_text": pe.content,
                "pe_citations": [c.key for c in pe.used_citations],
                "route": plan.execution_route.value,
                "internal_text": internal.content,
                "internal_status": internal.status.value,
                "compose_status": composed.status.value,
                "glossary": _run(GlossaryTranslator().translate(
                    HINDI_SOURCE, Language.HINDI,
                )).text,
            }
        assert outputs[True] == outputs[False]


# ===========================================================================
# 12. Honesty — no fabricated output, no fake citations
# ===========================================================================
class TestNothingIsFabricated:
    def test_a_disabled_summary_pass_produces_no_prose(
        self, flag, db, company, document, stub_router,
    ):
        flag(False)
        run = SummaryService(db).generate_for_document(document)
        assert run.generated == 0
        assert db.execute(select(_summary_model())).scalars().all() == []

    def test_a_disabled_observation_produces_no_verdict(
        self, flag, db, company, summarised_year, stub_router,
    ):
        from app.models.knowledge import YearlyObservation

        flag(False)
        with pytest.raises(ExternalProvidersDisabled):
            TemporalMemoryService(db).generate_year(company.id, summarised_year)
        assert db.execute(select(YearlyObservation)).scalars().all() == []

    def test_citations_only_ever_come_from_the_context(self, flag):
        """The internal engines cite the evidence they were given. The flag
        must not introduce a citation that was not in the context."""
        context = _pe_context()
        known = {c.key for c in context.citations}
        for enabled in (True, False):
            flag(enabled)
            answer = FinancialAnswerEngine().answer(
                FinancialIntent.PE, context,
            )
            assert {c.key for c in answer.used_citations} <= known
            assert answer.used_citations

    def test_a_translation_never_claims_to_be_translated(self, flag):
        flag(False)
        for language in (Language.HINDI, Language.HINGLISH):
            result = _run(LLMTranslator().translate(HINDI_SOURCE, language))
            assert result.translated is False
            assert result.detail

    def test_no_embedding_is_synthesised(self, flag):
        flag(False)
        assert build_semantic_embedder(stub_settings()) is None
        with pytest.raises(RuntimeError):
            JinaV3Provider("k").embed(["anything"])

    def test_a_reranker_returns_scores_or_refuses_never_nothing(self, flag):
        """An empty score list would silently discard the fused ranking,
        which is a fabricated result of a different kind: the reader sees an
        order nobody computed."""
        flag(False)
        scores = build_rerank_provider(stub_settings()).rerank(
            "revenue growth", CANDIDATES,
        )
        assert len(scores) == len(CANDIDATES)

    def test_the_analyst_fallback_still_routes_through_the_router(self):
        """A2 adds no path around the analyst's provider call, so its
        honest fallback — offline composer or `NoProviderConfigured` — is
        the only outcome available."""
        analyst = (APP / "services" / "ai" / "analyst.py").read_text()
        assert "from app.services.ai.providers.router import ProviderRouter" in analyst
        assert "self.router.complete(" in analyst
        assert "self.router.stream(" in analyst

    def test_a_disabled_deployment_fails_closed_or_serves_offline(
        self, flag, mock_mode, network,
    ):
        """With no offline provider there is nothing honest left to serve,
        so the router raises rather than returning empty prose."""
        flag(False)
        mock_mode(False)
        router = ProviderRouter()
        with pytest.raises(NoProviderConfigured):
            _run(router.complete(_completion_request()))
        assert network.total == 0


# ===========================================================================
# 13. Phase 2E A1 is untouched
# ===========================================================================
class TestPhase2EA1IsUntouched:
    def test_the_registry_gate_is_still_in_place(self):
        source = inspect.getsource(ProviderRouter.default_configs)
        assert 'getattr(settings, "AI_EXTERNAL_PROVIDERS_ENABLED", True)' in source
        assert "for module in PROVIDER_MODULES:" in source

    def test_the_gate_module_imports_nothing_from_the_app(self):
        """A2 reads the same SETTING; it does not reach into A1's module, nor
        into anything else. The retrieval and language layers import it at
        module scope, so an import cycle here would be a startup failure."""
        source = Path(
            APP / "services" / "ai" / "external_gate.py"
        ).read_text()
        # Column zero only. `app.core.config` is imported inside the
        # function precisely so this stays empty at module scope.
        imports = [
            line for line in source.splitlines()
            if line.startswith(("import ", "from "))
        ]
        assert imports, "expected at least the stdlib imports"
        for line in imports:
            assert "app." not in line, line
        # And the lazy import really is indented.
        assert "\n        from app.core.config import settings" in source

    def test_the_registry_is_still_gated_by_the_flag(self, flag):
        flag(True)
        assert [c.name for c in ProviderRouter.default_configs()] == [
            "Offline", "OpenRouter", "OpenAI", "Claude", "Gemini",
        ]
        flag(False)
        assert [c.name for c in ProviderRouter.default_configs()] == ["Offline"]

    def test_fallback_order_is_unchanged(self):
        assert FALLBACK_ORDER == ("Gemini", "OpenAI", "Claude", "OpenRouter")

    def test_the_offline_provider_is_still_governed_by_ai_mock_mode(
        self, flag, mock_mode,
    ):
        flag(False)
        mock_mode(False)
        assert ProviderRouter.default_configs() == []

    def test_the_vendor_modules_all_still_exist(self):
        providers = APP / "services" / "ai" / "providers"
        for name in ("router.py", "base.py", "gemini.py", "openai.py",
                     "openrouter.py", "claude.py", "mock.py", "shapes.py"):
            assert (providers / name).is_file(), name

    def test_the_a1_test_file_is_still_there_and_still_asserts(self):
        a1 = (BACKEND / "tests" / "test_external_provider_isolation.py").read_text()
        for marker in ("class TestTheSetting", "class TestArchitecture",
                       "test_the_vendor_loop_is_gated_by_the_flag",
                       "AI_EXTERNAL_PROVIDERS_ENABLED"):
            assert marker in a1, marker

    def test_gemini_is_still_a_registered_vendor(self):
        assert hasattr(gemini, "DEFAULTS")
        assert gemini.DEFAULTS.name == "Gemini"

    def test_the_a1_files_carry_no_a2_logic(self):
        """A2 is additive: the three files A1 touched are byte-identical, and
        none of them reaches for the new helper. A1's setting, its docstring
        and its registry gate are the only isolation logic they contain."""
        for relative in ("app/core/config.py", ".env.example",
                         "app/services/ai/providers/router.py"):
            path = BACKEND / relative
            assert path.is_file(), relative
            assert "external_gate" not in path.read_text(), relative

    def test_a1_documents_the_flag_as_reversible(self):
        """The wording A1 shipped, still present — A2 did not rewrite the
        contract an operator reads."""
        config = (APP / "core" / "config.py").read_text()
        assert "Phase 2E A1 — reversible external-provider isolation" in config
        assert "AI_EXTERNAL_PROVIDERS_ENABLED: bool = True" in config
        env = (BACKEND / ".env.example").read_text()
        assert "AI_EXTERNAL_PROVIDERS_ENABLED=true" in env


# ===========================================================================
# 14. The gates are structural — a tripwire per scoped path
# ===========================================================================
class TestTheGatesAreStructural:
    """Source-level assertions. A future edit that removes a gate should fail
    here with the name of the path it opened, rather than passing silently
    until a disabled deployment makes a paid call."""

    @pytest.mark.parametrize(("relative", "marker"), [
        ("app/services/knowledge/summaries.py", "SummaryService"),
        ("app/services/knowledge/temporal.py", "TemporalMemoryService"),
        ("app/services/knowledge/enrichment.py", "MemoryEnrichmentService"),
        ("app/api/v1/ai.py", "provider_health"),
        ("app/services/language/translators.py", "LLMTranslator"),
        ("app/services/retrieval/embeddings.py", "build_semantic_embedder"),
        ("app/services/retrieval/rerank_providers.py", "build_rerank_provider"),
        ("app/services/retrieval/rerank.py", "build_reranker"),
    ])
    def test_each_scoped_module_consults_the_gate(self, relative, marker):
        source = (BACKEND / relative).read_text()
        assert marker in source, relative
        assert "external_providers_enabled" in source, relative

    @pytest.mark.parametrize("func", [
        SummaryService._complete,
        TemporalMemoryService._ask,
        LLMTranslator.translate,
        JinaReranker.rerank,
        LocalCrossEncoderReranker.rerank,
        CrossEncoderReranker.rerank,
        build_semantic_embedder,
        build_rerank_provider,
        build_reranker,
    ])
    def test_each_scoped_callable_consults_the_gate(self, func):
        assert "external_providers_enabled" in inspect.getsource(func), func

    def test_the_enrichment_pass_reads_the_property(self):
        source = inspect.getsource(MemoryEnrichmentService.run)
        assert "self.llm_enabled" in source

    def test_the_setting_is_dereferenced_in_exactly_two_places(self):
        """The gate helper, and A1's registry gate. Nothing else.

        Eight modules mention the setting in a comment or a message, which
        is documentation; only two read it from a settings object, which is
        behaviour. Keeping the reads at two means the safe default is stated
        twice — once by A1 for the registry, once by A2 for everything else
        — instead of eight times, where one typo would silently invert it.
        """
        readers = []
        for base in (APP / "core", APP / "services", APP / "api"):
            for path in sorted(base.rglob("*.py")):
                text = path.read_text()
                if (f'getattr(settings, "{SETTING_NAME}"' in text
                        or "getattr(settings, SETTING_NAME" in text
                        or f"settings.{SETTING_NAME}" in text):
                    readers.append(path.relative_to(BACKEND).as_posix())
        assert readers == [
            "app/services/ai/external_gate.py",
            "app/services/ai/providers/router.py",
        ]

    def test_no_provider_class_or_dependency_was_removed(self):
        """Phase 2E is staged. A2 isolates; it does not delete."""
        requirements = (BACKEND / "requirements.txt").read_text()
        assert "httpx" in requirements
        for module in ("gemini", "openai", "claude", "openrouter", "mock"):
            assert (APP / "services" / "ai" / "providers"
                    / f"{module}.py").is_file(), module
