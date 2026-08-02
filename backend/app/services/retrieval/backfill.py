"""Automatic embedding backfill.

Requirement 2: once embeddings are available, the corpus embeds itself.

Runs on a schedule rather than being triggered, because the event it waits
for — a provider becoming reachable — produces no signal. A key is added to
the environment, or a quota resets overnight, and nothing tells the
application. Polling costs one indexed COUNT every half hour and removes the
manual step entirely.

Self-arming and self-disarming: with no provider configured it returns
`skipped` immediately; with one, it embeds a bounded slice per run and the
next run continues. An interrupted run resumes because the work queue is
"chunks whose spec does not match the current provider", which is derived
from state rather than tracked.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import structlog
from sqlalchemy import text

log = structlog.get_logger(__name__)

#: Chunks per run. Bounded so one pass cannot hold a worker for an hour or
#: exhaust a quota in a single burst; the schedule drains the rest.
DEFAULT_LIMIT = 500

#: Rows per transaction.
BATCH = 32


@dataclass(slots=True)
class BackfillRun:
    embedded: int = 0
    failed: int = 0
    remaining: int = 0
    provider: str | None = None
    spec: str | None = None
    skipped: bool = False
    detail: str | None = None
    latency_ms: float = 0.0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "embedded": self.embedded,
            "failed": self.failed,
            "remaining": self.remaining,
            "provider": self.provider,
            "spec": self.spec,
            "skipped": self.skipped,
            "detail": self.detail,
            "latency_ms": round(self.latency_ms, 1),
            "errors": self.errors[:5],
        }


class EmbeddingBackfillService:
    """Embeds chunks that carry no vector for the current provider."""

    #: Distinguishes "not supplied, resolve one" from "explicitly none".
    #: Without it a caller cannot inject the no-provider case, which is the
    #: state the service most needs to handle correctly.
    _UNSET = object()

    def __init__(self, db: Any, *, embedder: Any = _UNSET) -> None:
        self.db = db
        if embedder is self._UNSET:
            from app.services.retrieval.embeddings import (
                build_semantic_embedder,
            )
            embedder = build_semantic_embedder()
        self.embedder = embedder

    def pending(self, spec: str) -> int:
        return self.db.execute(text("""
            SELECT count(*) FROM document_chunks
            WHERE (embedding_spec_v2 IS DISTINCT FROM :spec)
              AND text IS NOT NULL AND length(text) > 0
        """), {"spec": spec}).scalar() or 0

    def run(self, *, limit: int = DEFAULT_LIMIT) -> BackfillRun:
        started = time.perf_counter()
        run = BackfillRun()

        if self.embedder is None:
            run.skipped = True
            run.detail = "no embedding provider configured"
            return run

        run.provider = self.embedder.name
        run.spec = self.embedder.spec.key
        run.remaining = self.pending(run.spec)
        if not run.remaining:
            run.skipped = True
            run.detail = "every chunk carries a current vector"
            run.latency_ms = (time.perf_counter() - started) * 1000
            return run

        target = min(limit, run.remaining)
        while run.embedded + run.failed < target:
            rows = self.db.execute(text("""
                SELECT id, text FROM document_chunks
                WHERE (embedding_spec_v2 IS DISTINCT FROM :spec)
                  AND text IS NOT NULL AND length(text) > 0
                ORDER BY id
                LIMIT :batch
            """), {"spec": run.spec,
                   "batch": min(BATCH, target - run.embedded - run.failed)}).all()
            if not rows:
                break

            try:
                vectors = self.embedder.embed([r[1] for r in rows])
            except Exception as exc:  # noqa: BLE001
                # A failure here is nearly always the provider being
                # unreachable, which will not change inside this run. Stop
                # and let the schedule retry rather than hammering it: the
                # embedding client's circuit breaker (RETR-002) would make
                # every remaining batch fail instantly anyway.
                run.errors.append(f"{type(exc).__name__}: {exc}"[:200])
                run.detail = "provider unavailable; will resume next run"
                break

            for (chunk_id, _), vector in zip(rows, vectors):
                literal = "[" + ",".join(f"{v:.7f}" for v in vector) + "]"
                self.db.execute(text("""
                    UPDATE document_chunks
                    SET embedding_v2 = CAST(:vec AS vector),
                        embedding_spec_v2 = :spec
                    WHERE id = :id
                """), {"vec": literal, "spec": run.spec, "id": chunk_id})
            self.db.commit()
            run.embedded += len(rows)

        run.remaining = self.pending(run.spec)
        run.latency_ms = (time.perf_counter() - started) * 1000

        # Build the ANN index once enough vectors exist to cluster. IVFFlat
        # clusters what it can see, so building it early produces a useless
        # index; this defers until the corpus is mostly embedded.
        if run.embedded and run.remaining == 0:
            self._ensure_index()

        log.info("embedding backfill", provider=run.provider,
                 embedded=run.embedded, remaining=run.remaining)
        return run

    def _ensure_index(self) -> None:
        """Create the IVFFlat index if it does not exist."""
        try:
            count = self.db.execute(text(
                "SELECT count(*) FROM document_chunks "
                "WHERE embedding_v2 IS NOT NULL"
            )).scalar() or 0
            if count < 100:
                return
            lists = max(10, min(int(count ** 0.5), 1000))
            self.db.execute(text("DROP INDEX IF EXISTS ix_chunks_vector"))
            self.db.execute(text(
                f"CREATE INDEX ix_chunks_vector ON document_chunks "
                f"USING ivfflat (embedding_v2 vector_cosine_ops) "
                f"WITH (lists = {lists})"
            ))
            self.db.commit()
            log.info("vector index built", vectors=count, lists=lists)
        except Exception as exc:  # noqa: BLE001 — an index is an optimisation
            self.db.rollback()
            log.warning("could not build vector index", error=str(exc)[:200])
