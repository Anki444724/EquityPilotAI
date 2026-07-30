# Institutional Equity Research Platform — SaaS

A Bloomberg / Capital IQ style equity research product. The financial logic is
derived from `Institutional_Equity_Research_Platform_v7.xlsx` (54 sheets,
11,647 formulas), which serves as the **business specification** — the workbook
is not converted or embedded.

## Status

| Module | Scope | State |
|---|---|---|
| **1** | Auth · Landing · Dashboard · Search · Company Profile · Nav · Theme | ✅ **Complete** |
| **2** | Historical statements, ratios, WC, debt, capex, shareholding | ✅ **Complete** |
| **3** | Forecast engine — projections, scenarios, assumptions, Highcharts | ✅ **Complete** |
| **4** | Valuation — 10 methodologies, WACC, sensitivity, Monte Carlo | ✅ **Complete** |
| **5** | Institutional scoring — 13 categories, weight profiles, confidence | ✅ **Complete** |
| **6** | AI research analyst — grounding, citations, guardrails | ✅ **Complete** |
| **7** | Document intelligence — OCR, tables, entities, vector search, knowledge graph | ✅ **Complete** |
| **8** | Portfolio intelligence — positions, risk, attribution, alerts, AI commentary | ✅ **Complete** |
| **9** | Report generator — dynamic blocks, 5 formats, citation audit | ✅ **Complete** |
| **10** | Commercial SaaS layer — auth, RBAC, multi-tenancy, billing, observability, jobs | ✅ **Complete** |

**Version 1.0 Release Candidate 2.** Running on **real financials for 135
NSE-listed companies** (42,025 facts, FY2006–FY2026). 1,903 tests · 100% of
2,279 validation checks against reported figures · 0 known CVEs · p95 18 ms.
See [`docs/RC2_REPORT.md`](docs/RC2_REPORT.md).

```bash
cd backend && python -m app.data     # ingest, derive and validate real data
```

## Quick start

```bash
# Backend  → http://localhost:8000  (docs at /docs)
cd backend
pip install -r requirements.txt
python -m app.db.seed          # 20 companies, 10,800 canonical facts
uvicorn app.main:app --reload

# Frontend → http://localhost:3000
cd frontend
npm install
npm run dev
```

No Postgres, Redis, mail server or OAuth account is required for local
development: the app defaults to SQLite and a clearly-labelled development
identity, behind the same interfaces used in production. Verification,
password reset and magic link all work end to end — with no SMTP host the
platform logs the link.

### Production shape, locally

```bash
docker compose up --build     # postgres · redis · api · worker · web
```

### Documentation

| Document | Contents |
|---|---|
| [`docs/ARCHITECTURE_V1.md`](docs/ARCHITECTURE_V1.md) | System diagram, layer rules, folder structure |
| [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) | Local, Docker, Railway, backup and recovery |
| [`docs/SECURITY_CHECKLIST.md`](docs/SECURITY_CHECKLIST.md) | 100+ controls, each with its test |
| [`docs/PRODUCTION_READINESS.md`](docs/PRODUCTION_READINESS.md) | Audit results, defects found, sign-off |
| [`docs/MODULE_1.md`](docs/MODULE_1.md) … [`MODULE_10.md`](docs/MODULE_10.md) | Per-module design notes |

## Verification

```bash
cd backend && python -m pytest      # 861 passed
cd frontend && npx tsc --noEmit     # clean
cd frontend && npm run build        # succeeds
```

Engine outputs are asserted against figures evaluated from the workbook itself
(QA_Report_v7.md §9.1): revenue 14,528.6, EBITDA 961.9, PAT 125.5, EPS 1.046,
with the balance sheet tying in all ten years.

## Documentation

- `docs/ARCHITECTURE.md` — layer model, workbook→software mapping, decisions
- `docs/MODULE_1.md` — foundation: auth, search, company profile
- `docs/MODULE_2.md` — financial analysis: 9 endpoints, 50 ratios
- `docs/MODULE_3.md` — forecast engine: 30 drivers, scenarios
- `docs/MODULE_4.md` — valuation: 10 methodologies, data-quality gate
- `docs/MODULE_5.md` — scoring: 13 categories, 5 weight profiles
- `docs/MODULE_6.md` — AI analyst: 4 providers, 17 capabilities, 153 tests
- `docs/openapi.json` — generated API specification
- `docs/workbook_spec.json` — machine-readable extract of the specification
