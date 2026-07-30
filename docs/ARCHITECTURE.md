# Institutional Equity Research Platform — SaaS Architecture

**Spec source:** `Institutional_Equity_Research_Platform_v7.xlsx` (54 sheets, 11,647 formulas,
259 named ranges), `QA_Report_v7.md`, `Upgrade_Summary_v7.md`.

The workbook is the **business specification**. It is not converted, embedded or re-rendered.
Its calculations, precedence rules and thresholds are re-implemented as a typed, tested Python
domain layer. The website is the product.

---

## 1. The single most important architectural decision

The workbook's central architecture — discovered and fixed in v7 — is the **Universal Company
Engine**: one company selection drives every downstream number through a single resolution step.

```
Selection → ActiveOffset (ONE lookup) → 540 Data Map cells → statements → everything
```

This maps onto the web app exactly:

```
company_id → CompanyContext (ONE query) → CanonicalFinancials → engines → API response
```

The workbook eliminated repeated lookups by resolving the selection **once** into a scalar
offset. We do the same: one indexed query materialises a `CanonicalFinancials` object, and every
engine consumes that object. No engine re-queries the database. This is the direct translation
of the workbook's *"avoid duplicated calculations"* rule and it is enforced by test.

### Workbook concept → software concept

| Workbook | Application |
|---|---|
| 12 company slots in `StoreVals` | Unbounded rows in `financial_facts`, indexed on `(company_id, fiscal_year, line_item)` |
| `ActiveOffset` resolver | `CompanyContext` loaded once per request |
| `0C Data Map` 4-tier precedence | `resolve_line_item()` precedence chain |
| 54 canonical line items | `LineItem` enum, generated from the workbook |
| Statement gates on 06/07/08 | `FinancialsService.build()` data-availability guard |
| `ProviderTable` registry | `providers` DB table + `ProviderRegistry` |
| Named ranges (`WACC_Value`, `DCF_Upside`) | Typed Pydantic fields on engine outputs |
| Manual override columns U..AD | `overrides` table, highest precedence |
| Recalc on selection change | React Query invalidation on `company_id` |

**The 12-slot limit disappears.** It existed because Excel needed fixed geometry. Postgres does
not. The QA report lists it as a caveat; the web app removes it.

---

## 2. Layered architecture

Dependencies point strictly inward. The domain layer imports nothing from FastAPI, SQLAlchemy or
HTTP. That is what makes the financial logic testable without a database and portable.

```
┌─────────────────────────────────────────────────────────────────┐
│ frontend/           Next.js 15 · React · TS · Tailwind          │
│                     AG-Grid-style tables · Highcharts · RQ      │
└──────────────────────────────┬──────────────────────────────────┘
                               │ typed OpenAPI client
┌──────────────────────────────▼──────────────────────────────────┐
│ api/       FastAPI routers · Clerk auth · RBAC · rate limits    │
├─────────────────────────────────────────────────────────────────┤
│ services/  Orchestration, caching, transactions                 │
├─────────────────────────────────────────────────────────────────┤
│ domain/    ★ PURE PYTHON — the workbook's brain ★               │
│            financials/ · valuation/ · scoring/ · research/      │
│            No I/O. No framework. Deterministic. 100% unit-test. │
├─────────────────────────────────────────────────────────────────┤
│ db/ models/  SQLAlchemy 2.0 · Postgres · Alembic                │
├─────────────────────────────────────────────────────────────────┤
│ ai/ ocr/ reports/ workers/   Providers · PyMuPDF · Celery       │
└─────────────────────────────────────────────────────────────────┘
```

**Why the domain layer is pure.** The workbook's value is 11,647 formulas of financial logic.
If that logic is entangled with HTTP handlers or ORM sessions it cannot be verified against the
workbook. Keeping it pure means every DCF, ratio and pillar score is a function of plain data —
so we can assert numerical equivalence with the spreadsheet in CI.

---

## 3. Canonical data model

Mirrors `0C Data Map`: 54 line items × N years × unbounded companies.

```
companies ──┬── financial_facts   (company_id, fiscal_year, line_item, value, source)
            ├── overrides         (analyst edits — highest precedence)
            ├── assumptions       (WACC, growth, forecast drivers)
            ├── scores            (11 pillars + 10 categories)
            ├── valuations        (DCF/FCFF/FCFE/relative snapshots)
            ├── documents         (R2 keys, OCR status)
            ├── ai_analyses       (21 sections)
            └── watchlist/portfolio/alerts
```

Key indexes: `(company_id, fiscal_year, line_item)` unique composite; trigram GIN on
`companies.name` and `.ticker` for sub-50 ms search; partial index on `alerts WHERE active`.

### Precedence — the `0C Data Map` rule, in code

```python
def resolve(company, year, item):
    # 1. analyst override always wins        (workbook cols U..AD)
    # 2. company store / imported facts       (workbook StoreVals)
    # 3. derived from an alias mapping        (workbook $M alias match)
    # 4. None — never a silent fabricated 0   (workbook sample default)
```

Tier 4 is where we deliberately **diverge**. The workbook falls back to sample constants so a
demo never looks blank. A commercial product must never show invented figures as real. We return
`None`, and the UI renders an explicit "no data" state. This is a data-integrity decision, and
it is the one place the app knowingly departs from the spec.

---

## 4. Calculation pipeline

Six stages, matching the workbook's required flow exactly:

```
1. Company Selection    →  CompanyContext (single resolution)
2. Financial Data       →  CanonicalFinancials (54 items × N years)
3. Calculation Engine   →  Statements → Ratios → Forecast → Valuation
4. AI Layer             →  Provider registry → 21 sections
5. Dashboard            →  Aggregated read models
6. Reports              →  PDF / XLSX / DOCX
```

Each stage consumes only the stage above. Enforced by an import-linter contract in CI, which is
the software equivalent of the workbook's "no cell on 06/07/08 reads `RawPL_Vals` directly" test.

---

## 5. AI provider abstraction

The workbook's hardest-won lesson. v7 shipped a worksheet registry while the VBA still had
`Select Case` over four literal provider names — the abstraction was cosmetic exactly where it
mattered. The QA report logs this as defect #3.

We will not repeat it. Providers are **database rows**, and the transport branches on
**payload shape**, never on provider name.

```python
class LLMProvider(Protocol):
    async def complete(self, req: CompletionRequest) -> CompletionResponse: ...

# registry columns mirror ProviderTable exactly:
# name | endpoint | auth_header | payload_shape | response_path | default_model
```

Three shape adapters (`openai`, `anthropic`, `gemini`) cover OpenRouter, OpenAI, Claude and
Gemini. Adding a fifth compatible provider is an INSERT, not a deploy. A CI test asserts no
provider name string literal appears in transport code.

---

## 6. Performance

Workbook evidence: flat scaling — 10× the universe cost +8.9% time, because one `MATCH()`
served 540 cells. Same principle applied here.

| Concern | Approach |
|---|---|
| Repeated lookups | `CompanyContext` loaded once per request, passed to all engines |
| Heavy recompute | Redis cache keyed `company:{id}:v{data_version}`; bumping the version invalidates atomically |
| Slow work | Celery: OCR, AI generation, report rendering, bulk imports |
| Payloads | Server components + streaming; grids virtualised |
| Queries | Composite indexes; `selectinload` to kill N+1; one round trip per statement |
| Client | React Query cache keyed on `company_id`, mirroring workbook recalc-on-selection |

Budgets: search < 50 ms p95 · statements < 200 ms p95 · full valuation < 400 ms p95.

---

## 7. Module plan

| # | Module | Depends on |
|---|---|---|
| **1** | Auth · Landing · Dashboard · Search · Company Profile · Nav · Theme | — |
| 2 | Historical statements, ratios, WC, debt, capex, shareholding | 1 |
| 3 | Forecast engine | 2 |
| 4 | Valuation — DCF/FCFF/FCFE/WACC/sensitivity/scenarios/relative | 3 |
| 5 | Institutional scoring — 11 pillars + 10 categories | 2, 4 |
| 6 | AI layer — registry, prompts, chat, 21 sections | 5 |
| 7 | Documents — upload, OCR, extraction, auto-summary | 6 |
| 8 | Portfolio, watchlist, alerts, analytics | 5 |
| 9 | Report generator — PDF, Excel, Word | all |
| 10 | Admin — users, subscriptions, plans, usage, logs | 1 |

Each module compiles, runs and is verified before the next begins.

---

## 8. Design system

Institutional terminal aesthetic — Bloomberg/FactSet density, not consumer SaaS whitespace.

- **Navy** `#0B1F3A` primary · `#12305C` elevated · **Accent** `#1F6FEB`
- **Semantic:** gain `#0B7A3B` · loss `#B3261E` · warn `#B45309`
- Dark-first with a light mode; grey scale for data density
- Tabular figures everywhere; monospace for all numerics so columns align
- 4px spacing grid; 32px dense table rows
- Keyboard first: `⌘K` search, `g+d` dashboard, `g+c` company, `?` shortcuts

Non-negotiable data-display rules, inherited from the workbook's discipline:
1. Never render a fabricated number. Missing data shows as `—`.
2. Always show units (`₹ cr`, `%`, `x`) and the fiscal year.
3. Negatives in red parentheses, Indian convention.
4. Every derived figure can show its inputs — the workbook's audit trail, preserved.

---

## 9. Verification strategy

The workbook was held to 92/92 static checks, 41/41 production checks, 0 critical defects and
0 value differences against v6.1. The application inherits that standard:

1. **Golden-value tests** — engine outputs asserted against figures evaluated from the workbook
   (e.g. DCF intrinsic 338.34 / 213.13 for the two engine-test fixtures).
2. **Invariant tests** — balance sheet ties in every year; no duplicated calculation; acyclic
   dependencies.
3. **Contract tests** — import-linter enforces layer boundaries.
4. **Provider tests** — no hard-coded provider names in transport.
5. **Type safety** — mypy strict on domain; TS strict, no `any`.

---

## 10. Known constraints

- No Microsoft Excel in this environment; workbook-equivalence tests use extracted golden values.
- Clerk, R2, Railway and LLM providers need real credentials; Module 1 ships with a dev-mode
  auth shim so the app runs end-to-end without paid accounts, behind the same interface.
- The 12-slot limit is intentionally removed.
- Sample-constant fallback is intentionally removed (§3).
