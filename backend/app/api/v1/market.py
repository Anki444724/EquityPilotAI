"""Market data endpoints.

    GET /market/providers            tier configuration and circuit state
    GET /market/cache                cache statistics
    GET /market/{ticker}             snapshot, naming the tier that served it
    GET /market/{ticker}/raw         parsed *and* raw provider payloads

Every response names its source. That is not decoration: a live FMP quote and
a figure from the platform's own database can be weeks apart, and a reader
comparing two numbers must be able to tell which is which.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.security import CurrentUser, get_current_user
from app.db.base import get_db
from app.data.providers.router import (
    SOURCE_DOCUMENTS, SOURCE_INTERNAL, SOURCE_NONE, cache, get_router,
)

router = APIRouter(tags=["market"])


@router.get("/market/providers", summary="Market-data provider status")
def providers(user: CurrentUser = Depends(get_current_user)) -> dict[str, Any]:
    """Which tiers are configured and healthy.

    Reports only whether a key is *present* — never the key, nor a prefix of
    it. A truncated secret in a status endpoint is still a secret.
    """
    engine = get_router()
    tiers: list[dict[str, Any]] = [
        {
            "priority": index + 1,
            "name": provider.name,
            "configured": provider.configured(),
            "available": provider.available,
            "timeout_seconds": provider.policy.timeout_seconds,
            "retry_attempts": provider.policy.attempts,
        }
        for index, provider in enumerate(engine.providers)
    ]
    tiers.append({"priority": len(tiers) + 1, "name": SOURCE_INTERNAL,
                  "configured": True, "available": True})
    tiers.append({"priority": len(tiers) + 1, "name": SOURCE_DOCUMENTS,
                  "configured": True, "available": True})
    return {"tiers": tiers, "cache": cache().stats()}


@router.get("/market/cache", summary="Market cache statistics")
def cache_stats(user: CurrentUser = Depends(get_current_user)) -> dict[str, Any]:
    return cache().stats()


@router.get("/market/{ticker}", summary="Market snapshot for one ticker")
def market_snapshot(
    ticker: str,
    news: bool = Query(default=True),
    history: bool = Query(default=True),
    earnings: bool = Query(default=True),
    refresh: bool = Query(default=False, description="Bypass the cache"),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    result = get_router().fetch(
        ticker, db=db, use_cache=not refresh, include_news=news,
        include_history=history, include_earnings=earnings,
    )
    if result.source == SOURCE_NONE:
        # 502 rather than an empty 200: a caller must not mistake "no tier
        # answered" for "this company has no data".
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            {
                "message": f"No provider could serve {ticker}.",
                "providers_attempted": result.attempted,
            },
        )
    return result.as_dict()


@router.get("/market/{ticker}/raw", summary="Snapshot plus raw provider payloads")
def market_snapshot_raw(
    ticker: str,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Both views, so a parsed figure can be audited against what arrived."""
    result = get_router().fetch(ticker, db=db, use_cache=False)
    if result.source == SOURCE_NONE:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            {
                "message": f"No provider could serve {ticker}.",
                "providers_attempted": result.attempted,
            },
        )
    return result.as_dict(include_raw=True)
