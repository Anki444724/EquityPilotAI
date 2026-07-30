"""Database backup, verification and restore.

A backup nobody has restored is a hypothesis. This module therefore does three
things rather than one: it takes the backup, it records a checksum, and it can
verify that the artefact still matches that checksum and still opens as a
database. The verification is the part usually left out and the part that
matters at three in the morning.

Two engines, two mechanisms:

* **SQLite** — the online backup API (`Connection.backup`). Consistent while
  the application is writing; a file copy is not.
* **Postgres** — `pg_dump` to a compressed custom-format archive. Shelling out
  rather than reimplementing: `pg_dump` handles large objects, extensions and
  ownership correctly, and a hand-rolled dump will not.

Document bytes are covered by the same backup because Module 7 stores extracted
content in the database and Module 9 stores rendered artefacts as `LargeBinary`
rows. There is no object store to back up separately — a deliberate
simplification, and one that stops the database and the file store drifting
into inconsistency with each other.
"""
from __future__ import annotations

import gzip
import hashlib
import os
import re
import shutil
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy import func, inspect, select, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.platform import BackupRecord
from app.services.platform.observability import get_logger

log = get_logger("ierp.backup")

#: A well-formed SQL identifier. Table names are interpolated into the row
#: count query below because a table name cannot be a bind parameter.
_SAFE_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class BackupError(Exception):
    pass


def _sqlite_path(url: str) -> str:
    """Extract the file path from a SQLAlchemy SQLite URL.

    Fiddlier than it looks, and worth its own function because getting it
    wrong means the backup silently targets the wrong file — or, as the
    original `lstrip("/")` did, converts an absolute path into a relative one:

        sqlite+pysqlite:////var/data/ierp.db   (four slashes = absolute)
            urlparse().path -> "//var/data/ierp.db"
            .lstrip("/")    -> "var/data/ierp.db"   ← now relative, wrong file

    Three slashes mean a relative path, four mean absolute. That distinction
    is the whole convention, and stripping leading slashes destroys it. Found
    by a test that pointed the backup at a tmp directory — the only
    configuration in which an absolute path is used, and precisely the one a
    production deployment uses.
    """
    path = urlparse(url).path
    if path.startswith("//"):
        return path[1:]            # four-slash form: absolute
    return path.lstrip("/") or "ierp.db"


class BackupService:
    def __init__(self, db: Session) -> None:
        self.db = db

    @property
    def directory(self) -> Path:
        path = Path(settings.BACKUP_DIR)
        path.mkdir(parents=True, exist_ok=True)
        return path

    # ==================================================================
    # Create
    # ==================================================================
    def create(self, *, label: str | None = None) -> BackupRecord:
        started = time.perf_counter()
        started_at = _utcnow()
        stamp = started_at.strftime("%Y%m%dT%H%M%SZ")
        name = f"ierp-{label or 'auto'}-{stamp}"

        try:
            if settings.DATABASE_URL.startswith("sqlite"):
                path = self._backup_sqlite(name)
            else:
                path = self._backup_postgres(name)
        except Exception as exc:  # noqa: BLE001
            record = BackupRecord(
                kind="database", location=str(self.directory / name),
                status="failed", error=str(exc)[:2000],
                started_at=started_at, finished_at=_utcnow(),
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
            )
            self.db.add(record)
            self.db.commit()
            self.db.refresh(record)
            log.error("backup failed", error=str(exc))
            raise BackupError(str(exc)) from exc

        checksum = self._checksum(path)
        tables, rows = self._inventory()

        record = BackupRecord(
            kind="database",
            location=str(path),
            size_bytes=path.stat().st_size,
            checksum=checksum,
            table_count=tables,
            row_count=rows,
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
            status="succeeded",
            started_at=started_at,
            finished_at=_utcnow(),
        )
        self.db.add(record)
        self.db.commit()
        self.db.refresh(record)

        log.info(
            "backup created", location=str(path),
            size_bytes=record.size_bytes, tables=tables, rows=rows,
            ms=record.duration_ms,
        )
        self.prune()
        return record

    def _backup_sqlite(self, name: str) -> Path:
        """Online backup, then gzip.

        `sqlite3.Connection.backup` copies page by page while holding the
        appropriate locks, so the result is consistent even under concurrent
        writes. Copying the file with `cp` is not, and produces an archive
        that restores to a corrupt database exactly when it is needed.
        """
        source_path = _sqlite_path(settings.DATABASE_URL)
        if not Path(source_path).exists():
            raise BackupError(f"database file not found: {source_path}")

        staging = self.directory / f"{name}.sqlite"
        source = sqlite3.connect(source_path)
        target = sqlite3.connect(staging)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()

        final = self.directory / f"{name}.sqlite.gz"
        with open(staging, "rb") as raw, gzip.open(final, "wb", compresslevel=6) as out:
            shutil.copyfileobj(raw, out)
        staging.unlink(missing_ok=True)
        return final

    def _backup_postgres(self, name: str) -> Path:
        """`pg_dump -Fc`: compressed, and restorable table by table."""
        if shutil.which("pg_dump") is None:
            raise BackupError("pg_dump is not on PATH")

        final = self.directory / f"{name}.dump"
        dsn = settings.DATABASE_URL.replace("postgresql+psycopg", "postgresql")
        result = subprocess.run(
            ["pg_dump", "--format=custom", "--compress=6", "--file", str(final), dsn],
            capture_output=True, text=True, timeout=1800,
        )
        if result.returncode != 0:
            raise BackupError(f"pg_dump failed: {result.stderr[:500]}")
        return final

    @staticmethod
    def _checksum(path: Path) -> str:
        """SHA-256, streamed. A multi-gigabyte dump must not be read into
        memory to be hashed."""
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def _inventory(self) -> tuple[int, int]:
        """Table and row counts, recorded so a restore can be sanity-checked
        against what was taken."""
        try:
            inspector = inspect(self.db.get_bind())
            tables = inspector.get_table_names()
            total = 0
            for table in tables:
                # The name comes from SQLAlchemy's own inspector, not from a
                # request, so this is not reachable injection. The identifier
                # check is defence in depth and costs one regex per table: it
                # makes the safety a property of the code rather than of a
                # chain of reasoning about where the name came from.
                if not _SAFE_IDENTIFIER.fullmatch(table):
                    continue
                try:
                    total += int(self.db.scalar(
                        text(f'SELECT COUNT(*) FROM "{table}"')
                    ) or 0)
                except Exception:  # noqa: BLE001 — a view or a lock; keep going
                    continue
            return len(tables), total
        except Exception:  # noqa: BLE001
            return 0, 0

    # ==================================================================
    # Verify
    # ==================================================================
    def verify(self, record: BackupRecord) -> tuple[bool, str]:
        """Confirm the artefact exists, still hashes the same, and opens.

        Three checks because they fail differently: a missing file is a
        storage problem, a checksum mismatch is corruption or tampering, and
        a file that hashes correctly but will not open means the backup was
        never valid.
        """
        path = Path(record.location)
        if not path.is_file():
            return False, "artefact missing"

        if record.checksum and self._checksum(path) != record.checksum:
            return False, "checksum mismatch — the artefact has changed"

        if path.suffixes[-2:] == [".sqlite", ".gz"]:
            try:
                staging = path.with_suffix("")
                with gzip.open(path, "rb") as raw, open(staging, "wb") as out:
                    shutil.copyfileobj(raw, out)
                connection = sqlite3.connect(staging)
                try:
                    result = connection.execute("PRAGMA integrity_check").fetchone()
                    tables = connection.execute(
                        "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
                    ).fetchone()[0]
                finally:
                    connection.close()
                staging.unlink(missing_ok=True)
                if result and result[0] != "ok":
                    return False, f"integrity check failed: {result[0]}"
                if record.table_count and tables < record.table_count:
                    return False, (
                        f"restored {tables} tables, expected {record.table_count}"
                    )
            except Exception as exc:  # noqa: BLE001
                return False, f"cannot open the archive: {exc}"

        record.verified_at = _utcnow()
        self.db.commit()
        return True, "verified"

    # ==================================================================
    # Restore
    # ==================================================================
    def restore_command(self, record: BackupRecord) -> str:
        """The exact command an operator runs.

        Restore is deliberately *not* an API call. A one-click restore is a
        one-click way to destroy a production database, and the safety comes
        from the step being manual and deliberate. What the platform owes the
        operator is the precise command, not the button.
        """
        path = Path(record.location)
        if path.suffixes[-2:] == [".sqlite", ".gz"]:
            return (
                f"gunzip -c {path} > restored.db && "
                f"sqlite3 restored.db 'PRAGMA integrity_check;' && "
                f"mv restored.db ierp.db"
            )
        return (
            f"pg_restore --clean --if-exists --no-owner "
            f"--dbname \"$DATABASE_URL\" {path}"
        )

    # ==================================================================
    # Housekeeping
    # ==================================================================
    def prune(self) -> int:
        """Keep the most recent N successful backups.

        Retention by count rather than by age: an instance that was down for a
        fortnight should not wake up and delete its only copies.
        """
        keep = max(1, settings.BACKUP_RETENTION_COUNT)
        records = list(self.db.scalars(
            select(BackupRecord)
            .where(BackupRecord.status == "succeeded")
            .order_by(BackupRecord.finished_at.desc())
        ))
        removed = 0
        for record in records[keep:]:
            Path(record.location).unlink(missing_ok=True)
            self.db.delete(record)
            removed += 1
        if removed:
            self.db.commit()
        return removed

    def list(self, *, limit: int = 30) -> list[BackupRecord]:
        return list(self.db.scalars(
            select(BackupRecord)
            .order_by(BackupRecord.finished_at.desc())
            .limit(limit)
        ))

    def status(self) -> dict[str, object]:
        """The backup panel: is there a recent, verified copy?"""
        latest = self.db.scalar(
            select(BackupRecord)
            .where(BackupRecord.status == "succeeded")
            .order_by(BackupRecord.finished_at.desc())
            .limit(1)
        )
        total = self.db.scalar(select(func.count(BackupRecord.id))) or 0

        age_hours = None
        if latest is not None and latest.finished_at is not None:
            finished = latest.finished_at
            if finished.tzinfo is None:
                finished = finished.replace(tzinfo=timezone.utc)
            age_hours = round((_utcnow() - finished).total_seconds() / 3600, 2)

        return {
            "configured": True,
            "directory": str(self.directory),
            "backup_count": int(total),
            "latest_at": latest.finished_at.isoformat() if latest and latest.finished_at else None,
            "latest_size_bytes": latest.size_bytes if latest else 0,
            "latest_verified_at": (
                latest.verified_at.isoformat() if latest and latest.verified_at else None
            ),
            "age_hours": age_hours,
            # A backup older than 48 hours is stale for a daily schedule; the
            # threshold is stated once here so the dashboard and any alerting
            # agree on what "stale" means.
            "stale": age_hours is None or age_hours > 48,
            "retention_count": settings.BACKUP_RETENTION_COUNT,
        }
