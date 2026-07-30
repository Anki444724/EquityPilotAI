# Module 2 — Historical Financial Analysis

**Scope:** Income Statement · Balance Sheet · Cash Flow · Ratios · Working Capital · Debt ·
Capex · Shareholding

**Status: complete, tested and running.** 237 tests passing. Awaiting approval for Module 3.

---

## 1. Folder structure

```
backend/
├── app/
│   ├── api/v1/
│   │   ├── analysis.py           ← 9 Module 2 endpoints (thin adapters only)
│   │   ├── auth.py  companies.py  dashboard.py  router.py
│   ├── domain/                   ← PURE calculation, no I/O, no framework
│   │   ├── calc.py               ← shared primitives (safe_div, avg_balance, days…)
│   │   └── financials/
│   │       ├── canonical.py      ← 4-tier precedence resolution
│   │       ├── line_items.py     ← 54 canonical items (generated)
│   │       └── statements.py     ← IS / BS / CF engines
│   ├── models/
│   │   ├── company.py            ← companies, financial_facts
│   │   └── analysis.py           ← debt_instruments, credit_ratings, shareholding
│   ├── schemas/
│   │   ├── common.py             ← MetricRow / MetricSection / AnalysisResponse
│   │   └── analysis.py           ← typed response models per endpoint
│   ├── services/
│   │   ├── analysis_service.py   ← orchestrator: ONE load, shared by all
│   │   ├── financials/service.py
│   │   ├── ratios/service.py
│   │   ├── working_capital/service.py
│   │   ├── debt/service.py
│   │   ├── capex/service.py
│   │   └── shareholding/service.py
│   └── db/  core/  workers/  ai/  ocr/  reports/
└── tests/                        ← 237 tests
    ├── conftest.py               ← shared fixtures + single seeded test DB
    ├── test_calc.py              (29)  test_statements.py       (15)
    ├── test_ratios.py            (26)  test_wc_capex.py         (23)
    ├── test_debt_shareholding.py (29)  test_analysis_api.py     (94)
    └── test_api.py               (21)

frontend/src/
├── app/companies/[id]/financials/page.tsx   ← 9-tab analysis workspace
├── components/analysis/
│   ├── metric-grid.tsx           ← generic renderer (zero business logic)
│   └── panels.tsx                ← debt / WC / shareholding panels
└── lib/{api,types,format}.ts
```

---

## 2. Database schema

Five tables. Anything **derivable** from the 54 canonical line items is computed on demand and
deliberately never stored — storing derived values is how a second source of truth appears.

| Table | Rows (seed) | Purpose |
|---|---:|---|
| `companies` | 20 | Master record, `data_version` for cache invalidation |
| `financial_facts` | 10,800 | Canonical grid: 54 items × 10 years × 20 companies |
| `debt_instruments` | 140 | Facility schedule — separately disclosed, not derivable |
| `credit_ratings` | 20 | Agency ratings |
| `shareholding_snapshots` | 240 | Quarterly patterns — 12 quarters × 20 companies |

**Key indexes**

| Index | Columns | Serves |
|---|---|---|
| `ix_fact_lookup` | `company_id, fiscal_year, line_item` | the single-query fact load |
| `uq_fact_company_year_item_precedence` | + `precedence` | one row per tier, enabling the 4-tier chain |
| `ix_debt_company_year` | `company_id, fiscal_year` | instrument schedule |
| `ix_shareholding_company_period` | `company_id, fiscal_year, quarter` | quarterly series |
| `ix_company_sector_mcap` | `sector, market_cap` | sector screens |

Percentages are stored as **fractions** (0.521 = 52.1%) throughout, so no unit conversion ever
happens between layers.

---

## 3. API documentation

Base: `/api/v1` · Auth: bearer token (dev identity when Clerk is unconfigured) ·
Full spec: `docs/openapi.json` (31 schemas), live at `/docs`.

| Method | Endpoint | Returns |
|---|---|---|
| GET | `/company/{ticker}/income-statement` | 7 sections, 40 rows |
| GET | `/company/{ticker}/balance-sheet` | 6 sections + balance check |
| GET | `/company/{ticker}/cash-flow` | 5 sections, CFO/CFI/CFF + quality |
| GET | `/company/{ticker}/ratios?wacc=0.12` | 6 families, **50 ratios** |
| GET | `/company/{ticker}/working-capital` | components, cycle days, intensity, 3 flags |
| GET | `/company/{ticker}/debt` | profile, instruments, ladder, 5 covenants |
| GET | `/company/{ticker}/capex` | maintenance/growth split, intensity |
| GET | `/company/{ticker}/shareholding` | pattern, pledge, trends, ownership signal |
| GET | `/company/{ticker}/financials` | headline summary + revenue CAGR |

`{ticker}` is case-insensitive and also accepts a company UUID.

### Shared response envelope

Every analysis endpoint returns the same shape, which is what lets one frontend component
render all of them:

```json
{
  "company":  { "id": "...", "name": "Titan Company Ltd", "ticker": "TITAN", ... },
  "periods":  { "fiscal_years": [2016, ...], "labels": ["FY16", ...], "unit": "₹ cr" },
  "sections": [
    { "key": "revenue", "title": "Revenue", "rows": [
        { "key": "total_revenue", "label": "Total revenue", "unit": "₹ cr",
          "values": [3036.0, ..., 14528.6],
          "is_subtotal": true, "indent": 0, "note": null }
    ]}
  ],
  "has_data": true,
  "warnings": []
}
```

The `unit` field is authoritative — the client formats according to what the backend declares
and never infers meaning from a value.

### Endpoint-specific extras

- **ratios** → `wacc_assumption` (ROIC spread and EVA are `null` without it)
- **working-capital** → `flags[]`, `cost_of_debt_assumption` (derived from reported finance costs)
- **debt** → `instruments[]`, `maturity_ladder[]`, `covenants[]`, `reconciliation`, `blended_rate`,
  `floating_rate_share`, `foreign_currency_share`, `flags[]`
- **shareholding** → `signal` (Accumulation … Distribution), `flags[]`

---

## 4. Unit test results

```
237 passed in 3.68s
```

| File | Tests | Covers |
|---|---:|---|
| `test_calc.py` | 29 | primitives; None-vs-zero; average balances; CAGR edge cases |
| `test_statements.py` | 15 | workbook equivalence; precedence chain |
| `test_ratios.py` | 26 | all six families, independently recomputed |
| `test_wc_capex.py` | 23 | cycle days on COGS; capex split invariants |
| `test_debt_shareholding.py` | 29 | reconciliation; ladder; covenants; ownership signal |
| `test_analysis_api.py` | 94 | all 9 endpoints, contract + universe-wide invariants |
| `test_api.py` | 21 | Module 1 regression (unchanged) |

**Verification highlights**

| Assertion | Result |
|---|---|
| Revenue / EBITDA / PAT vs workbook | 14,528.6 / 961.9 / 125.5 — exact |
| Balance sheet ties, all 10 years × 8 companies | ✔ `balance_check` = 0.000000 |
| Cash flow: CFO + CFI + CFF = net change | ✔ every period |
| **5-step Du Pont multiplies back to ROE** | ✔ to 1e-9 |
| Equity ratio = 1 / financial leverage | ✔ to 1e-9 |
| Debt schedule reconciles to balance-sheet gross debt | ✔ difference 0.0000 |
| Maturity ladder sums to gross debt | ✔ |
| Shareholding totals exactly 100%, all 12 quarters × 20 companies | ✔ |
| Empty company fabricates no values | ✔ every ratio `None` |

---

## 5. Performance

| Endpoint | p50 latency | Payload |
|---|---:|---:|
| income-statement | 10.3 ms | 10.6 KB |
| balance-sheet | 8.5 ms | — |
| cash-flow | 8.3 ms | — |
| ratios (50 ratios × 10 yrs) | 10.0 ms | 17.0 KB |
| working-capital | 8.5 ms | — |
| debt | 9.1 ms | 9.1 KB |
| capex | 8.4 ms | — |
| shareholding | 8.6 ms | — |

Frontend financials page SSR: **140 ms**. Budget was 200 ms p95 — met with headroom.

Statements are built once per request via `AnalysisService` `cached_property` and shared by
every service, so `/ratios` costs barely more than `/income-statement` despite computing 50
ratios on top of three full statements.

---

## 6. Compliance with the implementation rules

| Rule | How it is satisfied |
|---|---|
| Don't copy Excel formulas line-by-line | Services are written as domain logic with documented conventions; the workbook supplied *rules*, not code |
| Reusable Python services | Six service packages under `services/`, each independently constructible from plain lists |
| Each calculation exists once | `domain/calc.py` holds every primitive. Verified by grep: `DAYS_IN_YEAR` and `avg_balance` each defined once and imported everywhere |
| No business logic in frontend | `metric-grid.tsx` switches on the backend's `unit` string only. No financial arithmetic in any `.tsx` |
| All ratios computed by the API | 50 ratios returned pre-computed; client performs no division |
| Strongly typed Pydantic models | 31 OpenAPI schemas; `mypy`-friendly dataclasses in the domain layer |
| REST endpoint per module | 9 endpoints, ticker-addressed |
| DB models, not worksheet references | 5 tables; zero cell references anywhere in the codebase |
| No hard-coded row numbers | Verified — no `L15`-style references; line items are enum keys |
| Independently testable | Every service takes plain lists, so 87 unit tests run with no database at all |

---

## 7. Defects found and fixed during Module 2

**1. SQLAlchemy relationship failed to resolve (real bug, caught by a new test).**
`models/analysis.py` declared `relationship("Company")` by string without importing the class.
Any code path that imported `analysis` before `company` raised
`InvalidRequestError: expression 'Company' failed to locate a name`. The API happened to work
because `main.py` imported them in a lucky order; the isolated unit tests did not. Fixed with a
direct import and a typed `Mapped[Company]` relationship.

**2. Test cross-contamination between API modules.**
`test_api.py` and `test_analysis_api.py` each built their own in-memory database and each
installed a `dependency_overrides` entry on the *same* FastAPI app object. Whichever imported
last won, so 18 tests failed in a full run while passing in isolation. This was a fault in my
test harness, not the application. Fixed by moving the database and override into `conftest.py`
so both modules share one seeded instance.

**3. Seeded shareholding squeezed retail to zero.**
The first seed allocated promoter stakes up to 69%, which combined with institutional holdings
pushed the retail residual negative — it clamped to 0.0% and looked like a calculation bug.
The clamp was doing its job; the *data* was wrong. Rebalanced the generator and added a
universe-wide test asserting retail > 0 for every company.

**4. Duplicated calculations found by self-audit.**
After building the services I grepped for repeated logic and found `365` hard-coded in
`ratios/service.py` and `_avg` implemented separately in ratios and working capital. Both now
delegate to `domain/calc.py`. This was exactly the rule I had been asked to enforce, and I had
broken it in two places before checking.

---

## 8. Conventions applied (and why)

- **Average balances** for every flow-over-stock ratio. Closing balances overstate returns in a
  growing business; the specification uses averages and so do we.
- **DIO and DPO on COGS**, DSO on revenue. Payables and inventory relate to cost, not sales.
- **Undefined ≠ zero.** Any ratio with a zero or missing denominator returns `null` and renders
  as an em dash.
- **Operating working capital** excludes cash and debt, so it measures trading capital rather
  than the accounting subtotal.
- **Maintenance capex capped at gross capex** — a business cannot spend less than nothing on
  upkeep, so the D&A proxy is bounded.
- **Asset turn on growth capex lags one year**, since capital deployed this year produces
  revenue next year.
- **Debt schedule is reconciled, not trusted.** Instrument totals are compared against
  balance-sheet gross debt and any gap is surfaced as a flag rather than absorbed.

---

## 9. Known limitations

1. **Covenant thresholds are a default package**, not per-facility terms. Real covenants vary by
   agreement; capturing them needs a `covenant` table, which belongs with document ingestion
   (Module 7).
2. **Seed data is synthetic.** Ratios are therefore flat across years — the generator applies
   constant proportions. Real filings will produce genuine variation.
3. **Altman Z-score uses the manufacturing variant** with book equity. The market-value variant
   requires a share-price time series, which arrives with market data.
4. **No quarterly statements yet.** Only annual periods; the `09 Quarterly` view is deferred.
5. **Highcharts still unused** — the panels use CSS bars. Time-series charting is worth doing
   once, properly, alongside the forecast curves in Module 3.
