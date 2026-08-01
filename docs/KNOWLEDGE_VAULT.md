# Company Knowledge Vault

Permanent, versioned institutional memory. Deployed and live at commit
`e30809d`.

This delivers the foundation you approved — sections 1, 3 (storage half), 6,
10, 11, 12 and 13 of the brief. Sections 7 (knowledge graph), 9 (research
engine) and parts of 4/5 are addressed below under *What is not built*.

## What it does

```
document → extraction → vault (versioned, cited) → AI reads this FIRST
                                                  ↓ only if insufficient
                                                    RAG → raw PDF
```

| Built | Where |
|---|---|
| 20 vault sections, exactly as specified | `domain/knowledge/vault.py` |
| Versioned, never-overwritten entries | `models/knowledge.py`, `knowledge_entries` |
| 9 permanent summary kinds | `document_summaries` |
| Fact → vault promotion | `services/knowledge/ingest.py` |
| Read-first memory in the AI prompt | `context_builder._add_knowledge` |
| 8 API endpoints | `api/v1/knowledge.py` |
| Migration | `a3d95f1c7e42` |

## Live in production

```
total entries : 364     current : 208     superseded : 156
citable       : 100%    mean confidence : 0.649
companies     : 25      documents : 82    facts rejected : 0
```

Sections populated: risks (61), opportunities (50), financial statements (24),
company profile (12), capital allocation (11), business model (10), management
(9), ratios (7), revenue segments (5), and others.

### Nothing is overwritten — demonstrated

A real key in production carries **16 versions**, FY2008 → FY2023, each
retained with its own page citation:

```
v16 superseded FY2023 doc29 p.27 :: 11508.0
v15 superseded FY2022 doc29 p.27 ::  7028.0
...
v1  superseded FY2008 doc29 p.27 ::    21.0
```

That is the temporal memory requirement satisfied directly: sixteen years of
one metric answerable from the vault without re-reading a single PDF.

### The AI reads the vault first — demonstrated

A RELIANCE prompt now carries **12 `knowledge` citations ahead of 13 raw
document chunks**:

```
evidence by kind: statement 16, scoring 17, document 13, knowledge 12,
                  valuation 12, ratio 7, forecast 6, market 2

[Vault/opportunities]   EBITDA margin guidance    p.5  conf=0.72
[Vault/company_profile] Business description      p.5  conf=0.68
[Vault/risks]           Related-party transactions p.1 conf=0.62
```

`EvidenceKind.KNOWLEDGE` is deliberately distinct from `DOCUMENT`: a
distilled, versioned assertion is different evidence from the paragraph it
came from, and a reader should be able to tell.

## Design decisions worth stating

**Supersession orders by fiscal period, then source authority, then
confidence — never by ingestion time.** A backfill loading FY2024 after FY2026
must not rewind the vault, and `ORDER BY created_at` would let it, invisibly,
because every individual entry is correct. An audited annual report outranks
an investor deck of the same year.

**A stale assertion is still recorded**, as superseded from the outset. "We
saw this claim and rejected it as stale" is knowledge; discarding it would
make the vault's reasoning unauditable.

**An empty assertion is refused.** An entry with no value would still
supersede a good one and silently blank it.

## Three defects found

**VAULT-001** — `quarter` sized `String(4)` assuming "Q1". Real extracted
periods are `"Q1FY26"`, `"H1FY25"`. Postgres refused the insert, which is the
right failure: truncating a period label would have been worse than crashing.

**SUMMARY-001** — `max_tokens` is a *reservation* against the credit balance,
not a spend. OpenRouter returned **402 "you requested up to 1500 tokens, but
can only afford 329"** for every long summary while the 100-word one
succeeded, so eight of nine summaries silently became offline template prose.
Budgets are now proportionate to target length.

**CONFTEST-001** — the test schema is built by `create_all` from whatever
models happen to have been imported, so a new model module nothing else
imports is silently missing its table. Now imported explicitly.

## I broke production, and how

The deploy of `e30809d` **failed** with `DuplicateTable: relation
"knowledge_entries" already exists`, and the service returned 502 for roughly
six minutes.

My error: I created the tables with `Base.metadata.create_all()` while
building the vault against the production database, so when the migration ran
it collided with tables it was supposed to create. Resolved by stamping
`alembic_version` to `a3d95f1c7e42` — the `create_all` schema is identical to
what the migration builds — rather than dropping 364 real vault entries.

The lesson is narrow and worth recording: **never use `create_all` against a
database that Alembic manages.** It works, and then the next migration fails.

## What is NOT built

Stated plainly, because the brief is much larger than one session:

- **Summaries are 1 generated, 18 fallback.** The OpenRouter key's credit
  balance is exhausted — even a 240-token request now returns 402. The
  pipeline is correct and proven end to end; it needs credit to produce real
  analysis. Until then §6 exists structurally but not substantively.
- **Knowledge graph (§7)** — 723 relations are already persisted by the
  existing pipeline, but the macro/policy/commodity/forex layers the brief
  describes are not modelled.
- **AI research engine (§9)** — "Should I buy TCS?" still routes through the
  existing section orchestrator, not a reasoning engine over vault history.
- **Financial extraction (§5)** — unchanged from the last phase. Statement
  extraction from PDFs into `FinancialFact` remains unimplemented, and 368 of
  500 companies still have no financials.
- **Incremental update (§10)** — new filings create new versions correctly,
  but nothing yet detects *which* knowledge changed and skips the rest; the
  ingestor replays a company's facts in full.
- **Vault coverage is 25 of 507 companies**, because only 27 have documents.

## Endpoints

```
GET  /company/{ticker}/knowledge                        the vault
GET  /company/{ticker}/knowledge/{section}              one section
GET  /company/{ticker}/knowledge/{section}/{key}/history  every version
GET  /company/{ticker}/summaries                        stored memory
GET  /company/{ticker}/summaries/{kind}/timeline        temporal view
POST /company/{ticker}/knowledge/build                  promote facts
POST /knowledge/build-all                               promote everything
POST /knowledge/summarise                               generate summaries
GET  /knowledge/stats                                   coverage
```

## Verification

- 30 vault tests; **2,315 in the suite, 0 failures**
- Perimeter checks **33/33**
- Vault built from 364 real production facts, 0 rejected, 0 unmapped
- 16-version history confirmed in production
- 12 knowledge citations confirmed in a live prompt
