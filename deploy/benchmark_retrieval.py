#!/usr/bin/env python3
"""Benchmark Retrieval Engine 2.0 against the engine it replaces.

Both engines answer the same queries over the same corpus in the same process,
so the comparison is like-for-like. Two things are measured:

**Latency.** Wall time per query. The old engine loads every chunk for a
company into Python and scores them in a loop, so its cost grows with the
corpus; the new one asks Postgres for 40 rows.

**Retrieval quality**, by two complementary measures:

* *Known-item*: a phrase is lifted verbatim from a real chunk and used as the
  query. The chunk it came from is by definition the right answer, so the rank
  it is returned at is an objective measure with no human judgement.
* *Paraphrase*: the same target is asked for in different words, and in Hindi
  and Hinglish. This is the capability the old engine provably lacks — its own
  docstring says it captures "semantic paraphrase poorly" — and the one the
  brief is asking for.

Reciprocal rank is reported rather than a hit rate: finding the answer at
position 1 and at position 9 are both "hits" and are not the same result.

    export DATABASE_URL=...
    python3 deploy/benchmark_retrieval.py --company CIPLA
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import statistics
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "backend"))

import importlib  # noqa: E402
import pkgutil  # noqa: E402

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import app.models as _models  # noqa: E402

for _module in pkgutil.iter_modules(_models.__path__):
    importlib.import_module(f"app.models.{_module.name}")

from app.services.documents.service import DocumentService  # noqa: E402
from app.services.retrieval.engine import HybridRetrievalEngine  # noqa: E402

#: Paraphrase probes. Each is (query, the terms a correct passage must
#: contain). Judging by content rather than by a fixed chunk id keeps the
#: probe valid across corpora.
PARAPHRASE_PROBES: list[tuple[str, tuple[str, ...]]] = [
    ("How much money did the company make?", ("revenue", "income", "sales")),
    ("What does management expect going forward?",
     ("guidance", "outlook", "expect", "growth")),
    ("What could go wrong for this business?",
     ("risk", "uncertain", "adverse", "litigation")),
    ("Who runs the company?", ("director", "chairman", "officer", "board")),
    ("How much cash is on hand?", ("cash", "equivalents", "balance")),
    # The multilingual requirement, asked the way an Indian user would.
    ("कंपनी का राजस्व कितना है?", ("revenue", "income", "sales")),
    ("company ka revenue kitna hai", ("revenue", "income", "sales")),
    ("management guidance kya hai", ("guidance", "outlook", "expect")),
]


def _reciprocal_rank(positions: list[int]) -> float:
    return 1.0 / positions[0] if positions else 0.0


def known_item_probes(db, company_id: str, count: int) -> list[tuple[str, int]]:
    """Phrases lifted verbatim from real chunks, with their source chunk id."""
    rows = db.execute(text("""
        SELECT c.id, c.text FROM document_chunks c
        JOIN documents d ON d.id = c.document_id
        WHERE d.company_id = :cid AND length(c.text) > 400
        ORDER BY c.id
    """), {"cid": company_id}).all()
    if not rows:
        return []

    random.seed(11)          # reproducible
    sample = random.sample(rows, min(count, len(rows)))
    probes: list[tuple[str, int]] = []
    for chunk_id, body in sample:
        words = re.findall(r"[A-Za-z0-9₹%.,-]+", body)
        if len(words) < 20:
            continue
        start = len(words) // 3
        phrase = " ".join(words[start:start + 12])
        if len(phrase) > 25:
            probes.append((phrase, chunk_id))
    return probes


def run(db, ticker: str, *, top_k: int = 10) -> dict:
    company = db.execute(
        text("SELECT id, ticker FROM companies WHERE ticker = :t"),
        {"t": ticker.upper()},
    ).first()
    if company is None:
        raise SystemExit(f"unknown ticker {ticker}")
    company_id = company[0]

    chunks = db.execute(text("""
        SELECT count(*), count(embedding) , count(embedding_v2)
        FROM document_chunks c JOIN documents d ON d.id = c.document_id
        WHERE d.company_id = :cid
    """), {"cid": company_id}).first()

    legacy = DocumentService(db)
    hybrid = HybridRetrievalEngine(db)

    def legacy_search(query: str, top_k: int) -> list:
        """The OLD engine, called directly.

        BENCH-001. The first version of this harness called
        `DocumentService.search()` for the legacy side — but that method now
        routes through the hybrid engine, so the benchmark was comparing the
        new engine against itself and reported suspiciously identical ranks
        for every probe. Building the in-memory index and querying it
        directly is the only way to exercise the code being replaced.
        """
        from app.services.documents.pipeline.search import (
            DocumentSearch, SearchConfig,
        )

        store = legacy.build_index(company_id)
        engine = DocumentSearch(store, legacy.embedder, SearchConfig(top_k=top_k))
        return engine.search(query, top_k=top_k)

    report: dict = {
        "ticker": ticker.upper(),
        "chunks": chunks[0],
        "legacy_vectors": chunks[1],
        "semantic_vectors": chunks[2],
        "semantic_available": bool(chunks[2]) and hybrid.available,
    }

    # ------------------------------------------------------- known item
    probes = known_item_probes(db, company_id, 25)
    legacy_rr, hybrid_rr = [], []
    legacy_ms, hybrid_ms = [], []

    for phrase, target in probes:
        started = time.perf_counter()
        try:
            ids = [h.chunk_id for h in legacy_search(phrase, top_k)]
        except Exception:  # noqa: BLE001
            ids = []
        legacy_ms.append((time.perf_counter() - started) * 1000)
        legacy_rr.append(
            _reciprocal_rank([i + 1 for i, c in enumerate(ids) if c == target])
        )

        started = time.perf_counter()
        try:
            results = hybrid.retrieve(phrase, company_id=company_id,
                                      top_k=top_k)
            ids = [r.chunk_id for r in results]
        except Exception:  # noqa: BLE001
            ids = []
        hybrid_ms.append((time.perf_counter() - started) * 1000)
        hybrid_rr.append(
            _reciprocal_rank([i + 1 for i, c in enumerate(ids) if c == target])
        )

    report["known_item"] = {
        "probes": len(probes),
        "legacy_mrr": round(statistics.fmean(legacy_rr), 4) if legacy_rr else 0,
        "hybrid_mrr": round(statistics.fmean(hybrid_rr), 4) if hybrid_rr else 0,
        "legacy_hit_rate": round(
            sum(1 for r in legacy_rr if r) / len(legacy_rr), 4) if legacy_rr else 0,
        "hybrid_hit_rate": round(
            sum(1 for r in hybrid_rr if r) / len(hybrid_rr), 4) if hybrid_rr else 0,
    }
    report["latency"] = {
        "legacy_p50_ms": round(statistics.median(legacy_ms), 1) if legacy_ms else 0,
        "hybrid_p50_ms": round(statistics.median(hybrid_ms), 1) if hybrid_ms else 0,
        "legacy_mean_ms": round(statistics.fmean(legacy_ms), 1) if legacy_ms else 0,
        "hybrid_mean_ms": round(statistics.fmean(hybrid_ms), 1) if hybrid_ms else 0,
    }

    # ------------------------------------------------------ paraphrase
    rows = []
    for query, want in PARAPHRASE_PROBES:
        def _relevant(texts: list[str]) -> int:
            for position, body in enumerate(texts, start=1):
                low = (body or "").lower()
                if any(term in low for term in want):
                    return position
            return 0

        try:
            legacy_pos = _relevant(
                [h.text for h in legacy_search(query, top_k)]
            )
        except Exception:  # noqa: BLE001
            legacy_pos = 0
        try:
            hybrid_hits = hybrid.retrieve(query, company_id=company_id,
                                          top_k=top_k)
            hybrid_pos = _relevant([r.text for r in hybrid_hits])
        except Exception:  # noqa: BLE001
            hybrid_pos = 0

        rows.append({
            "query": query,
            "legacy_rank": legacy_pos or None,
            "hybrid_rank": hybrid_pos or None,
        })

    legacy_para = [1.0 / r["legacy_rank"] for r in rows if r["legacy_rank"]]
    hybrid_para = [1.0 / r["hybrid_rank"] for r in rows if r["hybrid_rank"]]
    report["paraphrase"] = {
        "probes": len(rows),
        "legacy_mrr": round(sum(legacy_para) / len(rows), 4),
        "hybrid_mrr": round(sum(hybrid_para) / len(rows), 4),
        "legacy_found": len(legacy_para),
        "hybrid_found": len(hybrid_para),
        "detail": rows,
    }
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--company", default="CIPLA")
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--json", default=None)
    args = parser.parse_args()

    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2
    db = sessionmaker(bind=create_engine(url, pool_pre_ping=True))()

    report = run(db, args.company, top_k=args.top_k)

    print(f"\n{'=' * 62}\nRETRIEVAL BENCHMARK — {report['ticker']}\n{'=' * 62}")
    print(f"chunks {report['chunks']}   legacy vectors "
          f"{report['legacy_vectors']}   semantic vectors "
          f"{report['semantic_vectors']}")
    print(f"semantic signal active: {report['semantic_available']}")

    k = report["known_item"]
    print(f"\nKNOWN-ITEM ({k['probes']} probes)")
    print(f"  {'':<14}{'legacy':>10}{'hybrid':>10}")
    print(f"  {'MRR':<14}{k['legacy_mrr']:>10}{k['hybrid_mrr']:>10}")
    print(f"  {'hit rate':<14}{k['legacy_hit_rate']:>10}{k['hybrid_hit_rate']:>10}")

    p = report["paraphrase"]
    print(f"\nPARAPHRASE + MULTILINGUAL ({p['probes']} probes)")
    print(f"  {'MRR':<14}{p['legacy_mrr']:>10}{p['hybrid_mrr']:>10}")
    print(f"  {'found':<14}{p['legacy_found']:>10}{p['hybrid_found']:>10}")
    for row in p["detail"]:
        print(f"    {str(row['legacy_rank'] or '-'):>3} -> "
              f"{str(row['hybrid_rank'] or '-'):>3}   {row['query'][:48]}")

    lat = report["latency"]
    print(f"\nLATENCY")
    print(f"  {'p50 ms':<14}{lat['legacy_p50_ms']:>10}{lat['hybrid_p50_ms']:>10}")
    print(f"  {'mean ms':<14}{lat['legacy_mean_ms']:>10}{lat['hybrid_mean_ms']:>10}")

    if args.json:
        with open(args.json, "w") as handle:
            json.dump(report, handle, indent=1)
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
