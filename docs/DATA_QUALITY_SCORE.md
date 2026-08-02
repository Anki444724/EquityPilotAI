# Data Quality Score

Deployed as `8f5c86d`. **503 of 503 companies scored, zero failures.**

Every AI answer now states how complete, current and trustworthy the data
beneath it is — and says so plainly when it is not.

## Live example

`GET /api/v1/company/CIPLA/quality`

```
CIPLA: 65.3/100   Grade B   warning: yes

Adequate coverage; several dimensions incomplete.
Latest annual report available.
Conference call transcripts available.
Financial statements partially complete.
Knowledge Vault populated.
Last update today.

identity                5.00/5      freshness            9.62/10
financial_statements   16.67/20     source_quality       7.14/10
documents               6.43/20     system_health        3.93/5
knowledge_vault         7.50/15
ai_coverage             9.00/15

Missing (13): cash flow statement · previous annual reports ·
investor presentations · credit rating reports · ESG reports · …

Last updated 0 days ago · Next crawl 2026-08-08 · Knowledge freshness 0 days
```

## Universe dashboard

`GET /api/v1/quality/dashboard`

| | |
|---|---|
| Companies scored | **503** |
| Average score | **30.41** |
| Above 90 / 80 / 70 / 60 | 0 / 0 / 0 / **6** |
| Grades | B 6 · C 13 · D 38 · **F 446** |

**Highest:** CIPLA 65.3 · TCS 63.3 · ICICIBANK 62.6 · RELIANCE 62.1 · INFY 61.0
**Lowest:** NVDA / NFLX / AAPL 20.2 · ICICIAMC 20.7

Average points by dimension — this is the useful part:

| Dimension | Avg | Weight | Reading |
|---|---|---|---|
| financial_statements | 15.34 | 20 | Strong — the screener backfill worked |
| freshness | 5.50 | 10 | Adequate |
| identity | 3.47 | 5 | Good |
| source_quality | 3.04 | 10 | NSE-only, no BSE, few IR URLs |
| ai_coverage | 1.89 | 15 | Thin |
| **system_health** | **0.49** | 5 | Few companies have documents at all |
| **documents** | **0.47** | 20 | **The binding constraint** |
| **knowledge_vault** | **0.21** | 15 | Follows from documents |

The average of 30.41 is a real finding, not a scoring artefact: the platform
holds 12 years of financials for all 500 companies but documents for only ~54.
Documents and the vault are worth 35 points combined and contribute 0.68.

## Design decisions

**Weights sum to exactly 100 — asserted at import, not documented.** A silent
drift to 95 would make every score in the platform wrong by a margin nobody
can see.

**Every check reads a real row.** No check infers a result from another
check's success. A scorer that rewards itself for consistency reports 100 for
a company it has never seen.

**Partial credit is proportional, never generous.** One of seven document
classes scores 1/7. The temptation is a floor so a sparse company does not
look bad; the result is a score that cannot distinguish sparse from adequate.

**Freshness decays.** "We hold an annual report" is not the claim "we hold a
*current* annual report". Checks decay linearly to zero at a horizon —
90 days for filings, 400 for annual reports — so a company last covered in
2019 does not rate as fully covered. Linear rather than a cliff: a filing one
day past the horizon is not worthless, and a step function makes scores jump
for no real change in the data.

**System health does not punish absence.** A company with no documents scores
its health checks zero with the detail `"no documents held"`, rather than
being recorded as a pipeline failure. Blaming the platform for a company
nobody uploaded anything for would double-count a gap the DOCUMENTS dimension
already records.

**A partially-satisfied check is not "missing".** Two of four quarters is
incomplete, not absent, and the ratio already says so. Listing it under
"Missing quarterly results" would be false.

**The scheme is published.** `GET /api/v1/quality/scheme` returns every
dimension, weight, check and grade band, so a score can be audited rather than
taken on trust.

## AI integration — verified live

```
POST /api/v1/company/POWERGRID/ai/analyse
{"capability": "business_summary"}

data_quality: score=29.8  grade=F  missing=33
warning: "Data quality for POWERGRID is 30/100 (grade F). This analysis is
          based on INCOMPLETE data. Missing: Missing cash flow statement,
          Missing latest annual report, …"

display_content: "> **Data quality warning.** Data quality for POWERGRID is
                  30/100 (grade F). This analysis is based on INCOMPLETE…"
```

Attached in `_result_out`, the single funnel every AI response passes through,
so no endpoint can omit it.

* **Present on every answer, not only poor ones.** A reader who sees the score
  only when it is bad learns to treat its absence as reassurance — exactly the
  inference the field exists to prevent.
* **Below 70 the warning is prepended to the visible answer**, not only
  returned structurally. A reader who stops halfway through still sees it, and
  a client that never looks at the field still shows it.
* **Scoring failure degrades to no context, never to a failed answer.** A
  research response is still worth returning if the scorer had a bad day.

## Automation — no manual updates

* **On new data:** the refresh runs at the end of every memory-enrichment
  pass, so a document arriving raises the score with no manual step.
* **On the passage of time:** a daily `QUALITY_REFRESH` sweep handles the
  other direction — a score *falls* as filings age past their horizons even
  though nothing new arrived, and nothing else would notice.

Both are guarded: a scoring failure must not undo a successful enrichment
pass, and the score is always recomputable on read.

## Endpoints

| Endpoint | Purpose |
|---|---|
| `GET /company/{ticker}/quality` | Score, grade, dimensions, missing items, freshness |
| `GET /quality/dashboard` | Average, leaderboards, threshold counts, grade mix |
| `GET /quality/scheme` | The published scoring scheme |
| `POST /quality/refresh` | Rescore the universe (operator only) |

Mounted before `market.router`: ROUTE-001 showed a greedy `{ticker}` route
captures static siblings registered after it and answers 200 with empty data.

## Tests

**2,438 → 2,476, zero failures.** 38 new, including that weights sum to 100,
every check has a human label, an empty company scores under 15, a stale
annual report scores below a current one, a partial check is not reported
missing, the sweep survives one failing company, and enrichment refreshes the
score.

## Caveats

- **The average of 30.41 will look alarming and is accurate.** It reflects
  document coverage of ~54 of 503 companies, not a scoring defect. It should
  rise as the crawler collects; that is the mechanism working as intended.
- **AAPL, NFLX and NVDA score 20.2** — the lowest in the universe. They are US
  companies on a different pipeline with no Indian filings, no crawl state and
  no IR discovery. The score is correct for what the platform holds about
  them, but it is measuring them against a scheme built for Indian issuers.
- **AI coverage is inferred from durable artefacts** (vault sections and
  summaries), not from a record of which capabilities have been generated. The
  AI layer generates on demand and does not persist a report per capability,
  so a company analysed ten times but never enriched scores zero there.
- **`catalysts` maps to the `opportunities` vault section** and `moat` to
  `business_model`/`competitors`. These are the closest durable evidence, not
  exact equivalents.
- **Scoring is ~0.7s per company**, so `/company/{ticker}/quality` recomputes
  live while the dashboard reads snapshots. A full sweep takes ~6 minutes.
- **`knowledge_freshness_days` is null** for companies with no vault entries —
  reported as null rather than 0, since "no knowledge" and "knowledge from
  today" must not look alike.
