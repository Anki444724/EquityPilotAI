"""Provider router — selection, retry, fallback, caching and accounting.

Everything above this line asks for a completion; this decides who serves it.

Retry and fallback are distinct concerns and are handled separately:

* **Retry** covers transient failure of the *same* provider — a 5xx, a timeout,
  a rate limit. Exponential backoff, capped attempts.
* **Fallback** covers a provider being unusable — no key, repeated failure,
  a 4xx that will never succeed. Move to the next in the chain.

Conflating them produces the worst outcome: hammering a broken provider while a
healthy one sits idle.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import random
import time
from collections import deque
from dataclasses import dataclass, field, replace

from app.domain.ai.types import (
    CompletionRequest, CompletionResponse, NoProviderConfigured, ProviderError,
    RateLimitError, TokenUsage,
)
from app.services.ai.providers import claude, gemini, mock, openai, openrouter
from app.services.ai.providers.base import LLMProvider, ProviderConfig
from app.services.ai.providers.shapes import SHAPE_ADAPTERS

#: Vendor modules supplying default registry rows.
PROVIDER_MODULES = (openrouter, openai, claude, gemini)

#: Declared fallback order, most preferred first.
#:
#: An ordering that matters commercially should not be a side effect of an
#: import tuple, which is what it was before this constant existed.
#:
#: **Phase 1 reversal — OpenRouter now leads, Gemini follows.** The earlier
#: order put Gemini first, and in practice that meant the platform served
#: template prose: the Gemini free tier's daily generation quota is spent
#: within a handful of reports, every subsequent call returns 429 with a
#: QuotaFailure detail, and the chain fell through to the offline provider.
#: A provider that answers reliably belongs ahead of one that answers for the
#: first few requests of the day. Gemini is retained immediately behind it, so
#: an OpenRouter outage still reaches a live model before the template.
#:
#: A provider absent from this list still works — it sorts after everything
#: named here — so adding a vendor module does not require editing the order
#: unless it needs a specific position.
FALLBACK_ORDER: tuple[str, ...] = ("OpenRouter", "Gemini", "OpenAI", "Claude")

MAX_ATTEMPTS = 3
BASE_BACKOFF_SECONDS = 0.5

#: Retries spent on a provider whose quota is exhausted, as opposed to one
#: that is briefly busy.
#:
#: A 429 carrying a daily-quota violation will still be a 429 in four seconds.
#: Retrying it three times costs the user eight seconds of latency and reaches
#: the same conclusion, so quota exhaustion falls through to the next provider
#: immediately. A plain rate limit — too many requests this minute — is still
#: worth retrying, because it clears.
QUOTA_EXHAUSTED_ATTEMPTS = 1
#: Completions are cached for this long. Research answers are expensive and
#: deterministic enough at low temperature that repeat questions should not
#: cost twice.
CACHE_TTL_SECONDS = 900
CACHE_MAX_ENTRIES = 256


@dataclass
class UsageLedger:
    """Running token and cost accounting, per provider."""

    calls: int = 0
    cached_hits: int = 0
    failures: int = 0
    fallbacks: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    by_provider: dict[str, dict[str, float]] = field(default_factory=dict)
    latencies_ms: deque[float] = field(default_factory=lambda: deque(maxlen=200))

    def record(self, response: CompletionResponse) -> None:
        self.calls += 1
        self.prompt_tokens += response.usage.prompt_tokens
        self.completion_tokens += response.usage.completion_tokens
        self.cost_usd += response.cost_usd
        if response.cached:
            self.cached_hits += 1
        else:
            self.latencies_ms.append(response.latency_ms)
        if response.fell_back_from:
            self.fallbacks += 1

        bucket = self.by_provider.setdefault(
            response.provider,
            {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0},
        )
        bucket["calls"] += 1
        bucket["prompt_tokens"] += response.usage.prompt_tokens
        bucket["completion_tokens"] += response.usage.completion_tokens
        bucket["cost_usd"] += response.cost_usd

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def p50_latency_ms(self) -> float:
        if not self.latencies_ms:
            return 0.0
        ordered = sorted(self.latencies_ms)
        return ordered[len(ordered) // 2]

    def snapshot(self) -> dict:
        return {
            "calls": self.calls,
            "cached_hits": self.cached_hits,
            "failures": self.failures,
            "fallbacks": self.fallbacks,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": round(self.cost_usd, 6),
            "p50_latency_ms": round(self.p50_latency_ms, 1),
            "by_provider": {
                k: {**v, "cost_usd": round(v["cost_usd"], 6)}
                for k, v in self.by_provider.items()
            },
        }


class ResponseCache:
    """Small TTL cache keyed on the exact request."""

    def __init__(self, ttl: float = CACHE_TTL_SECONDS, capacity: int = CACHE_MAX_ENTRIES):
        self.ttl = ttl
        self.capacity = capacity
        self._entries: dict[str, tuple[float, CompletionResponse]] = {}

    @staticmethod
    def key(request: CompletionRequest, provider: str) -> str:
        payload = json.dumps(
            {
                "provider": provider,
                "model": request.model,
                "temperature": request.temperature,
                "max_tokens": request.max_tokens,
                "messages": [(m.role.value, m.content) for m in request.messages],
            },
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    def get(self, key: str) -> CompletionResponse | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        stored_at, response = entry
        if time.time() - stored_at > self.ttl:
            self._entries.pop(key, None)
            return None
        return response

    def put(self, key: str, response: CompletionResponse) -> None:
        if len(self._entries) >= self.capacity:
            oldest = min(self._entries, key=lambda k: self._entries[k][0])
            self._entries.pop(oldest, None)
        self._entries[key] = (time.time(), response)

    def clear(self) -> None:
        self._entries.clear()


class ProviderRouter:
    """Chooses a provider, retries transient failures, falls back on hard ones."""

    def __init__(
        self,
        configs: list[ProviderConfig] | None = None,
        *,
        preferred: str | None = None,
        cache: ResponseCache | None = None,
        ledger: UsageLedger | None = None,
    ) -> None:
        self.configs = configs if configs is not None else self.default_configs()
        self.preferred = preferred
        self.cache = cache if cache is not None else ResponseCache()
        self.ledger = ledger or UsageLedger()

    # ------------------------------------------------------------- registry
    @staticmethod
    def default_configs() -> list[ProviderConfig]:
        """Registry rows from the vendor modules, keys injected from settings."""
        from app.core.config import settings

        keys = {
            "OpenRouter": getattr(settings, "OPENROUTER_API_KEY", None),
            "OpenAI": getattr(settings, "OPENAI_API_KEY", None),
            "Claude": getattr(settings, "ANTHROPIC_API_KEY", None),
            "Gemini": getattr(settings, "GEMINI_API_KEY", None),
        }
        out: list[ProviderConfig] = []
        # The offline provider is appended last so any live provider outranks
        # it; it exists so the layer degrades to grounded output rather than
        # to an error when no key is present.
        if settings.AI_MOCK_MODE:
            out.append(mock.DEFAULTS)
        for module in PROVIDER_MODULES:
            base = module.DEFAULTS
            # A vendor module may declare deployment-supplied fields (model,
            # attribution headers). Merged here rather than read inside the
            # module so the router remains the only place a registry row is
            # assembled, and the API key never leaves this function.
            overrides = {}
            if hasattr(module, "overrides"):
                overrides = module.overrides(settings)
            out.append(replace(
                base, api_key=keys.get(base.name), **overrides,
            ))
        return out

    def build(self, config: ProviderConfig) -> LLMProvider:
        if config.payload_shape == "offline":
            return mock.OfflineProvider(config)
        adapter = SHAPE_ADAPTERS.get(config.payload_shape)
        if adapter is None:
            raise ProviderError(
                f"no adapter for payload shape '{config.payload_shape}'",
                provider=config.name,
            )
        return adapter(config)

    def chain(self, preferred: str | None = None) -> list[ProviderConfig]:
        """Configured providers in fallback order, preferred first.

        Three sorts, applied least significant first, because Python's sort is
        stable and this reads far better than one compound key:

        1. `FALLBACK_ORDER` — the declared preference, Gemini then OpenRouter.
        2. offline last — a live provider always outranks the mock, so the
           platform never silently serves template output when a real model
           was available.
        3. explicit preference — a caller naming a provider gets it first.
        """
        wanted = (preferred or self.preferred or "").lower()
        usable = [c for c in self.configs if c.configured]

        def rank(config: ProviderConfig) -> int:
            try:
                return FALLBACK_ORDER.index(config.name)
            except ValueError:
                return len(FALLBACK_ORDER)

        usable.sort(key=rank)
        usable.sort(key=lambda c: 1 if c.payload_shape == "offline" else 0)
        if wanted:
            usable.sort(key=lambda c: 0 if c.name.lower() == wanted else 1)
        return usable

    @property
    def available(self) -> list[str]:
        return [c.name for c in self.configs if c.configured]

    # -------------------------------------------------------------- calling
    async def complete(
        self,
        request: CompletionRequest,
        *,
        preferred: str | None = None,
        use_cache: bool = True,
    ) -> CompletionResponse:
        chain = self.chain(preferred)
        if not chain:
            raise NoProviderConfigured(
                "No AI provider is configured. Set an API key for OpenRouter, "
                "OpenAI, Claude or Gemini to enable the AI layer."
            )

        first_choice = chain[0].name
        last_error: ProviderError | None = None

        for config in chain:
            cache_key = self.cache.key(request, config.name)
            if use_cache:
                hit = self.cache.get(cache_key)
                if hit is not None:
                    cached = CompletionResponse(
                        content=hit.content, provider=hit.provider, model=hit.model,
                        usage=hit.usage, latency_ms=0.0, cost_usd=0.0,
                        finish_reason=hit.finish_reason, cached=True,
                    )
                    self.ledger.record(cached)
                    return cached

            provider = self.build(config)

            for attempt in range(1, MAX_ATTEMPTS + 1):
                try:
                    response = await provider.complete(request)
                except RateLimitError as exc:
                    last_error = exc
                    self.ledger.failures += 1
                    # An exhausted quota will not clear within a request, so
                    # fall through to the next provider now rather than
                    # sleeping twice to learn the same thing.
                    ceiling = (
                        QUOTA_EXHAUSTED_ATTEMPTS
                        if getattr(exc, "quota_exhausted", False)
                        else MAX_ATTEMPTS
                    )
                    if attempt >= ceiling:
                        break
                    await asyncio.sleep(min(exc.retry_after, 8.0))
                    continue
                except ProviderError as exc:
                    last_error = exc
                    self.ledger.failures += 1
                    if not exc.retryable or attempt == MAX_ATTEMPTS:
                        break
                    # exponential backoff with jitter, so parallel callers
                    # do not retry in lockstep
                    delay = BASE_BACKOFF_SECONDS * (2 ** (attempt - 1))
                    await asyncio.sleep(delay + random.uniform(0, 0.25))
                    continue

                if config.name != first_choice:
                    response = CompletionResponse(
                        content=response.content, provider=response.provider,
                        model=response.model, usage=response.usage,
                        latency_ms=response.latency_ms, cost_usd=response.cost_usd,
                        finish_reason=response.finish_reason,
                        fell_back_from=first_choice,
                    )
                if use_cache:
                    self.cache.put(cache_key, response)
                self.ledger.record(response)
                return response

        raise last_error or ProviderError("all providers failed")

    async def stream(
        self, request: CompletionRequest, *, preferred: str | None = None
    ):
        """Stream from the first provider that can serve, falling back.

        The original took `chain[0]` and streamed from it unconditionally, so
        a primary that was merely out of quota broke streaming outright while
        `complete()` on the same chain fell back happily. Found when a live
        Gemini key with a spent daily allowance turned the chat endpoint into
        a 500 — the non-streaming path had been fixed and this one had not.

        Fallback here is coarser than in `complete()` by necessity: once a
        token has been yielded the response is on the wire and cannot be
        retracted, so a provider is only abandoned if it fails **before**
        producing anything.
        """
        chain = self.chain(preferred)
        if not chain:
            raise NoProviderConfigured("No AI provider is configured.")

        last_error: ProviderError | None = None
        for config in chain:
            provider = self.build(config)
            started_streaming = False
            try:
                async for token in provider.stream(request):
                    started_streaming = True
                    yield token
                return
            except ProviderError as exc:
                last_error = exc
                self.ledger.failures += 1
                if started_streaming:
                    # Mid-stream failure: the client already has a partial
                    # answer, and restarting on another provider would splice
                    # two different answers together. Fail honestly instead.
                    raise
                self.ledger.fallbacks += 1
                continue

        raise last_error or ProviderError("all providers failed to stream")
