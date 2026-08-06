"""Financial-statement admin models (Phase 3).

Adds corporate actions and fact-level version history on top of the existing
``financial_facts``, ``quarterly_results`` and ``shareholding_snapshots``
tables. Every mutation to a company's financials writes a
:class:`FinancialFactVersion` snapshot and bumps ``companies.data_version``, so
downstream engines (AI score, risk, growth, valuation, confidence) recompute on
the next read.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import (
    Date, DateTime, Float, ForeignKey, Index, Integer, JSON, String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class CorporateAction(Base):
    """A corporate action: dividend, bonus, split, buyback, rights, etc."""

    __tablename__ = "corporate_actions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[str] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    action_type: Mapped[str] = mapped_column(String(24), nullable=False)  # dividend|bonus|split|buyback|rights|merger
    ex_date: Mapped[date | None] = mapped_column(Date, index=True)
    record_date: Mapped[date | None] = mapped_column(Date)
    #: Value depends on type: dividend ₹/share, bonus ratio, split ratio, etc.
    value: Mapped[float | None] = mapped_column(Float)
    description: Mapped[str | None] = mapped_column(String(400))
    source: Mapped[str | None] = mapped_column(String(120))

    __table_args__ = (
        Index("ix_corporate_action_company_date", "company_id", "ex_date"),
    )


class FinancialFactVersion(Base):
    """An immutable snapshot of a company's financials after one edit.

    Captures the entire set of annual facts, quarterly results and shareholding
    for a company so an admin can review, diff and roll back. Records the actor
    and the change summary, satisfying version history / rollback / audit for
    the financial editor without a per-cell audit table.
    """

    __tablename__ = "financial_fact_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[str] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(36))
    actor_email: Mapped[str | None] = mapped_column(String(254))
    #: {"facts": [...], "quarterly": [...], "shareholding": [...], "actions": [...]}
    snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    change_type: Mapped[str] = mapped_column(String(16), default="update")
    summary: Mapped[str] = mapped_column(String(400), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False, index=True,
    )

    __table_args__ = (
        UniqueConstraint("company_id", "version", name="uq_financial_fact_version"),
    )
