"""Derive the "next 200" coverage candidates (proposed ranks 501-700).

The covered universe today is the Nifty 500 import. To expand coverage by
the next 200 most valuable listed Indian companies, the candidates are the
database's **non-NIFTY500 companies** — the production database already
holds the full BSE active master (every BSE ISIN is present in
``companies``), so "BSE master minus database" yields zero new companies
and the database itself is the candidate pool.

Pipeline:

1. **Candidates** — ``companies`` rows that are not NIFTY500 (by the
   ``index_membership`` tag, cross-checked against the live NSE Nifty 500
   list so a stale tag cannot leak a constituent in), active, and Indian
   (INE ISIN, or no ISIN with an INR currency).
2. **Market cap, read-only** — tier 0 is the BSE master's own ``Mktcap``,
   matched to candidates **by ISIN** and unit-calibrated (see
   :func:`calibrate_bse_mktcap`); secondary sources (FMP screener,
   screener.in, FMP profile) fill only the ISINs BSE does not cover.
3. **Ranking** — market cap descending, top 200, proposed ranks 501-700;
   ties break by name; candidates without a figure are counted in the
   coverage report, never ranked at zero.

This module is pure: every network touch and database read is injected, so
the derivation logic is unit-testable without a key, a connection or the
exchange. The CLI that wires in the real sources lives in
``deploy/derive_next_200.py`` and is strictly read-only against the
database — this is a report generator, not an importer. The final backfill
that would make the top 200 covered companies is a separate, explicit
step that runs only after the dry-run coverage report is reviewed.
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
    #: Where the figure came from ("bse_master", "fmp_screener", "screener.in",
    #: "fmp_profile", ...).
    mcap_source: str | None
    #: What the exclusion check concluded for this company.
    current_universe_status: str
    bse_scrip_code: str
    sector: str | None = None
    #: Database primary key of the company, so the reviewed report maps
    #: straight onto the rows a later backfill would ingest.
    company_id: str | None = None


@dataclass(frozen=True, slots=True)
class UniverseEntry:
    """A company already in the platform's database."""

    ticker: str
    isin: str | None
    listing_status: str | None = None


@dataclass(frozen=True, slots=True)
class CompanyCandidate:
    """A ``companies`` row, as read by the dry run.

    The candidate pool is the database itself (every BSE ISIN is already in
    ``companies``), so the derivation reads company rows and ranks them —
    it never re-derives the universe from the exchange.
    """

    company_id: str
    ticker: str
    name: str
    isin: str | None
    exchange: str | None = None
    listing_status: str | None = None
    index_membership: str | None = None
    currency: str | None = None
    sector: str | None = None
    bse_code: str | None = None


#: The tag the Nifty 500 importer writes onto covered constituents.
NIFTY500_TAG = "NIFTY500"


def classify_company(
    company: CompanyCandidate,
    live_nifty500_isins: frozenset[str] | None = None,
    live_nifty500_tickers: frozenset[str] | None = None,
) -> str:
    """What the exclusion check concluded for one database company.

    Exclusions, in order (the first hit wins and is counted under its own
    key in the coverage report):

    1. **NIFTY500 tag** — ``index_membership`` says the company is in the
       covered index. This is the primary partition.
    2. **Live Nifty 500 list** — the tag is a snapshot; NSE rebalances. A
       company whose ISIN or ticker is in today's list is excluded even if
       its tag was never updated, so a stale tag cannot leak a live
       constituent into the "next 200".
    3. **Not active** — delisted/suspended rows are retained in the database
       for their history, but they are not coverage candidates.
    4. **Non-Indian** — an ISIN that is not INE-prefixed (the US listings
       the platform holds from Phase 3), or no ISIN with a non-INR currency.
    """
    if (company.index_membership or "").strip().upper() == NIFTY500_TAG:
        return "excluded_nifty500_tag"

    isin = (company.isin or "").strip().upper()
    if isin:
        if live_nifty500_isins and isin in live_nifty500_isins:
            return "excluded_nifty500_live_list"
    elif (company.currency or "INR").strip().upper() != "INR":
        return "excluded_non_indian"

    if (
        live_nifty500_tickers
        and (company.ticker or "").strip().upper() in live_nifty500_tickers
    ):
        # A live constituent that slipped past the tag (and carries no INE
        # ISIN): still a Nifty 500 company, still excluded.
        return "excluded_nifty500_live_list"

    if (company.listing_status or "active").strip().lower() != "active":
        return "excluded_not_active"

    if isin and not isin.startswith(INR_PREFIX):
        return "excluded_non_indian"

    return "candidate"


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


def build_bse_mktcap_map(
    scrips: Sequence[BseScrip],
) -> tuple[dict[str, tuple[float, str]], str | None]:
    """ISIN -> (market cap in INR crore, BSE scrip code), unit-calibrated.

    The exchange's own figure for its own companies — matched to database
    candidates by ISIN, the security's legal identifier. Returns
    ``({}, None)`` when the ``Mktcap`` unit cannot be calibrated: the caller
    then uses the secondary sources for everything rather than ranking on a
    guessed unit.
    """
    unit, factor = calibrate_bse_mktcap(scrips)
    if unit is None:
        return {}, None
    out: dict[str, tuple[float, str]] = {}
    for scrip in scrips:
        if not scrip.isin or not scrip.mktcap or scrip.mktcap <= 0:
            continue
        out[scrip.isin.upper()] = (round(scrip.mktcap * factor, 1),
                                   scrip.scrip_code)
    return out, unit


def build_bse_identity_maps(
    scrips: Sequence[BseScrip],
) -> tuple[dict[str, str], dict[str, str]]:
    """Authoritative BSE-master identity maps: (symbol->ISIN, code->ISIN).

    Production holds a dual-listed security as two ``companies`` rows where
    the NSE row carries a **NULL** ISIN and only the BSE row carries the real
    one — so ISIN alone cannot join the two rows. The BSE master, already
    fetched for market cap, carries the authoritative join: each scrip's
    ``SYMBOL``/``scrip_id`` (the exchange ticker) and ``SCRIP_CD`` (the BSE
    scrip code) both map to its ``ISIN_NUMBER``. These exact-key maps resolve
    a NULL-ISIN NSE row to the same security as its BSE listing without any
    fuzzy name matching.

    Returns ``(symbol_to_isin, scrip_code_to_isin)``; only scrips that carry
    an ISIN contribute, and the first mapping wins on a (non-existent)
    symbol/code collision.
    """
    symbol_to_isin: dict[str, str] = {}
    scrip_code_to_isin: dict[str, str] = {}
    for scrip in scrips:
        isin = (scrip.isin or "").strip().upper()
        if not isin:
            continue
        symbol = (scrip.ticker or "").strip().upper()
        if symbol:
            symbol_to_isin.setdefault(symbol, isin)
        code = (scrip.scrip_code or "").strip()
        if code:
            scrip_code_to_isin.setdefault(code, isin)
    return symbol_to_isin, scrip_code_to_isin


def resolve_identity(
    cand: Any,
    symbol_to_isin: Mapping[str, str] | None = None,
    scrip_code_to_isin: Mapping[str, str] | None = None,
) -> str | None:
    """The authoritative ISIN for a candidate, resolved via the BSE master.

    Resolution order:

    1. The row's own ISIN (the primary identity, when present).
    2. The row's BSE scrip code looked up in the master's code->ISIN map —
       this catches a row that stores the BSE code but not the ISIN.
    3. The row's ticker looked up in the master's symbol->ISIN map — this
       catches the production case of a **NULL-ISIN NSE row** whose ticker is
       the BSE master's symbol for a security whose ISIN lives on the BSE
       row.

    The resolved ISIN is used for **deduplication and market-cap matching
    only — it is never written back to the database**. Returns ``None`` when
    the row cannot be tied to an ISIN (caller falls back to ticker/name).
    Exact-key lookups only; there is no fuzzy or name-similarity matching, so
    genuinely different securities are never merged on a near-name.
    """
    isin = (getattr(cand, "isin", None) or "").strip().upper()
    if isin:
        return isin
    if scrip_code_to_isin:
        code = (getattr(cand, "bse_code", None) or "").strip()
        if code and code in scrip_code_to_isin:
            return scrip_code_to_isin[code]
    if symbol_to_isin:
        ticker = (getattr(cand, "ticker", None) or "").strip().upper()
        if ticker and ticker in symbol_to_isin:
            return symbol_to_isin[ticker]
    return None


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


#: Legal-form tokens the two exchanges record differently for the same
#: security ("... Limited" on NSE vs "... Ltd" on BSE). Stripped from the
#: END of the name before exact same-security matching.
_LEGAL_SUFFIXES = frozenset({"LIMITED", "LTD", "LLP", "PLC", "PVT", "COMPANY"})
#: Leading articles that one exchange records and the other drops
#: ("The South Indian Bank Limited" vs "South Indian Bank Ltd").
_LEADING_ARTICLES = frozenset({"THE", "AN", "A"})


def normalize_legal_name(name: str) -> str:
    """Canonical company-name key for EXACT same-security matching.

    Uppercases, keeps alphanumerics, and normalises the legal-form and
    leading-article variation the two exchanges record differently for the
    same security: "The South Indian Bank Limited" (NSE) and "South Indian
    Bank Ltd" (BSE) both reduce to "SOUTHINDIANBANK"; "Cupid Limited" and
    "Cupid Ltd" both reduce to "CUPID"; "INDO-MIM Limited" and "INDO-MIM
    Ltd" both reduce to "INDOMIM".

    This is an exact canonical key, **not** a similarity score: two
    genuinely different companies with different names ("Vedanta" vs
    "Vedanta Aluminium Metal") reduce to different keys and are never
    merged on it.
    """
    words = re.findall(r"[A-Z0-9]+", (name or "").upper())
    while words and words[0] in _LEADING_ARTICLES:
        words = words[1:]
    while words and words[-1] in _LEGAL_SUFFIXES:
        words = words[:-1]
    return "".join(words)


def _build_name_isin_index(
    candidates: Sequence[Any],
    symbol_to_isin: Mapping[str, str] | None,
    scrip_code_to_isin: Mapping[str, str] | None,
) -> dict[str, str]:
    """normalized-name -> ISIN, for names mapping to exactly one ISIN.

    Only candidates that already resolve to an ISIN (own ISIN, or the BSE
    master's code/symbol maps) seed this index. A name that maps to two or
    more distinct ISINs is a data collision and is excluded, so a NULL-ISIN
    row is never merged on an ambiguous name. This index is what lets an
    NSE NULL-ISIN row be reconciled to its ISIN-bearing BSE row by exact
    (legal-suffix-normalised) company name.
    """
    from collections import defaultdict

    name_to_isins: dict[str, set[str]] = defaultdict(set)
    for cand in candidates:
        isin = resolve_identity(cand, symbol_to_isin, scrip_code_to_isin)
        if not isin:
            continue
        name = normalize_legal_name(getattr(cand, "name", None) or "")
        if name:
            name_to_isins[name].add(isin)
    return {name: isins.pop() for name, isins in name_to_isins.items()
            if len(isins) == 1}


def _full_isin(
    cand: Any,
    symbol_to_isin: Mapping[str, str] | None,
    scrip_code_to_isin: Mapping[str, str] | None,
    name_to_isin: Mapping[str, str] | None,
) -> str | None:
    """A candidate's security-level ISIN, two stages.

    Stage 1 (explicit identity): the row's own ISIN, else the BSE master's
    scrip-code / symbol maps. Stage 2 (name reconciliation): for a row that
    still has no ISIN, an EXACT legal-suffix-normalised company-name match to
    a single ISIN-bearing security. Stage 2 is what joins the production NSE
    NULL-ISIN row to its ISIN-bearing BSE row when the master's symbol/code
    maps do not connect them. The result is for dedupe and market-cap
    matching only — never a database write.
    """
    isin = resolve_identity(cand, symbol_to_isin, scrip_code_to_isin)
    if isin:
        return isin
    if name_to_isin:
        name = normalize_legal_name(getattr(cand, "name", None) or "")
        if name:
            return name_to_isin.get(name)
    return None


def _identity_key(
    cand: Any,
    symbol_to_isin: Mapping[str, str] | None = None,
    scrip_code_to_isin: Mapping[str, str] | None = None,
    name_to_isin: Mapping[str, str] | None = None,
) -> str:
    """Identity of a candidate security: ISIN first, then ticker, then name.

    ISIN is the security's legal identifier and the **primary** identity.
    When the row's own ISIN is NULL, :func:`_full_isin` recovers the
    security's ISIN from the BSE master (scrip code, then exchange symbol)
    and, failing that, from an exact legal-suffix-normalised company-name
    match to a single ISIN-bearing security — so a NULL-ISIN NSE row and its
    ISIN-bearing BSE row collapse to the same key. Only when no ISIN can be
    established do we fall back to the uppercase ticker, then the normalised
    name.
    """
    isin = _full_isin(cand, symbol_to_isin, scrip_code_to_isin, name_to_isin)
    if isin:
        return f"ISIN:{isin}"
    ticker = (getattr(cand, "ticker", None) or "").strip().upper()
    if ticker:
        return f"TICKER:{ticker}"
    name = normalise_name(getattr(cand, "name", None) or "")
    return f"NAME:{name}" if name else "NO-IDENTITY"


def _representative_company(
    members: list[CompanyCandidate],
    resolved_isin: str | None = None,
) -> CompanyCandidate:
    """Merge the NSE/BSE rows of one security into a complete representative.

    The anchor is the most complete member, whose ``company_id`` is the one
    a later backfill maps onto. Fields are taken from the first member that
    carries them, and the exchange is unified: an NSE row plus a BSE row for
    the same security becomes ``NSE/BSE``. This is what keeps the
    representative carrying the correct ticker, exchange and BSE scrip code
    even when those live on different rows.

    ``resolved_isin`` is the BSE-master-resolved ISIN, used only when no
    member physically carries one (e.g. both rows NULL-ISIN but tied to the
    same security via the master). It is in-memory for the report only —
    never written to the database.
    """
    def completeness(m: CompanyCandidate) -> int:
        return sum(1 for v in (
            m.company_id, m.ticker, m.name, m.isin, m.exchange, m.sector,
            m.bse_code,
        ) if v)

    anchor = max(members, key=completeness)

    parts: list[str] = []
    for m in members:
        for part in (m.exchange or "").upper().split("/"):
            part = part.strip()
            if part and part not in parts:
                parts.append(part)
    if "NSE" in parts and "BSE" in parts:
        exchange = "NSE/BSE"
    elif len(parts) == 1:
        exchange = parts[0]
    else:
        exchange = "/".join(parts) if parts else None

    # The dual-listed pair stores the NSE row's ticker as the exchange symbol
    # ("VAML") and the BSE row's ticker as the numeric scrip code ("544780").
    # Prefer a real symbol (non-numeric) for the representative ticker, and
    # recover the BSE scrip code from an explicit bse_code else a numeric
    # ticker, so the report carries both identifiers correctly.
    tickers = [m.ticker.strip() for m in members if (m.ticker or "").strip()]
    symbol = next((t for t in tickers if not t.isdigit()), None)
    ticker = symbol or (tickers[0] if tickers else anchor.ticker)
    bse_code = next((m.bse_code.strip() for m in members
                     if (m.bse_code or "").strip()), None)
    if not bse_code:
        bse_code = next((t for t in tickers if t.isdigit()), None)

    return CompanyCandidate(
        company_id=anchor.company_id,
        ticker=ticker,
        name=next((m.name for m in members if (m.name or "").strip()),
                  anchor.name),
        isin=next((m.isin for m in members if (m.isin or "").strip()),
                  anchor.isin or resolved_isin),
        exchange=exchange,
        listing_status=next((m.listing_status for m in members
                             if (m.listing_status or "").strip()), "active"),
        index_membership=next((m.index_membership for m in members
                               if (m.index_membership or "").strip()), None),
        currency=next((m.currency for m in members
                       if (m.currency or "").strip()), None),
        sector=next((m.sector for m in members if (m.sector or "").strip()),
                    None),
        bse_code=bse_code,
    )


def _merge_same_security(
    members: list[Any],
    symbol_to_isin: Mapping[str, str] | None = None,
    scrip_code_to_isin: Mapping[str, str] | None = None,
    name_to_isin: Mapping[str, str] | None = None,
) -> Any:
    """One representative for several rows of the same security.

    ``CompanyCandidate`` rows (the database shape) are field-merged so the
    representative keeps the best ticker, exchange and BSE scrip code even
    when those live on different rows, and carries the security-level ISIN
    (own, BSE-master-resolved, or name-reconciled) when no member physically
    has one. Other shapes keep their most informative member — the BSE master
    is one row per scrip, so this is a guard, not an expected path.
    """
    if all(isinstance(m, CompanyCandidate) for m in members):
        resolved = _full_isin(members[0], symbol_to_isin,
                              scrip_code_to_isin, name_to_isin)
        return _representative_company(members, resolved_isin=resolved)
    return next((m for m in members if (getattr(m, "ticker", "") or "").strip()),
                members[0])


def dedupe_candidates(
    candidates: Sequence[Any],
    symbol_to_isin: Mapping[str, str] | None = None,
    scrip_code_to_isin: Mapping[str, str] | None = None,
) -> list[Any]:
    """Collapse same-security rows into one representative, order-preserving.

    The ``companies`` table can hold a dual-listed security twice — an NSE
    row and a BSE row for the same ISIN, and in production the NSE row often
    carries a **NULL** ISIN while the BSE row carries the real one (and the
    BSE row's ticker is the numeric scrip code, not a symbol, so ISIN alone
    cannot join them). Ranking both would let one company occupy two of the
    200 slots.

    This groups by :func:`_identity_key`, where the security-level ISIN is
    recovered for a NULL-ISIN row from (1) the BSE master's scrip-code /
    symbol maps and, failing that, (2) an exact legal-suffix-normalised
    company-name match to a single ISIN-bearing security. A single
    representative is kept per security.

    Pure and order-preserving: the first group encountered keeps its
    position, so ranking order is otherwise unaffected.
    """
    name_to_isin = _build_name_isin_index(
        candidates, symbol_to_isin, scrip_code_to_isin)
    groups: dict[str, list[Any]] = {}
    order: list[str] = []
    for cand in candidates:
        key = _identity_key(cand, symbol_to_isin, scrip_code_to_isin,
                            name_to_isin)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(cand)

    out: list[Any] = []
    for key in order:
        members = groups[key]
        out.append(members[0] if len(members) == 1
                   else _merge_same_security(members, symbol_to_isin,
                                             scrip_code_to_isin, name_to_isin))
    return out


def rank_companies(
    candidates: Sequence[Any],
    market_cap_inr_crore: Mapping[str, tuple[float | None, str]],
    *,
    bse_scrip_codes: Mapping[str, str] | None = None,
    symbol_to_isin: Mapping[str, str] | None = None,
    scrip_code_to_isin: Mapping[str, str] | None = None,
    limit: int = 200,
    first_rank: int = FIRST_CANDIDATE_RANK,
    universe_label: str = "not_in_nifty500",
) -> tuple[list[CandidateRow], dict[str, int]]:
    """Rank pre-classified candidates by market cap.

    Accepts anything candidate-shaped (``CompanyCandidate`` from the
    database, ``BseScrip`` from the master) — fields are read defensively.
    ``market_cap_inr_crore`` maps an identifier — ISIN (preferred),
    uppercase exchange ticker, or normalised name — to
    ``(figure_inr_crore, source)``. ``bse_scrip_codes`` maps ISIN -> BSE
    scrip code, used to enrich database rows that carry no code of their
    own. A candidate whose figure is missing or non-positive cannot be
    ranked and is counted in the summary instead of being silently ranked
    at zero.

    ``symbol_to_isin`` / ``scrip_code_to_isin`` are the BSE-master identity
    maps: they let a **NULL-ISIN NSE row** be tied to the same security as
    its ISIN-bearing BSE row (dedupe) and to the BSE market-cap figure
    (matching), without any database write. When omitted, identity falls
    back to the row's own ISIN, then ticker, then name.

    Candidates are first collapsed by :func:`dedupe_candidates` so that a
    dual-listed security (an NSE row and a BSE row for the same security)
    counts as one company — otherwise one security would occupy two of the
    200 slots and the proposed ranks 501-700 would carry it twice.

    Returns ``(rows, counts)`` — rows carry proposed ranks starting at
    ``first_rank`` (501 by default), counts carry ``ranked``,
    ``no_market_cap_available`` (both over unique companies) and
    ``duplicates_collapsed`` (how many extra listing rows were folded away).
    """
    deduped = dedupe_candidates(candidates, symbol_to_isin,
                                scrip_code_to_isin)
    counts: dict[str, int] = {
        "ranked": 0,
        "no_market_cap_available": 0,
        "duplicates_collapsed": len(candidates) - len(deduped),
    }
    ranked: list[tuple[Any, float, str]] = []

    for cand in deduped:
        # Market-cap matching stays ISIN-first; the BSE-master-resolved ISIN
        # is tried before the row's own so a NULL-ISIN NSE row still finds
        # its BSE figure. Ticker/name remain the secondary-source fallback.
        resolved = resolve_identity(cand, symbol_to_isin,
                                    scrip_code_to_isin)
        isin = (getattr(cand, "isin", None) or "").strip().upper()
        keys = [k for k in (
            resolved,
            isin,
            (getattr(cand, "ticker", None) or "").strip().upper(),
            normalise_name(getattr(cand, "name", None) or ""),
        ) if k]
        figure = None
        for key in keys:
            if key in market_cap_inr_crore:
                figure = market_cap_inr_crore[key]
                break
        if figure is None or figure[0] is None or figure[0] <= 0:
            counts["no_market_cap_available"] += 1
            continue
        ranked.append((cand, figure[0], figure[1]))

    ranked.sort(key=lambda item: (-item[1], (getattr(item[0], "name", "") or "")))
    rows: list[CandidateRow] = []
    for index, (cand, amount, source) in enumerate(ranked[:limit]):
        isin = (getattr(cand, "isin", None) or "").strip().upper() or None
        exchange = getattr(cand, "exchange", None)
        if not exchange:
            exchange = ("BSE/NSE" if isin and isin in (bse_scrip_codes or {})
                        else "BSE")
        bse_code = (
            getattr(cand, "bse_code", None)
            or (bse_scrip_codes or {}).get(isin or "", "")
            or getattr(cand, "scrip_code", "")
        )
        rows.append(CandidateRow(
            proposed_rank=first_rank + index,
            ticker=(getattr(cand, "ticker", None) or "").strip().upper()
            or _ticker_from_name(getattr(cand, "name", "") or ""),
            name=getattr(cand, "name", "") or "",
            isin=isin,
            exchange=exchange,
            market_cap_inr_crore=round(amount, 1),
            mcap_source=source,
            current_universe_status=universe_label,
            bse_scrip_code=bse_code,
            sector=getattr(cand, "sector", None),
            company_id=getattr(cand, "company_id", None),
        ))
    counts["ranked"] = len(rows)
    return rows, counts


def rank_candidates(
    scrips: Sequence[BseScrip],
    universe_isins: set[str],
    universe_tickers: set[str],
    market_cap_inr_crore: Mapping[str, tuple[float | None, str]],
    *,
    limit: int = 200,
    first_rank: int = FIRST_CANDIDATE_RANK,
) -> tuple[list[CandidateRow], dict[str, int]]:
    """Classify then rank a list of BSE scrips against a known universe.

    Kept for the BSE-driven flow (and its tests); the database-driven flow
    classifies with :func:`classify_company` and ranks via
    :func:`rank_companies` directly.
    """
    counts: dict[str, int] = {}
    eligible: list[BseScrip] = []
    for scrip in scrips:
        status = classify(scrip, universe_isins, universe_tickers)
        if status != "candidate":
            counts[status] = counts.get(status, 0) + 1
            continue
        eligible.append(scrip)
    rows, rank_counts = rank_companies(
        eligible, market_cap_inr_crore, limit=limit, first_rank=first_rank,
        universe_label="not_in_current_universe",
    )
    counts["candidate"] = len(eligible)
    counts.update(rank_counts)
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
            "company_id": row.company_id,
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
        "proposed_rank", "company_id", "ticker", "company_name", "isin",
        "exchange", "market_cap_inr_crore", "mcap_source",
        "current_universe_status", "bse_scrip_code", "sector",
    ])
    for row in rows:
        writer.writerow([
            row.proposed_rank, row.company_id or "", row.ticker, row.name,
            row.isin or "",
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
