# Module 1 — Foundation

**Scope:** Authentication · Landing Page · Dashboard · Company Search · Company Profile ·
Navigation · Theme

**Status: complete, compiling and running.** Awaiting approval before Module 2.

---

## What shipped

### Backend (FastAPI)

| Endpoint | Purpose |
|---|---|
| `GET /health` | Liveness + environment |
| `GET /api/v1/auth/me` | Current user |
| `GET /api/v1/auth/config` | Public auth config for the client |
| `GET /api/v1/companies/search` | Ranked search (exact ticker → prefix → contains) |
| `GET /api/v1/companies` | Paginated list, sector filter, market-cap ordered |
| `GET /api/v1/companies/sectors` | Distinct sectors |
| `GET /api/v1/companies/{id}` | Company detail |
| `GET /api/v1/companies/{id}/profile` | Detail + headline financials + coverage |
| `GET /api/v1/dashboard/overview` | Universe coverage, sector mix, largest names |

### Domain layer (pure Python — the workbook's brain)

- `line_items.py` — the **54 canonical items**, generated from the workbook, not hand-typed
- `canonical.py` — the `0C Data Map` **4-tier precedence chain** and `CanonicalFinancials`
- `statements.py` — `06 Historical IS`, `07 Historical BS`, `08 Historical CF`

Every subtotal carries its originating cell reference in a comment, so the mapping back to
the specification stays auditable.

### Frontend (Next.js 16 · React 19 · Tailwind 4)

- Landing page, dashboard, companies list, company profile
- `AppShell` with sidebar, sticky header, theme toggle, footer provenance
- **⌘K command palette** with arrow-key navigation and live search
- **`g`+`d` / `g`+`c`** navigation shortcuts
- Excel-like data grid: sticky headers, sticky first column, subtotal emphasis, 32px rows
- Dark-first institutional theme (navy/white/grey) with light mode

---

## Verification

| Check | Result |
|---|---|
| Backend tests | **36 passed** |
| `tsc --noEmit` | clean, no `any` |
| `next build` | succeeds — 5 routes |
| Browser console errors | **none** across all 4 pages |
| API p95 (search / profile / dashboard) | 5 ms / 11 ms / 31 ms |
| Profile page SSR | 103 ms |

### Workbook equivalence

The engine is asserted against figures the workbook itself produced
(`QA_Report_v7.md` §9.1, fixture "Titan Company Ltd"):

| Metric | Workbook | Application |
|---|---:|---:|
| Revenue FY0 | 14,528.6 | **14,528.6** |
| EBITDA FY0 | 961.9 | **961.9** |
| PAT FY0 | 125.5 | **125.5** |
| EPS FY0 | 1.0458 | **1.0458** |
| Balance sheet | ties all years | **ties all years** |

Fixture A ("Reliance Industries Ltd") likewise reproduces 23,298.6 / 2,693.7 / 1,064.9 / 8.874.

---

## Deliberate divergences from the workbook

Two, both recorded here rather than made silently.

**1. No sample-constant fallback.** The workbook's precedence tier 4 falls back to stored
sample figures so a demo file never looks blank. A commercial product must never present a
fabricated number as real, so tier 4 resolves to `None` and the UI renders `—`. This is
enforced by test (`test_missing_resolves_to_none_not_a_fabricated_number`).

**2. No 12-slot limit.** The workbook's 12 company slots existed because Excel needed fixed
geometry — the QA report lists it as a caveat. Postgres has no such constraint, so the
Universal Company Engine is genuinely unbounded here.

---

## Architectural rule now enforced in code

The workbook's central optimisation was resolving a selection **once** into `ActiveOffset`,
with 540 cells consuming that one scalar. The application mirrors it exactly:
`CompanyService.load_financials()` performs **one** indexed query and returns an immutable
`CanonicalFinancials` shared by every engine. No engine re-queries the database.

This is what makes Modules 2–5 cheap to add: they consume the object that already exists.

---

## Known gaps (intentional, not defects)

- **Clerk** runs in dev-identity mode until keys are supplied. The interface does not change.
- **Redis/Celery** are configured but unused — nothing in Module 1 is slow enough to need them.
- **Highcharts** is installed; the dashboard uses CSS bars. Charts land with the time-series
  data in Module 2.
- **Seed data is synthetic**, generated with the workbook's own balance-sheet-plug technique.
  Real filings arrive via the Module 7 ingestion pipeline.
