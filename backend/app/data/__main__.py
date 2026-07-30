"""One command to build the real dataset from nothing.

    python -m app.data                 # ingest + derive + validate
    python -m app.data --no-validate   # just the data
    python -m app.data --limit 20      # a quick subset

Idempotent: re-running replaces each company's facts rather than appending, so
a partial run can simply be repeated.
"""
from __future__ import annotations

import argparse
import sys
import time


def main() -> int:
    parser = argparse.ArgumentParser(description="Ingest real NSE company data")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--with-yahoo", action="store_true",
                        help="attempt Yahoo enrichment (often rate limited)")
    parser.add_argument("--no-validate", action="store_true")
    args = parser.parse_args()

    sys.path.insert(0, ".")
    from app.db.base import Base, SessionLocal, engine
    from app.main import app  # noqa: F401  — registers every model

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    started = time.time()

    from app.data.derive_wc import derive_universe
    from app.data.ingest import ingest_universe

    print("=== INGEST")
    ingested = ingest_universe(db, limit=args.limit, with_yahoo=args.with_yahoo)
    ok = [r for r in ingested if r.ok]
    print(f"  {len(ok)}/{len(ingested)} companies · "
          f"{sum(r.fact_count for r in ok):,} facts")
    for failure in (r for r in ingested if not r.ok):
        print(f"  FAILED {failure.ticker}: {failure.error}")

    if args.with_yahoo:
        from app.data.enrich import enrich_universe

        print("\n=== ENRICH (Yahoo)")
        enriched = enrich_universe(db, progress=False)
        good = [r for r in enriched if r.ok]
        print(f"  {len(good)}/{len(enriched)} · +{sum(r.added for r in good):,} facts")

    print("\n=== DERIVE working capital from reported ratios")
    derived = derive_universe(db, progress=False)
    good = [r for r in derived if r.ok]
    print(f"  {len(good)}/{len(derived)} · +{sum(r.added for r in good):,} facts")
    if good:
        print(f"  coverage {sum(r.coverage_after for r in good) / len(good):.1%} "
              "of the 54 canonical items, latest year")

    if not args.no_validate:
        from sqlalchemy import select

        from app.data.nse_universe import NSE_UNIVERSE
        from app.data.validate import Validator
        from app.models.company import Company

        print("\n=== VALIDATE against reported figures")
        have = {c.ticker for c in db.scalars(select(Company))}
        validator = Validator(db)
        total = passed = 0
        failures: list[str] = []
        for ticker, _name, sector, _industry in NSE_UNIVERSE:
            if ticker not in have:
                continue
            result = validator.validate(ticker, sector)
            total += len(result.checks)
            passed += result.passed
            failures.extend(
                f"{ticker}: {c.name}" for c in result.checks if not c.passed
            )
        print(f"  {passed}/{total} checks passed ({passed / max(total, 1):.2%})")
        for failure in failures[:20]:
            print(f"  FAILED {failure}")

    print(f"\ndone in {time.time() - started:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
