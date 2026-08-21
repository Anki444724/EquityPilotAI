"""Company-universe synchronisation — Phase 1.

Extends the Nifty 500 importer's proven core to a 5,000-company universe
without forking its rules:

* **Identity** — exactly `Nifty500Importer._existing`'s ladder, generalised:
  ISIN first, then (ticker, exchange), then BSE scrip code. An existing
  company is UPDATED in place — its id, ticker and exchange survive — and
  only a genuinely new security INSERTs. There is no second identity system.
* **Batched and resumable** — rows are committed per batch, and each run
  records `next_index` in `ingestion_runs.stats`, so a crashed job resumes at
  its batch boundary instead of re-walking (and re-billing) the universe.
* **Failure-isolated** — one bad record cannot stop the run: it is recorded
  in `ingestion_failures` with its verbatim reason and the shared
  transient/permanent classification, then skipped.
* **Provider-labelled** — every row written here carries
  `metadata_source`, so a mock universe (`source='mock'`) is separable from
  real master data by query, forever.

Sources:
  mock        deterministic 5,000 synthetic companies (offline, test/dev)
  nifty500    the existing real import (NSE constituent CSVs + BSE master)
  full        NSE all-equity master ∪ BSE active-scrip master (real; network)

The mock generator deliberately uses ISINs with the `INM` prefix, tickers
with the `MCK` prefix and BSE codes in the reserved `9xxxxx` band, so a mock
identity can neither collide with nor be mistaken for a real security.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

import structlog
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.company import Company, FinancialFact
from app.models.ingestion import IngestionFailure, IngestionRun
from app.services.universe.financials_backfill import classify_ingest_failure

log = structlog.get_logger(__name__)

_EXCHANGES_IN = ("NSE", "BSE", "NSE/BSE")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ===========================================================================
# Records
# ===========================================================================
@dataclass(frozen=True, slots=True)
class UniverseRecord:
    """One company as a master source describes it."""

    ticker: str                     # NSE symbol, or BSE scrip code when BSE-only
    name: str
    exchange: str = "NSE"
    isin: str | None = None
    bse_code: str | None = None
    sector: str | None = None
    industry: str | None = None
    market_cap_category: str | None = None
    listing_status: str = "active"
    source: str = "nse_master"


# ===========================================================================
# Deterministic mock universe
# ===========================================================================
_SECTORS: tuple[tuple[str, str], ...] = (
    ("Financial Services", "Banking"),
    ("Financial Services", "NBFC"),
    ("Information Technology", "IT Services & Consulting"),
    ("Information Technology", "Software Products"),
    ("Healthcare", "Pharmaceuticals"),
    ("Healthcare", "Hospitals & Diagnostics"),
    ("Fast Moving Consumer Goods", "Packaged Foods"),
    ("Automobile and Auto Components", "Passenger Vehicles"),
    ("Automobile and Auto Components", "Auto Components"),
    ("Capital Goods", "Industrial Products"),
    ("Capital Goods", "Electrical Equipment"),
    ("Construction", "Civil Construction"),
    ("Energy", "Refineries & Marketing"),
    ("Energy", "Power Generation"),
    ("Metals and Mining", "Iron & Steel"),
    ("Metals and Mining", "Aluminium"),
    ("Consumer Durables", "Consumer Electronics"),
    ("Telecommunication", "Telecom Services"),
    ("Textiles", "Textiles & Apparels"),
    ("Chemicals", "Speciality Chemicals"),
    ("Realty", "Real Estate"),
    ("Services", "Logistics"),
)

_NAME_HEADS = (
    "Aurora", "Bramhaputra", "Chandni", "Deccan", "Everest", "Falcon",
    "Ganga", "Himalaya", "Indus", "Jamuna", "Kaveri", "Lakshmi",
    "Malabar", "Nilgiri", "Orient", "Panchami", "Rohini", "Sahyadri",
    "Thar", "Utkal", "Vindhya", "Yamini",
)
_NAME_MIDS = ("Industries", "Technologies", "Chemicals", "Infra",
              "Holdings", "Ventures", "Corporation", "Enterprises")
_NAME_TAILS = ("Ltd", "Ltd", "Ltd", "Limited")  # Ltd dominates, like reality


def _mock_rng(index: int, salt: str) -> float:
    """Deterministic 0..1 float for (index, salt) — same trick as the mock
    provider, kept local so the universe generator has no cross-module state."""
    import hashlib

    digest = hashlib.sha256(f"universe|{salt}|{index}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / float(1 << 64)


def generate_mock_universe(size: int = 5_000) -> list[UniverseRecord]:
    """`size` deterministic synthetic Indian companies.

    The 500-company real universe must coexist with this one in the same
    database (that coexistence is itself tested), so every generated identity
    is namespaced away from reality:
      ticker  MCK####            (no real NSE symbol starts MCK)
      ISIN    INM#########      (real Indian equity ISINs use INE)
      BSE     9#####             (active scrip codes do not start 9)
    """
    records: list[UniverseRecord] = []
    for i in range(size):
        u_name = _mock_rng(i, "name")
        u_mid = _mock_rng(i, "mid")
        u_tail = _mock_rng(i, "tail")
        u_sector = _mock_rng(i, "sector")
        u_isin = _mock_rng(i, "isin")
        u_dual = _mock_rng(i, "dual")
        u_cat = _mock_rng(i, "cat")

        name = (
            f"{_NAME_HEADS[int(u_name * len(_NAME_HEADS))]} "
            f"{_NAME_MIDS[int(u_mid * len(_NAME_MIDS))]} "
            f"{_NAME_TAILS[int(u_tail * len(_NAME_TAILS))]}"
        )
        sector, industry = _SECTORS[int(u_sector * len(_SECTORS))]
        # A handful of distinct real-sounding suffixes keep names unique.
        name = f"{name} {i:04d}"

        isin = f"INM{int(u_isin * 999_999_999):09d}"
        # ~85% dual-listed (carrying a BSE code), the rest NSE-only. The
        # scrip code is derived from the index, NOT drawn randomly: real
        # scrip codes are unique, and randomly drawing ~4,250 codes from a
        # ~96,000 pool collides ~90 times by the birthday paradox — which the
        # identity ladder would then correctly merge into one company,
        # silently shrinking the universe. Uniqueness by construction.
        dual = u_dual < 0.85
        records.append(UniverseRecord(
            ticker=f"MCK{i:04d}",
            name=name,
            exchange="NSE",
            isin=isin if u_isin > 0.005 else None,   # a few, like reality, lack ISIN
            bse_code=f"{900000 + i}" if dual else None,
            sector=sector,
            industry=industry,
            market_cap_category=(
                None if u_cat < 0.08 else
                "largecap" if u_cat < 0.18 else
                "midcap" if u_cat < 0.40 else "smallcap"
            ),
            source="mock",
        ))
    return records


# ===========================================================================
# Real sources
# ===========================================================================
def nifty500_records() -> list[UniverseRecord]:
    """The existing real import, expressed as UniverseRecords.

    Delegates to `Nifty500Importer`'s own fetchers — one implementation of
    'what the NSE says the universe is', not two.
    """
    from app.services.universe.nifty500 import build_universe

    return [
        UniverseRecord(
            ticker=item.symbol, name=item.name, exchange="NSE",
            isin=item.isin, bse_code=item.bse_code, sector=item.sector,
            market_cap_category=item.category, source="nse_master",
        )
        for item in build_universe()
    ]


def full_market_records() -> list[UniverseRecord]:
    """NSE all-equity master ∪ BSE active scrips — the ~5,000-company real source.

    Both masters carry ISIN, so the union is joined on ISIN exactly as the
    Nifty 500 importer joins the BSE scrip master. Network is required; any
    operator running this must accept the exchange sites' terms.
    """
    from app.services.universe.nifty500 import (
        BSE_SCRIP_MASTER, NSE_ARCHIVES, UniverseImportError, _fetch,
    )

    rows: dict[str, UniverseRecord] = {}

    # NSE's full equity list (same archive host as the constituent CSVs).
    import csv
    import io

    body = _fetch(f"{NSE_ARCHIVES}/../equities/EQUITY_L.csv").decode("utf-8-sig")
    for row in csv.DictReader(io.StringIO(body)):
        symbol = (row.get("SYMBOL") or "").strip().upper()
        if not symbol:
            continue
        isin = (row.get("ISIN NUMBER") or "").strip().upper() or None
        rows[symbol] = UniverseRecord(
            ticker=symbol,
            name=(row.get("NAME OF COMPANY") or symbol).strip(),
            exchange="NSE",
            isin=isin,
            sector=(row.get("SERIES") or None),
            source="nse_master",
        )

    # BSE active scrips, joined on ISIN where NSE already has the company.
    import json

    payload = json.loads(_fetch(BSE_SCRIP_MASTER, referer="https://www.bseindia.com/"))
    by_isin = {r.isin: r for r in rows.values() if r.isin}
    for entry in payload if isinstance(payload, list) else []:
        isin = str(entry.get("ISIN_NUMBER") or "").strip().upper()
        code = str(entry.get("SCRIP_CD") or "").strip()
        if not isin or not code:
            continue
        existing = by_isin.get(isin)
        if existing is not None:
            # Same security on both venues: enrich, never duplicate.
            rows[existing.ticker] = UniverseRecord(
                ticker=existing.ticker, name=existing.name, exchange="NSE/BSE",
                isin=existing.isin, bse_code=code, sector=existing.sector,
                source=existing.source,
            )
        else:
            rows[f"BSE{code}"] = UniverseRecord(
                ticker=code,  # BSE-only: identity is (scrip code, BSE)
                name=str(entry.get("Scrip_Name") or code).strip(),
                exchange="BSE", isin=isin, bse_code=code,
                sector=(entry.get("segment") or None), source="bse_master",
            )

    return list(rows.values())


def resolve_source(name: str | None = None) -> str:
    """Which source a sync run uses. 'auto' follows DATA_PROVIDER, so mock
    mode never reaches for a real exchange master and real mode never
    generates synthetic companies."""
    from app.core.config import settings

    if name and name != "auto":
        return name
    if settings.DATA_PROVIDER.lower() == "mock":
        return "mock"
    return "full"


def records_for_source(source: str, limit: int | None = None) -> list[UniverseRecord]:
    if source == "mock":
        from app.core.config import settings

        size = limit or settings.MOCK_UNIVERSE_SIZE
        return generate_mock_universe(size)
    if source == "nifty500":
        return nifty500_records()
    if source == "full":
        records = full_market_records()
        return records[:limit] if limit else records
    raise ValueError(f"unknown universe source '{source}'")


# ===========================================================================
# Sync report
# ===========================================================================
@dataclass(slots=True)
class UniverseSyncReport:
    source: str = "unknown"
    total_discovered: int = 0
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    #: Rows that matched an existing company through the identity ladder —
    #: the duplicates a naive import would have created.
    duplicates_prevented: int = 0
    failed: int = 0
    missing_isin: list[str] = field(default_factory=list)
    missing_exchange: list[str] = field(default_factory=list)
    #: Identity-quality audit at the end of the run (see _integrity()).
    duplicate_identities: int = 0
    companies_without_financials: int = 0
    next_index: int = 0
    run_id: int | None = None
    resumed_from: int = 0
    duration_seconds: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "total_discovered": self.total_discovered,
            "inserted": self.inserted,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "duplicates_prevented": self.duplicates_prevented,
            "failed": self.failed,
            "missing_isin": len(self.missing_isin),
            "missing_isin_symbols": self.missing_isin[:25],
            "missing_exchange": len(self.missing_exchange),
            "missing_exchange_symbols": self.missing_exchange[:25],
            "duplicate_identities": self.duplicate_identities,
            "companies_without_financials": self.companies_without_financials,
            "next_index": self.next_index,
            "resumed_from": self.resumed_from,
            "run_id": self.run_id,
            "duration_seconds": round(self.duration_seconds, 2),
        }


# ===========================================================================
# The service
# ===========================================================================
class CompanyUniverseService:
    """Batched, resumable, identity-preserving upsert of the company master."""

    def __init__(self, db: Session) -> None:
        self.db = db

    # ------------------------------------------------------------- identity
    def _find_existing(
        self, ticker: str, exchange: str, isin: str | None, bse_code: str | None,
    ) -> Company | None:
        """The identity ladder, verbatim from the Nifty 500 importer:
        ISIN first (a renamed ticker keeps its ISIN), then (ticker, exchange)
        within the Indian venue family, then the BSE scrip code.

        The exchange fallback is scoped to the Indian family on BOTH sides:
        an incoming NSE row must match an existing NSE or NSE/BSE row (and
        vice versa), but an incoming NASDAQ row with a colliding symbol is a
        genuinely different security — matching it would silently absorb a
        foreign listing into an Indian company, which is exactly what
        `uq_company_ticker_exchange` exists to permit.
        """
        if isin:
            found = self.db.scalar(select(Company).where(Company.isin == isin))
            if found is not None:
                return found
        if (exchange or "").upper() in _EXCHANGES_IN:
            found = self.db.scalar(
                select(Company).where(
                    Company.ticker == ticker,
                    Company.exchange.in_(_EXCHANGES_IN),
                )
            )
            if found is not None:
                return found
        if bse_code:
            found = self.db.scalar(
                select(Company).where(Company.bse_code == bse_code)
            )
            if found is not None:
                return found
        return None

    # ----------------------------------------------------------------- sync
    def sync(
        self,
        records: Sequence[UniverseRecord],
        *,
        source: str = "nse_master",
        batch_size: int = 500,
        max_batches: int | None = None,
        start_index: int = 0,
        run_id: int | None = None,
        job_id: int | None = None,
    ) -> UniverseSyncReport:
        """Upsert `records` in committed batches, isolated per-record failures.

        `start_index` resumes a previous run (the job handler reads it from
        the last run's `stats.next_index`), and `max_batches` bounds one
        invocation so a scheduled job can never run unbounded.
        """
        started = time.perf_counter()
        report = UniverseSyncReport(
            source=source,
            total_discovered=len(records),
            resumed_from=start_index,
        )

        own_run = run_id is None
        if own_run:
            run = IngestionRun(
                kind="company_universe_sync", provider=source,
                started_at=_utcnow(),
                stats={"total": len(records), "start_index": start_index},
            )
            if job_id is not None:
                run.job_id = job_id
            self.db.add(run)
            self.db.commit()
            run_id = run.id
        report.run_id = run_id

        end = len(records)
        if max_batches is not None:
            end = min(end, start_index + max_batches * batch_size)

        index = start_index
        while index < end:
            batch = records[index:min(index + batch_size, end)]
            for record in batch:
                try:
                    # A savepoint per record: rolling back one bad row must
                    # not discard the good rows already staged in this batch.
                    # A bare rollback() here would throw away every uncommitted
                    # insert before the failing one — the exact opposite of
                    # "one bad row must not stop the run".
                    with self.db.begin_nested():
                        self._upsert_record(record, report)
                except Exception as exc:  # noqa: BLE001 — one row must not stop the run
                    report.failed += 1
                    self._record_failure(
                        run_id, "company_universe_sync", record.ticker, str(exc),
                        payload={"record": _record_payload(record)},
                    )
                    log.warning("universe sync row failed",
                                symbol=record.ticker, error=str(exc)[:200])
                if record.isin is None and record.ticker not in report.missing_isin:
                    report.missing_isin.append(record.ticker)
                if not record.exchange and record.ticker not in report.missing_exchange:
                    report.missing_exchange.append(record.ticker)
            index += len(batch)
            self._update_run(run_id, report, index)
            self.db.commit()          # per-batch commit: crash-safe progress

        report.next_index = index
        report.duration_seconds = time.perf_counter() - started

        if index >= len(records):
            # Terminal pass: integrity + financial-coverage audit, recorded on
            # the run so the report answers the questions the brief asks.
            report.duplicate_identities = self._duplicate_identity_count()
            report.companies_without_financials = self._companies_without_financials()

        self._finish_run(run_id, report)
        log.info("company universe sync", **report.as_dict())
        return report

    def resume_position(self, kind: str = "company_universe_sync") -> int:
        """Where the last incomplete run stopped, so the next run resumes."""
        run = self.db.scalar(
            select(IngestionRun)
            .where(IngestionRun.kind == kind)
            .order_by(IngestionRun.id.desc())
            .limit(1)
        )
        if run is None or run.finished_at is not None:
            return 0
        stats = run.stats or {}
        return int(stats.get("next_index") or 0)

    # ------------------------------------------------------------- internals
    def _upsert_record(self, record: UniverseRecord, report: UniverseSyncReport) -> None:
        company = self._find_existing(
            record.ticker, record.exchange, record.isin, record.bse_code,
        )

        if company is None:
            self.db.add(Company(
                id=str(uuid.uuid4()),
                ticker=record.ticker,
                name=record.name,
                exchange=record.exchange,
                isin=record.isin,
                bse_code=record.bse_code,
                sector=record.sector,
                industry=record.industry,
                market_cap_category=record.market_cap_category,
                listing_status=record.listing_status,
                currency="INR",
                reporting_scale="crore",
                metadata_source=record.source,
                metadata_synced_at=_utcnow(),
            ))
            report.inserted += 1
            return

        # Existing company: refresh the fields this source is authoritative
        # for, preserving the id (and, by construction, the identity keys).
        report.duplicates_prevented += 1
        changes = 0
        for attr, value in (
            ("name", record.name),
            ("isin", record.isin),
            ("bse_code", record.bse_code),
            ("sector", record.sector),
            ("industry", record.industry),
            ("market_cap_category", record.market_cap_category),
            ("listing_status", record.listing_status),
            ("metadata_source", record.source),
        ):
            if value is None:
                continue
            if getattr(company, attr, None) != value:
                setattr(company, attr, value)
                changes += 1
        # The sync stamp always refreshes — it records that this master was
        # consulted — but never counts as a change, so 'updated' keeps meaning
        # 'a field actually moved'.
        company.metadata_synced_at = _utcnow()
        if changes:
            report.updated += 1
        else:
            report.unchanged += 1

    def _record_failure(
        self, run_id: int | None, kind: str, symbol: str, error: str, *,
        payload: dict[str, Any] | None = None,
    ) -> None:
        if run_id is None:
            return
        try:
            failure = IngestionFailure(
                run_id=run_id, kind=kind, symbol=symbol,
                error=(error or "unknown error")[:2000],
                failure_kind=classify_ingest_failure(error).value,
                last_attempt_at=_utcnow(),
            )
            if payload:
                failure.payload = payload
            self.db.add(failure)
            self.db.commit()
        except Exception:  # noqa: BLE001 — observability must not raise
            self.db.rollback()

    def _update_run(self, run_id: int | None, report: UniverseSyncReport, index: int) -> None:
        if run_id is None:
            return
        run = self.db.get(IngestionRun, run_id)
        if run is None:
            return
        run.succeeded = report.inserted + report.updated + report.unchanged
        run.failed = report.failed
        run.stats = {
            "total": report.total_discovered,
            "next_index": index,
            "inserted": report.inserted,
            "updated": report.updated,
            "unchanged": report.unchanged,
            "duplicates_prevented": report.duplicates_prevented,
        }

    def _finish_run(self, run_id: int | None, report: UniverseSyncReport) -> None:
        if run_id is None:
            return
        run = self.db.get(IngestionRun, run_id)
        if run is None:
            return
        run.finished_at = _utcnow()
        run.stats = {
            **(run.stats or {}),
            "next_index": report.next_index,
            "duplicate_identities": report.duplicate_identities,
            "companies_without_financials": report.companies_without_financials,
            "duration_seconds": round(report.duration_seconds, 2),
        }

    # ---------------------------------------------------------------- audits
    def _duplicate_identity_count(self) -> int:
        """Rows that would violate the identity rules if they existed.

        ISIN is UNIQUE in the schema, so duplicates are impossible for ISIN;
        what this measures is (ticker, exchange) collisions the ladder could
        have produced had it not matched — and it must always read zero.
        """
        pairs = self.db.execute(
            select(Company.ticker, Company.exchange, func.count())
            .where(Company.deleted_at.is_(None))
            .group_by(Company.ticker, Company.exchange)
            .having(func.count() > 1)
        ).all()
        return len(pairs)

    def _companies_without_financials(self) -> int:
        """Active universe companies with no canonical facts at all."""
        covered = (
            select(FinancialFact.company_id).distinct()
            .where(FinancialFact.company_id.is_not(None))
        )
        return int(self.db.scalar(
            select(func.count()).select_from(Company).where(
                Company.deleted_at.is_(None),
                Company.listing_status == "active",
                Company.id.not_in(covered),
            )
        ) or 0)


def _record_payload(record: UniverseRecord) -> dict[str, Any]:
    return {
        "ticker": record.ticker, "name": record.name,
        "exchange": record.exchange, "isin": record.isin,
        "bse_code": record.bse_code, "sector": record.sector,
        "industry": record.industry, "source": record.source,
        "market_cap_category": record.market_cap_category,
        "listing_status": record.listing_status,
    }
