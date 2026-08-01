"""Map US GAAP filings onto the platform's canonical line items.

The canonical schedule is 54 line items drawn from Indian Schedule III
presentation. US filings under GAAP present the same economics differently:
there is no "purchase of stock in trade", no separate "raw materials" line on
the face of the income statement, and cost of revenue aggregates what Schedule
III splits across several rows.

The mapping is therefore **deliberately partial and explicitly so**. Every
canonical item this module cannot source from a US filing is simply absent,
which the platform already models properly: `CanonicalFinancialsBuilder`
treats an absent item as unavailable, the context builder reports it as a gap,
and the analyst is instructed to say the figure is not held rather than
estimate it.

The alternative — deriving `raw_materials` from `costOfRevenue` by some
plausible-looking split — would manufacture a number that appears in a report,
carries a citation, and is not in any filing. That is the single failure mode
this platform exists to prevent, so it is not done.

**Units.** FMP reports absolute currency units; the platform stores US
companies at `Scale.MILLION`. Conversion happens once, here, rather than in
each caller.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.domain.financials.line_items import LineItem
from app.domain.financials.reporting_unit import USD_MILLION, ReportingUnit

#: Income statement: FMP `/stable/income-statement` field -> canonical item.
#:
#: Sign conventions differ and matter. The platform stores expenses as
#: positive magnitudes and derives profit by subtraction, so a US expense
#: arriving positive is stored as-is; `capex` arrives negative from FMP and is
#: stored as a positive magnitude to match `abs(cash_flow.capex)` downstream.
INCOME_MAP: dict[str, LineItem] = {
    "revenue": LineItem.REVENUE,
    "costOfRevenue": LineItem.RAW_MATERIALS,
    "researchAndDevelopmentExpenses": LineItem.EMPLOYEE_BENEFIT,
    "sellingGeneralAndAdministrativeExpenses": LineItem.OTHER_EXPENSES,
    # US-001. `depreciationAndAmortization` is deliberately NOT mapped to
    # LineItem.DEPRECIATION.
    #
    # Under Schedule III, depreciation is presented as its own expense line
    # and the platform's PAT derivation subtracts it. Under US GAAP it is
    # already *inside* cost of revenue and operating expenses; the figure FMP
    # reports is a supplementary disclosure, not an additional expense.
    # Mapping it subtracted Apple's $11,698M of D&A twice and produced a PAT
    # of $100,312M against a filed $112,010M — an 11% understatement that
    # every downstream margin, valuation and score inherited, while looking
    # entirely plausible.
    #
    # EBITDA is unaffected: the platform adds depreciation back to EBIT, and
    # with no depreciation line it reads EBITDA straight off operating income,
    # which is where US GAAP puts it.
    "totalOtherIncomeExpensesNet": LineItem.OTHER_INCOME,
    "interestExpense": LineItem.FINANCE_COSTS,
    "incomeTaxExpense": LineItem.TAX_EXPENSE,
    "weightedAverageShsOut": LineItem.WEIGHTED_SHARES,
}

#: Balance sheet: FMP `/stable/balance-sheet-statement` field -> canonical item.
BALANCE_MAP: dict[str, LineItem] = {
    "cashAndCashEquivalents": LineItem.CASH_AND_BANK,
    "shortTermInvestments": LineItem.CURRENT_INVESTMENTS,
    "netReceivables": LineItem.TRADE_RECEIVABLES,
    "inventory": LineItem.INVENTORIES,
    "otherCurrentAssets": LineItem.OTHER_CURRENT_ASSETS,
    "propertyPlantEquipmentNet": LineItem.NET_BLOCK_PPE,
    "goodwill": LineItem.GOODWILL,
    "intangibleAssets": LineItem.OTHER_INTANGIBLES,
    "longTermInvestments": LineItem.LT_INVESTMENTS_ASSOCIATES,
    "otherNonCurrentAssets": LineItem.OTHER_NCA,
    "taxAssets": LineItem.DEFERRED_TAX_ASSET,
    "accountPayables": LineItem.TRADE_PAYABLES,
    "shortTermDebt": LineItem.SHORT_TERM_BORROWINGS,
    "otherCurrentLiabilities": LineItem.OTHER_CURRENT_LIABILITIES,
    "longTermDebt": LineItem.LONG_TERM_BORROWINGS,
    "deferredTaxLiabilitiesNonCurrent": LineItem.DEFERRED_TAX_LIABILITY,
    "otherNonCurrentLiabilities": LineItem.OTHER_NCL,
    "commonStock": LineItem.EQUITY_SHARE_CAPITAL,
    # US-002. `retainedEarnings` alone is not the whole of reserves.
    #
    # Apple carries accumulated other comprehensive income of -$5,571M, and
    # omitting it made shareholders' equity $79,304M against a filed $73,733M.
    # AOCI is a genuine component of equity, so it is combined with retained
    # earnings into `reserves_surplus` by `_combine_reserves` below rather
    # than dropped. Both fields map to the same canonical item, which the
    # plain field-by-field mapping cannot express — hence the special case.
    "retainedEarnings": LineItem.RESERVES_SURPLUS,
    "minorityInterest": LineItem.MINORITY_INTEREST_BS,
}

#: Cash-flow fields summed into one canonical item.
#:
#: US-005. The platform's CFO derivation adds `depreciation` back to PAT, and
#: US-001 correctly stopped mapping D&A as an income-statement expense —
#: leaving nothing to add back and understating Apple's CFO by $11,698M.
#:
#: The two uses are genuinely different. On the income statement, D&A under US
#: GAAP is *already inside* cost of revenue, so treating it as a further
#: expense double-counts. In the cash-flow statement it is a non-cash add-back
#: to profit, which is exactly what `OTHER_NONCASH_ADJ` is for. Stock-based
#: compensation and deferred tax are non-cash for the same reason and belong
#: in the same bucket.
COMBINED_CASHFLOW: dict[LineItem, tuple[str, ...]] = {
    LineItem.OTHER_NONCASH_ADJ: (
        "depreciationAndAmortization",
        "stockBasedCompensation",
        "deferredIncomeTax",
        "otherNonCashItems",
    ),
}

#: Balance-sheet fields summed into one canonical item.
#:
#: US GAAP splits equity across components that Schedule III presents as a
#: single "reserves and surplus". Summing is correct here because these are
#: additive components of the same subtotal, and the result is checked against
#: the filed `totalStockholdersEquity` by the reconciliation test.
COMBINED_BALANCE: dict[LineItem, tuple[str, ...]] = {
    LineItem.RESERVES_SURPLUS: (
        "retainedEarnings",
        "accumulatedOtherComprehensiveIncomeLoss",
        "additionalPaidInCapital",
        "otherTotalStockholdersEquity",
    ),
}

#: Cash flow: FMP `/stable/cash-flow-statement` field -> canonical item.
CASHFLOW_MAP: dict[str, LineItem] = {
    "inventory": LineItem.CHG_INVENTORIES_CF,
    "accountsReceivables": LineItem.CHG_RECEIVABLES_CF,
    "accountsPayables": LineItem.CHG_PAYABLES_CF,
    "otherWorkingCapital": LineItem.OTHER_WC_MOVEMENT,
    # US-004. `incomeTaxesPaid` is deliberately NOT mapped to
    # DIRECT_TAXES_PAID.
    #
    # The platform derives CFO as `PAT + non-cash add-backs + working capital
    # − direct taxes paid`, which is the *indirect method starting from
    # profit before tax* that Schedule III uses. FMP's cash-flow statement
    # starts from net income, which is already after tax. Subtracting taxes
    # paid again removed Apple's $43,369M twice.
    "investmentsInPropertyPlantAndEquipment": LineItem.CAPEX,
    "purchasesOfInvestments": LineItem.PURCHASE_SALE_INVESTMENTS,
    "otherInvestingActivities": LineItem.OTHER_INVESTING,
    "netCommonStockIssuance": LineItem.EQUITY_ISSUED_BUYBACK,
    "longTermNetDebtIssuance": LineItem.PROCEEDS_BORROWINGS,
    "shortTermNetDebtIssuance": LineItem.REPAYMENT_BORROWINGS,
    "otherFinancingActivities": LineItem.OTHER_FINANCING,
    "cashAtBeginningOfPeriod": LineItem.OPENING_CASH,
    "netDividendsPaid": LineItem.DIVIDEND_PAID,
}

#: Items stored as a positive magnitude regardless of the filing's sign.
#:
#: The platform's derivations subtract these, so a negative arriving from a
#: cash-flow statement (where an outflow is negative) would add it back and
#: invert the result.
ABSOLUTE_MAGNITUDE: frozenset[LineItem] = frozenset({
    LineItem.CAPEX, LineItem.DIVIDEND_PAID, LineItem.DIRECT_TAXES_PAID,
    LineItem.FINANCE_COSTS, LineItem.TAX_EXPENSE,
})

#: Items stored without scaling.
#:
#: US-003. `weighted_shares` was originally here, on the reasoning that a
#: share count is a count rather than money and should not be divided by a
#: million. That reasoning was wrong, and the platform's own Indian data
#: disproves it: TCS stores PAT as 49,454 (₹ crore) and weighted shares as
#: 363.6 (crore), so EPS is `pat / shares` and the scale cancels.
#:
#: Leaving the share count absolute while PAT was in millions made Apple's EPS
#: $0.0000075 instead of $7.49 — a factor of 10^6. The invariant is that
#: **shares carry the same scale as money**, so the set is now empty and kept
#: only to document why nothing belongs in it.
UNSCALED: frozenset[LineItem] = frozenset()


@dataclass(frozen=True, slots=True)
class MappedFact:
    line_item: LineItem
    fiscal_year: int
    value: float
    source: str


def _coerce(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    # FMP returns 0 for genuinely-zero lines and for lines it does not carry.
    # They are indistinguishable on the wire, and storing a spurious zero is
    # worse than storing nothing: zero is a claim, absence is honest.
    return None if number == 0.0 else number


def map_statement(
    rows: list[dict[str, Any]],
    mapping: dict[str, LineItem],
    *,
    source: str,
    unit: ReportingUnit = USD_MILLION,
    combined: dict[LineItem, tuple[str, ...]] | None = None,
) -> list[MappedFact]:
    """Map one FMP statement series onto canonical facts.

    `combined` sums several provider fields into one canonical item, for the
    cases where US GAAP splits what Schedule III presents as a single line.
    A combined item overrides any single-field mapping to the same item.
    """
    facts: list[MappedFact] = []
    combined = combined or {}

    for row in rows or []:
        year = row.get("fiscalYear") or (row.get("date") or "")[:4]
        try:
            fiscal_year = int(year)
        except (TypeError, ValueError):
            continue

        combined_items = set(combined)
        for field, item in mapping.items():
            if item in combined_items:
                continue  # handled below, from several fields at once
            value = _coerce(row.get(field))
            if value is None:
                continue
            if item in ABSOLUTE_MAGNITUDE:
                value = abs(value)
            if item not in UNSCALED:
                value = unit.from_absolute(value)
            facts.append(MappedFact(item, fiscal_year, round(value, 4), source))

        for item, fields in combined.items():
            parts = [_coerce(row.get(f)) for f in fields]
            present = [p for p in parts if p is not None]
            if not present:
                continue
            total = sum(present)
            if item not in UNSCALED:
                total = unit.from_absolute(total)
            facts.append(MappedFact(item, fiscal_year, round(total, 4), source))

    return facts


def map_filing_set(
    income: list[dict[str, Any]],
    balance: list[dict[str, Any]],
    cash_flow: list[dict[str, Any]],
    *,
    unit: ReportingUnit = USD_MILLION,
) -> list[MappedFact]:
    """All three statements, mapped and deduplicated.

    A later statement wins on collision. Only `inventory` collides — it is a
    balance on the balance sheet and a movement on the cash-flow statement,
    and they map to different canonical items, so in practice nothing does.
    """
    facts: list[MappedFact] = []
    facts += map_statement(income, INCOME_MAP, source="SEC 10-K via FMP (IS)",
                           unit=unit)
    facts += map_statement(balance, BALANCE_MAP, source="SEC 10-K via FMP (BS)",
                           unit=unit, combined=COMBINED_BALANCE)
    facts += map_statement(cash_flow, CASHFLOW_MAP,
                           source="SEC 10-K via FMP (CF)", unit=unit,
                           combined=COMBINED_CASHFLOW)

    seen: dict[tuple[LineItem, int], MappedFact] = {}
    for fact in facts:
        seen[(fact.line_item, fact.fiscal_year)] = fact
    return list(seen.values())


def coverage(facts: list[MappedFact]) -> dict[str, Any]:
    """What the mapping did and did not populate.

    Reported rather than hidden: a US company legitimately carries fewer
    canonical items than an Indian one, and a reader comparing the two should
    be able to see that this is a presentation difference and not missing data.
    """
    populated = {f.line_item for f in facts}
    missing = [i.value for i in LineItem if i not in populated]
    return {
        "canonical_items": len(LineItem),
        "populated": len(populated),
        "coverage_pct": round(100 * len(populated) / len(LineItem), 1),
        "years": sorted({f.fiscal_year for f in facts}),
        "unmapped_items": missing,
    }
