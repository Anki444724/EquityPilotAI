"""Persist quarterly results and shareholding patterns from screener.

Separate from `financials_backfill` because the two answer different
questions. Annual facts feed the canonical 54-line-item grid that every
derived statement, ratio, valuation and score is computed from. Quarterly
results and shareholding are *separately disclosed facts* that nothing else
derives from — they are stored, displayed and versioned, and that is all.

The no-placeholder rule applies identically: a period with no reported figure
is never written. `QuarterlyResult.has_data` exists to make that assertable
rather than assumed.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Sequence

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.data.screener_source import ScreenerError, ScreenerFinancials, fetch_screener
from app.models.analysis import QuarterlyResult, ShareholdingSnapshot
from app.models.company import Company

log = structlog.get_logger(__name__)

#: Screener row label → QuarterlyResult column.
#: Labels carry a trailing `+` on expandable rows, stripped before matching.
QUARTER_MAP: dict[str, str] = {
    "sales": "revenue",
    "revenue": "revenue",              # financing-layout companies
    "expenses": "expenses",
    "operating profit": "operating_profit",
    "financing profit": "operating_profit",
    "opm %": "operating_margin",
    "financing margin %": "operating_margin",
    "other income": "other_income",
    "interest": "interest",
    "depreciation": "depreciation",
    "profit before tax": "profit_before_tax",
    "tax %": "tax_rate",
    "net profit": "net_profit",
    "eps in rs": "eps",
}

#: Columns screener reports as a percentage but the platform stores as a
#: fraction, so no layer has to remember which convention applies.
PERCENT_COLUMNS = {"operating_margin", "tax_rate"}

#: Screener's shareholding rows → ShareholdingSnapshot columns.
#:
#: Screener publishes the coarse split only. The model also has
#: `mutual_funds`, `insurance`, `banks_fis_aif` and `promoter_pledged`, which
#: come from the SEBI-format filing and are NOT derivable from this source:
#: screener reports one combined DII figure. Those columns are deliberately
#: left at their default rather than being apportioned by guesswork — an
#: invented split would look like data and be indistinguishable from a
#: disclosed one.
SHAREHOLDING_MAP: dict[str, str] = {
    "promoters": "promoter_indian",
    "fiis": "fii_fpi",
    "diis": "banks_fis_aif",   # combined domestic institutions
    "government": "government",
    "public": "others_custodians",
}


def _label(raw: str) -> str:
    return raw.lower().rstrip("+ ").strip()


@dataclass(slots=True)
class PeriodicOutcome:
    ticker: str
    ok: bool
    quarters_written: int = 0
    shareholding_written: int = 0
    reason: str | None = None


@dataclass(slots=True)
class PeriodicReport:
    outcomes: list[PeriodicOutcome] = field(default_factory=list)

    @property
    def succeeded(self) -> list[PeriodicOutcome]:
        return [o for o in self.outcomes if o.ok]

    @property
    def failed(self) -> list[PeriodicOutcome]:
        return [o for o in self.outcomes if not o.ok]

    @property
    def quarters(self) -> int:
        return sum(o.quarters_written for o in self.outcomes)

    @property
    def shareholding(self) -> int:
        return sum(o.shareholding_written for o in self.outcomes)


class PeriodicBackfillService:
    """Backfills quarterly results and shareholding across the universe."""

    def __init__(
        self,
        db: Session,
        *,
        delay_seconds: float = 0.3,
        fetch: Callable[..., ScreenerFinancials] = fetch_screener,
    ) -> None:
        self.db = db
        self.delay_seconds = delay_seconds
        self._fetch = fetch

    # ------------------------------------------------------------ selection
    def targets(self, *, limit: int | None = None) -> list[Company]:
        """Active companies, largecap first — same ordering rationale as the
        annual sweep: an interrupted run should leave the most-viewed
        companies covered."""
        stmt = (
            select(Company)
            .where(Company.listing_status == "active")
            .order_by(
                Company.market_cap_category.is_(None),
                Company.market_cap_category,
                Company.ticker,
            )
        )
        if limit:
            stmt = stmt.limit(limit)
        return list(self.db.scalars(stmt))

    # -------------------------------------------------------------- writing
    def _write_quarters(self, company: Company, data: ScreenerFinancials) -> int:
        if not data.quarters:
            return 0

        # Pivot from {label: {(fy, q): value}} to {(fy, q): {column: value}},
        # because a row is a period and screener hands us columns.
        periods: dict[tuple[int, int], dict[str, float]] = {}
        for raw_label, series in data.quarters.items():
            column = QUARTER_MAP.get(_label(raw_label))
            if column is None:
                continue
            for period, value in series.items():
                periods.setdefault(period, {})[column] = (
                    value / 100.0 if column in PERCENT_COLUMNS else value
                )

        existing = {
            (row.fiscal_year, row.quarter): row
            for row in self.db.scalars(
                select(QuarterlyResult).where(
                    QuarterlyResult.company_id == company.id
                )
            )
        }

        written = 0
        for (fiscal_year, quarter), values in periods.items():
            # The no-placeholder rule. A period screener lists but reports
            # nothing for is skipped rather than stored as a row of nulls.
            if not any(
                values.get(k) is not None
                for k in ("revenue", "operating_profit",
                          "profit_before_tax", "net_profit")
            ):
                continue

            row = existing.get((fiscal_year, quarter))
            if row is None:
                row = QuarterlyResult(
                    company_id=company.id,
                    fiscal_year=fiscal_year,
                    quarter=quarter,
                )
                self.db.add(row)
            for column, value in values.items():
                setattr(row, column, value)
            row.source = "screener.in"
            written += 1

        return written

    def _write_shareholding(
        self, company: Company, data: ScreenerFinancials,
    ) -> int:
        if not data.shareholding:
            return 0

        periods: dict[tuple[int, int], dict[str, float]] = {}
        for raw_label, series in data.shareholding.items():
            column = SHAREHOLDING_MAP.get(_label(raw_label))
            if column is None:
                continue                      # e.g. "No. of Shareholders"
            for period, value in series.items():
                # Stored as fractions (0.521 == 52.1%), per the model's
                # docstring, so no layer converts units.
                periods.setdefault(period, {})[column] = value / 100.0

        existing = {
            (row.fiscal_year, row.quarter): row
            for row in self.db.scalars(
                select(ShareholdingSnapshot).where(
                    ShareholdingSnapshot.company_id == company.id
                )
            )
        }

        written = 0
        for (fiscal_year, quarter), values in periods.items():
            if not any(values.values()):
                # An all-zero column is screener rendering a period it has no
                # pattern for, not a company with no shareholders.
                continue
            row = existing.get((fiscal_year, quarter))
            if row is None:
                row = ShareholdingSnapshot(
                    company_id=company.id,
                    fiscal_year=fiscal_year,
                    quarter=quarter,
                )
                self.db.add(row)
            for column, value in values.items():
                setattr(row, column, value)
            written += 1

        return written

    # -------------------------------------------------------------- running
    def run(
        self,
        companies: Sequence[Company] | None = None,
        *,
        limit: int | None = None,
        progress: bool = True,
    ) -> PeriodicReport:
        if companies is None:
            companies = self.targets(limit=limit)

        report = PeriodicReport()
        total = len(companies)
        failure_run = self._open_failure_run()

        for index, company in enumerate(companies, 1):
            try:
                data = self._fetch(company.ticker)
                quarters = self._write_quarters(company, data)
                shareholding = self._write_shareholding(company, data)
                self.db.commit()
                outcome = PeriodicOutcome(
                    ticker=company.ticker, ok=True,
                    quarters_written=quarters,
                    shareholding_written=shareholding,
                )
            except ScreenerError as exc:
                self.db.rollback()
                outcome = PeriodicOutcome(
                    ticker=company.ticker, ok=False, reason=f"screener: {exc}",
                )
            except Exception as exc:  # noqa: BLE001 — one bad ticker must not stop 500
                self.db.rollback()
                outcome = PeriodicOutcome(
                    ticker=company.ticker, ok=False,
                    reason=f"{type(exc).__name__}: {exc}",
                )

            report.outcomes.append(outcome)
            if outcome.ok:
                if failure_run is not None:
                    failure_run.succeeded += 1
            else:
                self._file_failure(
                    failure_run, company, outcome.reason or "unknown error",
                )
            if progress:
                state = "ok  " if outcome.ok else "FAIL"
                detail = (
                    f"{outcome.quarters_written:>2}q "
                    f"{outcome.shareholding_written:>2}sh"
                    if outcome.ok else (outcome.reason or "")[:70]
                )
                print(f"[{index:>3}/{total}] {state} {company.ticker:<14}{detail}",
                      flush=True)

            if self.delay_seconds and index < total:
                time.sleep(self.delay_seconds)

        self._close_failure_run(failure_run, report)
        return report

    # ------------------------------------------------- failure observability
    def _open_failure_run(self):
        """Same contract as the financials backfill's helper: best-effort."""
        try:
            from datetime import datetime, timezone

            from app.models.ingestion import IngestionRun

            run = IngestionRun(
                kind="periodic_sync", provider="screener.in",
                started_at=datetime.now(timezone.utc),
                stats={"operation": "quarterly_and_shareholding"},
            )
            self.db.add(run)
            self.db.commit()
            return run
        except Exception:  # noqa: BLE001 — observability must not break the sync
            self.db.rollback()
            return None

    def _file_failure(self, run, company: Company, error: str) -> None:
        if run is None:
            return
        try:
            from datetime import datetime, timezone

            from app.models.ingestion import IngestionFailure
            from app.services.universe.financials_backfill import (
                classify_ingest_failure,
            )

            self.db.add(IngestionFailure(
                run_id=run.id, kind="periodic_sync", symbol=company.ticker,
                company_id=company.id, error=(error or "unknown error")[:2000],
                failure_kind=classify_ingest_failure(error).value,
                last_attempt_at=datetime.now(timezone.utc),
                payload={
                    "operation": "quarterly_and_shareholding",
                    "source": "screener.in",
                },
            ))
            run.failed += 1
            self.db.commit()
        except Exception:  # noqa: BLE001
            self.db.rollback()

    def _close_failure_run(self, run, report: PeriodicReport) -> None:
        if run is None:
            return
        try:
            from datetime import datetime, timezone

            run.finished_at = datetime.now(timezone.utc)
            run.stats = {
                **(run.stats or {}),
                "attempted": len(report.outcomes),
                "succeeded": len(report.succeeded),
                "failed": len(report.failed),
            }
            self.db.commit()
        except Exception:  # noqa: BLE001
            self.db.rollback()
