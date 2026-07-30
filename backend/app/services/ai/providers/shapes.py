"""Wire-format adapters.

Three shapes cover the four required providers, which is the point: a provider
is defined by the dialect it speaks, not by its brand.
"""
from __future__ import annotations

from app.domain.ai.types import (
    CompletionRequest, PayloadShape, Role, TokenUsage,
)
from app.services.ai.providers.base import LLMProvider, dig


class OpenAIShapeProvider(LLMProvider):
    """`/chat/completions` dialect — OpenAI, OpenRouter, and most compatibles."""

    def build_payload(self, request: CompletionRequest, model: str) -> dict:
        payload = {
            "model": model,
            "messages": [{"role": m.role.value, "content": m.content}
                         for m in request.messages],
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
        }
        if request.response_format == "json":
            payload["response_format"] = {"type": "json_object"}
        return payload

    def extract_content(self, body: dict) -> str:
        return dig(body, self.config.response_path or "choices[0].message.content")

    def extract_usage(self, body: dict) -> TokenUsage:
        usage = body.get("usage") or {}
        return TokenUsage(
            prompt_tokens=int(usage.get("prompt_tokens", 0)),
            completion_tokens=int(usage.get("completion_tokens", 0)),
        )


class AnthropicShapeProvider(LLMProvider):
    """`/v1/messages` dialect. System prompt is a top-level field, not a message."""

    def build_payload(self, request: CompletionRequest, model: str) -> dict:
        system = " ".join(
            m.content for m in request.messages if m.role is Role.SYSTEM
        )
        conversation = [
            {"role": m.role.value, "content": m.content}
            for m in request.messages if m.role is not Role.SYSTEM
        ]
        payload = {
            "model": model,
            "messages": conversation,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
        }
        if system:
            payload["system"] = system
        return payload

    def extract_content(self, body: dict) -> str:
        return dig(body, self.config.response_path or "content[0].text")

    def extract_usage(self, body: dict) -> TokenUsage:
        usage = body.get("usage") or {}
        return TokenUsage(
            prompt_tokens=int(usage.get("input_tokens", 0)),
            completion_tokens=int(usage.get("output_tokens", 0)),
        )


class GeminiShapeProvider(LLMProvider):
    """`generateContent` dialect. Auth travels in the URL, roles are renamed."""

    def build_payload(self, request: CompletionRequest, model: str) -> dict:
        system = " ".join(
            m.content for m in request.messages if m.role is Role.SYSTEM
        )
        contents = [
            {
                # Gemini calls the assistant "model" and has no system role.
                "role": "model" if m.role is Role.ASSISTANT else "user",
                "parts": [{"text": m.content}],
            }
            for m in request.messages if m.role is not Role.SYSTEM
        ]
        payload = {
            "contents": contents,
            "generationConfig": {
                "temperature": request.temperature,
                "maxOutputTokens": request.max_tokens,
            },
        }
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        return payload

    def extract_content(self, body: dict) -> str:
        return dig(body, self.config.response_path
                   or "candidates[0].content.parts[0].text")

    def extract_usage(self, body: dict) -> TokenUsage:
        usage = body.get("usageMetadata") or {}
        return TokenUsage(
            prompt_tokens=int(usage.get("promptTokenCount", 0)),
            completion_tokens=int(usage.get("candidatesTokenCount", 0)),
        )


#: Shape -> adapter. The only dispatch table in the transport layer.
SHAPE_ADAPTERS: dict[str, type[LLMProvider]] = {
    PayloadShape.OPENAI.value: OpenAIShapeProvider,
    PayloadShape.ANTHROPIC.value: AnthropicShapeProvider,
    PayloadShape.GEMINI.value: GeminiShapeProvider,
}
