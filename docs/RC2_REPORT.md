# Release Candidate 2 — Production Readiness Report

**Date:** 30 July 2026 · **Version:** 1.0.0-rc2 · **Predecessor:** rc1 (synthetic data)

The single change that defines rc2: **the platform now runs on real reported
financials for 135 NSE-listed companies**, and every engine has been graded
against those filings rather than against a generator that was written to
satisfy it.

That change found **nine defects**, seven of them critical, none of which
1,850 passing tests had detected. They are listed in full in §2, because how
they were found is more useful than the fact that they were fixed.

---

## 1. Verdict

### **GO for public beta — conditional**

The platform is fit for a public beta with an explicit data-provenance
disclosure and the four conditions in §9. It is **not** yet fit for
investment-grade use by a paying institution, and the product already says so:
Module 4's data-quality gate refuses to publish a valuation it cannot stand
behind, and on this dataset it exercises that refusal.

**What is now proven**

| | |
|---|---|
| Real companies ingested | **135** across 28 sectors, 2006–2026 |
| Canonical facts | **42,025** from reported statements |
| Validation checks | **2,279 / 2,279 passed (100%)** |
| Companies fully clean | **135 / 135** |
| E2E HTTP calls | **1,620 / 1,620 (100%)**, p95 18 ms |
| AI responses audited | **1,755** · 0% hallucination · 100% citation coverage |
| Adversarial prompts | **160 / 160 correctly refused** |
| Automated tests | **1,903 passing** |
| Known CVEs | **0** (was 18) |
| Bandit HIGH findings | **0** (was 6) |

**What is not**

Data comes from two aggregators, not from filings. Coverage of the canonical
54 line items is 49.5%. Both are stated plainly in §6 and §8.

---

## 2. Defects

### 2.1 Critical

---

#### RC2-001 · Every ingested fact was unreadable
**Severity:** Critical · **Status:** Fixed · **Found by:** first validation run

`FinancialFact.precedence` is an **integer** enum (`OVERRIDE=1, STORE=2,
ALIAS=3, SAMPLE_DEFAULT=4`). The ingestion wrote `precedence="import"`.
SQLAlchemy accepted the string without complaint; the read path did not.

```
ValueError: 'import' is not a valid Precedence
```

Every one of 42,025 facts across all 135 companies was unreadable —
`AnalysisService.for_ticker` raised for every ticker. The platform had a full
real dataset and could not open any of it.

**Fix:** `precedence=int(Precedence.STORE)` in all three writers; 42,025 rows
repaired in place. **Guard:** `TestPrecedence` asserts no `precedence="` string
literal survives in `app/data/`.

---

#### RC2-002 · Four companies wrongly reported as having no financials
**Severity:** Critical · **Status:** Fixed

The consolidated-statement fallback triggered on HTTP 404. But a company that
files no consolidated accounts — a standalone insurer, a single-entity
manufacturer — is served the consolidated page with **HTTP 200 and an empty
table**. PAGEIND, SBILIFE, BDL and ICICIGI were all recorded as "no profit &
loss table" when each files a complete standalone set.

**Fix:** fall back on emptiness, not on status code. All four now ingest 12
years, with an honest `standalone statements` warning attached.

---

#### RC2-003 · Bank net profit inverted — a +₹79,219 cr profit read as −₹268,944 cr
**Severity:** Critical · **Status:** Fixed

Screener renders banks, NBFCs and insurers on a **financing** layout and
everyone else on an **operating** layout:

```
operating   Sales   − Expenses = Operating Profit,  Interest deducted after
financing   Revenue − Expenses = Financing Profit,  Interest ALREADY in Expenses
```

The ingestion applied the operating mapping to everything. For a bank that
leaves revenue null — there is no `Sales` row — and then deducts interest a
second time.

**HDFC Bank FY26: reported ₹79,219 cr, computed −₹268,944 cr.** A 130% error
with the opposite sign, on India's largest private bank. The same fault
affected all 20 banks, NBFCs and insurers in the universe.

**Fix:** detect the layout by the presence of `Financing Profit`; map `Revenue`
to revenue, book interest expended as a cost of revenue, and set finance costs
to zero so the income builder cannot deduct it twice. The layout is recorded as
a warning, because EV/EBITDA and FCFF remain meaningless for these businesses
whatever the arithmetic.

---

#### RC2-004 · Inventory overstated by a third — 107 balance sheets stopped balancing
**Severity:** Critical · **Status:** Fixed

Working-capital balances are recovered by inverting screener's reported days
ratios. The first version inverted all three on **sales**. Screener strikes
inventory days and payable days on **cost of goods**.

UltraTech reports 206 inventory days. On sales that is ₹49,955 cr of cement —
35% of a ₹141,315 cr balance sheet, which no cement company has ever held.
Named assets then exceeded the reported total, the balancing plug clamped to
zero, and **107 of 135 balance sheets failed to balance**.

**Fix:** invert on the denominator the source used. Debtor days on sales, the
other two on cost.

---

#### RC2-005 · Derived items double-counted inside the balancing plug
**Severity:** Critical · **Status:** Fixed

Receivables, inventory and payables are *components of* screener's `Other
Assets` and `Other Liabilities` buckets. Naming them after ingestion — when
the plug had already been computed as "reported total less everything named" —
counted them twice.

* Reliance FY26 assets came out ₹312,395 cr too high: exactly receivables plus
  inventories.
* UltraTech FY24 was ₹12,099 cr out on the liability side after the asset side
  was corrected: exactly trade payables.

**Fix:** recompute **both** plugs against the reported totals after derivation,
and cap each derived component at 90% of the bucket that contains it — a
component larger than its own parent is arithmetically impossible, whatever
the ratio says.

---

#### RC2-006 · A tax *rate* cannot reproduce a reported bottom line
**Severity:** Critical · **Status:** Fixed

Tax expense was derived as `PBT × Tax%`. Two common cases break it:

| Company | PBT | Rate | Derived | Reported | Error |
|---|---:|---:|---:|---:|---:|
| Crompton FY26 | −79 | 191% | **+72** | −231 | sign inverted |
| DLF FY26 | 2,932 | 11% | 2,609 | 4,415 | −41% |

A rate is meaningless on a negative base, and it cannot see income recognised
below the tax line (DLF's share of associate profit).

**Fix:** derive tax as `PBT − net profit`, which reproduces the published
bottom line by construction and absorbs minority interest and associate income
along with it.

---

#### RC2-007 · One missing share count crashed the entire scoring engine
**Severity:** Critical · **Status:** Fixed · **Location:** `growth_quality.py:86`

```python
if len(shares) >= 3 and shares[0] > 0 and len(revenues) >= 3:
    per_share_growth = cagr(
        revenues[0] / shares[0], revenues[-1] / shares[-1], ...
    )                              #          ^^^^^^^^^^ never guarded
```

The guard tested `shares[0]`; the division used `shares[-1]`.

LTIMindtree reports a zero weighted-share count in its latest year — a real
artefact of the LTI/Mindtree merger, where the aggregator has the merged
entity's revenue but not yet its share count. `ZeroDivisionError` took down
**all thirteen scoring categories**, including the twelve with nothing to do
with shares.

**Fix:** guard both endpoints. **Guard:** a test walks every scoring module and
asserts that each indexed divisor has a positivity check within six lines. The
first version of that test flagged `capital_allocation.py` as well — a false
positive, since it divides by the element it guards — and was corrected to
match the divisor rather than the line.

### 2.2 Major

---

#### RC2-008 · AI audit reported 0% citation coverage on perfectly cited output
**Severity:** Major (harness) · **Status:** Fixed

The audit read `AnalystResult.audit`. The field is `citation_audit`. `getattr`
returned `None`, and the harness recorded 0% coverage for all 400 responses
while the AI layer was in fact citing every figure it used.

Reported here rather than quietly corrected because **the harness was wrong,
not the product** — and a metrics harness that under-reports is more dangerous
than one that over-reports, since nobody investigates good news.

---

#### RC2-009 · The AI answered questions it had no evidence for
**Severity:** Major · **Status:** Fixed

Asked eight deliberately unanswerable questions — headcount, FY2031 revenue, an
earnings-call quote — the analyst returned eight confident-looking answers
built from whatever evidence ranked first.

Nothing was fabricated. Every figure was real and correctly cited, and the
citation audit passed at 100%. But answering *"how many employees?"* with cash
flow from operations is a non-answer dressed as an answer, and a reader
skimming the first line would not notice.

Two causes:
1. The relevance test compared the question against the **whole assembled
   prompt**, including sixty evidence labels, so overlap was always non-zero.
2. A shared word is not available evidence: *"revenue in FY2031"* overlaps
   *"Revenue (FY26)"* on one word and would be answered with the wrong year.

**Fix:** match against the analyst's question only (`ANALYST QUESTION:`), and
add an explicit out-of-scope test for future years, specific past dates,
unheld dimensions and off-universe peers. Result: **160/160 refusals**, with no
false refusals across a control set of legitimate questions and no change to
any of the 17 fixed capabilities.

### 2.3 Minor

---

#### RC2-010 · Three companies silently absent from the universe
**Severity:** Minor · **Status:** Fixed

Screener keys on its own historical slug, which lags corporate actions. LTIM
(LTI/Mindtree merger), TATAMOTORS (CV/PV demerger) and ZOMATO (renamed
Eternal) resolved to nothing — a silent coverage hole rather than a visible
failure. Fixed with a documented alias map.

---

#### RC2-011 · Six calibration tests asserted fixture constants
**Severity:** Minor (test quality) · **Status:** Fixed

Six tests pinned exact values from the synthetic seed — 19% growth, 6.62%
margin, FY2025 as base year — and read them from the live database. Every one
failed the moment real data arrived, not because the calibrator changed but
because the fixture did.

**A test that breaks when the data changes is testing the data.** Rewritten to
assert behaviour: that drivers are derived from history, bounded plausibly, and
labelled with the right provenance. Those hold for any company.

---

#### RC2-012 · A performance test measured noise
**Severity:** Minor (test quality) · **Status:** Fixed

`test_replay_is_linear_in_transactions` divided a 20,000-row wall time by a
2,000-row one and required the quotient under 30. The baseline takes ~8 ms, so
cold-start cost dominated it and the quotient swung between 5 and 50 across
runs.

Measured properly, the engine is **flat at 3.8–4.0 µs per transaction from
2,000 to 20,000 rows** — genuinely linear. The test now asserts unit cost with
a best-of-three timing. Verified stable over three consecutive runs.

---

#### RC2-013 · A Yahoo outage silently degraded the whole dataset
**Severity:** Minor · **Status:** Fixed

The first ingestion merged both sources in one pass. Yahoo's rate limiter
tripped on company one and the circuit stayed open for the entire run: 128
companies ingested at 46% coverage, uniformly enough to look like a design
limit rather than one bad minute.

**Fix:** separate the passes, add cooldown-and-resume, and — the durable fix —
derive working capital from screener's own ratios so Yahoo is genuinely
optional rather than nominally optional.

---

## 3. Performance

### Per endpoint, warm, real data (median of 9)

| Endpoint | p50 | p95 |
|---|---:|---:|
| `/health` | 2 ms | 2 ms |
| `/companies?page_size=25` | 4 ms | 6 ms |
| `/company/{t}/financials` | 9 ms | 11 ms |
| `/company/{t}/ratios` | 9 ms | 10 ms |
| `/company/{t}/forecast?horizon=5` | 10 ms | 11 ms |
| `/company/{t}/valuation` | 14 ms | 16 ms |
| `/company/{t}/scoring` | 16 ms | 17 ms |
| `/dashboard/overview` | 17 ms | 21 ms |

### End-to-end: 135 companies × 12 endpoints

**1,620 / 1,620 calls returned 200.** p50 10 ms · p95 18 ms · p99 22 ms · max 42 ms.

Heaviest: scoring (p95 24.6 ms), valuation (21.8 ms), forecast (17.6 ms) — the
three engines that run the others.

### Concurrency

| Run | Result |
|---|---|
| 25 concurrent, 600 requests | **0 server errors**, p95 422 ms |
| 50 concurrent, 1,500 requests (limiter off) | **1,500/1,500**, p95 777 ms |

### Query efficiency — no N+1 anywhere

| Stage | Queries | Time |
|---|---:|---:|
| Load analysis | 2 | 76.8 ms |
| Statements | 0 (cached) | 0.6 ms |
| Ratios | 0 | 1.4 ms |
| Forecast | 1 | 3.3 ms |
| Valuation | 2 | 5.1 ms |
| Scoring | 4 | 8.8 ms |

A full twelve-engine analysis costs **9 queries**. `EXPLAIN QUERY PLAN`
confirms the hot path uses `ix_fact_lookup (company_id, fiscal_year,
line_item)` — no scan against 42,025 rows.

**No optimisation was required.** Nothing measured above a 25 ms p95, so
changing the code would have added risk for no user-visible gain.

---

## 4. Security

### Dependency vulnerabilities: 18 → **0**

| Package | Was | Now | CVEs cleared |
|---|---|---|---:|
| python-jose | 3.3.0 | **removed** | 3 (one unfixable) |
| ecdsa | transitive | **removed** | 1 (won't-fix) |
| starlette | 0.41.3 | 1.3.1 | 7 |
| python-multipart | 0.0.20 | 0.0.31 | 6 |
| pytest | 8.3.4 | 9.1.1 | 1 |
| PyJWT | — | 2.13.0 | added, then patched |

The notable one is **python-jose**. It carries `PYSEC-2025-185` with no fix,
and pulls in `ecdsa`, whose Minerva timing vulnerability the maintainers have
declared out of scope and will not fix. The platform signs **HS256 only** —
none of the asymmetric machinery either library provides was in use. Replacing
it with PyJWT removed four unfixable CVEs at the cost of two import lines.

All 1,903 tests pass on the upgraded stack, including a check that `alg: none`
is still rejected.

### Static analysis: 6 HIGH → **0**

All six were SHA-1, every one a cache key or content fingerprint, none a
security digest. Marked `usedforsecurity=False`, which states the intent in
code rather than in a reviewer's memory.

**A regex-based bulk edit corrupted all six call sites** — it inserted the
argument into the inner `.encode()` call. Caught immediately by an import
check, then fixed individually. Recorded here because an automated edit that
compiles is not the same as one that is correct.

Three MEDIUM remain, all reviewed:

| Finding | Assessment |
|---|---|
| B310 ×2, `urlopen` in ingestion | Scheme fixed https, host a module constant, URL assembled from a ticker. Not reachable from a request. |
| B608, table name in a COUNT | Name comes from SQLAlchemy's inspector. An identifier allow-list was added as defence in depth. |

### Posture unchanged from rc1

Argon2id passwords · rotating refresh tokens with reuse detection · tenant
isolation proven by 9 tests · 100+ controls in `SECURITY_CHECKLIST.md`. The
rate limiter demonstrated itself during this sprint by correctly throttling a
1,620-call burst to 21% — the control working, not a defect.

---

## 5. AI quality

| Metric | Result |
|---|---|
| Responses audited | **1,755** (135 companies × 13 capabilities) |
| Hallucination rate | **0.000%** |
| Mean citation coverage | **100.00%** |
| Unresolvable citation markers | **0** |
| Fabricated figures | **0** |
| Adversarial refusals | **160 / 160 (100%)** |
| Detector self-test | **5 / 5** |

### The detector self-test matters more than the score

The offline provider is deterministic and template-driven, so **it cannot
hallucinate the way a real model does**. A clean audit of its output proves
almost nothing on its own. What the self-test proves is that the *verification
machinery* works: fed known-bad responses, the auditor catches an
unresolvable marker, a fabricated figure and an uncited numeric claim, while
accepting a correct response and tolerating legitimate rounding.

**A 0% hallucination rate against an offline provider is a statement about the
provider, not about the platform's safety with a real LLM.** With an API key
configured, the guardrails and citation audit are the controls that matter,
and they are demonstrably functional.

---

## 6. Data provenance — read before public beta

| | |
|---|---|
| Primary source | **screener.in**, consolidated, ₹ crore native, 12 years |
| Secondary | **Yahoo Finance** — granularity, quotes, price history |
| Coverage | **49.5%** of the 54 canonical line items, latest year |
| Companies | 135 of 136 attempted |
| Range | FY2006 – FY2026 |

**These are aggregators, not filings.** They are accurate on headline figures —
operating profit, net profit, EPS and total assets all reconcile within 3% for
every company tested — and they differ from an annual report at the margins,
mostly on classification. Every fact carries its `source`, so provenance
travels with the number.

**Coverage is 49.5%, not 100%.** Cash and bank balances, goodwill, intangibles
and deferred tax are not published by either source at line-item level. They
are **left absent rather than estimated** — an absent fact is honest, a
fabricated one is not, and Module 4 already degrades its data grade when cash
is unknown.

**The one company that could not be ingested** (SIEMENS-ENERGY, recently
demerged) is recorded as a failure rather than dropped from the universe.

---

## 7. Test suite

**1,903 tests passing.** 88% overall coverage; **92% excluding `app/data/`**,
which is network-bound ingestion code CI cannot exercise. That code was
validated against 135 live companies during this sprint, and its pure
transformation logic — parsing, unit conversion, sign convention, mapping — is
covered offline by 53 new tests in `test_real_data.py`, one per defect above.

| Suite | Tests |
|---|---:|
| test_document_engine | 213 |
| test_platform_services | 198 |
| test_platform_domain | 137 |
| test_scoring_engine | 112 |
| test_portfolio_engine | 95 |
| test_analysis_api | 94 |
| test_report_engine | 92 |
| test_platform_api | 82 |
| test_valuation_engines | 87 |
| test_ai_engine | 82 |
| test_ai_api | 71 |
| test_valuation_api | 70 |
| test_forecast_api | 64 |
| test_scoring_api | 58 |
| test_document_api | 55 |
| **test_real_data** | **53** |
| others | 340 |

---

## 8. Known limitations

1. **Aggregator data, not filings.** §6. An institutional deployment should
   ingest XBRL from BSE/NSE directly.
2. **49.5% line-item coverage.** Cash, goodwill, intangibles and deferred tax
   are absent, not estimated.
3. **Banks and insurers do not fit the schema.** They are ingested and read
   correctly, and Module 4 refuses to publish a DCF for them. That refusal is
   the correct behaviour, not a gap to close.
4. **Yahoo enrichment is unavailable from a single IP.** Rate limited too
   aggressively for a 135-company pass. Working capital is derived from
   screener's own ratios instead.
5. **Docker images were not built.** No Docker daemon in this environment. The
   Dockerfiles were verified statically against ten criteria (multi-stage,
   non-root, healthcheck, OCR and font dependencies, no runtime toolchain) and
   the compose topology parses, but **no image was built or started here**.
   CI builds and smoke-tests both.
6. **MFA modelled, not enforced.** Unchanged from rc1.
7. **OAuth unexercised.** No provider credentials. Unchanged from rc1.
8. **Billing hooks, not billing.** Unchanged from rc1.
9. **The AI audit ran against the offline provider.** §5.

---

## 9. Conditions for public beta

| # | Condition | Why |
|---|---|---|
| 1 | Display data provenance in the UI: source, as-of date, and the 49.5% coverage figure | Users must not mistake aggregator data for filings |
| 2 | Keep Module 4's data-quality gate enforcing | It is what stops an unreliable valuation being published |
| 3 | Set `SECRET_KEY`, `ENCRYPTION_KEY`, `NATIVE_AUTH=true`, `ENVIRONMENT=production` | The app refuses to sign or encrypt without the first two |
| 4 | Take a backup, verify it, and restore it into a scratch database | A backup nobody has restored is a hypothesis |

Then confirm:

```bash
curl -fsS https://api.yourdomain.com/health/ready | jq '.checks[] | select(.ok==false)'
# must return nothing
```

**Not yet suitable for:** paying institutional clients relying on
investment-grade output; anything requiring filing-level accuracy; regulated
advice.

---

## 10. Reproducing this

```bash
cd backend
python -m app.data                    # ingest 135 companies, derive, validate
python -m pytest tests/ -q            # 1,903 tests
python tests/load/loadtest.py --concurrency 50 --requests 1500
pip-audit -r requirements.txt         # 0 vulnerabilities
bandit -r app -ll --skip B101         # 0 high
```

The ingestion is idempotent — re-running replaces each company's facts rather
than appending, so a partial run can simply be repeated.

---

## 11. Sign-off

| Gate | Target | Actual | |
|---|---|---|:--:|
| Real data replaces synthetic | all companies | 135 / 136 | ✅ |
| Calculations validated | ≥95% | **100%** (2,279 checks) | ✅ |
| Companies fully clean | ≥90% | **100%** (135/135) | ✅ |
| E2E on ≥100 companies | 100 | **135** × 12 endpoints, 100% | ✅ |
| AI hallucination | 0 | **0.000%** of 1,755 | ✅ |
| Adversarial refusal | ≥90% | **100%** (160/160) | ✅ |
| Known CVEs | 0 | **0** (was 18) | ✅ |
| Bandit HIGH | 0 | **0** (was 6) | ✅ |
| Tests | all pass | **1,903 / 1,903** | ✅ |
| Coverage | ≥90% | 88% overall, **92% core** | ⚠️ |
| p95 latency | <100 ms | **18 ms** | ✅ |
| No 5xx under load | 0 | **0** at concurrency 50 | ✅ |
| Docker images build | verified | **static only** — no daemon | ⚠️ |
| Line-item coverage | 100% | **49.5%** — aggregator limit | ⚠️ |

**Recommendation: GO for public beta** with the four conditions in §9 and the
provenance disclosure in §6 shown in the product.

**NO-GO for institutional production** until financials are ingested from
filings rather than aggregators, and coverage of the canonical 54 rises above
80%.
