"""Structured logging, request metrics, error tracking and health checks.

The platform must be observable with no external service attached: no
Datadog, no Sentry, no Prometheus server. That constraint shapes every choice
here.

* **Logging** is structlog rendering JSON in production and colourised
  key-values in development. Every line carries the request id, so a support
  question ("what happened at 14:32?") is one grep.
* **Metrics** are one-minute buckets in a table with a fixed-boundary
  histogram. Percentiles are estimated by interpolating within the bucket the
  target rank falls in — exact percentiles need every sample, which is exactly
  what a bucket exists to avoid. Error is bounded by the bucket width.
* **Errors** are grouped by fingerprint with a count. An error loop must not
  be able to fill the database with evidence of itself.
* **Health** distinguishes liveness from readiness. Liveness answers "is this
  process running" — a Kubernetes restart trigger. Readiness answers "should
  traffic come here", which is a different question with a different remedy.
"""
from __future__ import annotations

import logging
import re
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.platform import ErrorEvent, RequestMetric


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ===========================================================================
# Structured logging
# ===========================================================================
_configured = False


def configure_logging() -> None:
    """Set up structlog once. Idempotent — the app factory and the worker
    both call it, and configuring twice duplicates every line."""
    global _configured
    if _configured:
        return

    renderer = (
        structlog.processors.JSONRenderer()
        if settings.LOG_FORMAT == "json"
        else structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            _redact_processor,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, settings.LOG_LEVEL, logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )
    logging.basicConfig(
        level=getattr(logging, settings.LOG_LEVEL, logging.INFO),
        format="%(message)s",
        stream=sys.stderr,
        force=True,
    )
    _configured = True


def _redact_processor(_logger, _method, event_dict: dict) -> dict:
    """Apply the audit redaction rules to every log line.

    The same function the audit trail uses, so a secret cannot be safe in one
    and exposed in the other. Logs are the likelier leak: they are shipped off
    the box, kept for a year, and read by more people.
    """
    from app.domain.platform.audit import redact

    return redact(event_dict)


def get_logger(name: str = "ierp"):
    configure_logging()
    return structlog.get_logger(name)


def bind_request(request_id: str, **extra: Any) -> None:
    """Attach request-scoped context to every subsequent log line.

    Uses contextvars, so it survives `await` boundaries and does not leak
    between concurrent requests the way a module-level global would.
    """
    structlog.contextvars.bind_contextvars(request_id=request_id, **extra)


def clear_request() -> None:
    structlog.contextvars.clear_contextvars()


# ===========================================================================
# Metrics
# ===========================================================================
#: Latency histogram boundaries in milliseconds. Dense where this application
#: actually lives (5-500 ms) and sparse in the tail, because the difference
#: between 5 s and 7 s does not change what anyone does about it.
LATENCY_BUCKETS_MS: tuple[float, ...] = (
    5, 10, 25, 50, 100, 250, 500, 1_000, 2_500, 5_000, 10_000,
)

#: Path segments replaced before a route becomes a metric label. Without this
#: `/companies/abc-123` and `/companies/def-456` are two routes and the table
#: grows without bound — the classic high-cardinality mistake.
_ID_PATTERNS = (
    (re.compile(r"/\d+(?=/|$)"), "/{id}"),
    (re.compile(r"/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}(?=/|$)"), "/{uuid}"),
    (re.compile(r"/[0-9a-fA-F]{32,}(?=/|$)"), "/{hash}"),
)


def normalise_route(path: str) -> str:
    """Collapse identifiers so the label set stays small and stable."""
    out = path
    for pattern, replacement in _ID_PATTERNS:
        out = pattern.sub(replacement, out)
    return out[:160]


def bucket_start(moment: datetime | None = None) -> datetime:
    """Truncate to the minute — the aggregation grain."""
    now = moment or _utcnow()
    return now.replace(second=0, microsecond=0)


def histogram_index(duration_ms: float) -> int:
    """Which histogram slot a duration falls in. The final slot is the
    overflow, so nothing is ever dropped."""
    for i, boundary in enumerate(LATENCY_BUCKETS_MS):
        if duration_ms <= boundary:
            return i
    return len(LATENCY_BUCKETS_MS)


def estimate_percentile(histogram: list[int], percentile: float) -> float:
    """Interpolate a percentile from bucket counts.

    Linear interpolation *within* the containing bucket rather than returning
    the boundary: reporting "p50 = 100 ms" for everything between 50 and 100
    ms makes the number useless for spotting a regression from 55 to 95.
    """
    total = sum(histogram)
    if total == 0:
        return 0.0

    target = total * percentile
    cumulative = 0
    for i, count in enumerate(histogram):
        if count == 0:
            continue
        if cumulative + count >= target:
            lower = 0.0 if i == 0 else LATENCY_BUCKETS_MS[i - 1]
            upper = (
                LATENCY_BUCKETS_MS[i] if i < len(LATENCY_BUCKETS_MS)
                else LATENCY_BUCKETS_MS[-1] * 2
            )
            within = (target - cumulative) / count
            return round(lower + (upper - lower) * within, 2)
        cumulative += count
    return float(LATENCY_BUCKETS_MS[-1])


@dataclass
class _Pending:
    """One minute of one route, accumulating in memory."""

    count: int = 0
    errors: int = 0
    total_ms: float = 0.0
    max_ms: float = 0.0
    histogram: list[int] = field(
        default_factory=lambda: [0] * (len(LATENCY_BUCKETS_MS) + 1)
    )


class MetricsCollector:
    """Accumulates in memory, flushes to the database in batches.

    A write per request would make the metrics system the slowest thing in the
    application it is measuring. Buffered by (bucket, route, method, status)
    and flushed on a size or age trigger, the cost is one insert per route per
    minute.
    """

    def __init__(self, *, flush_after: int = 200, flush_seconds: float = 30.0) -> None:
        self._pending: dict[tuple[datetime, str, str, str], _Pending] = {}
        self._observations = 0
        self._last_flush = time.monotonic()
        self.flush_after = flush_after
        self.flush_seconds = flush_seconds

    def observe(
        self, *, route: str, method: str, status_code: int, duration_ms: float,
    ) -> None:
        key = (
            bucket_start(),
            normalise_route(route),
            method.upper(),
            f"{status_code // 100}xx",
        )
        entry = self._pending.setdefault(key, _Pending())
        entry.count += 1
        entry.total_ms += duration_ms
        entry.max_ms = max(entry.max_ms, duration_ms)
        entry.histogram[histogram_index(duration_ms)] += 1
        if status_code >= 500:
            entry.errors += 1
        self._observations += 1

    @property
    def should_flush(self) -> bool:
        if not self._pending:
            return False
        return (
            self._observations >= self.flush_after
            or (time.monotonic() - self._last_flush) >= self.flush_seconds
        )

    def flush(self, db: Session) -> int:
        """Merge the buffer into `request_metrics`. Returns rows touched.

        Never raises: a metrics failure must not take down the request that
        triggered the flush.
        """
        if not self._pending:
            return 0

        snapshot, self._pending = self._pending, {}
        self._observations = 0
        self._last_flush = time.monotonic()

        touched = 0
        try:
            # One transaction per bucket rather than one for the batch. Two
            # flushes racing on the same (bucket, route, method, status) row
            # violate the unique constraint, and a single transaction would
            # lose the entire batch to one collision. Per-row lets the loser
            # retry just that row.
            for (bucket, route, method, status_class), entry in snapshot.items():
                row = db.scalar(
                    select(RequestMetric).where(
                        RequestMetric.bucket_start == bucket,
                        RequestMetric.route == route,
                        RequestMetric.method == method,
                        RequestMetric.status_class == status_class,
                    )
                )
                if row is None:
                    db.add(RequestMetric(
                        bucket_start=bucket, route=route, method=method,
                        status_class=status_class, count=entry.count,
                        error_count=entry.errors, total_ms=entry.total_ms,
                        max_ms=entry.max_ms, histogram=entry.histogram,
                    ))
                else:
                    _merge(row, entry)

                try:
                    db.commit()
                    touched += 1
                except IntegrityError:
                    # Another flush created the row between the SELECT and the
                    # INSERT. Re-read and merge into theirs rather than
                    # discarding this minute's samples.
                    db.rollback()
                    existing = db.scalar(
                        select(RequestMetric).where(
                            RequestMetric.bucket_start == bucket,
                            RequestMetric.route == route,
                            RequestMetric.method == method,
                            RequestMetric.status_class == status_class,
                        )
                    )
                    if existing is not None:
                        _merge(existing, entry)
                        db.commit()
                        touched += 1
        except Exception as exc:  # noqa: BLE001
            get_logger().warning("metrics flush failed", error=str(exc))
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
            return 0
        return touched


def _merge(row: RequestMetric, entry: _Pending) -> None:
    """Fold a buffered minute into an existing bucket row."""
    row.count += entry.count
    row.error_count += entry.errors
    row.total_ms += entry.total_ms
    row.max_ms = max(row.max_ms, entry.max_ms)
    merged = list(row.histogram or [0] * len(entry.histogram))
    for i, value in enumerate(entry.histogram):
        merged[i] = merged[i] + value
    row.histogram = merged


#: One collector per process. Module-level because the middleware needs it and
#: threading it through the app object buys nothing.
collector = MetricsCollector()


class MetricsService:
    """Reads what the collector wrote."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def overview(self, *, minutes: int = 60) -> dict[str, Any]:
        since = bucket_start() - timedelta(minutes=minutes)
        rows = list(self.db.scalars(
            select(RequestMetric).where(RequestMetric.bucket_start >= since)
        ))
        if not rows:
            return {
                "window_minutes": minutes, "requests": 0, "errors": 0,
                "error_rate": 0.0, "avg_ms": 0.0, "p50_ms": 0.0,
                "p95_ms": 0.0, "p99_ms": 0.0, "max_ms": 0.0, "rpm": 0.0,
            }

        total = sum(r.count for r in rows)
        errors = sum(r.error_count for r in rows)
        total_ms = sum(r.total_ms for r in rows)
        combined = [0] * (len(LATENCY_BUCKETS_MS) + 1)
        for row in rows:
            for i, value in enumerate(row.histogram or []):
                if i < len(combined):
                    combined[i] += value

        return {
            "window_minutes": minutes,
            "requests": total,
            "errors": errors,
            "error_rate": round(errors / total, 5) if total else 0.0,
            "avg_ms": round(total_ms / total, 2) if total else 0.0,
            "p50_ms": estimate_percentile(combined, 0.50),
            "p95_ms": estimate_percentile(combined, 0.95),
            "p99_ms": estimate_percentile(combined, 0.99),
            "max_ms": round(max(r.max_ms for r in rows), 2),
            "rpm": round(total / minutes, 2),
        }

    def by_route(self, *, minutes: int = 60, limit: int = 20) -> list[dict[str, Any]]:
        """Slowest routes first — the list an engineer actually wants."""
        since = bucket_start() - timedelta(minutes=minutes)
        grouped: dict[str, dict[str, Any]] = {}

        for row in self.db.scalars(
            select(RequestMetric).where(RequestMetric.bucket_start >= since)
        ):
            key = f"{row.method} {row.route}"
            entry = grouped.setdefault(key, {
                "route": row.route, "method": row.method, "count": 0,
                "errors": 0, "total_ms": 0.0, "max_ms": 0.0,
                "histogram": [0] * (len(LATENCY_BUCKETS_MS) + 1),
            })
            entry["count"] += row.count
            entry["errors"] += row.error_count
            entry["total_ms"] += row.total_ms
            entry["max_ms"] = max(entry["max_ms"], row.max_ms)
            for i, value in enumerate(row.histogram or []):
                if i < len(entry["histogram"]):
                    entry["histogram"][i] += value

        out = []
        for entry in grouped.values():
            count = entry["count"] or 1
            out.append({
                "route": entry["route"],
                "method": entry["method"],
                "count": entry["count"],
                "errors": entry["errors"],
                "error_rate": round(entry["errors"] / count, 5),
                "avg_ms": round(entry["total_ms"] / count, 2),
                "p95_ms": estimate_percentile(entry["histogram"], 0.95),
                "max_ms": round(entry["max_ms"], 2),
            })
        out.sort(key=lambda e: -e["p95_ms"])
        return out[:limit]

    def timeseries(self, *, minutes: int = 60) -> list[dict[str, Any]]:
        since = bucket_start() - timedelta(minutes=minutes)
        rows = self.db.execute(
            select(
                RequestMetric.bucket_start,
                func.sum(RequestMetric.count),
                func.sum(RequestMetric.error_count),
                func.sum(RequestMetric.total_ms),
            )
            .where(RequestMetric.bucket_start >= since)
            .group_by(RequestMetric.bucket_start)
            .order_by(RequestMetric.bucket_start)
        )
        return [
            {
                "at": bucket.isoformat() if hasattr(bucket, "isoformat") else str(bucket),
                "requests": int(count or 0),
                "errors": int(errors or 0),
                "avg_ms": round(float(total or 0) / int(count or 1), 2),
            }
            for bucket, count, errors, total in rows
        ]

    def purge(self, *, older_than_days: int) -> int:
        cutoff = bucket_start() - timedelta(days=older_than_days)
        result = self.db.execute(
            delete(RequestMetric).where(RequestMetric.bucket_start < cutoff)
        )
        self.db.commit()
        return int(result.rowcount or 0)


# ===========================================================================
# Error tracking
# ===========================================================================
#: Numbers, quoted strings and hex ids are stripped from a message before
#: fingerprinting, so "no company 41" and "no company 87" group as one error
#: rather than as two hundred.
_NORMALISE = (
    (re.compile(r"\b0x[0-9a-fA-F]+\b"), "<hex>"),
    (re.compile(r"\b\d[\d,._]*\b"), "<n>"),
    (re.compile(r"'[^']{0,80}'"), "'<s>'"),
    (re.compile(r'"[^"]{0,80}"'), '"<s>"'),
)


def normalise_message(message: str) -> str:
    out = message
    for pattern, replacement in _NORMALISE:
        out = pattern.sub(replacement, out)
    return out[:500]


def application_frames(tb: str, *, keep: int = 12) -> str:
    """Strip site-packages, keeping only our own code.

    A traceback through Starlette, SQLAlchemy and anyio is thirty frames of
    library and three of application. The three are the ones that matter.
    """
    lines = [
        line for line in tb.splitlines()
        if "site-packages" not in line and "/usr/lib/python" not in line
    ]
    return "\n".join(lines[-keep * 2:])[:4000]


class ErrorTracker:
    """Groups unhandled exceptions by fingerprint."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def capture(
        self,
        exc: BaseException,
        *,
        route: str | None = None,
        method: str | None = None,
        tenant_id: int | None = None,
        request_id: str | None = None,
    ) -> ErrorEvent | None:
        from app.services.platform.crypto import fingerprint as make_fingerprint

        tb = "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        )
        frames = application_frames(tb)
        top = next(
            (l.strip() for l in reversed(frames.splitlines()) if l.strip().startswith("File")),
            "",
        )
        digest = make_fingerprint(
            type(exc).__name__, normalise_message(str(exc)), top, route or "",
        )

        now = _utcnow()
        try:
            row = self.db.scalar(
                select(ErrorEvent).where(ErrorEvent.fingerprint == digest)
            )
            if row is None:
                row = ErrorEvent(
                    fingerprint=digest,
                    exc_type=type(exc).__name__[:120],
                    message=str(exc)[:2000],
                    route=route, method=method, stack=frames,
                    count=1, first_seen_at=now, last_seen_at=now,
                    tenant_id=tenant_id, last_request_id=request_id,
                )
                self.db.add(row)
            else:
                row.count += 1
                row.last_seen_at = now
                row.last_request_id = request_id
                # A recurrence means it is not fixed, whatever anyone marked.
                row.resolved_at = None
            self.db.commit()
            self.db.refresh(row)
        except Exception as inner:  # noqa: BLE001
            get_logger().error("error capture failed", error=str(inner))
            try:
                self.db.rollback()
            except Exception:  # noqa: BLE001
                pass
            return None

        get_logger().error(
            "unhandled exception",
            exc_type=type(exc).__name__, route=route, method=method,
            fingerprint=digest, request_id=request_id,
        )
        return row

    def list(
        self,
        *,
        resolved: bool | None = False,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[ErrorEvent], int]:
        stmt = select(ErrorEvent)
        if resolved is True:
            stmt = stmt.where(ErrorEvent.resolved_at.isnot(None))
        elif resolved is False:
            stmt = stmt.where(ErrorEvent.resolved_at.is_(None))
        total = self.db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        rows = list(self.db.scalars(
            stmt.order_by(ErrorEvent.last_seen_at.desc()).offset(offset).limit(limit)
        ))
        return rows, total

    def resolve(self, fingerprint: str, user_id: str) -> ErrorEvent | None:
        row = self.db.scalar(
            select(ErrorEvent).where(ErrorEvent.fingerprint == fingerprint)
        )
        if row is None:
            return None
        row.resolved_at = _utcnow()
        row.resolved_by = user_id
        self.db.commit()
        self.db.refresh(row)
        return row


# ===========================================================================
# Health
# ===========================================================================
@dataclass(frozen=True, slots=True)
class Check:
    name: str
    ok: bool
    detail: str = ""
    duration_ms: float = 0.0
    #: A degraded non-critical check does not make the service unready.
    critical: bool = True


@dataclass(frozen=True, slots=True)
class HealthReport:
    status: str                 # ok | degraded | unhealthy
    checks: list[Check]
    version: str
    environment: str
    uptime_seconds: float

    @property
    def ready(self) -> bool:
        return all(c.ok for c in self.checks if c.critical)


_STARTED_AT = time.monotonic()


class HealthService:
    """Liveness, readiness and the dependency checks behind them."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def liveness(self) -> dict[str, Any]:
        """Is the process alive? Deliberately touches nothing.

        A liveness probe that queries the database restarts a healthy
        application every time the database hiccups, turning a brief
        dependency outage into a restart storm.
        """
        return {
            "status": "ok",
            "version": settings.APP_VERSION,
            "environment": settings.ENVIRONMENT,
            "uptime_seconds": round(time.monotonic() - _STARTED_AT, 1),
        }

    def readiness(self) -> HealthReport:
        checks = [
            self._check_database(),
            self._check_migrations(),
            self._check_configuration(),
            self._check_configuration_optional(),
            self._check_queue(),
        ]
        critical_failed = any(not c.ok for c in checks if c.critical)
        degraded = any(not c.ok for c in checks if not c.critical)
        return HealthReport(
            status=(
                "unhealthy" if critical_failed
                else "degraded" if degraded
                else "ok"
            ),
            checks=checks,
            version=settings.APP_VERSION,
            environment=settings.ENVIRONMENT,
            uptime_seconds=round(time.monotonic() - _STARTED_AT, 1),
        )

    def _check_database(self) -> Check:
        started = time.perf_counter()
        try:
            self.db.execute(text("SELECT 1"))
            return Check(
                "database", True, "reachable",
                round((time.perf_counter() - started) * 1000, 2),
            )
        except Exception as exc:  # noqa: BLE001
            return Check(
                "database", False, str(exc)[:200],
                round((time.perf_counter() - started) * 1000, 2),
            )

    def _check_migrations(self) -> Check:
        """Do the tables the application needs exist?

        Cheaper and more honest than reading Alembic's version table: what
        matters is whether the schema serves the code, not whether a revision
        string matches.
        """
        started = time.perf_counter()
        from app.db.base import Base

        try:
            from sqlalchemy import inspect

            present = set(inspect(self.db.get_bind()).get_table_names())
            expected = set(Base.metadata.tables)
            missing = sorted(expected - present)
            return Check(
                "schema", not missing,
                "complete" if not missing else f"missing: {', '.join(missing[:5])}",
                round((time.perf_counter() - started) * 1000, 2),
            )
        except Exception as exc:  # noqa: BLE001
            return Check("schema", False, str(exc)[:200])

    def _check_configuration(self) -> Check:
        """Critical: only unsafe configuration keeps traffic away.

        Split deliberately from `_check_configuration_optional`. Anything that
        makes the service unsafe to expose (unsigned tokens, DEBUG, the
        development identity, SQLite, a plaintext CORS origin) fails this
        critical check and returns 503. A missing mail relay does not.
        """
        problems = settings.production_blocking_problems()
        return Check(
            "configuration", not problems,
            "; ".join(problems)[:300] if problems else "complete",
        )

    def _check_configuration_optional(self) -> Check:
        """Non-critical: features that are unavailable but do not risk harm."""
        problems = settings.production_degraded_problems()
        return Check(
            "optional_configuration", not problems,
            "; ".join(problems)[:300] if problems else "complete",
            critical=False,
        )

    def _check_queue(self) -> Check:
        """Non-critical: a stalled queue degrades the product but the API can
        still serve every synchronous request."""
        from app.services.platform.jobs.queue import JobQueue

        try:
            depth = JobQueue(self.db).depth()
            return Check(
                "queue", depth.is_healthy,
                (
                    f"{depth.queued} queued, {depth.running} running, "
                    f"{depth.dead_letter} dead-letter"
                ),
                critical=False,
            )
        except Exception as exc:  # noqa: BLE001
            return Check("queue", False, str(exc)[:200], critical=False)
