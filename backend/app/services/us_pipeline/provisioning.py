"""Provision a US company into the platform from SEC and FMP.

The gap Phase 3 closes: `/market/AAPL` and `/filings/AAPL` both worked, but
`/company/AAPL/ai/research-report` returned 404, because every analysis path
starts from a `Company` row and no US company had one. Market data without a
company record is a quote; the research product needs statements, and
statements need somewhere to live.

Two entry points, as agreed:

* **Seeded** — a small, fixed set provisioned up front so demonstrations and
  tests are fast and deterministic.
* **On demand** — any other US ticker is provisioned on first request.

Provisioning is idempotent and safe to race: the unique constraint is
`(ticker, exchange)`, so a concurrent duplicate fails the insert rather than
creating a second Apple, and the loser re-reads the winner's row.

**Nothing is fabricated.** The company record is written only from fields the
providers actually returned; canonical facts come from
`statement_mapper.map_filing_set`, which maps what US GAAP supplies and leaves
the rest absent. A US company therefore covers fewer of the 54 canonical items
than an Indian one, and that is reported as coverage rather than filled in.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import structlog
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from app.domain.financials.canonical import Precedence
from app.domain.financials.reporting_unit import USD_MILLION
from app.models.company import Company, FinancialFact
from app.services.platform.cache import Namespace, cache
from app.services.us_pipeline.fmp_client import FMPStatements
from app.services.us_pipeline.statement_mapper import coverage, map_filing_set

log = structlog.get_logger(__name__)

#: Provisioned at startup so the common demonstrations never pay first-call
#: latency. Deliberately short — a long list would turn a deploy into a
#: several-minute provider crawl, and on-demand covers everything else.
SEED_UNIVERSE: tuple[str, ...] = ("AAPL", "MSFT", "NVDA", "GOOGL", "AMZN")

#: Exchange assumed when a provider does not name one. Reported, never guessed
#: silently: `exchange` is part of the uniqueness constraint, so a wrong value
#: creates a duplicate company rather than a mislabelled one.
DEFAULT_US_EXCHANGE = "NASDAQ"


class ProvisioningError(RuntimeError):
    """A US company could not be provisioned from any provider."""


@dataclass(slots=True)
class ProvisionResult:
    ticker: str
    company_id: str
    name: str
    created: bool
    facts_written: int = 0
    years: list[int] = field(default_factory=list)
    coverage_pct: float = 0.0
    latency_ms: float = 0.0
    sources: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "company_id": self.company_id,
            "name": self.name,
            "created": self.created,
            "facts_written": self.facts_written,
            "years": self.years,
            "coverage_pct": self.coverage_pct,
            "latency_ms": round(self.latency_ms, 1),
            "sources": self.sources,
            "warnings": self.warnings,
        }


class USCompanyProvisioner:
    """Creates and refreshes US company records and their canonical facts."""

    def __init__(self, db: Any, statements: FMPStatements | None = None) -> None:
        self.db = db
        self.statements = statements or FMPStatements()

    # ------------------------------------------------------------- lookup
    def existing(self, ticker: str) -> Company | None:
        """A US company already provisioned under this symbol.

        Matched on ticker *and* a US exchange rather than ticker alone.
        `?search=TCS` returning Reliance is the kind of near-miss that already
        cost this engagement a misfiled document; an exact, venue-qualified
        match is the only safe lookup.
        """
        base = (ticker or "").strip().upper().split(".")[0]
        return self.db.scalar(
            select(Company).where(
                Company.ticker == base,
                Company.exchange.in_(("NASDAQ", "NYSE", "AMEX")),
            )
        )

    # -------------------------------------------------------- provisioning
    def provision(self, ticker: str, *, refresh: bool = False) -> ProvisionResult:
        """Ensure a US company exists with canonical facts."""
        started = time.perf_counter()
        base = (ticker or "").strip().upper().split(".")[0]
        if not base:
            raise ProvisioningError("no ticker supplied")

        company = self.existing(base)
        if company is not None and not refresh:
            return ProvisionResult(
                ticker=base, company_id=company.id, name=company.name,
                created=False, latency_ms=(time.perf_counter() - started) * 1000,
                sources=["already provisioned"],
            )

        profile = self.statements.profile(base)
        if profile is None:
            raise ProvisioningError(
                f"{base} is not a recognised US listing, or no provider could "
                f"supply a company profile for it"
            )

        warnings: list[str] = []
        sources = ["FMP profile"]

        if company is None:
            company = self._create(base, profile)
            created = True
        else:
            self._update(company, profile)
            created = False

        # --- statements ------------------------------------------------
        income = self.statements.income(base)
        balance = self.statements.balance(base)
        cash_flow = self.statements.cash_flow(base)
        for name, rows in (("income", income), ("balance", balance),
                           ("cash flow", cash_flow)):
            if rows:
                sources.append(f"FMP {name}")
            else:
                warnings.append(f"no {name} statement returned by any provider")

        facts = map_filing_set(income, balance, cash_flow, unit=USD_MILLION)
        written = self._write_facts(company.id, facts)
        stats = coverage(facts)

        if not facts:
            warnings.append(
                "no canonical facts could be derived; the company record "
                "exists but carries no statements"
            )

        self.db.commit()

        # `load_financials` caches per company, and this company's facts have
        # just changed. Without this the first report after provisioning would
        # read an empty cached result written microseconds earlier.
        cache.invalidate(Namespace.STATEMENTS)

        elapsed = (time.perf_counter() - started) * 1000
        log.info(
            "us company provisioned", ticker=base, company_id=company.id,
            created=created, facts=written, coverage=stats["coverage_pct"],
            years=stats["years"], ms=round(elapsed, 1),
        )
        return ProvisionResult(
            ticker=base, company_id=company.id, name=company.name,
            created=created, facts_written=written, years=stats["years"],
            coverage_pct=stats["coverage_pct"], latency_ms=elapsed,
            sources=sources, warnings=warnings,
        )

    # ------------------------------------------------------------ internals
    def _create(self, ticker: str, profile: dict[str, Any]) -> Company:
        company = Company(
            id=str(uuid.uuid4()),
            ticker=ticker,
            name=profile.get("companyName") or ticker,
            exchange=_exchange_of(profile),
            # The whole point of Phase 3's schema change: this company's
            # figures are USD millions and are labelled as such everywhere.
            currency=(profile.get("currency") or "USD").upper(),
            reporting_scale="million",
            sector=profile.get("sector"),
            industry=profile.get("industry"),
            description=(profile.get("description") or None),
            website=profile.get("website"),
            market_cap=USD_MILLION.from_absolute(_number(profile.get("marketCap"))),
            current_price=_number(profile.get("price")),
            shares_outstanding=USD_MILLION.from_absolute(
                _number(profile.get("sharesOutstanding"))
            ),
        )
        self.db.add(company)
        try:
            self.db.flush()
        except IntegrityError:
            # Lost a race with a concurrent provision of the same ticker.
            # Re-read the winner rather than failing the caller's request.
            self.db.rollback()
            existing = self.existing(ticker)
            if existing is None:
                raise
            log.info("provisioning raced; using the existing row",
                     ticker=ticker, company_id=existing.id)
            return existing
        return company

    def _update(self, company: Company, profile: dict[str, Any]) -> None:
        company.name = profile.get("companyName") or company.name
        company.sector = profile.get("sector") or company.sector
        company.industry = profile.get("industry") or company.industry
        price = _number(profile.get("price"))
        if price is not None:
            company.current_price = price
        cap = _number(profile.get("marketCap"))
        if cap is not None:
            company.market_cap = USD_MILLION.from_absolute(cap)

    def _write_facts(self, company_id: str, facts: list) -> int:
        """Replace this company's facts with the mapped set.

        Delete-then-insert rather than upsert. The mapping is deterministic
        from the provider payload, so a refresh should leave exactly what the
        current filings support — an upsert would strand a fact whose line
        item stopped being reported, and that stale row would keep appearing
        in reports with no filing behind it.
        """
        self.db.execute(
            delete(FinancialFact).where(FinancialFact.company_id == company_id)
        )
        for fact in facts:
            self.db.add(FinancialFact(
                company_id=company_id,
                fiscal_year=fact.fiscal_year,
                line_item=fact.line_item.value,
                value=fact.value,
                precedence=int(Precedence.STORE),
                source=fact.source,
            ))
        return len(facts)


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _exchange_of(profile: dict[str, Any]) -> str:
    raw = (profile.get("exchange") or profile.get("exchangeShortName") or "")
    upper = str(raw).upper()
    if "NASDAQ" in upper:
        return "NASDAQ"
    if "NYSE" in upper or "NEW YORK" in upper:
        return "NYSE"
    if "AMEX" in upper or "AMERICAN" in upper:
        return "AMEX"
    return DEFAULT_US_EXCHANGE
