"""Rate limiting and password policy — pure algorithms, no storage.

Both are here for the same reason: they are rules, they must be identical
everywhere they are applied, and they are far easier to prove correct without
a database or a clock attached.

The rate limiter is a **sliding-window counter**. A fixed window lets a caller
send a full window's allowance in the last instant of one window and again in
the first instant of the next — twice the intended rate across the boundary. A
true sliding log is exact but stores every timestamp. The weighted two-window
approximation used here is within a few percent of exact at a fraction of the
cost, and is what production limiters generally use.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import StrEnum


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------
class RateScope(StrEnum):
    """What the counter is keyed on. Distinct scopes so an aggressive IP
    cannot exhaust a paying tenant's allowance, and vice versa."""

    IP = "ip"
    USER = "user"
    TENANT = "tenant"
    API_KEY = "api_key"
    ENDPOINT = "endpoint"


@dataclass(frozen=True, slots=True)
class RateRule:
    """`limit` requests per `window_seconds`, with an optional burst."""

    scope: RateScope
    limit: int
    window_seconds: int = 60
    #: Requests permitted above `limit` momentarily. Absorbs the page-load
    #: fan-out of a dashboard that fires eight requests at once.
    burst: int = 0

    @property
    def capacity(self) -> int:
        return self.limit + self.burst

    @property
    def per_second(self) -> float:
        return self.limit / self.window_seconds


#: Defaults applied when no plan-specific limit governs. Login is deliberately
#: severe: it is the endpoint an attacker enumerates.
DEFAULT_RULES: dict[str, RateRule] = {
    "default": RateRule(RateScope.USER, limit=300, window_seconds=60, burst=60),
    "anonymous": RateRule(RateScope.IP, limit=60, window_seconds=60, burst=20),
    "auth.login": RateRule(RateScope.IP, limit=10, window_seconds=300),
    "auth.register": RateRule(RateScope.IP, limit=5, window_seconds=3600),
    "auth.magic_link": RateRule(RateScope.IP, limit=5, window_seconds=900),
    "auth.password_reset": RateRule(RateScope.IP, limit=5, window_seconds=3600),
    "ai.run": RateRule(RateScope.TENANT, limit=60, window_seconds=60),
    "report.generate": RateRule(RateScope.TENANT, limit=20, window_seconds=60),
    "document.upload": RateRule(RateScope.TENANT, limit=30, window_seconds=60),
}


@dataclass(frozen=True, slots=True)
class RateDecision:
    """The outcome, shaped so the caller can emit the standard headers."""

    allowed: bool
    limit: int
    remaining: int
    reset_after: float          # seconds until the window frees capacity
    retry_after: float = 0.0    # seconds the caller should wait; 0 when allowed
    used: float = 0.0

    def headers(self) -> dict[str, str]:
        """RFC-6585 / draft-ietf-httpapi style headers."""
        out = {
            "X-RateLimit-Limit": str(self.limit),
            "X-RateLimit-Remaining": str(max(0, self.remaining)),
            "X-RateLimit-Reset": str(int(math.ceil(self.reset_after))),
        }
        if not self.allowed:
            out["Retry-After"] = str(int(math.ceil(self.retry_after)))
        return out


def sliding_window(
    *,
    rule: RateRule,
    previous_count: int,
    current_count: int,
    elapsed_in_window: float,
) -> RateDecision:
    """Decide on a request using the weighted two-window approximation.

    `previous_count` is the completed window's total, `current_count` the
    in-flight window's, and `elapsed_in_window` how far into the current
    window we are. The previous window's contribution decays linearly as the
    current one fills:

        estimate = previous × (1 − elapsed/window) + current

    The request being decided is *not* yet counted in `current_count`; the
    caller increments only if the decision allows it.
    """
    window = float(rule.window_seconds)
    elapsed = min(max(elapsed_in_window, 0.0), window)
    decay = 1.0 - (elapsed / window)
    estimate = previous_count * decay + current_count

    capacity = rule.capacity
    allowed = estimate + 1 <= capacity
    remaining = int(max(0, math.floor(capacity - estimate - 1)))
    reset_after = window - elapsed

    retry_after = 0.0
    if not allowed:
        # How long until enough of the previous window decays away to make
        # room for one more request. If the current window alone is already
        # over capacity, nothing helps until it rolls over.
        if previous_count > 0 and current_count + 1 <= capacity:
            needed_decay = (estimate + 1 - capacity) / previous_count
            retry_after = min(window, max(0.0, needed_decay * window))
        else:
            retry_after = reset_after

    return RateDecision(
        allowed=allowed, limit=rule.limit, remaining=remaining,
        reset_after=round(reset_after, 3), retry_after=round(retry_after, 3),
        used=round(estimate, 3),
    )


# ---------------------------------------------------------------------------
# Password policy
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class PasswordPolicy:
    """What counts as an acceptable password.

    Length is weighted far above character-class rules, following current NIST
    guidance: `Tr0ub4dor&3` is weaker than `correct horse battery staple` and
    much harder to remember. Classes are still required at short lengths, and
    dropped once the password is long enough to make them irrelevant.
    """

    min_length: int = 10
    max_length: int = 256
    #: At or above this length, character-class requirements are waived.
    passphrase_length: int = 16
    require_lower: bool = True
    require_upper: bool = True
    require_digit: bool = True
    require_symbol: bool = False
    #: Reject passwords containing the local part of the user's own email.
    reject_email_local: bool = True


DEFAULT_PASSWORD_POLICY = PasswordPolicy()

#: A short deny-list of passwords that appear at the top of every breach
#: corpus. A real deployment should point at a full list; this catches the
#: worst offenders with no dependency.
COMMON_PASSWORDS: frozenset[str] = frozenset({
    "password", "password1", "password123", "12345678", "123456789",
    "1234567890", "qwerty", "qwerty123", "letmein", "welcome", "welcome1",
    "admin", "admin123", "iloveyou", "abc123", "monkey", "dragon",
    "sunshine", "princess", "football", "baseball", "trustno1", "passw0rd",
    "changeme", "secret", "master", "starwars", "whatever", "zaq12wsx",
})

_SYMBOLS = re.compile(r"[^A-Za-z0-9]")


def validate_password(
    password: str,
    *,
    policy: PasswordPolicy = DEFAULT_PASSWORD_POLICY,
    email: str | None = None,
) -> list[str]:
    """Return a list of problems. Empty means acceptable.

    Returns *all* failures rather than the first, so a user fixes their
    password in one attempt instead of five.
    """
    problems: list[str] = []

    if len(password) < policy.min_length:
        problems.append(
            f"Must be at least {policy.min_length} characters "
            f"({len(password)} given)."
        )
    if len(password) > policy.max_length:
        problems.append(f"Must be at most {policy.max_length} characters.")

    if password.lower().strip() in COMMON_PASSWORDS:
        problems.append("This password appears in every breach list. Choose another.")

    # Character classes are only demanded of shorter passwords.
    if len(password) < policy.passphrase_length:
        if policy.require_lower and not any(c.islower() for c in password):
            problems.append("Must contain a lower-case letter.")
        if policy.require_upper and not any(c.isupper() for c in password):
            problems.append("Must contain an upper-case letter.")
        if policy.require_digit and not any(c.isdigit() for c in password):
            problems.append("Must contain a digit.")
        if policy.require_symbol and not _SYMBOLS.search(password):
            problems.append("Must contain a symbol.")

    if policy.reject_email_local and email and "@" in email:
        local = email.split("@", 1)[0].lower()
        if len(local) >= 3 and local in password.lower():
            problems.append("Must not contain your email address.")

    return problems


def password_strength(password: str) -> float:
    """A 0-1 score for the strength meter.

    Deliberately crude and monotone in length; it exists to give the user
    feedback, not to gate anything. `validate_password` is the gate.
    """
    if not password:
        return 0.0
    classes = sum([
        any(c.islower() for c in password),
        any(c.isupper() for c in password),
        any(c.isdigit() for c in password),
        bool(_SYMBOLS.search(password)),
    ])
    length_score = min(1.0, len(password) / 20.0)
    class_score = classes / 4.0
    unique_score = min(1.0, len(set(password)) / 12.0)
    score = 0.55 * length_score + 0.25 * class_score + 0.20 * unique_score
    if password.lower().strip() in COMMON_PASSWORDS:
        score = min(score, 0.1)
    return round(min(1.0, score), 3)


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------
#: Pragmatic rather than RFC-5322-complete: rejects what is obviously not an
#: address, accepts everything a real user will type. Full RFC compliance
#: accepts strings no mail server will deliver to.
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")


def is_valid_email(email: str) -> bool:
    return bool(email) and len(email) <= 254 and bool(EMAIL_PATTERN.match(email))


def normalise_email(email: str) -> str:
    """Lower-cased and trimmed. The domain part of an address is
    case-insensitive by standard and the local part is case-sensitive in
    theory; in practice treating the whole thing case-insensitively is what
    every provider does and what users expect, and it prevents two accounts
    differing only in capitalisation."""
    return email.strip().lower()


#: Reserved handles. Claiming these lets someone impersonate the platform in
#: any context that shows a username — a support thread, an audit line, an
#: @mention — which is a social-engineering foothold rather than a cosmetic
#: problem.
RESERVED_USERNAMES = frozenset({
    "admin", "administrator", "root", "superuser", "super_admin", "superadmin",
    "system", "support", "help", "security", "billing", "api", "www", "mail",
    "noreply", "no-reply", "postmaster", "webmaster", "moderator", "staff",
    "official", "ierp", "equitypilot", "null", "undefined", "me", "self",
})

_USERNAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{1,62}[a-z0-9])$")


def normalise_username(username: str) -> str:
    """Trimmed and lower-cased, so capitalisation cannot fork an identity."""
    return (username or "").strip().lower()


def username_problems(username: str) -> list[str]:
    """Why this username is unacceptable. Empty list means it is fine.

    Returns every problem rather than the first, so the signup form can show
    the user everything to fix in one pass instead of one error per attempt.
    """
    handle = normalise_username(username)
    problems: list[str] = []
    if len(handle) < 3:
        problems.append("Username must be at least 3 characters.")
    if len(handle) > 64:
        problems.append("Username must be at most 64 characters.")
    if handle and not _USERNAME_RE.match(handle):
        problems.append(
            "Username may contain only letters, numbers, dots, hyphens and "
            "underscores, and must start and end with a letter or number."
        )
    if handle in RESERVED_USERNAMES:
        problems.append("That username is reserved.")
    if "@" in handle:
        # Otherwise a username could shadow someone else's email at login.
        problems.append("Username may not contain '@'.")
    return problems


def slugify(value: str, *, max_length: int = 48) -> str:
    """Tenant slug: lower-case, alphanumeric and hyphens, no leading or
    trailing hyphen. Used in URLs and subdomains, so the character set is
    conservative."""
    cleaned = re.sub(r"[^a-z0-9]+", "-", value.strip().lower())
    return cleaned.strip("-")[:max_length].strip("-")
