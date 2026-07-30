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

from sqlalchemy import (
    Date, Float, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint,
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
