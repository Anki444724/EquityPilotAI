"""AI Scoring Engine 3.0 service: the one entry point every consumer calls.

Resolves evidence, runs the engine, and manages the permanent version history.

**Nothing is ever overwritten.** :meth:`AIScoringService.record` inserts a new
version and marks the previous one superseded. The only case where it declines
to write is when the input fingerprint is unchanged — the same evidence
producing the same score is not a new version, it is the same version observed
twice, and recording it would fill the history with noise that makes a real
change harder to find.

**Sector statistics are computed once per sector, not once per company.** Three
of the ten modules need them. A 500-company batch that recomputed them per
company would issue 1,500 sector-wide aggregate queries to produce at most a
few dozen distinct answers.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

import structlog
from sqlalchemy import func, select

from app.domain.ai_scoring.framework import FRAMEWORK_VERSION
from app.domain.ai_scoring.types import AIScoreResult
from app.models.company import Company
from app.models.scoring import AIScoreVersion
from app.services.ai_scoring.engine import compute
from app.services.ai_scoring.evidence import EvidenceBuilder, ScoringEvidence
from app.services.ai_scoring.modules.common import series_cagr

log = structlog.get_logger(__name__)


class AIScoringError(ValueError):
    """Raised when a company cannot be scored at all."""


@dataclass(slots=True)
class RecordOutcome:
    """What happened when a result was offered to the history."""

    version: AIScoreVersion | None
    created: bool
    reason: str
    delta: float | None = None


class SectorStatsCache:
    """Sector aggregates, computed once and reused across a scoring batch.

    Deliberately a plain object rather than the platform cache: these figures
    are only valid for the duration of one batch, and putting them in Redis
    would mean a score computed on Monday quietly compared against Friday's
    sector medians.
    """

    def __init__(self, db: Any) -> None:
        self.db = db
        self._cache: dict[str, dict[str, Any]] = {}

    def for_sector(self, sector: str | None) -> dict[str, Any]:
        if not sector:
            return {}
        if sector not in self._cache:
            self._cache[sector] = self._compute(sector)
        return self._cache[sector]

    def _compute(self, sector: str) -> dict[str, Any]:
        companies = list(self.db.execute(
            select(Company).where(
                Company.sector == sector,
                Company.listing_status == "active",
            )
        ).scalars().all())
        if not companies:
            return {"peer_count": 0}

        caps = sorted(
            (c.market_cap for c in companies if c.market_cap), reverse=True
        )
        total_cap = sum(caps) if caps else 0.0
        top3 = sum(caps[:3]) if caps else 0.0

        # Per-company revenue CAGR from canonical facts. Computed from the
        # facts table directly rather than by building a full AnalysisService
        # per peer: a 40-company sector would otherwise mean 40 full statement
        # builds to produce one median.
        growths = self._revenue_growths([c.id for c in companies])

        return {
            "peer_count": max(0, len(companies) - 1),
            "sector_market_cap": total_cap or None,
            "top3_market_cap_share": (top3 / total_cap) if total_cap else None,
            "median_revenue_growth": _median(growths),
            "median_pe": self._median_pe(companies),
        }

    def _revenue_growths(self, company_ids: Sequence[str]) -> list[float]:
        """Revenue CAGR per company, read straight from `financial_facts`."""
        from app.models.company import FinancialFact

        if not company_ids:
            return []
        rows = self.db.execute(
            select(
                FinancialFact.company_id,
                FinancialFact.fiscal_year,
                FinancialFact.value,
            ).where(
                FinancialFact.company_id.in_(list(company_ids)),
                FinancialFact.line_item == "REVENUE",
                FinancialFact.value.is_not(None),
            )
        ).all()

        by_company: dict[str, dict[int, float]] = {}
        for company_id, year, value in rows:
            # Several precedences may exist for the same year. The canonical
            # builder resolves those by lowest precedence wins; replicating
            # that ordering here would duplicate the rule, so the last write
            # is taken and the resulting median is treated as indicative —
            # which is all a sector aggregate needs to be.
            by_company.setdefault(company_id, {})[year] = float(value)

        out: list[float] = []
        for series in by_company.values():
            if len(series) < 3:
                continue
            years = sorted(series)
            growth = series_cagr([series[y] for y in years])
            # Clip absurd outliers: a company whose earliest recorded revenue
            # is a rounding artefact produces a 400% CAGR that would drag a
            # median of forty companies on its own.
            if growth is not None and -0.5 < growth < 1.5:
                out.append(growth)
        return out

    def _median_pe(self, companies: Sequence[Company]) -> float | None:
        """Median trailing PE across the sector, from facts and price."""
        from app.models.company import FinancialFact

        ids = [c.id for c in companies if c.current_price]
        if not ids:
            return None

        latest_year = self.db.execute(
            select(func.max(FinancialFact.fiscal_year)).where(
                FinancialFact.company_id.in_(ids),
                FinancialFact.line_item == "WEIGHTED_SHARES",
            )
        ).scalar_one_or_none()
        if latest_year is None:
            return None

        rows = self.db.execute(
            select(
                FinancialFact.company_id,
                FinancialFact.line_item,
                FinancialFact.value,
            ).where(
                FinancialFact.company_id.in_(ids),
                FinancialFact.fiscal_year == latest_year,
                FinancialFact.line_item.in_(["WEIGHTED_SHARES", "PAT"]),
                FinancialFact.value.is_not(None),
            )
        ).all()

        facts: dict[str, dict[str, float]] = {}
        for company_id, line, value in rows:
            facts.setdefault(company_id, {})[line] = float(value)

        prices = {c.id: c.current_price for c in companies if c.current_price}
        pes: list[float] = []
        for company_id, values in facts.items():
            shares = values.get("WEIGHTED_SHARES")
            pat = values.get("PAT")
            price = prices.get(company_id)
            if not shares or not pat or not price or pat <= 0:
                continue
            eps = pat / shares
            if eps <= 0:
                continue
            pe = price / eps
            # A PE above 200 is either a near-zero earnings base or a data
            # error, and either way it is not information about the sector.
            if 0 < pe < 200:
                pes.append(pe)
        return _median(pes)


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


class AIScoringService:
    """Score companies and manage the permanent version history."""

    def __init__(self, db: Any, *, sector_cache: SectorStatsCache | None = None) -> None:
        self.db = db
        self.sectors = sector_cache or SectorStatsCache(db)
        self.evidence_builder = EvidenceBuilder(db)

    # -------------------------------------------------------------- scoring
    def build_evidence(self, company: Company) -> ScoringEvidence:
        """Resolve everything the ten modules read.

        The analysis and valuation engines are optional: a company with no
        financial history still receives a score built on documents and
        reference data, with every quantitative factor reported as missing.
        Refusing to score it would be less useful than scoring it honestly.
        """
        analysis = None
        bundle = None
        try:
            from app.services.analysis_service import AnalysisService
            analysis = AnalysisService.for_ticker(
                self.db, company.ticker, provision=False,
            )
        except Exception:  # noqa: BLE001
            log.warning("analysis unavailable for scoring", ticker=company.ticker)

        if analysis is not None and analysis.has_data:
            try:
                from app.domain.forecast.assumptions import Scenario
                from app.services.forecast.service import ForecastService
                from app.services.valuation.service import ValuationService
                bundle = ValuationService(self.db).value_company(
                    analysis, ForecastService(self.db), horizon=5,
                    scenario=Scenario.BASE,
                )
            except Exception:  # noqa: BLE001 — valuation is optional context
                log.warning("valuation unavailable for scoring",
                            ticker=company.ticker)

        return self.evidence_builder.build(
            company, analysis=analysis, valuation_bundle=bundle,
        )

    def score_company(self, company: Company) -> AIScoreResult:
        """Compute a fresh, explainable score. Does not persist."""
        evidence = self.build_evidence(company)
        stats = self.sectors.for_sector(company.sector)
        return compute(evidence, sector_stats=stats)

    def score_ticker(self, ticker: str) -> AIScoreResult:
        company = self.db.execute(
            select(Company).where(func.upper(Company.ticker) == ticker.upper())
        ).scalars().first()
        if company is None:
            raise AIScoringError(f"unknown ticker '{ticker}'")
        return self.score_company(company)

    # -------------------------------------------------------------- history
    def current(self, company_id: str) -> AIScoreVersion | None:
        return self.db.execute(
            select(AIScoreVersion).where(
                AIScoreVersion.company_id == company_id,
                AIScoreVersion.status == "current",
            )
        ).scalars().first()

    def history(
        self, company_id: str, *, limit: int = 50,
    ) -> list[AIScoreVersion]:
        """Every retained version, newest first. Nothing is ever pruned."""
        return list(self.db.execute(
            select(AIScoreVersion)
            .where(AIScoreVersion.company_id == company_id)
            .order_by(AIScoreVersion.version.desc())
            .limit(limit)
        ).scalars().all())

    def version(self, company_id: str, version: int) -> AIScoreVersion | None:
        return self.db.execute(
            select(AIScoreVersion).where(
                AIScoreVersion.company_id == company_id,
                AIScoreVersion.version == version,
            )
        ).scalars().first()

    def record(
        self,
        result: AIScoreResult,
        *,
        trigger: str = "manual",
        trigger_document_id: int | None = None,
        force: bool = False,
    ) -> RecordOutcome:
        """Append a new version, superseding the previous one.

        Declines to write when the input fingerprint AND the framework version
        both match the current row: the same evidence scored under the same
        framework is the same observation, and recording it again would bury
        real changes under identical rows. ``force`` overrides this, which the
        backfill uses when it genuinely wants a dated re-attestation.
        """
        existing = self.current(result.company_id)

        if (
            existing is not None
            and not force
            and existing.input_fingerprint == result.input_fingerprint
            and existing.framework_version == result.framework_version
        ):
            return RecordOutcome(
                version=existing, created=False,
                reason=(
                    f"Inputs unchanged since v{existing.version} "
                    f"(fingerprint {result.input_fingerprint[:12]}) under the "
                    f"same framework {result.framework_version}. No new "
                    "version written; the existing one still describes the "
                    "current evidence."
                ),
            )

        # Version numbers come from MAX rather than from a count: a count
        # would reuse a number if a row were ever removed, and reusing a
        # version number in an append-only table is worse than a gap.
        highest = self.db.execute(
            select(func.max(AIScoreVersion.version)).where(
                AIScoreVersion.company_id == result.company_id
            )
        ).scalar_one_or_none()
        next_version = int(highest or 0) + 1

        delta = None
        if existing is not None:
            delta = round(result.overall_score - existing.overall_score, 4)
            existing.status = "superseded"

        row = AIScoreVersion(
            company_id=result.company_id,
            version=next_version,
            status="current",
            framework_version=result.framework_version,
            overall_score=result.overall_score,
            rating=result.rating.value,
            recommendation=result.recommendation.value,
            coverage=result.coverage,
            module_scores={m.key: round(m.score, 4) for m in result.modules},
            probabilities={p.key: round(p.probability, 4)
                           for p in result.probabilities},
            detail=result.as_dict(),
            summary=result.summary,
            recommendation_reason=result.recommendation_reason,
            input_fingerprint=result.input_fingerprint,
            total_citations=result.total_citations,
            trigger=trigger,
            trigger_document_id=trigger_document_id,
            supersedes_version=existing.version if existing else None,
            score_delta=delta,
        )
        self.db.add(row)
        self.db.commit()

        log.info("ai score version recorded", ticker=result.ticker,
                 version=next_version, score=round(result.overall_score, 2),
                 delta=delta, trigger=trigger)

        return RecordOutcome(
            version=row, created=True,
            reason=(
                f"Recorded v{next_version}"
                + (f", superseding v{existing.version} "
                   f"({delta:+.2f} points)." if existing else " (first score).")
            ),
            delta=delta,
        )

    def score_and_record(
        self,
        company: Company,
        *,
        trigger: str = "manual",
        trigger_document_id: int | None = None,
        force: bool = False,
    ) -> tuple[AIScoreResult, RecordOutcome]:
        result = self.score_company(company)
        outcome = self.record(
            result, trigger=trigger,
            trigger_document_id=trigger_document_id, force=force,
        )
        return result, outcome

    # ------------------------------------------------------------ dashboard
    def leaderboard(self, *, top: int = 20, ascending: bool = False) -> list[dict]:
        order = (AIScoreVersion.overall_score.asc() if ascending
                 else AIScoreVersion.overall_score.desc())
        rows = self.db.execute(
            select(AIScoreVersion, Company)
            .join(Company, Company.id == AIScoreVersion.company_id)
            .where(AIScoreVersion.status == "current")
            .order_by(order)
            .limit(top)
        ).all()
        return [
            {
                "ticker": company.ticker, "name": company.name,
                "score": round(version.overall_score, 2),
                "rating": version.rating,
                "recommendation": version.recommendation,
                "coverage": round(version.coverage, 4),
                "version": version.version,
                "computed_at": version.computed_at,
            }
            for version, company in rows
        ]

    def dashboard(self) -> dict[str, Any]:
        """Universe-level summary of the current scores."""
        total = self.db.execute(
            select(func.count(AIScoreVersion.id))
            .where(AIScoreVersion.status == "current")
        ).scalar_one()

        if not total:
            return {
                "scored_companies": 0,
                "framework_version": FRAMEWORK_VERSION,
                "note": "No company has been scored yet.",
            }

        average, average_coverage, versions = self.db.execute(
            select(
                func.avg(AIScoreVersion.overall_score),
                func.avg(AIScoreVersion.coverage),
                func.sum(AIScoreVersion.version),
            ).where(AIScoreVersion.status == "current")
        ).one()

        ratings = dict(self.db.execute(
            select(AIScoreVersion.rating, func.count(AIScoreVersion.id))
            .where(AIScoreVersion.status == "current")
            .group_by(AIScoreVersion.rating)
        ).all())
        recommendations = dict(self.db.execute(
            select(AIScoreVersion.recommendation, func.count(AIScoreVersion.id))
            .where(AIScoreVersion.status == "current")
            .group_by(AIScoreVersion.recommendation)
        ).all())
        retained = self.db.execute(
            select(func.count(AIScoreVersion.id))
        ).scalar_one()

        return {
            "scored_companies": int(total),
            "average_score": round(float(average or 0.0), 2),
            "average_coverage": round(float(average_coverage or 0.0), 4),
            "ratings": ratings,
            "recommendations": recommendations,
            "versions_retained": int(retained),
            "framework_version": FRAMEWORK_VERSION,
            "top": self.leaderboard(top=10),
            "bottom": self.leaderboard(top=10, ascending=True),
        }

    # ------------------------------------------------------------- batching
    def score_universe(
        self,
        *,
        limit: int | None = None,
        trigger: str = "scheduled",
        companies: Iterable[Company] | None = None,
    ) -> dict[str, Any]:
        """Score every active company, recording versions where inputs moved.

        Sector statistics are shared across the whole batch through one cache,
        so a 500-company sweep computes each sector aggregate once.
        """
        started = time.perf_counter()
        if companies is None:
            stmt = select(Company).where(Company.listing_status == "active")
            if limit:
                stmt = stmt.limit(limit)
            companies = list(self.db.execute(stmt).scalars().all())
        else:
            companies = list(companies)

        scored = created = unchanged = failed = 0
        errors: list[str] = []

        for company in companies:
            try:
                _, outcome = self.score_and_record(company, trigger=trigger)
                scored += 1
                if outcome.created:
                    created += 1
                else:
                    unchanged += 1
            except Exception as exc:  # noqa: BLE001 — one bad company must
                # not abort a 500-company sweep
                failed += 1
                self.db.rollback()
                if len(errors) < 10:
                    errors.append(f"{company.ticker}: {exc}")
                log.warning("scoring failed", ticker=company.ticker,
                            error=str(exc))

        return {
            "companies": len(companies),
            "scored": scored,
            "new_versions": created,
            "unchanged": unchanged,
            "failed": failed,
            "errors": errors,
            "sectors_cached": len(self.sectors._cache),
            "elapsed_seconds": round(time.perf_counter() - started, 1),
        }
