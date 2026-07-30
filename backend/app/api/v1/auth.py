"""Authentication endpoints.

    GET    /auth/config                public configuration for the sign-in page
    GET    /auth/me                    the current session
    GET    /auth/password-policy       what a valid password looks like
    POST   /auth/register              create an account
    POST   /auth/login                 email + password
    POST   /auth/logout                end the session
    POST   /auth/refresh               rotate the refresh token
    POST   /auth/verify-email          redeem a verification token
    POST   /auth/resend-verification   issue a new one
    POST   /auth/magic-link            request a passwordless link
    POST   /auth/magic-link/consume    redeem it
    POST   /auth/password-reset        request a reset
    POST   /auth/password-reset/confirm  complete it
    POST   /auth/password              change a known password
    GET    /auth/oauth/{provider}      begin an OAuth flow
    GET    /auth/oauth/{provider}/callback   complete it
    GET    /auth/sessions              this user's live sessions
    DELETE /auth/sessions              revoke all of them

Every enumeration-sensitive endpoint — register, magic link, password reset —
returns the same body whether or not the address exists. That is why they all
share `MessageResponse` and why none of them 404.

Rate limits are applied per IP before any database work, so a credential
stuffing run is refused cheaply rather than after an Argon2 verification.
"""
from __future__ import annotations

import secrets
from typing import Annotated

import httpx
from fastapi import (
    APIRouter, Cookie, Depends, HTTPException, Request, Response, status,
)
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import CurrentUser, get_current_user
from app.db.base import get_db
from app.domain.platform.audit import AuditAction
from app.domain.platform.identity import (
    AuthProvider, Permission, Role, TokenType, UserStatus,
)
from app.domain.platform.limits import DEFAULT_PASSWORD_POLICY, RateScope
from app.models.platform import Tenant, User
from app.schemas.platform import (
    AuthConfig, LoginRequest, MagicLinkRequest, MessageResponse,
    PasswordChangeRequest, PasswordPolicyOut, PasswordResetConfirm,
    PasswordResetRequest, RefreshRequest, RegisterRequest, SessionUser,
    TokenRequest, TokenResponse,
)
from app.services.platform import rate_limit
from app.services.platform.audit_service import AuditService, RequestContext
from app.services.platform.email import EmailService
from app.services.platform.identity_service import (
    AuthError, AuthOutcome, IdentityService, PendingToken, RegistrationError,
    ReuseDetected,
)

router = APIRouter(prefix="/auth", tags=["auth"])

#: The refresh token lives here. httpOnly so script cannot read it, which is
#: what makes an XSS bug a session-hijack risk only for the fifteen minutes an
#: access token lives rather than for thirty days.
REFRESH_COOKIE = "ierp_refresh"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _context(request: Request) -> RequestContext:
    from app.core.security import _client_ip

    return RequestContext(
        ip_address=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
        request_id=getattr(request.state, "request_id", None),
    )


def _limit(request: Request, rule: str) -> None:
    """Refuse early if the caller is over the limit for this endpoint."""
    if not settings.RATE_LIMIT_ENABLED:
        return
    from app.core.security import _client_ip

    decision = rate_limit.check(rule, _client_ip(request), scope=RateScope.IP)
    if not decision.allowed:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Too many attempts. Please wait and try again.",
            headers=decision.headers(),
        )


def _set_refresh_cookie(response: Response, token: str, max_age: int) -> None:
    response.set_cookie(
        REFRESH_COOKIE, token,
        max_age=max_age, httponly=True,
        secure=settings.cookie_secure,
        samesite=settings.COOKIE_SAMESITE,
        domain=settings.COOKIE_DOMAIN,
        path="/api/v1/auth",   # narrow: the cookie is only ever sent here
    )


def _clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(
        REFRESH_COOKIE, path="/api/v1/auth", domain=settings.COOKIE_DOMAIN,
    )


def _token_response(
    outcome: AuthOutcome, response: Response, *, include_refresh: bool,
) -> TokenResponse:
    _set_refresh_cookie(
        response, outcome.tokens.refresh_token, outcome.tokens.refresh_expires_in,
    )
    return TokenResponse(
        access_token=outcome.tokens.access_token,
        expires_in=outcome.tokens.expires_in,
        csrf_token=outcome.tokens.csrf_token,
        refresh_token=outcome.tokens.refresh_token if include_refresh else None,
    )


def _dev_link(pending: PendingToken | None, path: str) -> str | None:
    """Expose the link in the response only when there is no mail server.

    Guarded twice — no SMTP host *and* not production — because a link in an
    API response is a password reset anyone who can see the response can
    complete.
    """
    if pending is None or settings.email_configured or settings.is_production:
        return None
    return f"{settings.EMAIL_LINK_BASE}{path}?token={pending.token}"


#: The same body for every enumeration-sensitive endpoint.
_NEUTRAL = "If that address is registered, we have sent an email with the next step."


# ===========================================================================
# Configuration and session
# ===========================================================================
@router.get("/config", response_model=AuthConfig, summary="Public auth config")
def auth_config() -> AuthConfig:
    return AuthConfig(
        provider="native" if settings.NATIVE_AUTH else (
            "clerk" if settings.CLERK_SECRET_KEY else "development"
        ),
        auth_enabled=settings.auth_enabled,
        native_auth=settings.NATIVE_AUTH,
        self_signup=settings.ALLOW_SELF_SIGNUP,
        oauth_providers=settings.oauth_providers,
        magic_link=True,
        email_configured=settings.email_configured,
        password_min_length=DEFAULT_PASSWORD_POLICY.min_length,
        publishable_key=settings.CLERK_PUBLISHABLE_KEY,
    )


@router.get("/password-policy", response_model=PasswordPolicyOut, summary="Password policy")
def password_policy() -> PasswordPolicyOut:
    policy = DEFAULT_PASSWORD_POLICY
    requires = []
    if policy.require_lower:
        requires.append("lower-case letter")
    if policy.require_upper:
        requires.append("upper-case letter")
    if policy.require_digit:
        requires.append("digit")
    if policy.require_symbol:
        requires.append("symbol")
    return PasswordPolicyOut(
        min_length=policy.min_length,
        passphrase_length=policy.passphrase_length,
        requires=requires,
        message=(
            f"At least {policy.min_length} characters. Character-class rules "
            f"are waived at {policy.passphrase_length} or more — a long "
            "passphrase is stronger than a short complicated password."
        ),
    )


@router.get("/me", response_model=SessionUser, summary="Current user")
def me(
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SessionUser:
    tenant = db.get(Tenant, user.tenant_id) if user.tenant_id else None
    record = db.get(User, user.user_id)
    return SessionUser(
        id=user.id,
        email=user.email,
        name=user.name,
        role=user.role.value,
        is_dev_identity=user.is_dev_identity,
        avatar_url=record.avatar_url if record else None,
        tenant_id=user.tenant_id,
        tenant_slug=user.tenant_slug,
        tenant_name=tenant.name if tenant else None,
        permissions=sorted(p.value for p in user.permissions),
        provider=str(user.provider),
        email_verified=bool(record.email_verified_at) if record else True,
        mfa_enabled=bool(record and record.mfa_method != "none"),
    )


# ===========================================================================
# Registration
# ===========================================================================
@router.post(
    "/register", response_model=MessageResponse,
    status_code=status.HTTP_201_CREATED, summary="Create an account",
)
def register(
    payload: RegisterRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> MessageResponse:
    _limit(request, "auth.register")

    if not settings.ALLOW_SELF_SIGNUP:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Self-service registration is disabled. Ask an administrator for an invitation.",
        )

    service = IdentityService(db)
    audit = AuditService(db)

    try:
        user, pending = service.register(
            email=payload.email, password=payload.password,
            name=payload.name, organisation=payload.organisation,
        )
    except RegistrationError as exc:
        if str(exc) == "__exists__":
            # Deliberately indistinguishable from success.
            audit.record(
                AuditAction.USER_REGISTERED, outcome="duplicate",
                actor_email=payload.email,
                summary="Registration attempted for an existing address",
                context=_context(request),
            )
            return MessageResponse(message=_NEUTRAL)
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            {"message": str(exc), "problems": exc.problems},
        ) from exc

    if pending is not None:
        EmailService().send_verification(
            to=user.email, name=user.name, token=pending.token,
        )

    audit.record(
        AuditAction.USER_REGISTERED,
        tenant_id=user.tenant_id, actor_id=user.id, actor_email=user.email,
        actor_role=user.role, resource_type="user", resource_id=user.id,
        summary=f"{user.email} registered", context=_context(request),
    )
    return MessageResponse(
        message=_NEUTRAL, dev_link=_dev_link(pending, "/verify"),
    )


@router.post("/verify-email", response_model=MessageResponse, summary="Verify an email address")
def verify_email(
    payload: TokenRequest, request: Request, db: Session = Depends(get_db),
) -> MessageResponse:
    service = IdentityService(db)
    try:
        user = service.verify_email(payload.token)
    except AuthError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    AuditService(db).record(
        AuditAction.EMAIL_VERIFIED,
        tenant_id=user.tenant_id, actor_id=user.id, actor_email=user.email,
        summary="Email address verified", context=_context(request),
    )
    return MessageResponse(message="Your email address is verified. You can now sign in.")


@router.post(
    "/resend-verification", response_model=MessageResponse,
    summary="Resend the verification email",
)
def resend_verification(
    payload: MagicLinkRequest, request: Request, db: Session = Depends(get_db),
) -> MessageResponse:
    _limit(request, "auth.magic_link")

    service = IdentityService(db)
    user = service.by_email(payload.email)
    pending = None
    if user is not None and user.email_verified_at is None:
        pending = service.request_email_verification(user)
        EmailService().send_verification(
            to=user.email, name=user.name, token=pending.token,
        )
        AuditService(db).record(
            AuditAction.EMAIL_VERIFICATION_SENT,
            tenant_id=user.tenant_id, actor_id=user.id, actor_email=user.email,
            summary="Verification email resent", context=_context(request),
        )
    return MessageResponse(message=_NEUTRAL, dev_link=_dev_link(pending, "/verify"))


# ===========================================================================
# Sign in and out
# ===========================================================================
@router.post("/login", response_model=TokenResponse, summary="Sign in")
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> TokenResponse:
    _limit(request, "auth.login")

    service = IdentityService(db)
    audit = AuditService(db)
    context = _context(request)

    try:
        outcome = service.authenticate(
            email=payload.email, password=payload.password,
            ip_address=context.ip_address, user_agent=context.user_agent,
        )
    except AuthError as exc:
        audit.record(
            AuditAction.LOGIN_FAILED, outcome="failure",
            actor_email=payload.email, summary=str(exc), context=context,
        )
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc

    audit.record(
        AuditAction.LOGIN_SUCCEEDED,
        principal=outcome.principal, summary="Signed in with password",
        context=context,
    )
    return _token_response(outcome, response, include_refresh=False)


@router.post("/logout", response_model=MessageResponse, summary="Sign out")
def logout(
    request: Request,
    response: Response,
    payload: RefreshRequest | None = None,
    refresh_cookie: Annotated[str | None, Cookie(alias=REFRESH_COOKIE)] = None,
    db: Session = Depends(get_db),
) -> MessageResponse:
    token = (payload.refresh_token if payload else None) or refresh_cookie
    if token:
        IdentityService(db).revoke_session(token)
    _clear_refresh_cookie(response)

    principal = getattr(request.state, "principal", None)
    AuditService(db).record(
        AuditAction.LOGOUT, principal=principal,
        summary="Signed out", context=_context(request),
    )
    return MessageResponse(message="Signed out.")


@router.post("/refresh", response_model=TokenResponse, summary="Rotate the session")
def refresh(
    request: Request,
    response: Response,
    payload: RefreshRequest | None = None,
    refresh_cookie: Annotated[str | None, Cookie(alias=REFRESH_COOKIE)] = None,
    db: Session = Depends(get_db),
) -> TokenResponse:
    token = (payload.refresh_token if payload else None) or refresh_cookie
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "No session to refresh.")

    service = IdentityService(db)
    context = _context(request)

    try:
        outcome, _ = service.refresh_session(
            token, ip_address=context.ip_address, user_agent=context.user_agent,
        )
    except ReuseDetected as exc:
        # A spent token was replayed. The family is already revoked; record it
        # as CRITICAL, because this is the only visible sign of a stolen token.
        user = db.get(User, exc.user_id)
        AuditService(db).record(
            AuditAction.TOKEN_REUSE_DETECTED, outcome="revoked",
            tenant_id=user.tenant_id if user else None,
            actor_id=exc.user_id, actor_email=user.email if user else None,
            summary=(
                "A refresh token was presented after use — the entire token "
                "family has been revoked."
            ),
            context=context, metadata={"family_id": exc.family_id},
        )
        _clear_refresh_cookie(response)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc
    except AuthError as exc:
        _clear_refresh_cookie(response)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc

    return _token_response(outcome, response, include_refresh=False)


# ===========================================================================
# Magic link
# ===========================================================================
@router.post("/magic-link", response_model=MessageResponse, summary="Request a sign-in link")
def magic_link(
    payload: MagicLinkRequest, request: Request, db: Session = Depends(get_db),
) -> MessageResponse:
    _limit(request, "auth.magic_link")

    service = IdentityService(db)
    pending = service.request_magic_link(payload.email)
    if pending is not None:
        EmailService().send_magic_link(
            to=pending.user.email, name=pending.user.name, token=pending.token,
        )
        AuditService(db).record(
            AuditAction.MAGIC_LINK_REQUESTED,
            tenant_id=pending.user.tenant_id, actor_id=pending.user.id,
            actor_email=pending.user.email,
            summary="Magic link requested", context=_context(request),
        )
    return MessageResponse(message=_NEUTRAL, dev_link=_dev_link(pending, "/magic"))


@router.post(
    "/magic-link/consume", response_model=TokenResponse, summary="Redeem a sign-in link",
)
def consume_magic_link(
    payload: TokenRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> TokenResponse:
    service = IdentityService(db)
    context = _context(request)
    try:
        outcome = service.authenticate_magic_link(
            token=payload.token,
            ip_address=context.ip_address, user_agent=context.user_agent,
        )
    except AuthError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    AuditService(db).record(
        AuditAction.MAGIC_LINK_CONSUMED, principal=outcome.principal,
        summary="Signed in with a magic link", context=context,
    )
    return _token_response(outcome, response, include_refresh=False)


# ===========================================================================
# Passwords
# ===========================================================================
@router.post("/password-reset", response_model=MessageResponse, summary="Request a password reset")
def password_reset(
    payload: PasswordResetRequest, request: Request, db: Session = Depends(get_db),
) -> MessageResponse:
    _limit(request, "auth.password_reset")

    service = IdentityService(db)
    pending = service.request_password_reset(payload.email)
    if pending is not None:
        EmailService().send_password_reset(
            to=pending.user.email, name=pending.user.name, token=pending.token,
        )
        AuditService(db).record(
            AuditAction.PASSWORD_RESET_REQUESTED,
            tenant_id=pending.user.tenant_id, actor_id=pending.user.id,
            actor_email=pending.user.email,
            summary="Password reset requested", context=_context(request),
        )
    return MessageResponse(
        message=_NEUTRAL, dev_link=_dev_link(pending, "/reset-password"),
    )


@router.post(
    "/password-reset/confirm", response_model=MessageResponse,
    summary="Complete a password reset",
)
def password_reset_confirm(
    payload: PasswordResetConfirm, request: Request, db: Session = Depends(get_db),
) -> MessageResponse:
    service = IdentityService(db)
    try:
        user = service.reset_password(payload.token, payload.password)
    except RegistrationError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            {"message": str(exc), "problems": exc.problems},
        ) from exc
    except AuthError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    AuditService(db).record(
        AuditAction.PASSWORD_RESET_COMPLETED,
        tenant_id=user.tenant_id, actor_id=user.id, actor_email=user.email,
        summary="Password reset; all sessions revoked", context=_context(request),
    )
    return MessageResponse(
        message="Your password has been changed. Sign in with the new password."
    )


@router.post("/password", response_model=MessageResponse, summary="Change your password")
def change_password(
    payload: PasswordChangeRequest,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> MessageResponse:
    service = IdentityService(db)
    record = db.get(User, user.user_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such account.")

    try:
        service.change_password(record, payload.current_password, payload.new_password)
    except RegistrationError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            {"message": str(exc), "problems": exc.problems},
        ) from exc
    except AuthError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    AuditService(db).record(
        AuditAction.PASSWORD_CHANGED, principal=user,
        summary="Password changed; all sessions revoked", context=_context(request),
    )
    return MessageResponse(message="Password changed. Please sign in again.")


# ===========================================================================
# OAuth
# ===========================================================================
_OAUTH = {
    "google": {
        "authorize": "https://accounts.google.com/o/oauth2/v2/auth",
        "token": "https://oauth2.googleapis.com/token",
        "userinfo": "https://www.googleapis.com/oauth2/v3/userinfo",
        "scope": "openid email profile",
    },
    "github": {
        "authorize": "https://github.com/login/oauth/authorize",
        "token": "https://github.com/login/oauth/access_token",
        "userinfo": "https://api.github.com/user",
        "scope": "read:user user:email",
    },
}


def _oauth_credentials(provider: str) -> tuple[str, str]:
    if provider == "google":
        return settings.GOOGLE_CLIENT_ID or "", settings.GOOGLE_CLIENT_SECRET or ""
    if provider == "github":
        return settings.GITHUB_CLIENT_ID or "", settings.GITHUB_CLIENT_SECRET or ""
    return "", ""


@router.get("/oauth/{provider}", summary="Begin an OAuth flow")
def oauth_start(provider: str, request: Request, response: Response) -> dict[str, str]:
    """Return the provider's authorisation URL and set the state cookie.

    `state` is random, stored in an httpOnly cookie, and compared on return.
    Without it, an attacker can hand a victim a callback URL carrying the
    attacker's authorisation code and link the victim's session to their
    account — login CSRF.
    """
    if provider not in _OAUTH or provider not in settings.oauth_providers:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"'{provider}' is not configured on this deployment.",
        )

    client_id, _ = _oauth_credentials(provider)
    state = secrets.token_urlsafe(24)
    redirect_uri = f"{settings.OAUTH_REDIRECT_BASE}/auth/callback/{provider}"

    config = _OAUTH[provider]
    url = (
        f"{config['authorize']}?client_id={client_id}"
        f"&redirect_uri={redirect_uri}&response_type=code"
        f"&scope={config['scope'].replace(' ', '%20')}&state={state}"
    )

    response.set_cookie(
        f"oauth_state_{provider}", state, max_age=600, httponly=True,
        secure=settings.cookie_secure, samesite="lax", path="/api/v1/auth",
    )
    return {"authorize_url": url, "state": state}


@router.get("/oauth/{provider}/callback", response_model=TokenResponse, summary="Complete an OAuth flow")
async def oauth_callback(
    provider: str,
    code: str,
    state: str,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> TokenResponse:
    if provider not in _OAUTH or provider not in settings.oauth_providers:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unsupported provider.")

    expected = request.cookies.get(f"oauth_state_{provider}")
    if not expected or not secrets.compare_digest(expected, state):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "OAuth state mismatch. Start the sign-in again.",
        )

    client_id, client_secret = _oauth_credentials(provider)
    config = _OAUTH[provider]
    redirect_uri = f"{settings.OAUTH_REDIRECT_BASE}/auth/callback/{provider}"

    async with httpx.AsyncClient(timeout=15) as client:
        token_resp = await client.post(
            config["token"],
            data={
                "client_id": client_id, "client_secret": client_secret,
                "code": code, "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
            headers={"Accept": "application/json"},
        )
        if token_resp.status_code != 200:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, "The provider rejected the authorisation code.",
            )
        access = token_resp.json().get("access_token")
        if not access:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "No access token returned.")

        profile_resp = await client.get(
            config["userinfo"], headers={"Authorization": f"Bearer {access}"},
        )
        profile = profile_resp.json()

        email = profile.get("email")
        if not email and provider == "github":
            # GitHub omits a private address from /user; the verified primary
            # is only available from the dedicated endpoint.
            emails = (await client.get(
                "https://api.github.com/user/emails",
                headers={"Authorization": f"Bearer {access}"},
            )).json()
            email = next(
                (e["email"] for e in emails if e.get("primary") and e.get("verified")),
                None,
            )

    if not email:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "The provider did not supply a verified email address.",
        )

    context = _context(request)
    try:
        outcome = IdentityService(db).authenticate_federated(
            provider=AuthProvider(provider),
            subject=str(profile.get("id") or profile.get("sub") or email),
            email=email,
            name=profile.get("name") or profile.get("login") or email.split("@")[0],
            avatar_url=profile.get("picture") or profile.get("avatar_url"),
            profile={k: v for k, v in profile.items() if k in ("login", "sub", "id")},
            ip_address=context.ip_address, user_agent=context.user_agent,
        )
    except AuthError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc

    AuditService(db).record(
        AuditAction.LOGIN_SUCCEEDED, principal=outcome.principal,
        summary=f"Signed in with {provider}", context=context,
        metadata={"provider": provider},
    )
    response.delete_cookie(f"oauth_state_{provider}", path="/api/v1/auth")
    return _token_response(outcome, response, include_refresh=False)


# ===========================================================================
# Sessions
# ===========================================================================
@router.get("/sessions", summary="Your active sessions")
def sessions(
    user: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db),
) -> list[dict[str, object]]:
    return [
        {
            "session_id": row.session_id,
            "issued_at": row.issued_at.isoformat() if row.issued_at else None,
            "expires_at": row.expires_at.isoformat() if row.expires_at else None,
            "ip_address": row.ip_address,
            "user_agent": row.user_agent,
            "current": row.session_id == user.session_id,
        }
        for row in IdentityService(db).active_sessions(user.user_id)
    ]


@router.delete("/sessions", response_model=MessageResponse, summary="Revoke every session")
def revoke_sessions(
    request: Request,
    response: Response,
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> MessageResponse:
    count = IdentityService(db).revoke_all_sessions(user.user_id, "user_requested")
    _clear_refresh_cookie(response)
    AuditService(db).record(
        AuditAction.LOGOUT, principal=user,
        summary=f"All sessions revoked ({count})", context=_context(request),
    )
    return MessageResponse(message=f"Revoked {count} session(s). Please sign in again.")
