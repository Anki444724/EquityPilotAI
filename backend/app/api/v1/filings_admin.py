"""Filing collection dashboard and controls.

Read endpoints are available to any authenticated user because the collection
state is useful context — "when was this company last checked?" is a
reasonable question for an analyst. Anything that *causes* work (running a
crawl, retrying, changing a tier) requires an operator role, because each of
those spends the platform's rate-limit budget against a third party.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from app.core.security import CurrentUser, get_current_user
from app.db.base import get_db
from app.domain.filings.collection import (
    CollectionStatus, CollectionTier, should_retry,
)
from app.models.company import Company
from app.models.filing_collection import CompanyCrawlState, DiscoveredFiling

router = APIRouter(tags=["filings"])

#: Companies a single synchronous crawl may attempt.
#:
#: Railway's HTTP edge closes a request at 5 minutes. At ~20 s per company
#: against a rate-limited exchange, anything above this returns a 502 to the
#: caller while the server keeps working — the worst combination, because the
#: operator cannot tell what happened.
MAX_SYNCHRONOUS_COMPANIES = 8


def _require_operator(user: CurrentUser) -> None:
    """Only an operator may spend the crawl budget."""
    role = str(getattr(user, "role", "") or "").lower()
    if role not in ("admin", "super_admin", "tenant_admin", "operator"):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "operator role required to trigger collection",
        )


# ---------------------------------------------------------------- dashboard
@router.get("/filings/dashboard", summary="Filing collection dashboard")
def dashboard(
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Counts by pipeline stage, storage used, and freshness."""
    from app.services.filings.collector import FilingCollector

    payload = FilingCollector(db).dashboard()

    recent = db.execute(
        select(DiscoveredFiling, Company.ticker)
        .join(Company, Company.id == DiscoveredFiling.company_id)
        .order_by(desc(DiscoveredFiling.discovered_at))
        .limit(20)
    ).all()
    payload["recent"] = [
        {
            "id": row.id, "ticker": ticker, "title": row.title,
            "source": row.source, "status": row.status,
            "doc_type": row.doc_type, "fiscal_year": row.fiscal_year,
            "quarter": row.quarter, "file_size": row.file_size,
            "sha256": row.content_sha256,
            "document_id": row.document_id,
            "discovered_at": (
                row.discovered_at.isoformat() if row.discovered_at else None
            ),
            "error": row.error,
        }
        for row, ticker in recent
    ]

    failed = db.scalar(
        select(func.count()).select_from(DiscoveredFiling)
        .where(DiscoveredFiling.status == CollectionStatus.FAILED.value)
    ) or 0
    payload["retryable"] = db.scalar(
        select(func.count()).select_from(DiscoveredFiling).where(
            DiscoveredFiling.status == CollectionStatus.FAILED.value,
            DiscoveredFiling.attempts < 3,
        )
    ) or 0
    payload["failed"] = failed
    return payload


@router.get("/filings", summary="Discovered filings")
def list_filings(
    ticker: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, le=500),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    query = (
        select(DiscoveredFiling, Company.ticker)
        .join(Company, Company.id == DiscoveredFiling.company_id)
    )
    if ticker:
        query = query.where(Company.ticker == ticker.upper())
    if status_filter:
        query = query.where(DiscoveredFiling.status == status_filter)
    rows = db.execute(
        query.order_by(desc(DiscoveredFiling.discovered_at)).limit(limit)
    ).all()

    return {
        "count": len(rows),
        "results": [
            {
                "id": row.id, "ticker": tick, "title": row.title,
                "source": row.source, "source_url": row.source_url,
                "status": row.status, "doc_type": row.doc_type,
                "filing_type": row.filing_type,
                "classification_confidence": row.classification_confidence,
                "fiscal_year": row.fiscal_year, "quarter": row.quarter,
                "language": row.language,
                "file_size": row.file_size, "sha256": row.content_sha256,
                "document_id": row.document_id, "attempts": row.attempts,
                "error": row.error,
                "discovered_at": (
                    row.discovered_at.isoformat() if row.discovered_at else None
                ),
                "downloaded_at": (
                    row.downloaded_at.isoformat() if row.downloaded_at else None
                ),
            }
            for row, tick in rows
        ],
    }


@router.get("/filings/companies", summary="Per-company crawl state")
def crawl_states(
    limit: int = Query(default=200, le=1000),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    rows = db.execute(
        select(CompanyCrawlState, Company.ticker, Company.name)
        .join(Company, Company.id == CompanyCrawlState.company_id)
        .order_by(Company.ticker)
        .limit(limit)
    ).all()
    return {
        "count": len(rows),
        "results": [
            {
                "ticker": ticker, "name": name, "tier": state.tier,
                "enabled": state.enabled, "ir_url": state.ir_url,
                "bse_scrip_code": state.bse_scrip_code,
                "last_crawled_at": (
                    state.last_crawled_at.isoformat()
                    if state.last_crawled_at else None
                ),
                "last_status": state.last_status,
                "last_error": state.last_error,
                "consecutive_failures": state.consecutive_failures,
                "documents_found": state.documents_found,
                "documents_ingested": state.documents_ingested,
            }
            for state, ticker, name in rows
        ],
    }


# ----------------------------------------------------------------- controls
@router.post("/filings/enable-universe",
             summary="Register every active Indian company for collection")
def enable_universe(
    tier: str = Query(default="weekly"),
    index: str | None = Query(default="NIFTY500"),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Create crawl state for the whole universe in one pass.

    Without this, a company is only registered the first time it happens to be
    crawled, so `due_companies` cannot see it and the scheduler never reaches
    it — a chicken-and-egg that leaves 368 freshly imported companies
    invisible to collection.

    Also backfills `bse_scrip_code` from the company record, which the
    Nifty 500 import populated for 498 of 500.
    """
    _require_operator(user)

    try:
        wanted_tier = CollectionTier(tier).value
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"tier must be one of {[t.value for t in CollectionTier]}",
        ) from exc

    query = select(Company).where(
        Company.exchange.in_(("NSE", "BSE", "NSE/BSE")),
        Company.listing_status == "active",
    )
    if index:
        query = query.where(Company.index_membership == index)
    companies = db.execute(query).scalars().all()

    created = updated = unchanged = 0
    for company in companies:
        state = db.scalar(
            select(CompanyCrawlState).where(
                CompanyCrawlState.company_id == company.id
            )
        )
        if state is None:
            db.add(CompanyCrawlState(
                company_id=company.id, tier=wanted_tier, enabled=True,
                bse_scrip_code=company.bse_code,
            ))
            created += 1
            continue
        changed = False
        if not state.bse_scrip_code and company.bse_code:
            state.bse_scrip_code = company.bse_code
            changed = True
        if not state.enabled:
            state.enabled = True
            state.consecutive_failures = 0
            changed = True
        updated += 1 if changed else 0
        unchanged += 0 if changed else 1

    db.commit()
    return {
        "companies": len(companies), "created": created,
        "updated": updated, "unchanged": unchanged, "tier": wanted_tier,
    }


@router.post("/filings/crawl", summary="Run a collection pass now")
def run_crawl(
    tickers: str | None = Query(
        default=None, description="Comma-separated; omit to crawl due companies",
    ),
    max_companies: int = Query(default=5, le=50),
    max_downloads: int = Query(default=5, le=25),
    download: bool = Query(default=True),
    delay: float = Query(
        default=1.5, ge=0.0, le=15.0,
        description="Seconds between companies; NSE rate-limits bursts",
    ),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Run synchronously.

    The scheduler runs this nightly as a background job; this endpoint exists
    so an operator can force a pass and see the result, which is also how the
    system is verified. Bounded hard by the query limits, because a crawl is
    slow and this call holds a request open.
    """
    _require_operator(user)
    from app.services.filings.collector import FilingCollector

    # GATEWAY-001. Railway's HTTP edge closes a request at 5 minutes with a
    # 502, and a crawl runs at roughly 20 s per company against a rate-limited
    # exchange — so 20 companies exceeded the deadline and the client saw a
    # bare 502 while the server carried on working. Batches are capped here so
    # the request cannot outlive the gateway; the nightly scheduler is the
    # right vehicle for the full universe.
    if max_companies > MAX_SYNCHRONOUS_COMPANIES:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"max_companies must be <= {MAX_SYNCHRONOUS_COMPANIES} for a "
            f"synchronous crawl; Railway closes the connection at 5 minutes. "
            f"Use the scheduler for the full universe.",
        )

    collector = FilingCollector(db, polite_delay=delay)
    if tickers:
        wanted = [t.strip().upper() for t in tickers.split(",") if t.strip()]
        results = []
        for symbol in wanted:
            company = db.scalar(select(Company).where(Company.ticker == symbol))
            if company is None:
                results.append({"ticker": symbol, "error": "unknown ticker"})
                continue
            results.append(collector.crawl_company(
                company, download=download, max_downloads=max_downloads,
            ).as_dict())
        return {"companies": len(results), "results": results}

    return collector.crawl_due(
        max_companies=max_companies, download=download,
        max_downloads_per_company=max_downloads,
    )


@router.post("/filings/drain", summary="Download already-discovered filings")
def drain_discovered(
    limit: int = Query(default=25, le=200),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Download filings that discovery already found, without re-listing.

    Discovery and download hit different NSE hosts: listing goes through the
    rate-limited `/api/corporate-announcements` endpoint, while the PDFs
    themselves come from the archive CDN, which does not throttle in the same
    way. Once a filing is known, fetching it therefore costs nothing against
    the discovery budget.

    That distinction is what makes the universe tractable: a throttled crawl
    still records what exists, and this drains the backlog afterwards at full
    speed.
    """
    _require_operator(user)
    from app.domain.filings.collection import CollectionStatus
    from app.services.filings.collector import FilingCollector

    rows = db.execute(
        select(DiscoveredFiling)
        .where(
            DiscoveredFiling.status == CollectionStatus.DISCOVERED.value,
            DiscoveredFiling.source_url.is_not(None),
        )
        .order_by(DiscoveredFiling.id)
        .limit(limit)
    ).scalars().all()

    collector = FilingCollector(db)
    counts: dict[str, int] = {}
    for row in rows:
        status = collector.collect_one(row)
        counts[status] = counts.get(status, 0) + 1
        db.flush()
    db.commit()

    return {"attempted": len(rows), "by_status": counts}


@router.post("/filings/{filing_id}/retry", summary="Retry a failed filing")
def retry_filing(
    filing_id: int,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    _require_operator(user)
    from app.services.filings.collector import FilingCollector

    row = db.get(DiscoveredFiling, filing_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown filing")

    try:
        current = CollectionStatus(row.status)
    except ValueError:
        current = CollectionStatus.FAILED
    if not should_retry(row.attempts or 0, current):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"filing is '{row.status}' after {row.attempts} attempt(s); "
            f"not retryable",
        )

    status_after = FilingCollector(db).collect_one(row)
    db.commit()
    return {"id": row.id, "status": status_after, "error": row.error}


@router.patch("/filings/companies/{ticker}", summary="Update crawl settings")
def update_crawl_state(
    ticker: str,
    tier: str | None = Query(default=None),
    enabled: bool | None = Query(default=None),
    ir_url: str | None = Query(default=None),
    bse_scrip_code: str | None = Query(default=None),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Register an IR URL, change a tier, or re-enable a paused company."""
    _require_operator(user)

    company = db.scalar(select(Company).where(Company.ticker == ticker.upper()))
    if company is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown ticker {ticker}")

    state = db.scalar(
        select(CompanyCrawlState).where(
            CompanyCrawlState.company_id == company.id
        )
    )
    if state is None:
        state = CompanyCrawlState(company_id=company.id)
        db.add(state)

    if tier is not None:
        try:
            state.tier = CollectionTier(tier).value
        except ValueError as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"tier must be one of {[t.value for t in CollectionTier]}",
            ) from exc
    if enabled is not None:
        state.enabled = enabled
        if enabled:
            # Re-enabling clears the failure count, otherwise a company
            # paused last month is immediately re-paused on its next failure.
            state.consecutive_failures = 0
    if ir_url is not None:
        state.ir_url = ir_url or None
    if bse_scrip_code is not None:
        state.bse_scrip_code = bse_scrip_code or None

    db.commit()
    return {
        "ticker": company.ticker, "tier": state.tier, "enabled": state.enabled,
        "ir_url": state.ir_url, "bse_scrip_code": state.bse_scrip_code,
        "consecutive_failures": state.consecutive_failures,
    }


@router.get("/scheduler/dashboard", summary="Scheduler coverage and health")
def scheduler_dashboard(
    window_hours: int = Query(default=24, ge=1, le=168),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Coverage, retries, failures, IR discovery, downloads and memory.

    Registered under `/scheduler/` rather than `/filings/` deliberately.
    ROUTE-001: `/filings/{ticker}` in the market router is declared before
    the static filing paths and captured `/filings/dashboard` as a ticker,
    answering 200 with an empty result — worse than a 404, because it looks
    like data. A distinct prefix cannot be shadowed by that route.

    Every figure is recomputed from the tables. A counter maintained by the
    crawler would drift the first time a pass is interrupted, and this crawler
    has been interrupted.
    """
    from app.services.filings.dashboard import SchedulerDashboard

    return SchedulerDashboard(db).snapshot(window_hours=window_hours)


@router.post("/scheduler/discover-ir", summary="Discover investor-relations URLs")
def discover_ir_urls(
    limit: int = Query(default=25, ge=1, le=500),
    overwrite: bool = Query(default=False),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Run IR discovery now. The scheduler also runs this daily."""
    _require_operator(user)
    from app.services.filings.ir_discovery import IRDiscoveryService

    return IRDiscoveryService(db).run(limit=limit, overwrite=overwrite).as_dict()
