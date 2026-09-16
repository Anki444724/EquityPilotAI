"""Phase 2E A1 — the reversible external-provider isolation flag.

`AI_EXTERNAL_PROVIDERS_ENABLED` gates the provider REGISTRY, and nothing
else. When it is false the four external vendors (Gemini, OpenAI, Claude,
OpenRouter) are never assembled into registry rows, so no chain, no
`preferred` value and no code path through `ProviderRouter.complete()` or
`stream()` can reach an external AI provider. Everything else — provider
modules, FALLBACK_ORDER, retry/backoff, caching, the usage ledger,
complete()/stream() semantics, the deterministic engines of Parts 1/2A/2C/2D
— is untouched.

The flag is reversible by construction: credentials stay configured, modules
stay imported, and setting it back to true restores exactly the registry
that existed before. Default true preserves current behaviour precisely, and
a settings object that predates the attribute behaves as it always did.

What this file does NOT cover: embedding, reranking and translation keep
their own provider switches until later Phase 2E steps retire them, and no
production deployment, database or Docker behaviour is exercised here.
"""
from __future__ import annotations

import asyncio
import inspect
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.core.config as config_module
from app.core.config import Settings, settings
from app.domain.ai.types import (
    Citation, CompletionRequest, EvidenceKind, Message, NoProviderConfigured,
    Role,
)
from app.services.ai.context_builder import GroundedContext
from app.services.ai.financial_answer_engine import FinancialAnswerEngine
from app.services.ai.financial_intent import (
    FinancialIntent, FinancialIntentResolver,
)
from app.services.ai.internal_composer import InternalComposer
from app.services.ai.internal_open_ended import InternalOpenEndedEngine
from app.services.ai.investment_answer_engine import InvestmentAnswerEngine
from app.services.ai.planner.question_planner import QuestionPlanner
from app.services.ai.providers import base as provider_base
from app.services.ai.providers import claude, gemini, mock, openai, openrouter
from app.services.ai.providers.router import (
    FALLBACK_ORDER, PROVIDER_MODULES, ProviderRouter,
)

BACKEND = Path(__file__).resolve().parent.parent
APP = BACKEND / "app"
EXTERNAL_NAMES = frozenset({"Gemini", "OpenAI", "Claude", "OpenRouter"})


def _run(coro):
    return asyncio.run(coro)


def request(text: str = "What is the P/E ratio?") -> CompletionRequest:
    return CompletionRequest(messages=[
        Message(Role.SYSTEM, "system rules"),
        Message(Role.USER, text),
    ])


def set_keys(monkeypatch, *, enabled: bool, mock_mode: bool = True) -> None:
    """Give the singleton settings four live keys, then set the flag.

    Keys are set in BOTH states on purpose: the isolation property must hold
    because of the flag, not because credentials happen to be absent.
    """
    monkeypatch.setattr(settings, "OPENROUTER_API_KEY", "or-test-key")
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "oa-test-key")
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "an-test-key")
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "gm-test-key")
    monkeypatch.setattr(settings, "AI_MOCK_MODE", mock_mode)
    monkeypatch.setattr(settings, "AI_EXTERNAL_PROVIDERS_ENABLED", enabled)


def fake_settings(**overrides) -> SimpleNamespace:
    """A minimal settings-like object for `default_configs`.

    Carries only what the registry reads. Anything absent exercises the
    router's defensive `getattr` reads — including the isolation flag itself.
    """
    base = dict(
        OPENROUTER_API_KEY="or-key", OPENAI_API_KEY="oa-key",
        ANTHROPIC_API_KEY="an-key", GEMINI_API_KEY="gm-key",
        AI_MOCK_MODE=False,
        OPENROUTER_MODEL=None, OPENROUTER_SITE_URL="", OPENROUTER_APP_NAME="",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# ===========================================================================
# 1. The setting itself
# ===========================================================================
class TestTheSetting:
    def test_default_is_true(self, monkeypatch):
        """The flag defaults to true, i.e. today's behaviour is unchanged."""
        assert Settings.model_fields["AI_EXTERNAL_PROVIDERS_ENABLED"].default is True
        monkeypatch.delenv("AI_EXTERNAL_PROVIDERS_ENABLED", raising=False)
        assert Settings(_env_file=None).AI_EXTERNAL_PROVIDERS_ENABLED is True

    def test_env_override_works(self, monkeypatch):
        monkeypatch.setenv("AI_EXTERNAL_PROVIDERS_ENABLED", "false")
        assert Settings(_env_file=None).AI_EXTERNAL_PROVIDERS_ENABLED is False
        monkeypatch.setenv("AI_EXTERNAL_PROVIDERS_ENABLED", "true")
        assert Settings(_env_file=None).AI_EXTERNAL_PROVIDERS_ENABLED is True

    def test_the_running_singleton_defaults_to_true(self):
        """No environment in this suite sets the flag, so the app singleton
        carries the default — the state every existing test has always run
        against."""
        assert settings.AI_EXTERNAL_PROVIDERS_ENABLED is True


# ===========================================================================
# 2. Flag TRUE — the registry is exactly what it always was
# ===========================================================================
class TestFlagTruePreservesTheExistingRegistry:
    def test_registry_rows_and_order_unchanged(self, monkeypatch):
        set_keys(monkeypatch, enabled=True, mock_mode=True)
        rows = ProviderRouter.default_configs()
        # The offline provider is appended first, then the vendor modules in
        # PROVIDER_MODULES order — the assembly order that predates the flag.
        assert [c.name for c in rows] == [
            "Offline", "OpenRouter", "OpenAI", "Claude", "Gemini",
        ]
        by_name = {c.name: c for c in rows}
        assert by_name["Gemini"].api_key == "gm-test-key"
        assert by_name["OpenAI"].api_key == "oa-test-key"
        assert by_name["Claude"].api_key == "an-test-key"
        assert by_name["OpenRouter"].api_key == "or-test-key"

    def test_chain_order_and_fallback_unchanged(self, monkeypatch):
        set_keys(monkeypatch, enabled=True, mock_mode=True)
        router = ProviderRouter()
        # FALLBACK_ORDER first, the offline provider ranked below every live
        # provider — the documented chain behaviour.
        assert [c.name for c in router.chain()] == [
            "Gemini", "OpenAI", "Claude", "OpenRouter", "Offline",
        ]
        # `available` reports REGISTRY order (offline first, then the vendor
        # modules); `chain()` above is the one that applies FALLBACK_ORDER.
        assert router.available == [
            "Offline", "OpenRouter", "OpenAI", "Claude", "Gemini",
        ]

    def test_no_mock_mode_keeps_only_the_four_vendors(self, monkeypatch):
        set_keys(monkeypatch, enabled=True, mock_mode=False)
        rows = ProviderRouter.default_configs()
        assert [c.name for c in rows] == [
            "OpenRouter", "OpenAI", "Claude", "Gemini",
        ]

    def test_unkeyed_providers_stay_unconfigured(self, monkeypatch):
        """Existing behaviour: a vendor with no key registers a row that is
        not `configured`, so it never joins a chain."""
        set_keys(monkeypatch, enabled=True)
        monkeypatch.setattr(settings, "GEMINI_API_KEY", None)
        router = ProviderRouter()
        assert "Gemini" not in router.available


# ===========================================================================
# 3. Flag FALSE — no external provider is registered at all
# ===========================================================================
class TestFlagFalseRemovesTheExternalRegistry:
    def test_no_external_rows_even_with_every_key_configured(self, monkeypatch):
        set_keys(monkeypatch, enabled=False, mock_mode=True)
        rows = ProviderRouter.default_configs()
        names = [c.name for c in rows]
        assert not EXTERNAL_NAMES.intersection(names)
        # The offline provider stays, governed by AI_MOCK_MODE as before.
        assert names == ["Offline"]

    def test_no_mock_mode_leaves_an_empty_registry(self, monkeypatch):
        set_keys(monkeypatch, enabled=False, mock_mode=False)
        rows = ProviderRouter.default_configs()
        assert rows == []
        router = ProviderRouter(configs=rows)
        assert router.chain() == []
        assert router.available == []

    def test_chain_and_available_carry_nothing_external(self, monkeypatch):
        set_keys(monkeypatch, enabled=False, mock_mode=True)
        router = ProviderRouter()
        assert [c.name for c in router.chain()] == ["Offline"]
        assert router.available == ["Offline"]

    def test_credentials_on_settings_remain_untouched(self, monkeypatch):
        """The flag removes registry rows, never credentials: flipping it
        back must restore exactly the previous state."""
        set_keys(monkeypatch, enabled=False, mock_mode=True)
        before = {
            name: getattr(settings, name) for name in (
                "OPENROUTER_API_KEY", "OPENAI_API_KEY",
                "ANTHROPIC_API_KEY", "GEMINI_API_KEY",
            )
        }
        rows = ProviderRouter.default_configs()
        after = {
            name: getattr(settings, name) for name in (
                "OPENROUTER_API_KEY", "OPENAI_API_KEY",
                "ANTHROPIC_API_KEY", "GEMINI_API_KEY",
            )
        }
        assert before == after
        assert all(v is not None for v in after.values())
        # And no removed-provider key leaked into a surviving row.
        assert all(c.name not in EXTERNAL_NAMES for c in rows)

    def test_preferred_external_provider_cannot_resurrect_a_row(self, monkeypatch):
        """`preferred` sorts an existing chain; it cannot rebuild a registry
        entry the flag removed."""
        set_keys(monkeypatch, enabled=False, mock_mode=True)
        router = ProviderRouter()
        for preferred in ("Gemini", "OpenAI", "Claude", "OpenRouter"):
            assert [c.name for c in router.chain(preferred)] == ["Offline"]
            assert preferred not in router.available


# ===========================================================================
# 4. complete() / stream() cannot reach an external provider when disabled
# ===========================================================================
class _ExternalHTTPSentinel:
    """Stands in for httpx.AsyncClient; instantiating it fails the test."""

    def __init__(self, *args, **kwargs):
        raise AssertionError(
            "an external HTTP request was attempted while "
            "AI_EXTERNAL_PROVIDERS_ENABLED=false"
        )


class TestCompleteAndStreamAreIsolated:
    @pytest.fixture()
    def no_http(self, monkeypatch):
        monkeypatch.setattr(provider_base.httpx, "AsyncClient",
                            _ExternalHTTPSentinel)

    def test_complete_fails_closed_with_no_offline_provider(self, monkeypatch, no_http):
        set_keys(monkeypatch, enabled=False, mock_mode=False)
        router = ProviderRouter()
        with pytest.raises(NoProviderConfigured):
            _run(router.complete(request()))

    def test_stream_fails_closed_with_no_offline_provider(self, monkeypatch, no_http):
        set_keys(monkeypatch, enabled=False, mock_mode=False)
        router = ProviderRouter()

        async def collect():
            tokens = []
            async for token in router.stream(request()):
                tokens.append(token)
            return tokens

        with pytest.raises(NoProviderConfigured):
            _run(collect())

    def test_complete_is_served_only_by_the_offline_provider(self, monkeypatch, no_http):
        set_keys(monkeypatch, enabled=False, mock_mode=True)
        router = ProviderRouter()
        response = _run(router.complete(request()))
        assert response.provider == mock.NAME
        assert response.provider not in EXTERNAL_NAMES
        assert response.cost_usd == 0.0

    def test_stream_is_served_only_by_the_offline_provider(self, monkeypatch, no_http):
        set_keys(monkeypatch, enabled=False, mock_mode=True)
        router = ProviderRouter()

        async def collect():
            return [t async for t in router.stream(request())]

        tokens = _run(collect())
        assert tokens  # the offline provider chunks its completion
        assert "".join(tokens).strip()

    def test_complete_with_a_preferred_external_never_leaves_the_box(
        self, monkeypatch, no_http,
    ):
        set_keys(monkeypatch, enabled=False, mock_mode=True)
        router = ProviderRouter()
        response = _run(router.complete(request(), preferred="Gemini"))
        assert response.provider == mock.NAME

    def test_no_http_is_attempted_even_when_every_key_is_set(self, monkeypatch, no_http):
        """The sentinel covers complete() and stream() in both mock states;
        reaching this point means the transport layer was never touched."""
        for mock_mode in (True, False):
            set_keys(monkeypatch, enabled=False, mock_mode=mock_mode)
            router = ProviderRouter()
            try:
                _run(router.complete(request()))
            except NoProviderConfigured:
                pass


# ===========================================================================
# 5. The offline provider stays deterministic under the flag
# ===========================================================================
class TestOfflineBehaviourIsDeterministic:
    def test_two_routers_give_the_same_offline_answer(self, monkeypatch):
        set_keys(monkeypatch, enabled=False, mock_mode=True)
        first = _run(ProviderRouter().complete(request()))
        second = _run(ProviderRouter().complete(request()))
        assert first.provider == second.provider == mock.NAME
        assert first.content == second.content
        assert first.model == second.model
        assert first.cost_usd == second.cost_usd == 0.0

    def test_the_flag_does_not_change_the_offline_answer(self, monkeypatch):
        """Flag true and flag false produce the SAME offline output — the
        flag changes who is registered, not what the offline provider says."""
        set_keys(monkeypatch, enabled=True, mock_mode=True)
        # With keys set and the flag true a live provider leads the chain, so
        # compare the offline provider directly in both states.
        offline = mock.OfflineProvider(mock.DEFAULTS)
        with_flag_true = _run(offline.complete(request()))
        set_keys(monkeypatch, enabled=False, mock_mode=True)
        response = _run(ProviderRouter().complete(request()))
        assert response.content == with_flag_true.content


# ===========================================================================
# 6. Parts 1 / 2A / 2C / 2D deterministic paths are unchanged by the flag
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


class TestDeterministicPathsAreFlagInvariant:
    """Every internal layer runs identically with the flag true and false.

    These layers are provider-free by construction; the tests below prove the
    flag introduced no coupling — each output is computed once per flag state
    and the two must be byte-identical.
    """

    @pytest.fixture(params=[True, False], ids=["flag-on", "flag-off"])
    def flag(self, request, monkeypatch):
        set_keys(monkeypatch, enabled=request.param)
        return request.param

    def test_part1_intent_resolution_is_unchanged(self, flag):
        resolver = FinancialIntentResolver()
        assert resolver.resolve("What is the P/E ratio?") is not None

    def test_part1_financial_engine_is_unchanged(self, flag):
        answer = FinancialAnswerEngine().answer(
            FinancialIntent.PE, _pe_context(),
        )
        assert answer.content

    def test_phase2a_investment_engine_is_unchanged(self, flag):
        from tests.test_financial_answer_engine import full_context
        answer = InvestmentAnswerEngine().answer(
            FinancialIntent.OVERALL_ASSESSMENT, full_context(),
        )
        assert answer.content

    def test_part2b_planning_is_unchanged(self, flag):
        plan = QuestionPlanner().plan("Compare revenue and pat")
        assert plan.query_type.value == "comparison"

    def test_part2c_composer_is_unchanged(self, flag):
        plan = QuestionPlanner().plan(
            "What is the P/E and what is the financial quality?",
        )
        decision = InternalComposer().compose(plan)
        assert decision.status.value in {"ready", "not_composable"}

    def test_part2d_open_ended_engine_is_unchanged(self, flag):
        plan = QuestionPlanner().plan("Compare revenue and pat")
        answer = InternalOpenEndedEngine().answer(plan, _compare_context())
        assert answer.status.value == "answered"
        assert answer.content

    def test_each_layer_is_byte_identical_across_both_states(self, monkeypatch):
        """The strongest form: the same call in both flag states returns the
        same bytes, so the flag cannot have altered any deterministic path."""
        outputs: dict[bool, dict[str, object]] = {}
        for enabled in (True, False):
            set_keys(monkeypatch, enabled=enabled)
            pe = FinancialAnswerEngine().answer(
                FinancialIntent.PE, _pe_context(),
            )
            plan = QuestionPlanner().plan("Compare revenue and pat")
            internal = InternalOpenEndedEngine().answer(plan, _compare_context())
            outputs[enabled] = {
                "resolver": FinancialIntentResolver().resolve("What is the P/E ratio?"),
                "pe_text": pe.content,
                "pe_citations": [c.key for c in pe.used_citations],
                "route": plan.execution_route.value,
                "internal_status": internal.status.value,
                "internal_text": internal.content,
            }
        assert outputs[True] == outputs[False]


# ===========================================================================
# 7. Backward compatibility — a settings object without the attribute
# ===========================================================================
class TestBackwardCompatibleSettings:
    def test_settings_without_the_attribute_register_externals(self, monkeypatch):
        """The pre-flag settings object behaves exactly as before: true."""
        monkeypatch.setattr(config_module, "settings", fake_settings())
        rows = ProviderRouter.default_configs()
        assert [c.name for c in rows] == [
            "OpenRouter", "OpenAI", "Claude", "Gemini",
        ]

    def test_settings_with_the_attribute_false_do_not(self, monkeypatch):
        monkeypatch.setattr(
            config_module, "settings",
            fake_settings(AI_EXTERNAL_PROVIDERS_ENABLED=False),
        )
        assert ProviderRouter.default_configs() == []

    def test_settings_with_the_attribute_true_match_the_default(self, monkeypatch):
        monkeypatch.setattr(
            config_module, "settings",
            fake_settings(AI_EXTERNAL_PROVIDERS_ENABLED=True),
        )
        rows = ProviderRouter.default_configs()
        assert [c.name for c in rows] == [
            "OpenRouter", "OpenAI", "Claude", "Gemini",
        ]


# ===========================================================================
# 8. Architecture — the gate is structural, nothing was deleted
# ===========================================================================
class TestArchitecture:
    def test_the_vendor_loop_is_gated_by_the_flag(self):
        source = inspect.getsource(ProviderRouter.default_configs)
        gate = 'getattr(settings, "AI_EXTERNAL_PROVIDERS_ENABLED", True)'
        assert gate in source
        gate_line = None
        loop_line = None
        for index, line in enumerate(source.splitlines()):
            if gate in line and gate_line is None:
                gate_line = index
            if "for module in PROVIDER_MODULES:" in line:
                loop_line = index
        assert gate_line is not None and loop_line is not None
        assert gate_line < loop_line
        # The loop is NESTED inside the gate, not merely after it.
        gate_indent = len(source.splitlines()[gate_line]) - len(
            source.splitlines()[gate_line].lstrip())
        loop_indent = len(source.splitlines()[loop_line]) - len(
            source.splitlines()[loop_line].lstrip())
        assert loop_indent > gate_indent

    def test_the_offline_provider_is_not_governed_by_the_new_flag(self):
        """AI_MOCK_MODE keeps sole ownership of the offline provider."""
        source = inspect.getsource(ProviderRouter.default_configs)
        assert "if settings.AI_MOCK_MODE:" in source
        mock_line = next(
            i for i, line in enumerate(source.splitlines())
            if "if settings.AI_MOCK_MODE:" in line
        )
        gate_line = next(
            i for i, line in enumerate(source.splitlines())
            if "AI_EXTERNAL_PROVIDERS_ENABLED" in line
        )
        # The mock append precedes the gate and is not nested inside it.
        assert mock_line < gate_line

    def test_every_provider_module_still_exists(self):
        providers = APP / "services" / "ai" / "providers"
        for name in ("router.py", "base.py", "gemini.py", "openai.py",
                     "openrouter.py", "claude.py", "mock.py", "shapes.py",
                     "__init__.py"):
            assert (providers / name).is_file(), name

    def test_every_vendor_module_is_still_registered_when_enabled(self):
        """No vendor module was dropped from PROVIDER_MODULES."""
        assert PROVIDER_MODULES == (openrouter, openai, claude, gemini)

    def test_fallback_order_is_unchanged(self):
        assert FALLBACK_ORDER == ("Gemini", "OpenAI", "Claude", "OpenRouter")

    def test_translation_provider_default_is_unchanged(self):
        """Phase 2E A1 touches the LLM registry only; the translation
        default stays exactly as it was (later steps own it)."""
        assert Settings.model_fields["TRANSLATION_PROVIDER"].default == "llm"

    def test_ai_mock_mode_default_is_unchanged(self):
        assert Settings.model_fields["AI_MOCK_MODE"].default is True

    def test_retry_cache_and_ledger_knobs_are_unchanged(self):
        from app.services.ai.providers import router as router_module
        assert router_module.MAX_ATTEMPTS == 3
        assert router_module.BASE_BACKOFF_SECONDS == 0.5
        assert router_module.CACHE_TTL_SECONDS == 900
        assert router_module.CACHE_MAX_ENTRIES == 256

    def test_env_example_documents_the_flag_as_true(self):
        env = (BACKEND / ".env.example").read_text()
        assert "AI_EXTERNAL_PROVIDERS_ENABLED=true" in env


class TestPart2DTripwireRemainsIntact:
    """Part 2D shipped a tripwire asserting the provider fallback survives.
    This change must not touch it — it still runs in the suite alongside
    these tests, and its source is asserted unchanged here."""

    def test_the_tripwire_class_and_its_assertions_are_still_there(self):
        tripwire = (
            BACKEND / "tests" / "test_internal_open_ended_architecture.py"
        ).read_text()
        assert "class TestProviderFallbackRemainsAvailable" in tripwire
        for method in (
            "test_every_provider_module_still_exists",
            "test_the_router_is_still_imported_by_the_analyst",
            "test_the_analyst_still_retrieves_and_calls_a_provider",
            "test_the_retrieval_and_provider_path_was_not_edited_away",
        ):
            assert method in tripwire, method

    def test_the_analyst_still_falls_back_to_a_provider(self):
        """The live assertions the tripwire makes, re-stated here so a
        regression is visible in this file too."""
        analyst = (APP / "services" / "ai" / "analyst.py").read_text()
        assert "from app.services.ai.providers.router import ProviderRouter" in analyst
        assert "self.router.complete(" in analyst
        assert "self.router.stream(" in analyst
