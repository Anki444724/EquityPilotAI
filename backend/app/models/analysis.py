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


class QuarterlyResult(Base):
    """One reported quarter of results, as filed.

    Stored rather than derived, for the same reason `ShareholdingSnapshot` is:
    a quarterly result is a **separately disclosed fact**, not something the
    annual statements can be decomposed into. Four quarters do not reconcile
    to the annual figure in Indian reporting — the Q4 column is routinely a
    balancing figure that absorbs audit adjustments — so deriving quarters
    from an annual series would be inventing numbers.

    Values are in ₹ crore, consistent with every other stored figure, and
    margins are fractions (0.145 == 14.5%) so no layer converts units.

    Deliberately narrow: this holds what an Indian quarterly filing actually
    reports at the summary level. It is not a second income statement, and it
    does not attempt a balance sheet or cash flow, because Indian companies do
    not publish those quarterly in a comparable form.
    """

    __tablename__ = "quarterly_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[str] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    #: April–March fiscal year the quarter closes within, so Sep 2024 is
    #: FY2025 Q2 — matching how the rest of the platform counts years.
    fiscal_year: Mapped[int] = mapped_column(Integer, nullable=False)
    quarter: Mapped[int] = mapped_column(Integer, nullable=False)  # 1..4

    revenue: Mapped[float | None] = mapped_column(Float)
    expenses: Mapped[float | None] = mapped_column(Float)
    operating_profit: Mapped[float | None] = mapped_column(Float)
    #: Fraction, not percent.
    operating_margin: Mapped[float | None] = mapped_column(Float)
    other_income: Mapped[float | None] = mapped_column(Float)
    interest: Mapped[float | None] = mapped_column(Float)
    depreciation: Mapped[float | None] = mapped_column(Float)
    profit_before_tax: Mapped[float | None] = mapped_column(Float)
    #: Fraction, not percent.
    tax_rate: Mapped[float | None] = mapped_column(Float)
    net_profit: Mapped[float | None] = mapped_column(Float)
    eps: Mapped[float | None] = mapped_column(Float)

    #: Provenance travels with the number, as everywhere else.
    source: Mapped[str | None] = mapped_column(String(120))

    company: Mapped[Company] = relationship()

    __table_args__ = (
        UniqueConstraint(
            "company_id", "fiscal_year", "quarter", name="uq_quarterly_period",
        ),
        Index("ix_quarterly_company_period", "company_id", "fiscal_year", "quarter"),
    )

    @property
    def period_label(self) -> str:
        return f"Q{self.quarter} FY{str(self.fiscal_year)[-2:]}"

    @property
    def has_data(self) -> bool:
        """True when the quarter carries at least one reported figure.

        Guards the no-placeholder rule: a row that would answer False here
        should never have been written.
        """
        return any(
            value is not None
            for value in (self.revenue, self.operating_profit,
                          self.profit_before_tax, self.net_profit)
        )
