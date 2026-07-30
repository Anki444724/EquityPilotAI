"""Seed the SaaS layer and backfill the tenancy columns.

Two jobs.

**Seed** — plans, a demo organisation, a set of users covering every role, an
API key, some usage and a populated audit trail. Enough that the admin panel
shows a real system rather than empty tables, which is the difference between
a screenshot that demonstrates something and one that demonstrates nothing.

**Backfill** — Modules 1-9 wrote `owner_id = "dev-user"` on portfolios,
watchlists, reports and documents. Those rows predate tenancy and would
otherwise belong to nobody. The backfill creates a real user with that exact
id, so every existing row resolves to a member of the demo organisation
without a single UPDATE to a Module 1-9 table.

Seeding is idempotent throughout: running it twice changes nothing the second
time.
"""
from __future__ import annotations

import random
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.domain.platform.audit import AuditAction
from app.domain.platform.identity import Role, TenantStatus, UserStatus
from app.domain.platform.jobs import JobKind, JobStatus
from app.domain.platform.plans import PlanTier, Quota, SubscriptionStatus
from app.models.platform import (
    ApiKey, AuditLog, BackgroundJob, Notification, Subscription, Tenant,
    UsageCounter, UsageEvent, User, UserIdentity,
)
from app.services.platform import crypto
from app.services.platform.audit_service import AuditService, RequestContext
from app.services.platform.entitlements import EntitlementService
from app.services.platform.tenancy import TenantService

#: The id Modules 1-9 wrote into every `owner_id` column. A user is created
#: with exactly this primary key so historical rows resolve to a real person.
LEGACY_OWNER_ID = "dev-user"

DEMO_PASSWORD = "ResearchDesk2026!"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
def seed_plans(db: Session) -> int:
    """Write the four plans. Never overwrites an operator's edits."""
    return EntitlementService(db).sync_catalogue()


# ---------------------------------------------------------------------------
def seed_tenants(db: Session) -> dict[str, Tenant]:
    """Three organisations, on three plans, in three states.

    One is not enough to prove isolation: the tests need a second tenant to
    attempt a cross-boundary read against, and the admin console needs more
    than one row to be worth looking at. The third is past-due, so the
    read-only degradation path is exercised by the seed rather than only by a
    test.
    """
    service = TenantService(db)
    out: dict[str, Tenant] = {}

    specs = [
        (settings.DEFAULT_TENANT_SLUG, "Demo Capital Advisors",
         PlanTier.PROFESSIONAL, TenantStatus.ACTIVE, "Asset Management"),
        ("northwind-research", "Northwind Research LLP",
         PlanTier.BASIC, TenantStatus.ACTIVE, "Independent Research"),
        ("meridian-pms", "Meridian PMS",
         PlanTier.FREE, TenantStatus.PAST_DUE, "Portfolio Management"),
    ]

    for slug, name, tier, status, industry in specs:
        existing = service.by_slug(slug)
        if existing is not None:
            out[slug] = existing
            continue

        tenant = service.create(
            name, slug=slug, tier=tier, status=status, industry=industry,
        )
        subscription = db.scalar(
            select(Subscription).where(Subscription.tenant_id == tenant.id)
        )
        if subscription is not None:
            subscription.status = (
                SubscriptionStatus.PAST_DUE if status is TenantStatus.PAST_DUE
                else SubscriptionStatus.ACTIVE
            ).value
        if slug == settings.DEFAULT_TENANT_SLUG:
            tenant.report_disclaimer = (
                "Prepared for institutional clients of Demo Capital Advisors. "
                "Not investment advice."
            )
            tenant.primary_colour = "#0f4c81"
        db.commit()
        out[slug] = tenant

    return out


# ---------------------------------------------------------------------------
def seed_users(db: Session, tenants: dict[str, Tenant]) -> dict[str, User]:
    """One user per role, so the RBAC matrix is demonstrable, not just
    described."""
    out: dict[str, User] = {}
    demo = tenants[settings.DEFAULT_TENANT_SLUG]
    northwind = tenants["northwind-research"]
    meridian = tenants["meridian-pms"]
    now = _utcnow()

    # The legacy owner. Its primary key is fixed, which is the whole point:
    # every `owner_id="dev-user"` row written by Modules 1-9 now resolves.
    people: list[tuple[str, str, str, Role, Tenant, str | None]] = [
        (LEGACY_OWNER_ID, "analyst@localhost", "Development Analyst",
         Role.SUPER_ADMIN, demo, None),
        (None, "priya.nair@democapital.in", "Priya Nair", Role.ADMIN, demo, None),
        (None, "arjun.mehta@democapital.in", "Arjun Mehta", Role.ANALYST, demo, None),
        (None, "sana.qureshi@democapital.in", "Sana Qureshi", Role.RESEARCHER, demo, None),
        (None, "client@familyoffice.in", "Vikram Rao", Role.SUBSCRIBER, demo, None),
        (None, "audit@democapital.in", "Compliance Desk", Role.READ_ONLY, demo, None),
        (None, "rohan.das@northwind.in", "Rohan Das", Role.ADMIN, northwind, None),
        (None, "kavya.iyer@northwind.in", "Kavya Iyer", Role.ANALYST, northwind, None),
        (None, "ops@meridianpms.in", "Meridian Ops", Role.ADMIN, meridian, None),
    ]

    password_hash = crypto.hash_password(DEMO_PASSWORD)

    for user_id, email, name, role, tenant, avatar in people:
        existing = db.scalar(select(User).where(User.email == email))
        if existing is not None:
            out[email] = existing
            continue

        user = User(
            id=user_id or crypto.new_id(),
            tenant_id=tenant.id,
            email=email,
            name=name,
            role=role.value,
            status=UserStatus.ACTIVE.value,
            password_hash=password_hash,
            password_changed_at=now,
            email_verified_at=now,
            avatar_url=avatar,
            last_login_at=now - timedelta(hours=random.randint(1, 72)),
            last_seen_at=now - timedelta(minutes=random.randint(1, 600)),
        )
        db.add(user)
        db.add(UserIdentity(
            user_id=user.id, provider="password", subject=email,
            email=email, linked_at=now,
        ))
        out[email] = user

    # One pending invitation, so the members table shows the state.
    if db.scalar(select(User).where(User.email == "new.joiner@democapital.in")) is None:
        db.add(User(
            id=crypto.new_id(), tenant_id=demo.id,
            email="new.joiner@democapital.in", name="Aditi Sharma",
            role=Role.RESEARCHER.value, status=UserStatus.PENDING.value,
            password_hash=None, invited_by=out.get("priya.nair@democapital.in").id
            if out.get("priya.nair@democapital.in") else None,
        ))

    db.commit()
    for tenant in tenants.values():
        TenantService(db).refresh_member_count(tenant.id)
    return out


# ---------------------------------------------------------------------------
def seed_api_keys(db: Session, tenants: dict[str, Tenant], users: dict[str, User]) -> int:
    """Two keys, so the panel shows an active one and a revoked one.

    The plaintext is discarded here exactly as it is in production — the seed
    cannot cheat the one-time-display rule either.
    """
    demo = tenants[settings.DEFAULT_TENANT_SLUG]
    if db.scalar(select(func.count(ApiKey.id)).where(ApiKey.tenant_id == demo.id)):
        return 0

    owner = users.get("priya.nair@democapital.in")
    if owner is None:
        return 0

    now = _utcnow()
    created = 0
    for name, role, revoked, calls in [
        ("Risk dashboard (read-only)", Role.READ_ONLY, False, 18_432),
        ("Retired CI key", Role.READ_ONLY, True, 2_101),
    ]:
        generated = crypto.generate_api_key()
        db.add(ApiKey(
            tenant_id=demo.id, created_by=owner.id, name=name,
            key_id=generated.key_id, key_hash=generated.key_hash,
            prefix=generated.prefix, role=role.value,
            expires_at=now + timedelta(days=365),
            revoked_at=now - timedelta(days=9) if revoked else None,
            last_used_at=now - timedelta(minutes=7) if not revoked else None,
            call_count=calls,
        ))
        created += 1
    db.commit()
    return created


# ---------------------------------------------------------------------------
def seed_usage(db: Session, tenants: dict[str, Tenant], users: dict[str, User]) -> int:
    """Thirty days of metered activity.

    Written as raw events and then rolled into counters by the same code path
    production uses, so the seeded numbers are internally consistent and the
    reconciliation job finds no drift on a fresh install.
    """
    demo = tenants[settings.DEFAULT_TENANT_SLUG]
    if db.scalar(select(func.count(UsageEvent.id)).where(UsageEvent.tenant_id == demo.id)):
        return 0

    rng = random.Random(20260730)
    service = EntitlementService(db)
    subscription = service.subscription_for(demo.id)

    member_ids = [
        u.id for u in users.values()
        if u.tenant_id == demo.id and u.status == UserStatus.ACTIVE.value
    ]

    profile = {
        Quota.AI_CALLS: (2, 14),
        Quota.REPORTS_GENERATED: (0, 4),
        Quota.DOCUMENTS_PROCESSED: (0, 3),
        Quota.API_REQUESTS: (200, 1_400),
        Quota.EXPORTS: (0, 6),
    }

    written = 0
    totals: dict[Quota, int] = {q: 0 for q in profile}
    now = _utcnow()

    for day_offset in range(29, -1, -1):
        when = now - timedelta(days=day_offset)
        # Weekends are quiet on a research desk; flat synthetic data looks
        # synthetic the moment it is charted.
        weekday_factor = 0.25 if when.weekday() >= 5 else 1.0
        for quota, (low, high) in profile.items():
            quantity = int(rng.randint(low, high) * weekday_factor)
            if quantity <= 0:
                continue
            tokens = quantity * rng.randint(1_200, 4_000) if quota is Quota.AI_CALLS else 0
            db.add(UsageEvent(
                tenant_id=demo.id,
                user_id=rng.choice(member_ids) if member_ids else None,
                quota=quota.value, quantity=quantity,
                cost_micros=int(tokens * 0.15) if tokens else 0,
                occurred_at=when.replace(
                    hour=rng.randint(9, 19), minute=rng.randint(0, 59),
                ),
            ))
            written += 1
            # Only the current billing period counts toward the live counter.
            if when.date() >= subscription.period_start:
                totals[quota] = totals.get(quota, 0) + quantity
            if tokens and when.date() >= subscription.period_start:
                totals[Quota.AI_TOKENS] = totals.get(Quota.AI_TOKENS, 0) + tokens

    db.commit()

    for quota, total in totals.items():
        if total <= 0:
            continue
        counter = service.counter(demo.id, quota, subscription)
        counter.used = total
        counter.last_event_at = now
    db.commit()
    return written


# ---------------------------------------------------------------------------
def seed_audit(db: Session, tenants: dict[str, Tenant], users: dict[str, User]) -> int:
    """A believable trail — including failures, which is what makes it useful."""
    demo = tenants[settings.DEFAULT_TENANT_SLUG]
    if db.scalar(select(func.count(AuditLog.id)).where(AuditLog.tenant_id == demo.id)):
        return 0

    rng = random.Random(4242)
    service = AuditService(db)
    members = [u for u in users.values() if u.tenant_id == demo.id]
    if not members:
        return 0

    script: list[tuple[AuditAction, str, str]] = [
        (AuditAction.LOGIN_SUCCEEDED, "success", "Signed in with password"),
        (AuditAction.REPORT_GENERATED, "success", "Institutional report for BHARATCP"),
        (AuditAction.DOCUMENT_UPLOADED, "success", "Annual report FY25 uploaded"),
        (AuditAction.AI_CALL, "success", "Investment thesis generated"),
        (AuditAction.PORTFOLIO_TRANSACTION, "success", "Buy 250 BHARATCP @ 268.00"),
        (AuditAction.LOGIN_FAILED, "failure", "Incorrect password"),
        (AuditAction.APIKEY_CREATED, "success", "Risk dashboard key issued"),
        (AuditAction.USER_ROLE_CHANGED, "success", "Sana Qureshi promoted to Researcher"),
        (AuditAction.ACCESS_DENIED, "denied", "GET /api/v1/admin/tenants requires 'platform:admin'"),
        (AuditAction.REPORT_DOWNLOADED, "success", "IC memo exported as PDF"),
        (AuditAction.SUBSCRIPTION_CHANGED, "success", "Upgraded to Professional"),
        (AuditAction.DOCUMENT_PROCESSED, "success", "Extraction complete, 57 fields"),
    ]

    written = 0
    now = _utcnow()
    for day_offset in range(13, -1, -1):
        for _ in range(rng.randint(2, 6)):
            action, outcome, summary = rng.choice(script)
            actor = rng.choice(members)
            row = service.record(
                action,
                tenant_id=demo.id,
                actor_id=actor.id, actor_email=actor.email, actor_role=actor.role,
                summary=summary, outcome=outcome,
                context=RequestContext(
                    ip_address=f"103.21.{rng.randint(1, 250)}.{rng.randint(1, 250)}",
                    user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
                    request_id=crypto.new_id(),
                ),
                commit=False,
            )
            if row is not None:
                # Backdate: `record` stamps now, and a trail that is all from
                # this second is not a trail.
                row.occurred_at = (now - timedelta(days=day_offset)).replace(
                    hour=rng.randint(8, 20), minute=rng.randint(0, 59),
                )
                written += 1
    db.commit()
    return written


# ---------------------------------------------------------------------------
def seed_jobs(db: Session, tenants: dict[str, Tenant]) -> int:
    """A queue with history: successes, one retrying, one dead-lettered.

    The dead letter is deliberate. An operator console that has only ever
    shown green has not been tested against the state it exists to surface.
    """
    demo = tenants[settings.DEFAULT_TENANT_SLUG]
    if db.scalar(select(func.count(BackgroundJob.id))):
        return 0

    now = _utcnow()
    rows = [
        (JobKind.REPORT_GENERATION, JobStatus.SUCCEEDED, 1, 1_735.0, None),
        (JobKind.DOCUMENT_PROCESSING, JobStatus.SUCCEEDED, 1, 8_420.0, None),
        (JobKind.EMBEDDING, JobStatus.SUCCEEDED, 1, 640.0, None),
        (JobKind.PORTFOLIO_REFRESH, JobStatus.SUCCEEDED, 1, 2_110.0, None),
        (JobKind.ALERT_EVALUATION, JobStatus.SUCCEEDED, 1, 890.0, None),
        (JobKind.NOTIFICATION, JobStatus.FAILED, 2, 120.0,
         "SMTPConnectError: connection refused"),
        (JobKind.DOCUMENT_PROCESSING, JobStatus.DEAD_LETTER, 3, 15_200.0,
         "PdfReadError: the source file is encrypted and no password was supplied"),
        (JobKind.USAGE_ROLLUP, JobStatus.QUEUED, 0, 0.0, None),
    ]

    for index, (kind, status, attempts, duration, error) in enumerate(rows):
        finished = (
            now - timedelta(hours=index + 1)
            if status in (JobStatus.SUCCEEDED, JobStatus.DEAD_LETTER) else None
        )
        db.add(BackgroundJob(
            tenant_id=demo.id, kind=kind.value, status=status.value,
            priority=50, attempts=attempts, max_attempts=3,
            duration_ms=duration, error=error,
            progress=1.0 if status is JobStatus.SUCCEEDED else 0.0,
            stage="done" if status is JobStatus.SUCCEEDED else status.value,
            started_at=now - timedelta(hours=index + 1, seconds=30),
            finished_at=finished,
            run_after=now + timedelta(minutes=4) if status is JobStatus.FAILED else now,
        ))
    db.commit()
    return len(rows)


# ---------------------------------------------------------------------------
def seed_notifications(db: Session, tenants: dict[str, Tenant], users: dict[str, User]) -> int:
    demo = tenants[settings.DEFAULT_TENANT_SLUG]
    if db.scalar(select(func.count(Notification.id))):
        return 0

    # `users` is keyed by email; the legacy owner is the development analyst.
    owner = users.get("analyst@localhost")
    if owner is None:
        return 0

    now = _utcnow()
    items = [
        ("quota.warning", "AI usage at 82% of your monthly allowance",
         "Your organisation has used 1,640 of 2,000 AI calls this period.", None),
        ("report.ready", "Institutional report ready — Bharat Consumer Products",
         "15 sections, 100% citation coverage. Available in PDF, Word and Excel.",
         "/reports"),
        ("alert.triggered", "3 portfolio alerts require attention",
         "Position size breach on BHARATCP; two valuation alerts suppressed as "
         "the underlying valuation is graded unreliable.", "/portfolio"),
    ]
    for index, (topic, subject, body, link) in enumerate(items):
        db.add(Notification(
            tenant_id=demo.id, user_id=owner.id, channel="in_app",
            topic=topic, subject=subject, body=body, link=link,
            sent_at=now - timedelta(hours=index * 5 + 1),
            delivery_status="sent",
            read_at=now - timedelta(hours=1) if index == 2 else None,
        ))
    db.commit()
    return len(items)


# ---------------------------------------------------------------------------
def backfill_ownership(db: Session, tenants: dict[str, Tenant]) -> dict[str, int]:
    """Report what Modules 1-9 rows now resolve to.

    No UPDATE is issued. `LEGACY_OWNER_ID` is a real user id, so every
    historical `owner_id="dev-user"` row already points at a member of the
    demo organisation. This function verifies that and reports the counts,
    because a migration that claims to have worked should be able to prove it.
    """
    from app.models.document import Document
    from app.models.portfolio import Portfolio, Watchlist
    from app.models.report import Report

    def _count(model, column) -> int:
        return int(db.scalar(
            select(func.count()).select_from(model).where(column == LEGACY_OWNER_ID)
        ) or 0)

    resolved = db.get(User, LEGACY_OWNER_ID) is not None
    return {
        "legacy_owner_resolves": int(resolved),
        "portfolios": _count(Portfolio, Portfolio.owner_id),
        "watchlists": _count(Watchlist, Watchlist.owner_id),
        "reports": _count(Report, Report.owner_id),
        "documents": _count(Document, Document.uploaded_by),
    }


# ---------------------------------------------------------------------------
def seed_module10(db: Session) -> dict[str, int]:
    """Everything, in dependency order. Idempotent."""
    plans = seed_plans(db)
    tenants = seed_tenants(db)
    users = seed_users(db, tenants)

    result = {
        "plans": plans,
        "tenants": len(tenants),
        "users": len(users),
        "api_keys": seed_api_keys(db, tenants, users),
        "usage_events": seed_usage(db, tenants, users),
        "audit_rows": seed_audit(db, tenants, users),
        "jobs": seed_jobs(db, tenants),
        "notifications": seed_notifications(db, tenants, users),
    }
    result.update(backfill_ownership(db, tenants))

    for tenant in tenants.values():
        TenantService(db).sync_storage(tenant.id)
    return result
