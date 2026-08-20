# EquityPilotAI — Blogger prototypes

Three separate deliverables live in this folder. They do not depend on each other, and none of
them touches the `frontend/` or `backend/` projects.

| File | What it is | Where it goes |
|---|---|---|
| **`equitypilotai-api-simulation-theme.xml`** | **The API simulation packaged as a full Blogger theme.** One `<b:skin>`, valid theme XML. | Blogger → **Theme → Edit HTML** (or Theme → Restore → upload) |
| `equitypilotai-api-simulation.html` | The identical application as a gadget fragment | Blogger → Layout → Add a Gadget → HTML/JavaScript |
| `equitypilotai-blogger-theme.xml` | Earlier 15-company build, as a theme | Theme → Edit HTML |
| `equitypilotai-blogger.html` / `index.html` | Earlier 15-company build, as a gadget | reference only |

### Which file goes where

Blogger has two completely different editors and they reject each other's content:

* **Theme → Edit HTML** validates a whole theme and requires exactly one `<b:skin>`. Pasting a
  gadget fragment there produces `There should be one and only one skin in the theme, and we
  found: 0`. Use `equitypilotai-api-simulation-theme.xml`.
* **Layout → Add a Gadget → HTML/JavaScript** takes a fragment and rejects nothing. Use
  `equitypilotai-api-simulation.html`.

Both files contain the same application, the same 500-company mock database and the same API
layer. Install one, not both.

---|---|---|
| **`equitypilotai-api-simulation.html`** | **Frontend-only API simulation: local API layer + 500-company mock database.** The current build. | Blogger → Layout → Add a Gadget → **HTML/JavaScript**, or a Blogger page in HTML view |
| `equitypilotai-blogger-theme.xml` | The same application packaged as a full Blogger theme | Blogger → Theme → Edit HTML |
| `equitypilotai-blogger.html` / `index.html` | The earlier, smaller gadget build | reference only |

---

# A. The paste-ready files

**Theme install (recommended for you):** `equitypilotai-api-simulation-theme.xml`, 2,799 lines.
Back up your current theme first (Theme → arrow next to Customise → Backup), then Theme → Edit
HTML, select all, delete, paste, Save. Structure: one `<html b:version='2'>` root, one `<head>`
with `<b:include data='blog' name='all-head-content'/>` and exactly one
`<b:skin><![CDATA[ … ]]></b:skin>` holding all 21,047 characters of CSS, one `<body>` carrying the
application markup directly plus an inert `<b:section>`/`<b:widget>` pair so Layout stays valid,
and one `<script>` with the application inside `//<![CDATA[ … //]]>`.

**Gadget install:** `equitypilotai-api-simulation.html` — one self-contained fragment, 2,745 lines.

* One root element: `<div id="epa-root" class="epa">`, nothing before or after it.
* CSS inside `<style type="text/css">` wrapped in `/*<![CDATA[*/ … /*]]>*/`, every rule scoped under `.epa`.
* JavaScript inside `<script type="text/javascript">` wrapped in `//<![CDATA[ … //]]>`.
* Zero HTML comments anywhere, so no comment can contain `--`.
* No `<html>`, `<head>`, `<body>`, `<b:skin>` or any Blogger theme XML.
* No React, no Next.js, no TypeScript, no imports, no modules, no external library, no CDN, no `fetch`.

Paste it, save, done. Local preview of the same file: `build/api-sim-preview.html`.

---

# B. API architecture

```
Blogger UI  (hash router, pages, components)
      |
      v
Local API layer            API.dashboard(), API.company(t), ... 16 methods
      |                    every call is async, timed, logged
      v
apiCall(endpoint, request, producer)      <-- the single choke point
      |
      v
Mock database              500 companies, lazy statements, lazy OHLCV
      |
      v
Business logic             CAGRs, ratios, peer medians, AI scoring engine
      |
      v
UI components              tables, SVG charts, score cards, verdict cards
```

Every read passes through `apiCall`, which:

1. starts a timer,
2. waits `CONFIG.LATENCY_MIN_MS` to `CONFIG.LATENCY_MAX_MS` so the UI behaves like it is on a network,
3. runs the producer inside `try/catch`, turning throws into an `ApiError {status, endpoint, message}`,
4. pushes an entry into the debug log (endpoint, request, status, ms, records, mode),
5. resolves or rejects a `Promise`.

Going live later touches only the producer bodies:

```js
CONFIG.USE_MOCK = false;
CONFIG.API_BASE = "https://your.api";
// then replace each producer with the http() helper already present next to apiCall()
```

Nothing above the API layer changes: the pages only ever see a promise.

**Performance.** Booting 500 companies builds only the light summary record for each (about 4 ms).
Statements, ratios and OHLCV are generated on first request and cached per ticker, so a session
that visits ten companies never pays for the other 490. Tables render the current page only
(10/25/50/100 rows) and rows are appended in 40-row frames so the main thread is never blocked.

---

# C. API methods

| Method | Simulated endpoint | Returns |
|---|---|---|
| `API.dashboard()` | `GET /api/v1/dashboard` | coverage, indices, largest, gainers, losers, sector aggregates |
| `API.companies(params)` | `GET /api/v1/companies` | `{total, page, pages, pageSize, sort, dir, filters, results}` |
| `API.company(ticker)` | `GET /api/v1/companies/{t}` | company record, highlights, shareholding, about |
| `API.financials(ticker)` | `GET /api/v1/companies/{t}/financials` | 6 fiscal years plus derived CAGRs, margins, D/E |
| `API.pnl(ticker)` | `GET /api/v1/companies/{t}/pnl` | profit and loss rows by fiscal year |
| `API.balanceSheet(ticker)` | `GET /api/v1/companies/{t}/balance-sheet` | balance sheet rows |
| `API.cashFlow(ticker)` | `GET /api/v1/companies/{t}/cash-flow` | cash flow rows |
| `API.ratios(ticker)` | `GET /api/v1/companies/{t}/ratios` | 16 ratios per fiscal year |
| `API.sharePrice(ticker, period)` | `GET /api/v1/companies/{t}/share-price` | quote, performance, OHLCV series (1M/3M/6M/1Y) |
| `API.news(ticker)` | `GET /api/v1/companies/{t}/news` | 10 simulated items with source, category, sentiment |
| `API.peers(ticker)` | `GET /api/v1/companies/{t}/peers` | peer rows plus `sectorMedian` |
| `API.aiAnalysis(ticker)` | `POST /api/v1/companies/{t}/ai-analysis` | six scores, verdict, confidence, positives, risks, reasoning |
| `API.search(query, params)` | `GET /api/v1/search` | same envelope as `companies` |
| `API.watchlist()` | `GET /api/v1/watchlists` | lists with computed rows |
| `API.addWatchlist(ticker, opts)` | `POST /api/v1/watchlists/entries` | `{added, listId, entries}` |
| `API.removeWatchlist(ticker, opts)` | `DELETE /api/v1/watchlists/entries/{t}` | `{removed, listId}` |

Extras on the same transport: `API.createWatchlist(name)`, `API.deleteWatchlist(id)`,
`API.sectors()`, `API.meta()`.

Usage is exactly as specified:

```js
API.company("MGL").then(function (data) {
  // render company
});
```

Everything is also exposed for the browser console as `window.EquityPilotAI`
(`.API`, `.DB`, `.CONFIG`, `.DEBUG`).

---

# D. Mock database structure

**500 companies.** About 125 are real NSE/BSE names (`dataSource: "REFERENCE_MOCK"`), the rest are
generated (`dataSource: "SYNTHETIC_MOCK"`). Every record carries `isMock: true`, a
`dataDisclaimer` and `dataVersion`, so a synthetic record can never be mistaken for live data.
The company header shows the same flag as a badge.

```js
{
  id: "epa-1000", ticker: "MGL", name: "Mahanagar Gas Ltd",
  exchange: "NSE", isin: "INE...", sector: "Utilities", industry: "Gas Distribution",
  marketCap: 14012, marketCapCategory: "Mid Cap",
  currentPrice: 1418.35, previousClose: 1405.5, dayChange: 12.85, dayChangePercent: 0.0091,
  "52WeekHigh": 1802.4, "52WeekLow": 902.1,
  pe: 12.4, pb: 2.1, eps: 114.2, roe: 0.171, roce: 0.223,
  debtToEquity: 0.12, dividendYield: 0.021,
  promoterHolding: 46.8, institutionalHolding: 31.2,
  sharesOutstanding: 9.88, listedYear: 1995,
  isMock: true, dataSource: "REFERENCE_MOCK", dataDisclaimer: "...", dataVersion: "..."
}
```

**Financial years** (6 per company, generated on demand and cached):

```js
{ fiscalYear: 2025, revenue, ebitda, ebit, depreciation, interest, pbt, tax, pat, eps,
  totalAssets, totalLiabilities, equity, debt, cash, netDebt,
  operatingCashFlow, investingCashFlow, financingCashFlow, freeCashFlow, capex, dividendsPaid }
```

**Derived** by `API.financials`: `revenueCagr`, `ebitdaCagr`, `patCagr`, `roe`, `roce`,
`netMargin`, `ebitdaMargin`, `debtToEquity`. `API.ratios` adds ROA, EBIT margin, FCF margin,
net debt to EBITDA, interest cover, asset turnover, cash conversion, effective tax rate,
P/E, P/B and EV to EBITDA per year.

**OHLCV**: `{date, open, high, low, close, volume}`, 22 / 66 / 130 / 250 points for 1M / 3M / 6M / 1Y,
drawn as SVG candlesticks with a volume strip. No chart library.

The figures are internally consistent: PAT is derived from market cap and P/E, equity from PAT and
ROE, so the ratio tables reconcile with the headline metrics instead of contradicting them.

---

# E. Testing each API

**In the UI.** Hash routes, no server rewrites:

```
#/dashboard      #/companies      #/watchlist      #/api-test
#/company/MGL    #/company/MGL/financials   #/company/MGL/pnl
#/company/MGL/balance-sheet      #/company/MGL/cash-flow
#/company/MGL/ratios             #/company/MGL/share-price
#/company/MGL/ai                 #/company/MGL/news    #/company/MGL/peers
```

**In the API Test Console** (`#/api-test`). Type a path and press Execute, or use a preset.
The response is pretty-printed JSON with status, resolution time and record count. Try:

```
/dashboard
/companies?sector=Automobile&sort=pe&dir=asc
/search?q=bank
/company/MGL
/company/MGL/financials
/company/MGL/ratios
/company/MGL/share-price?period=1Y
/company/MGL/peers
/company/MGL/news
/company/MGL/ai
/watchlist
/meta
```

An unknown path or ticker returns a 404 card with the message and a Retry button, and the rest of
the application keeps working.

**In the API debug panel.** The `API` button in the top bar opens a drawer listing the last 60
calls: status, endpoint, request, records returned, response time in milliseconds, and MOCK/LIVE
mode.

**In the browser console.**

```js
EquityPilotAI.API.aiAnalysis("M&M").then(console.log);
EquityPilotAI.API.companies({ sector: "Healthcare", sort: "roe", dir: "desc" }).then(console.log);
EquityPilotAI.DB.companies.length;   // 500
EquityPilotAI.DEBUG.log[0];          // last call record
```

**Other things worth exercising:** sortable column headers, sector and industry filters, page size
10/25/50/100, `Ctrl+K` global search, watchlist add/remove with buy price and buy-below (triggered
status turns green when the mock price is at or below the target), CSV export on the statement
tabs, the dark/light toggle, and the responsive layout below 1024px and 640px.

---

# F. Zero external API calls

Verified programmatically on the final file:

| Probe | Count |
|---|---|
| `XMLHttpRequest` | 0 |
| live `fetch(` calls | 0 (the only occurrence is inside a block comment showing the future `http()` helper) |
| `src=` attributes | 0 |
| CDN references (`cdn.`, `googleapis`, `jsdelivr`, `unpkg`) | 0 |
| `<script src>`, `<link href>`, `@import`, web fonts | 0 |
| `WebSocket`, `EventSource`, `navigator.sendBeacon` | 0 |
| `localStorage` uses | 3 (namespaced `epa:`) |

`CONFIG.API_BASE` is the empty string and `CONFIG.USE_MOCK` is `true`. Every method resolves from
the in-memory database. The AI analysis is a local rule engine: no OpenAI, no Anthropic, no
provider of any kind, and the payload reports `externalCalls: 0` and the banner
**MOCK AI ANALYSIS — NOT LIVE AI**.

---

# G. AWS is not required

Nothing in this build touches AWS, Railway, Render, Vercel, Firebase, Supabase, FastAPI,
PostgreSQL, Redis, or any hosted service. There is no server component, no build step, no
environment variable and no credential. The file is static text that Blogger serves as part of a
gadget; all computation happens in the visitor's browser and the only persistence is
`localStorage` on their own device. Hosting cost and API spend are both zero.

---

## Validation performed on this build

XML/gadget: expat parse of the raw fragment OK, exactly one root element `div`, no stray text,
0 HTML comments, 0 unescaped `&`, 0 literal `<` outside CDATA, 2 well-formed CDATA sections.

Runtime, in jsdom against the real file: 500 companies built in under 5 ms, all 24 required
company fields present on every record, all 16 API methods resolving, 6 fiscal years with all 15
statement fields, all four OHLCV periods, peer medians, AI scores with verdict and confidence,
404 handling, all 14 routes rendering, sorting, filtering and pagination, watchlist persistence,
API console execution and error path, debug panel logging, and the page-level API Error card with
Retry. **0 runtime errors.**
