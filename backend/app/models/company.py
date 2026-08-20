"""Company and canonical financial fact models.

`financial_facts` is the relational form of the workbook's `StoreVals` grid
('0A Data Import'!$AB$241:$FC$294). The workbook was limited to 12 company
slots by fixed spreadsheet geometry; Postgres has no such limit, so the
Universal Company Engine becomes genuinely unbounded here.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    DateTime, Float, ForeignKey, Index, Integer, JSON, String, Text,
    UniqueConstraint, func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Exchange(StrEnum):
    NSE = "NSE"
    BSE = "BSE"
    BOTH = "NSE/BSE"
    # Phase 3. Stored as a plain String(16) rather than a database enum, so
    # adding a venue is data rather than a migration.
    NASDAQ = "NASDAQ"
    NYSE = "NYSE"
    AMEX = "AMEX"


class Company(Base):
    """A listed company — the root of the Universal Company Engine."""

    __tablename__ = "companies"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    ticker: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    exchange: Mapped[str] = mapped_column(String(16), default=Exchange.NSE)
    isin: Mapped[str | None] = mapped_column(String(16), unique=True)

    #: BSE scrip code, where the company is also listed there.
    #:
    #: Stored as a string, not an integer: it is an identifier rather than a
    #: quantity, several are zero-padded, and arithmetic on it is always a
    #: bug. Nullable because a genuine NSE-only listing has none — BSE Ltd
    #: and CDSL are the two in the Nifty 500.
    bse_code: Mapped[str | None] = mapped_column(String(16), index=True)

    #: "largecap" | "midcap" | "smallcap", from NSE's own constituent indices
    #: (Nifty 100 / Midcap 150 / Smallcap 250) rather than a threshold we
    #: invented. Those three partition the Nifty 500 exactly, so the
    #: classification is the exchange's, not ours, and it stays correct when
    #: NSE rebalances.
    market_cap_category: Mapped[str | None] = mapped_column(String(12), index=True)

    #: Whether the company is currently an active, tradeable listing.
    #: Kept explicit so a delisted or suspended company can be retained for
    #: its history without being swept into daily collection.
    listing_status: Mapped[str] = mapped_column(
        String(12), default="active", server_default="active", nullable=False,
        index=True,
    )

    #: Index membership, e.g. "NIFTY500". Records why the company is in the
    #: universe at all, which is what makes a later universe change auditable.
    index_membership: Mapped[str | None] = mapped_column(String(64), index=True)

    #: How this company's stored figures are denominated (Phase 3).
    #:
    #: The platform was built for Indian listings, so "₹ crore" was a constant
    #: rather than a variable. That is correct for TCS and a fabrication for
    #: Apple — the same code path would have reported Apple's revenue as
    #: "416,161 ₹ cr". Currency and scale now travel with the company.
    #:
    #: `scale` is part of the stored value's *meaning*, not a display choice:
    #: Indian statements are stored in crore (1e7), US statements in millions
    #: (1e6). Defaults preserve the existing Indian corpus exactly.
    currency: Mapped[str] = mapped_column(String(3), default="INR",
                                          server_default="INR", nullable=False)
    reporting_scale: Mapped[str] = mapped_column(
        String(8), default="crore", server_default="crore", nullable=False,
    )

    sector: Mapped[str | None] = mapped_column(String(120), index=True)
    industry: Mapped[str | None] = mapped_column(String(120), index=True)

    # Denominated in `currency` at `reporting_scale` — ₹ crore for an Indian
    # listing, $ millions for a US one.
    market_cap: Mapped[float | None] = mapped_column(Float)
    current_price: Mapped[float | None] = mapped_column(Float)       # per share
    shares_outstanding: Mapped[float | None] = mapped_column(Float)

    description: Mapped[str | None] = mapped_column(Text)
    website: Mapped[str | None] = mapped_column(String(300))
    incorporated_year: Mapped[int | None] = mapped_column(Integer)

    # ---- Phase 2: enterprise company-management fields -----------------
    #: Nominal value of one share (₹).
    face_value: Mapped[float | None] = mapped_column(Float)
    #: Date the company first listed.
    listing_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ceo: Mapped[str | None] = mapped_column(String(160))
    employees: Mapped[int | None] = mapped_column(Integer)
    headquarters: Mapped[str | None] = mapped_column(String(200))
    #: Logo / favicon URLs (uploaded or external).
    logo_url: Mapped[str | None] = mapped_column(String(500))
    favicon_url: Mapped[str | None] = mapped_column(String(500))
    #: Soft-delete flag. Active rows are NULL; a deleted company keeps its
    #: history so it can be restored from the recycle bin.
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None, index=True,
    )

    #: Bumped whenever facts change, so cache keys invalidate atomically.
    data_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    facts: Mapped[list["FinancialFact"]] = relationship(
        back_populates="company", cascade="all, delete-orphan"
    )
    versions: Mapped[list["CompanyVersion"]] = relationship(
        back_populates="company", cascade="all, delete-orphan",
        order_by="CompanyVersion.version",
    )

    __table_args__ = (
        UniqueConstraint("ticker", "exchange", name="uq_company_ticker_exchange"),
        # Phase 3 note: the constraint is (ticker, exchange), not ticker
        # alone, which is what allows a US listing to coexist with an Indian
        # one that happens to share a symbol.
        #
        # Case-insensitive guard over the same key: every creation path
        # upper-cases the ticker, but the database is the last line of
        # defence — this index turns a hypothetical "m&m" insert next to
        # "M&M" into a constraint violation instead of a near-duplicate
        # company that every case-sensitive lookup would miss.
        Index(
            "uq_companies_exchange_ticker_ci", "exchange", func.upper(ticker),
            unique=True,
        ),
        Index("ix_company_sector_mcap", "sector", "market_cap"),
        Index("ix_company_listing_status", "listing_status"),
        Index("ix_company_exchange", "exchange"),
    )

    @property
    def reporting_unit(self):
        """Currency and scale for this company's figures.

        Single source of truth for every label the platform prints. Callers
        must not infer a unit from the exchange themselves — that inference is
        exactly what produced twenty-four hardcoded "₹ cr" strings.
        """
        from app.domain.financials.reporting_unit import ReportingUnit, Scale

        try:
            scale = Scale(self.reporting_scale or "crore")
        except ValueError:
            # An unrecognised scale written by a future build. Falling back to
            # crore would silently misstate by a factor of ten, so prefer the
            # neutral unit scale, which at least does not rescale the number.
            scale = Scale.UNIT
        return ReportingUnit(currency=(self.currency or "INR").upper(), scale=scale)

    @property
    def is_us_listed(self) -> bool:
        return (self.exchange or "").upper() in {"NASDAQ", "NYSE", "AMEX"}

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Company {self.ticker} {self.name!r}>"


class FinancialFact(Base):
    """One canonical line item, for one company, for one fiscal year.

    Relational equivalent of a single `StoreVals` cell.
    """

    __tablename__ = "financial_facts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[str] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    fiscal_year: Mapped[int] = mapped_column(Integer, nullable=False)
    line_item: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[float | None] = mapped_column(Float)

    #: Maps to app.domain.financials.canonical.Precedence
    precedence: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    source: Mapped[str | None] = mapped_column(String(120))

    company: Mapped[Company] = relationship(back_populates="facts")

    __table_args__ = (
        UniqueConstraint(
            "company_id", "fiscal_year", "line_item", "precedence",
            name="uq_fact_company_year_item_precedence",
        ),
        # The hot path: load every fact for one company in a single scan.
        Index("ix_fact_lookup", "company_id", "fiscal_year", "line_item"),
    )


class CompanyVersion(Base):
    """An immutable snapshot of one company edit, for rollback and audit.

    Every create/update records a row capturing the changed fields (before and
    after). The latest row is the current state's provenance; rolling back to
    an earlier version re-applies that snapshot and records a new row. This
    satisfies both the "every edit logged" and "version history / rollback"
    requirements without a per-field audit table.
    """

    __tablename__ = "company_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[str] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    #: The actor who made the change.
    actor_id: Mapped[str | None] = mapped_column(String(36))
    actor_email: Mapped[str | None] = mapped_column(String(254))
    #: {field: {"from": ..., "to": ...}} for the changed fields.
    changes: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    #: Full editable-field state after this edit — the rollback target.
    snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    #: "create" | "update" | "import" | "merge" | "rollback" | "restore"
    change_type: Mapped[str] = mapped_column(String(16), default="update")
    summary: Mapped[str] = mapped_column(String(400), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False, index=True,
    )

    company: Mapped[Company] = relationship(back_populates="versions")

    __table_args__ = (
        UniqueConstraint("company_id", "version", name="uq_company_version"),
    )
