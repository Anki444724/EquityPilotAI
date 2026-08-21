"""Batched market-data sync services — Phase 1.

Three bounded, idempotent sweeps driven by the DB-backed job queue:

* `PriceSyncService` — refresh `market_quotes` + the Redis serving cache for
  the stalest batch of companies. The browser is never involved; external
  calls happen here, in the worker, behind the provider router.
* `HistoricalPriceSyncService` — backfill daily OHLCV bars into
  `price_history`, companies with no history first.
* `FailedRetryService` — re-drive `ingestion_failures` rows whose backoff has
  elapsed, using the same classification the financials backfill uses.

All three record an `IngestionRun` with counters, so "last successful sync"
and "why did X fail" are queries, not log archaeology.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.data.providers.base import ProviderError
from app.models.company import Company
from app.models.ingestion import IngestionFailure, IngestionRun
from app.models.market import MarketQuote
from app.models.portfolio import PriceHistory
from app.services.market.persistence import upsert_daily_bars, upsert_quote
from app.services.universe.financials_backfill import (
    FailureKind, classify_ingest_failure,
)

log = structlog.get_logger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; the queue's comparisons are aware."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class TransientSyncFailure(Exception):
    """Raised when a batch hit transient provider errors, so the job-level
    bounded retry runs — mirroring `TransientIngestionFailure`."""

    def __init__(self, transient: int, attempted: int) -> None:
        self.transient = transient
        self.attempted = attempted
        super().__init__(
            f"{transient} of {attempted} symbol(s) hit a transient provider "
            "error; scheduling a bounded retry"
        )


def _provider():
    """The head of the configured chain (mock in mock mode, real otherwise)."""
    from app.data.providers.router import primary_market_provider

    return primary_market_provider()


def _provider_name() -> str:
    from app.data.providers.router import active_provider_name

    return active_provider_name()


# ===========================================================================
# Quote sync
# ===========================================================================
class PriceSyncService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def stalest_companies(self, limit: int) -> list[Company]:
        """Active companies, never-synced first, then stalest first.

        A LEFT JOIN over one row per company (market_quotes is one-to-one),
        so the ordering is an index-friendly sort at any universe size.
        """
        stmt = (
            select(Company)
            .outerjoin(MarketQuote, MarketQuote.company_id == Company.id)
            .where(
                Company.deleted_at.is_(None),
                Company.listing_status == "active",
            )
            .order_by(
                MarketQuote.fetched_at.asc().nullsfirst(),
                Company.ticker.asc(),
            )
            .limit(limit)
        )
        return list(self.db.scalars(stmt).all())

    def sync_batch(
        self, *, limit: int, job_id: int | None = None,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        provider = _provider()
        provider_label = _provider_name()
        run = IngestionRun(
            kind="price_sync", provider=provider_label, started_at=_utcnow(),
            job_id=job_id, stats={"limit": limit},
        )
        self.db.add(run)
        self.db.commit()

        companies = self.stalest_companies(limit)
        succeeded = failed = 0
        transient_symbols: list[str] = []
        failures: list[tuple[str, str]] = []

        for company in companies:
            try:
                quote = provider.fetch_quote(company.ticker)
                if quote is None or quote.price is None:
                    # Narrow call unsupported: fall back to the full snapshot
                    # through the router (which also names its tier).
                    from app.data.providers.router import get_router

                    snapshot = get_router().fetch(
                        company.ticker, db=self.db, use_cache=False,
                        include_news=False, include_history=False,
                        include_earnings=False,
                    ).snapshot
                    quote = snapshot.quote
                if quote is None or quote.price is None:
                    raise ProviderError(f"no usable quote for {company.ticker}")
                upsert_quote(self.db, company, quote, provider=provider_label)
                self._refresh_serving_cache(company, quote, provider_label)
                succeeded += 1
            except Exception as exc:  # noqa: BLE001 — one symbol must not stop the batch
                self.db.rollback()
                failed += 1
                reason = f"{type(exc).__name__}: {exc}"
                failures.append((company.ticker, reason))
                if classify_ingest_failure(reason) == FailureKind.TRANSIENT:
                    transient_symbols.append(company.ticker)
                self._record_failure(run.id, "price_sync", company.ticker,
                                     company.id, reason)

        run = self.db.get(IngestionRun, run.id)
        duration = time.perf_counter() - started
        assert run is not None
        run.succeeded = succeeded
        run.failed = failed
        run.finished_at = _utcnow()
        run.stats = {
            "attempted": len(companies), "succeeded": succeeded,
            "failed": failed, "transient": len(transient_symbols),
            "duration_seconds": round(duration, 2),
        }
        self.db.commit()
        log.info("price sync batch", attempted=len(companies),
                 succeeded=succeeded, failed=failed,
                 duration_seconds=round(duration, 2))

        result = {
            "attempted": len(companies), "succeeded": succeeded,
            "failed": failed, "transient": len(transient_symbols),
            "failures": failures[:25], "provider": provider_label,
            "duration_seconds": round(duration, 2),
        }
        if transient_symbols:
            raise TransientSyncFailure(len(transient_symbols), len(companies))
        return result

    def _refresh_serving_cache(self, company: Company, quote, provider_label: str) -> None:
        """Write-through to the shared serving cache so user pages hit Redis,
        not the database, for the quote's TTL window."""
        try:
            from app.data.providers.symbols import resolve
            from app.schemas.company import LiveMarket
            from app.services.live_market import market_status
            from app.services.platform.cache import Namespace, cache

            symbol = resolve(company.ticker, exchange=company.exchange).canonical
            cache.set(
                Namespace.MARKET_DATA,
                LiveMarket(
                    live_price=quote.price,
                    current_price=None,
                    price_source=provider_label,
                    last_updated=_utcnow().isoformat(),
                    market_status=quote.market_status or market_status(),
                    change=quote.change,
                    change_percent=quote.percent_change,
                    volume=quote.volume,
                ),
                "live-quote-v1",
                symbol,
            )
        except Exception:  # noqa: BLE001 — cache write failures degrade, never raise
            log.debug("serving-cache write skipped", ticker=company.ticker)

    def _record_failure(self, run_id, kind, symbol, company_id, reason):
        try:
            self.db.add(IngestionFailure(
                run_id=run_id, kind=kind, symbol=symbol, company_id=company_id,
                error=reason[:2000],
                failure_kind=classify_ingest_failure(reason).value,
                last_attempt_at=_utcnow(),
            ))
            self.db.commit()
        except Exception:  # noqa: BLE001
            self.db.rollback()


# ===========================================================================
# Historical sync
# ===========================================================================
class HistoricalPriceSyncService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def next_companies(self, limit: int) -> list[Company]:
        """Companies with no bars first, then the thinnest histories.

        `price_history` is the biggest Phase-1 table, so the batch chooser
        must not scan it per company: one GROUP BY subquery drives the order.
        """
        bar_counts = (
            select(
                PriceHistory.ticker.label("ticker"),
                func.count().label("bars"),
            )
            .group_by(PriceHistory.ticker)
            .subquery()
        )
        stmt = (
            select(Company)
            .outerjoin(bar_counts, bar_counts.c.ticker == Company.ticker)
            .where(
                Company.deleted_at.is_(None),
                Company.listing_status == "active",
            )
            .order_by(
                func.coalesce(bar_counts.c.bars, 0).asc(),
                Company.ticker.asc(),
            )
            .limit(limit)
        )
        return list(self.db.scalars(stmt).all())

    def sync_batch(
        self, *, limit: int, days: int, job_id: int | None = None,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        provider = _provider()
        provider_label = _provider_name()
        run = IngestionRun(
            kind="historical_price_sync", provider=provider_label,
            started_at=_utcnow(), job_id=job_id, stats={"limit": limit, "days": days},
        )
        self.db.add(run)
        self.db.commit()

        companies = self.next_companies(limit)
        succeeded = failed = 0
        bars_written = 0
        transient: list[str] = []
        failures: list[tuple[str, str]] = []

        for company in companies:
            try:
                bars = provider.fetch_history(company.ticker, days=days)
                if bars is None:
                    from app.data.providers.router import get_router

                    snapshot = get_router().fetch(
                        company.ticker, db=self.db, use_cache=False,
                        include_news=False, include_history=True,
                        include_earnings=False,
                        history_days=days,
                    ).snapshot
                    bars = snapshot.price_history
                bars_written += upsert_daily_bars(
                    self.db, company.ticker, bars, provider=provider_label,
                )
                succeeded += 1
            except Exception as exc:  # noqa: BLE001
                self.db.rollback()
                failed += 1
                reason = f"{type(exc).__name__}: {exc}"
                failures.append((company.ticker, reason))
                if classify_ingest_failure(reason) == FailureKind.TRANSIENT:
                    transient.append(company.ticker)
                try:
                    self.db.add(IngestionFailure(
                        run_id=run.id, kind="historical_price_sync",
                        symbol=company.ticker, company_id=company.id,
                        error=reason[:2000],
                        failure_kind=classify_ingest_failure(reason).value,
                        last_attempt_at=_utcnow(),
                    ))
                    self.db.commit()
                except Exception:  # noqa: BLE001
                    self.db.rollback()

        duration = time.perf_counter() - started
        run = self.db.get(IngestionRun, run.id)
        assert run is not None
        run.succeeded = succeeded
        run.failed = failed
        run.finished_at = _utcnow()
        run.stats = {
            "attempted": len(companies), "succeeded": succeeded,
            "failed": failed, "bars_written": bars_written,
            "duration_seconds": round(duration, 2),
        }
        self.db.commit()
        log.info("historical price sync batch", attempted=len(companies),
                 bars=bars_written, failed=failed,
                 duration_seconds=round(duration, 2))

        result = {
            "attempted": len(companies), "succeeded": succeeded,
            "failed": failed, "bars_written": bars_written,
            "failures": failures[:25], "provider": provider_label,
            "duration_seconds": round(duration, 2),
        }
        if transient:
            raise TransientSyncFailure(len(transient), len(companies))
        return result


# ===========================================================================
# Failed-data retry
# ===========================================================================
#: Backoff between retry attempts of one failed symbol, keyed by attempt.
_RETRY_BACKOFF_SECONDS = {1: 60, 2: 300, 3: 900, 4: 3600}


class FailedRetryService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def due_failures(self, limit: int, max_attempts: int) -> list[IngestionFailure]:
        now = _utcnow()
        rows = self.db.scalars(
            select(IngestionFailure)
            .where(IngestionFailure.resolved_at.is_(None))
            .order_by(IngestionFailure.last_attempt_at.asc())
            .limit(limit * 4)
        ).all()
        due: list[IngestionFailure] = []
        for row in rows:
            if len(due) >= limit:
                break
            if row.attempts >= max_attempts:
                continue                      # left for an operator
            if row.failure_kind != FailureKind.TRANSIENT.value and row.attempts >= 1:
                continue                      # permanent: retrying burns quota
            backoff = _RETRY_BACKOFF_SECONDS.get(row.attempts, 6 * 3600)
            last = _aware(row.last_attempt_at) or now
            if now - last >= timedelta(seconds=backoff):
                due.append(row)
        return due

    def run(self, *, limit: int, max_attempts: int, job_id: int | None = None) -> dict[str, Any]:
        started = time.perf_counter()
        run = IngestionRun(
            kind="failed_data_retry", provider=_provider_name(),
            started_at=_utcnow(), job_id=job_id, stats={"limit": limit},
        )
        self.db.add(run)
        self.db.commit()

        due = self.due_failures(limit, max_attempts)
        resolved = retried_failed = skipped = 0

        for failure in due:
            try:
                ok = self._dispatch(failure)
                if ok:
                    failure.resolved_at = _utcnow()
                    resolved += 1
                else:
                    failure.attempts += 1
                    failure.last_attempt_at = _utcnow()
                    retried_failed += 1
            except Exception as exc:  # noqa: BLE001
                self.db.rollback()
                failure = self.db.get(IngestionFailure, failure.id)
                assert failure is not None
                failure.attempts += 1
                failure.last_attempt_at = _utcnow()
                failure.error = f"{type(exc).__name__}: {exc}"[:2000]
                retried_failed += 1
            self.db.commit()

        duration = time.perf_counter() - started
        run = self.db.get(IngestionRun, run.id)
        assert run is not None
        run.succeeded = resolved
        run.failed = retried_failed
        run.finished_at = _utcnow()
        run.stats = {
            "due": len(due), "resolved": resolved,
            "still_failing": retried_failed, "skipped": skipped,
            "duration_seconds": round(duration, 2),
        }
        self.db.commit()
        log.info("failed data retry", due=len(due), resolved=resolved,
                 still_failing=retried_failed)
        return {
            "due": len(due), "resolved": resolved,
            "still_failing": retried_failed, "skipped": skipped,
            "duration_seconds": round(duration, 2),
        }

    def _dispatch(self, failure: IngestionFailure) -> bool:
        """Re-run the work one failure represents. Returns success."""
        kind = failure.kind
        if kind in ("price_sync", "historical_price_sync"):
            company = failure.company_id and self.db.get(Company, failure.company_id)
            if company is None:
                company = self.db.scalar(
                    select(Company).where(Company.ticker == failure.symbol)
                )
            if company is None:
                return False  # the company itself is gone; nothing to retry
            provider = _provider()
            if kind == "price_sync":
                quote = provider.fetch_quote(company.ticker)
                if quote is None or quote.price is None:
                    return False
                upsert_quote(self.db, company, quote, provider=_provider_name())
                return True
            bars = provider.fetch_history(company.ticker, days=365)
            return upsert_daily_bars(
                self.db, company.ticker, bars, provider=_provider_name(),
            ) > 0

        if kind == "company_universe_sync" and failure.payload:
            from app.services.universe.company_universe import (
                CompanyUniverseService, UniverseRecord, UniverseSyncReport,
            )
            record = UniverseRecord(**failure.payload.get("record", {}))
            svc = CompanyUniverseService(self.db)
            report = UniverseSyncReport(source=record.source)
            svc._upsert_record(record, report)  # noqa: SLF001 — the retry IS a sync
            self.db.commit()
            return True

        if kind == "financials_sync":
            # Targeted financial retry rides the existing bounded job.
            from app.domain.platform.jobs import JobKind
            from app.services.platform.jobs.queue import JobQueue

            JobQueue(self.db).enqueue(
                JobKind.FINANCIALS_BACKFILL,
                payload={"tickers": [failure.symbol]},
                resource_type="company", resource_id=failure.symbol,
            )
            failure.resolved_at = _utcnow()
            return True

        if kind == "periodic_sync":
            # Quarterly/shareholding retry: re-run the same periodic service
            # for the one company, through the same throttled screener path.
            from app.services.universe.periodic_backfill import (
                PeriodicBackfillService,
            )

            company = (
                self.db.get(Company, failure.company_id)
                if failure.company_id else None
            ) or self.db.scalar(
                select(Company).where(Company.ticker == failure.symbol)
            )
            if company is None:
                return False
            report = PeriodicBackfillService(
                self.db, delay_seconds=0.0,
            ).run(companies=[company], progress=False)
            outcome = report.outcomes[0] if report.outcomes else None
            return bool(outcome and outcome.ok)

        return False
