"""Models for data that is *not* derivable from the canonical statements.

Debt instruments and shareholding patterns are separately disclosed facts, not
computed values. In the workbook they were manual input blocks; here they are
first-class tables so they can be versioned, audited and queried.

Everything that CAN be derived from the 54 canonical line items (statements,
ratios, working capital, capex) is computed on demand and deliberately NOT
stored — storing derived values is how two sources of truth appear.
"""
from __future__ import annotations

from enum import StrEnum

from sqlalchemy import (
    Date, Float, ForeignKey, Index, Integer, String, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.company import Company


class DebtSecurity(StrEnum):
    SECURED = "Secured"
    UNSECURED = "Unsecured"


class RateType(StrEnum):
    FIXED = "Fixed"
    FLOATING = "Floating"


class DebtInstrument(Base):
    """One borrowing facility in the debt schedule."""

    __tablename__ = "debt_instruments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[str] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    fiscal_year: Mapped[int] = mapped_column(Integer, nullable=False)

    instrument: Mapped[str] = mapped_column(String(160), nullable=False)
    lender: Mapped[str | None] = mapped_column(String(160))
    security: Mapped[str] = mapped_column(String(24), default=DebtSecurity.SECURED)
    rate_type: Mapped[str] = mapped_column(String(24), default=RateType.FIXED)

    #: ₹ crore outstanding at the reporting date.
    amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    #: Annual coupon/interest rate as a fraction (0.085 == 8.5%).
    interest_rate: Mapped[float | None] = mapped_column(Float)
    #: Calendar year in which the facility is scheduled to be repaid.
    maturity_year: Mapped[int | None] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(8), default="INR")

    company: Mapped[Company] = relationship()

    __table_args__ = (
        Index("ix_debt_company_year", "company_id", "fiscal_year"),
    )


class CreditRating(Base):
    """Long-term credit rating as published by an agency."""

    __tablename__ = "credit_ratings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[str] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    agency: Mapped[str] = mapped_column(String(80), nullable=False)
    rating: Mapped[str] = mapped_column(String(24), nullable=False)
    outlook: Mapped[str | None] = mapped_column(String(40))
    action_date: Mapped[object | None] = mapped_column(Date)
    instrument_class: Mapped[str | None] = mapped_column(String(80))

    company: Mapped[Company] = relationship()

    __table_args__ = (Index("ix_rating_company", "company_id"),)


class ShareholdingSnapshot(Base):
    """Quarterly shareholding pattern.

    Percentages are stored as fractions (0.521 == 52.1%) so no unit conversion
    is ever needed between layers.
    """

    __tablename__ = "shareholding_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[str] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    fiscal_year: Mapped[int] = mapped_column(Integer, nullable=False)
    quarter: Mapped[int] = mapped_column(Integer, nullable=False)  # 1..4

    promoter_indian: Mapped[float] = mapped_column(Float, default=0.0)
    promoter_foreign: Mapped[float] = mapped_column(Float, default=0.0)
    fii_fpi: Mapped[float] = mapped_column(Float, default=0.0)
    mutual_funds: Mapped[float] = mapped_column(Float, default=0.0)
    insurance: Mapped[float] = mapped_column(Float, default=0.0)
    banks_fis_aif: Mapped[float] = mapped_column(Float, default=0.0)
    government: Mapped[float] = mapped_column(Float, default=0.0)
    others_custodians: Mapped[float] = mapped_column(Float, default=0.0)

    #: Fraction of the PROMOTER holding that is pledged (not of total equity).
    promoter_pledged: Mapped[float] = mapped_column(Float, default=0.0)

    company: Mapped[Company] = relationship()

    __table_args__ = (
        UniqueConstraint("company_id", "fiscal_year", "quarter", name="uq_shareholding_period"),
        Index("ix_shareholding_company_period", "company_id", "fiscal_year", "quarter"),
    )

    @property
    def period_label(self) -> str:
        return f"Q{self.quarter} FY{str(self.fiscal_year)[-2:]}"
