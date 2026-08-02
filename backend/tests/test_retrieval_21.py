"""Retrieval 2.1: provider abstraction, auto-backfill, no lexical regression."""

from __future__ import annotations

import importlib
import pkgutil

import pytest

import app.models as _models_pkg
from app.domain.platform.jobs import (
    DEFAULT_PRIORITY, JOB_LABELS, RETRY_POLICIES, SCHEDULES, JobKind,
)
from app.domain.retrieval.types import LEXICAL_DOMINANCE_RATIO
from app.services.retrieval.engine import HybridRetrievalEngine
from app.services.retrieval.rerank import (
    LexicalCoverageReranker, RerankCandidate, RerankScore,
)
from app.services.retrieval.rerank_providers import (
    PROVIDERS, CohereReranker, JinaReranker, LocalCrossEncoderReranker,
    OpenAIJudgeReranker, build_rerank_provider,
)

for _module in pkgutil.iter_modules(_models_pkg.__path__):
    importlib.import_module(f"app.models.{_module.name}")


class _Settings:
    """Minimal settings stand-in; every field the builder reads."""

    def __init__(self, **kw):
        self.RERANK_PROVIDER = kw.get("provider")
        self.RERANK_API_KEY = kw.get("key")
        self.RERANK_MODEL = kw.get("model")
        self.RERANK_ENDPOINT = kw.get("endpoint")


# ============================================ 3. provider abstraction

def test_all_four_providers_are_registered():
    assert set(PROVIDERS) == {"jina", "cohere", "openai", "local"}


@pytest.mark.parametrize(("name", "expected"), [
    ("jina", JinaReranker),
    ("cohere", CohereReranker),
    ("openai", OpenAIJudgeReranker),
])
def test_provider_is_selected_by_name_alone(name, expected):
    """Switching providers must be a deployment change and nothing else."""
    provider = build_rerank_provider(_Settings(provider=name, key="k"))
    assert isinstance(provider, expected)


def test_local_provider_needs_no_key():
    provider = build_rerank_provider(_Settings(provider="local"))
    # Available only where sentence-transformers is installed; either way it
    # must not fall back merely for lacking a key.
    assert isinstance(provider, (LocalCrossEncoderReranker,
                                 LexicalCoverageReranker))


def test_unset_provider_uses_the_lexical_fallback():
    assert isinstance(build_rerank_provider(_Settings()),
                      LexicalCoverageReranker)
    assert isinstance(build_rerank_provider(_Settings(provider="none")),
                      LexicalCoverageReranker)


def test_a_configured_provider_without_a_key_degrades_rather_than_fails():
    """A misconfigured reranker must lower retrieval quality, not take the
    endpoint down."""
    assert isinstance(build_rerank_provider(_Settings(provider="jina")),
                      LexicalCoverageReranker)


def test_an_unknown_provider_name_degrades():
    assert isinstance(build_rerank_provider(_Settings(provider="nope", key="k")),
                      LexicalCoverageReranker)


def test_endpoint_is_overridable_for_self_hosting():
    provider = build_rerank_provider(_Settings(
        provider="jina", key="k", endpoint="https://internal.example/rerank"))
    assert provider.endpoint == "https://internal.example/rerank"


def test_each_provider_has_a_sensible_default_model():
    assert "jina-reranker-v2" in JinaReranker("k").model
    assert "rerank-multilingual" in CohereReranker("k").model
    assert LocalCrossEncoderReranker().model_name.startswith("BAAI/")


def test_jina_response_shape_is_parsed():
    provider = JinaReranker("k")
    candidates = [RerankCandidate(11, "a"), RerankCandidate(22, "b")]
    scores = provider._parse(  # noqa: SLF001
        {"results": [{"index": 1, "relevance_score": 0.9},
                     {"index": 0, "relevance_score": 0.2}]},
        candidates,
    )
    assert [s.chunk_id for s in scores] == [22, 11]


def test_cohere_response_shape_is_parsed():
    provider = CohereReranker("k")
    scores = provider._parse(  # noqa: SLF001
        {"results": [{"index": 0, "relevance_score": 0.7}]},
        [RerankCandidate(5, "a")],
    )
    assert scores == [RerankScore(5, 0.7)]


def test_openai_judge_response_shape_is_parsed():
    provider = OpenAIJudgeReranker("k")
    scores = provider._parse(  # noqa: SLF001
        {"choices": [{"message": {"content":
         '{"scores":[{"index":0,"score":0.4},{"index":1,"score":0.95}]}'}}]},
        [RerankCandidate(1, "a"), RerankCandidate(2, "b")],
    )
    assert [s.chunk_id for s in scores] == [2, 1]


def test_a_provider_index_outside_the_candidate_list_is_ignored():
    """A malformed response must not raise IndexError inside retrieval."""
    scores = JinaReranker("k")._parse(  # noqa: SLF001
        {"results": [{"index": 99, "relevance_score": 1.0}]},
        [RerankCandidate(1, "a")],
    )
    assert scores == []


def test_terminal_status_codes_trip_the_circuit():
    """Retrying a 401 cannot succeed; every query must not pay for it."""
    provider = JinaReranker("k")
    assert 401 in provider._TERMINAL          # noqa: SLF001
    assert 402 in provider._TERMINAL          # noqa: SLF001
    assert 429 not in provider._TERMINAL, "a rate limit IS worth retrying"


# ================================= 5. never regress lexical retrieval

def test_a_decisive_lexical_hit_is_pinned_to_rank_one():
    """Requirement 5 cannot be met by tuning fusion weights.

    Rank fusion is consensus-seeking by design — right for an ambiguous
    question, wrong for a verbatim quotation where one signal is simply
    certain. Measured: a target ranked #1 lexically at 1.80 against a
    runner-up at 1.20 was still displaced by three chunks appearing in more
    signals.
    """
    assert HybridRetrievalEngine._dominant_lexical(  # noqa: SLF001
        [(7, 1.8), (9, 1.2)]) == 7


def test_a_close_lexical_race_is_left_to_fusion():
    """The guard must fire for quotations, not for ordinary questions."""
    assert HybridRetrievalEngine._dominant_lexical(  # noqa: SLF001
        [(7, 1.2), (9, 1.1)]) is None


def test_a_single_lexical_hit_is_not_decisive():
    """With nothing to compare against, a score says nothing about how
    distinctive the match is."""
    assert HybridRetrievalEngine._dominant_lexical([(7, 99.0)]) is None  # noqa: SLF001


def test_dominance_ratio_is_conservative():
    assert LEXICAL_DOMINANCE_RATIO >= 1.5


def test_a_zero_scoring_runner_up_still_promotes():
    assert HybridRetrievalEngine._dominant_lexical(  # noqa: SLF001
        [(7, 0.9), (9, 0.0)]) == 7


# ============================================= 2. automatic backfill

def test_backfill_job_is_registered_everywhere():
    kind = JobKind.EMBEDDING_BACKFILL
    assert kind in JOB_LABELS
    assert kind in DEFAULT_PRIORITY
    assert kind in RETRY_POLICIES

    from app.services.platform.jobs.handlers import handler_for
    assert handler_for(kind) is not None


def test_backfill_is_scheduled_frequently_enough_to_self_arm():
    """The event it waits for — a provider becoming reachable — produces no
    signal, so the schedule IS the trigger."""
    spec = next(s for s in SCHEDULES if s.kind == JobKind.EMBEDDING_BACKFILL)
    assert spec.every_seconds <= 3600


def test_backfill_skips_cleanly_with_no_provider():
    """Must cost nothing and claim nothing when unconfigured."""
    from app.services.retrieval.backfill import EmbeddingBackfillService

    class _DB:
        def execute(self, *a, **k):  # pragma: no cover - never reached
            raise AssertionError("queried the database with no provider")

    run = EmbeddingBackfillService(_DB(), embedder=None).run()
    assert run.skipped is True
    assert run.embedded == 0
    assert "no embedding provider" in run.detail


def test_backfill_reports_remaining_work():
    from app.services.retrieval.backfill import BackfillRun

    payload = BackfillRun(embedded=32, remaining=968,
                          provider="bge-m3").as_dict()
    assert payload["embedded"] == 32
    assert payload["remaining"] == 968
    assert payload["provider"] == "bge-m3"
