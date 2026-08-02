# From Advanced RAG to AI Memory System

Deployed as `dc9aa29`. Verified end to end in production.

## The gap this closes

The audit found the platform classified as **Advanced RAG** for one reason:
every memory service existed, was tested and worked — and nothing called any of
them automatically. The measurement was blunt:

> 163 documents had been ingested since the vault was last built, and not one
> had produced a vault entry.

One wire was missing. `DocumentIngestionService.run_job()` now enqueues
`JobKind.MEMORY_ENRICHMENT` when a document reaches `completed`.

## Verified in production, no manual API call

A PDF was uploaded to Cipla and nothing else was touched:

| | Before | After |
|---|---|---|
| Vault entries | 8 | **18** |
| AI Notes | **0** | **2** |
| Summaries | 0 | **27** |
| Observations | 5 | 6 |
| Enrichment jobs | 0 | 1 (`queued → running → succeeded`) |

Stage-by-stage, from the job's own record:

```
financial_promotion  ok=True  skipped  no unpromoted extracted figures
vault                ok=True  written=8   8 facts considered
ai_notes             ok=True  written=2
summaries            ok=True  written=27  3 documents
observations         ok=True  skipped     all 2 observable years covered
temporal_link        ok=True  FY2027 re-judged against FY2026
```

Then the 163-document backlog was drained through the same path — 38 jobs, all
succeeded:

| | Audit | Now |
|---|---|---|
| Vault entries | 364 | **555** |
| Companies with a vault | 25 | **53** |
| Summaries | 19 | **1,009** |
| Observations | 15 | **64** |
| **Documents newer than the newest vault entry** | **163** | **0** |

## Architecture

Six stages, in dependency order:

1. **Financial promotion** — extracted figures into canonical facts
2. **Vault** — `KnowledgeIngestor`, versioned assertions
3. **AI Notes** — the section that never had a producer
4. **Summaries** — `SummaryService`, per document
5. **Observations** — `TemporalMemoryService`, per fiscal year
6. **Temporal link** — re-judge the latest year once its predecessor exists

The **knowledge graph is deliberately absent** from this list. It already
updates synchronously during ingestion (`services/documents/service.py:370`),
where an edge seen in a second document is *merged* and its weight
incremented. Re-running it here would double every weight on every upload.

### It runs outside the document worker

Not tidiness. Production has crashed **three times** in a 1 GB container while
the document worker held a large PDF — the most recent, found while starting
this work, was a 62-page file producing 516 chunks that occupied the process
for 293 seconds. Adding LLM summarisation and observation generation to that
loop would have guaranteed a fourth.

Stabilised separately: 14 documents wedged in `processing` by earlier crashes
were requeued (a crash leaves them stuck forever, never retried and never
failed), and 11 PDFs over 12 MB were deferred pending a streaming fix.

### Extracted figures can never displace filed ones

Facts read out of a PDF enter at `Precedence.ALIAS` (3), strictly below the
`STORE` tier (2) that screener.in writes. Proven on UltraTech:

```
FY2025  75,955  precedence 2  screener.in      <- resolver serves this
FY2025  75,777  precedence 3  document:108     <- extracted, never served
FY2026  88,512  precedence 2  screener.in
FY2026  88,512  precedence 3  document:108     <- independent corroboration
```

`AnalysisService` resolves FY2025 revenue to **75,955**, ignoring the
extraction. A regex can fill a gap; it cannot overwrite an audited figure.

Only **three of ten** extracted FINANCIAL fields are promotable
(`revenue`, `cash_and_equivalents`, `capex`). The rest — PAT, EBITDA, EPS, FCF,
CFO, net worth — are quantities the statement builders *derive*; storing them
would create a second source of truth. A first draft of that map named `pat`,
`cfo` and `net_worth`, **none of which exist as canonical line items**; a test
now checks every mapping against `LineItem`.

### The AI answers from memory first

`render_evidence()` sorts `KNOWLEDGE` ahead of `DOCUMENT` and states the
precedence in the prompt. Ordering alone does not achieve this — a model given
eight vault entries and ten raw chunks will quote whichever reads best — so the
instruction is explicit, and it is suppressed when there is no memory to prefer.

## Defect found by the new tests

`JobQueue.enqueue` deduplicates on the **whole payload**. Including
`document_id` made every document in a burst a distinct job, so a crawl
delivering twenty filings for one company would schedule twenty full vault
rebuilds. Caught by a test that asserted one job and observed five. The payload
now carries only `company_id`; the document id moved to the job's resource
fields, where it does not affect the key.

## Resilience

- **Stages are individually guarded.** A rate-limited summariser must not stop
  the vault from updating. Proven in production during this work: the LLM quota
  was exhausted mid-backfill and the structural stages still completed —
  vault 364 → 555 with the LLM half degraded.
- **LLM stages are skippable**, so a deployment with no provider still gets the
  structural half of memory rather than nothing.
- **Hourly safety-net sweep** re-enriches any company whose documents have
  outrun its memory — the audit's own comparison, now self-healing.
- **A failed enqueue never fails the document.** A parsed, chunked, searchable
  document is a success; marking it failed would trigger a reprocessing loop.

## Caveats

- **The OpenRouter free tier is 50 requests/day and is currently exhausted**
  (`Rate limit exceeded: free-models-per-day`). This is why **52 of 64
  observations are fallbacks** and AI Notes stopped at 2. Fallbacks are stored
  as `superseded` and never served as analysis — verified: zero fallback
  observations have `status='current'`. The hourly sweep will regenerate them
  as quota returns. Ten dollars of credit lifts the cap to 1,000/day.
- **Financial promotion contributes little.** Only 11 promotable facts exist
  corpus-wide, and the extractor found none in the probe PDF. Some extractions
  are also visibly wrong — L&T "revenue" of 637 crore — which is precisely why
  they enter below the filed tier and are gated at 0.75 confidence.
- **Summaries and observations are capped per pass** (3 documents, 2 years) so
  one lease cannot time out. A company with eighty filings drains over several
  passes rather than in one.
- **The 12 MB document deferral is a workaround, not a fix.** Those PDFs need
  streaming or page-batched parsing before they can be processed in a 1 GB
  container.
- **Embeddings remain hashed n-grams, not semantic**, and there is still no
  vector database. This work changed what the platform *remembers*, not how it
  *retrieves*. Both audit findings stand.

## Reclassification

**AI Memory System.** Every uploaded document now permanently enriches the
company brain with no manual step, the vault is read ahead of RAG, and the
163-document gap is zero.

Not yet *Persistent Learning AI*: nothing adapts from outcomes. Confidence is
the model's own and is never calibrated against what actually happened, and no
prior-year verdict has yet fired on live data.
