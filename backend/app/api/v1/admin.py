"""Administration: the tenant console and the platform operator console.

Two audiences, one router, separated by permission rather than by prefix:

* `/admin/*` — a **tenant administrator** managing their own organisation.
  Every read is filtered to their tenant; there is no parameter that widens it.
* `/platform/*` — the **platform operator**. Cross-tenant, guarded by
  `require_operator`, which returns 404 rather than 403 so the console does not
  confirm its own existence to a customer who probes for it.

    -- tenant console ------------------------------------------------
    GET    /admin/overview               dashboard figures for this tenant
    GET    /admin/organisation           the tenant record
    PATCH  /admin/organisation           update it
    PUT    /admin/organisation/settings  merge settings
    GET    /admin/members                list members
    POST   /admin/members                invite one
    GET    /admin/members/{id}           one member
    PATCH  /admin/members/{id}/role      change role
    PATCH  /admin/members/{id}/status    suspend or reactivate
    DELETE /admin/members/{id}           remove
    GET    /admin/api-keys · POST · DELETE /{id}
    GET    /admin/subscription           plan, status, period
    POST   /admin/subscription           change plan
    DELETE /admin/subscription           cancel
    GET    /admin/entitlements           features, quotas, limits
    GET    /admin/usage                  metered consumption
    GET    /admin/usage/series           daily series for a quota
    GET    /admin/audit                  this tenant's trail
    GET    /admin/audit/summary          aggregates
    GET    /admin/storage                storage breakdown
    GET    /admin/jobs                   this tenant's jobs
    GET    /admin/notifications          in-app messages
    GET    /admin/rbac                   the permission matrix

    -- operator console ----------------------------------------------
    GET    /platform/overview            the whole estate
    GET    /platform/tenants · POST · GET/PATCH/DELETE /{id}
    POST   /platform/tenants/{id}/suspend · /reactivate
    PATCH  /platform/tenants/{id}/subscription
    GET    /platform/users               every user
    GET    /platform/plans · PATCH /{tier}
    GET    /platform/audit               every tenant's trail
    GET    /platform/errors · POST /{fingerprint}/resolve
    GET    /platform/metrics · /metrics/routes · /metrics/timeseries
    GET    /platform/jobs · POST /{id}/retry · /cancel · POST /jobs
    GET    /platform/queue · /schedules
    GET    /platform/backups · POST · POST /{id}/verify
    GET    /platform/readiness

Literal paths precede `/{id}` throughout — the trap Modules 7, 8 and 9 each hit
in turn.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import (
    CurrentUser, get_current_user, require, require_admin, require_operator,
    require_tenant,
)
from app.db.base import get_db
from app.domain.platform.audit import (
    AuditAction, AuditCategory, AuditSeverity, mask_secret,
)
from app.domain.platform.identity import (
    Permission, ROLE_DESCRIPTIONS, ROLE_LABELS, ROLE_ORDER, ROLE_PERMISSIONS,
    Role, UserStatus, outranks,
)
from app.domain.platform.jobs import (
    JOB_LABELS, JobKind, JobStatus,
)
from app.domain.platform.plans import (
    FEATURE_LABELS, Feature, LIMIT_LABELS, Limit, PlanTier, QUOTA_LABELS,
    QUOTA_UNITS, Quota, is_unlimited,
)
from app.models.platform import (
    AuditLog, BackupRecord, Notification, ScheduleState, Subscription, Tenant,
    User,
)
from app.schemas.platform import (
    ApiKeyCreate, ApiKeyOut, AuditLogOut, AuditSummaryOut, BackupOut,
    BackupStatusOut, BackupVerifyOut, EntitlementsOut, ErrorEventOut,
    HealthOut, InviteRequest, IssuedApiKeyOut, JobEnqueue, JobOut, LimitOut,
    MessageResponse, MetricsOverviewOut, NotificationOut, Page,
    PlanOut, PlanUpdate, PlatformOverviewOut, QueueDepthOut, QuotaUsageOut,
    RbacMatrixOut, RecycleBinOut, RecycleSoftDeleteRequest, RoleUpdate,
    RouteMetricOut, ScheduleOut, StatusUpdate, SubscriptionChange,
    SubscriptionOut, SubscriptionOverrides, SystemComponentOut,
    SystemStatusOut, TenantCreate,
    TenantDetailOut, TenantOut, TenantSettingsUpdate, TenantSuspend,
    TenantUpdate, UsageOverviewOut, UsagePointOut, UsageSeriesOut,
    UserDetailOut, UserOut,
)
from app.services.platform.api_keys import ApiKeyError, ApiKeyService
from app.services.platform.audit_service import AuditService, RequestContext
from app.services.platform.backup import BackupError, BackupService
from app.services.platform.email import EmailService
from app.services.platform.entitlements import BillingError, EntitlementService
from app.services.platform.identity_service import (
    AuthError, IdentityService, RegistrationError,
)
from app.services.platform.jobs.queue import JobQueue, QueueError
from app.services.platform.observability import (
    ErrorTracker, HealthService, MetricsService,
)
from app.services.platform.recycle_bin import RecycleBinError, RecycleBinService
from app.services.platform.tenancy import TenantError, TenantService

router = APIRouter(tags=["admin"])


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _context(request: Request) -> RequestContext:
    from app.core.security import _client_ip

    return RequestContext(
        ip_address=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
        request_id=getattr(request.state, "request_id", None),
    )


def _masked(record) -> ApiKeyOut:
    out = ApiKeyOut.model_validate(record)
    out.masked = f"{record.prefix}_{record.key_id[:4]}{mask_secret(record.key_id, 4)}"
    return out


def _quota_out(usage) -> QuotaUsageOut:
    return QuotaUsageOut(
        quota=usage.quota.value,
        label=QUOTA_LABELS[usage.quota],
        unit=QUOTA_UNITS[usage.quota],
        used=usage.used,
        allowance=usage.allowance,
        remaining=usage.remaining,
        utilisation=usage.utilisation,
        unlimited=usage.unlimited,
        exhausted=usage.exhausted,
    )


def _member_or_404(db: Session, tenant_id: int, user_id: str) -> User:
    """Fetch a member of *this* tenant.

    404, never 403, when the id belongs to another organisation: a
    distinguishable response would let an admin enumerate the platform's user
    ids by probing.
    """
    user = db.get(User, user_id)
    if user is None or user.tenant_id != tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such member.")
    return user


# ===========================================================================
# ===  TENANT CONSOLE  ======================================================
# ===========================================================================
@router.get(
    "/admin/overview", summary="Tenant dashboard",
    dependencies=[Depends(require(Permission.TENANT_READ))],
)
def admin_overview(
    tenant_id: int = Depends(require_tenant), db: Session = Depends(get_db),
) -> dict[str, object]:
    from app.models.document import Document
    from app.models.portfolio import Portfolio
    from app.models.report import Report

    tenants = TenantService(db)
    entitlements = EntitlementService(db).entitlements(tenant_id)
    tenant = tenants.require(tenant_id)

    member_ids = list(db.scalars(select(User.id).where(User.tenant_id == tenant_id)))

    def _count(model, column) -> int:
        if not member_ids:
            return 0
        return int(db.scalar(
            select(func.count()).select_from(model).where(column.in_(member_ids))
        ) or 0)

    return {
        "tenant": TenantOut.model_validate(tenant).model_dump(),
        "plan": entitlements.plan.name,
        "plan_tier": entitlements.plan.tier.value,
        "status": entitlements.status.value,
        "period_end": entitlements.period_end.isoformat(),
        "days_remaining": entitlements.days_remaining,
        "members": len(member_ids),
        "members_active": int(db.scalar(
            select(func.count(User.id)).where(
                User.tenant_id == tenant_id, User.status == UserStatus.ACTIVE.value,
            )
        ) or 0),
        "documents": _count(Document, Document.uploaded_by),
        "reports": _count(Report, Report.owner_id),
        "portfolios": _count(Portfolio, Portfolio.owner_id),
        "storage": tenants.storage_breakdown(tenant_id),
        "api_keys": ApiKeyService(db).statistics(tenant_id),
        "quotas": [_quota_out(u).model_dump() for u in entitlements.quotas.values()],
        "nearing_limit": [
            _quota_out(u).model_dump() for u in entitlements.nearing_limit
        ],
        "audit_7d": AuditService(db).summary(tenant_id=tenant_id, days=7),
    }


# --- organisation ----------------------------------------------------------
@router.get(
    "/admin/organisation", response_model=TenantDetailOut, summary="Your organisation",
    dependencies=[Depends(require(Permission.TENANT_READ))],
)
def get_organisation(
    tenant_id: int = Depends(require_tenant), db: Session = Depends(get_db),
) -> TenantDetailOut:
    service = TenantService(db)
    tenant = service.require(tenant_id)
    out = TenantDetailOut.model_validate(tenant)
    out.settings = tenant.settings or {}
    out.storage = service.storage_breakdown(tenant_id)
    subscription = db.scalar(
        select(Subscription).where(Subscription.tenant_id == tenant_id)
    )
    out.subscription = SubscriptionOut.model_validate(subscription) if subscription else None
    return out


@router.patch(
    "/admin/organisation", response_model=TenantOut, summary="Update your organisation",
)
def update_organisation(
    payload: TenantUpdate,
    request: Request,
    user: CurrentUser = Depends(require(Permission.TENANT_MANAGE)),
    tenant_id: int = Depends(require_tenant),
    db: Session = Depends(get_db),
) -> TenantOut:
    service = TenantService(db)
    tenant = service.update(service.require(tenant_id), **payload.model_dump())
    AuditService(db).record(
        AuditAction.TENANT_UPDATED, principal=user,
        resource_type="tenant", resource_id=tenant_id,
        summary=f"Organisation '{tenant.name}' updated",
        context=_context(request),
        metadata=payload.model_dump(exclude_none=True),
    )
    return TenantOut.model_validate(tenant)


@router.put(
    "/admin/organisation/settings", response_model=TenantDetailOut, summary="Update settings",
)
def update_settings(
    payload: TenantSettingsUpdate,
    request: Request,
    user: CurrentUser = Depends(require(Permission.TENANT_MANAGE)),
    tenant_id: int = Depends(require_tenant),
    db: Session = Depends(get_db),
) -> TenantDetailOut:
    service = TenantService(db)
    tenant = service.update_settings(service.require(tenant_id), payload.settings)
    AuditService(db).record(
        AuditAction.TENANT_SETTINGS_CHANGED, principal=user,
        resource_type="tenant", resource_id=tenant_id,
        summary="Organisation settings updated", context=_context(request),
        metadata={"keys": sorted(payload.settings)},
    )
    out = TenantDetailOut.model_validate(tenant)
    out.settings = tenant.settings or {}
    out.storage = service.storage_breakdown(tenant_id)
    return out


# --- members ---------------------------------------------------------------
@router.get(
    "/admin/members", response_model=Page[UserOut], summary="List members",
    dependencies=[Depends(require(Permission.MEMBER_READ))],
)
def list_members(
    tenant_id: int = Depends(require_tenant),
    db: Session = Depends(get_db),
    role: str | None = None,
    member_status: str | None = Query(default=None, alias="status"),
    search: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    sort: str = Query(default="created_at"),
    order: str = Query(default="desc", pattern="^(asc|desc)$"),
) -> Page[UserOut]:
    rows, total = IdentityService(db).list_members(
        tenant_id, role=role, status=member_status, search=search,
        offset=(page - 1) * page_size, limit=page_size,
        sort=sort, descending=order == "desc",
    )
    return Page[UserOut](
        items=[UserOut.model_validate(r) for r in rows],
        total=total, page=page, page_size=page_size,
    )


@router.post(
    "/admin/members", response_model=UserOut, status_code=status.HTTP_201_CREATED,
    summary="Invite a member",
)
def invite_member(
    payload: InviteRequest,
    request: Request,
    user: CurrentUser = Depends(require(Permission.MEMBER_MANAGE)),
    tenant_id: int = Depends(require_tenant),
    db: Session = Depends(get_db),
) -> UserOut:
    # Seats are a plan limit, so an invitation is an entitlement decision
    # before it is an identity one.
    decision = EntitlementService(db).check(tenant_id, limit=Limit.SEATS)
    if not decision:
        from app.core.security import _entitlement_error

        raise _entitlement_error(decision)

    if not outranks(user.role, payload.role) and user.role is not Role.SUPER_ADMIN:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "You cannot invite someone at or above your own level.",
        )

    try:
        member, pending = IdentityService(db).invite(
            tenant_id=tenant_id, email=payload.email, name=payload.name,
            role=payload.role, invited_by=user.user_id,
        )
    except RegistrationError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    tenant = TenantService(db).require(tenant_id)
    EmailService().send_invitation(
        to=member.email, name=member.name, organisation=tenant.name,
        inviter=user.name, token=pending.token,
    )

    AuditService(db).record(
        AuditAction.USER_INVITED, principal=user,
        resource_type="user", resource_id=member.id,
        summary=f"{member.email} invited as {ROLE_LABELS[payload.role]}",
        context=_context(request),
    )
    return UserOut.model_validate(member)


@router.get(
    "/admin/members/{user_id}", response_model=UserDetailOut, summary="One member",
    dependencies=[Depends(require(Permission.MEMBER_READ))],
)
def get_member(
    user_id: str,
    tenant_id: int = Depends(require_tenant),
    db: Session = Depends(get_db),
) -> UserDetailOut:
    member = _member_or_404(db, tenant_id, user_id)
    service = IdentityService(db)
    out = UserDetailOut.model_validate(member)
    out.permissions = sorted(p.value for p in ROLE_PERMISSIONS[Role(member.role)])
    out.active_sessions = len(service.active_sessions(member.id))
    out.identities = [i.provider for i in member.identities]
    return out


@router.patch(
    "/admin/members/{user_id}/role", response_model=UserOut, summary="Change a member's role",
)
def change_member_role(
    user_id: str,
    payload: RoleUpdate,
    request: Request,
    user: CurrentUser = Depends(require(Permission.MEMBER_MANAGE)),
    tenant_id: int = Depends(require_tenant),
    db: Session = Depends(get_db),
) -> UserOut:
    member = _member_or_404(db, tenant_id, user_id)
    service = IdentityService(db)

    demoting = payload.role not in (Role.ADMIN, Role.SUPER_ADMIN)
    if demoting and Role(member.role) in (Role.ADMIN, Role.SUPER_ADMIN):
        if service.last_admin_check(tenant_id, excluding=member.id):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "This is the organisation's last administrator. Promote "
                "someone else first.",
            )

    previous = member.role
    try:
        member = service.change_role(user, member, payload.role)
    except AuthError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc

    # Their API keys still carry the old role in their own column, so they
    # would outlive the demotion. Revoke them.
    ApiKeyService(db).revoke_for_user(member.id, "role_changed")

    AuditService(db).record(
        AuditAction.USER_ROLE_CHANGED, principal=user,
        resource_type="user", resource_id=member.id,
        summary=f"{member.email}: {previous} → {member.role}",
        context=_context(request),
        metadata={"from": previous, "to": member.role},
    )
    return UserOut.model_validate(member)


@router.patch(
    "/admin/members/{user_id}/status", response_model=UserOut, summary="Suspend or reactivate",
)
def change_member_status(
    user_id: str,
    payload: StatusUpdate,
    request: Request,
    user: CurrentUser = Depends(require(Permission.MEMBER_MANAGE)),
    tenant_id: int = Depends(require_tenant),
    db: Session = Depends(get_db),
) -> UserOut:
    member = _member_or_404(db, tenant_id, user_id)
    service = IdentityService(db)

    if payload.status is not UserStatus.ACTIVE and Role(member.role) in (
        Role.ADMIN, Role.SUPER_ADMIN,
    ):
        if service.last_admin_check(tenant_id, excluding=member.id):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "This is the organisation's last active administrator.",
            )

    try:
        member = service.set_status(user, member, payload.status)
    except AuthError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc

    if payload.status is not UserStatus.ACTIVE:
        ApiKeyService(db).revoke_for_user(member.id, f"status_{payload.status.value}")

    AuditService(db).record(
        AuditAction.USER_SUSPENDED if payload.status is not UserStatus.ACTIVE
        else AuditAction.USER_REACTIVATED,
        principal=user, resource_type="user", resource_id=member.id,
        summary=f"{member.email} set to {payload.status.value}",
        context=_context(request),
    )
    return UserOut.model_validate(member)


@router.delete(
    "/admin/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a member",
)
def remove_member(
    user_id: str,
    request: Request,
    user: CurrentUser = Depends(require(Permission.MEMBER_MANAGE)),
    tenant_id: int = Depends(require_tenant),
    db: Session = Depends(get_db),
) -> Response:
    member = _member_or_404(db, tenant_id, user_id)
    service = IdentityService(db)

    if member.id == user.user_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "You cannot remove yourself.")
    if not outranks(user.role, Role(member.role)):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "You cannot remove a member at or above your own level.",
        )
    if service.last_admin_check(tenant_id, excluding=member.id) and Role(member.role) in (
        Role.ADMIN, Role.SUPER_ADMIN,
    ):
        raise HTTPException(
            status.HTTP_409_CONFLICT, "This is the organisation's last administrator.",
        )

    email = member.email
    # Deactivate rather than delete. Their work — reports, portfolios,
    # transactions — is referenced by `owner_id` across four modules, and
    # deleting the row would either orphan or cascade away a research record
    # somebody may need to produce years later.
    service.set_status(user, member, UserStatus.DISABLED)
    service.revoke_all_sessions(member.id, "removed")
    ApiKeyService(db).revoke_for_user(member.id, "removed")
    TenantService(db).refresh_member_count(tenant_id)

    AuditService(db).record(
        AuditAction.USER_DELETED, principal=user,
        resource_type="user", resource_id=member.id,
        summary=f"{email} deactivated and all credentials revoked",
        context=_context(request),
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- API keys --------------------------------------------------------------
@router.get(
    "/admin/api-keys", response_model=list[ApiKeyOut], summary="List API keys",
    dependencies=[Depends(require(Permission.APIKEY_MANAGE))],
)
def list_api_keys(
    tenant_id: int = Depends(require_tenant),
    db: Session = Depends(get_db),
    include_revoked: bool = False,
) -> list[ApiKeyOut]:
    return [
        _masked(k)
        for k in ApiKeyService(db).list(tenant_id, include_revoked=include_revoked)
    ]


@router.post(
    "/admin/api-keys", response_model=IssuedApiKeyOut,
    status_code=status.HTTP_201_CREATED, summary="Create an API key",
)
def create_api_key(
    payload: ApiKeyCreate,
    request: Request,
    user: CurrentUser = Depends(require(Permission.APIKEY_MANAGE)),
    tenant_id: int = Depends(require_tenant),
    db: Session = Depends(get_db),
) -> IssuedApiKeyOut:
    decision = EntitlementService(db).check(
        tenant_id, feature=Feature.API_ACCESS, limit=Limit.API_KEYS,
    )
    if not decision:
        from app.core.security import _entitlement_error

        raise _entitlement_error(decision)

    try:
        issued = ApiKeyService(db).create(
            principal=user, name=payload.name, role=payload.role,
            expires_in_days=payload.expires_in_days,
        )
    except ApiKeyError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    AuditService(db).record(
        AuditAction.APIKEY_CREATED, principal=user,
        resource_type="api_key", resource_id=issued.record.id,
        summary=f"API key '{issued.record.name}' issued ({payload.role.value})",
        context=_context(request),
        # The plaintext is deliberately absent, and `redact` would strip it
        # anyway if a future edit tried to add it.
        metadata={"key_id": issued.record.key_id, "role": payload.role.value},
    )
    return IssuedApiKeyOut(key=_masked(issued.record), plaintext=issued.plaintext)


@router.delete(
    "/admin/api-keys/{key_id}", response_model=ApiKeyOut, summary="Revoke an API key",
)
def revoke_api_key(
    key_id: int,
    request: Request,
    user: CurrentUser = Depends(require(Permission.APIKEY_MANAGE)),
    tenant_id: int = Depends(require_tenant),
    db: Session = Depends(get_db),
) -> ApiKeyOut:
    try:
        record = ApiKeyService(db).revoke(tenant_id, key_id)
    except ApiKeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc

    AuditService(db).record(
        AuditAction.APIKEY_REVOKED, principal=user,
        resource_type="api_key", resource_id=key_id,
        summary=f"API key '{record.name}' revoked", context=_context(request),
    )
    return _masked(record)


# --- subscription and entitlements ----------------------------------------
@router.get(
    "/admin/subscription", response_model=SubscriptionOut, summary="Your subscription",
    dependencies=[Depends(require(Permission.SUBSCRIPTION_READ))],
)
def get_subscription(
    tenant_id: int = Depends(require_tenant), db: Session = Depends(get_db),
) -> SubscriptionOut:
    try:
        return SubscriptionOut.model_validate(
            EntitlementService(db).subscription_for(tenant_id)
        )
    except BillingError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


@router.post(
    "/admin/subscription", response_model=SubscriptionOut, summary="Change plan",
)
def change_subscription(
    payload: SubscriptionChange,
    request: Request,
    user: CurrentUser = Depends(require(Permission.SUBSCRIPTION_MANAGE)),
    tenant_id: int = Depends(require_tenant),
    db: Session = Depends(get_db),
) -> SubscriptionOut:
    service = EntitlementService(db)
    previous = service.subscription_for(tenant_id).plan_tier
    subscription = service.change_plan(
        tenant_id, payload.tier, billing_period=payload.billing_period,
    )
    AuditService(db).record(
        AuditAction.SUBSCRIPTION_CHANGED, principal=user,
        resource_type="subscription", resource_id=subscription.id,
        summary=f"Plan changed: {previous} → {payload.tier.value}",
        context=_context(request),
        metadata={"from": previous, "to": payload.tier.value},
    )
    return SubscriptionOut.model_validate(subscription)


@router.delete(
    "/admin/subscription", response_model=SubscriptionOut, summary="Cancel",
)
def cancel_subscription(
    request: Request,
    user: CurrentUser = Depends(require(Permission.SUBSCRIPTION_MANAGE)),
    tenant_id: int = Depends(require_tenant),
    db: Session = Depends(get_db),
    immediately: bool = False,
) -> SubscriptionOut:
    subscription = EntitlementService(db).cancel(tenant_id, immediately=immediately)
    AuditService(db).record(
        AuditAction.SUBSCRIPTION_CANCELLED, principal=user,
        resource_type="subscription", resource_id=subscription.id,
        summary=(
            "Subscription cancelled immediately" if immediately
            else "Subscription will end at the close of the current period"
        ),
        context=_context(request),
    )
    return SubscriptionOut.model_validate(subscription)


@router.get(
    "/admin/entitlements", response_model=EntitlementsOut, summary="Features, quotas and limits",
    dependencies=[Depends(require(Permission.SUBSCRIPTION_READ))],
)
def get_entitlements(
    tenant_id: int = Depends(require_tenant), db: Session = Depends(get_db),
) -> EntitlementsOut:
    e = EntitlementService(db).entitlements(tenant_id)

    warnings: list[str] = []
    for usage in e.nearing_limit:
        warnings.append(
            f"{QUOTA_LABELS[usage.quota]} at {usage.utilisation:.0%} of the "
            f"{e.plan.name} allowance."
        )
    if e.tenant_read_only:
        warnings.append("Billing is past due — the workspace is read-only.")
    if e.cancel_at_period_end:
        warnings.append(f"Access ends on {e.period_end.isoformat()}.")

    return EntitlementsOut(
        tenant_id=tenant_id,
        plan_tier=e.plan.tier.value,
        plan_name=e.plan.name,
        status=e.status.value,
        period_start=e.period_start,
        period_end=e.period_end,
        days_remaining=e.days_remaining,
        trial_ends_at=e.trial_ends_at,
        cancel_at_period_end=e.cancel_at_period_end,
        read_only=e.tenant_read_only,
        blocked=e.tenant_blocked,
        features=sorted(f.value for f in e.features),
        all_features=[
            {
                "key": f.value,
                "label": FEATURE_LABELS[f],
                "included": f in e.features,
            }
            for f in Feature
        ],
        quotas=[_quota_out(u) for u in e.quotas.values()],
        limits=[
            LimitOut(
                limit=l.value, label=LIMIT_LABELS[l],
                used=e.limit_usage[l], allowance=e.limits[l],
                unlimited=is_unlimited(e.limits[l]),
            )
            for l in Limit
        ],
        warnings=warnings,
    )


# --- usage -----------------------------------------------------------------
@router.get(
    "/admin/usage", response_model=UsageOverviewOut, summary="Metered usage",
    dependencies=[Depends(require(Permission.USAGE_READ))],
)
def get_usage(
    tenant_id: int = Depends(require_tenant),
    db: Session = Depends(get_db),
    days: int = Query(default=30, ge=1, le=365),
) -> UsageOverviewOut:
    from app.models.platform import UsageEvent

    service = EntitlementService(db)
    e = service.entitlements(tenant_id)

    series: list[UsageSeriesOut] = []
    for quota in (Quota.AI_CALLS, Quota.REPORTS_GENERATED,
                  Quota.DOCUMENTS_PROCESSED, Quota.API_REQUESTS):
        points = service.usage_timeseries(tenant_id, quota, days=days)
        series.append(UsageSeriesOut(
            quota=quota.value, label=QUOTA_LABELS[quota], unit=QUOTA_UNITS[quota],
            points=[UsagePointOut(date=d, value=v) for d, v in points],
            total=sum(v for _, v in points),
        ))

    since = _utcnow() - timedelta(days=days)
    top = db.execute(
        select(
            UsageEvent.user_id,
            func.sum(UsageEvent.quantity),
            func.count(UsageEvent.id),
        )
        .where(UsageEvent.tenant_id == tenant_id, UsageEvent.occurred_at >= since)
        .group_by(UsageEvent.user_id)
        .order_by(func.sum(UsageEvent.quantity).desc())
        .limit(10)
    )
    names = {u.id: u.name for u in db.scalars(select(User).where(User.tenant_id == tenant_id))}

    cost = db.scalar(
        select(func.coalesce(func.sum(UsageEvent.cost_micros), 0))
        .where(UsageEvent.tenant_id == tenant_id, UsageEvent.occurred_at >= since)
    ) or 0

    return UsageOverviewOut(
        tenant_id=tenant_id,
        period_start=e.period_start,
        period_end=e.period_end,
        quotas=[_quota_out(u) for u in e.quotas.values()],
        series=series,
        top_users=[
            {
                "user_id": uid,
                "name": names.get(uid, "Unknown"),
                "units": int(total or 0),
                "events": int(count or 0),
            }
            for uid, total, count in top if uid
        ],
        cost_usd=round(float(cost) / 1_000_000, 4),
    )


@router.get(
    "/admin/usage/series", response_model=UsageSeriesOut, summary="One quota over time",
    dependencies=[Depends(require(Permission.USAGE_READ))],
)
def usage_series(
    quota: Quota,
    tenant_id: int = Depends(require_tenant),
    db: Session = Depends(get_db),
    days: int = Query(default=30, ge=1, le=365),
) -> UsageSeriesOut:
    points = EntitlementService(db).usage_timeseries(tenant_id, quota, days=days)
    return UsageSeriesOut(
        quota=quota.value, label=QUOTA_LABELS[quota], unit=QUOTA_UNITS[quota],
        points=[UsagePointOut(date=d, value=v) for d, v in points],
        total=sum(v for _, v in points),
    )


# --- audit -----------------------------------------------------------------
@router.get(
    "/admin/audit", response_model=Page[AuditLogOut], summary="Your audit trail",
    dependencies=[Depends(require(Permission.AUDIT_READ))],
)
def tenant_audit(
    tenant_id: int = Depends(require_tenant),
    db: Session = Depends(get_db),
    action: str | None = None,
    category: AuditCategory | None = None,
    severity: AuditSeverity | None = None,
    actor_id: str | None = None,
    outcome: str | None = None,
    search: str | None = None,
    days: int = Query(default=30, ge=1, le=365),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
) -> Page[AuditLogOut]:
    rows, total = AuditService(db).query(
        tenant_id=tenant_id, unrestricted=False,
        action=action, category=category, severity=severity,
        actor_id=actor_id, outcome=outcome, search=search,
        since=_utcnow() - timedelta(days=days),
        offset=(page - 1) * page_size, limit=page_size,
    )
    return Page[AuditLogOut](
        items=[AuditLogOut.model_validate(r) for r in rows],
        total=total, page=page, page_size=page_size,
    )


@router.get(
    "/admin/audit/summary", response_model=AuditSummaryOut, summary="Audit aggregates",
    dependencies=[Depends(require(Permission.AUDIT_READ))],
)
def tenant_audit_summary(
    tenant_id: int = Depends(require_tenant),
    db: Session = Depends(get_db),
    days: int = Query(default=7, ge=1, le=90),
) -> AuditSummaryOut:
    return AuditSummaryOut(**AuditService(db).summary(tenant_id=tenant_id, days=days))


# --- storage, jobs, notifications, rbac ------------------------------------
@router.get(
    "/admin/storage", summary="Storage breakdown",
    dependencies=[Depends(require(Permission.TENANT_READ))],
)
def storage(
    tenant_id: int = Depends(require_tenant), db: Session = Depends(get_db),
) -> dict[str, object]:
    service = TenantService(db)
    breakdown = service.storage_breakdown(tenant_id)
    service.sync_storage(tenant_id)
    entitlements = EntitlementService(db).entitlements(tenant_id)
    allowance_mb = entitlements.limits[Limit.STORAGE_MB]
    return {
        "breakdown": breakdown,
        "total_bytes": breakdown["total"],
        "total_mb": round(breakdown["total"] / (1024 * 1024), 2),
        "allowance_mb": allowance_mb,
        "unlimited": is_unlimited(allowance_mb),
        "utilisation": (
            0.0 if is_unlimited(allowance_mb) or allowance_mb <= 0
            else round(breakdown["total"] / (allowance_mb * 1024 * 1024), 4)
        ),
    }


@router.get(
    "/admin/jobs", response_model=Page[JobOut], summary="Your background jobs",
    dependencies=[Depends(require(Permission.JOB_READ))],
)
def tenant_jobs(
    tenant_id: int = Depends(require_tenant),
    db: Session = Depends(get_db),
    kind: JobKind | None = None,
    job_status: JobStatus | None = Query(default=None, alias="status"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
) -> Page[JobOut]:
    rows, total = JobQueue(db).list(
        tenant_id=tenant_id, kind=kind, status=job_status,
        offset=(page - 1) * page_size, limit=page_size,
    )
    return Page[JobOut](
        items=[JobOut.model_validate(r) for r in rows],
        total=total, page=page, page_size=page_size,
    )


@router.get(
    "/admin/notifications", response_model=list[NotificationOut], summary="Your notifications",
)
def notifications(
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
    unread_only: bool = False,
    limit: int = Query(default=30, ge=1, le=100),
) -> list[NotificationOut]:
    stmt = select(Notification).where(Notification.user_id == user.user_id)
    if unread_only:
        stmt = stmt.where(Notification.read_at.is_(None))
    rows = db.scalars(stmt.order_by(Notification.created_at.desc()).limit(limit))
    return [NotificationOut.model_validate(r) for r in rows]


@router.get("/admin/rbac", response_model=RbacMatrixOut, summary="The permission matrix")
def rbac_matrix(user: CurrentUser = Depends(get_current_user)) -> RbacMatrixOut:
    """Served rather than duplicated in the frontend.

    The admin panel renders exactly what this returns, so the documented
    matrix and the enforced one cannot diverge — they are the same object.
    """
    return RbacMatrixOut(
        roles=[
            {
                "key": role.value,
                "label": ROLE_LABELS[role],
                "description": ROLE_DESCRIPTIONS[role],
                "seniority": index,
                "permission_count": len(ROLE_PERMISSIONS[role]),
            }
            for index, role in enumerate(ROLE_ORDER)
        ],
        permissions=[
            {
                "key": permission.value,
                "resource": permission.value.split(":")[0],
                "verb": permission.value.split(":")[1],
            }
            for permission in Permission
        ],
        matrix={
            role.value: sorted(p.value for p in ROLE_PERMISSIONS[role])
            for role in ROLE_ORDER
        },
    )


# ===========================================================================
# ===  PLATFORM OPERATOR CONSOLE  ===========================================
# ===========================================================================
@router.get(
    "/platform/overview", response_model=PlatformOverviewOut, summary="Platform overview",
    dependencies=[Depends(require_operator)],
)
def platform_overview(db: Session = Depends(get_db)) -> PlatformOverviewOut:
    from app.models.document import Document
    from app.models.platform import UsageEvent
    from app.models.portfolio import Portfolio
    from app.models.report import Report

    billing = EntitlementService(db)
    revenue = billing.platform_revenue()
    queue = JobQueue(db).depth()
    metrics = MetricsService(db).overview(minutes=1440)

    def _tenants(status_value: str) -> int:
        return int(db.scalar(
            select(func.count(Tenant.id)).where(Tenant.status == status_value)
        ) or 0)

    ai_30d = db.scalar(
        select(func.coalesce(func.sum(UsageEvent.quantity), 0)).where(
            UsageEvent.quota == Quota.AI_CALLS.value,
            UsageEvent.occurred_at >= _utcnow() - timedelta(days=30),
        )
    ) or 0

    health = HealthService(db).readiness()

    return PlatformOverviewOut(
        tenants=int(db.scalar(select(func.count(Tenant.id))) or 0),
        tenants_active=_tenants("active"),
        tenants_trial=_tenants("trial"),
        tenants_past_due=_tenants("past_due"),
        users=int(db.scalar(select(func.count(User.id))) or 0),
        users_active=int(db.scalar(
            select(func.count(User.id)).where(User.status == UserStatus.ACTIVE.value)
        ) or 0),
        mrr_inr=revenue["mrr_inr"],
        arr_inr=revenue["arr_inr"],
        tier_distribution=billing.tier_distribution(),
        storage_bytes=int(db.scalar(
            select(func.coalesce(func.sum(Tenant.storage_bytes), 0))
        ) or 0),
        documents=int(db.scalar(select(func.count(Document.id))) or 0),
        reports=int(db.scalar(select(func.count(Report.id))) or 0),
        portfolios=int(db.scalar(select(func.count(Portfolio.id))) or 0),
        ai_calls_30d=int(ai_30d),
        requests_24h=metrics["requests"],
        error_rate=metrics["error_rate"],
        queue=QueueDepthOut(
            queued=queue.queued, running=queue.running, failed=queue.failed,
            dead_letter=queue.dead_letter, succeeded_24h=queue.succeeded_24h,
            backlog=queue.backlog,
            oldest_queued_seconds=queue.oldest_queued_seconds,
            p50_duration_ms=queue.p50_duration_ms,
            p95_duration_ms=queue.p95_duration_ms,
            by_kind=queue.by_kind, healthy=queue.is_healthy,
        ),
        health=health.status,
    )


# --- tenants ---------------------------------------------------------------
@router.get(
    "/platform/tenants", response_model=Page[TenantOut], summary="Every organisation",
    dependencies=[Depends(require_operator)],
)
def list_tenants(
    db: Session = Depends(get_db),
    tenant_status: str | None = Query(default=None, alias="status"),
    search: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    sort: str = "created_at",
    order: str = Query(default="desc", pattern="^(asc|desc)$"),
) -> Page[TenantOut]:
    rows, total = TenantService(db).list(
        status=tenant_status, search=search,
        offset=(page - 1) * page_size, limit=page_size,
        sort=sort, descending=order == "desc",
    )
    return Page[TenantOut](
        items=[TenantOut.model_validate(r) for r in rows],
        total=total, page=page, page_size=page_size,
    )


@router.post(
    "/platform/tenants", response_model=TenantOut, status_code=status.HTTP_201_CREATED,
    summary="Create an organisation",
)
def create_tenant(
    payload: TenantCreate,
    request: Request,
    user: CurrentUser = Depends(require_operator),
    db: Session = Depends(get_db),
) -> TenantOut:
    try:
        tenant = TenantService(db).create(
            payload.name, slug=payload.slug, tier=payload.tier,
            industry=payload.industry, country=payload.country,
        )
    except TenantError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    AuditService(db).record(
        AuditAction.TENANT_CREATED, principal=user, tenant_id=tenant.id,
        resource_type="tenant", resource_id=tenant.id,
        summary=f"Organisation '{tenant.name}' created on {payload.tier.value}",
        context=_context(request),
    )
    return TenantOut.model_validate(tenant)


@router.get(
    "/platform/tenants/{tenant_id}", response_model=TenantDetailOut, summary="One organisation",
    dependencies=[Depends(require_operator)],
)
def get_tenant(tenant_id: int, db: Session = Depends(get_db)) -> TenantDetailOut:
    service = TenantService(db)
    tenant = service.get(tenant_id)
    if tenant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such organisation.")
    out = TenantDetailOut.model_validate(tenant)
    out.settings = tenant.settings or {}
    out.storage = service.storage_breakdown(tenant_id)
    subscription = db.scalar(
        select(Subscription).where(Subscription.tenant_id == tenant_id)
    )
    out.subscription = SubscriptionOut.model_validate(subscription) if subscription else None
    return out


@router.post(
    "/platform/tenants/{tenant_id}/suspend", response_model=TenantOut, summary="Suspend",
)
def suspend_tenant(
    tenant_id: int,
    payload: TenantSuspend,
    request: Request,
    user: CurrentUser = Depends(require_operator),
    db: Session = Depends(get_db),
) -> TenantOut:
    service = TenantService(db)
    tenant = service.get(tenant_id)
    if tenant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such organisation.")

    tenant = service.suspend(tenant, payload.reason)
    # Suspension must end access now, not in fifteen minutes.
    identity = IdentityService(db)
    for member in db.scalars(select(User).where(User.tenant_id == tenant_id)):
        identity.revoke_all_sessions(member.id, "tenant_suspended")

    AuditService(db).record(
        AuditAction.TENANT_SUSPENDED, principal=user, tenant_id=tenant_id,
        resource_type="tenant", resource_id=tenant_id,
        summary=f"'{tenant.name}' suspended: {payload.reason}",
        context=_context(request),
    )
    return TenantOut.model_validate(tenant)


@router.post(
    "/platform/tenants/{tenant_id}/reactivate", response_model=TenantOut, summary="Reactivate",
)
def reactivate_tenant(
    tenant_id: int,
    request: Request,
    user: CurrentUser = Depends(require_operator),
    db: Session = Depends(get_db),
) -> TenantOut:
    service = TenantService(db)
    tenant = service.get(tenant_id)
    if tenant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such organisation.")
    tenant = service.reactivate(tenant)
    AuditService(db).record(
        AuditAction.TENANT_REACTIVATED, principal=user, tenant_id=tenant_id,
        resource_type="tenant", resource_id=tenant_id,
        summary=f"'{tenant.name}' reactivated", context=_context(request),
    )
    return TenantOut.model_validate(tenant)


@router.patch(
    "/platform/tenants/{tenant_id}/subscription", response_model=SubscriptionOut,
    summary="Set contract overrides",
)
def override_subscription(
    tenant_id: int,
    payload: SubscriptionOverrides,
    request: Request,
    user: CurrentUser = Depends(require_operator),
    db: Session = Depends(get_db),
) -> SubscriptionOut:
    service = EntitlementService(db)
    try:
        subscription = service.subscription_for(tenant_id)
    except BillingError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc

    if payload.quota_overrides is not None:
        subscription.quota_overrides = payload.quota_overrides
    if payload.limit_overrides is not None:
        subscription.limit_overrides = payload.limit_overrides
    if payload.feature_overrides is not None:
        subscription.feature_overrides = payload.feature_overrides
    db.commit()
    db.refresh(subscription)

    AuditService(db).record(
        AuditAction.SUBSCRIPTION_CHANGED, principal=user, tenant_id=tenant_id,
        resource_type="subscription", resource_id=subscription.id,
        summary="Contract overrides applied", context=_context(request),
        metadata=payload.model_dump(exclude_none=True),
    )
    return SubscriptionOut.model_validate(subscription)


@router.get(
    "/platform/users", response_model=Page[UserOut], summary="Every user",
    dependencies=[Depends(require_operator)],
)
def list_all_users(
    db: Session = Depends(get_db),
    tenant_id: int | None = None,
    role: str | None = None,
    user_status: str | None = Query(default=None, alias="status"),
    search: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
) -> Page[UserOut]:
    stmt = select(User)
    if tenant_id is not None:
        stmt = stmt.where(User.tenant_id == tenant_id)
    if role:
        stmt = stmt.where(User.role == role)
    if user_status:
        stmt = stmt.where(User.status == user_status)
    if search:
        like = f"%{search.lower()}%"
        stmt = stmt.where(
            func.lower(User.email).like(like) | func.lower(User.name).like(like)
        )
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(
        stmt.order_by(User.created_at.desc())
        .offset((page - 1) * page_size).limit(page_size)
    )
    return Page[UserOut](
        items=[UserOut.model_validate(r) for r in rows],
        total=int(total), page=page, page_size=page_size,
    )


# --- plans -----------------------------------------------------------------
@router.get("/platform/plans", response_model=list[PlanOut], summary="Every plan")
def list_plans(
    db: Session = Depends(get_db), public_only: bool = False,
) -> list[PlanOut]:
    """Readable without authentication when `public_only` — this is the
    pricing page's data source, and a pricing page behind a login is not a
    pricing page."""
    service = EntitlementService(db)
    if not service.plans():
        service.sync_catalogue()
    return [PlanOut.model_validate(p) for p in service.plans(public_only=public_only)]


@router.patch(
    "/platform/plans/{tier}", response_model=PlanOut, summary="Edit a plan",
    dependencies=[Depends(require_operator)],
)
def update_plan(
    tier: PlanTier,
    payload: PlanUpdate,
    request: Request,
    # Operator status opens the console; `plan:manage` is the specific
    # capability to change what customers are charged. Naming it separately
    # means a future read-only operator role is expressible without a rewrite.
    user: CurrentUser = Depends(require(Permission.PLAN_MANAGE)),
    db: Session = Depends(get_db),
) -> PlanOut:
    try:
        plan = EntitlementService(db).update_plan(
            tier, **payload.model_dump(exclude_none=True)
        )
    except BillingError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc

    AuditService(db).record(
        AuditAction.PLAN_UPDATED, principal=user,
        resource_type="plan", resource_id=tier.value,
        summary=f"Plan '{tier.value}' updated", context=_context(request),
        metadata=payload.model_dump(exclude_none=True),
    )
    return PlanOut.model_validate(plan)


# --- audit, errors, metrics ------------------------------------------------
@router.get(
    "/platform/audit", response_model=Page[AuditLogOut], summary="Every audit row",
    dependencies=[Depends(require_operator)],
)
def platform_audit(
    db: Session = Depends(get_db),
    tenant_id: int | None = None,
    action: str | None = None,
    category: AuditCategory | None = None,
    severity: AuditSeverity | None = None,
    outcome: str | None = None,
    search: str | None = None,
    days: int = Query(default=30, ge=1, le=365),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
) -> Page[AuditLogOut]:
    rows, total = AuditService(db).query(
        tenant_id=tenant_id, unrestricted=True,
        action=action, category=category, severity=severity,
        outcome=outcome, search=search,
        since=_utcnow() - timedelta(days=days),
        offset=(page - 1) * page_size, limit=page_size,
    )
    return Page[AuditLogOut](
        items=[AuditLogOut.model_validate(r) for r in rows],
        total=total, page=page, page_size=page_size,
    )


@router.get(
    "/platform/errors", response_model=Page[ErrorEventOut], summary="Tracked errors",
    dependencies=[Depends(require_operator), Depends(require(Permission.SYSTEM_READ))],
)
def list_errors(
    db: Session = Depends(get_db),
    resolved: bool | None = False,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
) -> Page[ErrorEventOut]:
    rows, total = ErrorTracker(db).list(
        resolved=resolved, offset=(page - 1) * page_size, limit=page_size,
    )
    return Page[ErrorEventOut](
        items=[ErrorEventOut.model_validate(r) for r in rows],
        total=total, page=page, page_size=page_size,
    )


@router.post(
    "/platform/errors/{fingerprint}/resolve", response_model=ErrorEventOut,
    summary="Mark an error resolved",
)
def resolve_error(
    fingerprint: str,
    user: CurrentUser = Depends(require_operator),
    db: Session = Depends(get_db),
) -> ErrorEventOut:
    row = ErrorTracker(db).resolve(fingerprint, user.user_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such error.")
    return ErrorEventOut.model_validate(row)


@router.get(
    "/platform/metrics", response_model=MetricsOverviewOut, summary="Request metrics",
    dependencies=[Depends(require_operator), Depends(require(Permission.SYSTEM_READ))],
)
def platform_metrics(
    db: Session = Depends(get_db),
    minutes: int = Query(default=60, ge=1, le=10_080),
) -> MetricsOverviewOut:
    return MetricsOverviewOut(**MetricsService(db).overview(minutes=minutes))


@router.get(
    "/platform/metrics/routes", response_model=list[RouteMetricOut], summary="Per-route metrics",
    dependencies=[Depends(require_operator), Depends(require(Permission.SYSTEM_READ))],
)
def route_metrics(
    db: Session = Depends(get_db),
    minutes: int = Query(default=60, ge=1, le=10_080),
    limit: int = Query(default=20, ge=1, le=100),
) -> list[RouteMetricOut]:
    return [
        RouteMetricOut(**row)
        for row in MetricsService(db).by_route(minutes=minutes, limit=limit)
    ]


@router.get(
    "/platform/metrics/timeseries", summary="Requests over time",
    dependencies=[Depends(require_operator), Depends(require(Permission.SYSTEM_READ))],
)
def metrics_timeseries(
    db: Session = Depends(get_db),
    minutes: int = Query(default=60, ge=1, le=10_080),
) -> list[dict[str, object]]:
    return MetricsService(db).timeseries(minutes=minutes)


# --- jobs and queue --------------------------------------------------------
@router.get(
    "/platform/jobs", response_model=Page[JobOut], summary="Every job",
    dependencies=[Depends(require_operator)],
)
def platform_jobs(
    db: Session = Depends(get_db),
    tenant_id: int | None = None,
    kind: JobKind | None = None,
    job_status: JobStatus | None = Query(default=None, alias="status"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
) -> Page[JobOut]:
    rows, total = JobQueue(db).list(
        tenant_id=tenant_id, kind=kind, status=job_status,
        offset=(page - 1) * page_size, limit=page_size,
    )
    return Page[JobOut](
        items=[JobOut.model_validate(r) for r in rows],
        total=total, page=page, page_size=page_size,
    )


@router.post(
    "/platform/jobs", response_model=JobOut, status_code=status.HTTP_201_CREATED,
    summary="Enqueue a job",
    dependencies=[Depends(require_operator)],
)
def enqueue_job(
    payload: JobEnqueue,
    request: Request,
    user: CurrentUser = Depends(require(Permission.JOB_MANAGE)),
    db: Session = Depends(get_db),
) -> JobOut:
    job = JobQueue(db).enqueue(
        payload.kind, payload=payload.payload, tenant_id=user.tenant_id,
    )
    AuditService(db).record(
        AuditAction.JOB_ENQUEUED, principal=user,
        resource_type="job", resource_id=job.id,
        summary=f"{JOB_LABELS[payload.kind]} enqueued manually",
        context=_context(request),
    )
    return JobOut.model_validate(job)


@router.post(
    "/platform/jobs/{job_id}/retry", response_model=JobOut, summary="Replay a job",
    dependencies=[Depends(require_operator)],
)
def retry_job(
    job_id: int,
    user: CurrentUser = Depends(require(Permission.JOB_MANAGE)),
    db: Session = Depends(get_db),
) -> JobOut:
    try:
        return JobOut.model_validate(JobQueue(db).retry(job_id))
    except QueueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.post(
    "/platform/jobs/{job_id}/cancel", response_model=JobOut, summary="Cancel a job",
    dependencies=[Depends(require_operator)],
)
def cancel_job(
    job_id: int,
    user: CurrentUser = Depends(require(Permission.JOB_MANAGE)),
    db: Session = Depends(get_db),
) -> JobOut:
    try:
        return JobOut.model_validate(JobQueue(db).cancel(job_id))
    except QueueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.get(
    "/platform/queue", response_model=QueueDepthOut, summary="Queue depth",
    dependencies=[Depends(require_operator)],
)
def queue_depth(db: Session = Depends(get_db)) -> QueueDepthOut:
    d = JobQueue(db).depth()
    return QueueDepthOut(
        queued=d.queued, running=d.running, failed=d.failed,
        dead_letter=d.dead_letter, succeeded_24h=d.succeeded_24h,
        backlog=d.backlog, oldest_queued_seconds=d.oldest_queued_seconds,
        p50_duration_ms=d.p50_duration_ms, p95_duration_ms=d.p95_duration_ms,
        by_kind=d.by_kind, healthy=d.is_healthy,
    )


@router.get(
    "/platform/schedules", response_model=list[ScheduleOut], summary="Recurring jobs",
    dependencies=[Depends(require_operator)],
)
def schedules(db: Session = Depends(get_db)) -> list[ScheduleOut]:
    from app.services.platform.jobs.worker import ALL_SCHEDULES

    descriptions = {s.kind.value: s.description for s in ALL_SCHEDULES}
    rows = {r.kind: r for r in db.scalars(select(ScheduleState))}
    out: list[ScheduleOut] = []
    for spec in ALL_SCHEDULES:
        row = rows.get(spec.kind.value)
        out.append(ScheduleOut(
            kind=spec.kind.value,
            enabled=row.enabled if row else spec.enabled,
            every_seconds=row.every_seconds if row else spec.every_seconds,
            last_run_at=row.last_run_at if row else None,
            next_run_at=row.next_run_at if row else None,
            last_status=row.last_status if row else None,
            run_count=row.run_count if row else 0,
            description=descriptions.get(spec.kind.value, ""),
        ))
    return out


# --- backups ---------------------------------------------------------------
@router.get(
    "/platform/backups", response_model=list[BackupOut], summary="Backup history",
    dependencies=[Depends(require_operator)],
)
def list_backups(db: Session = Depends(get_db)) -> list[BackupOut]:
    return [BackupOut.model_validate(r) for r in BackupService(db).list()]


@router.get(
    "/platform/backups/status", response_model=BackupStatusOut, summary="Backup status",
    dependencies=[Depends(require_operator)],
)
def backup_status(db: Session = Depends(get_db)) -> BackupStatusOut:
    return BackupStatusOut(**BackupService(db).status())


@router.post(
    "/platform/backups", response_model=BackupOut, status_code=status.HTTP_201_CREATED,
    summary="Take a backup",
)
def create_backup(
    request: Request,
    user: CurrentUser = Depends(require_operator),
    db: Session = Depends(get_db),
) -> BackupOut:
    try:
        record = BackupService(db).create(label="manual")
    except BackupError as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc)) from exc

    AuditService(db).record(
        AuditAction.BACKUP_CREATED, principal=user,
        resource_type="backup", resource_id=record.id,
        summary=f"Backup written to {record.location} ({record.size_bytes} bytes)",
        context=_context(request),
    )
    return BackupOut.model_validate(record)


@router.post(
    "/platform/backups/{backup_id}/verify", response_model=BackupVerifyOut,
    summary="Verify a backup",
)
def verify_backup(
    backup_id: int,
    user: CurrentUser = Depends(require_operator),
    db: Session = Depends(get_db),
) -> BackupVerifyOut:
    """Confirm the artefact still hashes correctly and still opens.

    There is deliberately no restore endpoint. A one-click restore is a
    one-click way to lose a production database; what the platform owes an
    operator is the exact command, which this returns.
    """
    service = BackupService(db)
    record = db.get(BackupRecord, backup_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such backup.")
    ok, detail = service.verify(record)
    return BackupVerifyOut(
        backup_id=backup_id, ok=ok, detail=detail,
        restore_command=service.restore_command(record),
    )


@router.get(
    "/platform/readiness", response_model=HealthOut, summary="Detailed readiness",
    dependencies=[Depends(require_operator), Depends(require(Permission.SYSTEM_READ))],
)
def platform_readiness(db: Session = Depends(get_db)) -> HealthOut:
    report = HealthService(db).readiness()
    return HealthOut(
        status=report.status, version=report.version,
        environment=report.environment, uptime_seconds=report.uptime_seconds,
        ready=report.ready,
        checks=[
            {
                "name": c.name, "ok": c.ok, "detail": c.detail,
                "duration_ms": c.duration_ms, "critical": c.critical,
            }
            for c in report.checks
        ],
    )


# ===========================================================================
# Recycle bin (soft delete) — Phase 1
# ===========================================================================
@router.get(
    "/admin/recycle-bin", response_model=Page[RecycleBinOut],
    summary="Soft-deleted resources awaiting review",
    dependencies=[Depends(require(Permission.RECYCLE_READ))],
)
def recycle_bin_list(
    tenant_id: int = Depends(require_tenant),
    db: Session = Depends(get_db),
    status: str | None = Query(default=None, description="active | restored"),
    resource_type: str | None = None,
    search: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
) -> Page[RecycleBinOut]:
    rows, total = RecycleBinService(db).list(
        status=status, resource_type=resource_type, search=search,
        offset=(page - 1) * page_size, limit=page_size,
    )
    return Page[RecycleBinOut](
        items=[RecycleBinOut.model_validate(r) for r in rows],
        total=total, page=page, page_size=page_size,
    )


@router.post(
    "/admin/recycle-bin", response_model=RecycleBinOut,
    status_code=status.HTTP_201_CREATED,
    summary="Soft-delete a resource",
    dependencies=[Depends(require(Permission.RECYCLE_MANAGE))],
)
def recycle_soft_delete(
    request: Request,
    body: RecycleSoftDeleteRequest,
    tenant_id: int = Depends(require_tenant),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> RecycleBinOut:
    entry = RecycleBinService(db).soft_delete(
        resource_type=body.resource_type,
        resource_id=body.resource_id,
        display_name=body.display_name,
        payload=body.payload,
        principal=user,
        context=_context(request),
    )
    db.commit()
    return RecycleBinOut.model_validate(entry)


@router.post(
    "/admin/recycle-bin/{entry_id}/restore", response_model=RecycleBinOut,
    summary="Restore a soft-deleted resource",
    dependencies=[Depends(require(Permission.RECYCLE_MANAGE))],
)
def recycle_restore(
    request: Request,
    entry_id: int,
    tenant_id: int = Depends(require_tenant),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> RecycleBinOut:
    try:
        entry = RecycleBinService(db).restore(
            entry_id, principal=user, context=_context(request)
        )
    except RecycleBinError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    db.commit()
    return RecycleBinOut.model_validate(entry)


@router.delete(
    "/admin/recycle-bin/{entry_id}", response_model=RecycleBinOut,
    summary="Permanently purge a soft-deleted resource",
    dependencies=[Depends(require(Permission.RECYCLE_MANAGE))],
)
def recycle_purge(
    request: Request,
    entry_id: int,
    tenant_id: int = Depends(require_tenant),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> RecycleBinOut:
    try:
        entry = RecycleBinService(db).purge(
            entry_id, principal=user, context=_context(request)
        )
    except RecycleBinError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    db.commit()
    return RecycleBinOut.model_validate(entry)


@router.delete(
    "/admin/recycle-bin", response_model=MessageResponse,
    summary="Purge every soft-deleted resource",
    dependencies=[Depends(require(Permission.RECYCLE_MANAGE))],
)
def recycle_purge_all(
    request: Request,
    tenant_id: int = Depends(require_tenant),
    db: Session = Depends(get_db),
    resource_type: str | None = None,
    user: CurrentUser = Depends(get_current_user),
) -> MessageResponse:
    count = RecycleBinService(db).purge_all(
        resource_type=resource_type, principal=user, context=_context(request)
    )
    db.commit()
    return MessageResponse(message=f"Purged {count} item(s) from the recycle bin")


# ===========================================================================
# System status (admin dashboard foundation)
# ===========================================================================
@router.get(
    "/admin/system-status", response_model=SystemStatusOut,
    summary="Aggregate system health for the admin dashboard",
    dependencies=[Depends(require(Permission.AUDIT_READ))],
)
def system_status(
    tenant_id: int = Depends(require_tenant),
    db: Session = Depends(get_db),
) -> SystemStatusOut:
    from app.data.providers.router import get_router
    from app.domain.platform.audit import AuditAction
    from app.models.company import Company
    from app.models.platform import User
    from app.services.live_market import market_status

    # Database — reachable if a trivial query succeeds.
    components: list[SystemComponentOut] = []
    try:
        db.execute(select(func.count(Company.id)))
        components.append(SystemComponentOut(name="database", status="ok"))
    except Exception:  # noqa: BLE001
        components.append(SystemComponentOut(name="database", status="down"))

    # Redis — configured or not, and reachable.
    redis_ok = False
    if settings.REDIS_URL:
        try:
            import redis as _redis
            client = _redis.Redis.from_url(settings.REDIS_URL, socket_timeout=0.25)
            redis_ok = bool(client.ping())
        except Exception:  # noqa: BLE001
            redis_ok = False
        components.append(SystemComponentOut(
            name="redis", status="ok" if redis_ok else "down"))
    else:
        components.append(SystemComponentOut(
            name="redis", status="disabled", detail="not configured"))

    # Railway — this deployment's host reflects the Railway runtime.
    import os as _os
    railway = "railway" if _os.environ.get("RAILWAY_ENVIRONMENT") else (
        "railway" if (settings.DATABASE_URL or "").startswith("postgres") else "local"
    )
    components.append(SystemComponentOut(
        name="railway", status="ok" if railway == "railway" else "disabled",
        detail=railway))

    # Market data providers.
    try:
        engine = get_router()
        provider_names = ", ".join(
            p.name for p in engine.providers if p.configured()
        ) or "none configured"
        components.append(SystemComponentOut(
            name="market", status="ok" if provider_names != "none configured"
            else "degraded", detail=provider_names))
    except Exception:  # noqa: BLE001
        components.append(SystemComponentOut(name="market", status="degraded"))

    companies = db.execute(select(func.count(Company.id))).scalar_one()
    users = db.execute(select(func.count(User.id))).scalar_one()
    api_calls = 0
    try:
        api_calls = AuditService(db).query(
            tenant_id=None, unrestricted=True,
            action=AuditAction.APIKEY_USED, since=_utcnow() - timedelta(days=30),
            offset=0, limit=1,
        )[1]
    except Exception:  # noqa: BLE001
        api_calls = 0

    return SystemStatusOut(
        components=components,
        companies=companies,
        users=users,
        api_calls=api_calls,
        market_open=market_status(),
        generated_at=_utcnow(),
    )
