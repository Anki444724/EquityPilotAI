"""Extraction field registry — the 73 fields of `AI-2 Extracted Store`.

GENERATED from Institutional_Equity_Research_Platform_v7.xlsx via
docs/module7_spec.json. Do not hand-edit; regenerate from the workbook.

The workbook defines what an institutional extraction *is*: 73 named fields
across 16 categories, each with a unit and a target sheet. Module 7 treats
that list as the contract. Coverage is measured against it, so "we extracted
a lot" is replaced by "we extracted 41 of 73, and here are the 32 we did not".
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from app.domain.documents.types import Unit


class FieldCategory(StrEnum):
    """The 16 extraction categories of the workbook store."""

    FINANCIAL = "FINANCIAL"
    GUIDANCE = "GUIDANCE"
    CAPEX = "CAPEX"
    DEBT = "DEBT"
    ORDER_BOOK = "ORDER BOOK"
    CAPACITY = "CAPACITY"
    CUSTOMERS = "CUSTOMERS"
    SUBSIDIARIES = "SUBSIDIARIES"
    BUSINESS = "BUSINESS"
    MDA = "MD&A"
    RISKS = "RISKS"
    OPPORTUNITIES = "OPPORTUNITIES"
    ESG = "ESG"
    GOVERNANCE = "GOVERNANCE"
    MOAT = "MOAT"
    METRICS = "METRICS"


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """One extractable field: what it is, its unit, and where it lands."""

    key: str
    label: str
    category: FieldCategory
    unit: Unit
    target: str | None = None

    @property
    def is_numeric(self) -> bool:
        return self.unit is not Unit.TEXT


FIELD_SPECS: Final[tuple[FieldSpec, ...]] = (
    FieldSpec("revenue", "Revenue", FieldCategory.FINANCIAL, Unit.INR_CRORE, "06 Historical IS"),
    FieldSpec("ebitda", "EBITDA", FieldCategory.FINANCIAL, Unit.INR_CRORE, "06 Historical IS"),
    FieldSpec("pat", "PAT", FieldCategory.FINANCIAL, Unit.INR_CRORE, "06 Historical IS"),
    FieldSpec("eps", "EPS", FieldCategory.FINANCIAL, Unit.INR, "06 Historical IS"),
    FieldSpec("gross_debt", "Gross debt", FieldCategory.FINANCIAL, Unit.INR_CRORE, "07 Historical BS"),
    FieldSpec("cash_and_equivalents", "Cash & equivalents", FieldCategory.FINANCIAL, Unit.INR_CRORE, "07 Historical BS"),
    FieldSpec("net_worth", "Net worth", FieldCategory.FINANCIAL, Unit.INR_CRORE, "07 Historical BS"),
    FieldSpec("operating_cash_flow", "Operating cash flow", FieldCategory.FINANCIAL, Unit.INR_CRORE, "08 Historical CF"),
    FieldSpec("capex", "Capex", FieldCategory.FINANCIAL, Unit.INR_CRORE, "08 Historical CF"),
    FieldSpec("free_cash_flow", "Free cash flow", FieldCategory.FINANCIAL, Unit.INR_CRORE, "08 Historical CF"),
    FieldSpec("revenue_growth_guidance", "Revenue growth guidance", FieldCategory.GUIDANCE, Unit.PERCENT, "16 Revenue Forecast"),
    FieldSpec("ebitda_margin_guidance", "EBITDA margin guidance", FieldCategory.GUIDANCE, Unit.PERCENT, "17 EBITDA Forecast"),
    FieldSpec("capex_guidance", "Capex guidance", FieldCategory.GUIDANCE, Unit.INR_CRORE, "12 Capex Analysis"),
    FieldSpec("management_outlook_commentary", "Management outlook commentary", FieldCategory.GUIDANCE, Unit.TEXT, "31 IC Report"),
    FieldSpec("announced_capex_programme", "Announced capex programme", FieldCategory.CAPEX, Unit.INR_CRORE, "12 Capex Analysis"),
    FieldSpec("capex_spent_to_date", "Capex spent to date", FieldCategory.CAPEX, Unit.INR_CRORE, "12 Capex Analysis"),
    FieldSpec("commissioning_timeline", "Commissioning timeline", FieldCategory.CAPEX, Unit.TEXT, "12 Capex Analysis"),
    FieldSpec("expected_asset_turn", "Expected asset turn", FieldCategory.CAPEX, Unit.TIMES, "12 Capex Analysis"),
    FieldSpec("total_borrowings", "Total borrowings", FieldCategory.DEBT, Unit.INR_CRORE, "13 Debt Analysis"),
    FieldSpec("blended_cost_of_debt", "Blended cost of debt", FieldCategory.DEBT, Unit.PERCENT, "13 Debt Analysis"),
    FieldSpec("average_maturity", "Average maturity", FieldCategory.DEBT, Unit.YEARS, "13 Debt Analysis"),
    FieldSpec("credit_rating", "Credit rating", FieldCategory.DEBT, Unit.TEXT, "13 Debt Analysis"),
    FieldSpec("covenant_terms", "Covenant terms", FieldCategory.DEBT, Unit.TEXT, "13 Debt Analysis"),
    FieldSpec("order_book_backlog", "Order book / backlog", FieldCategory.ORDER_BOOK, Unit.INR_CRORE, "03 Company Info"),
    FieldSpec("order_inflow_during_year", "Order inflow during year", FieldCategory.ORDER_BOOK, Unit.INR_CRORE, "03 Company Info"),
    FieldSpec("book_to_bill_ratio", "Book-to-bill ratio", FieldCategory.ORDER_BOOK, Unit.TIMES, "03 Company Info"),
    FieldSpec("execution_period", "Execution period", FieldCategory.ORDER_BOOK, Unit.MONTHS, "03 Company Info"),
    FieldSpec("installed_capacity", "Installed capacity", FieldCategory.CAPACITY, Unit.UNITS, "03 Company Info"),
    FieldSpec("capacity_utilisation", "Capacity utilisation", FieldCategory.CAPACITY, Unit.PERCENT, "03 Company Info"),
    FieldSpec("planned_capacity_addition", "Planned capacity addition", FieldCategory.CAPACITY, Unit.UNITS, "03 Company Info"),
    FieldSpec("number_of_plants", "Number of plants", FieldCategory.CAPACITY, Unit.COUNT, "03 Company Info"),
    FieldSpec("top_5_customer_concentration", "Top-5 customer concentration", FieldCategory.CUSTOMERS, Unit.PERCENT, "03 Company Info"),
    FieldSpec("key_customer_names", "Key customer names", FieldCategory.CUSTOMERS, Unit.TEXT, "03 Company Info"),
    FieldSpec("customer_contract_tenure", "Customer contract tenure", FieldCategory.CUSTOMERS, Unit.TEXT, "03 Company Info"),
    FieldSpec("number_of_subsidiaries", "Number of subsidiaries", FieldCategory.SUBSIDIARIES, Unit.COUNT, "03 Company Info"),
    FieldSpec("loss_making_subsidiaries", "Loss-making subsidiaries", FieldCategory.SUBSIDIARIES, Unit.COUNT, "03 Company Info"),
    FieldSpec("subsidiary_revenue_contribution", "Subsidiary revenue contribution", FieldCategory.SUBSIDIARIES, Unit.PERCENT, "03 Company Info"),
    FieldSpec("business_description", "Business description", FieldCategory.BUSINESS, Unit.TEXT, "03 Company Info"),
    FieldSpec("segment_revenue_split", "Segment revenue split", FieldCategory.BUSINESS, Unit.PERCENT, "03 Company Info"),
    FieldSpec("geographic_revenue_split", "Geographic revenue split", FieldCategory.BUSINESS, Unit.PERCENT, "03 Company Info"),
    FieldSpec("product_brand_portfolio", "Product / brand portfolio", FieldCategory.BUSINESS, Unit.TEXT, "03 Company Info"),
    FieldSpec("market_share", "Market share", FieldCategory.BUSINESS, Unit.PERCENT, "04 Industry Analysis"),
    FieldSpec("management_discussion_summary", "Management discussion summary", FieldCategory.MDA, Unit.TEXT, "31 IC Report"),
    FieldSpec("strategy_priorities", "Strategy priorities", FieldCategory.MDA, Unit.TEXT, "31 IC Report"),
    FieldSpec("operational_highlights", "Operational highlights", FieldCategory.MDA, Unit.TEXT, "31 IC Report"),
    FieldSpec("principal_risk_1", "Principal risk 1", FieldCategory.RISKS, Unit.TEXT, "29 Risk Dashboard"),
    FieldSpec("principal_risk_2", "Principal risk 2", FieldCategory.RISKS, Unit.TEXT, "29 Risk Dashboard"),
    FieldSpec("principal_risk_3", "Principal risk 3", FieldCategory.RISKS, Unit.TEXT, "29 Risk Dashboard"),
    FieldSpec("litigation_contingent_liability", "Litigation / contingent liability", FieldCategory.RISKS, Unit.INR_CRORE, "29 Risk Dashboard"),
    FieldSpec("auditor_qualification", "Auditor qualification", FieldCategory.RISKS, Unit.BOOLEAN, "29 Risk Dashboard"),
    FieldSpec("related_party_transactions", "Related-party transactions", FieldCategory.RISKS, Unit.PERCENT_OF_REVENUE, "29 Risk Dashboard"),
    FieldSpec("growth_opportunity_1", "Growth opportunity 1", FieldCategory.OPPORTUNITIES, Unit.TEXT, "04 Industry Analysis"),
    FieldSpec("growth_opportunity_2", "Growth opportunity 2", FieldCategory.OPPORTUNITIES, Unit.TEXT, "04 Industry Analysis"),
    FieldSpec("new_product_market_entry", "New product / market entry", FieldCategory.OPPORTUNITIES, Unit.TEXT, "04 Industry Analysis"),
    FieldSpec("industry_tailwind", "Industry tailwind", FieldCategory.OPPORTUNITIES, Unit.TEXT, "04 Industry Analysis"),
    FieldSpec("esg_brsr_rating", "ESG / BRSR rating", FieldCategory.ESG, Unit.SCORE, "05 Management Analysis"),
    FieldSpec("scope_1_plus_2_emissions", "Scope 1+2 emissions", FieldCategory.ESG, Unit.TONNES_CO2, "05 Management Analysis"),
    FieldSpec("renewable_energy_share", "Renewable energy share", FieldCategory.ESG, Unit.PERCENT, "05 Management Analysis"),
    FieldSpec("water_waste_intensity", "Water / waste intensity", FieldCategory.ESG, Unit.INDEX, "05 Management Analysis"),
    FieldSpec("csr_spend", "CSR spend", FieldCategory.ESG, Unit.INR_CRORE, "05 Management Analysis"),
    FieldSpec("board_size", "Board size", FieldCategory.GOVERNANCE, Unit.COUNT, "05 Management Analysis"),
    FieldSpec("independent_director_share", "Independent director share", FieldCategory.GOVERNANCE, Unit.PERCENT, "05 Management Analysis"),
    FieldSpec("promoter_pledge", "Promoter pledge", FieldCategory.GOVERNANCE, Unit.PERCENT, "14 Shareholding"),
    FieldSpec("auditor_name_and_tenure", "Auditor name & tenure", FieldCategory.GOVERNANCE, Unit.TEXT, "05 Management Analysis"),
    FieldSpec("kmp_remuneration", "KMP remuneration", FieldCategory.GOVERNANCE, Unit.INR_CRORE, "05 Management Analysis"),
    FieldSpec("chairman_ceo_separation", "Chairman-CEO separation", FieldCategory.GOVERNANCE, Unit.BOOLEAN, "05 Management Analysis"),
    FieldSpec("stated_competitive_advantage", "Stated competitive advantage", FieldCategory.MOAT, Unit.TEXT, "28 Economic Moat"),
    FieldSpec("brand_ip_assets", "Brand / IP assets", FieldCategory.MOAT, Unit.TEXT, "28 Economic Moat"),
    FieldSpec("switching_cost_evidence", "Switching cost evidence", FieldCategory.MOAT, Unit.TEXT, "28 Economic Moat"),
    FieldSpec("employee_headcount", "Employee headcount", FieldCategory.METRICS, Unit.COUNT, "03 Company Info"),
    FieldSpec("revenue_per_employee", "Revenue per employee", FieldCategory.METRICS, Unit.INR_LAKH, "03 Company Info"),
    FieldSpec("randd_spend", "R&D spend", FieldCategory.METRICS, Unit.INR_CRORE, "03 Company Info"),
    FieldSpec("advertising_brand_spend", "Advertising / brand spend", FieldCategory.METRICS, Unit.INR_CRORE, "03 Company Info"),
)

FIELDS_BY_KEY: Final[dict[str, FieldSpec]] = {f.key: f for f in FIELD_SPECS}

FIELDS_BY_CATEGORY: Final[dict[FieldCategory, tuple[FieldSpec, ...]]] = {
    category: tuple(f for f in FIELD_SPECS if f.category is category)
    for category in FieldCategory
}

#: Total fields the platform undertakes to look for. Coverage denominator.
FIELD_COUNT: Final[int] = len(FIELD_SPECS)
