"""AI operations models (Phase 5).

``AIOverride`` lets an operator pin a company's AI score to manual values
(score, confidence, risk, summary, bull/bear case, recommendation) for a bounded
window. The scoring endpoint consults it before returning, so every surface —
company, dashboard, portfolio, watchlist — consumes the same manual score until
it expires or reverts to auto mode.
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


class AIOverride(Base):
    """A manual, time-boxed override of a company's AI score."""

    __tablename__ = "ai_overrides"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[str] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    ticker: Mapped[str] = mapped_column(String(32), nullable=False, index=True)

    #: Auto mode = no override (the live engine scores). Manual = pinned.
    mode: Mapped[str] = mapped_column(String(12), default="auto", nullable=False)
    #: Manual values (None means "don't override this field").
    manual_score: Mapped[float | None] = mapped_column(Float)
    manual_confidence: Mapped[float | None] = mapped_column(Float)
    manual_risk: Mapped[float | None] = mapped_column(Float)
    manual_summary: Mapped[str | None] = mapped_column(Text)
    manual_bull_case: Mapped[str | None] = mapped_column(Text)
    manual_bear_case: Mapped[str | None] = mapped_column(Text)
    manual_recommendation: Mapped[str | None] = mapped_column(String(32))

    reason: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    created_by: Mapped[str | None] = mapped_column(String(36))
    created_by_email: Mapped[str | None] = mapped_column(String(254))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False,
    )

    __table_args__ = (
        Index("ix_ai_override_active", "company_id", "expires_at"),
    )

    @property
    def is_active(self) -> bool:
        return self.mode == "manual" and (
            self.expires_at is None or self.expires_at > _utcnow()
        )
