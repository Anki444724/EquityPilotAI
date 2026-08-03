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


class AIScoreVersion(Base):
    """One permanently retained run of the AI Scoring Engine 3.0.

    **Append-only by construction.** The brief requires that historical scores
    are never overwritten and that every version is stored permanently, so
    there is no update path: a recalculation inserts a new row at
    ``version + 1`` and marks the previous row superseded. The unique
    constraint on (company, version) makes an accidental overwrite a database
    error rather than a silent loss.

    This differs deliberately from :class:`ScoreSnapshot`, which is keyed by
    date and *is* updated in place. That table answers "what was the score on
    this day"; this one answers "what did we ever say, when, and on what
    evidence" — a question that only stays answerable if nothing is ever
    replaced.

    ``input_fingerprint`` is a SHA256 over the observed inputs. Two consecutive
    runs with the same fingerprint saw the same world, which lets the learning
    loop skip writing a row that would add a version and no information — the
    only case where a recalculation legitimately produces nothing.

    Module and factor detail is stored as JSON rather than as child tables.
    A score version is written once and always read whole; normalising it
    would add two joins to every read and buy nothing, since no query ever
    asks "which companies scored above 8 on pricing power" — that question is
    answered against the live engine, not against history.
    """

    __tablename__ = "ai_score_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )

    #: Monotonic per company, starting at 1. Never reused.
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    #: "current" | "superseded". A superseded row is retained forever.
    status: Mapped[str] = mapped_column(
        String(12), default="current", nullable=False, index=True,
    )

    #: The framework definition that produced this. A weight change makes two
    #: versions incomparable, and comparing them anyway is how a phantom
    #: trend appears in a chart.
    framework_version: Mapped[str] = mapped_column(String(16), nullable=False)

    overall_score: Mapped[float] = mapped_column(Float, nullable=False, index=True)
    rating: Mapped[str] = mapped_column(String(4), nullable=False, index=True)
    recommendation: Mapped[str] = mapped_column(String(16), nullable=False,
                                                index=True)
    #: Weighted share of inputs that were observable, 0-1.
    coverage: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    #: {module_key: score_0_10} — enough for a trend line or a radar without
    #: deserialising the full payload.
    module_scores: Mapped[dict] = mapped_column(JSON, nullable=False)
    #: {probability_key: 0-1}.
    probabilities: Mapped[dict] = mapped_column(JSON, nullable=False)
    #: The complete explainable result: every factor, reason and citation.
    detail: Mapped[dict] = mapped_column(JSON, nullable=False)

    summary: Mapped[str | None] = mapped_column(Text)
    recommendation_reason: Mapped[str | None] = mapped_column(Text)

    #: SHA256 over the observed inputs. Equal fingerprints mean equal inputs.
    input_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False,
                                                   index=True)
    total_citations: Mapped[int] = mapped_column(Integer, default=0,
                                                 nullable=False)

    #: What caused this recalculation — "filing", "manual", "scheduled",
    #: "backfill". Recorded so a version can be traced to the event that
    #: produced it.
    trigger: Mapped[str] = mapped_column(String(24), default="manual",
                                         nullable=False, index=True)
    #: The document whose arrival triggered the rescore, where there was one.
    trigger_document_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("documents.id", ondelete="SET NULL"),
    )

    #: The version this one replaced. Null for the first.
    supersedes_version: Mapped[int | None] = mapped_column(Integer)
    #: Change in overall score against the version it replaced, for alerting.
    score_delta: Mapped[float | None] = mapped_column(Float)

    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
        index=True,
    )

    company: Mapped[Company] = relationship()

    __table_args__ = (
        # Makes an overwrite a database error rather than a silent loss.
        UniqueConstraint("company_id", "version", name="uq_ai_score_version"),
        # The hot read: the current score for one company.
        Index("ix_ai_score_current", "company_id", "status"),
        # The trend read: every version for one company, newest first.
        Index("ix_ai_score_history", "company_id", "computed_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return (f"<AIScoreVersion {self.company_id[:8]} v{self.version} "
                f"{self.overall_score:.1f} {self.rating}>")
