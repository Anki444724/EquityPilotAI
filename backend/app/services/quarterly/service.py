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

    def segments(self, company_id: str) -> dict:
        """Return segment data attached to quarterly results.

        Pulls from document_facts when richer quarterly segment data has been
        extracted during document intelligence processing.
        """
        from sqlalchemy import select
        from app.models.document import DocumentFact, Document

        try:
            facts = list(self.db.execute(
                select(DocumentFact)
                .join(Document, Document.id == DocumentFact.document_id)
                .where(
                    Document.company_id == company_id,
                    Document.doc_type.in_(["quarterly_result", "quarterly_results"]),
                    DocumentFact.key.ilike("%segment%")
                )
                .limit(30)
            ).scalars())
        except Exception:
            facts = []

        if facts:
            return {
                "available": True,
                "reason": "Segment data extracted from quarterly filings via document intelligence.",
                "data": [
                    {"label": getattr(f, "label", None) or getattr(f, "key", ""),
                     "value": getattr(f, "value", None),
                     "unit": getattr(f, "unit", "") or ""}
                    for f in facts
                ]
            }

        return {
            "available": False,
            "reason": "No segment data found for quarterly periods. "
                      "Most Indian companies provide detailed segment reporting only in annual reports.",
            "data": []
        }

    def ttm_summary(self, company_id: str) -> dict:
        """Explain why TTM is not auto-generated from quarters (Indian reporting reality)."""
        return {
            "generated": False,
            "reason": "TTM is deliberately not summed from four quarters. "
                      "Indian Q4 is routinely a balancing figure that absorbs audit adjustments. "
                      "Four quarters frequently do not equal the audited annual result.",
            "recommendation": "Use audited annual statements for full-year views. "
                              "Use quarterly rows for QoQ and YoY trend analysis only."
        }

    def full_quarterly_with_segments(self, company_id: str) -> list[dict]:
        """Return quarterly rows + any attached segment data.

        This strengthens the quarterly pipeline for production use.
        """
        rows = self.rows(company_id)
        seg = self.segments(company_id)

        result = []
        for r in rows:
            item = {
                "fiscal_year": r.fiscal_year,
                "quarter": r.quarter,
                "label": r.label,
                "revenue": r.revenue,
                "net_profit": r.net_profit,
                "revenue_qoq": r.revenue_qoq,
                "revenue_yoy": r.revenue_yoy,
                "profit_qoq": r.profit_qoq,
                "profit_yoy": r.profit_yoy,
                "segments": seg.get("data", []) if seg.get("available") else []
            }
            result.append(item)
        return result
