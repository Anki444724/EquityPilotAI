"""FastAPI application entry point.

Middleware order is the load-bearing detail in this file. Starlette runs
middleware in reverse registration order on the way in and in registration
order on the way out, so the last one added is the outermost. The registration
below therefore produces, from outside in:

    security headers → request context → metrics/errors → rate limit → CORS

which is what we want: a request rejected by the rate limiter is still counted
and still carries a request id, and a response that never reaches a route
still gets its security headers.
"""
from __future__ import annotations

import time
import uuid

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.v1.router import api_router
from app.core.config import settings
from app.db.base import Base, SessionLocal, engine
from app.models import analysis as _analysis_models  # noqa: F401  (register tables)
from app.models import company as _company_models  # noqa: F401
from app.models import forecast as _forecast_models  # noqa: F401
from app.models import scoring as _scoring_models  # noqa: F401
from app.models import ai as _ai_models  # noqa: F401
from app.models import document as _document_models  # noqa: F401
from app.models import portfolio as _portfolio_models  # noqa: F401
from app.models import report as _report_models  # noqa: F401
from app.models import platform as _platform_models  # noqa: F401  (Module 10)
from app.services.platform.observability import (
    ErrorTracker, MetricsService, collector, configure_logging, get_logger,
    normalise_route,
)

configure_logging()
log = get_logger("ierp.api")


from contextlib import asynccontextmanager  # noqa: E402


def _assert_schema_current() -> None:
    """Refuse to serve production traffic against an un-migrated database.

    Compares the tables the models declare against the tables that exist. A
    missing table means `alembic upgrade head` has not run — the deploy
    command runs it, so reaching here means it failed and was swallowed.

    Failing at startup is deliberate. The alternative is a container that
    passes its health check and returns 500 on the first real request, which
    is far harder to diagnose and is served to users rather than to the
    deployment log.
    """
    from sqlalchemy import inspect

    present = set(inspect(engine).get_table_names())
    expected = set(Base.metadata.tables)
    missing = sorted(expected - present)
    if missing:
        raise RuntimeError(
            f"database schema is not current — {len(missing)} table(s) "
            f"missing: {', '.join(missing[:8])}"
            f"{'…' if len(missing) > 8 else ''}. "
            "Run `alembic upgrade head` before starting."
        )
    log.info("schema verified", tables=len(present))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Alembic owns the schema in production; `create_all` keeps local
    # development frictionless.
    #
    # These are not interchangeable. `create_all` builds tables that do not
    # exist and does nothing to ones that do — so it cannot add a column, and
    # a deploy carrying a model change would start cleanly against a stale
    # database and fail on the first query. In production the schema must
    # come from a migration that has been reviewed, and the app must refuse
    # to start rather than paper over a mismatch.
    if settings.is_production:
        _assert_schema_current()
    else:
        Base.metadata.create_all(bind=engine)

    problems = settings.production_readiness_problems()
    for problem in problems:
        log.error("production readiness", problem=problem)

    log.info(
        "application starting",
        version=settings.APP_VERSION, environment=settings.ENVIRONMENT,
        auth=("native" if settings.NATIVE_AUTH else "development"),
        worker=settings.WORKER_ENABLED, scheduler=settings.SCHEDULER_ENABLED,
    )

    if settings.WORKER_ENABLED or settings.SCHEDULER_ENABLED:
        from app.services.platform.jobs.worker import start_in_process

        start_in_process(SessionLocal)

    yield

    # Flush whatever the collector is still holding, so the last minute of a
    # deployment's life is not lost from the metrics.
    db = SessionLocal()
    try:
        collector.flush(db)
    finally:
        db.close()

    if settings.WORKER_ENABLED or settings.SCHEDULER_ENABLED:
        from app.services.platform.jobs.worker import stop_in_process

        stop_in_process()

    log.info("application stopped")


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description=(
        "Institutional equity research platform. Financial logic is derived "
        "from Institutional_Equity_Research_Platform_v7.xlsx.\n\n"
        "**Authentication** — send `Authorization: Bearer <access token>` from "
        "`POST /api/v1/auth/login`, or an API key as `X-API-Key`. When native "
        "auth is disabled the platform resolves a clearly-labelled development "
        "identity instead.\n\n"
        "**Multi-tenancy** — every resource belongs to an organisation and is "
        "filtered to the caller's. Only the platform operator crosses that "
        "boundary."
    ),
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    lifespan=lifespan,
)


# ===========================================================================
# Middleware (registered inner → outer)
# ===========================================================================
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=[
        "X-Response-Time-ms", "X-Request-ID",
        "X-RateLimit-Limit", "X-RateLimit-Remaining", "X-RateLimit-Reset",
    ],
)


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    """Global per-caller rate limit.

    Keyed on the *credential* when one is presented and on the IP otherwise.
    Keying everything on IP looks simpler and is wrong: an entire research
    desk behind one corporate NAT would share a single anonymous budget and
    throttle each other, while the anonymous allowance has to stay small
    because it is the one an attacker gets for free. So authenticated traffic
    gets the larger `default` rule against a hash of its own credential, and
    unauthenticated traffic gets the small `anonymous` rule against its IP.

    The credential is hashed rather than used directly: this key can end up in
    a log or a Redis keyspace, and a bearer token must not.

    Endpoints with their own stricter rule — login, registration, magic link —
    apply it inside the handler, so this is a ceiling rather than the whole
    policy.
    """
    if not settings.RATE_LIMIT_ENABLED or request.url.path in _UNLIMITED_PATHS:
        return await call_next(request)

    from app.core.security import _client_ip
    from app.domain.platform.limits import RateScope
    from app.services.platform import rate_limit
    from app.services.platform.crypto import hash_token

    credential = (
        request.headers.get("x-api-key")
        or (
            request.headers.get("authorization", "").split(" ", 1)[1]
            if request.headers.get("authorization", "").lower().startswith("bearer ")
            else ""
        )
    )
    if credential:
        decision = rate_limit.check(
            "default", hash_token(credential)[:32], scope=RateScope.USER,
        )
    elif settings.auth_enabled:
        decision = rate_limit.check(
            "anonymous", _client_ip(request), scope=RateScope.IP,
        )
    else:
        # No credential *and* no identity system configured: every caller is
        # the development identity, so they are authenticated in every sense
        # that matters and the anonymous allowance would be the wrong one.
        # Throttling a local developer at 60 requests a minute would make the
        # product unusable in the configuration it ships in.
        decision = rate_limit.check(
            "default", _client_ip(request), scope=RateScope.IP,
        )

    if not decision.allowed:
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content={
                "detail": "Rate limit exceeded. Slow down and try again shortly.",
                "retry_after": decision.retry_after,
            },
            headers=decision.headers(),
        )

    response = await call_next(request)
    for key, value in decision.headers().items():
        response.headers[key] = value
    return response


@app.middleware("http")
async def observability_middleware(request: Request, call_next):
    """Time every request, count it, and capture anything that escapes.

    An exception reaching here has already bypassed FastAPI's handlers, so it
    is genuinely unhandled: it is fingerprinted, recorded, and turned into a
    500 that carries the request id but not the stack trace. Returning a
    traceback to a caller is an information leak.
    """
    started = time.perf_counter()
    try:
        response = await call_next(request)
        duration_ms = (time.perf_counter() - started) * 1000
        status_code = response.status_code
    except Exception as exc:  # noqa: BLE001
        duration_ms = (time.perf_counter() - started) * 1000
        status_code = 500
        _record(request, status_code, duration_ms, exc)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "detail": "An unexpected error occurred.",
                "request_id": getattr(request.state, "request_id", None),
            },
        )

    _record(request, status_code, duration_ms, None)
    response.headers["X-Response-Time-ms"] = f"{duration_ms:.1f}"
    response.headers["X-Request-ID"] = getattr(request.state, "request_id", "")
    return response


def _record(request: Request, status_code: int, duration_ms: float, exc: BaseException | None):
    """Observe one request. Never raises — observability must not be able to
    turn a working response into a failure.

    Recording is *in memory only*. The database write is handed to a
    background task that runs after the response is returned and after the
    request's own session has been released.

    That indirection is not fastidiousness; it is a fix for a real deadlock.
    The first version flushed inline, which meant every flush opened a second
    connection while the request still held its first. At the default pool of
    five plus ten overflow, twenty-five concurrent callers exhausted the pool,
    every subsequent checkout blocked for the full thirty-second timeout, and
    the process stopped answering entirely — including `/health`. A load test
    at concurrency 25 reproduced it in seconds; nothing at concurrency 1 ever
    would.
    """
    if not settings.METRICS_ENABLED:
        return
    try:
        route = getattr(request.scope.get("route"), "path", None) or request.url.path
        collector.observe(
            route=route, method=request.method,
            status_code=status_code, duration_ms=duration_ms,
        )
        if exc is not None:
            _defer(_capture_error, request, normalise_route(route), exc)
        elif collector.should_flush:
            _defer(_flush_metrics)
    except Exception:  # noqa: BLE001
        pass


#: At most one deferred observability write in flight. A burst of a thousand
#: requests must not spawn a thousand tasks each wanting a connection — which
#: would recreate the exhaustion this indirection exists to prevent.
_writer_busy = False


def _defer(fn, *args) -> None:
    """Run an observability write off the request path, one at a time."""
    global _writer_busy
    if _writer_busy:
        return

    import asyncio

    async def _run():
        global _writer_busy
        _writer_busy = True
        try:
            await asyncio.to_thread(fn, *args)
        except Exception:  # noqa: BLE001
            pass
        finally:
            _writer_busy = False

    try:
        asyncio.get_running_loop().create_task(_run())
    except RuntimeError:
        # No loop (a sync test client, say). Do it inline: correctness of the
        # data matters more than latency in that context, and there is no
        # concurrency to deadlock against.
        _writer_busy = True
        try:
            fn(*args)
        finally:
            _writer_busy = False


def _flush_metrics() -> None:
    db = SessionLocal()
    try:
        collector.flush(db)
    finally:
        db.close()


def _capture_error(request: Request, route: str, exc: BaseException) -> None:
    db = SessionLocal()
    try:
        principal = getattr(request.state, "principal", None)
        ErrorTracker(db).capture(
            exc, route=route, method=request.method,
            tenant_id=principal.tenant_id if principal else None,
            request_id=getattr(request.state, "request_id", None),
        )
        collector.flush(db)
    finally:
        db.close()


@app.middleware("http")
async def request_context_middleware(request: Request, call_next):
    """Give every request an id and bind it to the logger.

    An inbound `X-Request-ID` is honoured so a trace survives a proxy hop, but
    it is length-capped: an unbounded header value would otherwise flow into
    the audit table and the logs.
    """
    from app.services.platform.observability import bind_request, clear_request

    request_id = (request.headers.get("x-request-id") or str(uuid.uuid4()))[:36]
    request.state.request_id = request_id
    bind_request(
        request_id, method=request.method, path=request.url.path,
    )
    try:
        return await call_next(request)
    finally:
        clear_request()


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    """The standard hardening headers.

    HSTS is only sent in production: sending it from a localhost dev server
    pins the developer's browser to https for a host that does not serve it,
    and the only cure is clearing the browser's HSTS store.
    """
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = (
        "geolocation=(), microphone=(), camera=(), payment=()"
    )
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    if settings.is_production:
        response.headers["Strict-Transport-Security"] = (
            "max-age=31536000; includeSubDomains"
        )
    # A JSON API renders nothing, so the strictest possible policy applies.
    # The docs pages need a looser one — they load Swagger from a CDN.
    if not request.url.path.startswith(("/docs", "/redoc")):
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"
        )
    return response


#: Probes and static docs are exempt from the global limit. A health check
#: throttled by a rate limiter reports the service as down.
_UNLIMITED_PATHS = frozenset({
    "/health", "/health/live", "/health/ready", "/metrics",
    "/openapi.json", "/docs", "/redoc",
})


# ===========================================================================
# System endpoints
# ===========================================================================
@app.get("/health", tags=["system"], summary="Health check")
def health() -> dict[str, object]:
    """Module 1's health endpoint, unchanged in shape so nothing that already
    polls it breaks."""
    return {
        "status": "ok",
        "version": settings.APP_VERSION,
        "environment": settings.ENVIRONMENT,
        "auth_enabled": settings.auth_enabled,
    }


@app.get("/health/live", tags=["system"], summary="Liveness probe")
def liveness() -> dict[str, object]:
    """Is the process alive? Touches no dependency.

    A liveness probe that queries the database restarts a perfectly healthy
    application every time the database hiccups.
    """
    db = SessionLocal()
    try:
        from app.services.platform.observability import HealthService

        return HealthService(db).liveness()
    finally:
        db.close()


@app.get("/health/ready", tags=["system"], summary="Readiness probe")
def readiness() -> JSONResponse:
    """Should traffic be routed here? Checks every dependency.

    Returns 503 when a critical check fails, so a load balancer takes the
    instance out of rotation rather than sending it work it cannot do.
    """
    db = SessionLocal()
    try:
        from app.services.platform.observability import HealthService

        report = HealthService(db).readiness()
        payload = {
            "status": report.status,
            "ready": report.ready,
            "version": report.version,
            "environment": report.environment,
            "uptime_seconds": report.uptime_seconds,
            "checks": [
                {
                    "name": c.name, "ok": c.ok, "detail": c.detail,
                    "duration_ms": c.duration_ms, "critical": c.critical,
                }
                for c in report.checks
            ],
        }
        return JSONResponse(
            status_code=(
                status.HTTP_200_OK if report.ready
                else status.HTTP_503_SERVICE_UNAVAILABLE
            ),
            content=payload,
        )
    finally:
        db.close()


@app.get("/metrics", tags=["system"], summary="Metrics")
def metrics() -> dict[str, object]:
    """Aggregate request metrics, unauthenticated but non-sensitive.

    Counts and latencies only — no tenant names, no routes with identifiers in
    them, nothing that identifies a customer. In a real deployment this is
    bound to an internal network; the readiness of the data to be public is
    the second line of defence, not the first.
    """
    db = SessionLocal()
    try:
        collector.flush(db)
        service = MetricsService(db)
        from app.services.platform.jobs.queue import JobQueue

        depth = JobQueue(db).depth()
        return {
            "requests": service.overview(minutes=60),
            "queue": {
                "queued": depth.queued, "running": depth.running,
                "failed": depth.failed, "dead_letter": depth.dead_letter,
                "backlog": depth.backlog, "healthy": depth.is_healthy,
                "p95_duration_ms": depth.p95_duration_ms,
            },
            "version": settings.APP_VERSION,
            "environment": settings.ENVIRONMENT,
        }
    finally:
        db.close()


app.include_router(api_router, prefix=settings.API_V1_PREFIX)
