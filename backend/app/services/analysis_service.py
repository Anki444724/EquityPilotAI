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
import structlog

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


log = structlog.get_logger(__name__)

class AnalysisService:
    """Builds every Module 2 view from a single resolution."""

    def __init__(self, db: Session, company: Company, financials: CanonicalFinancials) -> None:
        self.db = db
        self.company = company
        self.fin = financials
        self._statements = FinancialStatementsService(financials)

    # ------------------------------------------------------------ factories
    @classmethod
    def for_ticker(
        cls, db: Session, ticker: str, *, provision: bool = True,
    ) -> "AnalysisService | None":
        """Load a company for analysis, provisioning a US listing if needed.

        Phase 3. Every analysis path starts here, which makes it the one place
        US support can be added without threading a flag through the API,
        the AI service and the report orchestrator. A US ticker with no
        company record is fetched from FMP and written to the database on
        first request; thereafter it behaves exactly like an Indian one.

        `provision=False` disables that, for callers that must not perform
        network I/O — the seed scripts and most tests.
        """
        svc = CompanyService(db)
        company = svc.get_by_ticker(ticker) or svc.get(ticker)
        if company is None and provision:
            company = cls._provision_us(db, ticker)
        if company is None:
            return None
        return cls(db, company, svc.load_financials(company.id))

    @staticmethod
    def _provision_us(db: Session, ticker: str):
        """Create a US company on first request. Returns None if not US."""
        from app.data.providers.symbols import resolve

        try:
            if not resolve(ticker).is_us:
                return None
        except Exception:  # noqa: BLE001 — an unresolvable symbol is simply unknown
            return None

        from app.services.us_pipeline.provisioning import (
            ProvisioningError, USCompanyProvisioner,
        )

        try:
            result = USCompanyProvisioner(db).provision(ticker)
        except ProvisioningError as exc:
            # Genuinely unknown to the providers. A 404 is the right answer,
            # so return None and let the caller raise it.
            log.info("us provisioning declined", ticker=ticker,
                     reason=str(exc)[:160])
            return None
        except Exception:  # noqa: BLE001 — a provider outage must not 500
            log.exception("us provisioning failed", ticker=ticker)
            return None

        from app.models.company import Company

        return db.get(Company, result.company_id)

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
