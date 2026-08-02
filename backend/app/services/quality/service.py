"""Computes the Data Quality Score from what the database actually holds.

Every check below reads a row. None infers a result from another check's
success, and none awards a floor for effort — a company the platform has
barely seen scores accordingly, which is the entire point of the exercise.

The scorer is deliberately read-only and stateless. It is cheap enough to run
on demand, and the persisted snapshot exists for the dashboard's aggregate
queries rather than because recomputation is expensive.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog
from sqlalchemy import func, select

from app.domain.quality.score import (
    CHECKS, FRESHNESS_HORIZON_DAYS, WEIGHTS, CheckResult, Dimension,
    DimensionScore, QualityScore,
)
from app.models.analysis import QuarterlyResult, ShareholdingSnapshot
from app.models.company import Company, FinancialFact
from app.models.document import Document, DocumentChunk, DocumentFact
from app.models.filing_collection import CompanyCrawlState, DiscoveredFiling
from app.models.knowledge import (
    DocumentSummary, KnowledgeEntry, YearlyObservation,
)

log = structlog.get_logger(__name__)

#: Canonical line items that evidence each statement. Chosen as the item that
#: cannot be absent from a real statement of that kind, so their presence is a
#: genuine signal rather than a coincidence of one populated field.
STATEMENT_MARKERS: dict[str, tuple[str, ...]] = {
    "income_statement": ("revenue", "employee_benefit", "other_expenses"),
    "balance_sheet": ("equity_share_capital", "reserves_surplus",
                      "trade_receivables"),
    "cash_flow": ("capex", "direct_taxes_paid", "opening_cash"),
}

#: Vault sections that evidence each Knowledge Vault check.
VAULT_SECTIONS: dict[str, tuple[str, ...]] = {
    "business_summary": ("business_model", "company_profile"),
    "ai_notes": ("ai_notes",),
    "historical_observations": ("historical_ai_analysis",),
}

#: Summary kinds that evidence each summary-backed check.
SUMMARY_KINDS: dict[str, tuple[str, ...]] = {
    "executive_summary": ("institutional", "brief_100", "detailed_500"),
    "investment_summary": ("investment",),
}

#: Vault sections and summaries that evidence each AI-coverage check. The AI
#: layer generates on demand rather than persisting a report per capability,
#: so coverage is measured by the durable artefacts it leaves behind.
AI_COVERAGE_EVIDENCE: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    # (vault sections, summary kinds)
    "business_model": (("business_model", "products", "revenue_segments"), ()),
    "bull_thesis": ((), ("bull_thesis",)),
    "bear_thesis": ((), ("bear_thesis",)),
    "risks": (("risks",), ("risk",)),
    "catalysts": (("opportunities",), ()),
    "valuation": (("valuation",), ()),
    "management_analysis": (("management", "promoters"), ("management",)),
    "moat": (("business_model", "competitors"), ()),
}

TEN_YEARS = 10


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _days_since(value: datetime | None) -> int | None:
    aware = _aware(value)
    if aware is None:
        return None
    return max((_utcnow() - aware).days, 0)


def _decay(value: datetime | None, horizon_days: int) -> float:
    """1.0 when current, sliding to 0.0 at the horizon.

    Linear rather than a cliff. A filing one day past the horizon is not
    worthless, and a step function makes a score jump for no real change in
    the underlying data.
    """
    days = _days_since(value)
    if days is None:
        return 0.0
    if days <= 0:
        return 1.0
    if days >= horizon_days:
        return 0.0
    return round(1.0 - (days / horizon_days), 4)


class DataQualityService:
    """Scores one company, or every company, out of 100."""

    def __init__(self, db: Any) -> None:
        self.db = db

    # ------------------------------------------------------------- scoring
    def score_company(self, company: Company) -> QualityScore:
        started = time.perf_counter()
        result = QualityScore(company_id=company.id, ticker=company.ticker)

        result.dimensions = [
            self._identity(company),
            self._financials(company),
            self._documents(company),
            self._vault(company),
            self._ai_coverage(company),
            self._freshness(company),
            self._source_quality(company),
            self._system_health(company),
        ]

        result.last_updated_days = self._last_updated_days(company)
        result.knowledge_freshness_days = self._knowledge_freshness_days(company)
        result.next_crawl_at = self._next_crawl_at(company)

        log.debug("quality scored", ticker=company.ticker,
                  score=result.score,
                  ms=round((time.perf_counter() - started) * 1000, 1))
        return result

    def score_ticker(self, ticker: str) -> QualityScore | None:
        company = self.db.scalar(
            select(Company).where(Company.ticker == ticker.upper())
        )
        return self.score_company(company) if company else None

    # -------------------------------------------------------- 1. identity
    def _identity(self, company: Company) -> DimensionScore:
        state = self._crawl_state(company)
        present = {
            "nse_symbol": bool((company.ticker or "").strip()),
            "isin": bool((company.isin or "").strip()),
            "sector": bool((company.sector or "").strip()),
            "industry": bool((company.industry or "").strip()),
            # A discovered IR URL counts: it identifies the company's own web
            # presence just as a registered website does, and refusing it
            # would penalise a company the platform successfully researched.
            "official_website": bool(
                (company.website or "").strip()
                or (state.ir_url if state else None)
            ),
        }
        return DimensionScore(
            Dimension.IDENTITY,
            [CheckResult(key, 1.0 if present[key] else 0.0)
             for key in CHECKS[Dimension.IDENTITY]],
        )

    # ------------------------------------------------------ 2. financials
    def _financials(self, company: Company) -> DimensionScore:
        rows = self.db.execute(
            select(FinancialFact.line_item, FinancialFact.fiscal_year)
            .where(FinancialFact.company_id == company.id)
        ).all()
        items = {r[0] for r in rows}
        years = {r[1] for r in rows if r[1]}

        checks: list[CheckResult] = []
        for key in ("income_statement", "balance_sheet", "cash_flow"):
            markers = STATEMENT_MARKERS[key]
            hits = sum(1 for m in markers if m in items)
            checks.append(CheckResult(
                key, hits / len(markers),
                detail=f"{hits}/{len(markers)} marker line items",
            ))

        quarters = self.db.scalar(
            select(func.count()).select_from(QuarterlyResult)
            .where(QuarterlyResult.company_id == company.id)
        ) or 0
        checks.append(CheckResult(
            "quarterly_results", 1.0 if quarters else 0.0,
            detail=f"{quarters} quarters",
        ))

        # TTM requires four consecutive quarters; fewer cannot produce one.
        checks.append(CheckResult(
            "ttm", 1.0 if quarters >= 4 else round(quarters / 4, 4),
            detail=f"{quarters}/4 quarters for a trailing year",
        ))

        checks.append(CheckResult(
            "ten_year_history",
            min(len(years) / TEN_YEARS, 1.0),
            detail=f"{len(years)} fiscal years",
        ))
        return DimensionScore(Dimension.FINANCIAL_STATEMENTS, checks)

    # ------------------------------------------------------- 3. documents
    def _documents(self, company: Company) -> DimensionScore:
        rows = self.db.execute(
            select(Document.doc_type, func.count(), func.max(Document.created_at))
            .where(
                Document.company_id == company.id,
                Document.status == "completed",
            )
            .group_by(Document.doc_type)
        ).all()
        counts = {r[0]: r[1] for r in rows}
        newest_annual = next(
            (r[2] for r in rows if r[0] == "annual_report"), None,
        )

        annual = counts.get("annual_report", 0)
        checks = [
            # "Latest" means current, not merely held. A 2019 annual report
            # does not describe the company an analyst is asking about.
            CheckResult(
                "latest_annual_report",
                _decay(newest_annual,
                       FRESHNESS_HORIZON_DAYS["latest_annual_report"]),
                detail=f"{annual} annual reports held",
            ),
            CheckResult(
                "previous_annual_reports",
                min(max(annual - 1, 0) / 3, 1.0),
                detail="3 prior years is full credit",
            ),
            CheckResult(
                "quarterly_pdfs",
                min(counts.get("quarterly_report", 0) / 4, 1.0),
            ),
            CheckResult(
                "investor_presentations",
                min(counts.get("investor_presentation", 0) / 2, 1.0),
            ),
            CheckResult(
                "conference_call_transcripts",
                min(counts.get("conference_call", 0) / 2, 1.0),
            ),
            CheckResult(
                "credit_rating_reports",
                1.0 if counts.get("credit_rating", 0) else 0.0,
            ),
            CheckResult(
                "esg_reports",
                1.0 if counts.get("esg_report", 0) else 0.0,
            ),
        ]
        return DimensionScore(Dimension.DOCUMENTS, checks)

    # ----------------------------------------------------------- 4. vault
    def _vault(self, company: Company) -> DimensionScore:
        sections = {
            r[0] for r in self.db.execute(
                select(KnowledgeEntry.section)
                .where(
                    KnowledgeEntry.company_id == company.id,
                    KnowledgeEntry.status == "current",
                )
                .distinct()
            ).all()
        }
        kinds = {
            r[0] for r in self.db.execute(
                select(DocumentSummary.kind)
                .where(
                    DocumentSummary.company_id == company.id,
                    DocumentSummary.is_fallback.is_(False),
                )
                .distinct()
            ).all()
        }
        observations = self.db.scalar(
            select(func.count()).select_from(YearlyObservation)
            .where(
                YearlyObservation.company_id == company.id,
                YearlyObservation.status == "current",
            )
        ) or 0

        checks = [
            CheckResult("business_summary", 1.0 if (
                sections & set(VAULT_SECTIONS["business_summary"])) else 0.0),
            CheckResult("ai_notes", 1.0 if "ai_notes" in sections else 0.0),
            CheckResult("executive_summary", 1.0 if (
                kinds & set(SUMMARY_KINDS["executive_summary"])) else 0.0),
            CheckResult("investment_summary", 1.0 if (
                kinds & set(SUMMARY_KINDS["investment_summary"])) else 0.0),
            CheckResult(
                "historical_observations",
                1.0 if "historical_ai_analysis" in sections else 0.0,
            ),
            # Two years is the minimum that makes a temporal series a series;
            # one observation is a snapshot.
            CheckResult(
                "temporal_memory", min(observations / 2, 1.0),
                detail=f"{observations} observed fiscal years",
            ),
        ]
        return DimensionScore(Dimension.KNOWLEDGE_VAULT, checks)

    # ----------------------------------------------------- 5. AI coverage
    def _ai_coverage(self, company: Company) -> DimensionScore:
        sections = {
            r[0] for r in self.db.execute(
                select(KnowledgeEntry.section)
                .where(
                    KnowledgeEntry.company_id == company.id,
                    KnowledgeEntry.status == "current",
                )
                .distinct()
            ).all()
        }
        kinds = {
            r[0] for r in self.db.execute(
                select(DocumentSummary.kind)
                .where(
                    DocumentSummary.company_id == company.id,
                    DocumentSummary.is_fallback.is_(False),
                )
                .distinct()
            ).all()
        }

        checks: list[CheckResult] = []
        for key in CHECKS[Dimension.AI_COVERAGE]:
            if key == "forecast":
                # A forecast needs history to extrapolate from; three years is
                # the floor below which a projection is arithmetic on noise.
                years = self.db.scalar(
                    select(func.count(func.distinct(FinancialFact.fiscal_year)))
                    .where(FinancialFact.company_id == company.id)
                ) or 0
                checks.append(CheckResult(
                    key, 1.0 if years >= 3 else 0.0,
                    detail=f"{years} fiscal years of history",
                ))
                continue
            if key == "confidence_score":
                # Confidence exists where a real (non-fallback) observation
                # carries one.
                scored = self.db.scalar(
                    select(func.count()).select_from(YearlyObservation)
                    .where(
                        YearlyObservation.company_id == company.id,
                        YearlyObservation.status == "current",
                        YearlyObservation.is_fallback.is_(False),
                        YearlyObservation.confidence > 0,
                    )
                ) or 0
                checks.append(CheckResult(key, 1.0 if scored else 0.0))
                continue

            want_sections, want_kinds = AI_COVERAGE_EVIDENCE.get(key, ((), ()))
            hit = bool(sections & set(want_sections)) or bool(kinds & set(want_kinds))
            checks.append(CheckResult(key, 1.0 if hit else 0.0))

        return DimensionScore(Dimension.AI_COVERAGE, checks)

    # ------------------------------------------------------- 6. freshness
    def _freshness(self, company: Company) -> DimensionScore:
        latest_filing = self.db.scalar(
            select(func.max(DiscoveredFiling.discovered_at))
            .where(DiscoveredFiling.company_id == company.id)
        )
        latest_annual = self.db.scalar(
            select(func.max(Document.created_at))
            .where(
                Document.company_id == company.id,
                Document.doc_type == "annual_report",
                Document.status == "completed",
            )
        )
        latest_quarter = self.db.scalar(
            select(func.max(QuarterlyResult.updated_at))
            .where(QuarterlyResult.company_id == company.id)
        )
        price_at = company.updated_at if company.current_price else None

        checks = [
            CheckResult("latest_filing",
                        _decay(latest_filing,
                               FRESHNESS_HORIZON_DAYS["latest_filing"]),
                        detail=_age_detail(latest_filing)),
            CheckResult("latest_quarterly",
                        _decay(latest_quarter,
                               FRESHNESS_HORIZON_DAYS["latest_quarterly"]),
                        detail=_age_detail(latest_quarter)),
            CheckResult("latest_annual_report",
                        _decay(latest_annual,
                               FRESHNESS_HORIZON_DAYS["latest_annual_report"]),
                        detail=_age_detail(latest_annual)),
            CheckResult("latest_price",
                        _decay(price_at,
                               FRESHNESS_HORIZON_DAYS["latest_price"]),
                        detail=_age_detail(price_at)),
        ]
        return DimensionScore(Dimension.FRESHNESS, checks)

    # -------------------------------------------------- 7. source quality
    def _source_quality(self, company: Company) -> DimensionScore:
        state = self._crawl_state(company)
        by_source = {
            r[0] for r in self.db.execute(
                select(DiscoveredFiling.source)
                .where(DiscoveredFiling.company_id == company.id)
                .distinct()
            ).all()
        }
        mean_confidence = self.db.scalar(
            select(func.avg(DiscoveredFiling.classification_confidence))
            .where(
                DiscoveredFiling.company_id == company.id,
                DiscoveredFiling.classification_confidence.is_not(None),
            )
        )
        facts = self.db.scalar(
            select(func.count()).select_from(FinancialFact)
            .where(FinancialFact.company_id == company.id)
        ) or 0

        # An IR URL discovered by probe is worth less than one verified, and
        # the stored confidence already expresses that. Reusing it keeps the
        # two views of the same fact consistent.
        ir_value = 0.0
        if state and state.ir_url:
            ir_value = float(state.ir_url_confidence or 0.5)

        checks = [
            CheckResult("official_ir", min(ir_value, 1.0),
                        detail=(state.ir_url_method if state else None)),
            CheckResult(
                "nse",
                1.0 if any("NSE" in s for s in by_source) else 0.0,
            ),
            CheckResult(
                "bse",
                1.0 if any("BSE" in s for s in by_source) else 0.0,
            ),
            CheckResult(
                "verified_financial_database", 1.0 if facts else 0.0,
                detail=f"{facts} canonical facts",
            ),
            CheckResult(
                "confidence",
                float(mean_confidence) if mean_confidence else 0.0,
                detail="mean classification confidence",
            ),
        ]
        return DimensionScore(Dimension.SOURCE_QUALITY, checks)

    # --------------------------------------------------- 8. system health
    def _system_health(self, company: Company) -> DimensionScore:
        total = self.db.scalar(
            select(func.count()).select_from(Document)
            .where(Document.company_id == company.id)
        ) or 0

        if not total:
            # No documents is not a pipeline failure. Scoring it zero would
            # blame the platform for a company nobody has uploaded anything
            # for; the DOCUMENTS dimension already records that absence.
            return DimensionScore(
                Dimension.SYSTEM_HEALTH,
                [CheckResult(key, 0.0, detail="no documents held")
                 for key in CHECKS[Dimension.SYSTEM_HEALTH]],
            )

        completed = self.db.scalar(
            select(func.count()).select_from(Document)
            .where(
                Document.company_id == company.id,
                Document.status == "completed",
            )
        ) or 0
        with_facts = self.db.scalar(
            select(func.count(func.distinct(DocumentFact.document_id)))
            .where(DocumentFact.company_id == company.id)
        ) or 0
        with_chunks = self.db.scalar(
            select(func.count(func.distinct(DocumentChunk.document_id)))
            .join(Document, Document.id == DocumentChunk.document_id)
            .where(Document.company_id == company.id)
        ) or 0
        embedded = self.db.scalar(
            select(func.count(func.distinct(DocumentChunk.document_id)))
            .join(Document, Document.id == DocumentChunk.document_id)
            .where(
                Document.company_id == company.id,
                DocumentChunk.embedding.is_not(None),
            )
        ) or 0

        checks = [
            CheckResult("successful_parsing", min(completed / total, 1.0),
                        detail=f"{completed}/{total} documents completed"),
            CheckResult("successful_extraction", min(with_facts / total, 1.0),
                        detail=f"{with_facts}/{total} yielded fields"),
            CheckResult("successful_embeddings", min(embedded / total, 1.0),
                        detail=f"{embedded}/{total} embedded"),
            CheckResult("successful_rag", min(with_chunks / total, 1.0),
                        detail=f"{with_chunks}/{total} indexed"),
        ]
        return DimensionScore(Dimension.SYSTEM_HEALTH, checks)

    # ------------------------------------------------------------ context
    def _crawl_state(self, company: Company) -> CompanyCrawlState | None:
        return self.db.scalar(
            select(CompanyCrawlState)
            .where(CompanyCrawlState.company_id == company.id)
        )

    def _last_updated_days(self, company: Company) -> int | None:
        """Days since the most recent evidence of ANY kind."""
        stamps = [
            self.db.scalar(
                select(func.max(Document.created_at))
                .where(Document.company_id == company.id)
            ),
            self.db.scalar(
                select(func.max(DiscoveredFiling.discovered_at))
                .where(DiscoveredFiling.company_id == company.id)
            ),
            self.db.scalar(
                select(func.max(KnowledgeEntry.created_at))
                .where(KnowledgeEntry.company_id == company.id)
            ),
            self.db.scalar(
                select(func.max(QuarterlyResult.updated_at))
                .where(QuarterlyResult.company_id == company.id)
            ),
        ]
        ages = [_days_since(s) for s in stamps if s is not None]
        return min(ages) if ages else None

    def _knowledge_freshness_days(self, company: Company) -> int | None:
        newest = self.db.scalar(
            select(func.max(KnowledgeEntry.created_at))
            .where(KnowledgeEntry.company_id == company.id)
        )
        return _days_since(newest)

    def _next_crawl_at(self, company: Company) -> datetime | None:
        state = self._crawl_state(company)
        if state is None or not state.enabled:
            return None
        from app.domain.filings.collection import (
            TIER_INTERVAL_SECONDS, CollectionTier,
        )
        try:
            tier = CollectionTier(state.tier)
        except ValueError:
            tier = CollectionTier.WEEKLY
        if tier is CollectionTier.PAUSED:
            return None
        last = _aware(state.last_crawled_at)
        if last is None:
            return _utcnow()
        return last + timedelta(seconds=TIER_INTERVAL_SECONDS[tier])


def _age_detail(value: datetime | None) -> str | None:
    days = _days_since(value)
    if days is None:
        return "never"
    return f"{days}d ago"


# ===========================================================================
# Persistence and aggregation
# ===========================================================================
DIMENSION_COLUMNS: dict[Dimension, str] = {
    Dimension.IDENTITY: "identity_points",
    Dimension.FINANCIAL_STATEMENTS: "financials_points",
    Dimension.DOCUMENTS: "documents_points",
    Dimension.KNOWLEDGE_VAULT: "vault_points",
    Dimension.AI_COVERAGE: "ai_points",
    Dimension.FRESHNESS: "freshness_points",
    Dimension.SOURCE_QUALITY: "source_points",
    Dimension.SYSTEM_HEALTH: "health_points",
}


class QualitySnapshotService:
    """Writes and reads the persisted score."""

    def __init__(self, db: Any) -> None:
        self.db = db
        self.scorer = DataQualityService(db)

    def refresh(self, company: Company) -> QualityScore:
        """Score one company and persist it. Idempotent."""
        from app.models.scoring import DataQualitySnapshot

        result = self.scorer.score_company(company)
        row = self.db.scalar(
            select(DataQualitySnapshot)
            .where(DataQualitySnapshot.company_id == company.id)
        )
        if row is None:
            row = DataQualitySnapshot(company_id=company.id)
            self.db.add(row)

        row.score = result.score
        row.grade = result.grade.value
        for dimension in result.dimensions:
            setattr(row, DIMENSION_COLUMNS[dimension.dimension],
                    dimension.points)
        row.missing_items = result.missing_items
        row.missing_count = len(result.missing_items)
        row.last_updated_days = result.last_updated_days
        row.knowledge_freshness_days = result.knowledge_freshness_days
        row.computed_at = _utcnow()
        self.db.commit()
        return result

    def refresh_all(self, *, limit: int | None = None) -> dict[str, Any]:
        """Rescore the universe. Used by the nightly sweep and backfills."""
        stmt = select(Company).where(Company.listing_status == "active")
        if limit:
            stmt = stmt.limit(limit)

        started = time.perf_counter()
        scored = 0
        failed: list[dict[str, str]] = []
        for company in self.db.scalars(stmt):
            try:
                self.refresh(company)
                scored += 1
            except Exception as exc:  # noqa: BLE001 — one company must not stop the sweep
                self.db.rollback()
                failed.append({
                    "ticker": company.ticker,
                    "error": f"{type(exc).__name__}: {exc}"[:200],
                })
                log.warning("quality refresh failed", ticker=company.ticker,
                            error=str(exc)[:200])
        return {
            "scored": scored,
            "failed": len(failed),
            "errors": failed[:10],
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
        }

    # --------------------------------------------------------- dashboard
    def dashboard(self, *, top: int = 10) -> dict[str, Any]:
        """Universe-wide quality, from the persisted snapshots."""
        from app.models.scoring import DataQualitySnapshot

        total = self.db.scalar(
            select(func.count()).select_from(DataQualitySnapshot)
        ) or 0
        if not total:
            return {
                "companies_scored": 0,
                "unavailable_reason": (
                    "No company has been scored yet. Scores are computed by "
                    "the nightly sweep or on first request."
                ),
            }

        average = self.db.scalar(select(func.avg(DataQualitySnapshot.score)))

        def _above(threshold: int) -> int:
            return self.db.scalar(
                select(func.count()).select_from(DataQualitySnapshot)
                .where(DataQualitySnapshot.score >= threshold)
            ) or 0

        def _leaderboard(descending: bool) -> list[dict[str, Any]]:
            order = (
                DataQualitySnapshot.score.desc() if descending
                else DataQualitySnapshot.score.asc()
            )
            rows = self.db.execute(
                select(DataQualitySnapshot, Company.ticker, Company.name)
                .join(Company, Company.id == DataQualitySnapshot.company_id)
                .order_by(order)
                .limit(top)
            ).all()
            return [
                {
                    "ticker": ticker, "name": name,
                    "score": snapshot.score, "grade": snapshot.grade,
                    "missing_count": snapshot.missing_count,
                }
                for snapshot, ticker, name in rows
            ]

        grades = {
            grade: count for grade, count in self.db.execute(
                select(DataQualitySnapshot.grade, func.count())
                .group_by(DataQualitySnapshot.grade)
            ).all()
        }

        dimension_means = {
            dimension.value: round(
                self.db.scalar(
                    select(func.avg(
                        getattr(DataQualitySnapshot, column)
                    ))
                ) or 0.0, 2,
            )
            for dimension, column in DIMENSION_COLUMNS.items()
        }

        return {
            "companies_scored": total,
            "average_score": round(float(average or 0.0), 2),
            "above": {
                "90": _above(90), "80": _above(80),
                "70": _above(70), "60": _above(60),
            },
            "by_grade": grades,
            "highest": _leaderboard(descending=True),
            "lowest": _leaderboard(descending=False),
            "average_points_by_dimension": dimension_means,
            "weights": {d.value: w for d, w in WEIGHTS.items()},
        }
