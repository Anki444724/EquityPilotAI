# AI Architecture Audit

Audit date: 2026-08-02. Deployed commit `9d33f8b`.
Every answer below is from the code as written and from live production data.
Where a claim I made in an earlier session is contradicted by the code, the
code wins and the contradiction is stated.

---

## Verdict table

| # | Capability | Answer |
|---|---|---|
| 1 | PDF Upload | **YES** |
| 2 | PDF Parsing | **YES** |
| 3 | Chunking | **YES** |
| 4 | Embeddings | **YES — but not semantic** |
| 5 | Vector Database | **NO** |
| 6 | RAG Retrieval | **YES** |
| 7 | Financial Statement Extraction | **NO** (from PDFs) |
| 8 | Company Vault | **YES** |
| 9 | AI Notes | **NO** |
| 10 | Historical AI Analysis | **PARTIAL** |
| 11 | Temporal Memory | **YES** |
| 12 | Observation Storage | **YES** |
| 13 | AI learning from newly uploaded PDFs | **NO** |
| 14 | Automatic Vault updates after upload | **NO** |
| 15 | Versioned Memory | **YES** |
| 16 | Knowledge Graph | **PARTIAL** |
| 17 | Long-term AI Memory | **PARTIAL** |
| 18 | Management Guidance Tracking | **YES (storage), NO (populated)** |
| 19 | Previous Prediction Verification | **YES (code), NEVER FIRED (data)** |
| 20 | Permanent learning vs RAG retrieval | **RAG retrieval, with a manually-triggered memory layer** |

---

## 1. PDF Upload — YES

`app/api/v1/documents.py:98-106` — `POST /documents/upload`, `UploadFile = File(...)`.
`app/services/documents/ingestion.py` — `accept()`: *"Validate, persist the
bytes, create the row, enqueue. No parsing."*

Live: 351 completed documents. Upload of a probe PDF during this audit returned
`{"id": 389, "status": "queued"}`.

## 2. PDF Parsing — YES

`app/services/documents/extractors/pdf.py` (PyMuPDF), `tables.py` (pdfplumber),
`ocr.py` (pytesseract). `requirements.txt:27-29` — `pymupdf==1.28.0`,
`pdfplumber==0.11.10`, `pytesseract==0.3.13`.

OCR degrades rather than fails: `ocr.py:98` — `if shutil.which("tesseract") is
None: logger.info("OCR unavailable: tesseract binary not on PATH")`.

Live: 2,225 `document_pages`, 659 `document_tables`.

## 3. Chunking — YES

`app/services/documents/pipeline/chunking.py:74` — `class SemanticChunker`,
invoked at `orchestrator.py:213` (`ProcessingStage.CHUNKING`).

Live: **8,758 chunks**.

## 4. Embeddings — YES, but they are not semantic

Vectors exist and are computed for every chunk:
`orchestrator.py:216` (`ProcessingStage.EMBEDDING`);
`app/models/document.py:205` — `embedding: Mapped[list | None] = mapped_column(JSON)`.

Live: **8,758 of 8,758 chunks embedded**, dimension 384.

**The model is not a neural embedder.**
`app/services/documents/pipeline/embeddings.py:176` —
`class HashingEmbeddingProvider`: *"Deterministic local embedder: hashed word
and character n-grams."* `_add()` uses `hashlib.blake2b`. There is no
`torch`, `transformers`, `sentence-transformers` or `numpy` in
`requirements.txt`.

Its own docstring is candid: *"It captures lexical and morphological
similarity well and semantic paraphrase poorly."*

`OpenAIEmbeddingProvider` exists (`embeddings.py:230`) and is honest about its
status: *"It has never been exercised against the live API here — there is no
key in this environment."* `build_embedder()` falls back to hashing unless
both `provider == "openai"` and a key are supplied.

Live confirmation — every document in production carries:
```
embedding_spec = 'local-hashing:hash-4g:384'   (295 documents)
```

So: vectors YES, **semantic embeddings NO**.

## 5. Vector Database — NO

`app/services/documents/pipeline/vector_store.py:166` —
`class InMemoryVectorStore(VectorStore)`: *"Exact hybrid search over an
in-process index."*

There is no persistent index. `app/services/documents/service.py:460-499` —
`build_index()` constructs a **fresh `InMemoryVectorStore` on every call** and
loads every chunk row for the company into it.

`app/models/document.py:17` states the position plainly: *"Embeddings are
stored as JSON rather than a native vector column… The vector store
abstraction is where a pgvector implementation would go."*

Live: `select count(*) from pg_extension where extname='vector'` → **0**.
No pgvector, no FAISS, no Pinecone, no Qdrant, no Chroma anywhere in the
dependency tree.

Vectors are **persisted in Postgres as JSON** and **searched in Python**.
That is not a vector database.

## 6. RAG Retrieval — YES

`app/services/documents/pipeline/search.py:66` — `class DocumentSearch`;
`search()` at line 80 embeds the query (line 91) and calls `store.search()`.

Retrieval is **hybrid**, and the weighting is deliberately lexical-dominant
because of the embedder's weakness — `vector_store.py:171-173`:
```
LEXICAL_WEIGHT = 0.55
SEMANTIC_WEIGHT = 0.45
```
with a real BM25 implementation at `vector_store.py:89` (`class BM25Index`).

Wired to the analyst: `app/services/ai/analyst.py:215` — `_retrieve()` calls
`service.search(question, company_id=..., top_k=self.RETRIEVAL_TOP_K)`.

## 7. Financial Statement Extraction from PDFs — NO

A financial *field* extractor exists — `pipeline/financials.py`, with
`LabelRule` (line 119), `ProseRule` (line 300), `match_label()` (line 260) —
and it runs (`orchestrator.py:210`). It produces **437 `document_facts`**.

But those never become canonical financials. Every writer of `FinancialFact`
in the codebase is:

```
app/data/ingest.py:411          screener.in / Yahoo
app/data/enrich.py:98           derived
app/data/derive_wc.py:211,306   derived
app/db/seed.py:176,437          seed data
app/services/us_pipeline/provisioning.py:246   FMP (US only)
```

**No file under `app/services/documents/` writes `FinancialFact`.** The
canonical 134,700 financial facts come from screener.in, not from any PDF.

So: narrative and metric fields are extracted from PDFs; **income statement,
balance sheet and cash flow are not**.

## 8. Company Vault — YES

`app/models/knowledge.py:39` — `class KnowledgeEntry`;
`app/domain/knowledge/vault.py:33` — `class VaultSection` (20 sections);
`app/services/knowledge/vault.py:105` — `assert_knowledge()`.

Live: **364 entries, 208 current, 156 superseded**, across 14 of 20 sections.

## 9. AI Notes — NO

`VaultSection.AI_NOTES` is declared (`domain/knowledge/vault.py:61`) and
**nothing writes to it**. A grep for `AI_NOTES` across `app/` returns only the
enum declaration itself.

Live: `select count(*) from knowledge_entries where section='ai_notes'` → **0**.

I described this section as implemented in an earlier session. It is a
declared enum member with no producer.

## 10. Historical AI Analysis — PARTIAL

The section exists and is populated, but only as a **relabelling of extracted
MD&A text**, not as stored AI analysis:
`app/services/knowledge/ingest.py:45` — `"MD&A": VaultSection.HISTORICAL_AI_ANALYSIS`.

Live: **7 entries**. Generated reports are not persisted into this section.

## 11. Temporal Memory — YES

`app/domain/knowledge/temporal.py` — `YearObservation`, `GuidanceVerdict`,
`credibility_score()`.
`app/models/knowledge.py:200` — `class YearlyObservation`.
`app/services/knowledge/temporal.py:84` — `class TemporalMemoryService`;
`build_company()` (line 338) iterates years chronologically.
Migration `c9d5f8a3b204`.

Live: **15 observation rows, 6 current, across 3 companies** (Cipla, Sun
Pharma, IOC) — the only companies with documents spanning more than one
fiscal year.

## 12. Observation Storage — YES

`temporal.py:615` — `_persist()`, inserting a new version and superseding the
previous. Read paths: `timeline()` (line 105), `history()` (line 118).
API: `GET /company/{ticker}/observations`, `/observations/{fy}/history`.

## 13. AI learning from newly uploaded PDFs — NO

This is the central finding, and it is measured rather than argued.

The pipeline's `KNOWLEDGE` stage does **not** touch the vault —
`orchestrator.py:221-225`:
```python
with stage(ProcessingStage.KNOWLEDGE):
    graph = None
    if build_graph:
        builder = KnowledgeGraphBuilder(company_name, company_ticker)
        graph = builder.add_entities(entities)
```
It builds the entity graph and stops. `KnowledgeIngestor` is not imported by
the orchestrator, the worker, or `handle_document_processing`.

Live evidence — the newest completed document at audit time (id 368,
ingested 14:11 today):

| Layer | Rows for doc 368 |
|---|---|
| chunks | 15 |
| document_facts | 1 |
| document_entities | 5 |
| document_relations | 3 |
| **knowledge_entries (Vault)** | **0** |
| **document_summaries** | **0** |

And across the corpus:
```
newest document created_at      : 2026-08-02 14:13:44
newest vault entry created_at   : 2026-08-01 18:07:04
documents newer than the newest vault entry : 163
```

**163 documents have been ingested since the vault was last built, and not one
of them produced a vault entry.** Of 351 completed documents, 350 have chunks
but only **82** have any vault entry and only **3** have summaries.

The AI can *retrieve* from a new PDF immediately. It does not *learn* from it.

## 14. Automatic Vault updates after upload — NO

`KnowledgeIngestor` and `TemporalMemoryService` are referenced from exactly
one place in the codebase — `app/api/v1/knowledge.py` (lines 186, 189, 199,
201, 302, 341, 371, 395) — i.e. **manual HTTP endpoints only**.

There is no `JobKind.KNOWLEDGE_BUILD`. The registry
(`app/domain/platform/jobs.py:53-64`) contains twelve kinds; none builds the
vault, summaries or observations.

The one automatic post-ingestion hook, `handle_filing_post_process`
(`handlers.py:401`), calls `PostFilingProcessor`, which **rescores** the
company. A grep for `Knowledge|vault|Temporal|Summary` in
`app/services/filings/post_filing.py` returns nothing.

## 15. Versioned Memory — YES

`app/services/knowledge/vault.py:105` `assert_knowledge()` — never updates;
inserts at `version + 1` and sets `superseded_by`.
Ordering is by **fiscal period → authority → confidence**, never ingestion
time (`domain/knowledge/vault.py` module docstring).

Live: max version **25** on a single key (`financial_statements.pat`);
156 superseded rows retained with their evidence. Observations version
identically — Cipla FY2027 holds v1 (0.20), v2 (0.75), v3 (0.90, current).

## 16. Knowledge Graph — PARTIAL

Real and persisted: `app/models/document.py:304` — `class DocumentRelation`;
builder at `pipeline/knowledge_graph.py:193`.

Live: **928 edges, 1,623 entities**:
```
operates_in 285 · exposed_to_risk 268 · director_of 249 · subsidiary_of 164
guides 72 · audited_by 26 · invests_in 15 · acquired 14 · sells_product 8
promoter_of 6
```

The brief's chain is *Company → Management → Subsidiaries → Competitors →
Suppliers → Customers → Sector → Macro Economy → Government Policy →
Commodities → Forex → Interest Rates → Peers.* `EntityKind`
(`domain/documents/types.py:177`) stops at `AUDITOR` — there is **no macro,
policy, commodity, forex, interest-rate or peer node type**. The company half
exists; the macro half does not.

## 17. Long-term AI Memory — PARTIAL

The storage is genuine and permanent: 364 vault entries with full version
lineage, 15 observations, nothing ever deleted. It is read first in the
prompt — `context_builder.py:206-207` calls `_add_knowledge(context)` **before**
`_add_documents(context)`, and `_add_temporal()` injects the year series.

But it does not accumulate on its own. It grows only when someone calls the
build endpoints, and 163 documents are currently outside it. Memory that
requires a manual trigger to absorb new evidence is durable, not long-term
in the accumulating sense the brief means.

## 18. Management Guidance Tracking — YES (mechanism), thinly populated

`app/models/knowledge.py:226` — `guidance: Mapped[str | None]`.
`temporal.py` captures it in the prompt and stores it via `_compose()`.

Live: **3 of 15 observation rows carry guidance.** Most Indian filings state
none, which the design anticipates.

## 19. Previous Prediction Verification — YES in code, NEVER FIRED in production

The mechanism is implemented and enforced:
- `GuidanceVerdict` — `domain/knowledge/temporal.py:33`
- `build_company()` passes the prior year forward — `temporal.py:344-365`
- `_compose()` forces `NOT_ASSESSABLE` unless the prior year recorded
  guidance — `temporal.py:594-596`
- `credibility_score()` excludes unassessable years from the denominator —
  `domain/knowledge/temporal.py`

It was verified end-to-end on a **controlled fixture**: FY2025 records
"commission Unit-3, cut net debt below 1.0x", FY2026 returns
`verdict=delivered` citing `[E1]`/`[E2]`.

On live data it has **never produced a verdict**:
```
select count(*) from yearly_observations where prior_verdict <> 'not_assessable'
→ 0
```
All 15 rows are `not_assessable`. No real company in the corpus yet has a year
with explicit guidance followed by a year that settles it. The capability is
real; the track record is empty.

## 20. Permanent learning, or RAG retrieval only?

**Predominantly RAG retrieval, with a real but manually-triggered memory layer
on top.**

What happens automatically on upload: parse → OCR → layout → tables →
sections → entities → financial fields → chunk → embed → entity graph.
The document becomes **retrievable** within minutes.

What does not happen automatically: no vault entry, no summary, no
observation, no canonical financial fact. Those require
`POST /company/{ticker}/knowledge/build`, `/knowledge/summarise` and
`/observations/build`.

The evidence is the 163-document gap. The AI answers questions about a new
filing by searching its text at query time. It does not revise what it
believes about the company until instructed to.

---

## Classification

### **Advanced RAG**

Not *Basic RAG*: retrieval is hybrid BM25 + vector with section and document
filters, evidence is cited to page and paragraph, answers are refused when
unsupported, and a genuine versioned knowledge store sits in front of
retrieval in the prompt.

Not an *AI Memory System*: that requires memory to update itself as evidence
arrives. 163 documents currently sit outside the vault, `AI_NOTES` is empty,
and no automatic path connects ingestion to the vault. The memory layer is
built and correct — it is simply not wired to the pipeline.

Not *Persistent Learning AI*: nothing adapts from outcomes. Confidence is the
model's own and is never calibrated against what actually happened. No
verdict has ever fired, so no track record informs any later judgement.

**The gap between Advanced RAG and AI Memory System is small and specific:**
one job kind that calls `KnowledgeIngestor.ingest_company()`, `SummaryService`
and `TemporalMemoryService.build_company()` when a document reaches
`completed`. Every one of those services exists, is tested and works. Nothing
calls them automatically.

---

## Corrections to my earlier statements

1. **"AI Notes"** — I previously described the 20 vault sections as
   implemented. `AI_NOTES` has no producer and 0 rows.
2. **"Knowledge Vault reads first"** — true for the 82 documents that have
   entries, misleading for the 163 that do not.
3. **Prior-year verification** — I reported this as working. It works on a
   fixture; it has never fired on live data. I should have said so more
   plainly at the time.

## Incident during this audit

Production was returning **502** when I began live probing. Deployment
`b239bc0d` (`9d33f8b`, my temporal-memory deploy) showed status **CRASHED** —
it had reported SUCCESS at deploy time and crashed later, while the document
worker processed a large filing backlog. The logs show a clean start
(`Application startup complete`) followed by ingestion of successively larger
PDFs (one at 73,793 ms) before the container died — the same 1 GB memory
exhaustion pattern seen previously, not a defect in the temporal code.

I redeployed the same SHA and service was restored (`health: 200`). The
backlog is still draining and remains a live risk: the container has one
document worker and 1 GB of RAM.

## Caveats

- Counts are from production at 2026-08-02 ~14:15 UTC while a 36-document
  backlog was draining; document totals will have moved.
- The probe PDF (id 389) was still `queued` behind that backlog at the time of
  writing, so Q13/14 rest on document 368 and the 163-document gap rather than
  on the probe.
- This audits the AI/knowledge architecture only. Market data, valuation,
  scoring and portfolio modules were not examined.
