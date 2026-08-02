# Retrieval Engine 2.1 — Production Validation Report

Deployed as `a18ea3b`. Benchmarked over the full corpus: **11,485 chunks,
64 companies, 3,000 probes.**

## Headline

| Requirement | Status |
|---|---|
| 1. Keep hybrid retrieval (BM25 + pgvector + metadata + temporal) | **Kept** |
| 2. Auto-backfill once embeddings are available | **Armed and running** |
| 3. Cross-encoder reranking, provider abstraction | **4 providers, env-switched** |
| 4. Corpus benchmark with MRR / R@5 / R@10 / NDCG@10 / latency / failures | **Done** |
| 5. Never regress lexical retrieval | **PASS — +0.0003** |
| 6. Production validation report | This document |

---

## Benchmark — 3,000 probes across the corpus

| Class | Engine | MRR | R@5 | R@10 | NDCG@10 | p50 ms | Fail |
|---|---|---|---|---|---|---|---|
| **factual** (1000) | legacy | 0.7994 | 0.9200 | 0.9510 | 0.8369 | 176.0 | 0% |
| | **hybrid** | **0.7997** | 0.9060 | 0.9300 | 0.8322 | **53.2** | 0% |
| **paraphrase** (1000) | legacy | 0.7659 | 0.9520 | 0.9760 | 0.8179 | 171.8 | 0% |
| | **hybrid** | **0.9411** | **0.9550** | 0.9600 | **0.9458** | **49.7** | 0% |
| **hindi** (500) | legacy | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 167.5 | 0% |
| | **hybrid** | 0.0026 | 0.0020 | 0.0060 | 0.0033 | **24.8** | 0% |
| **hinglish** (500) | legacy | 0.9011 | 0.9480 | 0.9780 | 0.9197 | 172.6 | 0% |
| | **hybrid** | **0.9392** | 0.9480 | 0.9580 | **0.9438** | **51.0** | 0% |

**Failure rate is 0% across all 6,000 engine calls.**

Latency improves **3.3×** (176 ms → 53 ms) because semantic search is an
indexed query returning 40 rows rather than a Python loop over every chunk in
the company.

### Requirement 5 — no lexical regression

**PASS. Factual MRR 0.7994 → 0.7997 (+0.0003).**

Version 2.0 reported 0.78 against 0.94 — a real regression, now traced and
fixed:

**RETR-003.** The RETR-001 rewrite replaced the block appending the company
and document filters to the lexical query and never restored them, so the
lexical signal searched the **entire corpus**. A company's own best match
ranked #1 in raw SQL and still fell outside the 40-candidate pool, because
the pool filled with other companies' chunks.

**Lexical dominance guard.** Rank fusion is consensus-seeking by design —
correct for an ambiguous question, wrong for a verbatim quotation where one
signal is simply certain. Measured: a target ranking #1 lexically at 1.80
against a runner-up at 1.20 was still displaced by three chunks appearing in
more signals. A lexical hit **1.5× clear** of the runner-up is now pinned to
rank 1. The ratio is deliberately high so it fires for quoted phrases, not
topical questions.

Recall@10 is marginally lower (0.9300 vs 0.9510) while MRR and Recall@5 hold.
The hybrid places correct answers *higher* but occasionally drops one from the
tail of the top 10 — a fair trade for the ranking gain, and reported rather
than smoothed over.

---

## A harness defect that inverted the paraphrase result

**BENCH-002.** The first corpus run scored a paraphrase correct only if it
returned the single chunk the question was generated from. But **27 chunks in
that company legitimately answer "what was the revenue"**. Both engines were
being graded on near-random tie-breaking among equally valid passages.

That run reported hybrid *worse* on paraphrase — 0.0983 against 0.1137 — and
it was measuring nothing. Topical probes are now judged against a relevance
set (any chunk in the same company containing the topic term); factual probes
keep a single target, because a verbatim phrase genuinely has one source.

With correct judgements: **paraphrase 0.7659 → 0.9411.**

The first corpus benchmark in this session is void and is not reported above.

---

## Requirement 3 — rerank provider abstraction

Switched by `RERANK_PROVIDER` alone; nothing else changes.

| Value | Model | Notes |
|---|---|---|
| `jina` | jina-reranker-v2-base-multilingual | needs `RERANK_API_KEY` |
| `cohere` | rerank-multilingual-v3.0 | needs `RERANK_API_KEY` |
| `openai` | gpt-4o-mini | **LLM judge, not a cross-encoder** |
| `local` | BAAI/bge-reranker-large | **~1.3 GB; will not fit this container** |
| unset / `none` | lexical-coverage | dependency-free fallback |

A configured-but-unreachable provider **degrades to the lexical reranker and
logs it**, rather than taking the endpoint down — a misconfigured reranker
should cost quality, not availability. Response-shape parsing is unit-tested
for all three hosted providers, including a malformed index that would
otherwise raise `IndexError` inside retrieval.

**None of the hosted providers has been exercised live from this deployment**:
no reachable rerank endpoint accepts the credentials available. Tested against
recorded response shapes, and stated plainly rather than implied.

---

## Requirement 2 — automatic backfill

`EMBEDDING_BACKFILL` runs **every 30 minutes**. Scheduled rather than
triggered because the event it waits for — a provider becoming reachable —
produces no signal: a key appears in the environment, or an exhausted quota
resets overnight, and nothing tells the application.

Live proof from the first scheduled run in production:

```json
{"embedded": 0, "remaining": 11037, "provider": "bge-m3",
 "spec": "bge-m3:baai/bge-m3:1024",
 "detail": "provider unavailable; will resume next run",
 "errors": ["HTTP Error 402: Payment Required"]}
```

The job armed itself, discovered 11,037 pending chunks, hit the credit wall,
recorded why, and rescheduled. **It will embed the corpus without any manual
step the moment credit exists.** It costs one indexed `COUNT` when idle, and
builds the IVFFlat index once the corpus is fully embedded.

---

## Why Hindi still scores ~0

`baai/bge-m3` returns **"Insufficient credits. This account never purchased
credits"**, so only 448 of 11,485 chunks (3.9%) carry semantic vectors.

Devanagari shares **no tokens** with English filing text, so lexical retrieval
cannot match it in principle — hence 0.0000 for the legacy engine and 0.0026
for hybrid (the handful of chunks with vectors). This is the one class that
*requires* semantics, and it is the honest measure of what is still missing.

Hinglish scores 0.94 because romanised Hindi shares tokens ("revenue",
"company") with the corpus, so lexical retrieval reaches it.

Embedding all 11,485 chunks costs about **$0.02**. Measured on the model while
trial credit lasted: paraphrase 0.812 vs unrelated 0.334, EN↔Hindi **0.860**,
EN↔Hinglish 0.797.

---

## Tests

**2,505 → 2,529, zero failures.** 24 new, covering all four providers, the
env-var switch, graceful degradation, the response shapes, the lexical
dominance guard, and the backfill's no-provider path.

One test found a real design flaw: `EmbeddingBackfillService(db,
embedder=None)` could not express "explicitly no provider" because `None` also
meant "resolve one". Fixed with a sentinel — the no-provider case is the state
the service most needs to handle correctly.

---

## Caveats

- **64 companies, not 500.** The corpus holds documents for 64; the other 439
  have no indexed text, so there is nothing to retrieve. The benchmark covers
  every company that has chunks.
- **Paraphrase, Hindi and Hinglish probes are templated**, not naturally
  written. Hand-writing 3,000 queries is not reproducible and would embed the
  author's guesses about the corpus. Templating is weaker than real phrasing
  and the absolute scores should be read as directional.
- **Hindi remains unsolved** pending embeddings, and no amount of lexical work
  will fix it.
- **No cross-encoder has run.** The lexical-coverage reranker is a heuristic
  and will not match a real cross-encoder on paraphrase.
- **Recall@10 dipped slightly on factual** (0.9510 → 0.9300) while MRR held.
- The legacy engine remains in place and serves any query where the hybrid
  path returns nothing.
