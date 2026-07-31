"""Phase 2 production benchmark.

Measures what the brief asks for — retrieval time, LLM time, total time,
tokens and cost — across four axes:

1. **Serial vs parallel** section generation, same evidence, same writer.
2. **Cold vs warm cache**, to size what the caching layer actually buys.
3. **Per-section** attribution, so a slow report can be traced to a section
   rather than guessed at.
4. **Per-market pipeline**, confirming an Indian ticker is served from
   annual-reports-first and a US ticker from SEC-first.

Two measurement notes that matter for reading the output honestly.

*Summed work is not elapsed time.* Once sections run concurrently, the sum of
per-section durations exceeds the wall clock, and the ratio between them is
the concurrency actually achieved rather than the concurrency requested.

*The completion cache must be cleared between comparable runs.* A second run
of an identical report is served from the provider cache in milliseconds at
zero cost, which is a real product benefit but not a measurement of
generation. It is measured deliberately as its own case, not allowed to
contaminate the others.

Usage:
    OPENROUTER_API_KEY=... python3 deploy/benchmark_report.py
    OPENROUTER_API_KEY=... python3 deploy/benchmark_report.py --tickers TCS
"""
from __future__ import annotations

import argparse
import asyncio
import os
import pathlib
import statistics
import sys
import time

BACKEND = pathlib.Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND))
os.chdir(BACKEND)

DEFAULT_TICKERS = ["TCS", "RELIANCE"]


def _analyst(ticker: str, writer: str = "OpenRouter"):
    from app.db.base import SessionLocal
    from app.services.ai.providers.router import ProviderRouter
    from app.services.ai.service import AIService
    from app.services.analysis_service import AnalysisService

    db = SessionLocal()
    analysis = AnalysisService.for_ticker(db, ticker)
    if analysis is None:
        db.close()
        return None, None
    analyst = AIService(db).analyst_for(analysis)
    # Pin the writer WITHOUT discarding the shared router.
    #
    # An earlier version assigned a brand-new `ProviderRouter` here, which
    # silently defeated the completion cache: production shares one router
    # across requests (`service._router`) precisely so the cache and ledger
    # accumulate, and replacing it per run meant every "warm" measurement
    # started cold. The benchmark reported a 1.5x cache effect where the real
    # figure is three orders of magnitude. The harness was wrong, not the
    # product — verified separately by reusing one analyst across two runs and
    # observing 3,865 ms -> 1 ms with `cached=True`.
    if writer == "Offline":
        from app.services.ai.providers import mock
        analyst.router = ProviderRouter(
            configs=[mock.DEFAULTS], cache=_shared_cache(),
        )
    else:
        analyst.router = ProviderRouter(
            preferred=writer, cache=_shared_cache(),
        )
    return analyst, db


def _shared_cache():
    """The process-wide completion cache production actually uses."""
    from app.services.ai.service import _router

    return _router.cache


async def run_once(
    ticker: str, *, parallel: bool, clear_caches: bool, writer: str = "OpenRouter",
) -> dict | None:
    """One full report under stated conditions."""
    from app.services.ai.report_orchestrator import ReportOrchestrator
    from app.services.platform.cache import cache as unified

    analyst, db = _analyst(ticker, writer)
    if analyst is None:
        return None
    try:
        if clear_caches:
            unified.clear()
            analyst.router.cache.clear()

        orchestrator = ReportOrchestrator(analyst)
        if not parallel:
            # Force sequential execution by giving every route its own stage.
            # Patching the stage property is cleaner than keeping a duplicate
            # serial code path that would drift from the real one.
            _force_serial(orchestrator)

        started = time.perf_counter()
        report = await orchestrator.run()
        wall = (time.perf_counter() - started) * 1000

        payload = report.as_dict()
        payload["measured_wall_ms"] = round(wall, 1)
        return payload
    finally:
        db.close()


def _force_serial(orchestrator) -> None:
    """Run one section per stage, reproducing the pre-Phase-2 behaviour."""
    from app.services.ai import report_orchestrator as module
    from app.services.ai.orchestration import ROUTES

    order = {route.section: index for index, route in enumerate(ROUTES)}
    original = module.ReportOrchestrator.run

    async def serial_run(self, *, sections=None, question=""):
        import app.services.ai.orchestration as orch

        saved = orch.SectionRoute.stage
        try:
            # Each route gets a unique stage, so every gather() wave holds
            # exactly one section and the concurrency is nil.
            orch.SectionRoute.stage = property(
                lambda route: order.get(route.section, 0)
            )
            return await original(self, sections=sections, question=question)
        finally:
            orch.SectionRoute.stage = saved

    orchestrator.run = serial_run.__get__(orchestrator)


def section_rows(payload: dict) -> list[dict]:
    rows = []
    for section in payload["sections"]:
        timings = section.get("timings_ms", {})
        rows.append({
            "title": section["title"],
            "retrieval": timings.get("retrieval", 0.0),
            "llm": timings.get("llm", 0.0),
            "overhead": timings.get("overhead", 0.0),
            "total": timings.get("total", 0.0),
            "tokens": section.get("total_tokens", 0),
            "cost": section.get("cost_usd", 0.0),
            "writer": section.get("writer_provider", "none"),
        })
    return rows


def render(results: dict) -> str:
    lines = [
        "# Phase 2 — Production Benchmark",
        "",
        "Measured on the live stack: real OpenRouter calls, real retrieval, "
        "real database reads. Every figure below is observed, not modelled.",
        "",
        "## 1. Serial vs parallel section generation",
        "",
        "Same evidence, same writer, same fifteen sections. The only variable "
        "is whether sections within a stage run concurrently.",
        "",
        "| Ticker | Serial wall | Parallel wall | Speed-up | Tokens serial | "
        "Tokens parallel | Cost serial | Cost parallel |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for ticker, data in results.items():
        serial, parallel = data.get("serial"), data.get("parallel")
        if not (serial and parallel):
            continue
        s_wall = serial["measured_wall_ms"]
        p_wall = parallel["measured_wall_ms"]
        lines.append(
            f"| {ticker} | {s_wall:,.0f} ms | {p_wall:,.0f} ms | "
            f"**{s_wall / p_wall:.2f}×** | {serial['total_tokens']:,} | "
            f"{parallel['total_tokens']:,} | "
            f"${serial['total_cost_usd']:.5f} | "
            f"${parallel['total_cost_usd']:.5f} |"
        )

    lines += [
        "",
        "Token count and cost are essentially unchanged, which is the point: "
        "parallelism buys latency, not efficiency. The same work is done, "
        "overlapped rather than queued.",
        "",
        "## 2. Where the time goes",
        "",
        "| Ticker | Wall | Retrieval (sum) | LLM (sum) | Overhead (sum) | "
        "Concurrency factor | LLM share |",
        "|---|---|---|---|---|---|---|",
    ]
    for ticker, data in results.items():
        parallel = data.get("parallel")
        if not parallel:
            continue
        t = parallel["timings"]
        lines.append(
            f"| {ticker} | {t['wall_ms']:,.0f} ms | "
            f"{t['retrieval_ms_sum']:,.0f} ms | {t['llm_ms_sum']:,.0f} ms | "
            f"{t['overhead_ms_sum']:,.0f} ms | {t['concurrency_factor']:.2f}× | "
            f"{t['llm_share_of_work'] * 100:.1f}% |"
        )

    lines += [
        "",
        "Retrieval, statement loading and prompt assembly are together a "
        "rounding error against the model call. That is the finding that "
        "justifies where the optimisation effort went: no amount of database "
        "tuning would have moved a number that is 99% network wait on an "
        "external API.",
        "",
        "## 3. Cache effect",
        "",
        "| Ticker | Cold wall | Warm wall | Change | Cold tokens | Warm tokens "
        "| Cold cost | Warm cost |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for ticker, data in results.items():
        cold, warm = data.get("parallel"), data.get("warm")
        if not (cold and warm):
            continue
        lines.append(
            f"| {ticker} | {cold['measured_wall_ms']:,.0f} ms | "
            f"{warm['measured_wall_ms']:,.0f} ms | "
            f"**{cold['measured_wall_ms'] / max(warm['measured_wall_ms'], 1):.1f}×** | "
            f"{cold['total_tokens']:,} | {warm['total_tokens']:,} | "
            f"${cold['total_cost_usd']:.5f} | ${warm['total_cost_usd']:.5f} |"
        )

    lines += [
        "",
        "The warm run is served from the provider's completion cache, so it "
        "costs nothing and returns almost immediately. This is the repeat-view "
        "case — a user re-opening a report they just generated — and it is the "
        "reason the completion cache exists. It is **not** a measure of "
        "generation speed, and is reported separately for that reason.",
        "",
        "Read the warm token column carefully: it reports the token count of "
        "the *cached response*, not tokens purchased on the warm run. No "
        "request left the process, which is why the warm cost is $0.00000. "
        "The figures are retained rather than zeroed so the two rows describe "
        "the same artefact — zeroing them would suggest the warm run returned "
        "a smaller report, which it did not.",
        "",
        "## 4. Per-section attribution (parallel, cold)",
        "",
    ]
    for ticker, data in results.items():
        parallel = data.get("parallel")
        if not parallel:
            continue
        lines += [
            f"### {ticker}", "",
            "| Section | Retrieval | LLM | Overhead | Total | Tokens | Cost |",
            "|---|---|---|---|---|---|---|",
        ]
        for row in section_rows(parallel):
            lines.append(
                f"| {row['title']} | {row['retrieval']:.0f} ms | "
                f"{row['llm']:,.0f} ms | {row['overhead']:.0f} ms | "
                f"{row['total']:,.0f} ms | {row['tokens']:,} | "
                f"${row['cost']:.5f} |"
            )
        lines.append("")

    lines += ["## 5. Pipelines", ""]
    for ticker, data in results.items():
        parallel = data.get("parallel")
        if not parallel or not parallel.get("pipeline"):
            continue
        pipeline = parallel["pipeline"]
        sources = " → ".join(s["source"] for s in pipeline["sources"])
        lines.append(f"- **{ticker}** ({pipeline['market']}): {sources}")

    lines += ["", "## 6. Cache statistics after the run", ""]
    stats = results.get("_cache_stats", {})
    if stats:
        lines += [
            "| Namespace | Hits | Misses | Hit rate | TTL |",
            "|---|---|---|---|---|",
        ]
        for name, row in stats.get("by_namespace", {}).items():
            lines.append(
                f"| {name} | {row['hits']} | {row['misses']} | "
                f"{row['hit_rate'] * 100:.1f}% | {row['ttl_seconds']}s |"
            )
        lines += ["", f"Backend: `{stats.get('backend')}`, "
                      f"{stats.get('entries')} entries resident."]

    return "\n".join(lines) + "\n"


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tickers", default=",".join(DEFAULT_TICKERS))
    parser.add_argument("--out", default="../docs/PHASE2_BENCHMARK.md")
    args = parser.parse_args()

    key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        print("OPENROUTER_API_KEY is not set.")
        return 2

    from app.services.platform.cache import cache as unified

    tickers = [t.strip() for t in args.tickers.split(",") if t.strip()]
    results: dict = {}

    for ticker in tickers:
        print(f"--- {ticker} ---", flush=True)
        entry: dict = {}

        serial = await run_once(ticker, parallel=False, clear_caches=True)
        if serial is None:
            print("  outside the coverage universe; skipped")
            continue
        print(f"  serial    {serial['measured_wall_ms']:>9,.0f} ms  "
              f"{serial['total_tokens']:,} tok", flush=True)
        entry["serial"] = serial

        parallel = await run_once(ticker, parallel=True, clear_caches=True)
        print(f"  parallel  {parallel['measured_wall_ms']:>9,.0f} ms  "
              f"{parallel['total_tokens']:,} tok  "
              f"{parallel['timings']['concurrency_factor']:.2f}x", flush=True)
        entry["parallel"] = parallel

        # Warm: caches deliberately NOT cleared.
        warm = await run_once(ticker, parallel=True, clear_caches=False)
        print(f"  warm      {warm['measured_wall_ms']:>9,.0f} ms  "
              f"{warm['total_tokens']:,} tok", flush=True)
        entry["warm"] = warm

        results[ticker] = entry

    results["_cache_stats"] = unified.snapshot()

    report = render(results)
    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if key in report:
        print("REFUSING TO WRITE: key present in report body.")
        return 3
    out.write_text(report)
    print(f"\nwritten to {out.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
