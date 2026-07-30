"""Second-pass enrichment from Yahoo.

Split out of `ingest.py` deliberately. The first run merged both sources in
one pass, and when Yahoo's rate limiter tripped on the very first company the
circuit stayed open for the whole run: 128 companies were ingested with
screener data only, at 46% coverage, and the failure was uniform enough to
look like a design limit rather than one bad minute.

Separating the passes means a Yahoo outage costs the granular lines and
nothing else, the pass can be re-run on its own, and — the point — a
provider's bad half-hour never again silently degrades an entire dataset.

Enrichment only ever *adds* line items screener does not carry. It never
overwrites a screener figure, because mixing two presentations line by line is
how a balance sheet stops balancing.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.data.ingest import YAHOO_DETAIL
from app.data.yahoo_source import (
    FetchError, fetch_financials, provider_available, reset_circuit,
)
from app.domain.financials.canonical import Precedence
from app.domain.financials.line_items import LineItem as LI
from app.models.company import Company, FinancialFact


@dataclass(slots=True)
class EnrichResult:
    ticker: str
    ok: bool
    added: int = 0
    items_added: list[str] = field(default_factory=list)
    coverage_before: float = 0.0
    coverage_after: float = 0.0
    error: str | None = None


def _coverage(db: Session, company_id: str, year: int) -> float:
    rows = db.scalars(
        select(FinancialFact.line_item).where(
            FinancialFact.company_id == company_id,
            FinancialFact.fiscal_year == year,
        )
    ).all()
    return round(len(set(rows)) / len(LI), 4)


def enrich_company(db: Session, ticker: str) -> EnrichResult:
    """Add Yahoo's granular lines to an already-ingested company."""
    company = db.scalar(select(Company).where(Company.ticker == ticker))
    if company is None:
        return EnrichResult(ticker=ticker, ok=False, error="not ingested")

    years = sorted({
        y for y in db.scalars(
            select(FinancialFact.fiscal_year).where(
                FinancialFact.company_id == company.id,
            )
        ).all()
    })
    if not years:
        return EnrichResult(ticker=ticker, ok=False, error="no facts")

    latest = max(years)
    before = _coverage(db, company.id, latest)

    try:
        yahoo = fetch_financials(ticker)
    except FetchError as exc:
        return EnrichResult(
            ticker=ticker, ok=False, coverage_before=before,
            coverage_after=before, error=str(exc)[:120],
        )

    # What screener already supplied, per (item, year). Enrichment must not
    # touch these.
    existing = {
        (row.line_item, row.fiscal_year)
        for row in db.scalars(
            select(FinancialFact).where(FinancialFact.company_id == company.id)
        ).all()
    }

    added = 0
    items: set[str] = set()
    for item in YAHOO_DETAIL + (LI.CAPEX,):
        for year, value in yahoo.facts.get(item, {}).items():
            if year not in years:
                continue
            if (item.value, year) in existing:
                continue
            db.add(FinancialFact(
                company_id=company.id, line_item=item.value, fiscal_year=year,
                value=float(value), precedence=int(Precedence.STORE), source="yahoo_finance",
            ))
            existing.add((item.value, year))
            items.add(item.value)
            added += 1

    if added:
        company.data_version = (company.data_version or 1) + 1
        db.commit()

    return EnrichResult(
        ticker=ticker, ok=True, added=added, items_added=sorted(items),
        coverage_before=before, coverage_after=_coverage(db, company.id, latest),
    )


#: How long to idle when the provider starts refusing.
#:
#: Measured, not guessed: Yahoo began serving again after roughly a minute of
#: silence. Sleeping and resuming turns a hard stop into a slow pass, which is
#: the right trade for a batch job nobody is watching.
COOLDOWN_SECONDS = 75
MAX_COOLDOWNS = 8


def enrich_universe(
    db: Session,
    *,
    progress: bool = True,
    cooldown_seconds: int = COOLDOWN_SECONDS,
    max_cooldowns: int = MAX_COOLDOWNS,
) -> list[EnrichResult]:
    """Enrich every ingested company, pausing when the provider pushes back.

    The first version stopped dead when the circuit opened, and the circuit
    opened on company one — so the whole pass achieved nothing and the dataset
    stayed at 46% coverage. The provider recovers after about a minute of
    quiet, so the right response to a rate limit is to wait rather than to
    give up. A pass that takes twenty minutes and succeeds beats one that
    takes twenty seconds and does not.
    """
    import time

    reset_circuit()
    tickers = list(db.scalars(select(Company.ticker).order_by(Company.ticker)))
    results: list[EnrichResult] = []
    cooldowns = 0
    pending = list(enumerate(tickers, 1))

    while pending:
        index, ticker = pending.pop(0)

        if not provider_available():
            if cooldowns >= max_cooldowns:
                if progress:
                    print(
                        f"  provider unavailable after {cooldowns} cooldowns — "
                        f"{len(pending) + 1} companies keep screener data only",
                        flush=True,
                    )
                break
            cooldowns += 1
            if progress:
                print(
                    f"  rate limited — cooling down {cooldown_seconds}s "
                    f"({cooldowns}/{max_cooldowns})",
                    flush=True,
                )
            time.sleep(cooldown_seconds)
            reset_circuit()
            pending.insert(0, (index, ticker))   # retry the one we skipped
            continue

        result = enrich_company(db, ticker)
        results.append(result)
        if progress:
            state = "ok " if result.ok else "skip"
            detail = (
                f"+{result.added:3d} facts  "
                f"{result.coverage_before:.0%} -> {result.coverage_after:.0%}"
                if result.ok else (result.error or "")[:60]
            )
            print(f"[{index:3d}/{len(tickers)}] {state} {ticker:<14}{detail}", flush=True)

    return results
