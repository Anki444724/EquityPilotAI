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


class Company(Base):
    """A listed company — the root of the Universal Company Engine."""

    __tablename__ = "companies"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    ticker: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    exchange: Mapped[str] = mapped_column(String(16), default=Exchange.NSE)
    isin: Mapped[str | None] = mapped_column(String(16), unique=True)

    sector: Mapped[str | None] = mapped_column(String(120), index=True)
    industry: Mapped[str | None] = mapped_column(String(120), index=True)

    market_cap: Mapped[float | None] = mapped_column(Float)          # ₹ crore
    current_price: Mapped[float | None] = mapped_column(Float)       # ₹  (workbook CMP)
    shares_outstanding: Mapped[float | None] = mapped_column(Float)  # crore

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
        Index("ix_company_sector_mcap", "sector", "market_cap"),
    )

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
