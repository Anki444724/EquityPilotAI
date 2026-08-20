"""Deduplicate companies and re-assert the canonical (ticker, exchange) identity.

Revision ID: 9f0b5e8c2d71
Revises: 164253079db3
Create Date: 2026-08-20

**Why this exists.** The production database holds duplicate ``companies``
rows for the same ticker (e.g. J&KBANK, M&M, M&MFIN, MINDACORP): one row owns
the financial history, its twin owns nothing. Every ticker lookup that fed a
single row into ``Session.scalar()`` picked an *arbitrary* twin, so the
financials backfill kept writing into one row while
``companies_without_financials()`` kept — correctly — reporting the other.

**What this migration does, in order.**

1. *Backs up first.* The entire ``companies`` table is copied to
   ``companies_pre_merge_backup`` before anything is touched. Every dependent
   row that belongs to a duplicate id is copied into
   ``company_merge_backup_<table>``. Nothing is lost; nothing is destructive
   until all dependent rows are accounted for.
2. *Groups duplicates.* A group is every row sharing ``upper(ticker)`` within
   a venue family: Indian venues (NSE/BSE/NSE+BSE) form one market and are
   grouped across exchanges; foreign venues group per (ticker, exchange).
3. *Picks the canonical row per group.* The row that owns the financial
   history wins (most facts, then most fiscal years), then the oldest, then
   the smallest id — deterministic and identical for every run. This is
   exactly the rule the bug report requires: "keep the company ID that
   already owns the financial history".
4. *Migrates every dependent foreign key* from the duplicates to the
   canonical row, table by table, respecting each table's unique constraints
   (conflict-free moves, version re-sequencing for versioned tables). On
   Postgres the referencing tables are discovered live from ``pg_catalog`` so
   a future table cannot be skipped; SQLite uses the model-derived list.
5. *Guards financial history with a hard invariant.* After every move the
   migration verifies that ``financial_facts`` still references the duplicate
   ids **zero times** — if any fact could not be moved without conflicting
   with the canonical row, the migration aborts (with everything backed up)
   rather than deleting or overwriting a single financial fact.
6. *Deletes the duplicate rows* only after every dependent reference has been
   migrated and verified, and records each merge in ``company_merge_log``.
7. *Re-asserts the database-level identity.* ``uq_company_ticker_exchange``
   is dropped and re-created (so the constraint is enforced even on a
   database where it was lost), and the case-insensitive guard index
   ``uq_companies_exchange_ticker_ci`` on ``(exchange, upper(ticker))`` is
   added. From here on, a check-then-insert race can no longer produce a
   second company row.

**Downgrade.** A faithful automatic reversal is not possible (dependent rows
have been merged). Downgrade refuses to guess; restoration is from the backup
tables, whose names are printed in the error.
"""
from __future__ import annotations

from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op
from sqlalchemy import bindparam

revision = "9f0b5e8c2d71"
down_revision = "164253079db3"
branch_labels = None
depends_on = None

#: Indian venues are one canonical market: the platform ingests every Indian
#: company under its NSE symbol, so two rows for one ticker that differ only
#: in exchange are the same security and must merge into one row.
INDIAN_EXCHANGES = ("NSE", "BSE", "NSE/BSE")

#: Metadata columns merged with COALESCE (canonical keeps its own value).
#: `isin` is handled separately — it is unique across the whole table, so
#: adopting the duplicate's ISIN needs a cross-row guard, not a COALESCE.
COALESCE_COLUMNS = (
    "name", "sector", "industry", "bse_code",
    "market_cap_category", "index_membership", "currency",
    "reporting_scale", "market_cap", "current_price", "shares_outstanding",
    "description", "website", "incorporated_year", "face_value",
    "listing_date", "ceo", "employees", "headquarters", "logo_url",
    "favicon_url",
)

#: Referencing tables whose unique key is exactly (company_id, version):
#: versions are re-sequenced above the canonical row's maximum, which makes
#: every row movable without conflict and loses no history.
VERSION_TABLES = {
    "ai_score_versions", "company_versions", "financial_fact_versions",
}

#: Referencing tables whose unique key is company_id alone: the duplicate's
#: row moves only when the canonical row has none; otherwise it is a
#: redundant twin and stays behind (backed up, then removed with the
#: duplicate by the foreign key cascade).
SINGLE_TABLES = {
    "company_crawl_state", "data_quality_snapshots",
}

#: Fallback for SQLite (development). Postgres discovers the referencing
#: tables and their unique keys live from the catalog; this list is the model
#: snapshot and is asserted against the models by the migration test.
#: (table, fk_column, [unique-key column tuples]; () == no unique key)
SQLITE_REFERENCING: dict[str, tuple[tuple[str, ...], ...]] = {
    "ai_analyses": (),
    "ai_overrides": (),
    "ai_score_versions": (("company_id", "version"),),
    "company_crawl_state": (("company_id",),),
    "company_versions": (("company_id", "version"),),
    "corporate_actions": (),
    "credit_ratings": (),
    "data_quality_snapshots": (("company_id",),),
    "debt_instruments": (),
    "discovered_filings": (),
    "document_summaries": (),
    "documents": (("company_id", "content_hash"),),
    "financial_fact_versions": (("company_id", "version"),),
    "financial_facts": (
        ("company_id", "fiscal_year", "line_item", "precedence"),
    ),
    "forecasts": (),
    "knowledge_entries": (("company_id", "section", "key", "version"),),
    "market_overrides": (),
    "portfolio_transactions": (),
    "quarterly_results": (("company_id", "fiscal_year", "quarter"),),
    "reports": (("company_id", "report_type", "version"),),
    "score_snapshots": (("company_id", "as_of", "profile_key"),),
    "shareholding_snapshots": (("company_id", "fiscal_year", "quarter"),),
    "watchlist_entries": (),
    "yearly_observations": (("company_id", "fiscal_year", "version"),),
}


def _q(ident: str) -> str:
    """Quote a SQL identifier defensively (identifiers come from pg_catalog)."""
    return '"' + ident.replace('"', '""') + '"'


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# Catalog introspection (Postgres) vs model snapshot (SQLite)
# --------------------------------------------------------------------------

def _referencing_tables(bind: sa.Connection) -> list[tuple[str, str]]:
    """[(table, fk_column)] for every FK referencing companies.id."""
    if bind.dialect.name == "postgresql":
        rows = bind.execute(sa.text(
            "SELECT con.conrelid::regclass::text AS tbl, "
            "       a.attname AS col "
            "FROM pg_constraint con "
            "JOIN pg_attribute a "
            "  ON a.attrelid = con.conrelid AND a.attnum = con.conkey[1] "
            "WHERE con.contype = 'f' "
            "  AND con.confrelid = 'companies'::regclass "
            "ORDER BY tbl"
        )).all()
        return [(str(t), str(c)) for t, c in rows]
    return [(table, "company_id") for table in SQLITE_REFERENCING]


def _unique_keys(bind: sa.Connection, table: str) -> list[tuple[str, ...]]:
    """Unique-key column tuples of `table` that contain company_id."""
    if bind.dialect.name == "postgresql":
        rows = bind.execute(sa.text(
            "SELECT con.conkey FROM pg_constraint con "
            "WHERE con.conrelid = CAST(:tbl AS regclass) "
            "AND con.contype IN ('u', 'p') "
            "UNION "
            "SELECT i.indkey FROM pg_index i "
            "WHERE i.indrelid = CAST(:tbl2 AS regclass) AND i.indisunique"
        ), {"tbl": table, "tbl2": table}).all()
        col_names: dict[int, str] = {
            r.attnum: r.attname for r in bind.execute(sa.text(
                "SELECT attnum, attname FROM pg_attribute "
                "WHERE attrelid = CAST(:tbl AS regclass) AND attnum > 0 "
                "AND NOT attisdropped"
            ), {"tbl": table}).all()
        }
        keys: list[tuple[str, ...]] = []
        for (raw,) in rows:
            # conkey is smallint[] (a list); indkey is int2vector (a string
            # like "1 2"). Normalise both to a tuple of column names.
            if isinstance(raw, str):
                nums = [int(n) for n in raw.split()]
            else:
                nums = [int(n) for n in raw]
            keys.append(tuple(col_names[n] for n in nums if n in col_names))
        keys = [k for k in keys if "company_id" in k]
        # de-dup (a constraint and its backing index both report the key)
        return sorted({k for k in keys}, key=len)
    return list(SQLITE_REFERENCING.get(table, ()))


# --------------------------------------------------------------------------
# Backup and audit tables
# --------------------------------------------------------------------------

def _create_audit_tables(bind: sa.Connection) -> None:
    bind.execute(sa.text(
        "CREATE TABLE IF NOT EXISTS companies_pre_merge_backup "
        "AS SELECT * FROM companies"
    ))
    bind.execute(sa.text(
        "CREATE TABLE IF NOT EXISTS company_merge_log ("
        "  merged_at TIMESTAMP NOT NULL,"
        "  dup_id VARCHAR(36) NOT NULL,"
        "  canonical_id VARCHAR(36) NOT NULL,"
        "  ticker VARCHAR(32) NOT NULL,"
        "  subject VARCHAR(200) NOT NULL,"
        "  dup_facts INTEGER,"
        "  canonical_facts INTEGER,"
        "  moved INTEGER,"
        "  conflicting INTEGER,"
        "  note VARCHAR(500),"
        "  PRIMARY KEY (dup_id, subject)"
        ")"
    ))


def _backup_child_rows(
    bind: sa.Connection, table: str, col: str, dup_ids: list[str],
) -> None:
    backup = f"company_merge_backup_{table}"
    bind.execute(sa.text(
        f"CREATE TABLE IF NOT EXISTS {_q(backup)} "
        f"AS SELECT * FROM {_q(table)} WHERE 1 = 0"
    ))
    bind.execute(
        sa.text(
            f"INSERT INTO {_q(backup)} SELECT * FROM {_q(table)} "
            f"WHERE {_q(col)} IN :ids"
        ).bindparams(bindparam("ids", expanding=True)),
        {"ids": tuple(dup_ids)},
    )


def _log(bind: sa.Connection, *, dup_id: str, canonical_id: str, ticker: str,
         subject: str, dup_facts: int = 0, canonical_facts: int = 0,
         moved: int = 0, conflicting: int = 0, note: str = "") -> None:
    bind.execute(sa.text(
        "INSERT INTO company_merge_log (merged_at, dup_id, canonical_id, "
        "ticker, subject, dup_facts, canonical_facts, moved, conflicting, note) "
        "VALUES (:at, :dup, :can, :ticker, :subject, :dup_facts, "
        ":can_facts, :moved, :conflicting, :note)"
    ), {
        "at": _utcnow(), "dup": dup_id, "can": canonical_id, "ticker": ticker,
        "subject": subject, "dup_facts": dup_facts, "can_facts": canonical_facts,
        "moved": moved, "conflicting": conflicting, "note": note,
    })


# --------------------------------------------------------------------------
# Grouping, canonical pick, metadata merge
# --------------------------------------------------------------------------

def _duplicate_groups(bind: sa.Connection) -> list[list[dict]]:
    """Rows grouped by (upper(ticker)) within a venue family, count > 1."""
    rows = bind.execute(sa.text(
        "SELECT id, ticker, exchange, created_at FROM companies"
    )).mappings().all()

    groups: dict[tuple, list[dict]] = {}
    for row in rows:
        exchange = (row["exchange"] or "").strip().upper()
        if exchange in INDIAN_EXCHANGES:
            key = ("IN", str(row["ticker"]).strip().upper())
        else:
            key = ("EX", str(row["ticker"]).strip().upper(), exchange)
        groups.setdefault(key, []).append(dict(row))

    return [group for group in groups.values() if len(group) > 1]


def _fact_stats(bind: sa.Connection, ids: list[str]) -> dict[str, tuple[int, int]]:
    if not ids:
        return {}
    rows = bind.execute(
        sa.text(
            "SELECT company_id, COUNT(*) AS n, COUNT(DISTINCT fiscal_year) AS y "
            "FROM financial_facts WHERE company_id IN :ids GROUP BY company_id"
        ).bindparams(bindparam("ids", expanding=True)),
        {"ids": tuple(ids)},
    ).all()
    return {str(r[0]): (int(r[1]), int(r[2])) for r in rows}


def _pick_canonical(group: list[dict],
                    stats: dict[str, tuple[int, int]]) -> dict:
    """Most facts, then most fiscal years, then oldest, then smallest id."""

    def key(row: dict) -> tuple:
        n, y = stats.get(str(row["id"]), (0, 0))
        return (-y, -n, row["created_at"] is None, row["created_at"], str(row["id"]))

    return min(group, key=key)


def _transfer_isin(bind: sa.Connection, canonical_id: str, dup_id: str) -> None:
    """Hand a unique ISIN from the duplicate to the canonical row, safely.

    ``companies.isin`` is UNIQUE across the whole table (``companies_isin_key``
    on Postgres). Assigning the duplicate's ISIN to the canonical row while the
    duplicate still owns it violates that constraint the moment the UPDATE
    touches the row — Postgres checks a plain UNIQUE constraint per row, not at
    commit — which is what production hit:

        duplicate key value violates unique constraint "companies_isin_key"
        DETAIL: Key (isin)=(INE101A01026) already exists.

    The ISIN is therefore *released from the duplicate first* and only then
    adopted by the canonical row. Both statements run on the migration's own
    connection inside its transaction, so no other session ever observes the
    intermediate state, and the duplicate row is deleted moments later anyway.

    Generic by construction: it reads whatever ISIN the pair happens to hold,
    so it applies to every duplicate group, not to one ticker.

    Idempotent: once the canonical row owns an ISIN the function returns
    without writing, so re-running the migration step is a no-op.

    Conservative: if a *third* company already holds that ISIN, neither row is
    touched. The canonical row keeps its NULL, the duplicate keeps its value
    for the backup tables, and the merge continues rather than aborting.
    """
    row = bind.execute(sa.text(
        "SELECT (SELECT isin FROM companies WHERE id = :can) AS canonical_isin,"
        "       (SELECT isin FROM companies WHERE id = :dup) AS duplicate_isin"
    ), {"can": canonical_id, "dup": dup_id}).one()
    canonical_isin, duplicate_isin = row[0], row[1]

    # Canonical already identified, or nothing to hand over: leave both rows.
    if canonical_isin is not None or duplicate_isin is None:
        return

    held_elsewhere = bind.execute(sa.text(
        "SELECT 1 FROM companies "
        "WHERE isin = :isin AND id <> :dup AND id <> :can LIMIT 1"
    ), {"isin": duplicate_isin, "dup": dup_id, "can": canonical_id}).scalar()
    if held_elsewhere:
        return

    # Release, then adopt. Order is the whole point of this function.
    bind.execute(sa.text(
        "UPDATE companies SET isin = NULL WHERE id = :dup AND isin = :isin"
    ), {"dup": dup_id, "isin": duplicate_isin})
    bind.execute(sa.text(
        "UPDATE companies SET isin = :isin WHERE id = :can AND isin IS NULL"
    ), {"can": canonical_id, "isin": duplicate_isin})


def _merge_metadata(bind: sa.Connection, canonical_id: str, dup_id: str) -> None:
    # The unique ISIN moves before the metadata merge runs, so the CASE below
    # can no longer be the statement that adopts a live ISIN: by the time it
    # evaluates, the canonical row either already owns the ISIN (and keeps its
    # own value) or the duplicate's ISIN is NULL. The expression is left in
    # place unchanged as the guard it always was.
    _transfer_isin(bind, canonical_id, dup_id)

    sets = ", ".join(
        f"{_q(c)} = COALESCE(companies.{_q(c)}, d.{_q(c)})"
        for c in COALESCE_COLUMNS
    )
    # ISIN is unique across the whole table: only adopt the duplicate's when
    # the canonical row has none and no *other* row holds it either.
    sets += ", isin = CASE"
    sets += " WHEN companies.isin IS NOT NULL THEN companies.isin"
    sets += " WHEN NOT EXISTS (SELECT 1 FROM companies o WHERE o.isin = d.isin"
    sets += "                  AND o.id <> :dup AND o.id <> :can) THEN d.isin"
    sets += " ELSE companies.isin END"
    # listing_status: active wins; deleted_at: an active row wins.
    sets += (", listing_status = CASE WHEN companies.listing_status = 'active' "
             "OR d.listing_status = 'active' THEN 'active' "
             "ELSE companies.listing_status END")
    sets += (", deleted_at = CASE WHEN companies.deleted_at IS NULL "
             "OR d.deleted_at IS NULL THEN NULL ELSE companies.deleted_at END")
    sets += ", data_version = companies.data_version + 1"

    bind.execute(sa.text(
        f"UPDATE companies SET {sets} "
        f"FROM (SELECT * FROM companies WHERE id = :dup) AS d "
        f"WHERE companies.id = :can"
    ), {"dup": dup_id, "can": canonical_id})


def _normalise_indian_exchange(bind: sa.Connection, canonical_id: str) -> None:
    """After the duplicate is gone, one Indian row per ticker — venue NSE.

    Deliberately a separate statement that runs *after* the duplicate row is
    deleted: normalising while both rows exist could transiently collide with
    uq_company_ticker_exchange. With the duplicate removed there is no other
    row in the Indian family for this ticker, so the update cannot conflict.
    """
    bind.execute(sa.text(
        "UPDATE companies SET exchange = 'NSE' WHERE id = :can"
    ), {"can": canonical_id})


# --------------------------------------------------------------------------
# Dependent-row migration
# --------------------------------------------------------------------------

def _column_equality(cols: tuple[str, ...], left_alias: str,
                     right_table: str) -> str:
    """x.c IS NOT DISTINCT FROM "table".c for every non-id key column."""
    return " AND ".join(
        f"{_q(left_alias)}.{_q(c)} IS NOT DISTINCT FROM "
        f"{_q(right_table)}.{_q(c)}"
        for c in cols if c != "company_id"
    )


def _move_dependent_rows(bind: sa.Connection, table: str, col: str,
                         dup_id: str, canonical_id: str) -> tuple[int, int]:
    """Move rows that belong to dup_id to canonical_id, conflict-free.

    Returns (moved, conflicting-left-behind). Left-behind rows are redundant
    twins (the canonical row already holds the same unique key); they are
    backed up in company_merge_backup_<table> and removed with the duplicate
    company row by the FK cascade.
    """
    keys = _unique_keys(bind, table)
    tq = _q(table)

    if table in VERSION_TABLES:
        # Re-sequence dup versions above the canonical maximum so every row
        # moves and no history is lost.
        max_version = bind.execute(sa.text(
            f"SELECT COALESCE(MAX(version), 0) FROM {tq} "
            f"WHERE {_q(col)} = :can"
        ), {"can": canonical_id}).scalar() or 0
        bind.execute(sa.text(
            f"UPDATE {tq} SET version = version + :off WHERE {_q(col)} = :dup"
        ), {"off": int(max_version), "dup": dup_id})
        moved = bind.execute(sa.text(
            f"UPDATE {tq} SET {_q(col)} = :can WHERE {_q(col)} = :dup"
        ), {"can": canonical_id, "dup": dup_id}).rowcount
        return int(moved or 0), 0

    if any(set(k) == {col} for k in keys):
        # company_id alone is unique: the dup's row moves only if the
        # canonical row has none.
        before = bind.execute(sa.text(
            f"SELECT COUNT(*) FROM {tq} WHERE {_q(col)} = :dup"
        ), {"dup": dup_id}).scalar() or 0
        bind.execute(sa.text(
            f"UPDATE {tq} SET {_q(col)} = :can WHERE {_q(col)} = :dup "
            f"AND NOT EXISTS (SELECT 1 FROM {tq} x WHERE x.{_q(col)} = :can2)"
        ), {"can": canonical_id, "can2": canonical_id, "dup": dup_id})
        after = bind.execute(sa.text(
            f"SELECT COUNT(*) FROM {tq} WHERE {_q(col)} = :dup"
        ), {"dup": dup_id}).scalar() or 0
        return int(before) - int(after), int(after)

    if not keys:
        moved = bind.execute(sa.text(
            f"UPDATE {tq} SET {_q(col)} = :can WHERE {_q(col)} = :dup"
        ), {"can": canonical_id, "dup": dup_id}).rowcount
        return int(moved or 0), 0

    # Conflict-free move against every unique key containing company_id.
    guards = " AND ".join(
        f"NOT EXISTS (SELECT 1 FROM {tq} x "
        f"WHERE x.{_q(col)} = :can AND {_column_equality(k, 'x', table)})"
        for k in keys
    )
    before = bind.execute(sa.text(
        f"SELECT COUNT(*) FROM {tq} WHERE {_q(col)} = :dup"
    ), {"dup": dup_id}).scalar() or 0
    bind.execute(sa.text(
        f"UPDATE {tq} SET {_q(col)} = :can WHERE {_q(col)} = :dup "
        f"AND {guards}"
    ), {"can": canonical_id, "dup": dup_id})
    after = bind.execute(sa.text(
        f"SELECT COUNT(*) FROM {tq} WHERE {_q(col)} = :dup"
    ), {"dup": dup_id}).scalar() or 0
    return int(before) - int(after), int(after)


def _financial_facts_invariant(bind: sa.Connection, dup_ids: list[str]) -> None:
    """Financial history is never deleted or overwritten by this migration."""
    leftover = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM financial_facts WHERE company_id IN :ids"
        ).bindparams(bindparam("ids", expanding=True)),
        {"ids": tuple(dup_ids)},
    ).scalar() or 0
    if leftover:
        raise RuntimeError(
            f"refusing to merge: {leftover} financial fact(s) still reference "
            "duplicate company id(s) and cannot be moved without conflicting "
            "with the canonical row. The facts are intact; restore the state "
            "and resolve the conflict manually before re-running."
        )


def _purge_leftovers(bind: sa.Connection, table: str, col: str,
                     dup_id: str) -> int:
    """Remove rows that could not be moved because the canonical row already
    holds the same unique key. These are redundant twins: every one was
    copied into company_merge_backup_<table> before anything moved, so the
    purge is explicit, auditable and lossless rather than an implicit
    foreign-key cascade side effect."""
    tq = _q(table)
    return int(bind.execute(sa.text(
        f"DELETE FROM {tq} WHERE {_q(col)} = :dup"
    ), {"dup": dup_id}).rowcount or 0)


# --------------------------------------------------------------------------
# upgrade / downgrade
# --------------------------------------------------------------------------

def upgrade() -> None:
    bind = op.get_bind()

    # 1. Safety: full companies snapshot + audit log before anything moves.
    _create_audit_tables(bind)

    referencing = _referencing_tables(bind)
    groups = _duplicate_groups(bind)

    for group in groups:
        ids = [str(r["id"]) for r in group]
        stats = _fact_stats(bind, ids)
        canonical = _pick_canonical(group, stats)
        canonical_id = str(canonical["id"])
        ticker = str(canonical["ticker"])
        indian_group = (canonical["exchange"] or "").strip().upper() in (
            INDIAN_EXCHANGES
        )

        for row in group:
            if str(row["id"]) == canonical_id:
                continue
            dup_id = str(row["id"])

            # 2. Back up every dependent row of this duplicate.
            for table, col in referencing:
                _backup_child_rows(bind, table, col, [dup_id])

            # 3. Migrate dependent rows, conflict-free, table by table.
            for table, col in referencing:
                moved, conflicting = _move_dependent_rows(
                    bind, table, col, dup_id, canonical_id,
                )
                if moved or conflicting:
                    _log(
                        bind, dup_id=dup_id, canonical_id=canonical_id,
                        ticker=ticker, subject=f"table:{table}",
                        moved=moved, conflicting=conflicting,
                        note=("conflicting rows are redundant twins kept in "
                              "company_merge_backup_" + table)
                        if conflicting else "",
                    )

            # 4. Hard invariant: no financial fact may be stranded.
            _financial_facts_invariant(bind, [dup_id])

            # 4b. Redundant twins that could not move (the canonical row
            #     already holds the same unique key) are purged explicitly —
            #     each one is backed up, and the merge log records the count.
            for table, col in referencing:
                if table == "financial_facts":
                    continue
                purged = _purge_leftovers(bind, table, col, dup_id)
                if purged:
                    _log(
                        bind, dup_id=dup_id, canonical_id=canonical_id,
                        ticker=ticker, subject=f"purged:{table}",
                        conflicting=purged,
                        note="redundant twin rows removed after backup to "
                             "company_merge_backup_" + table,
                    )

            # 5. Merge metadata, then remove the duplicate only after every
            #    dependent reference has been migrated and verified.
            _merge_metadata(bind, canonical_id, dup_id)
            dup_facts, _ = stats.get(dup_id, (0, 0))
            can_facts, _ = stats.get(canonical_id, (0, 0))
            bind.execute(sa.text(
                "DELETE FROM companies WHERE id = :dup"
            ), {"dup": dup_id})
            if indian_group:
                _normalise_indian_exchange(bind, canonical_id)
            _log(
                bind, dup_id=dup_id, canonical_id=canonical_id, ticker=ticker,
                subject="companies", dup_facts=dup_facts,
                canonical_facts=can_facts,
                note="duplicate company row removed after dependent rows "
                     "migrated and verified",
            )

    # 6. Re-assert the database-level identity so the canonical rule is
    #    enforced even where the constraint was lost.
    if bind.dialect.name == "postgresql":
        bind.execute(sa.text(
            "ALTER TABLE companies DROP CONSTRAINT IF EXISTS "
            "uq_company_ticker_exchange"
        ))
        bind.execute(sa.text(
            "DROP INDEX IF EXISTS uq_company_ticker_exchange"
        ))
        bind.execute(sa.text(
            "ALTER TABLE companies ADD CONSTRAINT uq_company_ticker_exchange "
            "UNIQUE (ticker, exchange)"
        ))
    bind.execute(sa.text(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_companies_exchange_ticker_ci "
        "ON companies (exchange, upper(ticker))"
    ))


def downgrade() -> None:
    raise RuntimeError(
        "company deduplication cannot be reversed automatically: dependent "
        "rows were merged into the canonical company ids. Restore manually "
        "from companies_pre_merge_backup and company_merge_backup_<table> "
        "(see company_merge_log for every merge performed)."
    )
