# Module 4 — Valuation Framework

**Scope:** ten valuation methodologies · full WACC engine · both discounting conventions and
terminal-value methods · sensitivity matrices · Monte Carlo · data-quality gate

**Status: complete and tested.** 538 tests passing (157 new). Awaiting review before Module 5.

---

## 1. Folder structure

```
backend/app/
├── domain/valuation/               ← PURE engines: no I/O, no framework
│   ├── wacc.py                     CAPM, Hamada relevering, dynamic schedule
│   ├── dcf.py                      FCFF + FCFE, both conventions, both TV methods
│   ├── relative.py                 multiples, targets, JUSTIFIED multiples
│   ├── ddm.py                      Gordon, two-stage, H-model
│   ├── sotp.py                     sum-of-the-parts + replacement value
│   ├── sensitivity.py              grids + Monte Carlo
│   └── data_quality.py             provenance and plausibility gate
├── services/valuation/service.py   orchestration and cross-method summary
├── schemas/valuation.py            typed contracts (70 OpenAPI schemas total)
└── api/v1/valuation.py             5 endpoints

frontend/src/
├── components/valuation/
│   ├── quality-banner.tsx          the disclosure, rendered above every output
│   └── panels.tsx                  football field, WACC, DCF, relative, matrix, MC
└── app/companies/[id]/valuation/page.tsx
```

---

## 2. The ten methodologies

| # | Method | Implementation |
|---|---|---|
| 1 | **DCF — FCFF** | Unlevered flows at WACC, full EV→equity bridge |
| 2 | **DCF — FCFE** | Levered flows at cost of equity, no bridge |
| 3 | **Relative** | Blended target across all multiple methods |
| 4 | **EV/EBITDA** | Trailing + 3 forward periods; EV-basis target |
| 5 | **P/E** | Trailing + forward; equity-basis target |
| 6 | **P/B** | Book-multiple target |
| 7 | **EV/Sales** | Margin-normalisation cross-check |
| 8 | **DDM** | Gordon · two-stage · H-model |
| 9 | **SOTP** | Per-segment basis, stakes, holdco discount |
| 10 | **Replacement value** | Inflation-indexed gross block + Tobin's Q |

Beyond the brief, the relative engine also computes **justified multiples** — what fundamentals
warrant, derived from the Gordon model rather than borrowed from peers:

```
Justified forward P/E  = (1 − b) / (Ke − g)
Justified trailing P/E = (1 − b)(1 + g) / (Ke − g)
Justified P/B          = (ROE − g) / (Ke − g)
Justified EV/EBITDA    = (1 − t)(1 − RR) / (WACC − g)
```

A peer trading at 30x tells you nothing about whether 30x is warranted. This does.

---

## 3. WACC engine

```
Ke = Rf + β × (mature ERP + country risk premium) + size premium + specific premium
βL = βU × [1 + (1 − t) × D/E]                              (Hamada)
WACC = We × Ke + Wd × Kd × (1 − t)
```

- **Three beta sources** — bottom-up relevered (default), regression, or the average.
  Bottom-up is default because single-stock regression betas are statistically unstable.
- **Country risk premium** separated from mature ERP, as emerging-market practice requires.
- **Cost of debt** observed where possible, synthetic (Rf + spread) where not, floored at 3%.
- **Dynamic WACC** recomputes the full build per period as leverage changes. Verified: a
  delevering forecast produces a *rising* WACC as the cheap debt weight falls.
- WACC bounded to 4%–35%; outside that the inputs are wrong and the flag is surfaced.

---

## 4. DCF conventions

| Convention | Discount periods | Effect |
|---|---|---|
| Year-end | 1, 2, 3… | conservative |
| **Mid-year** (default) | 0.5, 1.5, 2.5… | uplift of exactly √(1+r) − 1 |

Verified: mid-year/year-end PV ratio = **1.12^0.5** precisely at a 12% rate.

Terminal value supports **perpetual growth** and **exit multiple**, switchable per request. Each
reports the other as a diagnostic — the Gordon TV's implied exit multiple, and the exit
multiple's implied perpetual growth — so an analyst can see when a chosen multiple embeds an
absurd growth assumption.

Guards: growth within 50 bps of the discount rate is capped and warned; terminal value above
85% of EV is flagged.

---

## 5. Sensitivity and Monte Carlo

Both exploit `run_dcf` being a **pure function** — no I/O, no state, no argument mutation
(tested explicitly).

- **Sensitivity**: any two of WACC, terminal growth, revenue CAGR, EBIT margin, exit multiple.
  Configurable grid size, returned as both value and upside views, heat-mapped in the UI.
- **Monte Carlo**: triangular/normal/uniform distributions with bounds, seeded for
  reproducibility, returning percentiles, P(value > price) and a 20-bucket histogram.
  Failed trials are counted, never raised.

---

## 6. Data validation gate

The brief's requirement, implemented as a first-class engine.

**Provenance is an allowlist, not a blocklist.** Only recognised filing sources
(`annual_report`, `xbrl`, `audited_statement`…) can support an investment-grade conclusion.
Anything unrecognised — including our own `reference_model` — is downgraded. This was a
deliberate correction: my first implementation used a blocklist and certified an unknown source
as investment-grade.

**Four grades:**

| Grade | Meaning |
|---|---|
| `investment_grade` | Real filings, plausible outputs |
| `indicative` | Real data, some gaps or a stretched output |
| `illustrative` | Synthetic or incomplete — **disclosure shown** |
| `unreliable` | Output is not usable for any decision |

**Plausibility checks** run even on real data: upside > +300% or < −90%, EV/EBITDA > 60x,
broken balance sheet, non-converged forecast, coverage < 60%, ungrounded assumptions.

Every valuation response carries a `quality` block, and the UI renders the disclosure banner
above the numbers, sized and coloured by severity. **An extreme upside can never appear
unqualified** — enforced by a universe-wide test.

---

## 7. APIs

| Method | Endpoint |
|---|---|
| GET | `/company/{ticker}/valuation` — all methods + summary + quality |
| GET | `/company/{ticker}/valuation/wacc` |
| GET | `/company/{ticker}/valuation/sensitivity?row=&col=&steps=` |
| GET | `/company/{ticker}/valuation/simulation?trials=&seed=` |
| POST | `/company/{ticker}/valuation/sotp` |

Query parameters: `scenario`, `horizon` (3/5/10), `convention`, `terminal_method`,
`terminal_growth`, `exit_multiple`, `margin_of_safety`, `dynamic_wacc`.

---

## 8. Test results

```
538 passed in 6.94s
```

| File | Tests |
|---|---:|
| `test_valuation_engines.py` | **87** |
| `test_valuation_api.py` | **70** |
| *(Modules 1–3)* | 381 |

**Verified by independent recomputation:**

| Assertion | Result |
|---|---|
| CAPM build recomputed from components | ✔ |
| WACC = We·Ke + Wd·Kd(1−t) | ✔ |
| Hamada relever/unlever are inverses | ✔ |
| Mid-year uplift = √(1+r) exactly | ✔ |
| Gordon TV = CF×(1+g)/(r−g) | ✔ |
| Enterprise→equity bridge | ✔ |
| Justified P/E = (1−b)/(Ke−g) | ✔ |
| Justified P/B = (ROE−g)/(Ke−g) | ✔ |
| Sustainable payout = 1 − g/ROE | ✔ |
| H-model sits between Gordon and two-stage | ✔ |
| SOTP segment values + stakes + holdco discount | ✔ |
| Tobin's Q | ✔ |
| Sensitivity monotonic in WACC and growth | ✔ |
| Monte Carlo reproducible with a seed | ✔ |
| `run_dcf` does not mutate inputs | ✔ |
| Synthetic data always flagged | ✔ |
| Extreme upside always carries a critical flag | ✔ |

### Performance

| Operation | Time |
|---|---:|
| Single DCF | **0.031 ms** |
| WACC build | 0.008 ms |
| 5×5 sensitivity grid | 0.80 ms |
| Monte Carlo, 1,000 trials | 36 ms |
| Monte Carlo, 10,000 trials | 350 ms |
| `GET /valuation` (full HTTP, all 10 methods) | 13.4 ms |
| `GET /valuation` 10-year + dynamic WACC | 16.6 ms |

---

## 9. Defects found and fixed

**1. Justified multiples were economically incoherent.** My first implementation fed the
*reported* payout ratio into the Gordon formula alongside an unrelated terminal growth rate,
producing a justified forward P/E of 2.6x. The formula was right; the inputs described a company
that cannot exist — 18% ROE with 25% payout implies 13.5% growth, not 5%. Fixed by deriving the
payout from the sustainable-growth identity (`payout = 1 − g/ROE`) and **warning** when the
reported pair is inconsistent, rather than silently overriding it.

**2. The quality gate used a blocklist.** It flagged `seed` and `synthetic` but certified
`reference_model` as investment-grade. Any unanticipated source would have passed. Switched to
an allowlist of recognised filing sources — fail-safe by default.

**3. Reference company priced at 61x earnings.** My first reference dataset paired EPS 13.80
with a ₹845 price, producing a spurious −78% "Sell". Corrected to ₹268 (~19x), a plausible FMCG
rating.

**4. Three Module 1 tests hardcoded a universe of 20.** Adding the reference company broke them.
Fixed by deriving the expected count from the seed definition, so future additions don't break
unrelated assertions.

---

## 10. On the synthetic-data problem

I flagged in Module 3 that the seed universe produces implausible valuations. Module 4 addresses
it two ways:

1. **The gate now catches it automatically.** Titan's seed data yields a −96% downside and 307x
   EV/EBITDA; both trigger critical flags and the `unreliable` grade.
2. **A reference company was added** (`BHARATCP`) with coherent economics — 16.4% EBITDA margin,
   CFO ₹3,451cr against capex ₹1,165cr, 25.4% tax rate — so the engine can be validated against
   plausible inputs. It produces DCF ₹171, relative ₹277, TV at 64% of EV.

The crude seed's DCF is *correctly* negative: a business whose capex permanently exceeds its
operating cash flow has no positive intrinsic value. The engine is right; the data is not
investment-grade, and the platform now says so.

---

## 11. Known limitations

1. **Sensitivity axes for revenue CAGR and EBIT margin rescale the cash-flow stream** rather than
   re-running the full forecast engine per cell. Exact for WACC/growth/exit-multiple axes;
   first-order for the two operating axes. A full re-run per cell would be exact but costs ~25×
   more for a 5×5 grid.
2. **SOTP requires segments to be supplied** in the request. Automatic segment detection needs
   segment-level reporting, which arrives with document ingestion (Module 7).
3. **Replacement value uses an inflation-indexed gross-block proxy.** A rigorous estimate needs
   an asset-register vintage profile — marked future-ready as the brief specified.
4. **No real market data.** Beta, risk-free rate and ERP are configurable inputs with documented
   defaults, not live feeds.
5. **Monte Carlo varies WACC, terminal growth and a cash-flow scaling factor.** Correlated
   multivariate draws (e.g. margin and growth co-moving) would be more rigorous; the
   architecture supports it via additional `StochasticVariable` entries.
