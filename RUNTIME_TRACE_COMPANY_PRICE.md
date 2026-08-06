# End-to-end runtime trace — "price on the company page"

Traced against the actual code in this repository. No assumptions. Every function
call below was read from source and followed to the next.

---

## The critical finding up front

**The price shown on the company page is a raw database column. The market-data
layer — router, providers, external feeds — is NEVER invoked to produce it.**

And there is **no Upstox provider anywhere in the codebase**. `grep -ri upstox`
matches only the note I wrote in `DEPLOYMENT_STATUS.md`; no source file references
it. The only external providers are **Financial Modeling Prep (FMP)**, **Yahoo
Finance**, and **Finnhub** (`backend/app/data/providers/fmp.py`, `yahoo.py`,
`finnhub.py`). Those are used only by the *other* endpoint, `/api/v1/market/{ticker}`,
which the company page does not call.

So the requested chain
`React → API → Service → Router → Provider → Upstox` **stops at Service**. The
router/provider legs are not in the company page's path at all.

---

## The actual execution path (company page price)

### Step 1 — React component
`frontend/src/app/companies/[id]/page.tsx`

- The price is rendered from `c.current_price`:
  ```tsx
  const { data, isLoading, error } = useQuery({
    queryKey: ["company-profile", id],
    queryFn: () => api.companyProfile(id),
  });
  ...
  const { company: c, coverage } = data;   // c = data.company
  ...
  <div className="num text-2xl font-semibold">{rupees(c.current_price)}</div>
  ```
- `c` is `data.company`, so `c.current_price` comes from the **profile** payload,
  not from any market fetch.

### Step 2 — API client
`frontend/src/lib/api.ts`
```ts
companyProfile: (id: string) =>
  request<CompanyProfile>(`/api/v1/companies/${id}/profile`),
```
→ HTTP `GET /api/v1/companies/{id}/profile`

### Step 3 — API endpoint (route)
`backend/app/api/v1/companies.py`
```python
@router.get("/{company_id}/profile", response_model=CompanyProfile, ...)
def get_company_profile(company_id, svc=Depends(_service), ...):
    profile = svc.profile(company_id)
    ...
    return profile
```
`_service` → `CompanyService(db)`.

### Step 4 — Service
`backend/app/services/company_service.py`

`CompanyService.profile(company_id)`:
```python
ctx = self.load_context(company_id)          # company + financials
...
profile = CompanyProfile(
    company=CompanyDetail.model_validate(ctx.company),   # <-- price comes from here
    ...
)
```

`load_context(company_id)`:
```python
company = self.get(company_id)          # -> self.db.get(Company, company_id)
return CompanyContext(company, self.load_financials(company_id))
```

`get(company_id)`:
```python
def get(self, company_id: str) -> Company | None:
    return self.db.get(Company, company_id)   # ONE ORM read of the Company row
```

`CompanyDetail.model_validate(ctx.company)` copies `current_price` verbatim from the
ORM object (`from_attributes=True`, `backend/app/schemas/company.py`).

### Step 5 — Model / schema
`backend/app/models/company.py:91`
```python
current_price: Mapped[float | None] = mapped_column(Float)  # per share
```
A plain persisted column on the `Company` row. It is **not** a computed/proxy
property and does not hit any provider.

### The chain ends here.
Router → Provider → Upstox do not run. The value returned is whatever is stored in
the `companies.current_price` column.

---

## Where the stale database value is injected

### Injection point 1 (the one that matters) — `CompanyService.get()`
`backend/app/services/company_service.py`
```python
return self.db.get(Company, company_id)
```
This is the only source of `current_price` on the company page. It is a direct read
of the `Company` row, so the displayed price is exactly as stale as that column.

### Who writes `Company.current_price`? (proves it is not live)
`grep -rn "current_price" backend/app --include=*.py` shows the only assignments:
- `backend/app/data/ingest.py:400` — `company.current_price = price` (batch ingestion)
- `backend/app/db/seed.py:163` and `:342` — seed data
- `backend/app/services/us_pipeline/provisioning.py:228` — US pipeline provisioning

None of these run per-page or per-tick. They are one-off/batch jobs, so the price on
the company page is whatever the last of those jobs wrote — not a live quote.

### Injection point 2 (secondary, in the market layer itself)
`backend/app/data/providers/router.py:415` — `MarketDataRouter._from_internal_db()`:
```python
snapshot.quote = Quote(price=company.current_price)
```
This is the "Internal Financial Database" tier. When it serves, the price it returns
is the same stale `Company.current_price` column — the code even comments it:
```python
# Not live — as of the last ingestion — which is why the source is reported...
snapshot.unavailable.append("figures are as of the last ingestion, not live")
```
So even the `/api/v1/market/{ticker}` endpoint can serve the stale DB price when the
external tiers fail (for an Indian symbol the internal tier actually runs *before* the
externals — `_chain_for` returns `["internal", "documents", "external"]`).

---

## Contrast: the path that WOULD produce a live price (but is not used by the company page)

`backend/app/api/v1/market.py` → `GET /api/v1/market/{ticker}`

1. `market_snapshot()` → `get_router().fetch(ticker, db=db, use_cache=not refresh, ...)`
2. `MarketDataRouter.fetch()` (`router.py:239`)
   - `resolve(ticker)` → `ResolvedSymbol` (`providers/symbols.py`)
   - checks `TTLCache` (300 s TTL) → `cache.get(key)` / `cache.put(key, result)`
   - `_chain_for(resolved)` → for an Indian symbol `["internal", "documents", "external"]`
3. For the external tier: `self._try(provider, resolved, attempted, ...)` loops over
   `self.providers` = `[FinnhubProvider(), FMPProvider(), YahooProvider()]` (sorted by
   `priority`). Each call:
   - `provider.configured()` — FMP checks `settings.FMP_API_KEY`
   - `provider.fetch(resolved.canonical, ...)`
   - `FMPProvider.fetch` → `_call(...)` → `BaseMarketProvider._get_json(url, redact=key)`
     → `urllib.request.urlopen(...)` against `financialmodelingprep.com/api/v3|/stable`
   - `_get_json` applies the shared retry/throttle/circuit policy
4. Result wrapped in `MarketDataResult` → `as_dict()` with `source`, `fell_back`,
   `providers_attempted`, `latency_ms`, `cached`.
5. Response names the serving tier (FMP / Yahoo / Finnhub / Internal / Documents).

**Upstox does not appear here either.** There is no Upstox client, no Upstox API URL,
no Upstox auth, no symbol mapping to Upstox.

---

## Summary of the full execution path

```
Company page (frontend/src/app/companies/[id]/page.tsx)
  └─ useQuery  ->  api.companyProfile(id)            frontend/src/lib/api.ts:103
       └─ GET /api/v1/companies/{id}/profile        HTTP
            └─ get_company_profile                  backend/app/api/v1/companies.py
                 └─ CompanyService.profile(id)       backend/app/services/company_service.py
                      └─ load_context(id)
                           ├─ CompanyService.get(id)   -> db.get(Company, id)   ★STALE DB READ
                           └─ load_financials(id)      -> financials (not price)
                      └─ CompanyDetail.model_validate(company)   ★ copies current_price from ORM
                 └─ CompanyProfile.company.current_price  -> JSON
            └─ { ... company: { current_price: <DB value> } ... }
       └─ data.company.current_price -> rupees(c.current_price)  → rendered ₹ price
```

**The stale value is injected at `CompanyService.get()`**
(`self.db.get(Company, company_id)`) — `backend/app/services/company_service.py:65` —
and passed through unchanged by `CompanyDetail.model_validate(ctx.company)`. The
router/provider/Upstox legs are never part of this page's path, and Upstox does not
exist as a provider in this codebase.

To make the company page show a live price, it would have to be wired to the market
router (e.g. `MarketDataRouter.fetch("RELIANCE", db=db)`), which currently only backs
`/api/v1/market/{ticker}` and, for Indian symbols, prefers the same stale internal
tier unless an external provider answers.
