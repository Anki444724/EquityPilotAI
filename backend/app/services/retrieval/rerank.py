"""Reranking — a pluggable stage over the fused candidate list.

What the brief asked for and what is actually shipped
-----------------------------------------------------
The brief names `bge-reranker-large` and `jina-reranker-v2`. Neither is wired,
and the reason is a measured constraint rather than a preference:

* OpenRouter serves embeddings but lists **zero** rerank models
  (checked across all 337 models in its catalogue).
* `api.jina.ai/v1/rerank` and `api.cohere.ai/v1/rerank` both answer
  "authentication required"; this deployment holds neither key.
* A cross-encoder cannot be hosted here. `bge-reranker-large` is ~1.3 GB of
  weights plus a torch runtime, against a production container with 1 GB of
  RAM that has already crashed three times under memory pressure.

So this module ships the *interface* plus a dependency-free local reranker,
and adding a real cross-encoder later is a configuration change rather than a
rewrite. That is stated plainly instead of quietly substituting a weaker
component and calling it reranking.

What the local reranker actually does
-------------------------------------
It is a lexical-overlap scorer, not a neural model, and it will not match a
cross-encoder on paraphrase. It is worth running because it sees something no
retrieval signal does: the query and the passage **together**, so it can
reward a passage that covers *all* of the query's content terms over one that
matches a single high-idf term very strongly.
"""

from __future__ import annotations

import math
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar, Sequence

import structlog

log = structlog.get_logger(__name__)

_TOKEN = re.compile(r"[\w\u0900-\u097F]+", re.UNICODE)

#: Words that carry no retrieval signal. Kept short and English-only on
#: purpose: an aggressive stop list strips the content words of a Hinglish
#: query, and the brief requires Hinglish to work.
_STOP = frozenset("""
a an the of and or in on at to for from by with is are was were be been being
what which who whom whose how why when where does do did has have had this
that these those it its as if then than so such about into over under
""".split())


def _tokens(text: str) -> list[str]:
    return [t for t in _TOKEN.findall((text or "").lower()) if t not in _STOP]


@dataclass(slots=True)
class RerankCandidate:
    chunk_id: int
    text: str


@dataclass(slots=True)
class RerankScore:
    chunk_id: int
    score: float
    detail: str = ""


class Reranker(ABC):
    """Reorders candidates given the query and the passages together."""

    name: ClassVar[str] = "abstract"

    @property
    def available(self) -> bool:
        return True

    @abstractmethod
    def rerank(
        self, query: str, candidates: Sequence[RerankCandidate],
    ) -> list[RerankScore]: ...


class LexicalCoverageReranker(Reranker):
    """Local reranker: term coverage, density and proximity.

    Three components, each addressing a specific failure of rank fusion:

    * **Coverage** — what share of the query's content terms appear at all.
      Fusion happily ranks a passage first for matching one term intensely;
      a passage answering the whole question should outrank it.
    * **Density** — matches per unit length. A 2,000-character page that
      mentions the term once is a worse answer than a sentence about it.
    * **Proximity** — how close the matched terms are. Terms scattered across
      a page are usually coincidence; adjacent ones are usually the answer.

    No dependencies, microseconds per candidate, and deterministic — which
    also makes the benchmark reproducible.
    """

    name: ClassVar[str] = "lexical-coverage"

    COVERAGE_WEIGHT = 0.55
    DENSITY_WEIGHT = 0.25
    PROXIMITY_WEIGHT = 0.20

    def rerank(
        self, query: str, candidates: Sequence[RerankCandidate],
    ) -> list[RerankScore]:
        query_terms = set(_tokens(query))
        if not query_terms:
            return [RerankScore(c.chunk_id, 0.0) for c in candidates]

        out: list[RerankScore] = []
        for candidate in candidates:
            tokens = _tokens(candidate.text)
            if not tokens:
                out.append(RerankScore(candidate.chunk_id, 0.0))
                continue

            positions = [i for i, t in enumerate(tokens) if t in query_terms]
            matched = {tokens[i] for i in positions}

            coverage = len(matched) / len(query_terms)
            # Saturating: 10% term density is already a strong signal, and a
            # linear ratio would let a three-word chunk beat a real answer.
            density = 1.0 - math.exp(-10.0 * (len(positions) / len(tokens)))

            if len(positions) > 1:
                span = positions[-1] - positions[0] + 1
                proximity = len(positions) / span
            else:
                proximity = 1.0 if positions else 0.0

            score = (
                self.COVERAGE_WEIGHT * coverage
                + self.DENSITY_WEIGHT * density
                + self.PROXIMITY_WEIGHT * proximity
            )
            out.append(RerankScore(
                candidate.chunk_id, round(score, 6),
                detail=f"coverage={coverage:.2f} density={density:.2f} "
                       f"proximity={proximity:.2f}",
            ))

        out.sort(key=lambda s: (-s.score, s.chunk_id))
        return out


class CrossEncoderReranker(Reranker):
    """HTTP cross-encoder — `bge-reranker-large` or `jina-reranker-v2`.

    Present so the abstraction is real rather than notional, and so enabling a
    reranker is a key plus a setting. It has NEVER been exercised against a
    live endpoint from this deployment: no provider reachable here serves a
    rerank API. That is stated rather than implied by its existence.
    """

    name: ClassVar[str] = "cross-encoder"

    def __init__(
        self,
        endpoint: str,
        model: str,
        api_key: str | None,
        *,
        timeout: float = 30.0,
    ) -> None:
        self.endpoint = endpoint
        self.model = model
        self.api_key = api_key
        self.timeout = timeout

    @property
    def available(self) -> bool:
        return bool(self.api_key and self.endpoint)

    def rerank(
        self, query: str, candidates: Sequence[RerankCandidate],
    ) -> list[RerankScore]:
        if not self.available:
            raise RuntimeError("cross-encoder reranker is not configured")
        import json
        import urllib.request

        body = json.dumps({
            "model": self.model,
            "query": query,
            "documents": [c.text for c in candidates],
        }).encode()
        request = urllib.request.Request(
            self.endpoint, data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            payload = json.load(response)

        # Both vendors return {results: [{index, relevance_score}]}.
        scores = [
            RerankScore(
                candidates[row["index"]].chunk_id,
                float(row.get("relevance_score", 0.0)),
            )
            for row in payload.get("results", [])
        ]
        scores.sort(key=lambda s: -s.score)
        return scores


def build_reranker(settings: object | None = None) -> Reranker:
    """The configured reranker, falling back to the local one.

    Falls back rather than disabling reranking: the local scorer is weaker
    than a cross-encoder but strictly better than no reranking at all, and a
    deployment without a key should still get the coverage signal.
    """
    if settings is None:
        from app.core.config import settings as _settings
        settings = _settings

    endpoint = getattr(settings, "RERANKER_ENDPOINT", None)
    model = getattr(settings, "RERANKER_MODEL", None)
    key = getattr(settings, "RERANKER_API_KEY", None)
    if endpoint and model and key:
        return CrossEncoderReranker(endpoint, model, key)
    return LexicalCoverageReranker()
