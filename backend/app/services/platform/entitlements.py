"""Subscriptions, plans, metering and the entitlement gate.

`domain/platform/plans.py` decides *whether* an action is permitted given a
plan and a usage figure. This module supplies both sides of that question from
the database and records the consequence.

The rule that keeps metering honest: **a quota is consumed only when the work
succeeds.** `check()` asks, `consume()` records. A generation that raises
halfway must not bill the tenant for a report they did not receive, so the
call sites check first, do the work, and consume last.

The second rule: **plan rows win over the code catalogue.** The catalogue in
the domain module is the default and the test fixture. The `plans` table is
what a running system reads, so an operator can change pricing without a
deploy — and per-tenant contract overrides on the subscription are merged over
that, because enterprise deals always need them.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.domain.platform.plans import (
    ALLOWED, BillingPeriod, DenialReason, Entitlement, Feature, Limit,
    PLAN_CATALOGUE, PLAN_ORDER, PlanSpec, PlanTier, Quota, QuotaUsage,
    SubscriptionStatus, UNLIMITED, evaluate, is_unlimited,
)
from app.models.platform import (
    Plan, Subscription, Tenant, UsageCounter, UsageEvent,
)
from app.services.platform.tenancy import TenantService, _add_month


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class BillingError(Exception):
    """A commercial rule was broken — an unknown plan, a missing subscription."""


@dataclass(frozen=True, slots=True)
class TenantEntitlements:
    """Everything the UI needs to render what a tenant may do.

    Assembled once and passed around rather than recomputed per widget: a
    pricing panel that asks the database eleven times to draw eleven progress
    bars is how a dashboard becomes slow.
    """

    tenant_id: int
    plan: PlanSpec
    status: SubscriptionStatus
    period_start: date
    period_end: date
    features: frozenset[Feature]
    quotas: dict[Quota, QuotaUsage]
    limits: dict[Limit, int]
    limit_usage: dict[Limit, int]
    trial_ends_at: date | None = None
    cancel_at_period_end: bool = False
    tenant_blocked: bool = False
    tenant_read_only: bool = False

    def has(self, feature: Feature) -> bool:
        return feature in self.features

    @property
    def days_remaining(self) -> int:
        return max(0, (self.period_end - date.today()).days)

    @property
    def nearing_limit(self) -> list[QuotaUsage]:
        """Quotas above 80% — what the dashboard should warn about before the
        customer hits a wall mid-task."""
        return [u for u in self.quotas.values() if not u.unlimited and u.utilisation >= 0.8]


class EntitlementService:
    """Plans, subscriptions, metering and the gate itself."""

    def __init__(self, db: Session) -> None:
        self.db = db

    # ==================================================================
    # Plans
    # ==================================================================
    def sync_catalogue(self) -> int:
        """Write the code catalogue into the `plans` table.

        Insert-only for existing rows: an operator's edited price must survive
        a redeploy. Only genuinely new plans are added.
        """
        written = 0
        for order, tier in enumerate(PLAN_ORDER):
            spec = PLAN_CATALOGUE[tier]
            existing = self.db.scalar(select(Plan).where(Plan.tier == tier.value))
            if existing is not None:
                continue
            self.db.add(Plan(
                tier=tier.value,
                name=spec.name,
                tagline=spec.tagline,
                price_monthly_inr=spec.price_monthly_inr,
                price_annual_inr=spec.price_annual_inr,
                features=sorted(f.value for f in spec.features),
                quotas={q.value: v for q, v in spec.quotas.items()},
                limits={l.value: v for l, v in spec.limits.items()},
                trial_days=spec.trial_days,
                is_public=spec.is_public,
                sort_order=order,
            ))
            written += 1
        if written:
            self.db.commit()
        return written

    def plans(self, *, public_only: bool = False) -> list[Plan]:
        stmt = select(Plan).order_by(Plan.sort_order)
        if public_only:
            stmt = stmt.where(Plan.is_public.is_(True))
        return list(self.db.scalars(stmt))

    def plan_row(self, tier: PlanTier) -> Plan | None:
        return self.db.scalar(select(Plan).where(Plan.tier == tier.value))

    def update_plan(self, tier: PlanTier, **fields) -> Plan:
        row = self.plan_row(tier)
        if row is None:
            raise BillingError(f"no plan '{tier}'")
        editable = {
            "name", "tagline", "price_monthly_inr", "price_annual_inr",
            "features", "quotas", "limits", "trial_days", "is_public",
        }
        for key, value in fields.items():
            if key in editable and value is not None:
                setattr(row, key, value)
        self.db.commit()
        self.db.refresh(row)
        return row

    def spec_for(self, tier: PlanTier, subscription: Subscription | None = None) -> PlanSpec:
        """Resolve the effective plan: table row over code default, with the
        subscription's contract overrides merged on top."""
        base = PLAN_CATALOGUE[tier]
        row = self.plan_row(tier)

        if row is not None:
            features = frozenset(
                Feature(f) for f in (row.features or []) if f in Feature.__members__.values()
            )
            quotas = {
                Quota(k): int(v) for k, v in (row.quotas or {}).items()
                if k in Quota.__members__.values()
            }
            limits = {
                Limit(k): int(v) for k, v in (row.limits or {}).items()
                if k in Limit.__members__.values()
            }
            base = PlanSpec(
                tier=tier, name=row.name, tagline=row.tagline or "",
                price_monthly_inr=row.price_monthly_inr,
                price_annual_inr=row.price_annual_inr,
                features=features or base.features,
                quotas=quotas or dict(base.quotas),
                limits=limits or dict(base.limits),
                trial_days=row.trial_days, is_public=row.is_public,
            )

        if subscription is None:
            return base

        features = set(base.features)
        for key in subscription.feature_overrides or []:
            # A leading "-" removes a feature the plan would otherwise grant.
            if key.startswith("-"):
                name = key[1:]
                if name in Feature.__members__.values():
                    features.discard(Feature(name))
            elif key in Feature.__members__.values():
                features.add(Feature(key))

        quotas = dict(base.quotas)
        for key, value in (subscription.quota_overrides or {}).items():
            if key in Quota.__members__.values():
                quotas[Quota(key)] = int(value)

        limits = dict(base.limits)
        for key, value in (subscription.limit_overrides or {}).items():
            if key in Limit.__members__.values():
                limits[Limit(key)] = int(value)

        return PlanSpec(
            tier=base.tier, name=base.name, tagline=base.tagline,
            price_monthly_inr=base.price_monthly_inr,
            price_annual_inr=base.price_annual_inr,
            features=frozenset(features), quotas=quotas, limits=limits,
            trial_days=base.trial_days, is_public=base.is_public,
        )

    # ==================================================================
    # Subscriptions
    # ==================================================================
    def subscription_for(self, tenant_id: int) -> Subscription:
        sub = self.db.scalar(
            select(Subscription).where(Subscription.tenant_id == tenant_id)
        )
        if sub is None:
            raise BillingError(f"tenant {tenant_id} has no subscription")
        return self._roll_period(sub)

    def _roll_period(self, sub: Subscription) -> Subscription:
        """Advance the metering window if it has elapsed.

        Done lazily on read rather than by a nightly job, so a tenant's quota
        resets the moment their period ends even if the scheduler is down.
        The job exists too; this makes it an optimisation rather than a
        correctness dependency.
        """
        today = date.today()
        if sub.period_end > today:
            return sub

        guard = 0
        while sub.period_end <= today and guard < 120:
            sub.period_start = sub.period_end
            sub.period_end = (
                _add_month(sub.period_start)
                if sub.billing_period == BillingPeriod.MONTHLY.value
                else date(sub.period_start.year + 1, sub.period_start.month,
                          min(sub.period_start.day, 28))
            )
            guard += 1

        if sub.cancel_at_period_end and sub.status != SubscriptionStatus.CANCELLED.value:
            sub.status = SubscriptionStatus.CANCELLED.value

        self.db.commit()
        self.db.refresh(sub)
        return sub

    def change_plan(
        self,
        tenant_id: int,
        tier: PlanTier,
        *,
        billing_period: BillingPeriod = BillingPeriod.MONTHLY,
        status: SubscriptionStatus | None = None,
    ) -> Subscription:
        """Move a tenant to another plan.

        An upgrade takes effect immediately and keeps the current metering
        window — the customer paid for the higher allowance now, and resetting
        the period would hand them a second full allowance for the same month.
        """
        sub = self.subscription_for(tenant_id)
        sub.plan_tier = tier.value
        sub.billing_period = billing_period.value
        sub.status = (status or SubscriptionStatus.ACTIVE).value
        sub.cancel_at_period_end = False
        sub.cancelled_at = None

        tenant = self.db.get(Tenant, tenant_id)
        if tenant is not None and tenant.status in ("trial", "past_due"):
            tenant.status = "active"
        self.db.commit()
        self.db.refresh(sub)
        return sub

    def cancel(self, tenant_id: int, *, immediately: bool = False) -> Subscription:
        sub = self.subscription_for(tenant_id)
        sub.cancelled_at = _utcnow()
        if immediately:
            sub.status = SubscriptionStatus.CANCELLED.value
            sub.cancel_at_period_end = False
        else:
            # Access continues to the end of the paid period. Cutting it off
            # on the day of cancellation is a refund conversation nobody wants.
            sub.cancel_at_period_end = True
        self.db.commit()
        self.db.refresh(sub)
        return sub

    def mark_past_due(self, tenant_id: int) -> Subscription:
        sub = self.subscription_for(tenant_id)
        sub.status = SubscriptionStatus.PAST_DUE.value
        tenant = self.db.get(Tenant, tenant_id)
        if tenant is not None:
            tenant.status = "past_due"
        self.db.commit()
        self.db.refresh(sub)
        return sub

    def mark_paid(self, tenant_id: int) -> Subscription:
        sub = self.subscription_for(tenant_id)
        sub.status = SubscriptionStatus.ACTIVE.value
        tenant = self.db.get(Tenant, tenant_id)
        if tenant is not None and tenant.status == "past_due":
            tenant.status = "active"
        self.db.commit()
        self.db.refresh(sub)
        return sub

    # ==================================================================
    # Metering
    # ==================================================================
    def counter(self, tenant_id: int, quota: Quota, sub: Subscription | None = None) -> UsageCounter:
        """The counter for the current period, created on first touch."""
        sub = sub or self.subscription_for(tenant_id)
        row = self.db.scalar(
            select(UsageCounter).where(
                UsageCounter.tenant_id == tenant_id,
                UsageCounter.quota == quota.value,
                UsageCounter.period_start == sub.period_start,
            )
        )
        if row is None:
            row = UsageCounter(
                tenant_id=tenant_id, quota=quota.value,
                period_start=sub.period_start, period_end=sub.period_end,
                used=0, cost_micros=0,
            )
            self.db.add(row)
            self.db.commit()
            self.db.refresh(row)
        return row

    def usage(self, tenant_id: int, quota: Quota) -> QuotaUsage:
        sub = self.subscription_for(tenant_id)
        spec = self.spec_for(PlanTier(sub.plan_tier), sub)
        return QuotaUsage(
            quota=quota,
            used=self.counter(tenant_id, quota, sub).used,
            allowance=spec.quota(quota),
        )

    def consume(
        self,
        tenant_id: int,
        quota: Quota,
        quantity: int = 1,
        *,
        user_id: str | None = None,
        api_key_id: int | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        cost_micros: int = 0,
        meta: dict | None = None,
    ) -> QuotaUsage:
        """Record consumption. Called *after* the work succeeded.

        Writes both the raw event and the counter. The counter is what the
        gate reads; the event is what a billing dispute is settled with.
        """
        sub = self.subscription_for(tenant_id)
        now = _utcnow()

        self.db.add(UsageEvent(
            tenant_id=tenant_id, user_id=user_id, api_key_id=api_key_id,
            quota=quota.value, quantity=quantity,
            resource_type=resource_type, resource_id=resource_id,
            cost_micros=cost_micros, occurred_at=now, meta=meta,
        ))

        row = self.counter(tenant_id, quota, sub)
        row.used += quantity
        row.cost_micros += cost_micros
        row.last_event_at = now
        self.db.commit()

        spec = self.spec_for(PlanTier(sub.plan_tier), sub)
        return QuotaUsage(quota=quota, used=row.used, allowance=spec.quota(quota))

    # ==================================================================
    # The gate
    # ==================================================================
    def check(
        self,
        tenant_id: int,
        *,
        feature: Feature | None = None,
        quota: Quota | None = None,
        quantity: int = 1,
        limit: Limit | None = None,
        limit_requested: int = 1,
    ) -> Entitlement:
        """The single question every enforcement point asks.

        Delegates the decision to the pure `evaluate()` so the policy is
        testable without a database; this method's only job is to supply
        accurate inputs.
        """
        tenant = self.db.get(Tenant, tenant_id)
        if tenant is None:
            return Entitlement(
                allowed=False, reason=DenialReason.TENANT_SUSPENDED,
                message="Unknown organisation.",
            )

        sub = self.subscription_for(tenant_id)
        spec = self.spec_for(PlanTier(sub.plan_tier), sub)

        quota_used = self.counter(tenant_id, quota, sub).used if quota else 0
        limit_used = self.current_limit_usage(tenant_id, limit) if limit else 0

        return evaluate(
            spec=spec,
            subscription_status=SubscriptionStatus(sub.status),
            tenant_blocked=TenantService.is_blocked(tenant),
            tenant_read_only=(
                TenantService.is_read_only(tenant)
                and (quota is not None or limit is not None or feature is not None)
            ),
            feature=feature,
            quota=quota, quota_used=quota_used, quota_requested=quantity,
            limit=limit, limit_used=limit_used, limit_requested=limit_requested,
        )

    def current_limit_usage(self, tenant_id: int, limit: Limit) -> int:
        """How much of a non-resetting limit is in use.

        Counted from the owning tables rather than a stored tally, because a
        tally that misses one delete path over-charges a customer forever, and
        these counts are small and indexed.
        """
        from app.models.platform import ApiKey, User
        from app.models.portfolio import Portfolio, Watchlist

        if limit is Limit.SEATS:
            return self.db.scalar(
                select(func.count(User.id)).where(
                    User.tenant_id == tenant_id,
                    User.status.in_(("active", "pending")),
                )
            ) or 0

        if limit is Limit.API_KEYS:
            return self.db.scalar(
                select(func.count(ApiKey.id)).where(
                    ApiKey.tenant_id == tenant_id, ApiKey.revoked_at.is_(None),
                )
            ) or 0

        if limit in (Limit.PORTFOLIOS, Limit.WATCHLISTS):
            owners = list(self.db.scalars(
                select(User.id).where(User.tenant_id == tenant_id)
            ))
            if not owners:
                return 0
            model = Portfolio if limit is Limit.PORTFOLIOS else Watchlist
            return self.db.scalar(
                select(func.count(model.id)).where(model.owner_id.in_(owners))
            ) or 0

        if limit is Limit.STORAGE_MB:
            tenant = self.db.get(Tenant, tenant_id)
            return int((tenant.storage_bytes if tenant else 0) / (1024 * 1024))

        # Retention and rate limit are configuration, not consumption.
        return 0

    # ==================================================================
    # Assembled view
    # ==================================================================
    def entitlements(self, tenant_id: int) -> TenantEntitlements:
        """Everything at once, for the dashboard and the /me payload."""
        tenant = self.db.get(Tenant, tenant_id)
        if tenant is None:
            raise BillingError(f"no tenant {tenant_id}")

        sub = self.subscription_for(tenant_id)
        spec = self.spec_for(PlanTier(sub.plan_tier), sub)

        counters = {
            row.quota: row for row in self.db.scalars(
                select(UsageCounter).where(
                    UsageCounter.tenant_id == tenant_id,
                    UsageCounter.period_start == sub.period_start,
                )
            )
        }
        quotas = {
            q: QuotaUsage(
                quota=q,
                used=counters[q.value].used if q.value in counters else 0,
                allowance=spec.quota(q),
            )
            for q in Quota
        }

        limits = {l: spec.limit(l) for l in Limit}
        limit_usage = {l: self.current_limit_usage(tenant_id, l) for l in Limit}

        return TenantEntitlements(
            tenant_id=tenant_id,
            plan=spec,
            status=SubscriptionStatus(sub.status),
            period_start=sub.period_start,
            period_end=sub.period_end,
            features=spec.features,
            quotas=quotas,
            limits=limits,
            limit_usage=limit_usage,
            trial_ends_at=sub.trial_ends_at,
            cancel_at_period_end=sub.cancel_at_period_end,
            tenant_blocked=TenantService.is_blocked(tenant),
            tenant_read_only=TenantService.is_read_only(tenant),
        )

    # ==================================================================
    # Analytics
    # ==================================================================
    def usage_timeseries(
        self,
        tenant_id: int | None,
        quota: Quota,
        *,
        days: int = 30,
    ) -> list[tuple[date, int]]:
        """Daily totals for a chart. `tenant_id=None` aggregates the platform."""
        since = _utcnow() - timedelta(days=days)
        stmt = (
            select(
                func.date(UsageEvent.occurred_at).label("day"),
                func.sum(UsageEvent.quantity),
            )
            .where(UsageEvent.quota == quota.value, UsageEvent.occurred_at >= since)
            .group_by("day").order_by("day")
        )
        if tenant_id is not None:
            stmt = stmt.where(UsageEvent.tenant_id == tenant_id)

        out: list[tuple[date, int]] = []
        for day, total in self.db.execute(stmt):
            out.append((_as_date(day), int(total or 0)))
        return out

    def platform_revenue(self) -> dict[str, int]:
        """MRR and ARR from live subscriptions, in whole rupees.

        Annual subscriptions are divided by twelve so the figure is genuinely
        monthly recurring revenue rather than a mixture of two cadences.
        """
        mrr = 0
        for sub in self.db.scalars(
            select(Subscription).where(
                Subscription.status.in_((
                    SubscriptionStatus.ACTIVE.value, SubscriptionStatus.TRIALING.value,
                ))
            )
        ):
            if sub.status == SubscriptionStatus.TRIALING.value:
                continue  # a trial is not revenue
            spec = self.spec_for(PlanTier(sub.plan_tier), sub)
            mrr += (
                spec.price_annual_inr // 12
                if sub.billing_period == BillingPeriod.ANNUAL.value
                else spec.price_monthly_inr
            )
        return {"mrr_inr": mrr, "arr_inr": mrr * 12}

    def tier_distribution(self) -> dict[str, int]:
        rows = self.db.execute(
            select(Subscription.plan_tier, func.count(Subscription.id))
            .group_by(Subscription.plan_tier)
        )
        counts = {tier.value: 0 for tier in PlanTier}
        for tier, count in rows:
            counts[tier] = int(count)
        return counts


def _as_date(value) -> date:
    """SQLite returns `func.date()` as a string; Postgres returns a date."""
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])
