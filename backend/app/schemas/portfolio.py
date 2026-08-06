"""Typed contracts for the portfolio API.

Every response is a declared model. Two conventions hold throughout and are
stated once here rather than repeated on every field:

* **Money is in rupees**, unrounded in the payload; formatting is the client's
  business.
* **Weights, returns and rates are fractions**, not percentages. 12.5% is
  0.125. Module 6's percentage bug came from two layers disagreeing about this,
  so the API declares one convention and never varies it.
"""
from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from app.domain.portfolio.types import (
    AlertCategory, AlertSeverity, AlertStatus, AllocationDimension,
    CostBasisMethod, TransactionType,
)


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Portfolios
# ---------------------------------------------------------------------------
class PortfolioCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: str | None = None
    benchmark: str = "NIFTY 50"
    cost_basis: CostBasisMethod = CostBasisMethod.FIFO
    max_position_size: float = Field(default=0.10, gt=0, le=1)
    max_sector_weight: float = Field(default=0.35, gt=0, le=1)
    margin_of_safety: float = Field(default=0.20, ge=0, lt=1)
    risk_free_rate: float = Field(default=0.07, ge=0, le=0.5)
    target_positions: int = Field(default=15, ge=1, le=200)
    inception_date: date | None = None


class PortfolioUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = None
    benchmark: str | None = None
    max_position_size: float | None = Field(default=None, gt=0, le=1)
    max_sector_weight: float | None = Field(default=None, gt=0, le=1)
    margin_of_safety: float | None = Field(default=None, ge=0, lt=1)
    risk_free_rate: float | None = Field(default=None, ge=0, le=0.5)
    target_positions: int | None = Field(default=None, ge=1, le=200)
    is_active: bool | None = None


class PortfolioOut(ORMModel):
    id: int
    owner_id: str
    name: str
    description: str | None = None
    base_currency: str
    cost_basis: str
    benchmark: str
    max_position_size: float
    max_sector_weight: float
    margin_of_safety: float
    risk_free_rate: float
    target_positions: int
    inception_date: date | None = None
    is_active: bool
    created_at: datetime | None = None


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------
class TransactionCreate(BaseModel):
    ticker: str = Field(default="", max_length=32)
    txn_type: TransactionType
    trade_date: date
    quantity: float = Field(default=0.0, ge=0)
    price: float = Field(default=0.0, ge=0)
    fees: float = Field(default=0.0, ge=0)
    taxes: float = Field(default=0.0, ge=0)
    #: Read as "ratio_to for every ratio_from" — a 1:2 bonus is to=1, from=2.
    ratio_from: float | None = Field(default=None, gt=0)
    ratio_to: float | None = Field(default=None, gt=0)
    notes: str | None = None
    sequence: int | None = None


class TransactionOut(ORMModel):
    id: int
    portfolio_id: int
    company_id: str | None = None
    ticker: str
    txn_type: str
    trade_date: date
    sequence: int
    quantity: float
    price: float
    fees: float
    taxes: float
    ratio_from: float | None = None
    ratio_to: float | None = None
    notes: str | None = None


# ---------------------------------------------------------------------------
# Positions and holdings
# ---------------------------------------------------------------------------
class HoldingOut(BaseModel):
    ticker: str
    company_id: str | None = None
    name: str
    sector: str | None = None
    industry: str | None = None
    quantity: float
    average_cost: float | None = None
    cost: float
    current_price: float | None = None
    price_source: str | None = None
    last_updated: str | None = None
    market_status: str | None = None
    market_value: float | None = None
    unrealised_pnl: float | None = None
    unrealised_return: float | None = None
    realised_pnl: float = 0.0
    dividends: float = 0.0
    total_pnl: float | None = None
    weight: float
    target_weight: float | None = None
    drift: float | None = None
    max_position_size: float
    is_oversized: bool
    score: float | None = None
    rating: str | None = None
    risk_score: float | None = None
    intrinsic_value: float | None = None
    target_price: float | None = None
    upside: float | None = None
    expected_cagr: float | None = None
    liquidity_days: float | None = None
    holding_days: int | None = None
    first_bought: date | None = None


class RealisedTradeOut(BaseModel):
    ticker: str
    sell_date: date
    buy_date: date
    quantity: float
    cost_per_unit: float
    sale_per_unit: float
    cost: float
    proceeds: float
    pnl: float
    return_pct: float | None = None
    holding_days: int
    is_long_term: bool


class CashOut(BaseModel):
    balance: float
    deposits: float
    withdrawals: float
    buys: float
    sells: float
    dividends: float
    fees: float
    taxes: float
    interest: float
    net_invested: float


# ---------------------------------------------------------------------------
# Allocation
# ---------------------------------------------------------------------------
class AllocationSliceOut(BaseModel):
    key: str
    label: str
    market_value: float
    weight: float
    position_count: int
    target_weight: float | None = None
    drift: float | None = None
    unrealised_pnl: float | None = None


class AllocationOut(BaseModel):
    dimension: str
    slices: list[AllocationSliceOut] = Field(default_factory=list)
    unclassified_value: float = 0.0
    herfindahl: float
    effective_count: float


# ---------------------------------------------------------------------------
# Risk and performance
# ---------------------------------------------------------------------------
class RiskOut(BaseModel):
    observations: int
    annualised_return: float | None = None
    annualised_volatility: float | None = None
    sharpe: float | None = None
    sortino: float | None = None
    max_drawdown: float | None = None
    drawdown_recovered: bool | None = None
    var_95: float | None = None
    cvar_95: float | None = None
    var_99: float | None = None
    beta: float | None = None
    alpha: float | None = None
    tracking_error: float | None = None
    information_ratio: float | None = None
    up_capture: float | None = None
    down_capture: float | None = None
    herfindahl: float | None = None
    effective_positions: float | None = None
    top_5_concentration: float | None = None
    diversification_score: float | None = None
    largest_position_weight: float | None = None
    illiquid_positions: int = 0
    #: Statistics that could not be computed, each with the reason. A blank
    #: cell the user cannot account for is worse than no cell.
    unavailable: list[str] = Field(default_factory=list)


class SeriesPoint(BaseModel):
    as_of: date
    value: float
    net_flow: float = 0.0


class PerformanceOut(BaseModel):
    twr: float | None = None
    twr_annualised: float | None = None
    mwr: float | None = None
    benchmark_return: float | None = None
    active_return: float | None = None
    series: list[SeriesPoint] = Field(default_factory=list)
    rolling: list[dict] = Field(default_factory=list)
    underwater: list[dict] = Field(default_factory=list)
    contributions: list[dict] = Field(default_factory=list)


class AttributionRowOut(BaseModel):
    key: str
    label: str
    portfolio_weight: float
    benchmark_weight: float
    active_weight: float
    portfolio_return: float
    benchmark_return: float
    allocation: float
    selection: float
    interaction: float
    total: float


class AttributionOut(BaseModel):
    rows: list[AttributionRowOut] = Field(default_factory=list)
    portfolio_return: float
    benchmark_return: float
    active_return: float
    total_allocation: float
    total_selection: float
    total_interaction: float
    #: Active return the decomposition did not explain. Should be ~0.
    residual: float


class RebalanceTradeOut(BaseModel):
    ticker: str
    name: str
    action: str
    current_weight: float
    target_weight: float
    drift: float
    value_delta: float
    shares: float | None = None
    reason: str


# ---------------------------------------------------------------------------
# The composite view
# ---------------------------------------------------------------------------
class PortfolioSummaryOut(BaseModel):
    portfolio_id: int
    name: str
    benchmark: str
    as_of: date
    market_value: float
    cost_basis: float
    cash: float
    total_value: float
    unrealised_pnl: float
    realised_pnl: float
    dividends: float
    total_pnl: float
    total_return: float | None = None
    position_count: int
    cash_weight: float | None = None
    #: Holdings with no current price. Their value is excluded from the totals
    #: above, so the portfolio is larger than it appears.
    unpriced: list[str] = Field(default_factory=list)
    #: Per-ticker analytics failures, so a missing score is attributable.
    analytics_errors: dict[str, str] = Field(default_factory=dict)


class PortfolioViewOut(BaseModel):
    summary: PortfolioSummaryOut
    holdings: list[HoldingOut] = Field(default_factory=list)
    cash: CashOut
    allocations: dict[str, AllocationOut] = Field(default_factory=dict)
    risk: RiskOut
    performance: PerformanceOut
    rebalance: list[RebalanceTradeOut] = Field(default_factory=list)
    realised: list[RealisedTradeOut] = Field(default_factory=list)
    metrics: dict[str, float | None] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------
class AlertRuleOut(BaseModel):
    key: str
    label: str
    condition: str
    metric: str
    comparator: str
    threshold: float | str | None = None
    severity: str
    category: str
    action: str
    scope: str
    enabled: bool


class AlertEvaluationOut(BaseModel):
    key: str
    label: str
    category: str
    severity: str
    status: str
    condition: str
    action: str
    observed: float | str | None = None
    threshold: float | str | None = None
    ticker: str | None = None
    company_id: str | None = None
    detail: str = ""


class AlertEventOut(ORMModel):
    id: int
    portfolio_id: int | None = None
    rule_key: str
    ticker: str | None = None
    company_id: str | None = None
    label: str
    category: str
    severity: str
    status: str
    condition: str | None = None
    action: str | None = None
    observed: str | None = None
    threshold: str | None = None
    detail: str | None = None
    occurrences: int
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    acknowledged_at: datetime | None = None


class AlertSummaryOut(BaseModel):
    counts: dict[str, int]
    evaluations: list[AlertEvaluationOut] = Field(default_factory=list)


class AlertOverrideIn(BaseModel):
    rule_key: str
    enabled: bool | None = None
    threshold: float | None = None
    severity: AlertSeverity | None = None
    #: Supplied only for user-defined rules.
    label: str | None = None
    metric: str | None = None
    comparator: str | None = None
    is_custom: bool = False


# ---------------------------------------------------------------------------
# Watchlists
# ---------------------------------------------------------------------------
class WatchlistCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: str | None = None


class WatchlistUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = None


class WatchlistOut(ORMModel):
    id: int
    owner_id: str
    name: str
    description: str | None = None
    created_at: datetime | None = None


class WatchlistEntryCreate(BaseModel):
    ticker: str = Field(min_length=1, max_length=32)
    buy_below: float | None = Field(default=None, gt=0)
    target_price: float | None = Field(default=None, gt=0)
    conviction: str | None = None
    note: str | None = None


class WatchlistRowOut(BaseModel):
    id: int
    ticker: str
    company_id: str | None = None
    name: str
    sector: str | None = None
    price: float | None = None
    price_source: str | None = None
    last_updated: str | None = None
    market_status: str | None = None
    buy_below: float | None = None
    target_price: float | None = None
    upside: float | None = None
    score: float | None = None
    rating: str | None = None
    status: str
    note: str | None = None
    conviction: str | None = None
    added_on: date | None = None


# ---------------------------------------------------------------------------
# Snapshots and targets
# ---------------------------------------------------------------------------
class SnapshotOut(ORMModel):
    id: int
    portfolio_id: int
    as_of: date
    market_value: float
    cost_basis: float
    cash: float
    net_flow: float
    position_count: int
    benchmark_level: float | None = None


class TargetIn(BaseModel):
    dimension: AllocationDimension
    bucket_key: str = Field(min_length=1, max_length=120)
    target_weight: float = Field(ge=0, le=1)


class TargetOut(ORMModel):
    id: int
    dimension: str
    bucket_key: str
    target_weight: float


# ---------------------------------------------------------------------------
# AI commentary
# ---------------------------------------------------------------------------
class CommentaryCitationOut(BaseModel):
    key: str
    label: str
    kind: str
    value: float | str | None = None
    unit: str = ""
    source: str = ""


class CommentarySectionOut(BaseModel):
    key: str
    title: str
    body: str


class CommentaryOut(BaseModel):
    portfolio_id: int
    provider: str
    sections: list[CommentarySectionOut] = Field(default_factory=list)
    citations: list[CommentaryCitationOut] = Field(default_factory=list)
    disclosure: str


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------
class CapabilitiesOut(BaseModel):
    transaction_types: list[str]
    allocation_dimensions: list[str]
    alert_categories: list[str]
    alert_severities: list[str]
    cost_basis_methods: list[str]
    rules: list[AlertRuleOut]
    rating_position_limits: dict[str, float]
    cache: dict[str, float]
