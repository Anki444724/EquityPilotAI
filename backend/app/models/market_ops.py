"""Market operations models (Phase 4).

``MarketOverride`` lets an operator pin a company's market snapshot to manual
values (price, volume, market cap, PE, PB) for a bounded window. ``LiveMarketService``
consults it before the router, so every surface — dashboard, company, portfolio,
watchlist, AI — renders the same manual snapshot while the override is active, and
reverts automatically on expiry.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    DateTime, Float, ForeignKey, Index, Integer, String, Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MarketOverride(Base):
    """A manual, time-boxed override of a company's market snapshot."""

    __tablename__ = "market_overrides"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[str] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    ticker: Mapped[str] = mapped_column(String(32), nullable=False, index=True)

    #: Manual values (None means "don't override this field").
    manual_price: Mapped[float | None] = mapped_column(Float)
    manual_volume: Mapped[float | None] = mapped_column(Float)
    manual_market_cap: Mapped[float | None] = mapped_column(Float)
    manual_pe: Mapped[float | None] = mapped_column(Float)
    manual_pb: Mapped[float | None] = mapped_column(Float)

    #: Human-readable reason for the override (shown in the audit trail).
    reason: Mapped[str | None] = mapped_column(Text)

    #: When the override expires (auto-revert). NULL = no expiry.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    #: If true, the override clears when the next market open arrives or on an
    #: explicit "clear" — used for one-off corrections.
    auto_revert: Mapped[bool] = mapped_column(default=False, nullable=False)

    created_by: Mapped[str | None] = mapped_column(String(36))
    created_by_email: Mapped[str | None] = mapped_column(String(254))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False,
    )

    __table_args__ = (
        Index("ix_market_override_active", "company_id", "expires_at"),
    )

    @property
    def is_active(self) -> bool:
        now = _utcnow()
        return self.expires_at is None or self.expires_at > now
