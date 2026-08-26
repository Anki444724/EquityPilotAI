"""Persistence for the Angel One SmartAPI broker integration.

Two tables, both scoped to a single user:

* ``broker_accounts`` — one Angel One session per user. The Angel One
  access token and trading password are stored **enveloped** (AES-256-GCM,
  see ``services/platform/crypto.py``), never in plaintext, and never in an
  environment variable. A database leak yields ciphertext, not a trading
  session.
* ``broker_orders`` — every order this platform places, keyed by the Angel
  One order id. Postbacks are only ever applied to an order that exists here
  for the calling user, which is what makes an arbitrary postback order id
  useless to an attacker: an order id the platform never created is simply
  not in this table and is refused.

``tenant_id`` is denormalised for tenant isolation and reporting; the
authoritative ownership key is ``user_id`` (one Angel One login per person).

Uniqueness is enforced with unique *indexes* (rather than table-level
UniqueConstraints) so the schema is portable across SQLite (tests) and
Postgres (production) and Alembic can emit it without an unsupported
``ALTER TABLE ... ADD CONSTRAINT`` on SQLite.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean, DateTime, Float, ForeignKey, Index, Integer, LargeBinary, String,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class BrokerAccount(Base):
    """One Angel One SmartAPI session, owned by a single user."""

    __tablename__ = "broker_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
    )
    tenant_id: Mapped[int | None] = mapped_column(Integer)

    #: Angel One 10-digit client id (the trading client code). Public-ish,
    #: safe to store in plaintext — it is an identifier, not a secret.
    client_code: Mapped[str] = mapped_column(String(20), nullable=False)
    exchange: Mapped[str] = mapped_column(String(10), default="NSE", nullable=False)

    #: Angel One access token, enveloped. Never plaintext.
    access_token_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary)
    access_token_key_version: Mapped[int | None] = mapped_column(Integer)
    #: Angel One trading password (T2 token), enveloped.
    trading_password_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary)
    trading_password_key_version: Mapped[int | None] = mapped_column(Integer)

    #: Angel One's own validity window, recorded at login for display/status.
    angel_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    angel_valid_upto: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    connected: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_postback_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_broker_accounts_user_id", "user_id", unique=True),
        Index("ix_broker_accounts_tenant_id", "tenant_id"),
    )


class BrokerOrder(Base):
    """One order placed (or postback-updated) through this platform.

    ``angel_order_id`` is the Angel One order id. It is indexed and only
    updated by postbacks that reference an order already recorded here for
    the calling user, so a postback carrying a foreign or fabricated order id
    has no row to update and is refused.
    """

    __tablename__ = "broker_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
    )
    tenant_id: Mapped[int | None] = mapped_column(Integer)

    #: Angel One order id (set on place), and the exchange order id.
    angel_order_id: Mapped[str | None] = mapped_column(String(40))
    exchange_order_id: Mapped[str | None] = mapped_column(String(40))

    symbol: Mapped[str] = mapped_column(String(30), nullable=False)
    exchange: Mapped[str] = mapped_column(String(10), default="NSE", nullable=False)
    market: Mapped[str] = mapped_column(String(10), default="NSE", nullable=False)
    transak_type: Mapped[str] = mapped_column(String(1), nullable=False)  # B / S
    product_type: Mapped[str] = mapped_column(String(10), default="CNC", nullable=False)
    order_type: Mapped[str] = mapped_column(String(10), default="LIMIT", nullable=False)
    validity: Mapped[str] = mapped_column(String(10), default="DAY", nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    trigger_price: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    #: Angel One order status: OPEN, COMPLETE, REJECTED, CANCELLED, ...
    status: Mapped[str] = mapped_column(String(20), default="OPEN", nullable=False)
    fill_quantity: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    average_price: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    remark: Mapped[str | None] = mapped_column(String(200))

    placed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False)
    last_postback_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_broker_orders_user_id", "user_id"),
        Index("ix_broker_orders_tenant_id", "tenant_id"),
        Index("ix_broker_orders_angel_order_id", "angel_order_id"),
        Index("ix_broker_orders_status", "status"),
        Index("uq_broker_order_user_angel", "user_id", "angel_order_id", unique=True),
    )
