"""Unit tests for the AI layer: providers, grounding, citations, guardrails, memory."""
from __future__ import annotations

import asyncio
import re

import pytest

from app.domain.ai.types import (
    Citation, ClaimType, CompletionRequest, CompletionResponse, EvidenceKind,
    Message, NoProviderConfigured, PayloadShape, ProviderError, RateLimitError,
    Role, TokenUsage,
)
from app.services.ai.citation_engine import annotate, audit, _sentences
from app.services.ai.guardrails import (
    DISCLOSURE, check, classify_block, enforce,
)
from app.services.ai.memory import ConversationMemory, MemoryStore
from app.services.ai.prompt_builder import PromptBuilder
from app.services.ai.prompt_library import (
    BUILTIN_PROMPTS, SYSTEM_PREAMBLE, Capability, OutputStyle, get_prompt,
)
from app.services.ai.providers.base import ProviderConfig, dig
from app.services.ai.providers.router import (
    ProviderRouter, ResponseCache, UsageLedger,
)
from app.services.ai.providers.shapes import (
    AnthropicShapeProvider, GeminiShapeProvider, OpenAIShapeProvider,
    SHAPE_ADAPTERS,
)
from app.services.ai.context_builder import GroundedContext
from app.services.ai.tools import TOOLS, TOOLS_BY_NAME, describe_tools


def config(shape: str = "openai", **kw) -> ProviderConfig:
    defaults = dict(
        name="Test", endpoint="https://example.invalid/v1/x",
        auth_header="Authorization: Bearer {key}", payload_shape=shape,
        response_path="", default_model="test-model", api_key="k",
        input_cost_per_m=1.0, output_cost_per_m=2.0,
    )
    return ProviderConfig(**{**defaults, **kw})


def _no_delay(monkeypatch) -> None:
    """Remove retry backoff without recursing into the patched sleep."""
    import app.services.ai.providers.router as router_module

    async def instant(_seconds):
        return None

    monkeypatch.setattr(router_module.asyncio, "sleep", instant)


def request(text: str = "hello") -> CompletionRequest:
    return CompletionRequest(messages=[
        Message(Role.SYSTEM, "system rules"),
        Message(Role.USER, text),
    ])


# ================================================================== providers
class TestPathExtraction:
    @pytest.mark.parametrize("body,path,expected", [
        ({"choices": [{"message": {"content": "a"}}]}, "choices[0].message.content", "a"),
        ({"content": [{"text": "b"}]}, "content[0].text", "b"),
        ({"candidates": [{"content": {"parts": [{"text": "c"}]}}]},
         "candidates[0].content.parts[0].text", "c"),
    ])
    def test_all_three_shapes(self, body, path, expected):
        assert dig(body, path) == expected

    def test_missing_path_is_empty_not_an_error(self):
        assert dig({"a": 1}, "b[3].c") == ""


class TestPayloadShapes:
    def test_openai_puts_system_in_messages(self):
        payload = OpenAIShapeProvider(config()).build_payload(request(), "m")
        assert payload["messages"][0]["role"] == "system"
        assert payload["model"] == "m"

    def test_anthropic_hoists_system_to_top_level(self):
        payload = AnthropicShapeProvider(config("anthropic")).build_payload(request(), "m")
        assert payload["system"] == "system rules"
        assert all(m["role"] != "system" for m in payload["messages"])

    def test_gemini_renames_assistant_to_model(self):
        req = CompletionRequest(messages=[
            Message(Role.SYSTEM, "s"), Message(Role.USER, "u"),
            Message(Role.ASSISTANT, "a"),
        ])
        payload = GeminiShapeProvider(config("gemini")).build_payload(req, "m")
        roles = [c["role"] for c in payload["contents"]]
        assert "model" in roles
        assert "assistant" not in roles
        assert payload["systemInstruction"]["parts"][0]["text"] == "s"

    def test_three_shapes_registered(self):
        assert set(SHAPE_ADAPTERS) == {
            PayloadShape.OPENAI.value, PayloadShape.ANTHROPIC.value,
            PayloadShape.GEMINI.value,
        }

    def test_usage_parsed_per_dialect(self):
        openai = OpenAIShapeProvider(config()).extract_usage(
            {"usage": {"prompt_tokens": 10, "completion_tokens": 5}})
        anthropic = AnthropicShapeProvider(config("anthropic")).extract_usage(
            {"usage": {"input_tokens": 10, "output_tokens": 5}})
        gemini = GeminiShapeProvider(config("gemini")).extract_usage(
            {"usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5}})
        for usage in (openai, anthropic, gemini):
            assert usage.total_tokens == 15


class TestAuthAndUrl:
    def test_bearer_header_built_from_registry(self):
        headers = OpenAIShapeProvider(config(api_key="secret")).build_headers("m")
        assert headers["Authorization"] == "Bearer secret"

    def test_multi_part_auth_header(self):
        provider = AnthropicShapeProvider(config(
            "anthropic", auth_header="x-api-key: {key}|anthropic-version: 2023-06-01",
            api_key="s"))
        headers = provider.build_headers("m")
        assert headers["x-api-key"] == "s"
        assert headers["anthropic-version"] == "2023-06-01"

    def test_key_in_url_adds_no_auth_header(self):
        provider = GeminiShapeProvider(config(
            "gemini", auth_header="(key in URL)",
            endpoint="https://x/{model}:go?key={key}", api_key="s"))
        assert "Authorization" not in provider.build_headers("m")
        assert provider.build_url("gemini-1.5-pro") == (
            "https://x/gemini-1.5-pro:go?key=s")


class TestAbstractionIsReal:
    """The Module 4 lesson: a registry that still branches on vendor name is not
    an abstraction."""

    @staticmethod
    def _transport_code() -> str:
        """Transport source with comments and docstrings stripped.

        Prose may legitimately name vendors when explaining which dialects they
        share; executable code may not.
        """
        import ast
        from pathlib import Path

        root = Path(__file__).resolve().parents[1] / "app" / "services" / "ai" / "providers"
        chunks = []
        for name in ("base.py", "shapes.py"):
            tree = ast.parse((root / name).read_text())
            # ast.unparse drops comments; blanking docstrings removes the rest.
            for node in ast.walk(tree):
                if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                     ast.AsyncFunctionDef)):
                    doc = ast.get_docstring(node, clean=False)
                    if doc is not None and node.body:
                        node.body[0] = ast.Expr(value=ast.Constant(value=""))
            chunks.append(ast.unparse(tree))
        return "\n".join(chunks)

    def test_no_vendor_endpoint_hard_coded_in_transport(self):
        """Endpoints belong in the registry, never in transport code."""
        code = self._transport_code().lower()
        for endpoint in ("openrouter.ai", "api.openai.com", "api.anthropic.com",
                         "generativelanguage"):
            assert endpoint not in code, f"transport hard-codes '{endpoint}'"

    def test_transport_never_branches_on_provider_name(self):
        """The Module 4 defect: dispatching on vendor name rather than shape."""
        code = self._transport_code()
        for pattern in ("config.name ==", "self.name ==", 'name == "Open',
                        'name == "Claude"', 'name == "Gemini"'):
            assert pattern not in code, f"transport branches on name: {pattern}"


class TestCostAccounting:
    def test_cost_from_token_counts(self):
        provider = OpenAIShapeProvider(config(input_cost_per_m=3.0, output_cost_per_m=15.0))
        cost = provider.estimate_cost(TokenUsage(1_000_000, 1_000_000))
        assert cost == pytest.approx(18.0)

    def test_zero_usage_is_free(self):
        assert OpenAIShapeProvider(config()).estimate_cost(TokenUsage()) == 0.0


class TestUsageLedger:
    def test_accumulates_across_providers(self):
        ledger = UsageLedger()
        ledger.record(CompletionResponse("a", "P1", "m", TokenUsage(10, 5), 100.0, 0.01))
        ledger.record(CompletionResponse("b", "P2", "m", TokenUsage(20, 10), 200.0, 0.02))
        snapshot = ledger.snapshot()
        assert snapshot["calls"] == 2
        assert snapshot["total_tokens"] == 45
        assert snapshot["cost_usd"] == pytest.approx(0.03)
        assert set(snapshot["by_provider"]) == {"P1", "P2"}

    def test_cached_responses_excluded_from_latency(self):
        ledger = UsageLedger()
        ledger.record(CompletionResponse("a", "P", "m", TokenUsage(1, 1), 500.0, 0.0,
                                         cached=True))
        assert ledger.cached_hits == 1
        assert ledger.p50_latency_ms == 0.0


class TestResponseCache:
    def test_hit_and_miss(self):
        cache = ResponseCache()
        key = cache.key(request(), "P")
        assert cache.get(key) is None
        cache.put(key, CompletionResponse("x", "P", "m"))
        assert cache.get(key) is not None

    def test_different_requests_have_different_keys(self):
        cache = ResponseCache()
        assert cache.key(request("a"), "P") != cache.key(request("b"), "P")

    def test_expiry(self):
        cache = ResponseCache(ttl=-1)
        key = cache.key(request(), "P")
        cache.put(key, CompletionResponse("x", "P", "m"))
        assert cache.get(key) is None

    def test_capacity_evicts(self):
        cache = ResponseCache(capacity=2)
        for i in range(4):
            cache.put(cache.key(request(str(i)), "P"), CompletionResponse("x", "P", "m"))
        assert len(cache._entries) <= 2


# ------------------------------------------------------- router behaviour
class FlakyProvider(OpenAIShapeProvider):
    """Fails a set number of times, then succeeds."""

    def __init__(self, cfg, failures: int, error: Exception | None = None):
        super().__init__(cfg)
        self.remaining = failures
        self.calls = 0
        self.error = error

    async def complete(self, req):
        self.calls += 1
        if self.remaining > 0:
            self.remaining -= 1
            raise self.error or ProviderError("boom", provider=self.name, retryable=True)
        return CompletionResponse("ok", self.name, "m", TokenUsage(1, 1), 1.0, 0.0)


class TestRouter:
    def test_no_providers_raises_clearly(self):
        router = ProviderRouter(configs=[])
        with pytest.raises(NoProviderConfigured):
            asyncio.run(router.complete(request()))

    def test_unconfigured_provider_is_skipped(self):
        router = ProviderRouter(configs=[config(api_key=None)])
        with pytest.raises(NoProviderConfigured):
            asyncio.run(router.complete(request()))

    def test_retries_then_succeeds(self, monkeypatch):
        router = ProviderRouter(configs=[config(name="A")])
        flaky = FlakyProvider(router.configs[0], failures=2)
        monkeypatch.setattr(router, "build", lambda c: flaky)
        _no_delay(monkeypatch)
        response = asyncio.run(router.complete(request(), use_cache=False))
        assert response.content == "ok"
        assert flaky.calls == 3

    def test_gives_up_after_max_attempts(self, monkeypatch):
        router = ProviderRouter(configs=[config(name="A")])
        flaky = FlakyProvider(router.configs[0], failures=99)
        monkeypatch.setattr(router, "build", lambda c: flaky)
        _no_delay(monkeypatch)
        with pytest.raises(ProviderError):
            asyncio.run(router.complete(request(), use_cache=False))
        assert flaky.calls == 3

    def test_non_retryable_error_does_not_retry(self, monkeypatch):
        router = ProviderRouter(configs=[config(name="A")])
        flaky = FlakyProvider(
            router.configs[0], failures=99,
            error=ProviderError("bad request", provider="A", retryable=False),
        )
        monkeypatch.setattr(router, "build", lambda c: flaky)
        with pytest.raises(ProviderError):
            asyncio.run(router.complete(request(), use_cache=False))
        assert flaky.calls == 1, "a 4xx must not be retried"

    def test_falls_back_to_the_next_provider(self, monkeypatch):
        router = ProviderRouter(configs=[config(name="A"), config(name="B")])
        broken = FlakyProvider(
            router.configs[0], failures=99,
            error=ProviderError("dead", provider="A", retryable=False))
        healthy = FlakyProvider(router.configs[1], failures=0)

        def build(cfg):
            return broken if cfg.name == "A" else healthy

        monkeypatch.setattr(router, "build", build)
        response = asyncio.run(router.complete(request(), use_cache=False))
        assert response.provider == "B"
        assert response.fell_back_from == "A"
        assert router.ledger.fallbacks == 1

    def test_rate_limit_is_retried(self, monkeypatch):
        router = ProviderRouter(configs=[config(name="A")])
        flaky = FlakyProvider(
            router.configs[0], failures=1,
            error=RateLimitError("slow down", provider="A", retry_after=0.01))
        monkeypatch.setattr(router, "build", lambda c: flaky)
        response = asyncio.run(router.complete(request(), use_cache=False))
        assert response.content == "ok"
        assert flaky.calls == 2

    def test_cache_prevents_a_second_call(self, monkeypatch):
        router = ProviderRouter(configs=[config(name="A")])
        provider = FlakyProvider(router.configs[0], failures=0)
        monkeypatch.setattr(router, "build", lambda c: provider)
        asyncio.run(router.complete(request()))
        second = asyncio.run(router.complete(request()))
        assert provider.calls == 1
        assert second.cached is True

    def test_preferred_provider_goes_first(self):
        router = ProviderRouter(configs=[config(name="A"), config(name="B")])
        assert router.chain("b")[0].name == "B"

    def test_offline_provider_ranks_last(self):
        from app.services.ai.providers import mock
        router = ProviderRouter(configs=[mock.DEFAULTS, config(name="Live")])
        assert router.chain()[0].name == "Live"


class TestOfflineProvider:
    def test_produces_grounded_prose_from_evidence(self):
        from app.services.ai.providers import mock
        provider = mock.OfflineProvider(mock.DEFAULTS)
        req = CompletionRequest(messages=[
            Message(Role.SYSTEM, "TASK: Assess strength"),
            Message(Role.SYSTEM,
                    "[revenue] Revenue (FY25): 33,543.00 ₹ cr — source: 06 IS"),
            Message(Role.USER, "How strong?"),
        ])
        response = asyncio.run(provider.complete(req))
        assert "[revenue]" in response.content
        assert "33,543.00" in response.content
        assert response.usage.total_tokens > 0

    def test_says_so_when_there_is_no_evidence(self):
        from app.services.ai.providers import mock
        provider = mock.OfflineProvider(mock.DEFAULTS)
        response = asyncio.run(provider.complete(request("anything")))
        assert "Unavailable" in response.content

    def test_cannot_emit_an_uncited_number(self):
        """Structural guarantee: every figure comes from a citation line."""
        from app.services.ai.providers import mock
        provider = mock.OfflineProvider(mock.DEFAULTS)
        req = CompletionRequest(messages=[
            Message(Role.SYSTEM, "TASK: X"),
            Message(Role.SYSTEM, "[a] Alpha: 11.00 x — source: S"),
            Message(Role.USER, "q"),
        ])
        content = asyncio.run(provider.complete(req)).content
        numbers = set(re.findall(r"\d[\d,]*\.\d+", content))
        assert numbers <= {"11.00"}


# ================================================================== citations
class TestSentenceSplitting:
    def test_decimal_points_do_not_split_sentences(self):
        text = "Revenue reached 33,543.00 crore [revenue]. WACC is 15.17% [wacc]."
        assert len(_sentences(text)) == 2

    def test_plain_sentences_split(self):
        assert len(_sentences("One. Two. Three.")) == 3


class TestCitationAudit:
    def _evidence(self):
        return [
            Citation("revenue", "Revenue", EvidenceKind.STATEMENT, 33543.0, "₹ cr", "06 IS"),
            Citation("wacc", "WACC", EvidenceKind.VALUATION, 0.1517, "%", "Valuation"),
        ]

    def test_well_cited_answer_is_supported(self):
        text = "Revenue reached 33,543.00 crore [revenue]. WACC is 15.17% [wacc]."
        result = audit(text, self._evidence())
        assert result.is_supported
        assert result.coverage == pytest.approx(1.0)
        assert len(result.resolved) == 2

    def test_invented_key_is_caught(self):
        result = audit("Margins hit 42% [invented_key].", self._evidence())
        assert not result.is_supported
        assert result.unknown_keys == ["invented_key"]

    def test_fabricated_number_is_flagged(self):
        result = audit("Revenue was 99,999 crore.", self._evidence())
        assert "99999" in result.uncited_numbers

    def test_paraphrased_rounding_is_not_flagged(self):
        result = audit("Revenue of about 33,543 crore [revenue].", self._evidence())
        assert result.uncited_numbers == []

    def test_prose_without_numbers_is_supported(self):
        result = audit("The business appears durable.", self._evidence())
        assert result.coverage == 1.0

    def test_annotate_swaps_keys_for_labels(self):
        assert "[Revenue]" in annotate("Revenue rose [revenue].", self._evidence())

    def test_annotate_leaves_unknown_keys_visible(self):
        """Hiding an unknown key would conceal the failure."""
        assert "[bogus]" in annotate("Something [bogus].", self._evidence())


# ================================================================= guardrails
class TestGuardrails:
    def test_directive_advice_is_blocked(self):
        report = check("You should buy this stock immediately.")
        assert not report.passed
        assert any("directive" in v for v in report.violations)

    def test_certainty_language_is_blocked(self):
        report = check("Returns are guaranteed and it is risk-free.")
        assert not report.passed

    @pytest.mark.parametrize("text,expected", [
        ("Revenue was 33,543 crore [revenue].", ClaimType.FACT),
        ("The DCF projects an intrinsic value of 171 [dcf].", ClaimType.MODEL_OUTPUT),
        ("This suggests the balance sheet is sound.", ClaimType.INTERPRETATION),
        ("In my view the shares look attractive.", ClaimType.OPINION),
    ])
    def test_claim_classification(self, text, expected):
        assert classify_block(text) == expected

    def test_unclassifiable_prose_defaults_to_interpretation(self):
        """The cautious default — never silently promote reasoning to fact."""
        assert classify_block("Broadly speaking, matters stand thus") == \
            ClaimType.INTERPRETATION

    def test_unhedged_opinion_is_flagged(self):
        report = check("The valuation is compelling.")
        assert any("hedg" in v for v in report.violations)

    def test_hedged_opinion_passes(self):
        report = check("The valuation may be attractive if growth holds.")
        assert report.passed

    def test_disclosure_always_appended(self):
        text = "Revenue rose [revenue]."
        assert DISCLOSURE in enforce(text, check(text))

    def test_composition_counts_every_block(self):
        text = "Revenue was 100 [a].\n\nThis suggests strength.\n\nIn my view it may be good."
        report = check(text)
        assert sum(report.composition().values()) == len(report.blocks) == 3


# ===================================================================== memory
class TestMemory:
    def test_company_is_pinned(self):
        memory = ConversationMemory("s")
        memory.set_company("c1", "ABC", "ABC Ltd")
        assert "ABC Ltd" in memory.state_summary()

    def test_switching_company_clears_assumptions(self):
        memory = ConversationMemory("s")
        memory.set_company("c1", "A", "A Ltd")
        memory.remember_assumption("wacc", 0.12)
        memory.set_company("c2", "B", "B Ltd")
        assert memory.assumptions == {}

    def test_same_company_keeps_assumptions(self):
        memory = ConversationMemory("s")
        memory.set_company("c1", "A", "A Ltd")
        memory.remember_assumption("wacc", 0.12)
        memory.set_company("c1", "A", "A Ltd")
        assert memory.assumptions == {"wacc": 0.12}

    def test_history_trimmed_by_tokens_not_turns(self):
        memory = ConversationMemory("s")
        memory.add(Role.USER, "x" * 4000)
        memory.add(Role.USER, "short")
        assert len(memory.recent(budget=100)) == 1

    def test_turn_cap_enforced(self):
        memory = ConversationMemory("s")
        for i in range(60):
            memory.add(Role.USER, str(i))
        assert memory.turn_count <= 40

    def test_documents_deduplicated(self):
        memory = ConversationMemory("s")
        memory.attach_document("AR.pdf")
        memory.attach_document("AR.pdf")
        assert memory.documents == ["AR.pdf"]

    def test_store_isolates_sessions(self):
        store = MemoryStore()
        store.get("a").set_company("c1", "A", "A Ltd")
        assert store.get("b").company_id is None

    def test_store_evicts_at_capacity(self):
        store = MemoryStore(capacity=2)
        for i in range(5):
            store.get(f"s{i}")
        assert len(store._sessions) <= 2


# ============================================================ prompt library
class TestPromptLibrary:
    def test_every_capability_has_a_prompt(self):
        missing = [c.value for c in Capability if c.value not in BUILTIN_PROMPTS]
        assert missing == []

    def test_sixteen_analyst_capabilities_plus_chat(self):
        assert len(BUILTIN_PROMPTS) == 17

    def test_preamble_states_all_guardrails(self):
        for rule in ("GROUNDING", "CITATION", "CLAIM LABELLING",
                     "NO ADVICE AS CERTAINTY", "HONESTY ABOUT GAPS"):
            assert rule in SYSTEM_PREAMBLE

    def test_every_prompt_declares_evidence(self):
        for prompt in BUILTIN_PROMPTS.values():
            assert prompt.evidence, f"{prompt.key} declares no evidence"

    def test_prompts_are_versioned(self):
        assert all(p.version >= 1 for p in BUILTIN_PROMPTS.values())

    def test_unknown_prompt_raises(self):
        with pytest.raises(KeyError):
            get_prompt("not_a_capability")

    def test_template_renders_placeholders(self):
        rendered = get_prompt("swot").render(
            evidence_block="[a] A: 1 — source: S", gaps="", question="", extra="")
        assert "[a] A: 1" in rendered
        assert "{evidence}" not in rendered


class TestPromptBuilder:
    def _context(self):
        context = GroundedContext("c1", "ABC", "ABC Ltd", "IT")
        context.add(Citation("revenue", "Revenue", EvidenceKind.STATEMENT,
                             100.0, "₹ cr", "06 IS"))
        context.add(Citation("dcf_value", "DCF value", EvidenceKind.VALUATION,
                             50.0, "₹", "Valuation"))
        return context

    def test_preamble_comes_first(self):
        built = PromptBuilder().build(get_prompt("business_summary"), self._context())
        assert built.request.messages[0].role is Role.SYSTEM
        assert "ABSOLUTE RULES" in built.request.messages[0].content

    def test_only_declared_evidence_is_included(self):
        """A moat prompt should not be diluted with unrelated figures."""
        built = PromptBuilder().build(get_prompt("business_summary"), self._context())
        keys = {c.key for c in built.citations}
        assert "revenue" in keys
        assert "dcf_value" not in keys  # valuation is not in FINANCIALS

    def test_memory_state_is_injected(self):
        memory = ConversationMemory("s")
        memory.set_company("c1", "ABC", "ABC Ltd")
        memory.remember_assumption("wacc", 0.12)
        built = PromptBuilder().build(
            get_prompt("chat"), self._context(), memory=memory, question="q")
        assert any("wacc=0.12" in m.content for m in built.request.messages)

    def test_style_instruction_applied(self):
        built = PromptBuilder().build(
            get_prompt("investment_thesis"), self._context(),
            style=OutputStyle.BOARD_PRESENTATION)
        assert "board pack" in built.request.messages[-1].content

    def test_gaps_are_declared_to_the_model(self):
        context = self._context()
        context.unavailable.append("Forecast projections")
        built = PromptBuilder().build(get_prompt("business_summary"), context)
        joined = " ".join(m.content for m in built.request.messages)
        assert "UNAVAILABLE" in joined


# ====================================================================== tools
class TestTools:
    def test_six_tools_registered(self):
        assert len(TOOLS) == 6
        assert set(TOOLS_BY_NAME) == {
            "financial_lookup", "ratio_lookup", "forecast_lookup",
            "dcf_lookup", "scoring_lookup", "document_search",
        }

    def test_catalogue_describes_every_tool(self):
        description = describe_tools()
        for tool in TOOLS:
            assert tool.name in description


# ================================================================== grounding
class TestGroundedContext:
    def test_none_values_are_never_cited(self):
        """A missing figure must not reach the model at all."""
        context = GroundedContext("c", "T", "T Ltd")
        context.add(Citation("x", "X", EvidenceKind.STATEMENT, None, "", "S"))
        assert context.citations == []

    def test_percentages_render_as_points(self):
        context = GroundedContext("c", "T", "T Ltd")
        context.add(Citation("wacc", "WACC", EvidenceKind.VALUATION, 0.1517, "%", "V"))
        assert "15.17 %" in context.render_evidence()

    def test_empty_context_says_so(self):
        context = GroundedContext("c", "T", "T Ltd")
        assert "No platform figures" in context.render_evidence()

    def test_gaps_are_rendered_for_the_prompt(self):
        context = GroundedContext("c", "T", "T Ltd")
        context.unavailable.append("Valuation outputs")
        assert "Valuation outputs" in context.render_gaps()

    def test_evidence_filtered_by_kind(self):
        context = GroundedContext("c", "T", "T Ltd")
        context.add(Citation("a", "A", EvidenceKind.STATEMENT, 1.0, "", "S"))
        context.add(Citation("b", "B", EvidenceKind.SCORING, 2.0, "", "S"))
        rendered = context.render_evidence([EvidenceKind.SCORING])
        assert "[b]" in rendered and "[a]" not in rendered
