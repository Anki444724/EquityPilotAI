"""Real financial data ingestion from Yahoo Finance.

This replaces the synthetic generator in `db/seed.py`. What it produces is
**real reported data for real NSE-listed companies**, mapped onto the same 54
canonical line items the workbook defines, so every engine downstream is
unchanged.

Three honesty constraints, stated here because they govern how the validation
report must read:

1. **Yahoo is a secondary source, not a filing.** It aggregates from vendors
   and occasionally disagrees with the annual report at the margins —
   typically on classification (what counts as "other income") rather than on
   headline figures. Every fact is stamped `source="yahoo_finance"` and
   `Precedence.IMPORT`, so provenance travels with the number and the UI can
   say where it came from.

2. **Only four years of history are available**, not the ten the workbook
   assumes. Any ratio needing an opening balance loses its first year, CAGRs
   are computed over three years rather than nine, and the scoring engine's
   trend categories have less to work with. This is a data limitation, not a
   defect, and the report says so.

3. **Banks and insurers do not fit the schema.** They have no cost of goods,
   no working-capital cycle and no meaningful EV/EBITDA. They are ingested
   because a real universe contains them and because refusing to break on
   them is itself worth testing — not because the resulting DCF means
   anything.

Units: Yahoo reports absolute rupees. The platform works in ₹ crore
throughout, per `00 Setup`. Conversion happens once, here, at the boundary.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date

from app.domain.financials.line_items import LineItem as LI

#: Yahoo reports absolute rupees; the workbook works in crore.
CRORE = 1e7

_BASE = "https://query2.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/timeseries"
_QUOTE = "https://query2.finance.yahoo.com/v8/finance/chart"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

#: Yahoo field → canonical line item.
#:
#: Only unambiguous mappings appear here. Anything requiring arithmetic —
#: cost of goods split into raw materials and purchases, say — is derived in
#: `_derive`, where the reasoning can be written down. A silent guess inside a
#: lookup table is how a mis-mapped line item survives review.
DIRECT_MAP: dict[str, LI] = {
    # -- income statement ------------------------------------------------
    "annualOperatingRevenue": LI.REVENUE,
    "annualDepreciationAndAmortization": LI.DEPRECIATION,
    "annualInterestExpense": LI.FINANCE_COSTS,
    "annualTaxProvision": LI.TAX_EXPENSE,
    "annualMinorityInterests": LI.MINORITY_INTEREST,
    "annualBasicAverageShares": LI.WEIGHTED_SHARES,
    "annualSellingGeneralAndAdministration": LI.OTHER_EXPENSES,

    # -- balance sheet ----------------------------------------------------
    "annualCashAndCashEquivalents": LI.CASH_AND_BANK,
    "annualOtherShortTermInvestments": LI.CURRENT_INVESTMENTS,
    "annualAccountsReceivable": LI.TRADE_RECEIVABLES,
    "annualInventory": LI.INVENTORIES,
    "annualOtherCurrentAssets": LI.OTHER_CURRENT_ASSETS,
    "annualNetPPE": LI.NET_BLOCK_PPE,
    "annualConstructionInProgress": LI.CWIP,
    "annualGoodwill": LI.GOODWILL,
    "annualOtherIntangibleAssets": LI.OTHER_INTANGIBLES,
    "annualOtherNonCurrentAssets": LI.OTHER_NCA,
    "annualAccountsPayable": LI.TRADE_PAYABLES,
    "annualCurrentDebt": LI.SHORT_TERM_BORROWINGS,
    "annualOtherCurrentLiabilities": LI.OTHER_CURRENT_LIABILITIES,
    "annualCurrentProvisions": LI.SHORT_TERM_PROVISIONS,
    "annualLongTermDebt": LI.LONG_TERM_BORROWINGS,
    "annualNonCurrentDeferredTaxesLiabilities": LI.DEFERRED_TAX_LIABILITY,
    "annualOtherNonCurrentLiabilities": LI.OTHER_NCL,
    "annualCapitalStock": LI.EQUITY_SHARE_CAPITAL,
    "annualRetainedEarnings": LI.RESERVES_SURPLUS,
    "annualMinorityInterest": LI.MINORITY_INTEREST_BS,

    # -- cash flow ---------------------------------------------------------
    "annualChangeInInventory": LI.CHG_INVENTORIES_CF,
    "annualChangeInReceivables": LI.CHG_RECEIVABLES_CF,
    "annualChangeInPayable": LI.CHG_PAYABLES_CF,
    "annualTaxesRefundPaid": LI.DIRECT_TAXES_PAID,
    "annualCapitalExpenditure": LI.CAPEX,
    "annualSaleOfInvestment": LI.SALE_FIXED_ASSETS,
    "annualPurchaseOfInvestment": LI.PURCHASE_SALE_INVESTMENTS,
    "annualNetOtherInvestingChanges": LI.OTHER_INVESTING,
    "annualIssuanceOfDebt": LI.PROCEEDS_BORROWINGS,
    "annualRepaymentOfDebt": LI.REPAYMENT_BORROWINGS,
    "annualNetOtherFinancingCharges": LI.OTHER_FINANCING,
    "annualCashDividendsPaid": LI.DIVIDEND_PAID,
    "annualBeginningCashPosition": LI.OPENING_CASH,
    "annualOtherNonCashItems": LI.OTHER_NONCASH_ADJ,
}

#: Fields fetched but not mapped directly — used for derivation and for the
#: cross-checks in `validate.py`.
CONTEXT_FIELDS = (
    "annualTotalRevenue", "annualCostOfRevenue", "annualGrossProfit",
    "annualOperatingIncome", "annualPretaxIncome", "annualNetIncome",
    "annualNetIncomeCommonStockholders", "annualEBITDA", "annualEBIT",
    "annualTotalAssets", "annualTotalLiabilitiesNetMinorityInterest",
    "annualStockholdersEquity", "annualTotalEquityGrossMinorityInterest",
    "annualCurrentAssets", "annualCurrentLiabilities",
    "annualTotalNonCurrentAssets",
    "annualTotalNonCurrentLiabilitiesNetMinorityInterest",
    "annualTotalDebt", "annualNetDebt", "annualWorkingCapital",
    "annualOperatingCashFlow", "annualInvestingCashFlow",
    "annualFinancingCashFlow", "annualFreeCashFlow", "annualEndCashPosition",
    "annualOrdinarySharesNumber", "annualShareIssued",
    "annualBasicEPS", "annualDilutedEPS", "annualTotalExpenses",
    "annualOperatingExpense", "annualGrossPPE",
    "annualNetNonOperatingInterestIncomeExpense",
)

ALL_FIELDS = tuple(DIRECT_MAP) + CONTEXT_FIELDS


class FetchError(Exception):
    """The provider could not be reached, or returned nothing usable."""


@dataclass(slots=True)
class CompanyFinancials:
    """One company's real reported data, ready to persist."""

    ticker: str
    fiscal_years: list[int] = field(default_factory=list)
    #: canonical line item → {fiscal_year: value in ₹ crore}
    facts: dict[LI, dict[int, float]] = field(default_factory=dict)
    #: Raw Yahoo fields → {fiscal_year: value in ₹ crore}, for cross-checks.
    context: dict[str, dict[int, float]] = field(default_factory=dict)
    price: float | None = None
    shares_outstanding: float | None = None   # crore
    market_cap: float | None = None           # ₹ crore
    currency: str = "INR"
    warnings: list[str] = field(default_factory=list)

    @property
    def latest_year(self) -> int | None:
        return max(self.fiscal_years) if self.fiscal_years else None

    def value(self, item: LI, year: int) -> float | None:
        return self.facts.get(item, {}).get(year)

    def ctx(self, field_name: str, year: int) -> float | None:
        return self.context.get(field_name, {}).get(year)

    @property
    def fact_count(self) -> int:
        return sum(len(v) for v in self.facts.values())


#: Minimum seconds between requests to the provider.
#:
#: Yahoo returns 429 aggressively when a client fires without pause — the
#: first ingestion attempt hit it on request one. This is a public endpoint
#: being used courteously, not a quota we have bought, so the client throttles
#: itself rather than discovering the limit repeatedly.
MIN_INTERVAL = 0.75

_last_request_at = 0.0

#: Consecutive 429s. After enough of them the provider is treated as
#: unavailable for the remainder of the run rather than being hammered: a
#: 120-company ingestion that retries into a rate limit on every company takes
#: hours and still fails. Screener alone yields a usable dataset, so degrading
#: to it is better than stalling.
_consecutive_429 = 0
_CIRCUIT_THRESHOLD = 12


def provider_available() -> bool:
    return _consecutive_429 < _CIRCUIT_THRESHOLD


def reset_circuit() -> None:
    global _consecutive_429
    _consecutive_429 = 0


def _throttle() -> None:
    global _last_request_at
    elapsed = time.monotonic() - _last_request_at
    if elapsed < MIN_INTERVAL:
        time.sleep(MIN_INTERVAL - elapsed)
    _last_request_at = time.monotonic()


def _http_json(url: str, *, retries: int = 4, backoff: float = 3.0) -> dict:
    """GET JSON from Yahoo with conservative throttling and 429 handling."""
    global _consecutive_429, MIN_INTERVAL

    if not provider_available():
        raise FetchError("provider circuit open — too many 429s")

    last: Exception | None = None

    for attempt in range(retries):
        _throttle()

        try:
            request = urllib.request.Request(
                url,
                headers={
                    **_HEADERS,
                    "Cache-Control": "no-cache",
                    "Pragma": "no-cache",
                },
                method="GET",
            )

            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.loads(response.read())

            _consecutive_429 = 0
            return payload

        except urllib.error.HTTPError as exc:
            last = exc

            if exc.code == 429:
                _consecutive_429 += 1

                # Yahoo is rate-limiting this IP. Increase the interval
                # aggressively instead of retrying immediately.
                MIN_INTERVAL = min(max(MIN_INTERVAL * 2.0, 5.0), 30.0)

                if attempt < retries - 1:
                    delay = min(backoff * (2 ** attempt), 30.0)
                    time.sleep(delay)
                    continue

            elif 500 <= exc.code < 600:
                if attempt < retries - 1:
                    time.sleep(min(backoff * (2 ** attempt), 15.0))
                    continue

            break

        except Exception as exc:
            last = exc

            if attempt < retries - 1:
                time.sleep(min(backoff * (2 ** attempt), 15.0))
                continue

            break

    raise FetchError(f"{type(last).__name__}: {last}")

def _fiscal_year(as_of: str) -> int:
    """Indian fiscal year ending 31 March.

    A statement dated 2025-03-31 is FY2025. Yahoo occasionally reports a
    December year-end for a company that has changed its reporting period;
    anything from January to June is treated as belonging to the fiscal year
    it ends in, and later months to the next one.
    """
    when = date.fromisoformat(as_of[:10])
    return when.year if when.month <= 6 else when.year + 1


def fetch_financials(ticker: str, *, exchange_suffix: str = ".NS") -> CompanyFinancials:
    """Fetch and canonicalise one company's reported financials."""
    symbol = f"{ticker}{exchange_suffix}"
    types = ",".join(ALL_FIELDS)
    url = (
        f"{_BASE}/{symbol}?symbol={symbol}&type={types}"
        f"&period1=1104537600&period2=1790000000"
    )

    payload = _http_json(url)
    results = (payload.get("timeseries") or {}).get("result") or []
    if not results:
        raise FetchError(f"no fundamentals returned for {symbol}")

    out = CompanyFinancials(ticker=ticker)
    years: set[int] = set()

    for series in results:
        field_name = series.get("meta", {}).get("type", [None])[0]
        if not field_name:
            continue
        points = [p for p in series.get(field_name, []) if p]
        if not points:
            continue

        by_year: dict[int, float] = {}
        for point in points:
            try:
                year = _fiscal_year(point["asOfDate"])
                raw = point["reportedValue"]["raw"]
            except (KeyError, TypeError, ValueError):
                continue
            if raw is None:
                continue
            by_year[year] = float(raw) / CRORE
            years.add(year)

        if not by_year:
            continue

        # Share counts are counts, not money — they must not be divided by a
        # crore as a currency. They are converted to crore *of shares*, which
        # is the same arithmetic but a different meaning, and getting this
        # wrong makes every per-share figure out by seven orders of magnitude.
        item = DIRECT_MAP.get(field_name)
        if item is not None:
            out.facts[item] = by_year
        out.context[field_name] = by_year

    if not years:
        raise FetchError(f"no dated facts for {symbol}")

    out.fiscal_years = sorted(years)
    _derive(out)
    _sign_normalise(out)
    out.price, out.shares_outstanding, out.market_cap = _fetch_quote(symbol, out)
    return out


def _derive(data: CompanyFinancials) -> None:
    """Fill canonical items Yahoo does not report directly.

    Every derivation is stated rather than inferred, because a derived line
    item that nobody can explain is indistinguishable from a bug.
    """
    for year in data.fiscal_years:
        # --- cost of goods ------------------------------------------------
        # Yahoo reports a single `CostOfRevenue`. The workbook splits materials
        # from purchased stock, which no aggregator supplies. Rather than
        # inventing a split, the whole amount is assigned to raw materials and
        # purchases left empty: the sum is what every downstream calculation
        # uses, and a fabricated 70/30 split would be a number with no source.
        cost = data.ctx("annualCostOfRevenue", year)
        if cost is not None:
            data.facts.setdefault(LI.RAW_MATERIALS, {})[year] = cost
            data.facts.setdefault(LI.PURCHASE_STOCK_IN_TRADE, {})[year] = 0.0
            data.facts.setdefault(LI.CHANGE_INVENTORIES, {})[year] = 0.0

        # --- employee benefit ---------------------------------------------
        # Not separately reported. Operating expense less SG&A less
        # depreciation is the closest defensible residual; where that is
        # negative the split is unknowable and the item is left absent rather
        # than clamped to zero, which would assert a fact we do not have.
        opex = data.ctx("annualOperatingExpense", year)
        sga = data.value(LI.OTHER_EXPENSES, year)
        dep = data.value(LI.DEPRECIATION, year)
        if opex is not None and sga is not None:
            residual = opex - sga - (dep or 0.0)
            if residual > 0:
                data.facts.setdefault(LI.EMPLOYEE_BENEFIT, {})[year] = residual

        # --- other income ---------------------------------------------------
        # Pretax income less operating income, net of the interest already
        # captured in finance costs.
        pretax = data.ctx("annualPretaxIncome", year)
        operating = data.ctx("annualOperatingIncome", year)
        if pretax is not None and operating is not None:
            interest = data.value(LI.FINANCE_COSTS, year) or 0.0
            other = pretax - operating + interest
            data.facts.setdefault(LI.OTHER_INCOME, {})[year] = other

        # --- items with no counterpart in the feed ---------------------------
        # Set to zero explicitly rather than left absent: the canonical builder
        # treats absent as "unknown" and zero as "reported nil", and for these
        # the correct statement is that the aggregator carries no such line.
        for item in (
            LI.OTHER_OPERATING_INCOME, LI.EXCEPTIONAL_ITEMS, LI.OCI,
            LI.DEFERRED_TAX_ASSET, LI.LT_INVESTMENTS_ASSOCIATES,
            LI.CURRENT_MATURITIES_LTD, LI.EQUITY_ISSUED_BUYBACK,
            LI.OTHER_WC_MOVEMENT,
        ):
            data.facts.setdefault(item, {}).setdefault(year, 0.0)

    # --- reserves --------------------------------------------------------
    # Retained earnings alone understates reserves for a company with a share
    # premium. Total equity less share capital less minority interest is the
    # correct residual and ties the balance sheet by construction.
    for year in data.fiscal_years:
        equity = (
            data.ctx("annualStockholdersEquity", year)
            or data.ctx("annualTotalEquityGrossMinorityInterest", year)
        )
        capital = data.value(LI.EQUITY_SHARE_CAPITAL, year)
        if equity is not None and capital is not None:
            minority = data.value(LI.MINORITY_INTEREST_BS, year) or 0.0
            data.facts.setdefault(LI.RESERVES_SURPLUS, {})[year] = (
                equity - capital - minority
            )


def _sign_normalise(data: CompanyFinancials) -> None:
    """Apply the workbook's sign convention.

    `32 Documentation` §C: *"Costs are entered as positive numbers and
    subtracted by formula. Cash outflows in the cash-flow statement are
    entered as negative numbers."*

    Yahoo reports capex and dividends as negative (outflows) and repayments
    inconsistently. Getting this wrong flips the sign of free cash flow, which
    flips the sign of the entire DCF — the single most damaging unit error
    available in this domain, and the reason it is handled in one place with
    the convention quoted above it.
    """
    # Costs positive.
    for item in (LI.RAW_MATERIALS, LI.EMPLOYEE_BENEFIT, LI.OTHER_EXPENSES,
                 LI.DEPRECIATION, LI.FINANCE_COSTS, LI.TAX_EXPENSE):
        for year, value in data.facts.get(item, {}).items():
            data.facts[item][year] = abs(value)

    # Capex positive: the workbook's capex schedule treats it as a positive
    # spend and subtracts it, per sheet 12.
    for year, value in data.facts.get(LI.CAPEX, {}).items():
        data.facts[LI.CAPEX][year] = abs(value)

    # Dividends and repayments positive in magnitude; the cash-flow builder
    # applies the sign.
    for item in (LI.DIVIDEND_PAID, LI.REPAYMENT_BORROWINGS,
                 LI.DIRECT_TAXES_PAID):
        for year, value in data.facts.get(item, {}).items():
            data.facts[item][year] = abs(value)


def _fetch_quote(symbol: str, data: CompanyFinancials) -> tuple[float | None, float | None, float | None]:
    """Live price and share count.

    Failure here is tolerated: a company with financials but no quote is still
    worth ingesting, and the missing price is recorded as a warning rather
    than aborting the row.
    """
    try:
        payload = _http_json(f"{_QUOTE}/{symbol}?interval=1d&range=5d", retries=2)
        meta = payload["chart"]["result"][0]["meta"]
        price = meta.get("regularMarketPrice")
        currency = meta.get("currency", "INR")
        if currency != "INR":
            data.warnings.append(f"quote currency is {currency}, not INR")
    except Exception as exc:  # noqa: BLE001
        data.warnings.append(f"quote unavailable: {type(exc).__name__}")
        return None, None, None

    latest = data.latest_year
    shares = None
    if latest is not None:
        raw = data.ctx("annualOrdinarySharesNumber", latest)
        # Share counts came through the same /CRORE conversion as money, which
        # is arithmetically what we want — a count in crore — but it is worth
        # naming, because a stray factor of 1e7 here silently destroys every
        # per-share figure downstream.
        shares = raw

    market_cap = price * shares if (price and shares) else None
    return price, shares, market_cap


def fetch_price_history(ticker: str, *, days: int = 800,
                        exchange_suffix: str = ".NS") -> list[tuple[date, float]]:
    """Daily closes, for Module 8's beta, volatility and liquidity screens."""
    symbol = f"{ticker}{exchange_suffix}"
    payload = _http_json(
        f"{_QUOTE}/{symbol}?interval=1d&range={days}d", retries=2,
    )
    result = payload["chart"]["result"][0]
    stamps = result.get("timestamp") or []
    closes = (result.get("indicators", {}).get("quote") or [{}])[0].get("close") or []

    out: list[tuple[date, float]] = []
    for stamp, close in zip(stamps, closes):
        if close is None:
            continue
        out.append((date.fromtimestamp(stamp), float(close)))
    return out
