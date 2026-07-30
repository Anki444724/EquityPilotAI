"""Debt analysis service.

Three blocks from the specification: the instrument schedule, the maturity
ladder, and covenant headroom. Where the workbook had a fixed ten-row input
block, this reads the ``debt_instruments`` table, so a company may carry any
number of facilities.

Balance-sheet debt is always taken from the canonical statements; the
instrument schedule is reconciled *against* it and any gap is reported rather
than silently absorbed.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.domain.calc import safe_div
from app.domain.financials.statements import BalanceSheet, IncomeStatement
from app.models.analysis import DebtInstrument, RateType
from app.schemas.common import Flag, MetricRow, MetricSection, Unit


@dataclass(frozen=True, slots=True)
class Covenant:
    """A covenant test evaluated against the latest reported figures."""

    key: str
    label: str
    threshold: float
    actual: float | None
    #: "max" — actual must stay at or below; "min" — at or above.
    direction: str
    unit: str = Unit.MULTIPLE

    @property
    def compliant(self) -> bool | None:
        if self.actual is None:
            return None
        return self.actual <= self.threshold if self.direction == "max" else self.actual >= self.threshold

    @property
    def headroom(self) -> float | None:
        """Distance to breach, as a fraction of the threshold."""
        if self.actual is None or self.threshold == 0:
            return None
        return (
            (self.threshold - self.actual) / abs(self.threshold)
            if self.direction == "max"
            else (self.actual - self.threshold) / abs(self.threshold)
        )


#: Default covenant package. Overridable per company once facility terms are captured.
DEFAULT_COVENANTS = [
    ("net_debt_ebitda", "Net debt / EBITDA", 3.0, "max"),
    ("debt_equity", "Debt / equity", 2.0, "max"),
    ("interest_coverage", "Interest coverage (EBITDA / interest)", 3.0, "min"),
    ("dscr", "Debt service coverage ratio", 1.25, "min"),
    ("current_ratio", "Current ratio", 1.0, "min"),
]


class DebtService:
    def __init__(
        self,
        incomes: list[IncomeStatement],
        balances: list[BalanceSheet],
        instruments: list[DebtInstrument] | None = None,
    ) -> None:
        self.incomes = incomes
        self.balances = balances
        self.instruments = instruments or []
        self.n = len(incomes)

    # -------------------------------------------------------------- profile
    def profile_section(self) -> MetricSection:
        idx = range(self.n)
        return MetricSection(key="profile", title="A. Debt profile", rows=[
            MetricRow(key="short_term_borrowings", label="Short-term borrowings",
                      values=[self.balances[i].short_term_borrowings for i in idx], indent=1),
            MetricRow(key="current_maturities", label="Current maturities of LT debt",
                      values=[self.balances[i].current_maturities_ltd for i in idx], indent=1),
            MetricRow(key="long_term_borrowings", label="Long-term borrowings",
                      values=[self.balances[i].long_term_borrowings for i in idx], indent=1),
            MetricRow(key="gross_debt", label="Gross debt",
                      values=[self.balances[i].gross_debt for i in idx], is_subtotal=True),
            MetricRow(key="cash_equivalents", label="Less: cash & liquid investments",
                      values=[self.balances[i].cash_and_bank + self.balances[i].current_investments for i in idx], indent=1),
            MetricRow(key="net_debt", label="Net debt",
                      values=[self.balances[i].net_debt for i in idx], is_subtotal=True),
            MetricRow(key="finance_costs", label="Finance costs",
                      values=[self.incomes[i].finance_costs for i in idx]),
            MetricRow(key="implied_cost_of_debt", label="Implied cost of debt", unit=Unit.PERCENT,
                      values=[self._implied_cost(i) for i in idx],
                      note="Finance costs over average gross debt."),
        ])

    def _implied_cost(self, i: int) -> float | None:
        closing = self.balances[i].gross_debt
        opening = self.balances[i - 1].gross_debt if i > 0 else None
        avg = closing if opening is None else (closing + opening) / 2
        return safe_div(self.incomes[i].finance_costs, avg)

    def leverage_section(self) -> MetricSection:
        idx = range(self.n)
        return MetricSection(key="leverage", title="B. Leverage & coverage", rows=[
            MetricRow(key="net_debt_ebitda", label="Net debt / EBITDA", unit=Unit.MULTIPLE,
                      values=[safe_div(self.balances[i].net_debt, self.incomes[i].ebitda) for i in idx]),
            MetricRow(key="gross_debt_ebitda", label="Gross debt / EBITDA", unit=Unit.MULTIPLE,
                      values=[safe_div(self.balances[i].gross_debt, self.incomes[i].ebitda) for i in idx]),
            MetricRow(key="debt_equity", label="Debt / equity", unit=Unit.MULTIPLE,
                      values=[safe_div(self.balances[i].gross_debt, self.balances[i].shareholders_equity) for i in idx]),
            MetricRow(key="net_debt_equity", label="Net debt / equity", unit=Unit.MULTIPLE,
                      values=[safe_div(self.balances[i].net_debt, self.balances[i].shareholders_equity) for i in idx]),
            MetricRow(key="interest_coverage", label="EBITDA interest coverage", unit=Unit.MULTIPLE,
                      values=[safe_div(self.incomes[i].ebitda, self.incomes[i].finance_costs) for i in idx]),
            MetricRow(key="ebit_interest_coverage", label="EBIT interest coverage", unit=Unit.MULTIPLE,
                      values=[safe_div(self.incomes[i].ebit, self.incomes[i].finance_costs) for i in idx]),
            MetricRow(key="dscr", label="Debt service coverage ratio", unit=Unit.MULTIPLE,
                      values=[safe_div(self.incomes[i].ebitda,
                                       self.incomes[i].finance_costs + self.balances[i].current_maturities_ltd)
                              for i in idx]),
            MetricRow(key="debt_to_assets", label="Debt / total assets", unit=Unit.PERCENT,
                      values=[safe_div(self.balances[i].gross_debt, self.balances[i].total_assets) for i in idx]),
        ])

    # ------------------------------------------------------- instrument view
    def instrument_schedule(self) -> list[dict]:
        latest = max((d.fiscal_year for d in self.instruments), default=None)
        rows = [d for d in self.instruments if d.fiscal_year == latest]
        total = sum(d.amount for d in rows)
        return [
            {
                "instrument": d.instrument,
                "lender": d.lender,
                "security": d.security,
                "rate_type": d.rate_type,
                "amount": d.amount,
                "share_of_debt": safe_div(d.amount, total),
                "interest_rate": d.interest_rate,
                "maturity_year": d.maturity_year,
                "currency": d.currency,
            }
            for d in sorted(rows, key=lambda x: -x.amount)
        ]

    def blended_rate(self) -> float | None:
        """Amount-weighted average interest rate across the schedule."""
        latest = max((d.fiscal_year for d in self.instruments), default=None)
        rows = [d for d in self.instruments if d.fiscal_year == latest and d.interest_rate is not None]
        weight = sum(d.amount for d in rows)
        if not weight:
            return None
        return sum(d.amount * (d.interest_rate or 0) for d in rows) / weight

    def floating_share(self) -> float | None:
        latest = max((d.fiscal_year for d in self.instruments), default=None)
        rows = [d for d in self.instruments if d.fiscal_year == latest]
        total = sum(d.amount for d in rows)
        if not total:
            return None
        return sum(d.amount for d in rows if d.rate_type == RateType.FLOATING) / total

    def foreign_currency_share(self) -> float | None:
        latest = max((d.fiscal_year for d in self.instruments), default=None)
        rows = [d for d in self.instruments if d.fiscal_year == latest]
        total = sum(d.amount for d in rows)
        if not total:
            return None
        return sum(d.amount for d in rows if d.currency != "INR") / total

    def maturity_ladder(self) -> list[dict]:
        """Scheduled repayments by calendar year, with coverage against EBITDA."""
        latest_fy = max((d.fiscal_year for d in self.instruments), default=None)
        rows = [d for d in self.instruments if d.fiscal_year == latest_fy and d.maturity_year]
        if not rows:
            return []
        total = sum(d.amount for d in rows)
        ebitda = self.incomes[-1].ebitda if self.incomes else None

        buckets: dict[int, float] = {}
        for d in rows:
            buckets[d.maturity_year] = buckets.get(d.maturity_year, 0.0) + d.amount

        ladder, cumulative = [], 0.0
        for year in sorted(buckets):
            amount = buckets[year]
            cumulative += amount
            ladder.append({
                "year": year,
                "amount": amount,
                "share_of_debt": safe_div(amount, total),
                "cumulative": cumulative,
                "ebitda_coverage": safe_div(ebitda, amount),
            })
        return ladder

    def reconciliation(self) -> dict:
        """Instrument schedule vs balance-sheet gross debt."""
        latest_fy = max((d.fiscal_year for d in self.instruments), default=None)
        scheduled = sum(d.amount for d in self.instruments if d.fiscal_year == latest_fy)
        reported = self.balances[-1].gross_debt if self.balances else 0.0
        return {
            "instrument_total": scheduled,
            "balance_sheet_gross_debt": reported,
            "difference": scheduled - reported,
            "reconciled": abs(scheduled - reported) < 0.01 or scheduled == 0,
        }

    # ------------------------------------------------------------- covenants
    def covenants(self) -> list[Covenant]:
        if not self.incomes:
            return []
        inc, bal = self.incomes[-1], self.balances[-1]
        actuals = {
            "net_debt_ebitda": safe_div(bal.net_debt, inc.ebitda),
            "debt_equity": safe_div(bal.gross_debt, bal.shareholders_equity),
            "interest_coverage": safe_div(inc.ebitda, inc.finance_costs),
            "dscr": safe_div(inc.ebitda, inc.finance_costs + bal.current_maturities_ltd),
            "current_ratio": safe_div(bal.total_current_assets, bal.total_current_liabilities),
        }
        return [
            Covenant(key=k, label=lbl, threshold=t, direction=d, actual=actuals.get(k))
            for k, lbl, t, d in DEFAULT_COVENANTS
        ]

    def flags(self) -> list[Flag]:
        out = [
            Flag(
                key=f"covenant_{c.key}",
                label=f"{c.label} covenant breached",
                triggered=c.compliant is False,
                severity="alert" if c.compliant is False else "info",
                detail=(
                    f"actual {c.actual:.2f} vs {c.direction} {c.threshold:.2f}"
                    if c.actual is not None else "not measurable"
                ),
            )
            for c in self.covenants()
        ]
        rec = self.reconciliation()
        if not rec["reconciled"]:
            out.append(Flag(
                key="debt_reconciliation",
                label="Instrument schedule does not reconcile to balance-sheet debt",
                triggered=True, severity="warn",
                detail=f"difference of {rec['difference']:,.2f} ₹ cr",
            ))
        return out

    def all_sections(self) -> list[MetricSection]:
        return [self.profile_section(), self.leverage_section()]
