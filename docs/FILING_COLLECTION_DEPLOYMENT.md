# Automated Indian Filing Collection — Production Verification

Deployed and verified 2026-08-01.

- Backend: `https://backend-production-18956.up.railway.app`
- Final commit: **`722a074`**, deployment `f8769a3f`
- Commits this session: `4060213` (system) → `06c2346` (MIG-001) → `722a074` (STORAGE-001)

## Verification checklist

| # | Check | Result |
|---|---|---|
| 1 | Application starts | **PASS** — `status: ok`, environment `production` |
| 2 | Health endpoint | **PASS** — `ready: true`; database, schema, configuration and queue all OK |
| 3 | Automatic document ingestion | **PASS** — see below |
| 4 | AI research reports | **PASS** — TCS and RELIANCE 15/15 grounded; AAPL 14/15 |
| 5 | RAG retrieval | **PASS** — retrieving real auto-collected NSE content |
| 6 | FMP and Finnhub healthy | **PASS** — Finnhub serving US quotes, FMP serving 193 statement facts |
| 7 | No regressions | **PASS** — perimeter 33/33, modules 18/19 (1 known warning), 2,263 tests |

The only non-OK readiness item is `optional_configuration`: no SMTP host. Known
and non-blocking by design.

### 3. Automatic ingestion, measured in production

A targeted crawl of TCS and INFY from Railway's IP:

```
TCS : discovered=25 new=25 ingested=3 duplicates=0 skipped=4 failed=0
INFY: discovered=25 new=25 ingested=3 duplicates=0 skipped=4 failed=0
      NSE Corporate Filings   found=25   (IR: no URL registered; BSE: No Record Found!)
```

The **nightly scheduler then fired on its own**, without being asked:

```
by_status: discovered=422  embedding=77  completed=43  duplicate=1  skipped=82
storage: 229 MB            failed: 0
```

That is the requirement — no manual upload, no manual trigger. 82 procedural
notices were filtered as noise, one document was caught by SHA256 dedup as the
same PDF under a different URL, and `completed` grew from 3 to 43 during the
session as the post-processing chain drained.

### 5. RAG over auto-collected documents

```
doc47 p1  0.627  "We are enclosing herewith a copy of the transcript of th…"
doc19 p30 0.593  "Sequential revenue growth was also impacted by higher of…"
```

Earnings-call transcripts and board-meeting outcomes that nobody uploaded.
Research reports draw 5 of 15 sections from `Annual Report (RAG)`.

## Two defects found in production and fixed

### MIG-001 — migration omitted the inherited audit columns

All three new endpoints returned **500** on first deploy:

```
UndefinedColumn: column discovered_filings.created_at does not exist
```

`Base` declares `created_at`/`updated_at` on every model, so they are part of
each table's contract. The hand-written migration omitted them.

**Why 2,253 tests missed it.** The suite builds its schema with
`Base.metadata.create_all()`, which derives it *from the models* and therefore
supplies the inherited columns automatically. Production builds its schema by
running the *migrations*. Two independent descriptions of the same thing, and
nothing compared them — so a migration could disagree with the models
indefinitely and every test would still pass. The service layer was correct
throughout; verified directly against production Postgres, which is what
localised this to the API/schema boundary.

Fixed in `c4e2a91b7d38` (fresh databases) plus `d5f83c1e6a27` (databases that
already ran the broken version, guarded by an existence check).
`tests/test_migrations.py` now runs every migration into an empty database and
diffs the result against the models. **Verified by reintroducing the defect:
three tests fail; with the fix, seven pass and none skip.**

### STORAGE-001 — the crawler would have filled the volume

The first real run consumed **229 MB of the 500 MB documents volume within
minutes**, with individual shareholder-meeting PDFs above 20 MB. Unchecked,
the disk fills and the failure lands on whatever writes next — quite possibly
a user's upload rather than the crawl that caused it.

Automated collection is the lowest-priority consumer of shared storage, so it
now yields: free disk is checked before each download, reserving the
platform's own floor plus one maximum-sized download so the check cannot pass
and then be invalidated by the file it authorised. A blocked filing stays
`discovered` rather than `failed`, so it is picked up once space frees.

## Known limitations

- **Storage is the binding constraint.** 229 MB of 500 MB is used after a
  partial run of the universe. The guard prevents an outage but does not
  create space: the volume needs enlarging, or a retention policy that prunes
  low-value announcements, before full 135-company coverage is sustainable.
- **BSE returns "No Record Found!" for every probe.** Only 15 scrip codes are
  mapped and none was exercised successfully. NSE is carrying the exchange
  tier alone.
- **No investor-relations URLs are registered**, so Priority 1 contributes
  nothing yet. The crawler is built and tested; it needs URLs via
  `PATCH /api/v1/filings/companies/{ticker}`. This is the largest remaining
  gap against the brief.
- **The document worker is slower than the crawler.** 422 filings sit at
  `discovered` and 77 at `embedding`; the backlog drains at roughly 7 documents
  per 40 seconds. Correct but not fast — a second worker or a higher
  concurrency setting would help.
- **Financial extraction from collected PDFs is not yet feeding the canonical
  store.** Documents are parsed, chunked and embedded, and facts are extracted
  where the existing extractors recognise them, but statement-level extraction
  from a scanned exchange filing into `FinancialFact` is not proven.
- **Notifications reached zero users** because no watchlist covers the
  affected companies. The path is verified end to end locally (70.00 → 73.51,
  BBB → A, one notification queued).
- **The repository is public.** Flagged across several sessions.

## Credentials

Per your instruction the GitHub PAT and Railway token are **retained** at
`~/.secrets/creds.env` (chmod 600, outside the repository) rather than being
shredded at end of turn. The PAT was used via a one-time credential URL and is
**not** persisted in `.git/config` — the remote is stored without it. Tell me
when the project is complete and I will shred them; you rotate/revoke after.
