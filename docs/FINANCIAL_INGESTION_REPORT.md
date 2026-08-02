# Financial Ingestion — Nifty 500 Completion Report

Deployed as `e20dd31`. All figures below are read back from the production
database, not from the ingestion run's own tally.

## 1. Companies with financials

| | Count |
|---|---|
| Companies in the platform | 507 |
| Active (in scope) | **503** |
| Delisted (excluded — see §3) | 4 |
| **With financials** | **503** |
| **Still missing** | **0** |

Before this work: **135 of 503 (26.84%)**.

## 2. Coverage percentage

**100.00%** of active companies.

| Category | Covered | Total | % |
|---|---|---|---|
| Largecap | 100 | 100 | 100.0% |
| Midcap | 150 | 150 | 100.0% |
| Smallcap | 250 | 250 | 100.0% |
| Unclassified (US) | 3 | 3 | 100.0% |

Volume written: **134,700 canonical facts**, **6,319 quarterly rows**,
**4,753 shareholding rows**.

A company counts as covered only with **two or more** distinct fiscal years.
One stray year is not a financial history, and counting it would inflate this
number while leaving the statements unusable.

## 3. Companies still missing, and why

**No active company is missing financials.** Every exception below is a
partial-layer gap with a source-side cause, not an empty company.

| Company | Layer missing | Reason |
|---|---|---|
| AAPL, NFLX, NVDA | Quarterly, Shareholding, BS tie | **Not Indian.** screener.in returns `not listed` — correct behaviour for an Indian source. These use the US pipeline (SEC EDGAR + FMP) and report in $M under GAAP, where the platform's Indian balance-sheet identity does not apply. |
| TATAMOTORS, ZOMATO, LTIM, BHARATCP | — (excluded from the denominator) | **Delisted.** Retained for history, deliberately excluded from active collection. A delisted company has no current filings by definition; counting it as a miss would make 100% unreachable for the wrong reason. |

### History depth (genuine source limits, not failures)

339 of 500 Indian companies have the full 12 years. 26 have fewer than five —
in every case because the company listed recently and the history does not
exist: HDFCAMC 4y, NTPCGREEN 4y, ZFCVINDIA 5y, GROWW 5y. These are reported as
short histories rather than padded.

## 4. Validation report

Every layer was **computed**, not merely checked for row existence.
Statements, ratios, scores, valuation and forecast are all derived on demand,
so "facts are present" is not evidence a score can be produced. All 503
companies, no sampling.

| Layer | Pass | Fail | % |
|---|---|---|---|
| Income Statement | 503 | 0 | 100.0% |
| Balance Sheet | 503 | 0 | 100.0% |
| Cash Flow | 503 | 0 | 100.0% |
| Balance sheet ties | 500 | 3 | 99.4% |
| Ratios | 503 | 0 | 100.0% |
| Market Cap | 503 | 0 | 100.0% |
| Quarterly Results | 500 | 3 | 99.4% |
| Shareholding | 500 | 3 | 99.4% |
| AI Scores | 503 | 0 | 100.0% |
| Valuation + Forecast | 503 | 0 | 100.0% |

The three failures in every partial row are AAPL, NFLX and NVDA.
**Across the 500 Indian companies every layer is 100%.**

### Accuracy, not just presence

Reconciled against screener's own reported figures:

| Company | Revenue | PAT | Total assets | BS tie |
|---|---|---|---|---|
| CGPOWER FY26 | 12,418 = 12,418 | 1,199 = 1,199 | 12,644 = 12,644 | 0.0 |
| HDFCAMC FY26 | 4,616 = 4,616 | 2,858 = 2,858 | 9,991 = 9,991 | 0.0 |
| HINDZINC FY26 | 40,844 = 40,844 | 13,832 = 13,832 | 42,370 = 42,370 | 0.0 |

**Maximum absolute balance-sheet imbalance across all 500 Indian companies:
0.** Median 12 fiscal years, 13 quarters, 11 shareholding periods, 45 of 50
ratio rows populated. Scores span 37.8–79.9 (median 62.0) — a real
distribution, not a constant.

## 5. What was actually blocking this

FMP's free tier returns **HTTP 402 for every `.NS` symbol**. screener.in was
already wired as the designated primary — twelve years, ₹ crore native, Indian
consolidated presentation — and simply had nothing driving it across the 500.

`ingest_universe` could not: it iterates `NSE_UNIVERSE`, a hard-coded
136-entry tuple predating the Nifty 500 import. Extending it would maintain
the universe in two places. `FinancialsBackfillService` drives from the
**database**, so an index rebalance is data, not code. Canonicalisation is
**called, not copied** — including the financing-layout handling for banks and
NBFCs, without which HDFC Bank's net profit computed to −₹268,944 cr against a
reported +₹79,219 cr.

**Yahoo is off by default for bulk sweeps** — measured, not assumed. It 429s
from this IP and its backoff spends **~82s per company against screener's
1.4s**: 8.4 hours across 368 companies to add nothing, because the calls fail.
Screener alone reconciles exactly, as the table above shows.

## 6. No placeholders

A company or period the source has no data for is **not written at all**.
`ingest_company` returns `ok=False, error="no canonical facts derived"` rather
than an empty shell, and `QuarterlyResult.has_data` makes the guarantee
assertable rather than assumed. "No financial data available" therefore
remains truthful wherever it still appears.

## 7. Defects found and fixed

**QTR-001 (self-introduced).** Reusing the annual `_fiscal_year` reader for
the quarterly section collapsed thirteen columns onto four year keys, so
quarters silently overwrote one another and only the last survived. Added
`_fiscal_period` returning `(fiscal_year, quarter)`.

**SCRN-001.** The consolidated-page fallback only fired on an *empty* table.
GE Vernova T&D serves a consolidated page carrying a single `Dec 2010` column
while its standalone page carries Mar 2015–Mar 2026, so one fiscal year from
sixteen years ago was stored — worse than a failure, because it looks like
coverage. Consolidated must now be *competitive*, not merely present.

**SCRN-002.** Colgate Palmolive, AU Small Finance Bank and Five-Star serve a
consolidated page that is complete annually and a **stub** quarterly: two
`<th>` cells and a "View Standalone" link where the dates should be. Those
three showed no quarterly results at all. Quarterly now falls back to
standalone independently, so annual statements stay consolidated.

**QTR-002 (self-introduced, reached production).** `QuarterRow` is
`@dataclass(slots=True)` and has no `__dict__`, so the handler's
`QuarterRowOut(**row.__dict__)` raised and the endpoint returned **HTTP 500**
for every company that had quarters. The stored data was correct throughout.
Fixed with `dataclasses.asdict`.

### A test I nearly shipped that could not fail

The first regression test for QTR-002 asked the API for any company and
asserted 200. It **passed with the bug still in place**, because the seeded
test companies have no quarterly rows, so the failing comprehension never
executed. A test that cannot fail certifies the defect. It now inserts a
quarter first, and was verified by reintroducing the bug: 500 and a failing
test, then fix restored and passing.

Similarly, the backfill tests initially shared `TestingSession`; because the
service commits, rows leaked between tests and `coverage_snapshot` counted 38
companies where the test had created one. Both were harness defects, not
product defects.

## 8. Quarterly Results — new module

Built as a first-class module: `quarterly_results` table (migration
`b8c4e6f2a917`, with `created_at`/`updated_at` declared — the MIG-001 lesson),
`QuarterlyService` with QoQ and YoY, and `GET /company/{ticker}/quarterly`.

Quarters are **stored rather than derived** because four Indian quarters do
not reconcile to the audited annual figure — Q4 routinely absorbs audit
adjustments — so decomposing the annual statement would invent numbers. For
the same reason **TTM is deliberately not summed from four quarters**.

YoY is keyed on `(year−1, quarter)`, not indexed four rows back: a company
with a gap would otherwise compare the wrong quarters and report a nonsense
figure. Growth across a sign change returns null rather than presenting
−50 → +25 as "150% growth".

## Caveats

- **screener.in is a secondary source, not a filing.** Every fact is stamped
  `source="screener.in"` and carries `Precedence.IMPORT`, so a figure later
  extracted from an actual annual report supersedes it. These are
  investment-grade for screening and comparison, not a substitute for the
  filed accounts.
- **Yahoo's expense breakdown is absent.** ~30 of the 54 canonical items
  (raw materials, employee benefit, and balance-sheet detail) are aggregated
  by screener into single lines. Statements reconcile and ratios compute, but
  a cost-structure breakdown is coarser than it would be with Yahoo. Re-run
  with `--with-yahoo` once the throttle lifts.
- **Shareholding is the coarse split only.** Screener publishes
  promoter/FII/DII/government/public. `mutual_funds`, `insurance`,
  `banks_fis_aif` and `promoter_pledged` are left NULL rather than
  apportioned — an invented split would be indistinguishable from a disclosed
  one. The precise SEBI breakdown needs NSE filings, which throttle this
  sandbox's IP to roughly 4% success.
- **No frontend surface for Quarterly Results yet.** The API is live and
  tested; no tab renders it.
- **Fiscal-year convention.** A December year-end is assigned to the
  following fiscal year. Correct for Indian Apr–Mar reporting, and it is why
  some companies show an FY2027 Q1.
- The 3 US companies are unchanged by this work and remain on the SEC
  EDGAR + FMP pipeline.
