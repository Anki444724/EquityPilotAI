"""Typed contracts for the platform API.

Two conventions worth stating, because they are load-bearing rather than
stylistic.

**Request models never accept a field the caller must not set.** There is no
`tenant_id` on any create model and no `role` a caller could smuggle past a
guard. The tenant comes from the principal; the role is validated against the
actor's seniority in the service. A schema that accepts a field the server
then ignores is an invitation for someone to make it stop being ignored.

**Response models never carry a secret.** `ApiKeyOut` has no plaintext;
`IssuedApiKeyOut` has it exactly once, at creation, and is the only model in
the file that does. `UserOut` has no password hash. This is enforced by test.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Generic, TypeVar

from pydantic import (
    BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator,
)

from app.domain.platform.identity import Role, UserStatus
from app.domain.platform.jobs import JobKind, JobStatus
from app.domain.platform.plans import (
    BillingPeriod, Feature, Limit, PlanTier, Quota, SubscriptionStatus,
)

T = TypeVar("T")


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Page(BaseModel, Generic[T]):
    """The pagination envelope every list endpoint returns.

    Total is included even though it costs a second query: without it the UI
    cannot render "showing 25 of 340" or size a pager, and every client ends
    up inventing its own way to guess.
    """

    items: list[T]
    total: int
    page: int = 1
    page_size: int = 50

    @property
    def pages(self) -> int:
        return max(1, -(-self.total // self.page_size))


# ===========================================================================
# Authentication
# ===========================================================================
class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=10, max_length=256)
    #: Repeated by the signup form. Checked server-side as well as in the
    #: browser: client-side validation is a convenience, never a control, and
    #: this endpoint is reachable without the form.
    confirm_password: str | None = Field(default=None, max_length=256)
    name: str = Field(min_length=1, max_length=160)
    username: str | None = Field(default=None, max_length=64)
    #: Optional organisation name for the self-serve path. Absent means one is
    #: derived from the email domain.
    organisation: str | None = Field(default=None, max_length=160)

    @model_validator(mode="after")
    def _passwords_match(self) -> "RegisterRequest":
        if self.confirm_password is not None and self.confirm_password != self.password:
            raise ValueError("Passwords do not match.")
        return self


class UsernameAvailability(BaseModel):
    """Whether a username may be claimed, and why not."""

    username: str
    available: bool
    problems: list[str] = Field(default_factory=list)


class LoginRequest(BaseModel):
    """Sign in with either identifier.

    `identifier` accepts an email address or a username. `email` is retained
    so existing clients keep working; exactly one is required.
    """

    identifier: str | None = Field(default=None, max_length=254)
    email: EmailStr | None = None
    password: str = Field(min_length=1, max_length=256)
    #: Extends the refresh cookie's lifetime. The access token's short life is
    #: unchanged — "remember me" must not mean "hold a valid bearer token for
    #: a month".
    remember_me: bool = False

    @model_validator(mode="after")
    def _one_identifier(self) -> "LoginRequest":
        if not (self.identifier or self.email):
            raise ValueError("Provide an email address or a username.")
        return self

    @property
    def login_id(self) -> str:
        return (self.identifier or str(self.email or "")).strip()


class MagicLinkRequest(BaseModel):
    email: EmailStr


class TokenRequest(BaseModel):
    token: str = Field(min_length=8, max_length=512)


class PasswordResetRequest(BaseModel):
    email: EmailStr


class PasswordResetConfirm(BaseModel):
    token: str = Field(min_length=8, max_length=512)
    password: str = Field(min_length=10, max_length=256)


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=10, max_length=256)


class RefreshRequest(BaseModel):
    """Optional in the body — the refresh token is normally an httpOnly
    cookie. The field exists for non-browser clients that cannot hold one."""

    refresh_token: str | None = None


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "Bearer"
    expires_in: int
    csrf_token: str
    #: Only returned to clients that cannot use cookies. A browser gets it as
    #: an httpOnly cookie instead, where script cannot read it.
    refresh_token: str | None = None


class SessionUser(BaseModel):
    """The `/auth/me` payload. Module 1's shape, extended."""

    id: str
    email: str
    name: str
    role: str
    is_dev_identity: bool = False
    avatar_url: str | None = None
    tenant_id: int | None = None
    tenant_slug: str | None = None
    tenant_name: str | None = None
    permissions: list[str] = Field(default_factory=list)
    provider: str = "dev"
    email_verified: bool = True
    mfa_enabled: bool = False
    username: str | None = None
    #: "Premium User" rather than "subscriber" — the product vocabulary.
    role_display: str | None = None
    #: Saved response language, or None to auto-detect on every request.
    #: Read from the existing `users.preferences` JSON column, so remembering
    #: a preference needs no migration.
    language: str | None = None


class AuthConfig(BaseModel):
    """What the sign-in page needs to render itself.

    Advertising only configured providers means the UI never shows a button
    that cannot work.
    """

    provider: str = "native"
    auth_enabled: bool
    native_auth: bool = False
    self_signup: bool = True
    oauth_providers: list[str] = Field(default_factory=list)
    magic_link: bool = True
    email_configured: bool = False
    password_min_length: int = 10
    publishable_key: str | None = None


class MessageResponse(BaseModel):
    """A deliberately vague acknowledgement.

    Registration, password reset and magic link all return this same body
    whether or not the address exists, so the endpoints cannot be used to
    enumerate accounts.
    """

    message: str
    #: Present only when the console email transport is active, so a developer
    #: can complete the flow without a mail server. Never populated when SMTP
    #: is configured.
    dev_link: str | None = None


class PasswordPolicyOut(BaseModel):
    min_length: int
    passphrase_length: int
    requires: list[str]
    message: str


# ===========================================================================
# Tenancy
# ===========================================================================
class TenantOut(ORMModel):
    id: int
    slug: str
    name: str
    status: str
    industry: str | None = None
    country: str
    timezone: str
    base_currency: str
    logo_url: str | None = None
    primary_colour: str | None = None
    report_disclaimer: str | None = None
    storage_bytes: int = 0
    member_count: int = 0
    trial_ends_at: datetime | None = None
    created_at: datetime


class TenantDetailOut(TenantOut):
    settings: dict[str, Any] = Field(default_factory=dict)
    subscription: "SubscriptionOut | None" = None
    storage: dict[str, int] = Field(default_factory=dict)


class TenantCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    slug: str | None = Field(default=None, max_length=64)
    tier: PlanTier = PlanTier.FREE
    industry: str | None = Field(default=None, max_length=80)
    country: str = Field(default="IN", min_length=2, max_length=2)


class TenantUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=160)
    industry: str | None = Field(default=None, max_length=80)
    country: str | None = Field(default=None, min_length=2, max_length=2)
    timezone: str | None = Field(default=None, max_length=64)
    base_currency: str | None = Field(default=None, max_length=8)
    logo_url: str | None = Field(default=None, max_length=500)
    primary_colour: str | None = Field(default=None, max_length=9)
    report_disclaimer: str | None = None


class TenantSettingsUpdate(BaseModel):
    settings: dict[str, Any]


class TenantSuspend(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


# ===========================================================================
# Members
# ===========================================================================
class UserOut(ORMModel):
    id: str
    email: str
    name: str
    role: str
    status: str
    avatar_url: str | None = None
    tenant_id: int | None = None
    email_verified_at: datetime | None = None
    last_login_at: datetime | None = None
    last_seen_at: datetime | None = None
    mfa_method: str = "none"
    created_at: datetime


class UserDetailOut(UserOut):
    permissions: list[str] = Field(default_factory=list)
    active_sessions: int = 0
    identities: list[str] = Field(default_factory=list)
    failed_login_count: int = 0
    locked_until: datetime | None = None


class InviteRequest(BaseModel):
    email: EmailStr
    name: str = Field(min_length=1, max_length=160)
    role: Role = Role.READ_ONLY

    @field_validator("role")
    @classmethod
    def _no_operator_by_invitation(cls, value: Role) -> Role:
        # A platform operator is minted by an operator, never by an
        # organisation admin filling in a form.
        if value is Role.SUPER_ADMIN:
            raise ValueError("Super Admin cannot be granted by invitation.")
        return value


class RoleUpdate(BaseModel):
    role: Role


class StatusUpdate(BaseModel):
    status: UserStatus


# ===========================================================================
# Plans and subscriptions
# ===========================================================================
class PlanOut(ORMModel):
    id: int
    tier: str
    name: str
    tagline: str | None = None
    price_monthly_inr: int
    price_annual_inr: int
    features: list[str]
    quotas: dict[str, int]
    limits: dict[str, int]
    trial_days: int
    is_public: bool
    sort_order: int


class PlanUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=64)
    tagline: str | None = Field(default=None, max_length=240)
    price_monthly_inr: int | None = Field(default=None, ge=0)
    price_annual_inr: int | None = Field(default=None, ge=0)
    features: list[str] | None = None
    quotas: dict[str, int] | None = None
    limits: dict[str, int] | None = None
    trial_days: int | None = Field(default=None, ge=0, le=365)
    is_public: bool | None = None


class SubscriptionOut(ORMModel):
    id: int
    tenant_id: int
    plan_tier: str
    status: str
    billing_period: str
    period_start: date
    period_end: date
    trial_ends_at: date | None = None
    cancel_at_period_end: bool = False
    cancelled_at: datetime | None = None
    provider: str | None = None


class SubscriptionChange(BaseModel):
    tier: PlanTier
    billing_period: BillingPeriod = BillingPeriod.MONTHLY


class SubscriptionOverrides(BaseModel):
    """Contract terms negotiated outside the standard plan.

    Operator-only. Enterprise deals always need them, and applying them here
    keeps `evaluate()` free of special cases.
    """

    quota_overrides: dict[str, int] | None = None
    limit_overrides: dict[str, int] | None = None
    feature_overrides: list[str] | None = None


class QuotaUsageOut(BaseModel):
    quota: str
    label: str
    unit: str
    used: int
    allowance: int
    remaining: int
    utilisation: float
    unlimited: bool
    exhausted: bool


class LimitOut(BaseModel):
    limit: str
    label: str
    used: int
    allowance: int
    unlimited: bool


class EntitlementsOut(BaseModel):
    """Everything the UI needs to decide what to show and what to gate."""

    tenant_id: int
    plan_tier: str
    plan_name: str
    status: str
    period_start: date
    period_end: date
    days_remaining: int
    trial_ends_at: date | None = None
    cancel_at_period_end: bool = False
    read_only: bool = False
    blocked: bool = False
    features: list[str]
    all_features: list[dict[str, Any]]
    quotas: list[QuotaUsageOut]
    limits: list[LimitOut]
    warnings: list[str] = Field(default_factory=list)


class EntitlementCheckOut(BaseModel):
    allowed: bool
    reason: str
    message: str = ""
    upgrade_to: str | None = None
    used: int | None = None
    allowance: int | None = None


# ===========================================================================
# API keys
# ===========================================================================
class ApiKeyOut(ORMModel):
    id: int
    name: str
    key_id: str
    prefix: str
    role: str
    #: `ierp_live_a1b2…` — enough to recognise, useless to replay.
    masked: str = ""
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    last_used_at: datetime | None = None
    last_used_ip: str | None = None
    call_count: int = 0
    created_by: str
    created_at: datetime


class IssuedApiKeyOut(BaseModel):
    """The only response in the platform that carries a secret."""

    key: ApiKeyOut
    plaintext: str
    warning: str = (
        "Copy this key now. It is stored only as a hash and cannot be "
        "retrieved again."
    )


class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    role: Role = Role.READ_ONLY
    expires_in_days: int = Field(default=365, ge=1, le=730)


# ===========================================================================
# Usage and analytics
# ===========================================================================
class UsagePointOut(BaseModel):
    date: date
    value: int


class UsageSeriesOut(BaseModel):
    quota: str
    label: str
    unit: str
    points: list[UsagePointOut]
    total: int


class UsageOverviewOut(BaseModel):
    tenant_id: int | None = None
    period_start: date
    period_end: date
    quotas: list[QuotaUsageOut]
    series: list[UsageSeriesOut]
    top_users: list[dict[str, Any]] = Field(default_factory=list)
    cost_usd: float = 0.0


# ===========================================================================
# Audit
# ===========================================================================
class AuditLogOut(ORMModel):
    id: int
    tenant_id: int | None = None
    action: str
    category: str
    severity: str
    outcome: str
    actor_id: str | None = None
    actor_email: str | None = None
    actor_role: str | None = None
    resource_type: str | None = None
    resource_id: str | None = None
    summary: str
    ip_address: str | None = None
    request_id: str | None = None
    meta: dict[str, Any] | None = None
    occurred_at: datetime


class AuditSummaryOut(BaseModel):
    days: int
    total: int
    failures: int
    by_category: dict[str, int]
    by_severity: dict[str, int]
    by_action: dict[str, int]
    daily: list[dict[str, Any]]


# ===========================================================================
# Jobs
# ===========================================================================
class JobOut(ORMModel):
    id: int
    tenant_id: int | None = None
    kind: str
    status: str
    priority: int
    attempts: int
    max_attempts: int
    progress: float
    stage: str | None = None
    error: str | None = None
    resource_type: str | None = None
    resource_id: str | None = None
    run_after: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: float = 0.0
    result: dict[str, Any] | None = None
    created_at: datetime


class JobEnqueue(BaseModel):
    kind: JobKind
    payload: dict[str, Any] = Field(default_factory=dict)


class QueueDepthOut(BaseModel):
    queued: int
    running: int
    failed: int
    dead_letter: int
    succeeded_24h: int
    backlog: int
    oldest_queued_seconds: float
    p50_duration_ms: float
    p95_duration_ms: float
    by_kind: dict[str, int]
    healthy: bool


class ScheduleOut(ORMModel):
    kind: str
    enabled: bool
    every_seconds: int
    last_run_at: datetime | None = None
    next_run_at: datetime | None = None
    last_status: str | None = None
    run_count: int = 0
    description: str = ""


# ===========================================================================
# Observability
# ===========================================================================
class HealthCheckOut(BaseModel):
    name: str
    ok: bool
    detail: str = ""
    duration_ms: float = 0.0
    critical: bool = True


class HealthOut(BaseModel):
    status: str
    version: str
    environment: str
    uptime_seconds: float
    ready: bool
    checks: list[HealthCheckOut]


class MetricsOverviewOut(BaseModel):
    window_minutes: int
    requests: int
    errors: int
    error_rate: float
    avg_ms: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    max_ms: float
    rpm: float


class RouteMetricOut(BaseModel):
    route: str
    method: str
    count: int
    errors: int
    error_rate: float
    avg_ms: float
    p95_ms: float
    max_ms: float


class ErrorEventOut(ORMModel):
    id: int
    fingerprint: str
    exc_type: str
    message: str
    route: str | None = None
    method: str | None = None
    count: int
    first_seen_at: datetime
    last_seen_at: datetime
    tenant_id: int | None = None
    last_request_id: str | None = None
    resolved_at: datetime | None = None
    stack: str | None = None


# ===========================================================================
# Backups
# ===========================================================================
class BackupOut(ORMModel):
    id: int
    kind: str
    location: str
    size_bytes: int
    checksum: str | None = None
    table_count: int
    row_count: int
    duration_ms: float
    status: str
    verified_at: datetime | None = None
    error: str | None = None
    finished_at: datetime | None = None


class BackupStatusOut(BaseModel):
    configured: bool
    directory: str
    backup_count: int
    latest_at: str | None = None
    latest_size_bytes: int = 0
    latest_verified_at: str | None = None
    age_hours: float | None = None
    stale: bool
    retention_count: int


class BackupVerifyOut(BaseModel):
    backup_id: int
    ok: bool
    detail: str
    restore_command: str


# ===========================================================================
# Notifications
# ===========================================================================
class NotificationOut(ORMModel):
    id: int
    topic: str
    subject: str
    body: str
    link: str | None = None
    channel: str
    read_at: datetime | None = None
    sent_at: datetime | None = None
    created_at: datetime


# ===========================================================================
# Admin dashboard
# ===========================================================================
class PlatformOverviewOut(BaseModel):
    """The operator console's headline figures."""

    tenants: int
    tenants_active: int
    tenants_trial: int
    tenants_past_due: int
    users: int
    users_active: int
    mrr_inr: int
    arr_inr: int
    tier_distribution: dict[str, int]
    storage_bytes: int
    documents: int
    reports: int
    portfolios: int
    ai_calls_30d: int
    requests_24h: int
    error_rate: float
    queue: QueueDepthOut
    health: str


class RbacMatrixOut(BaseModel):
    """The permission matrix, served rather than duplicated in the frontend.

    The admin panel renders whatever this returns. A matrix hard-coded in
    TypeScript would be a second source of truth that drifts the first time a
    permission is added.
    """

    roles: list[dict[str, Any]]
    permissions: list[dict[str, Any]]
    matrix: dict[str, list[str]]


TenantDetailOut.model_rebuild()


class LanguagePreferenceRequest(BaseModel):
    """Save or clear the caller's preferred response language."""

    #: "auto" | "english" | "hindi" | "hinglish" | a BCP-47 tag.
    #: "auto" clears the stored preference and returns to detection.
    language: str = Field(default="auto", max_length=32)


class LanguagePreferenceResponse(BaseModel):
    language: str | None = None
    detail: str = ""
