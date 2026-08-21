"""Persistence for the portfolio layer.

Eight tables. The shape follows from one decision stated in
`domain/portfolio/types.py` and worth repeating here: **positions are not
stored**. There is no `positions` table. Quantity, average cost and realised
P&L are replayed from `portfolio_transactions` on every read.

That costs a replay per request — measured at well under a millisecond for a
realistic ledger — and buys the guarantee that a back-dated or corrected
transaction is reflected everywhere at once, with no repair job and no
possibility of a stored position disagreeing with the ledger that produced it.

What *is* stored is anything that cannot be derived: the transactions
themselves, dated valuation snapshots (a price history cannot be reconstructed
after the fact), benchmark levels, alert state, and user intent such as target
weights and watchlist buy prices.
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Boolean, Date, DateTime, Float, ForeignKey, Index, Integer, JSON, String,
    Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class Portfolio(Base):
    """A named book of holdings. A user may run several."""

    __tablename__ = "portfolios"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    base_currency: Mapped[str] = mapped_column(String(8), default="INR")
    #: Cost relief convention. FIFO matches Indian listed-equity tax practice.
    cost_basis: Mapped[str] = mapped_column(String(24), default="fifo")
    benchmark: Mapped[str] = mapped_column(String(32), default="NIFTY 50")

    #: Policy limits. Defaults mirror `0B Control Panel` and `39 Analytics`.
    max_position_size: Mapped[float] = mapped_column(Float, default=0.10)
    max_sector_weight: Mapped[float] = mapped_column(Float, default=0.35)
    margin_of_safety: Mapped[float] = mapped_column(Float, default=0.20)
    risk_free_rate: Mapped[float] = mapped_column(Float, default=0.07)
    target_positions: Mapped[int] = mapped_column(Integer, default=15)

    inception_date: Mapped[date | None] = mapped_column(Date)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    transactions: Mapped[list["PortfolioTransaction"]] = relationship(
        back_populates="portfolio", cascade="all, delete-orphan",
    )
    snapshots: Mapped[list["PortfolioSnapshot"]] = relationship(
        back_populates="portfolio", cascade="all, delete-orphan",
    )
    targets: Mapped[list["AllocationTarget"]] = relationship(
        back_populates="portfolio", cascade="all, delete-orphan",
    )

    __table_args__ = (
        UniqueConstraint("owner_id", "name", name="uq_portfolio_owner_name"),
    )


class PortfolioTransaction(Base):
    """One ledger entry — the only source of truth about a holding."""

    __tablename__ = "portfolio_transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    portfolio_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("portfolios.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    company_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("companies.id", ondelete="SET NULL"), index=True
    )
    #: Denormalised deliberately: a ticker must survive the company row being
    #: deleted, or a historical ledger becomes unreadable.
    ticker: Mapped[str] = mapped_column(String(32), nullable=False, index=True)

    txn_type: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    #: Tiebreak for same-day ordering. Two trades on one date relieve cost
    #: differently depending on sequence, and row order is not a guarantee.
    sequence: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    quantity: Mapped[float] = mapped_column(Float, default=0.0)
    price: Mapped[float] = mapped_column(Float, default=0.0)
    fees: Mapped[float] = mapped_column(Float, default=0.0)
    taxes: Mapped[float] = mapped_column(Float, default=0.0)

    #: Corporate-action ratio, read as "ratio_to for every ratio_from".
    ratio_from: Mapped[float | None] = mapped_column(Float)
    ratio_to: Mapped[float | None] = mapped_column(Float)

    notes: Mapped[str | None] = mapped_column(Text)
    external_ref: Mapped[str | None] = mapped_column(String(80))

    portfolio: Mapped[Portfolio] = relationship(back_populates="transactions")

    __table_args__ = (
        Index("ix_txn_portfolio_date", "portfolio_id", "trade_date", "sequence"),
        Index("ix_txn_portfolio_ticker", "portfolio_id", "ticker"),
    )


class PortfolioSnapshot(Base):
    """A dated valuation of the whole book.

    Stored rather than derived because it cannot be reconstructed: recomputing
    last March's portfolio value needs last March's prices, and the platform
    holds only the current one. Without snapshots there is no return series,
    and without a return series there is no volatility, Sharpe or drawdown.
    """

    __tablename__ = "portfolio_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    portfolio_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("portfolios.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    as_of: Mapped[date] = mapped_column(Date, nullable=False, index=True)

    market_value: Mapped[float] = mapped_column(Float, default=0.0)
    cost_basis: Mapped[float] = mapped_column(Float, default=0.0)
    cash: Mapped[float] = mapped_column(Float, default=0.0)
    #: External flows on this date. Required for time-weighted return —
    #: without it a deposit is indistinguishable from performance.
    net_flow: Mapped[float] = mapped_column(Float, default=0.0)
    position_count: Mapped[int] = mapped_column(Integer, default=0)
    benchmark_level: Mapped[float | None] = mapped_column(Float)

    portfolio: Mapped[Portfolio] = relationship(back_populates="snapshots")

    __table_args__ = (
        UniqueConstraint("portfolio_id", "as_of", name="uq_snapshot_portfolio_date"),
    )

    @property
    def total_value(self) -> float:
        return self.market_value + self.cash


class BenchmarkLevel(Base):
    """Index closing levels — the comparison series for beta and alpha."""

    __tablename__ = "benchmark_levels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    as_of: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    close: Mapped[float] = mapped_column(Float, nullable=False)

    __table_args__ = (
        UniqueConstraint("symbol", "as_of", name="uq_benchmark_symbol_date"),
    )


class AllocationTarget(Base):
    """A user's intended weight for a bucket — intent, so it must be stored."""

    __tablename__ = "allocation_targets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    portfolio_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("portfolios.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    dimension: Mapped[str] = mapped_column(String(20), nullable=False)
    bucket_key: Mapped[str] = mapped_column(String(120), nullable=False)
    target_weight: Mapped[float] = mapped_column(Float, nullable=False)

    portfolio: Mapped[Portfolio] = relationship(back_populates="targets")

    __table_args__ = (
        UniqueConstraint(
            "portfolio_id", "dimension", "bucket_key", name="uq_target_bucket",
        ),
    )


class Watchlist(Base):
    """A named list of candidates not yet owned."""

    __tablename__ = "watchlists"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    entries: Mapped[list["WatchlistEntry"]] = relationship(
        back_populates="watchlist", cascade="all, delete-orphan",
    )

    __table_args__ = (
        UniqueConstraint("owner_id", "name", name="uq_watchlist_owner_name"),
    )


class WatchlistEntry(Base):
    """One candidate, with the price at which the thesis becomes actionable."""

    __tablename__ = "watchlist_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    watchlist_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("watchlists.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    company_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("companies.id", ondelete="SET NULL"), index=True
    )
    ticker: Mapped[str] = mapped_column(String(32), nullable=False, index=True)

    #: Buy-below price. When absent the engine derives one from intrinsic value
    #: and the portfolio's margin of safety, rather than leaving the row inert.
    buy_below: Mapped[float | None] = mapped_column(Float)
    target_price: Mapped[float | None] = mapped_column(Float)
    conviction: Mapped[str | None] = mapped_column(String(20))
    note: Mapped[str | None] = mapped_column(Text)
    added_on: Mapped[date | None] = mapped_column(Date)

    watchlist: Mapped[Watchlist] = relationship(back_populates="entries")

    __table_args__ = (
        UniqueConstraint("watchlist_id", "ticker", name="uq_watch_entry_ticker"),
    )


class AlertRuleOverride(Base):
    """A user's change to a built-in rule, or a rule they defined.

    Built-in rules are code (they encode the workbook's specification); this
    table records only *departures* from them. Storing every rule as a row
    would make the shipped defaults mutable and unversionable.
    """

    __tablename__ = "alert_rule_overrides"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    portfolio_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("portfolios.id", ondelete="CASCADE"), index=True
    )
    owner_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    rule_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    threshold: Mapped[float | None] = mapped_column(Float)
    severity: Mapped[str | None] = mapped_column(String(16))
    #: Set for user-defined rules, which have no built-in counterpart.
    label: Mapped[str | None] = mapped_column(String(160))
    metric: Mapped[str | None] = mapped_column(String(64))
    comparator: Mapped[str | None] = mapped_column(String(12))
    is_custom: Mapped[bool] = mapped_column(Boolean, default=False)

    __table_args__ = (
        UniqueConstraint(
            "owner_id", "portfolio_id", "rule_key", name="uq_alert_override",
        ),
    )


class AlertEvent(Base):
    """A fired alert, retained so the same trigger is not re-notified daily.

    Deduplication is by `(portfolio, rule, ticker, open)` — an alert stays open
    until the condition clears, and re-firing an already-open alert updates its
    observed value rather than creating a second row.
    """

    __tablename__ = "alert_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    portfolio_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("portfolios.id", ondelete="CASCADE"), index=True
    )
    owner_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    rule_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    ticker: Mapped[str | None] = mapped_column(String(32), index=True)
    company_id: Mapped[str | None] = mapped_column(String(36), index=True)

    label: Mapped[str] = mapped_column(String(200), nullable=False)
    category: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), default="triggered", index=True)

    condition: Mapped[str | None] = mapped_column(String(200))
    action: Mapped[str | None] = mapped_column(String(200))
    observed: Mapped[str | None] = mapped_column(String(80))
    threshold: Mapped[str | None] = mapped_column(String(80))
    detail: Mapped[str | None] = mapped_column(Text)

    first_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cleared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: How many consecutive evaluations have seen this condition true.
    occurrences: Mapped[int] = mapped_column(Integer, default=1)

    __table_args__ = (
        Index("ix_alert_open", "portfolio_id", "status", "severity"),
        Index("ix_alert_dedup", "portfolio_id", "rule_key", "ticker", "status"),
    )

    @property
    def is_open(self) -> bool:
        return self.status in {"triggered", "acknowledged"}


class PriceHistory(Base):
    """Daily closes per ticker.

    Module 5 recorded "momentum is structurally missing — no price history
    ingested". This is that table. Position-level volatility, beta and the
    liquidity screen all read from here.
    """

    __tablename__ = "price_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    as_of: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    close: Mapped[float] = mapped_column(Float, nullable=False)
    volume: Mapped[float | None] = mapped_column(Float)
    #: Average daily traded value in ₹, for the liquidity screen.
    traded_value: Mapped[float | None] = mapped_column(Float)
    # ---- Phase 1: OHLC + provenance for the historical-price sync --------
    #: Nullable: the demo seed wrote close-only bars, and a figure the source
    #: did not report is stored as absent rather than back-filled.
    day_open: Mapped[float | None] = mapped_column(Float)
    day_high: Mapped[float | None] = mapped_column(Float)
    day_low: Mapped[float | None] = mapped_column(Float)
    #: 'mock' | 'yahoo' | 'fmp' | … — which tier supplied the bar.
    provider: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (
        UniqueConstraint("ticker", "as_of", name="uq_price_ticker_date"),
        Index("ix_price_ticker_date", "ticker", "as_of"),
    )
