# Phase 2 — Production Benchmark

Measured on the live stack: real OpenRouter calls, real retrieval, real database reads. Every figure below is observed, not modelled.

## 1. Serial vs parallel section generation

Same evidence, same writer, same fifteen sections. The only variable is whether sections within a stage run concurrently.

| Ticker | Serial wall | Parallel wall | Speed-up | Tokens serial | Tokens parallel | Cost serial | Cost parallel |
|---|---|---|---|---|---|---|---|
| TCS | 40,409 ms | 10,706 ms | **3.77×** | 27,250 | 27,369 | $0.00544 | $0.00551 |
| RELIANCE | 46,168 ms | 12,849 ms | **3.59×** | 27,751 | 27,394 | $0.00567 | $0.00545 |

Token count and cost are essentially unchanged, which is the point: parallelism buys latency, not efficiency. The same work is done, overlapped rather than queued.

## 2. Where the time goes

| Ticker | Wall | Retrieval (sum) | LLM (sum) | Overhead (sum) | Concurrency factor | LLM share |
|---|---|---|---|---|---|---|
| TCS | 10,706 ms | 10 ms | 39,656 ms | 4 ms | 3.71× | 100.0% |
| RELIANCE | 12,849 ms | 8 ms | 44,902 ms | 3 ms | 3.50× | 100.0% |

Retrieval, statement loading and prompt assembly are together a rounding error against the model call. That is the finding that justifies where the optimisation effort went: no amount of database tuning would have moved a number that is 99% network wait on an external API.

## 3. Cache effect

| Ticker | Cold wall | Warm wall | Change | Cold tokens | Warm tokens | Cold cost | Warm cost |
|---|---|---|---|---|---|---|---|
| TCS | 10,706 ms | 34 ms | **316.7×** | 27,369 | 27,369 | $0.00551 | $0.00000 |
| RELIANCE | 12,849 ms | 32 ms | **399.0×** | 27,394 | 27,394 | $0.00545 | $0.00000 |

The warm run is served from the provider's completion cache, so it costs nothing and returns almost immediately. This is the repeat-view case — a user re-opening a report they just generated — and it is the reason the completion cache exists. It is **not** a measure of generation speed, and is reported separately for that reason.

Read the warm token column carefully: it reports the token count of the *cached response*, not tokens purchased on the warm run. No request left the process, which is why the warm cost is $0.00000. The figures are retained rather than zeroed so the two rows describe the same artefact — zeroing them would suggest the warm run returned a smaller report, which it did not.

## 4. Per-section attribution (parallel, cold)

### TCS

| Section | Retrieval | LLM | Overhead | Total | Tokens | Cost |
|---|---|---|---|---|---|---|
| Executive Summary | 0 ms | 2,366 ms | 0 ms | 2,366 ms | 2,616 | $0.00049 |
| Business Model | 2 ms | 3,497 ms | 0 ms | 3,499 ms | 1,980 | $0.00042 |
| Revenue Segments | 2 ms | 1,317 ms | 0 ms | 1,319 ms | 1,719 | $0.00026 |
| Financial Performance | 0 ms | 4,007 ms | 0 ms | 4,008 ms | 2,029 | $0.00045 |
| Valuation | 0 ms | 3,005 ms | 0 ms | 3,005 ms | 1,493 | $0.00032 |
| Bull Thesis | 0 ms | 2,612 ms | 0 ms | 2,612 ms | 2,621 | $0.00050 |
| Bear Thesis | 0 ms | 3,396 ms | 0 ms | 3,397 ms | 2,723 | $0.00056 |
| Risks | 2 ms | 4,855 ms | 0 ms | 4,857 ms | 2,076 | $0.00048 |
| Catalysts | 2 ms | 2,835 ms | 0 ms | 2,837 ms | 1,944 | $0.00040 |
| Management Commentary | 1 ms | 0 ms | 0 ms | 1 ms | 0 | $0.00000 |
| Latest News | 1 ms | 1,998 ms | 0 ms | 2,000 ms | 1,009 | $0.00019 |
| Quality Scores | 0 ms | 2,508 ms | 0 ms | 2,508 ms | 1,515 | $0.00032 |
| Risk Scores | 0 ms | 2,812 ms | 0 ms | 2,812 ms | 1,562 | $0.00035 |
| Institutional Score | 0 ms | 1,634 ms | 0 ms | 1,634 ms | 1,439 | $0.00027 |
| Investment Verdict | 0 ms | 2,814 ms | 0 ms | 2,814 ms | 2,643 | $0.00052 |

### RELIANCE

| Section | Retrieval | LLM | Overhead | Total | Tokens | Cost |
|---|---|---|---|---|---|---|
| Executive Summary | 0 ms | 4,435 ms | 0 ms | 4,435 ms | 2,731 | $0.00055 |
| Business Model | 1 ms | 3,437 ms | 0 ms | 3,439 ms | 2,004 | $0.00042 |
| Revenue Segments | 1 ms | 1,595 ms | 0 ms | 1,596 ms | 1,733 | $0.00026 |
| Financial Performance | 0 ms | 4,360 ms | 0 ms | 4,360 ms | 2,039 | $0.00045 |
| Valuation | 0 ms | 2,765 ms | 0 ms | 2,765 ms | 1,483 | $0.00031 |
| Bull Thesis | 0 ms | 4,018 ms | 0 ms | 4,019 ms | 2,710 | $0.00055 |
| Bear Thesis | 0 ms | 3,835 ms | 0 ms | 3,836 ms | 2,708 | $0.00055 |
| Risks | 1 ms | 1,287 ms | 0 ms | 1,289 ms | 1,734 | $0.00026 |
| Catalysts | 1 ms | 3,745 ms | 0 ms | 3,747 ms | 1,995 | $0.00042 |
| Management Commentary | 1 ms | 0 ms | 0 ms | 1 ms | 0 | $0.00000 |
| Latest News | 1 ms | 2,228 ms | 0 ms | 2,230 ms | 1,028 | $0.00019 |
| Quality Scores | 0 ms | 2,962 ms | 0 ms | 2,962 ms | 1,531 | $0.00032 |
| Risk Scores | 0 ms | 3,491 ms | 0 ms | 3,492 ms | 1,549 | $0.00034 |
| Institutional Score | 0 ms | 2,828 ms | 0 ms | 2,828 ms | 1,425 | $0.00026 |
| Investment Verdict | 0 ms | 3,914 ms | 0 ms | 3,915 ms | 2,724 | $0.00056 |

## 5. Pipelines

- **TCS** (India): Annual Reports (RAG) → NSE Corporate Filings → BSE Corporate Announcements → Screener Financial Pipeline → Finnhub → FMP → Yahoo Finance
- **RELIANCE** (India): Annual Reports (RAG) → NSE Corporate Filings → BSE Corporate Announcements → Screener Financial Pipeline → Finnhub → FMP → Yahoo Finance

## 6. Cache statistics after the run

| Namespace | Hits | Misses | Hit rate | TTL |
|---|---|---|---|---|
| market | 0 | 0 | 0.0% | 300s |
| statements | 0 | 1 | 0.0% | 3600s |
| news | 0 | 0 | 0.0% | 900s |
| rag | 6 | 6 | 50.0% | 1800s |

Backend: `MemoryCache`, 7 entries resident.

### Steady-state hit rates

The table above is measured across a benchmark that deliberately clears the
caches between runs, so it understates them: `statements` shows a 0% hit rate
because it was purged immediately before each of its two reads.

A separate steady-state run — five reports over three companies, caches left
alone, which is how the platform actually behaves in production — gives:

| Namespace | Hits | Misses | Hit rate |
|---|---|---|---|
| statements | 2 | 3 | 40.0% |
| rag | 12 | 18 | 40.0% |
| market | 0 | 0 | n/a (no external fetch in this run) |
| news | 0 | 0 | n/a |
| **overall** | **14** | **21** | **40.0%** |

40% is the honest figure for a cold process warming up over five reports, and
it rises with uptime: the misses are almost entirely first-touch per company
per section-question, and those are paid once per TTL rather than once per
report. `market` and `news` record nothing here because these two companies
resolve from the internal database rather than an external provider, so no
market fetch occurred to cache.

