"""Canonical financial line items — the 54 items of `0C Data Map`.

GENERATED from Institutional_Equity_Research_Platform_v7.xlsx via
docs/workbook_spec.json. Do not hand-edit; regenerate from the workbook.

The order is significant: it is the store row order of `StoreVals`
('0A Data Import'!$AB$241:$FC$294) and therefore the canonical index.
"""
from __future__ import annotations

from enum import StrEnum
from typing import Final


class Statement(StrEnum):
    """Statement tag — `0C Data Map` column T."""

    PL = "PL"
    BS = "BS"
    CF = "CF"


class LineItem(StrEnum):
    """The 54 canonical line items, in workbook store order."""

    # --- PL ---
    REVENUE = "revenue"
    OTHER_OPERATING_INCOME = "other_operating_income"
    RAW_MATERIALS = "raw_materials"
    PURCHASE_STOCK_IN_TRADE = "purchase_stock_in_trade"
    CHANGE_INVENTORIES = "change_inventories"
    EMPLOYEE_BENEFIT = "employee_benefit"
    OTHER_EXPENSES = "other_expenses"
    DEPRECIATION = "depreciation"
    OTHER_INCOME = "other_income"
    FINANCE_COSTS = "finance_costs"
    EXCEPTIONAL_ITEMS = "exceptional_items"
    TAX_EXPENSE = "tax_expense"
    MINORITY_INTEREST = "minority_interest"
    OCI = "oci"
    DIVIDEND_PAID = "dividend_paid"
    WEIGHTED_SHARES = "weighted_shares"
    # --- BS ---
    CASH_AND_BANK = "cash_and_bank"
    CURRENT_INVESTMENTS = "current_investments"
    TRADE_RECEIVABLES = "trade_receivables"
    INVENTORIES = "inventories"
    OTHER_CURRENT_ASSETS = "other_current_assets"
    NET_BLOCK_PPE = "net_block_ppe"
    CWIP = "cwip"
    GOODWILL = "goodwill"
    OTHER_INTANGIBLES = "other_intangibles"
    LT_INVESTMENTS_ASSOCIATES = "lt_investments_associates"
    OTHER_NCA = "other_nca"
    DEFERRED_TAX_ASSET = "deferred_tax_asset"
    TRADE_PAYABLES = "trade_payables"
    SHORT_TERM_BORROWINGS = "short_term_borrowings"
    CURRENT_MATURITIES_LTD = "current_maturities_ltd"
    OTHER_CURRENT_LIABILITIES = "other_current_liabilities"
    SHORT_TERM_PROVISIONS = "short_term_provisions"
    LONG_TERM_BORROWINGS = "long_term_borrowings"
    DEFERRED_TAX_LIABILITY = "deferred_tax_liability"
    OTHER_NCL = "other_ncl"
    EQUITY_SHARE_CAPITAL = "equity_share_capital"
    RESERVES_SURPLUS = "reserves_surplus"
    MINORITY_INTEREST_BS = "minority_interest_bs"
    # --- CF ---
    OTHER_NONCASH_ADJ = "other_noncash_adj"
    CHG_INVENTORIES_CF = "chg_inventories_cf"
    CHG_RECEIVABLES_CF = "chg_receivables_cf"
    CHG_PAYABLES_CF = "chg_payables_cf"
    OTHER_WC_MOVEMENT = "other_wc_movement"
    DIRECT_TAXES_PAID = "direct_taxes_paid"
    CAPEX = "capex"
    SALE_FIXED_ASSETS = "sale_fixed_assets"
    PURCHASE_SALE_INVESTMENTS = "purchase_sale_investments"
    OTHER_INVESTING = "other_investing"
    EQUITY_ISSUED_BUYBACK = "equity_issued_buyback"
    PROCEEDS_BORROWINGS = "proceeds_borrowings"
    REPAYMENT_BORROWINGS = "repayment_borrowings"
    OTHER_FINANCING = "other_financing"
    OPENING_CASH = "opening_cash"


LINE_ITEM_LABELS: Final[dict[LineItem, str]] = {
    LineItem.REVENUE: "Revenue from operations",
    LineItem.OTHER_OPERATING_INCOME: "Other operating income",
    LineItem.RAW_MATERIALS: "Cost of raw materials consumed",
    LineItem.PURCHASE_STOCK_IN_TRADE: "Purchase of stock-in-trade",
    LineItem.CHANGE_INVENTORIES: "Changes in inventories",
    LineItem.EMPLOYEE_BENEFIT: "Employee benefit expense",
    LineItem.OTHER_EXPENSES: "Other expenses",
    LineItem.DEPRECIATION: "Depreciation & amortisation",
    LineItem.OTHER_INCOME: "Other income (non-operating)",
    LineItem.FINANCE_COSTS: "Finance costs",
    LineItem.EXCEPTIONAL_ITEMS: "Exceptional items",
    LineItem.TAX_EXPENSE: "Total tax expense",
    LineItem.MINORITY_INTEREST: "Minority interest",
    LineItem.OCI: "Other comprehensive income",
    LineItem.DIVIDEND_PAID: "Total dividend paid",
    LineItem.WEIGHTED_SHARES: "Weighted avg shares (crore)",
    LineItem.CASH_AND_BANK: "Cash & bank balances",
    LineItem.CURRENT_INVESTMENTS: "Current investments",
    LineItem.TRADE_RECEIVABLES: "Trade receivables",
    LineItem.INVENTORIES: "Inventories",
    LineItem.OTHER_CURRENT_ASSETS: "Other current assets",
    LineItem.NET_BLOCK_PPE: "Net block — PP&E",
    LineItem.CWIP: "Capital work-in-progress",
    LineItem.GOODWILL: "Goodwill",
    LineItem.OTHER_INTANGIBLES: "Other intangible assets",
    LineItem.LT_INVESTMENTS_ASSOCIATES: "Long-term investments & associates",
    LineItem.OTHER_NCA: "Other non-current assets",
    LineItem.DEFERRED_TAX_ASSET: "Deferred tax asset",
    LineItem.TRADE_PAYABLES: "Trade payables",
    LineItem.SHORT_TERM_BORROWINGS: "Short-term borrowings",
    LineItem.CURRENT_MATURITIES_LTD: "Current maturities of LT debt",
    LineItem.OTHER_CURRENT_LIABILITIES: "Other current liabilities",
    LineItem.SHORT_TERM_PROVISIONS: "Short-term provisions",
    LineItem.LONG_TERM_BORROWINGS: "Long-term borrowings",
    LineItem.DEFERRED_TAX_LIABILITY: "Deferred tax liability",
    LineItem.OTHER_NCL: "Other non-current liabilities",
    LineItem.EQUITY_SHARE_CAPITAL: "Equity share capital",
    LineItem.RESERVES_SURPLUS: "Reserves & surplus",
    LineItem.MINORITY_INTEREST_BS: "Minority interest (BS)",
    LineItem.OTHER_NONCASH_ADJ: "Other non-cash adjustments",
    LineItem.CHG_INVENTORIES_CF: "(Increase)/decrease in inventories",
    LineItem.CHG_RECEIVABLES_CF: "(Increase)/decrease in receivables",
    LineItem.CHG_PAYABLES_CF: "Increase/(decrease) in payables",
    LineItem.OTHER_WC_MOVEMENT: "Other working-capital movement",
    LineItem.DIRECT_TAXES_PAID: "Direct taxes paid",
    LineItem.CAPEX: "Purchase of PP&E (capex)",
    LineItem.SALE_FIXED_ASSETS: "Proceeds from sale of fixed assets",
    LineItem.PURCHASE_SALE_INVESTMENTS: "(Purchase)/sale of investments",
    LineItem.OTHER_INVESTING: "Other investing flows",
    LineItem.EQUITY_ISSUED_BUYBACK: "Equity issued / (buyback)",
    LineItem.PROCEEDS_BORROWINGS: "Proceeds from borrowings",
    LineItem.REPAYMENT_BORROWINGS: "Repayment of borrowings",
    LineItem.OTHER_FINANCING: "Other financing flows",
    LineItem.OPENING_CASH: "Opening cash balance",
}

LINE_ITEM_STATEMENT: Final[dict[LineItem, Statement]] = {
    LineItem.REVENUE: Statement.PL,
    LineItem.OTHER_OPERATING_INCOME: Statement.PL,
    LineItem.RAW_MATERIALS: Statement.PL,
    LineItem.PURCHASE_STOCK_IN_TRADE: Statement.PL,
    LineItem.CHANGE_INVENTORIES: Statement.PL,
    LineItem.EMPLOYEE_BENEFIT: Statement.PL,
    LineItem.OTHER_EXPENSES: Statement.PL,
    LineItem.DEPRECIATION: Statement.PL,
    LineItem.OTHER_INCOME: Statement.PL,
    LineItem.FINANCE_COSTS: Statement.PL,
    LineItem.EXCEPTIONAL_ITEMS: Statement.PL,
    LineItem.TAX_EXPENSE: Statement.PL,
    LineItem.MINORITY_INTEREST: Statement.PL,
    LineItem.OCI: Statement.PL,
    LineItem.DIVIDEND_PAID: Statement.PL,
    LineItem.WEIGHTED_SHARES: Statement.PL,
    LineItem.CASH_AND_BANK: Statement.BS,
    LineItem.CURRENT_INVESTMENTS: Statement.BS,
    LineItem.TRADE_RECEIVABLES: Statement.BS,
    LineItem.INVENTORIES: Statement.BS,
    LineItem.OTHER_CURRENT_ASSETS: Statement.BS,
    LineItem.NET_BLOCK_PPE: Statement.BS,
    LineItem.CWIP: Statement.BS,
    LineItem.GOODWILL: Statement.BS,
    LineItem.OTHER_INTANGIBLES: Statement.BS,
    LineItem.LT_INVESTMENTS_ASSOCIATES: Statement.BS,
    LineItem.OTHER_NCA: Statement.BS,
    LineItem.DEFERRED_TAX_ASSET: Statement.BS,
    LineItem.TRADE_PAYABLES: Statement.BS,
    LineItem.SHORT_TERM_BORROWINGS: Statement.BS,
    LineItem.CURRENT_MATURITIES_LTD: Statement.BS,
    LineItem.OTHER_CURRENT_LIABILITIES: Statement.BS,
    LineItem.SHORT_TERM_PROVISIONS: Statement.BS,
    LineItem.LONG_TERM_BORROWINGS: Statement.BS,
    LineItem.DEFERRED_TAX_LIABILITY: Statement.BS,
    LineItem.OTHER_NCL: Statement.BS,
    LineItem.EQUITY_SHARE_CAPITAL: Statement.BS,
    LineItem.RESERVES_SURPLUS: Statement.BS,
    LineItem.MINORITY_INTEREST_BS: Statement.BS,
    LineItem.OTHER_NONCASH_ADJ: Statement.CF,
    LineItem.CHG_INVENTORIES_CF: Statement.CF,
    LineItem.CHG_RECEIVABLES_CF: Statement.CF,
    LineItem.CHG_PAYABLES_CF: Statement.CF,
    LineItem.OTHER_WC_MOVEMENT: Statement.CF,
    LineItem.DIRECT_TAXES_PAID: Statement.CF,
    LineItem.CAPEX: Statement.CF,
    LineItem.SALE_FIXED_ASSETS: Statement.CF,
    LineItem.PURCHASE_SALE_INVESTMENTS: Statement.CF,
    LineItem.OTHER_INVESTING: Statement.CF,
    LineItem.EQUITY_ISSUED_BUYBACK: Statement.CF,
    LineItem.PROCEEDS_BORROWINGS: Statement.CF,
    LineItem.REPAYMENT_BORROWINGS: Statement.CF,
    LineItem.OTHER_FINANCING: Statement.CF,
    LineItem.OPENING_CASH: Statement.CF,
}

#: Canonical 1-based index, matching the `StoreVals` row offset.
LINE_ITEM_INDEX: Final[dict[LineItem, int]] = {
    LineItem.REVENUE: 1,
    LineItem.OTHER_OPERATING_INCOME: 2,
    LineItem.RAW_MATERIALS: 3,
    LineItem.PURCHASE_STOCK_IN_TRADE: 4,
    LineItem.CHANGE_INVENTORIES: 5,
    LineItem.EMPLOYEE_BENEFIT: 6,
    LineItem.OTHER_EXPENSES: 7,
    LineItem.DEPRECIATION: 8,
    LineItem.OTHER_INCOME: 9,
    LineItem.FINANCE_COSTS: 10,
    LineItem.EXCEPTIONAL_ITEMS: 11,
    LineItem.TAX_EXPENSE: 12,
    LineItem.MINORITY_INTEREST: 13,
    LineItem.OCI: 14,
    LineItem.DIVIDEND_PAID: 15,
    LineItem.WEIGHTED_SHARES: 16,
    LineItem.CASH_AND_BANK: 17,
    LineItem.CURRENT_INVESTMENTS: 18,
    LineItem.TRADE_RECEIVABLES: 19,
    LineItem.INVENTORIES: 20,
    LineItem.OTHER_CURRENT_ASSETS: 21,
    LineItem.NET_BLOCK_PPE: 22,
    LineItem.CWIP: 23,
    LineItem.GOODWILL: 24,
    LineItem.OTHER_INTANGIBLES: 25,
    LineItem.LT_INVESTMENTS_ASSOCIATES: 26,
    LineItem.OTHER_NCA: 27,
    LineItem.DEFERRED_TAX_ASSET: 28,
    LineItem.TRADE_PAYABLES: 29,
    LineItem.SHORT_TERM_BORROWINGS: 30,
    LineItem.CURRENT_MATURITIES_LTD: 31,
    LineItem.OTHER_CURRENT_LIABILITIES: 32,
    LineItem.SHORT_TERM_PROVISIONS: 33,
    LineItem.LONG_TERM_BORROWINGS: 34,
    LineItem.DEFERRED_TAX_LIABILITY: 35,
    LineItem.OTHER_NCL: 36,
    LineItem.EQUITY_SHARE_CAPITAL: 37,
    LineItem.RESERVES_SURPLUS: 38,
    LineItem.MINORITY_INTEREST_BS: 39,
    LineItem.OTHER_NONCASH_ADJ: 40,
    LineItem.CHG_INVENTORIES_CF: 41,
    LineItem.CHG_RECEIVABLES_CF: 42,
    LineItem.CHG_PAYABLES_CF: 43,
    LineItem.OTHER_WC_MOVEMENT: 44,
    LineItem.DIRECT_TAXES_PAID: 45,
    LineItem.CAPEX: 46,
    LineItem.SALE_FIXED_ASSETS: 47,
    LineItem.PURCHASE_SALE_INVESTMENTS: 48,
    LineItem.OTHER_INVESTING: 49,
    LineItem.EQUITY_ISSUED_BUYBACK: 50,
    LineItem.PROCEEDS_BORROWINGS: 51,
    LineItem.REPAYMENT_BORROWINGS: 52,
    LineItem.OTHER_FINANCING: 53,
    LineItem.OPENING_CASH: 54,
}

CANONICAL_ORDER: Final[tuple[LineItem, ...]] = tuple(LINE_ITEM_INDEX)

ITEMS_BY_STATEMENT: Final[dict[Statement, tuple[LineItem, ...]]] = {
    Statement.PL: (
        LineItem.REVENUE,
        LineItem.OTHER_OPERATING_INCOME,
        LineItem.RAW_MATERIALS,
        LineItem.PURCHASE_STOCK_IN_TRADE,
        LineItem.CHANGE_INVENTORIES,
        LineItem.EMPLOYEE_BENEFIT,
        LineItem.OTHER_EXPENSES,
        LineItem.DEPRECIATION,
        LineItem.OTHER_INCOME,
        LineItem.FINANCE_COSTS,
        LineItem.EXCEPTIONAL_ITEMS,
        LineItem.TAX_EXPENSE,
        LineItem.MINORITY_INTEREST,
        LineItem.OCI,
        LineItem.DIVIDEND_PAID,
        LineItem.WEIGHTED_SHARES,
    ),
    Statement.BS: (
        LineItem.CASH_AND_BANK,
        LineItem.CURRENT_INVESTMENTS,
        LineItem.TRADE_RECEIVABLES,
        LineItem.INVENTORIES,
        LineItem.OTHER_CURRENT_ASSETS,
        LineItem.NET_BLOCK_PPE,
        LineItem.CWIP,
        LineItem.GOODWILL,
        LineItem.OTHER_INTANGIBLES,
        LineItem.LT_INVESTMENTS_ASSOCIATES,
        LineItem.OTHER_NCA,
        LineItem.DEFERRED_TAX_ASSET,
        LineItem.TRADE_PAYABLES,
        LineItem.SHORT_TERM_BORROWINGS,
        LineItem.CURRENT_MATURITIES_LTD,
        LineItem.OTHER_CURRENT_LIABILITIES,
        LineItem.SHORT_TERM_PROVISIONS,
        LineItem.LONG_TERM_BORROWINGS,
        LineItem.DEFERRED_TAX_LIABILITY,
        LineItem.OTHER_NCL,
        LineItem.EQUITY_SHARE_CAPITAL,
        LineItem.RESERVES_SURPLUS,
        LineItem.MINORITY_INTEREST_BS,
    ),
    Statement.CF: (
        LineItem.OTHER_NONCASH_ADJ,
        LineItem.CHG_INVENTORIES_CF,
        LineItem.CHG_RECEIVABLES_CF,
        LineItem.CHG_PAYABLES_CF,
        LineItem.OTHER_WC_MOVEMENT,
        LineItem.DIRECT_TAXES_PAID,
        LineItem.CAPEX,
        LineItem.SALE_FIXED_ASSETS,
        LineItem.PURCHASE_SALE_INVESTMENTS,
        LineItem.OTHER_INVESTING,
        LineItem.EQUITY_ISSUED_BUYBACK,
        LineItem.PROCEEDS_BORROWINGS,
        LineItem.REPAYMENT_BORROWINGS,
        LineItem.OTHER_FINANCING,
        LineItem.OPENING_CASH,
    ),
}

assert len(CANONICAL_ORDER) == 54, "workbook defines exactly 54 canonical items"
