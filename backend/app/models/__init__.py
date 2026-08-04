"""SQLAlchemy model registry.

Import this package before calling :meth:`Base.metadata.create_all` or running
Alembic's metadata autogeneration.  SQLAlchemy registers declarative tables as
a module import side effect; leaving registration to whichever API route was
imported first makes a fresh SQLite database incomplete (for example, missing
``companies``) and fails later with ``no such table``.

The imports are deliberately explicit.  A registry that discovers modules at
runtime makes the schema dependent on filesystem ordering and obscures model
imports from type checkers and packaging tools.
"""
from __future__ import annotations

# noqa declarations keep this module a clear, central list of every model
# module while acknowledging imports are performed for table registration.
from app.models import ai as ai  # noqa: F401
from app.models import analysis as analysis  # noqa: F401
from app.models import company as company  # noqa: F401
from app.models import document as document  # noqa: F401
from app.models import filing_collection as filing_collection  # noqa: F401
from app.models import forecast as forecast  # noqa: F401
from app.models import knowledge as knowledge  # noqa: F401
from app.models import platform as platform  # noqa: F401
from app.models import portfolio as portfolio  # noqa: F401
from app.models import report as report  # noqa: F401
from app.models import scoring as scoring  # noqa: F401
