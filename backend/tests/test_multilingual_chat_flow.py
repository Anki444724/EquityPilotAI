"""The language flow, end to end: API → analyst → outbound adaptation.

These tests exist because production hit four related failures at once:

* every non-English request 500'd with an AttributeError — the analyst
  called a Phase 2 method (`response_instruction_phase2`) the adapter never
  defined;
* the Hindi/Hinglish answer therefore could never be rendered, because the
  outbound adaptation in `_finalise` was unreachable;
* the bge-m3 embedding provider 401'd (an account problem on OpenRouter),
  and the logs proved the failure but never the recovery;
* retrieval had to keep working on the lexical signal while the embedding
  provider was down.

The service-level tests use fakes (no database, no network); the API tests
run the real endpoints against an isolated, authenticated in-memory database
(NATIVE_AUTH login, per the test_admin_ai.py pattern) with a deterministic
fake translator installed, so "the response is in Hinglish" is asserted on
the actual HTTP payload.
"""
from __future__ import annotations

import asyncio
import io
import pathlib
import re
import time
import urllib.error

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.db.base import Base, get_db
from app.domain.ai.types import (
    Citation, CompletionResponse, EvidenceKind, TokenUsage,
)
from app.domain.language.types import CANONICAL_LANGUAGE, Language, spec_for
from app.domain.platform.identity import Role
from app.domain.platform.plans import PlanTier
from app.main import app
from app.services.ai.analyst import ResearchAnalyst
from app.services.ai.context_builder import GroundedContext
from app.services.ai.guardrails import DISCLOSURE
from app.services.language.adapter import LanguageAdapter
from app.services.language.prompt_templates import get_multilingual_prompt
from app.services.language.translators import TranslationResult
from app.services.platform.entitlements import EntitlementService
from app.services.platform.identity_service import IdentityService
from app.services.platform.tenancy import TenantService
from app.services.retrieval.engine import HybridRetrievalEngine

# Aliased on purpose: a bare `import app.models` would rebind the name `app`
# to the package and shadow the FastAPI instance imported above (the same
# trick test_admin_ai.py relies on).
import app.models as _models  # noqa: F401  (create_all must see every table)

REF = "BHARATCP"


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _FakeRouter:
    """A provider that records the request and returns canned content."""

    def __init__(self, content: str) -> None:
        self.content = content
        self.requests: list = []

    def chain(self, preferred=None):
        return []

    @property
    def available(self):
        return ["fake"]

    def build(self, config):
        raise RuntimeError("fake router serves directly")

    async def complete(self, request, preferred=None, use_cache=True):
        self.requests.append(request)
        return CompletionResponse(
            content=self.content, provider="fake", model="fake-1",
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5),
            latency_ms=1.0, cost_usd=0.0,
        )


class _FakeBuilder:
    """A context builder with one financial citation and no documents."""

    def __init__(self) -> None:
        class _Company:
            id = "c1"
            ticker = "RELIANCE"
            name = "Reliance Industries"

        self.analysis = type("_Analysis", (), {"company": _Company})()
        self.document_service = None  # no corpus: the no-evidence path

    def build(self) -> GroundedContext:
        return GroundedContext(
            company_id="c1", ticker="RELIANCE", name="Reliance Industries",
            citations=[
                Citation(
                    key="debt", label="Total Debt FY2025",
                    kind=EvidenceKind.STATEMENT, value=490000.0, unit="cr",
                    source="Financial Facts",
                ),
            ],
        )


class _FakeTranslator:
    """A deterministic translator: changes prose, preserves identifiers.

    Stands in for the live LLM translator on a deployment that has one. The
    marker makes "translation actually ran" visible in assertions.
    """

    name = "fake"

    def supports(self, language: Language) -> bool:
        return True

    async def translate(self, text, language, *, entities=None) -> TranslationResult:
        if language is CANONICAL_LANGUAGE:
            return TranslationResult(text=text, language=language,
                                     translated=True, provider=self.name)
        marker = "(हिन्दी) " if language is Language.HINDI else "(Hinglish) "
        return TranslationResult(
            text=f"{marker}{text}", language=language, translated=True,
            provider=self.name,
        )


class _DeadEmbedder:
    """An embedding provider that fails exactly like the 401'd bge-m3 does."""

    def embed_one(self, text: str):
        raise RuntimeError(
            "bge-m3 embeddings failed after 3 attempts: HTTP Error 401"
        )

    def embed(self, texts):
        raise RuntimeError("bge-m3 embeddings failed after 3 attempts")


class _FakeDB:
    """Executes the engine's SQL and returns canned rows per signal."""

    def __init__(self, *, lexical_rows=(), hydrate_rows=()) -> None:
        self.lexical_rows = list(lexical_rows)
        self.hydrate_rows = list(hydrate_rows)
        self.sql_calls: list[str] = []

    def execute(self, sql, params=None):
        query = sql.text if hasattr(sql, "text") else str(sql)
        self.sql_calls.append(query)
        if "ts_rank_cd" in query:
            return [tuple(row) for row in self.lexical_rows]
        if "c.id = ANY" in query:
            return [tuple(row) for row in self.hydrate_rows]
        return []


# ---------------------------------------------------------------------------
# Phase 2 instruction builder
# ---------------------------------------------------------------------------

class TestResponseInstructionPhase2:
    """The method the analyst calls. Its absence was the 500."""

    def test_english_is_empty(self):
        assert LanguageAdapter.response_instruction_phase2(
            Language.ENGLISH, "chat") == ""

    def test_planned_language_is_empty(self):
        """A language with no translation module must not be asked for."""
        assert LanguageAdapter.response_instruction_phase2(
            Language.MARATHI, "chat") == ""

    @pytest.mark.parametrize("language", [Language.HINDI, Language.HINGLISH])
    def test_names_the_language_and_keeps_the_identifier_rule(self, language):
        instruction = LanguageAdapter.response_instruction_phase2(
            language, "chat")
        assert f"RESPONSE LANGUAGE: {spec_for(language).label}" in instruction
        assert "citation" in instruction.lower()
        assert "company name" in instruction.lower()

    def test_chat_capability_gets_chat_guidance(self):
        instruction = LanguageAdapter.response_instruction_phase2(
            Language.HINGLISH, "chat")
        assert "chat conversation" in instruction

    def test_capability_is_optional(self):
        without = LanguageAdapter.response_instruction_phase2(Language.HINDI)
        with_none = LanguageAdapter.response_instruction_phase2(
            Language.HINDI, None)
        assert without == with_none
        assert "RESPONSE LANGUAGE: Hindi" in without

    def test_unknown_capability_keeps_the_instruction(self):
        base = LanguageAdapter.response_instruction(Language.HINDI)
        got = LanguageAdapter.response_instruction_phase2(
            Language.HINDI, "not_a_real_capability")
        assert got.startswith(base)

    def test_get_multilingual_prompt_is_identity_for_english(self):
        base = "the base instruction"
        assert get_multilingual_prompt(Language.ENGLISH, "chat", base) == base
        assert get_multilingual_prompt(Language.MARATHI, "chat", base) == base
        assert get_multilingual_prompt(Language.HINDI, "chat", "") == ""


class TestAnalystInstructionRegression:
    """The analyst must never reference an attribute the adapter lacks."""

    def test_every_referenced_adapter_attribute_exists(self):
        import app.services.ai.analyst as analyst_module

        source = pathlib.Path(analyst_module.__file__).read_text()
        referenced = set(re.findall(
            r"LanguageAdapter\.([A-Za-z_][A-Za-z0-9_]*)", source))
        assert referenced, "the analyst no longer uses the adapter at all?"
        missing = [name for name in referenced
                   if not hasattr(LanguageAdapter, name)]
        assert missing == [], (
            f"analyst references missing LanguageAdapter attributes: {missing}"
        )

    def test_a_failing_instruction_builder_cannot_500_a_request(self,
                                                                monkeypatch):
        """The instruction is prompt decoration: a failure degrades, never
        kills the request. This is the exact failure shape that shipped."""
        def boom(language, capability=None):
            raise RuntimeError("template store on fire")

        monkeypatch.setattr(LanguageAdapter, "response_instruction_phase2", boom)

        router = _FakeRouter("Total debt is 4,90,000 cr [debt].")
        analyst = ResearchAnalyst(_FakeBuilder(), router=router)
        result = _run(analyst.run(
            "chat",
            question="Reliance ka total debt kitna hai?",
            language=Language.HINGLISH,
        ))
        # The request completed (enforce() appends the disclosure footer),
        # the English answer leads, and the language block is present.
        assert result.content.startswith("Total debt is 4,90,000 cr [debt].")
        assert DISCLOSURE in result.content
        assert result.language is not None


# ---------------------------------------------------------------------------
# Service-level outbound adaptation
# ---------------------------------------------------------------------------

class TestAnalystOutboundAdaptation:
    """API → run() → _finalise() → adapt(), on the analyst itself."""

    QUESTION = "Reliance ka total debt kitna hai?"
    SENTENCE = "Total debt is 4,90,000 cr [debt]."

    def _analyst(self, monkeypatch) -> ResearchAnalyst:
        # Install the deterministic translator in the adapter's namespace.
        monkeypatch.setattr(
            "app.services.language.adapter.build_translator",
            lambda *args, **kwargs: _FakeTranslator(),
        )
        router = _FakeRouter(self.SENTENCE)
        return ResearchAnalyst(_FakeBuilder(), router=router), router

    def test_hinglish_question_renders_hinglish(self, monkeypatch):
        analyst, router = self._analyst(monkeypatch)
        result = _run(analyst.run(
            "chat", question=self.QUESTION, language=Language.HINGLISH,
        ))

        # Outbound: the display copy is the rendered text…
        assert result.display_content.startswith("(Hinglish)")
        assert "[Total Debt FY2025]" in result.display_content
        # …while the canonical copy stays the audited English (plus the
        # disclosure footer every answer carries).
        assert result.content.startswith(self.SENTENCE)
        assert "[debt]" in result.content
        assert "(Hinglish)" not in result.content

        # The language block reports what happened.
        assert result.language["language"] == "hinglish"
        assert result.language["translation"]["translated"] is True
        assert result.language["translation"]["provider"] == "fake"

        # Inbound: the writing prompt carried the Phase 2 instruction with
        # chat guidance.
        prompt_text = "\n".join(m.content for m in router.requests[0].messages)
        assert "RESPONSE LANGUAGE: Hinglish" in prompt_text
        assert "chat conversation" in prompt_text

    def test_hindi_question_renders_hindi(self, monkeypatch):
        analyst, _ = self._analyst(monkeypatch)
        result = _run(analyst.run(
            "chat", question=self.QUESTION, language=Language.HINDI,
        ))
        assert result.display_content.startswith("(हिन्दी)")
        assert result.content.startswith(self.SENTENCE)
        assert result.language["language"] == "hindi"

    def test_english_request_bypasses_the_adapter(self, monkeypatch):
        analyst, router = self._analyst(monkeypatch)
        result = _run(analyst.run(
            "chat", question="How much is the total debt?",
        ))
        # No language block, no instruction, no translation — the canonical
        # path is unchanged: display is the annotated English only.
        assert result.language is None
        assert result.display_content.startswith(
            "Total debt is 4,90,000 cr [Total Debt FY2025].")
        assert not result.display_content.startswith(("(Hinglish)", "(हिन्दी)"))
        prompt_text = "\n".join(m.content for m in router.requests[0].messages)
        assert "RESPONSE LANGUAGE" not in prompt_text

    def test_devanagari_question_normalised_for_retrieval(self, monkeypatch):
        """Inbound: the retriever sees English terms, not Devanagari."""
        analyst, _ = self._analyst(monkeypatch)
        _run(analyst.run(
            "chat", question="Reliance का कुल कर्ज कितना है?",
            language=Language.HINDI,
        ))
        # The normalisation contract: a Devanagari debt question must reach
        # the retriever with the English word the corpus actually holds.
        normalised = LanguageAdapter().normalise_query(
            "Reliance का कुल कर्ज कितना है?")
        assert "debt" in normalised.english.lower()
        assert normalised.was_rewritten


# ---------------------------------------------------------------------------
# BGE-M3 401 handling
# ---------------------------------------------------------------------------

class TestEmbeddingProvider401:
    """A terminal auth rejection: breaker trips, diagnosis names the fix."""

    @staticmethod
    def _http_error(code: int, reason: str) -> urllib.error.HTTPError:
        return urllib.error.HTTPError(
            "https://openrouter.ai/api/v1/embeddings", code, reason, {},
            io.BytesIO(b'{"error": "unauthorized"}'),
        )

    def test_401_trips_the_circuit_and_never_retries(self, monkeypatch):
        from app.services.retrieval.embeddings import BGEM3Provider

        provider = BGEM3Provider("sk-invalid-key", timeout=5)
        calls = {"n": 0}

        def fake_urlopen(request, timeout=None):
            calls["n"] += 1
            raise self._http_error(401, "Invalid API key")

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

        with pytest.raises(RuntimeError, match="HTTP Error 401"):
            provider.embed(["hello"])

        # Standing state: the breaker is open and the network is not hit
        # again during the cooldown.
        assert provider._tripped_until > time.monotonic()  # noqa: SLF001
        with pytest.raises(RuntimeError, match="circuit open"):
            provider.embed(["hello again"])
        assert calls["n"] == 1

    def test_diagnosis_names_the_setting_not_the_key(self):
        from app.services.retrieval.embeddings import (
            BGEM3Provider, JinaV3Provider, OpenAISmallProvider,
        )

        provider = BGEM3Provider("sk-super-secret")
        diagnosis = provider._terminal_diagnosis(401)  # noqa: SLF001
        assert "OPENROUTER_API_KEY" in diagnosis
        assert "baai/bge-m3" in diagnosis
        assert "EMBEDDING_PROVIDER" in diagnosis
        assert "sk-super-secret" not in diagnosis  # never the key itself

        jina = JinaV3Provider("j-super-secret")
        assert "JINA_API_KEY" in jina._terminal_diagnosis(401)  # noqa: SLF001
        assert "j-super-secret" not in jina._terminal_diagnosis(401)  # noqa: SLF001

        openai = OpenAISmallProvider("o-super-secret")
        assert "OPENAI_API_KEY" in openai._terminal_diagnosis(403)  # noqa: SLF001

    def test_402_diagnosis_mentions_credit(self):
        from app.services.retrieval.embeddings import BGEM3Provider

        diagnosis = BGEM3Provider("k")._terminal_diagnosis(402)  # noqa: SLF001
        assert "credit" in diagnosis
        assert "EMBEDDING_PROVIDER" in diagnosis
        assert "OPENROUTER_API_KEY" in diagnosis

    def test_402_trips_the_circuit_like_401(self, monkeypatch):
        """The production symptom: bge-m3 via OpenRouter returns HTTP 402
        (no credits). A standing billing state, not a blip — one attempt,
        then the breaker opens and retrieval degrades to lexical."""
        from app.services.retrieval.embeddings import BGEM3Provider

        provider = BGEM3Provider("sk-or-v1-no-credits", timeout=5)
        calls = {"n": 0}

        def fake_urlopen(request, timeout=None):
            calls["n"] += 1
            raise self._http_error(402, "Payment Required")

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

        with pytest.raises(RuntimeError, match="HTTP Error 402"):
            provider.embed(["hello"])

        assert provider._tripped_until > time.monotonic()  # noqa: SLF001
        with pytest.raises(RuntimeError, match="circuit open"):
            provider.embed(["hello again"])
        assert calls["n"] == 1

    def test_402_diagnosis_names_the_free_alternative(self):
        """An operator reading the log should learn the fix, not just the
        failure: jina-v3 has a free tier."""
        from app.services.retrieval.embeddings import BGEM3Provider

        diagnosis = BGEM3Provider("k")._terminal_diagnosis(402)  # noqa: SLF001
        assert "jina-v3" in diagnosis
        assert "JINA_API_KEY" in diagnosis

    def test_embedding_provider_setting_selects_the_provider(self, monkeypatch):
        """EMBEDDING_PROVIDER must actually be honoured by the engine.

        It was declared in settings with a docstring promising provider
        selection, but never passed to build_semantic_embedder — so an
        operator who set it to work around a 401'd bge-m3 got bge-m3 anyway.
        """
        from app.core.config import settings
        from app.services.retrieval.embeddings import (
            BGEM3Provider, JinaV3Provider,
        )

        monkeypatch.setattr(settings, "OPENROUTER_API_KEY", "a")
        monkeypatch.setattr(settings, "JINA_API_KEY", "b")
        monkeypatch.setattr(settings, "EMBEDDING_PROVIDER", "jina-v3")
        engine = HybridRetrievalEngine(_FakeDB())
        assert isinstance(engine.embedder, JinaV3Provider)

        monkeypatch.setattr(settings, "EMBEDDING_PROVIDER", None)
        engine = HybridRetrievalEngine(_FakeDB())
        # jina-v3 is the default; the OpenRouter-backed provider no longer
        # leads now that its account is exhausted.
        assert isinstance(engine.embedder, JinaV3Provider)

        monkeypatch.setattr(settings, "EMBEDDING_PROVIDER", "bge-m3")
        engine = HybridRetrievalEngine(_FakeDB())
        assert isinstance(engine.embedder, BGEM3Provider)

    def test_backfill_honours_the_same_setting(self, monkeypatch):
        """A re-embed on the wrong provider would store a different space."""
        from app.core.config import settings
        from app.services.retrieval.backfill import EmbeddingBackfillService
        from app.services.retrieval.embeddings import JinaV3Provider

        monkeypatch.setattr(settings, "OPENROUTER_API_KEY", "a")
        monkeypatch.setattr(settings, "JINA_API_KEY", "b")
        monkeypatch.setattr(settings, "EMBEDDING_PROVIDER", "jina-v3")
        service = EmbeddingBackfillService(_FakeDB())
        assert isinstance(service.embedder, JinaV3Provider)

    def test_production_shape_embeds_with_jina(self, monkeypatch):
        """OPENROUTER_API_KEY unset (exhausted account), JINA_API_KEY set:
        retrieval and backfill must agree on jina-v3, with or without the
        explicit EMBEDDING_PROVIDER setting."""
        from app.core.config import settings
        from app.services.retrieval.backfill import EmbeddingBackfillService
        from app.services.retrieval.embeddings import JinaV3Provider

        monkeypatch.setattr(settings, "OPENROUTER_API_KEY", None)
        monkeypatch.setattr(settings, "JINA_API_KEY", "jina-free-key")
        monkeypatch.setattr(settings, "OPENAI_API_KEY", None)

        for preferred in ("jina-v3", None):
            monkeypatch.setattr(settings, "EMBEDDING_PROVIDER", preferred)
            assert isinstance(
                HybridRetrievalEngine(_FakeDB()).embedder, JinaV3Provider,
            )
            assert isinstance(
                EmbeddingBackfillService(_FakeDB()).embedder, JinaV3Provider,
            )


# ---------------------------------------------------------------------------
# Lexical retrieval survives an embedding failure
# ---------------------------------------------------------------------------

class TestLexicalFallback:
    """The hybrid engine must serve lexical-only results, not no results."""

    LEXICAL_ROWS = [
        (7, 1.80), (8, 1.20),
    ]
    #: (id, document_id, text, page, paragraph, section, title,
    #:      doc_type, fiscal_year, filename)
    HYDRATE_ROWS = [
        (7, 101, "The company's total debt stood at 4,90,000 crore.",
         12, 2, "financial_review", "AR FY25", "annual_report", 2025, "ar.pdf"),
        (8, 101, "Debt to equity improved during the year.",
         13, 1, "financial_review", "AR FY25", "annual_report", 2025, "ar.pdf"),
    ]

    def _engine(self, db):
        return HybridRetrievalEngine(db, embedder=_DeadEmbedder())

    def test_semantic_failure_degrades_to_lexical(self):
        db = _FakeDB(lexical_rows=self.LEXICAL_ROWS,
                     hydrate_rows=self.HYDRATE_ROWS)
        results = self._engine(db).retrieve(
            "Reliance total debt how much", company_id="c1")

        assert len(results) == 2
        for result in results:
            # Lexical answered; semantic was absent, not merely empty.
            assert "semantic" not in result.signals
            assert result.signals.get("lexical") is not None
            assert result.raw.get("lexical", 0.0) > 0.0
        # The decisive lexical winner is pinned to rank 1.
        assert results[0].chunk_id == 7
        assert "4,90,000" in results[0].text

    def test_no_signal_at_all_returns_empty_not_an_error(self):
        db = _FakeDB()
        assert self._engine(db).retrieve("anything") == []

    def test_semantic_and_lexical_still_fuse_when_both_work(self):
        """The failure path must not have touched the happy path."""

        class _WorkingEmbedder:
            def embed_one(self, text):
                return [0.01] * 8

        class _AllSignalsDB(_FakeDB):
            def execute(self, sql, params=None):
                query = sql.text if hasattr(sql, "text") else str(sql)
                if "embedding_v2" in query:
                    return [(7, 0.87), (8, 0.71)]
                return super().execute(sql, params)

        db = _AllSignalsDB(lexical_rows=self.LEXICAL_ROWS,
                           hydrate_rows=self.HYDRATE_ROWS)
        engine = HybridRetrievalEngine(db, embedder=_WorkingEmbedder())
        results = engine.retrieve("total debt", company_id="c1")
        assert results
        # Chunk 7 was found by both signals and carries both raw scores.
        by_id = {r.chunk_id: r for r in results}
        assert by_id[7].signals.get("semantic") is not None
        assert by_id[7].signals.get("lexical") is not None
        assert by_id[7].raw.get("semantic") == 0.87


# ---------------------------------------------------------------------------
# API: the chat endpoint, all three languages
# ---------------------------------------------------------------------------

class TestMultilingualChatAPI:
    """The real endpoint. The fake translator stands in for the live LLM."""

    #: Satisfies the platform password policy (min length + character
    #: classes); it is test data, not a credential.
    PASSWORD = "a-strong-test-password-1"
    EMAIL = "lang@test.com"

    @pytest.fixture()
    def client(self, monkeypatch):
        """An authenticated client against an isolated in-memory database.

        With NATIVE_AUTH enabled — as in the production deployment — the
        chat/analyse endpoints require a bearer session token, so a bare
        ``TestClient(app)`` gets 401 on every call. This fixture follows the
        project pattern from ``test_admin_ai.py`` / ``test_platform_api.py``:
        isolated SQLite, entitlements synced, a tenant and a verified admin,
        the ``get_db`` dependency overridden, NATIVE_AUTH forced on, a real
        login through ``/api/v1/auth/login``, and the returned token in the
        client's Authorization header. Production authentication code is not
        touched — this is pure test setup, and both the dependency override
        and the NATIVE_AUTH value are restored on teardown.
        """
        # The deterministic fake translator stands in for the live LLM
        # (function-scoped; monkeypatch restores it after each test).
        monkeypatch.setattr(
            "app.services.language.adapter.build_translator",
            lambda *args, **kwargs: _FakeTranslator(),
        )

        # 1. isolated in-memory database; 2. the full schema.
        engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False}, poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=engine)
        session = sessionmaker(bind=engine, autoflush=False,
                               expire_on_commit=False)

        # 3. sync entitlements; 4. test tenant; 5. verified test user.
        with session() as db:
            EntitlementService(db).sync_catalogue()
            tenant = TenantService(db).create(
                "Multilingual Test Capital", tier=PlanTier.ENTERPRISE)
            IdentityService(db).register(
                email=self.EMAIL, password=self.PASSWORD, name="Lang Test",
                tenant_id=tenant.id, role=Role.ADMIN, auto_verify=True,
            )

        # 6. override get_db with the test session; 7. NATIVE_AUTH on.
        #    Both saved so teardown can restore the previous state exactly.
        def _override():
            db = session()
            try:
                yield db
            finally:
                db.close()

        prev_override = app.dependency_overrides.get(get_db)
        prev_native = settings.NATIVE_AUTH
        app.dependency_overrides[get_db] = _override
        settings.NATIVE_AUTH = True

        try:
            # 8. the client; 9. a real login; 10. bearer header for every
            #    request from here on.
            c = TestClient(app)
            login = c.post(
                "/api/v1/auth/login",
                json={"email": self.EMAIL, "password": self.PASSWORD},
            )
            assert login.status_code == 200, login.text
            c.headers.update({
                "Authorization": f"Bearer {login.json()['access_token']}",
            })

            # The chat tests address BHARATCP, and /ai/* refuses companies
            # without data — so provision it here with financial facts,
            # exactly like test_admin_ai.py does for AICO.
            created = c.post("/api/v1/admin/companies", json={
                "name": "Bharat Consumer Products Ltd", "ticker": REF,
                "isin": "INE818181819",
            })
            assert created.status_code == 201, created.text
            company_id = created.json()["id"]
            seeded = c.put(
                f"/api/v1/admin/financials/{company_id}/facts",
                json=[
                    {"fiscal_year": 2024, "line_item": "revenue",
                     "value": 12000.0},
                    {"fiscal_year": 2024, "line_item": "net_block_ppe",
                     "value": 3000.0},
                    {"fiscal_year": 2024, "line_item": "cash_and_bank",
                     "value": 1000.0},
                    {"fiscal_year": 2024, "line_item":
                     "equity_share_capital", "value": 500.0},
                    {"fiscal_year": 2024, "line_item":
                     "long_term_borrowings", "value": 2500.0},
                    {"fiscal_year": 2024, "line_item": "trade_receivables",
                     "value": 600.0},
                    {"fiscal_year": 2024, "line_item": "inventories",
                     "value": 400.0},
                    {"fiscal_year": 2024, "line_item": "trade_payables",
                     "value": 500.0},
                    {"fiscal_year": 2024, "line_item": "raw_materials",
                     "value": 4000.0},
                    {"fiscal_year": 2024, "line_item": "employee_benefit",
                     "value": 800.0},
                ],
            )
            assert seeded.status_code == 200, seeded.text

            # 11. the authenticated client.
            yield c
        finally:
            # 12. restore the previous override and NATIVE_AUTH; 13. dispose.
            settings.NATIVE_AUTH = prev_native
            if prev_override is not None:
                app.dependency_overrides[get_db] = prev_override
            else:
                app.dependency_overrides.pop(get_db, None)
            engine.dispose()

    def _chat(self, client, question, **extra):
        response = client.post(
            f"/api/v1/company/{REF}/ai/chat",
            json={"question": question, "session_id": "lang-test", **extra},
        )
        assert response.status_code == 200, response.text[:500]
        return response.json()

    def test_hinglish_chat_returns_200_and_hinglish_display(self, client):
        """The production failure: this request 500'd with AttributeError."""
        body = self._chat(
            client, "BHARATCP ka total debt kitna hai?",
            language="hinglish",
        )
        assert body["language"] is not None
        assert body["language"]["language"] == "hinglish"
        assert body["language"]["resolved_from"] == "requested"
        assert body["language"]["translation"]["translated"] is True
        # Display is rendered (a data-quality warning may be prepended), the
        # canonical copy is the audited English.
        assert "(Hinglish)" in body["display_content"]
        assert "(Hinglish)" not in body["content"]
        # Grounding: the rendered display keeps every citation label, and the
        # canonical text keeps every citation marker the audit resolved.
        for citation in body.get("citations", []):
            assert citation["label"] in body["display_content"]
            assert f"[{citation['key']}]" in body["content"]

    def test_autodetected_hinglish_without_explicit_language(self, client):
        body = self._chat(client, "BHARATCP ka total debt kitna hai?")
        assert body["language"] is not None
        assert body["language"]["language"] == "hinglish"
        assert "(Hinglish)" in body["display_content"]

    def test_devanagari_chat_renders_hindi(self, client):
        body = self._chat(client, "BHARATCP का कुल कर्ज कितना है?")
        assert body["language"]["language"] == "hindi"
        assert "(हिन्दी)" in body["display_content"]
        assert "(हिन्दी)" not in body["content"]

    def test_explicit_hindi_overrides_detection(self, client):
        body = self._chat(client, "How much is the total debt?",
                          language="hindi")
        assert body["language"]["language"] == "hindi"
        assert "(हिन्दी)" in body["display_content"]

    def test_english_chat_stays_english_and_unlabelled(self, client):
        """No regression on the canonical path: no language block at all."""
        body = self._chat(client, "How much is the total debt?")
        assert body["language"] is None
        assert "(Hinglish)" not in body["content"]
        assert "(Hinglish)" not in body["display_content"]
        assert "(हिन्दी)" not in body["display_content"]

    def test_explicit_english_disables_adaptation(self, client):
        body = self._chat(client, "BHARATCP ka total debt kitna hai?",
                          language="english")
        assert body["language"] is None
        assert "(Hinglish)" not in body["display_content"]

    def test_analyse_endpoint_also_serves_hinglish(self, client):
        """The same crash shape hit /ai/analyse; both routes share run()."""
        response = client.post(
            f"/api/v1/company/{REF}/ai/analyse",
            json={"capability": "business_summary",
                  "question": "BHARATCP ka business kaisa hai?",
                  "language": "hinglish"},
        )
        assert response.status_code == 200, response.text[:500]
        body = response.json()
        assert body["language"]["language"] == "hinglish"
        assert "(Hinglish)" in body["display_content"]
        assert "(Hinglish)" not in body["content"]

    def test_chat_with_no_document_evidence_is_not_a_500(self, client):
        """Retrieval unavailable (no corpus for the company) must degrade to
        the grounded facts answer, not an unhandled error."""
        response = client.post(
            f"/api/v1/company/{REF}/ai/chat",
            json={"question": "BHARATCP ka total debt kitna hai?",
                  "session_id": "no-evidence", "language": "hinglish"},
        )
        assert response.status_code == 200, response.text[:500]
        body = response.json()
        assert body["content"]
        assert body["language"]["language"] == "hinglish"
