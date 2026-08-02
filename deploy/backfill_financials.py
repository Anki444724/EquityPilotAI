#!/usr/bin/env python3
"""Run the universe financials backfill against a live database.

Resumable by construction: the target set is computed from the database each
time it runs, so a company that succeeded is simply no longer selected. An
interrupted sweep is restarted by running the command again — there is no
cursor to corrupt and no partial state to clean up.

    export DATABASE_URL="postgresql+psycopg://..."
    python3 deploy/backfill_financials.py --limit 50
    python3 deploy/backfill_financials.py            # everything remaining
    python3 deploy/backfill_financials.py --report-only
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

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import app.models as _models  # noqa: E402

for _module in pkgutil.iter_modules(_models.__path__):
    importlib.import_module(f"app.models.{_module.name}")

from app.services.universe.financials_backfill import (  # noqa: E402
    FinancialsBackfillService,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--delay", type=float, default=0.4)
    parser.add_argument("--with-yahoo", action="store_true",
                        help="Enrich detail lines. Slow while Yahoo throttles.")
    parser.add_argument("--report-only", action="store_true")
    parser.add_argument("--json", default=None)
    args = parser.parse_args()

    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2

    engine = create_engine(url, pool_pre_ping=True)
    db = sessionmaker(bind=engine)()
    service = FinancialsBackfillService(
        db, delay_seconds=args.delay, with_yahoo=args.with_yahoo,
    )

    before = service.coverage_snapshot()
    print(f"coverage before: {before['with_financials']}/{before['companies']}"
          f" ({before['coverage_pct']}%)", flush=True)

    if args.report_only:
        print(json.dumps(before, indent=2))
        return 0

    report = service.run(limit=args.limit, progress=True)
    after = service.coverage_snapshot()

    print(f"\nattempted {len(report.outcomes)}  "
          f"ok {len(report.succeeded)}  failed {len(report.failed)}")
    print(f"coverage after: {after['with_financials']}/{after['companies']}"
          f" ({after['coverage_pct']}%)")
    if report.failed:
        print("\nfailure reasons:")
        for reason, count in report.reasons().items():
            print(f"  {count:>4}  {reason}")

    if args.json:
        with open(args.json, "w") as handle:
            json.dump({
                "before": before,
                "after": after,
                "outcomes": [
                    {
                        "ticker": o.ticker, "name": o.name,
                        "category": o.market_cap_category, "ok": o.ok,
                        "years": o.fiscal_years, "facts": o.fact_count,
                        "coverage": o.coverage, "reason": o.reason,
                        "seconds": o.seconds,
                    }
                    for o in report.outcomes
                ],
            }, handle, indent=1)
        print(f"\nwrote {args.json}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
