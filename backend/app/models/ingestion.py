"""Ingestion observability — Phase 1.

`FinancialsBackfillService` reports per-company outcomes inside the job's
result payload and logs. That was sufficient while one bounded job was the
only sweep. Phase 1 adds four more sync jobs and a 10x universe, which turns
"what failed and why" into a first-class query:

* **Resumability** — a run records how far it got, so a crashed sweep resumes
  rather than restarting 5,000 rows of provider calls.
* **Retry** — `failed_data_retry` needs a durable list of failed symbols with
  their verbatim reason and transient/permanent classification, not a log
  grepped after the fact.

The classification mirrors
`app.services.universe.financials_backfill.classify_ingest_failure`
(transient = 429/timeout/network, permanent = 404/no-data) so every sync job
answers the same question the same way.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class IngestionRun(Base):
    """One execution of one sync job (or one manual sweep)."""

    __tablename__ = "ingestion_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    #: The background job that produced this run, when there was one.
    job_id: Mapped[int | None] = mapped_column(
        ForeignKey("background_jobs.id", ondelete="SET NULL"), nullable=True, index=True,
    )
    #: company_universe_sync | price_sync | historical_price_sync |
    #: financials_sync | failed_data_retry
    kind: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    #: 'mock' | 'nse_master' | 'bse_master' | 'screener' | 'yahoo' | …
    provider: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False, index=True,
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    succeeded: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: Machine-readable progress + counters, including `next_index` for
    #: resuming a batched sweep where it stopped.
    stats: Mapped[dict[str, Any] | None] = mapped_column(JSON)

    failures = relationship(
        "IngestionFailure", back_populates="run", cascade="all, delete-orphan",
    )

    @property
    def ok(self) -> bool:
        return self.failed == 0

    def __repr__(self) -> str:  # pragma: no cover
        return f"<IngestionRun {self.kind} #{self.id} ok={self.failed == 0}>"


class IngestionFailure(Base):
    """One symbol that one run could not process, with the verbatim reason.

    Rows are the `failed_data_retry` job's work queue: unresolved rows whose
    backoff has elapsed are retried; `failure_kind='permanent'` rows stay for
    an operator, because retrying a 404 burns provider quota and changes
    nothing.
    """

    __tablename__ = "ingestion_failures"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("ingestion_runs.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    #: The sync family that failed — same vocabulary as IngestionRun.kind.
    kind: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    company_id: Mapped[str | None] = mapped_column(String(36))
    error: Mapped[str] = mapped_column(Text, nullable=False)
    #: transient | permanent
    failure_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    last_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False,
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: What the retry job needs to rebuild the request (e.g. the universe
    #: record for a failed company upsert).
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)

    run = relationship("IngestionRun", back_populates="failures")

    __table_args__ = (
        # One live failure per (run, symbol): a retry appends its own run's
        # row rather than mutating history.
        UniqueConstraint("run_id", "symbol", name="uq_ingestion_failure_run_symbol"),
        # The retry job's lookup: open failures, oldest attempt first.
        Index("ix_ingestion_failures_open", "failure_kind", "last_attempt_at"),
    )

    @property
    def is_transient(self) -> bool:
        return self.failure_kind == "transient"

    @property
    def is_open(self) -> bool:
        return self.resolved_at is None

    def __repr__(self) -> str:  # pragma: no cover
        return (f"<IngestionFailure {self.kind} {self.symbol} "
                f"{self.failure_kind} attempts={self.attempts}>")
