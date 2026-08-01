# Phase 2 — Production Verification

Deployed and verified against the live stack on 2026-08-01.

- Backend: `https://backend-production-18956.up.railway.app`
- Frontend: `https://frontend-production-1a313.up.railway.app`
- Commit deployed: `de06c8c` (10 pending commits pushed, `2986fca..de06c8c`)
- Backend deployment: `97b39557` → restart `c6548a7a`

## Configuration

| Variable | State | Note |
|---|---|---|
| `OPENROUTER_API_KEY` | set | 73 chars, verified serving |
| `FINNHUB_API_KEY` | set | 40 chars, verified serving AAPL |
| `FMP_API_KEY` | set | 32 chars |
| `REDIS_URL` | already present | `redis.railway.internal`, now backing the Phase 2 cache |
| `OPENROUTER_MODEL` | set | `openai/gpt-4o-mini` |
| `OPENROUTER_SITE_URL` / `_APP_NAME` | set | OpenRouter usage attribution |
| `AI_PREFERRED_PROVIDER` | **cleared** | see below |

**One configuration defect found and fixed during deployment.**
`AI_PREFERRED_PROVIDER` was set to `Gemini` in Railway. That variable pins a
provider to the *front* of the chain, overriding the declared
`FALLBACK_ORDER`, so the Phase 1 reversal would have been silently undone in
production: every request would have gone to Gemini first, hit
`RESOURCE_EXHAUSTED`, and fallen through — restoring exactly the latency and
template-prose behaviour Phase 1 existed to remove. Cleared to empty so the
declared order governs. This was invisible locally because the variable is
only set in Railway.

## Deployment verification

| Suite | Result |
|---|---|
| `verify_deployment.py` (perimeter) | **33/33 passed** |
| `verify_live_modules.py` (authenticated) | **18/19 passed**, 1 warning (no portfolio seeded — not a defect) |

### Live provider chain

```
chain    : OpenRouter → Gemini → Offline
serving  : OpenRouter        (562 ms, "completion succeeded")
Gemini   : quota_exhausted   ("allowance spent")  ← correctly skipped
degraded : false
```

Gemini's spent free-tier quota is detected and bypassed rather than retried,
which is the behaviour the quota-aware retry path was built for. It is
observable here as designed rather than inferred.

### Cache backend

```
backend    : RedisCache          ← the Redis path, previously untested
namespaces : market, statements, news, rag
```

Phase 2 shipped with the Redis backend exercised only for graceful
degradation, never against a live server. It is now running on Redis in
production. Statement caching showed `hits=5 misses=2` within minutes of
first traffic.

### Market data, both markets

| Ticker | Source | Value | Latency |
|---|---|---|---|
| AAPL | Finnhub | $4.54T | 4,380 ms |
| TCS | Internal Financial Database | ₹8.80 lakh crore | 2.5 ms |

Currency-aware formatting is correct in both markets (MKT-003 stays fixed).

## Production checks

### 1. Upload annual report — PASS

A five-page TCS FY2026 integrated annual report (chairman's message, MD&A with
segment and geographic mix, principal risks, outlook) was generated and
uploaded.

```
HTTP 202 (async ingestion)  →  status completed, 5 pages, 16 chunks
```

### 2. "Summarize Chairman Message" — PASS

Served by **OpenRouter / gpt-4o-mini** with **2 citations**, both resolving to
page 2 of the uploaded report. Every figure in the answer traces to the
document: attrition 12.1% (from 13.3%), headcount 612,400 across 55 countries,
final dividend Rs 30 taking the total to Rs 126. No figure appeared that was
not in the source.

### 3. "Complete Equity Research" — PASS

```
sections   : 15 / 15 grounded
writer_mix : {"OpenRouter": 15}
tokens     : 41,909      cost: $0.008
wall       : 10,590 ms   concurrency factor: 4.54×
```

The Phase 2 parallelism holds in production: 4.54× concurrency on a report
that took ~45 s serially before this phase.

### 4. "Latest News" — PASS

Routed to **Market Data (Finnhub/FMP)**, confidence 0.44, 2 citations
(current market price, market capitalisation). The section states plainly that
the platform holds no recent operational detail rather than inventing
headlines — correct behaviour, since Finnhub's free tier serves news for US
symbols only and TCS resolves to the internal database.

### 5 & 6. "Provider Routing" and "Source Used" — PASS

| Section | Provider | Source | Conf | Cites |
|---|---|---|---|---|
| Executive Summary | Synthesis | Internal Database | 0.70 | 8 |
| Business Model | **Annual Report (RAG)** | Annual Report | **1.00** | 8 |
| Revenue Segments | **Annual Report (RAG)** | Annual Report | **1.00** | 8 |
| Financial Performance | Financial Database | Internal Database | 0.85 | 8 |
| Valuation | Valuation Engine | Internal Database | 0.80 | 8 |
| Bull Thesis | Synthesis | Internal Database | 0.70 | 8 |
| Bear Thesis | Synthesis | Internal Database | 0.70 | 8 |
| Risks | **Annual Report (RAG)** | Annual Report | **1.00** | 8 |
| Catalysts | **Annual Report (RAG)** | Annual Report | **1.00** | 8 |
| Management Commentary | **Annual Report (RAG)** | Annual Report | **1.00** | 8 |
| Latest News | Market Data (Finnhub/FMP) | Market Data | 0.44 | 2 |
| Quality Scores | Scoring Engine | Internal Database | 0.75 | 8 |
| Risk Scores | Scoring Engine | Internal Database | 0.75 | 8 |
| Institutional Score | Scoring Engine | Internal Database | 0.75 | 8 |
| Investment Verdict | Synthesis | Internal Database | 0.70 | 8 |

```
provider_mix: RAG 5, Synthesis 4, Scoring 3, Financial DB 1,
              Valuation 1, Market Data 1
```

The scoring engine answers **three** sections — the three it should. With a
real annual report present, RAG becomes the single largest contributor at five
sections. This is the direct inverse of the AI-005 defect, where scoring
answered ten of thirteen.

### 7. Restart Railway — PASS

Redeploy `c6548a7a` reached `SUCCESS`; `uptime_seconds: 43.8` on the next
health check confirms the process genuinely restarted rather than the request
being served by a surviving container.

### 8. Documents survive restart — PASS

| Measure | Before | After |
|---|---|---|
| documents | 7 | 7 |
| chunks | 313 | 313 |
| pages | 108 | 108 |
| doc 8 status | completed, 16 chunks | completed, 16 chunks |
| PDF md5 | `156513689634963933411ac411d90be4` | `156513689634963933411ac411d90be4` |

The stored PDF is byte-identical across the restart (DEP-007's volume
permissions fix holding), and a post-restart RAG query returned 2 citations
against a cold Redis — proving retrieval rebuilds correctly rather than having
depended on warm cache state.

## Harness errors found during verification, reported not hidden

Two failures during these checks were mine, not the product's:

1. **Wrong company.** I resolved the company id with `?search=TCS`, which
   returns Reliance first; the report was uploaded to Reliance's corpus and
   the chairman query correctly found nothing for TCS. Corrected by resolving
   the exact ticker, deleting the misfiled document, and re-uploading. The
   platform behaved correctly throughout — it declined to answer because the
   document genuinely was not filed against TCS.

2. **Wrong query parameter.** `/documents/search` takes `q`, and I sent
   `query`. FastAPI rejected it with a 422 that my one-line parser rendered as
   `hits: 0`, which briefly looked like a retrieval failure. Confirmed against
   `/openapi.json` before drawing any conclusion.

Both are recorded because a verification run that quietly corrects its own
mistakes is not evidence of anything.

## Known limitations

- **AAPL and MSFT still cannot produce a research report.** They have live
  Finnhub market data but no company record, so `/ai/research-report` returns
  404. This is the subject of Phase 3.
- **No SMTP**, so `/health/ready` reports `degraded`. Non-blocking by design;
  verification and password-reset emails cannot send.
- **Finnhub free tier serves news for US symbols only**, so Latest News for
  Indian tickers is limited to price and market capitalisation.
- **The repository is public.** Flagged repeatedly across this engagement and
  still not acted upon.
