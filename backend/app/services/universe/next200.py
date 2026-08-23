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

    BSE's field names have drifted across vintages, so every field is read
    defensively and a row missing both a scrip code and a name is dropped
    rather than producing a phantom candidate. The master's SYMBOL column
    (when present) is the exchange ticker; older vintages omit it, and such
    rows are kept with an empty ticker — they still carry ISIN and name,
    which is what the downstream matching needs.
    """
    out: list[BseScrip] = []
    for row in payload:
        segment = str(row.get("SEGMENT") or "Equity").strip().lower()
        status = str(row.get("SCRIP_STATUS") or row.get("STATUS") or "Active").strip().lower()
        if segment != "equity" or status != "active":
            continue
        code = str(row.get("SCRIP_CD") or row.get("SCRIP_CODE") or "").strip()
        name = str(row.get("COMP_NAME") or row.get("COMPANY_NAME") or "").strip()
        if not code or not name:
            continue
        ticker = str(row.get("SYMBOL") or "").strip().upper()
        isin = str(row.get("ISIN_NUMBER") or row.get("ISIN") or "").strip().upper() or None
        out.append(BseScrip(
            scrip_code=code,
            ticker=ticker,
            name=name,
            isin=isin,
            sector=str(row.get("BSE_SECTOR") or "").strip() or None,
            industry=str(row.get("INDUSTRY_NAME") or "").strip() or None,
        ))
    return out


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
