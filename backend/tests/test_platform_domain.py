"""Module 10 — the pure domain layer.

Every test here runs with no database, no FastAPI and no clock. That is the
point: an authorisation matrix, an entitlement rule and a retry schedule are
exactly the things that must be provable in isolation, because in production
they are the things nobody notices are wrong.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest

from app.domain.platform.audit import (
    AuditAction, AuditCategory, AuditSeverity, REDACTED, build,
    category_of, mask_email, mask_secret, redact, severity_of, tenant_visible,
)
from app.domain.platform.identity import (
    AuthProvider, AuthorizationError, CROSS_TENANT_PERMISSIONS,
    FEDERATED_PROVIDERS, LOGIN_ALLOWED_STATUSES, Permission, Principal,
    ROLE_DESCRIPTIONS, ROLE_LABELS, ROLE_ORDER, ROLE_PERMISSIONS, Role,
    TOKEN_TTL_SECONDS, TenantIsolationError, TokenType, UserStatus,
    has_permission, is_cross_tenant, outranks, permissions_for, seniority,
    write_permissions,
)
from app.domain.platform.jobs import (
    ACTIVE_STATUSES, DEFAULT_PRIORITY, InvalidTransition, JOB_LABELS, JobKind,
    JobPriority, JobStatus, QueueDepth, RETRY_POLICIES, RetryPolicy,
    SCHEDULES, ScheduleSpec, TERMINAL_STATUSES, TRANSITIONS, assert_transition,
    can_transition, idempotency_key, policy_for,
)
from app.domain.platform.limits import (
    COMMON_PASSWORDS, DEFAULT_PASSWORD_POLICY, DEFAULT_RULES, PasswordPolicy,
    RateRule, RateScope, is_valid_email, normalise_email, password_strength,
    slugify, sliding_window, validate_password,
)
from app.domain.platform.plans import (
    ALLOWED, BillingPeriod, DenialReason, Entitlement, Feature,
    FEATURE_LABELS, LIMIT_LABELS, Limit, PLAN_CATALOGUE, PLAN_ORDER, PlanTier,
    QUOTA_LABELS, QUOTA_UNITS, Quota, QuotaUsage, SubscriptionStatus,
    UNLIMITED, cheapest_plan_with, evaluate, is_unlimited, plan,
    upgrade_path,
)


# ===========================================================================
class TestRoleMatrix:
    def test_every_role_has_a_grant_a_label_and_a_description(self):
        for role in Role:
            assert role in ROLE_PERMISSIONS
            assert ROLE_LABELS[role]
            assert ROLE_DESCRIPTIONS[role]

    def test_role_order_covers_every_role_exactly_once(self):
        assert set(ROLE_ORDER) == set(Role)
        assert len(ROLE_ORDER) == len(Role) == 7

    def test_seniority_is_monotone_in_permissions(self):
        """A more senior role holds a superset of a more junior one.

        Asserted over the declared data rather than produced by inheritance,
        so a hand-edited matrix that accidentally removes a permission from a
        senior role fails here rather than in production.
        """
        for senior, junior in zip(ROLE_ORDER, ROLE_ORDER[1:]):
            assert ROLE_PERMISSIONS[junior] <= ROLE_PERMISSIONS[senior], (
                f"{junior} holds permissions {senior} does not: "
                f"{ROLE_PERMISSIONS[junior] - ROLE_PERMISSIONS[senior]}"
            )

    def test_guest_is_the_smallest_and_super_admin_the_largest(self):
        sizes = [len(ROLE_PERMISSIONS[r]) for r in ROLE_ORDER]
        assert sizes == sorted(sizes, reverse=True)
        assert ROLE_PERMISSIONS[Role.GUEST] == {Permission.COMPANY_READ}

    def test_only_super_admin_crosses_tenants(self):
        for role in Role:
            assert is_cross_tenant(role) is (role is Role.SUPER_ADMIN)

    def test_read_only_holds_no_write_permission(self):
        assert not (ROLE_PERMISSIONS[Role.READ_ONLY] & write_permissions())

    def test_guest_cannot_read_research_output(self):
        for permission in (
            Permission.VALUATION_READ, Permission.SCORING_READ,
            Permission.REPORT_READ, Permission.PORTFOLIO_READ,
        ):
            assert not has_permission(Role.GUEST, permission)

    def test_subscriber_consumes_but_does_not_author(self):
        assert has_permission(Role.SUBSCRIBER, Permission.AI_RUN)
        assert has_permission(Role.SUBSCRIBER, Permission.REPORT_GENERATE)
        assert not has_permission(Role.SUBSCRIBER, Permission.FORECAST_WRITE)
        assert not has_permission(Role.SUBSCRIBER, Permission.PORTFOLIO_WRITE)

    def test_researcher_writes_but_cannot_delete_others_work(self):
        assert has_permission(Role.RESEARCHER, Permission.FORECAST_WRITE)
        assert has_permission(Role.RESEARCHER, Permission.DOCUMENT_UPLOAD)
        assert not has_permission(Role.RESEARCHER, Permission.DOCUMENT_DELETE)
        assert not has_permission(Role.RESEARCHER, Permission.PORTFOLIO_DELETE)

    def test_analyst_cannot_administer(self):
        for permission in (
            Permission.MEMBER_MANAGE, Permission.SUBSCRIPTION_MANAGE,
            Permission.APIKEY_MANAGE, Permission.AUDIT_READ,
        ):
            assert not has_permission(Role.ANALYST, permission)

    def test_admin_cannot_reach_the_operator_console(self):
        for permission in CROSS_TENANT_PERMISSIONS:
            assert not has_permission(Role.ADMIN, permission)

    def test_permission_naming_is_consistent(self):
        for permission in Permission:
            assert re.fullmatch(r"[a-z_]+:[a-z_]+", permission.value), permission


class TestSeniority:
    def test_outranks_is_strict(self):
        assert outranks(Role.ADMIN, Role.ANALYST)
        assert not outranks(Role.ADMIN, Role.ADMIN)
        assert not outranks(Role.ANALYST, Role.ADMIN)

    def test_an_admin_cannot_administer_a_peer(self):
        """Two peers demoting each other is how an organisation loses its
        last administrator."""
        assert not outranks(Role.ADMIN, Role.ADMIN)

    def test_seniority_index_matches_declared_order(self):
        assert seniority(Role.SUPER_ADMIN) == 0
        assert seniority(Role.GUEST) == len(ROLE_ORDER) - 1


# ===========================================================================
class TestPrincipal:
    def _principal(self, **kwargs) -> Principal:
        base = dict(
            user_id="u1", email="a@b.com", name="A", role=Role.ANALYST,
            tenant_id=1,
        )
        base.update(kwargs)
        return Principal(**base)

    def test_id_aliases_user_id_for_modules_one_to_nine(self):
        """Modules 1-9 write `owner_id` from `.id`. If this alias ever
        diverges, every ownership row already in the database silently starts
        referring to a different person."""
        p = self._principal()
        assert p.id == p.user_id == "u1"

    def test_require_raises_the_domain_error_not_an_http_one(self):
        p = self._principal(role=Role.READ_ONLY)
        with pytest.raises(AuthorizationError) as exc:
            p.require(Permission.PORTFOLIO_WRITE)
        assert exc.value.permission is Permission.PORTFOLIO_WRITE
        assert "Read Only" in str(exc.value)

    def test_require_is_conjunctive(self):
        p = self._principal(role=Role.RESEARCHER)
        p.require(Permission.FORECAST_WRITE)
        with pytest.raises(AuthorizationError):
            p.require(Permission.FORECAST_WRITE, Permission.PORTFOLIO_DELETE)

    def test_require_any_is_disjunctive(self):
        p = self._principal(role=Role.RESEARCHER)
        p.require_any(Permission.PORTFOLIO_DELETE, Permission.FORECAST_WRITE)

    def test_read_only_tenant_blocks_writes_but_not_reads(self):
        p = self._principal(role=Role.ADMIN, tenant_read_only=True)
        assert p.can(Permission.COMPANY_READ)
        assert not p.can(Permission.COMPANY_WRITE)
        assert not p.can(Permission.MEMBER_MANAGE)

    def test_tenant_isolation_rejects_a_foreign_resource(self):
        p = self._principal(tenant_id=1)
        p.require_tenant(1)
        with pytest.raises(TenantIsolationError):
            p.require_tenant(2)

    def test_a_null_tenant_is_not_a_wildcard(self):
        """The dangerous reading of `tenant_id=None` is "matches anything".
        The safe one is "matches nothing"."""
        p = self._principal(tenant_id=None)
        with pytest.raises(TenantIsolationError):
            p.require_tenant(1)
        with pytest.raises(TenantIsolationError):
            p.require_tenant(None)

    def test_the_operator_crosses_every_boundary(self):
        p = self._principal(role=Role.SUPER_ADMIN, tenant_id=1)
        p.require_tenant(2)
        p.require_tenant(None)
        assert p.is_platform_operator

    def test_write_classification_covers_the_verbs_it_should(self):
        writes = write_permissions()
        assert Permission.PORTFOLIO_DELETE in writes
        assert Permission.DOCUMENT_UPLOAD in writes
        assert Permission.REPORT_GENERATE in writes
        assert Permission.AI_RUN in writes
        assert Permission.TENANT_CREATE in writes
        assert Permission.COMPANY_READ not in writes


class TestIdentityConstants:
    def test_only_active_users_may_sign_in(self):
        assert LOGIN_ALLOWED_STATUSES == {UserStatus.ACTIVE}
        assert UserStatus.PENDING not in LOGIN_ALLOWED_STATUSES

    def test_federated_providers_are_an_allow_list(self):
        assert FEDERATED_PROVIDERS == {AuthProvider.GOOGLE, AuthProvider.GITHUB}
        assert AuthProvider.PASSWORD not in FEDERATED_PROVIDERS

    def test_access_tokens_are_short_and_refresh_tokens_are_long(self):
        assert TOKEN_TTL_SECONDS[TokenType.ACCESS] <= 900
        assert TOKEN_TTL_SECONDS[TokenType.REFRESH] >= 7 * 24 * 3600

    def test_every_token_type_has_a_lifetime(self):
        for token_type in TokenType:
            assert TOKEN_TTL_SECONDS[token_type] > 0


# ===========================================================================
class TestPlanCatalogue:
    def test_the_four_named_plans_exist(self):
        assert set(PLAN_CATALOGUE) == {
            PlanTier.FREE, PlanTier.BASIC, PlanTier.PROFESSIONAL,
            PlanTier.ENTERPRISE,
        }

    def test_features_are_monotone_up_the_tiers(self):
        for lower, higher in zip(PLAN_ORDER, PLAN_ORDER[1:]):
            assert plan(lower).features <= plan(higher).features, (
                f"{higher} is missing features that {lower} includes"
            )

    def test_quotas_never_shrink_up_the_tiers(self):
        for lower, higher in zip(PLAN_ORDER, PLAN_ORDER[1:]):
            for quota in Quota:
                low, high = plan(lower).quota(quota), plan(higher).quota(quota)
                if is_unlimited(high):
                    continue
                assert high >= low, f"{quota}: {higher} ({high}) < {lower} ({low})"

    def test_price_increases_up_the_tiers(self):
        prices = [plan(t).price_monthly_inr for t in PLAN_ORDER]
        assert prices == sorted(prices)
        assert plan(PlanTier.FREE).price_monthly_inr == 0

    def test_annual_billing_is_cheaper_than_twelve_months(self):
        for tier in PLAN_ORDER:
            spec = plan(tier)
            if spec.price_monthly_inr == 0:
                continue
            assert spec.price_annual_inr < spec.price_monthly_inr * 12
            assert 0 < spec.annual_discount_pct < 0.5

    def test_a_missing_quota_is_zero_never_unlimited(self):
        """A quota nobody remembered to price must not be free."""
        spec = plan(PlanTier.FREE)
        assert spec.quota(Quota.AI_CALLS) == 0
        assert not is_unlimited(spec.quota(Quota.AI_CALLS))

    def test_enterprise_is_unmetered_where_advertised(self):
        spec = plan(PlanTier.ENTERPRISE)
        for quota in Quota:
            assert is_unlimited(spec.quota(quota))

    def test_enterprise_still_caps_api_keys_and_rate(self):
        """Not everything should be unlimited. Fifty live credentials is
        already a governance problem, and an unbounded rate limit is a
        denial-of-service vector against our own service."""
        spec = plan(PlanTier.ENTERPRISE)
        assert not is_unlimited(spec.limit(Limit.API_KEYS))
        assert not is_unlimited(spec.limit(Limit.RATE_LIMIT_PER_MINUTE))

    def test_every_feature_quota_and_limit_is_labelled(self):
        for feature in Feature:
            assert FEATURE_LABELS[feature]
        for quota in Quota:
            assert QUOTA_LABELS[quota] and QUOTA_UNITS[quota]
        for limit in Limit:
            assert LIMIT_LABELS[limit]

    def test_every_feature_is_sold_by_at_least_one_plan(self):
        """A flag that gates nothing is worse than no flag — it lets the
        pricing page promise something the code cannot withhold."""
        for feature in Feature:
            assert cheapest_plan_with(feature) is not None, feature

    def test_upgrade_path_is_strictly_upward(self):
        assert upgrade_path(PlanTier.FREE) == (
            PlanTier.BASIC, PlanTier.PROFESSIONAL, PlanTier.ENTERPRISE,
        )
        assert upgrade_path(PlanTier.ENTERPRISE) == ()

    def test_cheapest_plan_with_returns_the_lowest_tier(self):
        assert cheapest_plan_with(Feature.HISTORICAL_FINANCIALS) is PlanTier.FREE
        assert cheapest_plan_with(Feature.AI_ANALYST) is PlanTier.PROFESSIONAL
        assert cheapest_plan_with(Feature.SSO) is PlanTier.ENTERPRISE


class TestEntitlementDecision:
    PRO = PLAN_CATALOGUE[PlanTier.PROFESSIONAL]
    FREE = PLAN_CATALOGUE[PlanTier.FREE]

    def test_an_included_feature_is_allowed(self):
        assert evaluate(
            spec=self.PRO, subscription_status=SubscriptionStatus.ACTIVE,
            feature=Feature.AI_ANALYST,
        )

    def test_a_missing_feature_names_the_upgrade(self):
        decision = evaluate(
            spec=self.FREE, subscription_status=SubscriptionStatus.ACTIVE,
            feature=Feature.AI_ANALYST,
        )
        assert not decision
        assert decision.reason is DenialReason.FEATURE_NOT_IN_PLAN
        assert decision.upgrade_to is PlanTier.PROFESSIONAL
        assert "AI research analyst" in decision.message

    def test_quota_is_allowed_up_to_and_including_the_allowance(self):
        allowance = self.PRO.quota(Quota.REPORTS_GENERATED)
        assert evaluate(
            spec=self.PRO, subscription_status=SubscriptionStatus.ACTIVE,
            quota=Quota.REPORTS_GENERATED, quota_used=allowance - 1,
        )
        assert not evaluate(
            spec=self.PRO, subscription_status=SubscriptionStatus.ACTIVE,
            quota=Quota.REPORTS_GENERATED, quota_used=allowance,
        )

    def test_a_bulk_request_is_refused_if_it_would_overshoot(self):
        allowance = self.PRO.quota(Quota.REPORTS_GENERATED)
        assert not evaluate(
            spec=self.PRO, subscription_status=SubscriptionStatus.ACTIVE,
            quota=Quota.REPORTS_GENERATED,
            quota_used=allowance - 2, quota_requested=5,
        )

    def test_unlimited_quota_never_denies(self):
        assert evaluate(
            spec=PLAN_CATALOGUE[PlanTier.ENTERPRISE],
            subscription_status=SubscriptionStatus.ACTIVE,
            quota=Quota.AI_CALLS, quota_used=10_000_000,
        )

    def test_denial_precedence_names_the_real_obstacle(self):
        """A suspended tenant is told it is suspended, not that it is out of
        report credits. The user's next action differs entirely."""
        decision = evaluate(
            spec=self.FREE, subscription_status=SubscriptionStatus.CANCELLED,
            tenant_blocked=True,
            feature=Feature.AI_ANALYST, quota=Quota.AI_CALLS, quota_used=999,
        )
        assert decision.reason is DenialReason.TENANT_SUSPENDED

    def test_inactive_subscription_outranks_a_feature_denial(self):
        decision = evaluate(
            spec=self.PRO, subscription_status=SubscriptionStatus.PAST_DUE,
            feature=Feature.AI_ANALYST,
        )
        assert decision.reason is DenialReason.SUBSCRIPTION_INACTIVE

    def test_read_only_is_distinct_from_suspended(self):
        decision = evaluate(
            spec=self.PRO, subscription_status=SubscriptionStatus.ACTIVE,
            tenant_read_only=True, feature=Feature.AI_ANALYST,
        )
        assert decision.reason is DenialReason.TENANT_READ_ONLY

    def test_a_trial_is_entitled(self):
        assert evaluate(
            spec=self.PRO, subscription_status=SubscriptionStatus.TRIALING,
            feature=Feature.AI_ANALYST,
        )

    def test_limits_are_reported_with_their_usage(self):
        decision = evaluate(
            spec=self.FREE, subscription_status=SubscriptionStatus.ACTIVE,
            limit=Limit.SEATS, limit_used=1,
        )
        assert not decision
        assert decision.reason is DenialReason.LIMIT_REACHED
        assert decision.used == 1
        assert decision.allowance == 1
        assert decision.upgrade_to is PlanTier.BASIC

    def test_entitlement_is_truthy(self):
        assert bool(ALLOWED) is True
        assert bool(Entitlement(allowed=False)) is False


class TestQuotaUsage:
    def test_utilisation_and_remaining(self):
        usage = QuotaUsage(Quota.AI_CALLS, used=150, allowance=200)
        assert usage.utilisation == 0.75
        assert usage.remaining == 50
        assert not usage.exhausted

    def test_exhausted_at_the_allowance(self):
        assert QuotaUsage(Quota.AI_CALLS, used=200, allowance=200).exhausted

    def test_unlimited_reports_zero_utilisation_not_undefined(self):
        usage = QuotaUsage(Quota.AI_CALLS, used=99_999, allowance=UNLIMITED)
        assert usage.unlimited
        assert usage.utilisation == 0.0
        assert usage.remaining == UNLIMITED
        assert not usage.exhausted

    def test_a_zero_allowance_does_not_divide_by_zero(self):
        usage = QuotaUsage(Quota.AI_CALLS, used=0, allowance=0)
        assert usage.utilisation == 0.0
        assert usage.exhausted


# ===========================================================================
class TestRateLimitArithmetic:
    RULE = RateRule(RateScope.IP, limit=10, window_seconds=60)

    def test_an_empty_window_allows(self):
        decision = sliding_window(
            rule=self.RULE, previous_count=0, current_count=0,
            elapsed_in_window=0,
        )
        assert decision.allowed
        assert decision.remaining == 9

    def test_a_full_current_window_denies(self):
        decision = sliding_window(
            rule=self.RULE, previous_count=0, current_count=10,
            elapsed_in_window=30,
        )
        assert not decision.allowed
        assert decision.retry_after > 0

    def test_the_previous_window_decays_linearly(self):
        """Halfway through the window, half the previous count still counts.

        This is the whole reason for the weighted approximation: a fixed
        window lets a caller send a full allowance either side of the boundary
        and get double the intended rate.
        """
        early = sliding_window(
            rule=self.RULE, previous_count=10, current_count=0,
            elapsed_in_window=1,
        )
        late = sliding_window(
            rule=self.RULE, previous_count=10, current_count=0,
            elapsed_in_window=54,
        )
        # 1 s in: 10 × (1 − 1/60) = 9.83 carried over; +1 = 10.83 > 10.
        assert early.used == pytest.approx(9.833, abs=0.01)
        assert not early.allowed
        # 54 s in: only 10 × (1 − 54/60) = 1.0 still counts.
        assert late.used == pytest.approx(1.0, abs=0.01)
        assert late.allowed
        assert early.used > late.used

    def test_the_boundary_burst_is_prevented(self):
        """Ten in the last instant of one window, then ten in the first
        instant of the next, must not both be allowed."""
        decision = sliding_window(
            rule=self.RULE, previous_count=10, current_count=0,
            elapsed_in_window=0.1,
        )
        assert not decision.allowed

    def test_burst_raises_capacity(self):
        rule = RateRule(RateScope.IP, limit=10, window_seconds=60, burst=5)
        assert rule.capacity == 15
        assert sliding_window(
            rule=rule, previous_count=0, current_count=12, elapsed_in_window=30,
        ).allowed

    def test_headers_are_well_formed(self):
        allowed = sliding_window(
            rule=self.RULE, previous_count=0, current_count=0, elapsed_in_window=0,
        ).headers()
        assert allowed["X-RateLimit-Limit"] == "10"
        assert "Retry-After" not in allowed

        denied = sliding_window(
            rule=self.RULE, previous_count=0, current_count=10, elapsed_in_window=1,
        ).headers()
        assert denied["X-RateLimit-Remaining"] == "0"
        assert int(denied["Retry-After"]) >= 1

    def test_login_is_the_strictest_default_rule(self):
        login = DEFAULT_RULES["auth.login"]
        assert login.per_second < DEFAULT_RULES["default"].per_second
        assert login.limit <= 10

    def test_every_default_rule_is_sane(self):
        for name, rule in DEFAULT_RULES.items():
            assert rule.limit > 0, name
            assert rule.window_seconds > 0, name
            assert rule.burst >= 0, name


# ===========================================================================
class TestPasswordPolicy:
    def test_a_short_password_is_rejected(self):
        problems = validate_password("Ab1defg")
        assert any("at least" in p for p in problems)

    def test_all_problems_are_returned_not_just_the_first(self):
        """A user should fix their password in one attempt, not five."""
        assert len(validate_password("abc")) >= 2

    def test_a_long_passphrase_needs_no_character_classes(self):
        """`correct horse battery staple` is stronger than `Tr0ub4dor&3` and
        far easier to remember. Current NIST guidance, and the reason length
        outranks classes here."""
        assert validate_password("correct horse battery staple") == []

    def test_a_short_password_does_need_them(self):
        problems = validate_password("alllowercase")
        assert any("upper-case" in p for p in problems)
        assert any("digit" in p for p in problems)

    def test_breached_passwords_are_refused_at_any_length(self):
        for candidate in ("password123", "Password123", "  password123  "):
            problems = validate_password(candidate)
            assert any("breach" in p for p in problems), candidate

    def test_a_password_containing_the_email_is_refused(self):
        problems = validate_password(
            "priya.nair-Strong1", email="priya.nair@example.com",
        )
        assert any("email" in p for p in problems)

    def test_a_short_email_local_part_does_not_over_match(self):
        """A two-character local part would otherwise reject almost every
        password that happened to contain those letters."""
        assert validate_password("Sunflower42x", email="ab@example.com") == []

    def test_strength_is_monotone_in_length(self):
        assert password_strength("Ab1defghij") < password_strength("Ab1defghijklmnopqr")

    def test_a_breached_password_scores_near_zero(self):
        assert password_strength("password123") <= 0.1

    def test_an_empty_password_scores_zero_and_never_hashes(self):
        assert password_strength("") == 0.0

    def test_the_policy_is_configurable(self):
        strict = PasswordPolicy(min_length=16, require_symbol=True, passphrase_length=99)
        assert validate_password("Abcdefghij123456", policy=strict)


class TestEmailAndSlug:
    @pytest.mark.parametrize("address", [
        "a@b.co", "priya.nair@democapital.in", "x+tag@sub.domain.org",
    ])
    def test_valid_addresses(self, address):
        assert is_valid_email(address)

    @pytest.mark.parametrize("address", [
        "", "no-at-sign", "@nolocal.com", "a@b", "a b@c.com", "a@@b.com",
        "a@b.", "x" * 250 + "@example.com",
    ])
    def test_invalid_addresses(self, address):
        assert not is_valid_email(address)

    def test_normalisation_prevents_case_duplicate_accounts(self):
        assert normalise_email("  Priya.Nair@Example.COM ") == "priya.nair@example.com"

    @pytest.mark.parametrize("raw,expected", [
        ("Demo Capital Advisors", "demo-capital-advisors"),
        ("  Northwind  Research LLP  ", "northwind-research-llp"),
        ("Acme & Co. (India)", "acme-co-india"),
        ("---", ""),
    ])
    def test_slugify(self, raw, expected):
        assert slugify(raw) == expected

    def test_slug_has_no_leading_or_trailing_hyphen(self):
        for raw in ("!!! Hello !!!", "-a-", "  x  "):
            slug = slugify(raw)
            assert not slug.startswith("-") and not slug.endswith("-")


# ===========================================================================
class TestAuditVocabulary:
    def test_every_action_declares_a_category_and_severity(self):
        for action in AuditAction:
            assert isinstance(category_of(action), AuditCategory)
            assert isinstance(severity_of(action), AuditSeverity)

    def test_security_relevant_actions_are_not_logged_as_info(self):
        assert severity_of(AuditAction.TOKEN_REUSE_DETECTED) is AuditSeverity.CRITICAL
        assert severity_of(AuditAction.TENANT_ISOLATION_VIOLATION) is AuditSeverity.CRITICAL
        assert severity_of(AuditAction.LOGIN_FAILED) is AuditSeverity.WARNING
        assert severity_of(AuditAction.ACCESS_DENIED) is AuditSeverity.WARNING

    def test_system_events_are_hidden_from_tenants(self):
        assert not tenant_visible(AuditAction.BACKUP_CREATED)
        assert tenant_visible(AuditAction.LOGIN_SUCCEEDED)

    def test_action_names_are_dotted_and_namespaced(self):
        for action in AuditAction:
            assert re.fullmatch(r"[a-z_]+(\.[a-z_]+)+", action.value), action


class TestRedaction:
    def test_credential_keys_are_removed_whatever_the_value(self):
        out = redact({
            "password": "hunter2", "api_key": "x", "TOKEN": "y",
            "Authorization": "Bearer z", "session_id": "s",
        })
        assert all(v == REDACTED for v in out.values())

    def test_redaction_recurses(self):
        out = redact({"a": {"b": [{"secret": "s", "safe": 1}]}})
        assert out["a"]["b"][0]["secret"] == REDACTED
        assert out["a"]["b"][0]["safe"] == 1

    def test_key_matching_is_case_and_separator_insensitive(self):
        out = redact({
            "apiKey": 1, "API-KEY": 2, "openrouter_api_key": 3, "X-Api-Key": 4,
        })
        assert all(v == REDACTED for v in out.values())

    def test_credential_shaped_values_are_caught_under_innocent_keys(self):
        """Deny-by-default on the key name is the primary rule; this is the
        second net, for the field somebody named `note`."""
        out = redact({
            "note": "sk-abcdefghijklmnopqrstuvwx",
            "ref": "ierp_live_abcdefgh12345678",
            "other": "ghp_abcdefghijklmnopqrstuvwxyz012345",
        })
        assert out["note"] == REDACTED
        assert out["ref"] == REDACTED
        assert out["other"] == REDACTED

    def test_ordinary_values_survive_intact(self):
        out = redact({"ticker": "BHARATCP", "price": 268.0, "count": 3})
        assert out == {"ticker": "BHARATCP", "price": 268.0, "count": 3}

    def test_long_strings_are_truncated(self):
        out = redact({"body": "x" * 5000})
        assert len(out["body"]) < 600

    def test_deep_nesting_terminates(self):
        """A logging call must never be able to hang the process."""
        payload: dict = {"v": 1}
        for _ in range(40):
            payload = {"nested": payload}
        assert redact(payload) is not None

    def test_build_redacts_metadata(self):
        event = build(
            AuditAction.APIKEY_CREATED, at=datetime.now(timezone.utc),
            metadata={"api_key": "ierp_live_secret", "name": "CI"},
        )
        assert event.metadata["api_key"] == REDACTED
        assert event.metadata["name"] == "CI"

    def test_build_derives_category_and_severity(self):
        event = build(AuditAction.LOGIN_FAILED, at=datetime.now(timezone.utc))
        assert event.category is AuditCategory.AUTH
        assert event.severity is AuditSeverity.WARNING
        assert event.is_security_relevant


class TestMasking:
    def test_email_masking_keeps_the_domain(self):
        # First and last character survive; the seven-character local part
        # therefore leaves five stars.
        assert mask_email("analyst@example.com") == "a*****t@example.com"

    def test_masking_does_not_leak_the_local_part_length_wrongly(self):
        for local in ("abc", "abcdefghij"):
            masked = mask_email(f"{local}@x.com")
            assert masked.startswith(local[0])
            assert masked.split("@")[0].endswith(local[-1])

    def test_short_local_parts_are_still_masked(self):
        assert mask_email("ab@x.com") == "a*@x.com"

    def test_a_non_address_is_fully_redacted(self):
        assert mask_email("not-an-email") == REDACTED

    def test_secret_masking_keeps_only_the_tail(self):
        assert mask_secret("abcdefghij") == "******ghij"
        assert mask_secret("abc") == "***"


# ===========================================================================
class TestJobStateMachine:
    def test_every_status_has_a_transition_rule(self):
        for status in JobStatus:
            assert status in TRANSITIONS

    def test_terminal_states_are_terminal(self):
        assert TRANSITIONS[JobStatus.SUCCEEDED] == frozenset()
        assert TRANSITIONS[JobStatus.CANCELLED] == frozenset()

    def test_a_dead_letter_can_be_replayed_by_a_human(self):
        assert can_transition(JobStatus.DEAD_LETTER, JobStatus.QUEUED)

    def test_illegal_transitions_raise(self):
        with pytest.raises(InvalidTransition):
            assert_transition(JobStatus.SUCCEEDED, JobStatus.RUNNING)
        with pytest.raises(InvalidTransition):
            assert_transition(JobStatus.QUEUED, JobStatus.SUCCEEDED)

    def test_a_running_job_can_reach_every_outcome(self):
        for target in (
            JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.DEAD_LETTER,
            JobStatus.CANCELLED,
        ):
            assert can_transition(JobStatus.RUNNING, target)

    def test_active_and_terminal_partition_the_states(self):
        assert ACTIVE_STATUSES | TERMINAL_STATUSES == set(JobStatus)
        assert not (ACTIVE_STATUSES & TERMINAL_STATUSES)


class TestRetryPolicy:
    def test_backoff_grows_exponentially(self):
        policy = RetryPolicy(base_seconds=5, factor=4, jitter_fraction=0)
        assert policy.backoff_seconds(1) == 5
        assert policy.backoff_seconds(2) == 20
        assert policy.backoff_seconds(3) == 80

    def test_backoff_is_capped(self):
        policy = RetryPolicy(base_seconds=5, factor=10, max_seconds=100, jitter_fraction=0)
        assert policy.backoff_seconds(9) == 100

    def test_jitter_is_deterministic_for_a_seed(self):
        """Deterministic so the schedule is testable; varying so a thousand
        simultaneous failures do not retry in lockstep."""
        policy = RetryPolicy(jitter_fraction=0.2)
        assert policy.backoff_seconds(1, "job-1") == policy.backoff_seconds(1, "job-1")
        assert policy.backoff_seconds(1, "job-1") != policy.backoff_seconds(1, "job-2")

    def test_jitter_stays_within_its_band(self):
        policy = RetryPolicy(base_seconds=100, factor=1, jitter_fraction=0.2)
        for i in range(50):
            value = policy.backoff_seconds(1, f"job-{i}")
            assert 80 <= value <= 120

    def test_attempt_zero_is_rejected(self):
        with pytest.raises(ValueError):
            RetryPolicy().backoff_seconds(0)

    def test_exhaustion_goes_to_the_dead_letter_queue(self):
        policy = RetryPolicy(max_attempts=3)
        assert policy.outcome_after_failure(1) is JobStatus.FAILED
        assert policy.outcome_after_failure(2) is JobStatus.FAILED
        assert policy.outcome_after_failure(3) is JobStatus.DEAD_LETTER

    def test_next_run_is_in_the_future(self):
        now = datetime.now(timezone.utc)
        assert RetryPolicy().next_run_at(now, 1, "s") > now

    def test_every_kind_has_a_policy_a_priority_and_a_label(self):
        for kind in JobKind:
            assert isinstance(policy_for(kind), RetryPolicy)
            assert kind in DEFAULT_PRIORITY
            assert JOB_LABELS[kind]

    def test_notifications_retry_more_than_reports(self):
        """A failed notification is usually a transient network hiccup. A
        failed report render will fail identically the second time."""
        assert (
            RETRY_POLICIES[JobKind.NOTIFICATION].max_attempts
            > RETRY_POLICIES[JobKind.REPORT_GENERATION].max_attempts
        )

    def test_interactive_work_outranks_housekeeping(self):
        assert (
            DEFAULT_PRIORITY[JobKind.REPORT_GENERATION]
            < DEFAULT_PRIORITY[JobKind.BACKUP]
        )
        assert JobPriority.INTERACTIVE < JobPriority.BACKGROUND


class TestIdempotencyKey:
    def test_the_same_work_hashes_the_same(self):
        a = idempotency_key(JobKind.EMBEDDING, 1, {"document_id": 5})
        b = idempotency_key(JobKind.EMBEDDING, 1, {"document_id": 5})
        assert a == b

    def test_key_order_does_not_matter(self):
        a = idempotency_key(JobKind.EMBEDDING, 1, {"a": 1, "b": 2})
        b = idempotency_key(JobKind.EMBEDDING, 1, {"b": 2, "a": 1})
        assert a == b

    def test_different_tenants_never_collide(self):
        a = idempotency_key(JobKind.EMBEDDING, 1, {"document_id": 5})
        b = idempotency_key(JobKind.EMBEDDING, 2, {"document_id": 5})
        assert a != b

    def test_different_kinds_never_collide(self):
        a = idempotency_key(JobKind.EMBEDDING, 1, {"x": 1})
        b = idempotency_key(JobKind.BACKUP, 1, {"x": 1})
        assert a != b


class TestSchedules:
    def test_a_schedule_that_has_never_run_is_due(self):
        spec = ScheduleSpec(JobKind.BACKUP, 3600, "d")
        assert spec.due(None, datetime.now(timezone.utc))

    def test_a_recent_run_is_not_due(self):
        now = datetime.now(timezone.utc)
        spec = ScheduleSpec(JobKind.BACKUP, 3600, "d")
        assert not spec.due(now - timedelta(minutes=30), now)
        assert spec.due(now - timedelta(minutes=61), now)

    def test_a_disabled_schedule_is_never_due(self):
        spec = ScheduleSpec(JobKind.BACKUP, 1, "d", enabled=False)
        assert not spec.due(None, datetime.now(timezone.utc))

    def test_the_standing_schedule_is_coherent(self):
        kinds = [s.kind for s in SCHEDULES]
        assert len(kinds) == len(set(kinds)), "a kind is scheduled twice"
        for spec in SCHEDULES:
            assert spec.every_seconds > 0
            assert spec.description


class TestQueueDepth:
    def test_backlog_sums_the_unfinished(self):
        depth = QueueDepth(queued=3, running=2, failed=1)
        assert depth.backlog == 6

    def test_a_dead_letter_makes_the_queue_unhealthy(self):
        assert not QueueDepth(dead_letter=1).is_healthy

    def test_a_long_wait_makes_the_queue_unhealthy(self):
        assert not QueueDepth(queued=1, oldest_queued_seconds=600).is_healthy
        assert QueueDepth(queued=1, oldest_queued_seconds=10).is_healthy

    def test_an_empty_queue_is_healthy(self):
        assert QueueDepth().is_healthy


class TestUsernameIdentity:
    """AUTH-001: username as a second login identifier."""

    def test_reserved_and_malformed_usernames_are_refused(self):
        from app.domain.platform.limits import username_problems

        assert username_problems("ankitsingh") == []
        assert username_problems("ok_name-1") == []
        assert any("reserved" in p for p in username_problems("admin"))
        assert username_problems("ab")            # too short
        assert username_problems("has space")
        assert username_problems("a" * 70)        # too long
        assert any("@" in p for p in username_problems("looks@email"))

    def test_usernames_are_case_folded(self):
        """"AnkitSingh" and "ankitsingh" must be one identity, not two.

        A case-sensitive unique index would let both be claimed, which is an
        impersonation vector rather than a convenience.
        """
        from app.domain.platform.limits import normalise_username

        assert normalise_username("  AnkitSingh ") == "ankitsingh"
        assert normalise_username("ANKITSINGH") == normalise_username("ankitsingh")

    def test_login_accepts_either_identifier(self):
        from app.schemas.platform import LoginRequest

        assert LoginRequest(identifier="ankitsingh", password="x").login_id == "ankitsingh"
        assert LoginRequest(email="a@b.com", password="x").login_id == "a@b.com"
        with pytest.raises(ValueError):
            LoginRequest(password="x")          # neither supplied

    def test_signup_rejects_mismatched_confirmation(self):
        from app.schemas.platform import RegisterRequest

        with pytest.raises(ValueError):
            RegisterRequest(
                email="a@b.com", password="Str0ng!Passw0rd",
                confirm_password="different", name="A",
            )

    def test_admin_password_is_held_to_the_same_policy(self):
        """The account most worth attacking gets no exemption."""
        from app.domain.platform.limits import (
            DEFAULT_PASSWORD_POLICY, validate_password,
        )

        assert validate_password(
            "Ankit@987", policy=DEFAULT_PASSWORD_POLICY,
            email="ankitsingh835141@gmail.com",
        ), "a 9-character password must not pass a 10-character minimum"
