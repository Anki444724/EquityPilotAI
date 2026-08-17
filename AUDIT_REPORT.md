# EquityPilotAI — Comprehensive Product & Codebase Audit

**Date:** 2026-08-15
**Production:** https://equitypilot.in/
**Repo:** Anki444724/EquityPilotAI
**Branch audited:** arena/01a00048-equitypilotai (HEAD ea2ea9e)
**Auditor stance:** Senior product architect / UX/UI designer / full-stack engineer / codebase auditor
**Constraint:** Do NOT delete valuable data, reduce overload via IA, progressive disclosure, summaries, beginner/advanced views.

---

## 1. Executive Summary

EquityPilotAI is already a **Bloomberg-grade institutional research terminal** in terms of data depth:
- **135 companies**, 42k canonical facts, 10 fiscal years, 54 line items
- **Engines:** income statement / balance sheet / cash flow builders, 45+ ratios, working capital, debt, capex, DCF (FCFF/FCFE), DDM, relative, SOTP, sensitivity, Monte Carlo, WACC, 13-category scoring, 10-module AI scoring (AAA–C), 21 AI research sections, forecast with 3 scenarios, document intelligence (OCR, chunking, embeddings, knowledge graph), portfolio intelligence, reports (PDF/Excel/Word)

**Core problem:** Information architecture exposes institutional complexity directly to normal investors. Navigation shows 11 top-level items, most per-company, but company detail has no outbound links until `CompanyTabs` was added. Many tabs (Charts, Peers, News, Timeline) are declared in `CompanyTabs` but have **no route/page** — they 404 or scroll to same strip. Dashboard, Companies list, and Company Overview each render different metric grids with overlapping data, causing duplication and cognitive load.

**Financial-data ingestion for 500 companies is NOT ready yet** but architecture is 70% ready: `CanonicalFinancialsBuilder`, `Precedence` chain (override > store > alias > absent), `Nifty500Importer` with ISIN→BSE join, `TTLCache` with 15s NSE live / 300s default, `LiveMarketService` with non-blocking bulk_quotes + background refresh, `Document` pipeline. What is missing: validation at scale, automated periodic backfill, and source tracking for 500.

**Recommended fix:** Keep all data, but introduce **progressive disclosure**:
```
Simple view → Key metrics → AI summary → Positives → Risks → Detailed research → Raw/source
```
Primary journey: `Dashboard → Search company → Company Overview → AI Investment View → Key Financial Health → Valuation → News → Peers → Detailed Research`. All advanced data under **Advanced Research / Detailed Analysis**.

---

## 2. Current Architecture

### Frontend
- **Next.js 16**, App Router, `output: standalone`, React 19, TypeScript 5, Tailwind 4, `lucide-react`, TanStack Query 5.101.4
- **Layout:** `AppShell` with fixed desktop rail (56w) and mobile drawer (same `NavList`, role-filtered, per-company resolution via `sessionStorage` last-company). `Brand`, `UserCard`, `CommandPalette` (⌘K), `CompanyTabs` horizontal scroll strip with active tab into view.
- **State:** React Query, `AuthProvider`, `ThemeProvider`, `Providers`. Single `rawFetch` centralizes credentials — previously 7 call sites called `fetch()` directly → 401, fixed in FE-001/FE-004.
- **API client:** `frontend/src/lib/api.ts` — `API_BASE = NEXT_PUBLIC_API_URL ?? localhost:8000`, `request()` sends JSON + `credentials: include` + `authHeaders()`. Multipart handled separately.
- **Build:** Multi-stage Docker: deps → builder (ARG `NEXT_PUBLIC_API_URL` inlined at build) → runtime (`node server.js`, `HOSTNAME=0.0.0.0`). Previous Railpack bug fixed via `frontend/railway.toml` build.args.

### Backend
- **FastAPI**, Python 3.13 slim multi-stage (builder wheels, runtime with tesseract, DejaVu fonts, pg_dump). Entrypoint `docker-entrypoint.sh` fixes volume ownership.
- **Middleware order (outer→inner):** security headers → request context → metrics/errors → rate limit → CORS. Critical: rate limit keyed on credential hash, not IP alone, to avoid NAT throttling.
- **Lifespan:** In prod asserts schema via `inspect(engine)`, else `create_all`. Starts in-process worker + document worker if enabled. Flushes metrics on shutdown.
- **API v1 router:** 22 routers, order matters (ROUTE-001): `ai_scoring` before `market`, `filings_admin` before `market`, `quality` before `market` because market has `/{ticker}` greedy capture. `/companies` (search, sectors, list, detail, profile), `/dashboard/overview`, `/analysis`, `/forecast`, `/valuation` (all methods + summary), `/scoring`, `/ai-score`, `/ai` (capabilities, analysis, chat, report, evidence), `/documents`, `/portfolio`, `/reports`, `/knowledge`, `/quality`, `/market/{ticker}`, `/filings`, etc.
- **Services:** `CompanyService` (single resolution into `CanonicalFinancials`), `LiveMarketService` (shared TTL cache, `snapshot()`, `quote_only()` lightweight `.NS`/.BO handling, `bulk_quotes()` non-blocking with single worker + inflight dedup), `MarketDataRouter` (Finnhub→FMP→Yahoo→Internal→Documents, provenance, 15s NSE live TTL / 300s default), `ValuationService`, `ScoringService`, `AIScoringService` (10 modules), `ForecastService`, `Document` pipeline (extractors: pdf, office, ocr, tables → chunking → embeddings → entities → knowledge graph → vector_store), `PortfolioService`.

### Database / Data Models
- **SQLAlchemy 2.0**, `Base.metadata` 54 tables expected, verified in prod via `DEPLOYMENT_LIVE.md`.
- **Models:** `Company` (ticker, exchange BSE/NSE/NASDAQ, isin, bse_code, sector, industry, market_cap, current_price, shares_outstanding, data_version, soft delete, currency INR default, reporting_scale crore), `FinancialFact` (company_id, fiscal_year, line_item, value, precedence), `CompanyVersion` (audit), `Document`, `DocumentChunk` (embedding_v2 vector, spec), `Financials`, `Forecast`, `Knowledge`, `MarketOps` (manual override), `Platform` (tenants, users, sessions, jobs, audit), `Portfolio` (transactions, holdings, risk, alerts), `RecycleBin`, `Replication`, `Report` (blocks, citations), `Scoring` (AI scoring versions).
- **Canonical:** 54 line items (`LineItem`), `Precedence` (override > store > alias > absent), `ReportingUnit` (INR crore vs USD millions). Builder treats absent as unknown, zero as reported nil — critical for correct DCF.
- **Storage:** `LocalFileStorage` on Railway Volume `/data/documents`, S3/R2/MinIO optional. Migration script `migrate_to_bucket.py`.

### Authentication / Session Flow
- **Module 10 native auth:** JWT issuer `ierp`, access 900s, refresh 30d, remember-me cookie 90d, httpOnly Secure (forced in prod), SameSite lax, CSRF enabled, lockout 8 fails / 900s.
- **Legacy Clerk shim** still present for backwards compat, but `NATIVE_AUTH=true` in prod disables dev identity (previously every caller was super_admin).
- **Flows:** verification email, password reset, magic link, OAuth Google/GitHub (optional), invitation. `EmailService` uses console outbox when `SMTP_HOST` unset (dev), else `smtplib.SMTP` STARTTLS port 587.
- **Multi-tenancy:** every resource scoped to tenant, `DEFAULT_TENANT_SLUG=demo-capital` owns pre-Module10 rows. `ALLOW_SELF_SIGNUP=true`.

### API Structure
- **Auth:** `/api/v1/auth/*` (me, config, login, refresh, verify, magic, reset, OAuth)
- **Admin:** `/admin/*` (tenants, users, activity, companies, financials, market overrides, AI ops, documents, recycle bin)
- **Core:** `/companies/search`, `/sectors`, list, detail, profile; `/dashboard/overview`; `/analysis/{id}`; `/forecast`; `/company/{ticker}/valuation/*`; `/company/{ticker}/scoring/*`; `/ai-score`, `/company/{ticker}/ai-score`; `/company/{ticker}/ai/*` (capabilities, chat, report, evidence); `/documents`; `/portfolio`; `/reports`; `/market/{ticker}` (live market with provenance); `/filings`
- **Quality:** All endpoints carry `price_source`, `last_updated`, `market_status`, `data_quality` disclosure to avoid presenting stale DB price as live.

---

## 3. Current Navigation Map

**Top rail (`app-shell.tsx` NAV):**
- Dashboard (d)
- Companies (c)
- Financials (perCompany → financials)
- Valuation (perCompany → valuation)
- Scoring (perCompany → scoring)
- Forecast (perCompany → forecast)
- AI Research (perCompany → ai)
- Documents
- Portfolio
- Watchlist
- Reports
- Administration (admin roles)
- Platform Ops (operator roles)

**CompanyTabs (inside `/companies/[id]`):**
- Overview ("")
- Financials
- Valuation
- Scoring
- AI Analysis
- Forecast
- Documents
- Charts (declared but **no page** — 404)
- Peer Comparison (no page)
- News (no page)
- Knowledge Timeline (no page)

**Actual pages that exist under `/companies/[id]`:**
- `page.tsx` (Overview), `financials`, `valuation`, `scoring`, `ai`, `forecast`, `documents` — 7 of 11 tabs implemented.

**Landing:** `/` → marketing with features, stats, AI search box → `Open Terminal` → `/dashboard`.

---

## 4. Current Page-by-Page UX Audit

### Dashboard (`/dashboard`)
**Purpose:** Terminal home, live institutional research, 135 companies.
**Important:** Coverage stats, sectors, largest by mcap, recently added, AI search row, top picks, recent filings (hardcoded in file, not from API), market open indicator.
**Overload:** 4 different search inputs (global AI search row + top stats + AI picks + recent filings). Duplicates Companies search. Hardcoded `topPicks` (TCS 94 Strong Buy) not from API — misleading if real score differs.
**Confusion:** “Market Open” badge static, not from `market_status()`; “AI Search” placeholder suggests free text but no handler.
**Performance:** `dashboard/overview` calls `attach_many` → 8 snapshots → each hits TTL cache (fast now after bulk fix) but previously 16s delay due to BHARATCP external retry.
**Mobile:** Grid collapses but search row overflows.
**Opportunity:** Replace hardcoded picks/filings with live API, add beginner summary: “What is healthy today?”.

### Companies (`/companies`)
**Purpose:** Browse/search universe, filter by sector.
**Important:** Paginated list, sector filter, market price, mcap, sector.
**Problem fixed:** Previously price for only ~20-25 companies because `list_companies` returned `CompanySummary` without market data and most imported companies had `current_price=NULL`. Fixed via `bulk_quotes()` with background refresh.
**Overload:** Table shows ticker, name, sector, industry, mcap, current_price — good, but no quick health indicator (e.g., scoring grade, growth).
**Confusion:** No clear CTA to “View AI conclusion”.
**Performance:** Now non-blocking (cache/internal fallback immediately, single worker dedup), no 20 sequential Yahoo calls.
**Missing:** Simple view with key metrics, AI summary.

### Company Overview (`/companies/[id]`)
**Purpose:** Header + headline metrics + financial position + coverage + about.
**Important:** Ticker badge, exchange, sector, fact count, price with change, mcap, source/market_status, website, Add to Watchlist, revenue/EBITDA/PAT/EPS, balance_sheet_ties badge, grid table, coverage bar, fiscal years chips, description, info card.
**Overload:** Metrics grid is 4 cards only — good, but then full grid-table with 8 rows repeats same numbers. No visualization.
**Duplication:** Price shown in header and also in market section, no AI verdict.
**Confusion:** “Full statements, ratios… arrive in Modules 2-5” info card is stale — they already exist.
**Mobile:** Table `scroll-x` needed, okay.
**Opportunity:** Answer the 10 investor questions immediately (see recommended layout).

### Financials (`/companies/[id]/financials`)
**Purpose:** Income, balance, cash flow with 45+ ratios, WC, debt, capex.
**Important:** Three statements, ratios.
**Overload:** 54 items × 10 years = 540 cells at once, no summary.
**Confusion:** No beginner view — normal investor sees “Change in Inventories CF” without explanation.
**Missing:** Simple view (Revenue, PAT, Margin trends), AI summary of financial health.

### Valuation (`/companies/[id]/valuation`)
**Purpose:** DCF, WACC, relative, sensitivity, SOTP, simulation.
**Important:** Intrinsic value, upside, margin of safety, data-quality disclosure.
**Overload:** Shows all methodologies at once (FCFF, FCFE, DDM, relative, SOTP) with terminology like “Discount Convention”, “Terminal Method” — intimidating for normal investor.
**Confusion:** No clear “Is stock cheap or expensive?” answer at top.
**Performance:** DCF recomputed per request, but cached via financials.
**Opportunity:** Simple view: “Fair value ₹X vs market ₹Y → cheap/expensive”, then advanced.

### Scoring (`/companies/[id]/scoring`)
**Purpose:** 13 categories, AAA–C rating, buy/hold/sell.
**Important:** Overall score, grade, stars, recommendation, conviction, categories, strongest/weakest.
**Overload:** 13 categories each with many metrics → 3780 factors checked in backend. UI shows all expanded.
**Confusion:** Score 94 vs grade AAA vs Strong Buy — three vocabularies for same concept.
**Missing:** Simple positives/risks list.

### AI Research (`/companies/[id]/ai`)
**Purpose:** Provider-agnostic analysis across 21 sections.
**Important:** Capabilities, providers, analysis/chat/report/evidence tabs, language selector, citations, guardrails.
**Overload:** 21 sections + 4 tabs + language selector + warnings — too many controls above fold.
**Confusion:** Offline provider banner appears when no live key, but output still looks like AI.
**Missing:** One-paragraph AI conclusion at top.

### Forecast (`/companies/[id]/forecast`)
**Purpose:** Assumptions, 3 scenarios, revenue, margins, capex, debt, taxes, cashflow engine.
**Important:** Scenario editor.
**Overload:** 447 lines, assumption editor with many fields, no simple “Growth expected?”.
**Confusion:** Advanced financial modeling exposed directly.

### Documents (`/companies/[id]/documents`)
**Purpose:** Uploaded annual reports, OCR, chunking, embeddings, entities, graph, search.
**Important:** 619 lines, most complex page, handles upload, approval, publish, search with hybrid lexical/semantic.
**Overload:** Library + evidence + search all in one page.
**Performance:** Chunking/embeddings heavy, but async via worker.
**Opportunity:** Most investors don’t need this — hide behind Advanced.

### Charts (tab declared, no page)
**Purported:** Price history, beta, volatility.
**Actual:** No route → 404. Opportunity: Add simple price chart + key ratio trends.

### Peer Comparison (tab, no page)
**Purported:** Sector peers, multiples.
**Actual:** No page. Backend has `scoring/peers` and relative valuation. Opportunity: Merge with valuation.

### News (tab, no page)
**Purported:** Company news from market provider.
**Actual:** No page, but `MarketDataResult` includes news when `include_news=True`. Opportunity: Simple news list.

### Knowledge Timeline (tab, no page)
**Purported:** Temporal knowledge vault.
**Actual:** No page. Backend has `knowledge/vault`, `temporal`. Opportunity: Timeline of filings + news + AI scores.

### Portfolio (`/portfolio`)
**Purpose:** Workspace with 6 tabs: Overview, Holdings, Allocation, Risk, Alerts, AI Commentary.
**Important:** Holdings table, treemap, allocation pie, risk grid, underwater chart, value chart, alerts, AI commentary.
**Overload:** 6 tabs, each with many charts, dimension labels, attribution table, rebalance table.
**Confusion:** Gate on auth + initialising prevents 401, but notice handling complex.
**Performance:** View computed once per request (good), but frontend re-renders many charts.

### Watchlist (`/watchlist`)
**Purpose:** Track tickers, price, source, last_updated, market_status, notes.
**Important:** Simple list, add via company pages.
**Good:** Already uses `LiveMarketService` shared snapshot.
**Opportunity:** Add simple health indicator per row.

### Reports (`/reports`)
**Purpose:** Research report generator, publication-quality PDF/Excel/Word.
**Important:** Report types, capabilities, statistics (citation coverage, fully cited, mean build).
**Overload:** Stats + preview + generate + remove all same page.
**Mobile:** Grid 2→6 cols, okay.

### Administration (`/admin`)
**Purpose:** Operator console: tenants, users, companies, financials, market ops, AI ops, documents, recycle bin, activity, backup, schedules.
**Important:** 13 sections, role-filtered.
**Overload:** Heavy, but appropriate for admin.
**Good:** Audit trail via `CompanyVersion`.

### Documents (`/documents` top-level)
**Purpose:** Global document library.
**Overload:** Similar to company documents but global.

### Platform Ops (`/platform`)
**Purpose:** Health, readiness, metrics, backup.

---

## 5. Problems Ranked

### Critical
- **C1:** Charts/Peers/News/Timeline tabs declared but no pages → broken navigation, 404 expectation.
- **C2:** Dashboard hardcoded `topPicks` and `recentFilings` not from API → stale/misleading.
- **C3:** Financials/Valuation/Scoring expose institutional jargon without beginner view → normal investor lost.

### High
- **H1:** Information overload: 54 items × 10 years, 13 scoring categories, 21 AI sections, 6 portfolio tabs all visible at once.
- **H2:** Duplication: Revenue/EBITDA/PAT shown in Overview and Financials and Valuation summary.
- **H3:** Missing simple answer: “Is business healthy? Growing? Cheap? Biggest positive/risk? AI conclusion?” not answered at top.
- **H4:** Performance: Previous `attach_many` did 20 external calls blocking Companies list (fixed via `bulk_quotes` non-blocking, but must not regress).
- **H5:** Mobile: CompanyTabs horizontal strip needs scroll, Add to Watchlist button overlaps on small screens.

### Medium
- **M1:** Navigation has 11 top-level items, 7 per-company — too many. Financials/Valuation/Scoring/Forecast/AI overlap.
- **M2:** No beginner/advanced toggle.
- **M3:** Source transparency: citations exist in AI, but financial claims lack inline source (e.g., revenue FY2025 from which filing?).
- **M4:** Accessibility: tab strip lacks `aria-current`, but now has — needs audit.

### Low
- **L1:** Landing page stats (54, 45+, 11, 21) not linked to explanations.
- **L2:** Inconsistent naming: “Scoring” vs “AI Score” vs “AI Research”.

---

## 6. Recommended Information Architecture (Progressive Disclosure)

```
Dashboard (simple)
  ↓
Search Company (typeahead, sector, health badge)
  ↓
Company Overview (Simple View)
  ├─ What does company do? (1-line + sector/industry)
  ├─ Key metrics (Revenue, PAT margin, ROCE, Debt/Equity) with trend sparkline
  ├─ AI summary (1 paragraph)
  ├─ Important positives (top 3 from scoring strongest)
  ├─ Important risks (top 3 from weakest + guardrails)
  ├─ Valuation verdict (cheap/fair/expensive + upside)
  ├─ Recent news (3 items)
  ├─ Peer snapshot (3 peers, multiple)
  └─ CTA: Detailed Research
  ↓
Detailed Research (Advanced layer, tabs)
  ├─ Financial Health (statements + ratios + balance tie)
  ├─ Growth (revenue CAGR, PAT CAGR, forecast scenarios)
  ├─ Valuation (DCF, relative, SOTP behind “Advanced” toggle)
  ├─ Scoring (13 pillars behind “Show all”)
  ├─ AI Analysis (21 sections behind capability picker)
  ├─ Forecast (assumptions editor behind Advanced)
  ├─ Documents (library, citations)
  ├─ Charts (price, margins)
  ├─ Peers (full comparison)
  ├─ News (full list)
  └─ Timeline (knowledge vault)
```

---

## 7. Recommended Navigation

**Primary (always visible):**
- Dashboard
- Companies
- Portfolio
- Watchlist
- Reports

**Company-scoped (when company selected, shown in AppShell as contextual):**
- Overview
- AI Investment View (new, merged AI summary + scoring verdict)
- Financial Health (merged Financials + simplified ratios)
- Valuation
- News & Peers (merged)

**Under “More” or “Advanced Research”:**
- Detailed Financials (full 54 items)
- Detailed Scoring (13 categories)
- Forecast (assumptions)
- Documents
- Charts
- Timeline

**Rename:**
- Scoring → Financial Health Score
- AI Research → AI Investment View
- Documents → Filings & Research Docs

**Hide behind Advanced:**
- SOTP builder, sensitivity grid, Monte Carlo, working capital derivation, line-item mapping.

---

## 8. Recommended Company-Page Layout (answers 10 questions)

**Above fold (Simple View):**
1. **What does company do?** — `c.description` truncated to 1 line + sector/industry badges.
2. **Is financially healthy?** — 4 cards: Revenue (crore + YoY), PAT margin, ROCE, Net Debt/Equity with color tone.
3. **Is it growing?** — Sparkline for revenue 3y CAGR, PAT 3y CAGR.
4. **Expensive or cheap?** — Valuation verdict badge: “Cheap (15% upside)”, fair, expensive, with intrinsic vs market.
5. **Biggest positives** — Top 3 from `scoring.strongest` + AI scoring.
6. **Biggest risks** — Top 3 from `weakest` + guardrails + thin_evidence.
7. **Recent news** — 3 items from market provider.
8. **AI conclusion** — One-paragraph from `aiApi.analyse(ticker, investment_thesis)` with citations.
9. **Peers** — 3 peers with mcap, PE, ROCE, grade.
10. **Source data** — Link: “Inspect underlying source → Detailed Financials, Filings, Citations”

**Below fold — tabs for Detailed Research.**

---

## 9. Beginner vs Advanced UX Strategy

- **Default = Beginner:** Simple view, key metrics, AI summary, positives/risks, valuation verdict.
- **Toggle:** `Advanced Research` switch (localStorage) reveals:
  - Full 54-item grid, 45+ ratios, debt schedule, capex, working capital derivation
  - All 13 scoring pillars, metric-level origins
  - DCF assumptions, WACC build, terminal methods
  - 21 AI sections, evidence, guardrail panel
  - Document chunks, embeddings, knowledge graph
- **Implementation:** `useAdvanced()` hook, `AdvancedBadge`, and separate route `/companies/[id]/advanced/*` that reuses same APIs but renders `MetricGrid` without filter.

---

## 10. Features to Remove

- **Hardcoded dashboard `topPicks` and `recentFilings`** — replace with API or remove.
- **Stale info card** “Full statements… arrive in Modules 2-5” — now shipped.
- **Duplicate Add to Watchlist** logic in both `page.tsx` and `CompanyTabs` — merge into hook.
- **Empty tabs** Charts/Peers/News/Timeline without pages — remove from `CompanyTabs` until implemented, or implement minimal versions.

---

## 11. Features to Merge

- **Financials + Ratios + Working Capital + Debt + Capex** → single “Financial Health”
- **Valuation: DCF + Relative + SOTP** → one page with verdict on top, methods behind Advanced
- **Scoring + AI Scoring** → “AI Investment View”: overall score, rating, recommendation, plus 10-module AI score as breakdown
- **Documents (company) + Documents (global)** → one library with company filter
- **Portfolio Holdings + Allocation + Risk** → Overview already has allocation pie + risk grid, can merge
- **News + Knowledge Timeline** → Timeline already includes news + filings

---

## 12. Features to Add

- **AI Investment View** page: merged scoring + AI summary + positives/risks.
- **Simple valuation verdict** component: cheap/fair/expensive with margin of safety.
- **Key metrics sparkline** component: revenue, PAT, ROCE 5y trend.
- **Beginner explanations:** Tooltip for every jargon (EBITDA, PAT margin, WACC).
- **Source transparency:** Every financial cell links to filing or `source=yahoo_finance` with `Precedence`.
- **Peer Comparison minimal** (3 peers, PE, PB, ROCE) — uses existing `scoring/peers`.
- **Recent news minimal** (3 items) — uses `MarketDataResult.news` already fetched when `include_news=True`.
- **Empty state for 500 companies:** Show coverage % and “Import” CTA.

---

## 13. Performance Risks

- **Fixed:** Companies list previously did 20 external calls blocking — now `bulk_quotes()` non-blocking with single worker + inflight dedup, cache/internal fallback immediately, background refresh populates both light and full keys with 15s NSE / 300s default TTL. Must not reintroduce `attach_many()` into list.
- **Remaining:** 
  - Dashboard overview still calls `attach_many` for largest + recently_added (8-10 tickers) — okay with cache but could be converted to `bulk_quotes` too.
  - Valuation DCF recomputed per request — should cache via `Namespace` like financials.
  - Document search hybrid lexical/semantic — embedding provider may be heavy, needs rate limit.
  - Forecast engine with 3 scenarios — okay.
  - Frontend: Portfolio page renders 6 chart components at once — should lazy load.
- **N+1:** `CompanyService.search` does `limit*3` then sorts in Python — okay for 500 but will be slow for 5000. Add DB index on `ticker`, `name`, `sector` (already exists) and consider full-text.

---

## 14. Data Architecture Recommendations

- Keep `CanonicalFinancialsBuilder` with `Precedence` chain — excellent for 500+ companies.
- Keep `CompanyVersion` audit — needed for rollback.
- Add `DataQualityReport` to every financial response (already done for valuation, should extend to financials page).
- Source tracking: `FinancialFact.source` already stamps `yahoo_finance` / `import` — expose in UI.
- For 500 ingestion: Use `Nifty500Importer` pattern (ISIN→BSE join) as template for full universe. Add `bse_code` handling in symbol resolver (fixed: numeric → `.BO`).
- TTL: Preserve 15s NSE live / 300s default / internal 300s — implemented in `router.py`.

---

## 15. Financial-Data Readiness Assessment (500 companies)

- **Ready (70%):**
  - Canonical grid (54 items × 10 years) with builder
  - Precedence chain
  - Nifty500 importer with ISIN join and BSE mapping
  - TTL cache and non-blocking bulk quotes
  - Document pipeline (OCR, chunking, embeddings)
  - Validation harness (`validate.py`, `derive_wc.py`)
- **Not ready (30%):**
  - No automated periodic backfill for quarterly results (exists `backfill_periodic.py` but not scheduled)
  - No validation gate for 500 (currently 42k facts for 135 companies, need 500×54×10=270k)
  - No source tracking UI (source column exists but not shown)
  - Symbol resolver now fixed for numeric BSE and long tickers, but needs DB-backed resolver (company.exchange, bse_code, isin) — implemented in `_canonical_for_company()` but should be moved to DB lookup for 500.
  - No rate limiting for Yahoo ingestion at 500 scale — `MIN_INTERVAL=4.0s` + circuit breaker exists, but 500×90s = 12.5h ingestion needed.

---

## 16. Safe Implementation Order (PR-sized)

1. **PR1: Fix broken tabs** — Remove Charts/Peers/News/Timeline from `CompanyTabs` until implemented, or add placeholder pages with “Coming soon” + API wiring. (Critical)
2. **PR2: Dashboard live data** — Replace hardcoded `topPicks`/`recentFilings` with API (`ai_scoring/dashboard` + `filings/dashboard`).
3. **PR3: Company Overview simple view** — Implement 10-question layout above fold, keep existing detailed tables below fold behind `Advanced`.
4. **PR4: Beginner toggle** — `useAdvanced()` hook + localStorage, hide detailed tables by default.
5. **PR5: Merge Scoring + AI Scoring** → AI Investment View page.
6. **PR6: Merge Financials + ratios** → Financial Health with sparkline + coverage bar.
7. **PR7: Valuation verdict** — Cheap/fair/expensive badge at top of valuation page.
8. **PR8: News & Peers minimal** — 3 items using existing market news + scoring peers.
9. **PR9: Portfolio/Watchlist health badges** — Add ROCE, grade to table rows.
10. **PR10: Performance** — Convert dashboard `attach_many` to `bulk_quotes`, add React lazy for charts.
11. **PR11: Source transparency** — Show `source` and `Precedence` in financial cells.
12. **PR12: Docs** — Update navigation map, remove Railway hardcoding (already done for `OPENROUTER_SITE_URL`).

Each PR should be behind feature flag or `Advanced` toggle, not deleting data.

---

## 17. Testing Strategy

- **Unit:** `test_live_market.py` (market_status + fallback), `test_market_providers.py` (TTL, symbol resolver), `test_api.py` (list with market, profile with coverage).
- **Integration:** `test_price_consistency.py` (all five surfaces share same snapshot) — already fixed via 15s/300s TTL logic and dual-key caching.
- **New:** Add `test_symbol_resolver.py` for `RELIANCE`, `20MICRONS`, `21STCENMGM`, `544023`, `BHARATCP`, `AAPL`.
- **Frontend:** Add Playwright e2e for journey Dashboard→Companies→Company Overview→AI Investment View, check no 404 on tabs.
- **Performance:** Load test `/companies` with 500 companies, ensure first response <500ms (non-blocking) and background refresh populates within 15s.
- **Manual:** Mobile responsive check for CompanyTabs scroll + Add to Watchlist.

---

## 18. Risks of Modifying Current Application

- **Data loss:** `CompanyVersion` rollback must be preserved — any migration that drops `company_versions` loses audit.
- **Cache invalidation:** Changing TTL logic may cause price-consistency test to flake (seen: 15s NSE TTL caused dashboard vs detail timestamp divergence, fixed by dual-key put and internal 300s TTL).
- **External provider budget:** FMP 250 calls/day — bulk list must never call external synchronously (fixed, but risk of regression if someone reintroduces `attach_many`).
- **Auth:** `NATIVE_AUTH` vs dev identity — changing `AppShell` gate may reintroduce 401 caching bug (FE-002).
- **Document Volume:** Railway Volume at `/data/documents` is ephemeral without volume — migration to S3 must preserve bytes.
- **Domain:** `equitypilot.in` not yet wired, Railway URLs still in `frontend/railway.toml` — changing to `api.equitypilot.in` before DNS ready breaks prod.

---

## Implementation Roadmap (Small Safe Tasks)

**Phase 0 — Audit (done, this report)**

**Phase 1 — Navigation & Broken Tabs (1 week)**
- PR1, PR2

**Phase 2 — Company Simple View (2 weeks)**
- PR3, PR4, PR7, PR8

**Phase 3 — Merging & Advanced Layer (2 weeks)**
- PR5, PR6, PR9

**Phase 4 — Performance & Transparency (1 week)**
- PR10, PR11

**Phase 5 — Financial Data 500 (separate later phase)**
- Backfill, validation, periodic jobs, DB-backed symbol resolver

**Wait for approval before code changes.**

