"""Provider transport base.

One adapter per *wire format*, not per vendor. OpenRouter and OpenAI speak the
same dialect, so they share an adapter and differ only in registry data
(endpoint, auth header, default model). Adding a fifth OpenAI-compatible
provider is a configuration row, not a code change.

This is the correction of the defect found in Module 4's audit: the workbook
shipped a provider *table* while its VBA still branched on provider name. Here
the branch is on ``PayloadShape``, and a test asserts no vendor name appears in
transport code.
"""
from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

import httpx

from app.domain.ai.types import (
    CompletionRequest, CompletionResponse, Message, ProviderError,
    RateLimitError, Role, TokenUsage,
)


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    """Everything needed to talk to one provider. Mirrors the workbook registry."""

    name: str
    endpoint: str
    auth_header: str          # "Authorization: Bearer {key}" or "(key in URL)"
    payload_shape: str
    response_path: str
    default_model: str
    api_key: str | None = None
    timeout_seconds: float = 60.0
    #: USD per 1M tokens, for cost accounting.
    input_cost_per_m: float = 0.0
    output_cost_per_m: float = 0.0
    enabled: bool = True
    #: Non-auth headers a provider requires, as an immutable tuple of pairs.
    #:
    #: A tuple rather than a dict because `ProviderConfig` is frozen and
    #: hashable, and a dict field would break that. Introduced for OpenRouter,
    #: which attributes usage via `HTTP-Referer` and `X-Title` and rate-limits
    #: unattributed traffic harder. These could not travel in `auth_header`:
    #: that string is parsed by partitioning on the first `:`, so a value that
    #: is itself a URL is truncated at `https`.
    extra_headers: tuple[tuple[str, str], ...] = ()

    @property
    def configured(self) -> bool:
        return bool(self.api_key) and self.enabled

    def expand(self, text: str, model: str) -> str:
        """Substitute {key} and {model} placeholders."""
        return text.replace("{key}", self.api_key or "").replace("{model}", model)


#: httpx logs every request at INFO as `HTTP Request: POST <full url> "..."`.
#:
#: Gemini authenticates with the key in the **query string**, so that log line
#: prints the credential in clear text — into stdout, into Railway's log
#: stream, into whatever aggregator collects it, and into any support ticket
#: someone pastes it into. Observed directly during integration:
#:
#:     HTTP Request: POST https://...:generateContent?key=AQ.Ab8RN6... "429"
#:
#: Module 10 already redacts credentials from the application's own structured
#: logs, but this line is emitted by the transport library beneath that layer,
#: so the redaction never sees it. Raising httpx's logger to WARNING removes
#: the URL line while leaving genuine transport failures visible; the router
#: records provider, model, status and latency itself, so nothing diagnostic
#: is lost.
_HTTPX_SILENCED = False


def _silence_httpx_url_logging() -> None:
    global _HTTPX_SILENCED
    if _HTTPX_SILENCED:
        return
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    _HTTPX_SILENCED = True


class LLMProvider(ABC):
    """Transport contract. Business logic depends on this, never on a vendor."""

    def __init__(self, config: ProviderConfig) -> None:
        self.config = config

    @property
    def name(self) -> str:
        return self.config.name

    # ---------------------------------------------------------- wire format
    @abstractmethod
    def build_payload(self, request: CompletionRequest, model: str) -> dict: ...

    @abstractmethod
    def extract_content(self, body: dict) -> str: ...

    @abstractmethod
    def extract_usage(self, body: dict) -> TokenUsage: ...

    def build_headers(self, model: str) -> dict[str, str]:
        """Headers from the registry's auth column, plus any extras."""
        headers = {"Content-Type": "application/json"}
        for name, value in self.config.extra_headers:
            headers[name] = self.config.expand(value, model)
        auth = self.config.auth_header or ""
        if "key in URL" in auth:
            return headers
        for part in auth.split("|"):
            if ":" not in part:
                continue
            name, _, value = part.partition(":")
            headers[name.strip()] = self.config.expand(value.strip(), model)
        return headers

    def build_url(self, model: str) -> str:
        return self.config.expand(self.config.endpoint, model)

    # ------------------------------------------------------------- costing
    def estimate_cost(self, usage: TokenUsage) -> float:
        return (
            usage.prompt_tokens / 1_000_000 * self.config.input_cost_per_m
            + usage.completion_tokens / 1_000_000 * self.config.output_cost_per_m
        )

    # -------------------------------------------------------------- request
    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        """Issue a completion. Retries are handled by the router, not here."""
        if not self.config.configured:
            raise ProviderError(
                f"{self.name} has no API key configured",
                provider=self.name, retryable=False,
            )

        model = request.model or self.config.default_model
        started = time.perf_counter()

        _silence_httpx_url_logging()

        try:
            async with httpx.AsyncClient(timeout=self.config.timeout_seconds) as client:
                response = await client.post(
                    self.build_url(model),
                    headers=self.build_headers(model),
                    json=self.build_payload(request, model),
                )
        except httpx.TimeoutException as exc:
            raise ProviderError(f"{self.name} timed out", provider=self.name,
                                retryable=True) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"{self.name} transport error: {exc}",
                                provider=self.name, retryable=True) from exc

        latency_ms = (time.perf_counter() - started) * 1000

        if response.status_code == 429:
            try:
                retry_after = float(response.headers.get("retry-after", 2.0))
            except (TypeError, ValueError):
                retry_after = 2.0
            # A 429 means "slow down" or "you are out of allowance", and the
            # right response differs. Providers say which in the body:
            # Google returns a QuotaFailure detail naming the violated metric,
            # OpenRouter and OpenAI say "quota"/"credits" in the message. The
            # body was previously discarded, so a spent daily quota was
            # retried three times before falling back — eight seconds of
            # latency to reach a conclusion available immediately.
            body = (response.text or "")[:2000].lower()
            exhausted = any(
                marker in body for marker in (
                    "quotafailure", "quota exceeded", "exceeded your current quota",
                    "insufficient_quota", "perday", "per day", "billing",
                    "credits", "out of credit",
                )
            )
            raise RateLimitError(
                f"{self.name} {'quota exhausted' if exhausted else 'rate limited'}",
                provider=self.name, retry_after=retry_after,
                quota_exhausted=exhausted,
            )
        if response.status_code >= 500:
            raise ProviderError(f"{self.name} server error {response.status_code}",
                                provider=self.name, status=response.status_code,
                                retryable=True)
        if response.status_code >= 400:
            raise ProviderError(
                f"{self.name} rejected the request ({response.status_code}): "
                f"{response.text[:200]}",
                provider=self.name, status=response.status_code, retryable=False,
            )

        body = response.json()
        usage = self.extract_usage(body)
        return CompletionResponse(
            content=self.extract_content(body),
            provider=self.name, model=model, usage=usage,
            latency_ms=latency_ms, cost_usd=self.estimate_cost(usage),
        )

    async def stream(self, request: CompletionRequest):
        """Token stream.

        Default implementation degrades to a single completion chunked on
        whitespace, so streaming works for every provider even where the
        wire protocol differs. Adapters may override with true SSE.
        """
        response = await self.complete(request)
        for token in response.content.split(" "):
            yield token + " "
            await asyncio.sleep(0)


def dig(body: dict, path: str) -> str:
    """Read a value from a dotted path with array indices.

    Drives extraction from the registry's ``response_path`` column, so a new
    provider with a novel response shape needs no adapter — only a path.
    """
    current: object = body
    for part in path.replace("]", "").split("."):
        key, _, index = part.partition("[")
        if key:
            if not isinstance(current, dict) or key not in current:
                return ""
            current = current[key]
        if index:
            if not isinstance(current, list) or len(current) <= int(index):
                return ""
            current = current[int(index)]
    return current if isinstance(current, str) else str(current or "")
