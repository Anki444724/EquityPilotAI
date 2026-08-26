#!/usr/bin/env python3
"""Derive the next-200 coverage candidates (proposed ranks 501-700).

Read-only against the production database. It generates a report file; it
does NOT import companies, backfill financials, or write any row. The final
backfill that makes the reviewed top-200 covered companies is a separate,
explicit step and is NOT run by this script.

    export DATABASE_URL="postgresql+psycopg://..."
    python3 deploy/derive_next_200.py --report-only        # classification only
    python3 deploy/derive_next_200.py --limit 200          # full dry run

Design (revised 2026-08-24)
---------------------------
The first design derived candidates as "BSE master minus database".
Production invalidated it: the database already contains the full BSE
active master (7,042 companies — 500 NIFTY500 + 6,542 non-NIFTY500 — and
all 4,976 BSE ISINs are present in ``companies``), so that difference was
empty. The candidate pool is therefore the database itself:

1. **Candidates** — ``companies`` rows that are NOT NIFTY500 (the
   ``index_membership`` tag, cross-checked against today's NSE Nifty 500
   list so a stale tag cannot leak a live constituent in), active, and
   Indian (INE ISIN, or no ISIN with an INR currency).
2. **Market cap, read-only** — tier 0 is the BSE master's own ``Mktcap``,
   matched to candidates **by ISIN** with the unit calibrated (see
   ``calibrate_bse_mktcap``); secondary sources (FMP screener, screener.in,
   FMP profile) fill only the companies BSE does not match.
3. **Ranking** — market cap descending, top 200, proposed ranks 501-700.
   Candidates without a figure are counted, never ranked at zero.

The JSON report carries a ``coverage`` block — total candidates, BSE
matches, secondary-source matches, missing market caps, and the ranked
range — which is the artefact to review before any backfill is run.
"""
from __future__ import annotations

import argparse
import csv
import importlib
import io
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "backend"))

# The script is run from different places: the repo checkout on a host
# (backend/ one level up from deploy/), or copied into the API container
# (where the application lives at /app and this file may sit in /tmp).
# Add the first candidate that actually contains the `app` package.
for _candidate in (os.getcwd(), "/app"):
    if os.path.isfile(os.path.join(_candidate, "app", "__init__.py")):
        sys.path.insert(0, _candidate)

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import app.models as _models  # noqa: E402
for _module in importlib.__import__("pkgutil").iter_modules(_models.__path__):
    importlib.import_module(f"app.models.{_module.name}")

from app.services.universe.next200 import (  # noqa: E402
    BseScrip, CandidateRow, CompanyCandidate, build_bse_identity_maps,
    build_bse_mktcap_map, classify_company, dedupe_candidates,
    normalise_name, parse_bse_master, rank_companies, resolve_identity,
    to_csv, to_json,
)

_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
BSE_SCRIP_MASTER = (
    "https://api.bseindia.com/BseIndiaAPI/api/ListofScripData/w"
    "?Group=&Scripcode=&industry=&segment=Equity&status=Active"
)
NIFTY500_LIST = ("https://nsearchives.nseindia.com/content/indices/"
                 "ind_nifty500list.csv")
#: FMP reports absolute currency units; one crore is ten million.
_CRORE = 1e7


def fetch_bse_master() -> list[BseScrip]:
    """The BSE active-scrip master, parsed.

    Same endpoint and Referer the Nifty 500 importer already uses in
    production; the referer is what keeps BSE from redirecting to a web
    page.
    """
    request = urllib.request.Request(BSE_SCRIP_MASTER, headers={
        "User-Agent": _UA,
        "Referer": "https://www.bseindia.com/",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(request, timeout=90) as response:
        payload = json.load(response)
    return parse_bse_master(payload if isinstance(payload, list) else [])


def load_companies(url: str) -> list[CompanyCandidate]:
    """Every company row — read-only. One SELECT, no writes, no commit."""
    engine = create_engine(url, pool_pre_ping=True)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as db:
        rows = db.execute(text(
            "SELECT id, ticker, name, isin, exchange, listing_status, "
            "index_membership, currency, sector, bse_code "
            "FROM companies"
        )).all()
    engine.dispose()
    return [
        CompanyCandidate(
            company_id=r[0], ticker=r[1] or "", name=r[2] or "",
            isin=(r[3] or "").strip().upper() or None, exchange=r[4],
            listing_status=r[5], index_membership=r[6], currency=r[7],
            sector=r[8], bse_code=r[9],
        )
        for r in rows
    ]


def fetch_nifty500_list() -> tuple[frozenset[str], frozenset[str]]:
    """Today's NSE Nifty 500 constituents: (ISINs, uppercase tickers).

    The cross-check keeps NIFTY500 exclusion honest even if a constituent
    was added to the index after the last import and its tag was never
    updated. Returns two empty sets (with a warning) when the list cannot
    be fetched — the tag-based exclusion still applies, and a dry run must
    not be blocked by an auxiliary source.
    """
    try:
        request = urllib.request.Request(NIFTY500_LIST, headers={
            "User-Agent": _UA, "Accept-Encoding": "identity",
        })
        with urllib.request.urlopen(request, timeout=60) as response:
            rows = list(csv.DictReader(io.StringIO(response.read().decode(
                "utf-8-sig"))))
        isins = {
            (r.get("ISIN Code") or "").strip().upper()
            for r in rows if (r.get("ISIN Code") or "").strip()
        }
        tickers = {
            (r.get("Symbol") or "").strip().upper()
            for r in rows if (r.get("Symbol") or "").strip()
        }
        return frozenset(isins), frozenset(tickers)
    except Exception as exc:  # noqa: BLE001 — auxiliary source, not fatal
        print(f"    nifty500 live list unavailable ({exc}); "
              "tag-based exclusion still applies", file=sys.stderr)
        return frozenset(), frozenset()


def fmp_screener_market_caps(key: str) -> dict[str, tuple[float, str]]:
    """Bulk Indian listings from FMP's screener, if the plan allows it.

    Returns a map of uppercase FMP base symbol and normalised name to
    (mcap_inr_crore, source). A 402/403 from a free key is a plan limit,
    not a fault: it is recorded and the caller falls through to the
    per-company sources.
    """
    out: dict[str, tuple[float, str]] = {}
    for exchange in ("nsi", "bse"):
        offset = 0
        while True:
            url = (
                "https://financialmodelingprep.com/stable/stock-screener"
                f"?exchanges={exchange}&limit=5000&offset={offset}"
                f"&apikey={urllib.parse.quote(key)}"
            )
            try:
                request = urllib.request.Request(url, headers={"User-Agent": _UA})
                with urllib.request.urlopen(request, timeout=60) as response:
                    payload = json.load(response)
            except urllib.error.HTTPError as exc:
                print(f"  fmp screener {exchange}: HTTP {exc.code} "
                      "(plan limit) — falling through to per-company sources",
                      file=sys.stderr)
                return out
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                print(f"  fmp screener {exchange}: {exc}", file=sys.stderr)
                return out
            if not isinstance(payload, list) or not payload:
                break
            for row in payload:
                cap = row.get("marketCap")
                if not cap:
                    continue
                try:
                    crore = float(cap) / _CRORE
                except (TypeError, ValueError):
                    continue
                symbol = str(row.get("symbol") or "").upper()
                base = symbol.split(".")[0]
                entry = (crore, "fmp_screener")
                if base:
                    out.setdefault(base, entry)
                name = str(row.get("name") or "").strip()
                if name:
                    out.setdefault(normalise_name(name), entry)
            offset += len(payload)
            if offset > 20000:  # safety valve: the Indian market is <6k rows
                break
    return out


def screener_market_caps(candidates: list[CompanyCandidate],
                         known: dict[str, tuple[float, str]],
                         max_fetches: int) -> dict[str, tuple[float, str]]:
    """screener.in per candidate — the platform's proven financial source.

    ``fetch_screener`` already enforces the shared IP throttle and returns
    ``market_cap`` in crore; reusing it keeps this script on the exact
    source and cadence the production backfill uses.
    """
    from app.data.screener_source import ScreenerError, fetch_screener

    out = dict(known)
    fetches = 0
    for cand in candidates:
        if fetches >= max_fetches:
            break
        keys = [k for k in (
            (cand.isin or "").upper(), (cand.ticker or "").strip().upper(),
            normalise_name(cand.name),
        ) if k]
        if any(k in out for k in keys):
            continue
        slug = cand.ticker or cand.name
        try:
            data = fetch_screener(slug)
        except ScreenerError as exc:
            if "not listed" in str(exc):
                # Genuinely absent from screener.in — a real gap, not an
                # outage; record it so the report is honest.
                out.setdefault(normalise_name(cand.name),
                               (None, "screener_not_listed"))
            continue
        except Exception as exc:  # noqa: BLE001 — network hiccups are routine
            print(f"  screener {cand.name}: {exc}", file=sys.stderr)
            continue
        fetches += 1
        cap = getattr(data, "market_cap", None)
        if cap:
            entry = (float(cap), "screener.in")
            for key in keys:
                out.setdefault(key, entry)
        if fetches % 25 == 0:
            print(f"  screener.in: {fetches} fetched "
                  f"(~{fetches * 1.1 / 60:.0f} min elapsed at 1.1 s each)",
                  file=sys.stderr)
    return out


def fmp_profile_market_caps(candidates: list[CompanyCandidate], key: str,
                            known: dict[str, tuple[float, str]],
                            max_fetches: int) -> dict[str, tuple[float, str]]:
    """Last resort: FMP per-symbol profile, budget-aware.

    Hits the same ``/stable/profile`` payload the platform's FMP provider
    already parses (``marketCap`` off the first profile row), so the figure
    is the identical number the production market router reports.
    """
    out = dict(known)
    fetches = 0
    for cand in candidates:
        if fetches >= max_fetches:
            break
        keys = [k for k in (
            (cand.isin or "").upper(), (cand.ticker or "").strip().upper(),
            normalise_name(cand.name),
        ) if k]
        if any(k in out for k in keys):
            continue
        base = (cand.ticker or "").strip().upper() \
            or normalise_name(cand.name)[:12]
        if not base:
            continue
        for venue in (".NS", ".BO"):
            symbol = f"{base}{venue}"
            url = (
                "https://financialmodelingprep.com/stable/profile"
                f"?symbol={urllib.parse.quote(symbol)}"
                f"&apikey={urllib.parse.quote(key)}"
            )
            try:
                request = urllib.request.Request(url,
                                                 headers={"User-Agent": _UA})
                with urllib.request.urlopen(request, timeout=30) as response:
                    payload = json.load(response)
            except Exception:  # noqa: BLE001 — not listed / budget / network
                continue
            profiles = payload.get("profile") if isinstance(payload, dict) else None
            if not isinstance(profiles, list) or not profiles:
                continue
            cap = profiles[0].get("marketCap") or profiles[0].get("mktCap")
            if not cap:
                continue
            try:
                crore = float(cap) / _CRORE
            except (TypeError, ValueError):
                continue
            entry = (crore, "fmp_profile")
            for k in keys:
                out.setdefault(k, entry)
            fetches += 1
            break
    return out


def resolve_out_dir(cli_out: str | None) -> str:
    """Pick a writable output directory.

    The container's application directory (``/app``) is read-only on AWS,
    so a path next to the application is not a valid default. Resolution
    order: explicit ``--out`` (must be writable — a failed explicit path is
    an error, not a fallback), ``$UNIVERSE_REPORT_DIR``, ``./universe_725_
    investigation`` under the working directory, then
    ``/tmp/universe_725_investigation`` as the last safe in-container
    destination. For a report that must survive container rebuilds, pass a
    host-mounted path explicitly (e.g. ``/app/backups/universe_725_
    investigation`` on the AWS compose deployment, which mounts a volume).
    """
    def writable(path: str) -> bool:
        try:
            os.makedirs(path, exist_ok=True)
            probe = os.path.join(path, ".write_probe")
            with open(probe, "w") as fh:
                fh.write("ok")
            os.remove(probe)
            return True
        except OSError:
            return False

    if cli_out:
        if writable(cli_out):
            return cli_out
        raise SystemExit(
            f"--out directory {cli_out!r} is not writable "
            "(the container's /app is read-only; use a host-mounted path "
            "such as /app/backups/universe_725_investigation, or /tmp)"
        )

    candidates = []
    env_dir = os.environ.get("UNIVERSE_REPORT_DIR")
    if env_dir:
        candidates.append(env_dir)
    candidates.append(os.path.join(os.getcwd(), "universe_725_investigation"))
    candidates.append("/tmp/universe_725_investigation")

    for candidate in candidates:
        if writable(candidate):
            if candidate != candidates[-1]:
                print(f"    output directory: {candidate}", file=sys.stderr)
            return candidate
    raise SystemExit("no writable output directory found (tried: "
                     + ", ".join(candidates) + ")")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--limit", type=int, default=200,
                        help="how many ranked candidates to report (default 200)")
    parser.add_argument("--out", default=None,
                        help="output directory. Default: $UNIVERSE_REPORT_DIR, "
                             "else ./universe_725_investigation under the "
                             "current working directory, falling back to "
                             "/tmp/universe_725_investigation if neither is "
                             "writable (the container's /app is read-only; "
                             "on AWS prefer a host-mounted path such as "
                             "/app/backups/universe_725_investigation)")
    parser.add_argument("--report-only", action="store_true",
                        help="classification + same-security dedupe only — "
                             "the BSE master is still fetched (read-only) "
                             "because it is the authoritative identity "
                             "source, but no market-cap enrichment or "
                             "ranking is done")
    parser.add_argument("--no-nifty500-check", action="store_true",
                        help="skip the live NSE Nifty 500 cross-check "
                             "(tag-based NIFTY500 exclusion still applies)")
    parser.add_argument("--max-screener", type=int, default=20000,
                        help="safety valve on screener.in fetches (default: all)")
    parser.add_argument("--max-fmp-profile", type=int, default=0,
                        help="safety valve on FMP profile calls (default: 0 = off)")
    args = parser.parse_args()

    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2

    # Fail fast on an unwritable output path — before any fetch.
    out_dir = resolve_out_dir(args.out)

    print("1/5 loading companies from the database (read-only) ...")
    companies = load_companies(url)
    print(f"    {len(companies)} company records")

    if not args.no_nifty500_check:
        print("2/5 fetching today's NSE Nifty 500 list (cross-check) ...")
        n500_isins, n500_tickers = fetch_nifty500_list()
        print(f"    {len(n500_isins)} ISINs, {len(n500_tickers)} tickers")
    else:
        n500_isins, n500_tickers = frozenset(), frozenset()

    print("3/5 classifying candidates (NIFTY500 excluded) ...")
    status_counts: dict[str, int] = {}
    candidates: list[CompanyCandidate] = []
    for company in companies:
        status = classify_company(company, n500_isins, n500_tickers)
        status_counts[status] = status_counts.get(status, 0) + 1
        if status == "candidate":
            candidates.append(company)
    print(f"    classification: {status_counts}")

    # The BSE master is the authoritative identity source and is fetched once
    # (read-only). A dual-listed security arrives as two rows where the NSE
    # row often carries a NULL ISIN and only the BSE row carries the real
    # one, so the master's symbol->ISIN and scrip-code->ISIN maps resolve the
    # NULL-ISIN NSE row to the same security as its BSE listing — for DEDUPE
    # and MARKET-CAP MATCHING ONLY, never written back to the database.
    print("    fetching BSE master (authoritative identity + market cap) ...")
    bse_scrips = fetch_bse_master()
    symbol_to_isin, scrip_code_to_isin = build_bse_identity_maps(bse_scrips)
    bse_map, bse_unit = build_bse_mktcap_map(bse_scrips)
    bse_scrip_codes = {isin: code for isin, (_amt, code) in bse_map.items()}
    if bse_unit is None:
        print("    BSE master Mktcap unit could not be calibrated "
              "(no RELIANCE reference) — market cap falls to secondary "
              "sources; identity resolution still uses the master",
              file=sys.stderr)

    # Collapse same-security rows before enrichment and ranking, or one
    # security would count twice and occupy two of the 200 slots. ISIN is
    # the primary identity; the BSE master resolves NULL-ISIN NSE rows to
    # their BSE listing's ISIN; ticker/name remain the last-resort fallback.
    total_candidates = len(candidates)
    candidates = dedupe_candidates(
        candidates, symbol_to_isin=symbol_to_isin,
        scrip_code_to_isin=scrip_code_to_isin,
    )
    duplicates_collapsed = total_candidates - len(candidates)
    print(f"    deduplicated: {total_candidates} candidate rows -> "
          f"{len(candidates)} unique companies "
          f"({duplicates_collapsed} same-security rows collapsed)")

    bse_matched = 0
    secondary_matched = 0
    missing_mcap = 0
    mcap_map: dict[str, tuple[float, str]] = {}
    rows: list[CandidateRow] = []

    if args.report_only:
        print("    --report-only: identity resolved, but no market-cap "
              "enrichment or ranking")
    else:
        print("4/5 enriching market cap (read-only) ...")
        # Tier 0: the exchange's own Mktcap, matched by (resolved) ISIN.
        for cand in candidates:
            isin = resolve_identity(cand, symbol_to_isin, scrip_code_to_isin)
            if isin and isin in bse_map:
                amount, _code = bse_map[isin]
                mcap_map[isin] = (amount, "bse_master")
                bse_matched += 1
        print(f"    0. BSE master Mktcap by ISIN: unit '{bse_unit}', "
              f"{bse_matched}/{len(candidates)} matched")

        # Gap-fill, in the platform's own source order, for the companies
        # BSE did not match (NSE-only listings, no-ISIN rows, ...).
        key = os.environ.get("FMP_API_KEY", "").strip()
        if key and key != "FMP_API_KEY":
            print("    a. FMP screener (bulk, fills BSE gaps) ...")
            for _k, _v in fmp_screener_market_caps(key).items():
                mcap_map.setdefault(_k, _v)
        print(f"    b. screener.in per unmatched company "
              f"({len(candidates) - bse_matched} gaps, at ~1.1 s each) ...")
        mcap_map = screener_market_caps(candidates, mcap_map, args.max_screener)
        if key and args.max_fmp_profile:
            print(f"    c. FMP profile (up to {args.max_fmp_profile}) ...")
            mcap_map = fmp_profile_market_caps(
                candidates, key, mcap_map, args.max_fmp_profile)

        secondary_matched = sum(
            1 for cand in candidates
            if any(
                k in mcap_map and mcap_map[k][1] != "bse_master"
                for k in (
                    resolve_identity(cand, symbol_to_isin,
                                     scrip_code_to_isin) or "",
                    (cand.isin or "").upper(),
                    (cand.ticker or "").strip().upper(),
                    normalise_name(cand.name),
                ) if k
            )
        )
        missing_mcap = len(candidates) - bse_matched - secondary_matched

    print("5/5 ranking (proposed ranks 501-700) and writing report ...")
    if args.report_only:
        rows = []
        rank_counts = {"ranked": 0, "no_market_cap_available": 0,
                       "duplicates_collapsed": duplicates_collapsed}
    else:
        rows, rank_counts = rank_companies(
            candidates, mcap_map, limit=args.limit,
            # Enrich database rows with the master's scrip codes where the
            # DB row carries none of its own.
            bse_scrip_codes=bse_scrip_codes,
            # Resolve NULL-ISIN NSE rows to their BSE security for both
            # dedupe (already applied above, re-applied here as a guard) and
            # market-cap matching.
            symbol_to_isin=symbol_to_isin,
            scrip_code_to_isin=scrip_code_to_isin,
        )

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    csv_path = os.path.join(out_dir, f"next200_candidates_{stamp}.csv")
    json_path = os.path.join(out_dir, f"next200_candidates_{stamp}.json")

    coverage = {
        "total_companies_in_db": len(companies),
        "excluded_nifty500_tag": status_counts.get("excluded_nifty500_tag", 0),
        "excluded_nifty500_live_list":
            status_counts.get("excluded_nifty500_live_list", 0),
        "excluded_not_active": status_counts.get("excluded_not_active", 0),
        "excluded_non_indian": status_counts.get("excluded_non_indian", 0),
        "total_candidates": total_candidates,
        # Same security listed on NSE and BSE is one company: the rows are
        # collapsed (ISIN primary identity) before enrichment and ranking,
        # so every count below is over unique companies.
        "unique_candidates": len(candidates),
        "duplicates_collapsed": duplicates_collapsed,
        "bse_mktcap_matched": bse_matched,
        "bse_mktcap_unit": bse_unit,
        "secondary_matched": secondary_matched,
        "missing_market_cap": missing_mcap,
        "ranked": rank_counts.get("ranked", 0),
        "no_market_cap_unranked":
            rank_counts.get("no_market_cap_available", 0),
        "rank_range": (rows[0].proposed_rank, rows[-1].proposed_rank)
        if rows else None,
        "top_market_cap_inr_crore": rows[0].market_cap_inr_crore if rows else None,
        "bottom_market_cap_inr_crore":
            rows[-1].market_cap_inr_crore if rows else None,
        "mcap_sources": sorted({r.mcap_source for r in rows if r.mcap_source}),
    }
    with open(csv_path, "w", encoding="utf-8") as fh:
        fh.write(to_csv(rows))
    with open(json_path, "w", encoding="utf-8") as fh:
        fh.write(to_json(
            rows,
            generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            market_cap_as_of=stamp,
            summary=coverage,
        ))

    print("report written — read-only run, no database writes performed")
    print(f"    {csv_path}")
    print(f"    {json_path}")
    print(json.dumps(coverage, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
