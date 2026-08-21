"""Persistent market quotes — Phase 1.

The market router and `LiveMarketService` deliberately keep live quotes in a
short-lived cache, with `companies.current_price` as the only stored figure.
That is correct while quotes are ephemeral, but the 5,000-company sync needs a
durable record: a page visited after the cache TTL must not fall back to a
price that is weeks old, and "what did we know and when" is an operational
question the moment more than one provider can supply a figure.

One row per company: the latest quote. History is `price_history`, which keeps
daily bars; this table keeps the *current* snapshot, refreshed in place by the
`price_sync` job. `provider` names the tier that produced the figure, and a
mock provider writes ``provider='mock'`` so synthetic data can never be
mistaken for a real quote (or mixed into a real record — the mock chain and
the real chain are mutually exclusive by configuration).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import DateTime, Float, ForeignKey, Index, JSON, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MarketQuote(Base):
    """The latest persisted quote for one company."""

    __tablename__ = "market_quotes"

    #: One row per company — the company is the natural key, which makes the
    #: upsert in the sync job a single-statement conflict update.
    company_id: Mapped[str] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), primary_key=True,
    )
    #: Listing identifiers as the provider saw them, denormalised so the table
    #: can be read (and audited) without a join.
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    exchange: Mapped[str] = mapped_column(String(16), nullable=False)

    ltp: Mapped[float | None] = mapped_column(Float)
    previous_close: Mapped[float | None] = mapped_column(Float)
    day_open: Mapped[float | None] = mapped_column(Float)
    day_high: Mapped[float | None] = mapped_column(Float)
    day_low: Mapped[float | None] = mapped_column(Float)
    volume: Mapped[float | None] = mapped_column(Float)
    change_amt: Mapped[float | None] = mapped_column(Float)
    change_percent: Mapped[float | None] = mapped_column(Float)
    week_52_high: Mapped[float | None] = mapped_column(Float)
    week_52_low: Mapped[float | None] = mapped_column(Float)

    #: open | closed | weekend | unknown — as computed against IST at fetch
    #: time. Stored because "the quote was fetched while the market was shut"
    #: explains a stale figure better than the timestamp alone.
    market_status: Mapped[str] = mapped_column(
        String(12), default="unknown", server_default="unknown", nullable=False,
    )
    #: The tier that answered: 'mock', 'yahoo', 'fmp', 'finnhub', 'internal'…
    #: Provenance is data, not a code path.
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Raw provider metadata (currency, source label, confidence), for audit.
    meta: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False,
    )

    company = relationship("Company")

    __table_args__ = (
        Index("ix_market_quotes_stalest", "fetched_at"),
    )

    @property
    def is_mock(self) -> bool:
        return (self.provider or "").startswith("mock")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<MarketQuote {self.symbol} ltp={self.ltp} provider={self.provider}>"
