"""Canonical company resolution — exactly one row per (ticker, exchange).

The duplicate-company defect (one ticker, two ``companies`` rows, facts on
only one of them) had two root causes:

1. creation paths used *check-then-insert* without ever letting the database
   arbitrate a race, and
2. every ticker lookup was ``select(Company).where(Company.ticker == t)``
   fed to ``Session.scalar()`` — which, on more than one match, silently
   returns an *arbitrary* row instead of raising.

That second behaviour is what let the backfill ingest into whichever twin the
planner returned while ``companies_without_financials()`` kept (correctly)
reporting the other twin as uncovered.

This module is the single place that answers "which row *is* this company".
It is deliberately narrow:

* a company identity is ``(ticker, exchange)`` — the key the schema's
  ``uq_company_ticker_exchange`` constraint already declares;
* Indian venues (NSE, BSE, NSE/BSE) are one market and therefore one family:
  an Indian company is matched across them, with the platform's primary venue
  (NSE) preferred;
* when several rows still match (legacy data, before the deduplication
  migration has run), the canonical row is the one that owns the financial
  history, then the oldest, then the lexicographically smallest id — a total
  order, never an arbitrary planner pick.

Creation paths must still be race-safe at the database level; resolution only
makes reads deterministic. The migration
``9f0b5e8c2d71_company_deduplication_and_unique_identity`` re-asserts the
unique constraint, which is what converts the check-then-insert race from
"duplicate row" into "one insert loses and re-reads the winner".
"""

from __future__ import annotations

import structlog
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.company import Company, FinancialFact

log = structlog.get_logger(__name__)

#: Indian venues are one canonical market. The platform ingests every Indian
#: company under its NSE symbol, so "M&M" on BSE and "M&M" on NSE are the same
#: security — one company row.
INDIAN_EXCHANGES = ("NSE", "BSE", "NSE/BSE")

#: US venues keep their own namespace: a US listing may legitimately share a
#: symbol with an Indian one, which is exactly why the schema's uniqueness key
#: is (ticker, exchange) and not ticker alone.
US_EXCHANGES = ("NASDAQ", "NYSE", "AMEX")

#: Preferred venue inside the Indian family — the one every importer writes.
NSE = "NSE"

#: Stable ordering of the venue families for tie-breaking.
_VENUE_RANK = {"NSE": 0, "BSE": 1, "NSE/BSE": 2, "NASDAQ": 3, "NYSE": 4,
               "AMEX": 5}


def normalise_ticker(ticker: str | None) -> str | None:
    """Upper-case, stripped ticker. Callers feed this to lookups."""
    t = (ticker or "").strip().upper()
    return t or None


def venue_family(exchange: str | None) -> tuple[str, ...]:
    """The venue namespace a row belongs to: Indian, US, or itself."""
    e = (exchange or "").strip().upper()
    if e in INDIAN_EXCHANGES:
        return INDIAN_EXCHANGES
    if e in US_EXCHANGES:
        return US_EXCHANGES
    return (e,) if e else ()


def _fact_stats(db: Session, ids: list[str]) -> dict[str, tuple[int, int]]:
    """(fact count, distinct fiscal years) per company id — one query."""
    if not ids:
        return {}
    rows = db.execute(
        select(
            FinancialFact.company_id,
            func.count(FinancialFact.id),
            func.count(func.distinct(FinancialFact.fiscal_year)),
        )
        .where(FinancialFact.company_id.in_(ids))
        .group_by(FinancialFact.company_id)
    ).all()
    return {cid: (int(n), int(y)) for cid, n, y in rows}


def _pick(candidates: list[Company], stats: dict[str, tuple[int, int]]) -> Company:
    """Choose the canonical row among several matches.

    The row that owns the financial history wins; ties fall to the oldest row
    and finally to the smallest id. Deterministic, so two concurrent processes
    always agree on the same canonical id.
    """
    if len(candidates) == 1:
        return candidates[0]

    def key(c: Company) -> tuple:
        facts, years = stats.get(c.id, (0, 0))
        venue = _VENUE_RANK.get((c.exchange or "").upper(), 99)
        created = c.created_at
        return (-years, -facts, venue, created is None, created, c.id)

    chosen = min(candidates, key=key)
    log.warning(
        "multiple company rows share one ticker; resolving to the financial "
        "history owner (dedup migration should have merged these)",
        ticker=chosen.ticker,
        canonical_id=chosen.id,
        candidates=[c.id for c in candidates],
    )
    return chosen


def resolve_company(
    db: Session,
    ticker: str,
    *,
    exchange: str | None = None,
) -> Company | None:
    """Return the single canonical row for ``ticker``, or None.

    ``exchange`` scopes the search to that venue's family (an NSE ingest never
    matches a US-listed row that happens to share a symbol, and vice versa).
    With no exchange, the Indian family is preferred and the search falls back
    to any other venue's row — the platform's flows are Indian-first.
    """
    t = normalise_ticker(ticker)
    if not t:
        return None

    rows = list(
        db.scalars(select(Company).where(func.upper(Company.ticker) == t))
    )
    if not rows:
        return None

    if exchange:
        family = venue_family(exchange)
        rows = [c for c in rows if venue_family(c.exchange) == family]
        if not rows:
            return None
    else:
        indian = [c for c in rows if venue_family(c.exchange) == INDIAN_EXCHANGES]
        if indian:
            rows = indian

    return _pick(rows, _fact_stats(db, [c.id for c in rows]))
