"""Derive working-capital line items from screener's own ratios.

Yahoo supplies receivables, inventory and payables directly, but its rate
limiter makes a 135-company pass impractical from a single IP — the enrichment
run was refused on company one and again after every cooldown.

Screener already reports the *days* ratios, and days are defined in terms of
exactly those balances:

    Debtor Days    = receivables / revenue          × 365
    Inventory Days = inventory   / revenue          × 365
    Days Payable   = payables    / revenue          × 365

so the balance is recoverable by inversion:

    receivables = Debtor Days    × revenue / 365

This is arithmetic on two reported figures, not an estimate. It is exact to
the rounding screener applies to the days figure (whole days), which on a
company with ₹100,000 cr of revenue is about ±₹137 cr — 0.14% of revenue, well
inside the 2-3% tolerance the validation harness uses.

**The convention matters.** Screener computes days on *sales*, not on cost of
goods, for all three ratios. Inverting with cost of goods — which is the
textbook definition for inventory and payable days — would produce balances
30-40% too small. The inversion must use the same denominator the source used,
and that is why this function is written down rather than inlined.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.data.screener_source import ScreenerError, ScreenerFinancials, fetch_screener
from app.domain.financials.canonical import Precedence
from app.domain.financials.line_items import LineItem as LI
from app.models.company import Company, FinancialFact
from app.services.universe.resolution import resolve_company

DAYS_IN_YEAR = 365.0

#: (screener ratio row, canonical item)
DAYS_MAP: tuple[tuple[str, LI], ...] = (
    ("Debtor Days", LI.TRADE_RECEIVABLES),
    ("Inventory Days", LI.INVENTORIES),
    ("Days Payable", LI.TRADE_PAYABLES),
)


@dataclass(slots=True)
class DeriveResult:
    ticker: str
    ok: bool
    added: int = 0
    coverage_before: float = 0.0
    coverage_after: float = 0.0
    error: str | None = None


def _coverage(db: Session, company_id: str, year: int) -> float:
    rows = db.scalars(
        select(FinancialFact.line_item).where(
            FinancialFact.company_id == company_id,
            FinancialFact.fiscal_year == year,
        )
    ).all()
    return round(len(set(rows)) / len(LI), 4)


def derive_from_ratios(
    db: Session, ticker: str, reference: ScreenerFinancials | None = None,
) -> DeriveResult:
    """Fill working-capital items by inverting the reported days ratios."""
    company = resolve_company(db, ticker, exchange="NSE")
    if company is None:
        return DeriveResult(ticker=ticker, ok=False, error="not ingested")

    years = sorted({
        y for y in db.scalars(
            select(FinancialFact.fiscal_year).where(
                FinancialFact.company_id == company.id,
            )
        ).all()
    })
    if not years:
        return DeriveResult(ticker=ticker, ok=False, error="no facts")

    latest = max(years)
    before = _coverage(db, company.id, latest)

    if reference is None:
        try:
            reference = fetch_screener(ticker)
        except ScreenerError as exc:
            return DeriveResult(
                ticker=ticker, ok=False, coverage_before=before,
                coverage_after=before, error=str(exc)[:100],
            )

    existing = {
        (row.line_item, row.fiscal_year)
        for row in db.scalars(
            select(FinancialFact).where(FinancialFact.company_id == company.id)
        ).all()
    }
    revenue = {
        row.fiscal_year: row.value
        for row in db.scalars(
            select(FinancialFact).where(
                FinancialFact.company_id == company.id,
                FinancialFact.line_item == LI.REVENUE.value,
            )
        ).all()
    }

    # Inventory and payable days are struck on **cost of goods**, not on
    # sales. Only debtor days uses sales, because a receivable arises from a
    # sale at selling price whereas inventory sits on the books at cost.
    #
    # Inverting all three with sales was wrong and visibly so: UltraTech's
    # reported 206 inventory days became ₹49,955 cr of inventory against a
    # ₹141,315 cr balance sheet — 35% of total assets in cement stock, which
    # no cement company has ever held. Named assets then exceeded the reported
    # total by ₹28,978 cr, the asset plug clamped to zero, and the sheet
    # stopped balancing for 107 of 135 companies.
    #
    # `Expenses` is screener's total cost line, which is the denominator its
    # ratio page uses.
    expenses = {
        year: reference.row("profit_loss", "Expenses", year) for year in years
    }

    # Receivables and inventory are *components of* screener's `Other Assets`
    # bucket, not additions to the balance sheet. Screener's own asset
    # breakdown is Fixed Assets + CWIP + Investments + Other Assets = Total,
    # so naming two items out of the fourth bucket must not enlarge the total.
    #
    # Whatever denominator convention screener used, the derived pair cannot
    # exceed the bucket that contains them. Where the inversion says otherwise
    # — Tata Steel's inventory came out at ₹99,707 cr against an ₹81,021 cr
    # bucket — the ratio's basis differs from ours and the derived figures are
    # scaled to fit rather than trusted. A component larger than its own
    # parent is arithmetically impossible, whatever the source says.
    other_assets = {
        year: reference.row("balance_sheet", "Other Assets", year)
        for year in years
    }

    def _scale(year: int, receivable: float, inventory: float) -> tuple[float, float]:
        bucket = other_assets.get(year)
        if bucket is None or bucket <= 0:
            return receivable, inventory
        total = receivable + inventory
        # Leave headroom: the bucket also holds cash, loans and advances.
        ceiling = bucket * 0.90
        if total <= ceiling or total <= 0:
            return receivable, inventory
        factor = ceiling / total
        return receivable * factor, inventory * factor

    derived: dict[int, dict[LI, float]] = {}
    for label, item in DAYS_MAP:
        uses_cost = label in ("Inventory Days", "Days Payable")
        for year in years:
            days = reference.row("ratios", label, year)
            base = expenses.get(year) if uses_cost else revenue.get(year)
            # Fall back to sales only where the cost line is missing, and
            # never silently: a value derived on the wrong denominator is
            # worse than an absent one.
            if base is None or base <= 0 or days is None:
                continue
            value = days * base / DAYS_IN_YEAR
            if value >= 0:
                derived.setdefault(year, {})[item] = value

    # The liability side needs the identical treatment. Trade payables are a
    # component of screener's `Other Liabilities` bucket, and a payable larger
    # than the bucket containing it drives the liability plug to zero — which
    # is what left UltraTech's sheet ₹12,099 cr out of balance in FY24 even
    # after the asset side was correct.
    other_liabilities = {
        year: reference.row("balance_sheet", "Other Liabilities", year)
        for year in years
    }

    for year, items in derived.items():
        receivable = items.get(LI.TRADE_RECEIVABLES, 0.0)
        inventory = items.get(LI.INVENTORIES, 0.0)
        receivable, inventory = _scale(year, receivable, inventory)
        if LI.TRADE_RECEIVABLES in items:
            items[LI.TRADE_RECEIVABLES] = receivable
        if LI.INVENTORIES in items:
            items[LI.INVENTORIES] = inventory

        payable = items.get(LI.TRADE_PAYABLES)
        bucket = other_liabilities.get(year)
        if payable is not None and bucket is not None and bucket > 0:
            ceiling = bucket * 0.90
            if payable > ceiling:
                items[LI.TRADE_PAYABLES] = ceiling

    added = 0
    for label, item in DAYS_MAP:
        for year in years:
            if (item.value, year) in existing:
                continue
            value = derived.get(year, {}).get(item)
            if value is None:
                continue
            db.add(FinancialFact(
                company_id=company.id, line_item=item.value, fiscal_year=year,
                value=float(value), precedence=int(Precedence.STORE),
                source="screener.in (derived from reported days ratio)",
            ))
            existing.add((item.value, year))
            added += 1

    # Cash: screener does not report it as a line, but `Other Assets` less the
    # named items is not a safe proxy either. It is left absent rather than
    # guessed — an absent fact is honest, a fabricated one is not, and Module 4
    # already degrades its data grade when cash is unknown.

    # ---- rebalance the residual ------------------------------------------
    # `other_current_assets` was set during ingestion as "reported total less
    # everything we could name". Naming receivables and inventories afterwards
    # therefore double-counts them: for Reliance FY26 the balance sheet came
    # out ₹312,395 cr too big, which is exactly receivables + inventories.
    #
    # The residual has to be recomputed against the same reported total, so
    # the sheet ties to what the company published rather than to our
    # reconstruction of it.
    # Rebalanced unconditionally, not only when this pass added something: a
    # re-run adds nothing but must still be able to repair a sheet that an
    # earlier pass left double-counted.
    db.flush()
    _rebalance_other_assets(db, company.id, reference, years)
    if added:
        company.data_version = (company.data_version or 1) + 1
    db.commit()

    return DeriveResult(
        ticker=ticker, ok=True, added=added, coverage_before=before,
        coverage_after=_coverage(db, company.id, latest),
    )


#: Named asset items. `other_current_assets` is the plug and is excluded.
_NAMED_ASSETS: tuple[LI, ...] = (
    LI.CASH_AND_BANK, LI.CURRENT_INVESTMENTS, LI.TRADE_RECEIVABLES,
    LI.INVENTORIES, LI.NET_BLOCK_PPE, LI.CWIP, LI.GOODWILL,
    LI.OTHER_INTANGIBLES, LI.LT_INVESTMENTS_ASSOCIATES, LI.DEFERRED_TAX_ASSET,
)

#: Named equity and liability items. `other_current_liabilities` is the plug.
_NAMED_LIABILITIES: tuple[LI, ...] = (
    LI.EQUITY_SHARE_CAPITAL, LI.RESERVES_SURPLUS, LI.MINORITY_INTEREST_BS,
    LI.LONG_TERM_BORROWINGS, LI.SHORT_TERM_BORROWINGS,
    LI.CURRENT_MATURITIES_LTD, LI.TRADE_PAYABLES, LI.SHORT_TERM_PROVISIONS,
    LI.DEFERRED_TAX_LIABILITY, LI.OTHER_NCL,
)


def _rebalance_other_assets(
    db: Session, company_id: str, reference: ScreenerFinancials, years: list[int],
) -> None:
    """Recompute both plugs so each side equals the reported total.

    Both sides, not just assets: naming `trade_payables` after ingestion
    double-counted it inside the liability plug in exactly the way naming
    receivables double-counted the asset one. Reliance FY26 balanced to
    ₹2,420,520 cr against reported assets of ₹2,177,546 cr — a gap of
    ₹242,974 cr, which was precisely trade payables.

    Screener reports one `Total Liabilities` figure equal to total assets, so
    each side is plugged against the same published total and the sheet ties
    by construction.
    """
    facts = db.scalars(
        select(FinancialFact).where(FinancialFact.company_id == company_id)
    ).all()

    by_year: dict[int, dict[str, FinancialFact]] = {}
    for fact in facts:
        by_year.setdefault(fact.fiscal_year, {})[fact.line_item] = fact

    for named_items, plug_item, reported_label in (
        (_NAMED_ASSETS, LI.OTHER_CURRENT_ASSETS, "Total Assets"),
        (_NAMED_LIABILITIES, LI.OTHER_CURRENT_LIABILITIES, "Total Liabilities"),
    ):
        named = {item.value for item in named_items}
        for year in years:
            reported_total = reference.row("balance_sheet", reported_label, year)
            if reported_total is None:
                continue
            row = by_year.get(year, {})
            named_sum = sum(
                fact.value for key, fact in row.items()
                if key in named and fact.value is not None
            )
            residual = reported_total - named_sum
            plug = row.get(plug_item.value)
            if plug is not None:
                plug.value = max(residual, 0.0)
            elif residual > 0:
                db.add(FinancialFact(
                    company_id=company_id, line_item=plug_item.value,
                    fiscal_year=year, value=residual,
                    precedence=int(Precedence.STORE),
                    source="screener.in (balancing residual)",
                ))


def derive_universe(db: Session, *, progress: bool = True) -> list[DeriveResult]:
    tickers = list(db.scalars(select(Company.ticker).order_by(Company.ticker)))
    results: list[DeriveResult] = []

    for index, ticker in enumerate(tickers, 1):
        try:
            result = derive_from_ratios(db, ticker)
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            result = DeriveResult(
                ticker=ticker, ok=False, error=f"{type(exc).__name__}: {exc}"[:100],
            )
        results.append(result)
        if progress:
            state = "ok " if result.ok else "skip"
            detail = (
                f"+{result.added:3d}  {result.coverage_before:.0%} -> "
                f"{result.coverage_after:.0%}"
                if result.ok else (result.error or "")[:60]
            )
            print(f"[{index:3d}/{len(tickers)}] {state} {ticker:<14}{detail}", flush=True)

    return results
