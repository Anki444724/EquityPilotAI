# Scheduler Optimisation — Validation Report

Deployed as `365f80b`. All figures read from production on 2026-08-02.

## Requirement status

| # | Requirement | Status |
|---|---|---|
| 1 | Auto-discover IR URLs | **Done, 84 found — partial coverage** |
| 2 | 500 companies within 24h | **Capacity built, not yet demonstrated** |
| 3 | NSE retry with backoff | **Done** |
| 4 | Classify into 9 types | **Done, 33.6% → 68.8%** |
| 5 | Store URL, type, FY, confidence | **Done** |
| 6 | Scheduler dashboard | **Done, 7/7 metrics live** |

---

## 1. Investor Relations URL discovery

`ir_url` was NULL for all 501 rows, so Priority-1 contributed nothing and
100% of 2,902 filings came from NSE. Now **84 URLs stored**, all discovered
automatically.

| Method | Count | Meaning |
|---|---|---|
| `probe:200` | 61 | Fetched and confirmed (0.90) |
| `probe:root` | 14 | Corporate root only, no IR path matched (0.40) |
| `probe:403` | 7 | Page exists, refused a bot (0.60) |
| `seed` | 2 | Curated, hand-checked (0.95) |

Real examples: `adani.com/investors`, `aarti.co.in/investors`,
`aiaengineering.com/investors`, `adanigreenenergy.com/investors`.

**Why probing, not an API.** Established by testing, not assumption:

* NSE `quote-equity` → HTTP 403 from this environment
* BSE `ListofScripData` → 4,928 rows, **no website field**; its only URL
  (`NSURL`) points back at BSE's own price page
* BSE `CompanyProfile` / `ComplianceHeader` → not JSON

**Why 403 counts as a hit.** `cipla.com/investors` answers 200, but
`tcs.com/investor-relations` and `infosys.com/investors` answer **403** — they
exist and refuse a non-browser client. Discarding them would lose two of
India's largest companies. Stored at lower confidence with the method, so a
guess is visibly a guess.

**Honest limit: the hit rate is ~31%.** First live run found 14 of 45. Probing
the misses revealed two real gaps, both fixed: `.in` was never tried (aavas.in
resolves, aavas.com does not), and ABB and ABFRL answer 200 at the root while
every conventional path 404s. The root fallback then supplied 14 of the next
19 finds. The remaining ceiling is name-derivation: "Bajaj Finserv" does not
live at `bajajfinserv.com/investors`, and no amount of path-guessing fixes
that. **416 companies still have no IR URL.**

---

## 2. Coverage within 24 hours

| | Before | Now |
|---|---|---|
| Crawl interval | 86400s | **43200s (12h)** |
| Companies per pass | 25 | **260** |
| Downloads per company | 5 | 2 |
| Theoretical daily reach | 25 | **520** |

**Raising the budget alone would not have worked.** Crawl job 528 took 5h20m
for 25 companies (~12.8 min each) — and that time is almost entirely
*downloads*, not discovery. Discovery is one HTTP call per source. Splitting
them lets every company be *checked* daily while the download queue drains
under a bounded budget.

**SCHED-001 — a defect this exposed.** After deploying the 12h interval the
dashboard still showed 86400s. `sync_schedules` only *inserted* missing rows
and never compared `every_seconds` against the declared schedule, so the
change had no effect on a database that already held the row. The declared
schedule is now the source of truth; an operator's `enabled` flag is
deliberately not overwritten so a pause survives a deploy. Confirmed live:
`filing_crawl every=43200s`.

**Not yet demonstrated.** Coverage still reads 90/500 in the last 24h and 340
never crawled, because the first 12h pass has not run since the fix landed.
The capacity is in place and verified; the *outcome* needs a day to show.

---

## 3. NSE retry with exponential backoff

The provider made exactly **one** attempt with a bare `except`; 44 companies
recorded `"The read operation timed out"`.

Now 3 attempts, delays **1.9s → 5.7s → 16.9s** (base 1.5s, factor 3.0, ±30%
jitter).

* **Jitter matters more than growth.** A crawl loops over companies, so a
  fixed backoff aligns every retry into a burst and reproduces the overload
  that caused the timeout.
* **404 is not retried** — the symbol does not exist, and two more requests
  spend the rate-limit budget confirming it. 408/425/429/5xx are.
* **The cookie jar is dropped between attempts**, because a stale NSE session
  is a common cause of the second attempt failing identically.

---

## 4. Classification

**33.63% → 68.78%**, verified in production.

| Type | Count |
|---|---|
| exchange_filing | 1,312 |
| conference_call | 452 |
| shareholding | 93 |
| investor_presentation | 92 |
| credit_rating | 36 |
| quarterly_report | 11 |
| other | 487 |
| unclassified | 419 |

Added `DocumentType.SHAREHOLDING`, so all nine required classes exist. Rules
were derived from **actual production subject lines**, each annotated with the
count it addresses — the largest being 452 rows of
`"Analysts/Institutional Investor Meet/Con. Call Updates"`, missed because the
abbreviation is `"Con. Call"` with a full stop and `"Analysts/"` has no word
boundary.

New rules apply only to future crawls, so `deploy/reclassify_filings.py`
back-filled the existing 2,902 rows: **1,020 reclassified** (926 →
`exchange_filing`, 93 → `shareholding`). Conservative and idempotent — a row
is rewritten only when the new classification is genuinely better.

The residual 419 `unclassified` are noise-filtered rows (`Copy of Newspaper
Publication`) that were never stored with a type. The 487 `other` are
genuinely heterogeneous.

---

## 5. Stored fields

Already present on `DiscoveredFiling` and now populated:
`source_url`, `doc_type`, `filing_type`, `fiscal_year`,
`classification_confidence`. Asserted by test.

---

## 6. Scheduler dashboard

`GET /api/v1/scheduler/dashboard` — live output:

```
COVERAGE   universe=500 crawled24h=90 remaining=410 never=340
RETRIES    pending=3 exhausted=0
FAILURES   44 companies
IR URLS    total=84 today=84 missing=416
DOCUMENTS  downloaded_today=167 ingested_today=164 pending_download=2088
MEMORY     enrichment_runs_today=43 vault_entries_today=191
CLASSIFY   total=2902 classified=68.78%
```

All seven requested metrics, plus classification quality and live schedule
state. Every figure is recomputed from the tables rather than from a counter —
a counter drifts the first time a pass is interrupted, and this crawler has
been interrupted.

Mounted under `/scheduler/`, not `/filings/`: ROUTE-001 showed
`/filings/{ticker}` captures a static sibling path and answers 200 with empty
data, which is worse than a 404 because it looks like data.

---

## Tests

**2,397 → 2,438, zero failures.** 41 new.

Three test corrections worth noting, all mine rather than the product's:

1. An existing test asserted `every_seconds == 24 * 3600` and correctly caught
   the schedule change. Rewritten to assert the property that matters — full
   coverage within 24 hours — rather than a fixed interval.
2. The dead-host probe guard asserted a ceiling the root fallback legitimately
   raises; it now asserts the invariant (one probe per domain, not one per
   path).
3. The backoff band hard-coded `>= 10.0` and flaked when jitter produced 9.71.
   Bounds are now computed from the retry schedule.

---

## Caveats

- **IR discovery is ~31% and will not reach 100% by probing.** Name-derived
  domains cannot find a company whose IR page lives on an unrelated host. The
  remaining 416 need either a paid data source or manual registration via
  `PATCH /api/v1/filings/companies/{ticker}`.
- **24-hour coverage is capacity, not yet an observed outcome.** The dashboard
  will show it after the first full day on the 12h schedule.
- **BSE still returns zero filings.** `"No Record Found!"` for every probe.
  Out of scope here; unchanged from the audit.
- **2,088 filings await download.** The reduced per-company budget drains this
  more slowly by design; the alternative is the memory pressure that crashed
  production three times.
- **Retry backoff is verified by unit test, not against a live NSE timeout.**
  NSE returns 403 to this sandbox, so the retry path could not be exercised
  end-to-end from here.
- **The 44 recorded failures predate the retry fix.** Whether it reduces them
  needs the next crawl to say.
