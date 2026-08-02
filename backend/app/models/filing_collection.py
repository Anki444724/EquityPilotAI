"""Persistence for automated filing collection.

Two tables.

`DiscoveredFiling` is the ledger of everything the crawler has ever seen, one
row per (company, source, source reference). It exists so the system can
answer "have I already dealt with this?" without downloading the file again —
the URL is checked before any bytes move, and the SHA256 after, because the
same document is frequently published at two different URLs by NSE and BSE.

`CompanyCrawlState` is per-company scheduling and health: which tier the
company is on, when it was last visited, and how many consecutive failures it
has accumulated. Consecutive failures matter because a company whose IR site
has moved will fail every night forever otherwise, burning the crawl budget
that other companies need.

Neither table stores document *content*. Once a filing is downloaded it
becomes a normal `Document` through the existing ingestion service, and
`DiscoveredFiling.document_id` links the two. That keeps one storage path,
one processing pipeline and one retrieval index rather than a parallel set for
automatically-collected documents.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text,
    UniqueConstraint, func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
# `DiscoveredFiling.document_id` is a foreign key to `documents`, and
# SQLAlchemy resolves that target by name against the shared metadata. If this
# module is imported without `app.models.document` having been imported first,
# the mapper raises NoReferencedTableError at configuration time — which
# surfaces as an unrelated-looking failure in whatever happened to touch the
# ORM first. Importing it here makes the dependency explicit rather than
# relying on import order elsewhere.
from app.models.document import Document  # noqa: F401 - registers the FK target


class DiscoveredFiling(Base):
    """One document the crawler has seen, and what became of it."""

    __tablename__ = "discovered_filings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )

    #: "NSE Corporate Filings", "BSE Corporate Announcements",
    #: "Investor Relations" — the provider name, matching the filings layer.
    source: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    #: The exchange's own identifier where it supplies one; otherwise the URL.
    #: Combined with `source` this is the natural key for "seen before".
    source_reference: Mapped[str] = mapped_column(String(500), nullable=False)
    source_url: Mapped[str | None] = mapped_column(String(1000))

    title: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    filing_type: Mapped[str | None] = mapped_column(String(40), index=True)
    doc_type: Mapped[str | None] = mapped_column(String(40))
    #: Classifier confidence, so a low-confidence filing can be reviewed
    #: rather than silently trusted.
    classification_confidence: Mapped[float | None] = mapped_column(Float)

    published_on: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fiscal_year: Mapped[int | None] = mapped_column(Integer, index=True)
    quarter: Mapped[str | None] = mapped_column(String(4))
    language: Mapped[str] = mapped_column(String(8), default="en", nullable=False)

    status: Mapped[str] = mapped_column(
        String(16), default="discovered", nullable=False, index=True,
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)

    #: SHA256 of the downloaded bytes. Null until downloaded. This is the
    #: second dedup key: NSE and BSE publish identical PDFs at different URLs,
    #: so URL-level dedup alone stores each report twice.
    content_sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    file_size: Mapped[int | None] = mapped_column(Integer)

    #: The ingested document, once processing completes.
    document_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("documents.id", ondelete="SET NULL"), index=True,
    )

    discovered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    downloaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        # The crawler re-sees the same announcement every night. This is what
        # makes "already indexed" a single indexed lookup rather than a scan.
        UniqueConstraint("source", "source_reference",
                         name="uq_discovered_source_ref"),
        Index("ix_discovered_company_status", "company_id", "status"),
        Index("ix_discovered_status_discovered", "status", "discovered_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<DiscoveredFiling {self.source} {self.title[:40]!r} {self.status}>"


class CompanyCrawlState(Base):
    """Per-company crawl scheduling and health."""

    __tablename__ = "company_crawl_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False, unique=True, index=True,
    )

    tier: Mapped[str] = mapped_column(
        String(8), default="weekly", nullable=False, index=True,
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    #: The company's own investor-relations page, when known. Priority 1 in
    #: the brief; blank for companies whose page has not been registered.
    ir_url: Mapped[str | None] = mapped_column(String(500))
    #: How the IR URL was obtained and how much to trust it. Discovery is a
    #: heuristic — a derived domain plus a conventional path — so a stored URL
    #: must be visibly a guess rather than indistinguishable from one a human
    #: verified. `ir_url_checked_at` also stops the discoverer re-probing the
    #: same dead domain every night.
    ir_url_confidence: Mapped[float | None] = mapped_column(Float)
    ir_url_method: Mapped[str | None] = mapped_column(String(24))
    ir_url_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )

    #: BSE scrip code, needed because BSE's API keys on code rather than symbol.
    bse_scrip_code: Mapped[str | None] = mapped_column(String(16))

    last_crawled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_status: Mapped[str | None] = mapped_column(String(16))
    last_error: Mapped[str | None] = mapped_column(Text)

    #: Reset on any success. A company that fails repeatedly is demoted so a
    #: permanently broken source cannot consume the nightly budget forever.
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False,
    )
    documents_found: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    documents_ingested: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    __table_args__ = (
        Index("ix_crawl_due", "enabled", "tier", "last_crawled_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<CompanyCrawlState {self.company_id} {self.tier}>"
