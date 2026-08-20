# Bugfix: duplicate-company rows and the financial backfill false-PENDING bug

Status: **fixed, tested, verified on PostgreSQL 16 and SQLite**
Date: 2026-08-20
Migration: `9f0b5e8c2d71_company_deduplication_and_unique_identity`

---

## 1. Symptom

`companies` held two rows for the same Indian ticker. The financial history
was attached to exactly one of them:

| Ticker | duplicate id (0 facts)                  | canonical id (owns history)              |
|---|---|---|
| J&KBANK | `3bce6a96-a4d0-45b7-a5d7-c1c9010b3e7d` | `e5d1a8de-5d2d-4861-9bca-6f6085d4d930` — 276 facts / 12 years |
| M&M | `5868f82a-0195-4414-aabf-fc40fc2e1f37` | `dff1781c-00be-4237-b545-4df26a58b2e0` — 300 facts / 12 years |
| M&MFIN | `df0d50d4-ae62-4c69-b13f-0f054b32cd84` | `f8c38456-b0b7-434c-b7a3-e586e4661b0e` — 276 facts / 12 years |
| MINDACORP | `f707b006-e67f-4df1-8da6-740e505250d4` | `58f37e74-bbde-4ef7-a56e-2a51d6dcbe6d` — 300 facts / 12 years |

`FinancialsBackfillService.companies_without_financials()` uses a correct
`LEFT JOIN`, so it kept reporting the empty twin as uncovered and the sweep
re-ingested those tickers forever.

## 2. Root cause

Two defects combined:

1. **Non-atomic check-then-insert in every company creation path.**
   `ingest_company` (the path the backfill sweep drives), the Nifty 500
   importer, and the admin create endpoint all SELECT for an existing row,
   and only then INSERT. Several entry points run concurrently in
   production — the worker (concurrency 2), the operator-triggered backfill
   job, `deploy/backfill_financials.py` and `python -m app.data` — so two
   processes can pass the existence check together. The schema declares
   `uq_company_ticker_exchange`, which should have turned the losing insert
   into an error, but on the production data the constraint was not
   effective for these rows (either lost from the schema, or the twins
   differ in venue), so the race produced real duplicate rows.

2. **Ticker lookups resolved to an arbitrary row.**
   Every creation/read path used
   `db.scalar(select(Company).where(Company.ticker == t))`. With two
   matching rows, SQLAlchemy's `Session.scalar()` silently returns the
   *first* one (verified: no `MultipleResultsFound`), so the ingest wrote
   the freshly fetched facts into whichever twin the planner returned —
   leaving the other twin permanently empty while
   `companies_without_financials()` kept (correctly) flagging it.

## 3. The canonical-company rule

The platform's existing business key is **(ticker, exchange)** — that is the
identity the schema already declares, and it is deliberately *not* ticker
alone (a US listing may share a symbol with an Indian one). The rule now
enforced end to end:

* **Indian venues (NSE, BSE, NSE/BSE) are one market.** One ticker = one
  company row, canonical venue `NSE` (what every importer writes). The dedup
  migration merges cross-venue twins and normalises the surviving row to NSE.
* **US venues keep their own namespace.**
* **Enforcement at the database:**
  * `uq_company_ticker_exchange` on `(ticker, exchange)` — re-asserted by the
    migration (drop + re-create) so it is enforced even where it was lost;
  * new `uq_companies_exchange_ticker_ci` on `(exchange, upper(ticker))` —
    turns a hypothetical `m&m` next to `M&M` into a constraint violation.
* **Enforcement in code:** every creation path flushes the insert inside a
  `try/except IntegrityError` and, on losing a race, re-reads the winner and
  updates it (the protocol `USCompanyProvisioner` already documented).
  `_check_unique` in the admin service is now venue-aware.

## 4. Files changed

### Application code
| File | Change |
|---|---|
| `app/services/universe/resolution.py` | **New.** `resolve_company()` — single place that answers "which row *is* this company": venue-scoped match, financial-history owner wins ties, deterministic order; `venue_family()` for the venue-aware admin check. |
| `app/data/ingest.py` | `ingest_company` resolves canonically (NSE-scoped) and flushes inside `try/except IntegrityError`; a lost creation race refreshes the winner instead of failing or duplicating. |
| `app/services/universe/nifty500.py` | `_existing` symbol fallback resolves canonically; `_upsert` uses the same race-arbitration flush protocol. |
| `app/services/company_admin_service.py` | `_check_unique` is venue-family-aware; `create()` turns a lost race into a friendly `CompanyAdminError`; `bulk_edit` lookup resolves canonically. |
| `app/services/company_service.py` | `get_by_ticker` uses the resolver (previously `scalar_one_or_none()` → 500 `MultipleResultsFound` on a duplicate pair). |
| `app/data/enrich.py`, `app/data/derive_wc.py` | Fact-writing CLI paths resolve the canonical row. |
| `app/services/portfolio/service.py` | Transaction/watchlist lookups resolve the canonical row. |
| `app/services/quality/service.py`, `app/data/filings/router.py`, `app/data/providers/router.py`, `app/services/platform/jobs/handlers.py` | Remaining Indian-universe ticker lookups resolve canonically. |
| `app/models/company.py` | Adds the case-insensitive guard index `uq_companies_exchange_ticker_ci` to `__table_args__` (model ↔ migration parity). |

### Migration
| File | Change |
|---|---|
| `alembic/versions/9f0b5e8c2d71_company_deduplication_and_unique_identity.py` | **New.** See §5. |

### Tests
| File | Change |
|---|---|
| `tests/test_company_dedup_migration.py` | **New.** Replays the migration against the confirmed shape (12-year history + empty twin, dependent rows, conflicting twins, case-variant pair, conflicting-facts abort invariant). |
| `tests/test_company_resolution.py` | **New.** Resolver picks the history owner; venue scoping; ingest twice → one row; ingest/nifty-import/admin-create race recovery; DB-level duplicate rejection. |
| `tests/test_financials_backfill.py` | Added: 12-fiscal-year company not reported missing; only genuinely uncovered companies selected; snapshot counts each company once. |
| `tests/test_jobs_handlers.py` | Filing-crawl targeted run test updated for the resolver seam. |

### Docs
| File | Change |
|---|---|
| `docs/BUGFIX_COMPANY_DEDUPLICATION.md` | This report. |

`FinancialsBackfillService.companies_without_financials()` was **not**
modified — no duplicate-hiding workaround. It is correct once the data is
canonical, and the migration makes it so.

## 5. The migration, step by step (safe-by-construction)

`9f0b5e8c2d71` runs everything in one transaction (Postgres) and refuses to
touch financial history:

1. **Backup first.** `companies_pre_merge_backup` = full copy of `companies`
   as it was. An audit table `company_merge_log` records every merge.
2. **Group duplicates.** Same `upper(ticker)` within a venue family: Indian
   venues grouped across NSE/BSE/NSE+BSE; foreign venues per (ticker,
   exchange). Also catches case-variant pairs (`case1` vs `CASE1`).
3. **Pick the canonical row per group.** Most financial facts, then most
   fiscal years, then oldest, then smallest id — deterministic. This is the
   "keep the company ID that already owns the financial history" rule.
4. **Migrate every dependent FK** (24 tables referencing `companies.id`,
   discovered live from `pg_catalog` on Postgres so future tables cannot be
   skipped; model-derived list on SQLite):
   * plain FKs → moved wholesale;
   * `(company_id, version)` tables (`company_versions`,
     `financial_fact_versions`, `ai_score_versions`) → versions re-sequenced
     above the canonical maximum, nothing lost;
   * unique-key tables (`financial_facts`, `documents`,
     `quarterly_results`, `yearly_observations`, `knowledge_entries`,
     `reports`, `score_snapshots`, `shareholding_snapshots`,
     `data_quality_snapshots`, `company_crawl_state`) → conflict-free moves;
     redundant twins (same unique key on the canonical row) are copied to
     `company_merge_backup_<table>` first and then purged explicitly —
     no implicit cascade surprises.
5. **Financial-history hard invariant.** If any `financial_facts` row would
   be stranded, the migration **aborts** (everything rolls back) rather than
   deleting or overwriting a single fact.
6. **Metadata merge.** COALESCE fills the canonical row's nulls from the
   duplicate (sector, industry, ISIN with a cross-row uniqueness guard,
   listing status active-wins, `data_version + 1`).
7. **Delete the duplicate row** only after every dependent reference has
   been migrated, verified and backed up.
8. **Re-assert the identity.** Drop + re-create `uq_company_ticker_exchange`;
   create `uq_companies_exchange_ticker_ci` on `(exchange, upper(ticker))`.

`downgrade()` refuses to guess (a faithful reversal would have to un-merge
dependent rows); restoration is from the backup tables, which the error
message names.

## 6. Production runbook

```bash
# 0. Pre-flight backup (never skip — the migration also self-backs up)
pg_dump -Fc -d ierp -f ierp_pre_dedup_$(date +%F).dump

# 1. Verify what the migration will see (expect the duplicate pairs)
psql -d ierp -c "
  SELECT upper(ticker) t, exchange, COUNT(*) FROM companies
  GROUP BY 1,2 HAVING COUNT(*) > 1;"

# 2. Apply
cd backend && alembic upgrade head

# 3. Verify
psql -d ierp -c "
  SELECT c.ticker, c.id, COUNT(f.id) facts, COUNT(DISTINCT f.fiscal_year) years
  FROM companies c LEFT JOIN financial_facts f ON f.company_id = c.id
  WHERE c.ticker IN ('M&M','M&MFIN','J&KBANK','MINDACORP')
  GROUP BY c.id, c.ticker ORDER BY c.ticker;"
#  → one row per ticker, with the facts-owning id
psql -d ierp -c "SELECT * FROM company_merge_log ORDER BY merged_at;"
psql -d ierp -c "\d companies"   # uq_company_ticker_exchange present
```

## 7. Verification results

**Full test suite (SQLite):** `2980 passed, 6 skipped` (the 6 are the
pre-existing migration-chain skips on SQLite).

**PostgreSQL 16.2 replay** (real `ierp` database, `pgserver` binary build):
seeded with the exact four duplicate pairs above (exact ids, exact fact
counts — 276/300 facts across 12 fiscal years on the canonical rows, 0 on
the twins), dependent rows in `company_versions`, `financial_fact_versions`,
`documents` (incl. a content-hash twin pair), `quarterly_results`,
`watchlist_entries`, `data_quality_snapshots`, plus a same-exchange pair
`TESTCO` inserted under a deliberately dropped unique constraint:

* bug-report SQL after migration: **one row per ticker** —
  `J&KBANK → e5d1a8de…` (276/12), `M&M → dff1781c…` (300/12),
  `M&MFIN → f8c38456…` (276/12), `MINDACORP → 58f37e74…` (300/12);
* **financial history intact**: 1,164 total facts before and after;
* **0 dangling references** across all 24 referencing tables;
* `TESTCO` same-exchange pair merged, constraint re-asserted;
* dependents migrated: versions re-sequenced `[1, 2]`, watchlist repointed,
  quarters and documents moved, twin documents present in
  `company_merge_backup_documents`, dup quality snapshot in its backup;
* `uq_company_ticker_exchange` and `uq_companies_exchange_ticker_ci` exist
  **and enforce**: inserting `('M&M','NSE')` and `('m&m','NSE')` both raise
  `IntegrityError`;
* merge audit: 33 `company_merge_log` rows; all 4 dup rows preserved in
  `companies_pre_merge_backup`;
* `FinancialsBackfillService.companies_without_financials()` returns **no**
  covered ticker for the four pairs.

## 8. Regression tests map to the requirements

| Requirement | Test |
|---|---|
| Sync twice does not create another company | `test_ingest_company_twice_keeps_one_row`; existing `test_a_second_run_creates_nothing` |
| 12-year company not reported missing | `test_company_with_twelve_fiscal_years_is_not_reported_missing` |
| Duplicate ids cannot be recreated | `test_ingest_losing_a_creation_race_merges_into_the_winner`, `test_nifty500_import_losing_a_creation_race_merges_into_the_winner`, `test_admin_create_losing_a_creation_race_is_a_friendly_error`, `test_duplicate_tickers_are_blocked_by_the_database` |
| `companies_without_financials()` only genuinely uncovered | `test_only_genuinely_uncovered_companies_are_selected`, `test_coverage_snapshot_counts_each_company_once` |
| Migration preserves history & dependents | `TestConfirmedShapeMerge.*`, `TestCaseVariantMerge`, `TestFinancialHistoryInvariant` |

---

## 9. Follow-up fix: transferring a unique ISIN (production incident)

The first production run of `9f0b5e8c2d71` aborted:

```
sqlalchemy.exc.IntegrityError: duplicate key value violates unique constraint
"companies_isin_key"
DETAIL:  Key (isin)=(INE101A01026) already exists.
```

The pair involved:

| id | ticker | exchange | isin | created_at | financial history |
|---|---|---|---|---|---|
| `dff1781c-00be-4237-b545-4df26a58b2e0` | M&M | NSE | NULL | 2026-08-19 | **yes → canonical** |
| `5868f82a-0195-4414-aabf-fc40fc2e1f37` | M&M | NSE | INE101A01026 | 2026-08-17 | no → duplicate |

### Cause

`companies.isin` is UNIQUE across the whole table. `_merge_metadata()` adopted the
duplicate's ISIN with a CASE expression whose guard deliberately excluded the
duplicate itself (`o.id <> :dup`), so when the canonical row had no ISIN the
statement assigned the duplicate's ISIN **while the duplicate row still held it**.
PostgreSQL validates a plain UNIQUE constraint per row as the UPDATE executes, not
at commit, so the merge collided with `companies_isin_key` and the whole migration
rolled back. No data was harmed; the upgrade simply could not proceed.

### Fix

`_transfer_isin()` runs immediately before the metadata merge and does the transfer
in the only order that is legal: **release from the duplicate, then adopt on the
canonical row**.

```python
UPDATE companies SET isin = NULL   WHERE id = :dup AND isin = :isin
UPDATE companies SET isin = :isin  WHERE id = :can AND isin IS NULL
```

Both statements run on the migration's connection inside its transaction, so no
other session observes the intermediate state, and the duplicate row is deleted a
few statements later anyway.

Properties:

* **Generic** — reads whatever ISIN the pair holds; applies to every duplicate
  group, not to M&M.
* **Idempotent** — once the canonical row owns an ISIN the function returns without
  writing.
* **Non-destructive** — the constraint is never dropped, disabled or deferred; a
  third owner of the same ISIN leaves both rows untouched rather than aborting.
* **Minimal** — purely additive. Canonical selection, the COALESCE metadata merge,
  FK reassignment, the financial-facts invariant, backups, the merge log and the
  identity constraints are byte-for-byte unchanged.

### Regression tests

`backend/tests/test_company_dedup_migration.py`

| Test class | Covers |
|---|---|
| `TestProductionIsinTransfer` | the exact incident rows and ISIN: upgrade completes, `dff1781c…` survives owning `INE101A01026`, `5868f82a…` is gone with no dangling references, the 9 facts stay put, `companies_isin_key` is still enforced, no duplicate (ticker, exchange) remains |
| `TestIsinTransferRules` | `_transfer_isin` in isolation: release-before-assign, idempotency, canonical keeps its own ISIN, nothing to transfer, third owner leaves both rows untouched |

Verified failing before the fix (`UNIQUE constraint failed: companies.isin`) and
passing after.


---

## 10. Second production incident: the (ticker, exchange) identity clash

The next run failed earlier than the first:

```
psycopg.errors.UniqueViolation: duplicate key value violates unique constraint
"uq_company_ticker_exchange"
DETAIL:  Key (ticker, exchange)=(M&M, NSE) already exists.
```

raised by the first statement of the ISIN transfer:

```sql
UPDATE companies SET isin = NULL WHERE id = :dup AND isin = :isin
```

Both incident rows sit on the same venue, which the first incident report did
not show:

| id | ticker | exchange | isin | history |
|---|---|---|---|---|
| `dff1781c-00be-4237-b545-4df26a58b2e0` | M&M | NSE | NULL | yes, canonical |
| `5868f82a-0195-4414-aabf-fc40fc2e1f37` | M&M | NSE | INE101A01026 | no, duplicate |

### Why an UPDATE of `isin` violates a constraint on `(ticker, exchange)`

A UNIQUE constraint in PostgreSQL is a unique **index**, and an index is
maintained on write. An UPDATE writes a new row version; unless it qualifies as
a heap-only tuple it must insert a fresh entry into *every* index on the table.
The entry inserted for the duplicate carries `(M&M, NSE)` — the key the
canonical row already holds, live — so the insert is rejected. The column the
UPDATE actually changed is irrelevant.

The consequence is broader than the ISIN transfer: while the pair shares an
identity, **neither row can be written to at all**, so the metadata merge on
the canonical row was equally exposed. The database was in a state the
constraint should have prevented (the constraint enforces on write but never
retro-validated the rows already present), and the migration was trying to
repair exactly that state while the constraint blocked every repair.

### The ordering fix

`_quarantine_duplicate_identity()` runs first, before any statement touches
either row:

```sql
UPDATE companies SET ticker = '~DUP~<duplicate-uuid-hex>' WHERE id = :dup
```

The new index entry carries a key nothing else holds, so this write is legal
while the old key stays with the canonical row. From that point the pair no
longer collides, and every later statement — the ISIN release, the ISIN
adoption, the COALESCE metadata merge, the delete — writes non-conflicting
keys. The order inside `_merge_metadata()` is now:

1. `_quarantine_duplicate_identity()` — removes the `(ticker, exchange)` clash;
2. `_transfer_isin()` — releases the ISIN from the duplicate, then adopts it;
3. the COALESCE merge — unchanged;

followed by the existing delete, the Indian-venue normalisation, and step 6,
which drops and re-adds `uq_company_ticker_exchange` so the constraint ends the
migration valid over clean data.

Properties:

* **Nothing is deleted or disabled.** Both rows survive the quarantine, the
  constraint stays in force for the whole migration, and it is re-asserted at
  the end.
* **The canonical identity never changes.** Only the duplicate is renamed, only
  when it genuinely collides. An NSE/BSE pair is left alone.
* **The sentinel is invisible afterwards.** The duplicate is deleted a few
  statements later; `companies_pre_merge_backup` (written before the loop) and
  `company_merge_log` both hold the real ticker.
* **`ticker` is not in `COALESCE_COLUMNS`,** so the sentinel cannot reach the
  survivor.
* **Idempotent** — a row already carrying the prefix is left as it is.

### Regression tests

| Test class | Covers |
|---|---|
| `TestProductionIdentityAndIsinConflict` | both incident rows on NSE with the duplicate ISIN, under a trigger that reproduces PostgreSQL's write-time index semantics: the upgrade completes, the canonical row keeps `M&M`/`NSE` and gains the ISIN, no sentinel survives, the backup holds the real ticker, facts and documents are preserved, both constraints still enforce |
| `TestQuarantineRules` | `_quarantine_duplicate_identity` in isolation: clashing pair renamed (canonical untouched), different-exchange pair left alone, idempotency, distinct sentinels per duplicate, sentinel fits `companies.ticker` |

Verified failing before the fix with
`duplicate key value violates unique constraint uq_company_ticker_exchange`,
and passing after.
