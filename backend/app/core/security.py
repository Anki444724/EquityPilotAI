"""Authentication and authorisation dependencies.

This is where the domain's access rules meet FastAPI. The division is strict:
`domain/platform/identity.py` decides *whether* a principal may do something
and raises a plain exception; this module turns a request into a principal and
turns those exceptions into HTTP responses.

**Backwards compatibility.** Modules 1-9 were built against a `CurrentUser`
with `.id`, `.email`, `.name`, `.role` and `.is_dev_identity`, and depend on
`get_current_user` and `require_admin`. Both still exist and still behave; the
object they return is now a `Principal`, which exposes the same attributes plus
tenancy and permissions. Nothing in Modules 1-9 needed editing, and `.id` in
particular still yields the same value it always did, so every `owner_id`
already in the database keeps resolving to the same person.

**Three ways to authenticate**, all resolving to one `Principal`:

1. `Authorization: Bearer <jwt>` — a browser session.
2. `Authorization: Bearer ierp_live_…` or `X-API-Key` — a programmatic client.
3. No credential and native auth off — the labelled development identity, so
   the product remains explorable with no accounts configured.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

import httpx
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.base import get_db
from app.domain.platform.identity import (
    AuthProvider, AuthorizationError, Permission, Principal, Role,
    TenantIsolationError,
)
from app.domain.platform.plans import DenialReason, Entitlement, Feature, Limit, Quota
from app.services.platform.tenancy import TenantScope

# Modules 1-9 import this name. It is the same object, under the name they use.
CurrentUser = Principal


# ---------------------------------------------------------------------------
# The development identity
# ---------------------------------------------------------------------------
#: Used when no real identity system is configured. Deliberately labelled:
#: `is_dev_identity` is surfaced in the UI and in `/health`, so nobody can
#: mistake an unauthenticated deployment for a secured one.
DEV_USER = Principal(
    user_id="dev-user",
    email="analyst@localhost",
    name="Development Analyst",
    role=Role.SUPER_ADMIN,
    tenant_id=None,
    provider=AuthProvider.DEV,
    is_dev_identity=True,
    session_id="dev-session",
)


#: Cached development principal. The default tenant is looked up once per
#: process rather than on every request.
#:
#: This is not a micro-optimisation. `get_current_user` is `async def`, so its
#: body runs *on the event loop thread*; a synchronous database call inside it
#: blocks every other request in the process while it waits. Under load, with
#: the pool contended, that query waited on a connection and froze the entire
#: server — including `/health`, which touches no database at all. A load test
#: at concurrency 25 wedged the process completely; py-spy put the main thread
#: in exactly this call.
#:
#: The lookup is safe to cache: the default tenant's id does not change during
#: a process's life, and the dev identity only exists when no real identity
#: system is configured.
_DEV_PRINCIPAL: Principal | None = None


def _dev_principal(db: Session) -> Principal:
    """The development identity, bound to the default tenant if one exists.

    Binding matters: without a tenant the dev user cannot exercise any
    tenant-scoped code path, and the multi-tenant behaviour would go untested
    in exactly the configuration developers run.
    """
    global _DEV_PRINCIPAL
    if _DEV_PRINCIPAL is not None:
        return _DEV_PRINCIPAL

    from app.models.platform import Tenant
    from sqlalchemy import select

    try:
        tenant = db.scalar(
            select(Tenant).where(Tenant.slug == settings.DEFAULT_TENANT_SLUG)
        )
    except Exception:  # noqa: BLE001 — never fail a request over the dev shim
        return DEV_USER

    if tenant is None:
        # Not cached: the platform seed may not have run yet, and a later
        # request should pick the tenant up once it has.
        return DEV_USER

    _DEV_PRINCIPAL = Principal(
        user_id=DEV_USER.user_id,
        email=DEV_USER.email,
        name=DEV_USER.name,
        role=Role.SUPER_ADMIN,
        tenant_id=tenant.id,
        tenant_slug=tenant.slug,
        provider=AuthProvider.DEV,
        is_dev_identity=True,
        session_id="dev-session",
    )
    return _DEV_PRINCIPAL


def reset_dev_principal() -> None:
    """Clear the cache. Used by tests that seed a tenant mid-session."""
    global _DEV_PRINCIPAL
    _DEV_PRINCIPAL = None


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------
def _client_ip(request: Request) -> str:
    """The caller's address, honouring one proxy hop.

    Only the first entry of `X-Forwarded-For` is read, and only because
    Railway and every other PaSS terminate TLS in front of the app. Trusting
    the whole chain would let a caller forge their own address by sending the
    header themselves.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()[:45]
    return (request.client.host if request.client else "unknown")[:45]


def _verify_clerk_token_sync(token: str) -> Principal:
    """Legacy Clerk path, retained so a Module 1 deployment keeps working.

    Synchronous because `get_current_user` is a sync dependency and therefore
    already runs on a worker thread, where blocking is the correct behaviour.
    """
    with httpx.Client(timeout=10) as client:
        resp = client.get(
            "https://api.clerk.com/v1/me",
            headers={"Authorization": f"Bearer {token}"},
        )
    if resp.status_code != 200:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid session token")
    data = resp.json()
    emails = data.get("email_addresses") or [{}]
    return Principal(
        user_id=data.get("id", "unknown"),
        email=emails[0].get("email_address", ""),
        name=" ".join(filter(None, [data.get("first_name"), data.get("last_name")])) or "User",
        role=Role(data.get("public_metadata", {}).get("role", Role.ANALYST.value)),
    )


def get_current_user(
    request: Request, db: Session = Depends(get_db),
) -> Principal:
    """Resolve the caller. The dependency every protected route already uses.

    Order matters: an explicit API key beats a bearer token, and a bearer
    token that looks like an API key is treated as one. A client should never
    be able to change which credential is honoured by moving it between
    headers.
    """
    from app.services.platform.api_keys import ApiKeyError, ApiKeyService
    from app.services.platform.identity_service import AuthError, IdentityService

    ip = _client_ip(request)

    # 1. explicit API key header
    api_key = request.headers.get("x-api-key")
    if api_key:
        try:
            principal = ApiKeyService(db).authenticate(api_key, ip_address=ip)
        except ApiKeyError as exc:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc
        request.state.principal = principal
        return principal

    header = request.headers.get("authorization", "")
    bearer = header.split(" ", 1)[1].strip() if header.lower().startswith("bearer ") else ""

    # 2. an API key presented as a bearer token
    if bearer.startswith(("ierp_live_", "ierp_test_")):
        try:
            principal = ApiKeyService(db).authenticate(bearer, ip_address=ip)
        except ApiKeyError as exc:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc
        request.state.principal = principal
        return principal

    # 3. native session token
    if settings.NATIVE_AUTH:
        if not bearer:
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED, "Authentication required.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        try:
            principal = IdentityService(db).principal_from_access_token(bearer)
        except AuthError as exc:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc
        except Exception as exc:  # token malformed, expired, wrong issuer
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED, "Session expired. Please sign in again.",
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc
        request.state.principal = principal
        return principal

    # 4. legacy Clerk
    if settings.auth_enabled:
        if not bearer:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
        # Sync bridge: this dependency runs on the threadpool, so a blocking
        # call is correct here and an `await` is not available.
        principal = _verify_clerk_token_sync(bearer)
        request.state.principal = principal
        return principal

    # 5. development identity
    principal = _dev_principal(db)
    request.state.principal = principal
    return principal


def get_optional_user(
    request: Request, db: Session = Depends(get_db),
) -> Principal | None:
    """Like `get_current_user`, but returns None instead of raising.

    For endpoints that serve both signed-in and anonymous callers — the
    pricing page, the public plan list — where a 401 would be wrong.
    """
    try:
        return get_current_user(request, db)
    except HTTPException:
        return None


CurrentPrincipal = Annotated[Principal, Depends(get_current_user)]


# ---------------------------------------------------------------------------
# Authorisation
# ---------------------------------------------------------------------------
def require(*permissions: Permission) -> Callable:
    """Build a dependency demanding every listed permission.

        @router.get("/admin/users", dependencies=[Depends(require(Permission.MEMBER_READ))])

    A denial is audited as well as refused: an access-control failure is a
    security event, and the trail is the only place anyone will see a pattern
    of them.
    """

    def dependency(
        request: Request,
        principal: Principal = Depends(get_current_user),
        db: Session = Depends(get_db),
    ) -> Principal:
        try:
            principal.require(*permissions)
        except AuthorizationError as exc:
            _audit_denial(db, request, principal, str(exc.permission))
            raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
        return principal

    return dependency


def require_any(*permissions: Permission) -> Callable:
    """As `require`, but any one of the listed permissions suffices."""

    def dependency(
        request: Request,
        principal: Principal = Depends(get_current_user),
        db: Session = Depends(get_db),
    ) -> Principal:
        try:
            principal.require_any(*permissions)
        except AuthorizationError as exc:
            _audit_denial(db, request, principal, str(exc.permission))
            raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
        return principal

    return dependency


def require_admin(
    request: Request,
    principal: Principal = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Principal:
    """Tenant administration. Kept under its Module 1 name and signature."""
    try:
        principal.require(Permission.MEMBER_MANAGE)
    except AuthorizationError as exc:
        _audit_denial(db, request, principal, str(exc.permission))
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    return principal


def require_operator(
    request: Request,
    principal: Principal = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Principal:
    """Platform operator — the only role that crosses tenant boundaries."""
    try:
        principal.require(Permission.PLATFORM_ADMIN)
    except AuthorizationError as exc:
        _audit_denial(db, request, principal, str(exc.permission))
        raise HTTPException(
            # 404, not 403: the operator console should not confirm its own
            # existence to a customer who probes for it.
            status.HTTP_404_NOT_FOUND, "Not found.",
        ) from exc
    return principal


def _audit_denial(
    db: Session, request: Request, principal: Principal, permission: str,
) -> None:
    from app.domain.platform.audit import AuditAction
    from app.services.platform.audit_service import AuditService, RequestContext

    AuditService(db).record(
        AuditAction.ACCESS_DENIED,
        principal=principal,
        outcome="denied",
        summary=f"{request.method} {request.url.path} requires '{permission}'",
        context=RequestContext(
            ip_address=_client_ip(request),
            user_agent=request.headers.get("user-agent"),
            request_id=getattr(request.state, "request_id", None),
        ),
        metadata={"permission": permission, "path": request.url.path},
    )


# ---------------------------------------------------------------------------
# Tenancy
# ---------------------------------------------------------------------------
def get_tenant_scope(
    principal: Principal = Depends(get_current_user),
) -> TenantScope:
    """The tenant filter for this request. Injected into every service that
    reads tenant-owned data."""
    return TenantScope.for_principal(principal)


def require_tenant(
    principal: Principal = Depends(get_current_user),
) -> int:
    """The caller's tenant id, or 400 if they have none.

    An operator acting without selecting a tenant genuinely has no answer
    here, and guessing one would write data into somebody's organisation.
    """
    if principal.tenant_id is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "This action requires an organisation context.",
        )
    return principal.tenant_id


def tenant_guard(request: Request, db: Session, principal: Principal):
    """Wrap a block that may raise `TenantIsolationError`.

    Returns 404 rather than 403, because confirming that another
    organisation's resource exists is itself a disclosure — and records a
    CRITICAL audit event, because a cross-tenant reach is either a bug worth
    finding or an attack worth knowing about.
    """
    from contextlib import contextmanager

    @contextmanager
    def _guard():
        try:
            yield
        except TenantIsolationError as exc:
            from app.domain.platform.audit import AuditAction
            from app.services.platform.audit_service import (
                AuditService, RequestContext,
            )

            AuditService(db).record(
                AuditAction.TENANT_ISOLATION_VIOLATION,
                principal=principal,
                outcome="denied",
                summary=f"{request.method} {request.url.path} crossed a tenant boundary",
                context=RequestContext(
                    ip_address=_client_ip(request),
                    user_agent=request.headers.get("user-agent"),
                    request_id=getattr(request.state, "request_id", None),
                ),
                metadata={
                    "principal_tenant": exc.principal_tenant,
                    "resource_tenant": exc.resource_tenant,
                },
            )
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found.") from exc

    return _guard()


# ---------------------------------------------------------------------------
# Entitlements
# ---------------------------------------------------------------------------
def require_feature(feature: Feature) -> Callable:
    """Gate a route on the tenant's plan.

    402 Payment Required rather than 403 Forbidden: the caller is
    authenticated and authorised, and the obstacle is commercial. The
    distinction lets the frontend show an upgrade prompt for one and an access
    error for the other without parsing message strings.
    """

    def dependency(
        principal: Principal = Depends(get_current_user),
        db: Session = Depends(get_db),
    ) -> Principal:
        if principal.tenant_id is None:
            return principal   # no tenant, no plan to enforce (dev identity)

        from app.services.platform.entitlements import EntitlementService

        decision = EntitlementService(db).check(principal.tenant_id, feature=feature)
        if not decision:
            raise _entitlement_error(decision)
        return principal

    return dependency


def require_quota(quota: Quota, quantity: int = 1) -> Callable:
    """Check — but do not consume — a metered allowance.

    Consumption happens after the work succeeds. A route that consumed here
    would bill the customer for a report that then failed to render.
    """

    def dependency(
        principal: Principal = Depends(get_current_user),
        db: Session = Depends(get_db),
    ) -> Principal:
        if principal.tenant_id is None:
            return principal

        from app.services.platform.entitlements import EntitlementService

        decision = EntitlementService(db).check(
            principal.tenant_id, quota=quota, quantity=quantity,
        )
        if not decision:
            raise _entitlement_error(decision)
        return principal

    return dependency


def _entitlement_error(decision: Entitlement) -> HTTPException:
    """Map a denial to a status code that means what happened."""
    code = {
        DenialReason.FEATURE_NOT_IN_PLAN: status.HTTP_402_PAYMENT_REQUIRED,
        DenialReason.QUOTA_EXCEEDED: status.HTTP_402_PAYMENT_REQUIRED,
        DenialReason.LIMIT_REACHED: status.HTTP_402_PAYMENT_REQUIRED,
        DenialReason.SUBSCRIPTION_INACTIVE: status.HTTP_402_PAYMENT_REQUIRED,
        DenialReason.TENANT_READ_ONLY: status.HTTP_403_FORBIDDEN,
        DenialReason.TENANT_SUSPENDED: status.HTTP_403_FORBIDDEN,
    }.get(decision.reason, status.HTTP_403_FORBIDDEN)

    return HTTPException(
        code,
        detail={
            "message": decision.message,
            "reason": decision.reason.value,
            "feature": decision.feature.value if decision.feature else None,
            "quota": decision.quota.value if decision.quota else None,
            "limit": decision.limit.value if decision.limit else None,
            "used": decision.used,
            "allowance": decision.allowance,
            "upgrade_to": decision.upgrade_to.value if decision.upgrade_to else None,
        },
    )


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------
#: Methods that cannot change state and therefore need no CSRF token.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


def verify_csrf(request: Request, principal: Principal = Depends(get_current_user)) -> None:
    """Require a valid CSRF token on state-changing cookie-authenticated calls.

    Bearer-token and API-key callers are exempt, and correctly so: CSRF
    exploits the browser's automatic attachment of *cookies*. A token a script
    must place in a header cannot be attached by a cross-site form, so the
    attack does not apply and demanding a CSRF token would only break every
    API client.
    """
    if request.method in SAFE_METHODS:
        return
    if not settings.CSRF_ENABLED or principal.is_dev_identity:
        return
    if principal.api_key_id is not None:
        return
    if request.headers.get("authorization", "").lower().startswith("bearer "):
        return

    from app.services.platform.crypto import verify_csrf as check

    token = request.headers.get("x-csrf-token", "")
    if not principal.session_id or not check(principal.session_id, token):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "CSRF token missing or invalid.")
