# Live Deployment — Railway

**Status:** live and verified
**Frontend (start here):** <https://frontend-production-1a313.up.railway.app>
**Backend API:** <https://backend-production-18956.up.railway.app>
**Repository:** <https://github.com/Anki444724/EquityPilotAI>
**Deployed:** 31 July 2026
**Platform version:** 1.0.0

---

## 1. What is deployed

| Service | Type | Image / source | Status |
|---|---|---|---|
| `backend` | FastAPI (Docker, `/backend`) | GitHub `Anki444724/EquityPilotAI` @ `main` | ✅ live |
| `postgres` | PostgreSQL 16.14 | `pgvector/pgvector:pg16` | ✅ live, 10 GB volume |
| `redis` | Redis 7 | `redis:7-alpine` | ✅ live |
| `frontend` | Next.js 16 (Docker, `/frontend`) | GitHub `Anki444724/EquityPilotAI` @ `main` | ✅ live |

All four services sit in the Railway project **`lucid-enthusiasm`** (personal
workspace).

The free plan permits five services per workspace. Two were consumed by
`QuantBacktestPro`. With the user's explicit authorisation, the **duplicate**
copy in `lucid-enthusiasm` was deleted to free a slot — verified beforehand as
safe on six criteria: it had never held a public domain in any of its five
deployments, had no volume, no TCP proxy and no custom variables, and is
rebuildable from `Anki444724/QuantBacktestPro`. Its configuration was saved to
`quantbacktestpro_duplicate_backup.json` first.

**The user's active deployment at `quantbacktestpro-production.up.railway.app`
(project `proud-joy`) was not touched, and was re-confirmed returning HTTP 200
immediately after the deletion.** Only a *service* was removed; no project was
deleted.

---

## 2. Verified endpoints

```
https://backend-production-18956.up.railway.app/health          liveness
https://backend-production-18956.up.railway.app/health/ready    readiness
https://backend-production-18956.up.railway.app/docs            Swagger UI
https://backend-production-18956.up.railway.app/openapi.json    151 paths
https://backend-production-18956.up.railway.app/metrics         request metrics
```

TLS is a Let's Encrypt certificate valid to **27 October 2026**, served over
HTTP/2, with plain HTTP answering `301` to the HTTPS origin.

---

## 3. Environment variables

Every value is set on the Railway service, never in the repository. `.env` is
git-ignored and confirmed absent from the published tree and from all commit
history.

### Required

| Variable | Value | Why |
|---|---|---|
| `ENVIRONMENT` | `production` | Enables strict readiness and forces secure cookies |
| `DEBUG` | `false` | No stack traces to clients |
| `DATABASE_URL` | `postgresql+psycopg://ierp:***@${{postgres.RAILWAY_PRIVATE_DOMAIN}}:5432/ierp` | Private-network DSN |
| `REDIS_URL` | `redis://:***@${{redis.RAILWAY_PRIVATE_DOMAIN}}:6379/0` | Shared rate-limit state |
| `SECRET_KEY` | 48-byte urlsafe random | Signs every JWT |
| `ENCRYPTION_KEY` | 48-byte urlsafe random | AES-256-GCM for tenant secrets |
| `NATIVE_AUTH` | `true` | First-party identity |
| `AUTH_DEV_MODE` | `false` | Disables the "every caller is super admin" dev identity |
| `PORT` | `8000` | Must match the service domain's target port |
| `CORS_ORIGINS` | `["https://backend-production-18956.up.railway.app"]` | HTTPS only — a plaintext origin fails readiness |

### AI providers

| Variable | Status |
|---|---|
| `GEMINI_API_KEY` | set — key valid, **free-tier quota exhausted** |
| `AI_PREFERRED_PROVIDER` | `Gemini` |
| `OPENROUTER_API_KEY` | **not set** — fallback degrades straight to Offline |

### Operational

`COOKIE_SECURE=true`, `COOKIE_SAMESITE=none`, `CSRF_ENABLED=true`,
`RATE_LIMIT_ENABLED=true`, `RATE_LIMIT_BACKEND=redis`, `LOG_FORMAT=json`,
`LOG_LEVEL=INFO`, `METRICS_ENABLED=true`, `WORKER_ENABLED=true`,
`SCHEDULER_ENABLED=true`, `ALLOW_SELF_SIGNUP=true`,
`DEFAULT_TENANT_SLUG=demo-capital`, `OAUTH_REDIRECT_BASE`, `EMAIL_LINK_BASE`.

Not set, and reported honestly as degraded: `SMTP_HOST` and the OAuth client
credentials.

---

## 4. Database

Alembic migration `33573f2f567a` was run against the live PostgreSQL instance
and verified to create **all 54 tables exactly** — no missing, no extra —
before any traffic was served.

Production data was then loaded with the real-filings pipeline:

| Metric | Value |
|---|---|
| Companies | 135 of 136 (`SIEMENS-ENERGY` delisted from the source) |
| Canonical facts | 42,025 |
| Fiscal years | FY2006 – FY2026 |
| Line-item coverage | 49.5 % of 54 canonical items |
| Validation | **2,279 / 2,279 checks passed (100 %)** |

Plus platform seed data: 4 plans, 3 tenants, 9 users, 19,677 price rows.

---

## 5. Deployment defects found and fixed

| ID | Defect | Root cause | Fix |
|---|---|---|---|
| **DEP-001** | Redis deploy failed with no logs | `bitnami/redis:7.2` returns 404 — Bitnami withdrew its free Docker Hub catalogue | Switched to `redis:7-alpine`; password moved from `REDIS_PASSWORD` to `--requirepass` |
| **DEP-002** | `Invalid value for '--port': '$PORT' is not a valid integer` | Railway runs `startCommand` **without a shell**, so `$PORT` was passed literally | Wrapped in `sh -c '…'` with `${PORT:-8000}` |
| **DEP-003** | `/health/ready` returned 503 on a correctly-configured service | A missing SMTP relay was graded a **critical** readiness failure, so the deploy was rolled back over an undeliverable e-mail | Split readiness into blocking vs degraded (below) |
| **DEP-004** | Edge returned `502 Application failed to respond` while the container was healthy | Service domain targeted port 8000; uvicorn bound Railway's injected `PORT=8080` | Pinned `PORT=8000` to match the domain |
| **DEP-005** | Frontend logged `✓ Ready` then `1/1 replicas never became healthy` | Next's standalone server reads `process.env.HOSTNAME \|\| '0.0.0.0'`, and Docker sets `HOSTNAME` to the container ID — so it bound to that name instead of all interfaces | `ENV HOSTNAME=0.0.0.0` in the runtime stage |
| **DEP-006** | Frontend would have shipped calling `localhost:8000` | `NEXT_PUBLIC_*` is inlined at *build* time; with no `/frontend/railway.toml`, Railway used Railpack and never passed the Docker ARG | Added `frontend/railway.toml` with `[build.args]` |
| **FE-001** | "Cannot reach the API. Start the backend on port 8000" and "No companies match 'TCS'" | The frontend **never authenticated**. It was built against `AUTH_DEV_MODE=true`, where every caller is a super admin, so it had no sign-in at all | `request()` now sends credentials; added `AuthProvider`, a sign-in screen, and a session gate |
| **FE-002** | Dashboard stayed empty after signing in, until a manual reload | Queries that ran while anonymous cached their 401s; React Query never retried them | Invalidate all queries on sign-in and session restore; clear the cache on sign-out |
| **FE-003** | Financials, Valuation and AI Research greyed out as "Ships in Module 2/4/6" | The pages existed and worked, but the sidebar pointed at top-level `/financials`, `/valuation`, `/ai` — routes that were never built — and carried a stale `module:` flag from before those modules were written. The company detail page had no outbound links either | Added `CompanyTabs`, rendered by all seven company pages; repointed the sidebar at the company in view |

### FE-003 in detail — "the modules are disabled"

Nothing was missing. All six research pages were implemented, built and
deployed, and every one rendered correctly when its URL was typed directly:

| Page | Lines | Live content |
|---|---|---|
| `financials` | 300 | 12 fiscal years, 8 sub-tabs incl. Ratios |
| `forecast` | 445 | 8.9 % revenue CAGR over 5 years |
| `valuation` | 285 | DCF/Relative/WACC/Sensitivity/Monte Carlo |
| `scoring` | 313 | 13 categories, grade |
| `ai` | 355 | Investment thesis |
| `documents` | — | Document intelligence |

They were simply unreachable. The routes live under `/companies/[id]/…`,
while the sidebar advertised top-level paths that no route served, disabled
by a `module: 2/4/6` marker left over from before those modules existed. The
company detail page linked only back to the company list.

The result was a platform that looked like a company browser: **the analysis
was all there, and none of it was clickable.**

### FE-001 in detail — the reported failure

The environment variables were **not** baked incorrectly. The deployed bundle
contained the production API host and zero `localhost:8000` references, and
CORS was correct. The real fault was that the client had no way to
authenticate:

1. `request()` — used by the dashboard, the company list and search — sent no
   credentials at all. Only `authed()` attached the `Authorization` header.
2. `authApi.login` and `setSession` existed but were **never called**: a grep
   across `src/app` and `src/components` returned nothing. There was no
   sign-in UI, so the in-memory access token stayed `null` forever.
3. Every call therefore returned `401`, and the dashboard rendered any error
   as "Start the backend on port 8000" — a connectivity message for an
   authentication failure. Search hit the same 401 and rendered its empty
   state, "No companies match 'TCS'".

This was invisible in development because `AUTH_DEV_MODE=true` treats every
caller as a super admin. Production correctly enforces authentication, which
exposed the gap.

**An honest note on my earlier verification.** I previously observed these
401s and judged them "correct for an anonymous visitor". That was wrong: I had
proved the *backend* worked by calling it with `curl` and a bearer token,
never that the *product* worked. A user who cannot sign in has no way to stop
being anonymous. The authenticated harness now drives the real UI.

### DEP-005 in detail

The container logged every sign of success — `▲ Next.js 16.2.12`, `✓ Ready in
0ms` — and still failed eleven consecutive health checks. The tell was the
bind address: `http://6a5c62968e57:3000`, the container ID rather than
`0.0.0.0`.

Verified locally rather than assumed, by running the built standalone server
twice with different `HOSTNAME` values:

| `HOSTNAME` | Reachable on `127.0.0.1:3111` |
|---|---|
| `6a5c62968e57` (container-ID style) | **unreachable** |
| `0.0.0.0` | **HTTP 200** |

### DEP-003 in detail

`production_readiness_problems()` returned one flat list, and every entry made
the critical `configuration` check fail. That conflated two very different
things: configuration that makes the service **unsafe** (unsigned tokens,
`DEBUG` on, the development identity active, SQLite, a plaintext CORS origin)
and configuration that merely leaves a **feature** unavailable (no mail relay,
no LLM key).

The result was that a service with a healthy database, a complete schema and
correct security settings refused all traffic because it could not send a
password-reset e-mail. Railway marked the deploy failed and rolled it back.

It is now split:

- `production_blocking_problems()` → the critical `configuration` check → 503
- `production_degraded_problems()` → the non-critical `optional_configuration`
  check → reported, `status: "degraded"`, still serving
- `production_readiness_problems()` retained as the union, so the admin panel
  and the readiness report still show one complete picture

Two regression tests lock this in
(`test_missing_smtp_does_not_make_the_service_unready`,
`test_unsafe_configuration_still_blocks`).

---

## 6. Verification results

### Perimeter — `deploy/verify_deployment.py` → **33/33**

TLS valid, HSTS, CSP, `X-Frame-Options: DENY`, `X-Content-Type-Options:
nosniff`, 151 OpenAPI paths, every guarded route rejecting anonymous callers,
no API key published in any endpoint.

### Modules — `deploy/verify_live_modules.py` → **19/19**

An authenticated harness, because a `401` proves the guard and not the module.

| Module | Evidence from the live deployment |
|---|---|
| 1 · Companies | 136 companies; `RELIANCE` profile resolves |
| 2 · Financials | 12 years; FY26 revenue ₹1,055,780 cr; 6 ratio sections |
| 3 · Forecast | 5 forecast periods |
| 4 · Valuation | 6 methodologies; WACC computed; caveat honoured |
| 5 · Scoring | 13 categories; overall 59.44; grade BBB |
| 6 · AI | Chain `Gemini → Offline`; quota correctly detected; cited output |
| 7 · Documents | Capabilities served |
| 8 · Portfolio | Transaction → FIFO holding replayed from ledger, ₹1,200.50 average cost, live P&L, score and intrinsic value joined in |
| 9 · Reports | 17 sections, 1,982 words, 7 charts, 11 tables, **100 % citation coverage (18/18)**; **51-page PDF** and valid `.docx` downloaded |
| 10 · Tenancy | Tenant admin reachable; operator console correctly refused |

### Frontend, verified in a real browser (post-fix)

| Check | Result |
|---|---|
| Anonymous `/dashboard` | Sign-in screen shown; no "port 8000" message |
| Sign in through the form | `POST /auth/login` → **200**, `/auth/me` → **200** |
| Dashboard **without** reload | **136 companies · 136 with financials · 28 sectors · 42,565 data points** |
| Search "TCS" | **1 matching company — Tata Consultancy Services Ltd**, ₹2,432.00, ₹8.80 L Cr |
| Drill into TCS | Company page renders real financials |
| Session across reload | Survives via the httpOnly refresh cookie |
| All 8 authenticated pages | Render, no stale errors, no 5xx |
| Research tabs (post FE-003) | Financials, Forecast, Valuation, Scoring, AI Research, Documents all reachable by clicking, all rendering real output, no 5xx |
| **Full research workflow** | **136/136 companies** return 200 from all five engines (financials, ratios, forecast, valuation, scoring) |

### Frontend, verified in a real browser

Headless Chromium against the live site:

- All 9 routes return HTTP 200 (`/`, `/dashboard`, `/companies`, `/portfolio`,
  `/reports`, `/documents`, `/watchlist`, `/admin`, `/platform`)
- The shipped client chunk contains the production API host and **zero**
  `localhost:8000` references
- Anonymous page load produces `401`s from the API and **no CORS errors** —
  the cross-origin wiring is correct
- Logging in from browser JavaScript returned `200` with a token, and an
  authenticated `fetch` returned **136 companies**: RELIANCE, BHARTIARTL,
  HDFCBANK, ICICIBANK, SBIN
- CORS allows the frontend origin with credentials and **refuses**
  `https://evil.example.com`
- All four security headers present on the frontend origin

### Three bugs in the verification tooling, not the product

Reported rather than quietly corrected:

1. The module harness read `items` where the API returns `results`, and used
   `/api/v1/analysis/...` where the routes are `/api/v1/company/{ticker}/...`.
   It reported ten failures against a working platform.
2. It expected valuation `methodologies` as a list; the API returns one key
   per methodology.
3. `verify_deployment.py` checked "plain http does not serve content" *after*
   urllib had already followed the 301, so it saw the final 200 and no
   `Location`. Fixed with a non-following redirect handler.

A `401` from a guarded route is a pass in the perimeter script — it proves the
guard, not the module. That is exactly why the authenticated harness exists.

---

## 7. Known limitations

2. **Gemini's free-tier quota is exhausted.** The key is valid — a direct
   `generateContent` call returns `429 RESOURCE_EXHAUSTED`. The analyst serves
   deterministic, fully-cited offline output and `/api/v1/ai/health` reports
   `degraded: true` rather than pretending otherwise.
3. **No OpenRouter key**, so the fallback chain has nowhere to go but Offline.
4. **No SMTP**, so verification and password-reset e-mails cannot be sent.
5. **OAuth** is implemented but has no provider credentials.
6. Data comes from aggregators, not filings directly; 49.5 % line-item
   coverage, with absent items left absent rather than estimated.

---

## 8. Reproducing this deployment

```bash
export RAILWAY_TOKEN=<workspace token>

# The Railway CLI cannot be used with a workspace token: every command
# resolves `me`, which such tokens may not query, so `whoami` reports
# "Unauthorized" even though the token is valid. Drive the GraphQL API at
# https://backboard.railway.app/graphql/v2 directly, with a browser
# User-Agent (urllib's default is rejected with 403).

python3 -m alembic upgrade head        # 54 tables
python3 -m app.data                    # 135 companies, ~13 min
python3 deploy/verify_deployment.py   --url https://<app>.up.railway.app
python3 deploy/verify_live_modules.py --url https://<app>.up.railway.app
```

---

## 9. Credential hygiene

- No token or API key is committed; `git grep` for the Gemini, GitHub and
  Railway patterns returns only deliberate test fixtures.
- `.env` is `chmod 600`, git-ignored, and absent from the GitHub tree (`404`).
- The GitHub token was passed in the push URL in memory only; `.git/config`
  contains no credential and no remote.
- **DEP-003's sibling, PD-003**, remains fixed: Gemini authenticates by query
  string and httpx logs at INFO, which printed the key in clear text. `httpx`
  and `httpcore` loggers are raised to WARNING before any call.

**Revoke or rotate both tokens now that the deployment is complete.**
