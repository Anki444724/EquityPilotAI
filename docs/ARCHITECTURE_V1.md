# Architecture — Version 1.0

Institutional Equity Research Platform. Ten modules, one deployable system.

---

## 1. System diagram

```
                            ┌──────────────────────────────┐
                            │          BROWSER             │
                            │  Next.js 16 · React 19 · TS  │
                            │  Tailwind v4 · Highcharts    │
                            └───────────────┬──────────────┘
                                            │  HTTPS
                                            │  Bearer access token (15 min)
                                            │  httpOnly refresh cookie (30 d)
                                            ▼
   ┌─────────────────────────────────────────────────────────────────────────┐
   │                      MIDDLEWARE  (outermost first)                      │
   │                                                                         │
   │   security headers → request context → metrics/errors → rate limit      │
   │                                       → CORS                            │
   │                                                                         │
   │   Order is load-bearing. A request refused by the rate limiter is still │
   │   counted and still carries a request id; a response that never reaches │
   │   a route still gets its security headers.                              │
   └───────────────────────────────┬─────────────────────────────────────────┘
                                   ▼
   ┌─────────────────────────────────────────────────────────────────────────┐
   │                        API LAYER   FastAPI · 150 paths                  │
   │                                                                         │
   │   auth  companies  dashboard  analysis  forecast  valuation  scoring    │
   │   ai    documents  portfolio  reports   admin     platform              │
   │                                                                         │
   │   Every route resolves ONE principal, applies ONE tenant scope, and     │
   │   asks for permissions — never for role literals.                       │
   └───────────────────────────────┬─────────────────────────────────────────┘
                                   ▼
   ┌─────────────────────────────────────────────────────────────────────────┐
   │                            SERVICE LAYER                                │
   │                                                                         │
   │  financials  ratios  working_capital  debt  capex  shareholding         │
   │  forecast    valuation   scoring                                        │
   │  ai/         documents/  portfolio/   reports/                          │
   │  platform/ ── crypto · identity · tenancy · entitlements · api_keys     │
   │               audit · observability · rate_limit · email · backup       │
   │               jobs/{queue, handlers, worker}                            │
   │                                                                         │
   │  Each calculation is defined exactly once. Enforced by an AST test.     │
   └───────────────────────────────┬─────────────────────────────────────────┘
                                   ▼
   ┌─────────────────────────────────────────────────────────────────────────┐
   │                    DOMAIN LAYER   (pure · no infrastructure)            │
   │                                                                         │
   │  calc  financials/  forecast/  valuation/  scoring/                     │
   │  ai/types  documents/{types,fields}  portfolio/{…}  reports/{…}         │
   │  platform/{identity, plans, audit, jobs, limits}                        │
   │                                                                         │
   │  Imports no SQLAlchemy, no FastAPI, no settings, no clock.              │
   │  Every rule here is provable in a unit test with nothing attached.      │
   └───────────────────────────────┬─────────────────────────────────────────┘
                                   ▼
   ┌──────────────────────────┐    ┌──────────────────────────────────────┐
   │   PostgreSQL  54 tables  │    │   WORKER PROCESS                     │
   │   (SQLite in dev, WAL)   │◄───┤   python -m app.worker               │
   │                          │    │   claim → run → succeed/retry        │
   │   pool 20 + 20 overflow  │    │   scheduler: 5 recurring jobs        │
   └──────────────────────────┘    └──────────────────────────────────────┘
              ▲
              │
   ┌──────────┴───────────┐
   │  Redis  (optional)   │   shared rate-limit counters across replicas
   └──────────────────────┘
```

---

## 2. The ten modules

| # | Module | Domain | Services | Key tables |
|---|--------|--------|----------|-----------|
| 1 | Auth · Dashboard · Company | — | `company_service` | `companies`, `financial_facts` |
| 2 | Historical statements & ratios | `financials/` | `financials/`, `ratios/`, `working_capital/`, `debt/`, `capex/`, `shareholding/` | `debt_instruments`, `shareholding_snapshots` |
| 3 | Forecast engine | `forecast/` | `forecast/` | `forecasts`, `forecast_assumptions` |
| 4 | Valuation — 10 methodologies | `valuation/` | `valuation/` | — |
| 5 | Institutional scoring — 13 categories | `scoring/` | `scoring/` | `score_snapshots`, `scoring_weight_profiles` |
| 6 | AI research analyst | `ai/types` | `ai/` | `ai_prompts`, `ai_analyses`, `ai_usage` |
| 7 | Document intelligence | `documents/` | `documents/` | 8 `document_*` tables |
| 8 | Portfolio intelligence | `portfolio/` | `portfolio/` | 8 portfolio tables |
| 9 | Research report generator | `reports/` | `reports/` | `reports`, `report_artifacts`, `report_jobs` |
| 10 | **Commercial SaaS layer** | `platform/` | `platform/` | **20 platform tables** |

---

## 3. The five decisions that shape Module 10

### 3.1 Permissions are the primitive; roles are a bundle

Endpoints never ask *"is this an admin?"*. They ask *"may this caller write a
portfolio?"*. Roles are then free to change shape without touching a route,
and the matrix can be read as a table in one file. The reverse arrangement —
routes naming roles — is how authorisation logic ends up smeared across a
hundred handlers and quietly diverging.

Seniority monotonicity (Super Admin ⊇ Admin ⊇ Analyst ⊇ … ⊇ Guest) is
**asserted by a test over the declared data**, not produced by inheritance, so
a future role that is powerful in one dimension and weak in another remains
expressible.

### 3.2 `tenant_id` is the first column and the first predicate

A shared schema with a tenant column, not a database or schema per tenant.
The platform has to run on SQLite with no infrastructure *and* on one Postgres
instance on Railway, and it has to answer cross-tenant operator questions
without fanning out over N databases.

The cost is that isolation is a discipline rather than a wall. It is paid for
three ways: a single `TenantScope` that every service takes, a dependency that
constructs it once per request, and a test suite that attempts cross-tenant
reads and asserts 404.

**A cross-tenant reach raises rather than returning empty.** If it quietly
returned nothing, the caller would render a blank page and nobody would learn
that a boundary was probed. Raising means the API returns 404 (never revealing
existence) *and* writes a CRITICAL audit event. The user sees the same thing
either way; the operator does not.

### 3.3 One entitlement decision for the whole platform

`domain/platform/plans.evaluate()` answers "may this tenant do this now?" and
returns *why not*. Every enforcement point — API dependency, background
worker, admin preview — calls it. One place to express commercial policy, one
behaviour to test.

Checks run cheapest-and-most-fundamental first, so the message names the real
obstacle: a suspended tenant is told it is suspended, not that it is out of
report credits.

### 3.4 Quota is checked before the work and consumed after it

`check()` asks; `consume()` records. A generation that raises halfway must not
bill the customer for a report they never received. The raw `usage_events`
settle disputes; the `usage_counters` roll-up is what the gate reads, so a
quota decision is one indexed read.

### 3.5 Nothing secret is stored recoverably

| Secret | At rest | Recoverable? |
|--------|---------|--------------|
| Password | Argon2id, 64 MiB / t=3 / p=4 | No |
| Refresh token | SHA-256 digest | No |
| API key | SHA-256 digest of the whole string | No — shown once |
| One-time token | SHA-256 digest | No |
| Tenant secret (BYO AI key, SMTP) | AES-256-GCM, versioned key | Yes, by the app only |
| MFA secret | AES-256-GCM | Yes, by the app only |

Argon2 for passwords because it is memory-hard. SHA-256 for tokens because
they are 256 bits of `secrets.token_urlsafe` entropy — there is nothing to
brute-force, and Argon2 on every request would add 50 ms and buy nothing.

---

## 4. Request lifecycle

```
1.  security headers        CSP, HSTS (prod), nosniff, DENY, Permissions-Policy
2.  request context         X-Request-ID minted or honoured, bound to structlog
3.  observability           timer starts
4.  rate limit              per credential when present, per IP otherwise
5.  CORS                    exact origins only
6.  route match             literal paths always precede /{id}
7.  get_current_user        API key → JWT → Clerk → dev identity.  SYNC, so it
                            runs on the threadpool and cannot block the loop.
8.  require(Permission…)    denial → 403 + audit event
9.  require_tenant          400 if the principal has no organisation
10. require_feature/quota   denial → 402 with the upgrade target
11. handler                 service → domain → database
12. consume(quota)          only on success
13. audit                   redacted before it reaches the row
14. response                X-Response-Time-ms, X-Request-ID, rate headers
15. deferred write          metrics/errors flushed AFTER the session is
                            released — never on the request path
```

Step 15 is not fastidiousness. The first version flushed inline, opening a
second connection while the request held its first; at concurrency 25 the pool
was exhausted and the process stopped answering entirely. See
`docs/PRODUCTION_READINESS.md` §4.

---

## 5. Authentication

Four methods, one `Principal`:

```
Email + password  ─┐
Google OAuth      ─┤
GitHub OAuth      ─┼──► IdentityService._establish_session ──► Principal
Magic link        ─┘         │
                             ├─ access JWT   15 min, in memory only
API key           ──────────►└─ refresh      30 d, httpOnly cookie, rotating
```

**Rotation with reuse detection.** Each refresh mints a successor and marks
the presented token used. Presenting a spent token means it was captured, so
the *entire family* is revoked and a CRITICAL audit event is written. This is
the only available signal that a refresh token has been stolen.

**The access token is never persisted client-side.** It lives in a module
variable, not `localStorage`, so an XSS bug cannot read it. A page reload
recovers the session through the refresh cookie, which script cannot read at
all.

**Enumeration resistance.** Registration, password reset and magic link return
an identical body whether or not the address exists. Unknown email and wrong
password produce the same message and comparable timing — a dummy Argon2
verification runs on the "no such user" path specifically to keep them close.

---

## 6. Background work

```
   enqueue ──► [queued] ──claim──► [running] ──► [succeeded]
                  ▲                    │
                  │                    ├──► [failed] ──backoff──┐
                  └────requeue_ready───┘                        │
                                       └──► [dead_letter] ◄─────┘
                                                  │
                                              manual replay
```

* **Claim is a conditional UPDATE** — `WHERE id = ? AND status = 'queued'` —
  so two workers racing produce one winner and one retry, on SQLite and
  Postgres alike, with no `SELECT … FOR UPDATE`.
* **Leases, not locks.** A worker that dies mid-job leaves an expired lease,
  and the job is reclaimed. That is the same guarantee as a container being
  evicted, so it is the one worth relying on.
* **Dead letter, not deletion.** A queue that quietly loses work is worse than
  one that visibly stalls.
* **Deterministic jitter**, seeded on the job id, so a thousand simultaneous
  failures spread out while the schedule stays testable.

Nine job kinds; five recurring schedules (portfolio refresh, alert evaluation,
usage roll-up, backup, retention sweep).

---

## 7. Observability with no external service

| Concern | Mechanism | Why not the obvious thing |
|---------|-----------|---------------------------|
| Logging | structlog, JSON in prod | Same redaction as the audit trail — logs are the likelier leak |
| Metrics | 1-minute buckets + fixed-boundary histogram | Exact percentiles need every sample; a bucket exists to avoid that |
| Errors | Grouped by fingerprint, counted | One row per distinct error — an error loop cannot flood the table |
| Health | `/health/live` vs `/health/ready` | A liveness probe that queries the database restarts a healthy app when the database hiccups |
| Audit | Append-only, redacted at construction | No code path into `audit_logs` skips redaction |

Route labels are normalised (`/companies/42` → `/companies/{id}`) before they
become a metric dimension — otherwise every company id is its own label and
the table grows without bound.

---

## 8. Layer rules, enforced by test

`tests/test_platform_architecture.py`, 35 tests:

| Rule | Test |
|------|------|
| Domain imports no infrastructure | `test_no_platform_domain_module_imports_infrastructure` |
| Domain reads no settings | `test_the_domain_does_not_reach_for_settings` |
| Domain raises domain errors, not HTTP ones | `test_the_domain_raises_domain_errors_not_http_ones` |
| Services never import the API layer | `test_services_do_not_import_the_api_layer` |
| Only `crypto.py` hashes or signs | `test_only_crypto_hashes_passwords`, `…signs_or_decodes_jwts` |
| Each calculation defined once | `test_the_entitlement_decision_exists_once` and siblings |
| No response schema carries a secret | `test_no_response_schema_declares_a_secret_field` |
| Only `IssuedApiKeyOut` carries a plaintext | `test_the_only_model_carrying_a_plaintext_is_the_issued_key` |
| Every tenant-owned model has `tenant_id` | `test_every_tenant_owned_model_carries_a_tenant_id` |
| No request schema accepts `tenant_id` | `test_admin_routes_derive_the_tenant_from_the_principal` |
| Every `/platform/*` route is operator-guarded | `test_cross_tenant_endpoints_are_all_operator_guarded` |
| No route compares role literals | `test_routes_ask_for_permissions_not_roles` |
| Auth dependencies are synchronous | `test_auth_dependencies_are_synchronous` |
| Middleware opens no second session | `test_the_request_path_opens_no_second_session` |
| SQLite runs in WAL | `test_sqlite_actually_reports_wal` |

---

## 9. Folder structure

```
ierp/
├── backend/
│   ├── app/
│   │   ├── core/            config.py  security.py
│   │   ├── db/              base.py  seed.py  seed_portfolio.py  seed_platform.py
│   │   ├── domain/          ← pure. no SQLAlchemy, no FastAPI, no settings
│   │   │   ├── calc.py  financials/  forecast/  valuation/  scoring/
│   │   │   ├── ai/  documents/  portfolio/  reports/
│   │   │   └── platform/    identity.py  plans.py  audit.py  jobs.py  limits.py
│   │   ├── models/          10 modules of SQLAlchemy tables (54 total)
│   │   ├── schemas/         Pydantic contracts, one file per module
│   │   ├── services/
│   │   │   ├── …module 1-9 services…
│   │   │   └── platform/    crypto  identity_service  tenancy  entitlements
│   │   │                    api_keys  audit_service  observability  rate_limit
│   │   │                    email  backup  jobs/{queue,handlers,worker}
│   │   ├── api/v1/          13 routers
│   │   ├── main.py          app factory, middleware, health, metrics
│   │   └── worker.py        python -m app.worker
│   ├── tests/               26 files · 1,850 tests · load/
│   ├── Dockerfile           multi-stage, non-root, healthcheck
│   ├── requirements.txt
│   ├── railway.toml
│   └── .env.example         every setting documented
├── frontend/
│   ├── src/app/             15 routes incl. /admin and /platform
│   ├── src/components/      ui/ layout/ admin/ + 8 module folders
│   ├── src/lib/             api.ts  types.ts  format.ts  utils.ts
│   ├── Dockerfile           standalone output
│   └── next.config.ts
├── docs/                    ARCHITECTURE*.md  MODULE_1…10.md  DEPLOYMENT.md
│                            SECURITY_CHECKLIST.md  PRODUCTION_READINESS.md
│                            openapi.json  screenshots/
├── docker-compose.yml       postgres · redis · api · worker · web
├── railway.json
└── .github/workflows/ci.yml 5 jobs
```
