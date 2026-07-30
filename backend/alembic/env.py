"""Alembic environment.

Two decisions worth stating.

**The database URL comes from `Settings`, never from `alembic.ini`.** Railway
injects `DATABASE_URL` at runtime, and a URL committed to an ini file is both
wrong in production and a credential in source control. The ini keeps a
placeholder so `alembic` still runs standalone; the value is always overridden
here.

**Every model module is imported before `target_metadata` is read.** Alembic
autogenerates from whatever is registered on `Base.metadata` at that moment,
so a module that is not imported produces a migration that silently drops its
tables. That failure mode is quiet and catastrophic, which is why the imports
are explicit and commented rather than left to `app.main` as a side effect.
"""
from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.core.config import settings
from app.db.base import Base

# --- register every table ------------------------------------------------
# Order is irrelevant; completeness is not. Omitting one of these makes
# autogenerate emit a DROP TABLE for it.
from app.models import ai as _ai  # noqa: F401,E402
from app.models import analysis as _analysis  # noqa: F401,E402
from app.models import company as _company  # noqa: F401,E402
from app.models import document as _document  # noqa: F401,E402
from app.models import forecast as _forecast  # noqa: F401,E402
from app.models import platform as _platform  # noqa: F401,E402
from app.models import portfolio as _portfolio  # noqa: F401,E402
from app.models import report as _report  # noqa: F401,E402
from app.models import scoring as _scoring  # noqa: F401,E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# The single source of truth for where the database is.
config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)

target_metadata = Base.metadata


def _include_object(obj, name, type_, reflected, compare_to) -> bool:
    """Keep Alembic's attention on tables this application owns.

    A shared Postgres instance may carry extensions or another service's
    tables; autogenerate would otherwise offer to drop them.
    """
    if type_ == "table" and name in {"spatial_ref_sys", "alembic_version"}:
        return False
    return True


def run_migrations_offline() -> None:
    """Emit SQL without connecting — for review before a production apply."""
    context.configure(
        url=settings.DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        include_object=_include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # Detect column type changes, not just added and dropped columns.
            compare_type=True,
            compare_server_default=True,
            include_object=_include_object,
            # SQLite cannot ALTER most columns in place. Batch mode rebuilds
            # the table instead, so the same migration runs on the SQLite used
            # in development and the Postgres used in production.
            render_as_batch=settings.DATABASE_URL.startswith("sqlite"),
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
