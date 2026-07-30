# Module 8 — Portfolio Intelligence Platform

**Status:** complete, awaiting review
**Tests:** 1,277 passing (148 new in this module)
**API:** 80 paths total · 21 portfolio paths · 168 schemas
**Database:** 31 tables (10 new)

---

## 1. The organising decision

**A position is derived, never stored.** There is no `positions` table. Quantity,
average cost and realised P&L are replayed from the transaction ledger on every
read.

That costs a replay per request — measured at 250,000 transactions per second,
so well under a millisecond for any realistic book — and buys the guarantee
that a back-dated or corrected transaction is reflected everywhere at once,
with no repair job and no possibility of a stored position disagreeing with the
ledger that produced it. A test asserts the table does not exist.

The second decision is that **money is rupees, weights and rates are
fractions**, declared once at the API boundary and never varied. Module 6's
percentage bug came from two layers disagreeing about that, and it cost a day.

---

## 2. Architecture

```
                    ┌──────────────────────────────┐
  HTTP  ──────────► │  api/v1/portfolio.py         │  serialises only
                    └───────────────┬──────────────┘
                    ┌───────────────▼──────────────┐
                    │  PortfolioService            │  the only DB-aware layer
                    │  CRUD · ledger · cache       │  content-keyed LRU
                    └───────────────┬──────────────┘
                    ┌───────────────▼──────────────┐
                    │  PortfolioEngine.build()     │  ONE resolution per request
                    └───────────────┬──────────────┘
        ┌───────────────┬───────────┼───────────┬────────────────┐
        ▼               ▼           ▼           ▼                ▼
  PositionEngine   allocation    risk     performance      AlertEngine
  (ledger replay)  (5 cuts)  (12 metrics) (TWR/MWR/Brinson) (26 rules)
                                          │
                                          ▼
                                  CommentaryEngine
                              (grounded, cited, offline)
```

Layer boundaries are enforced by test:

| Layer | May import | Must not import |
|---|---|---|
| `app/domain/portfolio/` | dataclasses, enums, `domain.calc` | SQLAlchemy, FastAPI, httpx, `app.models`, `app.api` |
| `app/services/portfolio/engine.py` | domain only | the ORM |
| `app/services/portfolio/service.py` | engines **and** models | HTTP shapes |
| `app/api/v1/portfolio.py` | services and schemas | how anything is computed |

### The single-resolution rule

`PortfolioService.view()` resolves a portfolio exactly once per request into a
`PortfolioView`, and *every* endpoint reads from that object. Assembling
positions in one endpoint and re-assembling them in another is how two screens
end up disagreeing about the same portfolio's weight in a holding. This is the
same constraint that has governed the platform since Module 2.

---

## 3. Folder structure

```
backend/app/
├── domain/portfolio/
│   ├── types.py         Enums, Position, CashLedger, Allocation, AlertEvaluation
│   ├── positions.py     Ledger replay: FIFO/average, bonus, split, rights
│   ├── allocation.py    Sector · industry · market cap · country · style
│   ├── risk.py          Vol, Sharpe, Sortino, drawdown, VaR, CVaR, beta, alpha
│   ├── performance.py   TWR, MWR/XIRR, Brinson-Fachler, contribution, rolling
│   └── alerts.py        26 rules as data, plus rating-driven position limits
├── services/portfolio/
│   ├── engine.py        PortfolioView — one pass, everything consistent
│   ├── service.py       Persistence, ledger validation, content-keyed cache
│   └── commentary.py    Grounded AI commentary (Module 6 architecture)
├── models/portfolio.py  10 tables
├── schemas/portfolio.py Typed contracts
├── api/v1/portfolio.py  21 endpoints
└── db/seed_portfolio.py Price history, benchmark, demo book, snapshots

backend/tests/
├── test_portfolio_engine.py   95 tests — pure engines
└── test_portfolio_api.py      53 tests — HTTP integration
```

---

## 4. Database

Ten tables. What is stored is precisely what **cannot be derived**.

| Table | Why it must exist |
|---|---|
| `portfolios` | Identity and policy (position caps, margin of safety, benchmark) |
| `portfolio_transactions` | The only source of truth about any holding |
| `portfolio_snapshots` | A past valuation cannot be reconstructed — the platform holds only today's prices. Without snapshots there is no return series, and so no volatility, Sharpe or drawdown |
| `benchmark_levels` | The comparison series for beta and alpha |
| `price_history` | Module 5 recorded "momentum is structurally missing — no price history ingested". This is that table |
| `allocation_targets` | User intent, which is not derivable |
| `watchlists` / `watchlist_entries` | Candidates and their buy-below prices |
| `alert_rule_overrides` | Only *departures* from the built-in rules. Storing every rule as a row would make the shipped defaults mutable and unversionable |
| `alert_events` | Fired alerts, so the same trigger is not re-notified daily |

`portfolio_transactions` carries a `sequence` column. Two trades on one date
relieve cost differently depending on order, and a database's natural row order
is not a guarantee.

---

## 5. Portfolio engine

### Transaction replay

Eleven transaction types. Corporate actions are transactions rather than a
separate concept, because a bonus issue changes quantity exactly as a buy does
and must replay in the same chronological order.

| Behaviour | Rule | Verified |
|---|---|---|
| Fees on a buy | Capitalise into cost | A round trip at an unchanged price shows the loss it is |
| Fees on a sell | Reduce proceeds | Realised gain is net of the cost of realising it |
| Bonus 1:2 | Quantity ×1.5, **total cost unchanged** | 200 @ ₹2,400 → 300 @ ₹1,600.80, cost ₹480,240 |
| Split 1:2 | Quantity ×2, cost per unit ÷2 | 150 @ ₹1,500 → 300 @ ₹750 |
| Dividends | **Income, not cost relief** | The workbook nets them against cost, which overstates return on a high-yield holding |
| Oversell | `InsufficientHolding` | A negative position would misstate every weight in the book |
| Long term | > 365 days | Indian listed-equity convention |

Cost relief supports FIFO (the Indian tax convention, and the default) and
weighted average (what most broker statements quote). A test proves they give
different answers on the same ledger — ₹200,000 against ₹150,000 — because a
cost-basis setting that changes nothing is not a setting.

### Validation on write

`add_transaction` inserts, **then replays the whole ledger**, and rolls back if
the replay fails. A sell that exceeds the holding *at that point in history* is
only detectable by replaying. `delete_transaction` does the same: removing a
buy that orphans a later sell is refused rather than leaving a ledger that
cannot be replayed.

---

## 6. Allocation — five dimensions

Sector, industry, market cap, country and style. The first four are lookups.

**Style is computed, never hand-tagged.** A holding is "value" because its
Module 5 valuation score is strong and its growth score is not. Letting a user
tag a position as growth would make style allocation an opinion survey rather
than a measurement. Where two styles clear the threshold within five points of
each other the holding is BLEND — calling a genuinely balanced position
"growth" on a two-point margin is noise presented as signal.

**Unclassified value is reported, not dropped.** A portfolio with an
unclassified holding would otherwise show its remaining weights summing above
one.

---

## 7. Risk — twelve metrics

Every function returns a scalar or `None`. **`None` means the input could not
support the statistic, and never zero.** A Sortino ratio of zero and an
undefined Sortino ratio are opposite statements.

| Metric | Decision made, and why |
|---|---|
| Annualised return | **Geometric.** +50% then −50% has lost a quarter, not broken even |
| Volatility | Sample (n−1). A return series is a sample; the population form understates dispersion on short histories |
| Sharpe | Excess over total volatility |
| Sortino | Downside deviation divides by **all** observations, not the downside count. Otherwise one bad day in a thousand looks as risky as a hundred. The target is the *periodic* risk-free rate, so numerator and denominator share one hurdle |
| Max drawdown | Computed on the value series, not reconstructed from returns, so it is exact rather than accumulating drift |
| VaR / CVaR | **Historical, not parametric.** Equity returns have fatter tails than a normal distribution, and a Gaussian VaR understates exactly the losses the measure exists to warn about |
| Beta / alpha | Regression beta; Jensen's alpha |
| Tracking error, IR | Annualised active-return dispersion |
| Up/down capture | Share of the benchmark's moves captured |
| Effective positions | 1/HHI. Ten positions with one at 90% is not ten positions — this reports 1.2 |
| Diversification score | The workbook's `100 × (1−HHI) × min(1, names/target)`. Two penalties: weight concentration, and simply not holding enough names |
| Liquidity days | Position value ÷ (ADV × 20% participation). Trading more than a fifth of a day's volume moves the price against you |

`RiskProfile.unavailable` carries a plain-English reason for every statistic
that could not be computed, and the UI renders them. A blank cell the user
cannot account for is worse than no cell.

---

## 8. Performance — two returns, and the difference matters

| Measure | Answers | Neutralises |
|---|---|---|
| **Time-weighted (TWR)** | How did the *manager* do? | Deposits and withdrawals |
| **Money-weighted (MWR/XIRR)** | How did the *investor's rupees* do? | Nothing — timing counts |

A portfolio that doubled and then took a large deposit just before a fall has a
good TWR and a poor MWR. Both are true. Reporting only one — and most retail
tools report whichever flatters — is the error. A test asserts they diverge in
opposite directions on exactly that scenario.

**Verified against the demo book:** the engine reports TWR 13.6506%; an
independent chain-linked Modified Dietz calculation over the same 188 snapshots
gives 13.6506%.

Two further guards:
- **Sub-year periods are not annualised.** Turning a 4% gain over three weeks
  into 96% is arithmetically valid and analytically worthless.
- **XIRR uses bisection, not Newton-Raphson.** Newton is faster and diverges on
  the sign-alternating flow series a real portfolio produces; a silently
  divergent IRR returns a plausible wrong number.

### Attribution

Brinson-**Fachler**, decomposing active return into allocation, selection and
interaction. The Fachler refinement — subtracting the total benchmark return
from each segment's — is what stops an overweight in *any* rising segment from
scoring positively even when it lagged the index. Interaction is reported
separately rather than folded into selection, because merging them flatters a
manager who was overweight *and* right.

The decomposition is exact: **residual ~1e-18** in the engine test.

---

## 9. Alert engine — 26 rules

The workbook's `38 Alerts` rows 9–22 are transcribed with thresholds,
comparators, actions and priorities intact, plus seven portfolio-scoped and four
event rules covering the brief's remaining categories.

**A rule is data, not code.** Each names the metric it reads, the comparator,
the threshold and the severity; the engine resolves and compares. Adding a rule
is a row, not a branch, which is what makes user-defined alerts possible without
a deployment.

### Position sizing follows quality

From `30 Institutional Scorecard` H27:H33 — this is a real finding from the
workbook, not an invention:

| Grade | AAA | AA | A | BBB | BB | B | C |
|---|---|---|---|---|---|---|---|
| Max position | 8% | 6% | 4% | 2.5% | 1% | 0% | 0% |

The effective cap is the tighter of house policy and the rating-implied limit.
An unrated name gets 4% — a real limit, so it cannot quietly become the largest
position in the book.

### The one deliberate divergence from the workbook

**A rule whose input is missing evaluates to `UNAVAILABLE`, not `clear`.** The
workbook's `IF(F<E, …)` reads a blank cell as zero and prints "✓ clear" for a
company whose data was never loaded. Silence about a risk is not evidence of its
absence. Every unavailable evaluation carries the name of the metric that was
missing, and the UI shows them in their own section.

---

## 10. AI commentary

Module 6's architecture, applied to a portfolio: **the platform generates the
numbers, the model explains them.** Every figure in every sentence comes from a
`PortfolioView` computed before any prose was written, so the commentary is
structurally incapable of inventing a weight or a return.

Five sections, exactly as the brief specifies: portfolio health, top risks, top
opportunities, rebalancing suggestions, position commentary. Twenty-nine
citations for the demo book, each rendered as an inline chip resolving to the
evidence panel.

A test extracts every `[marker]` from the generated prose and asserts it
resolves to a real citation key — the same guard as Module 6's citation auditor.

---

## 11. Performance benchmarks

Measured on this sandbox, single process.

### Position replay

| Transactions | Positions | Time | Throughput |
|---|---|---|---|
| 100 | 100 | 0.6 ms | 180k txn/s |
| 1,000 | 200 | 4.0 ms | 253k txn/s |
| 10,000 | 200 | 37.9 ms | 264k txn/s |
| 50,000 | 200 | 246.9 ms | 203k txn/s |

Linear. A twenty-year ledger replays in well under a second.

### Risk metrics

| Observations | Full profile |
|---|---|
| 250 | 0.71 ms |
| 1,000 | 2.30 ms |
| 5,000 | 11.77 ms |
| 20,000 | 48.20 ms |

### End to end

| Operation | Time |
|---|---|
| Seed (19,677 price rows, 937 benchmark levels, 188 snapshots) | 1,842 ms |
| Portfolio view, cold | 201 ms |
| Portfolio view, warm (p50) | **0.735 ms** |
| Portfolio view, warm (p95) | 0.820 ms |
| Alert evaluation, 205 rules × holdings | 9.1 ms |
| AI commentary, 5 sections + 29 citations | 1.4 ms |

**Caching gives a 270× speedup**, with a 98% hit rate under repeated reads.

The 201 ms cold cost is dominated by Modules 4 and 5 running per holding — a
full DCF and a 13-category score for each of eleven names. That is the honest
cost of grounding portfolio alerts in real valuation rather than a stored
number, and it is why the cache exists.

### Cache invalidation is by content, not clock

The key is a hash of `(transaction count, max transaction id, max updated_at,
snapshot count)`. A new trade invalidates immediately; a quiet portfolio is
never recomputed. A TTL alone would serve a stale book for its duration; a
version counter alone would miss a deletion.

---

## 12. Defects found and fixed

Eight. The two marked ⚠ are the dangerous kind — plausible, silent, and wrong.

| # | Defect | Root cause | Fix |
|---|---|---|---|
| 1 ⚠ | Every holding tripped "Price above target": ₹2,945 price against a ₹16.79 "fair value" | Module 4 grades its own output and had marked these valuations `unreliable`; Module 8 used them anyway | Respect the existing gate — a valuation graded unreliable drives no alert and no position cap |
| 2 | Scores, ratings and risk all `None`; 138 of 151 alerts unavailable | `_scores` guessed the ScoringService API and a bare `except` swallowed the `AttributeError` | Call the real `score_company(analysis, forecast, valuation)`; record every failure in `analytics_errors` and surface it in the API |
| 3 ⚠ | TWR read **−11.9%** on a book that had gained ~20% | Price history was a fixed 760-day lookback while inception was ~1,300 days ago, so the series began 18 months *after* the first purchase | Anchor the window to `DEMO_INCEPTION` |
| 4 | Snapshots silently dropped the first 18 months | `[::every]` applied to an unfiltered date list | Slice from inception and always retain the final observation |
| 5 | `AttributeError` on every holding | `ShareholdingSnapshot` is keyed by `fiscal_year`/`quarter`, not `as_of` | Order by the real columns. *Found only because defect 2's fix made failures visible* |
| 6 | `Invalid isoformat string: ''` on an empty portfolio | `coalesce(max(updated_at), "")` — SQLAlchemy tried to parse `""` as a datetime | Drop the coalesce; NULL is a fine cache-key component |
| 7 | Risk category score compared as 0–1 against a 0–10 value | `CategoryScore.raw_score` is 0–10; the workbook's risk rule expects a fraction | Normalise in exactly one place, with the reason recorded |
| 8 | Rolling-returns axis rendered every gridline as "₹1" | Reused the money formatter for a chart of ratios | `format="ratio"` prop |

### Two cases where my test was wrong, not the product

Reported as found:

1. **`test_herfindahl_and_effective_count`** built four positions with no
   sector, then asserted HHI 0.25. Four unclassified holdings genuinely *are*
   one bucket, so HHI 1.0 was correct. The test now supplies distinct sectors,
   and a second test pins the collapsing behaviour deliberately.
2. **Attribution tolerance** was set to 1e-9 over the API. The Brinson maths is
   exact to ~1e-18 (proved in the engine test); the API rounds weights to six
   decimals for transport, so the achievable tolerance is ~1e-7. The assertion
   was testing the transport rounding, not the mathematics.

---

## 13. Verification summary

| Check | Result |
|---|---|
| Full suite | **1,277 passed** (1,129 pre-existing + 148 new) |
| Modules 1–7 regression | unchanged, all passing |
| `tsc --noEmit` | clean |
| `next build` | clean, 14 routes |
| OpenAPI | 80 paths · 21 portfolio · 168 schemas |
| Database | 31 tables |
| TWR vs independent Modified Dietz | 13.6506% vs **13.6506%** |
| Cash balance vs hand calculation | ₹135,798 vs **₹135,798** |
| Realised P&L vs hand calculation | ₹71,667 vs **₹71,667** |
| Bonus 1:2 on 200 @ ₹2,400 | 300 shares @ ₹1,600.80, cost unchanged |
| Brinson residual | ~1e-18 |
| CVaR ≤ VaR, VaR₉₉ ≤ VaR₉₅ | both hold |
| Beta of a series against itself | 1.0 |

### The demo book

11 positions · ₹76.05L total value · ₹8.55L total P&L · TWR 13.7% · Sharpe 1.56
· Sortino 3.56 · max drawdown −9.2% · beta 0.13 · 8.7 effective positions ·
diversification 65 · 27 alerts triggered, 58 clear, 120 not evaluated.

---

## 14. Known limitations

1. **Prices and benchmark levels are synthetic.** A seeded geometric random
   walk, calibrated so each series ends exactly at the `current_price` already
   on the company row. Every risk statistic is therefore illustrative. The
   walk's seed happens to give the benchmark a negative drift, which makes the
   portfolio's alpha look better than any real book should expect.

2. **120 of 205 alerts read "not evaluated" on the demo book.** Correct
   behaviour, not a gap in the engine: red-flag points, terminal-value share,
   model-integrity failures and document-derived metrics need Module 7 documents
   or Module 9 report runs that this book does not have. They are shown with
   their reason rather than as false clears.

3. **Attribution has no external benchmark.** With no index constituent data the
   benchmark is modelled as an equal weight across the sectors the portfolio
   holds, each earning the portfolio's average return. That makes *allocation*
   the meaningful term and selection near zero by construction. Stated on the
   screen rather than presented as a market comparison it is not.

4. **The queue is synchronous.** Snapshot recording and alert evaluation run
   in-request. Both are fast enough (9 ms for a full alert sweep) that a worker
   would be premature, but a nightly snapshot job is genuinely needed in
   production and is not deployed here.

5. **No short positions.** A sell beyond the holding raises rather than opening a
   short. Modelling shorts properly means borrow cost, margin and a different
   cost-basis treatment.

6. **Market-cap bands use absolute thresholds.** SEBI's classification is
   rank-based (top 100 large, next 150 mid), which needs the whole listed
   universe. The ₹ crore thresholds are the conventional approximation and are
   declared as overridable constants.

7. **Liquidity uses seeded traded value.** The 20% participation assumption is
   conventional but the volumes behind it are synthetic.

8. **Currency is INR only.** `base_currency` exists on the model; no FX
   translation is implemented.

---

## 15. What this unblocks

- **Module 9 (reports)** can render a portfolio review from `PortfolioView` and
  `CommentaryEngine` without recomputing anything.
- **Module 5 scoring** now has `price_history`, which is what the momentum
  category was missing.
- **Module 10 (admin)** has `alert_events` and the usage counters for
  operational dashboards.

Wiring momentum into Module 5 is deliberately *not* done here: it changes scores
the platform already publishes, and that deserves its own before-and-after
verification rather than being folded into this module.
