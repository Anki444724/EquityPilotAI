"""Module 10 — API and end-to-end behaviour.

Two suites here.

`TestPublicSurface` and friends run against the **shared** application client
from `conftest.py`, in the deployment shape Modules 1-9 have always been
tested in: no identity system configured, so the caller is the labelled
development identity. That proves Module 10 did not break the existing
product.

`TestTenantIsolation` and the security suites build their **own** application
with native auth switched on and two real organisations, because the isolation
guarantee cannot be demonstrated by a single super-admin caller. These are the
tests that would catch a leak between customers, which is the single most
serious failure mode a multi-tenant platform has.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.db.base import Base, get_db
from app.domain.platform.identity import Role
from app.domain.platform.plans import PlanTier
from app.main import app
from app.models.platform import Tenant, User
from app.services.platform.email import outbox
from app.services.platform.entitlements import EntitlementService
from app.services.platform.identity_service import IdentityService
from app.services.platform.tenancy import TenantService

client = TestClient(app)

PASSWORD = "CorrectHorseBattery1"


# ===========================================================================
# A second application, with native auth on and two real tenants.
# ===========================================================================
@pytest.fixture(scope="module")
def secured():
    """A client authenticating with real JWTs against two organisations.

    Its own database and its own dependency override, restored afterwards so
    the shared client used by every other suite is unaffected.
    """
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    with Session() as db:
        EntitlementService(db).sync_catalogue()
        tenants = TenantService(db)
        identity = IdentityService(db)

        alpha = tenants.create("Alpha Capital", tier=PlanTier.PROFESSIONAL)
        beta = tenants.create("Beta Research", tier=PlanTier.FREE)

        people = {}
        for tenant, email, role in [
            (alpha, "admin@alpha.com", Role.ADMIN),
            (alpha, "analyst@alpha.com", Role.ANALYST),
            (alpha, "reader@alpha.com", Role.READ_ONLY),
            (beta, "admin@beta.com", Role.ADMIN),
        ]:
            user, _ = identity.register(
                email=email, password=PASSWORD, name=email.split("@")[0],
                tenant_id=tenant.id, role=role, auto_verify=True,
            )
            people[email] = user

        # A platform operator, belonging to Alpha but able to cross out of it.
        operator, _ = identity.register(
            email="operator@ierp.io", password=PASSWORD, name="Operator",
            tenant_id=alpha.id, role=Role.SUPER_ADMIN, auto_verify=True,
        )
        people["operator@ierp.io"] = operator

        alpha_id, beta_id = alpha.id, beta.id

    def _override():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    previous_override = app.dependency_overrides.get(get_db)
    previous_native = settings.NATIVE_AUTH
    app.dependency_overrides[get_db] = _override
    settings.NATIVE_AUTH = True

    test_client = TestClient(app)

    def _login(email: str) -> dict[str, str]:
        response = test_client.post(
            "/api/v1/auth/login", json={"email": email, "password": PASSWORD},
        )
        assert response.status_code == 200, response.text
        return {"Authorization": f"Bearer {response.json()['access_token']}"}

    yield {
        "client": test_client,
        "login": _login,
        "alpha_id": alpha_id,
        "beta_id": beta_id,
        "session": Session,
    }

    settings.NATIVE_AUTH = previous_native
    if previous_override is not None:
        app.dependency_overrides[get_db] = previous_override
    else:
        app.dependency_overrides.pop(get_db, None)
    engine.dispose()


# ===========================================================================
class TestPublicSurface:
    """Module 10 must not have changed how Modules 1-9 behave."""

    def test_health_is_unchanged(self):
        body = client.get("/health").json()
        assert body["status"] == "ok"
        assert "version" in body

    def test_liveness_touches_no_dependency(self):
        body = client.get("/health/live").json()
        assert body["status"] == "ok"
        assert body["uptime_seconds"] >= 0

    def test_readiness_reports_each_check(self):
        body = client.get("/health/ready").json()
        assert "checks" in body
        assert {c["name"] for c in body["checks"]} >= {"database", "schema"}

    def test_metrics_expose_no_customer_data(self):
        """Counts and latencies only. No tenant names, no identifiers."""
        body = client.get("/metrics").json()
        assert "requests" in body and "queue" in body
        serialised = str(body).lower()
        for leak in ("email", "@", "tenant_name", "password"):
            assert leak not in serialised

    def test_auth_config_advertises_only_configured_providers(self):
        body = client.get("/api/v1/auth/config").json()
        assert body["oauth_providers"] == settings.oauth_providers

    def test_the_password_policy_is_published(self):
        body = client.get("/api/v1/auth/password-policy").json()
        assert body["min_length"] >= 10

    def test_the_plan_catalogue_is_public(self):
        """A pricing page behind a login is not a pricing page."""
        plans = client.get("/api/v1/platform/plans").json()
        assert {p["tier"] for p in plans} == {
            "free", "basic", "professional", "enterprise",
        }

    def test_security_headers_are_present(self):
        headers = client.get("/health").headers
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["X-Frame-Options"] == "DENY"
        assert "Content-Security-Policy" in headers

    def test_every_response_carries_a_request_id(self):
        assert client.get("/health").headers.get("X-Request-ID")

    def test_an_inbound_request_id_is_honoured(self):
        response = client.get("/health", headers={"X-Request-ID": "trace-me-123"})
        assert response.headers["X-Request-ID"] == "trace-me-123"

    def test_the_dev_identity_still_resolves(self):
        body = client.get("/api/v1/auth/me").json()
        assert body["is_dev_identity"] is True
        assert body["role"] == "super_admin"
        assert body["permissions"]

    def test_the_rbac_matrix_is_served_not_duplicated(self):
        """The admin panel renders this. A matrix hard-coded in TypeScript
        would be a second source of truth."""
        body = client.get("/api/v1/admin/rbac").json()
        assert len(body["roles"]) == 7
        assert set(body["matrix"]) == {r["key"] for r in body["roles"]}

    def test_modules_one_to_nine_still_answer(self):
        for path in (
            "/api/v1/companies?page_size=5",
            "/api/v1/dashboard/overview",
            "/api/v1/reports/capabilities",
            "/api/v1/portfolios/capabilities",
            "/api/v1/documents/capabilities",
        ):
            assert client.get(path).status_code == 200, path


class TestOpenApi:
    def test_the_schema_is_served_and_complete(self):
        schema = client.get("/openapi.json").json()
        assert len(schema["paths"]) >= 140

    def test_module_ten_paths_are_documented(self):
        paths = client.get("/openapi.json").json()["paths"]
        for path in (
            "/api/v1/auth/login", "/api/v1/auth/refresh",
            "/api/v1/admin/members", "/api/v1/admin/entitlements",
            "/api/v1/platform/tenants", "/api/v1/platform/queue",
        ):
            assert path in paths, path

    def test_every_endpoint_has_a_summary(self):
        """An undocumented endpoint in a published API is a support ticket."""
        schema = client.get("/openapi.json").json()
        missing = [
            f"{method.upper()} {path}"
            for path, operations in schema["paths"].items()
            for method, operation in operations.items()
            if method in ("get", "post", "patch", "put", "delete")
            and not operation.get("summary")
        ]
        assert missing == [], f"missing summaries: {missing[:10]}"

    def test_literal_paths_precede_parameterised_ones(self):
        """The trap Modules 7, 8 and 9 each hit: FastAPI matches in
        declaration order, so `/platform/plans/{tier}` declared before
        `/platform/backups/status` would swallow it."""
        for literal in (
            "/api/v1/platform/backups/status",
            "/api/v1/admin/audit/summary",
            "/api/v1/admin/usage/series",
            "/api/v1/platform/metrics/routes",
        ):
            code = client.get(literal).status_code
            # 400 is a legitimate answer here — `require_tenant` refuses when
            # the caller has no organisation, which the development identity
            # does not until the platform seed has run. What must never happen
            # is 422: that is the signature of the literal being swallowed by
            # a `/{id}` route and failing to coerce the segment to an integer,
            # which is the trap Modules 7, 8 and 9 each fell into.
            assert code != 422, f"{literal} was routed to a parameterised handler"
            assert code in (200, 400, 401, 403, 404), f"{literal}: {code}"


# ===========================================================================
class TestAuthenticationFlow:
    def test_register_verify_login_refresh_logout(self, secured):
        """The whole life of a session, end to end."""
        http = secured["client"]
        outbox.clear()

        registered = http.post("/api/v1/auth/register", json={
            "email": "brand.new@example.com", "password": PASSWORD,
            "name": "Brand New", "organisation": "Brand New Ltd",
        })
        assert registered.status_code == 201

        # The console transport captured the verification link.
        message = outbox.latest_for("brand.new@example.com")
        assert message is not None
        token = message.body.split("token=")[1].split()[0]

        # Unverified, so sign-in is refused.
        refused = http.post("/api/v1/auth/login", json={
            "email": "brand.new@example.com", "password": PASSWORD,
        })
        assert refused.status_code == 401

        assert http.post("/api/v1/auth/verify-email", json={"token": token}).status_code == 200

        signed_in = http.post("/api/v1/auth/login", json={
            "email": "brand.new@example.com", "password": PASSWORD,
        })
        assert signed_in.status_code == 200
        body = signed_in.json()
        assert body["access_token"] and body["csrf_token"]
        # The refresh token is an httpOnly cookie, not a body field.
        assert body["refresh_token"] is None
        assert "ierp_refresh" in signed_in.cookies

        refreshed = http.post("/api/v1/auth/refresh")
        assert refreshed.status_code == 200
        assert refreshed.json()["access_token"] != body["access_token"]

        assert http.post("/api/v1/auth/logout").status_code == 200

    def test_registering_an_existing_address_is_indistinguishable(self, secured):
        """Otherwise registration is a free membership oracle."""
        http = secured["client"]
        first = http.post("/api/v1/auth/register", json={
            "email": "dup@example.com", "password": PASSWORD, "name": "Dup",
        })
        second = http.post("/api/v1/auth/register", json={
            "email": "dup@example.com", "password": PASSWORD, "name": "Dup",
        })
        assert first.status_code == second.status_code == 201
        assert first.json()["message"] == second.json()["message"]

    def test_a_weak_password_is_refused_with_reasons(self, secured):
        response = secured["client"].post("/api/v1/auth/register", json={
            "email": "weak@example.com", "password": "password123", "name": "W",
        })
        assert response.status_code == 422

    def test_password_reset_for_an_unknown_address_looks_the_same(self, secured):
        http = secured["client"]
        known = http.post("/api/v1/auth/password-reset", json={"email": "admin@alpha.com"})
        unknown = http.post("/api/v1/auth/password-reset", json={"email": "ghost@nowhere.com"})
        assert known.status_code == unknown.status_code == 200
        assert known.json()["message"] == unknown.json()["message"]

    def test_a_magic_link_signs_a_user_in(self, secured):
        http = secured["client"]
        outbox.clear()
        http.post("/api/v1/auth/magic-link", json={"email": "reader@alpha.com"})

        message = outbox.latest_for("reader@alpha.com")
        token = message.body.split("token=")[1].split()[0]

        response = http.post("/api/v1/auth/magic-link/consume", json={"token": token})
        assert response.status_code == 200
        assert response.json()["access_token"]

    def test_a_magic_link_is_single_use(self, secured):
        http = secured["client"]
        outbox.clear()
        http.post("/api/v1/auth/magic-link", json={"email": "reader@alpha.com"})
        token = outbox.latest_for("reader@alpha.com").body.split("token=")[1].split()[0]

        assert http.post("/api/v1/auth/magic-link/consume", json={"token": token}).status_code == 200
        assert http.post("/api/v1/auth/magic-link/consume", json={"token": token}).status_code == 400

    def test_a_protected_route_refuses_an_anonymous_caller(self, secured):
        response = secured["client"].get("/api/v1/admin/members")
        assert response.status_code == 401
        assert response.headers.get("WWW-Authenticate") == "Bearer"

    def test_a_garbage_token_is_refused(self, secured):
        response = secured["client"].get(
            "/api/v1/admin/members",
            headers={"Authorization": "Bearer not-a-real-token"},
        )
        assert response.status_code == 401

    def test_login_is_rate_limited(self, secured):
        """The endpoint an attacker enumerates gets the strictest rule."""
        http = secured["client"]
        codes = [
            http.post("/api/v1/auth/login", json={
                "email": "nobody@alpha.com", "password": "wrong-password",
            }).status_code
            for _ in range(14)
        ]
        assert 429 in codes, "credential stuffing was not throttled"


# ===========================================================================
class TestRbacEnforcement:
    def test_a_reader_cannot_list_members(self, secured):
        response = secured["client"].get(
            "/api/v1/admin/members", headers=secured["login"]("reader@alpha.com"),
        )
        assert response.status_code == 403

    def test_an_analyst_cannot_administer(self, secured):
        headers = secured["login"]("analyst@alpha.com")
        for path in ("/api/v1/admin/members", "/api/v1/admin/api-keys",
                     "/api/v1/admin/audit", "/api/v1/admin/subscription"):
            assert secured["client"].get(path, headers=headers).status_code == 403, path

    def test_an_admin_can_administer_their_own_organisation(self, secured):
        headers = secured["login"]("admin@alpha.com")
        for path in ("/api/v1/admin/members", "/api/v1/admin/api-keys",
                     "/api/v1/admin/audit", "/api/v1/admin/entitlements",
                     "/api/v1/admin/overview", "/api/v1/admin/usage"):
            assert secured["client"].get(path, headers=headers).status_code == 200, path

    def test_an_admin_cannot_reach_the_operator_console(self, secured):
        """404, not 403 — the console should not confirm its own existence."""
        headers = secured["login"]("admin@alpha.com")
        for path in ("/api/v1/platform/tenants", "/api/v1/platform/overview",
                     "/api/v1/platform/queue", "/api/v1/platform/errors"):
            assert secured["client"].get(path, headers=headers).status_code == 404, path

    def test_the_operator_can(self, secured):
        headers = secured["login"]("operator@ierp.io")
        for path in ("/api/v1/platform/tenants", "/api/v1/platform/overview",
                     "/api/v1/platform/queue", "/api/v1/platform/errors",
                     "/api/v1/platform/metrics", "/api/v1/platform/schedules",
                     "/api/v1/platform/readiness"):
            assert secured["client"].get(path, headers=headers).status_code == 200, path

    def test_a_denial_is_written_to_the_audit_trail(self, secured):
        """An access-control failure is a security event; the trail is the
        only place anyone will see a pattern of them."""
        http = secured["client"]
        http.get("/api/v1/admin/members", headers=secured["login"]("reader@alpha.com"))

        rows = http.get(
            "/api/v1/admin/audit", params={"action": "security.access.denied"},
            headers=secured["login"]("admin@alpha.com"),
        ).json()
        assert rows["total"] >= 1


# ===========================================================================
class TestTenantIsolation:
    """The guarantee the whole multi-tenancy claim rests on."""

    def test_members_lists_are_disjoint(self, secured):
        alpha = secured["client"].get(
            "/api/v1/admin/members", headers=secured["login"]("admin@alpha.com"),
        ).json()
        beta = secured["client"].get(
            "/api/v1/admin/members", headers=secured["login"]("admin@beta.com"),
        ).json()

        alpha_emails = {u["email"] for u in alpha["items"]}
        beta_emails = {u["email"] for u in beta["items"]}
        assert alpha_emails & beta_emails == set()
        assert "admin@beta.com" not in alpha_emails
        assert "admin@alpha.com" not in beta_emails

    def test_a_foreign_member_is_a_404_never_a_403(self, secured):
        """A distinguishable response would let an admin enumerate the
        platform's user ids."""
        http = secured["client"]
        beta_members = http.get(
            "/api/v1/admin/members", headers=secured["login"]("admin@beta.com"),
        ).json()["items"]
        beta_user_id = beta_members[0]["id"]

        response = http.get(
            f"/api/v1/admin/members/{beta_user_id}",
            headers=secured["login"]("admin@alpha.com"),
        )
        assert response.status_code == 404

    def test_an_admin_cannot_modify_a_foreign_member(self, secured):
        http = secured["client"]
        beta_user_id = http.get(
            "/api/v1/admin/members", headers=secured["login"]("admin@beta.com"),
        ).json()["items"][0]["id"]

        headers = secured["login"]("admin@alpha.com")
        for method, path, payload in [
            ("patch", f"/api/v1/admin/members/{beta_user_id}/role", {"role": "read_only"}),
            ("patch", f"/api/v1/admin/members/{beta_user_id}/status", {"status": "suspended"}),
            ("delete", f"/api/v1/admin/members/{beta_user_id}", None),
        ]:
            # `TestClient.delete` takes no `json` argument, so the call is
            # built rather than passed through a uniform kwargs dict.
            if payload is None:
                response = getattr(http, method)(path, headers=headers)
            else:
                response = getattr(http, method)(path, json=payload, headers=headers)
            assert response.status_code == 404, path

    def test_organisation_records_do_not_leak(self, secured):
        alpha = secured["client"].get(
            "/api/v1/admin/organisation", headers=secured["login"]("admin@alpha.com"),
        ).json()
        beta = secured["client"].get(
            "/api/v1/admin/organisation", headers=secured["login"]("admin@beta.com"),
        ).json()
        assert alpha["id"] != beta["id"]
        assert alpha["name"] == "Alpha Capital"
        assert beta["name"] == "Beta Research"

    def test_audit_trails_are_disjoint(self, secured):
        http = secured["client"]
        alpha_rows = http.get(
            "/api/v1/admin/audit", headers=secured["login"]("admin@alpha.com"),
        ).json()["items"]
        beta_id = secured["beta_id"]
        assert all(r["tenant_id"] != beta_id for r in alpha_rows)

    def test_api_keys_are_disjoint(self, secured):
        http = secured["client"]
        created = http.post(
            "/api/v1/admin/api-keys",
            json={"name": "Alpha key", "role": "read_only", "expires_in_days": 30},
            headers=secured["login"]("admin@alpha.com"),
        )
        assert created.status_code == 201

        beta_keys = http.get(
            "/api/v1/admin/api-keys", headers=secured["login"]("admin@beta.com"),
        ).json()
        assert all(k["name"] != "Alpha key" for k in beta_keys)

    def test_usage_and_entitlements_are_per_tenant(self, secured):
        http = secured["client"]
        alpha = http.get(
            "/api/v1/admin/entitlements", headers=secured["login"]("admin@alpha.com"),
        ).json()
        beta = http.get(
            "/api/v1/admin/entitlements", headers=secured["login"]("admin@beta.com"),
        ).json()
        assert alpha["plan_tier"] == "professional"
        assert beta["plan_tier"] == "free"
        assert alpha["tenant_id"] != beta["tenant_id"]

    def test_the_operator_deliberately_sees_both(self, secured):
        tenants = secured["client"].get(
            "/api/v1/platform/tenants", headers=secured["login"]("operator@ierp.io"),
        ).json()
        names = {t["name"] for t in tenants["items"]}
        assert {"Alpha Capital", "Beta Research"} <= names


# ===========================================================================
class TestEntitlementEnforcement:
    def test_a_feature_outside_the_plan_returns_402_not_403(self, secured):
        """402 Payment Required is the honest code: the caller is
        authenticated and authorised, and the obstacle is commercial. The
        frontend shows an upgrade prompt for one and an access error for the
        other."""
        response = secured["client"].post(
            "/api/v1/admin/api-keys",
            json={"name": "k", "role": "read_only", "expires_in_days": 30},
            headers=secured["login"]("admin@beta.com"),   # Free plan: no API access
        )
        assert response.status_code == 402
        detail = response.json()["detail"]
        assert detail["reason"] == "feature_not_in_plan"
        assert detail["upgrade_to"] == "professional"

    def test_the_seat_limit_blocks_an_invitation(self, secured):
        """Beta is on Free, which sells one seat and already has it."""
        response = secured["client"].post(
            "/api/v1/admin/members",
            json={"email": "second@beta.com", "name": "Second", "role": "read_only"},
            headers=secured["login"]("admin@beta.com"),
        )
        assert response.status_code == 402
        assert response.json()["detail"]["reason"] == "limit_reached"

    def test_entitlements_list_included_and_excluded_features(self, secured):
        body = secured["client"].get(
            "/api/v1/admin/entitlements", headers=secured["login"]("admin@beta.com"),
        ).json()
        included = {f["key"] for f in body["all_features"] if f["included"]}
        excluded = {f["key"] for f in body["all_features"] if not f["included"]}
        assert "historical_financials" in included
        assert "ai_analyst" in excluded

    def test_quota_progress_is_reported(self, secured):
        body = secured["client"].get(
            "/api/v1/admin/entitlements", headers=secured["login"]("admin@alpha.com"),
        ).json()
        assert body["quotas"]
        for quota in body["quotas"]:
            assert 0.0 <= quota["utilisation"] <= 1.0 or quota["unlimited"]


# ===========================================================================
class TestApiKeyAuthentication:
    def test_a_key_authenticates_and_carries_its_own_role(self, secured):
        http = secured["client"]
        issued = http.post(
            "/api/v1/admin/api-keys",
            json={"name": "Dashboard", "role": "read_only", "expires_in_days": 30},
            headers=secured["login"]("admin@alpha.com"),
        ).json()

        plaintext = issued["plaintext"]
        assert plaintext.startswith("ierp_live_")

        me = http.get("/api/v1/auth/me", headers={"X-API-Key": plaintext}).json()
        assert me["role"] == "read_only"
        assert me["tenant_slug"] == "alpha-capital"

    def test_a_key_is_bounded_by_its_role_not_its_creator(self, secured):
        """An admin's read-only key must not be able to administer."""
        http = secured["client"]
        plaintext = http.post(
            "/api/v1/admin/api-keys",
            json={"name": "Limited", "role": "read_only", "expires_in_days": 30},
            headers=secured["login"]("admin@alpha.com"),
        ).json()["plaintext"]

        assert http.get(
            "/api/v1/admin/members", headers={"X-API-Key": plaintext},
        ).status_code == 403

    def test_a_key_works_as_a_bearer_token_too(self, secured):
        http = secured["client"]
        plaintext = http.post(
            "/api/v1/admin/api-keys",
            json={"name": "Bearer style", "role": "read_only", "expires_in_days": 30},
            headers=secured["login"]("admin@alpha.com"),
        ).json()["plaintext"]

        assert http.get(
            "/api/v1/auth/me", headers={"Authorization": f"Bearer {plaintext}"},
        ).status_code == 200

    def test_the_plaintext_is_never_returned_again(self, secured):
        http = secured["client"]
        http.post(
            "/api/v1/admin/api-keys",
            json={"name": "Once only", "role": "read_only", "expires_in_days": 30},
            headers=secured["login"]("admin@alpha.com"),
        )
        listing = http.get(
            "/api/v1/admin/api-keys", headers=secured["login"]("admin@alpha.com"),
        ).json()
        for key in listing:
            assert "plaintext" not in key
            assert not str(key.get("masked", "")).endswith("_")

    def test_a_revoked_key_stops_working(self, secured):
        http = secured["client"]
        headers = secured["login"]("admin@alpha.com")
        issued = http.post(
            "/api/v1/admin/api-keys",
            json={"name": "Doomed", "role": "read_only", "expires_in_days": 30},
            headers=headers,
        ).json()

        http.delete(f"/api/v1/admin/api-keys/{issued['key']['id']}", headers=headers)
        assert http.get(
            "/api/v1/auth/me", headers={"X-API-Key": issued["plaintext"]},
        ).status_code == 401

    def test_an_invalid_key_is_refused(self, secured):
        assert secured["client"].get(
            "/api/v1/auth/me", headers={"X-API-Key": "ierp_live_deadbeefdeadbeef_x"},
        ).status_code == 401


# ===========================================================================
class TestMemberAdministrationApi:
    def test_an_admin_cannot_demote_the_last_administrator(self, secured):
        """A tenant nobody can administer needs an operator to repair."""
        http = secured["client"]
        headers = secured["login"]("admin@beta.com")
        members = http.get("/api/v1/admin/members", headers=headers).json()["items"]
        admin_id = next(m["id"] for m in members if m["role"] == "admin")

        response = http.patch(
            f"/api/v1/admin/members/{admin_id}/role",
            json={"role": "read_only"}, headers=headers,
        )
        # Refused either as self-administration or as the last-admin rule.
        assert response.status_code in (403, 409)

    def test_an_admin_cannot_change_their_own_role(self, secured):
        http = secured["client"]
        headers = secured["login"]("admin@alpha.com")
        me = http.get("/api/v1/auth/me", headers=headers).json()
        response = http.patch(
            f"/api/v1/admin/members/{me['id']}/role",
            json={"role": "read_only"}, headers=headers,
        )
        assert response.status_code == 403

    def test_suspending_a_member_ends_their_sessions(self, secured):
        http = secured["client"]
        admin_headers = secured["login"]("admin@alpha.com")

        # Give the reader a live session first.
        reader_headers = secured["login"]("reader@alpha.com")
        assert http.get("/api/v1/auth/me", headers=reader_headers).status_code == 200

        members = http.get("/api/v1/admin/members", headers=admin_headers).json()["items"]
        reader_id = next(m["id"] for m in members if m["email"] == "reader@alpha.com")

        http.patch(
            f"/api/v1/admin/members/{reader_id}/status",
            json={"status": "suspended"}, headers=admin_headers,
        )
        assert http.get("/api/v1/auth/me", headers=reader_headers).status_code == 401

        # Restore, so the fixture stays usable for other tests in the module.
        http.patch(
            f"/api/v1/admin/members/{reader_id}/status",
            json={"status": "active"}, headers=admin_headers,
        )

    def test_super_admin_cannot_be_granted_by_invitation(self, secured):
        response = secured["client"].post(
            "/api/v1/admin/members",
            json={"email": "sneaky@alpha.com", "name": "S", "role": "super_admin"},
            headers=secured["login"]("admin@alpha.com"),
        )
        assert response.status_code == 422


# ===========================================================================
class TestResponseHygiene:
    """No response may carry a secret. Asserted structurally rather than by
    reading each schema, so a field added later is caught too."""

    FORBIDDEN = (
        "password_hash", "token_hash", "key_hash", "mfa_secret",
        "ciphertext", "secret_key", "encryption_key",
    )

    def _assert_clean(self, payload, path: str):
        serialised = str(payload).lower()
        for field in self.FORBIDDEN:
            assert field not in serialised, f"{path} leaked '{field}'"

    def test_member_payloads_carry_no_hash(self, secured):
        http = secured["client"]
        headers = secured["login"]("admin@alpha.com")
        listing = http.get("/api/v1/admin/members", headers=headers).json()
        self._assert_clean(listing, "/admin/members")

        member_id = listing["items"][0]["id"]
        self._assert_clean(
            http.get(f"/api/v1/admin/members/{member_id}", headers=headers).json(),
            "/admin/members/{id}",
        )

    def test_the_session_payload_carries_no_hash(self, secured):
        self._assert_clean(
            secured["client"].get(
                "/api/v1/auth/me", headers=secured["login"]("admin@alpha.com"),
            ).json(),
            "/auth/me",
        )

    def test_operator_views_carry_no_hash(self, secured):
        http = secured["client"]
        headers = secured["login"]("operator@ierp.io")
        for path in ("/api/v1/platform/users", "/api/v1/platform/tenants",
                     "/api/v1/platform/overview"):
            self._assert_clean(http.get(path, headers=headers).json(), path)

    def test_audit_metadata_is_redacted_end_to_end(self, secured):
        """The one place a plaintext key could plausibly reach the trail is
        API key creation, so that is the path worth checking."""
        http = secured["client"]
        headers = secured["login"]("admin@alpha.com")
        http.post(
            "/api/v1/admin/api-keys",
            json={"name": "Audited", "role": "read_only", "expires_in_days": 30},
            headers=headers,
        )
        rows = http.get(
            "/api/v1/admin/audit", params={"action": "security.apikey.created"},
            headers=headers,
        ).json()
        assert rows["total"] >= 1
        assert "ierp_live_" not in str(rows)


# ===========================================================================
class TestPagination:
    def test_list_endpoints_return_an_envelope(self, secured):
        body = secured["client"].get(
            "/api/v1/admin/members", headers=secured["login"]("admin@alpha.com"),
        ).json()
        assert {"items", "total", "page", "page_size"} <= set(body)

    def test_page_size_is_honoured_and_capped(self, secured):
        http = secured["client"]
        headers = secured["login"]("admin@alpha.com")

        page = http.get(
            "/api/v1/admin/members", params={"page_size": 2}, headers=headers,
        ).json()
        assert len(page["items"]) <= 2
        assert page["total"] >= len(page["items"])

        # Beyond the cap is a validation error, not an unbounded query.
        assert http.get(
            "/api/v1/admin/members", params={"page_size": 100_000}, headers=headers,
        ).status_code == 422

    def test_pages_do_not_overlap(self, secured):
        http = secured["client"]
        headers = secured["login"]("admin@alpha.com")
        first = http.get(
            "/api/v1/admin/members", params={"page": 1, "page_size": 2}, headers=headers,
        ).json()["items"]
        second = http.get(
            "/api/v1/admin/members", params={"page": 2, "page_size": 2}, headers=headers,
        ).json()["items"]
        assert {u["id"] for u in first} & {u["id"] for u in second} == set()

    def test_filtering_and_sorting(self, secured):
        http = secured["client"]
        headers = secured["login"]("admin@alpha.com")

        filtered = http.get(
            "/api/v1/admin/members", params={"role": "analyst"}, headers=headers,
        ).json()
        assert all(u["role"] == "analyst" for u in filtered["items"])

        searched = http.get(
            "/api/v1/admin/members", params={"search": "analyst@"}, headers=headers,
        ).json()
        assert searched["total"] >= 1

        ascending = http.get(
            "/api/v1/admin/members",
            params={"sort": "email", "order": "asc"}, headers=headers,
        ).json()["items"]
        assert [u["email"] for u in ascending] == sorted(u["email"] for u in ascending)


# ===========================================================================
class TestOperatorConsole:
    def test_the_overview_reports_the_estate(self, secured):
        body = secured["client"].get(
            "/api/v1/platform/overview", headers=secured["login"]("operator@ierp.io"),
        ).json()
        assert body["tenants"] >= 2
        assert body["users"] >= 5
        assert "queue" in body and "tier_distribution" in body
        assert body["health"] in ("ok", "degraded", "unhealthy")

    def test_a_tenant_can_be_created_suspended_and_reactivated(self, secured):
        http = secured["client"]
        headers = secured["login"]("operator@ierp.io")

        created = http.post(
            "/api/v1/platform/tenants",
            json={"name": "Gamma Partners", "tier": "basic"}, headers=headers,
        )
        assert created.status_code == 201
        tenant_id = created.json()["id"]

        suspended = http.post(
            f"/api/v1/platform/tenants/{tenant_id}/suspend",
            json={"reason": "non-payment"}, headers=headers,
        )
        assert suspended.json()["status"] == "suspended"

        reactivated = http.post(
            f"/api/v1/platform/tenants/{tenant_id}/reactivate", headers=headers,
        )
        assert reactivated.json()["status"] == "active"

    def test_a_plan_can_be_repriced_without_a_deploy(self, secured):
        http = secured["client"]
        headers = secured["login"]("operator@ierp.io")
        updated = http.patch(
            "/api/v1/platform/plans/basic",
            json={"price_monthly_inr": 2999}, headers=headers,
        )
        assert updated.status_code == 200
        assert updated.json()["price_monthly_inr"] == 2999

    def test_contract_overrides_take_effect(self, secured):
        """The enterprise-deal path: a Free tenant granted a paid feature."""
        http = secured["client"]
        operator = secured["login"]("operator@ierp.io")
        beta_id = secured["beta_id"]

        http.patch(
            f"/api/v1/platform/tenants/{beta_id}/subscription",
            json={"feature_overrides": ["ai_analyst"]}, headers=operator,
        )
        body = http.get(
            "/api/v1/admin/entitlements", headers=secured["login"]("admin@beta.com"),
        ).json()
        assert "ai_analyst" in body["features"]

        # Undo, so later tests see the plan as sold.
        http.patch(
            f"/api/v1/platform/tenants/{beta_id}/subscription",
            json={"feature_overrides": []}, headers=operator,
        )

    def test_a_job_can_be_enqueued_and_inspected(self, secured):
        http = secured["client"]
        headers = secured["login"]("operator@ierp.io")

        created = http.post(
            "/api/v1/platform/jobs",
            json={"kind": "usage_rollup", "payload": {}}, headers=headers,
        )
        assert created.status_code == 201
        assert created.json()["status"] == "queued"

        depth = http.get("/api/v1/platform/queue", headers=headers).json()
        assert depth["queued"] >= 1

    def test_schedules_are_listed_with_descriptions(self, secured):
        rows = secured["client"].get(
            "/api/v1/platform/schedules", headers=secured["login"]("operator@ierp.io"),
        ).json()
        assert len(rows) >= 5
        assert all(r["description"] for r in rows)

    def test_backup_status_is_reported(self, secured):
        body = secured["client"].get(
            "/api/v1/platform/backups/status",
            headers=secured["login"]("operator@ierp.io"),
        ).json()
        assert "stale" in body and "retention_count" in body

    def test_readiness_detail_is_operator_only(self, secured):
        http = secured["client"]
        assert http.get(
            "/api/v1/platform/readiness", headers=secured["login"]("admin@alpha.com"),
        ).status_code == 404
        assert http.get(
            "/api/v1/platform/readiness", headers=secured["login"]("operator@ierp.io"),
        ).status_code == 200


# ===========================================================================
class TestSecurityDependencies:
    """The dependency layer in `core/security.py`, exercised directly.

    Coverage of that module sat at 55% after the API suite: the happy paths
    were driven through routes, but the guards — CSRF, optional auth, tenant
    resolution, the entitlement error mapping — were not. These are precisely
    the branches that only run when something is wrong, which is when they
    matter most.
    """

    def test_the_dev_principal_is_cached_but_resettable(self):
        from app.core.security import _dev_principal, reset_dev_principal
        from app.db.base import SessionLocal

        reset_dev_principal()
        db = SessionLocal()
        try:
            first = _dev_principal(db)
            second = _dev_principal(db)
            # Cached, because this runs on the event loop thread and a query
            # per request there blocks the whole process under load.
            assert first is second or first == second
        finally:
            db.close()
            reset_dev_principal()

    def test_the_dev_principal_survives_a_broken_database(self):
        """The shim must never be the reason a request fails."""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from app.core.security import DEV_USER, _dev_principal, reset_dev_principal

        reset_dev_principal()
        engine = create_engine("sqlite+pysqlite:///:memory:")
        db = sessionmaker(bind=engine)()
        try:
            # No tables at all — the lookup raises internally.
            assert _dev_principal(db).is_dev_identity
        finally:
            db.close()
            engine.dispose()
            reset_dev_principal()

    def test_client_ip_honours_exactly_one_proxy_hop(self):
        """Only the first entry of X-Forwarded-For is trusted. Reading the
        whole chain lets a caller forge their own address."""
        from app.core.security import _client_ip

        class _Request:
            def __init__(self, headers, host="10.0.0.9"):
                self.headers = headers
                self.client = type("C", (), {"host": host})()

        assert _client_ip(_Request({"x-forwarded-for": "203.0.113.7, 10.0.0.1"})) == "203.0.113.7"
        assert _client_ip(_Request({})) == "10.0.0.9"

    def test_entitlement_errors_map_to_meaningful_status_codes(self):
        """402 for a commercial obstacle, 403 for an access one. The frontend
        shows an upgrade prompt for the first and an error for the second."""
        from app.core.security import _entitlement_error
        from app.domain.platform.plans import (
            DenialReason, Entitlement, Feature, PlanTier,
        )

        payment = _entitlement_error(Entitlement(
            allowed=False, reason=DenialReason.FEATURE_NOT_IN_PLAN,
            message="no", feature=Feature.AI_ANALYST, upgrade_to=PlanTier.PROFESSIONAL,
        ))
        assert payment.status_code == 402
        assert payment.detail["upgrade_to"] == "professional"

        forbidden = _entitlement_error(Entitlement(
            allowed=False, reason=DenialReason.TENANT_SUSPENDED, message="no",
        ))
        assert forbidden.status_code == 403

    def test_csrf_is_not_demanded_of_bearer_callers(self):
        """CSRF exploits the browser's automatic attachment of cookies. A
        header a script must set cannot be attached by a cross-site form, so
        demanding a token there would break every API client for no gain."""
        from app.core.security import verify_csrf
        from app.domain.platform.identity import Principal, Role

        class _Request:
            def __init__(self, method, headers):
                self.method = method
                self.headers = headers

        principal = Principal(
            user_id="u", email="e", name="n", role=Role.ADMIN,
            tenant_id=1, session_id="s1",
        )
        # Bearer-authenticated write: exempt.
        verify_csrf(_Request("POST", {"authorization": "Bearer x"}), principal)
        # Safe method: exempt.
        verify_csrf(_Request("GET", {}), principal)

    def test_csrf_rejects_a_cookie_write_without_a_token(self):
        from fastapi import HTTPException

        from app.core.config import settings
        from app.core.security import verify_csrf
        from app.domain.platform.identity import Principal, Role

        class _Request:
            def __init__(self, method, headers):
                self.method = method
                self.headers = headers

        principal = Principal(
            user_id="u", email="e", name="n", role=Role.ADMIN,
            tenant_id=1, session_id="s1", is_dev_identity=False,
        )
        previous = settings.CSRF_ENABLED
        settings.CSRF_ENABLED = True
        try:
            with pytest.raises(HTTPException) as exc:
                verify_csrf(_Request("POST", {}), principal)
            assert exc.value.status_code == 403
        finally:
            settings.CSRF_ENABLED = previous

    def test_csrf_accepts_a_correctly_signed_token(self):
        from app.core.config import settings
        from app.core.security import verify_csrf
        from app.domain.platform.identity import Principal, Role
        from app.services.platform.crypto import csrf_token

        class _Request:
            def __init__(self, method, headers):
                self.method = method
                self.headers = headers

        principal = Principal(
            user_id="u", email="e", name="n", role=Role.ADMIN,
            tenant_id=1, session_id="s1", is_dev_identity=False,
        )
        previous = settings.CSRF_ENABLED
        settings.CSRF_ENABLED = True
        try:
            verify_csrf(
                _Request("POST", {"x-csrf-token": csrf_token("s1")}), principal,
            )
        finally:
            settings.CSRF_ENABLED = previous

    def test_require_tenant_refuses_a_principal_without_one(self):
        """Guessing a tenant here would write one customer's data into
        another's organisation."""
        from fastapi import HTTPException

        from app.core.security import require_tenant
        from app.domain.platform.identity import Principal, Role

        with pytest.raises(HTTPException) as exc:
            require_tenant(Principal(
                user_id="u", email="e", name="n", role=Role.SUPER_ADMIN,
                tenant_id=None,
            ))
        assert exc.value.status_code == 400

    def test_tenant_scope_is_built_from_the_principal(self):
        from app.core.security import get_tenant_scope
        from app.domain.platform.identity import Principal, Role

        operator = get_tenant_scope(Principal(
            user_id="u", email="e", name="n", role=Role.SUPER_ADMIN, tenant_id=1,
        ))
        assert operator.unrestricted

        member = get_tenant_scope(Principal(
            user_id="u", email="e", name="n", role=Role.ADMIN, tenant_id=1,
        ))
        assert not member.unrestricted
        assert member.tenant_id == 1

    def test_optional_auth_returns_none_rather_than_raising(self, secured):
        """For endpoints serving both signed-in and anonymous callers, where
        a 401 would be wrong."""
        from app.core.security import get_optional_user
        from app.db.base import SessionLocal

        class _Request:
            method = "GET"
            headers: dict = {}
            state = type("S", (), {})()
            client = type("C", (), {"host": "127.0.0.1"})()

        db = SessionLocal()
        try:
            # Native auth is on inside the `secured` fixture, and no
            # credential is presented, so this would 401 under the strict
            # dependency.
            assert get_optional_user(_Request(), db) is None
        finally:
            db.close()

    def test_an_api_key_in_the_wrong_header_is_still_honoured_consistently(self, secured):
        """A client must not be able to change which credential is honoured
        by moving it between headers."""
        http = secured["client"]
        plaintext = http.post(
            "/api/v1/admin/api-keys",
            json={"name": "Header test", "role": "read_only", "expires_in_days": 30},
            headers=secured["login"]("admin@alpha.com"),
        ).json()["plaintext"]

        via_header = http.get("/api/v1/auth/me", headers={"X-API-Key": plaintext}).json()
        via_bearer = http.get(
            "/api/v1/auth/me", headers={"Authorization": f"Bearer {plaintext}"},
        ).json()
        assert via_header["role"] == via_bearer["role"] == "read_only"
        assert via_header["tenant_id"] == via_bearer["tenant_id"]
