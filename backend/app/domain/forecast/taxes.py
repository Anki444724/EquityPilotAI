"""Tax projection.

A single effective-rate driver, applied consistently to PBT for the income
statement and to EBIT for NOPAT in the FCFF build. Keeping one rate in one
place is what stops the two calculations diverging.
"""
from __future__ import annotations

from dataclasses import dataclass

from .assumptions import ForecastAssumptions


@dataclass(frozen=True, slots=True)
class TaxYear:
    period: int
    fiscal_year: int
    effective_rate: float
    pbt: float
    tax_expense: float
    pat: float
    #: Tax that would be paid on EBIT with no financing effects.
    tax_on_ebit: float
    nopat: float
    #: Value of the interest deduction.
    interest_tax_shield: float


class TaxForecast:
    def __init__(self, assumptions: ForecastAssumptions) -> None:
        self.a = assumptions

    def rate_at(self, period: int) -> float:
        return self.a.effective_tax_rate.at(period)

    def compute(
        self, period: int, fiscal_year: int, pbt: float, ebit: float, interest: float
    ) -> TaxYear:
        rate = self.rate_at(period)
        tax = pbt * rate
        return TaxYear(
            period=period,
            fiscal_year=fiscal_year,
            effective_rate=rate,
            pbt=pbt,
            tax_expense=tax,
            pat=pbt - tax,
            tax_on_ebit=ebit * rate,
            nopat=ebit * (1 - rate),
            interest_tax_shield=interest * rate,
        )
