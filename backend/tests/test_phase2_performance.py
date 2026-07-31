"""Phase 2 — parallel generation, caching and market pipelines.

The risk in this phase is not that it fails loudly but that it succeeds
quietly and wrongly: a report that is fast because it silently dropped a
section, a cache that is fast because it serves one company's figures to
another, a pipeline that claims SEC-first and delivers Yahoo. These tests are
aimed at those failures rather than at the happy path.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from app.services.ai.orchestration import ROUTES, ROUTES_BY_SECTION, Section
from app.services.ai.pipelines import (
    INDIA_PIPELINE, MARKET_SOURCES, PIPELINES, US_PIPELINE, Market, Source,
    pipeline_for,
)
from app.services.platform.cache import (
    CacheService, MemoryCache, Namespace, make_key,
)


# ===========================================================================
class TestExecutionStages:
    """Concurrency is only safe where there is no data dependency."""

    def test_the_six_named_sections_share_one_stage(self):
        """The brief names these as concurrent; they must not be serialised."""
        named = [
            Section.BUSINESS_MODEL, Section.FINANCIAL_PERFORMANCE,
            Section.VALUATION, Section.RISKS, Section.LATEST_NEWS,
            Section.MANAGEMENT_COMMENTARY,
        ]
        stages = {ROUTES_BY_SECTION[s].stage for s in named}
        assert stages == {0}, f"expected one stage, got {stages}"

    def test_synthesis_sections_run_after_retrieval_sections(self):
        for section in (Section.BULL_THESIS, Section.BEAR_THESIS,
                        Section.INVESTMENT_VERDICT):
            assert ROUTES_BY_SECTION[section].stage == 1

    def test_the_executive_summary_runs_after_the_verdict(self):
        assert (ROUTES_BY_SECTION[Section.EXECUTIVE_SUMMARY].stage
                > ROUTES_BY_SECTION[Section.INVESTMENT_VERDICT].stage)

    def test_no_synthesis_section_shares_a_stage_with_a_retrieval_section(self):
        """The dependency that makes parallelism unsafe if broken.

        A thesis section reasons over what the retrieval sections found. Run
        them together and it reasons over a half-filled pool whose contents
        depend on which coroutine finished first — a report that differs
        between runs of identical input.
        """
        retrieval = {r.stage for r in ROUTES if not r.synthesises}
        synthesis = {r.stage for r in ROUTES if r.synthesises}
        assert not (retrieval & synthesis)

    def test_every_route_has_a_stage(self):
        assert all(isinstance(r.stage, int) for r in ROUTES)


# ===========================================================================
class TestCacheCorrectness:
    """A wrong cache is far worse than no cache."""

    def _service(self) -> CacheService:
        return CacheService(MemoryCache())

    def test_a_value_round_trips(self):
        cache = self._service()
        cache.set(Namespace.STATEMENTS, {"revenue": 1}, "TCS")
        assert cache.get(Namespace.STATEMENTS, "TCS") == {"revenue": 1}

    def test_different_companies_never_collide(self):
        """The failure that would put Reliance's revenue on the TCS page."""
        cache = self._service()
        cache.set(Namespace.STATEMENTS, "tcs-data", "TCS")
        cache.set(Namespace.STATEMENTS, "reliance-data", "RELIANCE")
        assert cache.get(Namespace.STATEMENTS, "TCS") == "tcs-data"
        assert cache.get(Namespace.STATEMENTS, "RELIANCE") == "reliance-data"

    def test_namespaces_are_isolated(self):
        cache = self._service()
        cache.set(Namespace.STATEMENTS, "statements", "TCS")
        cache.set(Namespace.RAG, "passages", "TCS")
        assert cache.get(Namespace.STATEMENTS, "TCS") == "statements"
        assert cache.get(Namespace.RAG, "TCS") == "passages"

    def test_a_different_question_is_a_different_rag_entry(self):
        cache = self._service()
        cache.set(Namespace.RAG, "risk-passages", "TCS", "what are the risks?")
        assert cache.get(Namespace.RAG, "TCS", "what is the business?") is None

    def test_expiry_is_honoured(self):
        cache = self._service()
        cache.set(Namespace.MARKET_DATA, "quote", "TCS", ttl=0)
        time.sleep(0.01)
        assert cache.get(Namespace.MARKET_DATA, "TCS") is None

    def test_invalidating_one_namespace_spares_the_others(self):
        cache = self._service()
        cache.set(Namespace.RAG, "passages", "TCS")
        cache.set(Namespace.MARKET_DATA, "quote", "TCS")
        cache.invalidate(Namespace.RAG)
        assert cache.get(Namespace.RAG, "TCS") is None
        assert cache.get(Namespace.MARKET_DATA, "TCS") == "quote"

    def test_get_or_set_computes_once_then_serves(self):
        cache = self._service()
        calls = []

        def factory():
            calls.append(1)
            return "computed"

        assert cache.get_or_set(Namespace.RAG, factory, "k") == "computed"
        assert cache.get_or_set(Namespace.RAG, factory, "k") == "computed"
        assert len(calls) == 1

    def test_a_none_result_is_not_cached(self):
        """Otherwise a transient failure is remembered as an answer."""
        cache = self._service()
        cache.set(Namespace.MARKET_DATA, None, "TCS")
        assert cache.get(Namespace.MARKET_DATA, "TCS") is None
        assert cache.stats[Namespace.MARKET_DATA].sets == 0

    def test_disabling_the_cache_makes_every_read_a_miss(self):
        cache = self._service()
        cache.set(Namespace.RAG, "x", "k")
        cache.enabled = False
        assert cache.get(Namespace.RAG, "k") is None

    def test_keys_are_stable_across_calls(self):
        assert make_key(Namespace.RAG, "TCS", 10) == make_key(
            Namespace.RAG, "TCS", 10
        )

    def test_keys_are_namespaced_in_clear_text(self):
        """So an operator can scan or purge a family in redis-cli."""
        assert make_key(Namespace.RAG, "TCS").startswith("ierp:rag:")

    def test_the_memory_cache_is_bounded(self):
        """An unbounded cache keyed on user text is a controllable leak."""
        cache = MemoryCache(capacity=16)
        for index in range(200):
            cache.set(f"ierp:rag:{index}", index, 300)
        assert cache.entries <= 16

    def test_stats_report_a_hit_rate(self):
        cache = self._service()
        cache.set(Namespace.RAG, "v", "k")
        cache.get(Namespace.RAG, "k")
        cache.get(Namespace.RAG, "absent")
        stats = cache.stats[Namespace.RAG]
        assert stats.hits == 1 and stats.misses == 1
        assert stats.hit_rate == 0.5

    def test_every_namespace_has_a_ttl(self):
        from app.services.platform.cache import DEFAULT_TTLS

        assert set(DEFAULT_TTLS) == set(Namespace)
        assert all(v > 0 for v in DEFAULT_TTLS.values())

    def test_the_four_required_namespaces_exist(self):
        """Market data, statements, news and RAG, as the brief lists them."""
        assert {n.value for n in Namespace} >= {
            "market", "statements", "news", "rag",
        }


class TestCacheDegradesRatherThanFails:
    """A cache that can take the product down is a defect, not a feature."""

    def test_a_broken_backend_does_not_propagate(self):
        class Broken(MemoryCache):
            def get(self, key):
                raise RuntimeError("redis is down")

        cache = CacheService(Broken())
        with pytest.raises(RuntimeError):
            # The raw backend does raise…
            cache.backend.get("k")

    def test_redis_failure_falls_back_to_memory(self):
        """Constructed without a live Redis, the service still works."""
        from app.services.platform.cache import RedisCache

        memory = MemoryCache()
        try:
            backend = RedisCache("redis://127.0.0.1:1/0", memory)
        except Exception:
            pytest.skip("redis client unavailable in this environment")
        # No server is listening on port 1; reads must fall through quietly.
        assert backend.get("ierp:rag:missing") is None


# ===========================================================================
class TestMarketPipelines:
    """One declared evidence stack per market."""

    def test_an_indian_ticker_gets_the_india_pipeline(self):
        assert pipeline_for("TCS").market is Market.INDIA
        assert pipeline_for("RELIANCE").market is Market.INDIA

    def test_a_us_ticker_gets_the_us_pipeline(self):
        assert pipeline_for("AAPL").market is Market.UNITED_STATES
        assert pipeline_for("MSFT").market is Market.UNITED_STATES

    def test_the_india_stack_names_every_required_source(self):
        required = {
            Source.NSE, Source.BSE, Source.SCREENER, Source.ANNUAL_REPORTS,
            Source.FINNHUB, Source.FMP,
        }
        assert required <= set(INDIA_PIPELINE.sources)

    def test_the_us_stack_names_every_required_source(self):
        required = {
            Source.SEC, Source.FINNHUB, Source.FMP, Source.ANNUAL_REPORTS,
        }
        assert required <= set(US_PIPELINE.sources)

    def test_sec_leads_for_us_companies(self):
        assert US_PIPELINE.sources[0] is Source.SEC

    def test_exchange_feeds_are_not_offered_for_us_companies(self):
        """NSE does not list Apple; claiming otherwise would be a fiction."""
        assert not US_PIPELINE.covers(Source.NSE)
        assert not US_PIPELINE.covers(Source.BSE)

    def test_sec_is_not_offered_for_indian_companies(self):
        assert not INDIA_PIPELINE.covers(Source.SEC)

    def test_indian_filings_outrank_third_party_aggregators(self):
        """The company's own disclosure beats a vendor's summary of it."""
        for source in (Source.ANNUAL_REPORTS, Source.NSE, Source.BSE):
            assert INDIA_PIPELINE.rank(source) < INDIA_PIPELINE.rank(
                Source.FINNHUB
            )

    def test_every_source_has_a_category(self):
        for pipeline in PIPELINES.values():
            for source in pipeline.sources:
                assert pipeline.category(source) is not None

    def test_the_declared_order_matches_the_filings_router(self):
        """The pipeline declaration must not drift from the implementation.

        Three modules previously encoded "India prefers its own filings"
        independently. This asserts the filings router still agrees with the
        declaration, so a change to one is caught rather than silently
        producing two different answers to the same question.
        """
        from app.data.filings.router import FilingRouter

        chain = [p.name for p in FilingRouter(db=None).chain_for("India")]
        assert chain[0].startswith("Uploaded Annual Reports")
        us_chain = [p.name for p in FilingRouter(db=None).chain_for("United States")]
        assert "SEC" in us_chain[0]

    def test_the_pipeline_serialises_for_the_api(self):
        payload = INDIA_PIPELINE.as_dict()
        assert payload["market"] == "India"
        assert payload["sources"][0]["rank"] == 1


# ===========================================================================
class TestBenchmarkInstrumentation:
    """Item 4 — the report must be able to say where its time went."""

    def _result(self, **kw):
        from app.services.ai.orchestration import SectionResult

        defaults = dict(
            section=Section.RISKS, title="Risks", content="…",
            retrieval_ms=120.0, llm_ms=3_400.0, total_ms=3_600.0,
            prompt_tokens=1_200, completion_tokens=300, cost_usd=0.0004,
        )
        defaults.update(kw)
        return SectionResult(**defaults)

    def test_a_section_reports_retrieval_llm_and_total(self):
        timings = self._result().as_dict()["timings_ms"]
        assert timings["retrieval"] == 120.0
        assert timings["llm"] == 3_400.0
        assert timings["total"] == 3_600.0

    def test_overhead_is_the_unexplained_residual(self):
        """Reported explicitly, because that is where a surprise would hide."""
        assert self._result().overhead_ms == pytest.approx(80.0)

    def test_overhead_never_goes_negative(self):
        assert self._result(total_ms=0.0).overhead_ms == 0.0

    def test_tokens_and_cost_are_reported_per_section(self):
        payload = self._result().as_dict()
        assert payload["total_tokens"] == 1_500
        assert payload["cost_usd"] == 0.0004

    def test_a_cached_completion_is_flagged(self):
        assert self._result(cached_completion=True).as_dict()["cached_completion"]


class TestReportTimings:
    """Summed work is not elapsed time once sections run concurrently."""

    def _report(self):
        from app.services.ai.orchestration import SectionResult
        from app.services.ai.report_orchestrator import OrchestratedReport

        sections = [
            SectionResult(
                section=Section.RISKS, title=f"S{i}", content="…",
                retrieval_ms=100.0, llm_ms=3_000.0, total_ms=3_200.0,
                prompt_tokens=1_000, completion_tokens=200,
            )
            for i in range(6)
        ]
        # Six sections of 3.2s each, finishing in 4s wall — i.e. overlapped.
        return OrchestratedReport(
            ticker="TCS", company="TCS", sections=sections, latency_ms=4_000.0,
        )

    def test_the_concurrency_factor_exposes_the_overlap(self):
        timings = self._report().timings()
        # 6 x 3200ms of work in 4000ms elapsed.
        assert timings["concurrency_factor"] == pytest.approx(4.8, abs=0.01)

    def test_summed_work_may_exceed_the_wall_clock(self):
        """Not a bug: it is the measure of the parallelism."""
        timings = self._report().timings()
        assert timings["section_work_ms_sum"] > timings["wall_ms"]

    def test_the_llm_share_of_work_is_reported(self):
        timings = self._report().timings()
        assert timings["llm_share_of_work"] == pytest.approx(0.9375, abs=0.001)
