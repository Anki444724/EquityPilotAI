"""Market data endpoints.

    GET /market/{ticker}            snapshot, with the source that served it
    GET /market/{ticker}/raw        parsed *and* raw provider payloads
    GET /market/providers           configuration and circuit state

Every response names the provider that answered. That is not decoration: a
quote from the primary and a quote from the fallback can differ, and a reader
comparing two figures needs to know whether they came from the same place.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.core.security import CurrentUser, get_current_user
from app.data import finnhub_source as finnhub
from app.data.market_data import (
    SOURCE_FINNHUB, SOURCE_NONE, SOURCE_YAHOO, fetch_market_data,
)

router = APIRouter(tags=["market"])


@router.get("/market/providers", summary="Market-data provider status")
def providers(user: CurrentUser = Depends(get_current_user)) -> dict[str, Any]:
    """Which providers are configured, and whether the primary is healthy.

    Reports only whether a key is *present* — never the key, nor any prefix of
    it. A truncated secret in a status endpoint is still a secret in a status
    endpoint.
    """
    from app.core.config import settings

    return {
        "primary": SOURCE_FINNHUB,
        "fallback": SOURCE_YAHOO,
        "providers": [
            {
                "name": SOURCE_FINNHUB,
                "configured": bool(settings.FINNHUB_API_KEY),
                "available": finnhub.provider_available(),
                "min_interval_seconds": finnhub.MIN_INTERVAL,
                "endpoints": [
                    "company profile", "quote", "basic financials",
                    "company news", "earnings calendar",
                ],
            },
            {
                "name": SOURCE_YAHOO,
                "configured": True,       # no key required
                "available": True,
                "endpoints": ["quote"],
            },
        ],
    }


@router.get("/market/{ticker}", summary="Market snapshot for one ticker")
def market_snapshot(
    ticker: str,
    news: bool = Query(default=True),
    earnings: bool = Query(default=True),
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    result = fetch_market_data(
        ticker, include_news=news, include_earnings=earnings,
    )
    if result.source == SOURCE_NONE:
        # Every provider failed. 502 rather than an empty 200: a caller must
        # not mistake "nobody answered" for "no data exists".
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"No market-data provider could serve {ticker}: {result.reason}",
        )
    return result.as_dict()


@router.get("/market/{ticker}/raw", summary="Snapshot plus raw provider payloads")
def market_snapshot_raw(
    ticker: str,
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Both views, so a parsed figure can be audited against what arrived."""
    result = fetch_market_data(ticker)
    if result.source == SOURCE_NONE:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"No market-data provider could serve {ticker}: {result.reason}",
        )
    return result.as_dict(include_raw=True)
