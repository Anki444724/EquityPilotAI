# Phase 3 — US Company Research Pipeline

Deployed and verified in production on 2026-08-01. Commit `06ae8fd`,
deployment `9d969707`.

## The gap

`/market/AAPL` returned a live Finnhub quote and `/filings/AAPL` returned ten
SEC filings, but `/company/AAPL/ai/research-report` returned **404 unknown
ticker**. Every analysis path in the platform starts from a `Company` row, and
no US company had one. Market data without a company record is a quote; the
research product needs statements, and statements need somewhere to live.

## The unit problem, and why it mattered most

The platform was built for Indian listings, so `₹ crore` was not a variable —
it was a literal in twenty-four evidence lines, plus the forecast and valuation
blocks. Left alone, the first US report would have told the language model that
Apple's revenue was **416,161 ₹ cr**, and the model would have written it up
faithfully: the figure is real, the citation resolves, and nothing downstream
could flag it.

`domain/financials/reporting_unit.py` makes the unit a property of the company.
`companies` gains `currency` and `reporting_scale` (migration `b7c31f0a2d54`),
both `NOT NULL` with server defaults of `INR` / `crore`, so the 135 existing
companies are explicitly unchanged.

**Scale is part of a stored value's meaning, not a display choice.** Indian
statements are stored in crore (10⁷), US in millions (10⁶). A wrong scale is a
factor-of-ten error that no formatting test would catch, so currency and scale
travel together on the company record.

## Components

| Module | Responsibility |
|---|---|
| `us_pipeline/fmp_client.py` | FMP `/stable`, cached through the Phase 2 cache |
| `us_pipeline/statement_mapper.py` | US GAAP → the 54 canonical line items |
| `us_pipeline/provisioning.py` | Creates the company, writes canonical facts |
| `us_pipeline/seed.py` | Provisions AAPL, MSFT, NVDA, GOOGL, AMZN |

`AnalysisService.for_ticker` provisions a US listing on first request, so an
unknown US ticker simply works. `provision=False` keeps seeds and tests offline.

New endpoint: `POST /api/v1/us/provision/{ticker}` for warming a company ahead
of a demonstration or forcing a refresh after a new 10-K.

## Deliberately partial mapping

US companies populate **63–74%** of the canonical items, against ~100% for
Indian ones. US GAAP does not present Schedule III's lines: there is no
"purchase of stock in trade", and cost of revenue aggregates what Schedule III
splits across several rows.

Deriving those items from a plausible-looking split would manufacture a figure
that appears in a report, carries a citation, and exists in no filing. That is
the single failure mode this platform exists to prevent, so absent items stay
absent and are reported as coverage. The context builder already renders them
as gaps and the analyst is instructed to say the figure is not held.

## Five mapping defects, found by reconciling against the filed 10-K

Each was caught by asserting the platform's *derived* figures against Apple's
FY2025 10-K. Every one of them produced output that read perfectly.

| ID | Defect | Effect |
|---|---|---|
| **US-001** | D&A mapped as an income-statement expense. Under US GAAP it is already inside cost of revenue; the FMP field is a supplementary disclosure. | PAT understated by **$11,698M (11%)**, inherited by every margin, valuation and score |
| **US-002** | Equity took `retainedEarnings` only, omitting accumulated OCI | Shareholders' equity overstated by **$5,571M** |
| **US-003** | `weighted_shares` excluded from scaling, on the reasoning that a share count is not money | EPS **$0.0000075 instead of $7.49** — a factor of 10⁶ |
| **US-004** | `incomeTaxesPaid` subtracted in the CFO derivation, but FMP's cash-flow statement already starts from net income | Taxes counted twice, **−$43,369M** |
| **US-005** | With US-001 fixed, D&A was missing from the CFO non-cash add-backs | CFO understated by **$11,698M** |

US-003 is the instructive one. My reasoning — "a share count is a count, not
money, so it must not be divided by a million" — sounded right and was wrong.
The platform's own Indian data disproves it: TCS stores PAT as 49,454 (₹ crore)
*and* weighted shares as 363.6 (crore), so EPS is `pat / shares` and the scale
cancels. The invariant is that **shares carry the same scale as money**.

US-001 and US-005 look contradictory and are not: the same field is
double-counting as an expense on the income statement and required as an
add-back in the cash-flow statement.

### A measured provider boundary

FMP's free tier rejects `limit > 5` with **HTTP 402** and returns *no data at
all* — not a truncated five. Requesting the platform's usual ten years of
history therefore yielded zero statements while the profile call succeeded,
which is indistinguishable from a mapping bug. The limit is now clamped, and
the reason is recorded beside the constant.

## Reconciliation — Apple FY2025

All eight headline figures, derived by the platform's own engines from the
mapped canonical facts, against the filed 10-K:

| Metric | Platform | 10-K | Delta |
|---|---|---|---|
| Revenue | 416,161.00 | 416,161.00 | 0.0 |
| EBIT | 133,050.00 | 133,050.00 | 0.0 |
| Profit after tax | 112,010.00 | 112,010.00 | 0.0 |
| EPS (basic) | 7.49 | 7.49 | 0.00 |
| Total assets | 359,241.00 | 359,241.00 | 0.0 |
| Shareholders' equity | 73,733.00 | 73,733.00 | 0.0 |
| Cash flow from operations | 111,482.00 | 111,482.00 | 0.0 |
| Free cash flow | 98,767.00 | 98,767.00 | 0.0 |

All in USD millions.

## Production verification

| Check | Result |
|---|---|
| Migration applied | `/health/ready` schema **complete** |
| Provision AAPL | 187 facts, FY2021–FY2025, 70.4% coverage |
| AAPL research report | **15 sections, 14 grounded**, 26,957 tokens, $0.0054, 11.2 s, 3.57× concurrency |
| Pipeline used | SEC EDGAR → Finnhub → FMP → Annual Reports (RAG) |
| On-demand provisioning | **NFLX** — never seeded — full report in 13.6 s |
| Currency separation | AAPL report: **0 `₹`, 23 `$ M`**. TCS report: **25 `₹`, 0 `$ M`** |
| Indian regression | TCS 15/15 grounded, India pipeline, figures in crore ₹ |
| Perimeter suite | **33/33** |
| Authenticated modules | **18/19** (1 warning: no portfolio seeded) |
| Documents intact | 7 docs / 313 chunks / 108 pages; PDF md5 unchanged |
| Cache backend | RedisCache, 20 entries |

Test suite: **2,195 passing, 0 failures** (2,160 + 35 new).

## Deployment note

Railway's backend service has **no repository trigger** (`repoTriggers` is
empty), so pushing to `main` does not deploy. `serviceInstanceRedeploy`
redeploys the *pinned* commit, which silently rebuilt `de06c8c` twice and
looked like a successful deploy of Phase 3. The working call is
`serviceInstanceDeployV2(environmentId, serviceId, commitSha)`. Worth knowing
before the next deploy: verify the deployed SHA rather than the deployment
status.

## Known limitations

- **Five years of history, not ten.** A free-tier ceiling, not a design
  choice. The platform's forecast and valuation engines run on five years but
  have less trend to work with than for an Indian company with twelve.
- **63–74% canonical coverage** for US companies, as described above. Reported
  honestly rather than filled in.
- **No US annual reports ingested.** The US pipeline lists Annual Reports (RAG)
  fourth, but no US PDF has been uploaded, so Business Model, Risks and
  Management Commentary fall through to the financial database. Uploading a
  10-K would populate them exactly as the TCS report demonstrates.
- **SEC XBRL company-facts API returns 403 from this sandbox**, so independent
  reconciliation used FMP's own filing data plus internal consistency checks
  (gross profit, balance-sheet identity). The figures tie to Apple's published
  10-K, but the cross-check is not from a second independent source.
- **Segment and geographic data are unavailable** for US companies, so Revenue
  Segments is ungrounded until a 10-K is ingested.
- **The repository is public.** Flagged repeatedly; still not acted upon.
