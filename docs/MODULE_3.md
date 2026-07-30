# Module 3 — Forecast Engine

**Scope:** integrated three-statement projection engine · configurable horizons · bull/base/bear
scenarios · editable assumption model · Highcharts visualisation

**Status: complete and tested.** 381 tests passing (144 new). Awaiting review before Module 4.

---

## 1. Folder structure

```
backend/app/
├── domain/forecast/                ← PURE engine: no I/O, no framework
│   ├── assumptions.py              30 drivers, provenance, per-year overrides
│   ├── revenue.py                  4 build methods + growth fade
│   ├── margins.py                  EBITDA/EBIT from margin assumptions
│   ├── capex.py                    capex split + net-block roll-forward
│   ├── depreciation.py             asset-based schedule (analytical view)
│   ├── working_capital.py          cycle-days driven
│   ├── debt.py                     circular solver with cash sweep
│   ├── taxes.py                    one rate, applied to PBT and EBIT
│   ├── cashflow.py                 CFO/CFI/CFF, FCFF (two builds), FCFE
│   ├── scenarios.py                driver shifts + probability weighting
│   └── engine.py                   orchestrator
├── services/forecast/
│   ├── calibration.py              derives defaults from reported history
│   ├── metadata.py                 labels/units/groups for the UI
│   └── service.py                  persistence + 4-tier resolution
├── models/forecast.py              forecasts, forecast_assumptions
├── schemas/forecast.py             typed API contracts
└── api/v1/forecast.py              4 endpoints

frontend/src/
├── components/charts/index.tsx     Highcharts: history-vs-forecast, scenario,
│                                   cash-flow, value-range
├── components/forecast/assumption-editor.tsx
└── app/companies/[id]/forecast/page.tsx
```

---

## 2. Database schema

Only **inputs** are persisted. Projections are recomputed on every request, so a stored forecast
can never drift from what the engine produces today, and an engine improvement immediately
benefits every saved forecast.

| Table | Purpose |
|---|---|
| `forecasts` | horizon, revenue method, segments, `revision` counter |
| `forecast_assumptions` | **one driver per row** — not a JSON blob |

Row-level storage is deliberate: it lets a single assumption be queried, audited and attributed.
`scenario` is nullable — a `NULL` row is a shared base assumption; a non-null row is an explicit
per-scenario override.

```
forecast_assumptions
  driver           VARCHAR(64)   e.g. "ebitda_margin"
  scenario         VARCHAR(16)   NULL = base for all cases
  value            FLOAT
  by_year          JSON          {period: value} overrides
  source           VARCHAR(32)   default|historical|analyst|ai_extracted|management_guidance
  citation         TEXT          e.g. "FY25 annual report, MD&A p.42"
  requires_review  BOOLEAN
  UQ (forecast_id, driver, scenario)
```

---

## 3. APIs

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/company/{ticker}/forecast` | run a projection (`?scenario=&horizon=&method=`) |
| POST | `/company/{ticker}/forecast` | create a saved forecast |
| PUT | `/company/{ticker}/forecast/assumptions` | edit drivers |
| GET | `/company/{ticker}/forecast/scenarios` | bull/base/bear + weighted conclusion |
| GET | `/company/{ticker}/forecast/list` | saved forecasts |

Every response carries the projection, the 10-year history (for charting), the full assumption
set with provenance, engine-health flags and a grid-ready section view.

---

## 4. Forecast engine

### Execution order

```
revenue → capex/depreciation → margins → working capital
        → provisional tax → DEBT SOLVE → final tax → cash flow
```

**Tax is computed twice by design.** The first pass has no interest figure; once the debt solver
returns, PBT is restated with actual interest and the charge is recomputed. Without this the
model would tax a PBT that never existed.

### The circularity, solved explicitly

Interest depends on the debt balance, which depends on free cash flow, which depends on
interest. Spreadsheets resolve this with iterative calculation — fragile and order-dependent.
`solve_debt_schedule` iterates to a fixed point with a stated tolerance (1e-6) and **reports**
whether it converged rather than failing silently. Typical convergence: **7–8 iterations**.

Interest is charged on the **average** of opening and closing balances, correct when debt moves
materially during a year. Surplus cash sweeps into prepayment above a minimum operating buffer.

### Revenue — four methods

| Method | Behaviour |
|---|---|
| CAGR | single rate, optionally fading |
| Volume × Price | `(1+v)(1+p)−1` — 6% volume with 4% price is **10.24%**, not 10% |
| Segment | bottom-up; mix shift lifts blended growth automatically |
| Organic + Acquisition | tracks inorganic contribution separately |

**Growth fade** decays the near-term rate toward a long-run rate across the horizon. A flat CAGR
held for ten years implies a company outgrows its economy forever; `growth_fade` (0 = flat,
1 = full linear convergence) makes the fade explicit and adjustable.

### Two FCFF builds, reconciled

```
top-down   NOPAT + D&A + ΔNWC − capex
bottom-up  CFO − (1−t)(other income + interest income − interest) − capex
```

They must agree. During development they did **not** — the check caught a real error in my
bottom-up derivation, which I then derived from the PAT identity. They now agree to machine
precision (gap ≤ 2e-13), and any future divergence surfaces as a warning.

### Assumption model — 30 drivers, nothing hardcoded

Each driver is a scalar default plus optional `by_year` overrides plus a `Provenance` tag. A
flat rate is the degenerate case, not the design centre.

Coverage: revenue (7) · margins (4) · capex/depreciation (3) · working capital (5) · debt (5) ·
tax & distribution (2) · valuation (4).

### Calibration, not defaults

Assumptions are **derived from the company's own history** — trailing CAGR, 3-year average
margin, cycle days from balances, implied cost of debt from finance costs. Sanity bounds reject
implausible values: the seed's 73% effective tax rate is rejected and falls back to a documented
constant, marked `DEFAULT` so the UI shows which assumptions are grounded. For Titan, **14 of 30
drivers calibrate from history**.

### Scenarios derived, not duplicated

Bull and bear are **shifts from the base case**, so an analyst edit propagates into all three.
Rates shift additively (200 bps on a margin); multiples shift proportionally (±25%). Bounds keep
shifted values economically sensible — a 3% margin cannot shift to negative.

---

## 5. Highcharts implementation

| Chart | Purpose |
|---|---|
| History vs Forecast | reported (blue) and projected (purple) on one axis, shaded FORECAST band, margin on a secondary axis |
| EBITDA / PAT / EPS trends | same treatment per metric |
| Free cash flow | FCFF and FCFE columns with negative-value colouring |
| Scenario comparison | bull/base/bear splines for revenue, EBITDA, PAT, EPS, FCFF |
| Value range | horizontal bars with CMP and expected-value plot lines |

Charts re-theme on light/dark toggle, defer rendering until mount to avoid SSR mismatch, and
format tooltips through the shared formatters. **No chart computes anything** — every series is
a value the API returned.

---

## 6. Unit test results

```
381 passed in 4.88s
```

| File | Tests | Focus |
|---|---:|---|
| `test_forecast_engine.py` | **56** | drivers, revenue methods, fade, capex roll-forward, WC, debt solver, integration |
| `test_forecast_api.py` | **64** | 4 endpoints, horizons, methods, edits, AI-readiness, calibration |
| `test_forecast_scenarios.py` | **24** | derivation, bounds, probability weighting |
| *(Modules 1–2)* | 237 | unchanged, still passing |

**Verification highlights**

| Assertion | Result |
|---|---|
| Volume × price compounds, not adds | ✔ 10.24% |
| Growth fade decays 20% → 5% linearly | ✔ |
| Depreciation charged on **opening** block | ✔ |
| Maintenance + growth capex = gross capex | ✔ every period |
| CCC = DIO + DSO − DPO | ✔ |
| Debt solver reaches a true fixed point | ✔ 7–8 iterations, residual < 1e-6 |
| Interest on average balance | ✔ recomputed independently |
| **Both FCFF builds agree** | ✔ to 1e-6, all horizons |
| CFO + CFI + CFF = net cash flow | ✔ |
| PBT includes interest before tax | ✔ |
| Equity rolls forward with retained earnings | ✔ |
| Every driver overridable | ✔ all 30 tested |
| Analyst edit propagates into bull/bear | ✔ 30% → 32%/28% |
| Scenario override beats derived shift | ✔ |
| All 5 provenance types accepted | ✔ |
| 6 companies × 3 horizons converge & reconcile | ✔ |

### Performance

| Operation | Time |
|---|---:|
| 3-year forecast (engine only) | 0.254 ms |
| 5-year forecast | 0.396 ms |
| 10-year forecast | 0.821 ms |
| 3-scenario suite | 1.468 ms |
| `GET /forecast` (full HTTP) | ~11 ms |
| `GET /forecast/scenarios` | ~10 ms |

A 10-year, three-scenario run is three full integrated models with iterative debt solving —
under 1.5 ms.

---

## 7. AI readiness

The engine is AI-ready **by data model, not by a planned refactor**:

1. `Provenance` includes `AI_EXTRACTED` and `MANAGEMENT_GUIDANCE` — accepted today.
2. `citation` stores the source (`"FY25 annual report, MD&A p.42"`).
3. `requires_review` flags AI writes for human sign-off.
4. `ForecastService.update_assumptions()` is the **single** write path — the AI layer calls the
   same method an analyst does, differing only in `source`.
5. `by_year` lets guidance apply to specific years ("margin expands 100 bps next year only").

Tested: every provenance value is accepted through the API, and citations round-trip. When
Module 7 parses a document, it writes drivers here with **no backend change**.

---

## 8. Defects found and fixed

**1. Bottom-up FCFF was wrong (caught by the reconciliation check).** My first derivation
subtracted the interest tax shield and other income inconsistently, producing an 18–30 ₹ cr gap.
The check flagged it immediately; I re-derived from the PAT identity. This is exactly why the
two builds exist.

**2. Duplicate `_safe_div` in `statements.py`.** A private helper from Module 1 duplicated
`domain/calc.safe_div`. Found by a self-audit for repeated logic; now an alias.

**3. Seed script missed the new tables.** `create_all` ran before forecast models were imported —
the same class of ordering bug as Module 2. Fixed at the source.

**4. My test was wrong, not the engine.** `test_per_year_overrides_accepted` asserted year 2
equalled the flat input, but calibration sets `growth_fade=0.5`, so later years correctly decay.
The engine was right; I corrected the test to compare against the un-overridden path.

---

## 9. Known limitations

1. **Scenario value-per-share is an exit-multiple measure**, not a DCF. Full DCF, WACC build and
   sensitivity grids are Module 4. The measure is computed identically across cases so the
   comparison is fair.
2. **Seed data produces low valuations.** Titan's synthetic seed has a 6.6% EBITDA margin and
   89 cr shares against a real ₹3,320 price — roughly 307× EV/EBITDA. The engine arithmetic was
   verified exact against a hand calculation; the *data* is unrealistic, as documented in
   Module 2. Real filings will produce sensible upside figures.
3. **Working capital infers COGS from the EBITDA margin** rather than forecasting the cost stack
   line by line. Appropriate at this level; a full cost-build would need segment cost data.
4. **No mid-year convention** on cash flows yet — it belongs with the DCF discounting in Module 4.
5. **Segment forecasts do not yet carry per-segment margins.** The field exists on
   `SegmentAssumption`; wiring it through needs segment-level historical costs.
