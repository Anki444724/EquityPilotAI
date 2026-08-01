"""Company and canonical financial fact models.

`financial_facts` is the relational form of the workbook's `StoreVals` grid
('0A Data Import'!$AB$241:$FC$294). The workbook was limited to 12 company
slots by fixed spreadsheet geometry; Postgres has no such limit, so the
Universal Company Engine becomes genuinely unbounded here.
"""
from __future__ import annotations

from enum import StrEnum

from sqlalchemy import (
    Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


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

    #: Bumped whenever facts change, so cache keys invalidate atomically.
    data_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    facts: Mapped[list["FinancialFact"]] = relationship(
        back_populates="company", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("ticker", "exchange", name="uq_company_ticker_exchange"),
        # Phase 3 note: the constraint is (ticker, exchange), not ticker
        # alone, which is what allows a US listing to coexist with an Indian
        # one that happens to share a symbol.
        Index("ix_company_sector_mcap", "sector", "market_cap"),
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
