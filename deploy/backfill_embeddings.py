#!/usr/bin/env python3
"""Re-embed the corpus with semantic vectors.

Idempotent and resumable: chunks already carrying a vector for the current
embedding spec are skipped, so an interrupted run is restarted by running it
again. Changing the model changes the spec, which re-embeds everything —
correctly, because vectors from two spaces cannot share an index.

    export DATABASE_URL=...
    python3 deploy/backfill_embeddings.py --limit 2000
    python3 deploy/backfill_embeddings.py --index      # build IVFFlat after
"""

from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "backend"))

import importlib  # noqa: E402
import pkgutil  # noqa: E402

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import app.models as _models  # noqa: E402

for _module in pkgutil.iter_modules(_models.__path__):
    importlib.import_module(f"app.models.{_module.name}")

from app.services.retrieval.embeddings import build_semantic_embedder  # noqa: E402

#: Rows fetched and written per transaction. Small enough that a failure
#: costs one batch, large enough that commit overhead is not the bottleneck.
BATCH = 64


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--company", default=None,
                        help="Restrict to one ticker.")
    parser.add_argument("--index", action="store_true",
                        help="Build the IVFFlat index and exit.")
    parser.add_argument("--stats", action="store_true")
    args = parser.parse_args()

    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2

    engine = create_engine(url, pool_pre_ping=True)
    db = sessionmaker(bind=engine)()

    if args.stats:
        return _stats(db)
    if args.index:
        return _build_index(db)

    embedder = build_semantic_embedder()
    if embedder is None:
        print("no semantic embedding provider is configured", file=sys.stderr)
        return 3
    spec = embedder.spec.key
    print(f"provider: {embedder.name}  spec: {spec}")

    where = ["(c.embedding_spec_v2 IS DISTINCT FROM :spec)",
             "c.text IS NOT NULL", "length(c.text) > 0"]
    params: dict[str, object] = {"spec": spec}
    if args.company:
        where.append(
            "d.company_id = (SELECT id FROM companies WHERE ticker = :ticker)"
        )
        params["ticker"] = args.company.upper()

    pending = db.execute(text(f"""
        SELECT count(*) FROM document_chunks c
        JOIN documents d ON d.id = c.document_id
        WHERE {' AND '.join(where)}
    """), params).scalar() or 0
    target = min(pending, args.limit) if args.limit else pending
    print(f"pending: {pending}   this run: {target}")
    if not target:
        return 0

    started = time.perf_counter()
    done = 0
    failures = 0

    while done < target:
        rows = db.execute(text(f"""
            SELECT c.id, c.text FROM document_chunks c
            JOIN documents d ON d.id = c.document_id
            WHERE {' AND '.join(where)}
            ORDER BY c.id
            LIMIT :batch
        """), {**params, "batch": min(BATCH, target - done)}).all()
        if not rows:
            break

        try:
            vectors = embedder.embed([r[1] for r in rows])
        except Exception as exc:  # noqa: BLE001 — one batch must not end the run
            # Retry the rows one at a time before giving up on them. A batch
            # usually fails because of its aggregate size, not because every
            # chunk in it is bad, and abandoning 64 good chunks for one
            # oversized neighbour wastes the run.
            print(f"  batch of {len(rows)} failed ({str(exc)[:90]}); "
                  f"retrying individually")
            vectors = []
            salvaged = []
            for chunk_id, chunk_text in rows:
                try:
                    vectors.append(embedder.embed_one(chunk_text))
                    salvaged.append((chunk_id, chunk_text))
                except Exception:  # noqa: BLE001
                    failures += 1
            rows = salvaged
            if not rows:
                print("  every chunk in the batch failed; stopping")
                break

        for (chunk_id, _), vector in zip(rows, vectors):
            literal = "[" + ",".join(f"{v:.7f}" for v in vector) + "]"
            db.execute(text("""
                UPDATE document_chunks
                SET embedding_v2 = CAST(:vec AS vector),
                    embedding_spec_v2 = :spec
                WHERE id = :id
            """), {"vec": literal, "spec": spec, "id": chunk_id})
        db.commit()

        done += len(rows)
        rate = done / max(time.perf_counter() - started, 0.001)
        print(f"  {done}/{target}  ({rate:.1f} chunks/s)", flush=True)

    elapsed = time.perf_counter() - started
    print(f"\nembedded {done} chunks in {elapsed:.1f}s, {failures} failed")
    return 0


def _build_index(db) -> int:
    """Build the IVFFlat index, sized from the data.

    Built after the backfill rather than in the migration: IVFFlat clusters
    the vectors it can see, and an index built on an empty table is useless.
    """
    count = db.execute(text(
        "SELECT count(*) FROM document_chunks WHERE embedding_v2 IS NOT NULL"
    )).scalar() or 0
    if not count:
        print("no vectors to index")
        return 1

    # pgvector's guidance: rows/1000 up to a million. Clamped so a small
    # corpus does not get one list per handful of rows.
    lists = max(10, min(int(count ** 0.5), 1000))
    print(f"building IVFFlat over {count} vectors, lists={lists}")
    db.execute(text("DROP INDEX IF EXISTS ix_chunks_vector"))
    db.execute(text(
        f"CREATE INDEX ix_chunks_vector ON document_chunks "
        f"USING ivfflat (embedding_v2 vector_cosine_ops) WITH (lists = {lists})"
    ))
    db.commit()
    print("index built")
    return 0


def _stats(db) -> int:
    total = db.execute(text("SELECT count(*) FROM document_chunks")).scalar()
    v2 = db.execute(text(
        "SELECT count(*) FROM document_chunks WHERE embedding_v2 IS NOT NULL"
    )).scalar()
    specs = db.execute(text(
        "SELECT embedding_spec_v2, count(*) FROM document_chunks "
        "WHERE embedding_spec_v2 IS NOT NULL GROUP BY 1"
    )).all()
    print(f"chunks: {total}   semantic: {v2}   "
          f"({100 * v2 / total:.1f}%)" if total else "no chunks")
    for spec, n in specs:
        print(f"  {spec}: {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
