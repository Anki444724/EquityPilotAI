#!/usr/bin/env python3
"""Validation report for the financial ingestion pipeline.

Answers the deliverables directly, and — importantly — answers them by
*computing* each layer rather than by checking that a row exists. Statements,
ratios, scores, valuation and forecast are all derived on demand in this
platform, so "the facts are present" is not evidence that a score can be
produced. This runs the real services and records what actually comes back.

    export DATABASE_URL="postgresql+psycopg://..."
    python3 deploy/validate_coverage.py --sample 40
    python3 deploy/validate_coverage.py --all --json report.json
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import traceback

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "backend"))

import importlib  # noqa: E402
import pkgutil  # noqa: E402

from sqlalchemy import create_engine, func, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import app.models as _models  # noqa: E402

for _module in pkgutil.iter_modules(_models.__path__):
    importlib.import_module(f"app.models.{_module.name}")

from app.models.analysis import QuarterlyResult, ShareholdingSnapshot  # noqa: E402
from app.models.company import Company, FinancialFact  # noqa: E402
from app.services.analysis_service import AnalysisService  # noqa: E402
from app.services.forecast.service import ForecastService  # noqa: E402
from app.services.quarterly.service import QuarterlyService  # noqa: E402
from app.services.scoring.service import ScoringService  # noqa: E402
from app.services.valuation.service import ValuationService  # noqa: E402

MIN_USEFUL_YEARS = 2


def check_company(db, company: Company) -> dict:
    """Exercise every layer for one company and record what succeeded."""
    row: dict[str, object] = {
        "ticker": company.ticker,
        "name": company.name,
        "category": company.market_cap_category,
    }

    try:
        analysis = AnalysisService.for_ticker(db, company.ticker, provision=False)
    except Exception as exc:  # noqa: BLE001
        row["error"] = f"analysis: {type(exc).__name__}: {exc}"
        return row

    if analysis is None:
        row["error"] = "analysis: company did not resolve"
        return row

    # --- statements -------------------------------------------------------
    incomes = analysis.incomes
    balances = analysis.balances
    cash_flows = analysis.cash_flows
    row["income_years"] = len(incomes)
    row["balance_years"] = len(balances)
    row["cashflow_years"] = len(cash_flows)

    if incomes:
        latest = incomes[-1]
        row["fiscal_year"] = latest.fiscal_year
        row["revenue"] = latest.total_revenue
        row["pat"] = latest.pat
    if balances:
        row["total_assets"] = balances[-1].total_assets
        # A balance sheet that does not tie is a data defect, not a
        # presentation one, so it is surfaced per company.
        row["balance_check"] = balances[-1].balance_check
    if cash_flows:
        row["cfo"] = cash_flows[-1].cfo

    # --- ratios -----------------------------------------------------------
    try:
        sections = analysis.ratios().all_sections()
        total = sum(len(s.rows) for s in sections)
        filled = sum(
            1 for s in sections for r in s.rows
            if any(v is not None for v in r.values)
        )
        row["ratio_rows"] = total
        row["ratio_filled"] = filled
    except Exception as exc:  # noqa: BLE001
        row["ratio_error"] = f"{type(exc).__name__}: {exc}"

    # --- market cap -------------------------------------------------------
    row["market_cap"] = company.market_cap
    row["price"] = company.current_price

    # --- quarterly and shareholding --------------------------------------
    row["quarters"] = db.scalar(
        select(func.count()).select_from(QuarterlyResult)
        .where(QuarterlyResult.company_id == company.id)
    ) or 0
    row["shareholding"] = db.scalar(
        select(func.count()).select_from(ShareholdingSnapshot)
        .where(ShareholdingSnapshot.company_id == company.id)
    ) or 0

    # --- scoring, which internally runs forecast and valuation ------------
    try:
        result = ScoringService(db).score_company(
            analysis, ForecastService(db), ValuationService(db),
        )
        row["score"] = round(result.overall_score, 1)
        row["rating"] = getattr(result, "rating", None)
        row["categories_scored"] = sum(1 for c in result.categories if c.score_pct)
        row["categories_total"] = len(result.categories)
    except Exception as exc:  # noqa: BLE001
        row["score_error"] = f"{type(exc).__name__}: {exc}"
        row["score_trace"] = traceback.format_exc(limit=2)[-300:]

    return row


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", type=int, default=40)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--json", default=None)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()

    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2

    engine = create_engine(url, pool_pre_ping=True)
    db = sessionmaker(bind=engine)()

    # ---------------------------------------------------------- population
    year_counts = (
        select(
            FinancialFact.company_id.label("cid"),
            func.count(func.distinct(FinancialFact.fiscal_year)).label("years"),
        )
        .group_by(FinancialFact.company_id)
        .subquery()
    )
    rows = db.execute(
        select(Company, func.coalesce(year_counts.c.years, 0))
        .outerjoin(year_counts, year_counts.c.cid == Company.id)
        .order_by(Company.ticker)
    ).all()

    active = [(c, y) for c, y in rows if c.listing_status == "active"]
    delisted = [(c, y) for c, y in rows if c.listing_status != "active"]
    covered = [(c, y) for c, y in active if y >= MIN_USEFUL_YEARS]
    missing = [(c, y) for c, y in active if y < MIN_USEFUL_YEARS]

    print("=" * 66)
    print("COVERAGE")
    print("=" * 66)
    print(f"  companies (total)      {len(rows)}")
    print(f"  active                 {len(active)}")
    print(f"  delisted (excluded)    {len(delisted)}")
    print(f"  with financials        {len(covered)}")
    print(f"  still missing          {len(missing)}")
    pct = 100.0 * len(covered) / len(active) if active else 0.0
    print(f"  coverage               {pct:.2f}%")

    by_cat: dict[str, list[int]] = {}
    for company, years in active:
        bucket = by_cat.setdefault(company.market_cap_category or "unclassified",
                                   [0, 0])
        bucket[0] += 1
        if years >= MIN_USEFUL_YEARS:
            bucket[1] += 1
    print("\n  by category:")
    for category, (total, done) in sorted(by_cat.items()):
        print(f"    {category:<14} {done:>3}/{total:<3} "
              f"{100.0 * done / total:.1f}%")

    if missing:
        print("\n  STILL MISSING:")
        for company, years in missing:
            print(f"    {company.ticker:<14} {company.name[:38]:<38} "
                  f"{years} fiscal year(s)")

    # ------------------------------------------------------------ sampling
    population = [c for c, _ in covered]
    if args.all:
        sample = population
    else:
        random.seed(args.seed)
        sample = random.sample(population, min(args.sample, len(population)))
    sample.sort(key=lambda c: c.ticker)

    print("\n" + "=" * 66)
    print(f"LAYER VALIDATION  (n={len(sample)})")
    print("=" * 66)

    results = [check_company(db, company) for company in sample]

    def count(predicate) -> int:
        return sum(1 for r in results if predicate(r))

    checks = [
        ("Income statement", lambda r: r.get("income_years", 0) >= 2),
        ("Balance sheet", lambda r: r.get("balance_years", 0) >= 2),
        ("Cash flow", lambda r: r.get("cashflow_years", 0) >= 2),
        ("Balance sheet ties", lambda r: abs(r.get("balance_check") or 0) < 1.0),
        ("Ratios computed", lambda r: r.get("ratio_filled", 0) > 0),
        ("Market cap", lambda r: bool(r.get("market_cap"))),
        ("Quarterly results", lambda r: r.get("quarters", 0) > 0),
        ("Shareholding", lambda r: r.get("shareholding", 0) > 0),
        ("AI score", lambda r: r.get("score") is not None),
        ("Valuation + forecast", lambda r: r.get("categories_scored", 0) > 0),
    ]

    print(f"\n  {'LAYER':<24}{'PASS':>6}{'FAIL':>6}{'%':>8}")
    print("  " + "-" * 44)
    summary = {}
    for label, predicate in checks:
        ok = count(predicate)
        summary[label] = {"pass": ok, "fail": len(results) - ok}
        print(f"  {label:<24}{ok:>6}{len(results) - ok:>6}"
              f"{100.0 * ok / len(results):>7.1f}%")

    failures = [r for r in results if r.get("score") is None
                or r.get("income_years", 0) < 2]
    if failures:
        print(f"\n  companies failing a layer ({len(failures)}):")
        for r in failures[:20]:
            reason = (r.get("error") or r.get("score_error")
                      or f"{r.get('income_years', 0)} income years")
            print(f"    {r['ticker']:<14} {str(reason)[:70]}")

    if args.json:
        with open(args.json, "w") as handle:
            json.dump({
                "coverage": {
                    "total": len(rows), "active": len(active),
                    "delisted": len(delisted), "with_financials": len(covered),
                    "missing": len(missing), "coverage_pct": round(pct, 2),
                    "by_category": {k: {"total": v[0], "covered": v[1]}
                                    for k, v in by_cat.items()},
                    "missing_detail": [
                        {"ticker": c.ticker, "name": c.name, "years": y}
                        for c, y in missing
                    ],
                },
                "layers": summary,
                "sample": results,
            }, handle, indent=1, default=str)
        print(f"\nwrote {args.json}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
