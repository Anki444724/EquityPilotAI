"""Production safety for email / password-reset.

The bug that prompted this file: production had no SMTP_HOST, so
password-reset emails never arrived, while _dev_link was correctly disabled
in production for security (no token exposure via API). The result was a dead
flow: request succeeds (neutral message to avoid enumeration) but no email
ever leaves.

This file asserts the security properties that must stay true while making
the operational gap visible:

* _dev_link() returns None in production even when email is not configured
* _dev_link() returns a link in development when email not configured
* EmailService uses console transport when not configured, smtp when configured
* Production degraded problems include missing SMTP and wrong EMAIL_LINK_BASE
* Reset links use EMAIL_LINK_BASE (must be https://equitypilot.in in production)
* SMTP_SSL path (port 465) vs STARTTLS (587) both handled
"""
from __future__ import annotations

import pytest
from datetime import datetime, timedelta, timezone

from app.core.config import Settings
from app.services.platform.email import EmailService, outbox


class FakeUser:
    email = "test@example.com"
    name = "Test"


def make_pending(token="secret-reset-token"):
    from app.domain.platform.identity import TokenType
    from app.services.platform.identity_service import PendingToken
    return PendingToken(
        user=FakeUser(),  # type: ignore
        token=token,
        purpose=TokenType.PASSWORD_RESET,
        expires_at=datetime.now(timezone.utc)+timedelta(hours=1),
    )


class TestDevLinkSafety:
    def test_dev_link_disabled_in_production_even_without_smtp(self, monkeypatch):
        """_dev_link must never expose tokens in production API responses."""
        from app.api.v1.auth import _dev_link
        from app.core.config import settings

        monkeypatch.setattr(settings, "ENVIRONMENT", "production")
        monkeypatch.setattr(settings, "SMTP_HOST", None)
        assert settings.is_production is True
        assert settings.email_configured is False

        pending = make_pending("secret-reset-token")
        # In production, dev_link must be None to avoid token exposure
        assert _dev_link(pending, "/reset-password") is None

        monkeypatch.setattr(settings, "ENVIRONMENT", "development")

    def test_dev_link_enabled_in_development_without_smtp(self, monkeypatch):
        from app.api.v1.auth import _dev_link
        from app.core.config import settings

        monkeypatch.setattr(settings, "ENVIRONMENT", "development")
        monkeypatch.setattr(settings, "SMTP_HOST", None)
        monkeypatch.setattr(settings, "EMAIL_LINK_BASE", "http://localhost:3000")
        assert settings.is_production is False
        assert settings.email_configured is False

        pending = make_pending("dev-token-123")
        link = _dev_link(pending, "/reset-password")
        assert link is not None
        assert "dev-token-123" in link
        assert "http://localhost:3000" in link

    def test_dev_link_disabled_when_smtp_configured(self, monkeypatch):
        from app.api.v1.auth import _dev_link
        from app.core.config import settings

        monkeypatch.setattr(settings, "ENVIRONMENT", "development")
        monkeypatch.setattr(settings, "SMTP_HOST", "smtp.example.com")
        assert settings.email_configured is True

        pending = make_pending("should-not-appear")
        assert _dev_link(pending, "/reset-password") is None
        monkeypatch.setattr(settings, "SMTP_HOST", None)


class TestEmailTransportSelection:
    def test_console_transport_when_no_smtp(self, monkeypatch):
        from app.core.config import settings

        monkeypatch.setattr(settings, "SMTP_HOST", None)
        assert settings.email_configured is False

        outbox.clear()
        svc = EmailService()
        msg = svc.send(to="a@b.com", subject="Test", body="Body")
        assert msg.transport == "console"
        assert len(outbox) == 1

    def test_smtp_transport_when_configured(self, monkeypatch):
        from app.core.config import settings

        monkeypatch.setattr(settings, "SMTP_HOST", "smtp.example.com")
        monkeypatch.setattr(EmailService, "_send_smtp", lambda *a, **k: None)
        assert settings.email_configured is True

        outbox.clear()
        svc = EmailService()
        msg = svc.send(to="a@b.com", subject="Test", body="Body")
        assert msg.transport == "smtp"

        monkeypatch.setattr(settings, "SMTP_HOST", None)

    def test_smtp_ssl_path_for_port_465(self, monkeypatch):
        from app.core.config import settings
        import smtplib

        monkeypatch.setattr(settings, "SMTP_HOST", "smtp.example.com")
        monkeypatch.setattr(settings, "SMTP_PORT", 465)

        captured = {}

        class FakeSMTP_SSL:
            def __init__(self, *a, **k):
                captured["ssl"] = True
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def login(self, *a, **k): pass
            def send_message(self, *a, **k): pass

        class FakeSMTP:
            def __init__(self, *a, **k):
                captured["starttls"] = True
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def starttls(self): pass
            def login(self, *a, **k): pass
            def send_message(self, *a, **k): pass

        monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP_SSL)
        monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)

        EmailService()._send_smtp(to="a@b.com", subject="s", body="b")
        assert captured.get("ssl") is True
        assert "starttls" not in captured

        monkeypatch.setattr(settings, "SMTP_HOST", None)
        monkeypatch.setattr(settings, "SMTP_PORT", 587)


class TestProductionEmailConfig:
    def test_production_degraded_without_smtp(self):
        prod = Settings(ENVIRONMENT="production", SECRET_KEY="x"*48, SMTP_HOST=None, DATABASE_URL="postgresql://x", NATIVE_AUTH=True, DEBUG=False, CORS_ORIGINS=["https://equitypilot.in"], EMAIL_LINK_BASE="https://equitypilot.in")
        assert any("SMTP" in p for p in prod.production_degraded_problems())

    def test_production_degraded_with_localhost_link_base(self):
        prod = Settings(
            ENVIRONMENT="production",
            SECRET_KEY="x"*48,
            SMTP_HOST="smtp.example.com",
            EMAIL_LINK_BASE="http://localhost:3000",
            DATABASE_URL="postgresql://x",
            NATIVE_AUTH=True,
            DEBUG=False,
            CORS_ORIGINS=["https://equitypilot.in"],
        )
        problems = prod.production_degraded_problems()
        assert any("EMAIL_LINK_BASE" in p and "localhost" in p for p in problems)

    def test_production_degraded_with_http_link_base(self):
        prod = Settings(
            ENVIRONMENT="production",
            SECRET_KEY="x"*48,
            SMTP_HOST="smtp.example.com",
            EMAIL_LINK_BASE="http://equitypilot.in",
            DATABASE_URL="postgresql://x",
            NATIVE_AUTH=True,
            DEBUG=False,
            CORS_ORIGINS=["https://equitypilot.in"],
        )
        problems = prod.production_degraded_problems()
        assert any("HTTPS" in p for p in problems)

    def test_production_ok_with_proper_email_config(self):
        prod = Settings(
            ENVIRONMENT="production",
            SECRET_KEY="x"*48,
            SMTP_HOST="smtp.example.com",
            SMTP_FROM="no-reply@equitypilot.in",
            EMAIL_LINK_BASE="https://equitypilot.in",
            DATABASE_URL="postgresql://x",
            NATIVE_AUTH=True,
            DEBUG=False,
            CORS_ORIGINS=["https://equitypilot.in"],
        )
        problems = prod.production_degraded_problems()
        assert not any("SMTP host" in p for p in problems)
        assert not any("EMAIL_LINK_BASE" in p for p in problems)
        assert not any("SMTP_FROM" in p for p in problems)

    def test_reset_link_uses_production_domain(self, monkeypatch):
        from app.core.config import settings

        monkeypatch.setattr(settings, "EMAIL_LINK_BASE", "https://equitypilot.in")
        monkeypatch.setattr(settings, "SMTP_HOST", None)
        outbox.clear()
        svc = EmailService()
        msg = svc.send_password_reset(to="user@example.com", name="User", token="test-token-xyz")
        assert "https://equitypilot.in/reset-password?token=test-token-xyz" in msg.body
        assert "http://localhost" not in msg.body


class TestPasswordResetFlowSecurity:
    def test_neutral_message_for_unknown_and_known(self):
        from app.api.v1.auth import _NEUTRAL
        assert "If that address is registered" in _NEUTRAL

    def test_no_token_in_production_api_response(self, monkeypatch):
        from app.core.config import settings
        from app.api.v1.auth import _dev_link

        monkeypatch.setattr(settings, "ENVIRONMENT", "production")
        monkeypatch.setattr(settings, "SMTP_HOST", None)

        pending = make_pending("super-secret")
        assert _dev_link(pending, "/reset-password") is None
        monkeypatch.setattr(settings, "ENVIRONMENT", "development")
