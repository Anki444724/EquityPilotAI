"""Core portfolio types.

The load-bearing decision in this module is that a **position is derived, never
stored**. Quantity, average cost and realised P&L are all computed by replaying
the transaction ledger. Storing a position alongside its transactions creates
two sources of truth that drift the first time a corrective entry is posted,
and reconciling them afterwards is archaeology.

The second decision is that **money is never a bare float in a signature**.
Every monetary quantity here is in rupees, every weight is a fraction in [0,1],
and every rate is a fraction rather than a percentage. Module 6 taught that
lesson expensively: a WACC of 15.17% rendered as "0.15 %" because one layer
believed a fraction and another believed a percentage.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------
class TransactionType(StrEnum):
    """Every event that can change a position or the cash balance.

    Corporate actions are transactions rather than a separate concept, because
    a bonus issue changes quantity exactly as a buy does and must replay in the
    same chronological order.
    """

    BUY = "buy"
    SELL = "sell"
    DIVIDEND = "dividend"
    BONUS = "bonus"                # free shares, ratio-driven
    SPLIT = "split"                # face-value split, ratio-driven
    RIGHTS = "rights"              # subscribed at a stated price
    DEPOSIT = "deposit"            # cash in
    WITHDRAWAL = "withdrawal"      # cash out
    FEE = "fee"
    TAX = "tax"
    INTEREST = "interest"          # interest on idle cash

    @property
    def affects_quantity(self) -> bool:
        return self in _QUANTITY_EVENTS

    @property
    def is_cash_only(self) -> bool:
        return self in _CASH_ONLY_EVENTS

    @property
    def is_corporate_action(self) -> bool:
        return self in _CORPORATE_ACTIONS


_QUANTITY_EVENTS = frozenset({
    TransactionType.BUY, TransactionType.SELL, TransactionType.BONUS,
    TransactionType.SPLIT, TransactionType.RIGHTS,
})
_CASH_ONLY_EVENTS = frozenset({
    TransactionType.DEPOSIT, TransactionType.WITHDRAWAL, TransactionType.FEE,
    TransactionType.TAX, TransactionType.INTEREST,
})
_CORPORATE_ACTIONS = frozenset({
    TransactionType.BONUS, TransactionType.SPLIT, TransactionType.RIGHTS,
    TransactionType.DIVIDEND,
})


class CostBasisMethod(StrEnum):
    """How cost is relieved when a holding is sold.

    Indian tax practice is FIFO for listed equity, so that is the default.
    Weighted average is offered because most retail statements quote it and a
    user comparing against a broker statement needs the same convention.
    """

    FIFO = "fifo"
    WEIGHTED_AVERAGE = "weighted_average"


class AlertSeverity(StrEnum):
    """Workbook priorities, ordered."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"

    @property
    def rank(self) -> int:
        return _SEVERITY_RANK[self]


_SEVERITY_RANK = {
    AlertSeverity.CRITICAL: 0,
    AlertSeverity.HIGH: 1,
    AlertSeverity.MEDIUM: 2,
    AlertSeverity.LOW: 3,
}


class AlertCategory(StrEnum):
    """The alert families the brief enumerates."""

    PRICE = "price"
    VALUATION = "valuation"
    DCF_CHANGE = "dcf_change"
    SCORE_CHANGE = "score_change"
    RISK = "risk"
    MANAGEMENT = "management"
    DOCUMENT = "document"
    QUARTERLY_RESULT = "quarterly_result"
    CORPORATE_ACTION = "corporate_action"
    PORTFOLIO = "portfolio"


class AlertStatus(StrEnum):
    CLEAR = "clear"
    TRIGGERED = "triggered"
    #: The rule could not be evaluated — an input was missing. Distinct from
    #: `clear`, because "no evidence of a problem" and "we could not look" are
    #: very different statements and the workbook conflates them.
    UNAVAILABLE = "unavailable"
    ACKNOWLEDGED = "acknowledged"


class Comparator(StrEnum):
    """Threshold comparison direction, from the workbook's IF() formulas."""

    LT = "lt"
    LTE = "lte"
    GT = "gt"
    GTE = "gte"
    EQ = "eq"
    IN_SET = "in_set"

    def evaluate(self, value: float | str, threshold: float | str | set) -> bool:
        if self is Comparator.IN_SET:
            return value in (threshold if isinstance(threshold, (set, frozenset)) else {threshold})
        try:
            left, right = float(value), float(threshold)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return False
        return {
            Comparator.LT: left < right,
            Comparator.LTE: left <= right,
            Comparator.GT: left > right,
            Comparator.GTE: left >= right,
            Comparator.EQ: math.isclose(left, right, rel_tol=1e-9, abs_tol=1e-12),
        }[self]


class AllocationDimension(StrEnum):
    """The five allocation cuts the brief requires."""

    SECTOR = "sector"
    INDUSTRY = "industry"
    MARKET_CAP = "market_cap"
    COUNTRY = "country"
    STYLE = "style"


class MarketCapBand(StrEnum):
    """SEBI-aligned bands. Thresholds in ₹ crore, declared in `bands.py`."""

    LARGE = "large_cap"
    MID = "mid_cap"
    SMALL = "small_cap"
    MICRO = "micro_cap"
    UNKNOWN = "unknown"


class StyleBucket(StrEnum):
    """Style classification, derived from platform scores — never hand-tagged."""

    QUALITY = "quality"
    GROWTH = "growth"
    VALUE = "value"
    BLEND = "blend"
    UNKNOWN = "unknown"


class RebalanceAction(StrEnum):
    INCREASE = "increase"
    REDUCE = "reduce"
    HOLD = "hold"
    EXIT = "exit"


class WatchStatus(StrEnum):
    """Workbook watchlist status column."""

    TRIGGERED = "triggered"        # at or below buy price
    APPROACHING = "approaching"
    WATCHING = "watching"
    EXPENSIVE = "expensive"


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Lot:
    """One purchase lot, for FIFO cost relief."""

    trade_date: date
    quantity: float
    cost_per_unit: float

    @property
    def cost(self) -> float:
        return self.quantity * self.cost_per_unit


@dataclass(frozen=True, slots=True)
class RealisedTrade:
    """A closed round-trip, with the holding period tax treatment depends on."""

    ticker: str
    sell_date: date
    buy_date: date
    quantity: float
    cost_per_unit: float
    sale_per_unit: float

    @property
    def cost(self) -> float:
        return self.quantity * self.cost_per_unit

    @property
    def proceeds(self) -> float:
        return self.quantity * self.sale_per_unit

    @property
    def pnl(self) -> float:
        return self.proceeds - self.cost

    @property
    def holding_days(self) -> int:
        return (self.sell_date - self.buy_date).days

    @property
    def is_long_term(self) -> bool:
        """Indian listed equity: long term beyond 12 months."""
        return self.holding_days > 365

    @property
    def return_pct(self) -> float | None:
        return (self.pnl / self.cost) if self.cost else None


@dataclass(slots=True)
class Position:
    """A derived holding. Never persisted — always replayed from the ledger."""

    ticker: str
    company_id: str | None = None
    name: str = ""
    sector: str | None = None
    industry: str | None = None
    country: str = "India"

    quantity: float = 0.0
    #: Cost of the shares still held, after FIFO relief.
    cost: float = 0.0
    #: Dividends received on this holding, which reduce effective cost.
    dividends: float = 0.0
    realised_pnl: float = 0.0
    fees: float = 0.0

    current_price: float | None = None
    market_cap: float | None = None
    first_bought: date | None = None
    last_traded: date | None = None
    lots: list[Lot] = field(default_factory=list)

    # -- cost -----------------------------------------------------------
    @property
    def is_open(self) -> bool:
        # Float residue from a split ratio can leave 1e-13 shares behind.
        return self.quantity > 1e-9

    @property
    def average_cost(self) -> float | None:
        return (self.cost / self.quantity) if self.is_open else None

    @property
    def market_value(self) -> float | None:
        if not self.is_open or self.current_price is None:
            return None
        return self.quantity * self.current_price

    @property
    def unrealised_pnl(self) -> float | None:
        value = self.market_value
        return None if value is None else value - self.cost

    @property
    def unrealised_return(self) -> float | None:
        pnl = self.unrealised_pnl
        return (pnl / self.cost) if pnl is not None and self.cost else None

    @property
    def total_pnl(self) -> float | None:
        """Realised plus unrealised plus dividends — the only number that matters.

        An open position judged on unrealised P&L alone flatters a holding that
        has already returned much of its cost as dividends, and penalises one
        that has been partly trimmed at a profit.
        """
        unrealised = self.unrealised_pnl
        if unrealised is None:
            return self.realised_pnl + self.dividends if not self.is_open else None
        return unrealised + self.realised_pnl + self.dividends

    @property
    def total_return(self) -> float | None:
        total = self.total_pnl
        invested = self.cost if self.is_open else abs(self.realised_pnl) or None
        if total is None or not invested:
            return None
        return total / invested

    @property
    def holding_days(self) -> int | None:
        if self.first_bought is None:
            return None
        end = self.last_traded or date.today()
        return max(0, (end - self.first_bought).days)


@dataclass(slots=True)
class CashLedger:
    """Cash movements, tracked separately so portfolio value is never a plug."""

    deposits: float = 0.0
    withdrawals: float = 0.0
    buys: float = 0.0
    sells: float = 0.0
    dividends: float = 0.0
    fees: float = 0.0
    taxes: float = 0.0
    interest: float = 0.0

    @property
    def balance(self) -> float:
        return (
            self.deposits - self.withdrawals
            - self.buys + self.sells
            + self.dividends + self.interest
            - self.fees - self.taxes
        )

    @property
    def net_invested(self) -> float:
        """Capital the investor actually committed — the return denominator."""
        return self.deposits - self.withdrawals


@dataclass(slots=True)
class AllocationSlice:
    """One bucket of an allocation cut."""

    key: str
    label: str
    market_value: float
    weight: float
    position_count: int = 0
    target_weight: float | None = None
    unrealised_pnl: float | None = None

    @property
    def drift(self) -> float | None:
        """Actual minus target. Positive means overweight."""
        return None if self.target_weight is None else self.weight - self.target_weight


@dataclass(slots=True)
class Allocation:
    """A complete allocation cut across one dimension."""

    dimension: AllocationDimension
    slices: list[AllocationSlice] = field(default_factory=list)
    #: Value that could not be classified — reported, never silently dropped.
    unclassified_value: float = 0.0

    @property
    def largest(self) -> AllocationSlice | None:
        return max(self.slices, key=lambda s: s.weight, default=None)

    @property
    def herfindahl(self) -> float:
        """HHI on weights. 1.0 is a single holding; 1/n is perfectly equal."""
        return sum(s.weight ** 2 for s in self.slices)

    @property
    def effective_count(self) -> float:
        """1/HHI — the workbook's "effective positions"."""
        hhi = self.herfindahl
        return (1.0 / hhi) if hhi > 0 else 0.0


@dataclass(frozen=True, slots=True)
class ReturnPoint:
    """One point on a value or return series."""

    as_of: date
    value: float
    #: Net external flow on this date. Required for time-weighted return —
    #: without it a deposit reads as performance.
    net_flow: float = 0.0


@dataclass(slots=True)
class AlertEvaluation:
    """The result of evaluating one rule against one subject."""

    key: str
    label: str
    category: AlertCategory
    severity: AlertSeverity
    status: AlertStatus
    condition: str
    action: str
    observed: float | str | None = None
    threshold: float | str | None = None
    ticker: str | None = None
    company_id: str | None = None
    detail: str = ""

    @property
    def is_triggered(self) -> bool:
        return self.status is AlertStatus.TRIGGERED

    @property
    def sort_key(self) -> tuple[int, int, str]:
        return (
            0 if self.is_triggered else 1,
            self.severity.rank,
            self.label,
        )


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class PortfolioError(Exception):
    """Base class for portfolio failures."""


class InsufficientHolding(PortfolioError):
    """A sell exceeds the quantity held at that point in the ledger.

    Raised rather than allowing a negative position: short positions are a
    genuine feature that this module does not implement, and silently
    permitting a negative quantity would misreport every downstream weight.
    """


class InvalidTransaction(PortfolioError):
    """A transaction is internally inconsistent."""
