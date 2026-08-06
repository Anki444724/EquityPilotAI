"""User Management & Subscription Center endpoints (Phase 7)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.security import CurrentUser, get_current_user, require_operator
from app.db.base import get_db
from app.schemas.platform import (
    MessageResponse, Page, SubscriptionOut, UserDetailOut, UserOut,
)
from app.schemas.user_center import (
    InvoiceOut, NotificationOut, UserAnalyticsOut, UserCreate,
    UserSessionOut, UserUpdate,
)
from app.services.user_center import UserCenterError, UserCenterService

router = APIRouter(prefix="/admin/users", tags=["admin-users"])


def _service(db: Session = Depends(get_db)) -> UserCenterService:
    return UserCenterService(db)


def _invoice_out(inv) -> InvoiceOut:
    def _iso(v):
        return v.isoformat() if hasattr(v, "isoformat") else v
    return InvoiceOut(
        id=inv.id, tenant_id=inv.tenant_id, number=inv.number,
        plan_tier=inv.plan_tier, period_start=_iso(inv.period_start),
        period_end=_iso(inv.period_end), subtotal_paise=inv.subtotal_paise,
        tax_paise=inv.tax_paise, total_paise=inv.total_paise,
        currency=inv.currency, status=inv.status,
        issued_at=inv.issued_at, paid_at=inv.paid_at,
    )


def _notification_out(n) -> NotificationOut:
    return NotificationOut(
        id=n.id, tenant_id=n.tenant_id, user_id=n.user_id, channel=n.channel,
        topic=n.topic, subject=n.subject, body=n.body, link=n.link,
        read_at=n.read_at, sent_at=n.sent_at, delivery_status=n.delivery_status,
    )


def _error(exc: UserCenterError) -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))


def _permissions_for(role: str) -> list[str]:
    from app.domain.platform.identity import ROLE_PERMISSIONS, Role
    try:
        return sorted(p.value for p in ROLE_PERMISSIONS[Role(role)])
    except Exception:  # noqa: BLE001
        return []


# ============================================================== STATIC ROUTES
# These must be registered before the `/{user_id}` route so a literal
# "roles" / "announce" / "analytics" is not captured as a user id.
@router.get("/roles", summary="Available roles and permissions")
def roles():
    from app.domain.platform.identity import (
        ROLE_LABELS, ROLE_PERMISSIONS, ROLE_ORDER, Permission,
    )
    return {
        "roles": [
            {"key": r.value, "label": ROLE_LABELS[r],
             "permissions": sorted(p.value for p in ROLE_PERMISSIONS[r])}
            for r in ROLE_ORDER
        ],
        "all_permissions": [p.value for p in Permission],
    }


@router.post("/announce", response_model=MessageResponse, summary="Announce to all users")
def announce(subject: str, body: str = "", channel: str = "email",
             svc: UserCenterService = Depends(_service)):
    count = svc.announce(subject=subject, body=body, channel=channel)
    svc.db.commit()
    return MessageResponse(message=f"Announced to {count} user(s)")


@router.get("/analytics/summary", response_model=UserAnalyticsOut, summary="User analytics")
def analytics(days: int = Query(30, ge=1, le=365), svc: UserCenterService = Depends(_service)):
    return UserAnalyticsOut(**svc.analytics(days=days))


# ============================================================== USERS
@router.get("", summary="List users", dependencies=[Depends(require_operator)])
def list_users(
    role: str | None = None, status: str | None = None,
    search: str | None = None, tenant_id: int | None = None,
    page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=200),
    svc: UserCenterService = Depends(_service),
):
    rows, total = svc.list_users(
        role=role, status=status, search=search, tenant_id=tenant_id,
        page=page, page_size=page_size,
    )
    return Page[UserOut](items=rows, total=total, page=page, page_size=page_size)


@router.post("", response_model=UserOut, status_code=status.HTTP_201_CREATED,
             summary="Add a user", dependencies=[Depends(require_operator)])
def create_user(
    body: UserCreate, svc: UserCenterService = Depends(_service),
    user: CurrentUser = Depends(get_current_user),
):
    try:
        created = svc.create_user(
            email=body.email, name=body.name, role=body.role,
            tenant_id=body.tenant_id, status=body.status or "active",
            password=body.password, actor=user,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    svc.db.commit()
    return UserOut.model_validate(created)


# ============================================================== USER-SCOPED
@router.get("/{user_id}", response_model=UserDetailOut, summary="User detail")
def get_user(user_id: str, svc: UserCenterService = Depends(_service)):
    try:
        user = svc.get_user(user_id)
    except UserCenterError as exc:
        raise _error(exc)
    active = len(svc.active_sessions(user_id))
    return UserDetailOut(
        **UserOut.model_validate(user).model_dump(),
        permissions=_permissions_for(user.role),
        active_sessions=active,
        failed_login_count=user.failed_login_count,
        locked_until=user.locked_until,
    )


@router.patch("/{user_id}", response_model=UserOut, summary="Edit a user")
def update_user(
    user_id: str, body: UserUpdate, svc: UserCenterService = Depends(_service),
    user: CurrentUser = Depends(get_current_user),
):
    try:
        updated = svc.update_user(
            user_id, name=body.name, role=body.role, actor=user,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    svc.db.commit()
    return UserOut.model_validate(updated)


@router.post("/{user_id}/suspend", response_model=UserOut, summary="Suspend a user")
def suspend_user(user_id: str, svc: UserCenterService = Depends(_service),
                 user: CurrentUser = Depends(get_current_user)):
    u = svc.set_status(user_id, "suspended", actor=user)
    svc.db.commit()
    return UserOut.model_validate(u)


@router.post("/{user_id}/ban", response_model=UserOut, summary="Ban a user")
def ban_user(user_id: str, svc: UserCenterService = Depends(_service),
             user: CurrentUser = Depends(get_current_user)):
    u = svc.set_status(user_id, "disabled", actor=user)
    svc.db.commit()
    return UserOut.model_validate(u)


@router.post("/{user_id}/restore", response_model=UserOut, summary="Restore a user")
def restore_user(user_id: str, svc: UserCenterService = Depends(_service),
                 user: CurrentUser = Depends(get_current_user)):
    u = svc.set_status(user_id, "active", actor=user)
    svc.db.commit()
    return UserOut.model_validate(u)


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete a user")
def delete_user(user_id: str, svc: UserCenterService = Depends(_service)):
    try:
        svc.delete_user(user_id)
    except UserCenterError as exc:
        raise _error(exc)
    svc.db.commit()


# ------------------------------------------------------------- sessions
@router.get("/{user_id}/sessions", response_model=list[UserSessionOut], summary="Active devices")
def user_sessions(user_id: str, svc: UserCenterService = Depends(_service)):
    return [UserSessionOut(**s) for s in svc.active_sessions(user_id)]


@router.post("/{user_id}/sessions/logout-all", response_model=MessageResponse,
             summary="Log out all sessions")
def logout_all(user_id: str, svc: UserCenterService = Depends(_service)):
    count = svc.logout_all(user_id)
    svc.db.commit()
    return MessageResponse(message=f"Logged out {count} session(s)")


@router.post("/{user_id}/sessions/{session_id}/logout", response_model=MessageResponse,
             summary="Force-log out one device")
def force_logout(user_id: str, session_id: str, svc: UserCenterService = Depends(_service)):
    count = svc.force_logout(user_id, session_id)
    svc.db.commit()
    return MessageResponse(message=f"Logged out {count} session(s)")


# ------------------------------------------------------------- subscriptions
@router.get("/{user_id}/subscription", response_model=SubscriptionOut, summary="User's subscription")
def user_subscription(user_id: str, svc: UserCenterService = Depends(_service)):
    user = svc.get_user(user_id)
    if user.tenant_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user has no tenant")
    return SubscriptionOut.model_validate(svc.billing.subscription_for(user.tenant_id))


@router.post("/{user_id}/subscription", response_model=SubscriptionOut,
             summary="Upgrade / downgrade subscription")
def change_subscription(
    user_id: str, tier: str, billing_period: str = "monthly",
    svc: UserCenterService = Depends(_service),
):
    user = svc.get_user(user_id)
    if user.tenant_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user has no tenant")
    sub = svc.change_subscription(user.tenant_id, tier, billing_period=billing_period)
    svc.db.commit()
    return SubscriptionOut.model_validate(sub)


@router.post("/{user_id}/subscription/renew", response_model=SubscriptionOut, summary="Renew subscription")
def renew_subscription(user_id: str, svc: UserCenterService = Depends(_service)):
    user = svc.get_user(user_id)
    if user.tenant_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user has no tenant")
    sub = svc.renew_subscription(user.tenant_id)
    svc.db.commit()
    return SubscriptionOut.model_validate(sub)


@router.post("/{user_id}/subscription/extend", response_model=SubscriptionOut, summary="Extend expiry")
def extend_subscription(user_id: str, days: int = Query(30, ge=1, le=3650),
                        svc: UserCenterService = Depends(_service)):
    user = svc.get_user(user_id)
    if user.tenant_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user has no tenant")
    sub = svc.extend_subscription(user.tenant_id, days)
    svc.db.commit()
    return SubscriptionOut.model_validate(sub)


# ------------------------------------------------------------- payments
@router.get("/{user_id}/invoices", response_model=list[InvoiceOut], summary="User's invoices")
def user_invoices(user_id: str, svc: UserCenterService = Depends(_service)):
    user = svc.get_user(user_id)
    return [_invoice_out(i) for i in svc.list_invoices(user.tenant_id)]


@router.post("/{user_id}/invoices", response_model=InvoiceOut, summary="Issue a manual invoice")
def issue_invoice(
    user_id: str, plan_tier: str = "free", amount_paise: int = Query(0, ge=0),
    provider: str = "manual", svc: UserCenterService = Depends(_service),
):
    user = svc.get_user(user_id)
    if user.tenant_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user has no tenant")
    inv = svc.issue_invoice(
        tenant_id=user.tenant_id, plan_tier=plan_tier,
        amount_paise=amount_paise, provider=provider,
    )
    svc.db.commit()
    return _invoice_out(inv)


@router.post("/{user_id}/invoices/{invoice_id}/pay", response_model=InvoiceOut, summary="Mark paid")
def pay_invoice(user_id: str, invoice_id: int, svc: UserCenterService = Depends(_service)):
    inv = svc.mark_invoice_paid(invoice_id)
    svc.db.commit()
    return _invoice_out(inv)


@router.post("/{user_id}/invoices/{invoice_id}/refund", response_model=InvoiceOut, summary="Refund invoice")
def refund_invoice(user_id: str, invoice_id: int, svc: UserCenterService = Depends(_service)):
    inv = svc.refund_invoice(invoice_id)
    svc.db.commit()
    return _invoice_out(inv)


# ------------------------------------------------------------- security
@router.get("/{user_id}/security", summary="Security status (2FA, verification, history)")
def security_status(user_id: str, svc: UserCenterService = Depends(_service)):
    return svc.security_status(user_id)


@router.get("/{user_id}/login-history", summary="Login & IP history")
def login_history(user_id: str, limit: int = Query(50, ge=1, le=200),
                  svc: UserCenterService = Depends(_service)):
    return {"items": svc.login_history(user_id, limit)}


@router.post("/{user_id}/reset-password", response_model=MessageResponse, summary="Send password reset")
def reset_password(user_id: str, svc: UserCenterService = Depends(_service)):
    token = svc.request_password_reset(user_id)
    svc.db.commit()
    return MessageResponse(message="Password reset token issued" + (f": {token}" if token else ""))


@router.post("/{user_id}/verify-email", response_model=MessageResponse, summary="Send email verification")
def verify_email(user_id: str, svc: UserCenterService = Depends(_service)):
    token = svc.request_email_verification(user_id)
    svc.db.commit()
    return MessageResponse(message="Verification token issued")


# ------------------------------------------------------------- notifications
@router.post("/{user_id}/notify", response_model=NotificationOut, summary="Send a notification")
def notify_user(
    user_id: str, channel: str = "email", topic: str = "admin", subject: str = "Notification",
    body: str = "", svc: UserCenterService = Depends(_service),
):
    user = svc.get_user(user_id)
    n = svc.send_notification(
        user_id=user.id, tenant_id=user.tenant_id, channel=channel,
        topic=topic, subject=subject, body=body,
    )
    svc.db.commit()
    return _notification_out(n)
