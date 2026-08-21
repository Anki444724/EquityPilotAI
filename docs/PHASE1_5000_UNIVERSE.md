# Phase 1 — 5,000-Company Universe: Implementation Report

**Status:** complete; production NOT deployed (per instruction). All gates green:

| Gate | Result |
|---|---|
| Full backend suite | **3,026 passed, 6 skipped** (baseline before Phase 1: 2,961) |
| Migration round-trip (upgrade → downgrade ×5 → re-upgrade) | ✅ `tests/test_phase1_migrations.py` |
| 5,000-company deterministic mock pipeline | ✅ `tests/load/phase1_bench.py` + `test_company_universe_sync.py` |
| Repeated sync idempotent | ✅ second run: 0 inserted, 0 updated, 5,000 unchanged, 0 duplicates |
| No identity duplicates | ✅ `(ticker, exchange)` pairs and ISINs unique at 5,000 + 500 coexistence test |
| Existing 500-company data intact | ✅ ID-preservation test (`real-*` ids survive a 5,000-company sync) |
| Frontend | `tsc` clean · 14/14 tests · production build OK · **no UI files touched** |

---

## 1. Exact files changed

### Backend — new
| File | Purpose |
|---|---|
| `app/models/market.py` | `MarketQuote` — persistent latest quote per company |
| `app/models/ingestion.py` | `IngestionRun` / `IngestionFailure` — sync observability + retry queue |
| `app/data/providers/mock.py` | Deterministic, offline `MockMarketProvider` |
| `app/data/mock_financials.py` | Deterministic mock canonical facts (balance-sheet-tying) |
| `app/services/universe/company_universe.py` | Batched, resumable, identity-preserving universe sync + mock/Nifty500/full-market sources |
| `app/services/market/__init__.py`, `persistence.py`, `sync.py` | Quote/bar upserts; `PriceSyncService`, `HistoricalPriceSyncService`, `FailedRetryService` |
| `alembic/versions/b1e5a7d2c904_…`, `c2f6b8e3d015_…`, `d3a7c9f4e126_…`, `e4b8d0a5f237_…`, `f5c9e1b6a348_…` | The five Phase-1 migrations (below) |
| `tests/test_mock_provider.py`, `test_company_universe_sync.py`, `test_market_persistence.py`, `test_financial_upsert.py`, `test_search_universe.py`, `test_phase1_jobs.py`, `test_phase1_migrations.py` | 65 new tests |
| `tests/load/phase1_bench.py` | Full-scale measurement harness (SQLite or Postgres) |

### Backend — modified
| File | Change |
|---|---|
| `app/models/company.py` | `companies` += `metadata_source`, `metadata_synced_at`; `financial_facts` += `consolidated`, `fetched_at`, `data_version` |
| `app/models/portfolio.py` | `price_history` += `day_open`, `day_high`, `day_low`, `provider` |
| `app/models/__init__.py` | register the two new model modules |
| `app/core/config.py` | 12 new settings (§4) |
| `app/data/providers/base.py` | `Quote` += 52-week range + market status; base class += optional `fetch_quote` / `fetch_history` |
| `app/data/providers/router.py` | `DATA_PROVIDER` chain selection (mutually exclusive mock/real); `default_providers`, `active_provider_name`, `primary_market_provider` |
| `app/services/live_market.py` | quote refresher uses the configured chain's head (mock in mock mode) instead of hard-coded Yahoo |
| `app/data/ingest.py` | **delete-and-replace → idempotent upsert** (shared `_upsert_facts`); version snapshot written only when something changed |
| `app/services/company_service.py` | search extended to ISIN / BSE code / industry + 60s `search:{q}:{n}` cache |
| `app/services/platform/cache.py` | `Namespace.SEARCH` (TTL 60s) |
| `app/domain/platform/jobs.py` | 4 job kinds + labels + priorities + retry policies |
| `app/services/platform/jobs/worker.py` | `_phase1_schedules()` + `ALL_SCHEDULES` (env-gated; domain stays settings-free) |
| `app/services/platform/jobs/handlers.py` | 4 new handlers; `FINANCIALS_BACKFILL` delegates to the mock sweep in mock mode |
| `app/api/v1/companies.py` | 3 additive endpoints (§6) |
| `app/api/v1/admin.py` | schedules listing includes the Phase-1 gated entries |
| `app/schemas/company.py` | `CompanyQuote`, `CompanyPrices`, `PriceBar`, `CompanyDataStatus` |
| `.env.example` | every new setting documented |
| `tests/conftest.py` | `phase1_db`, `big_db`, `mock_provider_mode`, `phase1_client` fixtures |
| `tests/test_ingest_provenance.py` | updated to the Phase-1 contract (identical re-ingest ⇒ no new version; changed values ⇒ v2 + row-level `data_version` bump) |

### Frontend — modified (client only, zero UI change)
`src/lib/types.ts` (4 new interfaces + `PriceRange`), `src/lib/api.ts` (`api.quote/prices/dataStatus`). No page/component files touched; the mobile-responsive Financial Analysis UI is byte-identical.

## 2. Migrations added (all additive; all have tested downgrade)

| Revision | Change | Downgrade |
|---|---|---|
| `b1e5a7d2c904` | `companies.metadata_source`, `companies.metadata_synced_at` (nullable) | drop 2 columns |
| `c2f6b8e3d015` | `financial_facts.consolidated` (NOT NULL default true), `.fetched_at`, `.data_version` (NOT NULL default 1). **No constraint changed** — see note | drop 3 columns |
| `d3a7c9f4e126` | new `market_quotes` table (PK `company_id`, FK→companies CASCADE); `price_history` += OHLC + `provider` via batch ALTER | drop columns + table |
| `e4b8d0a5f237` | new `ingestion_runs`, `ingestion_failures` (FK→runs CASCADE; `job_id` SET NULL) | drop both tables |
| `f5c9e1b6a348` | `pg_trgm` + GIN indexes on `companies.name/ticker/isin` — **PostgreSQL only**, dialect-guarded, no-op elsewhere | drop 3 indexes |

**Constraint register (per the safety brief):**
- old `uq_fact_company_year_item_precedence` → **unchanged**. The audit's proposed basis-aware constraint swap is deferred to Phase 2 (Phase 1 writes consolidated rows only; the existing key is exactly the upsert conflict target). Reason + rollback are documented inside `c2f6b8e3d015`.
- `uq_company_ticker_exchange`, `isin` UNIQUE, `uq_price_ticker_date`, `uq_shareholding_period`, `uq_quarterly_period`: **all untouched** (asserted by test).
- Chain is linear: `164253079db3` (previous head) → … → `f5c9e1b6a348`. One pre-existing fork was avoided by chaining onto the true head.

**Known pre-existing limitation (not introduced here):** migration `3b4c7d9e0f1a` uses constraint ALTER, which the SQLite dialect cannot run — the full chain is Postgres-only (this is why `test_migrations.py` skips its full-chain diff on SQLite). Phase-1's own migrations are SQLite-compatible and round-trip tested from a stamped pre-Phase-1 schema.

## 3. Job types added (existing DB-backed queue — no second queue system)

| Kind | Default schedule | Batch default | Retry policy |
|---|---|---|---|
| `company_universe_sync` | 24h (env-gated) | 500/batch, resumable via `ingestion_runs.stats.next_index` | 3 attempts, 600s base |
| `price_sync` | 5min (env-gated) | 250 stalest-first | 3 attempts, 60s base |
| `historical_price_sync` | 24h (env-gated) | 100 companies/run | 2 attempts, 600s base |
| `failed_data_retry` | 15min (env-gated) | 200 symbols, per-attempt backoff 60s→6h | 3 attempts, 120s base |

All get idempotency keys (queue-level dedupe), leases, dead-lettering, structured logs, per-symbol failure reasons in `ingestion_failures` (transient vs permanent, shared classifier), and `ingestion_runs` records "last successful sync" per kind.

## 4. Environment variables added (all documented in `.env.example`)

```
DATA_PROVIDER=real                      # real | mock — mutually exclusive chains
MOCK_UNIVERSE_SIZE=5000
UNIVERSE_SOURCE=auto                    # auto | mock | nifty500 | full
UNIVERSE_SYNC_INTERVAL_SECONDS=86400    # 0 disables
UNIVERSE_SYNC_BATCH_SIZE=500
PRICE_SYNC_INTERVAL_SECONDS=300         # 0 disables
PRICE_SYNC_BATCH_SIZE=250
HISTORICAL_PRICE_SYNC_INTERVAL_SECONDS=86400
HISTORICAL_PRICE_SYNC_BATCH_SIZE=100
FAILED_RETRY_INTERVAL_SECONDS=900
FAILED_RETRY_MAX_ATTEMPTS=5
PRICE_HISTORY_BACKFILL_DAYS=1825
```
Defaults are conservative; `DATA_PROVIDER` defaults to `real` so an unset variable can never make production data synthetic. Worker concurrency remains `WORKER_CONCURRENCY`.

## 5. Database schema changes
See §2. Row-count plan at 5,000 companies (measured, SQLite): companies 5,000 · financial_facts ~1.15M (23 items × 10y) · market_quotes 5,000 · price_history ~1.25M/yr (~1.83M rows for 365 bars/company; 812 MB SQLite file incl. indices — Postgres with the same rows will be in the same hundreds-of-MB band; volume is dominated by `price_history`, exactly as the audit predicted).

## 6. API changes (additive only; existing contracts untouched)

| Endpoint | Description |
|---|---|
| `GET /api/v1/companies/{id}/quote` | Persisted quote: LTP, prev close, OHLC, volume, change, %change, 52w range, market status, `provider`, `data_kind: mock|real`, `fetched_at`. 404 with an honest message when the sync job has not reached the company. |
| `GET /api/v1/companies/{id}/prices?range=1D|1W|1M|3M|6M|1Y|3Y|5Y|MAX` | Daily OHLCV bars from `price_history`; `granularity: "daily"` stated explicitly; provider provenance labelled. |
| `GET /api/v1/companies/{id}/data-status` | Financial availability: fact count, fiscal years, latest FY, quarterly/shareholding counts, sources, quote presence, price bars, metadata source/sync time. |

Frontend client functions added (`api.quote`, `api.prices`, `api.dataStatus`) — no pages changed, so desktop and the mobile-responsive Financial Analysis UI are unchanged (build + suite verified).

## 7. Test results
- Backend: **3,026 passed, 6 skipped** (65 new Phase-1 tests across 7 files).
- Frontend: `tsc --noEmit` clean; vitest **14/14**; `next build` OK.
- Proofs explicitly required by the brief, and where they live:
  - 5,000 load → `test_company_universe_sync.py::TestFiveThousand`
  - repeated sync no duplicates → same file (`second.inserted == 0`, counts by table)
  - existing IDs retained → `test_existing_500_style_records_survive_a_new_universe`
  - multiple providers, one identity → `test_one_company_multiple_providers_no_duplicate`
  - failed jobs retry correctly → `test_phase1_jobs.py` (backoff → requeue → success; exhaustion → dead-letter)
  - provider failure can't corrupt data → `test_provider_failure_leaves_existing_data_intact`, `test_provider_failure_does_not_corrupt_existing_rows`
  - Redis/mem cache → `test_search_universe.py` (hit ≪ miss), mock-mode serving-cache write-through in `sync.py`
  - quote normalisation / historical upsert idempotency → `test_market_persistence.py`

## 8. 5,000-company deterministic mock pipeline results (SQLite, this sandbox)
| Step | Time | Result |
|---|---|---|
| Universe sync (first) | 13.4s | 5,000 inserted, 0 failed, 0 duplicate identities |
| Universe sync (rerun) | 10.0s | **0 inserted, 0 updated, 5,000 unchanged** — idempotent |
| Mock financial sweep | 216s | 1,150,000 facts; idle re-pass 0.18s (zero work) |
| Price sync (5,000) | 37s | 5,000 quotes (7.4ms/quote); rerun refreshes in place |
| Historical sync (365d) | 382s | 1,825,000 bars; rerun rewrites same rows, no new |
| Search | — | miss ~8.4ms, cached ~0.01ms at 5,000 companies |
| API (TestClient) | — | detail 65ms, quote 6.6ms, prices 7.1ms, data-status 47ms, search 49ms |
| Worker | — | 6.5 jobs/s through the real queue |
| Failed retry | — | 200 symbols resolved in 1.5s |
| DB size | — | 1.5MB → 812MB (facts + 1.8M bars) |

Postgres/Redis numbers must be re-measured on staging with the same script (`DATABASE_URL=… python3 tests/load/phase1_bench.py`); the pg_trgm indexes only exist there.

## 9. Redis cache impact
Quote cache entry ≈ 324 B → 5,000 live quotes ≈ **1.6MB**; statements cache ≈ 3.5KB/company warm → worst-case fully-warm ≈ **18MB**; search entries small and 60s-lived. Well inside the existing single Redis. Memory-backend estimates measured with pickle; on production Redis verify with `redis-cli memory usage "…"`. The provider router's separate 512-entry process cache is intentionally untouched this phase (unifying it is a Phase-4 item).

## 10. Rollback procedure
1. `alembic downgrade -1` five times (or `alembic downgrade 164253079db3`) — each revision's `downgrade()` is tested; all five drop only Phase-1 objects/columns.
2. Jobs: set the four `*_INTERVAL_SECONDS=0` (schedules disable without code) — rolling back code is not required to stop the syncs.
3. Code: revert the Phase-1 commits; the three API endpoints and client functions disappear with no impact on existing pages.
4. Data written by Phase 1 is separable: mock rows carry `metadata_source='mock'` / `provider='mock'` / `source='mock (synthetic)'`; real master rows carry `nse_master`/`bse_master`.
5. No destructive commands are issued by any Phase-1 code path.

## 11. Production deployment commands (NOT executed — for your approval)
```bash
# 0) backup first (existing backup job or operator snapshot)
# 1) migrations against the DISPOSABLE/staging DB, then production:
cd backend
DATABASE_URL=postgresql+psycopg://… alembic upgrade head
# 2) verify:
DATABASE_URL=… python3 -m pytest tests/test_phase1_migrations.py   # or run on staging CI
# 3) deploy api + worker images; enable schedules (defaults already conservative)
# 4) seed the real universe in bounded batches (operator):
#    POST /api/v1/platform/jobs {kind: company_universe_sync} (requires JOB_MANAGE)
# 5) re-run the benchmark against staging Postgres and record the numbers
```

## 12. Known limitations
- **5-year history at full universe** was benchmarked at 365 days in this sandbox (~6 min on SQLite). 5y ≈ 6.2M bars: schedule `historical_price_sync` nightly and let the batch walk finish over several runs (by design), or temporarily raise `HISTORICAL_PRICE_SYNC_BATCH_SIZE`.
- `1D` prices return the latest *daily* bar; intraday granularity requires a licensed feed (Phase 6).
- The mock financial generator writes 23 canonical items (P&L/BS/CF consistent); it does not populate `quarterly_results`/`shareholding_snapshots` — those come from the real screener path (Phase 2 scope).
- `f5c9e1b6a348` trigram indexes are Postgres-only and unexercised here (sandbox has no Postgres); staging run required.
- Quote sync staleness ordering uses `fetched_at` ascending; with `PRICE_SYNC_INTERVAL=300` and batch 250, a full 5,000 universe fully refreshes every ~60min worst-case (tune interval/batch as desired).

## 13. Data-provider licensing concerns
- `DATA_PROVIDER=real` chain remains Finnhub/FMP (US + fallback) and **Yahoo Finance for Indian quotes — an unofficial source**. It is suitable for internal/fallback use, NOT for redistributing real-time Indian market data to public users. A licensed NSE/BSE-authorised vendor feed is Phase 6; the provider registry already reserves slots (Infoway, AlphaVantage, Polygon, Custom).
- `DATA_PROVIDER=mock` is unmistakably labelled end-to-end (`provider='mock'`, `data_kind:'mock'`, `INM`/`MCK`/`9xxxxx` identifier bands) and can never be written while the real chain is selected, or vice-versa.
- Data labelling everywhere: REAL = provider ≠ mock · MOCK = provider starts with `mock` · CACHED = Redis/platform cache hit (serving layer) · HISTORICAL = `price_history` bars with per-bar provider.
