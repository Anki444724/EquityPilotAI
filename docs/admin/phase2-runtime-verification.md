# Phase 2 — Company Management: Runtime Verification Report

**Date:** 2026-08-06
**Module:** Company Management (heart of the Enterprise Admin Panel)
**Status:** ✅ All tests pass · ✅ Runtime flow verified

---

## 1. What was built

| Requirement | Status | Location |
|---|---|---|
| Add / Edit Company | ✅ | `POST/PATCH /api/v1/admin/companies` |
| Soft Delete / Restore / Permanent Delete | ✅ | `DELETE /{id}`, `POST /{id}/restore`, `DELETE /{id}/permanent` |
| CSV Import / Export | ✅ | `POST /import/csv`, `GET /export/csv` |
| Excel Import / Export | ✅ | `POST /import/xlsx`, `GET /export/xlsx` (openpyxl) |
| Bulk Editor (spreadsheet) | ✅ | `POST /bulk-edit` + `BulkEditor` UI |
| Search (name/symbol/ISIN/sector/industry) | ✅ | `search` query param (indexed `ILIKE` on 5 columns) |
| Filters (sector/industry/mcap/exchange/status) | ✅ | `sector`, `industry`, `market_cap_min/max`, `exchange`, `listing_status` |
| Merge Duplicates | ✅ | `POST /merge` (reassigns facts + versions) |
| Logo Upload | ✅ | `POST /{id}/logo` (logo_url / favicon_url) |
| Validation (dup symbol/ISIN, required) | ✅ | `CompanyAdminService._check_unique` |
| Version History / Rollback / Audit | ✅ | `company_versions` table + `GET /versions`, `POST /rollback` |
| Performance (10,000+) | ✅ | Indexed list/filter/search with server-side pagination |
| UI (dark, pagination, sorting, sticky headers) | ✅ | `companies-view.tsx` |

## 2. New schema (Alembic `3b4c7d9e0f1a`)
- `companies` gains: `face_value`, `listing_date`, `ceo`, `employees`, `headquarters`, `logo_url`, `favicon_url`, `deleted_at` (soft delete).
- New table `company_versions` (immutable snapshots + `changes` diff + actor + type) for rollback and audit.

## 3. Test results
- **`tests/test_admin_companies.py`** — 14 tests, all pass:
  - CRUD round-trip, permanent delete, required fields, duplicate symbol (case-insensitive), duplicate ISIN, update-duplicate rejection.
  - Versioning + rollback to an earlier snapshot.
  - Bulk edit across multiple companies.
  - CSV import + export round-trip; Excel (.xlsx) import.
  - Merge duplicates (reassigns + removes).
  - Search, filters, pagination, sorting.
  - RBAC guard on the list route.
- **`tests/test_migrations.py`** — passes (migration chain is linear and complete).
- **Full backend suite — passes (exit 0).**
- **Frontend:** `tsc --noEmit` clean, `eslint` 0 errors, `next build` succeeds.

## 4. Runtime verification (live API, seeded DB)

| Operation | Result |
|---|---|
| `POST /admin/companies` (create RT1) | ✅ 201, `ticker=RT1` |
| `PATCH /admin/companies/{id}` (ceo→Bob, market_cap→5000) | ✅ 200 |
| `GET /admin/companies/{id}/versions` | ✅ 2 versions (`create`, `update`) |
| `GET /admin/companies/export/csv` | ✅ streams CSV with header + rows |
| `DELETE /admin/companies/{id}` (soft) | ✅ sets `deleted_at` |
| `POST /admin/companies/{id}/restore` | ✅ clears `deleted_at` |

CSV export sample (verified live):
```
name,ticker,exchange,isin,bse_code,sector,industry,market_cap,... 
Asian Paints Ltd,ASIANPAINT,NSE,INE843673401,,Chemicals & Specialty,Paints,218880.0,...
```

## 5. Screenshot limitation
The sandbox blocks outbound access to the Chromium CDN, so a real browser
screenshot of the running panel could not be captured (the same constraint as
Phase 1). The Companies management UI is live at `/admin → Companies`, and a
faithful static preview is at `docs/admin/phase2-preview.html`.

## 6. Awaiting approval
Per the roadmap, Phase 2 stops here. **Do not proceed to Phase 3 (Financial
Statements) until Phase 2 is approved.**
