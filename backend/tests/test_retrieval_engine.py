"""Retrieval Engine 2.0: fusion, intent, confidence, reranking, providers."""

from __future__ import annotations

import pytest

from app.domain.retrieval.types import (
    CANDIDATE_POOL, RERANK_POOL, RRF_K, SIGNAL_WEIGHTS, RetrievalSignal,
    confidence_of, reciprocal_rank_fusion,
)
from app.services.retrieval.embeddings import (
    PROVIDER_ORDER, BGEM3Provider, JinaV3Provider, OpenAISmallProvider,
    build_semantic_embedder,
)
from app.services.retrieval.engine import _content_terms, parse_intent
from app.services.retrieval.rerank import (
    LexicalCoverageReranker, RerankCandidate, build_reranker,
)


# =========================================================== rank fusion

def test_fusion_rewards_agreement_across_signals():
    """A chunk both signals rank second should beat one ranked first by only
    one — corroboration across independent methods is evidence."""
    fused = reciprocal_rank_fusion({
        RetrievalSignal.SEMANTIC: [10, 20],
        RetrievalSignal.LEXICAL: [30, 20],
    })
    assert fused[20][0] > fused[10][0]
    assert fused[20][0] > fused[30][0]


def test_fusion_records_which_signal_found_what():
    fused = reciprocal_rank_fusion({
        RetrievalSignal.SEMANTIC: [7],
        RetrievalSignal.LEXICAL: [7],
    })
    assert fused[7][1] == {"semantic": 1, "lexical": 1}


def test_fusion_needs_no_score_normalisation():
    """The reason RRF was chosen over a weighted sum.

    BM25 is unbounded and cosine is in [-1, 1]. The old engine normalised
    BM25 by the maximum in the result set, which made a chunk's rank depend
    on which other chunks happened to be retrieved. Rank fusion takes only
    the ordering, so a signal with an unusual scale cannot destabilise it.
    """
    tight = reciprocal_rank_fusion({RetrievalSignal.SEMANTIC: [1, 2, 3]})
    assert tight[1][0] > tight[2][0] > tight[3][0]
    # Only the order was supplied — no scores were involved at all.
    assert tight[1][0] == pytest.approx(
        SIGNAL_WEIGHTS[RetrievalSignal.SEMANTIC] / (RRF_K + 1)
    )


def test_semantic_outweighs_metadata():
    """Metadata is a nudge, not a ranker. A passage that answers the question
    must not be outranked by one that merely has the right fiscal year."""
    assert (SIGNAL_WEIGHTS[RetrievalSignal.SEMANTIC]
            > SIGNAL_WEIGHTS[RetrievalSignal.METADATA] * 2)


def test_candidate_pool_is_wider_than_the_rerank_pool():
    """Fusion must see more than it reranks, or a passage ranked poorly by
    one signal can never be rescued by another."""
    assert CANDIDATE_POOL > RERANK_POOL


# ============================================================== confidence

def test_confidence_is_not_a_copy_of_the_score():
    """A passage can rank first in a weak field. The reader needs to know the
    difference between "best available" and "good"."""
    weak = confidence_of(semantic=0.31, signal_count=1)
    strong = confidence_of(semantic=0.88, signal_count=3)
    assert weak < 0.4
    assert strong > 0.8


def test_agreement_raises_confidence():
    alone = confidence_of(semantic=0.6, signal_count=1)
    corroborated = confidence_of(semantic=0.6, signal_count=3)
    assert corroborated > alone


def test_confidence_stays_in_range():
    assert confidence_of(semantic=5.0, signal_count=9, rerank_score=9.0) <= 1.0
    assert confidence_of(semantic=-3.0, signal_count=0) >= 0.0


# ================================================================== intent

def test_fiscal_year_is_parsed_from_several_forms():
    assert parse_intent("revenue in FY26").fiscal_year == 2026
    assert parse_intent("revenue in FY2026").fiscal_year == 2026
    assert parse_intent("what happened in 2024").fiscal_year == 2024


def test_document_type_is_inferred_from_the_question():
    assert "conference_call" in parse_intent(
        "what did management say on the call?").doc_types
    assert "shareholding" in parse_intent(
        "what is the promoter pledge?").doc_types


def test_recency_words_enable_the_temporal_signal():
    assert parse_intent("latest guidance").wants_recent is True
    assert parse_intent("guidance in 2019").wants_recent is False


def test_intent_does_not_invent_filters():
    """A hint the question did not intend becomes a filter that hides the
    answer, so only unambiguous signals are taken."""
    intent = parse_intent("how is the business doing")
    assert intent.fiscal_year is None
    assert intent.doc_types == []
    assert intent.has_metadata is False


# ======================================== RETR-001: lexical query building

def test_content_terms_drop_english_stopwords():
    """Regression for RETR-001, found by the first benchmark run.

    `plainto_tsquery` AND-joins every term including stopwords, so
    "Who runs the company?" became 'who' & 'runs' & 'the' & 'company' and
    matched nothing — while 'director' alone matched four chunks. The lexical
    signal returned zero rows for 6 of 8 natural-language probes and
    paraphrase MRR collapsed to 0.06 against the legacy engine's 0.50.
    """
    terms = _content_terms("Who runs the company?")
    assert "the" not in terms
    assert "who" not in terms
    assert "company" in terms


def test_devanagari_survives_term_extraction():
    terms = _content_terms("कंपनी का राजस्व कितना है?")
    assert "कंपनी" in terms
    assert "राजस्व" in terms


def test_hinglish_content_words_are_not_stripped():
    """An aggressive stop list would remove "kya"/"hai" and leave nothing.
    The list is English-only for exactly this reason."""
    terms = _content_terms("company ka revenue kitna hai")
    assert "revenue" in terms
    assert "company" in terms
    assert len(terms) >= 3


def test_terms_are_deduplicated_and_bounded():
    terms = _content_terms("revenue revenue revenue " + " ".join(
        f"word{i}" for i in range(50)))
    assert terms.count("revenue") == 1
    assert len(terms) <= 24


# ================================================================ reranking

def test_reranker_prefers_full_query_coverage():
    """Fusion happily ranks a passage first for matching one term intensely.
    A passage answering the whole question should outrank it."""
    reranker = LexicalCoverageReranker()
    scores = reranker.rerank(
        "revenue growth margin",
        [
            RerankCandidate(1, "revenue revenue revenue revenue revenue"),
            RerankCandidate(2, "revenue growth and margin all improved"),
        ],
    )
    assert scores[0].chunk_id == 2


def test_reranker_prefers_density_over_length():
    reranker = LexicalCoverageReranker()
    scores = reranker.rerank(
        "cash flow",
        [
            RerankCandidate(1, "cash flow was strong"),
            RerankCandidate(2, "cash flow " + ("filler " * 300)),
        ],
    )
    assert scores[0].chunk_id == 1


def test_reranker_handles_an_empty_query():
    scores = LexicalCoverageReranker().rerank(
        "the of and", [RerankCandidate(1, "anything")])
    assert scores[0].score == 0.0


def test_build_reranker_falls_back_to_local():
    """Weaker than a cross-encoder, but strictly better than no reranking —
    and no reranker is reachable from this deployment."""

    class _Settings:
        RERANKER_ENDPOINT = None
        RERANKER_MODEL = None
        RERANKER_API_KEY = None

    assert isinstance(build_reranker(_Settings()), LexicalCoverageReranker)


def test_build_reranker_uses_a_cross_encoder_when_configured():
    from app.services.retrieval.rerank import CrossEncoderReranker

    class _Settings:
        RERANKER_ENDPOINT = "https://api.jina.ai/v1/rerank"
        RERANKER_MODEL = "jina-reranker-v2-base-multilingual"
        RERANKER_API_KEY = "k"

    assert isinstance(build_reranker(_Settings()), CrossEncoderReranker)


# ============================================================== embeddings

def test_provider_order_matches_the_brief():
    assert PROVIDER_ORDER == (
        BGEM3Provider, JinaV3Provider, OpenAISmallProvider,
    )


def test_bge_m3_is_1024_dimensions():
    assert BGEM3Provider("k").spec.dimension == 1024
    assert OpenAISmallProvider("k").spec.dimension == 1536


def test_a_provider_without_a_key_is_unavailable():
    assert BGEM3Provider(None).available is False
    assert BGEM3Provider("k").available is True


def test_builder_returns_none_rather_than_downgrading():
    """A silent fall back to the hashed embedder would leave a lexical-only
    index still calling itself semantic."""

    class _Settings:
        OPENROUTER_API_KEY = None
        JINA_API_KEY = None
        OPENAI_API_KEY = None

    assert build_semantic_embedder(_Settings()) is None


def test_builder_follows_the_preference_order():
    class _Settings:
        OPENROUTER_API_KEY = "a"
        JINA_API_KEY = "b"
        OPENAI_API_KEY = "c"

    assert isinstance(build_semantic_embedder(_Settings()), BGEM3Provider)


def test_builder_skips_an_unconfigured_first_choice():
    class _Settings:
        OPENROUTER_API_KEY = None
        JINA_API_KEY = None
        OPENAI_API_KEY = "c"

    assert isinstance(build_semantic_embedder(_Settings()), OpenAISmallProvider)


def test_batches_respect_both_count_and_token_limits():
    """Measured on the live endpoint: a 16-chunk batch of filing text
    succeeds and a 32-chunk batch is rejected. Splitting on count alone
    lets one unusually long chunk push a legal batch over."""
    provider = BGEM3Provider("k")
    long_batches = provider._batches(["x" * 3000] * 10)  # noqa: SLF001
    assert all(
        sum(len(t) // 4 for t in batch) <= provider.max_batch_tokens
        for batch in long_batches
    )
    short_batches = provider._batches(["short"] * 40)  # noqa: SLF001
    assert all(len(b) <= provider.batch_size for b in short_batches)


def test_embedding_spec_distinguishes_the_spaces():
    """Two vectors from different spaces produce a cosine that is
    arithmetically valid and meaningless."""
    assert BGEM3Provider("k").spec.key != OpenAISmallProvider("k").spec.key
