"""Presentation metadata for assumption drivers.

Labels, units and grouping live here so the UI can render an assumption editor
generically. Adding a driver means adding one entry — no frontend change.
"""
from __future__ import annotations

from app.schemas.common import Unit

# name -> (label, unit, group)
DRIVER_META: dict[str, tuple[str, str, str]] = {
    # revenue
    "revenue_growth": ("Revenue growth", Unit.PERCENT, "Revenue"),
    "terminal_revenue_growth": ("Long-run growth", Unit.PERCENT, "Revenue"),
    "growth_fade": ("Growth fade (0–1)", Unit.RATIO, "Revenue"),
    "volume_growth": ("Volume growth", Unit.PERCENT, "Revenue"),
    "price_growth": ("Price / realisation growth", Unit.PERCENT, "Revenue"),
    "organic_growth": ("Organic growth", Unit.PERCENT, "Revenue"),
    "acquisition_growth": ("Acquisition growth", Unit.PERCENT, "Revenue"),
    # margins
    "gross_margin": ("Gross margin", Unit.PERCENT, "Margins"),
    "ebitda_margin": ("EBITDA margin", Unit.PERCENT, "Margins"),
    "margin_expansion": ("Annual margin expansion", Unit.PERCENT, "Margins"),
    "other_income_pct_revenue": ("Other income (% revenue)", Unit.PERCENT, "Margins"),
    # capex and depreciation
    "capex_pct_revenue": ("Capex (% revenue)", Unit.PERCENT, "Capex"),
    "maintenance_capex_pct": ("Maintenance share of capex", Unit.PERCENT, "Capex"),
    "depreciation_rate": ("Depreciation rate (on net block)", Unit.PERCENT, "Capex"),
    # working capital
    "inventory_days": ("Inventory days", Unit.DAYS, "Working capital"),
    "receivable_days": ("Receivable days", Unit.DAYS, "Working capital"),
    "payable_days": ("Payable days", Unit.DAYS, "Working capital"),
    "other_ca_pct_revenue": ("Other current assets (% revenue)", Unit.PERCENT, "Working capital"),
    "other_cl_pct_revenue": ("Other current liabilities (% revenue)", Unit.PERCENT, "Working capital"),
    # debt
    "interest_rate": ("Cost of debt", Unit.PERCENT, "Debt"),
    "debt_repayment_pct": ("Scheduled repayment (% opening debt)", Unit.PERCENT, "Debt"),
    "new_debt": ("New borrowing", Unit.CRORE, "Debt"),
    "cash_yield": ("Yield on surplus cash", Unit.PERCENT, "Debt"),
    "min_cash_pct_revenue": ("Minimum cash (% revenue)", Unit.PERCENT, "Debt"),
    # taxes and distribution
    "effective_tax_rate": ("Effective tax rate", Unit.PERCENT, "Tax & returns"),
    "dividend_payout": ("Dividend payout ratio", Unit.PERCENT, "Tax & returns"),
    # valuation
    "wacc": ("WACC", Unit.PERCENT, "Valuation"),
    "terminal_growth": ("Terminal growth", Unit.PERCENT, "Valuation"),
    "exit_ev_ebitda": ("Exit EV/EBITDA", Unit.MULTIPLE, "Valuation"),
    "target_pe": ("Target P/E", Unit.MULTIPLE, "Valuation"),
    "probability": ("Scenario probability", Unit.PERCENT, "Valuation"),
}

#: Order groups are presented in.
GROUP_ORDER = (
    "Revenue", "Margins", "Capex", "Working capital",
    "Debt", "Tax & returns", "Valuation",
)


def meta_for(name: str) -> tuple[str, str, str]:
    return DRIVER_META.get(name, (name.replace("_", " ").capitalize(), Unit.RATIO, "Other"))
