"""Company orchestration — the Universal Company Engine entry point.

The workbook resolves a company selection into a single scalar (`ActiveOffset`)
exactly once, and 540 downstream cells consume that one value. This service is
the direct analogue: :meth:`load_financials` performs **one** indexed query and
returns an immutable :class:`CanonicalFinancials` that every engine shares.

No engine re-queries the database. That is the workbook's "avoid duplicated
calculations" rule, enforced structurally.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.domain.financials.canonical import (
    CanonicalFinancials,
    CanonicalFinancialsBuilder,
    Precedence,
)
from app.domain.financials.line_items import LineItem
from app.domain.financials.statements import (
    build_balance_sheet,
    build_income_statement,
)
from app.models.company import Company, FinancialFact
from app.schemas.company import (
    CompanyDetail,
    CompanyProfile,
    CompanySummary,
    DataCoverage,
)
from app.services.live_market import LiveMarketService
from app.services.platform.cache import Namespace, cache

#: Valid canonical keys, used to skip unknown rows defensively.
_VALID_ITEMS = {item.value for item in LineItem}


@dataclass(frozen=True, slots=True)
class CompanyContext:
    """A company plus its resolved financials — built once per request."""

    company: Company
    financials: CanonicalFinancials


class CompanyService:
    def __init__(self, db: Session) -> None:
        self.db = db

    # ------------------------------------------------------------- retrieval
    def get(self, company_id: str) -> Company | None:
        return self.db.get(Company, company_id)

    def get_detail(self, company_id: str) -> CompanyDetail | None:
        """Company detail with the live market view attached."""
        company = self.get(company_id)
        if company is None:
            return None
        detail = CompanyDetail.model_validate(company)
        return LiveMarketService.attach(detail, company, self.db)

    def get_by_ticker(self, ticker: str) -> Company | None:
        stmt = select(Company).where(func.upper(Company.ticker) == ticker.upper())
        return self.db.execute(stmt).scalar_one_or_none()

    def search(self, query: str, limit: int = 20) -> list[CompanySummary]:
        """Search across every identity field, ranked so exact identifiers
        come first.

        Phase 1 widens the matched columns from name/ticker/sector to also
        ISIN, BSE scrip code and industry — the fields a 5,000-company
        universe is actually looked up by (an ISIN is what a statement or
        contract carries, not a ticker). Postgres serves the leading-wildcard
        patterns from the pg_trgm GIN indexes (migration f5c9e1b6a348); the
        query is unchanged on SQLite, where the test suite runs.

        Results are cached under `search:{query}:{limit}` for 60s: search-
        as-you-type re-asks the same prefix many times a second, and at this
        universe size a miss is a genuine scan rather than a rounding error.
        """
        q = (query or "").strip()
        if not q:
            return []
        cached = cache.get(Namespace.SEARCH, q, limit)
        if cached is not None:
            return cached

        pattern = f"%{q.lower()}%"
        stmt = (
            select(Company)
            .where(
                or_(
                    func.lower(Company.name).like(pattern),
                    func.lower(Company.ticker).like(pattern),
                    func.lower(Company.isin).like(pattern),
                    func.lower(Company.bse_code).like(pattern),
                    func.lower(Company.sector).like(pattern),
                    func.lower(Company.industry).like(pattern),
                )
            )
            .limit(limit * 3)
        )
        rows = self.db.execute(stmt).scalars().all()
        ql = q.lower()

        def rank(c: Company) -> tuple[int, float]:
            t, n = c.ticker.lower(), c.name.lower()
            if t == ql or (c.isin or "").lower() == ql or (c.bse_code or "").lstrip("0") == ql:
                bucket = 0
            elif t.startswith(ql):
                bucket = 1
            elif n.startswith(ql):
                bucket = 2
            elif ql in n:
                bucket = 3
            else:
                bucket = 4
            return bucket, -(c.market_cap or 0)

        ranked = sorted(rows, key=rank)[:limit]
        market = LiveMarketService(self.db).bulk_quotes(ranked)
        results = [
            CompanySummary.model_validate(c).model_copy(
                update={"market": market.get(c.ticker)}
            )
            for c in ranked
        ]
        cache.set(Namespace.SEARCH, results, q, limit)
        return results

    def list_companies(
        self,
        page: int = 1,
        page_size: int = 25,
        sector: str | None = None,
    ) -> tuple[int, list[CompanySummary]]:
        base = select(Company)
        if sector:
            base = base.where(Company.sector == sector)
        total = self.db.execute(
            select(func.count()).select_from(base.subquery())
        ).scalar_one()
        rows = (
            self.db.execute(
                base.order_by(Company.market_cap.desc().nullslast())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
            .scalars()
            .all()
        )
        market = LiveMarketService(self.db).bulk_quotes(rows)
        return total, [
            CompanySummary.model_validate(c).model_copy(
                update={"market": market.get(c.ticker)}
            )
            for c in rows
        ]

    def sectors(self) -> list[str]:
        stmt = (
            select(Company.sector)
            .where(Company.sector.is_not(None))
            .distinct()
            .order_by(Company.sector)
        )
        return [s for (s,) in self.db.execute(stmt).all() if s]

    # ------------------------------------------------- the single resolution
    def load_financials(self, company_id: str) -> CanonicalFinancials:
        """Resolve every canonical fact for a company in ONE query.

        Rows arrive at mixed precedence tiers; the builder applies the
        `0C Data Map` chain (override > store > alias > absent).

        Cached (Phase 2). One query it may be, but for a covered company it
        returns several hundred rows and then builds the full canonical
        structure over them, and every analysis, valuation, score and report
        section begins by calling this. Filed statements change when a company
        reports — four times a year at most — so the recomputation was pure
        waste on every request in between.
        """
        return cache.get_or_set(
            Namespace.STATEMENTS,
            lambda: self._load_financials_uncached(company_id),
            company_id,
        )

    def _load_financials_uncached(self, company_id: str) -> CanonicalFinancials:
        rows = (
            self.db.execute(
                select(
                    FinancialFact.line_item,
                    FinancialFact.fiscal_year,
                    FinancialFact.value,
                    FinancialFact.precedence,
                    FinancialFact.source,
                ).where(FinancialFact.company_id == company_id)
            )
            .all()
        )
        years = sorted({r.fiscal_year for r in rows})
        builder = CanonicalFinancialsBuilder(company_id, years)
        for item, year, value, precedence, source in rows:
            if item not in _VALID_ITEMS:
                continue
            builder.add(
                LineItem(item), year, value, Precedence(precedence), source
            )
        return builder.build()

    def load_context(self, company_id: str) -> CompanyContext | None:
        company = self.get(company_id)
        if company is None:
            return None
        return CompanyContext(company, self.load_financials(company_id))

    # ---------------------------------------------------------------- profile
    def profile(self, company_id: str) -> CompanyProfile | None:
        """Company header plus headline figures, derived via the engines."""
        ctx = self.load_context(company_id)
        if ctx is None:
            return None

        fin = ctx.financials
        populated = sum(
            1
            for item in LineItem
            for year in fin.fiscal_years
            if fin.get(item, year) is not None
        )
        coverage = DataCoverage(
            has_data=fin.has_data(),
            coverage=fin.coverage(),
            fiscal_years=list(fin.fiscal_years),
            items_populated=populated,
        )

        market = LiveMarketService(self.db).snapshot(ctx.company)
        detail = CompanyDetail.model_validate(ctx.company).model_copy(
            update={"market": market}
        )
        profile = CompanyProfile(
            company=detail,
            coverage=coverage,
            latest_fiscal_year=fin.latest_year,
            market=market,
        )
        if not fin.has_data() or fin.latest_year is None:
            return profile

        year = fin.latest_year
        income = build_income_statement(fin, year)
        balance = build_balance_sheet(fin, year)
        return profile.model_copy(
            update={
                "revenue": income.total_revenue,
                "ebitda": income.ebitda,
                "pat": income.pat,
                "eps": income.eps_basic,
                "ebitda_margin": income.ebitda_margin,
                "pat_margin": income.pat_margin,
                "net_debt": balance.net_debt,
                "total_assets": balance.total_assets,
                "balance_sheet_ties": balance.balances,
            }
        )
