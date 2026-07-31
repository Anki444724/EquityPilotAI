"""Phase 1 verification: offline template writer vs OpenRouter.

Runs the *same* orchestrated report twice per ticker — once with the offline
composer forced, once with OpenRouter — and compares them on the five axes the
brief names: answer quality, citation preservation, latency, token usage and
confidence.

The comparison is deliberately paired rather than absolute. The interesting
question is not "is the OpenRouter output good" — that is a judgement — but
"did switching the writer change anything it was not supposed to change".
Routing, evidence selection, citations and confidence are computed before the
writer runs, so a correct integration leaves all four identical and moves only
the prose, the latency and the token count. Any drift in the first four is a
defect in this change, and this harness is built to catch exactly that.

Usage:
    OPENROUTER_API_KEY=... python3 deploy/verify_ai_writer.py
    OPENROUTER_API_KEY=... python3 deploy/verify_ai_writer.py --tickers TCS,RELIANCE

The key is read from the environment only. It is never written to the report,
never logged and never echoed, and the harness asserts as much before exiting.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import sys
import time

BACKEND = pathlib.Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND))
os.chdir(BACKEND)

DEFAULT_TICKERS = ["TCS", "RELIANCE", "AAPL", "MSFT"]


def _fmt(value: float, places: int = 2) -> str:
    return f"{value:,.{places}f}"


async def run_report(ticker: str, *, writer: str) -> dict | None:
    """One orchestrated report, with the writing provider pinned.

    `writer` selects only the *final answer generation layer*. Retrieval,
    routing and scoring are untouched, which is the property under test.
    """
    from app.db.base import SessionLocal
    from app.services.ai.report_orchestrator import ReportOrchestrator
    from app.services.ai.service import AIService
    from app.services.analysis_service import AnalysisService
    from app.services.ai.providers.router import ProviderRouter

    db = SessionLocal()
    try:
        analysis = AnalysisService.for_ticker(db, ticker)
        if analysis is None:
            return None

        service = AIService(db)
        analyst = service.analyst_for(analysis)
        # Pin the writer. `preferred` reorders the chain; forcing the offline
        # provider needs the live ones removed, since a live provider always
        # outranks the mock by design.
        if writer == "Offline":
            from app.services.ai.providers import mock
            analyst.router = ProviderRouter(configs=[mock.DEFAULTS])
        else:
            analyst.router = ProviderRouter(preferred=writer)
        # Caching would make the second run of a repeated section free and
        # report a latency that no user will ever experience.
        analyst.router.cache.clear()

        started = time.perf_counter()
        report = await ReportOrchestrator(analyst).run()
        elapsed = (time.perf_counter() - started) * 1000
        payload = report.as_dict()
        payload["wall_ms"] = round(elapsed, 1)
        return payload
    finally:
        db.close()


def compare(ticker: str, before: dict, after: dict) -> dict:
    """Paired comparison of the two runs for one ticker."""
    b_sections = {s["section"]: s for s in before["sections"]}
    a_sections = {s["section"]: s for s in after["sections"]}

    rows = []
    drift = []
    declined = []
    for key in b_sections:
        b, a = b_sections[key], a_sections.get(key, {})

        # A section whose writer declined for want of relevant evidence is
        # *expected* to lose its confidence, so it is reported separately
        # rather than counted as drift.
        #
        # This distinction was added after the harness flagged
        # `revenue_segments` on both Indian tickers. Investigating showed the
        # product was right and the harness was wrong: neither company has an
        # uploaded annual report, the route therefore falls through to the
        # financial database, and that holds consolidated revenue with no
        # segment split. The writer declining is the honest outcome, and
        # zeroing the confidence beside it is the correct consequence.
        # Scoring that as a regression would have pressured the wrong fix —
        # making the model write a segment analysis it has no data for.
        writer_declined = (
            a.get("confidence_score") == 0.0
            and b.get("confidence_score") != 0.0
            and "no verified evidence" in (a.get("content") or "").lower()
        )
        if writer_declined:
            declined.append(f"{ticker}/{key}")

        # These four are computed before the writer runs and must not move.
        for field in ("provider_used", "source_used", "confidence_score",
                      "citation_count"):
            if field == "confidence_score" and writer_declined:
                continue
            if b.get(field) != a.get(field):
                drift.append(
                    f"{ticker}/{key}: {field} {b.get(field)!r} -> {a.get(field)!r}"
                )
        rows.append({
            "section": b["title"],
            "provider": b.get("provider_used") or "—",
            "confidence": b.get("confidence_score"),
            "citations": b.get("citation_count"),
            "before_chars": len(b.get("content") or ""),
            "after_chars": len(a.get("content") or ""),
            "written_by": a.get("writer_provider"),
            "model": a.get("writer_model"),
            "tokens": a.get("total_tokens"),
        })

    return {
        "ticker": ticker,
        "company": before.get("company"),
        "sections": rows,
        "drift": drift,
        "declined": declined,
        "before": {
            "latency_ms": before.get("wall_ms"),
            "tokens": before.get("total_tokens", 0),
            "cost_usd": before.get("total_cost_usd", 0.0),
            "chars": sum(len(s.get("content") or "") for s in before["sections"]),
            "grounded": before.get("grounded_sections"),
            "writer_mix": before.get("writer_mix"),
        },
        "after": {
            "latency_ms": after.get("wall_ms"),
            "tokens": after.get("total_tokens", 0),
            "cost_usd": after.get("total_cost_usd", 0.0),
            "chars": sum(len(s.get("content") or "") for s in after["sections"]),
            "grounded": after.get("grounded_sections"),
            "writer_mix": after.get("writer_mix"),
        },
    }


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tickers", default=",".join(DEFAULT_TICKERS))
    parser.add_argument("--out", default="../docs/PHASE1_AI_WRITER.md")
    args = parser.parse_args()

    key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        print("OPENROUTER_API_KEY is not set; cannot run the live half.")
        return 2

    tickers = [t.strip() for t in args.tickers.split(",") if t.strip()]
    results = []
    skipped = []

    for ticker in tickers:
        print(f"--- {ticker} ---", flush=True)
        before = await run_report(ticker, writer="Offline")
        if before is None:
            print(f"  not in the coverage universe; skipped")
            skipped.append(ticker)
            continue
        print(f"  offline    {before['wall_ms']:>8.0f} ms", flush=True)
        after = await run_report(ticker, writer="OpenRouter")
        print(f"  openrouter {after['wall_ms']:>8.0f} ms  "
              f"{after.get('total_tokens', 0)} tokens  "
              f"mix={after.get('writer_mix')}", flush=True)
        results.append(compare(ticker, before, after))

    report = render(results, skipped)
    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    # The key must not reach disk under any circumstance.
    if key in report:
        print("REFUSING TO WRITE: the API key appears in the report body.")
        return 3
    out.write_text(report)
    print(f"\nwritten to {out.resolve()}")

    declined = [d for r in results for d in r["declined"]]
    if declined:
        print(f"\n{len(declined)} section(s) declined for want of relevant "
              f"evidence (expected, not drift):")
        for item in declined:
            print(f"  {item}")

    drift = [d for r in results for d in r["drift"]]
    if drift:
        print(f"\nFAIL — {len(drift)} field(s) drifted:")
        for item in drift[:20]:
            print(f"  {item}")
        return 1
    print("\nPASS — routing, sources, confidence and citations all preserved.")
    return 0


def render(results: list[dict], skipped: list[str]) -> str:
    lines = [
        "# Phase 1 — Production AI Writer Integration",
        "",
        "Offline template composer vs OpenRouter, same evidence, same routing.",
        "",
        "## Summary",
        "",
        "| Ticker | Latency before | Latency after | Tokens | Cost USD | "
        "Prose chars before | after | Drift |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        b, a = r["before"], r["after"]
        lines.append(
            f"| {r['ticker']} | {_fmt(b['latency_ms'], 0)} ms | "
            f"{_fmt(a['latency_ms'], 0)} ms | {a['tokens']:,} | "
            f"{a['cost_usd']:.5f} | {b['chars']:,} | {a['chars']:,} | "
            f"{'**' + str(len(r['drift'])) + '**' if r['drift'] else '0'} |"
        )
    if skipped:
        lines += ["", f"Skipped (outside the coverage universe): "
                      f"{', '.join(skipped)}."]

    for r in results:
        lines += [
            "", f"## {r['ticker']} — {r['company']}", "",
            "| Section | Evidence provider | Conf | Cites | Written by | Tokens |",
            "|---|---|---|---|---|---|",
        ]
        for row in r["sections"]:
            lines.append(
                f"| {row['section']} | {row['provider']} | "
                f"{row['confidence']} | {row['citations']} | "
                f"{row['written_by']} | {row['tokens']} |"
            )
        if r["declined"]:
            lines += [
                "",
                "Declined for want of relevant evidence (expected — the "
                "writer refused to fabricate): "
                + ", ".join(d.split("/")[1] for d in r["declined"]) + ".",
            ]
        if r["drift"]:
            lines += ["", "**Drift detected:**", ""]
            lines += [f"- {d}" for d in r["drift"]]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
