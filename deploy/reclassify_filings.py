#!/usr/bin/env python3
"""Re-run the classifier over already-discovered filings.

New classifier rules apply to future crawls only; the rows already in the
table keep whatever type they were given when they were discovered. After the
scheduler-optimisation rules were added, 1,506 rows were still `other` and 420
still NULL purely because they predate the rules.

Idempotent and conservative: a row is only rewritten when the new
classification is genuinely better — a real type where there was none, or a
higher confidence. A row that is already well typed is left alone, so running
this twice changes nothing the second time.

    export DATABASE_URL="postgresql+psycopg://..."
    python3 deploy/reclassify_filings.py --dry-run
    python3 deploy/reclassify_filings.py
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "backend"))

import importlib  # noqa: E402
import pkgutil  # noqa: E402

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import app.models as _models  # noqa: E402

for _module in pkgutil.iter_modules(_models.__path__):
    importlib.import_module(f"app.models.{_module.name}")

from app.domain.filings.collection import classify  # noqa: E402
from app.models.filing_collection import DiscoveredFiling  # noqa: E402

#: Types that mean "we did not really work out what this is".
VAGUE = {None, "", "other"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--batch", type=int, default=2000)
    args = parser.parse_args()

    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2

    engine = create_engine(url, pool_pre_ping=True)
    db = sessionmaker(bind=engine)()

    rows = db.execute(
        select(DiscoveredFiling).limit(args.batch * 10)
    ).scalars().all()

    before = Counter((r.doc_type or "unclassified") for r in rows)
    changed = 0
    moved: Counter[str] = Counter()

    for row in rows:
        result = classify(row.title or "", url=row.source_url)
        new_type = result.doc_type.value
        old_type = row.doc_type

        improves = (
            (old_type in VAGUE and new_type not in VAGUE)
            or (new_type == old_type
                and result.confidence > (row.classification_confidence or 0))
            or (old_type not in VAGUE and new_type not in VAGUE
                and result.confidence > (row.classification_confidence or 0) + 0.1)
        )
        if not improves:
            continue

        moved[f"{old_type or 'unclassified'} -> {new_type}"] += 1
        if not args.dry_run:
            row.doc_type = new_type
            row.filing_type = result.filing_type.value
            row.classification_confidence = result.confidence
        changed += 1

    if not args.dry_run:
        db.commit()

    after = Counter()
    for row in rows:
        after[(row.doc_type or "unclassified")] += 1

    total = len(rows)
    vague_before = sum(before[k] for k in ("other", "unclassified"))
    vague_after = sum(after[k] for k in ("other", "unclassified"))

    print(f"{'DRY RUN — ' if args.dry_run else ''}rows examined: {total}")
    print(f"reclassified: {changed}")
    print(f"\nclassified before: {100 * (total - vague_before) / total:.2f}%")
    print(f"classified after : {100 * (total - vague_after) / total:.2f}%")
    print("\ntop transitions:")
    for transition, count in moved.most_common(12):
        print(f"  {count:>5}  {transition}")
    print("\ndistribution after:")
    for doc_type, count in after.most_common():
        print(f"  {count:>5}  {doc_type}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
