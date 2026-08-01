"""Provision the seeded US universe.

Run as `python -m app.services.us_pipeline.seed`, or from the deployment
entrypoint. Idempotent: a company already present is left alone unless
`--refresh` is given.

Kept out of the application's startup path deliberately. Provisioning makes
three provider calls per company, so seeding five companies inside a container
start would add several seconds to every deploy and make a boot failure
dependent on a third party's availability. On-demand provisioning already
covers a cold cache; this is only to make the common demonstrations instant.
"""
from __future__ import annotations

import argparse
import sys

import structlog

from app.db.base import SessionLocal
from app.services.us_pipeline.provisioning import (
    SEED_UNIVERSE, ProvisioningError, USCompanyProvisioner,
)

log = structlog.get_logger(__name__)


def seed_us_universe(
    tickers: tuple[str, ...] = SEED_UNIVERSE, *, refresh: bool = False,
) -> dict[str, object]:
    """Provision each ticker, reporting per-company outcomes."""
    provisioned: list[dict] = []
    failed: list[dict] = []

    with SessionLocal() as db:
        provisioner = USCompanyProvisioner(db)
        for ticker in tickers:
            try:
                result = provisioner.provision(ticker, refresh=refresh)
                provisioned.append(result.as_dict())
                print(
                    f"  {ticker:8} {result.name[:34]:36} "
                    f"{result.facts_written:>4} facts  "
                    f"{result.coverage_pct:>5.1f}%  "
                    f"{'created' if result.created else 'existing'}"
                )
            except ProvisioningError as exc:
                # One unavailable ticker must not abandon the rest.
                failed.append({"ticker": ticker, "error": str(exc)})
                print(f"  {ticker:8} FAILED — {exc}")
            except Exception as exc:  # noqa: BLE001
                failed.append({"ticker": ticker, "error": str(exc)[:200]})
                print(f"  {ticker:8} FAILED — {type(exc).__name__}: {exc}")

    return {"provisioned": provisioned, "failed": failed}


def main() -> int:  # pragma: no cover - operational entrypoint
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tickers", default=",".join(SEED_UNIVERSE))
    parser.add_argument("--refresh", action="store_true",
                        help="re-fetch statements for companies already present")
    args = parser.parse_args()

    tickers = tuple(t.strip().upper() for t in args.tickers.split(",") if t.strip())
    print(f"provisioning {len(tickers)} US companies…")
    summary = seed_us_universe(tickers, refresh=args.refresh)

    ok = len(summary["provisioned"])
    bad = len(summary["failed"])
    print(f"\n{ok} provisioned, {bad} failed")
    # Non-zero only if nothing worked: a partial seed is still useful, and
    # failing the deploy over one unavailable symbol would be wrong.
    return 1 if ok == 0 and bad else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
