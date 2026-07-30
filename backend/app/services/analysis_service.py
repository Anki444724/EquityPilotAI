"""Analysis orchestrator.

Loads a company's context ONCE and hands the same computed statement objects to
every downstream service. This is the Module 1 single-resolution rule extended
across Module 2: statements are built once per request, then reused by ratios,
working capital, capex and debt.

Without this, each endpoint would rebuild the statements and the "each
calculation exists only once" guarantee would quietly fail.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.financials.canonical import CanonicalFinancials
from app.domain.financials.statements import (
    BalanceSheet, CashFlowStatement, IncomeStatement,
)
from app.models.analysis import DebtInstrument, ShareholdingSnapshot
from app.models.company import Company
from app.schemas.common import CompanyRef, PeriodMeta, Unit
from app.services.capex.service import CapexService
from app.services.company_service import CompanyService
from app.services.debt.service import DebtService
from app.services.financials.service import FinancialStatementsService
from app.services.ratios.service import RatioService
from app.services.shareholding.service import ShareholdingService
from app.services.working_capital.service import WorkingCapitalService


def fiscal_label(year: int) -> str:
    return f"FY{str(year)[-2:]}"


@dataclass(slots=True)
class AnalysisContext:
    """One company's fully resolved analysis inputs."""

    company: Company
    financials: CanonicalFinancials


class AnalysisService:
    """Builds every Module 2 view from a single resolution."""

    def __init__(self, db: Session, company: Company, financials: CanonicalFinancials) -> None:
        self.db = db
        self.company = company
        self.fin = financials
        self._statements = FinancialStatementsService(financials)

    # ------------------------------------------------------------ factories
    @classmethod
    def for_ticker(cls, db: Session, ticker: str) -> "AnalysisService | None":
        svc = CompanyService(db)
        company = svc.get_by_ticker(ticker) or svc.get(ticker)
        if company is None:
            return None
        return cls(db, company, svc.load_financials(company.id))

    # ----------------------------------------------- computed once, reused
    @cached_property
    def incomes(self) -> list[IncomeStatement]:
        return self._statements.income_statements()

    @cached_property
    def balances(self) -> list[BalanceSheet]:
        return self._statements.balance_sheets()

    @cached_property
    def cash_flows(self) -> list[CashFlowStatement]:
        return self._statements.cash_flows()

    @cached_property
    def debt_instruments(self) -> list[DebtInstrument]:
        return list(
            self.db.execute(
                select(DebtInstrument).where(DebtInstrument.company_id == self.company.id)
            ).scalars().all()
        )

    @cached_property
    def shareholding_snapshots(self) -> list[ShareholdingSnapshot]:
        return list(
            self.db.execute(
                select(ShareholdingSnapshot).where(
                    ShareholdingSnapshot.company_id == self.company.id
                )
            ).scalars().all()
        )

    @cached_property
    def implied_cost_of_debt(self) -> float | None:
        """Derived from reported finance costs, not assumed."""
        return DebtService(self.incomes, self.balances)._implied_cost(len(self.incomes) - 1) \
            if self.incomes else None

    # ------------------------------------------------------------- metadata
    @property
    def has_data(self) -> bool:
        return self.fin.has_data()

    def company_ref(self) -> CompanyRef:
        return CompanyRef.model_validate(self.company)

    def periods(self, unit: str = Unit.CRORE) -> PeriodMeta:
        years = list(self.fin.fiscal_years)
        return PeriodMeta(
            fiscal_years=years,
            labels=[fiscal_label(y) for y in years],
            latest_fiscal_year=years[-1] if years else None,
            unit=unit,
        )

    # ------------------------------------------------------------- services
    @property
    def statements(self) -> FinancialStatementsService:
        return self._statements

    def ratios(self, wacc: float | None = None) -> RatioService:
        return RatioService(self.incomes, self.balances, self.cash_flows, wacc)

    def working_capital(self) -> WorkingCapitalService:
        return WorkingCapitalService(self.incomes, self.balances, self.implied_cost_of_debt)

    def capex(self) -> CapexService:
        return CapexService(self.incomes, self.balances, self.cash_flows)

    def debt(self) -> DebtService:
        return DebtService(self.incomes, self.balances, self.debt_instruments)

    def shareholding(self) -> ShareholdingService:
        return ShareholdingService(self.shareholding_snapshots, self.company.market_cap)
