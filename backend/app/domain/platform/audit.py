"""The audit trail vocabulary and redaction rules.

The workbook's `AI Logs` sheet states the requirement in one line: *"Every
upload, extraction, API call and refresh is logged here with a timestamp. API
keys are never logged."* That second clause is the load-bearing one, and it is
implemented here as code rather than left as a convention.

Design notes.

**An audit event is immutable and self-describing.** It records who, what,
which resource, from where, and the outcome. It never stores a diff of the
whole object — only the fields that changed, redacted. Storing whole payloads
is how secrets end up in the log that was supposed to prove they were kept
safe.

**Redaction is deny-by-default on key name.** A key whose name matches a
sensitive pattern is replaced, whatever its value and however deeply nested.
The alternative — an allow-list of safe keys — fails silently the first time
someone adds a field.

**Severity is derived, not typed by hand.** Each action declares its severity
once, so a security-relevant event cannot be logged at `info` because the call
site was written in a hurry.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class AuditCategory(StrEnum):
    AUTH = "auth"
    ACCOUNT = "account"
    TENANT = "tenant"
    BILLING = "billing"
    RESEARCH = "research"
    DOCUMENT = "document"
    PORTFOLIO = "portfolio"
    REPORT = "report"
    AI = "ai"
    ADMIN = "admin"
    SECURITY = "security"
    SYSTEM = "system"


class AuditSeverity(StrEnum):
    INFO = "info"
    NOTICE = "notice"      # a change worth reading in a weekly review
    WARNING = "warning"    # refused or suspicious, but handled
    CRITICAL = "critical"  # security-relevant, page someone


class AuditAction(StrEnum):
    """Every recordable action. Adding one here is the only way to log."""

    # -- authentication -----------------------------------------------
    LOGIN_SUCCEEDED = "auth.login.succeeded"
    LOGIN_FAILED = "auth.login.failed"
    LOGOUT = "auth.logout"
    TOKEN_REFRESHED = "auth.token.refreshed"
    TOKEN_REUSE_DETECTED = "auth.token.reuse_detected"
    MAGIC_LINK_REQUESTED = "auth.magic_link.requested"
    MAGIC_LINK_CONSUMED = "auth.magic_link.consumed"
    PASSWORD_RESET_REQUESTED = "auth.password.reset_requested"
    PASSWORD_RESET_COMPLETED = "auth.password.reset_completed"
    PASSWORD_CHANGED = "auth.password.changed"
    EMAIL_VERIFICATION_SENT = "auth.email.verification_sent"
    EMAIL_VERIFIED = "auth.email.verified"
    OAUTH_LINKED = "auth.oauth.linked"
    MFA_ENROLLED = "auth.mfa.enrolled"
    MFA_DISABLED = "auth.mfa.disabled"

    # -- accounts and membership --------------------------------------
    USER_REGISTERED = "account.user.registered"
    USER_INVITED = "account.user.invited"
    USER_ROLE_CHANGED = "account.user.role_changed"
    USER_SUSPENDED = "account.user.suspended"
    USER_REACTIVATED = "account.user.reactivated"
    USER_DELETED = "account.user.deleted"

    # -- tenants -------------------------------------------------------
    TENANT_CREATED = "tenant.created"
    TENANT_UPDATED = "tenant.updated"
    TENANT_SETTINGS_CHANGED = "tenant.settings.changed"
    TENANT_SUSPENDED = "tenant.suspended"
    TENANT_REACTIVATED = "tenant.reactivated"

    # -- billing --------------------------------------------------------
    SUBSCRIPTION_CREATED = "billing.subscription.created"
    SUBSCRIPTION_CHANGED = "billing.subscription.changed"
    SUBSCRIPTION_CANCELLED = "billing.subscription.cancelled"
    PLAN_UPDATED = "billing.plan.updated"
    INVOICE_ISSUED = "billing.invoice.issued"
    PAYMENT_SUCCEEDED = "billing.payment.succeeded"
    PAYMENT_FAILED = "billing.payment.failed"
    BILLING_WEBHOOK_RECEIVED = "billing.webhook.received"

    # -- API keys --------------------------------------------------------
    APIKEY_CREATED = "security.apikey.created"
    APIKEY_REVOKED = "security.apikey.revoked"
    APIKEY_USED = "security.apikey.used"

    # -- product actions ---------------------------------------------------
    DOCUMENT_UPLOADED = "document.uploaded"
    DOCUMENT_PROCESSED = "document.processed"
    DOCUMENT_DELETED = "document.deleted"
    REPORT_GENERATED = "report.generated"
    REPORT_DOWNLOADED = "report.downloaded"
    REPORT_DELETED = "report.deleted"
    PORTFOLIO_CREATED = "portfolio.created"
    PORTFOLIO_TRANSACTION = "portfolio.transaction"
    FORECAST_SAVED = "research.forecast.saved"
    AI_CALL = "ai.call"

    # -- enforcement --------------------------------------------------------
    ACCESS_DENIED = "security.access.denied"
    TENANT_ISOLATION_VIOLATION = "security.tenant.isolation_violation"
    RATE_LIMITED = "security.rate_limited"
    QUOTA_EXCEEDED = "security.quota_exceeded"

    # -- operations ---------------------------------------------------------
    JOB_ENQUEUED = "system.job.enqueued"
    JOB_COMPLETED = "system.job.completed"
    JOB_FAILED = "system.job.failed"
    BACKUP_CREATED = "system.backup.created"
    BACKUP_RESTORED = "system.backup.restored"
    SETTINGS_CHANGED = "system.settings.changed"


#: Category and severity, declared once per action.
_ACTION_META: dict[AuditAction, tuple[AuditCategory, AuditSeverity]] = {
    AuditAction.LOGIN_SUCCEEDED: (AuditCategory.AUTH, AuditSeverity.INFO),
    AuditAction.LOGIN_FAILED: (AuditCategory.AUTH, AuditSeverity.WARNING),
    AuditAction.LOGOUT: (AuditCategory.AUTH, AuditSeverity.INFO),
    AuditAction.TOKEN_REFRESHED: (AuditCategory.AUTH, AuditSeverity.INFO),
    AuditAction.TOKEN_REUSE_DETECTED: (AuditCategory.SECURITY, AuditSeverity.CRITICAL),
    AuditAction.MAGIC_LINK_REQUESTED: (AuditCategory.AUTH, AuditSeverity.INFO),
    AuditAction.MAGIC_LINK_CONSUMED: (AuditCategory.AUTH, AuditSeverity.NOTICE),
    AuditAction.PASSWORD_RESET_REQUESTED: (AuditCategory.AUTH, AuditSeverity.NOTICE),
    AuditAction.PASSWORD_RESET_COMPLETED: (AuditCategory.AUTH, AuditSeverity.NOTICE),
    AuditAction.PASSWORD_CHANGED: (AuditCategory.AUTH, AuditSeverity.NOTICE),
    AuditAction.EMAIL_VERIFICATION_SENT: (AuditCategory.AUTH, AuditSeverity.INFO),
    AuditAction.EMAIL_VERIFIED: (AuditCategory.AUTH, AuditSeverity.NOTICE),
    AuditAction.OAUTH_LINKED: (AuditCategory.AUTH, AuditSeverity.NOTICE),
    AuditAction.MFA_ENROLLED: (AuditCategory.SECURITY, AuditSeverity.NOTICE),
    AuditAction.MFA_DISABLED: (AuditCategory.SECURITY, AuditSeverity.WARNING),

    AuditAction.USER_REGISTERED: (AuditCategory.ACCOUNT, AuditSeverity.NOTICE),
    AuditAction.USER_INVITED: (AuditCategory.ACCOUNT, AuditSeverity.NOTICE),
    AuditAction.USER_ROLE_CHANGED: (AuditCategory.ACCOUNT, AuditSeverity.NOTICE),
    AuditAction.USER_SUSPENDED: (AuditCategory.ACCOUNT, AuditSeverity.WARNING),
    AuditAction.USER_REACTIVATED: (AuditCategory.ACCOUNT, AuditSeverity.NOTICE),
    AuditAction.USER_DELETED: (AuditCategory.ACCOUNT, AuditSeverity.WARNING),

    AuditAction.TENANT_CREATED: (AuditCategory.TENANT, AuditSeverity.NOTICE),
    AuditAction.TENANT_UPDATED: (AuditCategory.TENANT, AuditSeverity.INFO),
    AuditAction.TENANT_SETTINGS_CHANGED: (AuditCategory.TENANT, AuditSeverity.NOTICE),
    AuditAction.TENANT_SUSPENDED: (AuditCategory.TENANT, AuditSeverity.WARNING),
    AuditAction.TENANT_REACTIVATED: (AuditCategory.TENANT, AuditSeverity.NOTICE),

    AuditAction.SUBSCRIPTION_CREATED: (AuditCategory.BILLING, AuditSeverity.NOTICE),
    AuditAction.SUBSCRIPTION_CHANGED: (AuditCategory.BILLING, AuditSeverity.NOTICE),
    AuditAction.SUBSCRIPTION_CANCELLED: (AuditCategory.BILLING, AuditSeverity.WARNING),
    AuditAction.PLAN_UPDATED: (AuditCategory.BILLING, AuditSeverity.NOTICE),
    AuditAction.INVOICE_ISSUED: (AuditCategory.BILLING, AuditSeverity.INFO),
    AuditAction.PAYMENT_SUCCEEDED: (AuditCategory.BILLING, AuditSeverity.INFO),
    AuditAction.PAYMENT_FAILED: (AuditCategory.BILLING, AuditSeverity.WARNING),
    AuditAction.BILLING_WEBHOOK_RECEIVED: (AuditCategory.BILLING, AuditSeverity.INFO),

    AuditAction.APIKEY_CREATED: (AuditCategory.SECURITY, AuditSeverity.NOTICE),
    AuditAction.APIKEY_REVOKED: (AuditCategory.SECURITY, AuditSeverity.NOTICE),
    AuditAction.APIKEY_USED: (AuditCategory.SECURITY, AuditSeverity.INFO),

    AuditAction.DOCUMENT_UPLOADED: (AuditCategory.DOCUMENT, AuditSeverity.INFO),
    AuditAction.DOCUMENT_PROCESSED: (AuditCategory.DOCUMENT, AuditSeverity.INFO),
    AuditAction.DOCUMENT_DELETED: (AuditCategory.DOCUMENT, AuditSeverity.NOTICE),
    AuditAction.REPORT_GENERATED: (AuditCategory.REPORT, AuditSeverity.INFO),
    AuditAction.REPORT_DOWNLOADED: (AuditCategory.REPORT, AuditSeverity.INFO),
    AuditAction.REPORT_DELETED: (AuditCategory.REPORT, AuditSeverity.NOTICE),
    AuditAction.PORTFOLIO_CREATED: (AuditCategory.PORTFOLIO, AuditSeverity.INFO),
    AuditAction.PORTFOLIO_TRANSACTION: (AuditCategory.PORTFOLIO, AuditSeverity.INFO),
    AuditAction.FORECAST_SAVED: (AuditCategory.RESEARCH, AuditSeverity.INFO),
    AuditAction.AI_CALL: (AuditCategory.AI, AuditSeverity.INFO),

    AuditAction.ACCESS_DENIED: (AuditCategory.SECURITY, AuditSeverity.WARNING),
    AuditAction.TENANT_ISOLATION_VIOLATION: (AuditCategory.SECURITY, AuditSeverity.CRITICAL),
    AuditAction.RATE_LIMITED: (AuditCategory.SECURITY, AuditSeverity.WARNING),
    AuditAction.QUOTA_EXCEEDED: (AuditCategory.BILLING, AuditSeverity.WARNING),

    AuditAction.JOB_ENQUEUED: (AuditCategory.SYSTEM, AuditSeverity.INFO),
    AuditAction.JOB_COMPLETED: (AuditCategory.SYSTEM, AuditSeverity.INFO),
    AuditAction.JOB_FAILED: (AuditCategory.SYSTEM, AuditSeverity.WARNING),
    AuditAction.BACKUP_CREATED: (AuditCategory.SYSTEM, AuditSeverity.NOTICE),
    AuditAction.BACKUP_RESTORED: (AuditCategory.SYSTEM, AuditSeverity.CRITICAL),
    AuditAction.SETTINGS_CHANGED: (AuditCategory.SYSTEM, AuditSeverity.NOTICE),
}


def category_of(action: AuditAction) -> AuditCategory:
    return _ACTION_META[action][0]


def severity_of(action: AuditAction) -> AuditSeverity:
    return _ACTION_META[action][1]


#: Actions a tenant administrator may read. Everything else is operator-only,
#: because one customer must not learn about another's payment failures.
def tenant_visible(action: AuditAction) -> bool:
    return category_of(action) is not AuditCategory.SYSTEM


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------
#: Key names whose values must never reach the log. Matched case-insensitively
#: as a substring, so `openrouter_api_key`, `apiKey` and `X-API-Key` all hit.
SENSITIVE_KEY_PATTERN = re.compile(
    r"(password|passwd|secret|token|api[_-]?key|apikey|authorization|auth|"
    r"credential|private[_-]?key|session|cookie|otp|pin|signature|salt|hash)",
    re.IGNORECASE,
)

REDACTED = "[redacted]"

#: Values that look like a credential even under an innocent key name.
_VALUE_PATTERNS = (
    re.compile(r"^sk-[A-Za-z0-9_-]{16,}$"),           # OpenAI-style
    re.compile(r"^ierp_(live|test)_[A-Za-z0-9]{8,}$"),  # our own keys
    re.compile(r"^eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\."),  # JWT
    re.compile(r"^gh[pousr]_[A-Za-z0-9]{20,}$"),       # GitHub tokens
)

_MAX_VALUE_CHARS = 512


def redact(payload: Any, _depth: int = 0) -> Any:
    """Return `payload` with every credential-shaped key or value removed.

    Recurses through dicts, lists and tuples. Depth-limited because an audit
    payload that is twenty levels deep is a bug, and a cycle would otherwise
    hang the logger — a logging call must never be able to take the process
    down.
    """
    if _depth > 8:
        return "[truncated]"

    if isinstance(payload, dict):
        out: dict[str, Any] = {}
        for key, value in payload.items():
            name = str(key)
            if SENSITIVE_KEY_PATTERN.search(name):
                out[name] = REDACTED
            else:
                out[name] = redact(value, _depth + 1)
        return out

    if isinstance(payload, (list, tuple)):
        return [redact(v, _depth + 1) for v in payload]

    if isinstance(payload, str):
        if any(p.match(payload) for p in _VALUE_PATTERNS):
            return REDACTED
        if len(payload) > _MAX_VALUE_CHARS:
            return payload[:_MAX_VALUE_CHARS] + "…"
        return payload

    return payload


def mask_email(email: str) -> str:
    """`analyst@example.com` → `a******t@example.com`.

    Used where an audit row is shown to someone who should see that an event
    happened without learning the whole address — a failed-login list, for
    instance, which is otherwise a directory of valid usernames.
    """
    if "@" not in email:
        return REDACTED
    local, _, domain = email.partition("@")
    if len(local) <= 2:
        return f"{local[:1]}*@{domain}"
    return f"{local[0]}{'*' * (len(local) - 2)}{local[-1]}@{domain}"


def mask_secret(secret: str, keep: int = 4) -> str:
    """Show only the last `keep` characters — the form used for API keys in
    the admin panel, where an operator needs to recognise a key without being
    able to use it."""
    if len(secret) <= keep:
        return "*" * len(secret)
    return f"{'*' * (len(secret) - keep)}{secret[-keep:]}"


# ---------------------------------------------------------------------------
# The event
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class AuditEvent:
    """One immutable trail entry, ready to persist.

    Constructed through `build()` so category, severity and redaction are
    applied uniformly; the constructor is never called directly by callers.
    """

    action: AuditAction
    category: AuditCategory
    severity: AuditSeverity
    at: datetime
    actor_id: str | None = None
    actor_email: str | None = None
    actor_role: str | None = None
    tenant_id: int | None = None
    resource_type: str | None = None
    resource_id: str | None = None
    summary: str = ""
    outcome: str = "success"
    ip_address: str | None = None
    user_agent: str | None = None
    request_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_security_relevant(self) -> bool:
        return (
            self.category in (AuditCategory.SECURITY, AuditCategory.AUTH)
            or self.severity is AuditSeverity.CRITICAL
        )


def build(
    action: AuditAction,
    *,
    at: datetime,
    actor_id: str | None = None,
    actor_email: str | None = None,
    actor_role: str | None = None,
    tenant_id: int | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    summary: str = "",
    outcome: str = "success",
    ip_address: str | None = None,
    user_agent: str | None = None,
    request_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> AuditEvent:
    """Build a redacted event. The only supported way to create one."""
    return AuditEvent(
        action=action,
        category=category_of(action),
        severity=severity_of(action),
        at=at,
        actor_id=actor_id,
        actor_email=actor_email,
        actor_role=actor_role,
        tenant_id=tenant_id,
        resource_type=resource_type,
        resource_id=resource_id,
        summary=summary or action.value,
        outcome=outcome,
        ip_address=ip_address,
        user_agent=(user_agent or "")[:300] or None,
        request_id=request_id,
        metadata=redact(metadata or {}),
    )
