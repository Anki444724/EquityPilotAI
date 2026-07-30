"""Programmatic API keys.

A key is a tenant-scoped, role-bounded credential that authenticates without a
browser. Three rules make it safe.

**The plaintext exists once.** `create()` returns it; the database holds only
a SHA-256 digest. There is no "show key" endpoint, because there is nothing to
show.

**A key can never exceed its creator.** A Researcher cannot mint an Admin key
and thereby promote themselves. Checked at creation, not at use, so the
invariant holds even if the creator is later demoted — and the key is revoked
when that happens.

**Verification is one indexed lookup.** The public key id is embedded in the
plaintext, so we fetch one row and compare one digest in constant time, rather
than hashing against every key in the table.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.domain.platform.identity import Principal, Role, seniority
from app.models.platform import ApiKey, Tenant, User
from app.services.platform import crypto


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class ApiKeyError(Exception):
    """A key could not be created or used."""


@dataclass(frozen=True, slots=True)
class IssuedKey:
    """The one and only time the plaintext is available."""

    record: ApiKey
    plaintext: str


class ApiKeyService:
    def __init__(self, db: Session) -> None:
        self.db = db

    # ==================================================================
    # Lifecycle
    # ==================================================================
    def create(
        self,
        *,
        principal: Principal,
        name: str,
        role: Role | None = None,
        expires_in_days: int | None = 365,
        scopes: list[str] | None = None,
        live: bool = True,
    ) -> IssuedKey:
        """Mint a key for the principal's tenant.

        Defaults to a *read-only* key rather than to the creator's own role.
        The safe default matters more than the convenient one: most keys feed
        a dashboard or a script that only reads, and a key that can delete a
        portfolio because nobody chose a role is a bad accident.
        """
        if principal.tenant_id is None:
            raise ApiKeyError("An API key must belong to an organisation.")

        target = role or Role.READ_ONLY
        if seniority(target) < seniority(principal.role):
            raise ApiKeyError(
                "An API key cannot be more privileged than the person creating it."
            )

        # Expiry is capped rather than optional. A never-expiring credential
        # in a CI system outlives the employee who created it.
        days = min(expires_in_days or 365, 365 * 2)
        generated = crypto.generate_api_key(live=live)

        record = ApiKey(
            tenant_id=principal.tenant_id,
            created_by=principal.user_id,
            name=name.strip()[:120] or "API key",
            key_id=generated.key_id,
            key_hash=generated.key_hash,
            prefix=generated.prefix,
            role=target.value,
            scopes=scopes or None,
            expires_at=_utcnow() + timedelta(days=days),
        )
        self.db.add(record)
        self.db.commit()
        self.db.refresh(record)
        return IssuedKey(record=record, plaintext=generated.plaintext)

    def list(self, tenant_id: int, *, include_revoked: bool = False) -> list[ApiKey]:
        stmt = select(ApiKey).where(ApiKey.tenant_id == tenant_id)
        if not include_revoked:
            stmt = stmt.where(ApiKey.revoked_at.is_(None))
        return list(self.db.scalars(stmt.order_by(ApiKey.created_at.desc())))

    def get(self, tenant_id: int, key_pk: int) -> ApiKey | None:
        return self.db.scalar(
            select(ApiKey).where(ApiKey.id == key_pk, ApiKey.tenant_id == tenant_id)
        )

    def revoke(self, tenant_id: int, key_pk: int) -> ApiKey:
        record = self.get(tenant_id, key_pk)
        if record is None:
            raise ApiKeyError("No such API key.")
        if record.revoked_at is None:
            record.revoked_at = _utcnow()
            self.db.commit()
            self.db.refresh(record)
        return record

    def revoke_for_user(self, user_id: str, reason: str = "user_change") -> int:
        """Revoke every key a person created.

        Called when they are demoted, suspended or removed. Without it, a
        departing analyst's key keeps their old privileges indefinitely — the
        single most common way an offboarding is incomplete.
        """
        result = self.db.execute(
            update(ApiKey)
            .where(ApiKey.created_by == user_id, ApiKey.revoked_at.is_(None))
            .values(revoked_at=_utcnow())
        )
        self.db.commit()
        return int(result.rowcount or 0)

    # ==================================================================
    # Verification
    # ==================================================================
    def authenticate(self, plaintext: str, *, ip_address: str | None = None) -> Principal:
        """Resolve a presented key to a principal, or raise.

        Every failure produces the same message. A response that distinguishes
        "unknown key" from "revoked key" tells an attacker which of their
        guesses was once real.
        """
        key_id = crypto.parse_api_key(plaintext)
        if key_id is None:
            raise ApiKeyError("Invalid API key.")

        record = self.db.scalar(select(ApiKey).where(ApiKey.key_id == key_id))
        if record is None:
            raise ApiKeyError("Invalid API key.")

        if not crypto.tokens_equal(record.key_hash, crypto.hash_token(plaintext)):
            raise ApiKeyError("Invalid API key.")
        if record.revoked_at is not None:
            raise ApiKeyError("Invalid API key.")
        expires = _aware(record.expires_at)
        if expires is not None and expires <= _utcnow():
            raise ApiKeyError("Invalid API key.")

        tenant = self.db.get(Tenant, record.tenant_id)
        if tenant is None or tenant.status in ("suspended", "cancelled"):
            raise ApiKeyError("Invalid API key.")

        creator = self.db.get(User, record.created_by)
        if creator is not None and creator.status not in ("active",):
            # The key outlived its owner's account. Revoke it now rather than
            # letting a suspended person's automation keep running.
            record.revoked_at = _utcnow()
            self.db.commit()
            raise ApiKeyError("Invalid API key.")

        self._touch(record, ip_address)

        return Principal(
            user_id=record.created_by,
            email=creator.email if creator else f"apikey-{record.key_id}",
            name=f"{record.name} (API key)",
            role=Role(record.role),
            tenant_id=record.tenant_id,
            tenant_slug=tenant.slug,
            provider="api_key",  # type: ignore[arg-type]  — StrEnum accepts the value
            api_key_id=record.id,
            tenant_read_only=tenant.status == "past_due",
            session_id=f"apikey:{record.key_id}",
        )

    def _touch(self, record: ApiKey, ip_address: str | None) -> None:
        """Record use.

        Written at most once a minute. A key driving a polling dashboard would
        otherwise turn every read into a write, and "last used" to the minute
        is as precise as anyone needs.
        """
        now = _utcnow()
        last = _aware(record.last_used_at)
        record.call_count = (record.call_count or 0) + 1
        if last is None or (now - last).total_seconds() >= 60:
            record.last_used_at = now
            record.last_used_ip = ip_address
            self.db.commit()

    # ==================================================================
    # Reporting
    # ==================================================================
    def statistics(self, tenant_id: int | None = None) -> dict[str, int]:
        stmt = select(
            func.count(ApiKey.id),
            func.sum(func.coalesce(ApiKey.call_count, 0)),
        )
        if tenant_id is not None:
            stmt = stmt.where(ApiKey.tenant_id == tenant_id)
        total, calls = self.db.execute(stmt).one()

        active_stmt = select(func.count(ApiKey.id)).where(ApiKey.revoked_at.is_(None))
        if tenant_id is not None:
            active_stmt = active_stmt.where(ApiKey.tenant_id == tenant_id)

        return {
            "total": int(total or 0),
            "active": int(self.db.scalar(active_stmt) or 0),
            "calls": int(calls or 0),
        }

    def expire_stale(self) -> int:
        """Revoke keys past their expiry date.

        They already fail authentication; this makes the state visible in the
        admin panel rather than leaving a list of keys that look live and are
        not.
        """
        result = self.db.execute(
            update(ApiKey)
            .where(
                ApiKey.revoked_at.is_(None),
                ApiKey.expires_at.isnot(None),
                ApiKey.expires_at <= _utcnow(),
            )
            .values(revoked_at=_utcnow())
        )
        self.db.commit()
        return int(result.rowcount or 0)
