"""Single shared live-market service.

Every surface that shows a price — the company page, the dashboard, the
companies list/search, and the watchlist — renders from this one service, which
in turn is the single caller of :class:`MarketDataRouter`. That router owns the
TTL cache, so all surfaces for the same ticker within the cache window return
the *same* snapshot rather than each resolving its own, possibly-stale value.

The DB column ``Company.current_price`` is deliberately **not** the displayed
price here. It is carried as an explicit fallback (``current_price``) and used
only when the router could not produce a live figure, with ``price_source`` and
``market_status`` labelling the provenance so a fallback is never passed off as
a live quote.
"""
from __future__ import annotations

from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from sqlalchemy.orm import Session

from app.data.providers.router import (
    SOURCE_DOCUMENTS, SOURCE_INTERNAL, SOURCE_NONE, get_router,
)
from app.models.company import Company
from app.schemas.company import LiveMarket

#: NSE trading day in Asia/Kolkata. Market hours are 09:15–15:30 IST, Mon–Fri.
#: India is UTC+5:30 with no DST, applied as a fixed offset.
_IST_OFFSET = 5 * 3600 + 30 * 60
_OPEN_SECONDS, _CLOSE_SECONDS = (9 * 3600 + 15 * 60), (15 * 3600 + 30 * 60)

#: Tiers that answer without a live exchange feed. A price served from these is
#: explicitly *not* live and must be labelled as such.
_NON_LIVE_TIERS = {SOURCE_INTERNAL, SOURCE_DOCUMENTS, SOURCE_NONE}


def market_status(now_utc: datetime | None = None) -> str:
    """Is the Indian cash market open right now?

    NSE trades Monday–Friday, 09:15–15:30 IST. Deterministic and injectable so
    tests can assert the label without depending on wall-clock time.
    """
    now = (now_utc or datetime.now(timezone.utc))
    ist_epoch = int(now.timestamp()) + _IST_OFFSET
    local = datetime.fromtimestamp(ist_epoch, timezone.utc)
    if local.weekday() >= 5:
        return "weekend"
    seconds = local.hour * 3600 + local.minute * 60 + local.second
    return "open" if _OPEN_SECONDS <= seconds <= _CLOSE_SECONDS else "closed"


class LiveMarketService:
    """Resolves the market snapshot for one or many companies."""

    def __init__(self, db: Session) -> None:
        self.db = db
        self._router = get_router()

    # -- single ----------------------------------------------------------
    def price_for(self, company: Company) -> float | None:
        """The displayed current market price for one company.

        Live when a provider answers; the stored column is the explicit
        fallback only when no live figure is available (identical to what the
        company page shows). This is the single source a valuation or forecast
        consumes so its "current price" can never diverge from the market view.
        """
        return self.snapshot(company).live_price

    def snapshot(self, company: Company) -> LiveMarket:
        """The normalized market view for one company.

        Hits the shared market router (and therefore the shared TTL cache).
        Falls back to the stored ``current_price`` column only when the router
        could not produce a usable figure, and always labels the source.
        """
        stored = company.current_price if company else None
        fallback = LiveMarket(
            live_price=stored, current_price=stored,
            price_source=SOURCE_INTERNAL,
            market_status="closed",
        )
        if company is None:
            return fallback

        try:
            result = self._router.fetch(
                company.ticker,
                db=self.db,
                use_cache=True,
                include_news=False,
                include_history=False,
                include_earnings=False,
            )
        except Exception:  # noqa: BLE001 - market data must never fail the page
            return fallback

        if result is None or result.source == SOURCE_NONE:
            return fallback

        quote = result.snapshot.quote
        meta = result.snapshot.meta
        status = (
            "closed"
            if result.source in _NON_LIVE_TIERS or quote is None or not quote.price
            else market_status()
        )
        market = LiveMarket(
            live_price=quote.price if quote else stored,
            current_price=stored,
            price_source=result.source,
            last_updated=meta.last_updated if meta else None,
            market_status=status,
            change=quote.change if quote else None,
            change_percent=quote.percent_change if quote else None,
            volume=quote.volume if quote else None,
        )

        # Phase 4: a manual override (if active) takes precedence so every
        # surface — dashboard, company, portfolio, watchlist, AI — consumes the
        # exact same manual snapshot until it expires / auto-reverts.
        try:
            from app.services.market_ops import MarketOpsService
            overridden = MarketOpsService(self.db).apply_override(company, market)
            if overridden is not None:
                return overridden
        except Exception:  # noqa: BLE001 - an override must never break the page
            pass
        return market

    # -- batch -----------------------------------------------------------
    def attach_many(self, companies: list[Company]) -> dict[str, LiveMarket]:
        """Resolve market snapshots for a batch of companies.

        Uses the shared cached router while avoiding unnecessary heavy
        market payloads for list/search/dashboard surfaces.
        """
        result: dict[str, LiveMarket] = {}

        for company in companies:
            if not company:
                continue

            stored = company.current_price

            fallback = LiveMarket(
                live_price=stored,
                current_price=stored,
                price_source=SOURCE_INTERNAL,
                market_status="closed",
            )

            try:
                market_result = self._router.fetch(
                    company.ticker,
                    db=self.db,
                    use_cache=True,
                    include_news=False,
                    include_history=False,
                    include_earnings=False,
                )
            except Exception:
                result[company.ticker] = fallback
                continue

            if market_result is None or market_result.source == SOURCE_NONE:
                result[company.ticker] = fallback
                continue

            quote = market_result.snapshot.quote
            meta = market_result.snapshot.meta

            status = (
                "closed"
                if (
                    market_result.source in _NON_LIVE_TIERS
                    or quote is None
                    or not quote.price
                )
                else market_status()
            )

            market = LiveMarket(
                live_price=quote.price if quote else stored,
                current_price=stored,
                price_source=market_result.source,
                last_updated=meta.last_updated if meta else None,
                market_status=status,
                change=quote.change if quote else None,
                change_percent=quote.percent_change if quote else None,
                volume=quote.volume if quote else None,
            )

            try:
                from app.services.market_ops import MarketOpsService

                overridden = MarketOpsService(self.db).apply_override(
                    company, market
                )
                if overridden is not None:
                    market = overridden
            except Exception:
                pass

            result[company.ticker] = market

        return result

    @staticmethod
    def attach(summary: Any, company: Company, db: Session) -> Any:
        """Return ``summary`` with its ``market`` view populated in place."""
        service = LiveMarketService(db)
        market = service.snapshot(company)
        return summary.model_copy(update={"market": market})
