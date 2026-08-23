"""Derive the "next 200" coverage candidates (proposed ranks 501-700).

The covered universe today is the Nifty 500 import. To expand it by the next
200 most valuable listed Indian companies, the candidates are:

1. **Universe** — BSE's active-scrip master (segment=Equity, status=Active),
   the same authoritative exchange source the Nifty 500 importer already
   joins for BSE codes. It covers dual-listed and BSE-only listings alike.
2. **Exclusion** — the current universe, taken from the database (every
   existing company record), matched on **ISIN first, then uppercase ticker**.
   ISIN is the security's legal identifier; ticker collisions between
   exchanges are a documented hazard the Nifty 500 import report already
   called out, so a ticker match alone never counts as proof of identity.
3. **Ranking** — total market capitalisation, from the platform's own market
   data provider (FMP — its profile/quote payloads carry ``marketCap`` and
   the production router already parses it, see ``app/data/providers/fmp.py``).

This module is pure: every network touch and database read is injected, so
the derivation logic is unit-testable without a key, a connection or the
exchange. The CLI that wires in the real sources lives in
``deploy/derive_next_200.py`` and is strictly read-only against the
database — this is a report generator, not an importer.
"""
from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

#: Indian ISINs carry the INE prefix; US listings are US-prefixed.
INR_PREFIX = "INE"

#: The rank the next company *inside* the current universe would hold, so
#: the first candidate starts here (501) and the 200th holds 700.
FIRST_CANDIDATE_RANK = 501


@dataclass(frozen=True, slots=True)
class BseScrip:
    """One row of BSE's active-scrip master (equity segment)."""

    scrip_code: str
    #: Exchange ticker when the master supplies one; empty otherwise.
    ticker: str
    name: str
    isin: str | None
    sector: str | None = None
    industry: str | None = None
    #: BSE's own market-cap figure, in the unit BSE reports it (undocumented —
    #: see :func:`calibrate_bse_mktcap`). Raw value, not yet in crore.
    mktcap: float | None = None


def _first(row: Mapping[str, Any], *keys: str) -> Any:
    """First non-empty value among exact keys, or None.

    BSE's ``ListofScripData`` payload has migrated field spellings over the
    years (``COMP_NAME`` -> ``Scrip_Name``, mixed case like ``scrip_id`` and
    ``Mktcap``). Every read therefore tries the current spelling first and
    falls back to the older ones, so both generations of the master parse.
    """
    for key in keys:
        value = row.get(key)
        if value is None or value == "":
            continue
        return value
    return None


def _to_float(value: Any) -> float | None:
    """BSE sends numbers and numeric strings (sometimes comma-grouped)."""
    if value is None:
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True, slots=True)
class CandidateRow:
    """One ranked candidate for the report."""

    proposed_rank: int
    ticker: str
    name: str
    isin: str | None
    exchange: str
    #: Total market capitalisation in INR crore (display units).
    market_cap_inr_crore: float | None
    #: Where the figure came from ("fmp_screener", "fmp_profile", ...).
    mcap_source: str | None
    #: What the exclusion check concluded for this company.
    current_universe_status: str
    bse_scrip_code: str
    sector: str | None = None


@dataclass(frozen=True, slots=True)
class UniverseEntry:
    """A company already in the platform's database."""

    ticker: str
    isin: str | None
    listing_status: str | None = None


# ---------------------------------------------------------------------------
# BSE master parsing
# ---------------------------------------------------------------------------

def parse_bse_master(payload: Iterable[Mapping[str, Any]]) -> list[BseScrip]:
    """BSE ``ListofScripData`` rows -> active equity scrips.

    The current payload generation uses ``SCRIP_CD``, ``Scrip_Name``,
    ``Status``, ``Segment``, ``ISIN_NUMBER``, ``INDUSTRY``, ``scrip_id`` and
    ``Mktcap``; earlier generations used all-caps names (``COMP_NAME``,
    ``SEGMENT``, ``SCRIP_STATUS`` ...). Every field is read current-first
    with the older spellings as fallback, and a row missing both a scrip
    code and a name is dropped rather than producing a phantom candidate.

    The exchange ticker is ``SYMBOL`` when present, else ``scrip_id``; rows
    carrying neither are kept with an empty ticker — they still have ISIN
    and name, which is what the downstream matching needs.
    """
    out: list[BseScrip] = []
    for row in payload:
        segment = str(_first(row, "Segment", "SEGMENT") or "Equity").strip().lower()
        status = str(_first(row, "Status", "STATUS", "SCRIP_STATUS") or "Active").strip().lower()
        if segment != "equity" or status != "active":
            continue
        code = str(_first(row, "SCRIP_CD", "SCRIP_CODE") or "").strip()
        name = str(_first(row, "Scrip_Name", "COMP_NAME", "COMPANY_NAME") or "").strip()
        if not code or not name:
            continue
        ticker = str(_first(row, "SYMBOL", "scrip_id") or "").strip().upper()
        isin = str(_first(row, "ISIN_NUMBER", "ISIN") or "").strip().upper() or None
        out.append(BseScrip(
            scrip_code=code,
            ticker=ticker,
            name=name,
            isin=isin,
            sector=str(_first(row, "BSE_SECTOR") or "").strip() or None,
            industry=str(_first(row, "INDUSTRY", "INDUSTRY_NAME") or "").strip() or None,
            mktcap=_to_float(_first(row, "Mktcap", "MKTCAP", "MKT_CAP")),
        ))
    return out


#: RELIANCE's ISIN — the calibration reference. It is the largest
#: Indian-listed company, whose total market cap sits between 5e13 and 5e15
#: rupees (5e5..5e7 crore) for any plausible date, far from the boundary
#: between the two candidate units.
_RELIANCE_ISIN = "INE002A01018"
#: The two candidate units are unambiguously separated: above 1e12 only
#: absolute rupees are possible for the market leader, and at or below 1e9
#: only crore is.
_RUPEE_FLOOR = 1e12
_CRORE_CEILING = 1e9


def calibrate_bse_mktcap(
    scrips: Sequence[BseScrip],
) -> tuple[str | None, float]:
    """Resolve the unit of BSE's undocumented ``Mktcap`` field.

    Returns ``(unit, factor_to_crore)`` — ``("rupee", 1e-7)``,
    ``("crore", 1.0)`` or ``(None, 0.0)`` when the unit cannot be told
    apart. The check runs against RELIANCE when the master carries it,
    else against the scrip with the largest figure (the market leader is
    still far from the unit boundary).

    Why calibrate instead of assuming: BSE has migrated this API's field
    spellings before, and the unit is not documented anywhere; a wrong
    assumption would silently rank the entire universe 1e7 off. The
    reference magnitude is stable across market cycles, which is exactly
    the property a unit check needs.
    """
    reference: float | None = None
    for scrip in scrips:
        if scrip.isin == _RELIANCE_ISIN and scrip.mktcap:
            reference = scrip.mktcap
            break
    if reference is None:
        leader = max(
            (s.mktcap for s in scrips if s.mktcap), default=None,
        )
        if leader is not None and leader > 1e11:
            # A market leader at 1e11+ cannot be crore (that would be the
            # most valuable company on earth); it is rupees.
            reference = leader
    if reference is None:
        return None, 0.0
    if reference > _RUPEE_FLOOR:
        return "rupee", 1e-7
    if reference <= _CRORE_CEILING:
        return "crore", 1.0
    return None, 0.0


# ---------------------------------------------------------------------------
# Universe exclusion
# ---------------------------------------------------------------------------

def universe_sets(entries: Iterable[UniverseEntry]) -> tuple[set[str], set[str]]:
    """(ISIN set, uppercase-ticker set) for O(1) exclusion checks."""
    isins: set[str] = set()
    tickers: set[str] = set()
    for entry in entries:
        if entry.isin:
            isins.add(entry.isin.upper())
        if entry.ticker:
            tickers.add(entry.ticker.strip().upper())
    return isins, tickers


def classify(
    scrip: BseScrip,
    universe_isins: set[str],
    universe_tickers: set[str],
) -> str:
    """What the exclusion check concluded for one scrip.

    ISIN match first: a company that renames its ticker keeps its ISIN, so
    an ISIN hit is the only match that cannot be a different security. A
    ticker-only hit is recorded as such — still excluded (same ticker with
    no ISIN overlap is a genuine duplicate risk, and the import report shows
    exactly these rows exist), but visible in the report so a reviewer can
    see the reasoning instead of a silent drop.
    """
    if scrip.isin and scrip.isin in universe_isins:
        return "excluded_isin_match"
    # Ticker comparison is case-insensitive: the master's casing drifts
    # between vintages and the universe set is uppercased.
    if scrip.ticker and scrip.ticker.strip().upper() in universe_tickers:
        return "excluded_ticker_match"
    if not scrip.isin:
        # A BSE row without an ISIN cannot be deduplicated against the
        # universe or reliably ranked through FMP — surfaced in the summary,
        # never silently dropped into the candidate list.
        return "excluded_no_isin"
    if not scrip.isin.startswith(INR_PREFIX):
        # Not an Indian listing — US/foreign scrips are outside this
        # expansion's scope (the platform covers the three US listings it
        # chose in Phase 3, by decision, not by index).
        return "excluded_non_indian_isin"
    return "candidate"


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------

def normalise_name(name: str) -> str:
    """Alphanumerics only, uppercased — the name-matching key."""
    return re.sub(r"[^A-Z0-9]+", "", (name or "").upper())


def rank_candidates(
    scrips: Sequence[BseScrip],
    universe_isins: set[str],
    universe_tickers: set[str],
    market_cap_inr_crore: Mapping[str, tuple[float | None, str]],
    *,
    limit: int = 200,
    first_rank: int = FIRST_CANDIDATE_RANK,
) -> tuple[list[CandidateRow], dict[str, int]]:
    """Rank eligible scrips by market cap.

    ``market_cap_inr_crore`` maps an identifier — ISIN (preferred),
    uppercase exchange ticker, or normalised name — to
    ``(figure_inr_crore, source)``. A candidate whose figure is missing or
    non-positive cannot be ranked and is counted in the summary instead of
    being silently ranked at zero.

    Returns ``(rows, counts)`` where counts breaks down every non-candidate
    and every unrankable candidate.
    """
    counts: dict[str, int] = {
        "candidate": 0, "no_market_cap_available": 0,
    }
    candidates: list[tuple[BseScrip, float, str]] = []

    for scrip in scrips:
        status = classify(scrip, universe_isins, universe_tickers)
        if status != "candidate":
            counts[status] = counts.get(status, 0) + 1
            continue
        counts["candidate"] += 1

        keys = [k for k in (
            (scrip.isin or "").upper(),
            scrip.ticker,
            normalise_name(scrip.name),
        ) if k]
        figure = None
        for key in keys:
            if key in market_cap_inr_crore:
                figure = market_cap_inr_crore[key]
                break
        if figure is None or figure[0] is None or figure[0] <= 0:
            counts["no_market_cap_available"] += 1
            continue
        candidates.append((scrip, figure[0], figure[1]))

    candidates.sort(key=lambda item: (-item[1], item[0].name))
    rows = [
        CandidateRow(
            proposed_rank=first_rank + index,
            ticker=scrip.ticker or _ticker_from_name(scrip.name),
            name=scrip.name,
            isin=scrip.isin,
            exchange="BSE" if scrip.ticker and scrip.ticker.endswith(".BO") else "BSE/NSE",
            market_cap_inr_crore=round(amount, 1),
            mcap_source=source,
            current_universe_status="not_in_current_universe",
            bse_scrip_code=scrip.scrip_code,
            sector=scrip.sector,
        )
        for index, (scrip, amount, source) in enumerate(candidates[:limit])
    ]
    return rows, counts


def _ticker_from_name(name: str) -> str:
    """A labelled placeholder for master rows without a SYMBOL column."""
    return normalise_name(name)[:12] or "UNNAMED"


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------

def rows_to_dicts(rows: Sequence[CandidateRow]) -> list[dict[str, Any]]:
    return [
        {
            "proposed_rank": row.proposed_rank,
            "ticker": row.ticker,
            "company_name": row.name,
            "isin": row.isin,
            "exchange": row.exchange,
            "market_cap_inr_crore": row.market_cap_inr_crore,
            "mcap_source": row.mcap_source,
            "current_universe_status": row.current_universe_status,
            "bse_scrip_code": row.bse_scrip_code,
            "sector": row.sector,
        }
        for row in rows
    ]


def to_csv(rows: Sequence[CandidateRow]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([
        "proposed_rank", "ticker", "company_name", "isin", "exchange",
        "market_cap_inr_crore", "mcap_source", "current_universe_status",
        "bse_scrip_code", "sector",
    ])
    for row in rows:
        writer.writerow([
            row.proposed_rank, row.ticker, row.name, row.isin or "",
            row.exchange,
            "" if row.market_cap_inr_crore is None else row.market_cap_inr_crore,
            row.mcap_source or "", row.current_universe_status,
            row.bse_scrip_code, row.sector or "",
        ])
    return buffer.getvalue()


def to_json(
    rows: Sequence[CandidateRow],
    *,
    generated_at: str,
    market_cap_as_of: str,
    summary: dict[str, Any],
) -> str:
    return json.dumps(
        {
            "generated_at": generated_at,
            "market_cap_as_of": market_cap_as_of,
            "summary": summary,
            "candidates": rows_to_dicts(rows),
        },
        indent=2,
        ensure_ascii=False,
    )
