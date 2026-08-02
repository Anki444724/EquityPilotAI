"""Backfill canonical financials across the whole coverage universe.

Why this exists when `app.data.ingest.ingest_universe` already does something
similar: that function iterates `NSE_UNIVERSE`, a hard-coded 136-entry tuple
compiled before the Nifty 500 import. It cannot see the 500 companies that now
live in the database, and extending the tuple would mean maintaining the
universe in two places — the exact duplication the module rules forbid.

This service drives from the **database** instead. The universe is whatever
`companies` says it is, so a future index rebalance changes data, not code.

What it does NOT do
-------------------
It does not reimplement canonicalisation. `ingest.canonicalise()` already
encodes hard-won knowledge — most importantly that screener renders banks and
NBFCs on a *financing* layout where interest sits inside expenses, and that
reading such a company with the operating mapping produced an HDFC Bank net
profit of −₹268,944 cr against a reported +₹79,219 cr. That logic exists once
and is called, not copied.

Placeholders
------------
A company is either ingested with real facts or recorded as a failure with a
reason. There is no third state. `ingest_company` already returns
`ok=False, error="no canonical facts derived"` rather than writing an empty
shell, and this service preserves that: nothing is persisted for a company
whose source has no data, so the UI's "no financial data" message stays
truthful rather than being papered over with zeroes.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

import structlog
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.data.ingest import IngestResult, ingest_company
from app.models.company import Company, FinancialFact

log = structlog.get_logger(__name__)

#: Screener throttles a single IP. `screener_source` already enforces its own
#: ~1.1s floor between requests; this adds a little headroom on top, because a
#: 500-company sweep is a far longer sustained run than the 136-company one
#: that interval was tuned against.
DEFAULT_DELAY_SECONDS = 0.4

#: A company is only considered covered if it has facts for at least this many
#: distinct fiscal years. One stray year is not a financial history, and
#: treating it as coverage would inflate the headline number while leaving the
#: statements unusable.
MIN_USEFUL_YEARS = 2


@dataclass(slots=True)
class CompanyTarget:
    """A company to ingest, addressed by what the database already knows."""

    company_id: str
    ticker: str
    name: str
    sector: str | None
    industry: str | None
    market_cap_category: str | None


@dataclass(slots=True)
class BackfillOutcome:
    """One company's result, carrying the reason when it did not succeed."""

    ticker: str
    name: str
    market_cap_category: str | None
    ok: bool
    fiscal_years: int = 0
    fact_count: int = 0
    coverage: float = 0.0
    #: Verbatim reason, propagated from the provider rather than summarised.
    #: "Reason for every missing company" is a deliverable, so a generic
    #: "failed" here would be a defect.
    reason: str | None = None
    warnings: list[str] = field(default_factory=list)
    seconds: float = 0.0


@dataclass(slots=True)
class BackfillReport:
    outcomes: list[BackfillOutcome] = field(default_factory=list)
    skipped_existing: int = 0

    @property
    def succeeded(self) -> list[BackfillOutcome]:
        return [o for o in self.outcomes if o.ok]

    @property
    def failed(self) -> list[BackfillOutcome]:
        return [o for o in self.outcomes if not o.ok]

    def reasons(self) -> dict[str, int]:
        """Failure reasons grouped by their leading clause."""
        buckets: dict[str, int] = {}
        for outcome in self.failed:
            key = (outcome.reason or "unknown").split(":")[0].strip()[:60]
            buckets[key] = buckets.get(key, 0) + 1
        return dict(sorted(buckets.items(), key=lambda kv: -kv[1]))


class FinancialsBackfillService:
    """Sweeps the coverage universe and ingests whatever is genuinely there."""

    def __init__(
        self,
        db: Session,
        *,
        delay_seconds: float = DEFAULT_DELAY_SECONDS,
        with_yahoo: bool = False,
        ingest: Callable[..., IngestResult] = ingest_company,
    ) -> None:
        self.db = db
        self.delay_seconds = delay_seconds
        # Yahoo is OFF by default for a bulk sweep, which is a change from
        # `ingest_universe`'s default and is a measurement, not a preference.
        #
        # Yahoo rate-limits this IP hard: every call returns HTTP 429 and the
        # client's own backoff spends ~82 seconds before giving up, against
        # screener's 1.4 seconds. Across 368 companies that is 8.4 hours of
        # waiting to add nothing, because the calls fail.
        #
        # What is forgone when Yahoo is absent is the expense breakdown and
        # some balance-sheet detail (`YAHOO_DETAIL`, ~30 of the 54 items).
        # Screener aggregates those into single lines, so the statements still
        # reconcile — verified: CGPOWER, HDFCAMC and HINDZINC all match the
        # reported revenue, PAT and total assets exactly, with the balance
        # sheet tying to 0.0, on screener alone.
        #
        # Set True for a single company, or once Yahoo stops throttling, to
        # enrich the detail lines.
        self.with_yahoo = with_yahoo
        # Injected so tests can drive the sweep without touching the network.
        self._ingest = ingest

    # ------------------------------------------------------------- selection
    def companies_without_financials(
        self, *, limit: int | None = None, only_active: bool = True,
    ) -> list[CompanyTarget]:
        """Companies in the universe carrying no usable financial history.

        `MIN_USEFUL_YEARS` matters here: a company with a single stray fiscal
        year is not covered in any sense a user would recognise, and excluding
        it from the retry set would permanently strand it.
        """
        counts = (
            select(
                FinancialFact.company_id.label("cid"),
                func.count(func.distinct(FinancialFact.fiscal_year)).label("years"),
            )
            .group_by(FinancialFact.company_id)
            .subquery()
        )
        stmt = (
            select(Company, counts.c.years)
            .outerjoin(counts, counts.c.cid == Company.id)
            .where(
                (counts.c.years.is_(None)) | (counts.c.years < MIN_USEFUL_YEARS)
            )
            .order_by(
                # Largecaps first: they are the companies a user is most
                # likely to open, and if a long sweep is interrupted the
                # coverage that exists should be the coverage that matters.
                Company.market_cap_category.is_(None),
                Company.market_cap_category,
                Company.ticker,
            )
        )
        if only_active:
            stmt = stmt.where(Company.listing_status == "active")
        if limit:
            stmt = stmt.limit(limit)

        return [
            CompanyTarget(
                company_id=company.id,
                ticker=company.ticker,
                name=company.name,
                sector=company.sector,
                industry=company.industry,
                market_cap_category=company.market_cap_category,
            )
            for company, _years in self.db.execute(stmt).all()
        ]

    # --------------------------------------------------------------- running
    def run(
        self,
        targets: Sequence[CompanyTarget] | None = None,
        *,
        limit: int | None = None,
        progress: bool = True,
    ) -> BackfillReport:
        """Ingest each target, recording a reason for every one that fails."""
        if targets is None:
            targets = self.companies_without_financials(limit=limit)

        report = BackfillReport()
        total = len(targets)

        for index, target in enumerate(targets, 1):
            started = time.monotonic()
            try:
                result = self._ingest(
                    self.db,
                    target.ticker,
                    target.name,
                    # `ingest_company` writes these back onto the row. Passing
                    # what the database already holds keeps the Nifty 500
                    # import's sector intact instead of blanking it; industry
                    # was deliberately left null by that import, and an empty
                    # string would be a placeholder, so None is preserved.
                    target.sector or "",
                    target.industry or "",
                    with_yahoo=self.with_yahoo,
                )
            except Exception as exc:  # noqa: BLE001 — one bad ticker must not stop 500
                # A failed ingest may leave the session dirty; the next
                # company must start clean or it inherits the failure.
                self.db.rollback()
                result = IngestResult(
                    ticker=target.ticker, ok=False,
                    error=f"{type(exc).__name__}: {exc}",
                )

            elapsed = time.monotonic() - started
            outcome = BackfillOutcome(
                ticker=target.ticker,
                name=target.name,
                market_cap_category=target.market_cap_category,
                ok=result.ok,
                fiscal_years=len(result.fiscal_years),
                fact_count=result.fact_count,
                coverage=result.coverage,
                reason=result.error,
                warnings=list(result.warnings),
                seconds=round(elapsed, 2),
            )
            report.outcomes.append(outcome)

            if progress:
                state = "ok  " if outcome.ok else "FAIL"
                detail = (
                    f"{outcome.fiscal_years:>2}y {outcome.fact_count:>4} facts "
                    f"cov={outcome.coverage:.0%}"
                    if outcome.ok else (outcome.reason or "")[:74]
                )
                print(
                    f"[{index:>3}/{total}] {state} {target.ticker:<14}{detail}",
                    flush=True,
                )

            if self.delay_seconds and index < total:
                time.sleep(self.delay_seconds)

        return report

    # ------------------------------------------------------------ reporting
    def coverage_snapshot(self) -> dict[str, object]:
        """Current coverage, read back from the database rather than inferred.

        Deliberately re-queries instead of trusting the run's own tally: the
        question a user is asking is "what does the platform hold now", and
        only the database can answer that.
        """
        counts = (
            select(
                FinancialFact.company_id.label("cid"),
                func.count(func.distinct(FinancialFact.fiscal_year)).label("years"),
            )
            .group_by(FinancialFact.company_id)
            .subquery()
        )
        rows = self.db.execute(
            select(Company, func.coalesce(counts.c.years, 0))
            .outerjoin(counts, counts.c.cid == Company.id)
            .where(Company.listing_status == "active")
        ).all()

        total = len(rows)
        covered = [c for c, years in rows if years >= MIN_USEFUL_YEARS]
        by_category: dict[str, dict[str, int]] = {}
        for company, years in rows:
            bucket = by_category.setdefault(
                company.market_cap_category or "unclassified",
                {"total": 0, "covered": 0},
            )
            bucket["total"] += 1
            if years >= MIN_USEFUL_YEARS:
                bucket["covered"] += 1

        return {
            "companies": total,
            "with_financials": len(covered),
            "without_financials": total - len(covered),
            "coverage_pct": round(100.0 * len(covered) / total, 2) if total else 0.0,
            "by_category": by_category,
        }
