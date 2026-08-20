"""Import the Nifty 500 universe from NSE's own published constituent lists.

Every field comes from an authoritative source rather than from a heuristic:

* **Symbol, name, industry, ISIN** — NSE's `ind_nifty500list.csv`, the file
  the exchange publishes to define the index.
* **Market-cap category** — NSE's Nifty 100 / Midcap 150 / Smallcap 250
  constituent lists. Those three partition the Nifty 500 exactly (100 + 150 +
  250 = 500), so a company's category is the exchange's classification, not a
  threshold we invented, and it stays correct when NSE rebalances.
* **BSE code** — joined from BSE's active-scrip master on **ISIN**, not on
  name or symbol. Names differ across exchanges ("ABB India Ltd" vs "ABB India
  Limited") and symbols occasionally collide; ISIN is the security's legal
  identifier and is the only join that is safe.

**Sector vs industry.** NSE's file supplies one taxonomy column, labelled
"Industry", holding twenty macro groupings ("Financial Services", "Capital
Goods"). Those are sectors in the sense the platform already uses the word, so
they populate `sector`. `industry` is left as the finer classification and is
only set when a provider supplies one — writing the sector into both would
make the two columns agree by construction and tell a reader nothing.

Nothing here downloads a document, computes a score, or touches R2. Import
only, as instructed.
"""
from __future__ import annotations

import csv
import io
import json
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models.company import Company
from app.services.universe.resolution import resolve_company

log = structlog.get_logger(__name__)

_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")

NSE_ARCHIVES = "https://nsearchives.nseindia.com/content/indices"
BSE_SCRIP_MASTER = (
    "https://api.bseindia.com/BseIndiaAPI/api/ListofScripData/w"
    "?Group=&Scripcode=&industry=&segment=Equity&status=Active"
)

#: Constituent list -> market-cap category. Order matters only for clarity;
#: the three sets are disjoint.
CATEGORY_SOURCES: tuple[tuple[str, str], ...] = (
    ("ind_nifty100list.csv", "largecap"),
    ("ind_niftymidcap150list.csv", "midcap"),
    ("ind_niftysmallcap250list.csv", "smallcap"),
)

INDEX_NAME = "NIFTY500"
TIMEOUT_SECONDS = 90.0


class UniverseImportError(RuntimeError):
    """The universe could not be fetched. Import is refused, not partial."""


@dataclass(slots=True)
class ImportReport:
    total_in_index: int = 0
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    failed: int = 0
    bse_matched: int = 0
    bse_unmatched: list[str] = field(default_factory=list)
    category_counts: dict[str, int] = field(default_factory=dict)
    sector_counts: dict[str, int] = field(default_factory=dict)
    missing_isin: list[str] = field(default_factory=list)
    missing_category: list[str] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)
    latency_ms: float = 0.0

    @property
    def imported(self) -> int:
        return self.created + self.updated + self.unchanged

    @property
    def ok(self) -> bool:
        return self.failed == 0 and self.imported == self.total_in_index

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_in_index": self.total_in_index,
            "imported": self.imported,
            "created": self.created,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "failed": self.failed,
            "bse_matched": self.bse_matched,
            "bse_unmatched": self.bse_unmatched,
            "bse_coverage_percent": (
                round(self.bse_matched / self.total_in_index * 100, 1)
                if self.total_in_index else 0.0
            ),
            "category_counts": self.category_counts,
            "sector_counts": self.sector_counts,
            "missing_isin": self.missing_isin,
            "missing_category": self.missing_category,
            "errors": self.errors,
            "latency_ms": round(self.latency_ms, 1),
            "ok": self.ok,
        }


@dataclass(frozen=True, slots=True)
class Constituent:
    symbol: str
    name: str
    sector: str | None
    isin: str | None
    category: str | None
    bse_code: str | None


def _fetch(url: str, *, referer: str | None = None) -> bytes:
    headers = {
        "User-Agent": _UA,
        # Never advertise gzip: urllib does not transparently decompress and
        # the body comes back as bytes that fail to decode.
        "Accept-Encoding": "identity",
    }
    if referer:
        headers["Referer"] = referer
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return response.read()
    except (urllib.error.HTTPError, urllib.error.URLError, OSError) as exc:
        raise UniverseImportError(f"could not fetch {url}: {exc}") from exc


def fetch_index(filename: str) -> list[dict[str, str]]:
    """One NSE constituent CSV as a list of rows."""
    body = _fetch(f"{NSE_ARCHIVES}/{filename}").decode("utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(body)))
    if not rows:
        raise UniverseImportError(f"{filename} returned no rows")
    return rows


def fetch_categories() -> dict[str, str]:
    """Symbol -> market-cap category, from NSE's own constituent indices."""
    categories: dict[str, str] = {}
    for filename, label in CATEGORY_SOURCES:
        try:
            for row in fetch_index(filename):
                symbol = (row.get("Symbol") or "").strip().upper()
                if symbol:
                    categories[symbol] = label
        except UniverseImportError as exc:
            # A missing category is a gap in enrichment, not a reason to
            # abandon the import; the company still belongs in the universe.
            log.warning("category source unavailable", file=filename,
                        error=str(exc)[:160])
    return categories


def fetch_bse_codes() -> dict[str, str]:
    """ISIN -> BSE scrip code, from BSE's active-scrip master.

    Joined on ISIN rather than name or symbol: names differ across exchanges
    and symbols occasionally collide, whereas ISIN is the security's legal
    identifier.
    """
    try:
        payload = json.loads(
            _fetch(BSE_SCRIP_MASTER, referer="https://www.bseindia.com/")
        )
    except (UniverseImportError, json.JSONDecodeError) as exc:
        log.warning("BSE scrip master unavailable", error=str(exc)[:160])
        return {}

    out: dict[str, str] = {}
    for row in payload if isinstance(payload, list) else []:
        isin = str(row.get("ISIN_NUMBER") or "").strip().upper()
        code = str(row.get("SCRIP_CD") or "").strip()
        if isin and code:
            out[isin] = code
    return out


def build_universe() -> list[Constituent]:
    """The Nifty 500, enriched with category and BSE code."""
    rows = fetch_index("ind_nifty500list.csv")
    categories = fetch_categories()
    bse = fetch_bse_codes()

    out: list[Constituent] = []
    for row in rows:
        symbol = (row.get("Symbol") or "").strip().upper()
        if not symbol:
            continue
        isin = (row.get("ISIN Code") or "").strip().upper() or None
        # NSE's "Industry" column holds macro groupings — Financial Services,
        # Capital Goods — which are sectors in this platform's vocabulary.
        sector = (row.get("Industry") or "").strip() or None
        out.append(Constituent(
            symbol=symbol,
            name=(row.get("Company Name") or symbol).strip(),
            sector=sector,
            isin=isin,
            category=categories.get(symbol),
            bse_code=bse.get(isin) if isin else None,
        ))
    return out


class Nifty500Importer:
    """Creates or updates company records for the Nifty 500."""

    def __init__(self, db: Any) -> None:
        self.db = db

    def _existing(self, symbol: str, isin: str | None) -> Company | None:
        """Match on ISIN first, then symbol.

        ISIN is the stronger key: a company that renames its ticker keeps its
        ISIN, and matching on symbol alone would create a duplicate row for
        the same security. Symbol is the fallback for the 135 companies
        already present, some of which predate ISIN being populated.

        The symbol fallback resolves through :func:`resolve_company` so a
        legacy duplicate can never make the import write to an arbitrary twin
        — the financial-history owner is the canonical row.
        """
        if isin:
            found = self.db.scalar(select(Company).where(Company.isin == isin))
            if found is not None:
                return found
        return resolve_company(self.db, symbol, exchange="NSE")

    def run(self, *, dry_run: bool = False) -> ImportReport:
        started = time.perf_counter()
        report = ImportReport()

        universe = build_universe()
        report.total_in_index = len(universe)

        for item in universe:
            try:
                self._upsert(item, report, dry_run=dry_run)
            except Exception as exc:  # noqa: BLE001 — one bad row must not stop 500
                report.failed += 1
                report.errors.append({
                    "symbol": item.symbol,
                    "error": f"{type(exc).__name__}: {exc}"[:200],
                })
                log.warning("company import failed", symbol=item.symbol,
                            error=str(exc)[:160])
                self.db.rollback()

            if item.bse_code:
                report.bse_matched += 1
            else:
                report.bse_unmatched.append(item.symbol)
            if not item.isin:
                report.missing_isin.append(item.symbol)
            if not item.category:
                report.missing_category.append(item.symbol)
            key = item.category or "uncategorised"
            report.category_counts[key] = report.category_counts.get(key, 0) + 1
            sector = item.sector or "unclassified"
            report.sector_counts[sector] = report.sector_counts.get(sector, 0) + 1

        if not dry_run:
            self.db.commit()

        report.latency_ms = (time.perf_counter() - started) * 1000
        log.info("nifty500 import complete", **{
            k: v for k, v in report.as_dict().items()
            if k not in ("sector_counts", "bse_unmatched", "missing_isin",
                         "missing_category", "errors")
        })
        return report

    def _upsert(self, item: Constituent, report: ImportReport, *,
                dry_run: bool) -> None:
        company = self._existing(item.symbol, item.isin)

        if company is None:
            if dry_run:
                report.created += 1
                return
            self.db.add(Company(
                id=str(uuid.uuid4()),
                ticker=item.symbol,
                name=item.name,
                exchange="NSE",
                isin=item.isin,
                bse_code=item.bse_code,
                sector=item.sector,
                # `industry` deliberately left unset: NSE supplies one
                # taxonomy column and it is the sector. Copying it into both
                # would make them agree by construction and inform nobody.
                industry=None,
                market_cap_category=item.category,
                listing_status="active",
                index_membership=INDEX_NAME,
                currency="INR",
                reporting_scale="crore",
            ))
            try:
                # The flush is the race arbiter: uq_company_ticker_exchange
                # rejects a concurrent duplicate insert for the same symbol,
                # and the losing import merges into the winner instead of
                # leaving two identity rows.
                self.db.flush()
            except IntegrityError:
                self.db.rollback()
                company = self._existing(item.symbol, item.isin)
                if company is None:  # pragma: no cover — the constraint said a row exists
                    raise
                log.warning("nifty500 import lost a creation race; "
                            "merging into the existing row", symbol=item.symbol)
            else:
                report.created += 1
                return

        # Update only fields this import is authoritative for, and only when
        # they actually change — so `updated` means something.
        changes = 0
        for attribute, value in (
            ("name", item.name),
            ("isin", item.isin),
            ("bse_code", item.bse_code),
            ("sector", item.sector),
            ("market_cap_category", item.category),
            ("index_membership", INDEX_NAME),
            ("listing_status", "active"),
        ):
            if value is None:
                continue
            if getattr(company, attribute, None) != value:
                if not dry_run:
                    setattr(company, attribute, value)
                changes += 1

        if changes:
            report.updated += 1
        else:
            report.unchanged += 1
