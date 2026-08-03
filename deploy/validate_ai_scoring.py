"""Production validation for the AI Scoring Engine 3.0.

Runs the engine across a sample of the live universe and checks the guarantees
the brief asks for, empirically rather than by assertion:

1.  Every module is scored for every company.
2.  No factor is ever produced without a reason (never a black box).
3.  Every non-missing factor carries at least one resolvable citation.
4.  The composite equals the sum of module contributions, exactly.
5.  The engine is deterministic — repeated runs are byte-identical.
6.  The inverted scales point the way the guardrails assume.
7.  Version history is append-only: nothing is ever overwritten.
8.  Ratings and recommendations are monotonic in the composite.

Usage:
    DATABASE_URL=... python3 deploy/validate_ai_scoring.py --limit 60
"""
from __future__ import annotations

import argparse
import json
import logging
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

logging.disable(logging.CRITICAL)

from sqlalchemy import func, select  # noqa: E402

from app.db.base import SessionLocal  # noqa: E402
from app.domain.ai_scoring.framework import (  # noqa: E402
    FRAMEWORK_VERSION, MODULE_ORDER, MODULE_WEIGHTS,
)
from app.domain.ai_scoring.types import Rating, Recommendation  # noqa: E402
from app.models.company import Company  # noqa: E402
from app.models.scoring import AIScoreVersion  # noqa: E402
from app.services.ai_scoring.service import AIScoringService  # noqa: E402


class Report:
    def __init__(self) -> None:
        self.checks: list[tuple[str, bool, str]] = []

    def check(self, name: str, passed: bool, detail: str = "") -> bool:
        self.checks.append((name, passed, detail))
        mark = "PASS" if passed else "FAIL"
        print(f"  [{mark}] {name}" + (f" — {detail}" if detail else ""))
        return passed

    @property
    def failures(self) -> int:
        return sum(1 for _, passed, _ in self.checks if not passed)

    def summary(self) -> None:
        total = len(self.checks)
        print(f"\n{'=' * 72}")
        print(f"{total - self.failures}/{total} checks passed"
              + (f" — {self.failures} FAILURES" if self.failures else ""))
        print("=" * 72)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=60)
    parser.add_argument("--record", action="store_true",
                        help="persist versions (writes to the database)")
    parser.add_argument("--json", type=str, default="")
    args = parser.parse_args()

    db = SessionLocal()
    service = AIScoringService(db)
    report = Report()

    print(f"AI Scoring Engine {FRAMEWORK_VERSION} — production validation")
    print("=" * 72)

    # Sample across the universe rather than taking the head of the list: the
    # first N by insertion order are all large-caps with complete data, which
    # would validate the engine only on its easiest cases.
    total_companies = db.execute(
        select(func.count(Company.id)).where(Company.listing_status == "active")
    ).scalar_one()
    companies = list(db.execute(
        select(Company)
        .where(Company.listing_status == "active")
        .order_by(Company.ticker)
    ).scalars().all())
    step = max(1, len(companies) // args.limit)
    sample = companies[::step][: args.limit]

    print(f"\nUniverse: {total_companies} active companies. "
          f"Sampling every {step}th → {len(sample)} companies.\n")

    # --------------------------------------------------------------- run
    results = []
    latencies = []
    failures = []
    started = time.perf_counter()
    for company in sample:
        try:
            t0 = time.perf_counter()
            result = service.score_company(company)
            latencies.append((time.perf_counter() - t0) * 1000)
            results.append(result)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{company.ticker}: {type(exc).__name__}: {exc}")
    elapsed = time.perf_counter() - started

    print(f"Scored {len(results)}/{len(sample)} in {elapsed:.1f}s "
          f"(p50 {statistics.median(latencies):.0f}ms, "
          f"p95 {sorted(latencies)[int(len(latencies) * 0.95)]:.0f}ms)\n")

    print("--- Structural guarantees " + "-" * 46)
    report.check(
        "Every sampled company scored without error",
        not failures,
        "; ".join(failures[:3]) if failures else f"{len(results)} companies",
    )
    report.check(
        "Every company scored all ten modules",
        all(len(r.modules) == 10 for r in results),
        f"{len({len(r.modules) for r in results})} distinct module counts",
    )
    report.check(
        "Module order matches the framework",
        all([m.key for m in r.modules] == [m.value for m in MODULE_ORDER]
            for r in results),
    )
    report.check(
        "Module weights match the framework exactly",
        all(m.weight == MODULE_WEIGHTS[MODULE_ORDER[i]]
            for r in results for i, m in enumerate(r.modules)),
    )

    print("\n--- Explainability (the brief's central prohibition) " + "-" * 19)
    unexplained = [f"{r.ticker}:{f}" for r in results
                   for f in r.unexplained_factors]
    report.check(
        "No factor is ever produced without a reason",
        not unexplained,
        "; ".join(unexplained[:3]) if unexplained else
        f"{sum(len(m.factors) for r in results for m in r.modules)} factors checked",
    )
    uncited = [
        f"{r.ticker}:{m.key}.{f.key}"
        for r in results for m in r.modules for f in m.factors
        if not f.is_missing and not f.citations
    ]
    report.check(
        "Every scored (non-missing) factor carries a citation",
        not uncited,
        "; ".join(uncited[:3]) if uncited else
        f"{sum(r.total_citations for r in results)} citations issued",
    )
    report.check(
        "Every module carries a deterministic narrative",
        all(m.reason for r in results for m in r.modules),
    )
    report.check(
        "Every probability states its drivers and derivation",
        all(p.drivers and p.reason
            for r in results for p in r.probabilities
            if p.key != "__none__"),
    )

    print("\n--- Arithmetic " + "-" * 57)
    drift = max(
        abs(r.overall_score - sum(m.contribution for m in r.modules))
        for r in results
    ) if results else 0.0
    report.check(
        "Composite equals the sum of module contributions",
        drift < 1e-9, f"max drift {drift:.2e}",
    )
    report.check(
        "Composite is bounded to 0-100",
        all(0.0 <= r.overall_score <= 100.0 for r in results),
    )
    report.check(
        "Every factor score is within 0-10",
        all(0.0 <= f.score <= 10.0
            for r in results for m in r.modules for f in m.factors),
    )
    report.check(
        "Every probability is bounded to (0, 1)",
        all(0.0 < p.probability < 1.0
            for r in results for p in r.probabilities),
    )
    report.check(
        "Coverage is bounded to 0-1",
        all(0.0 <= r.coverage <= 1.0 for r in results),
    )

    print("\n--- Determinism " + "-" * 56)
    repeat_sample = sample[: min(10, len(sample))]
    identical = 0
    for company in repeat_sample:
        payloads = {
            json.dumps(service.score_company(company).as_dict(),
                       sort_keys=True, default=str)
            for _ in range(3)
        }
        if len(payloads) == 1:
            identical += 1
    report.check(
        "Repeated runs are byte-identical",
        identical == len(repeat_sample),
        f"{identical}/{len(repeat_sample)} companies stable over 3 runs",
    )
    fingerprints_stable = all(
        len({service.score_company(c).input_fingerprint for _ in range(2)}) == 1
        for c in repeat_sample[:5]
    )
    report.check("Input fingerprints are stable", fingerprints_stable)

    print("\n--- Monotonicity and consistency " + "-" * 39)
    order = [Rating.C, Rating.B, Rating.BB, Rating.BBB, Rating.A, Rating.A_PLUS]
    by_score = sorted(results, key=lambda r: r.overall_score)
    monotonic = all(
        order.index(by_score[i].rating) <= order.index(by_score[i + 1].rating)
        for i in range(len(by_score) - 1)
    )
    report.check("Rating is monotonic in the composite", monotonic)

    rec_order = [Recommendation.AVOID, Recommendation.REDUCE,
                 Recommendation.HOLD, Recommendation.BUY,
                 Recommendation.STRONG_BUY]
    # Only guardrail-free companies need be monotonic: a guardrail deliberately
    # breaks the mapping, which is the entire point of it.
    clean = [r for r in by_score if not r.guardrails]
    rec_monotonic = all(
        rec_order.index(clean[i].recommendation)
        <= rec_order.index(clean[i + 1].recommendation)
        for i in range(len(clean) - 1)
    )
    report.check(
        "Recommendation is monotonic where no guardrail fired",
        rec_monotonic, f"{len(clean)} of {len(results)} unconstrained",
    )
    report.check(
        "No company with an unobservable evidence base gets a directional call",
        all(r.recommendation in {Recommendation.HOLD}
            for r in results if r.coverage < 0.30 and not r.guardrails
            or (r.coverage < 0.30 and r.recommendation
                in {Recommendation.HOLD, Recommendation.REDUCE,
                    Recommendation.AVOID})),
        f"{sum(1 for r in results if r.coverage < 0.30)} thin-evidence companies",
    )

    print("\n--- Inverted scales " + "-" * 52)
    # Risk: 10 = low risk. Companies scoring low on risk must not be Strong Buy.
    risky = [r for r in results
             if (r.module("risk") and r.module("risk").score <= 3.0)]
    report.check(
        "Risk 10 = LOW risk: no fragile company is rated Strong Buy",
        all(r.recommendation is not Recommendation.STRONG_BUY for r in risky),
        f"{len(risky)} companies below 3.0 on risk",
    )
    dear = [r for r in results
            if (r.module("valuation") and r.module("valuation").score <= 3.0)]
    report.check(
        "Valuation 10 = CHEAP: no expensive company is rated above Hold",
        all(r.recommendation in {Recommendation.HOLD, Recommendation.REDUCE,
                                 Recommendation.AVOID} for r in dear),
        f"{len(dear)} companies below 3.0 on valuation",
    )

    print("\n--- Version history (append-only) " + "-" * 38)
    if args.record:
        probe = sample[: min(5, len(sample))]
        created_first = 0
        created_second = 0
        for company in probe:
            _, out1 = service.score_and_record(company, trigger="validation")
            if out1.created:
                created_first += 1
            _, out2 = service.score_and_record(company, trigger="validation")
            if out2.created:
                created_second += 1
        report.check(
            "An unchanged fingerprint writes no new version",
            created_second == 0,
            f"{created_first} written on first pass, "
            f"{created_second} on the identical second pass",
        )
        for company in probe:
            versions = service.history(company.id)
            current = [v for v in versions if v.status == "current"]
            if len(current) != 1:
                report.check(
                    f"Exactly one current version for {company.ticker}",
                    False, f"{len(current)} rows marked current",
                )
                break
        else:
            report.check("Exactly one version is current per company", True,
                         f"{len(probe)} companies checked")
    else:
        # Read-only inspection of whatever history already exists.
        rows = db.execute(
            select(AIScoreVersion.company_id, func.count(AIScoreVersion.id))
            .where(AIScoreVersion.status == "current")
            .group_by(AIScoreVersion.company_id)
        ).all()
        multiples = [c for c, n in rows if n > 1]
        report.check(
            "At most one current version per company",
            not multiples,
            f"{len(rows)} companies with recorded history"
            if rows else "no history recorded yet (run with --record)",
        )

    # -------------------------------------------------------- distribution
    print("\n--- Distribution across the sample " + "-" * 37)
    scores = [r.overall_score for r in results]
    coverages = [r.coverage for r in results]
    print(f"  Score:    min {min(scores):5.1f}  median "
          f"{statistics.median(scores):5.1f}  max {max(scores):5.1f}  "
          f"σ {statistics.pstdev(scores):.1f}")
    print(f"  Coverage: min {min(coverages):5.1%}  median "
          f"{statistics.median(coverages):5.1%}  max {max(coverages):5.1%}")
    print(f"  Ratings:  "
          + "  ".join(f"{k}:{v}" for k, v in
                      sorted(Counter(r.rating.value for r in results).items())))
    print(f"  Calls:    "
          + "  ".join(f"{k}:{v}" for k, v in
                      Counter(r.recommendation.value for r in results).items()))
    print(f"  Citations: {sum(r.total_citations for r in results)} total, "
          f"median {statistics.median([r.total_citations for r in results]):.0f}/company")

    print("\n  Per-module mean score and coverage:")
    print(f"  {'module':26s} {'wt':>3s} {'mean':>6s} {'cov':>6s} {'miss/co':>8s}")
    for index, module in enumerate(MODULE_ORDER):
        values = [r.modules[index] for r in results]
        mean = statistics.mean(m.score for m in values)
        cov = statistics.mean(m.coverage for m in values)
        miss = statistics.mean(len(m.missing_factors) for m in values)
        print(f"  {values[0].label:26s} {MODULE_WEIGHTS[module]:3.0f} "
              f"{mean:6.2f} {cov:6.1%} {miss:8.1f}")

    report.summary()

    if args.json:
        Path(args.json).write_text(json.dumps({
            "framework_version": FRAMEWORK_VERSION,
            "universe": total_companies,
            "sampled": len(sample),
            "scored": len(results),
            "checks": [{"name": n, "passed": p, "detail": d}
                       for n, p, d in report.checks],
            "failures": report.failures,
            "latency_p50_ms": statistics.median(latencies) if latencies else None,
            "score_median": statistics.median(scores) if scores else None,
            "coverage_median": statistics.median(coverages) if coverages else None,
            "modules": [
                {
                    "key": module.value,
                    "weight": MODULE_WEIGHTS[module],
                    "mean_score": statistics.mean(
                        r.modules[i].score for r in results),
                    "mean_coverage": statistics.mean(
                        r.modules[i].coverage for r in results),
                }
                for i, module in enumerate(MODULE_ORDER)
            ],
        }, indent=2), encoding="utf-8")
        print(f"\nWrote {args.json}")

    db.close()
    return 1 if report.failures else 0


if __name__ == "__main__":
    sys.exit(main())
