# Retrieval Engine 2.0

Deployed as `331364f`. **pgvector 0.8.6 live on production Postgres 16.14.**

## Requirement status

| # | Requirement | Status |
|---|---|---|
| 1 | pgvector | **Done** — extension enabled, vector column and GIN index live |
| 2 | Semantic embeddings | **Built and verified; not yet backfilled** — see Credits |
| 3 | Hybrid retrieval (4 signals) | **Done** |
| 4 | Reranking | **Interface + local reranker.** Named models not reachable |
| 5 | Synonyms / paraphrase / HI / EN / Hinglish | **Verified on the model; awaiting backfill** |
| 6 | score / confidence / source / chunk | **Done** |

---

## Benchmark

CIPLA, 65 chunks, 25 known-item probes and 8 paraphrase probes, both engines
in one process against the same corpus.

| | Legacy | Hybrid 2.0 |
|---|---|---|
| **Known-item MRR** | **0.9413** | 0.7800 |
| Known-item hit rate | 1.00 | 0.80 |
| **Paraphrase MRR** | 0.5000 | **0.5771** |
| Paraphrase probes found | 5 / 8 | **7 / 8** |
| Latency p50 | **66.7 ms** | 88.6 ms |
| Latency mean | 78.8 ms | 97.7 ms |

Per-probe rank, legacy → hybrid (`-` = not found in top 10):

```
  -  ->  5   How much money did the company make?
  2  ->  1   What does management expect going forward?
  -  ->  4   What could go wrong for this business?
  1  ->  1   Who runs the company?
  1  ->  1   How much cash is on hand?
  -  ->  1   कंपनी का राजस्व कितना है?          <- Hindi, legacy finds nothing
  1  ->  6   company ka revenue kitna hai
  2  ->  -   management guidance kya hai
```

### Reading this honestly

**Hybrid currently loses on known-item, and that is expected.** These probes
are verbatim phrases lifted from chunks — pure lexical matching, which is
exactly what the hashed n-gram engine is good at. The semantic signal that
would recover the gap is not yet active.

**Hybrid already wins on paraphrase and Hindi** with the semantic signal
*switched off*, purely from better lexical query construction and rank
fusion. It finds 7 of 8 against the legacy engine's 5, including the Hindi
question the legacy engine cannot answer at all.

**The comparison is not yet the one the brief asked for.** Both columns are
running without semantic embeddings. The number that matters — semantic
retrieval against hashed n-grams — needs the backfill below.

---

## Credits: why semantic is not yet live

`baai/bge-m3` on OpenRouter now returns:

```
HTTP 402 — "Insufficient credits. This account never purchased credits."
```

Even a 65-token request. Earlier probes in this session succeeded and spent
the last of a trial allowance; I initially diagnosed a per-request token
ceiling and that was **wrong** — the account has simply never had credit.
Embedding all 11,477 chunks costs about **$0.02**.

Verified on the live endpoint while credit lasted:

| Pair | Cosine |
|---|---|
| "revenue grew" vs "sales increased" (paraphrase) | **0.812** |
| "revenue grew" vs "the cat sat on the mat" | 0.334 |
| EN question vs the same question in Hindi | **0.860** |
| EN question vs the same in Hinglish | **0.797** |

One number there justifies the whole hybrid design: *"EN question vs an
unrelated EN question"* scores 0.496 while *"Hindi question vs the English
passage answering it"* scores 0.486. Dense similarity alone cannot separate
those, which is why semantic is fused with BM25 and reranked rather than
trusted on its own.

**Nothing else is blocked.** Add credit (or a Jina/OpenAI key — both already
wired) and run:

```bash
python3 deploy/backfill_embeddings.py          # ~$0.02, 11,477 chunks
python3 deploy/backfill_embeddings.py --index  # IVFFlat
```

The semantic signal activates automatically. No code change.

---

## Architecture

**Four signals, fused by Reciprocal Rank Fusion** rather than a weighted sum.
The scores are not commensurable — BM25 is unbounded, cosine is [-1, 1],
metadata is boolean. The old engine normalised BM25 by the maximum in the
result set, which made a chunk's rank depend on *which other chunks happened
to be retrieved*. RRF takes only the ordering, so no signal's scale can
destabilise the ranking.

* **Semantic** — indexed `ORDER BY embedding_v2 <=> query` inside Postgres.
  The old engine scored all 11,477 chunks in Python per query.
* **Lexical** — Postgres full-text with a GIN index, generated from the text
  so it cannot drift out of sync.
* **Metadata** — fiscal year and document type parsed from the question.
* **Temporal** — recency as a *signal*, not a filter, so an older passage
  that answers the question is not discarded for being old.

**Reranking is pluggable.** `bge-reranker-large` and `jina-reranker-v2` are
not wired, and the reason is measured rather than assumed: OpenRouter lists
**zero** rerank models across all 337 in its catalogue, Jina and Cohere both
answer "authentication required", and a cross-encoder is ~1.3 GB of weights
against a 1 GB container that has already crashed three times. Shipped
instead: a `Reranker` interface, a dependency-free lexical-coverage
implementation (coverage, density, proximity), and a `CrossEncoderReranker`
that needs only three settings. That is stated plainly rather than
substituting something weaker and calling it the same thing.

**Confidence is not the score.** A passage can rank first in a weak field.
Confidence combines absolute semantic similarity, cross-signal agreement, and
the rerank score, so a reader can tell "best available" from "good".

---

## Defects found by benchmarking

**RETR-001 — the lexical signal matched nothing.** Postgres has no
OR-by-default query parser: `plainto_tsquery` AND-joins every term, and so
does `websearch_to_tsquery`. "Who runs the company?" became
`'who' & 'runs' & 'the' & 'company'` and matched **zero** chunks, while
`'director'` alone matched four. Six of eight natural-language probes returned
nothing and paraphrase MRR collapsed to **0.06** against the legacy 0.50. Now
builds an explicit OR of content terms, with an English-only stop list so
Hindi and Hinglish tokens survive.

**RETR-002 — 99% of latency was a dead endpoint.** Every query paid the full
retry ladder against the credit-exhausted embedding API before falling back:
**6,269 ms** per query against **63 ms** for the lexical signal alone. A retry
ladder is right for a transient timeout and wrong for a standing state. Added
a circuit breaker on 401/402/403 — three failing calls went from 18 s to
0.07 s, and p50 latency from 6,269 ms to 89 ms.

**BENCH-001 — my own harness was measuring nothing.** The first benchmark
called `DocumentService.search()` for the "legacy" side, but that method now
routes through the hybrid engine, so it compared the new engine against
itself. The tell was suspiciously identical ranks for every probe. Fixed to
build the in-memory index directly. **The first two benchmark runs in this
session were invalid and are disregarded.**

---

## Tests

**2,476 → 2,505, zero failures.** 29 new, including that fusion rewards
cross-signal agreement, needs no normalisation, that confidence is not a copy
of the score, that Devanagari survives term extraction, that Hinglish content
words are not stripped, that the provider order matches the brief, and that
the builder returns `None` rather than silently downgrading to the hashed
embedder.

---

## Caveats

- **The headline benchmark is not the one the brief asked for.** Both columns
  ran without semantic embeddings. Until the backfill runs, the comparison
  measures rank fusion and query construction, not semantics.
- **Hybrid is behind on known-item retrieval (0.78 vs 0.94).** Verbatim-phrase
  matching is the hashed engine's strength. Whether semantic closes this gap
  is unproven and should be re-measured after the backfill rather than
  assumed.
- **Multilingual capability is verified on the model, not on the corpus.**
  bge-m3 scores EN↔HI at 0.860 in isolation; how it ranks against 11,477 real
  filing chunks is untested.
- **No reranker named in the brief is running.** The local one is a lexical
  heuristic and will not match a cross-encoder on paraphrase.
- **The IVFFlat index does not exist yet** — it clusters the vectors it can
  see, so it is built by the backfill rather than the migration.
- **The benchmark is one company, 65 chunks.** Directionally useful, not a
  corpus-wide result.
- The legacy engine and its 384-dim vectors remain in place and serve every
  query where the hybrid path returns nothing.
