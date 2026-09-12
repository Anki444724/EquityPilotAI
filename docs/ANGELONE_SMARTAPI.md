# Angel One SmartAPI Broker Integration

Live trading integration for Angel One (Indian broker) through its **SmartAPI**
REST contract. The platform acts as a SmartAPI application: users connect
their *own* Angel One trading account, and the platform can read their
session data (profile, funds, orders, positions, trades, holdings) and place,
modify and cancel orders on their behalf — with every step owned, audited and
rate limited by the platform.

```
Browser ──GET /login──▶ API ──302──▶ Angel One sign-in page
                                (user enters their own broker credentials)
Browser ◀──redirect─────────────────────┘
Browser ──GET /callback?access_token=…&client_code=…&state=…──▶ API
API: validate + consume single-use state, envelop the token, persist
Browser ◀──302── configured frontend URL

API ──SmartAPI REST (Bearer <user's token>)──▶ api.angelone.com/pal
Angel One ──POST /postback──▶ API (order status changes)
```

---

## 1. Architecture

| Layer | File | Responsibility |
| --- | --- | --- |
| REST client | `backend/app/services/broker/angelone_client.py` | Thin httpx client for the official SmartAPI contract. Envelope parsing, error mapping, base64 credential encoding. **Never logs headers or bodies.** |
| Product logic | `backend/app/services/broker/angelone_service.py` | Session lifecycle, single-use callback state, ownership scoping, order persistence, postback validation, normalisation, audit calls. |
| Errors | `backend/app/services/broker/exceptions.py` | One namespaced hierarchy; each exception carries the HTTP status the API must surface. |
| Persistence | `backend/app/models/broker.py` | `broker_accounts` (one session per user, tokens enveloped) and `broker_orders` (orders this platform created). |
| Schemas | `backend/app/schemas/broker.py` | Pydantic contracts; strict order validation at the API boundary. |
| API | `backend/app/api/v1/broker_angelone.py` | The 14 endpoints under `/api/v1/broker/angelone`. |
| Migration | `backend/alembic/versions/b10a01b0a101_add_broker_tables.py` | Additive, reversible. |

**Design decisions**

* **Raw httpx, no `smartapi-python`.** The SmartAPI contract is a small,
  stable set of JSON endpoints; a 150-line client is easier to audit than a
  dependency, and it keeps the token path under our control (see Security).
* **Browser authorization flow is primary.** The user signs in on Angel
  One's own page; the platform only ever *receives* the session in the
  callback. The platform never sees or stores the user's broker
  password/TOTP as entered by the user — it stores what the broker hands
  back (access token + T2 trading password), enveloped. The client also
  implements the REST `login`/`validate` endpoints from the official
  contract for headless deployments, but no public route exposes them.
* **Ownership is the security model.** Every read and order call resolves
  the caller's *own* `BrokerAccount`; order references resolve against the
  caller's *own* `BrokerOrder` rows. There is no cross-user path.
* **Postbacks are untrusted input.** They carry no platform identity, so
  they are applied only to order ids the platform itself created, and only
  when the broker's client code matches the owning account (see §7).

---

## 2. Configuration

All values in `backend/app/core/config.py` (env-driven; see
`backend/.env.example` for the annotated block).

| Variable | Default | Purpose |
| --- | --- | --- |
| `ANGELONE_API_KEY` | *(unset)* | The platform's SmartAPI application key (developer portal). Sent as the login redirect's `APP_ID`/`CLIENT_ID`. **Platform secret — environment only.** |
| `ANGELONE_LOGIN_URL` | `https://risk.angelone.in/fund/auth` | Where `/login` sends the browser for the broker's own sign-in. |
| `ANGELONE_API_BASE` | `https://api.angelone.com/pal` | Base of the SmartAPI REST contract. |
| `ANGELONE_REDIRECT_URL` | `http://localhost:8000/api/v1/broker/angelone/callback` | Registered with Angel One as the authorization callback. **Production: `https://equitypilot.in/api/v1/broker/angelone/callback`** |
| `ANGELONE_POSTBACK_URL` | `http://localhost:8000/api/v1/broker/angelone/postback` | Registered with Angel One as the order-status postback. **Production: `https://equitypilot.in/api/v1/broker/angelone/postback`** |
| `ANGELONE_FRONTEND_REDIRECT` | `http://localhost:3000` | Where the browser lands after a successful callback. Configured value only — never caller-supplied. |
| `ANGELONE_STATE_TTL_SECONDS` | `300` | Lifetime of the single-use callback `state` token. |
| `ANGELONE_TIMEOUT_SECONDS` | `15` | Outbound broker HTTP timeout. |
| `ANGELONE_PUBLIC_IP` | *(unset)* | The public IP registered with Angel One. **Production: `15.252.155.41`** Informational. |

Nothing secret is hard-coded: the only fixed values are the public Angel
One endpoint hosts.

---

## 3. Setup

1. **Register the SmartAPI application** in the Angel One developer portal:
   * API key + secret.
   * Redirect URL: `https://equitypilot.in/api/v1/broker/angelone/callback`
   * Postback URL: `https://equitypilot.in/api/v1/broker/angelone/postback`
   * Public IP: `15.252.155.41` (the deployment's static egress IP).
2. **Set the environment** on the API service:
   ```
   ANGELONE_API_KEY=<from portal>
   ANGELONE_REDIRECT_URL=https://equitypilot.in/api/v1/broker/angelone/callback
   ANGELONE_POSTBACK_URL=https://equitypilot.in/api/v1/broker/angelone/postback
   ANGELONE_FRONTEND_REDIRECT=https://equitypilot.in
   ANGELONE_PUBLIC_IP=15.252.155.41
   ```
   (The two defaults — login URL and API base — are the public Angel One
   hosts and need no override.)
3. **Migrate the database:**
   ```
   alembic upgrade head     # revision b10a01b0a101 adds the broker tables
   ```
4. **Permissions (no action needed):** `broker:read` is granted from
   Researcher up; `broker:trade` from Analyst up (see §5).

### Verification

* `GET /api/v1/broker/angelone/status` → `{"connected": false, …}`.
* `GET /api/v1/openapi.json` lists the 13 broker paths.
* Walk the login flow in a staging browser; watch
  `broker.login.started` / `broker.login.succeeded` appear in the audit
  trail.

---

## 4. Endpoints

All under `/api/v1/broker/angelone`. Permissions use the existing platform
matrix (`domain/platform/identity.py`); rate limits use the existing
sliding-window infrastructure (`domain/platform/limits.py`).

| Method & path | Permission | Limit | Description |
| --- | --- | --- | --- |
| `GET /login` | `broker:read` | `broker.login` 5/10min | Mints the single-use state; 302 to the broker's sign-in page. |
| `GET /callback` | `browser session` | — | Validates + consumes the state, persists the enveloped session, 302 to the configured frontend URL. |
| `GET /status` | `broker:read` | `broker.read` | Session state: connected, client code, expiry, last postback. |
| `POST /disconnect` | `broker:read` | `broker.read` | Ends the session; wipes both enveloped tokens (idempotent). |
| `GET /profile` | `broker:read` | `broker.read` | Live broker profile. |
| `GET /funds` | `broker:read` | `broker.read` | Live margin/funds. |
| `GET /orders` | `broker:read` | `broker.read` | Today's order book. |
| `GET /positions` | `broker:read` | `broker.read` | Live positions. |
| `GET /trades?trade_date=YYYY-MM-DD` | `broker:read` | `broker.read` | Executed trades for one day (defaults to today). |
| `GET /holdings` | `broker:read` | `broker.read` | Holdings. |
| `POST /orders` | `broker:trade` | `broker.trade` 10/min | Place an order. |
| `POST /orders/modify` | `broker:trade` | `broker.trade` | Modify a platform-created order. |
| `DELETE /orders/{order_id}` | `broker:trade` | `broker.trade` | Cancel a platform-created order. |
| `POST /postback` | *none* (broker-to-platform) | `broker.postback` 120/min/IP | Apply a broker order-status change. |

Note on the tasking's `GET /orders/modify`: modification is a write and has
exactly one method, `POST`. A `GET` on that path answers 405 by
construction — there is no read form of "modify".

### Order request (`POST /orders`)

```json
{
  "symbol": "RELIANCE",
  "side": "B",
  "product": "CNC",
  "order_type": "LIMIT",
  "validity": "DAY",
  "quantity": 10,
  "price": 2450.0,
  "trigger_price": null
}
```

* `side`: `B` | `S` — the broker's own vocabulary.
* `product`: `CNC` | `MIS` | `MARGIN`.
* `order_type`: `LIMIT` | `MARKET` | `SL-M` | `SL-L`.
* `validity`: `DAY` | `IOC`.
* Validation is strict at the boundary: `MARKET` takes no price;
  `LIMIT`/`SL-M`/`SL-L` require a price; `SL-M`/`SL-L` require a
  `trigger_price`. Violations are 422 **before** any broker call.
* Response `201` with `order_id` (the Angel One order id), `status: "OPEN"`,
  and the order's symbol/exchange/quantity/price.

### Error mapping

| Broker condition | HTTP | Meaning |
| --- | --- | --- |
| No/expired/disconnected session | `401` | Log in to your broker (again). |
| Malformed request / callback without valid state | `400` | Fix the request; retry is pointless. |
| Order not found (not this platform's, not this user's) | `404` | No existence disclosure. |
| Exchange/broker rejected the order | `422` | Read the broker's message. |
| Platform rate limit | `429` + `Retry-After` | Wait. |
| Broker rate limit | `429` + `Retry-After` | Wait. |
| Broker unreachable / bad envelope | `502` | Transient; retry later. |

---

## 5. Permissions

Added to the existing permission matrix (no parallel system):

```
Permission.BROKER_READ  = "broker:read"    → Researcher and above
Permission.BROKER_TRADE = "broker:trade"   → Analyst and above
```

* Read covers the session itself (start/status/disconnect) plus all data
  reads. A Researcher can connect their broker and watch it; they cannot
  move money.
* Trade covers place/modify/cancel.
* `trade` is in the platform's *write-verb* set, so a **past-due tenant is
  degraded to read-only even on its own broker account** — an unpaid
  organisation cannot place real orders, mirroring how it cannot write
  research.
* Every denial is audited (`security.access.denied`) by the existing
  `require(...)` dependency, exactly like every other permission.

---

## 6. Security

1. **Zero secret logging.** The client logs method, path, HTTP status, and
   the broker's own error code/message — never headers or bodies. The
   platform's audit redaction (deny-by-default on key name) independently
   catches `access_token`, `trading_password`, `TOTP`, `otp`, … if any code
   ever passes one into audit metadata. Both layers are covered by tests
   (`test_secrets_never_reach_the_logger`,
   `test_audit_redaction_covers_broker_secret_names`).
2. **Encrypted persistence.** Per-user access tokens and T2 trading
   passwords are enveloped with the platform's AES-256-GCM envelope
   (`services/platform/crypto.py`), versioned alongside every other stored
   secret. A database leak yields ciphertext. **Per-user broker tokens are
   never environment variables.** The only broker secret in the
   environment is the platform-level SmartAPI API key.
3. **Callback state: validated, single-use, TTL-bound.** Minted with
   `secrets.token_urlsafe(32)`, stored in the `broker_state` cache namespace
   bound to the user, compared with `secrets.compare_digest`, consumed
   *before* anything else runs, and replayable within one minute only in the
   sense of being *recognised and refused* (audited). Unknown, mismatched,
   expired or replayed states are refused with 400 and audited as
   `broker.callback.rejected`.
4. **No cross-user access.** Accounts and orders are always queried with
   the caller's `user_id`. A foreign order id 404s — it never 403s, because
   "that order exists but isn't yours" is a disclosure.
5. **Postbacks touch only platform-created orders** (§7). Arbitrary or
   fabricated order ids have no row to update and are refused.
6. **Broker identifiers are validated.** Client codes must match
   `^\d{10}$` before they are accepted into a session or compared against a
   postback; symbols are upper-cased and length-bounded; order fields are
   validated by Pydantic before a broker call is spent.
7. **The callback redirect is safe.** After a successful callback the
   browser goes to `ANGELONE_FRONTEND_REDIRECT` — a configured value, never
   a parameter of the request, so the flow cannot become an open redirect.
8. **Rate limiting.** `broker.login` 5/10min/user, `broker.read`
   60/min/user, `broker.trade` 10/min/user, `broker.postback`
   120/min/IP — all on the platform's existing sliding-window limiter.
9. **Audit.** Every transition writes to the existing audit trail:
   `broker.login.started/succeeded/failed`, `broker.callback.rejected`,
   `broker.disconnected`, `broker.order.placed/modified/cancelled/rejected`,
   `broker.postback.received/rejected`.

---

## 7. Callback and postback flows

### Callback (browser)

```
GET /callback?access_token=…&client_code=…&valid_upto=…&trading_password=…&state=…
```

1. Rate-limited context resolved; the caller must be authenticated (their
   own browser session is intact — the redirect came from *their* login).
2. `state` must equal the one minted for this user within its TTL, and must
   not already be consumed → otherwise 400 + audit
   `broker.callback.rejected`.
3. The state is **consumed immediately** (marked used, short TTL) before any
   further processing, so a concurrent duplicate can only ever complete
   once.
4. `access_token` must be present and `client_code` must be a 10-digit
   code → otherwise 400 + audit.
5. The session is upserted: tokens enveloped, `valid_upto` parsed
   (`YYYYMMDDHHMMSS`), `connected=true`, audit `broker.login.succeeded`.
6. 302 to `ANGELONE_FRONTEND_REDIRECT`.

### Postback (broker → platform)

Angel One POSTs a JSON order-status object directly to the registered
postback URL. **No platform identity travels with it**, so the endpoint is
the strictest in the module:

```json
{
  "ORDER_ID": "1000000001",
  "EXCH_ORDER_ID": "EXCH-77",
  "CLIENT_CODE": "1234567890",
  "SYMBOL": "RELIANCE",
  "TRANS_TYPE": "B",
  "ORDER_STATUS": "COMPLETE",
  "QUANTITY": "10",
  "FILL_QTY": "10",
  "AVG_PRICE": "2449.50",
  "REMARK": ""
}
```

Processing, in order:

1. `broker.postback` rate limit (per IP).
2. Body must be a JSON object → else 400.
3. `ORDER_ID` required → else 400 + audit `broker.postback.rejected`.
4. The order id must exist in `broker_orders` — **an id this platform never
   created is refused (404) and audited**; nothing is ever *created* from a
   postback.
5. If the postback carries a `CLIENT_CODE`, it must equal the
   `client_code` of the account that owns the order → else 404 + audit.
6. If the postback carries a `SYMBOL`, it must equal the recorded order's
   symbol → else 404 + audit.
7. Apply: `status`, `fill_quantity`, `average_price`,
   `exchange_order_id`, `remark`, timestamps; commit.
8. Audit `broker.postback.received` with the order's owner as actor and the
   owning tenant as tenant.
9. Respond `{"accepted": true, "order_id": …, "status": …}`.

Refusals return a generic body (no confirmation that an order id exists or
not beyond the standard 404/400), and every refusal is audited with
`outcome: "rejected"`.

### Session expiry behaviour

* The broker's `valid_upto` is stored on the account. `/status` reports
  `connected: false` once it passes; any data/order call raises
  `BrokerSessionError` → 401 *"Your broker session has expired"* **before**
  spending the round trip.
* Even if the clock check is missed (e.g. `valid_upto` unparseable — the
  safe direction is "expired"), the broker's own 401/403 is mapped to the
  same 401 by the client.
* Re-login is a fresh `/login` → `/callback`; the upsert rotates the
  enveloped tokens in place (one row per user, always).
* Disconnect is explicit and irreversible-without-relogin: both enveloped
  tokens are set to `NULL`, so a stale client cannot be minted from the row.

---

## 8. Troubleshooting

| Symptom | Likely cause | Check |
| --- | --- | --- |
| `/login` 502s in dev | `ANGELONE_API_KEY` unset — the redirect URL is still built, but the broker will refuse a login without a valid app id. | Set the key; confirm it in the SmartAPI console. |
| Callback 400 `Login state is missing or unknown` | State expired (TTL), wrong user completed the callback, or the callback URL registered with Angel One differs from `ANGELONE_REDIRECT_URL` so Angel One redirected to a host without our state. | Compare the registered redirect URL with the env var; retry within 5 minutes. |
| Callback 400 `already completed` | The browser (or a proxy retry) delivered the callback twice. | Expected; start a new login. Look for `broker.callback.rejected` in the audit. |
| Data calls 401 `session expired` | Broker session lapsed (Angel One sessions are short-lived, often end-of-day). | Re-login. |
| Orders 422 with broker message | Exchange rejection (funds, bands, symbol). The broker's `message` is in the response detail and the audit trail. | Read the message; it is the broker's, not a platform bug. |
| `/postback` 404 in the field | Angel One posts to the URL registered in the console — if `ANGELONE_POSTBACK_URL` (env) and the console disagree, or the order was placed outside the platform, refusal is *correct*. | Verify console registration = `https://equitypilot.in/api/v1/broker/angelone/postback`. |
| Postback 429 | A burst above 120/min/IP — usually the broker replaying a full order book at market open. | Transient; the broker retries. |
| SQLite `can't compare naive/aware`-class errors | None expected — stored broker timestamps go through the platform's `_aware` helper. If you add a new datetime comparison in this module, use `_aware(...)` first (SQLite drops tzinfo). | — |

---

## 9. Known SmartAPI limitations

* **Session lifetime is broker-controlled.** Angel One access tokens are
  typically valid for a few hours / until end of trading day; the platform
  can detect expiry (`valid_upto`, 401 mapping) but cannot extend a session.
  Re-login is the only remedy, and it requires the user's browser.
* **No broker-side order history beyond intraday.** The SmartAPI data
  endpoints serve the intraday order book, positions, one day of trades, and
  holdings. Historical orders older than the day must be read from the
  broker's portal; the platform's `broker_orders` table is the durable
  record of what *this platform* placed.
* **Modify re-sends the whole order.** SmartAPI has no partial update; the
  platform reconstructs the full order object from its own row plus the
  changes. A field the broker changed server-side (rare) would be
  overwritten by the client's value.
* **Postbacks have no signature.** SmartAPI postbacks are not HMAC-signed;
  authenticity is inferred from (a) the order id being platform-created,
  (b) client-code match, and (c) the registered public IP. The platform
  therefore treats a postback as a *status hint over its own order record*
  — it can move an order's status/fill, but it cannot create orders, change
  quantities of other users' orders, or touch funds.
* **One broker account per user.** `broker_accounts` has a unique index on
  `user_id`; connecting a second Angel One account replaces the first
  (re-login). Multi-account support would be a model change.
* **NSE/BSE segment ids are fixed** (`NSE=91`, `BSE=92`) per the contract;
  other segments are not used by this integration.

---

## 10. Tests

`backend/tests/test_broker_angelone.py` — 52 tests, **no network calls and
no real orders**:

* Login/callback: state minting, valid callback, single-use replay, unknown
  state, expired state, missing token, invalid client code.
* Session lifecycle: status before/after login, disconnect token wipe,
  401s without a session.
* Data reads: profile, funds, orders, positions, trades (default date),
  holdings — including string→number normalisation.
* Orders: placement (payload shape, persistence, audit), strict validation
  422s, modify (full payload, row update), cancel, unknown-order 404s,
  broker-rejection 422, session 401, network 502, broker rate limit 429.
* Platform rate limiting: trade limit and login limit both trip 429.
* Authorisation: real API keys for real Researcher and Subscriber users —
  read-but-not-trade, and no-read-at-all — plus the matrix itself.
* Postbacks: applied update, unknown order, missing id, client-code
  mismatch, symbol mismatch, non-JSON body.
* Client-level: real httpx against `MockTransport` — token travels only in
  the `Authorization` header, secrets never reach the logger, envelope
  errors map correctly, base64/valid-upto parsing, REST login validation.
* Redaction: broker secret key names and JWT-shaped values never survive
  into audit metadata.

Run:

```
cd backend
.venv/bin/python -m pytest tests/test_broker_angelone.py -v
.venv/bin/python -m pytest tests/
```
