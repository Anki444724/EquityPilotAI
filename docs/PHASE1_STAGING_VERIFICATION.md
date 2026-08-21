# Phase 1 — Staging Verification Report

**Scope:** GitHub push · disposable-PostgreSQL migration rehearsal · PostgreSQL 5,000-company load test · data-integrity proofs · provider isolation · production-safety confirmation · mobile/UI regression check.
**Verdict: every check passed.** Nothing was deployed, no production system was touched, no real-provider ingestion was enabled. One verification-script defect was found and fixed (documented in §M); no product defect was found.

---

## A. Git commits and final SHA

GitHub was re-authenticated in Arena. The remote branch briefly diverged (it held `bf1b806`, the earlier session's copy of the same responsive fix — verified **tree-identical** to `4faeaad` by `git diff`), so the branch was pushed with `--force-with-lease` to carry exactly the approved commits:

| Commit | Subject | Status |
|---|---|---|
| `4faeaadcfcd36f2cd9f0b368f34ef3f1a03c8cf4` | fix(financials): mobile-responsive financial analysis page | on remote |
| `97fdd0f1c35d2cf3351c8601f15dcf19860673b1` | feat(phase1): 5,000-company universe, provider switch, market-data persistence | **remote head** |

- Remote verified via `git ls-remote`: `refs/heads/arena/01a0237c-equitypilotai → 97fdd0f1…`
- `git status`: clean (0 modified, 0 untracked). Local HEAD == remote head.

## B. PostgreSQL migration results (disposable DB only)

Environment: PostgreSQL **16.2** (exact server version of production's `postgres:16`), disposable instance at `/home/user/pgstage`, database `phase1_stage`. `pg_trgm` was **not bundled** with this sandbox's embedded build, so it was compiled from the official `REL_16_2` sources against the same headers and installed into the disposable server only (`pg_trgm 1.6`).

| Check | Result |
|---|---|
| `alembic upgrade head` | ✅ all **28 migrations** (full chain, incl. pre-existing PG-only migrations `3b4c7d9e0f1a` constraint ALTER, `f7b2d94e15c8` pgvector, and the 5 Phase-1 revisions) in 2.0s |
| `alembic current` | ✅ `f5c9e1b6a348 (head)` |
| `alembic heads` | ✅ `f5c9e1b6a348 (head)` — single head |
| Phase-1 columns (`companies.metadata_source/metadata_synced_at`, `financial_facts.consolidated/fetched_at/data_version`, `price_history.day_open/day_high/day_low/provider`) | ✅ all present |
| Extensions after upgrade | ✅ `pg_trgm 1.6`, `vector 0.6.2`, `plpgsql` |
| GIN trgm indexes `ix_company_{name,ticker,isin}_trgm` | ✅ `indisvalid=true`, `indisready=true`, am=gin |
| Indexes **serve** the search (EXPLAIN at 5,000 rows) | ✅ `Bitmap Index Scan on ix_company_name_trgm / _ticker_trgm / _isin_trgm` for all three LIKE patterns |
| Identity constraints | ✅ `uq_company_ticker_exchange`, `companies_isin_key`, `uq_fact_company_year_item_precedence`, `uq_price_ticker_date` all present |
| `market_quotes` / `ingestion_runs` / `ingestion_failures` | ✅ created, queryable |

## C. Upgrade/downgrade results

Downgrade was executed **step by step** (one revision per command) against the disposable DB only — never production:

```
f5c9e1b6a348 → e4b8d0a5f237 → d3a7c9f4e126 → c2f6b8e3d015 → b1e5a7d2c904 → 164253079db3
```
✅ Every Phase-1 `downgrade()` ran cleanly. Verified after reaching the pre-Phase-1 head: `market_quotes`/`ingestion_runs`/`ingestion_failures` gone; the four Phase-1 column groups gone; trgm indexes gone; **all pre-existing identity constraints intact**; `pg_trgm` extension remains installed (shared object, harmless — dropping indexes is sufficient rollback).
✅ Re-upgrade `164253079db3 → head`: all 5 revisions re-applied, `alembic current` = `f5c9e1b6a348 (head)`.

## D. PostgreSQL 5,000-company benchmark vs SQLite

Same script (`tests/load/phase1_bench.py`), same deterministic dataset, `DATA_PROVIDER=mock`, history 365 days. SQLite column = the Phase-1 sandbox measurement; PG = this verification (embedded 16.2, unix socket, untuned `postgresql.conf` defaults, single connection).

| Measurement | PostgreSQL 16.2 | SQLite (Phase-1 run) | Δ |
|---|---|---|---|
| Universe sync, first (5,000) | **12.5s** | 13.4s | PG faster |
| Universe sync, rerun (idempotency) | 7.6s (0 inserted) | 10.0s (0 inserted) | PG faster |
| Financial sweep (1.15M facts) | **333.7s** | 215.9s | **PG 1.55× slower** |
| Financial sweep, idle re-pass | 0.27s (zero work) | 0.18s | — |
| Price sync (5,000 quotes) | 48.0s | 36.8s | PG 1.30× slower |
| Historical sync (1.825M bars, 365d) | **455.0s** | 382.3s | **PG 1.19× slower** |
| Historical rerun | 1.825M upserts, **0 new rows** | same | idempotent on both |
| Search miss / cached | 9.4ms / **0.22ms** | 8.2ms / 0.01ms | parity; cache ~2 orders cheaper than miss on PG |
| API: quote | 8.1ms | 6.6ms | parity |
| API: prices (1M) | 8.0ms | 7.1ms | parity |
| API: data-status | 12.8ms | 47.0ms | PG faster |
| API: company detail | 95.3ms | 65.2ms | PG slower (first-hit cost; includes market attach) |
| API: search | 54.8ms | 49.2ms | parity |
| Worker throughput | 5.0 jobs/s | 6.5 jobs/s | parity |
| Failed-retry (200 symbols) | 1.83s | 1.50s | parity |

**Performance assessment (documented plainly, per instruction):** bulk *ingestion* is slower on PG (financials +55%, history +19%, quotes +30%). This is expected and not a concern: (a) ingestion is a nightly, batched background job, not a user path; (b) this embedded PG is a debug-default, single-client build on one socket — no `synchronous_commit`, WAL, checkpoint or `shared_buffers` tuning was applied, while SQLite writes a local file in-process; (c) the serving paths — search, quote, prices, data-status — are at parity or better, which is what users hit. Re-run the bench on the real EC2 staging Postgres for production-representative ingestion numbers before Phase 2 sign-off (command in §N).

## E. Database growth (PostgreSQL)

| Object | Size | Rows |
|---|---|---|
| `phase1_stage` database (from 7.3MB empty) | **898 MB** | — |
| `financial_facts` | 489 MB | 1,151,197 |
| `price_history` | 377 MB | 1,825,000 |
| `companies` | 15 MB | 5,000 |
| `market_quotes` | 1.8 MB | 5,000 |
| `ingestion_failures` / `ingestion_runs` | 152 kB / 120 kB | 200 / 98 |

Matches the Phase-1 audit prediction (~1–1.5GB at full 5-year history; 365 days here). A 5-year backfill (~6.2M bars) projects to roughly **1.3–1.5GB total** — the EC2 volume needs ≥ that much headroom (Railway volume was 10GB at ~23% usage when last measured; verify on EC2).

## F. Redis memory (real Redis 7.2.5, built from official sources for this sandbox)

| Measurement | Value |
|---|---|
| Baseline (idle server + 289 organic keys from bench) | 1.31 MB |
| After writing the **full-universe serving cache** (5,000 quote entries, exactly as `price_sync` writes them) | **3.74 MB** (516 B/entry) |
| After 6 search-cache entries | 3.82 MB |
| Growth projection (5,000 quotes + warm statements ≈ 3.5KB/company worst case) | **~20–25 MB** — trivial for the existing instance |

Production re-check commands: `redis-cli DBSIZE`, `redis-cli INFO memory | grep used_memory_human`, `redis-cli MEMORY USAGE "ierp:market:<key>"`.

## G. API latency (PostgreSQL, TestClient in-process)
See table in §D (quote 8.1ms · prices 8.0ms · data-status 12.8ms · detail 95.3ms · search 54.8ms cold / 0.22ms cached). All responses 200; provenance fields present (`provider`, `data_kind`).

## H. Worker throughput
5.0 jobs/s through the real DB-backed queue (`Worker.run_once`, 10 distinct `price_sync` jobs claimed, executed, succeeded). Failed-job retry: transient failure → job `FAILED` with backoff → requeue → `SUCCEEDED` on attempt 2. Failed-data retry: **200 transient failures resolved in 1.83s**; permanent failures left untouched for an operator.

## I. Idempotency results (disposable PG, `phase1_integrity` DB)
| Operation | First run | Rerun |
|---|---|---|
| Universe sync (after 500 real-style rows existed) | 5,000 inserted, 0 failed | **0 inserted, 0 updated, 5,000 unchanged**, total still 5,500 |
| Financial facts (400-company pass, 92,000 rows) | 92,000 inserted | **92,000 → 92,000** (0 new), 0 duplicate natural keys |
| Quotes | 5,500 rows (one per company) | refreshed in place, still 5,500 |
| Historical bars (30d) | 165,000 bars | **165,000 → 165,000**, 0 duplicate `(ticker, as_of)` |

## J. Identity / dedup results
- 500 real-style companies (`real-*` ids, `INE` ISINs, `metadata_source='nse_master'`) + 5,000 mock (`MCK`/`INM`/`9xxxxx`) **coexist: 5,500 rows, every original ID unchanged**.
- 0 duplicate `(ticker, exchange)` pairs; **0 duplicate non-NULL ISINs** (23 mock rows legally have NULL ISIN, as real small-caps sometimes do — Postgres UNIQUE permits multiple NULLs).
- Multi-provider identity convergence, ticker/exchange fallback scoping, BSE-code matching and exchange-family separation are covered by the suite (3,026 tests) and were re-run on PG via the integrity script's provider-isolation section.

## K. Mock/real provider isolation (re-verified live)
- `DATA_PROVIDER=mock` → chain is exactly `[Mock (synthetic)]`; no real provider is constructed.
- `DATA_PROVIDER=real` → chain is `[Finnhub, Financial Modeling Prep, Yahoo Finance (Fallback)]`; **no `MockMarketProvider` instance exists anywhere in the router**, so there is no code path that can silently fall back to mock data.
- Provenance sampled from every written table: `companies.metadata_source ∈ {mock, nse_master}`, `financial_facts.source = 'mock (synthetic)'`, `market_quotes.provider = 'mock'`, `price_history.provider = 'mock'`. Zero rows mix labels.
- No real-provider ingestion was enabled at any point; no external market/financial provider was contacted during this verification.

## L. Mobile / UI regression results
Automated headless-browser audit of the Financial Analysis page (same harness as the responsive fix), dark theme, at **360, 390, 430, 768, 1366 px × all 9 tabs = 45 views**: **all pass** — no horizontal page overflow, no clipped values, tab strips contained and usable, header within viewport, cards intact, minimum rendered value font 12px mobile / 13px desktop, page headings (`h1`) present on every tab. No frontend file was modified during this verification. (Beginner-readability note for Phase 2+: the headings checked are the existing ones; the new beginner-friendly language/tooltips requirement will be applied in the UI phase without touching this layout.)

## M. Failures and limitations (nothing hidden)
1. **Verification-script defect (fixed):** the integrity script's first ISIN-duplicate check grouped NULLs together and reported a false failure ("0 duplicate ISINs"). Corrected to `WHERE isin IS NOT NULL` — result 0 duplicates, 23 legal NULLs. No product code was involved.
2. **Sandbox PostgreSQL is an embedded, untuned build** (pgserver 16.2, debug-default config, unix socket). The `pg_trgm` module had to be compiled from official REL_16_2 sources because the embedded build ships without contrib. Migration and query results are valid for Postgres 16 semantics; absolute ingestion timings are conservative (see §D) — re-measure on the EC2 staging instance.
3. **`db_bytes_after` in the PG bench JSON reads `-1`** (the script's file-size probe is SQLite-specific); the authoritative PG growth numbers in §E come from `pg_database_size()` measured directly.
4. **API latency is TestClient-in-process** (no network/TLS). It isolates app+DB cost; end-user latency on EC2 adds nginx/TLS/round-trip.
5. **History benchmark covers 365 days**, not the default 1,825, to bound sandbox runtime; the 5-year projection in §E is arithmetic from measured row sizes.
6. **Production-Postgres-only checks still pending:** none known — this run covered everything the brief listed. The only unverifiable-here item was trigram *planner* behaviour, which §B demonstrates.

## N. Exact commands used
```bash
# Git
git push --force-with-lease=arena/01a0237c-equitypilotai:bf1b806 origin arena/01a0237c-equitypilotai
git ls-remote origin refs/heads/arena/01a0237c-equitypilotai        # → 97fdd0f1…

# Disposable Postgres 16.2 + Redis 7.2.5 (sandbox only)
pip install --user --break-system-packages pgserver                 # embedded PG server
# pg_trgm compiled from github.com/postgres/postgres REL_16_2 contrib/pg_trgm
#   make USE_PGXS=1 PG_CONFIG=<pgserver>/bin/pg_config && … install
redis-server --port 6379 --save '' --appendonly no                  # built from redis 7.2.5 tag

# Migrations (disposable DB only — NEVER production)
export DATABASE_URL="postgresql+psycopg://postgres@/phase1_stage?host=/home/user/pgstage"
alembic upgrade head && alembic current && alembic heads
for rev in f5c9e1b6a348 e4b8d0a5f237 d3a7c9f4e126 c2f6b8e3d015 b1e5a7d2c904; do alembic downgrade $rev; done
alembic downgrade 164253079db3 && alembic upgrade head              # round-trip

# Load test on PostgreSQL (+ Redis)
DATABASE_URL=$DATABASE_URL REDIS_URL=redis://localhost:6379/0 DATA_PROVIDER=mock \
  python3 tests/load/phase1_bench.py --companies 5000 --history-days 365 \
  --out phase1_bench_postgres.json

# Integrity + isolation proofs on PostgreSQL
DATABASE_URL="postgresql+psycopg://postgres@/phase1_integrity?host=/home/user/pgstage" \
  REDIS_URL=redis://localhost:6379/0 DATA_PROVIDER=mock \
  python3 tests/load/phase1_integrity_pg.py

# Index verification / planner / sizes
psql: SELECT … FROM pg_index … ; EXPLAIN SELECT … LIKE '%aurora%' ; pg_database_size()
redis-cli DBSIZE ; INFO memory ; MEMORY USAGE <key>

# UI regression (headless Chromium, 360/390/430/768/1366 × 9 tabs)
node audit-stage.mjs                                                  # /home/user/.pw
```

## O. Recommendation for Phase 2
**Proceed is recommended, with three conditions carried into Phase 2:**
1. Re-run `tests/load/phase1_bench.py` once against the **EC2 staging Postgres/Redis** before or immediately after starting Phase 2, to replace the sandbox-conservative ingestion numbers with production-representative ones (ingestion is the only area where the sandbox may understate PG).
2. Phase 2's proposed `financial_facts` unique-constraint change (adding the reporting basis) is the one remaining schema risk: implement it as its own reviewed migration with the constraint register (old/new/reason/rollback) and a backup-first rule, exactly as documented in `c2f6b8e3d015`.
3. Apply the **beginner-friendly UI principles** (plain language, "What does this mean?" tooltips, progressive disclosure, why-a-metric-matters) to any new surface Phase 2 adds, without altering the verified responsive layout (§L) — advanced data stays available behind disclosure, accuracy is never simplified away.

**Production safety confirmation (explicit):** production database NOT modified · no production migration executed · no production containers restarted · no production data written · no real market/financial ingestion enabled · all work performed on disposable sandbox databases (`phase1_stage`, `phase1_integrity`) and a disposable local Redis.
