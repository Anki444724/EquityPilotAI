# Impact Report — Valuation / Forecast "Current Price" Alignment

**Date:** 2026-08-06
**Scope:** Valuation module and Forecast (scenario) module — the displayed "Current Price".
**Status:** Report generated **before** any valuation logic is modified.

---

## 1. The decision

**Determination: the valuation/forecast "Current Price" is a *displayed current
market price*, so it must come from `LiveMarketService`.**

Rationale (evidence, not assumption):
- It is rendered to the user under the label **"Current price"** with hint **"market"**
  (`valuation/page.tsx:143`) and is drawn as the current-price marker on the
  value-range charts (`valuation/page.tsx:184`, `forecast/page.tsx:397`).
- It is the *same semantic* as the five pages already migrated to `LiveMarketService`
  (Dashboard / Companies List / Company Detail / Watchlist / Portfolio) — the 
  "price today" against which value and upside are judged.
- It currently reads the **stale `Company.current_price` DB column** via
  `analysis.company.current_price`. Leaving it there re-creates the exact divergence
  the migration eliminated: once a live provider is configured, the company page shows
  the live quote while the valuation page shows the stale column under the label
  "Current price · market".
- Therefore it is **not** an intentional "historical valuation snapshot" that deserves
  a rename. It is presented as today's market price, so it should be sourced from the
  live market service. (The *financial statements* driving intrinsic value remain the
  historical/filing data — only the market-price reference changes.)

**This changes the SOURCE of an input. It does NOT change any valuation formula.**

---

## 2. Current data flow (traced)

```
Company page / Dashboard / List / Watchlist / Portfolio
  → LiveMarketService.snapshot(company)          ← LIVE (already migrated)

Valuation & Forecast (NOT migrated — still DB column)
  AnalysisService.for_ticker() → AnalysisService.company (Company ORM)
    → analysis.company.current_price             ← STALE DB COLUMN
    → feeds DCF / Relative / DDM / Replacement / SOTP / Summary / Scenarios
```

`LiveMarketService.snapshot(company).live_price` already returns live-when-available
and falls back to the stored column (labelled via `price_source`), so a company with no
live feed degrades exactly as the internal tier does today — no regression when external
providers are absent.

---

## 3. Downstream effects — every computed output affected by the input change

Switching the `current_price` input from the DB column to the live-or-fallback market
price changes the **value** of that input. When the live price differs from the stored
price, all of the following recomputed outputs change. **The formulas themselves are
untouched.**

### 3.1 `backend/app/services/valuation/service.py` (input read at 8 sites)
| Method | Uses `current_price` for | Output that changes |
|---|---|---|
| `run_fcff` / `run_fcfe` → `DCFInputs.current_price` | `upside = intrinsic/price − 1`, `in_buy_zone = price ≤ max_buy` | `DCFOut.upside`, `.in_buy_zone`, `.current_price` |
| `run_relative` → `RelativeInputs.current_price` | P/E, P/B, dividend yield, PEG (`relative.py:174-201`); blended upside (`:383`) | `RelativeOut.current` multiples, `.upside`, `.current_price` |
| `run_ddm_model` → `DDMInputs.current_price` | upside | `DDMOut.upside` |
| `run_replacement` | upside | `ReplacementOut.upside` |
| `sensitivity` (`build_grid`) | `DCFInputs.current_price` + `build_grid(current_price)` | `SensitivityOut.current_price`, `.upside_cells` |
| `monte_carlo` | `run_simulation(current_price)` | `SimulationOut.current_price`, `.probability_above_price` |
| `summarise(current_price=…)` | per-method upside, blended `upside`, `in_buy_zone`, `recommendation` thresholds | `SummaryOut.upside`, `.recommendation`, `.in_buy_zone`, `.current_price` |
| `value_company` → `summarise(...)` | main `/valuation` response | full `ValuationResponse` |

Note: `maximum_buy_price = weighted × (1 − margin_of_safety)` is **independent** of
`current_price`, so it is unchanged; only `in_buy_zone` (which compares price to max-buy)
changes.

### 3.2 `backend/app/api/v1/valuation.py` (3 direct reads)
- L318 (sensitivity summary), L378 (simulation summary), L423 (SOTP `run_sotp`).

### 3.3 `backend/app/api/v1/forecast.py`
- L226 `cmp_price = analysis.company.current_price` → `run_all_scenarios(cmp_price)` →
  per-scenario `upside`, `expected_upside`, `bull_upside`, `bear_downside`,
  `risk_reward`, `verdict`, and `ScenarioResponse.current_price`.

### 3.4 Frontend consumers (display only — re-render live value)
- `valuation/page.tsx:143` "Current price" Stat, `:184` ValueRangeChart.
- `valuation/panels.tsx:17` FootballField `price = summary.current_price`, `:429` sim hint.
- `forecast/page.tsx:367` expected-upside hint, `:397` ValueRangeChart.

---

## 4. Test impact

| Suite | Impact |
|---|---|
| `test_valuation_engines.py`, `test_forecast_engine.py` | **None.** They call domain functions with an explicit `current_price` argument — unaffected by the source change at the service/API layer. |
| `test_valuation_api.py` (BHARATCP, TITAN), `test_forecast_api.py` | Values **unchanged in the test env** because seeded companies are served by the internal tier, which returns the same stored column value. |
| **Latency risk (new):** wiring valuation/forecast to `MarketDataRouter` means a **non-universe** symbol (BHARATCP) resolves as a US listing → external (Yahoo) is attempted once → network timeout (~12s) then falls to internal. Mitigation: the market cache is shared/300s, so this is a single cold-cache cost, and the internal tier returns the identical value, so assertions still hold. Empirically verified in the prior full-suite run (a 12s BHARATCP internal fetch already occurs). |
| `test_price_consistency.py`, `test_live_market.py` | Unaffected. |

---

## 5. Files to change

Backend:
- `app/services/live_market.py` — add a `price_for(company)` helper (returns `snapshot(company).live_price`, i.e. live-or-fallback).
- `app/services/valuation/service.py` — replace all 8 `analysis.company.current_price` reads with `self._market_price(analysis)`.
- `app/api/v1/valuation.py` — replace the 3 direct reads with `LiveMarketService(db).price_for(analysis.company)`.
- `app/api/v1/forecast.py` — set `cmp_price` from `LiveMarketService(db).price_for(analysis.company)`.

Frontend:
- No relabel required (the chosen path keeps the "Current price · market" label now correctly backed by live data). Optionally add a `price_source` note for provenance consistency — deferred unless requested.

No valuation **formula** changes. No DB schema/migration changes.

---

## 6. Risks

1. **Live-input drift:** because `current_price` is now a live input, `upside`,
   `recommendation` and `in_buy_zone` can move intraday as the quote changes while
   intrinsic value (filing-derived) stays fixed. This is standard market behaviour and
   is the intended meaning of "upside vs today's price", but it means a cached report
   and a fresh page may show different upside.
2. **External-provider dependency:** when only the internal tier is configured, the
   value is identical to today (stored column). When a live provider is added, valuation
   inputs use the live quote (with fallback), consistent with every other surface.
3. **Test latency:** the single cold-cache external attempt for BHARATCP (see §4). To be
   confirmed by running the suite; if it proves flaky, a follow-up can pin the reference
   company to the internal tier for tests (no production behaviour change).

---

## 7. Out of scope (other `Company.current_price` consumers — noted, not changed)

These read the DB column but are **not** part of the valuation/forecast display scope.
They carry the same stale-source concern and are candidates for a follow-up:
- `app/services/scoring/service.py:188-192` (P/B ratio metric)
- `app/services/reports/builder.py` / `renderers/base.py` ("Current price" in generated reports)
- `app/services/ai_scoring/evidence.py`, `app/services/ai/context_builder.py`
- `app/api/v1/valuation.py` `build_wacc` uses `company.market_cap` (a separate field, not `current_price`) for `market_value_equity` — left unchanged.

---

## 8. Acceptance checks (post-change)
1. `/company/RELIANCE/valuation` `summary.current_price` equals the live `market.live_price`.
2. `/company/RELIANCE/forecast/scenarios` `current_price` equals the same value.
3. Full backend test suite green (with the BHARATCP cold-cache latency bounded).
4. Frontend `tsc`/`next build` green (labels unchanged).

---

## 9. Implementation complete — verification

Executed on 2026-08-06. **No valuation or forecast formula was changed** — only the
source of the `current_price` input.

### Changes
- `app/services/live_market.py` — added `LiveMarketService.price_for(company)` (returns
  `snapshot(company).live_price`, i.e. live-when-available, stored-fallback otherwise).
- `app/services/valuation/service.py` — `__init__` builds a `LiveMarketService`; new
  `_market_price(analysis)` helper; all **9** `current_price` reads
  (`run_fcff`, `run_fcfe`, `run_relative`, `run_ddm_model`, `run_replacement`,
  `sensitivity`, `monte_carlo`, `value_company→summarise`) now call it.
- `app/api/v1/valuation.py` — `_market_price(db, analysis)` helper; the 3 direct reads
  (sensitivity `summarise`, simulation `summarise`, SOTP `run_sotp`) now use it.
- `app/api/v1/forecast.py` — `get_scenarios` sets `cmp_price` from
  `LiveMarketService(db).price_for(analysis.company)`.

### Verified at runtime
- `tests/test_valuation_live_alignment.py` (new): valuation `summary.current_price`,
  `dcf_fcff.current_price`, `relative.current_price` and forecast-scenario
  `current_price` all equal `market.live_price` (2945.0) for RELIANCE.
- **Full backend suite green** (exit 0), including `test_valuation_api.py`,
  `test_forecast_api.py`, engines and scenario suites.
- Frontend `tsc --noEmit` green (labels unchanged — the "Current price · market" label
  is now correctly backed by live data).
- Observed BHARATCP cold-cache external attempt (~12s once, then internal fallback) as
  predicted in §4/§6; bounded by the shared 300s market cache.

### Outcome
The valuation and forecast pages now render the **same live market price** as the
Dashboard / Companies / Company Detail / Watchlist / Portfolio pages. Because no live
provider is configured in this environment, the value is identical to the stored column
(2945.0); when a live provider is added, valuation inputs automatically move to the live
quote, consistent with every other surface.
