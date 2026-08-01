# Nifty 500 Import Report

**Import only.** No document ingestion, no AI scoring, no R2 writes were
performed, as instructed. Awaiting your approval before the next phase.

- Commit `0715684`, migration `f2b71c4e9a08`
- Run against production: 2026-08-01, 39.2 s

## 1. Import summary

| Metric | Value |
|---|---|
| Constituents in the index | **500** |
| Imported | **500** |
| Created (new companies) | **368** |
| Updated (existing, enriched) | **132** |
| Failed | **0** |
| Duration | 39.2 s |

The universe grew from 139 to **507 company records**: 500 Nifty 500
constituents, 3 US listings from Phase 3, and 4 superseded Indian listings
retained for their history (see §5).

## 2. Field coverage

| Field | Coverage | Source |
|---|---|---|
| NSE Symbol | 500/500 (100%) | `ind_nifty500list.csv` |
| Company Name | 500/500 (100%) | `ind_nifty500list.csv` |
| ISIN | 500/500 (100%) | `ind_nifty500list.csv` |
| Sector | 500/500 (100%) | `ind_nifty500list.csv` |
| Market Cap Category | 500/500 (100%) | NSE constituent indices |
| **BSE Code** | **498/500 (99.6%)** | BSE scrip master, joined on ISIN |
| Listing Status | 500/500 (100%) | Set `active` on import |

### Market-cap categories

| Category | Count | Source index |
|---|---|---|
| largecap | 100 | Nifty 100 |
| midcap | 150 | Nifty Midcap 150 |
| smallcap | 250 | Nifty Smallcap 250 |

These partition the Nifty 500 exactly (100 + 150 + 250 = 500), so the
classification is **NSE's own**, not a market-cap threshold I invented. It
stays correct when NSE rebalances.

### Sector distribution (top 8 of 20)

| Sector | Count |
|---|---|
| Financial Services | 101 |
| Capital Goods | 63 |
| Healthcare | 48 |
| Automobile and Auto Components | 38 |
| Consumer Services | 29 |
| Fast Moving Consumer Goods | 28 |
| Information Technology | 27 |
| Chemicals | 26 |

## 3. Data sources

Every field comes from an authoritative source rather than a heuristic:

- **NSE `ind_nifty500list.csv`** — the file the exchange publishes to define
  the index. Symbol, name, ISIN and sector.
- **NSE Nifty 100 / Midcap 150 / Smallcap 250 lists** — market-cap category.
- **BSE active-scrip master** (`api.bseindia.com`, 4,928 active equities) —
  BSE code, **joined on ISIN**. Names differ across exchanges ("ABB India Ltd"
  vs "ABB India Limited") and symbols occasionally collide; ISIN is the
  security's legal identifier and the only safe join.

### Sector vs industry

NSE supplies **one** taxonomy column, labelled "Industry", holding twenty
macro groupings. Those are sectors in this platform's vocabulary, so they
populate `sector`. **`industry` is deliberately left null.** Copying the same
value into both columns would make them agree by construction and tell a
reader nothing; it will be populated when a provider supplies a genuinely
finer classification.

## 4. Verification

Independent re-fetch of the NSE file and comparison against the database:

```
spot-check of 10 random constituents: 0/10 mismatches
  FLUOROCHEM VOLTAS BIKAJI HEROMOTOCO NCC
  AFCONS APARINDS SJVN KARURVYSYA ATGL
  — ISIN, sector, category and BSE code all correct
```

| Integrity check | Result |
|---|---|
| Duplicate ISINs | **none** |
| Duplicate tickers | **none** |
| Tagged `index_membership=NIFTY500` | 500 |
| Test suite | **2,349 passing, 0 failures** |

## 5. Companies not in the Nifty 500

Seven records sit outside the index. All are explained:

| Ticker | Reason | Action |
|---|---|---|
| AAPL, NFLX, NVDA | US listings from Phase 3 | Retained, `active` |
| TATAMOTORS | **Demerged** into TMCV and TMPV, both now in the index | Marked `delisted` |
| ZOMATO | **Renamed** to ETERNAL, which is in the index | Marked `delisted` |
| LTIM | No longer a constituent | Marked `delisted` |
| BHARATCP | No longer a constituent | Marked `delisted` |

These four carry real financial history (336, 244, 326 and 540 canonical facts
respectively), so **they were not deleted**. Marking them `delisted` preserves
the history while excluding them from the active universe, so daily collection
will not pick them up.

Note *why* they duplicated rather than updating in place: the legacy rows have
**no ISIN**, so the ISIN-first match could not find them and the new listings
were created alongside. That is the correct outcome — matching "Tata Motors
Ltd" to TMCV on name would have been a guess — but it is worth knowing that
21 of the original 139 rows had an ISIN and the rest matched on symbol.

## 6. Missing data

| Gap | Count | Detail |
|---|---|---|
| Missing BSE code | **2** | `BSE` (BSE Ltd) and `CDSL` — genuinely NSE-only listings, not a lookup failure |
| Missing ISIN | 0 | — |
| Missing sector | 0 | — |
| Missing category | 0 | — |
| Failed imports | 0 | — |

## 7. Scope confirmation

Verified after the run:

```
documents for newly-imported companies : 0
filings discovered in the last 2 hours  : 0
total documents                         : 131 (unchanged)
```

No ingestion, no scoring, no R2 activity. A test
(`test_the_importer_does_not_ingest_or_score`) asserts the module references
no ingestion, scoring or replication service, so the constraint cannot erode
silently in a later change.

## 8. Caveats

- **The 368 new companies have no financial data.** They are identity records
  only — no statements, no documents, no scores. Any API call that needs
  financials will report them as unavailable until the next phase runs.
- **`industry` is null for all 500.** By design, as explained in §3.
- **Category is a point-in-time snapshot.** NSE rebalances the constituent
  indices periodically; re-running the importer refreshes it, but nothing
  currently does so on a schedule.
- **The BSE join depends on an undocumented endpoint.**
  `api.bseindia.com/.../ListofScripData` is not a published API and could
  change without notice. A failure there degrades to a null BSE code rather
  than failing the import.
- **One pre-existing local test fixture needed updating** — `ierp.db` predated
  the new columns and six forecast tests failed against it until the columns
  were added. Production was never affected; the schema arrived there via the
  migration.
