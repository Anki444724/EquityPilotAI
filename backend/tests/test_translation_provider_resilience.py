"""Failure-mode tests for the language provider boundary.

These exercise the paths that protect source figures and citations when a live
provider is unavailable or returns an invalid translation.
"""
from __future__ import annotations

import asyncio

from app.domain.ai.types import CompletionResponse, ProviderError, TokenUsage
from app.domain.language.types import Language
from app.services.language.translators import LLMTranslator, PassthroughTranslator


def run(coro):
    return asyncio.run(coro)


class OfflineRouter:
    chain = [type("Provider", (), {"name": "offline"})()]

    async def complete(self, request):  # pragma: no cover - must not be called
        raise AssertionError("offline-only chains must use the glossary directly")


class ResponseRouter:
    def __init__(self, response=None, error=None):
        self.chain = [type("Provider", (), {"name": "live"})()]
        self.response = response
        self.error = error
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        if self.error:
            raise self.error
        return self.response


def test_offline_chain_uses_honest_glossary_fallback():
    result = run(LLMTranslator(OfflineRouter()).translate(
        "Revenue was ₹100 crore [revenue].", Language.HINDI,
    ))
    assert result.translated is False
    assert result.provider == "glossary"
    assert "राजस्व" in result.text
    assert "[revenue]" in result.text
    assert "offline composer" in result.detail


def test_live_router_error_returns_original_with_explanation():
    router = ResponseRouter(error=ProviderError("quota exhausted", provider="live"))
    source = "Revenue was ₹100 crore [revenue]."
    result = run(LLMTranslator(router).translate(source, Language.HINDI))
    assert result.text == source
    assert result.translated is False
    assert "unavailable" in result.detail


def test_offline_response_after_live_fallback_is_not_presented_as_translation():
    router = ResponseRouter(CompletionResponse(
        content="unrelated boilerplate", provider="offline", model="offline",
    ))
    result = run(LLMTranslator(router).translate("Revenue grew [revenue].", Language.HINDI))
    assert result.translated is False
    assert result.provider == "glossary"
    assert "[revenue]" in result.text
    assert len(router.requests) == 1


def test_missing_protected_token_fails_closed_to_english():
    router = ResponseRouter(CompletionResponse(
        content="राजस्व बढ़ा।", provider="live", model="model",
        usage=TokenUsage(prompt_tokens=12, completion_tokens=8), cost_usd=0.01,
    ))
    source = "Revenue was ₹100 crore [revenue]."
    result = run(LLMTranslator(router).translate(source, Language.HINDI))
    assert result.text == source
    assert result.translated is False
    assert result.integrity_problems
    assert result.cost_usd == 0.01


def test_passthrough_labels_non_english_as_unavailable():
    result = run(PassthroughTranslator().translate("Revenue", Language.HINDI))
    assert result.text == "Revenue"
    assert result.translated is False
    assert "No translator" in result.detail
