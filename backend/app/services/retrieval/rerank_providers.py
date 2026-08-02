"""Cross-encoder rerank providers, selected by environment variable alone.

Four providers, three hosted and one local:

    RERANK_PROVIDER=jina    RERANK_API_KEY=...  jina-reranker-v2-base-multilingual
    RERANK_PROVIDER=cohere  RERANK_API_KEY=...  rerank-multilingual-v3.0
    RERANK_PROVIDER=openai  RERANK_API_KEY=...  gpt-4o-mini as a judge
    RERANK_PROVIDER=local   (no key)            sentence-transformers cross-encoder
    RERANK_PROVIDER=none / unset                lexical-coverage fallback

All answer the same `Reranker` interface, so switching one for another is a
deployment change and nothing else recompiles.

Two deserve a caveat rather than a claim.

**OpenAI is not a cross-encoder.** It is an LLM scoring query-passage pairs
through a prompt. A legitimate reranking strategy, and noticeably slower and
dearer than a real cross-encoder, so it is offered rather than recommended.

**Local needs `sentence-transformers` and roughly 1.3 GB resident** for
`bge-reranker-large`. The production container has 1 GB and has already been
killed three times under memory pressure, so it is implemented and
deliberately not enabled here. It exists for a deployment with the headroom.

No hosted provider has been exercised from this deployment: no rerank
endpoint reachable here accepts the credentials available. The wiring is
tested against recorded response shapes, stated plainly rather than implied.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, ClassVar, Sequence

import structlog

from app.services.retrieval.rerank import (
    LexicalCoverageReranker, RerankCandidate, Reranker, RerankScore,
)

log = structlog.get_logger(__name__)


class _HTTPReranker(Reranker):
    """Shared transport for the hosted rerank APIs."""

    endpoint: ClassVar[str] = ""
    default_model: ClassVar[str] = ""
    attempts: ClassVar[int] = 2
    #: Terminal states — retrying a 401 or 402 cannot succeed.
    _TERMINAL: ClassVar[frozenset[int]] = frozenset({400, 401, 402, 403, 404})

    def __init__(
        self,
        api_key: str | None,
        model: str | None = None,
        *,
        timeout: float = 30.0,
        endpoint: str | None = None,
    ) -> None:
        self.api_key = api_key
        self.model = model or self.default_model
        self.timeout = timeout
        self.endpoint = endpoint or self.endpoint
        #: Same reasoning as the embedding circuit breaker (RETR-002): a
        #: standing failure must not cost every query a retry ladder.
        self._tripped_until = 0.0

    @property
    def available(self) -> bool:
        return bool(self.api_key and self.endpoint)

    def _payload(self, query: str, documents: list[str]) -> dict[str, Any]:
        return {"model": self.model, "query": query, "documents": documents}

    def _parse(
        self, payload: dict[str, Any], candidates: Sequence[RerankCandidate],
    ) -> list[RerankScore]:
        raise NotImplementedError

    def rerank(
        self, query: str, candidates: Sequence[RerankCandidate],
    ) -> list[RerankScore]:
        if not self.available:
            raise RuntimeError(f"{self.name} reranker is not configured")
        if time.monotonic() < self._tripped_until:
            raise RuntimeError(f"{self.name} circuit open")
        if not candidates:
            return []

        body = json.dumps(
            self._payload(query, [c.text for c in candidates])
        ).encode()
        last: Exception | None = None

        for attempt in range(1, self.attempts + 1):
            request = urllib.request.Request(
                self.endpoint, data=body,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
            )
            try:
                with urllib.request.urlopen(request,
                                            timeout=self.timeout) as response:
                    return self._parse(json.load(response), candidates)
            except urllib.error.HTTPError as exc:
                last = exc
                if exc.code in self._TERMINAL:
                    self._tripped_until = time.monotonic() + 300.0
                    log.warning("reranker unavailable", provider=self.name,
                                code=exc.code)
                    break
            except Exception as exc:  # noqa: BLE001
                last = exc
            if attempt < self.attempts:
                time.sleep(1.0 * attempt)

        raise RuntimeError(f"{self.name} rerank failed: {str(last)[:160]}")


class JinaReranker(_HTTPReranker):
    name: ClassVar[str] = "jina"
    endpoint: ClassVar[str] = "https://api.jina.ai/v1/rerank"
    default_model: ClassVar[str] = "jina-reranker-v2-base-multilingual"

    def _parse(self, payload, candidates):
        out = [
            RerankScore(candidates[row["index"]].chunk_id,
                        float(row.get("relevance_score", 0.0)))
            for row in payload.get("results", [])
            if 0 <= int(row.get("index", -1)) < len(candidates)
        ]
        out.sort(key=lambda s: -s.score)
        return out


class CohereReranker(_HTTPReranker):
    name: ClassVar[str] = "cohere"
    endpoint: ClassVar[str] = "https://api.cohere.ai/v2/rerank"
    default_model: ClassVar[str] = "rerank-multilingual-v3.0"

    def _payload(self, query, documents):
        # `top_n` is explicit: Cohere returns everything without it, which is
        # the same set here but leaves the intent unstated.
        return {"model": self.model, "query": query,
                "documents": documents, "top_n": len(documents)}

    def _parse(self, payload, candidates):
        out = [
            RerankScore(candidates[row["index"]].chunk_id,
                        float(row.get("relevance_score", 0.0)))
            for row in payload.get("results", [])
            if 0 <= int(row.get("index", -1)) < len(candidates)
        ]
        out.sort(key=lambda s: -s.score)
        return out


class OpenAIJudgeReranker(_HTTPReranker):
    """LLM-as-judge. Not a cross-encoder — see the module docstring."""

    name: ClassVar[str] = "openai"
    endpoint: ClassVar[str] = "https://api.openai.com/v1/chat/completions"
    default_model: ClassVar[str] = "gpt-4o-mini"

    def _payload(self, query, documents):
        listing = "\n\n".join(
            f"[{i}] {text[:1200]}" for i, text in enumerate(documents)
        )
        return {
            "model": self.model,
            "temperature": 0.0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": (
                    "You score how well each passage answers the question. "
                    'Reply with JSON {"scores":[{"index":int,'
                    '"score":0.0-1.0}]} and nothing else.'
                )},
                {"role": "user", "content":
                 f"QUESTION: {query}\n\nPASSAGES:\n{listing}"},
            ],
        }

    def _parse(self, payload, candidates):
        content = payload["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        out: list[RerankScore] = []
        for row in parsed.get("scores", []):
            index = int(row.get("index", -1))
            if 0 <= index < len(candidates):
                out.append(RerankScore(candidates[index].chunk_id,
                                       float(row.get("score", 0.0))))
        out.sort(key=lambda s: -s.score)
        return out


class LocalCrossEncoderReranker(Reranker):
    """`sentence-transformers` cross-encoder, in-process.

    The highest-quality option and the one this deployment cannot run:
    `bge-reranker-large` needs roughly 1.3 GB resident against a 1 GB
    container already killed three times under memory pressure.

    Loaded lazily, so importing this module costs nothing where it is unused.
    """

    name: ClassVar[str] = "local"

    def __init__(self, model: str | None = None) -> None:
        self.model_name = model or "BAAI/bge-reranker-large"
        self._model: Any = None

    @property
    def available(self) -> bool:
        try:
            import sentence_transformers  # noqa: F401
        except Exception:  # noqa: BLE001
            return False
        return True

    def _load(self):
        if self._model is None:
            from sentence_transformers import CrossEncoder

            log.info("loading local cross-encoder", model=self.model_name)
            self._model = CrossEncoder(self.model_name)
        return self._model

    def rerank(
        self, query: str, candidates: Sequence[RerankCandidate],
    ) -> list[RerankScore]:
        if not candidates:
            return []
        model = self._load()
        scores = model.predict([(query, c.text[:2000]) for c in candidates])
        out = [
            RerankScore(c.chunk_id, float(s))
            for c, s in zip(candidates, scores)
        ]
        out.sort(key=lambda s: -s.score)
        return out


PROVIDERS: dict[str, type] = {
    JinaReranker.name: JinaReranker,
    CohereReranker.name: CohereReranker,
    OpenAIJudgeReranker.name: OpenAIJudgeReranker,
    LocalCrossEncoderReranker.name: LocalCrossEncoderReranker,
}


def build_rerank_provider(settings: object | None = None) -> Reranker:
    """Resolve the reranker from settings, falling back to the local scorer.

    Falls back rather than failing: a misconfigured reranker must degrade
    retrieval quality, not take the endpoint down. The fallback is logged, so
    a deployment that believes it has Jina enabled can discover that it does
    not.
    """
    if settings is None:
        from app.core.config import settings as _settings
        settings = _settings

    name = (getattr(settings, "RERANK_PROVIDER", None)
            or os.environ.get("RERANK_PROVIDER") or "").strip().lower()
    if not name or name == "none":
        return LexicalCoverageReranker()

    provider_cls = PROVIDERS.get(name)
    if provider_cls is None:
        log.warning("unknown rerank provider; using lexical fallback",
                    requested=name, known=sorted(PROVIDERS))
        return LexicalCoverageReranker()

    model = getattr(settings, "RERANK_MODEL", None)
    if provider_cls is LocalCrossEncoderReranker:
        provider = provider_cls(model)
    else:
        provider = provider_cls(
            getattr(settings, "RERANK_API_KEY", None), model,
            endpoint=getattr(settings, "RERANK_ENDPOINT", None),
        )

    if not provider.available:
        log.warning("rerank provider configured but unavailable; "
                    "using lexical fallback", provider=name)
        return LexicalCoverageReranker()
    return provider
