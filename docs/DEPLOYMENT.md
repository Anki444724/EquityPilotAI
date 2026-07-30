# Deployment Guide — v1.0

Three ways to run this: locally with no infrastructure, locally in the
production shape, and on Railway. All three use the same images and the same
configuration surface.

---

## 1. Local, no infrastructure

The default. SQLite, no Redis, no mail server, no accounts.

```bash
# backend
cd backend
pip install -r requirements.txt
sudo apt-get install -y tesseract-ocr fonts-dejavu-core   # OCR + the ₹ glyph

python3 - <<'PY'
import sys; sys.path.insert(0, '.')
from app.main import app
from app.db.base import Base, engine, SessionLocal
Base.metadata.create_all(bind=engine)
from app.db.seed import seed, seed_module2, seed_reference_company
from app.db.seed_portfolio import seed_module8
from app.db.seed_platform import seed_module10
db = SessionLocal()
seed(db); seed_module2(db); seed_reference_company(db)
seed_module8(db); print(seed_module10(db))
PY

uvicorn app.main:app --reload --port 8000

# frontend, in another shell
cd frontend && npm install && npm run dev
```

`http://localhost:3000` · API docs at `http://localhost:8000/docs`.

Authentication is off, so every caller resolves to a **clearly-labelled
development identity** with Super Admin rights, bound to the `demo-capital`
organisation. The sidebar shows a persistent `DEV IDENTITY` banner so this can
never be mistaken for a secured deployment.

Verification, password reset and magic link all work: with no SMTP host the
platform uses a console transport that logs the link at INFO.

---

## 2. Local, production shape

```bash
docker compose up --build
```

Brings up Postgres, Redis, the API, a **separate worker process** and the web
front end. This is the topology worth testing against: bugs that only appear
with a shared rate limiter, a real connection pool or a job claimed by two
workers do not appear under `uvicorn --reload`.

| Service | Port | Notes |
|---------|------|-------|
| `web` | 3000 | Next.js standalone build |
| `api` | 8000 | `WORKER_ENABLED=false` — it serves requests |
| `worker` | — | `python -m app.worker`, scheduler enabled |
| `postgres` | 5432 | healthchecked with `pg_isready` |
| `redis` | 6379 | shared rate-limit counters |

The compose file contains local-only secrets, clearly labelled. They must not
be reused anywhere — committing a real key is exactly the failure the
workbook's own `AI Settings` sheet warns about.

---

## 3. Railway

### 3.1 Provision

```bash
railway login
railway init
railway add --database postgres
railway add --database redis        # optional; enables shared rate limiting
```

### 3.2 Configure

Generate the two secrets first — the application refuses to sign tokens or
encrypt secrets in production without them:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"   # SECRET_KEY
python3 -c "import secrets; print(secrets.token_urlsafe(48))"   # ENCRYPTION_KEY
```

```bash
railway variables set \
  ENVIRONMENT=production \
  DEBUG=false \
  SECRET_KEY="<generated>" \
  ENCRYPTION_KEY="<generated>" \
  NATIVE_AUTH=true \
  LOG_FORMAT=json \
  RATE_LIMIT_BACKEND=redis \
  CORS_ORIGINS='["https://app.yourdomain.com"]' \
  EMAIL_LINK_BASE=https://app.yourdomain.com \
  OAUTH_REDIRECT_BASE=https://app.yourdomain.com \
  SMTP_HOST=smtp.provider.com \
  SMTP_USERNAME=... SMTP_PASSWORD=... \
  SMTP_FROM=no-reply@yourdomain.com
```

`DATABASE_URL` and `REDIS_URL` are injected by Railway.

### 3.3 Deploy

Three services from one repository:

| Service | Root | Start command |
|---------|------|---------------|
| **api** | `backend/` | `uvicorn app.main:app --host 0.0.0.0 --port $PORT --proxy-headers --forwarded-allow-ips=*` |
| **worker** | `backend/` | `python -m app.worker` |
| **web** | `frontend/` | `node server.js` (standalone output) |

`--proxy-headers` is not optional. TLS terminates at Railway's edge, and
without it every client IP is recorded as the proxy's — which means the rate
limiter throttles the entire internet as a single caller and every audit row
records the same address.

Set on the worker only:

```
WORKER_ENABLED=true
SCHEDULER_ENABLED=true
```

**Exactly one process may run the scheduler.** Two and every recurring job
fires twice — two backups, two retention sweeps.

The frontend needs `NEXT_PUBLIC_API_URL` at *build* time, not run time; Next
inlines `NEXT_PUBLIC_*` during the build, so changing the backend URL requires
a rebuild.

### 3.4 First boot

```bash
railway run python3 -c "
import sys; sys.path.insert(0,'.')
from app.main import app
from app.db.base import Base, engine, SessionLocal
Base.metadata.create_all(bind=engine)
from app.db.seed_platform import seed_plans
print('plans seeded:', seed_plans(SessionLocal()))"
```

Then register the first account through the UI. Whoever registers first owns
their organisation as Admin. To mint a platform operator, promote that user
once:

```bash
railway run python3 -c "
import sys; sys.path.insert(0,'.')
from app.db.base import SessionLocal
from app.models.platform import User
from sqlalchemy import select
db = SessionLocal()
u = db.scalar(select(User).where(User.email=='you@yourdomain.com'))
u.role = 'super_admin'; db.commit(); print('promoted', u.email)"
```

---

## 4. Configuration reference

`backend/.env.example` documents all 67 settings. The ones that matter in
production:

| Setting | Required | Consequence if wrong |
|---------|----------|----------------------|
| `SECRET_KEY` | **Yes** | App refuses to sign tokens. Without it, anyone can mint an admin session. |
| `ENCRYPTION_KEY` | **Yes** (falls back to `SECRET_KEY`) | Stored tenant secrets undecryptable after restart |
| `NATIVE_AUTH` | **Yes** | `false` in production means every caller is a Super Admin |
| `DATABASE_URL` | **Yes** | SQLite is not a multi-tenant production database |
| `CORS_ORIGINS` | **Yes** | A wildcard or `http://` origin defeats the cookie protections |
| `SMTP_HOST` | Yes in practice | Verification and reset emails cannot be delivered |
| `SCHEDULER_ENABLED` | One process only | Duplicate recurring jobs |
| `RATE_LIMIT_BACKEND` | `redis` when replicated | Each replica enforces its own share of the limit |

`GET /health/ready` computes these rather than asserting them — see
`Settings.production_readiness_problems()` — and returns **503** when a
critical check fails, so a load balancer removes the instance rather than
sending it work it cannot do.

---

## 5. Health and monitoring

| Endpoint | Auth | Purpose |
|----------|------|---------|
| `GET /health` | none | Module 1's original shape, unchanged |
| `GET /health/live` | none | Liveness. Touches nothing. Use for restart policy. |
| `GET /health/ready` | none | Readiness. Checks database, schema, config, queue. 503 when unready. |
| `GET /metrics` | none | Aggregate counts and latencies. No customer data. |
| `GET /api/v1/platform/readiness` | operator | The same checks, with detail |
| `GET /api/v1/platform/metrics` | operator | p50/p95/p99, per route |
| `GET /api/v1/platform/queue` | operator | Depth, backlog, dead letters |

**Point the container restart policy at `/health/live`, and the load balancer
at `/health/ready`.** Reversing them turns a brief database outage into a
restart storm.

The queue check is deliberately **non-critical**: a stalled queue degrades the
product, but the API can still serve every synchronous request, and taking the
instance out of rotation would make things worse.

---

## 6. Scaling

| Symptom | Action |
|---------|--------|
| API latency rises, CPU flat | Raise `pool_size` in `db/base.py`; check for a slow query in `/platform/metrics/routes` |
| API CPU saturated | Add API replicas. They are stateless. |
| Queue backlog grows | Add worker replicas. Claim is atomic; they will not collide. |
| Rate limits behaving per-replica | Set `REDIS_URL` and `RATE_LIMIT_BACKEND=redis` |
| Recurring jobs firing twice | More than one process has `SCHEDULER_ENABLED=true` |

Sizing: pool is 20 + 20 overflow per process, so 40 connections. Postgres
defaults to 100, comfortably fitting two replicas with headroom for `psql` and
`pg_dump`.

---

## 7. Backup and recovery

### Strategy

| What | How | Where |
|------|-----|-------|
| Database | `pg_dump -Fc` (Postgres) or the SQLite online backup API | `BACKUP_DIR`, retained by count |
| Documents | **Inside the database.** Module 7 stores extracted content and Module 9 stores rendered artefacts as `LargeBinary` rows. | Same backup |
| Secrets | Not in the backup. `ENCRYPTION_KEY` must be stored separately in a secret manager. | — |

There is no object store to back up separately. That is a deliberate
simplification: it removes any possibility of the database and the file store
drifting into inconsistency with each other.

**Retention is by count, not age.** An instance that was down for a fortnight
should not wake up and delete its only copies.

### Verification

A backup nobody has restored is a hypothesis. `POST
/api/v1/platform/backups/{id}/verify` performs three distinct checks:

1. the artefact exists — a storage problem;
2. it still matches its recorded SHA-256 — corruption or tampering;
3. it decompresses and passes `PRAGMA integrity_check` with the expected table
   count — it was never valid.

Run it on a schedule, not only after an incident.

### Recovery procedure

Restore is **deliberately not an API call**. A one-click restore is a one-click
way to destroy a production database. The platform supplies the exact command
instead:

```bash
# 1. Stop writers
railway service scale api --replicas 0
railway service scale worker --replicas 0

# 2. Fetch and verify the artefact
#    (the command below is returned by the verify endpoint)
pg_restore --clean --if-exists --no-owner --dbname "$DATABASE_URL" ierp-auto-20260730T0200Z.dump

# 3. Confirm the schema is complete before admitting traffic
curl -fsS https://api.yourdomain.com/health/ready

# 4. Restart
railway service scale api --replicas 2
railway service scale worker --replicas 1
```

For SQLite:

```bash
gunzip -c ierp-auto-20260730T0200Z.sqlite.gz > restored.db
sqlite3 restored.db 'PRAGMA integrity_check;'
mv restored.db ierp.db
```

**Recovery objectives.** With the daily schedule, RPO is 24 hours and RTO is
roughly 15 minutes for a database of this size. Tighten RPO by raising the
backup frequency in `domain/platform/jobs.SCHEDULES` or by enabling Railway's
own point-in-time recovery, which is continuous and strictly better than any
schedule this application can run.

---

## 8. CI/CD

`.github/workflows/ci.yml` — five jobs:

| Job | What it proves |
|-----|----------------|
| `backend` | 1,850 tests on **both** SQLite and Postgres, ≥85% coverage gate |
| `quality` | The 35 architectural invariants, on their own so a violation is unmissable |
| `frontend` | `tsc --noEmit` and a production build |
| `security` | `pip-audit`, `bandit`, `gitleaks` |
| `docker` | Both images build **and the API image starts and answers** |

The Postgres matrix leg matters. SQLite tolerates looser typing and has no
real concurrency; a suite that only ever runs on SQLite passes right up until
deployment.

---

## 9. Common problems

| Symptom | Cause | Fix |
|---------|-------|-----|
| 401 on every request after deploying | `NATIVE_AUTH=true` with no account yet | Register through the UI, then promote to `super_admin` |
| Every audit row shows the same IP | Missing `--proxy-headers` | Add it to the start command |
| Rate limit triggers far too early | Same cause — all traffic looks like one caller | Same fix |
| Every ₹ is a black box in a PDF | DejaVu fonts absent | `apt-get install fonts-dejavu-core` (the Dockerfile does) |
| OCR silently returns nothing | tesseract binary absent | `apt-get install tesseract-ocr` |
| Recurring jobs run twice | Two processes with `SCHEDULER_ENABLED=true` | Enable on the worker only |
| `/health/ready` returns 503 | A critical check failed | Read `checks[]` — it names the problem |
| Frontend calls `localhost:8000` in production | `NEXT_PUBLIC_API_URL` is inlined at build time | Rebuild the image with the right value |
