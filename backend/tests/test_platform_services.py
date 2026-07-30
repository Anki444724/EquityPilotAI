"""Module 10 — services: crypto, identity, tenancy, entitlements, queue, backup.

These tests use a real database, because the behaviour under test *is* the
interaction with it: token rotation, tenant filtering, quota counters and
atomic job claiming are all statements about persistence.

Each test class gets its own in-memory database so that a test which suspends
a tenant cannot change what a later test sees.
"""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.domain.platform.audit import AuditAction, AuditSeverity
from app.domain.platform.identity import (
    AuthProvider, Principal, Role, TokenType, UserStatus,
)
from app.domain.platform.jobs import InvalidTransition, JobKind, JobStatus
from app.domain.platform.plans import (
    Feature, Limit, PlanTier, Quota, SubscriptionStatus,
)
from app.models.platform import (
    ApiKey, AuditLog, BackgroundJob, RefreshToken, Subscription, Tenant,
    UsageCounter, UsageEvent, User,
)
from app.services.platform import crypto
from app.services.platform.api_keys import ApiKeyError, ApiKeyService
from app.services.platform.audit_service import AuditService, RequestContext
from app.services.platform.backup import BackupService
from app.services.platform.entitlements import BillingError, EntitlementService
from app.services.platform.identity_service import (
    AuthError, IdentityService, RegistrationError, ReuseDetected,
)
from app.services.platform.jobs.queue import JobQueue, QueueError
from app.services.platform.observability import (
    ErrorTracker, LATENCY_BUCKETS_MS, MetricsCollector, MetricsService,
    estimate_percentile, normalise_message, normalise_route,
)
from app.services.platform.tenancy import (
    TenantError, TenantScope, TenantService, _add_month,
)


@pytest.fixture
def db():
    """A private in-memory database per test."""
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def session_factory(db):
    """A factory returning the same session — the worker takes a factory."""
    return lambda: db


def _tenant(db, name="Acme Research", tier=PlanTier.PROFESSIONAL) -> Tenant:
    return TenantService(db).create(name, tier=tier)


def _user(db, tenant, email="a@acme.com", role=Role.ADMIN) -> User:
    user, _ = IdentityService(db).register(
        email=email, password="CorrectHorseBattery1", name="A Person",
        tenant_id=tenant.id, role=role, auto_verify=True,
    )
    return user


def _principal(user: User, tenant: Tenant) -> Principal:
    return Principal(
        user_id=user.id, email=user.email, name=user.name,
        role=Role(user.role), tenant_id=tenant.id, tenant_slug=tenant.slug,
    )


# ===========================================================================
class TestCrypto:
    def test_password_round_trip(self):
        digest = crypto.hash_password("CorrectHorseBattery1")
        assert crypto.verify_password("CorrectHorseBattery1", digest)
        assert not crypto.verify_password("wrong", digest)

    def test_hashes_are_salted(self):
        a = crypto.hash_password("same-password")
        b = crypto.hash_password("same-password")
        assert a != b, "identical passwords must not produce identical hashes"

    def test_argon2id_is_the_algorithm(self):
        assert crypto.hash_password("x" * 12).startswith("$argon2id$")

    def test_a_null_hash_never_verifies(self):
        """A user who only ever signs in with Google has no password hash.
        That must be a refusal, not a crash and certainly not a success."""
        assert not crypto.verify_password("anything", None)
        assert not crypto.verify_password("anything", "")

    def test_a_corrupt_hash_is_refused_not_raised(self):
        assert not crypto.verify_password("x", "not-a-hash")

    def test_an_empty_password_refuses_to_hash(self):
        with pytest.raises(ValueError):
            crypto.hash_password("")

    def test_tokens_are_unpredictable_and_long(self):
        tokens = {crypto.new_token() for _ in range(200)}
        assert len(tokens) == 200
        assert all(len(t) >= 40 for t in tokens)

    def test_token_hashing_is_stable_and_one_way(self):
        token = crypto.new_token()
        assert crypto.hash_token(token) == crypto.hash_token(token)
        assert token not in crypto.hash_token(token)
        assert len(crypto.hash_token(token)) == 64

    def test_api_key_structure_and_parsing(self):
        generated = crypto.generate_api_key()
        assert generated.plaintext.startswith("ierp_live_")
        assert crypto.parse_api_key(generated.plaintext) == generated.key_id
        assert generated.key_hash == crypto.hash_token(generated.plaintext)

    @pytest.mark.parametrize("bad", [
        "", "nope", "ierp_live_short_x", "wrong_live_aaaaaaaaaaaaaaaa_x",
        "ierp_live_NOTHEX0000000000_x",
    ])
    def test_malformed_api_keys_are_rejected_before_a_lookup(self, bad):
        assert crypto.parse_api_key(bad) is None

    def test_jwt_round_trip(self):
        token = crypto.encode_jwt({"sub": "u1", "typ": "access"}, ttl_seconds=60)
        claims = crypto.decode_jwt(token, expected_type="access")
        assert claims["sub"] == "u1"
        assert claims["exp"] > claims["iat"]
        assert claims["jti"]

    def test_a_token_of_the_wrong_type_is_refused(self):
        """Without this check an access token and a refresh token are
        interchangeable, and a thirty-day credential becomes a session."""
        token = crypto.encode_jwt({"sub": "u1", "typ": "refresh"}, ttl_seconds=60)
        with pytest.raises(crypto.TokenError):
            crypto.decode_jwt(token, expected_type="access")

    def test_an_expired_token_is_refused(self):
        token = crypto.encode_jwt({"sub": "u1", "typ": "access"}, ttl_seconds=-10)
        with pytest.raises(crypto.TokenError):
            crypto.decode_jwt(token)

    def test_a_tampered_token_is_refused(self):
        token = crypto.encode_jwt({"sub": "u1", "typ": "access"}, ttl_seconds=60)
        head, payload, signature = token.split(".")
        with pytest.raises(crypto.TokenError):
            crypto.decode_jwt(f"{head}.{payload}.{signature[:-4]}zzzz")

    def test_the_none_algorithm_is_not_accepted(self):
        """The classic JWT confusion attack: an unsigned token accepted
        because the library trusted the header's `alg`."""
        import base64
        import json

        def _b64(data: dict) -> str:
            raw = json.dumps(data).encode()
            return base64.urlsafe_b64encode(raw).decode().rstrip("=")

        forged = (
            _b64({"alg": "none", "typ": "JWT"}) + "." +
            _b64({"sub": "attacker", "typ": "access", "exp": 9_999_999_999}) + "."
        )
        with pytest.raises(crypto.TokenError):
            crypto.decode_jwt(forged)

    def test_secret_encryption_round_trip(self):
        blob = crypto.encrypt_secret("sk-super-secret-value")
        assert b"sk-super-secret-value" not in blob
        assert crypto.decrypt_secret(blob) == "sk-super-secret-value"

    def test_encryption_is_non_deterministic(self):
        a = crypto.encrypt_secret("same")
        b = crypto.encrypt_secret("same")
        assert a != b, "a fresh nonce must be used each time"

    def test_tampered_ciphertext_fails_to_decrypt(self):
        """AES-GCM authenticates: a modified ciphertext raises rather than
        decrypting to something plausible but wrong."""
        blob = bytearray(crypto.encrypt_secret("value"))
        blob[-1] ^= 0xFF
        with pytest.raises(Exception):
            crypto.decrypt_secret(bytes(blob))

    def test_webhook_signature_verification(self):
        payload = b'{"event":"paid"}'
        signature = crypto.sign_payload(payload, "whsec")
        assert crypto.verify_signature(payload, signature, "whsec")
        assert not crypto.verify_signature(payload, signature, "wrong-secret")
        assert not crypto.verify_signature(b'{"event":"x"}', signature, "whsec")

    def test_csrf_tokens_are_session_bound(self):
        assert crypto.verify_csrf("s1", crypto.csrf_token("s1"))
        assert not crypto.verify_csrf("s2", crypto.csrf_token("s1"))
        assert not crypto.verify_csrf("s1", "")


# ===========================================================================
class TestTenantService:
    def test_create_makes_a_tenant_and_a_subscription_together(self):
        """A tenant without a subscription cannot answer an entitlement
        question, so the two are never created separately."""

    def test_creation(self, db):
        tenant = TenantService(db).create("Demo Capital", tier=PlanTier.BASIC)
        assert tenant.slug == "demo-capital"
        subscription = db.scalar(
            select(Subscription).where(Subscription.tenant_id == tenant.id)
        )
        assert subscription is not None
        assert subscription.plan_tier == PlanTier.BASIC.value

    def test_slug_collisions_are_resolved(self, db):
        service = TenantService(db)
        a = service.create("Demo Capital")
        b = service.create("Demo Capital")
        c = service.create("Demo Capital")
        assert [a.slug, b.slug, c.slug] == [
            "demo-capital", "demo-capital-2", "demo-capital-3",
        ]

    def test_a_trial_plan_starts_a_trial(self, db):
        tenant = TenantService(db).create("Trialist", tier=PlanTier.PROFESSIONAL)
        assert tenant.status == "trial"
        assert tenant.trial_ends_at is not None

    def test_a_free_plan_is_active_immediately(self, db):
        assert TenantService(db).create("Freebie", tier=PlanTier.FREE).status == "active"

    def test_update_ignores_fields_outside_the_allow_list(self, db):
        """A schema change must not be able to smuggle `status` into a PATCH."""
        service = TenantService(db)
        tenant = service.create("Acme")
        service.update(tenant, name="Acme Two", status="active", storage_bytes=99)
        assert tenant.name == "Acme Two"
        assert tenant.storage_bytes == 0

    def test_settings_merge_rather_than_replace(self, db):
        service = TenantService(db)
        tenant = service.create("Acme")
        service.update_settings(tenant, {"theme": "dark"})
        service.update_settings(tenant, {"locale": "en-IN"})
        assert tenant.settings == {"theme": "dark", "locale": "en-IN"}

    def test_suspension_and_reactivation(self, db):
        service = TenantService(db)
        tenant = service.create("Acme")
        service.suspend(tenant, "non-payment")
        assert service.is_blocked(tenant)
        assert tenant.suspended_reason == "non-payment"
        service.reactivate(tenant)
        assert not service.is_blocked(tenant)
        assert tenant.suspended_at is None

    def test_expired_trials_degrade_to_past_due_not_suspended(self, db):
        """The customer keeps read access to their own research while they
        sort out payment. Suspending on day one loses accounts."""
        service = TenantService(db)
        tenant = service.create("Trialist", tier=PlanTier.PROFESSIONAL)
        tenant.trial_ends_at = datetime.now(timezone.utc) - timedelta(days=1)
        db.commit()

        assert service.expire_trials() == 1
        db.refresh(tenant)
        assert tenant.status == "past_due"
        assert service.is_read_only(tenant)
        assert not service.is_blocked(tenant)

    def test_month_arithmetic_clamps_to_month_length(self):
        """31 January + 1 month is 28 February, not 3 March. Otherwise a
        tenant who signed up on the 31st is metered on a different day every
        month."""
        assert _add_month(date(2026, 1, 31)) == date(2026, 2, 28)
        assert _add_month(date(2024, 1, 31)) == date(2024, 2, 29)  # leap year
        assert _add_month(date(2026, 12, 15)) == date(2027, 1, 15)


class TestTenantScope:
    def test_a_scope_filters_to_its_tenant(self, db):
        a, b = _tenant(db, "Alpha"), _tenant(db, "Beta")
        _user(db, a, "a@alpha.com")
        _user(db, b, "b@beta.com")

        scope = TenantScope(tenant_id=a.id)
        rows = db.scalars(scope.apply(select(User), User.tenant_id)).all()
        assert {u.email for u in rows} == {"a@alpha.com"}

    def test_an_operator_scope_sees_everything(self, db):
        a, b = _tenant(db, "Alpha"), _tenant(db, "Beta")
        _user(db, a, "a@alpha.com")
        _user(db, b, "b@beta.com")

        scope = TenantScope(tenant_id=a.id, unrestricted=True)
        rows = db.scalars(scope.apply(select(User), User.tenant_id)).all()
        assert len(rows) == 2

    def test_a_null_tenant_matches_nothing(self, db):
        """The safe reading of "belongs to no organisation"."""
        a = _tenant(db, "Alpha")
        _user(db, a, "a@alpha.com")
        scope = TenantScope(tenant_id=None)
        assert db.scalars(scope.apply(select(User), User.tenant_id)).all() == []

    def test_check_raises_on_a_foreign_row(self, db):
        from app.domain.platform.identity import TenantIsolationError

        scope = TenantScope(tenant_id=1)
        scope.check(1)
        with pytest.raises(TenantIsolationError):
            scope.check(2)
        with pytest.raises(TenantIsolationError):
            scope.check(None)

    def test_assign_refuses_without_a_tenant(self):
        with pytest.raises(TenantError):
            TenantScope(tenant_id=None, unrestricted=True).assign()


# ===========================================================================
class TestRegistrationAndLogin:
    def test_self_serve_registration_creates_an_organisation(self, db):
        service = IdentityService(db)
        user, pending = service.register(
            email="Founder@Acme.IN", password="CorrectHorseBattery1",
            name="Founder",
        )
        assert user.email == "founder@acme.in", "the address must be normalised"
        assert user.role == Role.ADMIN.value, "whoever creates the org owns it"
        assert user.status == UserStatus.PENDING.value
        assert pending is not None and pending.purpose is TokenType.EMAIL_VERIFY
        assert db.get(Tenant, user.tenant_id).name == "Acme"

    def test_a_consumer_domain_names_the_workspace_after_the_person(self, db):
        user, _ = IdentityService(db).register(
            email="someone@gmail.com", password="CorrectHorseBattery1",
            name="Priya Nair",
        )
        assert db.get(Tenant, user.tenant_id).name == "Priya's Workspace"

    def test_a_weak_password_is_refused_with_reasons(self, db):
        with pytest.raises(RegistrationError) as exc:
            IdentityService(db).register(
                email="a@b.com", password="short", name="A",
            )
        assert exc.value.problems

    def test_a_duplicate_address_is_indistinguishable(self, db):
        """The router turns this sentinel into the same body a genuine
        registration returns, so the endpoint is not a membership oracle."""
        service = IdentityService(db)
        service.register(email="a@b.com", password="CorrectHorseBattery1", name="A")
        with pytest.raises(RegistrationError) as exc:
            service.register(email="a@b.com", password="CorrectHorseBattery1", name="A")
        assert str(exc.value) == "__exists__"

    def test_a_pending_user_cannot_sign_in(self, db):
        service = IdentityService(db)
        service.register(email="a@b.com", password="CorrectHorseBattery1", name="A")
        with pytest.raises(AuthError) as exc:
            service.authenticate(email="a@b.com", password="CorrectHorseBattery1")
        assert "verify" in str(exc.value).lower()

    def test_verification_activates_the_account(self, db):
        service = IdentityService(db)
        _, pending = service.register(
            email="a@b.com", password="CorrectHorseBattery1", name="A",
        )
        user = service.verify_email(pending.token)
        assert user.status == UserStatus.ACTIVE.value
        assert user.email_verified_at is not None
        assert service.authenticate(email="a@b.com", password="CorrectHorseBattery1")

    def test_a_verification_token_is_single_use(self, db):
        service = IdentityService(db)
        _, pending = service.register(
            email="a@b.com", password="CorrectHorseBattery1", name="A",
        )
        service.verify_email(pending.token)
        with pytest.raises(AuthError):
            service.verify_email(pending.token)

    def test_an_unknown_address_and_a_wrong_password_look_identical(self, db):
        service = IdentityService(db)
        tenant = _tenant(db)
        _user(db, tenant, "real@acme.com")

        with pytest.raises(AuthError) as unknown:
            service.authenticate(email="nobody@acme.com", password="whatever12345")
        with pytest.raises(AuthError) as wrong:
            service.authenticate(email="real@acme.com", password="whatever12345")
        assert str(unknown.value) == str(wrong.value)

    def test_a_successful_sign_in_mints_a_session(self, db):
        tenant = _tenant(db)
        _user(db, tenant, "real@acme.com")
        outcome = IdentityService(db).authenticate(
            email="real@acme.com", password="CorrectHorseBattery1",
        )
        assert outcome.tokens.access_token
        assert outcome.tokens.refresh_token
        assert outcome.tokens.csrf_token
        assert outcome.principal.tenant_id == tenant.id

    def test_repeated_failures_lock_the_account(self, db):
        from app.core.config import settings

        service = IdentityService(db)
        tenant = _tenant(db)
        user = _user(db, tenant, "real@acme.com")

        for _ in range(settings.MAX_FAILED_LOGINS):
            with pytest.raises(AuthError):
                service.authenticate(email="real@acme.com", password="wrong-password")

        db.refresh(user)
        assert user.locked_until is not None

        # Even the correct password is refused while locked.
        with pytest.raises(AuthError) as exc:
            service.authenticate(email="real@acme.com", password="CorrectHorseBattery1")
        assert "Too many failed attempts" in str(exc.value)

    def test_a_successful_sign_in_clears_the_failure_count(self, db):
        service = IdentityService(db)
        tenant = _tenant(db)
        user = _user(db, tenant, "real@acme.com")

        with pytest.raises(AuthError):
            service.authenticate(email="real@acme.com", password="wrong-password")
        service.authenticate(email="real@acme.com", password="CorrectHorseBattery1")
        db.refresh(user)
        assert user.failed_login_count == 0


class TestFederatedLogin:
    def test_google_creates_an_account_already_verified(self, db):
        """The provider vouched for the address, so there is nothing left for
        us to verify."""
        outcome = IdentityService(db).authenticate_federated(
            provider=AuthProvider.GOOGLE, subject="g-1",
            email="new@acme.com", name="New Person",
        )
        assert outcome.user.status == UserStatus.ACTIVE.value
        assert outcome.user.email_verified_at is not None

    def test_a_second_sign_in_reuses_the_same_account(self, db):
        service = IdentityService(db)
        first = service.authenticate_federated(
            provider=AuthProvider.GOOGLE, subject="g-1",
            email="new@acme.com", name="New Person",
        )
        second = service.authenticate_federated(
            provider=AuthProvider.GOOGLE, subject="g-1",
            email="new@acme.com", name="New Person",
        )
        assert first.user.id == second.user.id
        assert db.scalar(select(func.count(User.id))) == 1

    def test_google_links_to_an_existing_password_account(self, db):
        service = IdentityService(db)
        tenant = _tenant(db)
        existing = _user(db, tenant, "both@acme.com")

        outcome = service.authenticate_federated(
            provider=AuthProvider.GOOGLE, subject="g-2",
            email="both@acme.com", name="Both",
        )
        assert outcome.user.id == existing.id

    def test_a_non_federated_provider_is_refused(self, db):
        """The allow-list is what makes auto-linking on email safe. A provider
        that does not verify addresses would be an account-takeover hole."""
        with pytest.raises(AuthError):
            IdentityService(db).authenticate_federated(
                provider=AuthProvider.PASSWORD, subject="x",
                email="a@b.com", name="A",
            )


class TestTokenRotation:
    def _signed_in(self, db):
        tenant = _tenant(db)
        _user(db, tenant, "real@acme.com")
        return IdentityService(db).authenticate(
            email="real@acme.com", password="CorrectHorseBattery1",
        )

    def test_refresh_issues_a_new_pair(self, db):
        service = IdentityService(db)
        first = self._signed_in(db)
        second, _ = service.refresh_session(first.tokens.refresh_token)
        assert second.tokens.refresh_token != first.tokens.refresh_token
        assert second.tokens.access_token != first.tokens.access_token

    def test_the_session_id_survives_rotation(self, db):
        service = IdentityService(db)
        first = self._signed_in(db)
        second, _ = service.refresh_session(first.tokens.refresh_token)
        assert second.tokens.session_id == first.tokens.session_id

    def test_replaying_a_spent_token_revokes_the_whole_family(self, db):
        """The only available signal that a refresh token has been stolen."""
        service = IdentityService(db)
        first = self._signed_in(db)
        second, _ = service.refresh_session(first.tokens.refresh_token)

        with pytest.raises(ReuseDetected):
            service.refresh_session(first.tokens.refresh_token)

        # The successor the legitimate client holds is now dead too — that is
        # the point: we cannot tell attacker from victim, so both are stopped.
        with pytest.raises(AuthError):
            service.refresh_session(second.tokens.refresh_token)

    def test_an_unknown_token_is_refused(self, db):
        with pytest.raises(AuthError):
            IdentityService(db).refresh_session("not-a-real-token")

    def test_an_expired_refresh_token_is_refused(self, db):
        service = IdentityService(db)
        outcome = self._signed_in(db)
        row = db.scalar(select(RefreshToken))
        row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.commit()
        with pytest.raises(AuthError):
            service.refresh_session(outcome.tokens.refresh_token)

    def test_logout_revokes_the_family(self, db):
        service = IdentityService(db)
        outcome = self._signed_in(db)
        assert service.revoke_session(outcome.tokens.refresh_token)
        with pytest.raises(AuthError):
            service.refresh_session(outcome.tokens.refresh_token)

    def test_active_sessions_excludes_rotated_and_revoked(self, db):
        service = IdentityService(db)
        outcome = self._signed_in(db)
        assert len(service.active_sessions(outcome.user.id)) == 1
        service.refresh_session(outcome.tokens.refresh_token)
        assert len(service.active_sessions(outcome.user.id)) == 1

    def test_an_access_token_resolves_to_a_live_principal(self, db):
        service = IdentityService(db)
        outcome = self._signed_in(db)
        principal = service.principal_from_access_token(outcome.tokens.access_token)
        assert principal.user_id == outcome.user.id
        assert principal.tenant_id == outcome.user.tenant_id

    def test_a_suspended_user_cannot_use_a_live_access_token(self, db):
        """The user row is loaded rather than trusted from the claims. A
        suspension must not wait fifteen minutes to take effect."""
        service = IdentityService(db)
        outcome = self._signed_in(db)
        outcome.user.status = UserStatus.SUSPENDED.value
        db.commit()
        with pytest.raises(AuthError):
            service.principal_from_access_token(outcome.tokens.access_token)


class TestPasswordFlows:
    def test_reset_changes_the_password_and_kills_every_session(self, db):
        service = IdentityService(db)
        tenant = _tenant(db)
        user = _user(db, tenant, "real@acme.com")
        signed_in = service.authenticate(
            email="real@acme.com", password="CorrectHorseBattery1",
        )

        pending = service.request_password_reset("real@acme.com")
        service.reset_password(pending.token, "BrandNewPassphrase2")

        assert service.authenticate(email="real@acme.com", password="BrandNewPassphrase2")
        with pytest.raises(AuthError):
            service.authenticate(email="real@acme.com", password="CorrectHorseBattery1")
        # If the reset happened because the account was compromised, leaving
        # the attacker's session alive defeats the whole exercise.
        with pytest.raises(AuthError):
            service.refresh_session(signed_in.tokens.refresh_token)

    def test_an_unknown_address_yields_no_token(self, db):
        assert IdentityService(db).request_password_reset("nobody@nowhere.com") is None

    def test_issuing_a_new_token_invalidates_the_previous_one(self, db):
        """Two live reset links means a user can forward the older one to
        support and hand over a working credential."""
        service = IdentityService(db)
        tenant = _tenant(db)
        _user(db, tenant, "real@acme.com")

        first = service.request_password_reset("real@acme.com")
        second = service.request_password_reset("real@acme.com")

        with pytest.raises(AuthError):
            service.reset_password(first.token, "BrandNewPassphrase2")
        assert service.reset_password(second.token, "BrandNewPassphrase2")

    def test_a_weak_new_password_leaves_the_link_usable(self, db):
        """Consuming the token on a policy failure would strand the user with
        a dead link and a password they never changed."""
        service = IdentityService(db)
        tenant = _tenant(db)
        _user(db, tenant, "real@acme.com")
        pending = service.request_password_reset("real@acme.com")

        with pytest.raises(RegistrationError):
            service.reset_password(pending.token, "weak")
        assert service.reset_password(pending.token, "BrandNewPassphrase2")

    def test_a_reset_token_cannot_be_spent_as_a_magic_link(self, db):
        service = IdentityService(db)
        tenant = _tenant(db)
        _user(db, tenant, "real@acme.com")
        pending = service.request_password_reset("real@acme.com")
        with pytest.raises(AuthError):
            service.authenticate_magic_link(token=pending.token)

    def test_an_expired_token_is_refused(self, db):
        from app.models.platform import OneTimeToken

        service = IdentityService(db)
        tenant = _tenant(db)
        _user(db, tenant, "real@acme.com")
        pending = service.request_password_reset("real@acme.com")

        row = db.scalar(select(OneTimeToken))
        row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
        with pytest.raises(AuthError) as exc:
            service.reset_password(pending.token, "BrandNewPassphrase2")
        assert "expired" in str(exc.value).lower()

    def test_magic_link_signs_in_and_verifies_the_address(self, db):
        service = IdentityService(db)
        user, _ = service.register(
            email="a@b.com", password="CorrectHorseBattery1", name="A",
        )
        assert user.email_verified_at is None

        pending = service.request_magic_link("a@b.com")
        outcome = service.authenticate_magic_link(token=pending.token)
        assert outcome.user.email_verified_at is not None
        assert outcome.user.status == UserStatus.ACTIVE.value

    def test_change_password_requires_the_current_one(self, db):
        service = IdentityService(db)
        tenant = _tenant(db)
        user = _user(db, tenant, "real@acme.com")
        with pytest.raises(AuthError):
            service.change_password(user, "wrong-password", "BrandNewPassphrase2")


class TestMemberAdministration:
    def test_an_admin_may_change_a_junior_role(self, db):
        service = IdentityService(db)
        tenant = _tenant(db)
        admin = _user(db, tenant, "admin@acme.com", Role.ADMIN)
        member = _user(db, tenant, "member@acme.com", Role.READ_ONLY)

        updated = service.change_role(_principal(admin, tenant), member, Role.ANALYST)
        assert updated.role == Role.ANALYST.value

    def test_an_admin_may_not_change_a_peer(self, db):
        service = IdentityService(db)
        tenant = _tenant(db)
        a = _user(db, tenant, "a@acme.com", Role.ADMIN)
        b = _user(db, tenant, "b@acme.com", Role.ADMIN)
        with pytest.raises(AuthError):
            service.change_role(_principal(a, tenant), b, Role.READ_ONLY)

    def test_nobody_may_change_their_own_role(self, db):
        service = IdentityService(db)
        tenant = _tenant(db)
        admin = _user(db, tenant, "admin@acme.com", Role.ADMIN)
        with pytest.raises(AuthError):
            service.change_role(_principal(admin, tenant), admin, Role.SUPER_ADMIN)

    def test_an_admin_may_not_grant_a_role_above_their_own(self, db):
        service = IdentityService(db)
        tenant = _tenant(db)
        admin = _user(db, tenant, "admin@acme.com", Role.ADMIN)
        member = _user(db, tenant, "member@acme.com", Role.READ_ONLY)
        with pytest.raises(AuthError):
            service.change_role(_principal(admin, tenant), member, Role.SUPER_ADMIN)

    def test_a_role_change_ends_the_member_sessions(self, db):
        """The old role is inside any live access token."""
        service = IdentityService(db)
        tenant = _tenant(db)
        admin = _user(db, tenant, "admin@acme.com", Role.ADMIN)
        member = _user(db, tenant, "member@acme.com", Role.ANALYST)
        signed_in = service.authenticate(
            email="member@acme.com", password="CorrectHorseBattery1",
        )

        service.change_role(_principal(admin, tenant), member, Role.READ_ONLY)
        with pytest.raises(AuthError):
            service.refresh_session(signed_in.tokens.refresh_token)

    def test_last_admin_detection(self, db):
        service = IdentityService(db)
        tenant = _tenant(db)
        admin = _user(db, tenant, "admin@acme.com", Role.ADMIN)
        _user(db, tenant, "reader@acme.com", Role.READ_ONLY)

        assert service.last_admin_check(tenant.id, excluding=admin.id)
        _user(db, tenant, "admin2@acme.com", Role.ADMIN)
        assert not service.last_admin_check(tenant.id, excluding=admin.id)

    def test_an_invitation_creates_a_pending_member_with_no_password(self, db):
        """No password is generated: every channel for transmitting one is
        worse than a single-use link."""
        service = IdentityService(db)
        tenant = _tenant(db)
        admin = _user(db, tenant, "admin@acme.com", Role.ADMIN)

        member, pending = service.invite(
            tenant_id=tenant.id, email="new@acme.com", name="New",
            role=Role.RESEARCHER, invited_by=admin.id,
        )
        assert member.password_hash is None
        assert member.status == UserStatus.PENDING.value
        assert pending.purpose is TokenType.PASSWORD_RESET

    def test_an_invited_member_activates_by_setting_a_password(self, db):
        service = IdentityService(db)
        tenant = _tenant(db)
        admin = _user(db, tenant, "admin@acme.com", Role.ADMIN)
        member, pending = service.invite(
            tenant_id=tenant.id, email="new@acme.com", name="New",
            role=Role.RESEARCHER, invited_by=admin.id,
        )
        service.reset_password(pending.token, "TheirOwnPassphrase3")
        db.refresh(member)
        assert member.status == UserStatus.ACTIVE.value
        assert member.email_verified_at is not None


# ===========================================================================
class TestEntitlementService:
    def test_the_catalogue_seeds_four_plans(self, db):
        service = EntitlementService(db)
        assert service.sync_catalogue() == 4
        assert len(service.plans()) == 4

    def test_seeding_twice_does_not_duplicate_or_overwrite(self, db):
        """An operator's edited price must survive a redeploy."""
        service = EntitlementService(db)
        service.sync_catalogue()
        service.update_plan(PlanTier.BASIC, price_monthly_inr=1)
        assert service.sync_catalogue() == 0
        assert service.plan_row(PlanTier.BASIC).price_monthly_inr == 1

    def test_a_check_passes_within_the_allowance(self, db):
        tenant = _tenant(db, tier=PlanTier.PROFESSIONAL)
        service = EntitlementService(db)
        service.sync_catalogue()
        assert service.check(tenant.id, feature=Feature.AI_ANALYST)

    def test_a_check_fails_for_a_feature_outside_the_plan(self, db):
        tenant = _tenant(db, tier=PlanTier.FREE)
        service = EntitlementService(db)
        service.sync_catalogue()
        decision = service.check(tenant.id, feature=Feature.AI_ANALYST)
        assert not decision
        assert decision.upgrade_to is PlanTier.PROFESSIONAL

    def test_consumption_moves_the_counter(self, db):
        tenant = _tenant(db)
        service = EntitlementService(db)
        service.sync_catalogue()

        service.consume(tenant.id, Quota.AI_CALLS, 5)
        service.consume(tenant.id, Quota.AI_CALLS, 3)
        assert service.usage(tenant.id, Quota.AI_CALLS).used == 8

    def test_consumption_writes_a_raw_event_as_well(self, db):
        """The counter settles quota decisions; the events settle disputes."""
        tenant = _tenant(db)
        service = EntitlementService(db)
        service.sync_catalogue()
        service.consume(tenant.id, Quota.AI_CALLS, 2, resource_type="report", resource_id="7")

        event = db.scalar(select(UsageEvent))
        assert event.quantity == 2
        assert event.resource_id == "7"

    def test_the_gate_refuses_once_the_quota_is_spent(self, db):
        tenant = _tenant(db, tier=PlanTier.BASIC)
        service = EntitlementService(db)
        service.sync_catalogue()

        allowance = service.spec_for(PlanTier.BASIC).quota(Quota.REPORTS_GENERATED)
        service.consume(tenant.id, Quota.REPORTS_GENERATED, allowance)
        decision = service.check(tenant.id, quota=Quota.REPORTS_GENERATED)
        assert not decision
        assert decision.used == allowance

    def test_a_seat_limit_counts_real_members(self, db):
        tenant = _tenant(db, tier=PlanTier.FREE)
        service = EntitlementService(db)
        service.sync_catalogue()
        _user(db, tenant, "one@acme.com")
        assert not service.check(tenant.id, limit=Limit.SEATS)

    def test_contract_overrides_beat_the_plan(self, db):
        """Enterprise deals always need this, and bolting it on later means
        special-casing the entitlement check."""
        tenant = _tenant(db, tier=PlanTier.FREE)
        service = EntitlementService(db)
        service.sync_catalogue()

        subscription = service.subscription_for(tenant.id)
        subscription.feature_overrides = [Feature.AI_ANALYST.value]
        subscription.quota_overrides = {Quota.AI_CALLS.value: 500}
        db.commit()

        assert service.check(tenant.id, feature=Feature.AI_ANALYST)
        assert service.spec_for(PlanTier.FREE, subscription).quota(Quota.AI_CALLS) == 500

    def test_a_negative_override_removes_a_granted_feature(self, db):
        tenant = _tenant(db, tier=PlanTier.PROFESSIONAL)
        service = EntitlementService(db)
        service.sync_catalogue()
        subscription = service.subscription_for(tenant.id)
        subscription.feature_overrides = [f"-{Feature.AI_ANALYST.value}"]
        db.commit()
        assert not service.check(tenant.id, feature=Feature.AI_ANALYST)

    def test_an_upgrade_keeps_the_current_metering_window(self, db):
        """Resetting the period on upgrade hands the customer a second full
        allowance for the same month."""
        tenant = _tenant(db, tier=PlanTier.BASIC)
        service = EntitlementService(db)
        service.sync_catalogue()
        before = service.subscription_for(tenant.id).period_start

        service.change_plan(tenant.id, PlanTier.PROFESSIONAL)
        assert service.subscription_for(tenant.id).period_start == before

    def test_the_period_rolls_forward_lazily_on_read(self, db):
        """Done on read as well as by a job, so a quota resets on time even if
        the scheduler is down."""
        tenant = _tenant(db)
        service = EntitlementService(db)
        service.sync_catalogue()
        subscription = service.subscription_for(tenant.id)
        subscription.period_start = date.today() - timedelta(days=70)
        subscription.period_end = date.today() - timedelta(days=40)
        db.commit()

        rolled = service.subscription_for(tenant.id)
        assert rolled.period_end > date.today()

    def test_cancelling_keeps_access_to_the_period_end(self, db):
        tenant = _tenant(db)
        service = EntitlementService(db)
        service.sync_catalogue()
        subscription = service.cancel(tenant.id)
        assert subscription.cancel_at_period_end
        # Still an entitled status — a Professional tenant is created on a
        # trial, and cancelling schedules the end rather than applying it.
        assert SubscriptionStatus(subscription.status) in (
            SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIALING,
        )
        assert service.check(tenant.id, feature=Feature.HISTORICAL_FINANCIALS)

    def test_cancelling_immediately_stops_access(self, db):
        tenant = _tenant(db)
        service = EntitlementService(db)
        service.sync_catalogue()
        service.cancel(tenant.id, immediately=True)
        assert not service.check(tenant.id, feature=Feature.HISTORICAL_FINANCIALS)

    def test_a_suspended_tenant_is_refused_everything(self, db):
        tenant = _tenant(db)
        service = EntitlementService(db)
        service.sync_catalogue()
        TenantService(db).suspend(tenant, "abuse")
        decision = service.check(tenant.id, feature=Feature.HISTORICAL_FINANCIALS)
        assert not decision
        assert decision.reason.value == "tenant_suspended"

    def test_the_assembled_view_covers_every_quota_and_limit(self, db):
        tenant = _tenant(db)
        service = EntitlementService(db)
        service.sync_catalogue()
        view = service.entitlements(tenant.id)
        assert set(view.quotas) == set(Quota)
        assert set(view.limits) == set(Limit)

    def test_nearing_limit_flags_only_the_busy_quotas(self, db):
        tenant = _tenant(db, tier=PlanTier.BASIC)
        service = EntitlementService(db)
        service.sync_catalogue()
        allowance = service.spec_for(PlanTier.BASIC).quota(Quota.REPORTS_GENERATED)
        service.consume(tenant.id, Quota.REPORTS_GENERATED, int(allowance * 0.85))

        flagged = {u.quota for u in service.entitlements(tenant.id).nearing_limit}
        assert Quota.REPORTS_GENERATED in flagged
        assert Quota.API_REQUESTS not in flagged

    def test_a_trial_contributes_no_revenue(self, db):
        _tenant(db, "Trialist", tier=PlanTier.PROFESSIONAL)
        service = EntitlementService(db)
        service.sync_catalogue()
        assert service.platform_revenue()["mrr_inr"] == 0

    def test_mrr_normalises_annual_billing_to_a_month(self, db):
        from app.domain.platform.plans import BillingPeriod

        tenant = _tenant(db, tier=PlanTier.PROFESSIONAL)
        service = EntitlementService(db)
        service.sync_catalogue()
        service.change_plan(
            tenant.id, PlanTier.PROFESSIONAL, billing_period=BillingPeriod.ANNUAL,
        )
        spec = service.spec_for(PlanTier.PROFESSIONAL)
        assert service.platform_revenue()["mrr_inr"] == spec.price_annual_inr // 12

    def test_a_missing_subscription_raises(self, db):
        with pytest.raises(BillingError):
            EntitlementService(db).subscription_for(9999)


# ===========================================================================
class TestApiKeyService:
    def test_creation_returns_the_plaintext_exactly_once(self, db):
        tenant = _tenant(db)
        admin = _user(db, tenant, "admin@acme.com", Role.ADMIN)
        issued = ApiKeyService(db).create(
            principal=_principal(admin, tenant), name="CI",
        )
        assert issued.plaintext.startswith("ierp_live_")
        # Nothing stored can reconstruct it.
        assert issued.plaintext not in (issued.record.key_hash, issued.record.key_id)

    def test_a_key_authenticates_to_a_principal(self, db):
        tenant = _tenant(db)
        admin = _user(db, tenant, "admin@acme.com", Role.ADMIN)
        service = ApiKeyService(db)
        issued = service.create(
            principal=_principal(admin, tenant), name="CI", role=Role.READ_ONLY,
        )
        principal = service.authenticate(issued.plaintext)
        assert principal.tenant_id == tenant.id
        assert principal.role is Role.READ_ONLY
        assert principal.api_key_id == issued.record.id

    def test_a_key_defaults_to_read_only(self, db):
        """Most keys feed a dashboard. A key that can delete a portfolio
        because nobody chose a role is a bad accident."""
        tenant = _tenant(db)
        admin = _user(db, tenant, "admin@acme.com", Role.ADMIN)
        issued = ApiKeyService(db).create(
            principal=_principal(admin, tenant), name="CI",
        )
        assert issued.record.role == Role.READ_ONLY.value

    def test_a_key_cannot_exceed_its_creator(self, db):
        tenant = _tenant(db)
        analyst = _user(db, tenant, "analyst@acme.com", Role.ANALYST)
        with pytest.raises(ApiKeyError):
            ApiKeyService(db).create(
                principal=_principal(analyst, tenant), name="Escalate",
                role=Role.ADMIN,
            )

    def test_a_revoked_key_stops_working(self, db):
        tenant = _tenant(db)
        admin = _user(db, tenant, "admin@acme.com", Role.ADMIN)
        service = ApiKeyService(db)
        issued = service.create(principal=_principal(admin, tenant), name="CI")
        service.revoke(tenant.id, issued.record.id)
        with pytest.raises(ApiKeyError):
            service.authenticate(issued.plaintext)

    def test_an_expired_key_stops_working(self, db):
        tenant = _tenant(db)
        admin = _user(db, tenant, "admin@acme.com", Role.ADMIN)
        service = ApiKeyService(db)
        issued = service.create(principal=_principal(admin, tenant), name="CI")
        issued.record.expires_at = datetime.now(timezone.utc) - timedelta(days=1)
        db.commit()
        with pytest.raises(ApiKeyError):
            service.authenticate(issued.plaintext)

    def test_a_key_dies_with_its_owners_account(self, db):
        """The most common way an offboarding is incomplete."""
        tenant = _tenant(db)
        admin = _user(db, tenant, "admin@acme.com", Role.ADMIN)
        service = ApiKeyService(db)
        issued = service.create(principal=_principal(admin, tenant), name="CI")

        admin.status = UserStatus.SUSPENDED.value
        db.commit()
        with pytest.raises(ApiKeyError):
            service.authenticate(issued.plaintext)

    def test_a_key_dies_with_its_tenant(self, db):
        tenant = _tenant(db)
        admin = _user(db, tenant, "admin@acme.com", Role.ADMIN)
        service = ApiKeyService(db)
        issued = service.create(principal=_principal(admin, tenant), name="CI")

        TenantService(db).suspend(tenant, "abuse")
        with pytest.raises(ApiKeyError):
            service.authenticate(issued.plaintext)

    def test_every_failure_reports_the_same_message(self, db):
        """A response distinguishing "unknown" from "revoked" tells an
        attacker which guess was once real."""
        tenant = _tenant(db)
        admin = _user(db, tenant, "admin@acme.com", Role.ADMIN)
        service = ApiKeyService(db)
        issued = service.create(principal=_principal(admin, tenant), name="CI")
        service.revoke(tenant.id, issued.record.id)

        messages = set()
        for candidate in ("garbage", crypto.generate_api_key().plaintext, issued.plaintext):
            with pytest.raises(ApiKeyError) as exc:
                service.authenticate(candidate)
            messages.add(str(exc.value))
        assert messages == {"Invalid API key."}

    def test_revoking_by_user_covers_every_key_they_made(self, db):
        tenant = _tenant(db)
        admin = _user(db, tenant, "admin@acme.com", Role.ADMIN)
        service = ApiKeyService(db)
        principal = _principal(admin, tenant)
        service.create(principal=principal, name="One")
        service.create(principal=principal, name="Two")
        assert service.revoke_for_user(admin.id) == 2
        assert service.list(tenant.id) == []

    def test_use_is_recorded(self, db):
        tenant = _tenant(db)
        admin = _user(db, tenant, "admin@acme.com", Role.ADMIN)
        service = ApiKeyService(db)
        issued = service.create(principal=_principal(admin, tenant), name="CI")
        service.authenticate(issued.plaintext, ip_address="10.0.0.1")
        db.refresh(issued.record)
        assert issued.record.call_count == 1
        assert issued.record.last_used_at is not None


# ===========================================================================
class TestAuditService:
    def test_an_event_is_written_with_derived_category_and_severity(self, db):
        service = AuditService(db)
        row = service.record(
            AuditAction.LOGIN_FAILED, tenant_id=1, actor_email="a@b.com",
            summary="bad password", outcome="failure",
        )
        assert row.category == "auth"
        assert row.severity == "warning"

    def test_metadata_is_redacted_on_the_way_in(self, db):
        """There is no code path into `audit_logs` that skips redaction."""
        row = AuditService(db).record(
            AuditAction.APIKEY_CREATED, tenant_id=1,
            metadata={"api_key": "ierp_live_secret_value", "name": "CI"},
        )
        assert row.meta["api_key"] == "[redacted]"
        assert row.meta["name"] == "CI"

    def test_a_tenant_sees_only_its_own_rows(self, db):
        service = AuditService(db)
        service.record(AuditAction.LOGIN_SUCCEEDED, tenant_id=1, summary="a")
        service.record(AuditAction.LOGIN_SUCCEEDED, tenant_id=2, summary="b")

        rows, total = service.query(tenant_id=1)
        assert total == 1
        assert rows[0].summary == "a"

    def test_a_tenant_cannot_see_system_events(self, db):
        """One customer must not learn about another's infrastructure."""
        service = AuditService(db)
        service.record(AuditAction.BACKUP_CREATED, tenant_id=1, summary="backup")
        service.record(AuditAction.LOGIN_SUCCEEDED, tenant_id=1, summary="login")

        rows, total = service.query(tenant_id=1)
        assert total == 1
        assert rows[0].summary == "login"

    def test_the_operator_sees_everything(self, db):
        service = AuditService(db)
        service.record(AuditAction.BACKUP_CREATED, tenant_id=1, summary="backup")
        service.record(AuditAction.LOGIN_SUCCEEDED, tenant_id=2, summary="login")
        _, total = service.query(unrestricted=True)
        assert total == 2

    def test_filters_compose(self, db):
        service = AuditService(db)
        service.record(AuditAction.LOGIN_FAILED, tenant_id=1, outcome="failure")
        service.record(AuditAction.LOGIN_SUCCEEDED, tenant_id=1)
        _, failures = service.query(tenant_id=1, outcome="failure")
        assert failures == 1

    def test_a_write_failure_never_breaks_the_caller(self, db):
        """An application that returns 500 because it could not record that it
        succeeded is worse than one that loses an audit row.

        `db.close()` is not enough to provoke this: SQLAlchemy simply opens a
        new connection on next use, so the write succeeded and my first
        attempt at this test asserted the wrong thing. Dropping the table is
        a failure the session cannot paper over.
        """
        from sqlalchemy import text

        db.execute(text("DROP TABLE audit_logs"))
        db.commit()
        assert AuditService(db).record(AuditAction.LOGIN_SUCCEEDED, tenant_id=1) is None

    def test_the_summary_aggregates(self, db):
        service = AuditService(db)
        for _ in range(3):
            service.record(AuditAction.LOGIN_SUCCEEDED, tenant_id=1)
        service.record(AuditAction.LOGIN_FAILED, tenant_id=1, outcome="failure")

        summary = service.summary(tenant_id=1, days=7)
        assert summary["total"] == 4
        assert summary["failures"] == 1
        assert summary["by_category"]["auth"] == 4

    def test_purge_keeps_critical_rows(self, db):
        """"We deleted the evidence on schedule" is not an answer for an
        auditor."""
        service = AuditService(db)
        service.record(AuditAction.LOGIN_SUCCEEDED, tenant_id=1)
        service.record(AuditAction.TOKEN_REUSE_DETECTED, tenant_id=1)
        for row in db.scalars(select(AuditLog)):
            row.occurred_at = datetime.now(timezone.utc) - timedelta(days=400)
        db.commit()

        assert service.purge(older_than_days=365) == 1
        remaining = db.scalars(select(AuditLog)).all()
        assert len(remaining) == 1
        assert remaining[0].severity == AuditSeverity.CRITICAL.value


# ===========================================================================
class TestJobQueue:
    def test_enqueue_and_claim(self, db):
        queue = JobQueue(db)
        job = queue.enqueue(JobKind.EMBEDDING, payload={"company_id": "x"})
        assert job.status == JobStatus.QUEUED.value

        claim = queue.claim("worker-1")
        assert claim is not None
        assert claim.job.id == job.id
        assert claim.job.status == JobStatus.RUNNING.value
        assert claim.job.attempts == 1

    def test_an_empty_queue_claims_nothing(self, db):
        assert JobQueue(db).claim("worker-1") is None

    def test_deduplication_returns_the_pending_job(self, db):
        """A user clicking Generate twice gets one report."""
        queue = JobQueue(db)
        first = queue.enqueue(JobKind.EMBEDDING, payload={"company_id": "x"})
        second = queue.enqueue(JobKind.EMBEDDING, payload={"company_id": "x"})
        assert first.id == second.id

    def test_the_same_work_is_new_again_once_finished(self, db):
        queue = JobQueue(db)
        first = queue.enqueue(JobKind.EMBEDDING, payload={"company_id": "x"})
        queue.claim("w")
        queue.succeed(first.id)
        second = queue.enqueue(JobKind.EMBEDDING, payload={"company_id": "x"})
        assert second.id != first.id

    def test_deduplication_can_be_switched_off(self, db):
        queue = JobQueue(db)
        a = queue.enqueue(JobKind.EMBEDDING, payload={"x": 1}, deduplicate=False)
        b = queue.enqueue(JobKind.EMBEDDING, payload={"x": 1}, deduplicate=False)
        assert a.id != b.id

    def test_priority_orders_the_queue(self, db):
        queue = JobQueue(db)
        queue.enqueue(JobKind.BACKUP)                 # BACKGROUND
        interactive = queue.enqueue(JobKind.REPORT_GENERATION, payload={"n": 1})
        assert queue.claim("w").job.id == interactive.id

    def test_two_workers_cannot_claim_the_same_job(self, db):
        """The conditional update is what makes this true on SQLite and
        Postgres alike, without SELECT … FOR UPDATE."""
        queue = JobQueue(db)
        queue.enqueue(JobKind.EMBEDDING, payload={"x": 1})
        assert JobQueue(db).claim("worker-1") is not None
        assert JobQueue(db).claim("worker-2") is None

    def test_a_future_job_is_not_claimed_early(self, db):
        queue = JobQueue(db)
        queue.enqueue(
            JobKind.EMBEDDING, payload={"x": 1},
            run_after=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        assert queue.claim("w") is None

    def test_an_expired_lease_is_reclaimable(self, db):
        """A worker that dies mid-job must not strand the work forever."""
        queue = JobQueue(db)
        queue.enqueue(JobKind.EMBEDDING, payload={"x": 1})
        claim = queue.claim("worker-1", lease_seconds=1)
        claim.job.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()

        reclaimed = queue.claim("worker-2")
        assert reclaimed is not None
        assert reclaimed.job.id == claim.job.id
        assert reclaimed.job.attempts == 2

    def test_reaping_returns_abandoned_jobs_to_the_queue(self, db):
        queue = JobQueue(db)
        queue.enqueue(JobKind.EMBEDDING, payload={"x": 1})
        claim = queue.claim("worker-1")
        claim.job.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()

        assert queue.reap_expired_leases() == 1
        assert queue.get(claim.job.id).status == JobStatus.QUEUED.value

    def test_success_records_a_result_and_a_duration(self, db):
        queue = JobQueue(db)
        job = queue.enqueue(JobKind.EMBEDDING, payload={"x": 1})
        queue.claim("w")
        done = queue.succeed(job.id, {"chunks": 12})
        assert done.status == JobStatus.SUCCEEDED.value
        assert done.result == {"chunks": 12}
        assert done.progress == 1.0
        assert done.locked_by is None

    def test_failure_schedules_a_retry(self, db):
        queue = JobQueue(db)
        job = queue.enqueue(JobKind.EMBEDDING, payload={"x": 1})
        queue.claim("w")
        failed = queue.fail(job.id, "boom")
        assert failed.status == JobStatus.FAILED.value
        # SQLite returns naive datetimes even from a timezone-aware column,
        # so the comparison must be made on common ground.
        scheduled = failed.run_after
        if scheduled.tzinfo is None:
            scheduled = scheduled.replace(tzinfo=timezone.utc)
        assert scheduled > datetime.now(timezone.utc)

    def test_exhausted_attempts_reach_the_dead_letter_queue(self, db):
        """A queue that quietly loses work is worse than one that visibly
        stalls."""
        queue = JobQueue(db)
        job = queue.enqueue(JobKind.REPORT_GENERATION, payload={"x": 1})

        queue.claim("w")
        queue.fail(job.id, "first")
        queue.requeue_ready() or queue.retry_ready_for_test() if False else None
        job.run_after = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
        queue.requeue_ready()
        queue.claim("w")
        final = queue.fail(job.id, "second")

        assert final.status == JobStatus.DEAD_LETTER.value

    def test_a_due_retry_is_requeued(self, db):
        queue = JobQueue(db)
        job = queue.enqueue(JobKind.EMBEDDING, payload={"x": 1})
        queue.claim("w")
        queue.fail(job.id, "boom")
        assert queue.claim("w") is None, "a job in backoff must not be claimed"

        job.run_after = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
        assert queue.requeue_ready() == 1
        assert queue.claim("w") is not None

    def test_a_dead_letter_can_be_replayed_with_fresh_attempts(self, db):
        queue = JobQueue(db)
        job = queue.enqueue(JobKind.EMBEDDING, payload={"x": 1})
        queue.claim("w")
        job.status = JobStatus.DEAD_LETTER.value
        db.commit()

        replayed = queue.retry(job.id)
        assert replayed.status == JobStatus.QUEUED.value
        assert replayed.attempts == 0

    def test_an_illegal_transition_raises(self, db):
        queue = JobQueue(db)
        job = queue.enqueue(JobKind.EMBEDDING, payload={"x": 1})
        queue.claim("w")
        queue.succeed(job.id)
        with pytest.raises(InvalidTransition):
            queue.cancel(job.id)

    def test_depth_reports_the_queue(self, db):
        queue = JobQueue(db)
        queue.enqueue(JobKind.EMBEDDING, payload={"x": 1})
        queue.enqueue(JobKind.BACKUP, payload={"x": 2})
        depth = queue.depth()
        assert depth.queued == 2
        assert depth.backlog == 2

    def test_purge_keeps_failures(self, db):
        """Failures and dead letters are the ones somebody may still need."""
        queue = JobQueue(db)
        ok = queue.enqueue(JobKind.EMBEDDING, payload={"x": 1})
        queue.claim("w")
        queue.succeed(ok.id)
        ok.finished_at = datetime.now(timezone.utc) - timedelta(days=30)

        bad = queue.enqueue(JobKind.EMBEDDING, payload={"x": 2})
        queue.claim("w")
        queue.fail(bad.id, "boom")
        bad.finished_at = datetime.now(timezone.utc) - timedelta(days=30)
        db.commit()

        assert queue.purge_completed(older_than_days=7) == 1
        assert queue.get(bad.id) is not None

    def test_an_unknown_job_raises(self, db):
        with pytest.raises(QueueError):
            JobQueue(db).succeed(9999)


class TestWorker:
    def test_a_worker_runs_a_job_to_success(self, db, session_factory):
        from app.services.platform.jobs import handlers
        from app.services.platform.jobs.worker import Worker

        original = handlers.HANDLERS.get(JobKind.EMBEDDING)
        handlers.HANDLERS[JobKind.EMBEDDING] = lambda _db, payload: {"ran": payload}
        try:
            queue = JobQueue(db)
            job = queue.enqueue(JobKind.EMBEDDING, payload={"company_id": "x"})
            worker = Worker(session_factory, worker_id="test")

            assert worker.run_once() is True
            assert queue.get(job.id).status == JobStatus.SUCCEEDED.value
            assert worker.processed == 1
        finally:
            if original:
                handlers.HANDLERS[JobKind.EMBEDDING] = original

    def test_a_handler_exception_fails_the_job_not_the_worker(self, db, session_factory):
        from app.services.platform.jobs import handlers
        from app.services.platform.jobs.worker import Worker

        original = handlers.HANDLERS.get(JobKind.EMBEDDING)

        def _explode(_db, _payload):
            raise RuntimeError("handler blew up")

        handlers.HANDLERS[JobKind.EMBEDDING] = _explode
        try:
            queue = JobQueue(db)
            job = queue.enqueue(JobKind.EMBEDDING, payload={"x": 1})
            worker = Worker(session_factory, worker_id="test")

            assert worker.run_once() is True
            assert worker.failed == 1
            row = queue.get(job.id)
            assert row.status in (JobStatus.FAILED.value, JobStatus.DEAD_LETTER.value)
            assert "handler blew up" in row.error
        finally:
            if original:
                handlers.HANDLERS[JobKind.EMBEDDING] = original

    def test_an_empty_queue_returns_false(self, db, session_factory):
        from app.services.platform.jobs.worker import Worker

        assert Worker(session_factory).run_once() is False

    def test_every_job_kind_has_a_handler(self):
        from app.services.platform.jobs.handlers import handler_for

        for kind in JobKind:
            assert callable(handler_for(kind))

    def test_the_scheduler_enqueues_due_work_once(self, db, session_factory):
        from app.services.platform.jobs.worker import Scheduler

        scheduler = Scheduler(session_factory)
        first = scheduler.tick()
        assert first["enqueued"] > 0
        # Immediately again: nothing is due, so nothing is enqueued.
        assert scheduler.tick()["enqueued"] == 0


# ===========================================================================
class TestObservability:
    def test_route_normalisation_collapses_identifiers(self):
        """Otherwise every company id is its own metric label and the table
        grows without bound."""
        assert normalise_route("/api/v1/companies/42") == "/api/v1/companies/{id}"
        assert normalise_route("/api/v1/reports/7/download/pdf") == (
            "/api/v1/reports/{id}/download/pdf"
        )
        assert normalise_route(
            "/api/v1/companies/8b1f2c3d-4e5a-6789-abcd-ef0123456789"
        ) == "/api/v1/companies/{uuid}"

    def test_a_static_route_is_untouched(self):
        assert normalise_route("/api/v1/companies/search") == "/api/v1/companies/search"

    def test_percentile_estimation_interpolates(self):
        histogram = [0] * (len(LATENCY_BUCKETS_MS) + 1)
        histogram[3] = 100      # everything in the 25-50 ms bucket
        p50 = estimate_percentile(histogram, 0.50)
        assert 25 < p50 < 50, "must interpolate, not return the boundary"

    def test_percentile_of_nothing_is_zero(self):
        assert estimate_percentile([0] * 12, 0.95) == 0.0

    def test_percentiles_are_ordered(self):
        histogram = [0] * (len(LATENCY_BUCKETS_MS) + 1)
        histogram[1], histogram[5], histogram[8] = 80, 15, 5
        p50 = estimate_percentile(histogram, 0.50)
        p95 = estimate_percentile(histogram, 0.95)
        p99 = estimate_percentile(histogram, 0.99)
        assert p50 <= p95 <= p99

    def test_the_collector_buffers_and_flushes(self, db):
        collector = MetricsCollector(flush_after=3)
        for _ in range(3):
            collector.observe(
                route="/api/v1/x", method="GET", status_code=200, duration_ms=12,
            )
        assert collector.should_flush
        assert collector.flush(db) == 1

        overview = MetricsService(db).overview(minutes=5)
        assert overview["requests"] == 3
        assert overview["errors"] == 0

    def test_server_errors_are_counted_separately(self, db):
        collector = MetricsCollector(flush_after=1)
        collector.observe(route="/x", method="GET", status_code=500, duration_ms=5)
        collector.observe(route="/x", method="GET", status_code=404, duration_ms=5)
        collector.flush(db)

        overview = MetricsService(db).overview(minutes=5)
        assert overview["requests"] == 2
        assert overview["errors"] == 1, "4xx is the caller's fault, not ours"

    def test_flushing_an_empty_buffer_is_a_no_op(self, db):
        assert MetricsCollector().flush(db) == 0

    def test_message_normalisation_groups_similar_errors(self):
        """"no company 41" and "no company 87" are one error, not two
        hundred."""
        a = normalise_message("no company 41 found")
        b = normalise_message("no company 87 found")
        assert a == b

    def test_repeat_errors_increment_rather_than_duplicate(self, db):
        """An error loop must not be able to fill the database with evidence
        of itself."""
        tracker = ErrorTracker(db)
        for _ in range(5):
            try:
                raise ValueError("the same failure")
            except ValueError as exc:
                tracker.capture(exc, route="/api/v1/x", method="GET")

        rows, total = tracker.list()
        assert total == 1
        assert rows[0].count == 5

    def test_different_errors_group_separately(self, db):
        tracker = ErrorTracker(db)
        try:
            raise ValueError("one")
        except ValueError as exc:
            tracker.capture(exc, route="/a")
        try:
            raise KeyError("two")
        except KeyError as exc:
            tracker.capture(exc, route="/b")
        _, total = tracker.list()
        assert total == 2

    def test_resolving_and_recurrence(self, db):
        """A recurrence reopens the error, whatever anyone marked.

        The two raises must come from the *same* source line. The fingerprint
        includes the top application frame — deliberately, so the same
        exception type from two different call sites stays two errors — which
        means a test that raises from two different lines is testing
        something else entirely. My first version did exactly that and failed
        for that reason, not because reopening was broken.
        """
        from app.models.platform import ErrorEvent

        tracker = ErrorTracker(db)

        def _boom():
            raise ValueError("boom")

        def _capture():
            try:
                _boom()
            except ValueError as exc:
                return tracker.capture(exc, route="/a")

        row = _capture()
        tracker.resolve(row.fingerprint, "operator")
        assert db.get(ErrorEvent, row.id).resolved_at is not None

        again = _capture()
        assert again.id == row.id, "the same failure must group, not duplicate"
        assert db.get(ErrorEvent, row.id).resolved_at is None
        assert db.get(ErrorEvent, row.id).count == 2

    def test_the_same_error_from_two_call_sites_stays_two_errors(self, db):
        """The counterpart to the test above: grouping must not be so coarse
        that two unrelated bugs with the same message become one."""
        tracker = ErrorTracker(db)

        def _one():
            raise ValueError("boom")

        def _two():
            raise ValueError("boom")

        for raiser in (_one, _two):
            try:
                raiser()
            except ValueError as exc:
                tracker.capture(exc, route="/a")

        _, total = tracker.list()
        assert total == 2

    def test_health_liveness_touches_nothing(self, db):
        from app.services.platform.observability import HealthService

        payload = HealthService(db).liveness()
        assert payload["status"] == "ok"
        assert payload["uptime_seconds"] >= 0

    def test_readiness_checks_every_dependency(self, db):
        from app.services.platform.observability import HealthService

        report = HealthService(db).readiness()
        names = {c.name for c in report.checks}
        assert {"database", "schema", "configuration", "queue"} <= names
        assert report.ready

    def test_the_queue_check_is_not_critical(self, db):
        """A stalled queue degrades the product; the API can still serve every
        synchronous request."""
        from app.services.platform.observability import HealthService

        queue_check = next(
            c for c in HealthService(db).readiness().checks if c.name == "queue"
        )
        assert queue_check.critical is False


# ===========================================================================
class TestBackup:
    def test_a_backup_is_written_hashed_and_verifiable(self, db, tmp_path):
        from app.core.config import settings

        original_dir, original_url = settings.BACKUP_DIR, settings.DATABASE_URL
        source = tmp_path / "source.db"

        # A real file-backed database, because the SQLite online backup API
        # cannot copy the in-memory one the fixture provides.
        file_engine = create_engine(f"sqlite+pysqlite:///{source}")
        Base.metadata.create_all(bind=file_engine)
        file_engine.dispose()

        settings.BACKUP_DIR = str(tmp_path / "backups")
        settings.DATABASE_URL = f"sqlite+pysqlite:///{source}"
        try:
            service = BackupService(db)
            record = service.create(label="test")
            assert record.status == "succeeded"
            assert record.size_bytes > 0
            assert len(record.checksum) == 64

            ok, detail = service.verify(record)
            assert ok, detail
        finally:
            settings.BACKUP_DIR, settings.DATABASE_URL = original_dir, original_url

    def test_a_corrupted_artefact_fails_verification(self, db, tmp_path):
        from app.core.config import settings
        from pathlib import Path

        original_dir, original_url = settings.BACKUP_DIR, settings.DATABASE_URL
        source = tmp_path / "source.db"
        file_engine = create_engine(f"sqlite+pysqlite:///{source}")
        Base.metadata.create_all(bind=file_engine)
        file_engine.dispose()

        settings.BACKUP_DIR = str(tmp_path / "backups")
        settings.DATABASE_URL = f"sqlite+pysqlite:///{source}"
        try:
            service = BackupService(db)
            record = service.create(label="test")
            Path(record.location).write_bytes(b"corrupted")

            ok, detail = service.verify(record)
            assert not ok
            assert "checksum" in detail
        finally:
            settings.BACKUP_DIR, settings.DATABASE_URL = original_dir, original_url

    def test_a_missing_artefact_fails_verification(self, db, tmp_path):
        from app.models.platform import BackupRecord

        record = BackupRecord(
            kind="database", location=str(tmp_path / "gone.sqlite.gz"),
            size_bytes=1, checksum="x" * 64, status="succeeded",
        )
        db.add(record)
        db.commit()
        ok, detail = BackupService(db).verify(record)
        assert not ok
        assert "missing" in detail

    def test_the_restore_command_is_provided_rather_than_a_button(self, db):
        """A one-click restore is a one-click way to lose a production
        database."""
        from app.models.platform import BackupRecord

        record = BackupRecord(
            kind="database", location="/tmp/x.sqlite.gz", status="succeeded",
        )
        command = BackupService(db).restore_command(record)
        assert "gunzip" in command and "integrity_check" in command

    def test_status_flags_a_stale_backup(self, db):
        status = BackupService(db).status()
        assert status["stale"] is True
        assert status["backup_count"] == 0


# ===========================================================================
class TestJobHandlers:
    """The handlers, driven directly.

    Coverage here was 18% after the first pass — the worker was tested, the
    work was not. These call each handler as the worker does, which is the
    only way to know that a background path still matches the service API it
    depends on. Module 9's `RatioService.margin_ratios()` episode is the
    precedent: a guessed method name survived review and was caught only by
    execution.
    """

    def test_every_kind_resolves_to_a_callable(self):
        from app.services.platform.jobs.handlers import HANDLERS, handler_for

        assert set(HANDLERS) == set(JobKind)
        for kind in JobKind:
            assert callable(handler_for(kind))

    def test_an_unregistered_kind_raises(self):
        from app.services.platform.jobs.handlers import handler_for

        with pytest.raises(KeyError):
            handler_for("not-a-kind")  # type: ignore[arg-type]

    def test_usage_rollup_reconciles_a_drifted_counter(self, db):
        """The counter is incremented in line with each consumption, so it
        should already be right. This proves the reconciliation catches it
        when it is not — a counter that disagrees with its own evidence is a
        billing dispute waiting to happen."""
        from app.services.platform.jobs.handlers import handle_usage_rollup

        tenant = _tenant(db)
        service = EntitlementService(db)
        service.sync_catalogue()
        service.consume(tenant.id, Quota.AI_CALLS, 7)

        counter = service.counter(tenant.id, Quota.AI_CALLS)
        counter.used = 999          # deliberate drift
        db.commit()

        result = handle_usage_rollup(db, {})
        assert result["counters_repaired"] >= 1
        db.refresh(counter)
        assert counter.used == 7, "the counter was not reconciled to the events"

    def test_usage_rollup_reports_no_repair_when_consistent(self, db):
        from app.services.platform.jobs.handlers import handle_usage_rollup

        tenant = _tenant(db)
        service = EntitlementService(db)
        service.sync_catalogue()
        service.consume(tenant.id, Quota.AI_CALLS, 3)

        assert handle_usage_rollup(db, {})["counters_repaired"] == 0

    def test_retention_sweep_reports_each_class_it_deleted(self, db):
        from app.services.platform.jobs.handlers import handle_retention_sweep

        result = handle_retention_sweep(db, {})
        assert {"metrics_deleted", "audit_deleted", "jobs_deleted"} <= set(result)
        assert all(isinstance(v, int) for v in result.values())

    def test_document_processing_skips_a_vanished_document(self, db):
        """A retry after the document was deleted must be a no-op, not a
        crash that dead-letters a job about nothing."""
        from app.services.platform.jobs.handlers import handle_document_processing

        result = handle_document_processing(db, {"document_id": 999_999})
        assert result["skipped"] is True

    def test_document_processing_fails_loudly_when_the_spool_is_gone(self, db):
        """Module 7 does not store the raw upload, so the bytes cannot be
        reconstructed. Failing is right; marking the document processed with
        nothing in it is not."""
        from app.models.document import Document
        from app.services.platform.jobs.handlers import handle_document_processing

        from app.models.company import Company

        company = Company(
            id="c-spool", name="Spool Co", ticker="SPOOL", exchange="NSE",
        )
        db.add(company)
        db.flush()

        document = Document(
            company_id=company.id, filename="x.pdf", doc_type="annual_report",
            file_format="pdf", content_hash="abc123", status="queued",
        )
        db.add(document)
        db.commit()

        with pytest.raises(FileNotFoundError):
            handle_document_processing(
                db, {"document_id": document.id, "spool_path": "/nowhere/x.pdf"},
            )

    def test_notification_delivery_marks_the_row_sent(self, db):
        from app.models.platform import Notification
        from app.services.platform.email import outbox
        from app.services.platform.jobs.handlers import handle_notification

        tenant = _tenant(db)
        notification = Notification(
            tenant_id=tenant.id, channel="email", topic="test",
            subject="Quota warning", body="You are at 82%.",
        )
        db.add(notification)
        db.commit()

        outbox.clear()
        result = handle_notification(
            db, {"notification_id": notification.id, "to": "a@acme.com"},
        )
        assert result["channel"] == "email"
        db.refresh(notification)
        assert notification.sent_at is not None
        assert notification.delivery_status == "sent"
        assert len(outbox) == 1

    def test_notification_is_not_sent_twice(self, db):
        from app.models.platform import Notification
        from app.services.platform.jobs.handlers import handle_notification

        tenant = _tenant(db)
        notification = Notification(
            tenant_id=tenant.id, channel="email", topic="t",
            subject="s", body="b", sent_at=datetime.now(timezone.utc),
        )
        db.add(notification)
        db.commit()

        assert handle_notification(db, {"notification_id": notification.id})["skipped"]

    def test_portfolio_refresh_tolerates_an_empty_estate(self, db):
        from app.services.platform.jobs.handlers import handle_portfolio_refresh

        result = handle_portfolio_refresh(db, {})
        assert result["portfolios"] == 0
        assert result["failed"] == 0

    def test_alert_evaluation_tolerates_an_empty_estate(self, db):
        from app.services.platform.jobs.handlers import handle_alert_evaluation

        assert handle_alert_evaluation(db, {})["portfolios"] == 0

    def test_metering_failure_does_not_fail_delivered_work(self, db):
        """A customer who received the work must not see it fail because the
        counter could not be written. The roll-up reconciles the drift."""
        from app.services.platform.jobs.handlers import _consume

        # tenant 999999 has no subscription, so metering raises internally.
        _consume(db, {"tenant_id": 999_999}, "ai_calls")   # must not raise

    def test_metering_is_skipped_when_a_job_has_no_tenant(self, db):
        from app.services.platform.jobs.handlers import _consume

        _consume(db, {}, "ai_calls")   # must not raise


class TestEmailService:
    def test_the_console_transport_records_every_message(self):
        from app.services.platform.email import EmailService, outbox

        outbox.clear()
        EmailService().send(to="a@b.com", subject="Subject", body="Body")
        assert len(outbox) == 1
        assert outbox.latest_for("a@b.com").subject == "Subject"

    def test_each_flow_produces_a_usable_link(self):
        """Verification, reset and magic link must all work end to end with no
        mail server — that is what makes the product explorable on a laptop."""
        from app.services.platform.email import EmailService, outbox

        service = EmailService()
        for method, address in [
            (service.send_verification, "verify@b.com"),
            (service.send_password_reset, "reset@b.com"),
            (service.send_magic_link, "magic@b.com"),
        ]:
            outbox.clear()
            method(to=address, name="Person", token="tok-123")
            message = outbox.latest_for(address)
            assert message is not None
            assert "tok-123" in message.body
            assert "http" in message.body

    def test_the_outbox_is_bounded(self):
        """An unbounded outbox in a long development session is a slow leak."""
        from app.services.platform.email import Outbox, SentMessage

        outbox = Outbox(capacity=5)
        for i in range(20):
            outbox.add(SentMessage(
                to=f"{i}@b.com", subject="s", body="b",
                at=datetime.now(timezone.utc), transport="console",
            ))
        assert len(outbox) == 5

    def test_an_invitation_names_the_organisation_and_inviter(self):
        from app.services.platform.email import EmailService, outbox

        outbox.clear()
        EmailService().send_invitation(
            to="new@b.com", name="New", organisation="Alpha Capital",
            inviter="Priya Nair", token="tok",
        )
        message = outbox.latest_for("new@b.com")
        assert "Alpha Capital" in message.body
        assert "Priya Nair" in message.subject


class TestRateLimiterStorage:
    def test_the_memory_limiter_counts_and_refuses(self):
        from app.domain.platform.limits import RateRule, RateScope
        from app.services.platform.rate_limit import MemoryRateLimiter

        limiter = MemoryRateLimiter()
        rule = RateRule(RateScope.IP, limit=3, window_seconds=60)

        allowed = sum(1 for _ in range(10) if limiter.check("k", rule).allowed)
        assert allowed == 3

    def test_keys_are_independent(self):
        from app.domain.platform.limits import RateRule, RateScope
        from app.services.platform.rate_limit import MemoryRateLimiter

        limiter = MemoryRateLimiter()
        rule = RateRule(RateScope.IP, limit=1, window_seconds=60)
        assert limiter.check("a", rule).allowed
        assert limiter.check("b", rule).allowed
        assert not limiter.check("a", rule).allowed

    def test_a_peek_does_not_consume(self):
        from app.domain.platform.limits import RateRule, RateScope
        from app.services.platform.rate_limit import MemoryRateLimiter

        limiter = MemoryRateLimiter()
        rule = RateRule(RateScope.IP, limit=1, window_seconds=60)
        limiter.check("k", rule, consume=False)
        assert limiter.check("k", rule).allowed

    def test_the_key_map_is_bounded(self):
        """An unbounded limiter map is a memory leak whose size an attacker
        controls, simply by varying their source address."""
        from app.domain.platform.limits import RateRule, RateScope
        from app.services.platform.rate_limit import MemoryRateLimiter

        limiter = MemoryRateLimiter(capacity=100)
        rule = RateRule(RateScope.IP, limit=10, window_seconds=60)
        for i in range(500):
            limiter.check(f"ip-{i}", rule)
        assert limiter.tracked_keys <= 100

    def test_reset_clears_state(self):
        from app.domain.platform.limits import RateRule, RateScope
        from app.services.platform.rate_limit import MemoryRateLimiter

        limiter = MemoryRateLimiter()
        rule = RateRule(RateScope.IP, limit=1, window_seconds=60)
        limiter.check("k", rule)
        limiter.reset("k")
        assert limiter.check("k", rule).allowed

    def test_a_plan_rate_becomes_a_rule_with_burst(self):
        from app.services.platform.rate_limit import plan_rule

        rule = plan_rule(600)
        assert rule.limit == 600
        assert rule.burst > 0
