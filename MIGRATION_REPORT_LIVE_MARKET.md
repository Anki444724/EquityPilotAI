# Migration Report — Live Market Price Architecture

**Author:** arena agent · **Date:** 2026-08-06
**Scope:** Replace the database `Company.current_price` as the *displayed* market
price with a single shared live-market path backed by `MarketDataRouter` + its cache.
**Status:** Report generated **before** any code is changed (this file is the plan).

---

## 1. Problem statement

The runtime trace (`RUNTIME_TRACE_COMPANY_PRICE.md`) established that the price shown
on the company page is a raw database column read in `CompanyService.get()`:

```
Company page → api.companyProfile → GET /api/v1/companies/{id}/profile
  → CompanyService.profile → CompanyService.get → db.get(Company, id)
  → Company.current_price  (plain Float column, written only by seed/ingest jobs)
```

The market layer (`MarketDataRouter.fetch`, FMP/Yahoo/Finnhub providers, the TTL
cache) exists but is **never called** by the company page, the dashboard, the
companies list, or the watchlist. Every one of those surfaces renders the stale DB
column. There is also **no Upstox provider** in the codebase (FMP / Yahoo / Finnhub
are the only externals).

### The required architecture
```
Company Page (and Dashboard / Companies / Watchlist)
  → CompanyService.profile / list / search / dashboard / watchlist
  → LiveMarketService.snapshot(ticker, db)     [NEW shared service]
  → MarketDataRouter.fetch(ticker, db)         [existing]
  → Live Provider (FMP→Yahoo→Finnhub)  /  Internal DB / Documents  (fallback tiers)
  → TTL cache (300s)                             [existing MarketDataRouter cache]
  → LiveMarket payload {live_price, price_source, last_updated, market_status,
                        change, change_percent, volume}
  → Frontend renders live_price (falls back to current_price only when no live data)
```

---

## 2. Requirements mapping

| # | Requirement | Implementation location |
|---|---|---|
| 1 | Remove `Company.current_price` as the *displayed* market price | All frontend price cells switch to `market.live_price` (with `current_price` only as an explicit fallback). `Company.current_price` column is left intact as the historical/fallback source. |
| 2 | Company Profile API includes `live_price, price_source, last_updated, market_status, change, change_percent, volume` | New `LiveMarket` schema, added to `CompanyProfile` (top-level `market`) and to `CompanySummary`/`CompanyDetail`. |
| 3 | Company page consumes the live market cache | `CompanyService.profile` calls `LiveMarketService.snapshot`, which calls `MarketDataRouter.fetch(..., use_cache=True)` → the shared TTL cache. |
| 4 | DB `current_price` only historical/fallback | Kept as the `current_price` field and used only when `market.live_price` is `None` (i.e. when the internal tier serves). |
| 5 | Company page, Dashboard, Watchlist, Screener, Compare use the same live market service | Dashboard `largest`/`recently_added`, companies list & search, and watchlist rows all enrich through `LiveMarketService`. **Note:** no Screener or Compare page currently exists in the repo (only landing-page placeholders) — they will consume the same service when created. |
| 6 | No page reads `Company.current_price` as the displayed live price | Frontend cells read `market.live_price`; provenance (`price_source`) is surfaced so a fallback to stored data is visibly labeled, never silently presented as live. |

---

## 3. Current-state audit (every read of the displayed price)

Backend (all read `Company.current_price` directly, no market call):
- `backend/app/api/v1/companies.py` → `CompanyService.profile/get/search/list_companies`
- `backend/app/services/company_service.py:65` `CompanyService.get` → `db.get(Company, id)`
- `backend/app/api/v1/dashboard.py` → `overview()` → `CompanySummary.model_validate(c)` for `largest` & `recently_added`
- `backend/app/services/portfolio/service.py:864` `watchlist_view` → `price = company.current_price`

Frontend (all render the DB value):
- `frontend/src/app/companies/[id]/page.tsx:127` — `rupees(c.current_price)`
- `frontend/src/app/companies/page.tsx:107` — `rupees(c.current_price)` (list & search)
- `frontend/src/app/dashboard/page.tsx:173` — `rupees(c.current_price)` (largest)
- `frontend/src/components/portfolio/panels.tsx:475` (portfolio holdings) & `:764` (watchlist `row.price`)
- `frontend/src/lib/types.ts` — `CompanySummary.current_price`

Out of scope (noted, not changed): valuation/forecast pages and the portfolio
holdings table read `current_price` through the valuation engine / stored positions.
These are not in the requirement list (Company, Dashboard, Watchlist, Screener,
Compare) and changing the valuation engine's price input is a separate risk. They are
called out in §7 as follow-ups.

---

## 4. Target data contract

### New `LiveMarket` schema (backend `app/schemas/company.py`)
```python
class LiveMarket(BaseModel):
    live_price: float | None            # best current price (cache served)
    current_price: float | None         # stored historical/fallback (DB column)
    price_source: str | None            # "Financial Modeling Prep" / "Yahoo Finance
                                        # (Fallback)" / "Finnhub" / "Internal Financial
                                        # Database" / "Uploaded Documents (RAG)"
    last_updated: str | None            # ISO-8601 from provider metadata
    market_status: str                  # "open" | "closed" | "weekend" | "unknown"
    change: float | None
    change_percent: float | None
    volume: float | None
```
Attached to `CompanySummary` (inherited by `CompanyDetail`) as `market`, and exposed
at the top level of `CompanyProfile` as `market`.

### New shared service `backend/app/services/live_market.py`
- `LiveMarketService.snapshot(company, db) -> LiveMarket`
  calls `MarketDataRouter.fetch(company.ticker, db=db)` (uses the TTL cache),
  normalizes the `MarketSnapshot.quote` + `meta` into `LiveMarket`.
- `LiveMarketService.attach(company_or_summary, db) -> None` / batch helpers for
  lists, so list/search/dashboard enrich in one place.
- `market_status`: computed from NSE IST trading hours (Mon–Fri 09:15–15:30 IST),
  deterministic and injectable for tests.

### Watchlist
- `WatchlistRowOut` gains `price_source: str | None`.
- `PortfolioService.watchlist_view` sets `price` from `LiveMarketService.snapshot`
  (live_price, fallback current_price) and fills `price_source`.

---

## 5. Files to change

Backend:
- `backend/app/schemas/company.py` — add `LiveMarket`, extend `CompanySummary` &
  `CompanyProfile`.
- `backend/app/schemas/portfolio.py` — add `price_source` to `WatchlistRowOut`.
- `backend/app/services/live_market.py` — **new** shared service.
- `backend/app/services/company_service.py` — enrich `profile`, `get`, `search`,
  `list_companies` with `market`.
- `backend/app/api/v1/dashboard.py` — enrich `largest` & `recently_added`.
- `backend/app/services/portfolio/service.py` — `watchlist_view` uses live price.

Frontend:
- `frontend/src/lib/types.ts` — add `LiveMarket`, add `market` to `CompanySummary`,
  add `price_source` to `WatchlistRow`.
- `frontend/src/app/companies/[id]/page.tsx` — render `market.live_price` (+ source
  indicator).
- `frontend/src/app/companies/page.tsx` — render `market.live_price`.
- `frontend/src/app/dashboard/page.tsx` — render `market.live_price`.
- `frontend/src/components/portfolio/panels.tsx` — watchlist `row.price` source note.

Tests:
- Add backend tests for `LiveMarketService` normalization and endpoint enrichment;
  update any existing assertion that would break (existing tests assert field values,
  not exact key sets, so adding `market` should not break them).
- Existing frontend builds are type-checked via `tsc`; `market` is optional/nullable
  with a `?? current_price` fallback so no page regresses to `undefined`.

---

## 6. Risk & fallback semantics

- **No live provider configured / outage:** the router falls through tiers exactly as
  it does today (`internal` → `documents` → `external` for Indian symbols). `price_source`
  then names the internal/document tier, `market_status` reflects a non-live source,
  and `live_price` carries the stored/derived figure. The page shows a clearly-labeled
  non-live value rather than a live lie. **This preserves the existing guarantee that
  every price names its source.**
- **Cache:** `MarketDataRouter` uses a 300s in-process TTL cache shared by all callers,
  so the company page, dashboard, list, and watchlist for the same ticker within 5
  minutes all hit the same cached snapshot (requirement 3 & 5).
- **Existing tests:** `test_api.py` profile/dashboard assertions check specific field
  values (revenue, ebitda, largest ticker) — adding a `market` object does not change
  them. Profile/dashboard calls for seeded companies are served by the internal tier
  (no external network), so no test will make live HTTP calls.
- **Network safety in tests:** Indian symbols resolve to the internal tier first and
  the seeded companies exist, so enrichment performs no external requests.

---

## 7. Out of scope / follow-ups
- Valuation & forecast engines still read `company.current_price` as an *input*. Making
  those consume `market.live_price` is a separate change with model-accuracy risk; left
  for a dedicated follow-up.
- Portfolio holdings `position.current_price` is a stored snapshot on each position;
  not a "company page" surface. Left unchanged.
- No Screener or Compare page exists yet; the shared `LiveMarketService` is the single
  hook they will use.

---

## 8. Acceptance checks (post-change)
1. `GET /api/v1/companies/{id}/profile` returns `market.{live_price, price_source,
   last_updated, market_status, change, change_percent, volume}`.
2. Company page, `/companies`, `/dashboard`, and `/watchlist` all render from
   `market.live_price` and show `price_source`.
3. Repeated profile/dashboard calls within the TTL return identical cached figures.
4. No frontend page reads `Company.current_price` directly for the *displayed* price
   (only as a fallback inside the `market.live_price ?? current_price` expression).
5. Backend test suite still green.

---

## 9. Implementation complete — verification

Executed on 2026-08-06 after this report was approved. All of §5 was implemented.

### Addendum — Portfolio & Watchlist wired to the shared live service (same session)

Following a runtime verification that exposed the Portfolio page still reading
`Company.current_price` (the DB column) while the other four shared the live path,
the migration was completed so **all five surfaces consume the exact same
`LiveMarketService.snapshot`**:

- `app/services/portfolio/service.py` — `view()` now builds `prices` from
  `LiveMarketService.snapshot(...).live_price` (not `c.current_price`); `_analytics`
  accepts and uses a live-price map for `expected_cagr`; `_price_for` helper added.
- `app/services/portfolio/engine.py` — `HoldingView` carries `price_source`,
  `last_updated`, `market_status` provenance.
- `app/schemas/portfolio.py` + `app/api/v1/portfolio.py` — `HoldingOut` and
  `WatchlistRowOut` expose `price_source`, `last_updated`, `market_status`.
- `app/services/portfolio/service.py` `watchlist_view` now returns `last_updated`
  and `market_status` alongside `price`/`price_source`.
- Frontend: `types.ts` (`Holding`, `WatchlistRow` fields) and the HoldingsTable +
  WatchlistTable render `price_source`, `last_updated`, `market_status`.

### Verified at runtime — all five pages identical (RELIANCE)

```
  PAGE             | PRICE  | SOURCE                       | LAST UPDATED                 | STATUS
  Dashboard        | 2945.0 | Internal Financial Database  | 2026-08-06T11:31:45.339534+00:00 | closed
  Companies List   | 2945.0 | Internal Financial Database  | 2026-08-06T11:31:45.339534+00:00 | closed
  Company Detail   | 2945.0 | Internal Financial Database  | 2026-08-06T11:31:45.339534+00:00 | closed
  Watchlist        | 2945.0 | Internal Financial Database  | 2026-08-06T11:31:45.339534+00:00 | closed
  Portfolio        | 2945.0 | Internal Financial Database  | 2026-08-06T11:31:45.339534+00:00 | closed
```

Price, source, last_updated and market_status are byte-for-byte identical across all
five pages. Enforced by `tests/test_price_consistency.py` (asserts the four contract
fields equal on every surface).

Backend:
- `app/schemas/company.py` — `LiveMarket` model added; `CompanySummary` & `CompanyProfile`
  now carry `market`.
- `app/schemas/portfolio.py` — `WatchlistRowOut.price_source` added.
- `app/services/live_market.py` — **new** `LiveMarketService` (single `snapshot`
  through `MarketDataRouter.fetch(use_cache=True)`), `market_status()` (NSE IST hours),
  and `attach_many`/`attach` helpers.
- `app/services/company_service.py` — `profile`, `get_detail`, `search`, `list_companies`
  enrich with the shared market view.
- `app/api/v1/dashboard.py` — `largest` & `recently_added` enriched.
- `app/services/portfolio/service.py` — `watchlist_view` prices from the live service
  and reports `price_source`.

Frontend:
- `lib/types.ts` — `LiveMarket`, `CompanySummary.market`, `WatchlistRow.price_source`.
- `lib/format.ts` — `marketPrice()` (renders `market.live_price`, falls back to
  `current_price` only when there is no live figure).
- Company page renders `marketPrice(c)` + change/`price_source`/`market_status`;
  `/companies`, `/dashboard` render `marketPrice(c)`; watchlist shows `price_source`.

Tests:
- `tests/test_live_market.py` (new) — `market_status` derivation + fallback labelling.
- `tests/test_api.py` — `TestLiveMarketEnrichment` (profile/detail/list/search/dashboard
  carry the `market` contract).

Verification results:
- **Backend full suite:** `pytest` → 100% pass (includes new tests).
- **Frontend:** `tsc --noEmit` → clean; `eslint` → 0 errors (only pre-existing unused-var
  warnings); `next build` → compiles successfully.

Notes:
- Screener and Compare pages do not yet exist in the repo (landing-page placeholders
  only); the shared `LiveMarketService` is the single hook they will consume.
- Valuation/forecast engines and stored portfolio positions still read
  `Company.current_price` as an *input* — flagged in §7 as a separate follow-up, not part
  of this change.
