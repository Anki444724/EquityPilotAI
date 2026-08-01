"""Migrate document storage from the Railway Volume to an S3 bucket.

Safe to run against production. Nothing is deleted, every copy is verified by
read-back before the database row is repointed, and the run is idempotent so
an interruption is recovered by running it again.

Usage:
    # look, change nothing
    python3 deploy/migrate_to_bucket.py --dry-run

    # migrate, then benchmark
    python3 deploy/migrate_to_bucket.py
    python3 deploy/migrate_to_bucket.py --benchmark-only

Configuration is read from the environment — the same five variables the
application uses — so this script never holds a credential of its own:

    DATABASE_URL
    DOCUMENT_STORAGE_PATH        (the volume, i.e. the source)
    DOCUMENT_S3_BUCKET / _ENDPOINT / _REGION / _ACCESS_KEY / _SECRET_KEY
"""
from __future__ import annotations

import argparse
import os
import pathlib
import statistics
import sys
import time

BACKEND = pathlib.Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND))
os.chdir(BACKEND)


def _storages():
    from app.core.config import settings
    from app.services.documents.storage import (
        LocalFileStorage, S3CompatibleStorage,
    )

    if not settings.DOCUMENT_S3_BUCKET:
        raise SystemExit(
            "DOCUMENT_S3_BUCKET is unset — nothing to migrate to.\n"
            "Set the five DOCUMENT_S3_* variables first."
        )
    source = LocalFileStorage(settings.DOCUMENT_STORAGE_PATH)
    destination = S3CompatibleStorage(
        settings.DOCUMENT_S3_BUCKET,
        endpoint_url=settings.DOCUMENT_S3_ENDPOINT,
        region=settings.DOCUMENT_S3_REGION,
        access_key=settings.DOCUMENT_S3_ACCESS_KEY,
        secret_key=settings.DOCUMENT_S3_SECRET_KEY,
    )
    return source, destination


def benchmark(destination, *, sizes=(64_000, 1_000_000, 8_000_000),
              rounds: int = 3) -> list[dict]:
    """Round-trip upload/download timings at several object sizes.

    Reported per size rather than as one average: the platform stores 200 KB
    board-meeting notices and 24 MB annual reports, and a single mean hides
    whichever of those dominates.
    """
    rows: list[dict] = []
    for size in sizes:
        payload = b"%PDF-1.4\n" + os.urandom(max(0, size - 20)) + b"\n%%EOF"
        key = f"_benchmark/probe_{size}.bin"
        ups: list[float] = []
        downs: list[float] = []
        for _ in range(rounds):
            started = time.perf_counter()
            destination.put(key, payload)
            ups.append((time.perf_counter() - started) * 1000)

            started = time.perf_counter()
            got = destination.read(key)
            downs.append((time.perf_counter() - started) * 1000)
            if len(got) != len(payload):
                raise SystemExit(
                    f"benchmark read-back mismatch at {size} bytes: "
                    f"{len(got)} != {len(payload)}"
                )
        mb = len(payload) / (1024 * 1024)
        up = statistics.median(ups)
        down = statistics.median(downs)
        rows.append({
            "bytes": len(payload),
            "mb": round(mb, 2),
            "upload_ms": round(up, 1),
            "download_ms": round(down, 1),
            "upload_mbps": round(mb / (up / 1000), 2) if up else 0.0,
            "download_mbps": round(mb / (down / 1000), 2) if down else 0.0,
        })
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--benchmark-only", action="store_true")
    parser.add_argument("--no-benchmark", action="store_true")
    args = parser.parse_args()

    from app.db.base import SessionLocal
    from app.services.documents.migrate_storage import StorageMigrator

    source, destination = _storages()

    if not args.benchmark_only:
        db = SessionLocal()
        try:
            report = StorageMigrator(
                db, source, destination, dry_run=args.dry_run,
            ).run(limit=args.limit)
        finally:
            db.close()

        payload = report.as_dict()
        print("=== migration ===")
        for key in ("total", "migrated", "already_present", "missing_source",
                    "failed", "megabytes_copied", "throughput_mbps", "ok"):
            print(f"  {key:18} {payload[key]}")
        for outcome in payload["outcomes"]:
            if outcome["status"] in ("failed", "missing_source"):
                print(f"  !! {outcome['status']}: doc {outcome['document_id']} "
                      f"{outcome['key']} — {outcome['detail'][:90]}")
        if not report.ok and not args.dry_run:
            print("\nMigration did not complete cleanly. The volume is "
                  "unchanged and remains authoritative.")
            return 1

    if not args.no_benchmark:
        print("\n=== benchmark (median of 3) ===")
        print(f"  {'size':>9} {'upload':>11} {'download':>11} "
              f"{'up MB/s':>9} {'down MB/s':>10}")
        for row in benchmark(destination):
            print(f"  {row['mb']:>7.2f}MB {row['upload_ms']:>9.1f}ms "
                  f"{row['download_ms']:>9.1f}ms {row['upload_mbps']:>9.2f} "
                  f"{row['download_mbps']:>10.2f}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
