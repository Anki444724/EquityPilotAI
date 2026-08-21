"""Phase 1 benchmark: the full 5,000-company deterministic pipeline, measured.

Runs against SQLite by default (no infrastructure needed) and against
PostgreSQL/Redis when DATABASE_URL/REDIS_URL point at them, so the same
script produces the local number and the staging number.

    python3 tests/load/phase1_bench.py                 # 5,000 × mock, SQLite
    python3 tests/load/phase1_bench.py --companies 5000 --history-days 1825
    DATABASE_URL=postgresql+psycopg://... python3 tests/load/phase1_bench.py

Measures (requirement K): database size before/after, ingestion time for the
universe / financials / quotes / history, repeated-sync idempotency cost,
search and API latency (cold and cached), worker throughput, retry
throughput. Everything runs through the REAL services and the REAL job
queue — no shortcuts that would flatter the numbers.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

os.environ.setdefault("DATA_PROVIDER", "mock")
# The URL must be fixed BEFORE any app import: `app.db.base` binds SessionLocal
# to settings.DATABASE_URL at import time, and the request-metrics middleware
# writes through that session. Mutating settings later rebinds nothing, which
# is how the first run measured 500s from an empty stray ierp.db.
if not os.environ.get("DATABASE_URL"):
    os.environ["DATABASE_URL"] = "sqlite+pysqlite:///phase1_bench.db"


def _db_size(url: str, path: Path | None) -> int:
    if path is not None and path.exists():
        return path.stat().st_size
    return -1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--companies", type=int, default=5_000)
    parser.add_argument("--history-days", type=int, default=365)
    parser.add_argument("--financials", type=int, default=5_000,
                        help="companies to give mock financials (0 = skip)")
    parser.add_argument("--out", default=None, help="write JSON results here")
    args = parser.parse_args()

    from sqlalchemy import create_engine, func, select
    from sqlalchemy.orm import sessionmaker

    import app.models  # noqa: F401
    from app.core.config import settings
    from app.db.base import Base

    settings.DATA_PROVIDER = "mock"
    settings.MOCK_UNIVERSE_SIZE = args.companies

    from app.data.providers.router import reset_router
    reset_router()

    sqlite_path = None
    url = settings.DATABASE_URL
    if url.startswith("sqlite"):
        sqlite_path = Path("phase1_bench.db").resolve()
        if sqlite_path.exists():
            sqlite_path.unlink()

    engine = create_engine(url)
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine)
    db = factory()

    from fastapi.testclient import TestClient
    from app.main import app
    from app.db.base import get_db

    def _override():
        yield db

    app.dependency_overrides[get_db] = _override
    client = TestClient(app)

    results: dict[str, object] = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "engine": engine.dialect.name,
        "companies": args.companies,
        "history_days": args.history_days,
        "db_bytes_before": _db_size(url, sqlite_path),
    }

    def step(name: str):
        print(f"— {name}", flush=True)
        return time.perf_counter()

    # ---------------------------------------------------------------- universe
    from app.services.universe.company_universe import (
        CompanyUniverseService, generate_mock_universe,
    )
    records = generate_mock_universe(args.companies)
    svc = CompanyUniverseService(db)

    t0 = step("universe sync (first run)")
    first = svc.sync(records, source="mock", batch_size=500)
    results["universe_first_s"] = round(time.perf_counter() - t0, 2)
    results["universe_first"] = first.as_dict()

    t0 = step("universe sync (second run — idempotency cost)")
    second = svc.sync(records, source="mock", batch_size=500)
    results["universe_second_s"] = round(time.perf_counter() - t0, 2)
    results["universe_second"] = second.as_dict()

    # -------------------------------------------------------------- financials
    if args.financials:
        from app.services.platform.jobs.handlers import handler_for
        from app.domain.platform.jobs import JobKind
        t0 = step(f"mock financials sweep ({args.financials} companies)")
        fin_total = {}
        remaining = args.financials
        while remaining > 0:
            batch = min(500, remaining)
            out = handler_for(JobKind.FINANCIALS_BACKFILL)(db, {"limit": batch})
            remaining = out.get("universe_without_financials", 0)
            for k in ("attempted", "inserted", "updated", "unchanged"):
                fin_total[k] = fin_total.get(k, 0) + out.get(k, 0)
        results["financials_s"] = round(time.perf_counter() - t0, 2)
        results["financials"] = fin_total

        t0 = step("mock financials sweep (second pass — must be zero-work)")
        idle = handler_for(JobKind.FINANCIALS_BACKFILL)(db, {"limit": 500})
        results["financials_idle_s"] = round(time.perf_counter() - t0, 3)
        results["financials_idle"] = idle

    # ------------------------------------------------------------------ quotes
    from app.services.market.sync import PriceSyncService
    t0 = step(f"price sync ({args.companies} quotes, batches of 250)")
    done = 0
    while done < args.companies:
        out = PriceSyncService(db).sync_batch(limit=250)
        done += out["attempted"]
    results["price_sync_s"] = round(time.perf_counter() - t0, 2)

    t0 = step("price sync (second pass — refresh in place)")
    PriceSyncService(db).sync_batch(limit=250)
    results["price_sync_batch250_s"] = round(time.perf_counter() - t0, 2)

    # ---------------------------------------------------------------- history
    from app.services.market.sync import HistoricalPriceSyncService
    t0 = step(f"historical sync ({args.companies} × {args.history_days} bars)")
    done, bars = 0, 0
    while done < args.companies:
        out = HistoricalPriceSyncService(db).sync_batch(
            limit=100, days=args.history_days,
        )
        done += out["attempted"]
        bars += out["bars_written"]
    results["history_sync_s"] = round(time.perf_counter() - t0, 2)
    results["history_bars_written"] = bars

    from app.models.company import Company
    from app.models.company import FinancialFact
    from app.models.market import MarketQuote
    from app.models.portfolio import PriceHistory
    results["rows"] = {
        "companies": db.scalar(select(func.count()).select_from(Company)),
        "financial_facts": db.scalar(
            select(func.count()).select_from(FinancialFact)),
        "market_quotes": db.scalar(
            select(func.count()).select_from(MarketQuote)),
        "price_history": db.scalar(
            select(func.count()).select_from(PriceHistory)),
    }

    # ------------------------------------------------------------------ search
    from app.services.company_service import CompanyService
    search_svc = CompanyService(db)

    def _latency_ms(query: str, repeat: int = 1) -> float:
        t = time.perf_counter()
        for _ in range(repeat):
            search_svc.search(query, limit=20)
        return round((time.perf_counter() - t) / repeat * 1000, 2)

    step("search latency")
    results["search_cold_ms"] = _latency_ms("MCK04")          # likely cached warm
    # A never-repeated query measures the true miss path.
    miss_queries = [f"zz{i}" for i in range(20)]
    t = time.perf_counter()
    for q in miss_queries:
        search_svc.search(q, limit=20)
    results["search_miss_ms"] = round((time.perf_counter() - t) / 20 * 1000, 2)
    t = time.perf_counter()
    for _ in range(50):
        search_svc.search("MCK04", limit=20)
    results["search_cached_ms"] = round((time.perf_counter() - t) / 50 * 1000, 2)

    # --------------------------------------------------------------- API paths
    step("API latency (TestClient, same process)")
    sample = db.scalars(select(Company).limit(5)).all()
    for label, path in (
        ("api_company_detail_ms", "/api/v1/companies/{id}"),
        ("api_quote_ms", "/api/v1/companies/{id}/quote"),
        ("api_data_status_ms", "/api/v1/companies/{id}/data-status"),
        ("api_prices_ms", "/api/v1/companies/{id}/prices?range=1M"),
    ):
        t = time.perf_counter()
        n = 0
        for c in sample:
            r = client.get(path.format(id=c.id))
            if r.status_code == 200:
                n += 1
        results[label] = round((time.perf_counter() - t) / max(n, 1) * 1000, 2)
        results[label + "_ok"] = n
    t = time.perf_counter()
    for q in ("MCK04", "aurora", "Healthcare"):
        client.get("/api/v1/companies/search", params={"q": q})
    results["api_search_ms"] = round((time.perf_counter() - t) / 3 * 1000, 2)

    # ------------------------------------------------------------------ worker
    step("worker throughput (real queue, real Worker.run_once)")
    from app.domain.platform.jobs import JobKind
    from app.models.platform import BackgroundJob
    from app.services.platform.jobs.queue import JobQueue
    from app.services.platform.jobs.worker import Worker

    queue = JobQueue(db)
    # Distinct payloads: the queue deduplicates identical pending work (by
    # design), and 10 identical enqueues would collapse into one job.
    for i in range(10):
        queue.enqueue(JobKind.PRICE_SYNC, payload={"limit": 20 + i})
    db.commit()
    worker = Worker(factory, worker_id="bench-worker")
    t = time.perf_counter()
    processed = 0
    while worker.run_once():
        processed += 1
    results["worker_jobs_per_s"] = round(
        processed / max(time.perf_counter() - t, 1e-6), 2)
    results["worker_jobs_processed"] = processed

    step("retry throughput")
    from app.models.ingestion import IngestionFailure, IngestionRun
    from app.services.market.sync import FailedRetryService
    run = IngestionRun(kind="price_sync", provider="mock",
                       started_at=datetime.now(timezone.utc))
    db.add(run)
    db.commit()
    stale = datetime(2020, 1, 1, tzinfo=timezone.utc)
    for i in range(200):
        db.add(IngestionFailure(
            run_id=run.id, kind="price_sync", symbol=f"MCK{i:04d}",
            error="ProviderError: timeout", failure_kind="transient",
            last_attempt_at=stale,
        ))
    db.commit()
    t = time.perf_counter()
    retry_out = FailedRetryService(db).run(limit=200, max_attempts=5)
    results["retry_200_s"] = round(time.perf_counter() - t, 2)
    results["retry_resolved"] = retry_out["resolved"]

    # ------------------------------------------------------------------- close
    results["db_bytes_after"] = _db_size(url, sqlite_path)
    results["finished_at"] = datetime.now(timezone.utc).isoformat()

    print("\n=== RESULTS ===")
    print(json.dumps(results, indent=2, default=str))
    if args.out:
        Path(args.out).write_text(json.dumps(results, indent=2, default=str))
        print(f"\nwritten to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
