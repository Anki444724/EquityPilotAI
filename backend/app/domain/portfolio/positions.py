"""Position engine — replays a transaction ledger into positions.

Everything a portfolio knows about a holding is derived here. Nothing about
quantity, cost or realised P&L is stored; it is recomputed from the ledger on
every read, which means a corrected or back-dated transaction is reflected
immediately and consistently rather than requiring a repair job.

Ordering is the whole game. Transactions replay in `(date, sequence)` order,
because a bonus issue that lands between two buys changes the cost per share of
the first lot but not the second, and a split applied out of order silently
misstates every subsequent cost relief.
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import date
from typing import Iterable, Protocol, Sequence

from app.domain.portfolio.types import (
    CashLedger, CostBasisMethod, InsufficientHolding, InvalidTransaction, Lot,
    Position, RealisedTrade, TransactionType,
)

#: Quantity below which a holding is treated as closed. Split and bonus ratios
#: are floats, so an exact zero is not reliably reachable.
QUANTITY_EPSILON = 1e-9


class TransactionLike(Protocol):
    """Structural type for anything replayable.

    A Protocol rather than a base class so the engine works against ORM rows,
    plain dataclasses and test fixtures without any of them importing it.
    """

    ticker: str
    txn_type: str
    trade_date: date
    quantity: float
    price: float
    fees: float
    taxes: float
    ratio_from: float | None
    ratio_to: float | None
    sequence: int


@dataclass(slots=True)
class ReplayResult:
    """Everything the ledger implies."""

    positions: dict[str, Position]
    cash: CashLedger
    realised: list[RealisedTrade]

    @property
    def open_positions(self) -> list[Position]:
        return [p for p in self.positions.values() if p.is_open]

    @property
    def closed_positions(self) -> list[Position]:
        return [p for p in self.positions.values() if not p.is_open]

    @property
    def realised_pnl(self) -> float:
        return sum(t.pnl for t in self.realised)

    @property
    def total_cost(self) -> float:
        return sum(p.cost for p in self.open_positions)

    @property
    def total_dividends(self) -> float:
        return sum(p.dividends for p in self.positions.values())


def sort_key(txn: TransactionLike) -> tuple[date, int]:
    """Chronological order, with an explicit tiebreak.

    Two transactions on one date must have a defined order — a buy and a sell
    of the same scrip on the same day relieve cost differently depending on
    which came first, and a database's natural row order is not a guarantee.
    """
    return (txn.trade_date, txn.sequence)


class PositionEngine:
    """Replays transactions into positions, cash and realised trades."""

    def __init__(self, method: CostBasisMethod = CostBasisMethod.FIFO) -> None:
        self.method = method

    # ------------------------------------------------------------------
    def replay(self, transactions: Iterable[TransactionLike]) -> ReplayResult:
        ordered = sorted(transactions, key=sort_key)
        positions: dict[str, Position] = {}
        lots: dict[str, deque[Lot]] = defaultdict(deque)
        cash = CashLedger()
        realised: list[RealisedTrade] = []

        for txn in ordered:
            kind = TransactionType(txn.txn_type)
            if kind.is_cash_only:
                self._apply_cash(cash, kind, txn)
                continue

            ticker = txn.ticker
            if not ticker:
                raise InvalidTransaction(f"{kind.value} requires a ticker")
            position = positions.setdefault(ticker, Position(ticker=ticker))

            handler = {
                TransactionType.BUY: self._buy,
                TransactionType.SELL: self._sell,
                TransactionType.DIVIDEND: self._dividend,
                TransactionType.BONUS: self._bonus,
                TransactionType.SPLIT: self._split,
                TransactionType.RIGHTS: self._rights,
            }[kind]
            handler(position, lots[ticker], cash, txn, realised)

            position.last_traded = txn.trade_date
            fee = (txn.fees or 0.0) + (txn.taxes or 0.0)
            if fee:
                position.fees += fee

        for ticker, position in positions.items():
            position.lots = list(lots[ticker])
            # Re-derive cost from surviving lots so cost and quantity can never
            # disagree, however many corporate actions intervened.
            position.cost = sum(lot.cost for lot in position.lots)
            position.quantity = sum(lot.quantity for lot in position.lots)
            if position.quantity <= QUANTITY_EPSILON:
                position.quantity = 0.0
                position.cost = 0.0

        return ReplayResult(positions=positions, cash=cash, realised=realised)

    # ---------------------------------------------------------- handlers
    @staticmethod
    def _apply_cash(cash: CashLedger, kind: TransactionType, txn) -> None:
        amount = abs(txn.quantity * txn.price) if txn.quantity else abs(txn.price)
        mapping = {
            TransactionType.DEPOSIT: "deposits",
            TransactionType.WITHDRAWAL: "withdrawals",
            TransactionType.FEE: "fees",
            TransactionType.TAX: "taxes",
            TransactionType.INTEREST: "interest",
        }
        setattr(cash, mapping[kind], getattr(cash, mapping[kind]) + amount)

    def _buy(self, position: Position, lots: deque[Lot], cash: CashLedger,
             txn, realised: list) -> None:
        if txn.quantity <= 0:
            raise InvalidTransaction("a buy needs a positive quantity")
        fees = (txn.fees or 0.0) + (txn.taxes or 0.0)
        gross = txn.quantity * txn.price
        # Fees capitalise into cost, which is both the tax treatment and the
        # only way a round-trip at an unchanged price shows the loss it is.
        cost_per_unit = (gross + fees) / txn.quantity
        lots.append(Lot(txn.trade_date, txn.quantity, cost_per_unit))
        cash.buys += gross + fees
        if position.first_bought is None:
            position.first_bought = txn.trade_date

    def _sell(self, position: Position, lots: deque[Lot], cash: CashLedger,
              txn, realised: list) -> None:
        if txn.quantity <= 0:
            raise InvalidTransaction("a sell needs a positive quantity")
        held = sum(lot.quantity for lot in lots)
        if txn.quantity > held + QUANTITY_EPSILON:
            raise InsufficientHolding(
                f"cannot sell {txn.quantity:g} {position.ticker}: "
                f"{held:g} held on {txn.trade_date}"
            )

        fees = (txn.fees or 0.0) + (txn.taxes or 0.0)
        gross = txn.quantity * txn.price
        # Fees reduce proceeds rather than being expensed separately, so a
        # realised gain is net of the cost of realising it.
        net_per_unit = (gross - fees) / txn.quantity
        cash.sells += gross - fees

        for lot, taken in self._relieve(lots, txn.quantity):
            realised.append(RealisedTrade(
                ticker=position.ticker, sell_date=txn.trade_date,
                buy_date=lot.trade_date, quantity=taken,
                cost_per_unit=lot.cost_per_unit, sale_per_unit=net_per_unit,
            ))
            position.realised_pnl += taken * (net_per_unit - lot.cost_per_unit)

    def _relieve(self, lots: deque[Lot], quantity: float):
        """Yield (lot, quantity_taken) pairs, consuming lots in policy order."""
        if self.method is CostBasisMethod.WEIGHTED_AVERAGE:
            yield from self._relieve_average(lots, quantity)
            return
        remaining = quantity
        while remaining > QUANTITY_EPSILON and lots:
            lot = lots[0]
            taken = min(lot.quantity, remaining)
            yield lot, taken
            remaining -= taken
            if lot.quantity - taken <= QUANTITY_EPSILON:
                lots.popleft()
            else:
                lots[0] = Lot(lot.trade_date, lot.quantity - taken, lot.cost_per_unit)

    @staticmethod
    def _relieve_average(lots: deque[Lot], quantity: float):
        """Collapse to a single blended lot, then relieve from it.

        The earliest purchase date is retained so holding-period classification
        stays conservative — under averaging there is no single acquisition
        date, and assuming the earliest would overstate long-term treatment, so
        the *latest* contributing date is used instead.
        """
        total_qty = sum(lot.quantity for lot in lots)
        if total_qty <= QUANTITY_EPSILON:
            return
        total_cost = sum(lot.cost for lot in lots)
        blended = Lot(
            max(lot.trade_date for lot in lots),
            total_qty, total_cost / total_qty,
        )
        lots.clear()
        taken = min(quantity, total_qty)
        yield blended, taken
        left = total_qty - taken
        if left > QUANTITY_EPSILON:
            lots.append(Lot(blended.trade_date, left, blended.cost_per_unit))

    @staticmethod
    def _dividend(position: Position, lots: deque[Lot], cash: CashLedger,
                  txn, realised: list) -> None:
        """Dividends are income, not a cost adjustment.

        The workbook nets them against cost. That understates the cost base and
        therefore overstates return on a high-yield holding, so they are
        tracked separately and rolled into `total_pnl` instead.
        """
        held = sum(lot.quantity for lot in lots)
        amount = txn.quantity * txn.price if txn.quantity else held * txn.price
        tax = (txn.taxes or 0.0)
        position.dividends += amount - tax
        cash.dividends += amount
        if tax:
            cash.taxes += tax

    @staticmethod
    def _ratio(txn) -> float:
        """Multiplier applied to quantity by a bonus or split.

        `ratio_to : ratio_from` reads as "to for every from" — a 1:2 bonus is
        one new share for every two held, giving a multiplier of 1.5.
        """
        to_, from_ = txn.ratio_to, txn.ratio_from
        if not to_ or not from_ or from_ <= 0:
            raise InvalidTransaction(
                "a bonus or split needs ratio_from and ratio_to"
            )
        return to_ / from_

    def _bonus(self, position: Position, lots: deque[Lot], cash: CashLedger,
               txn, realised: list) -> None:
        """Free shares: quantity rises, total cost is unchanged.

        Cost per unit therefore falls proportionally, which is what makes a
        later FIFO relief correct.
        """
        multiplier = 1.0 + self._ratio(txn)
        self._rescale(lots, multiplier)

    def _split(self, position: Position, lots: deque[Lot], cash: CashLedger,
               txn, realised: list) -> None:
        """A face-value split. Same arithmetic as a bonus, different cause."""
        self._rescale(lots, self._ratio(txn))

    @staticmethod
    def _rescale(lots: deque[Lot], multiplier: float) -> None:
        if multiplier <= 0:
            raise InvalidTransaction("ratio must be positive")
        for index, lot in enumerate(lots):
            lots[index] = Lot(
                lot.trade_date,
                lot.quantity * multiplier,
                lot.cost_per_unit / multiplier,
            )

    def _rights(self, position: Position, lots: deque[Lot], cash: CashLedger,
                txn, realised: list) -> None:
        """A subscribed rights issue is economically a buy at the rights price."""
        self._buy(position, lots, cash, txn, realised)


# ---------------------------------------------------------------------------
def enrich(
    positions: Sequence[Position],
    prices: dict[str, float],
    meta: dict[str, dict] | None = None,
) -> list[Position]:
    """Attach live prices and company metadata to replayed positions.

    Kept separate from replay so the engine stays pure and testable against a
    ledger alone. A missing price leaves `current_price` as ``None``, which
    propagates to `market_value` as ``None`` rather than zero — a holding whose
    price is unknown must not silently value itself at nil.
    """
    info = meta or {}
    for position in positions:
        position.current_price = prices.get(position.ticker)
        details = info.get(position.ticker)
        if details:
            position.company_id = details.get("company_id")
            position.name = details.get("name", position.ticker)
            position.sector = details.get("sector")
            position.industry = details.get("industry")
            position.market_cap = details.get("market_cap")
            position.country = details.get("country", "India")
    return list(positions)
