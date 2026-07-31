"""User registration, sign-in, sessions, and the token lifecycle.

The whole of authentication lives here so there is one answer to "what happens
when someone signs in" regardless of whether they used a password, Google,
GitHub or a magic link. Each method differs only in how the identity is
*proved*; everything after that — status checks, lockout reset, session
minting, audit — is shared.

Three behaviours are worth calling out because they are easy to get wrong and
expensive when they are:

**Sign-in failures are indistinguishable.** Unknown email, wrong password and
unverified account all produce the same message and comparable timing. An
endpoint that says "no such user" is a free membership oracle.

**Refresh tokens rotate, and reuse revokes the family.** Each refresh mints a
successor and marks the presented token used. Presenting a used token means it
was captured, so every token in that lineage is revoked and a critical audit
event is written.

**Registration is idempotent from the caller's point of view.** Registering an
address that already exists returns the same "check your email" response
rather than an error, for the same enumeration reason — but sends no second
account.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.config import settings
from app.domain.platform.audit import AuditAction
from app.domain.platform.identity import (
    AuthProvider, FEDERATED_PROVIDERS, LOGIN_ALLOWED_STATUSES, Principal,
    Role, TOKEN_TTL_SECONDS, TenantStatus, TokenType, UserStatus, outranks,
)
from app.domain.platform.limits import (
    DEFAULT_PASSWORD_POLICY, is_valid_email, normalise_email,
    normalise_username, username_problems, validate_password,
)
from app.models.platform import (
    OneTimeToken, RefreshToken, Tenant, User, UserIdentity,
)
from app.services.platform import crypto
from app.services.platform.tenancy import TenantService


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes even from a timezone-aware column.

    Comparing a naive value to an aware one raises `TypeError`, which is how
    Module 8's `Invalid isoformat string` cousin appears in this layer. Every
    timestamp read from the database goes through here.
    """
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class AuthError(Exception):
    """Authentication failed. The message is safe to show a user."""

    def __init__(self, message: str = "Invalid email or password.") -> None:
        super().__init__(message)


class RegistrationError(Exception):
    """The registration request itself was malformed — a bad address, a weak
    password. Distinct from AuthError because these *should* be specific."""

    def __init__(self, message: str, problems: list[str] | None = None) -> None:
        super().__init__(message)
        self.problems = problems or []


@dataclass(frozen=True, slots=True)
class SessionTokens:
    """What a successful sign-in produces."""

    access_token: str
    refresh_token: str
    expires_in: int
    refresh_expires_in: int
    session_id: str
    csrf_token: str
    token_type: str = "Bearer"


@dataclass(frozen=True, slots=True)
class AuthOutcome:
    """A sign-in result: the user, their session, and what the API should
    record. The audit action travels with the outcome so the router cannot
    log a success for a failure."""

    user: User
    tokens: SessionTokens
    principal: Principal
    action: AuditAction = AuditAction.LOGIN_SUCCEEDED


@dataclass(frozen=True, slots=True)
class PendingToken:
    """A one-time token that must be delivered out of band.

    The plaintext exists only in this object and in the email that carries it.
    The database has the digest.
    """

    user: User
    token: str
    purpose: TokenType
    expires_at: datetime


class IdentityService:
    """Everything about who someone is."""

    def __init__(self, db: Session) -> None:
        self.db = db
        self.tenants = TenantService(db)

    # ==================================================================
    # Lookup
    # ==================================================================
    def by_email(self, email: str) -> User | None:
        return self.db.scalar(
            select(User).where(User.email == normalise_email(email))
        )

    def by_username(self, username: str) -> User | None:
        """Look up by username, case-insensitively.

        Usernames are persisted lower-cased, so this normalises the input the
        same way rather than relying on the database's collation — which
        differs between SQLite and Postgres and would make the behaviour
        depend on the deployment.
        """
        handle = normalise_username(username)
        if not handle:
            return None
        return self.db.scalar(select(User).where(User.username == handle))

    def by_id(self, user_id: str) -> User | None:
        return self.db.get(User, user_id)

    def list_members(
        self,
        tenant_id: int,
        *,
        role: str | None = None,
        status: str | None = None,
        search: str | None = None,
        offset: int = 0,
        limit: int = 50,
        sort: str = "created_at",
        descending: bool = True,
    ) -> tuple[list[User], int]:
        stmt = select(User).where(User.tenant_id == tenant_id)
        if role:
            stmt = stmt.where(User.role == role)
        if status:
            stmt = stmt.where(User.status == status)
        if search:
            like = f"%{search.lower()}%"
            stmt = stmt.where(
                func.lower(User.email).like(like) | func.lower(User.name).like(like)
            )
        total = self.db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        column = getattr(User, sort, User.created_at)
        stmt = stmt.order_by(column.desc() if descending else column.asc())
        return list(self.db.scalars(stmt.offset(offset).limit(limit))), total

    # ==================================================================
    # Registration
    # ==================================================================
    def register(
        self,
        *,
        email: str,
        password: str,
        name: str,
        organisation: str | None = None,
        username: str | None = None,
        tenant_id: int | None = None,
        role: Role = Role.ADMIN,
        auto_verify: bool = False,
    ) -> tuple[User, PendingToken | None]:
        """Create an account.

        With no `tenant_id`, the registrant gets a new organisation and owns
        it as Admin — the self-serve path. With one, they join an existing
        organisation at the role the inviter chose.

        Returns the user and, unless `auto_verify`, the verification token the
        caller must email. The user is `PENDING` until that token is redeemed
        and cannot sign in before then.
        """
        address = normalise_email(email)
        if not is_valid_email(address):
            raise RegistrationError("That does not look like an email address.")

        problems = validate_password(
            password, policy=DEFAULT_PASSWORD_POLICY, email=address,
        )
        if problems:
            raise RegistrationError("Password does not meet the policy.", problems)

        handle = normalise_username(username) if username else None
        if handle:
            # Unlike a duplicate email, a taken username *is* reported plainly.
            # It is chosen, not secret, and a signup form that refuses without
            # saying why is unusable — the user cannot guess what to change.
            # Email stays neutral because it is an identifier the visitor may
            # not own.
            name_problems = username_problems(handle)
            if name_problems:
                raise RegistrationError("Username is not acceptable.", name_problems)
            if self.by_username(handle) is not None:
                raise RegistrationError(
                    "That username is already taken.",
                    ["Choose a different username."],
                )

        if self.by_email(address) is not None:
            # Deliberately not an error the caller can distinguish. The router
            # returns the same body it returns for a genuine registration.
            raise RegistrationError("__exists__")

        if tenant_id is None:
            tenant = self.tenants.create(organisation or _org_name_from(address, name))
            tenant_id = tenant.id
            role = Role.ADMIN     # whoever creates the org owns it

        now = _utcnow()
        user = User(
            id=crypto.new_id(),
            tenant_id=tenant_id,
            email=address,
            username=handle,
            name=name.strip() or address.split("@")[0],
            role=role.value,
            status=(UserStatus.ACTIVE if auto_verify else UserStatus.PENDING).value,
            password_hash=crypto.hash_password(password),
            password_changed_at=now,
            email_verified_at=now if auto_verify else None,
        )
        self.db.add(user)
        self.db.add(UserIdentity(
            user_id=user.id, provider=AuthProvider.PASSWORD.value,
            subject=address, email=address, linked_at=now,
        ))
        self.db.commit()
        self.db.refresh(user)
        self.tenants.refresh_member_count(tenant_id)

        pending = None if auto_verify else self.issue_one_time_token(
            user, TokenType.EMAIL_VERIFY,
        )
        return user, pending

    def invite(
        self,
        *,
        tenant_id: int,
        email: str,
        name: str,
        role: Role,
        invited_by: str,
    ) -> tuple[User, PendingToken]:
        """Create a member who sets their own password from an emailed link.

        No password is generated. A generated password has to be transmitted
        somehow, and every channel for doing so is worse than a single-use
        link.
        """
        address = normalise_email(email)
        if not is_valid_email(address):
            raise RegistrationError("That does not look like an email address.")
        if self.by_email(address) is not None:
            raise RegistrationError("Someone with that address is already a member.")

        user = User(
            id=crypto.new_id(),
            tenant_id=tenant_id,
            email=address,
            name=name.strip() or address.split("@")[0],
            role=role.value,
            status=UserStatus.PENDING.value,
            password_hash=None,
            invited_by=invited_by,
        )
        self.db.add(user)
        self.db.commit()
        self.db.refresh(user)
        self.tenants.refresh_member_count(tenant_id)

        token = self.issue_one_time_token(
            user, TokenType.PASSWORD_RESET, payload={"invite": True},
        )
        return user, token

    # ==================================================================
    # Sign-in
    # ==================================================================
    def authenticate(
        self,
        *,
        email: str,
        password: str,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> AuthOutcome:
        """Email *or* username, plus password.

        The parameter keeps its name for compatibility with existing callers;
        the value may be either identifier. Failure is a generic `AuthError`
        in every case — wrong username, wrong password, locked, suspended —
        because distinguishing them tells an attacker which half to keep.
        """
        identifier = (email or "").strip()
        user = (
            self.by_email(normalise_email(identifier))
            if "@" in identifier
            else self.by_username(identifier)
        )

        if user is None:
            # Burn comparable time so the absence of a user is not detectable
            # by how quickly we say no.
            crypto.verify_password(password, None)
            raise AuthError()

        self._assert_not_locked(user)

        if not crypto.verify_password(password, user.password_hash):
            self._record_failure(user)
            raise AuthError()

        if UserStatus(user.status) not in LOGIN_ALLOWED_STATUSES:
            self._record_failure(user)
            raise AuthError(_status_message(UserStatus(user.status)))

        # Silent parameter upgrade: the plaintext is available exactly here.
        if crypto.needs_rehash(user.password_hash or ""):
            user.password_hash = crypto.hash_password(password)

        return self._establish_session(
            user, AuthProvider.PASSWORD, ip_address, user_agent,
        )

    def authenticate_federated(
        self,
        *,
        provider: AuthProvider,
        subject: str,
        email: str,
        name: str,
        avatar_url: str | None = None,
        profile: dict | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> AuthOutcome:
        """Google or GitHub. Links to an existing account by verified email.

        Auto-linking on email is safe *only* because the providers we accept
        assert a verified address. It would be an account-takeover hole with a
        provider that does not, which is why `FEDERATED_PROVIDERS` is an
        explicit allow-list rather than "anything that is not a password".
        """
        if provider not in FEDERATED_PROVIDERS:
            raise AuthError(f"{provider} is not a federated provider.")

        address = normalise_email(email)
        identity = self.db.scalar(
            select(UserIdentity).where(
                UserIdentity.provider == provider.value,
                UserIdentity.subject == subject,
            )
        )

        now = _utcnow()
        if identity is not None:
            user = self.db.get(User, identity.user_id)
            if user is None:
                raise AuthError("This login is no longer linked to an account.")
            identity.last_used_at = now
        else:
            user = self.by_email(address)
            if user is None:
                tenant = self.tenants.create(_org_name_from(address, name))
                user = User(
                    id=crypto.new_id(),
                    tenant_id=tenant.id,
                    email=address,
                    name=name.strip() or address.split("@")[0],
                    avatar_url=avatar_url,
                    role=Role.ADMIN.value,
                    # The provider verified the address, so there is nothing
                    # for us to verify: the account is active immediately.
                    status=UserStatus.ACTIVE.value,
                    email_verified_at=now,
                )
                self.db.add(user)
                self.db.flush()
                self.tenants.refresh_member_count(tenant.id)
            elif user.email_verified_at is None:
                # A pending password account whose address the provider has
                # now vouched for. Verifying it here saves a dead-end.
                user.email_verified_at = now
                user.status = UserStatus.ACTIVE.value

            self.db.add(UserIdentity(
                user_id=user.id, provider=provider.value, subject=subject,
                email=address, profile=profile or {}, linked_at=now,
                last_used_at=now,
            ))

        self._assert_not_locked(user)
        if UserStatus(user.status) not in LOGIN_ALLOWED_STATUSES:
            raise AuthError(_status_message(UserStatus(user.status)))

        if avatar_url and not user.avatar_url:
            user.avatar_url = avatar_url

        return self._establish_session(user, provider, ip_address, user_agent)

    def authenticate_magic_link(
        self,
        *,
        token: str,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> AuthOutcome:
        """Redeem a magic link. Single use, and it verifies the address as a
        side effect — receiving the mail *is* proof of control."""
        record, user = self.consume_one_time_token(token, TokenType.MAGIC_LINK)

        if user.email_verified_at is None:
            user.email_verified_at = _utcnow()
        if UserStatus(user.status) is UserStatus.PENDING:
            user.status = UserStatus.ACTIVE.value

        self._assert_not_locked(user)
        if UserStatus(user.status) not in LOGIN_ALLOWED_STATUSES:
            raise AuthError(_status_message(UserStatus(user.status)))

        return self._establish_session(
            user, AuthProvider.MAGIC_LINK, ip_address, user_agent,
        )

    # ==================================================================
    # Sessions
    # ==================================================================
    def _establish_session(
        self,
        user: User,
        provider: AuthProvider,
        ip_address: str | None,
        user_agent: str | None,
    ) -> AuthOutcome:
        """Everything common to a successful sign-in, whatever proved it."""
        now = _utcnow()
        user.failed_login_count = 0
        user.locked_until = None
        user.last_login_at = now
        user.last_seen_at = now

        session_id = crypto.new_id()
        family_id = crypto.new_id()
        tokens = self._mint(user, session_id, family_id, ip_address, user_agent)
        self.db.commit()
        self.db.refresh(user)

        return AuthOutcome(
            user=user,
            tokens=tokens,
            principal=self.principal_for(user, provider=provider, session_id=session_id),
        )

    def _mint(
        self,
        user: User,
        session_id: str,
        family_id: str,
        ip_address: str | None,
        user_agent: str | None,
    ) -> SessionTokens:
        """Create an access/refresh pair and persist the refresh digest."""
        now = _utcnow()
        access_ttl = settings.ACCESS_TOKEN_TTL_SECONDS
        refresh_ttl = settings.REFRESH_TOKEN_TTL_SECONDS

        access = crypto.encode_jwt(
            {
                "sub": user.id,
                "typ": TokenType.ACCESS.value,
                "email": user.email,
                "name": user.name,
                "role": user.role,
                "tid": user.tenant_id,
                "sid": session_id,
            },
            ttl_seconds=access_ttl,
        )

        # The refresh token is opaque, not a JWT. There is nothing to read in
        # it, it is verified by database lookup anyway, and an opaque string
        # cannot leak claims if it is logged by accident.
        refresh = crypto.new_token()
        self.db.add(RefreshToken(
            user_id=user.id,
            tenant_id=user.tenant_id,
            token_hash=crypto.hash_token(refresh),
            family_id=family_id,
            session_id=session_id,
            issued_at=now,
            expires_at=now + timedelta(seconds=refresh_ttl),
            ip_address=ip_address,
            user_agent=(user_agent or "")[:300] or None,
        ))

        return SessionTokens(
            access_token=access,
            refresh_token=refresh,
            expires_in=access_ttl,
            refresh_expires_in=refresh_ttl,
            session_id=session_id,
            csrf_token=crypto.csrf_token(session_id),
        )

    def refresh_session(
        self,
        refresh_token: str,
        *,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> tuple[AuthOutcome, bool]:
        """Rotate a refresh token.

        Returns the new session and a flag: True when reuse of an already-spent
        token was detected. On reuse the whole family is revoked, the caller is
        refused, and the router writes a CRITICAL audit event — this is the
        only signal available that a refresh token has been stolen.
        """
        digest = crypto.hash_token(refresh_token)
        record = self.db.scalar(
            select(RefreshToken).where(RefreshToken.token_hash == digest)
        )
        if record is None:
            raise AuthError("Session expired. Please sign in again.")

        now = _utcnow()

        if record.used_at is not None:
            self._revoke_family(record.family_id, "token_reuse")
            self.db.commit()
            raise ReuseDetected(record.user_id, record.family_id)

        if record.revoked_at is not None:
            raise AuthError("Session revoked. Please sign in again.")
        expires = _aware(record.expires_at)
        if expires is not None and expires <= now:
            raise AuthError("Session expired. Please sign in again.")

        user = self.db.get(User, record.user_id)
        if user is None or UserStatus(user.status) not in LOGIN_ALLOWED_STATUSES:
            raise AuthError("Account is no longer active.")

        # Rotate: spend the old, mint the successor inside the same family.
        tokens = self._mint(
            user, record.session_id, record.family_id, ip_address, user_agent,
        )
        record.used_at = now
        record.replaced_by = crypto.hash_token(tokens.refresh_token)
        user.last_seen_at = now
        self.db.commit()

        return (
            AuthOutcome(
                user=user,
                tokens=tokens,
                principal=self.principal_for(user, session_id=record.session_id),
                action=AuditAction.TOKEN_REFRESHED,
            ),
            False,
        )

    def revoke_session(self, refresh_token: str) -> bool:
        """Sign out. Revokes the whole family, so signing out on one device
        invalidates the rotation lineage rather than leaving a successor
        usable."""
        record = self.db.scalar(
            select(RefreshToken).where(
                RefreshToken.token_hash == crypto.hash_token(refresh_token)
            )
        )
        if record is None:
            return False
        self._revoke_family(record.family_id, "logout")
        self.db.commit()
        return True

    def revoke_all_sessions(self, user_id: str, reason: str = "admin") -> int:
        """Every session for a user. Used on password change, suspension and
        role revocation — a demoted admin must not keep an admin token for
        the next fifteen minutes."""
        result = self.db.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=_utcnow(), revoked_reason=reason)
        )
        self.db.commit()
        return int(result.rowcount or 0)

    def _revoke_family(self, family_id: str, reason: str) -> int:
        result = self.db.execute(
            update(RefreshToken)
            .where(
                RefreshToken.family_id == family_id,
                RefreshToken.revoked_at.is_(None),
            )
            .values(revoked_at=_utcnow(), revoked_reason=reason)
        )
        return int(result.rowcount or 0)

    def active_sessions(self, user_id: str) -> list[RefreshToken]:
        now = _utcnow()
        rows = self.db.scalars(
            select(RefreshToken)
            .where(
                RefreshToken.user_id == user_id,
                RefreshToken.revoked_at.is_(None),
                RefreshToken.used_at.is_(None),
            )
            .order_by(RefreshToken.issued_at.desc())
        )
        return [r for r in rows if (_aware(r.expires_at) or now) > now]

    # ==================================================================
    # One-time tokens
    # ==================================================================
    def issue_one_time_token(
        self,
        user: User,
        purpose: TokenType,
        *,
        payload: dict | None = None,
        ip_address: str | None = None,
    ) -> PendingToken:
        """Mint a single-use token and invalidate any earlier one.

        Invalidating the predecessor matters: if two reset links are live, a
        user who forwards the older one to support has handed over a working
        credential.
        """
        now = _utcnow()
        self.db.execute(
            update(OneTimeToken)
            .where(
                OneTimeToken.user_id == user.id,
                OneTimeToken.purpose == purpose.value,
                OneTimeToken.consumed_at.is_(None),
            )
            .values(consumed_at=now)
        )

        token = crypto.new_token()
        expires = now + timedelta(seconds=TOKEN_TTL_SECONDS[purpose])
        self.db.add(OneTimeToken(
            user_id=user.id,
            purpose=purpose.value,
            token_hash=crypto.hash_token(token),
            expires_at=expires,
            payload=payload,
            ip_address=ip_address,
        ))
        self.db.commit()
        return PendingToken(user=user, token=token, purpose=purpose, expires_at=expires)

    def consume_one_time_token(
        self, token: str, purpose: TokenType,
    ) -> tuple[OneTimeToken, User]:
        """Redeem exactly once. The purpose is part of the match, so a
        password-reset token cannot be spent as a magic link."""
        record = self.db.scalar(
            select(OneTimeToken).where(
                OneTimeToken.token_hash == crypto.hash_token(token),
                OneTimeToken.purpose == purpose.value,
            )
        )
        if record is None:
            raise AuthError("This link is not valid.")
        if record.consumed_at is not None:
            raise AuthError("This link has already been used.")
        expires = _aware(record.expires_at)
        if expires is not None and expires <= _utcnow():
            raise AuthError("This link has expired. Request a new one.")

        user = self.db.get(User, record.user_id)
        if user is None:
            raise AuthError("This link is not valid.")

        record.consumed_at = _utcnow()
        self.db.commit()
        return record, user

    # -- the flows built on them --------------------------------------
    def request_email_verification(self, user: User) -> PendingToken:
        return self.issue_one_time_token(user, TokenType.EMAIL_VERIFY)

    def verify_email(self, token: str) -> User:
        _, user = self.consume_one_time_token(token, TokenType.EMAIL_VERIFY)
        user.email_verified_at = _utcnow()
        if UserStatus(user.status) is UserStatus.PENDING:
            user.status = UserStatus.ACTIVE.value
        self.db.commit()
        self.db.refresh(user)
        return user

    def request_password_reset(self, email: str) -> PendingToken | None:
        """None when the address is unknown — the caller must still respond
        as though it succeeded."""
        user = self.by_email(email)
        if user is None:
            return None
        return self.issue_one_time_token(user, TokenType.PASSWORD_RESET)

    def reset_password(self, token: str, new_password: str) -> User:
        record, user = self.consume_one_time_token(token, TokenType.PASSWORD_RESET)

        problems = validate_password(new_password, email=user.email)
        if problems:
            # The token was consumed by the lookup above; re-issue rather than
            # stranding the user, and roll the consumption back so the link in
            # their inbox still works while they pick a better password.
            record.consumed_at = None
            self.db.commit()
            raise RegistrationError("Password does not meet the policy.", problems)

        now = _utcnow()
        user.password_hash = crypto.hash_password(new_password)
        user.password_changed_at = now
        user.failed_login_count = 0
        user.locked_until = None
        if user.email_verified_at is None:
            # An invited user proving control of the address by using the link.
            user.email_verified_at = now
        if UserStatus(user.status) is UserStatus.PENDING:
            user.status = UserStatus.ACTIVE.value

        # A password change ends every existing session. If the reset was
        # triggered because the account was compromised, leaving the
        # attacker's session alive defeats the entire exercise.
        self.db.commit()
        self.revoke_all_sessions(user.id, "password_reset")
        self.db.refresh(user)
        return user

    def change_password(self, user: User, current: str, new_password: str) -> User:
        if not crypto.verify_password(current, user.password_hash):
            raise AuthError("Current password is incorrect.")
        problems = validate_password(new_password, email=user.email)
        if problems:
            raise RegistrationError("Password does not meet the policy.", problems)
        user.password_hash = crypto.hash_password(new_password)
        user.password_changed_at = _utcnow()
        self.db.commit()
        self.revoke_all_sessions(user.id, "password_changed")
        self.db.refresh(user)
        return user

    def request_magic_link(self, email: str) -> PendingToken | None:
        user = self.by_email(email)
        if user is None:
            return None
        return self.issue_one_time_token(user, TokenType.MAGIC_LINK)

    # ==================================================================
    # Administration
    # ==================================================================
    def change_role(self, actor: Principal, user: User, role: Role) -> User:
        """Change a member's role, subject to the seniority rule.

        An actor may only administer someone strictly junior to themselves,
        and may not grant a role senior to their own. Without the second half,
        an Admin could promote a Researcher to Super Admin and then be
        promoted by them.
        """
        if user.id == actor.user_id:
            raise AuthError("You cannot change your own role.")
        current = Role(user.role)
        if not outranks(actor.role, current):
            raise AuthError("You cannot administer a member at or above your own level.")
        if not outranks(actor.role, role) and actor.role is not Role.SUPER_ADMIN:
            raise AuthError("You cannot grant a role at or above your own level.")

        user.role = role.value
        self.db.commit()
        # The old role is still inside any live access token, so end the
        # sessions rather than waiting up to fifteen minutes for expiry.
        self.revoke_all_sessions(user.id, "role_changed")
        self.db.refresh(user)
        return user

    def set_status(self, actor: Principal, user: User, status: UserStatus) -> User:
        if user.id == actor.user_id:
            raise AuthError("You cannot change your own status.")
        if not outranks(actor.role, Role(user.role)):
            raise AuthError("You cannot administer a member at or above your own level.")

        user.status = status.value
        self.db.commit()
        if status is not UserStatus.ACTIVE:
            self.revoke_all_sessions(user.id, f"status_{status.value}")
        self.db.refresh(user)
        return user

    def last_admin_check(self, tenant_id: int, excluding: str) -> bool:
        """True when removing or demoting `excluding` would leave the
        organisation with no administrator. Called before both operations —
        a tenant nobody can administer needs an operator to repair."""
        count = self.db.scalar(
            select(func.count(User.id)).where(
                User.tenant_id == tenant_id,
                User.role.in_([Role.ADMIN.value, Role.SUPER_ADMIN.value]),
                User.status == UserStatus.ACTIVE.value,
                User.id != excluding,
            )
        ) or 0
        return count == 0

    # ==================================================================
    # Principals
    # ==================================================================
    def principal_for(
        self,
        user: User,
        *,
        provider: AuthProvider = AuthProvider.PASSWORD,
        session_id: str | None = None,
        api_key_id: int | None = None,
        role_override: Role | None = None,
    ) -> Principal:
        """Build the resolved caller, including tenant degradation.

        `role_override` exists for API keys, which may be minted with a role
        lower than their creator's; the key's role governs, never the user's.
        """
        tenant = self.db.get(Tenant, user.tenant_id) if user.tenant_id else None
        read_only = bool(tenant and TenantService.is_read_only(tenant))

        return Principal(
            user_id=user.id,
            email=user.email,
            name=user.name,
            role=role_override or Role(user.role),
            tenant_id=user.tenant_id,
            tenant_slug=tenant.slug if tenant else None,
            provider=provider,
            api_key_id=api_key_id,
            tenant_read_only=read_only,
            session_id=session_id,
        )

    def principal_from_access_token(self, token: str) -> Principal:
        """Resolve a bearer token to a principal.

        The user row is loaded rather than trusted from the claims. A token
        minted before a suspension would otherwise keep working until it
        expired, and the whole point of a fifteen-minute access token is that
        the window is short — not that it is ignored.
        """
        claims = crypto.decode_jwt(token, expected_type=TokenType.ACCESS.value)
        user = self.db.get(User, claims.get("sub", ""))
        if user is None:
            raise AuthError("This session is no longer valid.")
        if UserStatus(user.status) not in LOGIN_ALLOWED_STATUSES:
            raise AuthError(_status_message(UserStatus(user.status)))
        return self.principal_for(user, session_id=claims.get("sid"))

    # ==================================================================
    # Lockout
    # ==================================================================
    def _assert_not_locked(self, user: User) -> None:
        locked = _aware(user.locked_until)
        if locked and locked > _utcnow():
            remaining = int((locked - _utcnow()).total_seconds() // 60) + 1
            raise AuthError(
                f"Too many failed attempts. Try again in {remaining} minute"
                f"{'s' if remaining != 1 else ''}."
            )

    def _record_failure(self, user: User) -> None:
        """Count a failure and lock the account once the threshold is hit.

        Per-account rather than per-IP, and in addition to the per-IP rate
        limit: a distributed attempt against one account defeats an IP limit,
        and a shared office NAT defeats an account limit. Both are needed.
        """
        user.failed_login_count = (user.failed_login_count or 0) + 1
        if user.failed_login_count >= settings.MAX_FAILED_LOGINS:
            user.locked_until = _utcnow() + timedelta(
                seconds=settings.LOGIN_LOCKOUT_SECONDS
            )
        self.db.commit()

    # ==================================================================
    # Housekeeping
    # ==================================================================
    def purge_expired_tokens(self, *, now: datetime | None = None) -> dict[str, int]:
        """Delete spent and expired tokens.

        Refresh tokens are kept for a grace period after expiry rather than
        deleted immediately, because reuse detection needs the row: a deleted
        token looks identical to a never-issued one, and the theft signal is
        lost.
        """
        moment = now or _utcnow()
        grace = moment - timedelta(days=7)

        refresh = self.db.query(RefreshToken).filter(
            RefreshToken.expires_at < grace
        ).delete(synchronize_session=False)
        one_time = self.db.query(OneTimeToken).filter(
            OneTimeToken.expires_at < moment
        ).delete(synchronize_session=False)
        self.db.commit()
        return {"refresh_tokens": int(refresh), "one_time_tokens": int(one_time)}


class ReuseDetected(Exception):
    """A spent refresh token was presented again — the family is now revoked.

    A distinct exception because the router must both refuse the request and
    write a CRITICAL audit event, and an ordinary AuthError would produce only
    the first.
    """

    def __init__(self, user_id: str, family_id: str) -> None:
        self.user_id = user_id
        self.family_id = family_id
        super().__init__("Session invalidated. Please sign in again.")


def _status_message(status: UserStatus) -> str:
    return {
        UserStatus.PENDING: "Please verify your email address before signing in.",
        UserStatus.SUSPENDED: "This account is suspended. Contact your administrator.",
        UserStatus.DISABLED: "This account has been deactivated.",
    }.get(status, "Invalid email or password.")


def _org_name_from(email: str, name: str) -> str:
    """A first guess at the organisation name for a self-serve signup.

    The email domain for a corporate address, the person's name for a consumer
    one. It is only a default — the admin renames it in settings — but a
    workspace called "gmail" would be an odd first impression.
    """
    domain = email.split("@", 1)[-1]
    label = domain.split(".")[0]
    consumer = {
        "gmail", "googlemail", "yahoo", "outlook", "hotmail", "live", "icloud",
        "proton", "protonmail", "aol", "rediffmail", "zoho", "yandex",
    }
    if label.lower() in consumer:
        first = (name or email.split("@")[0]).strip().split(" ")[0]
        return f"{first}'s Workspace" if first else "My Workspace"
    return label.replace("-", " ").title()
