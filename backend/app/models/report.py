"""Persistence for the report generator.

Three tables. The shape follows from the brief's "versioned reports" and
"caching" requirements, and from one observation: a rendered PDF is expensive
to produce and cheap to store, so it is stored.

* **Report** — one generation run: what was asked for, what came out, and the
  audit that judged it.
* **ReportArtifact** — the rendered bytes, one row per format. Kept in the
  database rather than on disk because the platform must run with no object
  store, and because a report and its artefacts should not be able to
  disagree about which version they belong to.
* **ReportJob** — the background queue, same pattern as Module 7's ingestion.

Versioning is per company and report type: regenerating an institutional report
for BHARATCP creates version 2 and leaves version 1 intact and retrievable. A
report that was sent to a committee must still resolve months later, exactly as
a superseded document does in Module 7.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean, DateTime, Float, ForeignKey, Index, Integer, JSON, LargeBinary,
    String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class Report(Base):
    """One report generation run."""

    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    #: Denormalised so a historical report stays readable if the company row
    #: is removed — the same reasoning as Module 7's document ticker.
    ticker: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    company_name: Mapped[str] = mapped_column(String(200), nullable=False)

    owner_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    report_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    theme: Mapped[str] = mapped_column(String(12), default="light")

    #: Increments per (company, report_type). Prior versions are retained.
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    superseded_by: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("reports.id", ondelete="SET NULL")
    )

    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    error: Mapped[str | None] = mapped_column(Text)

    analyst: Mapped[str | None] = mapped_column(String(120))
    portfolio_id: Mapped[int | None] = mapped_column(Integer, index=True)

    # -- outcome ------------------------------------------------------
    section_count: Mapped[int] = mapped_column(Integer, default=0)
    insufficient_count: Mapped[int] = mapped_column(Integer, default=0)
    block_count: Mapped[int] = mapped_column(Integer, default=0)
    chart_count: Mapped[int] = mapped_column(Integer, default=0)
    table_count: Mapped[int] = mapped_column(Integer, default=0)
    evidence_count: Mapped[int] = mapped_column(Integer, default=0)
    word_count: Mapped[int] = mapped_column(Integer, default=0)

    #: Citation audit, so a report's integrity is judged once and recorded
    #: rather than recomputed differently by each reader.
    citation_coverage: Mapped[float] = mapped_column(Float, default=0.0)
    citation_clean: Mapped[bool] = mapped_column(Boolean, default=False)
    audit: Mapped[dict | None] = mapped_column(JSON)

    #: Content key of the inputs. Identical inputs return the cached report.
    input_hash: Mapped[str | None] = mapped_column(String(40), index=True)
    provenance: Mapped[dict | None] = mapped_column(JSON)
    #: The block tree, serialised. Lets a new format be rendered later without
    #: re-running the engines.
    document: Mapped[dict | None] = mapped_column(JSON)

    build_ms: Mapped[float] = mapped_column(Float, default=0.0)
    generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    artifacts: Mapped[list["ReportArtifact"]] = relationship(
        back_populates="report", cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index("ix_report_company_type", "company_id", "report_type"),
        Index("ix_report_owner_status", "owner_id", "status"),
        UniqueConstraint(
            "company_id", "report_type", "version", name="uq_report_version",
        ),
    )

    @property
    def is_current(self) -> bool:
        return self.superseded_by is None


class ReportArtifact(Base):
    """A rendered output — one row per format."""

    __tablename__ = "report_artifacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    report_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("reports.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    fmt: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    filename: Mapped[str] = mapped_column(String(240), nullable=False)
    media_type: Mapped[str] = mapped_column(String(120), nullable=False)
    #: The bytes. A PDF of a 20-page report is a few hundred kilobytes, which
    #: is comfortably within what a row should hold, and it keeps the platform
    #: free of a filesystem dependency it otherwise does not need.
    payload: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    page_count: Mapped[int | None] = mapped_column(Integer)
    render_ms: Mapped[float] = mapped_column(Float, default=0.0)

    report: Mapped[Report] = relationship(back_populates="artifacts")

    __table_args__ = (
        UniqueConstraint("report_id", "fmt", name="uq_artifact_report_format"),
    )


class ReportJob(Base):
    """A queued generation task."""

    __tablename__ = "report_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    report_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("reports.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    owner_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    stage: Mapped[str] = mapped_column(String(24), default="queued")
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=2)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[float] = mapped_column(Float, default=0.0)
    #: Per-stage timings, for the performance panel.
    timings: Mapped[dict | None] = mapped_column(JSON)

    __table_args__ = (
        Index("ix_report_job_status", "status", "id"),
    )
