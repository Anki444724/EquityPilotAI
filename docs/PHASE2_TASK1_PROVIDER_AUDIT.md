# Phase 2 — Task 1: Real-Provider Financial Data Pipeline Audit

**Read-only audit.** No code, schema, migration, frontend or deployment was changed. Every claim below cites the exact file and function/class it comes from. Each section explains the issue **in simple language first**, then gives the technical detail.

---

## The one-paragraph story

EquityPilotAI gets its real financial data by **reading Screener.in's public company pages** (annual P&L, balance sheet, cash flow, ratios, quarterly results, shareholding) and **optionally adding extra detail from Yahoo Finance** (expense breakdown, price history). Screener is the boss: when both report the same number, Screener wins. There is no paid/licensed financial feed wired in today — the market-data provider abstraction (Finnhub/FMP/Yahoo tiers) is used for **quotes and US companies**, not for Indian financial statements. A background job (`financials_backfill`) walks companies that have no history yet and ingests them in small, throttled batches; quarterly/shareholding backfill exists but is **only run by hand** from an operator script.

```
companies.ticker (NSE symbol)
   │  SLUG_ALIASES (3 manual renames)          app/data/screener_source.py
   ▼
fetch_screener() ── consolidated page ── fallbacks ─► standalone page
   │  (P&L / BS / CF / ratios / quarters / shareholding, ₹ crore)
   ▼
ingest_company()  ── canonicalise() ── merge (screener wins) ── _upsert_facts()
   │                 app/data/ingest.py                    financial_facts (upsert, Phase 1)
   └─► enrich_universe() (Yahoo adds missing detail only)   app/data/enrich.py
   └─► PeriodicBackfillService (quarterly + shareholding)   app/services/universe/periodic_backfill.py
        ─ driven only by deploy/backfill_periodic.py (manual)
```

---

## 1. Existing provider implementations

**Simple:** There are three real sources of financial data: Screener.in (the main one), Yahoo Finance (fills gaps), and FMP (only for US companies). A fourth "provider" is the offline mock used for testing.

**Technical:**
| Source | File | Role |
|---|---|---|
| Screener.in | `app/data/screener_source.py` → `fetch_screener(ticker, consolidated=True)` | Primary Indian financial source. Scrapes the company page's HTML tables (`_parse_section`, `_parse_period_section`) into `ScreenerFinancials` (P&L, BS, CF, ratios, quarters, shareholding, banner summary). Consolidated-first with three documented fallbacks (see §6). |
| Yahoo Finance | `app/data/yahoo_source.py` → `fetch_financials()`, `fetch_price_history()`, `_fetch_quote()` | Supplementary detail (`YAHOO_DETAIL` list in `app/data/ingest.py`), price + daily closes. Consumed by `app/data/enrich.py` and the market tier `app/data/providers/yahoo.py`. |
| FMP / Finnhub | `app/data/providers/fmp.py`, `finnhub.py`, `app/services/us_pipeline/` | US listings only (provisioned on demand, 3 companies in production). Not used for Indian statements. |
| Mock | `app/data/providers/mock.py`, `app/data/mock_financials.py` | Phase-1 deterministic offline provider; **mutually exclusive with the real chain** via `DATA_PROVIDER` (`app/data/providers/router.py::default_providers`). |

**Key finding:** the financial sources do **not** implement the provider interface used for market data (`BaseMarketProvider`, `app/data/providers/base.py`). Screener and Yahoo-financials predate that abstraction and are plain modules with their own throttling. There is no `FinancialDataProvider` interface to plug a licensed feed into yet — that is Phase-2 work.

## 2. Provider interface / base classes

**Simple:** Quotes have a proper "universal socket" any vendor can plug into. Financial statements don't — each source is hand-wired.

**Technical:** `BaseMarketProvider` (`app/data/providers/base.py`) provides `fetch()`, shared `RetryPolicy` (attempts, exponential backoff, `min_interval` throttle, circuit threshold), typed errors (`ProviderAuthError` never retried; `ProviderRateLimited`; `SymbolNotFound`), and since Phase 1 the narrow `fetch_quote()` / `fetch_history()` hooks. Only Finnhub/FMP/Yahoo/Mock implement it. Screener/Yahoo-financial modules each implement their own throttle+retry inline (`screener_source._fetch`, `yahoo_source._http_json`) — duplicated policy, but battle-tested.

## 3. Provider router and fallback logic

**Simple:** For prices, the system tries vendors in order and tells you which one answered. For financial statements there is no router — Screener is simply the source, with careful in-page fallbacks.

**Technical:** Market quotes: `MarketDataRouter` (`app/data/providers/router.py`) — external tiers by priority → internal DB tier → documents tier; every response names its source; `DATA_PROVIDER=mock|real` selects the whole chain (`default_providers()`). Financials: the "router" is the merge policy in `app/data/ingest.py::ingest_company` — screener canonicalised first; Yahoo merged only where screener is silent ("screener wins on any line both report"; cross-source disagreements >2% are recorded, not averaged, `SourceDisagreement`). Quote serving for pages never blocks on providers: `app/services/live_market.py::LiveMarketService.bulk_quotes` (cache → stored price → background refresh queue).

## 4. Current financial-facts ingestion flow

**Simple:** One company at a time: download its Screener page, translate the rows into the platform's 54 standard line items, then save with "update-if-changed" logic so re-running never duplicates.

**Technical:** `app/data/ingest.py::ingest_company()` → `fetch_screener()` → `canonicalise()` (incl. the bank/NBFC *financing layout* handling — misreading it produced a −₹268,944 cr HDFC Bank PAT before this existed) → `_upsert_facts()` (Phase 1: ON CONFLICT upsert on `(company_id, fiscal_year, line_item, precedence)`; unchanged rows untouched, `data_version` bumps only on value change; rows the source no longer reports are **retained**) → `_write_fact_version()` only when something changed → updates `company.current_price/shares_outstanding/market_cap`. Driver: `FinancialsBackfillService` (`app/services/universe/financials_backfill.py`) — DB-driven target selection (companies lacking ≥2 fiscal years, delisted excluded, largecaps first), sweep bounded at 25/run (`DEFAULT_SWEEP_LIMIT`), 0.4s inter-company delay (`DEFAULT_DELAY_SECONDS`), per-company failure classification (`classify_ingest_failure`: 429/timeout/network = transient; 404/no-data = permanent), `TransientIngestionFailure` escalates to the job's bounded retry. Job handler: `app/services/platform/jobs/handlers.py::handle_financials_backfill` (mock mode delegates to `_mock_financials_sweep`).

## 5. Company → provider symbol mapping

**Simple:** The website's ticker is passed to Screener almost unchanged, and to Yahoo with a suffix like `.NS`. Only three renamed companies have a manual translation table, and the "is this Indian?" test only knows the original ~136 tickers.

**Technical:**
- Screener: `companies.ticker` → slug via `SLUG_ALIASES` in `app/data/screener_source.py` (exactly 3 entries: LTIM→MINDTREE, TATAMOTORS→TMPV, ZOMATO→ETERNAL, each a real corporate action). No alias discovery mechanism — a renamed company silently 404s ("not listed") until someone adds it by hand.
- Yahoo/market: `app/data/providers/symbols.py::resolve()` appends `.NS`/`.BO` only when the bare symbol is in `_indian_universe()` — which is the **hard-coded 136-entry `NSE_UNIVERSE` tuple** (`app/data/nse_universe.py`), not the database universe. The same 136-tuple drives `normalise_symbol()` in `app/data/providers/base.py`. Phase-1's 5,000-company universe is invisible to this resolver (mock provider strips suffixes, so mock mode never noticed).
- BSE-only companies (Phase-1 `full` source creates them with `ticker=<scrip code>`): untested against Screener's URL scheme and unmapped for Yahoo — no evidence either works today.

## 6. Annual financial data

**Simple:** Fully working and proven at 500 companies in production — 12 years of history, in ₹ crore, consolidated with a smart fallback to standalone when needed.

**Technical:** `fetch_screener()` parses the four annual sections (`_parse_section` for `profit-loss`, `balance-sheet`, `cash-flow`, `ratios`), year headers via `_fiscal_year()` ("Mar 2025"→2025). Fallback ladder, all recorded as warnings: (a) consolidated page empty → standalone; (b) consolidated page stale (≤2 years vs a longer standalone series, e.g. GVT&D's leftover Dec-2010 column) → standalone; (c) consolidated quarterly block a stub (e.g. COLPAL, AUBANK) → quarterly rows from the standalone page only. Production result: 503/503 active companies covered, 134,700 facts (`docs/FINANCIAL_INGESTION_REPORT.md`). Yahoo enrichment adds only items screener lacks (`app/data/enrich.py`, never overwrites; `Precedence.STORE`, `source='yahoo_finance'`).

## 7. Quarterly financial data

**Simple:** Collected and stored correctly — but only when someone runs a script by hand. No schedule runs it.

**Technical:** `_parse_period_section()` + `_fiscal_period()` in `screener_source.py` key columns by `(fiscal_year, quarter)` (Indian FY: Jun=Q1 … Mar=Q4 — the docstring records why a year-only key silently loses 12 of 13 columns). `PeriodicBackfillService._write_quarters()` (`app/services/universe/periodic_backfill.py`) maps rows via `QUARTER_MAP` (operating vs *financing* layout both handled), converts percent→fraction, honours the no-placeholder rule, and upserts by read-modify-write on `uq_quarterly_period`. **The only driver is `deploy/backfill_periodic.py`** — no `JobKind`, no schedule, no entry in `ingestion_failures`. Production holds 6,319 quarterly rows from past manual runs.

## 8. Shareholding data

**Simple:** The coarse split (promoters, FIIs, DIIs, government, public) is captured quarterly. The fine SEBI categories (mutual funds vs insurance vs banks, promoter pledged %) are **not** available from this source and stay empty rather than being guessed.

**Technical:** `SHAREHOLDING_MAP` in `periodic_backfill.py` maps screener's five rows into `ShareholdingSnapshot` — DIIs land combined in `banks_fis_aif`; `mutual_funds`, `insurance`, `promoter_pledged` are left at defaults (documented as not derivable). Stored as fractions; unique per `(company, FY, quarter)`. Same manual-only driver as §7. 4,753 rows in production.

## 9. Data freshness / fetched_at handling

**Simple:** Newly saved numbers are stamped with *when* they were fetched and *how often* they changed. But two gaps: a whole class of numbers (quarterly/shareholding) has no timestamp at all, and **already-covered companies are never re-checked for new results** — the job only looks for companies with no history.

**Technical:**
- Present: `financial_facts.fetched_at` / `data_version` / `consolidated` (Phase-1 migration `c2f6b8e3d015`), set by `_upsert_facts()`; `companies.metadata_synced_at` (universe); `market_quotes.fetched_at`; `ingestion_runs` records each sweep.
- Gap A: `QuarterlyResult` and `ShareholdingSnapshot` (`app/models/analysis.py`) have **no fetched_at/source-refresh columns** (`QuarterlyResult.source` exists; shareholding has none) — you cannot tell a 2019-vintage shareholding row from a fresh one.
- Gap B: `FinancialsBackfillService` targets only companies *without* a usable history (`MIN_USEFUL_YEARS = 2`); there is **no scheduled refresh for already-covered companies**, so a new FY2026 annual report is never ingested automatically. Freshness today is whatever the operator manually re-runs.
- Gap C: rows written by `enrich.py` are ORM inserts that never set `fetched_at` (stays NULL) — provenance timestamp missing on the Yahoo-added detail.

## 10. Error handling, retry and rate limiting

**Simple:** The system is polite to the websites it reads (forced pauses that lengthen when told to slow down), one bad company never stops the batch, and failed background jobs retry with growing delays before giving up. Financial-sync failures, however, are not yet filed into the Phase-1 "failed symbols" table that the retry job reads.

**Technical:**
- Screener (`screener_source._fetch`): global `MIN_INTERVAL = 1.1s`; on HTTP 429 the interval widens ×1.5 up to 8s and sleeps `6s × attempt`; 3 retries with exponential backoff; 30s timeout; 404 → permanent `ScreenerError("not listed")`.
- Yahoo-financial (`yahoo_source._http_json`): `MIN_INTERVAL = 4.0s` widening to 10s; consecutive-429 circuit breaker (`_consecutive_429`, `provider_available()`, `reset_circuit()`); `enrich_universe` treats an open circuit as a 75s cooldown × up to 8, then resumes ("a slow pass beats a stopped one").
- Job level: per-kind `RetryPolicy` with deterministic jittered exponential backoff, dead-letter after max attempts (`app/domain/platform/jobs.py`, `app/services/platform/jobs/queue.py::fail/requeue_ready`); `TransientIngestionFailure` bridges per-company transient errors into job retries.
- Gap: `handle_financials_backfill` returns `failure_reasons` in its job result but does **not** write `ingestion_failures` rows (`app/models/ingestion.py`), so the Phase-1 `failed_data_retry` job cannot pick up financial failures; only universe/price/history failures are filed.

## 11. Existing Redis / database caching

**Simple:** Frequently-read pages are served from a short-lived cache so the database isn't hammered. One subtle bug: after fresh numbers are ingested, the cache can keep serving the old numbers for up to an hour because it isn't told to forget them.

**Technical:** `app/services/platform/cache.py` — namespaces `market`(300s) / `statements`(3600s) / `news`(900s) / `rag`(1800s) / `search`(60s, Phase 1); memory↔Redis with graceful fallback. Canonical financials are cached by **`company_id` alone** (`company_service.load_financials` → `cache.get_or_set(Namespace.STATEMENTS, …, company_id)`). `ingest_company` and `enrich` bump `companies.data_version` (whose model docstring says cache keys invalidate atomically) but **never call `cache.invalidate(Namespace.STATEMENTS, company_id)`** and the key contains no version — so a re-ingest leaves stale statements cached up to the 1h TTL. (Admin edits and US provisioning *do* invalidate: `financial_admin_service.py`, `us_pipeline/provisioning.py`.) Additionally, `app/data/providers/router.py` keeps its own in-process 512-entry `TTLCache` separate from the platform cache (known Phase-4 unification item).

## 12. Existing 5,000-company sync jobs

**Simple:** Four new background jobs from Phase 1 keep the company list, live prices, and price history up to date, in small batches, on schedules you can configure. The financial job is the old one, which only fills *empty* companies.

**Technical:** `app/domain/platform/jobs.py` kinds `COMPANY_UNIVERSE_SYNC`, `PRICE_SYNC`, `HISTORICAL_PRICE_SYNC`, `FAILED_DATA_RETRY`; env-gated schedules in `app/services/platform/jobs/worker.py::_phase1_schedules` (intervals/batches in `app/core/config.py`, 0 disables). Universe source resolution `app/services/universe/company_universe.py::resolve_source` follows `DATA_PROVIDER`; the real `full` source (`full_market_records()`) fetches NSE's `EQUITY_L.csv` + BSE scrip master joined on ISIN — **written in Phase 1 but never executed against the live exchanges** (sandbox had no access; the Nifty-500 variant is the battle-tested one). `FINANCIALS_BACKFILL` remains the financial sync; its real path is unchanged 25-company sweeps.

## 13. What is production-ready today

- **Screener annual pipeline** — proven at 500 companies, 100% coverage, bank/NBFC layouts, three consolidated/standalone fallbacks, corporate-action slug map, adaptive throttling (`app/data/screener_source.py`, `app/data/ingest.py`).
- **Upsert persistence** — Phase-1 idempotent `_upsert_facts` + version snapshots, verified on PostgreSQL.
- **Universe identity + sync** — ISIN-first ladder, resumable batches, integrity-proven at 5,500 rows (`app/services/universe/company_universe.py`).
- **Job queue, retries, dead-letter, observability** (`ingestion_runs`/`ingestion_failures` for universe/price/history).
- **Quote + OHLC persistence and the mock provider** (Phase-1 verified).
- **Quarterly/shareholding backfill logic** — correct and idempotent, but manual-only (see below).

## 14. What is missing for 500 → 5,000

1. **No financial-provider abstraction**: nothing a licensed feed can implement; screener/yahoo-financial bypass `BaseMarketProvider` and are invisible to `DATA_PROVIDER` (only the *job handler* is gated — calling `FinancialsBackfillService` directly, e.g. via the admin backfill API, still hits Screener in mock mode).
2. **Symbol mapping stops at 136**: `symbols._indian_universe()` / `base.normalise_symbol` use the hard-coded `NSE_UNIVERSE`; BSE-only tickers and the 5,000-company universe are unmapped; `SLUG_ALIASES` is manual with no failure-driven discovery.
3. **Throughput arithmetic**: screener's 1.1s floor + 0.4s delay ≈ 1.5–2s/company → a cold 5,000-company ingest is ~2–3 hours; the scheduled sweep does **25/day** → ~200 days. Sweep size is env-tunable but there is no "warm the universe in chunks then settle to 25" runbook.
4. **No refresh path for covered companies** (§9 Gap B) and **no schedule/failure-tracking for quarterly + shareholding** (§7, §8, §10 gap).
5. **Cache invalidation gap on ingest** (§11) and **fetched_at gaps** (§9 A/C).

---

## What already works
Annual statements for the whole current universe from Screener (consolidated-first, bank-aware, upsert-idempotent, provenance-labelled), Yahoo detail enrichment that never overwrites, quarterly + shareholding capture with correct Indian fiscal keys, adaptive rate-limit handling on both sources, a production job queue with retries/dead-letter, Phase-1 universe/quote/history sync jobs, and the offline mock twin of the market pipeline.

## What is missing
A pluggable financial-provider interface (for a licensed feed); DB-backed symbol resolution + BSE slug handling; scheduled quarterly/shareholding sync wired into `ingestion_failures`; a freshness/refresh sweep for already-covered companies; statements-cache invalidation on ingest; `fetched_at` on quarterly/shareholding/enrich rows; a chunked warm-up runbook (and live verification of the NSE `EQUITY_L.csv` full-universe fetch).

## Top 5 risks
1. **Terms-of-service / licensing on Screener scraping at 10× volume** — the pipeline depends on one unofficial source with no contractual right; sustained 5,000-company scraping raises blocking and legal exposure (the audit's Phase-0 note stands: licensed feed is the endgame).
2. **Silent coverage holes from symbol drift** — renames/demergers outside the 3-entry `SLUG_ALIASES` and any BSE-only listing produce "not listed" (permanent) failures that are recorded but easy to miss at 5,000 symbols.
3. **Stale-cache wrong numbers** — §11: a user can see up-to-1-hour-old statements after a refresh ingest; for a product making accuracy claims this is the highest-severity *correctness* gap found.
4. **Freshness illusion** — nothing re-ingests covered companies (§9 Gap B); pages will quietly age as new results are published.
5. **Throughput/fragility of a single scrape worker** — ~2–3h minimum per full pass, one IP, one process; a single NSE/screener behaviour change (HTML shape) breaks parsing for the entire universe with only per-company errors as the signal.

## Recommended next small task
**"Financial refresh + cache correctness" (Phase 2, Step 1):** make the existing pipeline trustworthy at any scale before adding any provider: (a) invalidate `Namespace.STATEMENTS` for a company inside `ingest_company`/`enrich` (or put `data_version` in the cache key); (b) add a *refresh* mode to `FinancialsBackfillService` that re-visits covered companies whose newest fiscal year is older than the current FY, bounded by the same sweep/batch/throttle machinery; (c) wire financial and periodic backfill failures into `ingestion_failures` so `failed_data_retry` covers them. No schema change required; no new provider; behaviour verifiable with the existing mock + tests.

## Exact files the next task should modify
1. `backend/app/data/ingest.py` — `ingest_company()` (cache invalidation; failure surfacing).
2. `backend/app/data/enrich.py` — `enrich_*` (cache invalidation; set `fetched_at`).
3. `backend/app/services/universe/financials_backfill.py` — `FinancialsBackfillService` (stale-freshness target selection; ingestion-failure recording).
4. `backend/app/services/universe/periodic_backfill.py` — failure recording (+ optional job-kind in step 2).
5. `backend/app/services/platform/jobs/handlers.py` — `handle_financials_backfill` (refresh-mode payload; failure filing).
6. `backend/app/services/company_service.py` — `load_financials()` cache key (version-aware) if the key approach is chosen over explicit invalidation.
7. `backend/tests/test_financials_backfill.py`, `backend/tests/test_ingest_provenance.py` — regression coverage for the above.
*(No migrations, no frontend, no provider changes in this step.)*

---
*Audit only — no implementation was started. Awaiting direction.*
