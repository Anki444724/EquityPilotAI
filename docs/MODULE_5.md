# Module 5 — Institutional Scoring Engine

**Scope:** 13 scoring categories · editable weight profiles · confidence engine ·
AI-ready explanations · radar/contribution/history/peer visualisation

**Status: complete and tested.** 708 tests passing (170 new). Awaiting review before Module 6.

---

## 1. Folder structure

```
backend/app/
├── domain/scoring/                 ← PURE primitives, no I/O
│   ├── base.py                     MetricScore, confidence, band/linear/trend scoring
│   ├── weights.py                  13 categories, 5 built-in profiles
│   └── inputs.py                   ScoringInputs + QualitativeInputs
├── services/scoring/               ← the 13 category scorers
│   ├── business_quality.py         financial_quality.py    management_quality.py
│   ├── capital_allocation.py       competitive_advantage.py governance.py
│   ├── risk.py  (business risk)    financial_risk.py       valuation.py
│   ├── growth_quality.py           cash_flow_quality.py    esg.py
│   ├── momentum.py
│   ├── overall_score.py            composite, grade, stars, recommendation
│   └── service.py                  orchestration, profiles, history, peers
├── models/scoring.py               weight profiles + score snapshots
├── schemas/scoring.py              typed contracts
└── api/v1/scoring.py               5 endpoints

frontend/src/
├── components/scoring/panels.tsx   stars, grade, confidence bar, category table,
│                                   profile picker, explanation feed, peer table
├── components/charts/index.tsx     + ScoreRadar, ContributionChart, ScoreHistoryChart
└── app/companies/[id]/scoring/page.tsx
```

---

## 2. Database schema

Only two tables, because only two things are worth storing.

| Table | Purpose |
|---|---|
| `scoring_weight_profiles` | User-defined philosophies. Built-ins live in code. |
| `score_snapshots` | Point-in-time records — the one place a *computed* value is persisted |

Snapshots are the exception to the platform's "never store derived values" rule, and
deliberately so: "what did this score last quarter" cannot be answered by the live engine,
because the inputs have since changed. A trend needs a record.

```
score_snapshots
  company_id, as_of, profile_key        UQ — one snapshot per company/date/profile
  overall_score, grade, stars, recommendation, conviction
  confidence, verified_pct, estimated_pct, missing_pct
  category_scores  JSON                 {category: raw_score} — rebuilds a radar
```

---

## 3. The 13 categories

| Category | Anchor metric |
|---|---|
| Business Quality | **ROIC vs WACC spread** |
| Financial Quality | ROE, ROCE, accrual quality |
| Management Quality | **Incremental ROIC** on capital deployed |
| Capital Allocation | Reinvestment rate, judged *conditionally* on ROIC |
| Competitive Moat | Qualitative sources **+ excess-return persistence** |
| Corporate Governance | Audit qualifications, pledge, related-party |
| Financial Risk | Net debt/EBITDA, coverage, Altman Z |
| Business Risk | Earnings volatility, cyclicality, fixed-cost intensity |
| Valuation | Upside, P/E, EV/EBITDA, premium to justified multiple |
| Growth Quality | Self-funded growth, profit vs revenue, per-share growth |
| Cash Flow Quality | CFO/EBITDA, FCF consistency, capex intensity |
| ESG | Environmental, social, disclosure |
| Momentum | Price and earnings momentum |

Every category returns the five required outputs: **raw score, weighted score, confidence,
explanation, data source**.

Three deliberate design choices are worth calling out:

**Capital allocation is judged conditionally.** Heavy reinvestment scores *well* when ROIC
exceeds WACC and *badly* when it does not — the same 60% reinvestment rate is value-accretive
or value-dilutive depending on returns, and a single band table would get that backwards.

**Moat requires corroboration.** A company can score 10/10 on every qualitative moat source and
still be capped, because the category also tests whether ROIC actually exceeded WACC over the
period. A moat with no excess returns is a story, not a moat. Tested explicitly.

**Financial risk and business risk are separate.** A debt-free company can still run a violently
cyclical business; conflating the two hides that.

---

## 4. Weight engine

Five built-in profiles, each encoding a recognisable philosophy. Weights are declared on a 0–100
scale for readability and **normalised on construction**, so relative weights of 3/2/1 and
0.5/0.33/0.17 are identical.

| Profile | Top three emphases |
|---|---|
| Balanced | Financial Quality 12% · Business Quality 11% · Valuation 11% |
| Conservative | **Financial Risk 14%** · Financial Quality 12% · Governance 11% |
| Growth | **Growth Quality 17%** · Moat 14% · Business Quality 13% |
| Value | **Valuation 23%** · Financial Risk 12% · Financial Quality 11% |
| Quality | **Moat 17%** · Business Quality 15% · Financial Quality 13% |

Custom profiles are created through `PUT /scoring/weights`, validated (unknown categories,
negative weights and built-in collisions all rejected), and normalised. Value weights valuation
at 23% against Growth's 4% — the same company genuinely scores differently.

---

## 5. Confidence engine

The engine's central idea: **a score is worthless without knowing how much of it rests on real
data.**

Every metric carries a `DataOrigin` — `verified` (1.00), `estimated` (0.65), `analyst` (0.50) or
`missing` (0.00) — and confidence is the **weight-adjusted** mean of those. Weighted, not
counted, so a heavily weighted gap damages confidence more than a trivial one. Tested.

Every response reports **confidence %, verified %, estimated %, analyst % and missing %**, and
the four shares always sum to 1.

**Confidence gates the recommendation.** Below 55% the engine caps its call at HOLD and says
why: *"Capped at HOLD: confidence is only 43% (38% of weighted inputs are missing), which does
not support a directional call."* The platform should be less certain when it knows less.

Two further overrides exist, both stated in the rationale rather than applied silently:

- **Valuation cap** — quality never justifies any price. Observed live: a 71.0/100 composite
  mapping to ACCUMULATE was capped to HOLD because valuation scored 2.7/10.
- **Balance-sheet cap** — fragility outranks everything, capping at REDUCE.

---

## 6. AI-ready explanations

Every metric produces a prose explanation citing the figure that drove it. Real output:

> ROIC of 28.6% versus a 15.1% cost of capital is a +13.4% spread — value-creating.

> Capital deployed over the last three years earned 27.1%, a +11.9% spread over the 15.1% cost
> of capital — management is creating value with new investment.

> The market pays a +147% premium to the multiple fundamentals justify on growth, payout and
> cost of equity.

`GET /scoring/explanation` returns these pre-sorted into **key positives**, **key negatives** and
**data gaps**, each tagged with category, weight, origin and source. Tests assert they are
readable sentences and that over 60% cite a number.

---

## 7. APIs

| Method | Endpoint |
|---|---|
| GET | `/company/{ticker}/scoring?profile=&save=` |
| GET | `/company/{ticker}/scoring/history?profile=` |
| GET | `/company/{ticker}/scoring/explanation?profile=` |
| GET | `/company/{ticker}/scoring/peers?limit=` |
| GET | `/scoring/weights` |
| PUT | `/scoring/weights` |

---

## 8. Visualisation

| Chart | Purpose |
|---|---|
| **Radar** | All 13 categories, 0–10, with optional peer-median overlay |
| **Contribution** | Weighted contribution — what actually drives the composite |
| **Score history** | Composite over time with grade-band shading |
| **Confidence bar** | Segmented verified/estimated/analyst/missing |
| **Peer table** | Same-sector companies scored on identical assumptions |

---

## 9. Test results

```
708 passed in 8.4s
```

| File | Tests |
|---|---:|
| `test_scoring_engine.py` | **112** |
| `test_scoring_api.py` | **58** |
| *(Modules 1–4)* | 538 |

**Verified by independent recomputation:**

| Assertion | Result |
|---|---|
| Confidence is weight-adjusted, not counted | ✔ |
| Confidence recomputed by hand (0.4×1.0 + 0.3×0.65 + 0.3×0) | ✔ |
| Origin shares always sum to 1 | ✔ |
| Aggregate = weighted mean | ✔ |
| All 5 profiles normalise to 1.0 | ✔ |
| Relative weights are scale-invariant | ✔ |
| Value weights valuation above Growth | ✔ |
| All 13 scorers return valid categories | ✔ |
| **Every metric has an explanation** | ✔ |
| Quantitative categories ≥50% verified | ✔ |
| ESG/momentum report 100% missing when unsupplied | ✔ |
| Qualitative inputs raise both score and confidence | ✔ |
| Capital allocation flips above/below WACC | ✔ |
| **Moat capped without financial corroboration** | ✔ |
| Composite = weighted mean × 10 | ✔ |
| Grade boundaries at 85/75/65/55/45/35 | ✔ |
| Stars always half-steps | ✔ |
| Empty company does not crash | ✔ |
| Low confidence prevents a directional call | ✔ |

### Performance

| Operation | Time |
|---|---:|
| 13-category composite (engine only) | **0.66 ms** |
| `GET /scoring` (full HTTP) | 16.2 ms |
| `GET /scoring/explanation` | 16.7 ms |
| `GET /scoring/peers` (4 companies) | 62.1 ms |
| `GET /scoring/weights` | 3.4 ms |

---

## 10. Reuse across the platform

`ScoringService.score_company()` is the single entry point. Dashboard, AI Analyst, Portfolio,
Watchlist, Alerts, Research Report and any future agent all call it and receive the same object.
None re-derive a score; none can drift from another.

---

## 11. Defects found and fixed

**1. A flat series scored as deterioration.** `trend_score` computed consistency as "share of
periods that improved", so a perfectly stable metric scored 2.5/10 — the narrative read *"ROE has
deteriorated by 0 bps a year"*, which is nonsense. Worse, it penalised exactly the predictable
businesses the framework is meant to favour. Fixed with a flatness epsilon: a genuinely flat
series now scores neutral, and the narratives say "held steady".

**2. Highcharts `highcharts-more` broke SSR.** Radar charts need the polar module, but in
Highcharts 13 it self-registers at import and touches browser globals, producing
`Cannot read properties of undefined (reading 'SeriesRegistry')` and a 500 on the scoring page. I
first tried the v10 callable-factory style, which was also wrong. Fixed by loading it lazily on
mount — the chart wrapper already defers rendering, so nothing draws before it lands.

**3. My test asserted the wrong thing.** `test_capital_allocation_judges_reinvestment_conditionally`
set two WACC values expecting different branches, but the fixture's ROIC was below *both*, so both
correctly took the dilutive path. The engine was right; the test was constructed carelessly.
Rewritten to place the WACC either side of the fixture's actual ROIC and to assert on the
narrative wording.

---

## 12. Known limitations

1. **Qualitative inputs are mostly unpopulated.** Governance, moat and ESG depend on analyst
   judgement or document extraction, so on seeded data they report ~22% missing and confidence
   sits at 75%. This is the engine working correctly — it is honest about what it does not know —
   but scores will strengthen materially once Module 7 extracts these from filings.
2. **Momentum is structurally missing.** No price history is ingested yet, so all four metrics
   report `missing`. Built now because the framework must be complete and Module 8's alerting
   will need it.
3. **Peer comparison is same-sector only**, by market cap. A true peer set needs industry
   classification finer than the current sector field.
4. **History needs repeated runs.** Snapshots are keyed by date, so a trend line only appears
   after the score has been run on more than one day. Seeded data therefore shows a single point.
5. **Band thresholds are house defaults.** They are inspectable constants in each scorer rather
   than configuration; making them profile-editable is a reasonable future step, but it would add
   a large surface area for little immediate gain.
