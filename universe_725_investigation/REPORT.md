# Universe expansion 525 → 725: investigation (2026-08-23)

Investigation only — no production data touched.

## Files

- `nifty500_live_2026-08-23.txt` — tickers extracted from the LIVE NSE
  `ind_nifty500list.csv` on 2026-08-23 (499 unique tickers; the list
  rebalances, treat as a snapshot).
- `nifty200_live_2026-08-23.txt` — tickers extracted from the LIVE NSE
  `ind_nifty200list.csv` on 2026-08-23 (200 unique tickers).

## Key findings

1. The 525-company covered universe = NSE Nifty500 constituents
   (`backend/app/services/universe/nifty500.py`, commit 0715684, migration
   f2b71c4e9a08) + financial facts backfilled from Screener.in
   (`backend/app/services/universe/financials_backfill.py`).

2. Nifty 200 (the only 200-name NSE index) is a SUBSET of Nifty 500:
   199/200 tickers are already in the Nifty500 list; the one difference is
   Cummins (CUMMINS in Nifty200 vs CUMMINSIND in Nifty500 — same ISIN
   INE298A01020, same company). Union adds ~0 companies. If "725" was
   computed as 525 + 200 (Nifty200), that arithmetic is the bug.

3. NSE publishes NO constituent list beyond the top 500
   (ind_niftymicrocap150list.csv does not exist; Nifty500 is NSE's largest
   general index). The next 200 (rank ~501-700) must be derived — e.g. from
   the BSE active-scrip master (already used by the importer for BSE codes)
   by free-float market cap, excluding the current universe.

4. BHARATCP is in NEITHER list. It is a legacy pre-Nifty500 test-fixture row
   (docs/NIFTY500_IMPORT_REPORT.md §5): marked `delisted` on 2026-08-01,
   540 canonical facts retained. The backfill sweep's `only_active=True`
   filter excludes it. Including it is an explicit business decision, not an
   index-driven one.

5. Overwrite semantics:
   - Nifty500Importer: identity fields only (name, ISIN, BSE code, sector,
     category, index_membership, listing_status); ISIN-first match; never
     touches financial facts. Safe to re-run. dry_run supported.
   - FinancialsBackfillService sweep: selects only companies with < 2 fiscal
     years of facts (MIN_USEFUL_YEARS) and only `active` listings. Covered
     companies are never re-fetched. Safe.
   - `ingest_company` (underlying): DESTRUCTIVE for an existing company —
     deletes all its FinancialFact rows and re-fetches from Screener
     (data_version bumped). The sweep never calls it on covered companies,
     but a TARGETED run (`tickers` payload / `companies_by_tickers`) does.
     Do not targeted-run against the existing 525.

## Verify production's exact 525

```sql
SELECT listing_status, count(*) FROM companies GROUP BY 1;
SELECT index_membership, count(*) FROM companies GROUP BY 1;
SELECT count(DISTINCT c.id)
FROM companies c JOIN financial_facts f ON f.company_id = c.id
WHERE c.listing_status = 'active'
  AND c.id IN (
    SELECT company_id FROM financial_facts
    GROUP BY company_id HAVING count(DISTINCT fiscal_year) >= 2
  );
```

Diff the live NSE list against the DB:

```sql
SELECT ticker FROM companies
WHERE ticker NOT IN (<tickers from nifty500_live_2026-08-23.txt>);
```

## Commands (when approved)

1. Identity refresh (no financial data): `Nifty500Importer(db).run(dry_run=True)`
   first, then `dry_run=False` (ad-hoc script with DATABASE_URL — there is no
   API endpoint or deploy script for it in the repo).
2. Financials: `DATABASE_URL=... python3 deploy/backfill_financials.py
   --report-only`, then `... --limit 50` increments (or the daily scheduled
   job `financials_backfill`, 25 companies/run, resumable; or
   `POST /api/v1/platform/financials/backfill`).
3. Document collection registration: `POST /api/v1/filings/enable-universe?index=NIFTY500`.
