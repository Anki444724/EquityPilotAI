"""Scoring inputs.

A single immutable bundle assembled once per request and passed to every
category scorer. This is the same single-resolution discipline used since
Module 1: the statements, ratios, forecast and valuation are each computed
once, and thirteen scorers read from the one object.

Qualitative inputs (board independence, moat sources, ESG) have no source in
the financial statements. They are optional fields here; when absent the
relevant metric is marked ``MISSING`` and the confidence engine reports the gap
rather than the scorer inventing a value.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.domain.financials.statements import (
    BalanceSheet, CashFlowStatement, IncomeStatement,
)
from app.domain.forecast.engine import ForecastResult
from app.domain.valuation.data_quality import DataQualityReport


@dataclass(frozen=True, slots=True)
class QualitativeInputs:
    """Analyst judgements, all optional and all 0–10 unless noted."""

    # governance
    board_independence: float | None = None      # fraction of independent directors
    audit_quality: float | None = None
    related_party_intensity: float | None = None  # related-party txns / revenue
    disclosure_quality: float | None = None
    auditor_is_big_four: bool | None = None
    audit_qualifications: int | None = None

    # moat
    brand_strength: float | None = None
    switching_costs: float | None = None
    network_effects: float | None = None
    cost_advantage: float | None = None
    intangible_assets: float | None = None
    efficient_scale: float | None = None

    # management
    capital_allocation_record: float | None = None
    guidance_credibility: float | None = None
    tenure_years: float | None = None
    promoter_pledge: float | None = None          # share of promoter holding

    # business
    customer_concentration: float | None = None   # top-5 share of revenue
    revenue_visibility: float | None = None
    industry_growth: float | None = None
    porters_five_forces: float | None = None      # 1 (attractive) – 5 (hostile)

    # ESG
    environmental_score: float | None = None
    social_score: float | None = None
    esg_disclosure: float | None = None

    # momentum
    price_return_12m: float | None = None
    price_return_3m: float | None = None
    earnings_revision: float | None = None
    relative_strength: float | None = None


@dataclass(frozen=True, slots=True)
class ScoringInputs:
    """Everything the thirteen scorers need, resolved once."""

    company_id: str
    ticker: str
    name: str

    incomes: list[IncomeStatement]
    balances: list[BalanceSheet]
    cash_flows: list[CashFlowStatement]

    forecast: ForecastResult | None = None

    # valuation outputs
    wacc: float | None = None
    cost_of_equity: float | None = None
    intrinsic_value: float | None = None
    current_price: float | None = None
    upside: float | None = None
    ev_ebitda: float | None = None
    pe_ratio: float | None = None
    pb_ratio: float | None = None
    justified_premium: float | None = None    # actual/justified − 1
    margin_of_safety: float | None = None

    quality_report: DataQualityReport | None = None
    qualitative: QualitativeInputs = field(default_factory=QualitativeInputs)

    # ------------------------------------------------------------- helpers
    @property
    def latest_income(self) -> IncomeStatement | None:
        return self.incomes[-1] if self.incomes else None

    @property
    def latest_balance(self) -> BalanceSheet | None:
        return self.balances[-1] if self.balances else None

    @property
    def latest_cash_flow(self) -> CashFlowStatement | None:
        return self.cash_flows[-1] if self.cash_flows else None

    @property
    def years(self) -> int:
        return len(self.incomes)

    def prior_balance(self, offset: int = 1) -> BalanceSheet | None:
        idx = len(self.balances) - 1 - offset
        return self.balances[idx] if idx >= 0 else None

    def avg_balance(self, attr: str, offset: int = 0) -> float | None:
        """Average opening/closing balance, the convention used throughout."""
        idx = len(self.balances) - 1 - offset
        if idx < 0:
            return None
        closing = getattr(self.balances[idx], attr, None)
        opening = getattr(self.balances[idx - 1], attr, None) if idx > 0 else None
        if closing is None:
            return opening
        return closing if opening is None else (closing + opening) / 2

    def series(self, statement: str, attr: str, periods: int = 5) -> list[float | None]:
        """Trailing series of a statement attribute, oldest first."""
        source = {
            "income": self.incomes,
            "balance": self.balances,
            "cash_flow": self.cash_flows,
        }[statement]
        return [getattr(row, attr, None) for row in source[-periods:]]
