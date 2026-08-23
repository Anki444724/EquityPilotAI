#!/usr/bin/env python3
"""Derive the next-200 coverage candidates (proposed ranks 501-700).

Read-only against the production database. It generates a report file; it
does NOT import companies, backfill financials, or write any row.

    export DATABASE_URL="postgresql+psycopg://..."
    python3 deploy/derive_next_200.py --report-only        # exclusion only
    python3 deploy/derive_next_200.py --limit 200          # full report

Pipeline
--------
1. BSE active-scrip master (segment=Equity, status=Active) — the same
   exchange endpoint the Nifty 500 importer already calls; candidate
   universe, with ISIN and name for every scrip.
2. Current universe from the ``companies`` table (every existing record,
   read-only) — excluded by ISIN first, then uppercase ticker.
3. Market cap, in the platform's own source order:
   a. FMP screener for the Indian exchanges — one bulk listing with
      ``marketCap`` per stock, when the key's plan allows it;
   b. screener.in per candidate — the platform's primary financial source,
      already proven in production (``app.data.screener_source``), which
      parses ``market_cap`` from each company's overview;
   c. FMP per-symbol profile — last resort, budget-aware.
4. Rank by market cap, take the top N, write CSV + JSON.

Screener.in throttles a single IP (the shared source enforces ~1.1 s per
request), so a full first run over ~4,400 candidates takes roughly an hour;
it is resumable — the JSON report records which candidates still lack a
figure and a rerun continues from there.
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
import time
import urllib.error
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
    BseScrip, CandidateRow, UniverseEntry, calibrate_bse_mktcap, classify,
    normalise_name, parse_bse_master, rank_candidates, rows_to_dicts,
    to_csv, to_json, universe_sets,
)

_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
BSE_SCRIP_MASTER = (
    "https://api.bseindia.com/BseIndiaAPI/api/ListofScripData/w"
    "?Group=&Scripcode=&industry=&segment=Equity&status=Active"
)
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


def load_universe(url: str) -> list[UniverseEntry]:
    """Every existing company record — read-only, no writes, no commits."""
    engine = create_engine(url, pool_pre_ping=True)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as db:
        rows = db.execute(text(
            "SELECT ticker, isin, listing_status FROM companies"
        )).all()
    engine.dispose()
    return [UniverseEntry(ticker=r[0] or "", isin=r[1] or None,
                          listing_status=r[2]) for r in rows]


def bse_master_market_caps(
    scrips: list[BseScrip],
) -> tuple[dict[str, tuple[float, str]], str | None]:
    """Tier 0: the exchange's own ``Mktcap`` figure, already in hand.

    Free (no extra call) and authoritative (the listing exchange reports
    its own companies). The unit is undocumented, so it is calibrated
    first (see ``calibrate_bse_mktcap``); when the unit cannot be resolved
    the tier is skipped rather than used with a guessed unit, and the
    fallback tiers run instead.

    Returns ``(mcap_map, unit)`` where the map is keyed by ISIN, ticker and
    normalised name to ``(inr_crore, "bse_master")``.
    """
    unit, factor = calibrate_bse_mktcap(scrips)
    if unit is None:
        print("    0. BSE master Mktcap: unit could not be calibrated "
              "(no RELIANCE reference) — skipping this tier", file=sys.stderr)
        return {}, None
    out: dict[str, tuple[float, str]] = {}
    for scrip in scrips:
        if not scrip.mktcap or scrip.mktcap <= 0:
            continue
        entry = (round(scrip.mktcap * factor, 1), "bse_master")
        keys = [k for k in (
            (scrip.isin or "").upper(), scrip.ticker,
            normalise_name(scrip.name),
        ) if k]
        for key in keys:
            out.setdefault(key, entry)
    print(f"    0. BSE master Mktcap: unit resolved as '{unit}', "
          f"{len(out)} identifiers with a figure", file=sys.stderr)
    return out, unit


def fmp_screener_market_caps(key: str) -> dict[str, tuple[float, str]]:
    """Bulk Indian listings from FMP's screener, if the plan allows it.

    Returns a map of (ISIN? no —) uppercase FMP base symbol and normalised
    name to (mcap_inr_crore, source). A 402/403 from a free key is a plan
    limit, not a fault: it is recorded and the caller falls through to the
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
                    out[base] = entry
                name = str(row.get("name") or "").strip()
                if name:
                    out[normalise_name(name)] = entry
            offset += len(payload)
            if offset > 20000:  # safety valve: the Indian market is <6k rows
                break
    return out


def screener_market_caps(scrips: list[BseScrip],
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
    for scrip in scrips:
        if fetches >= max_fetches:
            break
        keys = [k for k in (
            (scrip.isin or "").upper(), scrip.ticker,
            normalise_name(scrip.name),
        ) if k]
        if any(k in out for k in keys):
            continue
        slug = scrip.ticker or scrip.name
        try:
            data = fetch_screener(slug)
        except ScreenerError as exc:
            if "not listed" in str(exc):
                # Genuinely absent from screener.in — a real gap, not an
                # outage; record it so the report is honest.
                out[normalise_name(scrip.name)] = (None, "screener_not_listed")
            continue
        except Exception as exc:  # noqa: BLE001 — network hiccups are routine
            print(f"  screener {scrip.name}: {exc}", file=sys.stderr)
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


def fmp_profile_market_caps(scrips: list[BseScrip], key: str,
                            known: dict[str, tuple[float, str]],
                            max_fetches: int) -> dict[str, tuple[float, str]]:
    """Last resort: FMP per-symbol profile, budget-aware.

    Hits the same ``/stable/profile`` payload the platform's FMP provider
    already parses (``marketCap`` off the first profile row), so the figure
    is the identical number the production market router reports.
    """
    import urllib.parse

    out = dict(known)
    fetches = 0
    for scrip in scrips:
        if fetches >= max_fetches:
            break
        keys = [k for k in (
            (scrip.isin or "").upper(), scrip.ticker,
            normalise_name(scrip.name),
        ) if k]
        if any(k in out for k in keys):
            continue
        base = scrip.ticker or normalise_name(scrip.name)[:12]
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
    so the previous default — a path next to this script — failed at
    write time, *after* the run had already fetched everything. Resolution
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
                        help="exclusion classification only — no market cap fetch")
    parser.add_argument("--max-screener", type=int, default=20000,
                        help="safety valve on screener.in fetches (default: all)")
    parser.add_argument("--max-fmp-profile", type=int, default=0,
                        help="safety valve on FMP profile calls (default: 0 = off)")
    args = parser.parse_args()

    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2

    # Fail fast on an unwritable output path — before the BSE fetch and the
    # hour-long market-cap sweep, not after them.
    out_dir = resolve_out_dir(args.out)

    print("1/4 fetching BSE active-scrip master ...")
    scrips = fetch_bse_master()
    print(f"    {len(scrips)} active equity scrips")

    print("2/4 loading current universe (read-only) ...")
    entries = load_universe(url)
    isins, tickers = universe_sets(entries)
    print(f"    {len(entries)} existing company records "
          f"({len(isins)} ISINs, {len(tickers)} tickers)")

    status_counts: dict[str, int] = {}
    candidates: list[BseScrip] = []
    for scrip in scrips:
        status = classify(scrip, isins, tickers)
        status_counts[status] = status_counts.get(status, 0) + 1
        if status == "candidate":
            candidates.append(scrip)
    print(f"    classification: {status_counts}")

    if args.report_only:
        print("    --report-only: skipping market cap")
        rows: list[CandidateRow] = []
        mcap_map: dict[str, tuple[float, str]] = {}
    else:
        print("3/4 fetching market caps ...")
        mcap_map: dict[str, tuple[float, str]] = {}
        # Tier 0: the exchange's own figure, already in hand — no extra
        # calls. Lower tiers only fill the gaps it leaves.
        mcap_map, _bse_unit = bse_master_market_caps(scrips)
        key = os.environ.get("FMP_API_KEY", "").strip()
        if key and key != "FMP_API_KEY":
            print("    a. FMP screener (bulk, fills BSE gaps) ...")
            for _k, _v in fmp_screener_market_caps(key).items():
                mcap_map.setdefault(_k, _v)
            print(f"       {len(mcap_map)} identifiers with market cap")
        print(f"    b. screener.in per candidate "
              f"({len(candidates)} candidates, gaps only) ...")
        mcap_map = screener_market_caps(candidates, mcap_map, args.max_screener)
        if key and args.max_fmp_profile:
            print(f"    c. FMP profile (up to {args.max_fmp_profile}) ...")
            mcap_map = fmp_profile_market_caps(
                candidates, key, mcap_map, args.max_fmp_profile)

    rows, counts = rank_candidates(
        scrips, isins, tickers, mcap_map, limit=args.limit,
    )

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    csv_path = os.path.join(out_dir, f"next200_candidates_{stamp}.csv")
    json_path = os.path.join(out_dir, f"next200_candidates_{stamp}.json")

    summary = {
        "bse_active_equity_scrips": len(scrips),
        "current_universe_records": len(entries),
        "exclusion_counts": status_counts,
        "ranking_counts": counts,
        "reported": len(rows),
        "rank_range": (rows[0].proposed_rank, rows[-1].proposed_rank)
        if rows else None,
        "top_market_cap_inr_crore": rows[0].market_cap_inr_crore if rows else None,
        "bottom_market_cap_inr_crore": rows[-1].market_cap_inr_crore if rows else None,
        "sources": sorted({r.mcap_source for r in rows if r.mcap_source}),
    }
    with open(csv_path, "w", encoding="utf-8") as fh:
        fh.write(to_csv(rows))
    with open(json_path, "w", encoding="utf-8") as fh:
        fh.write(to_json(
            rows,
            generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            market_cap_as_of=stamp,
            summary=summary,
        ))

    print("4/4 report written (no database writes performed)")
    print(f"    {csv_path}")
    print(f"    {json_path}")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
