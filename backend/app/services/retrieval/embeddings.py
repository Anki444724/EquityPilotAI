"""Semantic embedding providers, in the brief's preference order.

    1. BAAI/bge-m3            — served by OpenRouter, 1024 dimensions
    2. jina-embeddings-v3     — Jina's own API, needs JINA_API_KEY
    3. text-embedding-3-small — OpenAI, needs OPENAI_API_KEY

`bge-m3` is the default because it is the only one of the three reachable with
the credentials this deployment holds, and because it is genuinely
multilingual — which the brief requires and the other two are weaker at for
Devanagari.

Measured on the live endpoint before this module was written:

    paraphrase   "revenue grew"  vs "sales increased"      0.812
    unrelated    "revenue grew"  vs "the cat sat"          0.334
    cross-lingual EN question    vs the same in Hindi      0.860
    cross-lingual EN question    vs the same in Hinglish   0.797

The hashed n-gram embedder this replaces scores paraphrases near zero: it
matches characters, not meaning.

One number in that table justifies the whole hybrid design. "EN question vs an
unrelated EN question" scores 0.496, while "Hindi question vs the English
passage that answers it" scores 0.486. Dense similarity alone cannot separate
those, so semantic retrieval is fused with BM25 and reranked rather than
trusted on its own.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar, Sequence

import structlog

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class EmbeddingSpec:
    """Identity of an embedding space.

    Stored beside every vector. Two vectors from different spaces produce a
    cosine that is arithmetically valid and entirely meaningless, so the store
    refuses to mix them — the same discipline the previous engine applied, and
    the reason the 384-dimension hashed vectors can coexist with these 1024s
    during migration rather than corrupting each other.
    """

    provider: str
    model: str
    dimension: int

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.model}:{self.dimension}"


class SemanticEmbeddingProvider(ABC):
    name: ClassVar[str] = "abstract"

    @property
    @abstractmethod
    def spec(self) -> EmbeddingSpec: ...

    @abstractmethod
    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]

    @property
    def available(self) -> bool:
        return True


class _HTTPEmbeddingProvider(SemanticEmbeddingProvider):
    """Shared transport for the OpenAI-compatible embedding APIs.

    All three preferred providers speak the same request and response shape,
    so the retry and batching live here once.
    """

    endpoint: ClassVar[str] = ""
    model: ClassVar[str] = ""
    dimension: ClassVar[int] = 1024
    #: Requests are batched; too large a batch risks a provider-side limit and
    #: costs the whole batch on a retry.
    batch_size: ClassVar[int] = 16
    #: Ceiling on the ESTIMATED tokens in one request.
    #:
    #: Measured, not guessed. On the live endpoint a 16-chunk batch of real
    #: filing text (5,076 tokens) succeeds and a 32-chunk batch returns
    #: HTTP 402 "Payment Required" — with the account showing 19,999.81
    #: remaining. The 402 is a ceiling on the request's estimated cost, not a
    #: statement about the balance: exactly the reservation-not-spend
    #: behaviour recorded as SUMMARY-001 for completions.
    #:
    #: Batches are therefore split by estimated tokens as well as by count, so
    #: one unusually long chunk cannot push an otherwise legal batch over.
    max_batch_tokens: ClassVar[int] = 6000
    attempts: ClassVar[int] = 3

    #: How long the provider stays tripped after a hard failure.
    #:
    #: RETR-002. Without this, every query paid the full retry ladder
    #: (1.5s + 4.5s) before falling back to lexical, because the credit
    #: exhaustion that stops one query stops all of them. Measured: retrieval
    #: took 6,269ms per query against 63ms for the lexical signal alone —
    #: 99% of the latency was waiting for an endpoint already known to be
    #: down. A retry ladder is right for a transient timeout and wrong for a
    #: state that will not change within a request.
    CIRCUIT_SECONDS: ClassVar[float] = 300.0

    #: Failures that are a standing state rather than a blip. Retrying a 402
    #: is pointless: the account will not acquire credit between attempts.
    _TERMINAL_CODES: ClassVar[frozenset[int]] = frozenset({401, 402, 403})

    def __init__(self, api_key: str | None, *, timeout: float = 60.0) -> None:
        self.api_key = api_key
        self.timeout = timeout
        self._spec = EmbeddingSpec(self.name, self.model, self.dimension)
        self._tripped_until: float = 0.0

    @property
    def spec(self) -> EmbeddingSpec:
        return self._spec

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _post(self, batch: Sequence[str]) -> list[list[float]]:
        if time.monotonic() < self._tripped_until:
            raise RuntimeError(
                f"{self.name} circuit open until "
                f"{self._tripped_until - time.monotonic():.0f}s from now"
            )

        body = json.dumps({"model": self.model, "input": list(batch)}).encode()
        last: Exception | None = None

        for attempt in range(1, self.attempts + 1):
            request = urllib.request.Request(
                self.endpoint, data=body, headers=self._headers(),
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    payload = json.load(response)
                if "error" in payload:
                    raise RuntimeError(str(payload["error"])[:200])
                rows = sorted(payload["data"], key=lambda d: d["index"])
                return [row["embedding"] for row in rows]
            except urllib.error.HTTPError as exc:
                last = exc
                if exc.code in self._TERMINAL_CODES:
                    # Standing state, not a blip. Trip the breaker so the rest
                    # of this request — and the next five minutes of them —
                    # degrade to lexical immediately instead of each paying
                    # the full ladder.
                    self._tripped_until = time.monotonic() + self.CIRCUIT_SECONDS
                    log.warning("embedding provider unavailable",
                                provider=self.name, code=exc.code,
                                cooldown_s=self.CIRCUIT_SECONDS)
                    break
                if attempt >= self.attempts:
                    break
            except Exception as exc:  # noqa: BLE001
                last = exc
                if attempt >= self.attempts:
                    break
                # Exponential, same reasoning as the NSE provider: a rate
                # limit is the common failure and retrying immediately just
                # spends the remaining budget.
                delay = 1.5 * (3.0 ** (attempt - 1))
                log.info("embedding retry", provider=self.name,
                         attempt=attempt, delay=delay, error=str(exc)[:120])
                time.sleep(delay)

        raise RuntimeError(
            f"{self.name} embeddings failed after {self.attempts} attempts: "
            f"{str(last)[:200]}"
        )

    def _batches(self, texts: Sequence[str]) -> list[list[str]]:
        """Split by BOTH count and estimated tokens.

        ~4 characters per token is the usual English approximation and is
        conservative for Devanagari, which tokenises more densely — erring
        toward smaller batches is the safe direction when the failure mode is
        a rejected request.
        """
        batches: list[list[str]] = []
        current: list[str] = []
        tokens = 0
        for item in texts:
            estimate = max(1, len(item) // 4)
            over_tokens = tokens + estimate > self.max_batch_tokens
            if current and (over_tokens or len(current) >= self.batch_size):
                batches.append(current)
                current, tokens = [], 0
            current.append(item)
            tokens += estimate
        if current:
            batches.append(current)
        return batches

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not self.available:
            raise RuntimeError(f"{self.name} requires an API key")
        out: list[list[float]] = []
        for batch in self._batches(texts):
            out.extend(self._post(batch))
        return out


class BGEM3Provider(_HTTPEmbeddingProvider):
    """BAAI/bge-m3 via OpenRouter. First preference."""

    name: ClassVar[str] = "bge-m3"
    endpoint: ClassVar[str] = "https://openrouter.ai/api/v1/embeddings"
    model: ClassVar[str] = "baai/bge-m3"
    dimension: ClassVar[int] = 1024


class JinaV3Provider(_HTTPEmbeddingProvider):
    """jina-embeddings-v3. Second preference; needs JINA_API_KEY."""

    name: ClassVar[str] = "jina-v3"
    endpoint: ClassVar[str] = "https://api.jina.ai/v1/embeddings"
    model: ClassVar[str] = "jina-embeddings-v3"
    dimension: ClassVar[int] = 1024


class OpenAISmallProvider(_HTTPEmbeddingProvider):
    """text-embedding-3-small. Fallback; needs OPENAI_API_KEY."""

    name: ClassVar[str] = "openai-small"
    endpoint: ClassVar[str] = "https://api.openai.com/v1/embeddings"
    model: ClassVar[str] = "text-embedding-3-small"
    dimension: ClassVar[int] = 1536


#: Preference order exactly as the brief specifies.
PROVIDER_ORDER: tuple[type[_HTTPEmbeddingProvider], ...] = (
    BGEM3Provider, JinaV3Provider, OpenAISmallProvider,
)

_KEY_SETTINGS: dict[str, tuple[str, ...]] = {
    BGEM3Provider.name: ("OPENROUTER_API_KEY",),
    JinaV3Provider.name: ("JINA_API_KEY",),
    OpenAISmallProvider.name: ("OPENAI_API_KEY",),
}


def build_semantic_embedder(
    settings: object | None = None, *, preferred: str | None = None,
) -> SemanticEmbeddingProvider | None:
    """First configured provider in preference order, or None.

    Returns None rather than falling back to the hashed embedder. The caller
    decides what to do without semantics, and a silent downgrade to a
    lexical-only index that still calls itself semantic is exactly the kind of
    quiet degradation this platform refuses.
    """
    if settings is None:
        from app.core.config import settings as _settings
        settings = _settings

    ordered = list(PROVIDER_ORDER)
    if preferred:
        ordered.sort(key=lambda cls: 0 if cls.name == preferred else 1)

    for provider_cls in ordered:
        for setting_name in _KEY_SETTINGS.get(provider_cls.name, ()):
            key = getattr(settings, setting_name, None)
            if key:
                return provider_cls(key)
    return None
