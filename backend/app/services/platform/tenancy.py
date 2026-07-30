"""Tenant resolution, isolation and settings.

The isolation guarantee in one sentence: **every query for a tenant-owned row
goes through `TenantScope`, which adds the `tenant_id` predicate, and any
attempt to fetch a row belonging to another tenant raises rather than
returning None.**

The distinction between raising and returning None matters. If a cross-tenant
fetch quietly returns nothing, the caller renders an empty page and nobody
learns that an isolation boundary was probed. If it raises, the API turns it
into a 404 (never revealing existence) *and* writes a critical audit event.
The user sees the same thing either way; the operator does not.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, TypeVar

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from sqlalchemy.sql import Select

from app.core.config import settings
from app.domain.platform.identity import (
    BLOCKED_TENANT_STATUSES, Principal, READ_ONLY_TENANT_STATUSES, Role,
    TenantIsolationError, TenantStatus,
)
from app.domain.platform.limits import slugify
from app.domain.platform.plans import (
    BillingPeriod, PlanTier, SubscriptionStatus, plan as plan_spec,
)
from app.models.platform import Subscription, Tenant, User

T = TypeVar("T")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TenantError(Exception):
    """A tenant-level rule was broken — a duplicate slug, a missing tenant."""


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class TenantScope:
    """The tenant filter for one request.

    Constructed once per request from the principal and threaded into every
    service that touches tenant-owned data. Services never read
    `principal.tenant_id` themselves — they take a scope — which is what makes
    "did we remember to filter?" a question with one answer instead of fifty.
    """

    tenant_id: int | None
    #: True for the platform operator, who legitimately reads across tenants.
    unrestricted: bool = False

    @classmethod
    def for_principal(cls, principal: Principal) -> "TenantScope":
        return cls(
            tenant_id=principal.tenant_id,
            unrestricted=principal.is_platform_operator,
        )

    @classmethod
    def system(cls) -> "TenantScope":
        """For background workers and migrations, which act for every tenant.
        Named explicitly so an unrestricted scope is always a deliberate act
        and greppable in review."""
        return cls(tenant_id=None, unrestricted=True)

    def apply(self, stmt: Select, column: Any) -> Select:
        """Add the tenant predicate to a select.

        An operator's query is returned untouched. A principal with no tenant
        and no operator rights gets a predicate that matches nothing — the
        safe reading of "belongs to no organisation".
        """
        if self.unrestricted:
            return stmt
        if self.tenant_id is None:
            return stmt.where(column.is_(None) & column.isnot(None))  # always false
        return stmt.where(column == self.tenant_id)

    def check(self, row_tenant_id: int | None) -> None:
        """Assert an already-loaded row belongs to this scope."""
        if self.unrestricted:
            return
        if row_tenant_id is None or row_tenant_id != self.tenant_id:
            raise TenantIsolationError(self.tenant_id, row_tenant_id)

    def owns(self, row_tenant_id: int | None) -> bool:
        if self.unrestricted:
            return True
        return row_tenant_id is not None and row_tenant_id == self.tenant_id

    def assign(self) -> int:
        """The tenant id to stamp on a row being created.

        An operator creating tenant-owned data must say which tenant; there is
        no sensible default, and guessing produces orphaned rows.
        """
        if self.tenant_id is None:
            raise TenantError("cannot create tenant-owned data without a tenant")
        return self.tenant_id


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------
class TenantService:
    """Tenant lifecycle, settings and derived counters."""

    def __init__(self, db: Session) -> None:
        self.db = db

    # -- lookup -------------------------------------------------------
    def get(self, tenant_id: int) -> Tenant | None:
        return self.db.get(Tenant, tenant_id)

    def by_slug(self, slug: str) -> Tenant | None:
        return self.db.scalar(select(Tenant).where(Tenant.slug == slug))

    def require(self, tenant_id: int) -> Tenant:
        tenant = self.get(tenant_id)
        if tenant is None:
            raise TenantError(f"no tenant with id {tenant_id}")
        return tenant

    def list(
        self,
        *,
        status: str | None = None,
        search: str | None = None,
        offset: int = 0,
        limit: int = 50,
        sort: str = "created_at",
        descending: bool = True,
    ) -> tuple[list[Tenant], int]:
        """Operator-facing listing. Returns the page and the unpaged total, so
        the UI can show "showing 25 of 340" without a second round trip."""
        stmt = select(Tenant)
        if status:
            stmt = stmt.where(Tenant.status == status)
        if search:
            like = f"%{search.lower()}%"
            stmt = stmt.where(
                func.lower(Tenant.name).like(like) | func.lower(Tenant.slug).like(like)
            )

        total = self.db.scalar(
            select(func.count()).select_from(stmt.subquery())
        ) or 0

        column = getattr(Tenant, sort, Tenant.created_at)
        stmt = stmt.order_by(column.desc() if descending else column.asc())
        rows = list(self.db.scalars(stmt.offset(offset).limit(limit)))
        return rows, total

    # -- lifecycle ----------------------------------------------------
    def create(
        self,
        name: str,
        *,
        slug: str | None = None,
        tier: PlanTier = PlanTier.FREE,
        status: TenantStatus | None = None,
        country: str = "IN",
        industry: str | None = None,
    ) -> Tenant:
        """Create an organisation and its subscription in one transaction.

        A tenant without a subscription cannot answer an entitlement question,
        so the two are created together and never separately. The trial window
        comes from the plan, not from a constant here.
        """
        base = slugify(slug or name) or "org"
        final = self._unique_slug(base)

        spec = plan_spec(tier)
        now = _utcnow()
        trial_ends = now + timedelta(days=spec.trial_days) if spec.trial_days else None
        resolved_status = status or (
            TenantStatus.TRIAL if spec.trial_days else TenantStatus.ACTIVE
        )

        tenant = Tenant(
            slug=final, name=name.strip(), status=resolved_status.value,
            country=country, industry=industry, trial_ends_at=trial_ends,
            settings={}, storage_bytes=0, member_count=0,
        )
        self.db.add(tenant)
        self.db.flush()   # need the id for the subscription

        period_start = now.date()
        self.db.add(Subscription(
            tenant_id=tenant.id,
            plan_tier=tier.value,
            status=(
                SubscriptionStatus.TRIALING if spec.trial_days
                else SubscriptionStatus.ACTIVE
            ).value,
            billing_period=BillingPeriod.MONTHLY.value,
            period_start=period_start,
            period_end=_add_month(period_start),
            trial_ends_at=trial_ends.date() if trial_ends else None,
        ))
        self.db.commit()
        self.db.refresh(tenant)
        return tenant

    def _unique_slug(self, base: str) -> str:
        """Append -2, -3 … until free. Racy in theory; the unique constraint
        is the actual guarantee and this only avoids the common collision."""
        candidate, n = base, 1
        while self.by_slug(candidate) is not None:
            n += 1
            candidate = f"{base}-{n}"
            if n > 200:
                raise TenantError(f"cannot derive a free slug from '{base}'")
        return candidate

    def update(self, tenant: Tenant, **fields: Any) -> Tenant:
        """Patch permitted columns. The allow-list is explicit so a future
        request body cannot smuggle `status` or `storage_bytes` past the
        schema and into the row."""
        editable = {
            "name", "industry", "country", "timezone", "base_currency",
            "logo_url", "primary_colour", "report_disclaimer",
        }
        for key, value in fields.items():
            if key in editable and value is not None:
                setattr(tenant, key, value)
        self.db.commit()
        self.db.refresh(tenant)
        return tenant

    def update_settings(self, tenant: Tenant, patch: dict[str, Any]) -> Tenant:
        """Merge into the settings blob.

        A merge rather than a replace: two admins editing different
        preferences in two tabs should not silently discard each other's work.
        A JSON column is replaced wholesale by SQLAlchemy, so the merged dict
        is reassigned to trigger the update.
        """
        merged = dict(tenant.settings or {})
        merged.update({k: v for k, v in patch.items() if v is not None})
        tenant.settings = merged
        self.db.commit()
        self.db.refresh(tenant)
        return tenant

    def suspend(self, tenant: Tenant, reason: str) -> Tenant:
        tenant.status = TenantStatus.SUSPENDED.value
        tenant.suspended_at = _utcnow()
        tenant.suspended_reason = reason
        self.db.commit()
        self.db.refresh(tenant)
        return tenant

    def reactivate(self, tenant: Tenant) -> Tenant:
        tenant.status = TenantStatus.ACTIVE.value
        tenant.suspended_at = None
        tenant.suspended_reason = None
        self.db.commit()
        self.db.refresh(tenant)
        return tenant

    # -- derived ------------------------------------------------------
    def refresh_member_count(self, tenant_id: int) -> int:
        count = self.db.scalar(
            select(func.count(User.id)).where(User.tenant_id == tenant_id)
        ) or 0
        tenant = self.get(tenant_id)
        if tenant is not None:
            tenant.member_count = count
            self.db.commit()
        return count

    def storage_breakdown(self, tenant_id: int) -> dict[str, int]:
        """Bytes held per resource class.

        Computed live from the owning tables rather than from a running total,
        because a running total drifts the first time a delete misses a
        decrement, and storage is a billable quantity.
        """
        from app.models.document import Document
        from app.models.report import ReportArtifact, Report

        user_ids = list(self.db.scalars(
            select(User.id).where(User.tenant_id == tenant_id)
        ))

        documents = 0
        if user_ids:
            documents = self.db.scalar(
                select(func.coalesce(func.sum(Document.size_bytes), 0))
                .where(Document.uploaded_by.in_(user_ids))
            ) or 0

        artifacts = 0
        if user_ids:
            artifacts = self.db.scalar(
                select(func.coalesce(func.sum(ReportArtifact.size_bytes), 0))
                .join(Report, Report.id == ReportArtifact.report_id)
                .where(Report.owner_id.in_(user_ids))
            ) or 0

        return {
            "documents": int(documents),
            "reports": int(artifacts),
            "total": int(documents) + int(artifacts),
        }

    def sync_storage(self, tenant_id: int) -> int:
        total = self.storage_breakdown(tenant_id)["total"]
        tenant = self.get(tenant_id)
        if tenant is not None:
            tenant.storage_bytes = total
            self.db.commit()
        return total

    # -- state helpers ------------------------------------------------
    @staticmethod
    def status_of(tenant: Tenant) -> TenantStatus:
        return TenantStatus(tenant.status)

    @classmethod
    def is_blocked(cls, tenant: Tenant) -> bool:
        return cls.status_of(tenant) in BLOCKED_TENANT_STATUSES

    @classmethod
    def is_read_only(cls, tenant: Tenant) -> bool:
        return cls.status_of(tenant) in READ_ONLY_TENANT_STATUSES

    def expire_trials(self, *, now: datetime | None = None) -> int:
        """Move tenants whose trial has elapsed to past-due.

        Past-due rather than suspended: the customer keeps read access to
        their own research while they sort out payment. Suspending on the
        first missed day is how a SaaS loses an account it could have kept.
        """
        moment = now or _utcnow()
        rows = list(self.db.scalars(
            select(Tenant).where(
                Tenant.status == TenantStatus.TRIAL.value,
                Tenant.trial_ends_at.isnot(None),
                Tenant.trial_ends_at <= moment,
            )
        ))
        for tenant in rows:
            tenant.status = TenantStatus.PAST_DUE.value
            sub = self.db.scalar(
                select(Subscription).where(Subscription.tenant_id == tenant.id)
            )
            if sub is not None:
                sub.status = SubscriptionStatus.PAST_DUE.value
        if rows:
            self.db.commit()
        return len(rows)


def _add_month(start: date) -> date:
    """One calendar month on, clamped to the month's length.

    31 January + 1 month is 28/29 February, not 3 March. Getting this wrong
    means a tenant that signed up on the 31st is metered on a different day
    every month.
    """
    year, month = start.year, start.month + 1
    if month > 12:
        year, month = year + 1, 1
    import calendar

    day = min(start.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)
