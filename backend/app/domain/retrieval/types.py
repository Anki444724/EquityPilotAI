"""Retrieval Engine 2.0 — domain types and fusion rules.

The engine this replaces scored every chunk in a company's corpus in Python,
against vectors produced by hashed character n-grams. It worked, and its own
docstring was honest about the limit: *"captures lexical and morphological
similarity well and semantic paraphrase poorly"*. A question phrased
differently from the filing did not retrieve it, and a question in Hindi
retrieved nothing at all.

This module holds the rules. The stores and providers live in `services`.

Four retrieval signals, combined
--------------------------------
Semantic, lexical (BM25), metadata and temporal. They are fused with
**Reciprocal Rank Fusion** rather than a weighted sum of scores, and that
choice is the central design decision here.

A weighted sum requires the scores to be commensurable. They are not: BM25 is
unbounded and corpus-dependent, cosine sits in [-1, 1], and a metadata match
is a boolean. The previous engine normalised BM25 by the maximum score in the
result set, which makes a document's rank depend on which other documents
happened to be retrieved — the same chunk scores differently for the same
query as the corpus grows.

RRF ranks each signal independently and fuses the ranks. It needs no
normalisation, no per-corpus tuning, and cannot be destabilised by one signal
having an unusual scale.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class RetrievalSignal(StrEnum):
    """The four retrieval signals the brief requires."""

    SEMANTIC = "semantic"
    LEXICAL = "lexical"
    METADATA = "metadata"
    TEMPORAL = "temporal"


#: Weight applied to each signal's RRF contribution.
#:
#: Semantic leads now that the embeddings are real — the reverse of the old
#: engine, which weighted lexical 0.55 to 0.45 precisely because its vectors
#: were not semantic. Metadata and temporal are deliberately small: they are
#: *filters that should nudge*, not rank a passage above one that answers the
#: question.
SIGNAL_WEIGHTS: dict[RetrievalSignal, float] = {
    RetrievalSignal.SEMANTIC: 1.0,
    RetrievalSignal.LEXICAL: 0.8,
    RetrievalSignal.METADATA: 0.3,
    RetrievalSignal.TEMPORAL: 0.3,
}

#: RRF damping constant. 60 is the value from Cormack et al. (2009) and is
#: kept rather than tuned: it flattens the contribution of rank-1 relative to
#: rank-2, which is what stops a single confident signal dominating a fused
#: ranking.
RRF_K = 60

#: Candidates pulled from each signal before fusion. Wider than the final
#: `top_k` so a passage ranked poorly by one signal can still be rescued by
#: another — the entire point of fusing.
CANDIDATE_POOL = 40

#: How many fused candidates are reranked. Reranking is the most expensive
#: stage, and a passage outside the top 20 after fusion is not going to be
#: promoted to the top 5 by reordering.
RERANK_POOL = 20


@dataclass(slots=True)
class RetrievalResult:
    """One retrieved passage and everything needed to judge it.

    The brief asks for relevance score, confidence, source and chunk. All four
    are here, and `confidence` is deliberately NOT a copy of the score:
    a passage can rank first in a weak field, and a reader needs to know the
    difference between "best available" and "good".
    """

    chunk_id: int
    document_id: int
    text: str
    page: int
    paragraph: int
    section: str
    document_title: str
    #: Fused relevance, 0-1. Comparable within one query, not across queries.
    score: float
    #: How much to trust this result on its own terms, 0-1. Derived from
    #: signal agreement and absolute semantic similarity — see `confidence_of`.
    confidence: float
    #: Which signals retrieved it, and where each ranked it.
    signals: dict[str, int] = field(default_factory=dict)
    #: Per-signal raw scores, for auditing a ranking that looks wrong.
    raw: dict[str, float] = field(default_factory=dict)
    rerank_score: float | None = None
    source: str = ""
    metadata: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "chunk_id": self.chunk_id,
            "document_id": self.document_id,
            "text": self.text,
            "page": self.page,
            "paragraph": self.paragraph,
            "section": self.section,
            "document_title": self.document_title,
            "score": round(self.score, 6),
            "confidence": round(self.confidence, 4),
            "signals": self.signals,
            "raw": {k: round(v, 6) for k, v in self.raw.items()},
            "rerank_score": (
                round(self.rerank_score, 6)
                if self.rerank_score is not None else None
            ),
            "source": self.source,
            "metadata": self.metadata,
        }


def reciprocal_rank_fusion(
    rankings: dict[RetrievalSignal, list[int]],
    *,
    k: int = RRF_K,
) -> dict[int, tuple[float, dict[str, int]]]:
    """Fuse per-signal rankings into one score per chunk.

    `rankings` maps a signal to chunk ids in descending relevance. Returns
    chunk id -> (fused score, {signal: rank}).

    Rank-based rather than score-based on purpose: see the module docstring.
    """
    fused: dict[int, float] = {}
    provenance: dict[int, dict[str, int]] = {}

    for signal, ordered in rankings.items():
        weight = SIGNAL_WEIGHTS.get(signal, 1.0)
        for position, chunk_id in enumerate(ordered, start=1):
            fused[chunk_id] = fused.get(chunk_id, 0.0) + weight / (k + position)
            provenance.setdefault(chunk_id, {})[signal.value] = position

    return {
        chunk_id: (score, provenance.get(chunk_id, {}))
        for chunk_id, score in fused.items()
    }


def confidence_of(
    *,
    semantic: float,
    signal_count: int,
    rerank_score: float | None = None,
) -> float:
    """How much to trust one result, 0-1.

    Three inputs, and the reasoning for each:

    * **Absolute semantic similarity.** A cosine of 0.42 is the best match in
      a corpus that does not contain the answer. Rank cannot express that;
      absolute similarity can.
    * **Signal agreement.** A passage found by both the embedding and BM25 is
      more likely to be right than one found by either alone. Corroboration
      across independent methods is evidence in retrieval exactly as it is in
      research.
    * **Rerank score**, where a reranker ran. It has seen the query and the
      passage together, which neither retrieval signal has.

    Deliberately conservative. It is better to under-claim confidence in a
    good result than to over-claim it in a bad one, because the downstream
    consumer is an LLM that will state whatever it is given.
    """
    base = max(0.0, min(1.0, semantic))
    agreement = min(signal_count, 3) / 3.0

    if rerank_score is not None:
        # The reranker is the strongest signal available, but it is a local
        # heuristic unless a cross-encoder is configured, so it informs the
        # figure rather than replacing it.
        score = 0.45 * base + 0.25 * agreement + 0.30 * max(0.0, min(1.0, rerank_score))
    else:
        score = 0.65 * base + 0.35 * agreement

    return round(max(0.0, min(1.0, score)), 4)
