"""Enterprise User Management & Subscription Center (Phase 7).

Reuses the platform's existing identity / entitlement / notification /
session machinery and adds an operator-facing API: users (add / edit / ban /
suspend / restore / delete), roles, subscriptions (upgrade / downgrade / renew /
extend), payments (manual invoice / refund), sessions (list / logout-all /
force logout), security (2FA / reset / verification / login & IP history),
notifications (email / push / announcement), and analytics (new / active /
premium / revenue / retention).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.security import Principal
from app.domain.platform.identity import Role, TokenType, UserStatus
from app.domain.platform.plans import PlanTier
from app.models.platform import (
    Invoice, Notification, RefreshToken, Subscription, Tenant, User,
)
from app.schemas.platform import UserOut
from app.services.platform.entitlements import BillingError, EntitlementService
from app.services.platform.identity_service import AuthError, IdentityService


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _temporary_password() -> str:
    """A policy-compliant temporary password for a user created without one."""
    import secrets
    return f"Temp-{secrets.token_hex(8)}-1aZ"


class UserCenterError(Exception):
    """Raised when a user-center operation cannot be honoured."""


class UserCenterService:
    """Operator-facing user, subscription, session, payment and analytics ops."""

    def __init__(self, db: Session) -> None:
        self.db = db
        self.identity = IdentityService(db)
        self.billing = EntitlementService(db)

    # ==================================================================
    # Users
    # ==================================================================
    def list_users(
        self, *, role: str | None = None, status: str | None = None,
        search: str | None = None, tenant_id: int | None = None,
        page: int = 1, page_size: int = 25,
    ) -> tuple[list[UserOut], int]:
        stmt = select(User)
        if role:
            stmt = stmt.where(User.role == role)
        if status:
            stmt = stmt.where(User.status == status)
        if tenant_id is not None:
            stmt = stmt.where(User.tenant_id == tenant_id)
        if search:
            like = f"%{search.lower()}%"
            stmt = stmt.where(
                func.lower(User.email).like(like)
                | func.lower(User.name).like(like)
                | func.lower(User.username).like(like)
            )
        total = self.db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
        rows = self.db.execute(
            stmt.order_by(User.created_at.desc())
            .offset((page - 1) * page_size).limit(page_size)
        ).scalars().all()
        return [UserOut.model_validate(r) for r in rows], int(total)

    def get_user(self, user_id: str) -> User:
        user = self.db.get(User, user_id)
        if user is None:
            raise UserCenterError("user not found")
        return user

    def create_user(
        self, *, email: str, name: str, role: str, tenant_id: int | None = None,
        status: str = "active", password: str | None = None,
        actor: Principal | None = None,
    ) -> User:
        role_enum = Role(role)
        # Self-serve: no tenant -> creates a new organisation owned by the user.
        user, _ = self.identity.register(
            email=email, password=password or _temporary_password(),
            name=name, tenant_id=tenant_id, role=role_enum, auto_verify=True,
        )
        user.status = status
        self.db.commit()
        return user

    def update_user(
        self, user_id: str, *, name: str | None = None, role: str | None = None,
        actor: Principal | None = None,
    ) -> User:
        user = self.get_user(user_id)
        if name is not None:
            user.name = name
        if role is not None and role != user.role:
            self.identity.change_role(actor, user, Role(role))
        self.db.commit()
        self.db.refresh(user)
        return user

    def set_status(
        self, user_id: str, status: str, *, actor: Principal | None = None,
    ) -> User:
        user = self.get_user(user_id)
        self.identity.set_status(actor, user, UserStatus(status))
        return user

    def delete_user(self, user_id: str) -> None:
        user = self.get_user(user_id)
        self.identity.revoke_all_sessions(user_id, "user_deleted")
        self.db.delete(user)
        self.db.commit()

    # ==================================================================
    # Sessions
    # ==================================================================
    def active_sessions(self, user_id: str) -> list[dict[str, Any]]:
        rows = self.identity.active_sessions(user_id)
        return [
            {
                "session_id": r.session_id,
                "ip_address": r.ip_address,
                "user_agent": r.user_agent,
                "issued_at": r.issued_at.isoformat(),
                "expires_at": r.expires_at.isoformat(),
            }
            for r in rows
        ]

    def logout_all(self, user_id: str) -> int:
        return self.identity.revoke_all_sessions(user_id, "admin_logout_all")

    def force_logout(self, user_id: str, session_id: str) -> int:
        rows = self.db.execute(
            RefreshToken.__table__.update()
            .where(
                RefreshToken.user_id == user_id,
                RefreshToken.session_id == session_id,
                RefreshToken.revoked_at.is_(None),
            )
            .values(revoked_at=_utcnow(), revoked_reason="admin_force_logout")
        )
        self.db.commit()
        return int(rows.rowcount or 0)

    # ==================================================================
    # Subscriptions
    # ==================================================================
    def change_subscription(
        self, tenant_id: int, tier: str, *, billing_period: str = "monthly",
        actor: Principal | None = None,
    ) -> Subscription:
        from app.domain.platform.plans import BillingPeriod
        return self.billing.change_plan(
            tenant_id, PlanTier(tier), billing_period=BillingPeriod(billing_period),
        )

    def renew_subscription(self, tenant_id: int) -> Subscription:
        sub = self.billing.subscription_for(tenant_id)
        sub = self.billing._roll_period(sub)
        sub.status = "active"
        sub.cancel_at_period_end = False
        self.db.commit()
        self.db.refresh(sub)
        return sub

    def extend_subscription(self, tenant_id: int, days: int) -> Subscription:
        sub = self.billing.subscription_for(tenant_id)
        sub.period_end = sub.period_end + timedelta(days=days)
        self.db.commit()
        self.db.refresh(sub)
        return sub

    # ==================================================================
    # Payments / invoices
    # ==================================================================
    def issue_invoice(
        self, *, tenant_id: int, plan_tier: str, amount_paise: int,
        provider: str = "manual", currency: str = "INR",
    ) -> Invoice:
        sub = self.billing.subscription_for(tenant_id)
        invoice = Invoice(
            tenant_id=tenant_id,
            number=f"INV-{tenant_id}-{int(_utcnow().timestamp())}",
            plan_tier=plan_tier,
            period_start=sub.period_start, period_end=sub.period_end,
            subtotal_paise=amount_paise, tax_paise=0, total_paise=amount_paise,
            currency=currency, status="issued", issued_at=_utcnow(),
            line_items=[{"provider": provider, "amount_paise": amount_paise}],
        )
        self.db.add(invoice)
        self.db.commit()
        return invoice

    def mark_invoice_paid(self, invoice_id: int) -> Invoice:
        inv = self.db.get(Invoice, invoice_id)
        if inv is None:
            raise UserCenterError("invoice not found")
        inv.status = "paid"
        inv.paid_at = _utcnow()
        self.db.commit()
        self.db.refresh(inv)
        return inv

    def refund_invoice(self, invoice_id: int) -> Invoice:
        inv = self.db.get(Invoice, invoice_id)
        if inv is None:
            raise UserCenterError("invoice not found")
        inv.status = "refunded"
        self.db.commit()
        self.db.refresh(inv)
        return inv

    def list_invoices(self, tenant_id: int | None = None) -> list[Invoice]:
        stmt = select(Invoice)
        if tenant_id is not None:
            stmt = stmt.where(Invoice.tenant_id == tenant_id)
        return list(self.db.execute(stmt.order_by(Invoice.issued_at.desc())).scalars())

    # ==================================================================
    # Notifications
    # ==================================================================
    def send_notification(
        self, *, user_id: str | None = None, tenant_id: int | None = None,
        channel: str, topic: str, subject: str, body: str, link: str | None = None,
    ) -> Notification:
        n = Notification(
            user_id=user_id, tenant_id=tenant_id, channel=channel, topic=topic,
            subject=subject, body=body, link=link, sent_at=_utcnow(),
            delivery_status="sent",
        )
        self.db.add(n)
        self.db.commit()
        return n

    def announce(self, *, subject: str, body: str, channel: str = "email") -> int:
        """Send an announcement to all active users."""
        users = self.db.execute(
            select(User).where(User.status == "active")
        ).scalars().all()
        for u in users:
            self.db.add(Notification(
                user_id=u.id, tenant_id=u.tenant_id, channel=channel,
                topic="announcement", subject=subject, body=body, sent_at=_utcnow(),
                delivery_status="sent",
            ))
        self.db.commit()
        return len(users)

    # ==================================================================
    # Security / history
    # ==================================================================
    def security_status(self, user_id: str) -> dict[str, Any]:
        user = self.get_user(user_id)
        return {
            "user_id": user.id,
            "email_verified": user.email_verified_at is not None,
            "mfa_method": user.mfa_method,
            "mfa_ready": user.mfa_method != "none" and user.mfa_enrolled_at is not None,
            "password_changed_at": user.password_changed_at.isoformat() if user.password_changed_at else None,
            "failed_login_count": user.failed_login_count,
            "locked_until": user.locked_until.isoformat() if user.locked_until else None,
            "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
        }

    def login_history(self, user_id: str, limit: int = 50) -> list[dict[str, Any]]:
        """Recent login events from the audit trail."""
        from app.models.platform import AuditLog
        rows = self.db.execute(
            select(AuditLog).where(
                AuditLog.actor_id == user_id,
                AuditLog.action.in_(["auth.login.succeeded", "auth.login.failed"]),
            ).order_by(AuditLog.occurred_at.desc()).limit(limit)
        ).scalars().all()
        return [
            {
                "occurred_at": r.occurred_at.isoformat(),
                "action": r.action,
                "outcome": r.outcome,
                "ip_address": r.ip_address,
                "user_agent": r.user_agent,
            }
            for r in rows
        ]

    def request_password_reset(self, user_id: str) -> str:
        user = self.get_user(user_id)
        pending = self.identity.request_password_reset(user.email)
        return pending.token if pending else ""

    def request_email_verification(self, user_id: str) -> str:
        user = self.get_user(user_id)
        pending = self.identity.issue_one_time_token(user, TokenType.EMAIL_VERIFY)
        return pending.token

    # ==================================================================
    # Analytics
    # ==================================================================
    def analytics(self, *, days: int = 30) -> dict[str, Any]:
        since = _utcnow() - timedelta(days=days)
        total_users = int(self.db.execute(select(func.count(User.id))).scalar_one() or 0)
        active_users = int(self.db.execute(
            select(func.count(User.id)).where(User.status == "active")
        ).scalar_one() or 0)
        new_users = int(self.db.execute(
            select(func.count(User.id)).where(User.created_at >= since)
        ).scalar_one() or 0)

        premium = 0
        revenue_paise = 0
        try:
            revenue = self.billing.platform_revenue()
            premium = sum(self.billing.tier_distribution().values())
            revenue_paise = revenue.get("monthly_paise", 0) + revenue.get("annual_paise", 0)
        except Exception:  # noqa: BLE001
            pass

        tenants = int(self.db.execute(select(func.count(Tenant.id))).scalar_one() or 0)

        # Retention: users with a login event in the window.
        from app.models.platform import AuditLog
        retained = int(self.db.execute(
            select(func.count(func.distinct(AuditLog.actor_id))).where(
                AuditLog.action == "auth.login.succeeded",
                AuditLog.occurred_at >= since,
            )
        ).scalar_one() or 0)

        return {
            "days": days,
            "total_users": total_users,
            "active_users": active_users,
            "new_users": new_users,
            "premium_users": premium,
            "free_users": max(0, total_users - premium),
            "revenue_inr": round(revenue_paise / 100, 2),
            "tenants": tenants,
            "retention_pct": round((retained / total_users * 100), 1) if total_users else 0.0,
        }
