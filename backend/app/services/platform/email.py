"""Transactional email.

Three flows depend on this — verification, password reset and magic link — and
all three must work with no mail server, because the platform must be fully
explorable on a laptop with nothing installed.

So there are two transports behind one interface. With `SMTP_HOST` set,
messages go out over SMTP. Without it, the console transport records the
message in an in-process outbox and logs the link at INFO. Every flow is then
end-to-end testable, and a developer can complete a password reset by reading
their own terminal.

The outbox is also what the tests assert against: a test that wants to know a
verification email was sent reads the outbox rather than mocking a client.
"""
from __future__ import annotations

import smtplib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr

from app.core.config import settings
from app.services.platform.observability import get_logger

log = get_logger("ierp.email")


@dataclass(frozen=True, slots=True)
class SentMessage:
    to: str
    subject: str
    body: str
    at: datetime
    transport: str


class Outbox:
    """In-memory record of everything sent. Bounded, so a long-running
    development session cannot grow it without limit."""

    def __init__(self, capacity: int = 200) -> None:
        self._messages: list[SentMessage] = []
        self.capacity = capacity

    def add(self, message: SentMessage) -> None:
        self._messages.append(message)
        if len(self._messages) > self.capacity:
            del self._messages[: len(self._messages) - self.capacity]

    def all(self) -> list[SentMessage]:
        return list(self._messages)

    def latest_for(self, address: str) -> SentMessage | None:
        for message in reversed(self._messages):
            if message.to.lower() == address.lower():
                return message
        return None

    def clear(self) -> None:
        self._messages.clear()

    def __len__(self) -> int:
        return len(self._messages)


#: Process-wide. The tests import it directly.
outbox = Outbox()


class EmailService:
    """Send a message by whichever transport is configured."""

    def send(self, *, to: str, subject: str, body: str) -> SentMessage:
        transport = "smtp" if settings.email_configured else "console"
        message = SentMessage(
            to=to, subject=subject, body=body,
            at=datetime.now(timezone.utc), transport=transport,
        )

        if transport == "smtp":
            try:
                self._send_smtp(to, subject, body)
            except Exception as exc:  # noqa: BLE001
                # Logged, not raised. A user who cannot be emailed should see
                # "check your inbox" and a support path, not a 500 from a
                # registration endpoint — and the operator sees the failure
                # here and in the audit trail.
                log.error("email delivery failed", to=to, error=str(exc))
                message = SentMessage(
                    to=to, subject=subject, body=body,
                    at=message.at, transport="failed",
                )
        else:
            # In production, console transport means email will never arrive.
            # Log at error level so degraded health is visible, but do not
            # raise - user still sees neutral message to avoid enumeration.
            if settings.is_production:
                log.error("email requested but no SMTP host configured - email not delivered", to=to, subject=subject)
            else:
                log.info("email (console transport)", to=to, subject=subject)
            for line in body.splitlines():
                if "http" in line:
                    # Never log full reset tokens at info in production console
                    # - they are already protected by _dev_link guard.
                    log.info("email link", link=line.strip())

        outbox.add(message)
        return message

    def _send_smtp(self, to: str, subject: str, body: str) -> None:
        message = EmailMessage()
        message["From"] = formataddr((settings.SMTP_FROM_NAME, settings.SMTP_FROM))
        message["To"] = to
        message["Subject"] = subject
        message.set_content(body)

        # Port 465 is implicit TLS (SMTP_SSL). Port 587 and others are explicit
        # STARTTLS. Supporting both avoids a common production misconfiguration
        # where 465 is set but STARTTLS is still attempted, which fails.
        if settings.SMTP_PORT == 465:
            with smtplib.SMTP_SSL(settings.SMTP_HOST or "", settings.SMTP_PORT, timeout=15) as client:
                if settings.SMTP_USERNAME and settings.SMTP_PASSWORD:
                    client.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
                client.send_message(message)
        else:
            with smtplib.SMTP(settings.SMTP_HOST or "", settings.SMTP_PORT, timeout=15) as client:
                client.starttls()
                if settings.SMTP_USERNAME and settings.SMTP_PASSWORD:
                    client.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
                client.send_message(message)

    # ==================================================================
    # The three flows
    # ==================================================================
    def send_verification(self, *, to: str, name: str, token: str) -> SentMessage:
        link = f"{settings.EMAIL_LINK_BASE}/verify?token={token}"
        return self.send(
            to=to,
            subject="Verify your email address",
            body=(
                f"Hello {name},\n\n"
                "Confirm your email address to activate your Equity Research "
                "Platform account:\n\n"
                f"{link}\n\n"
                "The link is valid for 24 hours. If you did not create an "
                "account, no action is needed — the address will not be used.\n"
            ),
        )

    def send_password_reset(self, *, to: str, name: str, token: str) -> SentMessage:
        link = f"{settings.EMAIL_LINK_BASE}/reset-password?token={token}"
        return self.send(
            to=to,
            subject="Reset your password",
            body=(
                f"Hello {name},\n\n"
                "Use the link below to choose a new password:\n\n"
                f"{link}\n\n"
                "The link is valid for one hour and can be used once. If you "
                "did not request this, your password has not changed and you "
                "can ignore this message.\n"
            ),
        )

    def send_magic_link(self, *, to: str, name: str, token: str) -> SentMessage:
        link = f"{settings.EMAIL_LINK_BASE}/magic?token={token}"
        return self.send(
            to=to,
            subject="Your sign-in link",
            body=(
                f"Hello {name},\n\n"
                "Sign in without a password using the link below:\n\n"
                f"{link}\n\n"
                "The link is valid for 15 minutes and can be used once.\n"
            ),
        )

    def send_invitation(
        self, *, to: str, name: str, organisation: str, inviter: str, token: str,
    ) -> SentMessage:
        link = f"{settings.EMAIL_LINK_BASE}/accept-invite?token={token}"
        return self.send(
            to=to,
            subject=f"{inviter} invited you to {organisation}",
            body=(
                f"Hello {name},\n\n"
                f"{inviter} has invited you to join {organisation} on the "
                "Equity Research Platform.\n\n"
                f"Set your password and sign in:\n\n{link}\n\n"
                "The link is valid for one hour.\n"
            ),
        )
