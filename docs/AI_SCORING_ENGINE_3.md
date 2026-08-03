# AI Scoring Engine 3.0 — Explainable Institutional Scoring

**Framework version:** `3.0.0`
**Status:** deployed, 503 of 503 active companies scored
**Validation:** 22 of 22 production checks passed on a 60-company live sample
**Tests:** 2,618 passing, 0 failures (89 new)
**Deployed:** backend SHA `2a6144a`

---

## 1. The governing rule

> The AI must NEVER generate a rating from a single prompt or LLM opinion.

This is enforced structurally, not by convention.

Every number the engine produces is arithmetic over observed inputs. No module
scorer imports an AI provider, calls a completion endpoint, or reads a
sentiment. A language model may write prose *about* a finished score, and that
prose is carried in `ModuleScore.ai_commentary` — a field the arithmetic never
reads, on a frozen dataclass that is already fully populated by the time any
narrator could run.

Three tests hold the line:

| Test | What it proves |
|---|---|
| `test_no_module_scorer_calls_a_model` | Static scan of `app/services/ai_scoring/` finds no LLM import or call |
| `test_ai_commentary_is_not_read_by_the_arithmetic` | Attaching commentary to all ten modules leaves the composite bit-identical |
| `test_determinism` | Five runs over identical evidence produce one byte-identical payload |

The engine is fully deterministic. This was verified on live production data:
10 companies × 3 runs each produced exactly one distinct JSON payload per
company.

---

## 2. The framework

Ten modules, fixed weights summing to 100. The sum is asserted at import — a
framework summing to 97 would silently cap every company at 97 while looking
entirely reasonable.

| # | Module | Weight | Factors | Principal source |
|---|---|---:|---:|---|
| 1 | Company Data | 10 | 6 | Universe reference data, knowledge vault |
| 2 | Financial Statements | 15 | 11 | Canonical `financial_facts` |
| 3 | Latest News | 8 | 6 | `discovered_filings` (NSE / BSE / IR) |
| 4 | Industry Analysis | 8 | 5 | Sector aggregates, knowledge vault |
| 5 | Management Commentary | 10 | 7 | Documents, temporal memory |
| 6 | AI Analysis Engine | 10 | 6 | Vault assertions, summaries, observations |
| 7 | Business Quality Score | 14 | 6 | Statements (proxied) |
| 8 | Growth Score | 10 | 5 | Statements, disclosures |
| 9 | Risk Score | 8 | 5 | Balance sheet, announcements |
| 10 | Valuation Score | 7 | 6 | Valuation engine, sector medians |
| | **Total** | **100** | **63** | |

### Output scales

- **Overall score** 0–100, the sum of module contributions (`score/10 × weight`).
- **Rating** A+ / A / BBB / BB / B / C.
- **Recommendation** Strong Buy / Buy / Hold / Reduce / Avoid.

### Two inverted scales — stated because they are the likeliest misreading

**Risk: 10 means LOW risk. Valuation: 10 means CHEAP.** Both guardrails assume
this direction, and `TestInvertedScales` asserts it end to end: an unlevered
company must out-score a geared one on Risk, and a company on 9× earnings must
out-score one on 78×.

---

## 3. Explainability

The brief requires weight, score, reason, evidence and citations for every
score. All five are **mandatory constructor fields** on `FactorScore` — a
factor without a reason raises `ValueError` and cannot be built.

Measured across the 60-company production sample:

| Guarantee | Result |
|---|---|
| Factors evaluated | 3,780 |
| Factors without a reason | **0** |
| Scored (non-missing) factors without a citation | **0** |
| Citations issued | 3,298 (median 56 per company) |

Citations are resolvable, not decorative. Each carries a `kind` and a
`reference` that points at a real row — `documents:412`, `knowledge_entries:88`,
`discovered_filings:9021`, `financial_facts:<company>:REVENUE`.

### Missing is a state, not a zero

An unobservable factor scores the neutral midpoint (5.0/10) and contributes
zero to **coverage**. Scoring it 0 would punish a company for the platform's
ignorance; scoring it 10 would flatter it. Coverage is what tells the reader
which case they are in, and it is weighted by framework weight — a gap in the
15-point Financial Statements module hurts more than one in 7-point Valuation.

---

## 4. Future probability

Five probabilities, each an explicit logistic over a named set of module scores
with published coefficients. `GET /ai-score/framework` returns the coefficients,
so any figure can be reconstructed by hand.

| Probability | Heaviest driver |
|---|---|
| Outperforming the Nifty | Business Quality 0.24, Valuation 0.22 |
| Earnings Growth | Growth 0.30, Financial Statements 0.24 |
| Revenue Growth | Growth 0.32, Industry 0.24 |
| Multiple Expansion | Valuation 0.36 |
| Overall Investment | Business Quality 0.20, Financial Statements 0.18 |

Three properties are deliberate:

1. **Bounded to [0.02, 0.98].** Certainty is not available from this evidence.
2. **Shrinkage toward 50% in proportion to missing coverage**, reported on the
   result rather than hidden. A 78% on complete filings and a 78% on two
   reference fields are different claims.
3. **A missing module is dropped from the normaliser, never read as zero.**
   Reading absence as zero would score an unobserved module as catastrophic —
   the most common way an evidence gap becomes a confident negative claim.
   `test_missing_module_is_dropped_not_read_as_zero` guards this.

---

## 5. Guardrails

A weighted mean is not a recommendation. Four guardrails cap the call, and
**every one that fires is reported even when a tighter one supersedes it** —
three problems is materially different from one.

| Guardrail | Fires when | Caps at |
|---|---|---|
| Fragile balance sheet | Risk ≤ 3.0/10 | Reduce |
| Expensive | Valuation ≤ 3.0/10 | Hold |
| Weak financials | Financial Statements ≤ 3.0/10 | Hold |
| Thin evidence | Coverage < 45% | Hold |

Guardrails may only cap, never upgrade (`test_guardrails_never_upgrade`).

On the live sample, 23 of 60 companies were capped by the valuation guardrail —
which is what one would expect of the Indian market at current multiples, and
is the guardrail doing its job rather than a defect.

---

## 6. Learning — versioned, append-only history

> Whenever new filings arrive, automatically recalculate every module.
> Historical scores must never be overwritten. Store every score version permanently.

### Never overwritten

`ai_score_versions` has **no update path**. A recalculation inserts at
`version + 1` and marks the previous row `superseded`. A `UNIQUE (company_id,
version)` constraint makes an accidental overwrite a database error rather than
a silent loss — `test_duplicate_version_is_a_database_error` triggers it
deliberately.

Version numbers come from `MAX + 1`, not `COUNT + 1`: a gap is better than a
collision if a row is ever removed.

Each version stores the **complete explainable payload** — every factor, reason
and citation as it stood — so a version written months ago reads back exactly as
written. `GET /company/{ticker}/ai-score/version/{n}` returns the stored JSON
verbatim rather than re-rendering it through today's schema, which would
silently reshape history.

### The no-op case

An unchanged **input fingerprint** (SHA256 over the observed inputs) under the
same framework version writes nothing. The same evidence producing the same
score is not a new version — it is the same version observed twice, and
recording it would bury real changes under identical rows. Verified in
production: a second identical pass over 5 companies wrote 0 rows.

### Two triggers

| Trigger | Path | Latency |
|---|---|---|
| **Filing arrives** | `PostFilingProcessor` recalculates inline after ingestion | minutes |
| **Scheduled** | `AI_SCORE_REFRESH` job, daily, oldest-scored-first | 24h ceiling |

The scheduled sweep exists because several modules read time-sensitive
evidence: the twelve-month news window rolls forward daily, so an announcement
ageing out of it changes the score with no new data arriving. It is ordered
oldest-first so a truncated run makes progress through the universe rather than
rescoring the same head of the list — the defect that once left 340 of 501
companies never crawled.

`AI_SCORE_REFRESH` is registered in **all five** registries (`JobKind`,
`JOB_LABELS`, `DEFAULT_PRIORITY`, `RETRY_POLICIES`, `HANDLERS`) plus
`SCHEDULES`. This is the JOB-001 defect class, and
`test_every_job_kind_is_fully_registered` now guards the whole enum.

---

## 7. API

| Endpoint | Purpose |
|---|---|
| `GET /ai-score/framework` | Weights, bands, guardrails, probability coefficients |
| `GET /ai-score/dashboard` | Universe summary, leaderboards |
| `GET /company/{ticker}/ai-score` | Fresh score, **read-only** |
| `POST /company/{ticker}/ai-score/recalculate` | Score and append a version |
| `GET /company/{ticker}/ai-score/history` | Every retained version |
| `GET /company/{ticker}/ai-score/version/{n}` | One version, verbatim |

The GET deliberately does **not** write a version: a read that mutates the
permanent record would mean looking at a company changed its history, and a
dashboard polling fifty companies would manufacture fifty versions an hour.

`/ai-score/history` returns `spans_framework_versions`, because a trend line
drawn across two framework versions compares two different questions and the
chart cannot tell on its own.

---

## 8. Production results

**Universe: 503 active companies, all scored, 0 failures. 247s for a full sweep
(~490ms per company end to end; ~5ms of that is scoring arithmetic, the rest is
evidence retrieval).**

| Metric | Value |
|---|---|
| Average score | 60.03 |
| Average coverage | 57.5% |
| Ratings | BBB 164 · BB 315 · B 24 |
| Recommendations | Buy 14 · Hold 465 · Reduce 24 |
| Versions retained | 503 |

Highest: BAJFINANCE 71.5, TCS 71.3, PFC 71.0, COALINDIA 70.4, RECLTD 70.0.
Lowest: ABREL 41.1, SAMMAANCAP 44.1, INDIACEM 44.9, RPOWER 46.3, ABFRL 46.4.

### Per-module coverage — where the evidence actually is

| Module | Wt | Mean score | Coverage |
|---|---:|---:|---:|
| Financial Statements | 15 | 6.95 | **97.3%** |
| Latest News | 8 | 6.50 | 90.0% |
| Business Quality | 14 | 6.10 | 80.1% |
| Valuation | 7 | 3.90 | 69.1% |
| Risk | 8 | 6.33 | 60.5% |
| Growth | 10 | 6.98 | 60.0% |
| Company Data | 10 | 6.22 | 58.3% |
| Industry Analysis | 8 | 5.26 | 32.8% |
| AI Analysis Engine | 10 | 5.11 | **4.3%** |
| Management Commentary | 10 | 5.01 | **0.4%** |

This table is the honest headline of the phase. The quantitative half of the
framework is near-complete: Financial Statements at 97.3% coverage reflects the
100% financial ingestion achieved earlier. The document-dependent half is
close to empty — Management Commentary at 0.4% and AI Analysis at 4.3% mean
**20 of the 100 available points are currently scored almost entirely on
neutral defaults.**

That is a data-coverage limitation, not an engine defect, and the engine
reports it rather than concealing it: those companies carry an explicit
"effectively unassessed modules" warning naming the point total at stake. It
also explains the compressed distribution (σ 5.3, 315 of 503 rated BB) — two
modules contributing a flat 5.0/10 to every company pull the whole universe
toward the middle. **Scores will spread as documents land**, and because
history is append-only that movement will be visible rather than retroactive.

---

## 9. Defects found and fixed

### SCORE-3.0-001 — a company with no evidence was rated "Reduce"

**Found by:** `test_an_empty_company_still_scores_with_gaps_reported`.

A company for which nothing is observable scores every factor at the neutral
midpoint of 5.0/10, which composites to exactly 50.0. The Hold band began at
52.0, so 50.0 fell through to **Reduce** — a directional sell call derived from
having no evidence whatsoever, produced by arithmetic rather than by any
observation about the business.

The thin-evidence guardrail did not save it: a guardrail may only *cap* a
recommendation, never raise one.

**Fix:** the Hold floor is pinned to 50.0, which is the "we know nothing" point
by construction. A score below 50 now means observed weakness rather than
absent data. An import-time assertion prevents the band from ever drifting back:

```python
assert recommendation_for(NEUTRAL_COMPOSITE) is Recommendation.HOLD
```

### SCORE-3.0-002 — a scored zero cited nothing

**Found by:** the production validation harness, on live data — ASTRAL and
BAJAJ-AUTO scored `latest_news.negative` and `latest_news.orders` as REPORTED
with **zero citations**.

"Eighteen disclosures were read and none was adverse" is a genuine finding.
"No disclosures were read" is a gap. Both rendered identically in the panel,
so a zero-evidence clean bill of health was indistinguishable from a verified
one.

**Fix:** a zero-count factor now cites the three most recent announcements it
scanned, and states the window size in its evidence line. Both the unit suite
and the harness now assert that no scored factor anywhere lacks a citation.

### CLASSIFY-001 — a resignation was not a management announcement

**Found by:** `test_categories[Resignation of Chief Financial Officer]`.

`\bresignation\b` appeared only under "negative". A company whose only
management disclosures were departures therefore scored zero on management
communication — reading an absence of announcements where there were several.

**Fix:** resignation patterns added to "management" as well. `classify` returns
a *set* precisely so an event can be two things at once; a CFO resignation is
both adverse and a management announcement.

### AISCORE-001 — `/ai-score/history` returned HTTP 500 in production

**Found by:** hitting the deployed endpoint after the first deploy. Every unit
test passed; the endpoint was broken on every single call.

I built `CompanyRef` field by field from a *guess* at its schema — omitting
`exchange`, which is required, and passing `industry`, which the model does not
declare. Pydantic raised on construction, so the endpoint returned 500
unconditionally.

The service-layer tests could not catch this because they never built a
response model. Only an actual HTTP request does, and I had not written one.

**Fix:** `CompanyRef.model_validate(company)` — the model reads whatever it
actually declares, so the endpoint cannot drift from its schema again. Eight
`TestAPIContract` tests now exercise every endpoint through a real
`TestClient` and assert the status code. Confirmed by reverting the fix: the
new test reproduces the 500 exactly, and passes once restored.

**Two harness bugs of my own along the way, both mine and not the product's:**
the first fixture used the in-memory `db` fixture, which cannot cross the
thread `TestClient` runs the app on; the second put `import app.models.x`
above `from app.main import app`, binding `app` to the *package* and turning
`app.dependency_overrides` into an `AttributeError`; the third called
`dependency_overrides.clear()` in teardown, which removed the session-wide
`get_db` override `conftest.py` installs at import — producing **130 failures
and 161 errors** across `test_valuation_api`, `test_document_api`,
`test_scoring_api` and `test_report_api`, none of which had anything wrong with
them. A fixture that tears down more than it set up is a harness bug that looks
exactly like a product regression. Teardown now saves and restores.

---

## 10. Production incident — an orphaned migration blocking all schema changes

Production `alembic_version` read `a2c8e5f91b47` — **a revision that exists in
no commit on `main` and in no file in the repository.** It had created an
`ai_score_versions` table with a different schema
(`engine_version` / `explainability` rather than `framework_version` / `detail`)
holding 2 rows for a single company, written 2026-08-02 23:50 and 23:52 UTC.
It appears to be an abandoned earlier attempt at this phase whose code was never
committed.

The consequence was more serious than the stray table: **`alembic upgrade head`
failed outright, so no migration of any kind could be applied to production.**

**Resolution** (option chosen by the user — preserve, do not delete):

1. Both rows exported to `deploy/orphaned_ai_score_versions_backup.json`.
2. Table renamed to `ai_score_versions_orphaned_20260802`. Its constraints and
   indexes were renamed too — in Postgres these travel with the table, and the
   new table could not have created its own `uq_ai_score_version` while the old
   one held the name.
3. `alembic_version` reset to `f7b2d94e15c8`, the last revision actually present
   in the repository.
4. `a2c8e5d91f47` applied cleanly.

Nothing was deleted. The orphaned table remains queryable in production until
you choose to drop it.

---

## 11. Design decisions worth stating

**Each calculation exists once.** Management credibility is read from
`TemporalMemoryService.credibility` rather than recomputed — that verdict was
reached with the evidence available at the time, and recomputing it now with
hindsight would quietly rewrite the track record it measures. Valuation
multiples come from the existing valuation engine, which also propagates the
Module 4 illustrative-data disclosure verbatim.

**Sector statistics are computed once per sector, not once per company.** Three
modules need them; a 500-company sweep would otherwise issue 1,500 sector-wide
aggregate queries to produce 22 distinct answers. Measured: 22 sectors cached
across the full universe sweep.

**Growth reads history, never the forecast.** Scoring a company on its own
projected growth makes the score a function of the analyst's optimism rather
than of the company.

**Capacity expansion is read from disclosures, never inferred from gross
block.** Maintenance capex and a new production line are indistinguishable in
the aggregate; the engine reports capex intensity as context and marks the
factor missing.

**A CAGR from a non-positive base returns `None`.** The usual workaround —
taking the absolute value — produces a growth rate with the wrong sign for a
company recovering from a loss, precisely the case where the number would be
read most eagerly.

**Fallback summaries are excluded from every evidence count.** Template prose
the platform wrote to itself when no model was reachable is not analysis, and
crediting it would let a score rise because the platform talked to itself.

---

## 12. Caveats and known limitations

- **Management Commentary coverage is 0.4% and AI Analysis is 4.3%.** Twenty of
  the hundred available points are currently scored on neutral defaults for
  most companies. The engine flags this per company, but the composite is
  compressed toward the middle as a result, and inter-company discrimination is
  weaker than the framework is capable of. This resolves as documents land.
- **Only ~54 of 503 companies hold documents at all**, and 439 have no indexed
  text. The document-dependent modules cannot improve until that changes.
- **Sector medians use an average-based approximation, not `percentile_cont`.**
  SQLite — which the test suite runs on — has no percentile function, and a
  portable approximation both engines agree on was preferred to an exact figure
  that only works in production. Adequate for a sector aggregate; not a
  precision instrument.
- **News classification is keyword-driven.** It is deliberately conservative and
  word-boundary anchored, but it will miss unusually-phrased subject lines and
  cannot read the *direction* of a regulatory event — only that one occurred.
- **No cross-company calibration.** Scores are absolute against fixed bands, not
  ranked within the universe. A sector where every constituent is expensive will
  see every constituent capped by the valuation guardrail, which is arguably
  correct but is not a relative-value tool.
- **Market cap bands convert USD at a fixed 83 INR/USD.** A band boundary is not
  a valuation, so this is adequate, but it is a hardcoded constant.
- **`market_cap_rank` is computed against companies with a recorded market cap
  only**, so a sector where half the constituents lack the field produces a
  flattering rank.
- **The orphaned production table remains in the database** under
  `ai_score_versions_orphaned_20260802`, per your instruction to preserve it.
- **The repository is still public.**
