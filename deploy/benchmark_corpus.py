#!/usr/bin/env python3
"""Corpus-wide retrieval benchmark: legacy vs hybrid, four query classes.

Reports MRR, Recall@5, Recall@10, NDCG@10, latency and failure rate for:

  * factual   — a phrase lifted verbatim from a chunk (known-item)
  * paraphrase— the same passage asked for in different words
  * hindi     — the question in Devanagari
  * hinglish  — the question in romanised Hindi

The factual class is objective: the chunk the phrase came from is by
definition the right answer, so no human judgement enters. The other three
are generated from the SAME target chunk, so the correct answer is known for
those too — which is what makes a paraphrase score comparable to a factual
one rather than a matter of opinion.

Paraphrase, Hindi and Hinglish queries are TEMPLATED from the target's
salient terms rather than written by hand. Hand-writing 1,000 of each is not
reproducible and would embed the author's guesses about what the corpus says.
Templating is weaker than natural phrasing and is stated as such in the
report.

    export DATABASE_URL=...
    python3 deploy/benchmark_corpus.py --factual 1000 --paraphrase 1000 \
        --hindi 500 --hinglish 500
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import re
import statistics
import sys
import time
from collections import Counter
from dataclasses import dataclass, field

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

_WORD = re.compile(r"[A-Za-z][A-Za-z0-9'-]{3,}")

#: Generated query text -> the topic term it was built from, so a probe can
#: be graded against every passage about that topic.
_TERM_FOR_QUERY: dict[str, str] = {}

#: Financial vocabulary that signals what a passage is ABOUT. A paraphrase
#: built from these is a question about the topic rather than a restatement
#: of the words, which is the distinction being measured.
_TOPIC_TERMS: dict[str, tuple[str, str, str]] = {
    # term        english question      hindi                 hinglish
    "revenue": ("what was the revenue", "राजस्व कितना था",
                "revenue kitna tha"),
    "profit": ("what was the profit", "लाभ कितना था", "profit kitna tha"),
    "margin": ("what were the margins", "मार्जिन क्या था",
               "margin kya tha"),
    "dividend": ("what dividend was declared", "लाभांश क्या घोषित हुआ",
                 "dividend kya declare hua"),
    "capex": ("what was the capital expenditure", "पूंजीगत व्यय कितना था",
              "capex kitna tha"),
    "debt": ("what is the debt position", "कर्ज की स्थिति क्या है",
             "debt kitna hai"),
    "growth": ("what was the growth", "वृद्धि कितनी थी", "growth kitni thi"),
    "guidance": ("what is the guidance", "मार्गदर्शन क्या है",
                 "guidance kya hai"),
    "risk": ("what are the risks", "जोखिम क्या हैं", "risk kya hain"),
    "director": ("who are the directors", "निदेशक कौन हैं",
                 "director kaun hain"),
    "auditor": ("who is the auditor", "लेखा परीक्षक कौन है",
                "auditor kaun hai"),
    "acquisition": ("what acquisition was made", "क्या अधिग्रहण हुआ",
                    "acquisition kya hua"),
    "expansion": ("what expansion is planned", "विस्तार की योजना क्या है",
                  "expansion ka plan kya hai"),
    "cash": ("how much cash is held", "नकदी कितनी है", "cash kitna hai"),
    "export": ("what are the exports", "निर्यात कितना है",
               "export kitna hai"),
}


def _relevance_sets(db, probes: list[Probe]) -> dict[str, set[int]]:
    """Chunk ids that legitimately answer each topical query, per company.

    Keyed by "company_id|term" so the same question against two companies
    keeps separate answer sets.
    """
    wanted: set[tuple[str, str]] = set()
    for probe in probes:
        if probe.kind == "factual":
            continue
        term = _TERM_FOR_QUERY.get(probe.query)
        if term:
            wanted.add((probe.company_id, term))

    out: dict[str, set[int]] = {}
    for company_id, term in wanted:
        rows = db.execute(text("""
            SELECT c.id FROM document_chunks c
            JOIN documents d ON d.id = c.document_id
            WHERE d.company_id = :cid AND lower(c.text) LIKE :pat
        """), {"cid": company_id, "pat": f"%{term}%"}).all()
        out[f"{company_id}|{term}"] = {r[0] for r in rows}
    return out


def _rank_of(probe: Probe, ids: list[int],
             relevant: dict[str, set[int]]) -> int:
    """Rank of the first CORRECT result, or 0.

    Factual probes have one right answer. Topical probes are graded against
    the relevance set, falling back to the generating chunk if the set is
    somehow empty.
    """
    if probe.kind == "factual":
        return next((i for i, c in enumerate(ids, 1) if c == probe.target), 0)

    term = _TERM_FOR_QUERY.get(probe.query)
    allowed = relevant.get(f"{probe.company_id}|{term}") if term else None
    if not allowed:
        allowed = {probe.target}
    return next((i for i, c in enumerate(ids, 1) if c in allowed), 0)


@dataclass(slots=True)
class Probe:
    kind: str
    query: str
    target: int
    company_id: str


@dataclass(slots=True)
class Metrics:
    """Ranking metrics for one engine on one query class."""

    ranks: list[int] = field(default_factory=list)      # 0 = not found
    latencies: list[float] = field(default_factory=list)
    failures: int = 0

    @property
    def n(self) -> int:
        return len(self.ranks)

    @property
    def mrr(self) -> float:
        if not self.ranks:
            return 0.0
        return sum(1.0 / r if r else 0.0 for r in self.ranks) / len(self.ranks)

    def recall_at(self, k: int) -> float:
        if not self.ranks:
            return 0.0
        return sum(1 for r in self.ranks if r and r <= k) / len(self.ranks)

    @property
    def ndcg_at_10(self) -> float:
        """NDCG@10 with a single relevant document per query.

        With one relevant item the ideal DCG is 1.0, so NDCG reduces to
        1/log2(rank+1). Stated explicitly because NDCG is often quoted with
        graded relevance and these judgements are binary.
        """
        if not self.ranks:
            return 0.0
        return sum(
            (1.0 / math.log2(r + 1)) if r and r <= 10 else 0.0
            for r in self.ranks
        ) / len(self.ranks)

    @property
    def failure_rate(self) -> float:
        total = len(self.ranks) + self.failures
        return self.failures / total if total else 0.0

    def as_dict(self) -> dict[str, object]:
        return {
            "queries": self.n,
            "mrr": round(self.mrr, 4),
            "recall@5": round(self.recall_at(5), 4),
            "recall@10": round(self.recall_at(10), 4),
            "ndcg@10": round(self.ndcg_at_10, 4),
            "p50_ms": round(statistics.median(self.latencies), 1)
            if self.latencies else 0.0,
            "p95_ms": round(
                statistics.quantiles(self.latencies, n=20)[18], 1
            ) if len(self.latencies) >= 20 else (
                round(max(self.latencies), 1) if self.latencies else 0.0
            ),
            "failures": self.failures,
            "failure_rate": round(self.failure_rate, 4),
        }


def build_probes(db, counts: dict[str, int], seed: int = 17) -> list[Probe]:
    """Generate probes across every company that has indexed text."""
    rows = db.execute(text("""
        SELECT c.id, c.text, d.company_id
        FROM document_chunks c
        JOIN documents d ON d.id = c.document_id
        WHERE d.status = 'completed' AND length(c.text) > 500
    """)).all()
    if not rows:
        return []

    random.seed(seed)
    probes: list[Probe] = []

    # -- factual: a verbatim phrase from the middle of the chunk ----------
    pool = random.sample(rows, min(counts["factual"], len(rows)))
    for chunk_id, body, company_id in pool:
        words = re.findall(r"[A-Za-z0-9₹%.,()-]+", body)
        if len(words) < 24:
            continue
        start = len(words) // 3
        phrase = " ".join(words[start:start + 12])
        if len(phrase) > 25:
            probes.append(Probe("factual", phrase, chunk_id, company_id))

    # -- topical: paraphrase / hindi / hinglish from the same target ------
    # Only chunks containing a known topic term qualify: the question must
    # have an answer in the target, or the probe measures nothing.
    topical = [
        (cid, body, company, term)
        for cid, body, company in rows
        for term in _TOPIC_TERMS
        if term in body.lower()
    ]
    random.shuffle(topical)

    for kind, index in (("paraphrase", 0), ("hindi", 1), ("hinglish", 2)):
        wanted = counts[kind]
        seen: set[int] = set()
        for chunk_id, _body, company_id, term in topical:
            if len(seen) >= wanted:
                break
            if chunk_id in seen:
                continue
            seen.add(chunk_id)
            query = _TOPIC_TERMS[term][index]
            _TERM_FOR_QUERY[query] = term
            probes.append(Probe(kind, query, chunk_id, company_id))

    return probes


def run(db, counts: dict[str, int], *, top_k: int = 10) -> dict:
    legacy = DocumentService(db)
    hybrid = HybridRetrievalEngine(db)

    def legacy_search(query: str, company_id: str) -> list[int]:
        """The OLD engine, called directly.

        BENCH-001: `DocumentService.search()` now routes through the hybrid
        engine, so calling it here would compare the new engine with itself.
        """
        from app.services.documents.pipeline.search import (
            DocumentSearch, SearchConfig,
        )

        store = legacy.build_index(company_id)
        engine = DocumentSearch(store, legacy.embedder, SearchConfig(top_k=top_k))
        return [h.chunk_id for h in engine.search(query, top_k=top_k)]

    probes = build_probes(db, counts)

    # BENCH-002. A topical probe has MANY correct answers.
    #
    # The first corpus run scored a paraphrase correct only if it returned the
    # one chunk the question was generated from — but 27 chunks in that same
    # company legitimately answer "what was the revenue". Both engines were
    # being marked on near-random tie-breaking among equally valid passages,
    # which measures nothing about retrieval quality.
    #
    # Topical probes are therefore judged against a RELEVANCE SET: any chunk
    # in the same company containing the topic term. Factual probes keep a
    # single target, because a verbatim phrase genuinely has one source.
    relevant = _relevance_sets(db, probes)
    by_kind: dict[str, dict[str, Metrics]] = {
        kind: {"legacy": Metrics(), "hybrid": Metrics()}
        for kind in ("factual", "paraphrase", "hindi", "hinglish")
    }

    started = time.perf_counter()
    for index, probe in enumerate(probes, start=1):
        for engine_name in ("legacy", "hybrid"):
            metrics = by_kind[probe.kind][engine_name]
            call_started = time.perf_counter()
            try:
                if engine_name == "legacy":
                    ids = legacy_search(probe.query, probe.company_id)
                else:
                    ids = [
                        r.chunk_id for r in hybrid.retrieve(
                            probe.query, company_id=probe.company_id,
                            top_k=top_k,
                        )
                    ]
            except Exception:  # noqa: BLE001
                metrics.failures += 1
                continue
            metrics.latencies.append((time.perf_counter() - call_started) * 1000)
            metrics.ranks.append(_rank_of(probe, ids, relevant))
        if index % 100 == 0:
            rate = index / max(time.perf_counter() - started, 0.001)
            print(f"  {index}/{len(probes)} probes ({rate:.1f}/s)", flush=True)

    corpus = db.execute(text("""
        SELECT count(*), count(embedding_v2),
               count(DISTINCT d.company_id)
        FROM document_chunks c JOIN documents d ON d.id = c.document_id
    """)).first()

    return {
        "corpus": {
            "chunks": corpus[0],
            "semantic_vectors": corpus[1],
            "companies": corpus[2],
        },
        "semantic_active": bool(corpus[1]) and hybrid.available,
        "probe_counts": dict(Counter(p.kind for p in probes)),
        "results": {
            kind: {name: m.as_dict() for name, m in engines.items()}
            for kind, engines in by_kind.items()
        },
        "elapsed_s": round(time.perf_counter() - started, 1),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--factual", type=int, default=1000)
    parser.add_argument("--paraphrase", type=int, default=1000)
    parser.add_argument("--hindi", type=int, default=500)
    parser.add_argument("--hinglish", type=int, default=500)
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--json", default=None)
    args = parser.parse_args()

    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2
    db = sessionmaker(bind=create_engine(url, pool_pre_ping=True))()

    report = run(db, {
        "factual": args.factual, "paraphrase": args.paraphrase,
        "hindi": args.hindi, "hinglish": args.hinglish,
    }, top_k=args.top_k)

    corpus = report["corpus"]
    print(f"\n{'=' * 78}")
    print("CORPUS RETRIEVAL BENCHMARK")
    print(f"{'=' * 78}")
    print(f"chunks {corpus['chunks']}   companies {corpus['companies']}   "
          f"semantic vectors {corpus['semantic_vectors']}   "
          f"semantic active: {report['semantic_active']}")
    print(f"probes: {report['probe_counts']}    elapsed {report['elapsed_s']}s")

    header = (f"\n{'class':<12}{'engine':<9}{'MRR':>8}{'R@5':>8}{'R@10':>8}"
              f"{'NDCG@10':>9}{'p50ms':>9}{'fail%':>8}")
    print(header)
    print("-" * len(header.strip()) )
    for kind, engines in report["results"].items():
        for name, m in engines.items():
            if not m["queries"]:
                continue
            print(f"{kind:<12}{name:<9}{m['mrr']:>8.4f}{m['recall@5']:>8.4f}"
                  f"{m['recall@10']:>8.4f}{m['ndcg@10']:>9.4f}"
                  f"{m['p50_ms']:>9.1f}{100 * m['failure_rate']:>7.1f}%")

    # Requirement 5 is a pass/fail gate, not a number to admire.
    factual = report["results"]["factual"]
    if factual["legacy"]["queries"] and factual["hybrid"]["queries"]:
        delta = factual["hybrid"]["mrr"] - factual["legacy"]["mrr"]
        verdict = "PASS" if delta >= -0.001 else "FAIL"
        print(f"\nREQUIREMENT 5 — no lexical regression: {verdict} "
              f"(factual MRR {factual['legacy']['mrr']:.4f} -> "
              f"{factual['hybrid']['mrr']:.4f}, delta {delta:+.4f})")

    if args.json:
        with open(args.json, "w") as handle:
            json.dump(report, handle, indent=1)
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
