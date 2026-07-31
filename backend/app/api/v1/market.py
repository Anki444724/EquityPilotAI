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

import time
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


@router.get("/providers/health", summary="Provider health")
def providers_health(
    probe: bool = Query(default=False, description="Make one live call per provider"),
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Reachability, authentication, rate limit, latency, last success.

    `probe=false` reports what has been observed so far and costs nothing.
    `probe=true` makes one real call per provider, which is the only way to
    distinguish "configured" from "actually working" — but it spends quota,
    so it is opt-in rather than the default.
    """
    from app.data.providers.base import (
        ProviderAuthError, ProviderError, ProviderNotConfigured,
    )

    engine = get_router()
    report: list[dict[str, Any]] = []
    for provider in engine.providers:
        entry = provider.health()
        entry["reachable"] = None
        entry["authenticated"] = None
        if probe and provider.configured():
            started = time.perf_counter()
            try:
                provider.fetch("AAPL", include_news=False,
                               include_history=False, include_earnings=False)
                entry.update(reachable=True, authenticated=True)
            except ProviderAuthError:
                entry.update(reachable=True, authenticated=False)
            except (ProviderNotConfigured,):
                entry.update(reachable=None, authenticated=False)
            except ProviderError as exc:
                # Reached it and got a considered answer; the failure is
                # about coverage or the plan, not connectivity.
                entry.update(reachable=True, authenticated=True,
                             probe_note=str(exc)[:120])
            except Exception as exc:  # noqa: BLE001
                entry.update(reachable=False, authenticated=None,
                             probe_note=f"{type(exc).__name__}: {exc}"[:120])
            entry["probe_ms"] = round((time.perf_counter() - started) * 1000, 1)
        report.append(entry)

    for name in (SOURCE_INTERNAL, SOURCE_DOCUMENTS):
        report.append({"provider": name, "configured": True,
                       "circuit_open": False, "reachable": True,
                       "authenticated": True})

    healthy = sum(1 for r in report if r.get("configured")
                  and not r.get("circuit_open"))
    return {
        "status": "ok" if healthy else "degraded",
        "providers": report,
        "cache": cache().stats(),
    }


@router.get("/filings/{ticker}", summary="Official filings for one company")
def filings(
    ticker: str,
    filing_type: str | None = Query(default=None),
    limit: int = Query(default=10, ge=1, le=50),
    all_sources: bool = Query(default=False),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Walk the filing chain and report what answered.

    India: uploaded reports -> NSE -> BSE. US: SEC EDGAR -> uploaded reports.
    Every filing carries its source category, a regulator reference and a
    confidence score that ranks official documents above third-party APIs.
    """
    from app.data.filings.base import FilingType
    from app.data.filings.router import FilingRouter

    wanted = None
    if filing_type:
        try:
            wanted = [FilingType(filing_type)]
        except ValueError:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"unknown filing_type; expected one of "
                f"{[t.value for t in FilingType]}",
            )

    return FilingRouter(db).fetch(
        ticker, filing_types=wanted, limit=limit, all_sources=all_sources,
    ).as_dict()


@router.get("/pipelines", summary="Report pipelines by market")
def pipelines(user: CurrentUser = Depends(get_current_user)) -> dict[str, Any]:
    """The declared evidence stack for each market, highest precedence first."""
    from app.services.ai.pipelines import describe_pipelines

    return describe_pipelines()


@router.get("/pipelines/{ticker}", summary="Pipeline governing one ticker")
def pipeline_for_ticker(
    ticker: str, user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    from app.services.ai.pipelines import pipeline_for

    return pipeline_for(ticker).as_dict()


@router.get("/market/cache", summary="Market cache statistics")
def cache_stats(user: CurrentUser = Depends(get_current_user)) -> dict[str, Any]:
    """Market-tier cache only. See `/platform/cache` for all four namespaces."""
    return cache().stats()


@router.get("/platform/cache", summary="Unified cache statistics")
def platform_cache_stats(
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Hit rates for market data, statements, news and RAG retrieval.

    The operational question this answers is "is the cache actually working",
    which was previously unanswerable: market data reported its own hit rate
    and the other three read paths reported nothing at all.
    """
    from app.services.platform.cache import cache as unified

    payload = unified.snapshot()
    payload["market_tier"] = cache().stats()
    return payload


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
