"""Quarterly results — reading, sequencing and period-on-period comparison.

The calculations live here and only here. The API serialises what this
returns and the frontend renders it; neither computes a growth rate.

Deliberately conservative about what it derives. QoQ and YoY are computed
because they are arithmetic on two disclosed figures. A "trailing twelve
months" is NOT summed from four quarters: Indian Q4 is frequently a balancing
figure absorbing audit adjustments, so four quarters need not equal the
audited annual result, and presenting a TTM built that way beside the annual
statements would show two different truths for the same period.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.analysis import QuarterlyResult


@dataclass(slots=True)
class QuarterRow:
    """One quarter, with its comparisons already resolved."""

    fiscal_year: int
    quarter: int
    label: str
    revenue: float | None
    expenses: float | None
    operating_profit: float | None
    operating_margin: float | None
    other_income: float | None
    interest: float | None
    depreciation: float | None
    profit_before_tax: float | None
    tax_rate: float | None
    net_profit: float | None
    eps: float | None
    #: Sequential growth against the immediately preceding quarter.
    revenue_qoq: float | None = None
    profit_qoq: float | None = None
    #: Growth against the same quarter a year earlier — the comparison that
    #: matters for a seasonal business, where QoQ mostly measures the season.
    revenue_yoy: float | None = None
    profit_yoy: float | None = None
    source: str | None = None


def _growth(current: float | None, previous: float | None) -> float | None:
    """Fractional growth, or None where it would be meaningless.

    A sign change makes percentage growth uninterpretable — a swing from
    −50 to +25 is not "150% growth" in any sense a reader would accept — so
    it returns None rather than a number that invites a wrong conclusion.
    """
    if current is None or previous is None:
        return None
    if previous == 0:
        return None
    if (previous < 0) != (current < 0):
        return None
    return (current - previous) / abs(previous)


class QuarterlyService:
    """Reads stored quarters and derives their comparisons."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def rows(self, company_id: str) -> list[QuarterRow]:
        """Every stored quarter for a company, oldest first."""
        records = list(self.db.scalars(
            select(QuarterlyResult)
            .where(QuarterlyResult.company_id == company_id)
            .order_by(QuarterlyResult.fiscal_year, QuarterlyResult.quarter)
        ))

        by_period = {(r.fiscal_year, r.quarter): r for r in records}
        rows: list[QuarterRow] = []

        for index, record in enumerate(records):
            previous = records[index - 1] if index else None
            # Keyed lookup rather than `records[index - 4]`: a company with a
            # gap in its history would otherwise compare against the wrong
            # quarter and silently report a nonsense YoY.
            year_ago = by_period.get((record.fiscal_year - 1, record.quarter))

            rows.append(QuarterRow(
                fiscal_year=record.fiscal_year,
                quarter=record.quarter,
                label=record.period_label,
                revenue=record.revenue,
                expenses=record.expenses,
                operating_profit=record.operating_profit,
                operating_margin=record.operating_margin,
                other_income=record.other_income,
                interest=record.interest,
                depreciation=record.depreciation,
                profit_before_tax=record.profit_before_tax,
                tax_rate=record.tax_rate,
                net_profit=record.net_profit,
                eps=record.eps,
                revenue_qoq=_growth(record.revenue,
                                    previous.revenue if previous else None),
                profit_qoq=_growth(record.net_profit,
                                   previous.net_profit if previous else None),
                revenue_yoy=_growth(record.revenue,
                                    year_ago.revenue if year_ago else None),
                profit_yoy=_growth(record.net_profit,
                                   year_ago.net_profit if year_ago else None),
                source=record.source,
            ))

        return rows

    def latest(self, company_id: str) -> QuarterRow | None:
        rows = self.rows(company_id)
        return rows[-1] if rows else None

    def has_data(self, company_id: str) -> bool:
        return self.db.scalar(
            select(QuarterlyResult.id)
            .where(QuarterlyResult.company_id == company_id)
            .limit(1)
        ) is not None
