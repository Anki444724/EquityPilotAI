"""Persistence for the SaaS platform layer.

Fourteen tables. The organising principle is that **`tenant_id` is the first
column of every business table and the first predicate of every query**. Rows
belonging to different organisations live in the same tables, separated by a
column and by a query filter that is applied in one place (`TenantScope`)
rather than remembered at each call site.

Why a shared schema rather than a schema or database per tenant: the platform
must run on SQLite with no infrastructure and on a single Postgres instance on
Railway, and it must be able to answer cross-tenant operator questions ("which
organisations are near their AI quota?") without fanning out over N databases.
The cost is that isolation is a discipline rather than a wall, which is why it
is enforced by a dependency, asserted by tests, and audited when violated.

Notes on the sensitive columns:

* **No password is stored.** `User.password_hash` holds an Argon2id digest.
* **No token is stored.** `RefreshToken.token_hash` and `ApiKey.key_hash` hold
  SHA-256 digests; the plaintext is shown once at creation and never again.
* **Secrets at rest are enveloped.** `TenantSecret.ciphertext` is Fernet-style
  AES; see `services/platform/crypto.py`.
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Boolean, Date, DateTime, Float, ForeignKey, Index, Integer, JSON,
    LargeBinary, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


# ===========================================================================
# Tenancy
# ===========================================================================
class Tenant(Base):
    """An organisation. The unit of isolation, billing and configuration."""

    __tablename__ = "tenants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    #: URL-safe identifier. Stable for the tenant's lifetime — renaming the
    #: organisation does not change it, because links and API clients depend
    #: on it.
    slug: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="trial", nullable=False, index=True)

    #: Free-text, shown in the operator console.
    industry: Mapped[str | None] = mapped_column(String(80))
    country: Mapped[str] = mapped_column(String(2), default="IN")
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Kolkata")
    base_currency: Mapped[str] = mapped_column(String(8), default="INR")

    #: White-label branding, applied to reports when the plan allows it.
    logo_url: Mapped[str | None] = mapped_column(String(500))
    primary_colour: Mapped[str | None] = mapped_column(String(9))
    report_disclaimer: Mapped[str | None] = mapped_column(Text)

    #: Arbitrary per-tenant preferences. JSON rather than columns because the
    #: set grows with the product and none of it is queried.
    settings: Mapped[dict | None] = mapped_column(JSON)

    #: Denormalised counters, maintained by the storage service. Recomputable
    #: from the source tables, kept here so the admin list does not run a
    #: dozen aggregate queries per row.
    storage_bytes: Mapped[int] = mapped_column(Integer, default=0)
    member_count: Mapped[int] = mapped_column(Integer, default=0)

    trial_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    suspended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    suspended_reason: Mapped[str | None] = mapped_column(Text)

    users: Mapped[list["User"]] = relationship(
        back_populates="tenant", cascade="all, delete-orphan",
    )
    subscription: Mapped["Subscription"] = relationship(
        back_populates="tenant", uselist=False, cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index("ix_tenant_status", "status"),
    )


class TenantSecret(Base):
    """An encrypted per-tenant credential — a bring-your-own AI key, an SMTP
    password, a webhook signing secret.

    Stored enveloped rather than in plaintext, which is precisely what the
    workbook's `AI Settings` sheet warns it cannot do: *"your API key is
    stored in this workbook in clear text"*. The platform's answer to that
    warning is this table.
    """

    __tablename__ = "tenant_secrets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    #: Which key encrypted it, so keys can be rotated without a flag day.
    key_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    #: Last four characters, for recognition in the UI. Never the whole value.
    hint: Mapped[str | None] = mapped_column(String(16))
    created_by: Mapped[str | None] = mapped_column(String(64))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_tenant_secret_name"),
    )


# ===========================================================================
# Identity
# ===========================================================================
class User(Base):
    """A person. Belongs to exactly one tenant.

    One tenant per user is a deliberate simplification: multi-org membership
    needs an org-switcher in every surface of the UI and a "current org" in
    every token, and the product has no demand for it yet. The membership is
    modelled as a foreign key rather than a join table so that adding the join
    table later is a migration rather than a rewrite of every query.
    """

    __tablename__ = "users"

    #: A UUID string rather than an integer. User ids appear in tokens, logs
    #: and the `owner_id` columns Modules 7-9 already write, and a guessable
    #: sequential id in a URL invites enumeration.
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("tenants.id", ondelete="CASCADE"), index=True,
    )

    email: Mapped[str] = mapped_column(String(254), nullable=False, unique=True, index=True)
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    avatar_url: Mapped[str | None] = mapped_column(String(500))
    role: Mapped[str] = mapped_column(String(24), default="read_only", nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False, index=True)

    #: Argon2id. Null for users who only ever sign in with Google, GitHub or a
    #: magic link — a null hash must never verify.
    password_hash: Mapped[str | None] = mapped_column(String(255))
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: Future-ready MFA, as the brief asks: the enrolment is modelled and the
    #: secret is stored enveloped, but no method is enforced at login yet.
    mfa_method: Mapped[str] = mapped_column(String(16), default="none", nullable=False)
    mfa_secret_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary)
    mfa_enrolled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: Brute-force defence. Cleared on a successful sign-in.
    failed_login_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    invited_by: Mapped[str | None] = mapped_column(String(36))
    preferences: Mapped[dict | None] = mapped_column(JSON)

    tenant: Mapped["Tenant"] = relationship(back_populates="users")
    identities: Mapped[list["UserIdentity"]] = relationship(
        back_populates="user", cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index("ix_user_tenant_role", "tenant_id", "role"),
        Index("ix_user_tenant_status", "tenant_id", "status"),
    )


class UserIdentity(Base):
    """A federated login linked to a user — Google, GitHub, or a local
    password record.

    Separate from `users` so one person can sign in with a password *and*
    with Google without either being the canonical identity, and so linking a
    new provider never rewrites the user row.
    """

    __tablename__ = "user_identities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    provider: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    #: The provider's own identifier for this person.
    subject: Mapped[str] = mapped_column(String(191), nullable=False)
    email: Mapped[str | None] = mapped_column(String(254))
    #: Whatever the provider returned, minus anything credential-shaped.
    profile: Mapped[dict | None] = mapped_column(JSON)
    linked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped["User"] = relationship(back_populates="identities")

    __table_args__ = (
        UniqueConstraint("provider", "subject", name="uq_identity_provider_subject"),
    )


class RefreshToken(Base):
    """One issued refresh token, stored as a digest.

    Rotation with reuse detection: each refresh mints a new token and marks
    the old one used, recording the successor in `replaced_by`. Presenting an
    already-used token means the token was captured, so the *entire family*
    is revoked and a critical audit event is written. This is the standard
    OAuth 2.1 recommendation and it is the reason the table stores lineage
    rather than just a flag.
    """

    __tablename__ = "refresh_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    tenant_id: Mapped[int | None] = mapped_column(Integer, index=True)
    #: SHA-256 of the token. A database leak yields no usable session.
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    #: All tokens descended from one sign-in share a family id, so reuse
    #: detection can revoke the lineage rather than a single row.
    family_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    session_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)

    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_reason: Mapped[str | None] = mapped_column(String(64))
    replaced_by: Mapped[str | None] = mapped_column(String(64))

    ip_address: Mapped[str | None] = mapped_column(String(45))
    user_agent: Mapped[str | None] = mapped_column(String(300))

    __table_args__ = (
        Index("ix_refresh_family", "family_id", "revoked_at"),
    )


class OneTimeToken(Base):
    """Email verification, password reset and magic-link tokens.

    One table for all three because the lifecycle is identical — issue,
    single use, expiry — and three near-identical tables would drift. The
    `purpose` column distinguishes them and is part of the lookup, so a
    password-reset token cannot be redeemed as a magic link.
    """

    __tablename__ = "one_time_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    purpose: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Extra context the redeeming handler needs — the invited role, say.
    payload: Mapped[dict | None] = mapped_column(JSON)
    ip_address: Mapped[str | None] = mapped_column(String(45))

    __table_args__ = (
        Index("ix_ott_user_purpose", "user_id", "purpose", "consumed_at"),
    )


class ApiKey(Base):
    """A programmatic credential, scoped to a tenant and a role.

    The plaintext is `ierp_live_<id>_<secret>`; only the SHA-256 of the whole
    string is stored. The embedded id lets lookup be a single indexed query
    rather than a hash comparison against every row.
    """

    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    created_by: Mapped[str] = mapped_column(String(36), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    #: Public half, safe to display and to index.
    key_id: Mapped[str] = mapped_column(String(24), nullable=False, unique=True, index=True)
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    #: A key never exceeds the role it was minted with, and that role never
    #: exceeds its creator's — checked at creation.
    role: Mapped[str] = mapped_column(String(24), default="read_only", nullable=False)
    #: Optional narrowing to specific permissions. Empty means "the role's".
    scopes: Mapped[list | None] = mapped_column(JSON)

    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_ip: Mapped[str | None] = mapped_column(String(45))
    call_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    __table_args__ = (
        Index("ix_apikey_tenant_active", "tenant_id", "revoked_at"),
    )


# ===========================================================================
# Commerce
# ===========================================================================
class Plan(Base):
    """A sellable plan. Seeded from `domain/platform/plans.py`, then editable.

    The code catalogue is the default and the test fixture; this table is what
    the running system reads, so pricing and allowances can be changed by an
    operator without a deploy. Where the two disagree, the row wins.
    """

    __tablename__ = "plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tier: Mapped[str] = mapped_column(String(24), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    tagline: Mapped[str | None] = mapped_column(String(240))
    price_monthly_inr: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    price_annual_inr: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: Feature keys, quota map and limit map. JSON because the sets are
    #: sparse, evolve with the product, and are never queried by element.
    features: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    quotas: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    limits: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    trial_days: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_public: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class Subscription(Base):
    """A tenant's current commercial state. One row per tenant."""

    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, unique=True, index=True,
    )
    plan_tier: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), default="trialing", nullable=False, index=True)
    billing_period: Mapped[str] = mapped_column(String(12), default="monthly", nullable=False)

    #: The metering window. Quota consumption is counted within it and reset
    #: when it rolls, which is why it is stored rather than derived from the
    #: calendar — a tenant that signed up on the 20th meters to the 20th.
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)

    trial_ends_at: Mapped[date | None] = mapped_column(Date)
    cancel_at_period_end: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: Per-tenant overrides negotiated in a contract, merged over the plan's
    #: own values. Enterprise deals always need this and bolting it on later
    #: means special-casing the entitlement check.
    quota_overrides: Mapped[dict | None] = mapped_column(JSON)
    limit_overrides: Mapped[dict | None] = mapped_column(JSON)
    feature_overrides: Mapped[list | None] = mapped_column(JSON)

    #: Billing-provider handles. Null until a provider is connected — the
    #: hooks exist, the integration is a configuration step.
    provider: Mapped[str | None] = mapped_column(String(24))
    provider_customer_id: Mapped[str | None] = mapped_column(String(64))
    provider_subscription_id: Mapped[str | None] = mapped_column(String(64))

    tenant: Mapped["Tenant"] = relationship(back_populates="subscription")


class Invoice(Base):
    """A billing document. Issued locally; a payment provider reconciles it."""

    __tablename__ = "invoices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    number: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    plan_tier: Mapped[str] = mapped_column(String(24), nullable=False)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    #: Stored in paise. Money is never a float: 0.1 + 0.2 is not 0.3, and an
    #: invoice that is out by a rounding error is a support ticket.
    subtotal_paise: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tax_paise: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_paise: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), default="INR", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="issued", nullable=False, index=True)
    issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    line_items: Mapped[list | None] = mapped_column(JSON)

    __table_args__ = (
        Index("ix_invoice_tenant_status", "tenant_id", "status"),
    )


class BillingEvent(Base):
    """An inbound webhook from a payment provider, recorded before it is
    acted on.

    Written first, processed second, and unique on the provider's event id, so
    a provider's at-least-once delivery becomes exactly-once processing.
    """

    __tablename__ = "billing_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int | None] = mapped_column(Integer, index=True)
    provider: Mapped[str] = mapped_column(String(24), nullable=False)
    event_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    payload: Mapped[dict | None] = mapped_column(JSON)
    signature_valid: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("provider", "event_id", name="uq_billing_event"),
    )


# ===========================================================================
# Metering and observability
# ===========================================================================
class UsageEvent(Base):
    """One metered occurrence — an AI call, a report, a processed document.

    The raw event, kept for the audit and for disputes. The counters in
    `usage_counters` are the roll-up the entitlement check reads; keeping both
    means a quota decision is one indexed read while the underlying detail
    remains available.
    """

    __tablename__ = "usage_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    user_id: Mapped[str | None] = mapped_column(String(36), index=True)
    api_key_id: Mapped[int | None] = mapped_column(Integer, index=True)
    quota: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    quantity: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    #: What consumed it, for the drill-down: "report:1042", "document:88".
    resource_type: Mapped[str | None] = mapped_column(String(32))
    resource_id: Mapped[str | None] = mapped_column(String(64))
    #: Marginal cost in micro-USD, where the underlying service charges.
    cost_micros: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True,
    )
    meta: Mapped[dict | None] = mapped_column(JSON)

    __table_args__ = (
        Index("ix_usage_tenant_quota_time", "tenant_id", "quota", "occurred_at"),
    )


class UsageCounter(Base):
    """Consumption of one quota by one tenant within one period.

    The row the entitlement check reads and increments. `period_start` is the
    subscription's own window start, not a calendar month, so metering matches
    billing exactly.
    """

    __tablename__ = "usage_counters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    quota: Mapped[str] = mapped_column(String(32), nullable=False)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cost_micros: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "quota", "period_start", name="uq_usage_counter_period",
        ),
    )


class AuditLog(Base):
    """The immutable trail. Append-only by convention and by API: there is no
    update or delete endpoint, and the retention sweep is the only writer that
    ever removes a row."""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int | None] = mapped_column(Integer, index=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    category: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    outcome: Mapped[str] = mapped_column(String(16), default="success", nullable=False)

    actor_id: Mapped[str | None] = mapped_column(String(36), index=True)
    actor_email: Mapped[str | None] = mapped_column(String(254))
    actor_role: Mapped[str | None] = mapped_column(String(24))

    resource_type: Mapped[str | None] = mapped_column(String(32), index=True)
    resource_id: Mapped[str | None] = mapped_column(String(64))
    summary: Mapped[str] = mapped_column(String(500), default="", nullable=False)

    ip_address: Mapped[str | None] = mapped_column(String(45))
    user_agent: Mapped[str | None] = mapped_column(String(300))
    request_id: Mapped[str | None] = mapped_column(String(36), index=True)
    #: Redacted before it arrives here — see `domain/platform/audit.redact`.
    meta: Mapped[dict | None] = mapped_column(JSON)

    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True,
    )

    __table_args__ = (
        Index("ix_audit_tenant_time", "tenant_id", "occurred_at"),
        Index("ix_audit_category_time", "category", "occurred_at"),
    )


class RequestMetric(Base):
    """Per-endpoint performance, aggregated into one-minute buckets.

    Buckets rather than rows-per-request: a busy minute is one row instead of
    ten thousand, the metrics endpoint reads a handful of rows, and no
    external time-series database is required for the platform to be
    observable. Percentiles are estimated from a fixed-boundary histogram —
    exact percentiles need every sample, and the boundaries here (5 ms to
    10 s) span the range this application actually produces.
    """

    __tablename__ = "request_metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bucket_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True,
    )
    route: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    method: Mapped[str] = mapped_column(String(8), nullable=False)
    status_class: Mapped[str] = mapped_column(String(4), nullable=False)  # 2xx…5xx
    count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_ms: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    max_ms: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    #: Counts per latency boundary, as a JSON list aligned to
    #: `services/platform/metrics.LATENCY_BUCKETS_MS`.
    histogram: Mapped[list | None] = mapped_column(JSON)

    __table_args__ = (
        UniqueConstraint(
            "bucket_start", "route", "method", "status_class",
            name="uq_metric_bucket",
        ),
    )


class ErrorEvent(Base):
    """An unhandled exception, grouped by fingerprint.

    One row per distinct error, with a count — not one row per occurrence. An
    error loop must not be able to fill the database with its own evidence.
    """

    __tablename__ = "error_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    #: Hash of exception type + normalised message + top application frame.
    fingerprint: Mapped[str] = mapped_column(String(40), nullable=False, unique=True, index=True)
    exc_type: Mapped[str] = mapped_column(String(120), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    route: Mapped[str | None] = mapped_column(String(160), index=True)
    method: Mapped[str | None] = mapped_column(String(8))
    #: Application frames only, provider paths stripped.
    stack: Mapped[str | None] = mapped_column(Text)
    count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True,
    )
    tenant_id: Mapped[int | None] = mapped_column(Integer, index=True)
    last_request_id: Mapped[str | None] = mapped_column(String(36))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_by: Mapped[str | None] = mapped_column(String(36))


# ===========================================================================
# Background work
# ===========================================================================
class BackgroundJob(Base):
    """The unified queue.

    Modules 7 and 9 keep their own job tables for their domain-specific
    columns; this is the table the worker polls and the monitoring endpoint
    reports on, and it links back to those rows through
    `resource_type`/`resource_id`.
    """

    __tablename__ = "background_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int | None] = mapped_column(Integer, index=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), default="queued", nullable=False, index=True)
    priority: Mapped[int] = mapped_column(Integer, default=50, nullable=False)

    payload: Mapped[dict | None] = mapped_column(JSON)
    result: Mapped[dict | None] = mapped_column(JSON)

    #: Enqueuing the same work twice while the first is pending returns the
    #: first. Not unique at the database level, because the same work may
    #: legitimately be requested again once the earlier copy has finished.
    idempotency_key: Mapped[str | None] = mapped_column(String(32), index=True)
    resource_type: Mapped[str | None] = mapped_column(String(32))
    resource_id: Mapped[str | None] = mapped_column(String(64))

    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    #: Backoff target. The poller only takes jobs whose time has come, which
    #: is what makes retry scheduling work without a timer service.
    run_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    #: Claim marker. A worker sets both atomically; a job whose lease has
    #: expired is reclaimable, so a worker that dies mid-job does not strand
    #: the work forever.
    locked_by: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    progress: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    stage: Mapped[str | None] = mapped_column(String(32))
    error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index("ix_job_poll", "status", "priority", "run_after"),
        Index("ix_job_tenant_kind", "tenant_id", "kind"),
    )


class ScheduleState(Base):
    """Last-run bookkeeping for each recurring job, so a restart does not
    re-run everything and two workers do not both fire the nightly backup."""

    __tablename__ = "schedule_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, unique=True, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    every_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_status: Mapped[str | None] = mapped_column(String(16))
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    run_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class Notification(Base):
    """An in-app or emailed message. Queued as a job, recorded here."""

    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int | None] = mapped_column(Integer, index=True)
    user_id: Mapped[str | None] = mapped_column(String(36), index=True)
    channel: Mapped[str] = mapped_column(String(16), default="in_app", nullable=False)
    topic: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    subject: Mapped[str] = mapped_column(String(240), nullable=False)
    body: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: Deep link into the product.
    link: Mapped[str | None] = mapped_column(String(500))
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivery_status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index("ix_notification_user_read", "user_id", "read_at"),
    )


class BackupRecord(Base):
    """A completed backup. What was taken, when, how big, and whether its
    checksum still verifies — a backup nobody can prove is readable is not a
    backup."""

    __tablename__ = "backup_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(16), default="database", nullable=False)
    location: Mapped[str] = mapped_column(String(500), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    checksum: Mapped[str | None] = mapped_column(String(64))
    table_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    duration_ms: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="succeeded", nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
