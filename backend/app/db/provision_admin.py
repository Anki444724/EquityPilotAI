"""Provision the real administrator and retire the seeded demo accounts.

Run as a module so it works identically on a laptop and on Railway:

    python -m app.db.provision_admin

The password is read from `ADMIN_PASSWORD` in the environment. It is never a
default, never a literal in this file, and never written anywhere in clear:
only the Argon2id hash reaches the database, produced by the same
`crypto.hash_password` the login path verifies against, so there is no second
implementation to drift.

The seeded `@democapital.in` accounts are suspended rather than deleted. They
own audit rows, API keys and portfolios; deleting them would either cascade
that history away or fail on a foreign key. Suspension makes them unable to
authenticate while leaving the record intact, which is what an auditor needs.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

from sqlalchemy import select

from app.core import crypto
from app.db.base import SessionLocal
from app.domain.platform.identity import Role, UserStatus
from app.domain.platform.limits import (
    DEFAULT_PASSWORD_POLICY, normalise_email, normalise_username,
    username_problems, validate_password,
)
from app.models.platform import Tenant, User

#: Seeded demonstration accounts. Disabled, never deleted.
DEMO_EMAIL_DOMAIN = "@democapital.in"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def provision(
    *,
    email: str,
    username: str,
    name: str,
    password: str,
    disable_demo: bool = True,
) -> dict[str, object]:
    """Create or update the administrator. Idempotent."""
    address = normalise_email(email)
    handle = normalise_username(username)

    problems = username_problems(handle)
    if problems:
        raise SystemExit(f"username rejected: {'; '.join(problems)}")

    # The administrator is held to the same policy as everyone else. An admin
    # account is the one most worth attacking, so exempting it would be
    # precisely backwards.
    weak = validate_password(password, policy=DEFAULT_PASSWORD_POLICY, email=address)
    if weak:
        raise SystemExit(
            "password rejected by the policy:\n  - " + "\n  - ".join(weak)
        )

    db = SessionLocal()
    try:
        tenant = db.scalar(select(Tenant).order_by(Tenant.id))
        if tenant is None:
            raise SystemExit("no tenant exists; seed the platform first")

        existing = db.scalar(select(User).where(User.email == address))
        clash = db.scalar(select(User).where(User.username == handle))
        if clash is not None and (existing is None or clash.id != existing.id):
            raise SystemExit(f"username '{handle}' is already taken by another account")

        now = _utcnow()
        created = existing is None
        user = existing or User(id=crypto.new_id(), tenant_id=tenant.id)

        user.email = address
        user.username = handle
        user.name = name
        user.role = Role.SUPER_ADMIN.value
        user.status = UserStatus.ACTIVE.value
        # Hashed with Argon2id by the shared helper. The plaintext exists only
        # as a local variable for the lifetime of this call.
        user.password_hash = crypto.hash_password(password)
        user.password_changed_at = now
        user.email_verified_at = now
        user.failed_login_count = 0
        user.locked_until = None
        if created:
            db.add(user)

        disabled: list[str] = []
        if disable_demo:
            for demo in db.scalars(
                select(User).where(User.email.like(f"%{DEMO_EMAIL_DOMAIN}"))
            ).all():
                if demo.id == user.id:
                    continue
                demo.status = UserStatus.SUSPENDED.value
                # Blank the hash as well as the status: a suspended account
                # whose credentials still verify is one configuration mistake
                # away from being usable again.
                demo.password_hash = None
                disabled.append(demo.email)

        db.commit()
        return {
            "action": "created" if created else "updated",
            "id": user.id,
            "email": user.email,
            "username": user.username,
            "role": user.role,
            "status": user.status,
            "demo_accounts_disabled": disabled,
        }
    finally:
        db.close()


def main() -> None:  # pragma: no cover - operator entry point
    password = os.environ.get("ADMIN_PASSWORD", "")
    if not password:
        print(
            "ADMIN_PASSWORD is not set. Refusing to invent a default: an "
            "administrator with a guessable password is worse than none.",
            file=sys.stderr,
        )
        raise SystemExit(2)

    result = provision(
        email=os.environ.get("ADMIN_EMAIL", "ankitsingh835141@gmail.com"),
        username=os.environ.get("ADMIN_USERNAME", "ankitsingh"),
        name=os.environ.get("ADMIN_NAME", "Ankit Singh"),
        password=password,
        disable_demo=os.environ.get("ADMIN_DISABLE_DEMO", "true").lower() != "false",
    )
    # The password is deliberately absent from this output.
    for key, value in result.items():
        print(f"  {key}: {value}")


if __name__ == "__main__":  # pragma: no cover
    main()
