# Module 7 — Document Intelligence Platform

**Status:** complete, awaiting review
**Tests:** 1,129 passing (253 new in this module)
**API:** 59 paths total · 15 document paths · 130 schemas
**Database:** 21 tables (9 new)

---

## 1. What this module is

Modules 2–5 compute. Module 6 explains. Module 7 is the only part of the
platform that *learns something the platform did not already know*.

Everything before this point reasons over data that arrived through the
canonical financial store. This module opens a second intake: a PDF lands, and
by the time the pipeline finishes, the platform holds structured financials,
named entities, a knowledge graph, a searchable index and a set of citations
that every other module can consume.

The organising principle throughout is that **a document is not text — it is a
provenance chain**. Every fact learned here can answer: which document, which
version, which page, which section, which paragraph. A fact that cannot answer
those questions is not evidence, and the pipeline refuses to store it.

---

## 2. Architecture

```
                       ┌──────────────────────────────┐
   upload (bytes)  ──► │  DocumentService             │  ← the only DB-aware layer
                       │  dedup · versioning · queue  │
                       └───────────────┬──────────────┘
                                       │  bytes
                       ┌───────────────▼──────────────┐
                       │  IngestionPipeline (pure)    │  no DB · no HTTP · no vendor
                       └───────────────┬──────────────┘
                                       │
   PARSE ─► OCR ─► LAYOUT ─► TABLES ─► SECTIONS ─► ENTITIES ─► FINANCIALS
        ─► CHUNKING ─► EMBEDDING ─► INDEXING ─► KNOWLEDGE
                                       │
                       ┌───────────────▼──────────────┐
                       │  persistence · vector store  │
                       └───────────────┬──────────────┘
                                       │
              ┌────────────────────────┼────────────────────────┐
              ▼                        ▼                        ▼
       DocumentSearch          KnowledgeGraph            Module 6 AI layer
    (answer + citations)      (nodes + edges)        (EvidenceKind.DOCUMENT)
```

Three layers, and the separation is enforced by test:

| Layer | Knows about | Must not know about |
|---|---|---|
| `app/domain/documents/` | dataclasses, enums, pure helpers | SQLAlchemy, FastAPI, httpx, any model |
| `app/services/documents/pipeline/` | domain types, each other | the database, the ORM |
| `app/services/documents/service.py` | pipeline **and** models | HTTP request/response shapes |
| `app/api/v1/documents.py` | services and schemas | how anything is computed |

`test_document_engine.py::TestArchitecture` greps the domain and pipeline
packages for forbidden imports and fails the build if the boundary is crossed.

### Why the pipeline is pure

`IngestionPipeline.run()` takes bytes and returns an `IngestionResult`. It never
opens a session, writes a file or makes a request. That single decision is what
makes the module testable without fixtures, reusable by the queue worker and a
bulk re-index alike, and profileable stage by stage without instrumentation.

---

## 3. Folder structure

```
backend/app/
├── domain/documents/
│   ├── types.py            Every dataclass and enum. Provenance chain lives here.
│   └── fields.py           GENERATED — the 73 fields of the workbook's AI-2 store
├── services/documents/
│   ├── service.py          Persistence, dedup, versioning, queue, search façade
│   ├── extractors/
│   │   ├── base.py         Parser contract + format registry (decorator-based)
│   │   ├── ocr.py          OcrPolicy (the per-page decision) + Tesseract engine
│   │   ├── pdf.py          PyMuPDF layout + pdfplumber tables + OCR fallback
│   │   ├── tables.py       Units, merged cells, header flattening, confidence
│   │   └── office.py       DOCX · TXT · Markdown · HTML · CSV · XLSX
│   └── pipeline/
│       ├── orchestrator.py The eleven stages, each timed
│       ├── classify.py     Document-type classification (content ≫ filename)
│       ├── sections.py     Heading candidacy + scored classification
│       ├── entities.py     Cue-phrase extraction over regulated language
│       ├── financials.py   Table rules + prose rules → the 73-field store
│       ├── chunking.py     Semantic chunking + deduplication
│       ├── embeddings.py   Provider abstraction + local hashing embedder
│       ├── vector_store.py VectorStore contract + BM25 + hybrid retrieval
│       ├── search.py       Extractive answers + the citation framework
│       └── knowledge_graph.py  Nodes, edges, evidence on every edge
├── models/document.py      9 tables
├── schemas/document.py     Typed request/response contracts
└── api/v1/documents.py     15 endpoints

backend/tests/
├── test_document_engine.py  202 tests — pure pipeline
├── test_document_api.py      51 tests — HTTP + Module 6 integration
└── fixtures/make_docs.py     Realistic Indian-filing PDFs, rendered by PyMuPDF
```

---

## 4. The specification came from the workbook

`AI-2 Extracted Store` and `AI CONTROL CENTER` are the Module 7 spec, and they
were read as one:

- **73 extraction fields** across **16 categories**, each with a declared unit
  and a target sheet
- **12 document-register types**, unioned with the eight analytical classes the
  brief names
- A coverage-and-confidence panel (Section 3) reporting per category

`app/domain/documents/fields.py` is **generated** from the workbook via
`docs/module7_spec.json`, exactly as `line_items.py` was in Module 1. Coverage
is measured against `FIELD_COUNT`, so the answer to "how much did we extract?"
is always "57 of 73", never "a lot".

A test asserts every extraction rule references a real generated field. That
test exists because a rule written for `r_and_d_spend` against a field
generated as `randd_spend` sat silently dead — nothing fails when a rule's key
is wrong, coverage just quietly reports one field fewer.

---

## 5. OCR pipeline

The brief's requirement is precise: *automatically determine whether OCR is
required, and do not OCR machine-readable PDFs unnecessarily*.

**The decision is per page, not per document.** Indian annual reports routinely
interleave a typeset MD&A with scanned, signed auditor certificates. A
document-level flag either misses the scans or corrupts the typeset text.

`OcrPolicy.needs_ocr()` takes three signals per page:

| Signal | Threshold | Rationale |
|---|---|---|
| Absolute character count | < 120 | A page with no text layer |
| Character density per unit area | < 0.06/1000pt² | A large page with a caption on it |
| Raster image coverage | ≥ 55% | What a scan looks like to a PDF parser |

Any one is sufficient. A genuinely near-blank divider page will be sent to OCR
and come back near-blank: wasted work, not a wrong answer, and the safe
direction to err in.

**Verified behaviour** (`tesseract 5.5.0`):

| Document | `used_ocr` | Page source | OCR confidence |
|---|---|---|---|
| Native annual report, 3 pp | `False` | all `native` | — |
| Scanned exchange filing, 1 p | `True` | `ocr` | 0.958 |

Where a page has both a thin text layer and a large image, both are kept and
the page is marked `MIXED` rather than one being discarded.

**If Tesseract is absent the engine raises `OcrUnavailable` rather than
returning empty strings.** A scanned annual report that silently yielded no
text would be indistinguishable from an empty filing, and the platform would go
on to report 0% coverage as though that were a fact about the company.

---

## 6. Table extraction engine

Three preservations, in ascending order of difficulty:

### Units — the one that matters most

An Indian annual report states "(₹ in lakhs)" once, in six-point type above the
table. Every number below it is then a hundredth of what a naive reader
assumes. Getting this wrong produces figures wrong by exactly 100× that look
entirely plausible.

Precedence when resolving a cell's unit:

1. **The cell's own suffix** — a "%" beside a number overrides a ₹ crore table,
   because margin columns sit inside financial tables constantly.
2. **A field whose unit is inherently non-monetary** — a headcount printed
   inside a "₹ in crore" statement is still a headcount. Without this,
   `to_crore()` would turn 19,220 employees into ₹19,220 crore.
3. **The table's declared unit.**
4. **The field spec** — what the number *should* be, which is a weaker claim
   than what the document says it is.

`detect_unit()` returns `UNKNOWN` rather than guessing. Defaulting to ₹ crore
because it is the commonest Indian convention is precisely the silent-default
behaviour that makes a 100× error invisible.

### Merged cells

Header rows are forward-filled and the spans recorded. **Body rows are never
forward-filled** — a blank cell in a financial table means nil or
not-applicable, not "same as the cell to my left". Restricting the repair to
the header is the conservative reading, and the recorded spans let a reviewer
verify it. XLSX merges are read natively from openpyxl rather than inferred.

### Multi-line cell unpacking

pdfplumber returns the region between two ruling lines as a single cell. A
statement drawn with only an outer border and a header rule therefore arrives
as *one* body row whose every cell holds thirteen newline-separated values —
the whole income statement, collapsed.

`unpack_multiline_rows()` expands a row only where **every populated cell splits
into the same number of parts**. Where the counts disagree the row is left
alone, because zipping mismatched columns would pair a label with the wrong
number — an invisible error, far worse than a visible skip.

---

## 7. Section detection

Two stages, because either alone is unreliable.

**1. Heading candidacy** — typography (font size against the document's *median*
body size, weight, case) plus shape (short, no terminal full stop). Median not
mean: a cover page in 48pt would drag a mean upward far enough that genuine
headings stopped clearing the threshold.

**2. Scored classification** — not first-hit-wins, so "Notes to the Financial
Statements" reaches `NOTES_TO_ACCOUNTS` rather than being captured by
`FINANCIAL_STATEMENTS`.

### Sections span block ordinals, not pages

This is the subtler half. Four sections of the reference annual report begin on
page 2. A page-granular lookup attributed all four to whichever started first,
so governance text was cited as ESG. Each `DetectedSection` therefore records
`start_order`/`end_order` over global block ordinals, and `section_for_order()`
is the accurate lookup. `section_for_page()` remains for scans and CSVs, which
have no block structure — at reduced, and clearly reduced, confidence.

**Verified on the reference annual report** — 8 sections, 4 sharing page 2:

| Section | Pages | Blocks | Confidence |
|---|---|---|---|
| Chairman's Letter | 1–1 | 1–2 | 0.85 |
| Business Overview | 1–1 | 3–7 | 0.85 |
| Management Discussion | 1–1 | 8–13 | 0.95 |
| Risk Factors | 2–2 | 14–15 | 0.81 |
| Corporate Governance | 2–2 | 16–19 | 0.87 |
| ESG | 2–2 | 20–21 | 0.95 |
| Auditor Report | 2–2 | 22–23 | 0.94 |
| Financial Statements | 2–3 | 24–38 | 0.94 |

---

## 8. Entity extraction

No NER model ships with this platform, and that is a deliberate trade. A
general-purpose model trained on news mislabels Indian corporate prose badly,
and a fine-tuned one is not something a deterministic test suite can pin down.

What is used instead is **cue-phrase extraction** built around the formulaic
language regulated filings are obliged to use. "wholly-owned subsidiary",
"Independent Director", "statutory auditors are" are not stylistic choices;
they are near-mandatory constructions, which makes them reliable anchors.

**The honest limitation, stated plainly: precision is high, recall is bounded by
the cue list.** Every entity carries a confidence and the sentence it came from,
so any of them can be checked by eye.

Sixteen entity kinds are extracted, covering everything the brief lists.

---

## 9. Semantic chunking and the vector store

### Chunking

Three rules, each with a reason:

- **Never split a sentence.** A half-sentence retrieved as evidence is worse
  than no evidence, because it reads as complete.
- **Never cross a section boundary.** A chunk straddling the end of Risk Factors
  and the start of the Auditor's Report cannot cite either honestly.
- **Overlap by a sentence**, so a fact stated across a seam stays retrievable.

Sentence splitting is decimal-aware — the same lesson as Module 6's citation
auditor, where splitting on every full stop tore `33,543.00` in half.

Deduplication matters more in filings than almost anywhere: an annual report
repeats its safe-harbour paragraph verbatim dozens of times. The first
occurrence is kept — it is real content on the page it appears — and repeats
beyond a threshold are dropped.

### Vector store — provider-independent

`VectorStore` is an ABC. `InMemoryVectorStore` does exact brute-force search;
for a corpus of a few hundred thousand chunks that is genuinely the right
answer — exact recall, no index build, no tuning. An ANN index trades recall for
speed the platform does not yet need, and a `pgvector` implementation would slot
in behind the same interface without changing anything above it.

**Mixing embedding spaces raises rather than degrading.** Two spaces in one index
produce similarity scores that are arithmetically valid and completely
meaningless — exactly the class of quiet error this platform refuses.

### Retrieval is hybrid

BM25 supplies exact term matching; the vector supplies tolerance to phrasing.
Each covers the other's failure, and both component scores are surfaced so a
weak answer can be diagnosed rather than merely distrusted.

BM25 is unbounded and cosine is bounded, so the lexical score is normalised
against the query's own ceiling before blending — otherwise a single high-idf
term swamps the semantic signal entirely.

### Embeddings

`HashingEmbeddingProvider` is the default: hashed word, bigram and character
n-grams with signed projection, 384 dimensions, deterministic. Not a compromise
for the demo — it is the correct default for a platform that must index a
300-page report without a network round-trip per chunk, and it lets the tests
pin exact numbers instead of mocking. `OpenAIEmbeddingProvider` is present and
wired; it has never been exercised against the live API here, and that is
stated rather than implied.

The local embedder captures lexical similarity well and semantic paraphrase
poorly. That limitation is real and is *why* retrieval is hybrid.

---

## 10. Knowledge graph

- **Edges carry pages.** An edge is a claim about the world, and a claim needs
  evidence. Every edge records the pages on which the relationship was
  observed, so a graph answer cites like any other answer.
- **Identity is `kind:normalised_name`.** "Acme Ltd." and "ACME Limited" unify;
  a person and a company sharing a name stay two nodes.
- **Nothing is inferred transitively.** If A is a subsidiary of B and B of C, the
  graph does not assert A is a subsidiary of C. It may well be, but the document
  did not say so, and a graph that invents edges fabricates evidence.

**Verified on the three-document corpus:** 36 nodes, 35 edges across 11 relation
types — 3 subsidiaries, 3 directors, 2 competitors, 2 suppliers, 1 customer,
1 auditor, 5 countries, 2 segments, 5 risks, 5 guidance statements, 6 capex/debt.

---

## 11. Citation framework

`DocumentCitation` carries exactly the four fields the brief requires —
**document, page, section, paragraph** — and can only be constructed from a real
`SearchHit`, so a citation cannot be assembled for a passage never retrieved.

### Answers are extractive, not generative

The composer assembles answers out of sentences that exist **verbatim** in
retrieved chunks. It never writes a sentence of its own beyond the framing.
This is the same principle Module 6 was built on — the platform produces the
evidence, the LLM explains it — and an extractive answer *cannot* hallucinate a
number, because every character came from a page.

### `verify_answer_citations()`

The document-side counterpart of Module 6's citation auditor: it checks that
every page an answer names was actually retrieved. An answer citing p.42 when
nothing from p.42 was retrieved is fabricated evidence, which is worse than no
answer. Tested by tampering with a verified answer and asserting the audit
catches it.

### The platform declines to answer

Confidence weights term coverage by **informativeness**. Matching "dividend" is
evidence; matching "policy" is not. If none of a query's informative terms
appears in the top hits, confidence collapses regardless of the other signals
and the answer is withheld with an explicit reason.

| Query | Confidence | Outcome |
|---|---|---|
| "What is the EBITDA margin guidance?" | 0.79 | answered, 17% cited to p.1 |
| "Who are the competitors?" | 0.76 | answered, HUL / Nestlé cited |
| "What is the credit rating?" | 0.78 | answered, CRISIL AA+ cited |
| "What are the principal risks?" | 0.85 | answered from Risk Factors |
| **"What is the dividend policy?"** | **0.06** | **declined — nothing supports it** |

---

## 12. Quality: dedup, versioning, incremental re-indexing, queue

**Duplicate detection.** Identical bytes are one document. The content hash is
unique per company, so a second upload returns the first rather than
re-indexing it.

**Version detection.** The same filename with different bytes is a new version.
The predecessor is marked superseded and leaves the search index, but is
**never deleted** — a citation issued last quarter must still resolve to the
text it quoted.

**Incremental re-indexing.** `reindex()` re-embeds stored chunks without
re-parsing. Changing the embedding model costs an embedding pass, not an OCR
pass — over a real corpus, the difference between minutes and hours.

**Queue.** A table, not Redis, so the platform keeps its zero-infrastructure
promise. Jobs are claimed with a conditional `UPDATE ... WHERE status='queued'`,
which is atomic on both SQLite and Postgres — two workers racing cannot both
win. Selecting then updating would let them.

---

## 13. API

| Method | Path | Purpose |
|---|---|---|
| POST | `/documents/upload` | Upload and ingest (multipart) |
| GET | `/documents` | List, filterable by type / status / superseded |
| GET | `/documents/search` | Answer · passages · pages · confidence · citations |
| GET | `/documents/capabilities` | Engine self-description (registry-derived) |
| GET | `/documents/statistics` | Corpus counters, OCR and embedding status |
| GET | `/documents/coverage` | Extraction coverage vs the 73 fields |
| GET | `/documents/chunks` | Indexed passages with provenance |
| GET | `/documents/tables` | Recovered tables, units and merges preserved |
| GET | `/documents/entities` | Extracted entities, filterable by kind |
| GET | `/documents/facts` | The structured extraction store |
| GET | `/documents/knowledge` | The knowledge graph |
| GET | `/documents/jobs` | Ingestion queue with per-stage timings |
| POST | `/documents/reindex` | Re-embed without re-parsing |
| GET | `/documents/{id}` | One document with sections and page provenance |
| GET | `/documents/{id}/pages/{n}` | Page text and how it was obtained |
| DELETE | `/documents/{id}` | Remove a document |

Literal paths are declared before `/{document_id}` — FastAPI matches in
declaration order, and without that `/documents/search` routes to the detail
handler and fails on integer coercion.

---

## 14. Performance benchmarks

Measured on this sandbox. Synthetic annual reports at four scales, single
process, cold cache.

### End-to-end ingestion

| Document | Size | Pages | Chars | Chunks | Fields | Total | Throughput |
|---|---|---|---|---|---|---|---|
| small | 0.02 MB | 3 | 5,279 | 25 | 62 | **150 ms** | 20 pp/s |
| medium | 0.14 MB | 18 | 31,674 | 60 | 62 | **346 ms** | 52 pp/s |
| large | 0.38 MB | 45 | 98,609 | 86 | 62 | **747 ms** | 60 pp/s |
| very large | 0.89 MB | 103 | 242,041 | 98 | 62 | **1,669 ms** | 62 pp/s |

Scaling is linear in pages. Throughput *rises* with size because fixed costs
amortise.

### Stage breakdown (103-page document)

| Stage | ms | Share |
|---|---|---|
| parse (incl. layout + tables) | 455 | 27% |
| financials | 780 | 47% |
| entities | 265 | 16% |
| chunking | 106 | 6% |
| embedding | 42 | 3% |
| sections | 20 | 1% |

### Retrieval and embedding

| Operation | Result |
|---|---|
| Search over 98 chunks | **p50 6.8 ms** · p95 7.3 ms · max 10.9 ms |
| Embedding throughput | **2,677 chunks/s** |
| OCR, single rasterised page | 1,443 ms |

### The 27× table-extraction fix

The first implementation ran pdfplumber on **45 of 45 pages** of a 45-page
report, because the pre-filter treated financial vocabulary as sufficient
evidence of a table. An MD&A page is full of "₹", "crore" and "%" and contains
no table at all.

| | Before | After |
|---|---|---|
| pdfplumber candidate pages (of 45) | 45 | **6** |
| Parse time, 45 pages | 7,245 ms | **266 ms** |
| End-to-end, 103 pages | 18,983 ms | **1,669 ms** |
| Tables recovered | 6 | **6** (all, confidence 1.0) |

The fix separates concerns properly: the pre-filter is a **latency**
optimisation (permissive is cheap, strict loses data), and `_plausible_grid()`
is the **correctness** gate, judging the recovered grid rather than guessing
from text. `TestPerformance` pins both selectivity and recall.

---

## 15. Integration with Module 6

Module 6 shipped with two acknowledged holes. Both are now closed.

**`EvidenceKind.DOCUMENT` was permanently empty.** The AI context for BHARATCP
harvested 60 citations, none of them from a document.

| | Module 6 | Module 7 |
|---|---|---|
| Total AI citations | 60 | **100** |
| `document` | **0** | **40** |
| statement / ratio / forecast / valuation / scoring / market | 60 | 60 (unchanged) |

Each document citation names its source page, e.g.
`doc_revenue_fy25 · 33,543.00 ₹ cr · BHARATCP_AnnualReport_FY25.pdf p.3`.

**`document_search` was a placeholder** that returned the same pre-built context
regardless of the question. It now performs genuine retrieval, and a test
asserts that different queries return different passages — a tool whose answer
does not depend on its argument is not a search.

Module 6's 153 tests pass unchanged. The integration is additive.

---

## 16. Defects found and fixed

Eleven, found by the test harness rather than by inspection. The two marked ⚠
are the dangerous kind: silent, plausible, and invisible in the output.

| # | Defect | Root cause | Fix |
|---|---|---|---|
| 1 ⚠ | `"Particulars"` inferred as a currency | Unanchored `rs` matched inside ordinary words — also `Others`, `Reserves` | Word-boundary-anchored `_CCY` token |
| 2 | Three prose pages reported as tables | Pre-filter treated vocabulary as sufficient; text-strategy shreds prose into a 63×6 grid | Geometry-based pre-filter + `_plausible_grid()` gate |
| 3 | 13-row income statement collapsed to 1 row | pdfplumber returns a lattice region as one newline-packed cell | `unpack_multiline_rows()`, refusing mismatched depths |
| 4 | Entity names bled backwards across sentences | `re.I` makes `[A-Z]` match lowercase, destroying the proper-noun anchor | `(?-i:...)` scoping + sentence-boundary trim |
| 5 ⚠ | `FY 2024-25` resolved to **FY24** | First pattern captured `24` from `2024` and never saw `-25`; every figure filed against the wrong year | Ranges tried before bare years, resolving to the closing half |
| 6 ⚠ | Headcount inherited `₹ crore` from its table | Unit precedence let the table override the field spec | `_UNIT_IMMUNE` — non-monetary fields outrank the table |
| 7 | A rule sat silently dead | Written for `r_and_d_spend`; field generated as `randd_spend` | Fixed, plus a test asserting every rule binds to a real field |
| 8 | Governance text cited as ESG | Sections were page-granular; four sections share page 2 | Block ordinals + `section_for_order()` |
| 9 ⚠ | "Dividend policy" answered at 0.64 confidence | Coverage counted "policy" as evidence | Informativeness-weighted coverage + a hard gate |
| 10 | "Who are the competitors?" returned nothing | Query says "competitors", filings say "compete" | Iterated-to-fixed-point stemming, plus an irregular table |
| 11 | Transcript speech extracted as directors | `Role: Name` is right in a governance report; in a transcript the text after the colon is *speech* — yielded "Palm" and "Thank" as directors | Name-precedes-role pattern only, plus a common-word guard |

### Three cases where my test was wrong, not the product

Reported as found:

1. **Coverage equality test** compared `0.6712` against `0.6712328…` — it was
   asserting against the raw quotient while the product rounds for display. The
   product was right.
2. **Duplicate-detection test** re-rendered the fixture PDF between uploads.
   PyMuPDF stamps a creation timestamp, so the bytes genuinely differed and the
   product correctly refused to call it a duplicate.
3. **Fixture renderer** estimated text-box heights from character count; when
   the estimate ran short, PyMuPDF drew the next block into the leftover space
   and a 16pt heading was swallowed by the paragraph above it. Two sections
   vanished, and section detection was suspected before the fixture was.

### One bug I introduced and fixed

`DocumentDetail.metadata` with `from_attributes=True` picked up SQLAlchemy's own
`Base.metadata` object and failed validation on every detail request. Renamed to
`doc_metadata` with `serialization_alias="metadata"`.

### One UI defect

The search-answer card kept hardcoded `bg-white` / `dark:` Tailwind classes.
This theme switches via CSS variables, not a `dark` class, so the dark variants
never fired and the answer rendered as light grey on white — invisible. Swept to
theme tokens; the knowledge-graph labels were also colliding at the poles of the
radial layout and were re-laid out into two vertical columns.

---

## 17. Verification summary

| Check | Result |
|---|---|
| Full test suite | **1,129 passed** (861 pre-existing + 268 new/updated) |
| Module 6 regression | 153 tests unchanged, all passing |
| `tsc --noEmit` | clean |
| `next build` | clean, 12 routes |
| OpenAPI | 59 paths · 15 document · 130 schemas |
| Database | 21 tables |
| Extraction coverage, 3-doc corpus | **57 of 73 fields (78.1%)**, mean confidence 0.73 |
| Financial values vs reference model | exact — 33,543 / 5,490.7 / 3,450.9 / 15,606.9 |
| Knowledge graph | 36 nodes · 35 edges · 11 relation types |
| AI document evidence | 0 → **40 citations** |

### Extraction coverage by category

| Category | Found | Category | Found |
|---|---|---|---|
| FINANCIAL | 9/10 | ESG | 5/5 |
| GUIDANCE | 4/4 | GOVERNANCE | 5/6 |
| CAPEX | 4/4 | RISKS | 5/6 |
| DEBT | 5/5 | OPPORTUNITIES | 4/4 |
| ORDER BOOK | 2/4 | METRICS | 3/4 |
| CAPACITY | 3/4 | BUSINESS | 3/5 |
| MD&A | 3/3 | CUSTOMERS | 1/3 |
| SUBSIDIARIES | 1/3 | MOAT | 0/3 |

ESG went 1/5 → 5/5 and GOVERNANCE 1/6 → 5/6 once prose rules were added for
fields that had table rules only. ESG and governance figures are almost always
written as sentences, never tabulated, so they had been invisible.

---

## 18. Known limitations

Stated plainly rather than discovered later.

1. **The local embedder is lexical, not semantic.** Hashed n-grams capture
   spelling and inflection well and paraphrase poorly. This is why retrieval is
   hybrid. A real embedding model is one config change away and has never been
   exercised here — there is no API key in this environment.

2. **Entity recall is bounded by the cue list.** Cue-phrase extraction has high
   precision and partial recall. A subsidiary disclosed in a phrasing no rule
   anticipates is missed silently. Every entity carries its evidence so what
   *is* found can be checked; what is missed cannot be.

3. **Version identity is by filename.** Two genuinely different documents saved
   under one name will chain as versions. The content hash means no information
   is lost, but the lineage would read wrongly.

4. **The queue is synchronous by default.** `upload(process=True)` runs the
   pipeline in-request. The job table, atomic claim and `claim_next_job()`
   support a background worker, but no worker process is deployed here.

5. **Graph identity can over-merge.** Two different companies with the same
   short name collide into one node.

6. **Prose rules are regex, not comprehension.** They find the phrasings they
   anticipate. Confidence is scored lower for prose than for tables to reflect
   this, but a confidently-worded regex match is still a regex match.

7. **MOAT category remains at 0/3** on this corpus, and CUSTOMERS/SUBSIDIARIES
   are partial. These need documents that discuss them explicitly.

8. **No LLM has been exercised live** — carried forward from Module 6, unchanged.

9. **Scanned-document tables are not recovered.** OCR restores text but ruling
   lines are gone, so table pre-filtering skips OCR'd pages rather than
   producing unreliable grids.

---

## 19. What this unblocks

- **Module 5 scoring** — `QualitativeInputs` (board independence, promoter
  pledge, ESG, related-party intensity, audit qualifications) are now extracted
  and can lift scoring confidence off its 75% floor.
- **Module 6 AI** — document evidence and real retrieval, as verified above.
- **Module 4 data quality** — extracted facts can be written with a source in
  `TRUSTED_SOURCES` so the valuation gate can grade them investment-grade,
  rather than the current `reference_model` labelling.

Wiring those three consumers is deliberately *not* done in this module: each
changes the numbers another module already produces, and that deserves its own
before-and-after verification rather than being folded in here.
