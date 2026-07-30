"""Module 8 seed — a demonstration portfolio with real history.

Two things are generated that the platform could not otherwise have:

* **Daily price history and benchmark levels.** Module 5 recorded "momentum is
  structurally missing — no price history ingested". Volatility, Sharpe, beta,
  drawdown and VaR are all undefined without a series, so one is synthesised.
* **A dated transaction ledger and matching valuation snapshots.** Time-weighted
  return needs snapshots that include the flows; they cannot be reconstructed
  after the fact from current prices.

The series is synthetic and deliberately labelled so throughout. It is a
geometric random walk with a fixed seed, calibrated so each holding's final
price equals the `current_price` already on the company row — otherwise the
history and the live valuation would disagree at the last observation, which
is exactly the kind of quiet inconsistency this platform is built to avoid.
"""
from __future__ import annotations

import random
from datetime import date, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models.company import Company
from app.models.portfolio import (
    BenchmarkLevel, Portfolio, PortfolioSnapshot, PortfolioTransaction,
    PriceHistory, Watchlist, WatchlistEntry,
)
from app.services.portfolio.engine import PortfolioEngine

#: Fixed so every run of the demo produces identical figures.
SEED = 20250730
#: The demo book's inception. Price history must begin here or earlier: a
#: series that starts after the first purchase makes the return series open
#: partway through the holding period, which reported a loss on a book that
#: had in fact gained. The window is anchored to this date rather than to a
#: fixed lookback from today.
DEMO_INCEPTION = date(2023, 1, 2)
TRADING_DAYS_PER_YEAR = 252
DEMO_OWNER = "dev-user"

#: The demonstration book. Weights are deliberately uneven so concentration
#: alerts have something real to fire on.
DEMO_HOLDINGS: tuple[tuple[str, int, float, str], ...] = (
    ("RELIANCE",   260, 2380.0, "2023-02-06"),
    ("TCS",        130, 3260.0, "2023-02-14"),
    ("HDFCBANK",   380, 1585.0, "2023-03-02"),
    ("INFY",       320, 1395.0, "2023-04-11"),
    ("HINDUNILVR", 110, 2540.0, "2023-05-18"),
    ("TITAN",       95, 2960.0, "2023-06-22"),
    ("SUNPHARMA",  210, 1080.0, "2023-08-09"),
    ("LT",         105, 3180.0, "2023-09-14"),
    ("BHARTIARTL", 300,  980.0, "2023-11-07"),
    ("ASIANPAINT", 120, 2870.0, "2024-01-16"),
    ("BHARATCP",   900,  238.0, "2024-02-20"),
)

DEMO_WATCHLIST: tuple[tuple[str, float | None, str], ...] = (
    ("MARUTI", 10500.0, "Waiting for a better entry after the rally."),
    ("ULTRACEMCO", 9200.0, "Cement cycle turning; size on weakness."),
    ("NESTLEIND", 2200.0, "Quality compounder, valuation demanding."),
    ("WIPRO", 420.0, "Turnaround unproven — small position only."),
    ("CIPLA", 1350.0, "Watching US pipeline approvals."),
)

#: Annual drift and volatility used to generate each walk.
_DRIFT = 0.11
_VOLATILITY = 0.24


def _walk(
    final_price: float, days: int, rng: random.Random
) -> list[float]:
    """A geometric random walk ending exactly at `final_price`.

    Generated forward from an arbitrary base then rescaled, so the last
    observation equals the price already stored on the company. History that
    disagrees with the live price at its own final point would make every
    return calculation subtly wrong.
    """
    daily_vol = _VOLATILITY / (TRADING_DAYS_PER_YEAR ** 0.5)
    daily_drift = _DRIFT / TRADING_DAYS_PER_YEAR
    series = [100.0]
    for _ in range(days - 1):
        shock = rng.gauss(daily_drift - 0.5 * daily_vol ** 2, daily_vol)
        series.append(max(1e-6, series[-1] * (1.0 + shock)))
    scale = final_price / series[-1]
    return [round(value * scale, 2) for value in series]


def _trading_days(start: date, end: date) -> list[date]:
    """Weekdays between two dates. Exchange holidays are not modelled."""
    days: list[date] = []
    cursor = start
    while cursor <= end:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


def seed_price_history(
    db: Session, *, start: date | None = None, end: date | None = None
) -> int:
    """Generate daily closes for every company, ending at its current price."""
    end = end or date.today()
    start = start or (DEMO_INCEPTION - timedelta(days=5))
    days = _trading_days(start, end)
    rng = random.Random(SEED)

    companies = list(db.scalars(select(Company)).all())
    db.execute(delete(PriceHistory))

    written = 0
    for company in companies:
        if not company.current_price:
            continue
        series = _walk(company.current_price, len(days), rng)
        # Traded value in ₹: a plausible fraction of market cap turning over
        # daily, which is what the liquidity screen divides by.
        turnover = (company.market_cap or 10_000.0) * 1e7 * 0.0012
        for when, close in zip(days, series):
            db.add(PriceHistory(
                ticker=company.ticker, as_of=when, close=close,
                volume=round(turnover / close, 0),
                traded_value=round(turnover, 2),
            ))
            written += 1
    db.commit()
    return written


def seed_benchmark(
    db: Session, symbol: str = "NIFTY 50",
    *, start: date | None = None, end: date | None = None,
) -> int:
    """Generate index levels over the same window as the price history."""
    end = end or date.today()
    start = start or (DEMO_INCEPTION - timedelta(days=5))
    days = _trading_days(start, end)
    rng = random.Random(SEED + 1)

    db.execute(delete(BenchmarkLevel).where(BenchmarkLevel.symbol == symbol))
    # The index is less volatile than a single stock, which is what makes beta
    # and tracking error meaningful rather than noise.
    level = 18_000.0
    daily_vol = 0.13 / (TRADING_DAYS_PER_YEAR ** 0.5)
    daily_drift = 0.09 / TRADING_DAYS_PER_YEAR
    for when in days:
        level = max(1.0, level * (1.0 + rng.gauss(daily_drift, daily_vol)))
        db.add(BenchmarkLevel(symbol=symbol, as_of=when, close=round(level, 2)))
    db.commit()
    return len(days)


def seed_demo_portfolio(db: Session, owner_id: str = DEMO_OWNER) -> Portfolio:
    """Create the demonstration portfolio, its ledger and its snapshots."""
    existing = db.scalar(
        select(Portfolio).where(
            Portfolio.owner_id == owner_id, Portfolio.name == "Core Equity"
        )
    )
    if existing is not None:
        return existing

    portfolio = Portfolio(
        owner_id=owner_id, name="Core Equity",
        description="Demonstration book — synthetic prices, illustrative only.",
        benchmark="NIFTY 50", inception_date=DEMO_INCEPTION,
        max_position_size=0.12, max_sector_weight=0.35,
    )
    db.add(portfolio)
    db.flush()

    tickers = {c.ticker: c for c in db.scalars(select(Company)).all()}
    sequence = 0

    def add(ticker: str, kind: str, when: date, **kwargs) -> None:
        nonlocal sequence
        company = tickers.get(ticker)
        db.add(PortfolioTransaction(
            portfolio_id=portfolio.id,
            company_id=company.id if company else None,
            ticker=ticker, txn_type=kind, trade_date=when,
            sequence=sequence, **kwargs,
        ))
        sequence += 1

    add("", "deposit", DEMO_INCEPTION, quantity=1, price=6_000_000)

    for ticker, quantity, price, when in DEMO_HOLDINGS:
        if ticker not in tickers:
            continue
        trade_date = date.fromisoformat(when)
        add(ticker, "buy", trade_date, quantity=quantity, price=price,
            fees=round(quantity * price * 0.0005, 2))

    # A corporate action, a dividend, a partial exit and a top-up, so the
    # ledger exercises every path the position engine implements.
    if "RELIANCE" in tickers:
        add("RELIANCE", "bonus", date(2023, 10, 12), ratio_from=4, ratio_to=1)
    if "TCS" in tickers:
        add("TCS", "dividend", date(2023, 7, 20), quantity=130, price=27.0)
        add("TCS", "dividend", date(2024, 7, 18), quantity=130, price=31.0)
    if "INFY" in tickers:
        add("INFY", "sell", date(2024, 3, 21), quantity=120, price=1655.0,
            fees=99.3)
    if "HDFCBANK" in tickers:
        add("HDFCBANK", "buy", date(2024, 6, 11), quantity=120, price=1520.0,
            fees=91.2)
    add("", "deposit", date(2024, 4, 8), quantity=1, price=750_000)

    db.commit()
    return portfolio


def seed_snapshots(db: Session, portfolio_id: int, *, every: int = 5) -> int:
    """Build the valuation history from stored prices and the ledger.

    Positions are replayed as at each date and valued at that date's close, so
    the series is a genuine reconstruction rather than today's holdings priced
    backwards — which would silently assume the investor always owned what they
    own now.
    """
    portfolio = db.get(Portfolio, portfolio_id)
    if portfolio is None:
        return 0

    transactions = list(db.scalars(
        select(PortfolioTransaction)
        .where(PortfolioTransaction.portfolio_id == portfolio_id)
        .order_by(PortfolioTransaction.trade_date, PortfolioTransaction.sequence)
    ).all())
    if not transactions:
        return 0

    prices: dict[date, dict[str, float]] = {}
    for row in db.scalars(select(PriceHistory)).all():
        prices.setdefault(row.as_of, {})[row.ticker] = row.close

    benchmarks = {
        row.as_of: row.close for row in db.scalars(
            select(BenchmarkLevel).where(
                BenchmarkLevel.symbol == portfolio.benchmark
            )
        ).all()
    }

    start = transactions[0].trade_date
    # Sample every Nth trading day *from inception*, and always keep the last
    # observation. Slicing a descending or unfiltered list silently discarded
    # the first eighteen months of history, which made the return series start
    # near its own peak and report a loss on a book that had gained.
    eligible = sorted(d for d in prices if d >= start)
    dates = eligible[::every]
    if eligible and dates and dates[-1] != eligible[-1]:
        dates.append(eligible[-1])
    db.execute(
        delete(PortfolioSnapshot).where(
            PortfolioSnapshot.portfolio_id == portfolio_id
        )
    )

    engine = PortfolioEngine()
    written = 0
    for when in dates:
        history = [t for t in transactions if t.trade_date <= when]
        if not history:
            continue
        replay = engine.positions.replay(history)
        closes = prices.get(when, {})
        market_value = sum(
            position.quantity * closes[position.ticker]
            for position in replay.open_positions
            if position.ticker in closes
        )
        flow = sum(
            t.quantity * t.price for t in history
            if t.trade_date == when and t.txn_type in ("deposit", "withdrawal")
        )
        db.add(PortfolioSnapshot(
            portfolio_id=portfolio_id, as_of=when,
            market_value=round(market_value, 2),
            cost_basis=round(replay.total_cost, 2),
            cash=round(replay.cash.balance, 2),
            net_flow=round(flow, 2),
            position_count=len(replay.open_positions),
            benchmark_level=benchmarks.get(when),
        ))
        written += 1
    db.commit()
    return written


def seed_watchlist(db: Session, owner_id: str = DEMO_OWNER) -> Watchlist:
    existing = db.scalar(
        select(Watchlist).where(
            Watchlist.owner_id == owner_id, Watchlist.name == "Candidates"
        )
    )
    if existing is not None:
        return existing

    watchlist = Watchlist(
        owner_id=owner_id, name="Candidates",
        description="Names under coverage but not yet owned.",
    )
    db.add(watchlist)
    db.flush()

    tickers = {c.ticker: c for c in db.scalars(select(Company)).all()}
    for ticker, buy_below, note in DEMO_WATCHLIST:
        company = tickers.get(ticker)
        if company is None:
            continue
        db.add(WatchlistEntry(
            watchlist_id=watchlist.id, ticker=ticker,
            company_id=company.id, buy_below=buy_below, note=note,
            added_on=date(2024, 5, 1),
        ))
    db.commit()
    return watchlist


def seed_module8(db: Session, owner_id: str = DEMO_OWNER) -> dict[str, int]:
    """Everything Module 8 needs, in dependency order."""
    prices = seed_price_history(db)
    benchmark = seed_benchmark(db)
    portfolio = seed_demo_portfolio(db, owner_id)
    snapshots = seed_snapshots(db, portfolio.id)
    watchlist = seed_watchlist(db, owner_id)
    return {
        "price_rows": prices,
        "benchmark_rows": benchmark,
        "portfolio_id": portfolio.id,
        "snapshots": snapshots,
        "watchlist_id": watchlist.id,
    }
