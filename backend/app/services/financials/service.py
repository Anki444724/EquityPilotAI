"""Financial statement presentation service.

Builds the income statement, balance sheet and cash-flow payloads.

The arithmetic lives in ``app.domain.financials.statements`` (Module 1) and is
NOT repeated here. This service's only job is to select, label and order the
computed values for transport. That separation is what lets the same engine
feed ratios, valuation and reports without any figure being computed twice.
"""
from __future__ import annotations

from app.domain.calc import cagr, growth
from app.domain.financials.canonical import CanonicalFinancials
from app.domain.financials.statements import (
    BalanceSheet,
    CashFlowStatement,
    IncomeStatement,
    build_balance_sheet,
    build_cash_flow,
    build_income_statement,
)
from app.schemas.common import MetricRow, MetricSection, Unit

# --------------------------------------------------------------------------
# Statement layouts.
#
# Each entry: (key, label, attribute, unit, is_subtotal, indent)
# Declarative so the ordering is data, not control flow — adding a line is a
# list edit, never a code change.
# --------------------------------------------------------------------------

_IS_REVENUE = [
    ("revenue_operations", "Revenue from operations", "revenue_operations", Unit.CRORE, False, 0),
    ("other_operating_income", "Other operating income", "other_operating_income", Unit.CRORE, False, 0),
    ("total_revenue", "Total revenue", "total_revenue", Unit.CRORE, True, 0),
]

_IS_COGS = [
    ("raw_materials", "Cost of raw materials consumed", "raw_materials", Unit.CRORE, False, 1),
    ("purchase_stock_in_trade", "Purchase of stock-in-trade", "purchase_stock_in_trade", Unit.CRORE, False, 1),
    ("change_inventories", "Changes in inventories", "change_inventories", Unit.CRORE, False, 1),
    ("total_cogs", "Total cost of goods sold", "total_cogs", Unit.CRORE, True, 0),
    ("gross_profit", "Gross profit", "gross_profit", Unit.CRORE, True, 0),
]

_IS_OPEX = [
    ("employee_benefit", "Employee benefit expense", "employee_benefit", Unit.CRORE, False, 1),
    ("other_expenses", "Other expenses", "other_expenses", Unit.CRORE, False, 1),
    ("total_opex", "Total operating expenditure", "total_opex", Unit.CRORE, True, 0),
    ("ebitda", "EBITDA", "ebitda", Unit.CRORE, True, 0),
]

_IS_PROFIT = [
    ("depreciation", "Depreciation & amortisation", "depreciation", Unit.CRORE, False, 1),
    ("ebit", "EBIT (operating profit)", "ebit", Unit.CRORE, True, 0),
    ("other_income", "Other income (non-operating)", "other_income", Unit.CRORE, False, 1),
    ("finance_costs", "Finance costs", "finance_costs", Unit.CRORE, False, 1),
    ("pbt_before_exceptional", "PBT before exceptional items", "pbt_before_exceptional", Unit.CRORE, True, 0),
    ("exceptional_items", "Exceptional items", "exceptional_items", Unit.CRORE, False, 1),
    ("pbt", "Profit before tax", "pbt", Unit.CRORE, True, 0),
    ("tax_expense", "Total tax expense", "tax_expense", Unit.CRORE, False, 1),
    ("pat_before_minority", "PAT before minority interest", "pat_before_minority", Unit.CRORE, True, 0),
    ("minority_interest", "Less: minority interest", "minority_interest", Unit.CRORE, False, 1),
    ("pat", "PAT attributable to owners", "pat", Unit.CRORE, True, 0),
    ("oci", "Other comprehensive income", "oci", Unit.CRORE, False, 1),
    ("total_comprehensive_income", "Total comprehensive income", "total_comprehensive_income", Unit.CRORE, True, 0),
]

_IS_PER_SHARE = [
    ("dividend_paid", "Total dividend paid", "dividend_paid", Unit.CRORE, False, 0),
    ("weighted_shares", "Weighted average shares", "weighted_shares", Unit.COUNT, False, 0),
    ("eps_basic", "EPS — basic", "eps_basic", Unit.RUPEES, False, 0),
    ("eps_diluted", "EPS — diluted", "eps_diluted", Unit.RUPEES, False, 0),
    ("dividend_per_share", "Dividend per share", "dividend_per_share", Unit.RUPEES, False, 0),
]

_IS_MARGINS = [
    ("gross_margin", "Gross margin", "gross_margin", Unit.PERCENT, False, 0),
    ("ebitda_margin", "EBITDA margin", "ebitda_margin", Unit.PERCENT, False, 0),
    ("ebit_margin", "EBIT margin", "ebit_margin", Unit.PERCENT, False, 0),
    ("pat_margin", "Net profit margin", "pat_margin", Unit.PERCENT, False, 0),
    ("effective_tax_rate", "Effective tax rate", "effective_tax_rate", Unit.PERCENT, False, 0),
]

_BS_CURRENT_ASSETS = [
    ("cash_and_bank", "Cash & bank balances", "cash_and_bank", Unit.CRORE, False, 1),
    ("current_investments", "Current investments", "current_investments", Unit.CRORE, False, 1),
    ("trade_receivables", "Trade receivables", "trade_receivables", Unit.CRORE, False, 1),
    ("inventories", "Inventories", "inventories", Unit.CRORE, False, 1),
    ("other_current_assets", "Other current assets", "other_current_assets", Unit.CRORE, False, 1),
    ("total_current_assets", "Total current assets", "total_current_assets", Unit.CRORE, True, 0),
]

_BS_NON_CURRENT_ASSETS = [
    ("net_block_ppe", "Net block — PP&E", "net_block_ppe", Unit.CRORE, False, 1),
    ("cwip", "Capital work-in-progress", "cwip", Unit.CRORE, False, 1),
    ("goodwill", "Goodwill", "goodwill", Unit.CRORE, False, 1),
    ("other_intangibles", "Other intangible assets", "other_intangibles", Unit.CRORE, False, 1),
    ("lt_investments_associates", "Long-term investments & associates", "lt_investments_associates", Unit.CRORE, False, 1),
    ("other_nca", "Other non-current assets", "other_nca", Unit.CRORE, False, 1),
    ("deferred_tax_asset", "Deferred tax asset", "deferred_tax_asset", Unit.CRORE, False, 1),
    ("total_non_current_assets", "Total non-current assets", "total_non_current_assets", Unit.CRORE, True, 0),
    ("total_assets", "TOTAL ASSETS", "total_assets", Unit.CRORE, True, 0),
]

_BS_CURRENT_LIABS = [
    ("trade_payables", "Trade payables", "trade_payables", Unit.CRORE, False, 1),
    ("short_term_borrowings", "Short-term borrowings", "short_term_borrowings", Unit.CRORE, False, 1),
    ("current_maturities_ltd", "Current maturities of LT debt", "current_maturities_ltd", Unit.CRORE, False, 1),
    ("other_current_liabilities", "Other current liabilities", "other_current_liabilities", Unit.CRORE, False, 1),
    ("short_term_provisions", "Short-term provisions", "short_term_provisions", Unit.CRORE, False, 1),
    ("total_current_liabilities", "Total current liabilities", "total_current_liabilities", Unit.CRORE, True, 0),
]

_BS_NON_CURRENT_LIABS = [
    ("long_term_borrowings", "Long-term borrowings", "long_term_borrowings", Unit.CRORE, False, 1),
    ("deferred_tax_liability", "Deferred tax liability", "deferred_tax_liability", Unit.CRORE, False, 1),
    ("other_ncl", "Other non-current liabilities", "other_ncl", Unit.CRORE, False, 1),
    ("total_non_current_liabilities", "Total non-current liabilities", "total_non_current_liabilities", Unit.CRORE, True, 0),
    ("total_liabilities", "Total liabilities", "total_liabilities", Unit.CRORE, True, 0),
]

_BS_EQUITY = [
    ("equity_share_capital", "Equity share capital", "equity_share_capital", Unit.CRORE, False, 1),
    ("reserves_surplus", "Reserves & surplus", "reserves_surplus", Unit.CRORE, False, 1),
    ("shareholders_equity", "Shareholders' equity", "shareholders_equity", Unit.CRORE, True, 0),
    ("minority_interest", "Minority interest", "minority_interest", Unit.CRORE, False, 1),
    ("total_equity", "Total equity", "total_equity", Unit.CRORE, True, 0),
    ("total_equity_and_liabilities", "TOTAL EQUITY & LIABILITIES", "total_equity_and_liabilities", Unit.CRORE, True, 0),
]

_BS_DERIVED = [
    ("gross_debt", "Gross debt", "gross_debt", Unit.CRORE, False, 0),
    ("net_debt", "Net debt", "net_debt", Unit.CRORE, False, 0),
    ("capital_employed", "Capital employed", "capital_employed", Unit.CRORE, False, 0),
    ("invested_capital", "Invested capital", "invested_capital", Unit.CRORE, False, 0),
    ("net_working_capital", "Net working capital", "net_working_capital", Unit.CRORE, False, 0),
    ("balance_check", "Balance check (assets − equity & liabilities)", "balance_check", Unit.CRORE, False, 0),
]

_CF_OPERATING = [
    ("pat", "Profit after tax", "pat", Unit.CRORE, False, 1),
    ("depreciation", "Add: depreciation & amortisation", "depreciation", Unit.CRORE, False, 1),
    ("finance_costs", "Add: finance costs", "finance_costs", Unit.CRORE, False, 1),
    ("other_noncash_adj", "Other non-cash adjustments", "other_noncash_adj", Unit.CRORE, False, 1),
    ("operating_profit_before_wc", "Operating profit before WC changes", "operating_profit_before_wc", Unit.CRORE, True, 0),
    ("chg_inventories", "(Increase)/decrease in inventories", "chg_inventories", Unit.CRORE, False, 1),
    ("chg_receivables", "(Increase)/decrease in receivables", "chg_receivables", Unit.CRORE, False, 1),
    ("chg_payables", "Increase/(decrease) in payables", "chg_payables", Unit.CRORE, False, 1),
    ("other_wc_movement", "Other working-capital movement", "other_wc_movement", Unit.CRORE, False, 1),
    ("working_capital_change", "Total working-capital change", "working_capital_change", Unit.CRORE, True, 0),
    ("direct_taxes_paid", "Less: direct taxes paid", "direct_taxes_paid", Unit.CRORE, False, 1),
    ("cfo", "CASH FLOW FROM OPERATIONS", "cfo", Unit.CRORE, True, 0),
]

_CF_INVESTING = [
    ("capex", "Purchase of PP&E (capex)", "capex", Unit.CRORE, False, 1),
    ("sale_fixed_assets", "Proceeds from sale of fixed assets", "sale_fixed_assets", Unit.CRORE, False, 1),
    ("purchase_sale_investments", "(Purchase)/sale of investments", "purchase_sale_investments", Unit.CRORE, False, 1),
    ("other_investing", "Other investing flows", "other_investing", Unit.CRORE, False, 1),
    ("cfi", "CASH FLOW FROM INVESTING", "cfi", Unit.CRORE, True, 0),
]

_CF_FINANCING = [
    ("equity_issued_buyback", "Equity issued / (buyback)", "equity_issued_buyback", Unit.CRORE, False, 1),
    ("proceeds_borrowings", "Proceeds from borrowings", "proceeds_borrowings", Unit.CRORE, False, 1),
    ("repayment_borrowings", "Repayment of borrowings", "repayment_borrowings", Unit.CRORE, False, 1),
    ("dividend_paid", "Dividend paid", "dividend_paid", Unit.CRORE, False, 1),
    ("interest_paid", "Interest paid", "interest_paid", Unit.CRORE, False, 1),
    ("other_financing", "Other financing flows", "other_financing", Unit.CRORE, False, 1),
    ("cff", "CASH FLOW FROM FINANCING", "cff", Unit.CRORE, True, 0),
]

_CF_RECONCILIATION = [
    ("net_cash_flow", "Net increase/(decrease) in cash", "net_cash_flow", Unit.CRORE, True, 0),
    ("opening_cash", "Opening cash balance", "opening_cash", Unit.CRORE, False, 0),
    ("closing_cash", "Closing cash balance", "closing_cash", Unit.CRORE, True, 0),
]

_CF_QUALITY = [
    ("free_cash_flow", "Free cash flow (CFO − capex)", "free_cash_flow", Unit.CRORE, False, 0),
    ("fcf_to_equity", "Free cash flow to equity", "fcf_to_equity", Unit.CRORE, False, 0),
    ("cfo_to_pat", "CFO / PAT (accrual quality)", "cfo_to_pat", Unit.MULTIPLE, False, 0),
]


def _rows(layout, objects: list[object]) -> list[MetricRow]:
    """Project a layout across the period objects."""
    out: list[MetricRow] = []
    for key, label, attr, unit, subtotal, indent in layout:
        out.append(
            MetricRow(
                key=key,
                label=label,
                unit=unit,
                values=[getattr(o, attr, None) for o in objects],
                is_subtotal=subtotal,
                indent=indent,
            )
        )
    return out


class FinancialStatementsService:
    """Produces statement sections from resolved canonical financials."""

    def __init__(self, financials: CanonicalFinancials) -> None:
        self.fin = financials
        self.years = list(financials.fiscal_years)

    # ---------------------------------------------------------- computation
    def income_statements(self) -> list[IncomeStatement]:
        return [build_income_statement(self.fin, y) for y in self.years]

    def balance_sheets(self) -> list[BalanceSheet]:
        return [build_balance_sheet(self.fin, y) for y in self.years]

    def cash_flows(self) -> list[CashFlowStatement]:
        return [build_cash_flow(self.fin, y) for y in self.years]

    # -------------------------------------------------------------- sections
    def income_statement_sections(self) -> list[MetricSection]:
        st = self.income_statements()
        revenue = [s.total_revenue for s in st]
        ebitda = [s.ebitda for s in st]
        pat = [s.pat for s in st]

        growth_rows = [
            MetricRow(
                key="revenue_growth", label="Revenue growth (YoY)", unit=Unit.PERCENT,
                values=[None] + [growth(revenue[i], revenue[i - 1]) for i in range(1, len(revenue))],
            ),
            MetricRow(
                key="ebitda_growth", label="EBITDA growth (YoY)", unit=Unit.PERCENT,
                values=[None] + [growth(ebitda[i], ebitda[i - 1]) for i in range(1, len(ebitda))],
            ),
            MetricRow(
                key="pat_growth", label="PAT growth (YoY)", unit=Unit.PERCENT,
                values=[None] + [growth(pat[i], pat[i - 1]) for i in range(1, len(pat))],
            ),
        ]

        return [
            MetricSection(key="revenue", title="Revenue", rows=_rows(_IS_REVENUE, st)),
            MetricSection(key="cogs", title="Cost of goods sold", rows=_rows(_IS_COGS, st)),
            MetricSection(key="opex", title="Operating expenditure", rows=_rows(_IS_OPEX, st)),
            MetricSection(key="profit", title="Profit bridge", rows=_rows(_IS_PROFIT, st)),
            MetricSection(key="per_share", title="Dividend & per-share data", rows=_rows(_IS_PER_SHARE, st)),
            MetricSection(key="margins", title="Profitability margins", rows=_rows(_IS_MARGINS, st)),
            MetricSection(key="growth", title="Growth analysis", rows=growth_rows),
        ]

    def balance_sheet_sections(self) -> list[MetricSection]:
        bs = self.balance_sheets()
        return [
            MetricSection(key="current_assets", title="Current assets", rows=_rows(_BS_CURRENT_ASSETS, bs)),
            MetricSection(key="non_current_assets", title="Non-current assets", rows=_rows(_BS_NON_CURRENT_ASSETS, bs)),
            MetricSection(key="current_liabilities", title="Current liabilities", rows=_rows(_BS_CURRENT_LIABS, bs)),
            MetricSection(key="non_current_liabilities", title="Non-current liabilities", rows=_rows(_BS_NON_CURRENT_LIABS, bs)),
            MetricSection(key="equity", title="Equity", rows=_rows(_BS_EQUITY, bs)),
            MetricSection(key="derived", title="Derived capital measures", rows=_rows(_BS_DERIVED, bs)),
        ]

    def cash_flow_sections(self) -> list[MetricSection]:
        cf = self.cash_flows()
        return [
            MetricSection(key="operating", title="Operating activities", rows=_rows(_CF_OPERATING, cf)),
            MetricSection(key="investing", title="Investing activities", rows=_rows(_CF_INVESTING, cf)),
            MetricSection(key="financing", title="Financing activities", rows=_rows(_CF_FINANCING, cf)),
            MetricSection(key="reconciliation", title="Cash reconciliation", rows=_rows(_CF_RECONCILIATION, cf)),
            MetricSection(key="quality", title="Cash-flow quality", rows=_rows(_CF_QUALITY, cf)),
        ]

    # ------------------------------------------------------------ diagnostics
    def balance_warnings(self) -> list[str]:
        return [
            f"Balance sheet does not tie in FY{b.fiscal_year} (out by {b.balance_check:,.2f} ₹ cr)"
            for b in self.balance_sheets()
            if not b.balances
        ]

    def revenue_cagr(self, periods: int | None = None) -> float | None:
        rev = [s.total_revenue for s in self.income_statements()]
        if periods is not None and len(rev) > periods:
            rev = rev[-(periods + 1):]
        if len(rev) < 2:
            return None
        return cagr(rev[0], rev[-1], len(rev) - 1)
