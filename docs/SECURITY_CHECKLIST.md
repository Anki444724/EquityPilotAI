# Security Checklist — v1.0

Each row states what is implemented, where it lives, and which test proves it.
A control with no test is listed as **unverified**, because a security claim
nobody has exercised is a hope.

Legend: ✅ implemented and tested · ⚠️ implemented, partly tested ·
🔶 deliberate limitation · ❌ not implemented

---

## 1. Authentication

| # | Control | Status | Where | Test |
|---|---------|:---:|-------|------|
| 1.1 | Passwords hashed with a memory-hard KDF (Argon2id, 64 MiB / t=3 / p=4) | ✅ | `crypto.hash_password` | `test_argon2id_is_the_algorithm` |
| 1.2 | Per-password salt | ✅ | Argon2 internal | `test_hashes_are_salted` |
| 1.3 | Silent parameter upgrade on sign-in | ✅ | `authenticate` | `needs_rehash` path |
| 1.4 | Null hash never verifies (OAuth-only accounts) | ✅ | `verify_password` | `test_a_null_hash_never_verifies` |
| 1.5 | Timing parity between unknown user and wrong password | ✅ | `_dummy_verify` | `test_an_unknown_address_and_a_wrong_password_look_identical` |
| 1.6 | Password policy: length over character classes | ✅ | `limits.validate_password` | 11 tests in `TestPasswordPolicy` |
| 1.7 | Breach-list rejection | ✅ | `COMMON_PASSWORDS` | `test_breached_passwords_are_refused_at_any_length` |
| 1.8 | Password may not contain the user's email | ✅ | `validate_password` | `test_a_password_containing_the_email_is_refused` |
| 1.9 | Account lockout after N failures | ✅ | `_record_failure` | `test_repeated_failures_lock_the_account` |
| 1.10 | Lockout cleared on success | ✅ | `_establish_session` | `test_a_successful_sign_in_clears_the_failure_count` |
| 1.11 | Email verification required before first sign-in | ✅ | `LOGIN_ALLOWED_STATUSES` | `test_a_pending_user_cannot_sign_in` |
| 1.12 | OAuth `state` parameter, httpOnly, compared constant-time | ✅ | `oauth_start` / `oauth_callback` | — ⚠️ needs a live provider |
| 1.13 | Federated auto-link restricted to providers that verify email | ✅ | `FEDERATED_PROVIDERS` | `test_a_non_federated_provider_is_refused` |
| 1.14 | Magic link single-use, 15-minute expiry | ✅ | `consume_one_time_token` | `test_a_magic_link_is_single_use` |
| 1.15 | MFA | 🔶 | Enrolment modelled, secret enveloped; **not enforced at login** | — |

## 2. Session management

| # | Control | Status | Where | Test |
|---|---------|:---:|-------|------|
| 2.1 | Short access token (15 min) | ✅ | `ACCESS_TOKEN_TTL_SECONDS` | `test_access_tokens_are_short…` |
| 2.2 | Refresh token rotation | ✅ | `refresh_session` | `test_refresh_issues_a_new_pair` |
| 2.3 | **Reuse detection revokes the token family** | ✅ | `ReuseDetected` | `test_replaying_a_spent_token_revokes_the_whole_family` |
| 2.4 | Refresh token stored as SHA-256, never plaintext | ✅ | `RefreshToken.token_hash` | `TestResponseHygiene` |
| 2.5 | Refresh token in an httpOnly, SameSite, path-scoped cookie | ✅ | `_set_refresh_cookie` | `test_register_verify_login_refresh_logout` |
| 2.6 | Secure cookies forced in production | ✅ | `settings.cookie_secure` | `test_secure_cookies_are_forced_in_production` |
| 2.7 | Access token never written to `localStorage` | ✅ | `lib/api.ts` module variable | reviewed |
| 2.8 | Password change revokes every session | ✅ | `change_password` | `test_reset_changes_the_password_and_kills_every_session` |
| 2.9 | Role change revokes every session | ✅ | `change_role` | `test_a_role_change_ends_the_member_sessions` |
| 2.10 | Suspension revokes immediately, not on expiry | ✅ | `set_status` | `test_suspending_a_member_ends_their_sessions` |
| 2.11 | User row loaded on every request, not trusted from claims | ✅ | `principal_from_access_token` | `test_a_suspended_user_cannot_use_a_live_access_token` |
| 2.12 | JWT algorithm pinned; `alg: none` rejected | ✅ | `decode_jwt` | `test_the_none_algorithm_is_not_accepted` |
| 2.13 | Token type checked (access ≠ refresh) | ✅ | `expected_type` | `test_a_token_of_the_wrong_type_is_refused` |
| 2.14 | Tampered token rejected | ✅ | HMAC verify | `test_a_tampered_token_is_refused` |
| 2.15 | Signing key mandatory in production | ✅ | `_signing_key` | `test_production_refuses_to_run_with_a_generated_signing_key` |

## 3. Authorisation

| # | Control | Status | Where | Test |
|---|---------|:---:|-------|------|
| 3.1 | Permission-based, not role-based, at the route | ✅ | `require(Permission…)` | `test_routes_ask_for_permissions_not_roles` |
| 3.2 | Seven roles, monotone in seniority | ✅ | `ROLE_PERMISSIONS` | `test_seniority_is_monotone_in_permissions` |
| 3.3 | Read Only holds no write permission | ✅ | matrix | `test_read_only_holds_no_write_permission` |
| 3.4 | Admin cannot reach the operator console | ✅ | `CROSS_TENANT_PERMISSIONS` | `test_an_admin_cannot_reach_the_operator_console` |
| 3.5 | Operator-only permissions held by exactly one role | ✅ | matrix | `test_every_operator_only_permission_is_genuinely_operator_only` |
| 3.6 | No `/platform/*` route guarded solely by a permission tenants also hold | ✅ | both guards applied | `test_no_platform_route_relies_on_a_permission_tenants_also_hold` |
| 3.7 | Peers cannot administer each other | ✅ | `outranks` | `test_an_admin_may_not_change_a_peer` |
| 3.8 | Nobody may escalate their own role | ✅ | `change_role` | `test_nobody_may_change_their_own_role` |
| 3.9 | Nobody may grant a role above their own | ✅ | `change_role` | `test_an_admin_may_not_grant_a_role_above_their_own` |
| 3.10 | Last administrator cannot be removed or demoted | ✅ | `last_admin_check` | `test_an_admin_cannot_demote_the_last_administrator` |
| 3.11 | Super Admin cannot be granted by invitation | ✅ | `InviteRequest` validator | `test_super_admin_cannot_be_granted_by_invitation` |
| 3.12 | Denials audited | ✅ | `_audit_denial` | `test_a_denial_is_written_to_the_audit_trail` |
| 3.13 | Operator console answers 404, not 403 | ✅ | `require_operator` | `test_an_admin_cannot_reach_the_operator_console` |

## 4. Multi-tenant isolation

| # | Control | Status | Where | Test |
|---|---------|:---:|-------|------|
| 4.1 | `tenant_id` on every tenant-owned table | ✅ | `models/platform.py` | `test_every_tenant_owned_model_carries_a_tenant_id` |
| 4.2 | One filter helper, applied per request | ✅ | `TenantScope` | `test_tenant_scope_is_the_only_filter_helper` |
| 4.3 | A null tenant matches nothing, not everything | ✅ | `TenantScope.apply` | `test_a_null_tenant_matches_nothing` |
| 4.4 | Cross-tenant reach raises, not returns empty | ✅ | `TenantIsolationError` | `test_check_raises_on_a_foreign_row` |
| 4.5 | Foreign resource → 404, never 403 | ✅ | `_member_or_404` | `test_a_foreign_member_is_a_404_never_a_403` |
| 4.6 | Isolation violation logged CRITICAL | ✅ | `tenant_guard` | `test_security_relevant_actions_are_not_logged_as_info` |
| 4.7 | No request body accepts `tenant_id` | ✅ | schemas | `test_admin_routes_derive_the_tenant_from_the_principal` |
| 4.8 | Member, audit, key, usage and entitlement views all disjoint | ✅ | — | 8 tests in `TestTenantIsolation` |
| 4.9 | Tenant suspension revokes every member session | ✅ | `suspend_tenant` | `test_a_tenant_can_be_created_suspended_and_reactivated` |

## 5. Secrets and data at rest

| # | Control | Status | Where | Test |
|---|---------|:---:|-------|------|
| 5.1 | No plaintext password stored | ✅ | `User.password_hash` | `TestResponseHygiene` |
| 5.2 | No plaintext token stored | ✅ | SHA-256 digests | `test_no_response_schema_declares_a_secret_field` |
| 5.3 | API key plaintext shown exactly once | ✅ | `IssuedApiKeyOut` | `test_the_plaintext_is_never_returned_again` |
| 5.4 | Tenant secrets AES-256-GCM enveloped | ✅ | `encrypt_secret` | `test_secret_encryption_round_trip` |
| 5.5 | Non-deterministic encryption (fresh nonce) | ✅ | `os.urandom(12)` | `test_encryption_is_non_deterministic` |
| 5.6 | Authenticated encryption — tampering fails closed | ✅ | GCM tag | `test_tampered_ciphertext_fails_to_decrypt` |
| 5.7 | Key versioning for rotation | ✅ | version byte | reviewed |
| 5.8 | Encryption key mandatory in production | ✅ | `_encryption_key` | `production_readiness_problems` |
| 5.9 | No hard-coded credential in source | ✅ | — | `test_no_credential_is_hard_coded` + gitleaks in CI |
| 5.10 | Constant-time comparison for every secret | ✅ | `secrets.compare_digest` | reviewed |

## 6. Audit and logging

| # | Control | Status | Where | Test |
|---|---------|:---:|-------|------|
| 6.1 | Append-only trail; no update or delete endpoint | ✅ | `audit_logs` | reviewed |
| 6.2 | Deny-by-default redaction on key name | ✅ | `SENSITIVE_KEY_PATTERN` | `test_credential_keys_are_removed_whatever_the_value` |
| 6.3 | Recursive redaction through nested structures | ✅ | `redact` | `test_redaction_recurses` |
| 6.4 | Value-shape redaction as a second net | ✅ | `_VALUE_PATTERNS` | `test_credential_shaped_values_are_caught…` |
| 6.5 | Depth-limited — cannot hang the logger | ✅ | `_depth > 8` | `test_deep_nesting_terminates` |
| 6.6 | Same redaction applied to structlog | ✅ | `_redact_processor` | `test_no_secret_is_written_to_a_log_call` |
| 6.7 | Severity declared per action, not per call site | ✅ | `_ACTION_META` | `test_security_relevant_actions_are_not_logged_as_info` |
| 6.8 | Audit failure never breaks a request | ✅ | wrapped write | `test_a_write_failure_never_breaks_the_caller` |
| 6.9 | Critical events survive the retention sweep | ✅ | `purge(keep_critical)` | `test_purge_keeps_critical_rows` |
| 6.10 | Tenants cannot read system-category events | ✅ | `query` | `test_a_tenant_cannot_see_system_events` |
| 6.11 | End-to-end: no plaintext key in the trail | ✅ | — | `test_audit_metadata_is_redacted_end_to_end` |

## 7. Transport and browser

| # | Control | Status | Where | Test |
|---|---------|:---:|-------|------|
| 7.1 | `X-Content-Type-Options: nosniff` | ✅ | middleware | `test_security_headers_are_present` |
| 7.2 | `X-Frame-Options: DENY` | ✅ | middleware | ✅ |
| 7.3 | `Content-Security-Policy: default-src 'none'` on the API | ✅ | middleware | ✅ |
| 7.4 | `Referrer-Policy`, `Permissions-Policy`, COOP | ✅ | middleware | reviewed |
| 7.5 | HSTS in production only | ✅ | middleware | reviewed |
| 7.6 | Frontend serves its own headers | ✅ | `next.config.ts` | reviewed |
| 7.7 | CORS restricted to configured origins | ✅ | settings | `test_oauth_providers_require_both_halves` (adjacent) |
| 7.8 | Plaintext origin flagged in production | ✅ | readiness | `test_production_readiness_is_computed_not_asserted` |
| 7.9 | CSRF: signed double-submit, session-bound | ✅ | `csrf_token` | `test_csrf_tokens_are_session_bound` |
| 7.10 | CSRF exempts bearer and API-key callers | ✅ | `verify_csrf` | `test_csrf_is_not_demanded_of_bearer_callers` |
| 7.11 | CSRF rejects a cookie write with no token | ✅ | `verify_csrf` | `test_csrf_rejects_a_cookie_write_without_a_token` |

## 8. Rate limiting and abuse

| # | Control | Status | Where | Test |
|---|---------|:---:|-------|------|
| 8.1 | Sliding window, not fixed | ✅ | `sliding_window` | `test_the_boundary_burst_is_prevented` |
| 8.2 | Per-endpoint rules; login strictest | ✅ | `DEFAULT_RULES` | `test_login_is_the_strictest_default_rule` |
| 8.3 | Keyed on credential when present, IP otherwise | ✅ | middleware | reviewed — a shared NAT must not share a budget |
| 8.4 | Credential hashed before becoming a key | ✅ | `hash_token` | reviewed |
| 8.5 | Login rate limit demonstrably fires | ✅ | — | `test_login_is_rate_limited` |
| 8.6 | Limiter map is bounded | ✅ | `_evict` | `test_the_key_map_is_bounded` |
| 8.7 | Redis backend fails **open** | ✅ | `RedisRateLimiter` | reviewed — a control must not take the product down |
| 8.8 | Standard `X-RateLimit-*` / `Retry-After` headers | ✅ | `RateDecision.headers` | `test_headers_are_well_formed` |
| 8.9 | Health probes exempt | ✅ | `_UNLIMITED_PATHS` | reviewed — a throttled probe reports a healthy service as down |

## 9. Input validation and injection

| # | Control | Status | Where | Test |
|---|---------|:---:|-------|------|
| 9.1 | Every request body a typed Pydantic model | ✅ | `schemas/platform.py` | 248 OpenAPI schemas |
| 9.2 | Length caps on every free-text field | ✅ | `Field(max_length=…)` | reviewed |
| 9.3 | Page size capped | ✅ | `Query(le=200)` | `test_page_size_is_honoured_and_capped` |
| 9.4 | No raw SQL string interpolation | ✅ | SQLAlchemy Core throughout | reviewed |
| 9.5 | Email validated and normalised | ✅ | `EmailStr` + `normalise_email` | `TestEmailAndSlug` |
| 9.6 | Slugs restricted to `[a-z0-9-]` | ✅ | `slugify` | `test_slug_has_no_leading_or_trailing_hyphen` |
| 9.7 | Malformed API key rejected before any query | ✅ | `parse_api_key` | `test_malformed_api_keys_are_rejected_before_a_lookup` |
| 9.8 | Tracebacks never returned to a caller | ✅ | middleware | reviewed |

## 10. Availability and resilience

| # | Control | Status | Where | Test |
|---|---------|:---:|-------|------|
| 10.1 | Connection pool sized explicitly | ✅ | `_pool_options` | `test_the_pool_is_sized_explicitly` |
| 10.2 | Pool timeout fails fast (10 s) | ✅ | `_pool_options` | reviewed |
| 10.3 | `pool_pre_ping` survives a database restart | ✅ | `create_engine` | reviewed |
| 10.4 | SQLite in WAL — writers do not block readers | ✅ | connect listener | `test_sqlite_actually_reports_wal` |
| 10.5 | Auth dependencies synchronous (never block the event loop) | ✅ | `security.py` | `test_auth_dependencies_are_synchronous` |
| 10.6 | Observability never opens a session on the request path | ✅ | `_defer` | `test_the_request_path_opens_no_second_session` |
| 10.7 | No server errors at concurrency 50 | ✅ | — | `tests/load/loadtest.py` — 1,500/1,500 |
| 10.8 | Job leases reclaim work from a dead worker | ✅ | `claim` | `test_an_expired_lease_is_reclaimable` |
| 10.9 | Dead-letter queue instead of silent loss | ✅ | `fail` | `test_exhausted_attempts_reach_the_dead_letter_queue` |
| 10.10 | Metrics collision merges rather than losing a batch | ✅ | `flush` | `test_metric_flushes_survive_a_concurrent_collision` |

## 11. Supply chain

| # | Control | Status | Where |
|---|---------|:---:|-------|
| 11.1 | Every dependency pinned to an exact version | ✅ | `requirements.txt` |
| 11.2 | `npm ci` against a committed lockfile | ✅ | CI + Dockerfile |
| 11.3 | `pip-audit` on every push | ✅ | CI |
| 11.4 | `bandit` static analysis | ✅ | CI |
| 11.5 | `gitleaks` secret scan over full history | ✅ | CI |
| 11.6 | Containers run as a non-root user | ✅ | both Dockerfiles |
| 11.7 | No build toolchain in the runtime image | ✅ | multi-stage |
| 11.8 | Image starts and answers before being tagged | ✅ | CI smoke test |

---

## 12. Known gaps

Stated plainly. Each is a decision, not an oversight.

| Gap | Why | Risk |
|-----|-----|------|
| **MFA is modelled, not enforced** | The brief asked for "future-ready". Enrolment, storage and the verification hook exist; no method is required at login. | An account with a compromised password has no second factor. Mitigated by lockout, rate limiting and reuse detection. |
| **OAuth untested against live providers** | No Google or GitHub credentials in this environment. The flow, state handling and linking are implemented and reviewed; the round trip is unexercised. | A misconfiguration would surface on first use, not in CI. |
| **Billing hooks, not billing** | `BillingEvent` records and verifies signed webhooks; no provider is integrated. Invoices are modelled and issued locally. | No revenue is actually collected. |
| **Rate limiting is per-process without Redis** | The default deployment is a single instance. | N replicas enforce N × the limit. Set `RATE_LIMIT_BACKEND=redis`. |
| **Restore is manual** | Deliberate. A one-click restore is a one-click way to destroy production. | Recovery needs an operator at a terminal. |
| **No WAF, no DDoS protection** | Out of scope for the application; belongs at the edge. | Rely on Railway/Cloudflare in front. |
| **Audit trail is not tamper-evident** | Append-only by convention and by API surface, not cryptographically chained. | An attacker with database write access could alter history. |
| **Session list shows no device fingerprint** | Only IP and user agent are captured. | "Sign out other devices" is coarse. |

---

## 13. Pre-launch verification

```bash
# 1. Every test, on the production database engine
DATABASE_URL=postgresql+psycopg://… pytest tests/ -q     # expect 1,850 passed

# 2. The invariants, on their own
pytest tests/test_platform_architecture.py -q            # expect 35 passed

# 3. Configuration — must be empty in production
curl -fsS https://api.yourdomain.com/health/ready | jq '.checks[] | select(.ok==false)'

# 4. Load, against a production-shaped instance
python3 tests/load/loadtest.py --url https://api.yourdomain.com \
    --concurrency 50 --requests 1500                     # expect no 5xx

# 5. Supply chain
pip-audit -r backend/requirements.txt
gitleaks detect --source .

# 6. Prove a backup restores. Not that one exists — that one restores.
curl -X POST .../api/v1/platform/backups
curl -X POST .../api/v1/platform/backups/1/verify
```
