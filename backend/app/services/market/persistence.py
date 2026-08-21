"""Durable market-data persistence — Phase 1.

Redis remains the fast serving cache; these functions are the write-through
and read-back against PostgreSQL/SQLite so a quote survives its cache TTL and
the 5,000-company sync has somewhere durable to land.

Both writes are UPSERTS on the natural key — `market_quotes.company_id` and
`price_history.(ticker, as_of)` — so running the same sync twice (or ten
times) updates rows in place and creates nothing new. That is the idempotency
the sync jobs are measured by, and it is enforced by the database constraint,
not by the caller's discipline.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.data.providers.base import Quote
from app.models.company import Company
from app.models.market import MarketQuote
from app.models.portfolio import PriceHistory

PROVENANCE_MOCK = "mock"
PROVENANCE_REAL = "real"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _insert_for(db: Session):
    """The dialect's INSERT constructor: upsert syntax is shared, the
    statement object is not."""
    name = db.get_bind().dialect.name
    return pg_insert if name == "postgresql" else sqlite_insert


# ===========================================================================
# Quotes
# ===========================================================================
def upsert_quote(
    db: Session,
    company: Company,
    quote: Quote,
    *,
    provider: str,
    fetched_at: datetime | None = None,
) -> MarketQuote:
    """Persist the latest quote for one company (one row, refreshed in place)."""
    now = fetched_at or _utcnow()
    values = {
        "company_id": company.id,
        "symbol": company.ticker,
        "exchange": company.exchange or "NSE",
        "ltp": quote.price,
        "previous_close": quote.previous_close,
        "day_open": quote.day_open,
        "day_high": quote.day_high,
        "day_low": quote.day_low,
        "volume": quote.volume,
        "change_amt": quote.change,
        "change_percent": (
            quote.percent_change
            if quote.percent_change is not None and abs(quote.percent_change) <= 100
            else None
        ),
        "week_52_high": quote.week_52_high,
        "week_52_low": quote.week_52_low,
        "market_status": quote.market_status or "unknown",
        "provider": provider,
        "meta": {"source": provider, "currency": "INR"},
        "fetched_at": now,
        # created_at/updated_at are server-managed defaults on insert; on the
        # conflict path updated_at is set explicitly below.
        "created_at": now,
        "updated_at": now,
    }
    stmt = _insert_for(db)(MarketQuote).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=[MarketQuote.__table__.c["company_id"]],
        set_={
            "symbol": stmt.excluded.symbol,
            "exchange": stmt.excluded.exchange,
            "ltp": stmt.excluded.ltp,
            "previous_close": stmt.excluded.previous_close,
            "day_open": stmt.excluded.day_open,
            "day_high": stmt.excluded.day_high,
            "day_low": stmt.excluded.day_low,
            "volume": stmt.excluded.volume,
            "change_amt": stmt.excluded.change_amt,
            "change_percent": stmt.excluded.change_percent,
            "week_52_high": stmt.excluded.week_52_high,
            "week_52_low": stmt.excluded.week_52_low,
            "market_status": stmt.excluded.market_status,
            "provider": stmt.excluded.provider,
            "meta": stmt.excluded.meta,
            "fetched_at": stmt.excluded.fetched_at,
            "updated_at": stmt.excluded.updated_at,
        },
    )
    db.execute(stmt)
    db.commit()

    # The upsert bypassed the ORM, so any row already in the identity map is
    # stale. Expire everything before the read-back returns the truth.
    db.expire_all()
    row = db.get(MarketQuote, company.id)
    assert row is not None
    return row


def latest_quote(db: Session, company_id: str) -> MarketQuote | None:
    return db.get(MarketQuote, company_id)


# ===========================================================================
# Historical daily bars
# ===========================================================================
def upsert_daily_bars(
    db: Session,
    ticker: str,
    bars: Sequence[dict[str, Any]],
    *,
    provider: str,
) -> int:
    """Idempotent OHLCV upsert on (ticker, as_of).

    Bars arrive oldest-first from the providers; ordering is not assumed —
    the conflict target is the date, so any order produces the same rows.
    Returns the number of bars written (not newly created: repeated syncs
    rewrite the same rows and count the same).
    """
    if not bars:
        return 0

    model = PriceHistory
    stmt_cls = _insert_for(db)(model)
    written = 0
    # Chunked executemany keeps one statement per chunk inside SQLite's
    # variable limit while staying a single round-trip per chunk.
    for chunk_start in range(0, len(bars), 400):
        chunk = bars[chunk_start:chunk_start + 400]
        rows = []
        for bar in chunk:
            close = _as_float(bar.get("close"))
            if close is None:
                continue  # a bar without a close is not a bar
            rows.append({
                "ticker": ticker,
                "as_of": _as_date(bar.get("date")),
                "close": close,
                "volume": _as_float(bar.get("volume")),
                "day_open": _as_float(bar.get("open")),
                "day_high": _as_float(bar.get("high")),
                "day_low": _as_float(bar.get("low")),
                "provider": provider,
                "created_at": _utcnow(),
                "updated_at": _utcnow(),
            })
        if not rows:
            continue
        stmt = stmt_cls.values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=[model.__table__.c["ticker"], model.__table__.c["as_of"]],
            set_={
                "close": stmt.excluded.close,
                "volume": stmt.excluded.volume,
                "day_open": stmt.excluded.day_open,
                "day_high": stmt.excluded.day_high,
                "day_low": stmt.excluded.day_low,
                "provider": stmt.excluded.provider,
                "updated_at": stmt.excluded.updated_at,
            },
        )
        db.execute(stmt)
        written += len(rows)
    db.commit()
    # Same identity-map staleness as upsert_quote: the raw upsert does not
    # refresh objects the session already holds.
    db.expire_all()
    return written


def price_series(
    db: Session, ticker: str, *, days: int | None = None,
) -> list[PriceHistory]:
    """Daily bars oldest→newest for one ticker."""
    stmt = select(PriceHistory).where(PriceHistory.ticker == ticker)
    if days is not None:
        stmt = stmt.where(
            PriceHistory.as_of >= date.today() - timedelta(days=days)
        )
    stmt = stmt.order_by(PriceHistory.as_of.asc())
    return list(db.scalars(stmt).all())


#: API range names → lookback days. `1D` returns the latest trading day's bar
#: (daily data has no intraday granularity — that arrives with a licensed
#: feed in Phase 6, and the endpoint labels the granularity it serves).
RANGE_DAYS: dict[str, int | None] = {
    "1D": 3,       # calendar days that certainly contain ≥1 trading day
    "1W": 7,
    "1M": 31,
    "3M": 93,
    "6M": 186,
    "1Y": 366,
    "3Y": 1096,
    "5Y": 1826,
    "MAX": None,   # everything held
}


def bars_for_range(
    db: Session, ticker: str, range_name: str,
) -> list[PriceHistory]:
    days = RANGE_DAYS.get((range_name or "1M").upper())
    if days is None:
        series = price_series(db, ticker)
    else:
        series = price_series(db, ticker, days=days)
    if (range_name or "").upper() == "1D" and series:
        # Only the most recent trading day.
        return series[-1:]
    return series


# ===========================================================================
# helpers
# ===========================================================================
def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])
