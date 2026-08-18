"""Fast, shared live-price snapshots for user-facing company pages.

Company, dashboard, search, watchlist and portfolio requests never call an
external market provider. They read a short-lived shared quote cache and fall
back to the company's stored price immediately. Cache misses are queued for a
single bounded daemon worker, which refreshes Yahoo quotes sequentially.

The cache is the platform cache (Redis when configured, otherwise process
memory), so list, detail and dashboard pages use the same snapshot and TTL.
"""
from __future__ import annotations

from datetime import datetime, timezone
import queue
import threading
import time
from typing import Any

import structlog
from sqlalchemy.orm import Session

from app.data.providers.router import SOURCE_INTERNAL
from app.data.providers.symbols import resolve
from app.data.providers.yahoo import YahooProvider
from app.models.company import Company
from app.schemas.company import LiveMarket
from app.services.platform.cache import Namespace, cache

log = structlog.get_logger(__name__)

_IST_OFFSET = 5 * 3600 + 30 * 60
_OPEN_SECONDS, _CLOSE_SECONDS = (9 * 3600 + 15 * 60), (15 * 3600 + 30 * 60)
_LIVE_QUOTE_KEY = "live-quote-v1"
_RETRY_SECONDS = 30.0
_QUEUE_CAPACITY = 512


def market_status(now_utc: datetime | None = None) -> str:
    """Return the current Indian cash-market state."""
    now = now_utc or datetime.now(timezone.utc)
    ist_epoch = int(now.timestamp()) + _IST_OFFSET
    local = datetime.fromtimestamp(ist_epoch, timezone.utc)
    if local.weekday() >= 5:
        return "weekend"
    seconds = local.hour * 3600 + local.minute * 60 + local.second
    return "open" if _OPEN_SECONDS <= seconds <= _CLOSE_SECONDS else "closed"


def _cache_key(company: Company) -> str:
    """Canonical listing key; exchange disambiguates all Nifty 500 symbols."""
    return resolve(company.ticker, exchange=company.exchange).canonical


class _QuoteRefresher:
    """One daemon worker: bounded memory, no request-thread provider calls."""

    def __init__(self) -> None:
        self._queue: queue.Queue[str] = queue.Queue(maxsize=_QUEUE_CAPACITY)
        self._lock = threading.Lock()
        self._pending: set[str] = set()
        self._retry_after: dict[str, float] = {}
        self._thread: threading.Thread | None = None

    def schedule(self, symbol: str) -> None:
        now = time.monotonic()
        with self._lock:
            if symbol in self._pending or now < self._retry_after.get(symbol, 0):
                return
            self._pending.add(symbol)
            try:
                self._queue.put_nowait(symbol)
            except queue.Full:
                self._pending.discard(symbol)
                log.warning("live quote refresh queue full", symbol=symbol)
                return
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._run, name="live-quote-refresh", daemon=True,
                )
                self._thread.start()

    def _run(self) -> None:
        provider = YahooProvider()
        while True:
            symbol = self._queue.get()
            try:
                snapshot, _ = provider.fetch(
                    symbol, include_news=False, include_history=False,
                    include_earnings=False,
                )
                quote = snapshot.quote
                if quote is None or quote.price is None or quote.price <= 0:
                    raise ValueError("Yahoo returned no usable price")
                market = LiveMarket(
                    live_price=quote.price,
                    current_price=None,
                    price_source=provider.name,
                    last_updated=datetime.now(timezone.utc).isoformat(),
                    market_status=market_status(),
                    change=quote.change,
                    change_percent=quote.percent_change,
                    volume=quote.volume,
                )
                cache.set(Namespace.MARKET_DATA, market, _LIVE_QUOTE_KEY, symbol)
                with self._lock:
                    self._retry_after.pop(symbol, None)
            except Exception as exc:  # noqa: BLE001 - one symbol must not stop the queue
                with self._lock:
                    self._retry_after[symbol] = time.monotonic() + _RETRY_SECONDS
                log.info("live quote refresh deferred", symbol=symbol,
                         error=str(exc)[:160])
            finally:
                with self._lock:
                    self._pending.discard(symbol)
                self._queue.task_done()


_REFRESHER = _QuoteRefresher()


class LiveMarketService:
    """Build cached-or-stored snapshots without blocking external I/O."""

    def __init__(self, db: Session | None) -> None:
        self.db = db

    @staticmethod
    def _fallback(company: Company | None) -> LiveMarket:
        stored = company.current_price if company else None
        return LiveMarket(
            live_price=stored,
            current_price=stored,
            price_source=SOURCE_INTERNAL,
            last_updated=None,
            market_status="closed",
        )

    def price_for(self, company: Company) -> float | None:
        return self.snapshot(company).live_price

    def snapshot(self, company: Company | None) -> LiveMarket:
        if company is None:
            return self._fallback(None)
        return self.bulk_quotes([company]).get(company.ticker, self._fallback(company))

    def bulk_quotes(self, companies: list[Company]) -> dict[str, LiveMarket]:
        """Return immediately from cache/stored data and queue stale misses.

        There is intentionally no call to ``MarketDataRouter.fetch`` here: for
        Indian listings that router may return the internal tier first, and an
        external provider call on a cache miss would block the HTTP request.
        """
        out: dict[str, LiveMarket] = {}
        for company in companies:
            if company is None:
                continue
            symbol = _cache_key(company)
            cached = cache.get(
                Namespace.MARKET_DATA, _LIVE_QUOTE_KEY, symbol,
            )
            market = cached if isinstance(cached, LiveMarket) else self._fallback(company)
            if cached is None:
                _REFRESHER.schedule(symbol)

            # The cached quote deliberately excludes the DB fallback value so
            # it can be shared across pages/processes. Add that page-specific
            # audit field without mutating the cached object.
            if market.current_price != company.current_price:
                market = market.model_copy(update={"current_price": company.current_price})

            try:
                from app.services.market_ops import MarketOpsService
                overridden = MarketOpsService(self.db).apply_override(company, market)
                if overridden is not None:
                    market = overridden
            except Exception:  # noqa: BLE001 - overrides cannot break a read page
                pass
            out[company.ticker] = market
        return out

    @staticmethod
    def attach(summary: Any, company: Company, db: Session) -> Any:
        market = LiveMarketService(db).snapshot(company)
        return summary.model_copy(update={"market": market})
