"""SQLAlchemy engine, session factory and declarative base."""
from __future__ import annotations

from collections.abc import Generator
from datetime import datetime, timezone

from sqlalchemy import DateTime, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from app.core.config import settings


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    """Declarative base with audit timestamps on every table."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )


_IS_SQLITE = settings.DATABASE_URL.startswith("sqlite")

_connect_args = (
    {
        # FastAPI runs sync endpoints on a threadpool, so a pooled connection
        # is used from more than one thread over its life.
        "check_same_thread": False,
        # Wait rather than raise immediately when another connection holds the
        # write lock. Without this a concurrent write is an instant
        # "database is locked" error rather than a brief wait.
        "timeout": 30,
    }
    if _IS_SQLITE else {}
)

def _pool_options() -> dict:
    """Connection-pool sizing.

    SQLite ignores pooling (one file, one writer), so only the server engines
    are configured.

    The defaults SQLAlchemy ships — five connections plus ten overflow — are
    sized for a script, not a web service. A load test at concurrency 25
    exhausted them and every subsequent checkout blocked for the full
    thirty-second timeout, so the process stopped answering entirely. The
    numbers below are deliberate:

    * `pool_size=20` covers the steady state of a single instance.
    * `max_overflow=20` absorbs a burst without unbounded growth. Total 40 per
      process; Postgres defaults to 100 connections, so two or three replicas
      fit comfortably with headroom for psql and pg_dump.
    * `pool_timeout=10` fails fast. Waiting thirty seconds for a connection
      turns a capacity problem into a total outage, because the caller has
      given up long before and the queue only grows. A 500 after ten seconds
      is a bad response; a hung worker is no response at all.
    * `pool_recycle=1800` pre-empts the idle-connection timeouts that managed
      Postgres services and connection poolers impose.
    """
    if _IS_SQLITE:
        if ":memory:" in settings.DATABASE_URL:
            # An in-memory database exists only within its connection, so it
            # must not be pooled across several. The test fixtures supply
            # StaticPool for exactly this reason.
            return {}
        # SQLite is pooled too. Leaving it on SQLAlchemy's default of five
        # plus ten looked harmless — "SQLite has one writer anyway" — and was
        # not: FastAPI serves sync endpoints from a forty-thread pool, so
        # forty concurrent requests each want a connection. A load test at
        # concurrency 25 exhausted the pool, and every further checkout
        # blocked for the full thirty-second timeout.
        return {
            "pool_size": 20,
            "max_overflow": 30,
            "pool_timeout": 10,
            "pool_recycle": 1800,
        }
    return {
        "pool_size": 20,
        "max_overflow": 20,
        "pool_timeout": 10,
        "pool_recycle": 1800,
    }


engine = create_engine(
    settings.DATABASE_URL,
    echo=settings.DB_ECHO,
    # Verifies a pooled connection before handing it out, so a database
    # restart or a dropped idle connection surfaces as a reconnect rather than
    # as a failed request.
    pool_pre_ping=True,
    connect_args=_connect_args,
    **_pool_options(),
)

if _IS_SQLITE:
    from sqlalchemy import event

    @event.listens_for(engine, "connect")
    def _configure_sqlite(dbapi_connection, _record) -> None:
        """Put SQLite into write-ahead logging.

        The default rollback journal takes a **database-wide exclusive lock
        for the duration of every write**, so a single writer blocks every
        reader. That is tolerable for a CLI tool and fatal for a web service:
        a load test at concurrency 25 stopped the process answering at all,
        because the metrics flush wrote every few seconds and each write froze
        every request in flight — including `/health`.

        WAL lets readers continue against the last committed snapshot while a
        writer appends. Writers still serialise with one another, which is
        SQLite's design and the reason Postgres is the production target, but
        reads no longer queue behind them.

        `synchronous=NORMAL` is the standard companion to WAL: durable across
        an application crash, with a theoretical risk of losing the last
        transactions in a power failure. For a development and demo database
        that is the right trade; production runs Postgres, where none of this
        applies.
        """
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        # Enforce declared foreign keys — SQLite ignores them unless asked,
        # so an ON DELETE CASCADE is silently a no-op without this.
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Generator:
    """FastAPI dependency yielding a scoped session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
