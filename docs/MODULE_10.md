# Module 10 — Commercial SaaS Platform Layer

> *"This is NOT an admin page. This is the Commercial SaaS Platform Layer."*

Modules 1–9 built a research product. Module 10 makes it a business that can
be sold to more than one customer at once, operated by someone who is not the
author, and deployed by someone who has never read the code.

**1,850 tests · 92% coverage · 150 API paths · 54 tables · 0 undocumented endpoints**

---

## 1. What the workbook did and did not supply

The specification workbook has 54 sheets and none of them describes a SaaS
platform — it is a single-analyst desktop model. Two things were still taken
from it rather than invented:

* **`AI Logs`**: *"Every upload, extraction, API call and refresh is logged
  here with a timestamp. **API keys are never logged.**"* That second clause
  became `domain/platform/audit.redact` — implemented as code, enforced by
  test, applied to the structured log as well as the trail.
* **`AI Settings`**: *"your API key is stored in this workbook in clear text
  … Treat the file as confidential."* The `tenant_secrets` table is the
  platform's answer to that warning: AES-256-GCM, versioned key, never
  returned by any endpoint.

Everything else here is platform engineering, and is documented as such rather
than dressed up as a workbook derivation.

---

## 2. The five load-bearing decisions

### 2.1 Permissions are the primitive; roles are a bundle

Routes never ask *"is this an admin?"*. They ask *"may this caller manage
members?"*. 33 permissions, 7 roles, one declared matrix.

The matrix is written out **in full, one row per role**, rather than derived by
inheritance — because the property that matters in an access-control table is
that any role's grant can be read at a glance and diffed in review.
Monotonicity is then *verified*:

```python
def test_seniority_is_monotone_in_permissions(self):
    for senior, junior in zip(ROLE_ORDER, ROLE_ORDER[1:]):
        assert ROLE_PERMISSIONS[junior] <= ROLE_PERMISSIONS[senior]
```

| Role | Permissions | Summary |
|------|:---:|---------|
| Super Admin | 33 | The only role that crosses tenant boundaries |
| Admin | 29 | Owns one organisation: members, billing, keys, audit |
| Analyst | 19 | Publishes research; deletes; trades a book |
| Researcher | 13 | Contributes research; cannot delete others' work |
| Subscriber | 10 | Consumes research; runs AI; generates reports |
| Read Only | 8 | Reads everything published; writes nothing |
| Guest | 1 | Company data only |

### 2.2 `tenant_id` is the first column and the first predicate

Shared schema, not a database per tenant — the platform must run on SQLite
with no infrastructure *and* answer cross-tenant operator questions without
fanning out over N databases.

The guarantee is paid for three ways: one `TenantScope` that every service
takes, one dependency that builds it, and a test suite that attempts
cross-tenant reads.

**A cross-tenant reach raises rather than returning empty.** Returning nothing
renders a blank page and nobody learns a boundary was probed. Raising means
404 to the caller (never confirming existence) *and* a CRITICAL audit event.

```python
def apply(self, stmt, column):
    if self.unrestricted:
        return stmt
    if self.tenant_id is None:
        return stmt.where(column.is_(None) & column.isnot(None))  # always false
    return stmt.where(column == self.tenant_id)
```

`tenant_id=None` matches **nothing**. The dangerous reading — "null means
wildcard" — is the one that leaks every customer's data to a principal with no
organisation.

### 2.3 One entitlement decision

`domain/platform/plans.evaluate()` is the only place commercial policy is
expressed. Pure — no database, no settings — so the whole pricing model is
testable in microseconds.

Checks run cheapest-and-most-fundamental first, so the message names the real
obstacle: a suspended tenant is told it is suspended, not that it is out of
report credits.

Denials carry the remedy:

```json
{ "reason": "feature_not_in_plan",
  "message": "AI research analyst is not included in the Free plan.",
  "upgrade_to": "professional" }
```

Returned as **402 Payment Required**, not 403. The caller is authenticated and
authorised; the obstacle is commercial, and the frontend shows an upgrade
prompt rather than an error.

### 2.4 Check before, consume after

`check()` asks; `consume()` records. A generation that raises halfway must not
bill the customer for a report they never received.

Both a raw `usage_events` row and a `usage_counters` increment are written:
the counter is what the gate reads (one indexed lookup), the events are what a
billing dispute is settled with. A nightly job reconciles them and reports
drift, because a counter that disagrees with its own evidence is a dispute
waiting to happen.

### 2.5 Nothing secret is recoverable

Argon2id for passwords (memory-hard). SHA-256 for tokens — they are 256 bits
of `secrets.token_urlsafe` entropy, so there is nothing to brute-force and
Argon2 on every request would add 50 ms for no gain. AES-256-GCM for stored
tenant secrets, versioned for rotation.

Enforced structurally:

```python
def test_the_only_model_carrying_a_plaintext_is_the_issued_key(self):
    assert carriers == ["IssuedApiKeyOut"]
```

---

## 3. Authentication

Four methods, one `Principal`. Each differs only in how identity is *proved*;
everything after — status checks, lockout reset, session minting, audit — is
shared, so there is one answer to "what happens when someone signs in".

### Rotation with reuse detection

```
sign in ──► RT₁ ──refresh──► RT₂ ──refresh──► RT₃
              │
              └── presented again ──► the ENTIRE family is revoked
                                      + CRITICAL audit event
```

Presenting a spent token means it was captured. We cannot tell the attacker
from the victim, so both are stopped — and the revocation is the only
available signal that a refresh token has been stolen.

### Enumeration resistance

Registration, password reset and magic link return an identical body whether
or not the address exists. Unknown email and wrong password produce the same
message *and comparable timing* — a dummy Argon2 verification runs on the
"no such user" path specifically to keep them close:

```python
if user is None:
    crypto.verify_password(password, None)   # burn comparable time
    raise AuthError()
```

### Token storage

The access token lives in a **module variable**, not `localStorage` — script
cannot read a variable it does not have a reference to, so an XSS bug cannot
exfiltrate it. The refresh token is an **httpOnly, path-scoped, SameSite
cookie** that script cannot read at all. A page reload recovers the session
through `/auth/refresh`.

---

## 4. Database — 20 new tables (54 total)

### Tenancy
| Table | Purpose |
|-------|---------|
| `tenants` | The unit of isolation, billing and configuration |
| `tenant_secrets` | AES-256-GCM enveloped BYO credentials |

### Identity
| Table | Purpose |
|-------|---------|
| `users` | A person. Argon2id hash, or null for OAuth-only |
| `user_identities` | Federated links — one person, several providers |
| `refresh_tokens` | Digests, with **family lineage** for reuse detection |
| `one_time_tokens` | Verification, reset and magic link — one lifecycle, three purposes |
| `api_keys` | Programmatic credentials, role-bounded and expiring |

### Commerce
| Table | Purpose |
|-------|---------|
| `plans` | Seeded from code, then operator-editable |
| `subscriptions` | Status, metering window, contract overrides |
| `invoices` | Money in **paise** — never a float |
| `billing_events` | Webhooks, unique per provider event → exactly-once |

### Metering and observability
| Table | Purpose |
|-------|---------|
| `usage_events` | The raw record — settles disputes |
| `usage_counters` | The roll-up — one indexed read per gate |
| `audit_logs` | 57 actions, redacted at construction |
| `request_metrics` | One-minute buckets + fixed-boundary histogram |
| `error_events` | Grouped by fingerprint, counted |

### Operations
| Table | Purpose |
|-------|---------|
| `background_jobs` | The unified queue — leases, priority, dead letter |
| `schedule_state` | Last-run bookkeeping for 5 recurring jobs |
| `notifications` | In-app and emailed messages |
| `backup_records` | Location, checksum, and whether it still verifies |

Three modelling choices worth calling out:

* **Money is an integer.** `invoices` stores paise. `0.1 + 0.2 != 0.3`, and an
  invoice out by a rounding error is a support ticket.
* **Metrics are buckets, not rows.** A busy minute is one row, not ten
  thousand, and the platform is observable with no time-series database.
* **Errors are grouped, not appended.** An error loop must not be able to fill
  the database with evidence of itself.

---

## 5. Background work

```
enqueue ──► [queued] ──claim──► [running] ──► [succeeded]
               ▲                    │
               └───requeue_ready────┼──► [failed] ──backoff──┘
                                    └──► [dead_letter] ──manual replay──┘
```

* **Claim is a conditional UPDATE**, so two workers racing produce one winner
  and one retry — on SQLite and Postgres alike, with no `SELECT … FOR UPDATE`.
* **Leases, not locks.** A worker that dies leaves an expired lease and the job
  is reclaimed. That is the same guarantee as an evicted container, so it is
  the one worth relying on.
* **Dead letter, not deletion.** A queue that quietly loses work is worse than
  one that visibly stalls.
* **Deterministic jitter**, seeded on the job id: a thousand simultaneous
  failures spread out, and the schedule is still exactly testable.

Nine kinds; the five the brief named plus alert evaluation, usage roll-up,
backup and retention sweep.

Handlers **call the existing services** — report generation calls
`ReportService.generate`, exactly as the synchronous endpoint does. The
background path must not become a second implementation that drifts.

---

## 6. Observability with no external service

| Concern | Approach | Why not the obvious thing |
|---------|----------|---------------------------|
| Logging | structlog, JSON in prod | Same redaction as the audit — logs are the likelier leak |
| Metrics | 1-minute buckets, interpolated percentiles | Exact percentiles need every sample; a bucket exists to avoid that |
| Errors | Fingerprint + count | One row per distinct error |
| Health | `live` ≠ `ready` | A liveness probe that queries the database restarts a healthy app when the database hiccups |

Route labels are normalised (`/companies/42` → `/companies/{id}`) before
becoming a metric dimension — otherwise every company id is its own label and
the table grows without bound.

Percentiles interpolate *within* the containing bucket rather than returning
its boundary, because reporting "p50 = 100 ms" for everything between 50 and
100 ms makes the number useless for spotting a regression from 55 to 95.

---

## 7. API — 43 new paths

**`/auth`** (17) — config, policy, me, register, login, logout, refresh, verify,
resend, magic link ×2, reset ×2, change password, OAuth ×2, sessions ×2

**`/admin`** (19) — overview, organisation ×3, members ×6, API keys ×3,
subscription ×3, entitlements, usage ×2, audit ×2, storage, jobs,
notifications, RBAC

**`/platform`** (24) — overview, tenants ×6, users, plans ×2, audit, errors ×2,
metrics ×3, jobs ×4, queue, schedules, backups ×4, readiness

Plus `/health/live`, `/health/ready`, `/metrics`.

Every path documented. Literal paths precede `/{id}` throughout — the trap
Modules 7, 8 and 9 each hit in turn, now guarded by test.

---

## 8. Frontend

Two pages, both rendering exactly what the backend computed.

**`/admin`** — Overview · Members · Subscription · Usage · API Keys · Audit Log
**`/platform`** — Overview · Organisations · Users · Plans · Jobs & Queue ·
Health & Errors · Backups

No business logic. A `QuotaBar` renders the utilisation the backend supplied;
it does not compute one. The RBAC matrix is *fetched*, not duplicated in
TypeScript, so the documented matrix and the enforced one cannot diverge —
they are the same object.

A 402 renders as "Upgrade required" with the target plan; a 403 renders as an
access error; a 404 on `/platform` renders as "available to platform operators
only". Three different user actions, three different messages.

---

## 9. Backwards compatibility

Modules 1–9 were built against a `CurrentUser` with `.id`, `.email`, `.name`,
`.role`, and every `owner_id` column they write comes from `.id`.

`Principal` exposes `.id` as an alias for `user_id`, and the seed creates a
real user whose primary key is literally `"dev-user"` — the value Modules 1–9
have been writing all along. **No `UPDATE` was issued against any Module 1–9
table.** Every existing portfolio, watchlist, report and document resolves to
a member of the demo organisation because the id it already stores is now a
real user.

The backfill function verifies this rather than performing it:

```
legacy_owner_resolves: 1 · portfolios: 1 · watchlists: 1 · reports: 4
```

One test in `test_api.py` changed: the development identity's role is now
`super_admin` rather than `admin`, because the seven-role model replaced the
three-role one and the dev operator must reach the operator console. That is a
vocabulary change, stated in the test's own docstring.

---

## 10. What the audit found

Four real defects, three serious. Full detail in
`docs/PRODUCTION_READINESS.md` §4; in brief:

1. **Total deadlock at concurrency 25** — three independent causes: an `async
   def` dependency doing synchronous work on the event loop, middleware
   opening a second session per request, and an unsized connection pool.
   1,700 tests passed throughout, because every one made a single request.
2. **SQLite in rollback-journal mode** froze all readers on every write. WAL
   fixed it; `foreign_keys=ON` was also silently missing, so every declared
   cascade had been a no-op.
3. **Concurrent metric flushes lost a whole batch** to a unique-constraint
   collision.
4. **A self-inflicted authorisation hole**: wiring `JOB_MANAGE` onto
   `/platform/jobs/*` to satisfy a coverage rule opened three cross-tenant
   endpoints to tenant admins, because Admins hold that permission too. The
   permission was necessary and not sufficient. A new invariant test now
   forbids the pattern.

Five further failures were **my tests being wrong**, not the product, and are
listed as such.

---

## 11. Known limitations

1. **MFA is modelled, not enforced.** The brief said future-ready.
2. **OAuth is unexercised** — no provider credentials in this environment.
3. **Billing is hooks, not billing** — no payment provider connected.
4. **Rate limiting is per-process** without Redis.
5. **Restore is manual, deliberately.**
6. **The audit trail is not tamper-evident** — append-only by convention, not
   cryptographically chained.
7. **Throughput is bounded by SQLite and one worker** in the default
   configuration.
8. **Service-layer coverage is uneven** (59–94%); the uncovered branches need
   Redis, SMTP or `pg_dump` present. The domain layer they wrap is 98–100%.

---

## 12. Screenshots

`docs/screenshots/m10-01` … `m10-12`:

| | |
|---|---|
| `m10-01` Admin overview — plan, quotas, activity | `m10-07` Platform overview — MRR, health, queue |
| `m10-02` Members — inline role and status | `m10-08` Organisations — suspend / reactivate |
| `m10-03` Subscription — features and limits | `m10-09` Jobs & queue — dead letter with replay |
| `m10-04` Usage — series and top members | `m10-10` Health & errors — readiness, slow routes |
| `m10-05` API keys — one-time plaintext | `m10-11` Backups — verify and restore command |
| `m10-06` Audit log — filtered, redacted | `m10-12` Plans — the sellable catalogue |
