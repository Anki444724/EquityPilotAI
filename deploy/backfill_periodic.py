#!/usr/bin/env python3
"""Backfill quarterly results and shareholding across the universe.

Idempotent: re-running updates existing periods in place rather than
duplicating them, so an interrupted sweep is restarted by running it again.

    export DATABASE_URL="postgresql+psycopg://..."
    python3 deploy/backfill_periodic.py --limit 20
    python3 deploy/backfill_periodic.py
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "backend"))

import importlib  # noqa: E402
import pkgutil  # noqa: E402

from sqlalchemy import create_engine, func, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import app.models as _models  # noqa: E402

for _module in pkgutil.iter_modules(_models.__path__):
    importlib.import_module(f"app.models.{_module.name}")

from app.models.analysis import QuarterlyResult, ShareholdingSnapshot  # noqa: E402
from app.services.universe.periodic_backfill import (  # noqa: E402
    PeriodicBackfillService,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--delay", type=float, default=0.3)
    parser.add_argument("--json", default=None)
    args = parser.parse_args()

    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2

    engine = create_engine(url, pool_pre_ping=True)
    db = sessionmaker(bind=engine)()

    def counts() -> tuple[int, int, int, int]:
        return (
            db.scalar(select(func.count()).select_from(QuarterlyResult)) or 0,
            db.scalar(select(func.count(func.distinct(
                QuarterlyResult.company_id)))) or 0,
            db.scalar(select(func.count()).select_from(ShareholdingSnapshot)) or 0,
            db.scalar(select(func.count(func.distinct(
                ShareholdingSnapshot.company_id)))) or 0,
        )

    before = counts()
    print(f"before: {before[0]} quarters / {before[1]} companies · "
          f"{before[2]} shareholding / {before[3]} companies", flush=True)

    service = PeriodicBackfillService(db, delay_seconds=args.delay)
    report = service.run(limit=args.limit, progress=True)
    after = counts()

    print(f"\nattempted {len(report.outcomes)}  ok {len(report.succeeded)}  "
          f"failed {len(report.failed)}")
    print(f"wrote {report.quarters} quarter rows, "
          f"{report.shareholding} shareholding rows")
    print(f"after: {after[0]} quarters / {after[1]} companies · "
          f"{after[2]} shareholding / {after[3]} companies")

    if report.failed:
        print("\nfailures:")
        for outcome in report.failed[:40]:
            print(f"  {outcome.ticker:<14} {outcome.reason}")

    if args.json:
        with open(args.json, "w") as handle:
            json.dump({
                "before": before, "after": after,
                "outcomes": [
                    {"ticker": o.ticker, "ok": o.ok,
                     "quarters": o.quarters_written,
                     "shareholding": o.shareholding_written,
                     "reason": o.reason}
                    for o in report.outcomes
                ],
            }, handle, indent=1)
        print(f"\nwrote {args.json}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
