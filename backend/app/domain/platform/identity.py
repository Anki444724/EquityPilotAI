"""Identity, roles and the permission matrix.

This is the authorisation *vocabulary* of the platform. It is deliberately
pure: no SQLAlchemy, no FastAPI, no settings. A permission decision must be
answerable in a unit test with no database and no request, because that is the
only way to prove the matrix says what we think it says.

Three ideas carry the design.

**Permissions are the primitive, roles are a bundle.** Endpoints never ask
"is this an admin?" — they ask "may this caller write a portfolio?". Roles are
then free to change shape without touching a single route. The reverse
arrangement (routes naming roles) is how authorisation logic ends up smeared
across a hundred handlers and quietly diverging.

**The matrix is monotone in seniority but not assumed to be.** Super Admin
does hold a superset of Admin, and Admin of Analyst, and so on down to Guest —
but that is *asserted by a test over the declared data*, not produced by
inheritance. A future role that is powerful in one dimension and weak in
another must be expressible.

**Roles are scoped to a tenant.** A Super Admin is the only role that crosses
tenant boundaries; every other role, however senior, is confined to its own
organisation. That single rule is what makes the multi-tenancy claim true, and
it is enforced here rather than remembered at each call site.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------
class Permission(StrEnum):
    """Every distinct thing a caller may be allowed to do.

    Named `<resource>:<verb>` so the matrix reads as a table. `manage` implies
    administrative control of a resource class rather than of one instance.
    """

    # -- research surface (Modules 1-6) -------------------------------
    COMPANY_READ = "company:read"
    COMPANY_WRITE = "company:write"
    FORECAST_READ = "forecast:read"
    FORECAST_WRITE = "forecast:write"
    VALUATION_READ = "valuation:read"
    SCORING_READ = "scoring:read"
    SCORING_WRITE = "scoring:write"
    AI_READ = "ai:read"
    AI_RUN = "ai:run"

    # -- document intelligence (Module 7) -----------------------------
    DOCUMENT_READ = "document:read"
    DOCUMENT_UPLOAD = "document:upload"
    DOCUMENT_DELETE = "document:delete"

    # -- portfolio (Module 8) -----------------------------------------
    PORTFOLIO_READ = "portfolio:read"
    PORTFOLIO_WRITE = "portfolio:write"
    PORTFOLIO_DELETE = "portfolio:delete"

    # -- reports (Module 9) -------------------------------------------
    REPORT_READ = "report:read"
    REPORT_GENERATE = "report:generate"
    REPORT_DELETE = "report:delete"

    # -- platform (Module 10) -----------------------------------------
    APIKEY_MANAGE = "apikey:manage"
    MEMBER_READ = "member:read"
    MEMBER_MANAGE = "member:manage"
    TENANT_READ = "tenant:read"
    TENANT_MANAGE = "tenant:manage"
    SUBSCRIPTION_READ = "subscription:read"
    SUBSCRIPTION_MANAGE = "subscription:manage"
    USAGE_READ = "usage:read"
    AUDIT_READ = "audit:read"
    JOB_READ = "job:read"
    JOB_MANAGE = "job:manage"

    # -- cross-tenant, operator-only ----------------------------------
    PLATFORM_ADMIN = "platform:admin"
    TENANT_CREATE = "tenant:create"
    PLAN_MANAGE = "plan:manage"
    SYSTEM_READ = "system:read"


#: Permissions that let the holder act outside their own tenant. Any route
#: guarded by one of these must not apply a tenant filter; every other route
#: must.
CROSS_TENANT_PERMISSIONS: frozenset[Permission] = frozenset({
    Permission.PLATFORM_ADMIN,
    Permission.TENANT_CREATE,
    Permission.PLAN_MANAGE,
    Permission.SYSTEM_READ,
})


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------
class Role(StrEnum):
    """The seven roles named in the brief, most senior first."""

    SUPER_ADMIN = "super_admin"
    ADMIN = "admin"
    ANALYST = "analyst"
    RESEARCHER = "researcher"
    SUBSCRIBER = "subscriber"
    READ_ONLY = "read_only"
    GUEST = "guest"


#: Seniority order, most senior first. Used for the monotonicity test and for
#: "can this member manage that member" checks — you may not modify a peer or
#: a senior.
ROLE_ORDER: tuple[Role, ...] = (
    Role.SUPER_ADMIN, Role.ADMIN, Role.ANALYST, Role.RESEARCHER,
    Role.SUBSCRIBER, Role.READ_ONLY, Role.GUEST,
)

ROLE_LABELS: dict[Role, str] = {
    Role.SUPER_ADMIN: "Super Admin",
    Role.ADMIN: "Admin",
    Role.ANALYST: "Analyst",
    Role.RESEARCHER: "Researcher",
    Role.SUBSCRIBER: "Subscriber",
    Role.READ_ONLY: "Read Only",
    Role.GUEST: "Guest",
}

ROLE_DESCRIPTIONS: dict[Role, str] = {
    Role.SUPER_ADMIN: (
        "Platform operator. The only role that crosses tenant boundaries: "
        "creates organisations, edits plans, reads system health."
    ),
    Role.ADMIN: (
        "Owns one organisation. Full research access plus members, billing, "
        "API keys and the tenant's audit trail."
    ),
    Role.ANALYST: (
        "Publishes research. Writes forecasts, scoring overrides, portfolios "
        "and reports; uploads and deletes documents."
    ),
    Role.RESEARCHER: (
        "Contributes research. Writes forecasts and reports and uploads "
        "documents, but cannot delete another member's work or trade a book."
    ),
    Role.SUBSCRIBER: (
        "Consumes research and runs the AI analyst, but authors nothing "
        "beyond generating a report for their own use."
    ),
    Role.READ_ONLY: "Reads everything the tenant has published. Writes nothing.",
    Role.GUEST: "Unauthenticated or invited visitor. Company data only.",
}


# --- the matrix ------------------------------------------------------------
# Declared literally, one row per role. Written out in full rather than by
# inheritance so that the grant for any role can be read at a glance and
# diffed in review — the property that matters most in an access-control
# table. Monotonicity is then *verified* by test rather than assumed.
P = Permission

_GUEST: frozenset[Permission] = frozenset({
    P.COMPANY_READ,
})

_READ_ONLY: frozenset[Permission] = _GUEST | {
    P.FORECAST_READ, P.VALUATION_READ, P.SCORING_READ, P.AI_READ,
    P.DOCUMENT_READ, P.PORTFOLIO_READ, P.REPORT_READ,
}

_SUBSCRIBER: frozenset[Permission] = _READ_ONLY | {
    P.AI_RUN, P.REPORT_GENERATE,
}

_RESEARCHER: frozenset[Permission] = _SUBSCRIBER | {
    P.FORECAST_WRITE, P.DOCUMENT_UPLOAD, P.JOB_READ,
}

_ANALYST: frozenset[Permission] = _RESEARCHER | {
    P.COMPANY_WRITE, P.SCORING_WRITE, P.DOCUMENT_DELETE,
    P.PORTFOLIO_WRITE, P.PORTFOLIO_DELETE, P.REPORT_DELETE,
}

_ADMIN: frozenset[Permission] = _ANALYST | {
    P.APIKEY_MANAGE, P.MEMBER_READ, P.MEMBER_MANAGE,
    P.TENANT_READ, P.TENANT_MANAGE,
    P.SUBSCRIPTION_READ, P.SUBSCRIPTION_MANAGE,
    P.USAGE_READ, P.AUDIT_READ, P.JOB_MANAGE,
}

_SUPER_ADMIN: frozenset[Permission] = _ADMIN | CROSS_TENANT_PERMISSIONS

ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.SUPER_ADMIN: _SUPER_ADMIN,
    Role.ADMIN: _ADMIN,
    Role.ANALYST: _ANALYST,
    Role.RESEARCHER: _RESEARCHER,
    Role.SUBSCRIBER: _SUBSCRIBER,
    Role.READ_ONLY: _READ_ONLY,
    Role.GUEST: _GUEST,
}


def permissions_for(role: Role) -> frozenset[Permission]:
    """Every permission the role grants."""
    return ROLE_PERMISSIONS[role]


def has_permission(role: Role, permission: Permission) -> bool:
    return permission in ROLE_PERMISSIONS[role]


def seniority(role: Role) -> int:
    """0 is most senior. Lower number outranks higher."""
    return ROLE_ORDER.index(role)


def outranks(actor: Role, subject: Role) -> bool:
    """May `actor` administer a member holding `subject`?

    Strictly greater seniority is required, so an Admin cannot demote, remove
    or escalate a fellow Admin. Two people at the same level changing each
    other's access is how an organisation loses its last administrator.
    """
    return seniority(actor) < seniority(subject)


def is_cross_tenant(role: Role) -> bool:
    """True only for roles that may see beyond their own organisation."""
    return bool(ROLE_PERMISSIONS[role] & CROSS_TENANT_PERMISSIONS)


# ---------------------------------------------------------------------------
# Auth methods and lifecycle states
# ---------------------------------------------------------------------------
class AuthProvider(StrEnum):
    """How an identity proved itself."""

    PASSWORD = "password"
    GOOGLE = "google"
    GITHUB = "github"
    MAGIC_LINK = "magic_link"
    API_KEY = "api_key"
    DEV = "dev"


#: Providers that federate to a third party and therefore carry no local
#: password hash and need no local email-verification step — the provider has
#: already verified the address.
FEDERATED_PROVIDERS: frozenset[AuthProvider] = frozenset({
    AuthProvider.GOOGLE, AuthProvider.GITHUB,
})


class UserStatus(StrEnum):
    PENDING = "pending"        # registered, email not yet verified
    ACTIVE = "active"
    SUSPENDED = "suspended"    # blocked by an admin
    DISABLED = "disabled"      # deactivated, retained for audit


#: Only these may authenticate. `PENDING` is deliberately excluded: an
#: unverified address must not become a session.
LOGIN_ALLOWED_STATUSES: frozenset[UserStatus] = frozenset({UserStatus.ACTIVE})


class TenantStatus(StrEnum):
    TRIAL = "trial"
    ACTIVE = "active"
    PAST_DUE = "past_due"      # billing failed; read-only grace period
    SUSPENDED = "suspended"
    CANCELLED = "cancelled"


#: A tenant in one of these states may still read but may not create, so an
#: unpaid invoice degrades the product rather than deleting the customer's
#: access to their own data.
READ_ONLY_TENANT_STATUSES: frozenset[TenantStatus] = frozenset({
    TenantStatus.PAST_DUE,
})

#: A tenant in one of these states may do nothing at all.
BLOCKED_TENANT_STATUSES: frozenset[TenantStatus] = frozenset({
    TenantStatus.SUSPENDED, TenantStatus.CANCELLED,
})


class TokenType(StrEnum):
    ACCESS = "access"
    REFRESH = "refresh"
    EMAIL_VERIFY = "email_verify"
    PASSWORD_RESET = "password_reset"
    MAGIC_LINK = "magic_link"
    MFA_CHALLENGE = "mfa_challenge"


#: Default lifetimes in seconds. Short access tokens with rotating refresh
#: tokens: a stolen access token expires in fifteen minutes, and a stolen
#: refresh token is detected on reuse (see `services/platform/tokens.py`).
TOKEN_TTL_SECONDS: dict[TokenType, int] = {
    TokenType.ACCESS: 15 * 60,
    TokenType.REFRESH: 30 * 24 * 3600,
    TokenType.EMAIL_VERIFY: 24 * 3600,
    TokenType.PASSWORD_RESET: 60 * 60,
    TokenType.MAGIC_LINK: 15 * 60,
    TokenType.MFA_CHALLENGE: 5 * 60,
}


class MFAMethod(StrEnum):
    """Future-ready, as the brief asks. The enrolment record and the
    verification hook exist; no method is enforced at login yet."""

    NONE = "none"
    TOTP = "totp"
    WEBAUTHN = "webauthn"
    EMAIL_OTP = "email_otp"


# ---------------------------------------------------------------------------
# The resolved caller
# ---------------------------------------------------------------------------
class AuthorizationError(Exception):
    """Raised by the domain when a principal lacks a permission.

    The API layer maps this to 403. The domain does not import HTTPException,
    so this rule is testable without a web framework.
    """

    def __init__(self, permission: Permission, role: Role) -> None:
        self.permission = permission
        self.role = role
        super().__init__(
            f"role '{ROLE_LABELS[role]}' lacks permission '{permission}'"
        )


class TenantIsolationError(Exception):
    """Raised when a principal reaches for another tenant's resource."""

    def __init__(self, principal_tenant: int | None, resource_tenant: int | None) -> None:
        self.principal_tenant = principal_tenant
        self.resource_tenant = resource_tenant
        super().__init__("resource belongs to another organisation")


@dataclass(frozen=True, slots=True)
class Principal:
    """The authenticated caller, independent of how they authenticated.

    A session token, an API key and the development identity all resolve to
    this one shape, which is why nothing downstream of authentication needs to
    know which of the three it is dealing with.
    """

    user_id: str
    email: str
    name: str
    role: Role
    tenant_id: int | None = None
    tenant_slug: str | None = None
    provider: AuthProvider = AuthProvider.DEV
    #: Present when the caller authenticated with an API key.
    api_key_id: int | None = None
    #: Set when the tenant is degraded (past-due) — writes are refused even
    #: for roles that would otherwise be allowed.
    tenant_read_only: bool = False
    is_dev_identity: bool = False
    #: Session identifier, carried into the audit trail.
    session_id: str | None = None

    # -- queries ------------------------------------------------------
    @property
    def id(self) -> str:
        """Alias for `user_id`.

        Modules 1-9 were built against a `CurrentUser` that exposed `.id`, and
        every `owner_id` column they write is populated from it. Keeping the
        alias means Module 10 could replace the identity system underneath
        them without editing ninety call sites — and, more importantly,
        without any risk of an ownership column silently changing meaning.
        """
        return self.user_id

    @property
    def permissions(self) -> frozenset[Permission]:
        return ROLE_PERMISSIONS[self.role]

    @property
    def is_platform_operator(self) -> bool:
        return Permission.PLATFORM_ADMIN in self.permissions

    def can(self, permission: Permission) -> bool:
        if permission not in self.permissions:
            return False
        if self.tenant_read_only and _is_write(permission):
            return False
        return True

    # -- assertions ---------------------------------------------------
    def require(self, *permissions: Permission) -> None:
        """All of `permissions`, or raise. Conjunctive by design: a route that
        genuinely accepts either of two permissions should say so explicitly
        rather than relying on a disjunctive default nobody notices."""
        for permission in permissions:
            if not self.can(permission):
                raise AuthorizationError(permission, self.role)

    def require_any(self, *permissions: Permission) -> None:
        if not any(self.can(p) for p in permissions):
            raise AuthorizationError(permissions[0], self.role)

    def require_tenant(self, tenant_id: int | None) -> None:
        """Assert the caller may touch a resource owned by `tenant_id`.

        The platform operator passes. Everyone else must match exactly — and
        a principal with no tenant may not reach a tenant-owned resource,
        which closes the "null means wildcard" hole.
        """
        if self.is_platform_operator:
            return
        if tenant_id is None or self.tenant_id is None or tenant_id != self.tenant_id:
            raise TenantIsolationError(self.tenant_id, tenant_id)


#: Verbs that mutate. Used to degrade a past-due tenant to read-only without
#: enumerating every write permission at every call site.
_WRITE_SUFFIXES = ("write", "delete", "manage", "upload", "generate", "run", "create")


def _is_write(permission: Permission) -> bool:
    return permission.value.rsplit(":", 1)[-1] in _WRITE_SUFFIXES


def write_permissions() -> frozenset[Permission]:
    """Every permission classified as a mutation. Exposed for the tests and
    for the admin UI's permission matrix, so the classification is visible
    rather than buried in a helper."""
    return frozenset(p for p in Permission if _is_write(p))
