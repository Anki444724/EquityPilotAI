"""Report service — gathering, building, rendering, caching, versioning.

The only module that knows both the platform engines and the database. Its
shape follows the same pattern as Modules 7 and 8: gather inputs once, build
once, cache against a content key, and record every failure rather than
swallowing it.

**Caching is by content, not clock.** The key hashes the company's data
version, the report type, the theme and the requested sections. Identical
inputs return the stored report and its already-rendered artefacts. A changed
forecast assumption or a new document changes the company's data version and
therefore the key, so a stale report can never be served as current.

**Gathering never raises.** Each engine is called inside its own guard and a
failure is recorded in `ReportInputs.errors`. A report whose valuation engine
fell over should still contain its financial analysis and say plainly why the
valuation is missing — which is exactly what the "Insufficient evidence"
requirement is for.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.domain.reports.blocks import (
    ReportDocument, ReportType, Theme, narratives_for,
)
from app.domain.reports.citations import audit_report
from app.models.company import Company
from app.models.report import Report, ReportArtifact, ReportJob
from app.services.reports.builder import ReportBuilder, ReportInputs
from app.services.reports.charts.engine import ChartEngine
from app.services.reports.renderers import docx, pdf, web, xlsx  # noqa: F401
from app.services.reports.renderers.base import (
    OutputFormat, RenderResult, renderer_for,
)
from app.services.reports.serialise import document_to_dict

logger = logging.getLogger(__name__)

#: Formats produced by default. HTML is always included: it is the in-app
#: preview and costs two milliseconds.
DEFAULT_FORMATS: tuple[OutputFormat, ...] = (
    OutputFormat.HTML, OutputFormat.PDF,
)


class ReportError(Exception):
    """A report could not be generated."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class GenerationResult:
    report: Report
    document: ReportDocument
    artifacts: list[RenderResult]
    cached: bool = False
    timings: dict[str, float] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.timings is None:
            self.timings = {}


class ReportService:
    """Generates, stores and serves reports."""

    def __init__(self, db: Session) -> None:
        self.db = db
        self.errors: dict[str, str] = {}

    # ================================================================
    # Generation
    # ================================================================
    def generate(
        self,
        company_id: str,
        report_type: ReportType,
        *,
        owner_id: str,
        formats: Sequence[OutputFormat] = DEFAULT_FORMATS,
        theme: Theme = Theme.LIGHT,
        analyst: str = "",
        portfolio_id: int | None = None,
        include_ai: bool = True,
        use_cache: bool = True,
    ) -> GenerationResult:
        company = self.db.get(Company, company_id)
        if company is None:
            raise ReportError(f"unknown company '{company_id}'")

        formats = tuple(dict.fromkeys(formats)) or DEFAULT_FORMATS
        key = self._cache_key(company, report_type, theme, formats, include_ai)

        if use_cache:
            cached = self._cached(company_id, report_type, key, formats)
            if cached is not None:
                return cached

        timings: dict[str, float] = {}
        self.errors = {}

        started = time.perf_counter()
        inputs = self._gather(company, portfolio_id, report_type, include_ai)
        timings["gather"] = round((time.perf_counter() - started) * 1000, 2)

        started = time.perf_counter()
        builder = ReportBuilder(
            inputs, report_type, theme=theme, analyst=analyst,
        )
        document = builder.build()
        timings["build"] = round((time.perf_counter() - started) * 1000, 2)

        started = time.perf_counter()
        audit = audit_report(document)
        timings["audit"] = round((time.perf_counter() - started) * 1000, 2)

        report = self._persist(
            company, report_type, document, audit, key, owner_id,
            analyst, portfolio_id, theme, timings,
        )

        started = time.perf_counter()
        # One chart engine across every format, so a chart is rasterised once
        # and reused by the PDF, the DOCX and the HTML preview.
        engine = ChartEngine(theme)
        artifacts: list[RenderResult] = []
        for fmt in formats:
            rendered = renderer_for(fmt, engine).render(document)
            artifacts.append(rendered)
            timings[f"render_{fmt.value}"] = rendered.took_ms
            self.db.add(ReportArtifact(
                report_id=report.id, fmt=fmt.value,
                filename=rendered.filename, media_type=fmt.media_type,
                payload=rendered.payload, size_bytes=rendered.size_bytes,
                page_count=rendered.page_count, render_ms=rendered.took_ms,
            ))
        timings["render_total"] = round((time.perf_counter() - started) * 1000, 2)
        timings["charts"] = engine.stats().get("misses", 0)

        report.status = "ready"
        report.build_ms = round(sum(
            v for k, v in timings.items() if k != "charts"
        ), 2)
        self.db.commit()
        self.db.refresh(report)

        return GenerationResult(
            report=report, document=document, artifacts=artifacts,
            cached=False, timings=timings,
        )

    # ---------------------------------------------------------- caching
    def _cache_key(
        self, company: Company, report_type: ReportType, theme: Theme,
        formats: Sequence[OutputFormat], include_ai: bool,
    ) -> str:
        """Content key. Any change to the inputs produces a new key.

        `data_version` is bumped by the platform whenever a company's facts
        change, so a re-imported filing or an edited forecast invalidates every
        report built from it without any explicit cache busting.
        """
        raw = "|".join([
            company.id,
            str(getattr(company, "data_version", 1)),
            str(getattr(company, "current_price", "")),
            report_type.value, theme.value,
            ",".join(sorted(f.value for f in formats)),
            "ai" if include_ai else "noai",
        ])
        return hashlib.sha1(raw.encode(), usedforsecurity=False).hexdigest()[:20]

    def _cached(
        self, company_id: str, report_type: ReportType, key: str,
        formats: Sequence[OutputFormat],
    ) -> GenerationResult | None:
        report = self.db.scalar(
            select(Report)
            .where(
                Report.company_id == company_id,
                Report.report_type == report_type.value,
                Report.input_hash == key,
                Report.status == "ready",
                Report.superseded_by.is_(None),
            )
            .order_by(Report.version.desc())
        )
        if report is None:
            return None
        stored = {a.fmt for a in report.artifacts}
        if not {f.value for f in formats} <= stored:
            # A previously-generated report that lacks a newly-requested
            # format is not a hit: returning it would silently omit the file.
            return None

        document = None
        return GenerationResult(
            report=report, document=document, artifacts=[], cached=True,
        )

    # --------------------------------------------------------- gather
    def _gather(
        self, company: Company, portfolio_id: int | None,
        report_type: ReportType, include_ai: bool,
    ) -> ReportInputs:
        """Resolve every engine once. Failures are recorded, never raised."""
        from app.services.analysis_service import AnalysisService

        inputs = ReportInputs(company=company, errors=self.errors)

        analysis = self._guard(
            "analysis", lambda: AnalysisService.for_ticker(self.db, company.ticker)
        )
        if analysis is None or not getattr(analysis, "has_data", False):
            self.errors.setdefault(
                "analysis",
                "No financial statements have been imported for this company.",
            )
            inputs.peers = self._peers(company)
            return inputs
        inputs.analysis = analysis

        inputs.ratios = self._guard("ratios", lambda: self._ratios(analysis))

        from app.services.forecast.service import ForecastService
        from app.services.valuation.service import ValuationService

        forecast_service = ForecastService(self.db)
        valuation_service = ValuationService(self.db)

        inputs.forecast = self._guard(
            "forecast", lambda: self._forecast(analysis, forecast_service)
        )
        inputs.valuation = self._guard(
            "valuation",
            lambda: valuation_service.value_company(analysis, forecast_service),
        )
        inputs.scoring = self._guard(
            "scoring",
            lambda: self._scoring(analysis, forecast_service, valuation_service),
        )
        inputs.peers = self._peers(company)
        inputs.documents = self._guard(
            "documents", lambda: self._documents(company.id)
        ) or []

        if portfolio_id is not None:
            inputs.portfolio = self._guard(
                "portfolio", lambda: self._portfolio(portfolio_id)
            )
            if inputs.portfolio is not None:
                inputs.holding = next(
                    (
                        h for h in inputs.portfolio.holdings
                        if h.ticker == company.ticker
                    ),
                    None,
                )

        if include_ai:
            inputs.narratives = self._guard(
                "narratives", lambda: self._narratives(analysis, report_type)
            ) or {}
        return inputs

    def _guard(self, key: str, call) -> Any:
        try:
            return call()
        except Exception as exc:  # pragma: no cover - resilience path
            logger.warning("report input '%s' failed: %s", key, exc)
            self.errors[key] = f"{type(exc).__name__}: {exc}"
            return None

    def _ratios(self, analysis) -> Any:
        from app.services.ratios.service import RatioService

        service = RatioService(
            analysis.incomes, analysis.balances, analysis.cash_flows,
        )
        # `all_sections()` is the service's own entry point. An earlier version
        # guessed at per-family methods that do not exist; the failure was
        # recorded in `errors` rather than swallowed, which is how it surfaced.
        return type("Ratios", (), {"sections": service.all_sections()})()

    @staticmethod
    def _forecast(analysis, forecast_service) -> Any:
        from app.domain.forecast.assumptions import Scenario

        context = forecast_service.build_context(
            analysis.company, analysis.statements, years=5,
        )
        saved = forecast_service.active_for_company(analysis.company.id)
        return forecast_service.run(context, saved, Scenario.BASE)

    def _scoring(self, analysis, forecast_service, valuation_service) -> Any:
        from app.services.scoring.service import ScoringService

        return ScoringService(self.db).score_company(
            analysis, forecast_service, valuation_service,
        )

    def _peers(self, company: Company) -> list[Company]:
        if not company.sector:
            return []
        return list(self.db.scalars(
            select(Company)
            .where(Company.sector == company.sector)
            .order_by(Company.market_cap.desc().nullslast())
            .limit(12)
        ).all())

    def _documents(self, company_id: str) -> list[Any]:
        from app.models.document import DocumentEntity

        return list(self.db.scalars(
            select(DocumentEntity)
            .where(DocumentEntity.company_id == company_id)
            .order_by(DocumentEntity.confidence.desc())
            .limit(60)
        ).all())

    def _portfolio(self, portfolio_id: int) -> Any:
        from app.services.portfolio.service import PortfolioService

        return PortfolioService(self.db).view(portfolio_id)

    def _narratives(self, analysis, report_type: ReportType) -> dict[str, Any]:
        """Run the AI capabilities this report type asks for.

        Concurrently: each is an independent model round-trip, and a deep
        research report asks for thirteen. Sequential execution would make the
        AI layer the whole cost of the report.
        """
        from app.services.ai.service import AIService

        analyst = AIService(self.db).analyst_for(analysis)
        capabilities = narratives_for(report_type)

        async def run_all() -> dict[str, Any]:
            async def one(capability: str):
                try:
                    return capability, await analyst.run(capability)
                except Exception as exc:  # pragma: no cover - resilience path
                    logger.warning("narrative '%s' failed: %s", capability, exc)
                    return capability, None

            results = await asyncio.gather(*(one(c) for c in capabilities))
            return {k: v for k, v in results if v is not None}

        return asyncio.run(run_all())

    # -------------------------------------------------------- persist
    def _persist(
        self, company: Company, report_type: ReportType,
        document: ReportDocument, audit, key: str, owner_id: str,
        analyst: str, portfolio_id: int | None, theme: Theme,
        timings: dict[str, float],
    ) -> Report:
        previous = self.db.scalar(
            select(Report)
            .where(
                Report.company_id == company.id,
                Report.report_type == report_type.value,
                Report.superseded_by.is_(None),
            )
            .order_by(Report.version.desc())
        )
        version = (previous.version + 1) if previous else 1

        statistics = document.statistics()
        report = Report(
            company_id=company.id, ticker=company.ticker,
            company_name=company.name, owner_id=owner_id,
            report_type=report_type.value, title=document.cover.title,
            theme=theme.value, version=version, status="building",
            analyst=analyst or None, portfolio_id=portfolio_id,
            section_count=statistics["sections"],
            insufficient_count=statistics["sections_insufficient"],
            block_count=statistics["blocks"],
            chart_count=statistics["charts"],
            table_count=statistics["tables"],
            evidence_count=statistics["evidence"],
            word_count=statistics["words"],
            citation_coverage=audit.coverage,
            citation_clean=audit.is_clean,
            audit=audit.summary(),
            input_hash=key,
            provenance=dict(document.provenance),
            document=document_to_dict(document),
            generated_at=document.generated_at or _utcnow(),
        )
        self.db.add(report)
        self.db.flush()

        if previous is not None:
            # The prior version stays retrievable — a report sent to a
            # committee must still resolve months later.
            previous.superseded_by = report.id

        self.db.add(ReportJob(
            report_id=report.id, owner_id=owner_id, status="ready",
            stage="done", progress=1.0, attempts=1,
            started_at=_utcnow(), finished_at=_utcnow(),
            duration_ms=sum(v for k, v in timings.items() if k != "charts"),
            timings=timings,
        ))
        return report

    # ================================================================
    # Reads
    # ================================================================
    def get(self, report_id: int) -> Report | None:
        return self.db.get(Report, report_id)

    def list_reports(
        self, *, owner_id: str | None = None, company_id: str | None = None,
        report_type: ReportType | None = None,
        include_superseded: bool = True, limit: int = 100,
    ) -> list[Report]:
        query = select(Report)
        if owner_id is not None:
            query = query.where(Report.owner_id == owner_id)
        if company_id is not None:
            query = query.where(Report.company_id == company_id)
        if report_type is not None:
            query = query.where(Report.report_type == report_type.value)
        if not include_superseded:
            query = query.where(Report.superseded_by.is_(None))
        return list(self.db.scalars(
            query.order_by(Report.id.desc()).limit(limit)
        ).all())

    def artifact(
        self, report_id: int, fmt: OutputFormat
    ) -> ReportArtifact | None:
        return self.db.scalar(
            select(ReportArtifact).where(
                ReportArtifact.report_id == report_id,
                ReportArtifact.fmt == fmt.value,
            )
        )

    def render_additional(
        self, report_id: int, fmt: OutputFormat
    ) -> ReportArtifact:
        """Render a format that was not produced at generation time.

        Rebuilt from the stored block tree rather than by re-running the
        engines: the report's content is fixed at the version it was generated
        at, and re-gathering could quietly produce different numbers under the
        same version number.
        """
        existing = self.artifact(report_id, fmt)
        if existing is not None:
            return existing

        report = self.db.get(Report, report_id)
        if report is None:
            raise ReportError(f"unknown report {report_id}")
        if not report.document:
            raise ReportError(
                "this report has no stored content and cannot be re-rendered"
            )

        from app.services.reports.serialise import document_from_dict

        document = document_from_dict(report.document)
        rendered = renderer_for(fmt, ChartEngine(document.theme)).render(document)
        artifact = ReportArtifact(
            report_id=report.id, fmt=fmt.value, filename=rendered.filename,
            media_type=fmt.media_type, payload=rendered.payload,
            size_bytes=rendered.size_bytes, page_count=rendered.page_count,
            render_ms=rendered.took_ms,
        )
        self.db.add(artifact)
        self.db.commit()
        return artifact

    def delete(self, report_id: int) -> None:
        report = self.db.get(Report, report_id)
        if report is None:
            raise ReportError(f"unknown report {report_id}")
        # Re-point any successor so deleting a middle version does not orphan
        # the chain.
        for successor in self.db.scalars(
            select(Report).where(Report.superseded_by == report_id)
        ).all():
            successor.superseded_by = report.superseded_by
        self.db.delete(report)
        self.db.commit()

    def versions(self, company_id: str, report_type: ReportType) -> list[Report]:
        return list(self.db.scalars(
            select(Report)
            .where(
                Report.company_id == company_id,
                Report.report_type == report_type.value,
            )
            .order_by(Report.version.desc())
        ).all())

    def jobs(self, owner_id: str | None = None, limit: int = 50) -> list[ReportJob]:
        query = select(ReportJob).order_by(ReportJob.id.desc()).limit(limit)
        if owner_id is not None:
            query = query.where(ReportJob.owner_id == owner_id)
        return list(self.db.scalars(query).all())

    def statistics(self, owner_id: str | None = None) -> dict[str, Any]:
        query = select(Report)
        if owner_id is not None:
            query = query.where(Report.owner_id == owner_id)
        reports = list(self.db.scalars(query).all())
        current = [r for r in reports if r.superseded_by is None]

        by_type: dict[str, int] = {}
        for report in reports:
            by_type[report.report_type] = by_type.get(report.report_type, 0) + 1

        artifacts = list(self.db.scalars(select(ReportArtifact)).all())
        by_format: dict[str, int] = {}
        for artifact in artifacts:
            by_format[artifact.fmt] = by_format.get(artifact.fmt, 0) + 1

        clean = [r for r in reports if r.citation_clean]
        return {
            "reports": len(reports),
            "current": len(current),
            "superseded": len(reports) - len(current),
            "artifacts": len(artifacts),
            "bytes_stored": sum(a.size_bytes for a in artifacts),
            "by_type": dict(sorted(by_type.items())),
            "by_format": dict(sorted(by_format.items())),
            "citation_clean": len(clean),
            "mean_coverage": round(
                sum(r.citation_coverage for r in reports) / len(reports), 4
            ) if reports else 0.0,
            "mean_build_ms": round(
                sum(r.build_ms for r in reports) / len(reports), 2
            ) if reports else 0.0,
        }
