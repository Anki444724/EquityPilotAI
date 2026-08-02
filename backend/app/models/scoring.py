"""Persistence for scoring.

Two things are stored and nothing else:

* **Weight profiles** — a user's investment philosophy, which is an input.
* **Score snapshots** — a point-in-time record, which is *history*.

The snapshot is the one place in the platform where a computed result is
persisted. That is deliberate: "what did this company score last quarter" is a
question the live engine cannot answer, because the inputs have since changed.
A trend needs a record.

Category detail is stored as JSON rather than as rows. Snapshots are written
once and read whole, never queried by individual metric, so a normalised table
would add joins without buying anything.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Date, DateTime, Float, ForeignKey, Index, Integer, JSON, String, Text,
    UniqueConstraint, func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.company import Company


class ScoringWeightProfile(Base):
    """A user-defined weight profile. Built-ins live in code, not here."""

    __tablename__ = "scoring_weight_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    owner: Mapped[str | None] = mapped_column(String(64), index=True)

    #: {category_key: relative_weight}. Normalised on load.
    weights: Mapped[dict] = mapped_column(JSON, nullable=False)
    #: The built-in profile this was derived from, for provenance.
    derived_from: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (
        UniqueConstraint("key", "owner", name="uq_profile_key_owner"),
    )


class ScoreSnapshot(Base):
    """A scoring run, preserved so trends can be measured."""

    __tablename__ = "score_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[str] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    as_of: Mapped[object] = mapped_column(Date, nullable=False)

    profile_key: Mapped[str] = mapped_column(String(64), nullable=False)

    overall_score: Mapped[float] = mapped_column(Float, nullable=False)
    grade: Mapped[str] = mapped_column(String(8), nullable=False)
    stars: Mapped[float] = mapped_column(Float, nullable=False)
    recommendation: Mapped[str] = mapped_column(String(16), nullable=False)
    conviction: Mapped[str | None] = mapped_column(String(16))

    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    verified_pct: Mapped[float] = mapped_column(Float, default=0.0)
    estimated_pct: Mapped[float] = mapped_column(Float, default=0.0)
    missing_pct: Mapped[float] = mapped_column(Float, default=0.0)

    #: {category_key: raw_score} — enough to rebuild a radar or a trend line.
    category_scores: Mapped[dict] = mapped_column(JSON, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)

    company: Mapped[Company] = relationship()

    __table_args__ = (
        UniqueConstraint("company_id", "as_of", "profile_key",
                         name="uq_snapshot_company_date_profile"),
        Index("ix_snapshot_company_date", "company_id", "as_of"),
    )


class DataQualitySnapshot(Base):
    """The Data Quality Score for one company, as last computed.

    Stored so the dashboard can aggregate across 500 companies in one query
    rather than scoring each on request — a full sweep is ~0.7s per company,
    which is fine on demand and far too slow for a leaderboard.

    One row per company, updated in place. Deliberately NOT versioned, unlike
    the knowledge vault: this is a derived measurement of current state, not
    an assertion about the company, and a history of it would grow without
    ever being read. The score is always recomputable from the underlying
    rows, which are themselves versioned.
    """

    __tablename__ = "data_quality_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False, unique=True, index=True,
    )

    score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False,
                                         index=True)
    grade: Mapped[str] = mapped_column(String(2), default="F", nullable=False,
                                       index=True)

    #: Points earned per dimension, so the dashboard can report where the
    #: universe is weak without recomputing every company.
    identity_points: Mapped[float] = mapped_column(Float, default=0.0)
    financials_points: Mapped[float] = mapped_column(Float, default=0.0)
    documents_points: Mapped[float] = mapped_column(Float, default=0.0)
    vault_points: Mapped[float] = mapped_column(Float, default=0.0)
    ai_points: Mapped[float] = mapped_column(Float, default=0.0)
    freshness_points: Mapped[float] = mapped_column(Float, default=0.0)
    source_points: Mapped[float] = mapped_column(Float, default=0.0)
    health_points: Mapped[float] = mapped_column(Float, default=0.0)

    missing_count: Mapped[int] = mapped_column(Integer, default=0,
                                               nullable=False)
    #: JSON array of human-readable missing items, so the panel renders
    #: without a second pass over the scorer.
    missing_items: Mapped[list | None] = mapped_column(JSON)

    last_updated_days: Mapped[int | None] = mapped_column(Integer)
    knowledge_freshness_days: Mapped[int | None] = mapped_column(Integer)

    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )

    __table_args__ = (
        Index("ix_quality_leaderboard", "score", "grade"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<DataQualitySnapshot {self.company_id[:8]} {self.score}>"
