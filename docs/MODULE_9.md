# Module 9 — Research Report Generator

**Status:** complete, awaiting review
**Tests:** 1,410 passing (133 new in this module)
**API:** 89 paths total · 9 report paths · 181 schemas
**Database:** 34 tables (3 new)

---

## 1. The organising decision

The brief's hardest constraint is **"never use static templates"**. The way to
honour that structurally, rather than by promising it, is to make a report a
**data structure instead of a document**: a tree of typed blocks that each
renderer walks.

A PDF, a Word file, a spreadsheet, an HTML page and a Markdown file are then
five traversals of one tree — not five templates that must be kept in
agreement. Adding a section means emitting blocks, and every output format
gains it at once.

Three tests enforce this rather than trusting it:

- every renderer must declare it handles the **complete** block vocabulary;
- no renderer may contain a section title (a renderer that names "Executive
  Summary" is a template);
- report composition must be data — `REPORT_SECTIONS` is a dict keyed by report
  type, so a seventh type is a row, not a builder.

---

## 2. Architecture

```
        ┌────────────────────────────────────────────┐
  HTTP  │  api/v1/reports.py            serialises   │
        └───────────────────┬────────────────────────┘
        ┌───────────────────▼────────────────────────┐
        │  ReportService     gather · cache · version│  only DB-aware layer
        └───────────────────┬────────────────────────┘
        ┌───────────────────▼────────────────────────┐
        │  ReportBuilder     one _build_* per section│  emits blocks, no layout
        └───────────────────┬────────────────────────┘
                            ▼
                    ReportDocument
              (cover + ordered Sections of Blocks)
                            │
        ┌───────────┬───────┼────────┬───────────┬──────────┐
        ▼           ▼       ▼        ▼           ▼          ▼
      PDF        DOCX     XLSX     HTML      Markdown   CitationAudit
   ReportLab  python-docx openpyxl  inline      text     coverage check
        └───────────┴───────┴────────┘
                    ChartEngine (matplotlib, content-addressed cache)
```

Layer boundaries are enforced by test:

| Layer | May import | Must not import |
|---|---|---|
| `app/domain/reports/` | dataclasses, enums | SQLAlchemy, FastAPI, **reportlab, docx, openpyxl, matplotlib** |
| `app/services/reports/builder.py` | domain + engine results | renderers |
| `app/services/reports/renderers/` | domain + the chart engine | `app.models`, `Session` |
| `app/api/v1/reports.py` | the service and schemas | how anything is composed |

The domain layer knowing nothing about ReportLab is what makes the block tree
storable, diffable and renderable by a format that does not exist yet.

---

## 3. Folder structure

```
backend/app/
├── domain/reports/
│   ├── blocks.py       13 block types, 19 sections, 6 report types, composition
│   └── citations.py    EvidenceRegistry · audit_report · annotate
├── services/reports/
│   ├── builder.py      One _build_* per section. Decides sufficiency, emits blocks
│   ├── service.py      Gather · cache · version · persist · re-render
│   ├── serialise.py    Lossless block-tree round trip
│   ├── charts/engine.py  7 chart families, content-addressed cache
│   └── renderers/
│       ├── base.py     Contract + registry + shared formatters
│       ├── pdf.py      ReportLab: TOC, page numbers, bookmarks, Unicode fonts
│       ├── docx.py     Word styles, TOC field, PAGE field footer
│       ├── xlsx.py     Sheet per section, native charts, evidence sheet
│       └── web.py      Self-contained HTML (+ @media print) and Markdown
├── models/report.py    Report · ReportArtifact · ReportJob
├── schemas/report.py   Typed contracts
└── api/v1/reports.py   9 endpoints

backend/tests/
├── test_report_engine.py   92 tests — blocks, citations, charts, renderers
└── test_report_api.py      41 tests — HTTP integration
```

---

## 4. The spec came from the workbook

`31 IC Report` and `33 Print Report` are the report specification, and they were
read as one:

- **10 numbered IC sections** — recommendation summary, thesis, key arguments,
  catalysts, risks, valuation summary, key financials, what would change our
  mind, monitorables, analyst certification
- **23 AI narrative blocks** listed at rows 116–138
- **Cross-method valuation weights** — DCF FCFF 35%, FCFE 15%, relative 30%,
  scenario 20%
- **12 key financial rows** — the summary table this module reproduces
- **44 cover fields** from `01 Cover`

`docs/module9_spec.json` holds the extraction.

---

## 5. Template system

### Blocks

Thirteen types, and every renderer handles all thirteen: heading, paragraph,
bullets, key-value, table, metric grid, chart, callout, quote, divider, page
break, **insufficient**, citation list.

`Insufficient` is a first-class block, not an absence. That is the mechanism
behind the brief's dynamic-content rule.

### Report types are composition, not code

| Type | Sections | AI narratives |
|---|---|---|
| Quick | 5 | 1 |
| IC Memo | 9 | 5 |
| Quarterly Update | 7 | 3 |
| Institutional | 16 | 8 |
| Initiation | 18 | 10 |
| Deep Research | 19 | 13 |

A test asserts no two types select the same section set — two types that
produce the same thing are one type.

### Sections render in canonical order

`SECTION_ORDER` fixes the sequence regardless of the order builders ran, so two
reports of one type are comparable page by page.

---

## 6. Dynamic content — "Insufficient evidence."

The brief requires that sections without evidence display exactly that, and
that sections are never fabricated.

**Sufficiency is decided before content is written**, not discovered halfway
through. A builder that finds its inputs missing calls
`Section.mark_insufficient(reason)` and returns; it never emits a half-section
with blank cells.

The one place this module goes further than the brief: the statement carries
**why**.

> *Insufficient evidence. No peer companies are under coverage in this sector.*

"Insufficient evidence." alone tells a reader nothing about whether to go and
find the data or accept the gap.

Observed on the reference company: the institutional report populated all 15
sections; the IC memo and deep-research reports each marked one section
insufficient (portfolio fit, with no portfolio supplied) and said so.

---

## 7. Citation framework

The brief requires every factual statement to reference its evidence, naming
five engines. Eight `EvidenceSource` values are modelled — the five named plus
forecast, portfolio and market data.

### Three mechanisms

1. **`EvidenceRegistry`** collects evidence as the report is built, so the
   appendix is assembled from what was *used*. It refuses a key reused with a
   different value: two statements citing one reference and meaning different
   things is a defect, not a convenience.

2. **`audit_report()`** finds numeric claims in the prose that carry no
   citation. This is the check that matters — a report citing its headline and
   nothing else meets the letter of the requirement and none of its intent.

3. **`annotate()`** swaps `[key]` markers for readable references at render
   time. An unresolvable marker is left **visible**: hiding it would make a
   broken reference read as though it were never cited.

### The auditor's judgement calls

| Case | Treatment | Why |
|---|---|---|
| `₹33,543.00` | one sentence | Module 6 shipped a splitter that tore it in two, orphaned the citation and reported 50% coverage on a perfectly cited answer |
| "three pillars" | not a claim | Small integers in prose are counts, not assertions about the company |
| `[revenue_fy25]` | "25" is not a claim | Markers are stripped before hunting numbers |
| Paragraph introducing a cited table | supported | Block-level evidence counts; forcing inline repetition would be noise |
| "Insufficient evidence. Only 2 of 13 inputs…" | not a claim | Explicitly hedged sentences are exempt |

**Result on the reference company: 16 of 16 numeric claims supported, 100%
coverage, zero dangling markers, across all four report types generated.**

---

## 8. PDF engine

ReportLab platypus rather than HTML-to-PDF. The reason is control over the
three things the brief asks for and converters handle badly: **page numbers**
that know the document, a **table of contents** with real page references, and
**PDF bookmarks** in the reader's navigation pane.

The two-pass `multiBuild` is the mechanism: page numbers do not exist until the
document is laid out, so ReportLab lays it out twice and the second pass renders
the contents page with the numbers the first pass recorded.

**Verified on a 15-page institutional report:** 32 bookmarks in correct
hierarchy, dot-leader TOC with real page numbers, running header and footer,
metadata (title, author, subject) populated.

---

## 9. DOCX, Excel, HTML, Markdown

**Word.** Real heading styles, not hand-formatted bold — that is what populates
the navigation pane. The TOC is a Word *field* so Word computes and refreshes
the page numbers; a rendered list would be wrong the moment anyone edited the
file. The footer uses a `PAGE` field for the same reason.

**Excel** inverts the emphasis deliberately. An analyst opening a spreadsheet
wants the numbers: a summary sheet, one sheet per section, and a filterable
evidence sheet. Formatted strings are parsed **back to numbers** — a
spreadsheet of text nobody can sum defeats the export — and charts are **native
Excel charts**, so they update when an input is edited. Cells beginning with
`=`, `+`, `-` or `@` are prefixed to neutralise formula injection.

**HTML** is the in-app preview and a print target: `@media print` with `@page`
margins and page-break control. Charts are inlined as base64 data URIs, so the
file is self-contained with no asset directory to lose.

**Markdown** is the plain-text fallback — useful for diffing two versions of a
report. Charts become data tables, since Markdown has no images without assets.

---

## 10. Chart engine

Matplotlib with the Agg backend. Seven drawing families cover the brief's ten
chart kinds: grouped bars, lines with a percentage axis, horizontal bars, radar,
donut, and a diverging heat-map for sensitivity.

Two decisions worth recording:

- **A chart with no data is not drawn.** `Chart.has_data` gates every call. An
  empty axis reads as "the value was zero" rather than "we had nothing", which
  is the fabrication the brief forbids.
- **Charts are cached by content hash**, keyed on data, theme and size. The same
  chart rendered for the PDF, the DOCX and the HTML preview is rasterised once.
  Measured hit rate across a five-format render: **89%**.

A radar with fewer than three axes is refused rather than drawn as a line.

---

## 11. Quality: versioning, caching, background jobs

**Versioning** is per company and report type. Regenerating creates version *n+1*
and marks its predecessor superseded — retained and retrievable, because a
report sent to a committee must still resolve months later. Deleting a middle
version re-points its successor so the chain is never orphaned.

**Caching is by content, not clock.** The key hashes the company's
`data_version`, current price, report type, theme, requested formats and the AI
flag. A re-imported filing or an edited assumption bumps `data_version` and
invalidates every report built from it, with no explicit busting. A cached
report that lacks a newly-requested format is **not** a hit — returning it would
silently omit the file.

**The block tree is stored**, so a DOCX of a report generated last month as a
PDF returns *that* report rather than a fresh one built from today's numbers. A
test asserts the round trip is lossless by comparing re-rendered Markdown byte
for byte.

**Gathering never raises.** Each engine is called inside a guard and failures
are recorded in `errors` and surfaced by the API. A report whose valuation
engine fell over still contains its financial analysis and says plainly why the
valuation is missing.

---

## 12. API

| Method | Path | Purpose |
|---|---|---|
| POST | `/reports/generate` | Build a report |
| GET | `/reports` | List, filterable by company / type / superseded |
| GET | `/reports/capabilities` | Self-description from the registries |
| GET | `/reports/statistics` | Corpus counters and mean coverage |
| GET | `/reports/jobs` | Generation queue with per-stage timings |
| GET | `/reports/{id}` | One report with its section index |
| GET | `/reports/{id}/versions` | The full version chain |
| GET | `/reports/{id}/download/{fmt}` | The rendered artefact |
| GET | `/reports/{id}/preview` | Inline HTML |
| DELETE | `/reports/{id}` | Remove a report |

Literal paths precede `/{report_id}` — the same FastAPI ordering trap Modules 7
and 8 hit.

---

## 13. Performance benchmarks

Measured on this sandbox, single process.

### Generation by report type (all five formats)

| Type | Sections | Charts | Words | Gather | Build | Render | Total |
|---|---|---|---|---|---|---|---|
| Quick | 4 | 4 | 289 | 46 ms | 0.6 ms | 812 ms | **859 ms** |
| IC Memo | 8 | 1 | 1,054 | 36 ms | 0.6 ms | 602 ms | **640 ms** |
| Quarterly Update | 6 | 4 | 730 | 34 ms | 0.5 ms | 818 ms | **854 ms** |
| Institutional | 15 | 7 | 1,697 | 47 ms | 1.2 ms | 1,686 ms | **1,735 ms** |
| Initiation | 17 | 7 | 2,081 | 57 ms | 1.2 ms | 2,028 ms | **2,088 ms** |
| Deep Research | 18 | 7 | 2,443 | 48 ms | 4.9 ms | 2,016 ms | **2,071 ms** |

Composition is essentially free (0.5–5 ms). Rendering dominates, and within it
the PDF does.

### Per-format render cost (institutional, 15 sections)

| Format | p50 | Size | |
|---|---|---|---|
| Markdown | **0.4 ms** | 22 KB | |
| HTML | **2.0 ms** | 338 KB | charts inlined |
| Excel | **89 ms** | 39 KB | |
| Word | **274 ms** | 243 KB | |
| PDF | **647 ms** | 324 KB | 15 pages |

Chart cache across the five: **56 hits / 7 misses, 89% hit rate.**

### Caching and serialisation

| Operation | Result |
|---|---|
| Generation, cold | 1,346 ms |
| Generation, cached (p50) | **2.06 ms** |
| **Speedup** | **654×** |
| Serialise block tree | 0.7 ms (98 KB JSON) |
| Deserialise | 2.7 ms |
| AI narratives on / off (deep research) | 44 ms vs 19 ms gather; 2,443 vs 264 words |

---

## 14. Defects found and fixed

Six. All were caught by running the thing rather than by reading it.

| # | Defect | Root cause | Fix |
|---|---|---|---|
| 1 | `Invalid color value 'b45309'` — every callout crashed the PDF | `hexval()` returns `0xrrggbb`; slicing the prefix left digits with no leading `#` | A single `_hex()` helper, so the conversion exists in one place |
| 2 | `can't jump from outline level 0 to level 2` | PDF outlines are a strict tree; a section heading followed by a table caption skips a level | Clamp each entry to at most one level deeper than the last |
| 3 | `Index entries not resolved after 10 passes` | `multiBuild` lays out repeatedly; my bookmark counter accumulated across passes, so the TOC never converged | Reset per-pass state in `beforeDocument()` |
| 4 ⚠ | **Every ₹ printed as a black box** | Base-14 Helvetica has no rupee glyph — affected every monetary figure while the layout still looked right | Register DejaVu Sans, with Helvetica retained as a fallback |
| 5 | Metric grid left a row three-quarters empty | Five metrics in four columns reads as missing data | Pick a column count that divides evenly where possible |
| 6 | `include_document=false` shipped the whole block tree anyway | `from_attributes` populates the field straight off the ORM column | Clear it explicitly — it was several hundred KB per call nobody asked for |

Defect 4 is the dangerous kind: the report still typeset correctly, the tables
still aligned, and only the glyph was wrong. A test now asserts `₹` survives
into the extracted PDF text.

### One case where my guess was wrong, and the error-recording caught it

`_ratios()` called `RatioService.margin_ratios()`, which does not exist. Because
gathering records failures rather than swallowing them, the run completed and
`errors` reported `AttributeError: 'RatioService' object has no attribute
'margin_ratios'`. The real entry point is `all_sections()`. This is the second
module in a row where making failures visible has paid for itself.

### One screenshot artefact, reported as such

The first full-page capture showed the header bar overlapping the statistics
card. A viewport-only capture of the same page rendered correctly — Playwright's
`full_page` mode re-renders with the sticky header detached. **A capture
artefact, not a product defect.** The screenshot script now un-sticks the header
before a full-page shot.

---

## 15. Verification summary

| Check | Result |
|---|---|
| Full suite | **1,410 passed** (1,277 pre-existing + 133 new) |
| Modules 1–8 regression | unchanged, all passing |
| `tsc --noEmit` | clean |
| `next build` | clean, 15 routes |
| OpenAPI | 89 paths · 9 reports · 181 schemas |
| Database | 34 tables |
| Citation coverage, all report types | **100%**, zero dangling markers |
| PDF bookmarks | 32, hierarchy never skips a level |
| Rupee glyph in extracted PDF text | present |
| Serialisation round trip | byte-identical re-render |
| Every renderer handles every block kind | asserted |
| No renderer hard-codes a section title | asserted |

### Reference report (BHARATCP, institutional)

15 sections · 7 charts · 10 tables · 37 cited figures · 1,697 words · 15 PDF
pages · 5 formats · 3.5 s · 100% citation coverage.

---

## 16. Known limitations

1. **AI narratives run against the offline provider.** No LLM API key exists in
   this environment. The prose is composed deterministically from the same
   grounded evidence a live model would receive, so the citation and guardrail
   behaviour is real — but the writing quality is not what a live model would
   produce. Carried forward from Module 6, unchanged.

2. **Generation is synchronous.** `ReportJob` exists and records per-stage
   timings, but no worker process is deployed. A deep-research report takes two
   seconds, which is within a request budget; a hundred-company batch would
   need the worker.

3. **Two sections are thin.** Industry Analysis and Peer Comparison currently
   render coverage-universe tables rather than genuine competitive analysis,
   because the platform holds no industry data beyond its own peer set.

4. **Sensitivity charts need a grid the valuation service does not expose by
   default.** The renderer handles the heat-map; `value_company()` does not
   compute a sensitivity grid unless asked, so the chart is usually absent.

5. **Excel native charts skip three kinds.** Radar, heat-map and donut have no
   clean openpyxl equivalent, so those appear as data only. The other formats
   render all ten.

6. **The block tree is stored as JSON in a column.** 98 KB per report is
   comfortable; a corpus of tens of thousands would want it elsewhere.

7. **No print CSS for the DOCX path.** Word's own pagination governs, which
   means a Word export will not paginate identically to the PDF.

8. **Charts are rasterised, not vector.** A PDF chart is a 150 dpi PNG rather
   than embedded vector art. It prints cleanly at A4 and does not scale
   indefinitely.

---

## 17. What this unblocks

- **Module 10 (admin)** has `report_jobs`, artefact byte counts and per-stage
  timings for operational dashboards, plus `citation_coverage` as a quality
  metric worth surfacing to an administrator.
- Any future format — PowerPoint, JSON-LD, a filing submission — is a new
  renderer against the existing tree, with no change to composition.
