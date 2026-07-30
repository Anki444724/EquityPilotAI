"""Real-data ingestion: two sources, one canonical fact store.

`screener.in` is primary — twelve years, ₹ crore native, Indian consolidated
presentation. `Yahoo Finance` supplies the expense breakdown screener
aggregates away, plus the live quote and price history Module 8 needs.

The merge rule is stated once and applied everywhere: **screener wins on any
line both report.** Not because it is more accurate in principle, but because
it is the source whose presentation matches the workbook's schema, and mixing
two presentations line by line is how a balance sheet stops balancing.

Where the two disagree by more than a tolerance, the disagreement is
**recorded rather than resolved**. Two independent sources differing on a
reported figure is precisely the signal a validation sprint exists to surface,
and averaging them away would destroy it.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.data.nse_universe import NSE_UNIVERSE, is_financial
from app.data.screener_source import ScreenerError, ScreenerFinancials, fetch_screener
from app.data.yahoo_source import CompanyFinancials, FetchError, fetch_financials
from app.domain.financials.canonical import Precedence
from app.domain.financials.line_items import LineItem as LI
from app.models.company import Company, FinancialFact

#: Screener's row labels → canonical line items. Only the unambiguous ones;
#: anything needing arithmetic is derived below with the reasoning written out.
SCREENER_PL: dict[str, LI] = {
    "Sales": LI.REVENUE,
    "Other Income": LI.OTHER_INCOME,
    "Interest": LI.FINANCE_COSTS,
    "Depreciation": LI.DEPRECIATION,
}

SCREENER_BS: dict[str, LI] = {
    "Equity Capital": LI.EQUITY_SHARE_CAPITAL,
    "Reserves": LI.RESERVES_SURPLUS,
    "CWIP": LI.CWIP,
    "Investments": LI.LT_INVESTMENTS_ASSOCIATES,
    "Fixed Assets": LI.NET_BLOCK_PPE,
}

#: Yahoo fills the granularity screener aggregates into `Expenses`.
YAHOO_DETAIL: tuple[LI, ...] = (
    LI.RAW_MATERIALS, LI.EMPLOYEE_BENEFIT, LI.OTHER_EXPENSES,
    LI.CASH_AND_BANK, LI.CURRENT_INVESTMENTS, LI.TRADE_RECEIVABLES,
    LI.INVENTORIES, LI.OTHER_CURRENT_ASSETS, LI.GOODWILL,
    LI.OTHER_INTANGIBLES, LI.OTHER_NCA, LI.DEFERRED_TAX_ASSET,
    LI.TRADE_PAYABLES, LI.SHORT_TERM_BORROWINGS, LI.CURRENT_MATURITIES_LTD,
    LI.OTHER_CURRENT_LIABILITIES, LI.SHORT_TERM_PROVISIONS,
    LI.DEFERRED_TAX_LIABILITY, LI.OTHER_NCL, LI.MINORITY_INTEREST_BS,
    LI.MINORITY_INTEREST, LI.WEIGHTED_SHARES,
    LI.CHG_INVENTORIES_CF, LI.CHG_RECEIVABLES_CF, LI.CHG_PAYABLES_CF,
    LI.DIRECT_TAXES_PAID, LI.SALE_FIXED_ASSETS, LI.PURCHASE_SALE_INVESTMENTS,
    LI.OTHER_INVESTING, LI.PROCEEDS_BORROWINGS, LI.REPAYMENT_BORROWINGS,
    LI.OTHER_FINANCING, LI.OPENING_CASH, LI.OTHER_NONCASH_ADJ,
)

#: Relative tolerance when comparing the two sources on the same figure.
#: 2% absorbs classification differences (what counts as "other income") while
#: still catching a genuine disagreement about a headline number.
CROSS_SOURCE_TOLERANCE = 0.02


@dataclass(slots=True)
class SourceDisagreement:
    ticker: str
    item: str
    fiscal_year: int
    screener: float
    yahoo: float

    @property
    def relative(self) -> float:
        base = max(abs(self.screener), abs(self.yahoo), 1.0)
        return abs(self.screener - self.yahoo) / base


@dataclass(slots=True)
class IngestResult:
    ticker: str
    ok: bool
    company_id: str | None = None
    fiscal_years: list[int] = field(default_factory=list)
    fact_count: int = 0
    price: float | None = None
    error: str | None = None
    warnings: list[str] = field(default_factory=list)
    disagreements: list[SourceDisagreement] = field(default_factory=list)
    #: Share of the 54 canonical items populated for the latest year.
    coverage: float = 0.0


def _screener_series(table: dict[str, dict[int, float]], label: str) -> dict[int, float]:
    """Look up a screener row, tolerating the trailing `+` it appends to
    expandable rows."""
    for key, series in table.items():
        if key.lower().rstrip("+ ").strip() == label.lower():
            return series
    return {}


def canonicalise(
    screener: ScreenerFinancials, yahoo: CompanyFinancials | None,
) -> tuple[dict[LI, dict[int, float]], list[SourceDisagreement], list[str]]:
    """Merge both sources into the canonical 54 line items."""
    facts: dict[LI, dict[int, float]] = {}
    disagreements: list[SourceDisagreement] = []
    warnings: list[str] = []

    def put(item: LI, year: int, value: float | None) -> None:
        if value is None:
            return
        facts.setdefault(item, {})[year] = float(value)

    years = screener.fiscal_years

    # ---- which presentation is this? ---------------------------------------
    # Screener renders banks, NBFCs and insurers on a *financing* layout and
    # everyone else on an *operating* layout, and the two are not
    # interchangeable:
    #
    #   operating   Sales   − Expenses = Operating Profit,  Interest deducted after
    #   financing   Revenue − Expenses = Financing Profit,  Interest ALREADY inside Expenses
    #
    # The distinction is not cosmetic. Reading a bank with the operating
    # mapping leaves revenue null (there is no `Sales` row) and then subtracts
    # interest a second time: HDFC Bank's FY26 net profit came out at
    # −₹268,944 cr against a reported +₹79,219 cr. A 130% error with the
    # opposite sign, on the largest private bank in the country.
    financing = "Financing Profit" in {
        k.rstrip("+ ").strip() for k in screener.profit_loss
    }
    if financing:
        warnings.append(
            "financing presentation — interest is a cost of revenue, not a "
            "post-operating deduction; EV/EBITDA and FCFF are not meaningful"
        )

    # ---- screener: the presentation the schema was designed around --------
    for label, item in SCREENER_PL.items():
        # `Interest` means opposite things in the two layouts. On a financing
        # statement it is already inside Expenses, so mapping it to
        # FINANCE_COSTS — which the income builder subtracts again — is the
        # double-count above. It is deliberately skipped.
        if financing and label == "Interest":
            continue
        for year, value in _screener_series(screener.profit_loss, label).items():
            put(item, year, value)

    if financing:
        # Revenue for a financing company is total income (interest earned
        # plus fees), which screener labels `Revenue`.
        for year, value in _screener_series(screener.profit_loss, "Revenue").items():
            put(LI.REVENUE, year, value)
        # Interest expended is a cost of doing business here, so it belongs in
        # operating costs rather than below the operating line.
        for year, value in _screener_series(screener.profit_loss, "Interest").items():
            put(LI.RAW_MATERIALS, year, value)
        for year in years:
            put(LI.FINANCE_COSTS, year, 0.0)
    for label, item in SCREENER_BS.items():
        for year, value in _screener_series(screener.balance_sheet, label).items():
            put(item, year, value)

    # ---- derivations, each with its reasoning -----------------------------
    pbt = _screener_series(screener.profit_loss, "Profit before tax")
    tax_pct = _screener_series(screener.profit_loss, "Tax %")
    net = _screener_series(screener.profit_loss, "Net Profit")
    expenses = _screener_series(screener.profit_loss, "Expenses")
    eps = _screener_series(screener.profit_loss, "EPS in Rs")
    payout = _screener_series(screener.profit_loss, "Dividend Payout %")

    for year in years:
        # Tax expense: the schema wants an amount, screener publishes a rate
        # *and* a net profit. Prefer PBT − net profit, because it reproduces
        # the reported bottom line by construction.
        #
        # PBT × rate looked equivalent and is not, for two common cases:
        #
        #   Crompton FY26   PBT −79, tax rate 191%  →  derived +72 against a
        #                   reported −231. A loss turned into a profit: the
        #                   rate is meaningless on a negative base.
        #   DLF FY26        PBT 2,932, rate 11%     →  derived 2,609 against a
        #                   reported 4,415, because share of associate profit
        #                   is added *below* the tax line and the rate cannot
        #                   see it.
        #
        # The subtraction absorbs both, along with minority interest and
        # anything else sitting between PBT and the published bottom line.
        if year in pbt and year in net:
            put(LI.TAX_EXPENSE, year, pbt[year] - net[year])
        elif year in pbt and year in tax_pct:
            put(LI.TAX_EXPENSE, year, pbt[year] * tax_pct[year] / 100.0)

        # Weighted shares: net profit ÷ EPS. Screener reports both, and the
        # quotient is the share count the EPS was actually struck on — which
        # is more reliable than a period-end count for a company that issued
        # or bought back stock mid-year.
        if year in net and year in eps and eps[year]:
            put(LI.WEIGHTED_SHARES, year, net[year] / eps[year])

        # Dividend paid: payout percentage of net profit.
        if year in net and year in payout:
            put(LI.DIVIDEND_PAID, year, net[year] * payout[year] / 100.0)

        # Borrowings: screener reports one figure. Splitting it into long and
        # short term without a source would be invention, so the whole amount
        # is long-term and short-term borrowings come from Yahoo where
        # available. The debt *total* — which is what every ratio uses — is
        # right either way.
        borrowings = _screener_series(screener.balance_sheet, "Borrowings").get(year)
        if borrowings is not None:
            put(LI.LONG_TERM_BORROWINGS, year, borrowings)

        # Cash flow: the three activity totals are reported; the schema wants
        # the components. Capex is taken as investing outflow net of
        # investment purchases where Yahoo supplies them, and directly from
        # Yahoo otherwise.
        cfo = _screener_series(screener.cash_flow, "Cash from Operating Activity").get(year)
        if cfo is not None:
            facts.setdefault(LI.OTHER_WC_MOVEMENT, {}).setdefault(year, 0.0)

    # ---- Yahoo: the granularity screener aggregates away ------------------
    if yahoo is not None:
        for item in YAHOO_DETAIL:
            for year, value in yahoo.facts.get(item, {}).items():
                if year in years:
                    put(item, year, value)

        # Capex, only from Yahoo — screener does not report it separately.
        for year, value in yahoo.facts.get(LI.CAPEX, {}).items():
            if year in years:
                put(LI.CAPEX, year, value)

        # ---- cross-source validation --------------------------------------
        # Where both report the same thing, compare rather than merge.
        for item, yahoo_field in (
            (LI.REVENUE, "annualOperatingRevenue"),
            (LI.DEPRECIATION, "annualDepreciationAndAmortization"),
            (LI.FINANCE_COSTS, "annualInterestExpense"),
        ):
            for year in years:
                left = facts.get(item, {}).get(year)
                right = yahoo.ctx(yahoo_field, year) or yahoo.value(item, year)
                if left is None or right is None:
                    continue
                base = max(abs(left), abs(right), 1.0)
                if abs(left - right) / base > CROSS_SOURCE_TOLERANCE:
                    disagreements.append(SourceDisagreement(
                        ticker=screener.ticker, item=item.value,
                        fiscal_year=year, screener=left, yahoo=right,
                    ))
    else:
        warnings.append("Yahoo unavailable — expense breakdown and capex absent")

    # ---- expense residual --------------------------------------------------
    # Screener's single `Expenses` line is authoritative for the total. Where
    # Yahoo supplied a breakdown, scale it to reconcile; where it did not, put
    # everything in other expenses. Either way the total is the reported one,
    # so operating profit ties.
    for year in years:
        total = expenses.get(year)
        if total is None:
            continue
        if financing:
            # Interest was already booked to RAW_MATERIALS as a cost of
            # revenue. Screener's `Expenses` for a financing company is the
            # non-interest operating cost, so the two sum to total cost and
            # the residual split must not double-count.
            put(LI.OTHER_EXPENSES, year, total)
            continue
        parts = [
            facts.get(LI.RAW_MATERIALS, {}).get(year),
            facts.get(LI.EMPLOYEE_BENEFIT, {}).get(year),
            facts.get(LI.OTHER_EXPENSES, {}).get(year),
        ]
        known = sum(p for p in parts if p is not None)
        if known <= 0:
            put(LI.OTHER_EXPENSES, year, total)
            put(LI.RAW_MATERIALS, year, 0.0)
            put(LI.EMPLOYEE_BENEFIT, year, 0.0)
        elif abs(known - total) / max(total, 1.0) > 0.005:
            scale = total / known
            for item in (LI.RAW_MATERIALS, LI.EMPLOYEE_BENEFIT, LI.OTHER_EXPENSES):
                current = facts.get(item, {}).get(year)
                if current is not None:
                    put(item, year, current * scale)

    # ---- balance-sheet residuals -------------------------------------------
    # Screener reports `Other Assets` and `Other Liabilities` as buckets. The
    # schema's individual items come from Yahoo where available; whatever is
    # left over goes to the corresponding "other" line so the sheet balances
    # against the reported total rather than against our reconstruction.
    for year in years:
        total_assets = _screener_series(screener.balance_sheet, "Total Assets").get(year)
        if total_assets is None:
            continue
        named_assets = sum(
            facts.get(item, {}).get(year, 0.0)
            for item in (
                LI.CASH_AND_BANK, LI.CURRENT_INVESTMENTS, LI.TRADE_RECEIVABLES,
                LI.INVENTORIES, LI.NET_BLOCK_PPE, LI.CWIP, LI.GOODWILL,
                LI.OTHER_INTANGIBLES, LI.LT_INVESTMENTS_ASSOCIATES,
                LI.DEFERRED_TAX_ASSET,
            )
        )
        residual = total_assets - named_assets
        put(LI.OTHER_CURRENT_ASSETS, year, max(residual, 0.0))
        if residual < 0:
            warnings.append(
                f"FY{year}: named assets exceed reported total by "
                f"{abs(residual):,.0f} cr — source presentations differ"
            )

        total_liabilities = _screener_series(
            screener.balance_sheet, "Total Liabilities",
        ).get(year)
        if total_liabilities is None:
            continue
        named_liabilities = sum(
            facts.get(item, {}).get(year, 0.0)
            for item in (
                LI.EQUITY_SHARE_CAPITAL, LI.RESERVES_SURPLUS,
                LI.MINORITY_INTEREST_BS, LI.LONG_TERM_BORROWINGS,
                LI.SHORT_TERM_BORROWINGS, LI.CURRENT_MATURITIES_LTD,
                LI.TRADE_PAYABLES, LI.SHORT_TERM_PROVISIONS,
                LI.DEFERRED_TAX_LIABILITY,
            )
        )
        put(LI.OTHER_CURRENT_LIABILITIES, year,
            max(total_liabilities - named_liabilities, 0.0))

    # ---- items no aggregator carries -----------------------------------------
    for year in years:
        for item in (
            LI.OTHER_OPERATING_INCOME, LI.CHANGE_INVENTORIES,
            LI.EXCEPTIONAL_ITEMS, LI.OCI, LI.PURCHASE_STOCK_IN_TRADE,
            LI.EQUITY_ISSUED_BUYBACK,
        ):
            facts.setdefault(item, {}).setdefault(year, 0.0)

    return facts, disagreements, warnings


def ingest_company(
    db: Session,
    ticker: str,
    name: str,
    sector: str,
    industry: str,
    *,
    with_yahoo: bool = True,
) -> IngestResult:
    """Fetch, canonicalise and persist one real company."""
    try:
        screener = fetch_screener(ticker)
    except ScreenerError as exc:
        return IngestResult(ticker=ticker, ok=False, error=f"screener: {exc}")

    yahoo: CompanyFinancials | None = None
    if with_yahoo:
        try:
            yahoo = fetch_financials(ticker)
        except FetchError as exc:
            screener.warnings.append(f"yahoo unavailable: {exc}")

    facts, disagreements, warnings = canonicalise(screener, yahoo)
    if not facts:
        return IngestResult(ticker=ticker, ok=False, error="no canonical facts derived")

    price = screener.price or (yahoo.price if yahoo else None)
    shares = None
    latest = screener.latest_year
    if latest is not None:
        shares = facts.get(LI.WEIGHTED_SHARES, {}).get(latest)
    market_cap = screener.market_cap or (price * shares if price and shares else None)

    existing = db.scalar(select(Company).where(Company.ticker == ticker))
    company_id = existing.id if existing else str(uuid.uuid4())

    if existing is not None:
        db.execute(delete(FinancialFact).where(FinancialFact.company_id == company_id))
        company = existing
        company.name, company.sector, company.industry = name, sector, industry
        company.data_version = (company.data_version or 1) + 1
    else:
        company = Company(
            id=company_id, name=name, ticker=ticker, exchange="NSE",
            sector=sector, industry=industry,
        )
        db.add(company)

    company.current_price = price
    company.shares_outstanding = shares
    company.market_cap = market_cap
    company.description = (
        f"{name} — {industry}, {sector}. Financials sourced from screener.in "
        f"(consolidated) and Yahoo Finance."
    )

    written = 0
    for item, series in facts.items():
        for year, value in series.items():
            db.add(FinancialFact(
                company_id=company_id,
                line_item=item.value,
                fiscal_year=year,
                value=float(value),
                precedence=int(Precedence.STORE),
                source="screener.in+yahoo" if yahoo else "screener.in",
            ))
            written += 1

    db.commit()

    latest_populated = sum(
        1 for series in facts.values() if latest in series
    )
    return IngestResult(
        ticker=ticker, ok=True, company_id=company_id,
        fiscal_years=screener.fiscal_years, fact_count=written,
        price=price,
        warnings=screener.warnings + warnings,
        disagreements=disagreements,
        coverage=round(latest_populated / len(LI), 4),
    )


def ingest_universe(
    db: Session,
    *,
    limit: int | None = None,
    with_yahoo: bool = True,
    progress: bool = True,
) -> list[IngestResult]:
    """Ingest the coverage universe. Failures are recorded, not raised."""
    results: list[IngestResult] = []
    universe = NSE_UNIVERSE[:limit] if limit else NSE_UNIVERSE

    for index, (ticker, name, sector, industry) in enumerate(universe, 1):
        try:
            result = ingest_company(
                db, ticker, name, sector, industry, with_yahoo=with_yahoo,
            )
        except Exception as exc:  # noqa: BLE001 — one bad ticker must not stop the run
            db.rollback()
            result = IngestResult(
                ticker=ticker, ok=False, error=f"{type(exc).__name__}: {exc}",
            )
        results.append(result)

        if progress:
            state = "ok " if result.ok else "FAIL"
            detail = (
                f"{len(result.fiscal_years)}y {result.fact_count:4d} facts "
                f"cov={result.coverage:.0%}"
                if result.ok else (result.error or "")[:70]
            )
            print(f"[{index:3d}/{len(universe)}] {state} {ticker:<14}{detail}", flush=True)

    return results
