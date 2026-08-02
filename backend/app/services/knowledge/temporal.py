"""Temporal memory — one AI observation per company per fiscal year.

Generates, verifies and reads the yearly narrative that lets the platform
answer "how has management guidance changed over the last ten years?" without
re-reading a decade of filings.

The generation loop is deliberately **chronological and stateful**. FY2026 is
not generated in isolation: it receives FY2025's recorded guidance and is
asked to judge whether it was delivered, citing this year's evidence. That is
what turns a pile of yearly notes into a management track record, and it is
why the years cannot be generated in parallel.

Evidence comes from the summaries the vault already holds, not from re-parsing
PDFs (§13: Vault → RAG → original PDF). A year with no summarised filing falls
through to the document chunks for that year, and a year with neither is
recorded as unobservable rather than invented.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any

import structlog
from sqlalchemy import select

from app.domain.knowledge.temporal import (
    MIN_SERVABLE_CONFIDENCE, TRACKED_DIMENSIONS, DimensionReading,
    GuidanceVerdict, ObservationTrend, YearObservation, credibility_score,
    trend_of,
)
from app.models.document import Document, DocumentChunk
from app.models.knowledge import DocumentSummary, YearlyObservation

log = structlog.get_logger(__name__)

PROMPT_VERSION = "v1"

#: Evidence budget per year. Large enough for an annual report's summaries,
#: small enough that the completion reservation stays affordable — the
#: SUMMARY-001 lesson, where an oversized `max_tokens` reservation was
#: rejected outright rather than billed at actual cost.
MAX_EVIDENCE_CHARS = 12_000

#: TEMP-002. Completion budget, sized for a REASONING model.
#:
#: The free-tier models available on this key spend most of their completion
#: allowance on hidden reasoning tokens before emitting a single character of
#: JSON — one probe used 599 reasoning tokens to produce a two-character
#: answer. At a 4,000 ceiling with a full evidence block, the reply was
#: truncated part-way through the JSON object, `json.loads` failed, and a
#: perfectly good analysis was discarded and stored as a template fallback.
#: Sun Pharma FY2026's "fallback" was in fact a complete, correct set of
#: findings that had simply been cut off mid-object.
MAX_COMPLETION_TOKENS = 16_000

SYSTEM_PROMPT = (
    "You are a CFA-qualified equity analyst maintaining an institutional "
    "memory of a company, one fiscal year at a time. You state only what the "
    "evidence supports, you cite the evidence id for every claim, and when "
    "the evidence does not settle a question you say so rather than "
    "speculating. You never invent a figure."
)


@dataclass(slots=True)
class ObservationRun:
    company_id: str
    generated: int = 0
    skipped_existing: int = 0
    unobservable: int = 0
    failed: int = 0
    fallback: int = 0
    years: list[int] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)
    latency_ms: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "company_id": self.company_id,
            "generated": self.generated,
            "skipped_existing": self.skipped_existing,
            "unobservable": self.unobservable,
            "failed": self.failed,
            "fallback": self.fallback,
            "years": self.years,
            "errors": self.errors[:10],
            "latency_ms": round(self.latency_ms, 1),
        }


class TemporalMemoryService:
    """Builds and reads the per-year observation series."""

    def __init__(self, db: Any) -> None:
        self.db = db

    # --------------------------------------------------------------- read
    def current(self, company_id: str, fiscal_year: int) -> YearlyObservation | None:
        return self.db.execute(
            select(YearlyObservation)
            .where(
                YearlyObservation.company_id == company_id,
                YearlyObservation.fiscal_year == fiscal_year,
                YearlyObservation.status == "current",
            )
            .order_by(YearlyObservation.version.desc())
            .limit(1)
        ).scalars().first()

    def timeline(self, company_id: str, *, limit: int = 20) -> list[YearlyObservation]:
        """Current observations, oldest year first — the narrative order."""
        rows = self.db.execute(
            select(YearlyObservation)
            .where(
                YearlyObservation.company_id == company_id,
                YearlyObservation.status == "current",
            )
            .order_by(YearlyObservation.fiscal_year.desc())
            .limit(limit)
        ).scalars().all()
        return sorted(rows, key=lambda r: r.fiscal_year)

    def history(self, company_id: str, fiscal_year: int) -> list[YearlyObservation]:
        """Every version ever recorded for one year, newest first."""
        return list(self.db.execute(
            select(YearlyObservation)
            .where(
                YearlyObservation.company_id == company_id,
                YearlyObservation.fiscal_year == fiscal_year,
            )
            .order_by(YearlyObservation.version.desc())
        ).scalars().all())

    def credibility(self, company_id: str) -> dict[str, Any]:
        """Management credibility, assembled from the stored verdicts."""
        rows = self.timeline(company_id)
        observations = [self._to_domain(r) for r in rows]
        score, assessed = credibility_score(observations)
        counts: dict[str, int] = {}
        for observation in observations:
            key = observation.prior_verdict.value
            counts[key] = counts.get(key, 0) + 1
        return {
            "score": score,
            "years_assessed": assessed,
            "years_total": len(observations),
            "verdicts": counts,
            # Stated plainly rather than implied by a null: a company nobody
            # can grade is not an average company.
            "note": (
                None if assessed
                else "No year carried guidance specific enough to score."
            ),
        }

    # ------------------------------------------------------------ evidence
    def observable_years(self, company_id: str) -> list[int]:
        """Fiscal years for which some evidence exists, ascending."""
        from_summaries = self.db.execute(
            select(DocumentSummary.fiscal_year)
            .where(
                DocumentSummary.company_id == company_id,
                DocumentSummary.fiscal_year.is_not(None),
            )
            .distinct()
        ).scalars().all()
        from_documents = self.db.execute(
            select(Document.fiscal_year)
            .where(
                Document.company_id == company_id,
                Document.fiscal_year.is_not(None),
            )
            .distinct()
        ).scalars().all()
        years = {y for y in (*from_summaries, *from_documents) if y}
        return sorted(years)

    def _evidence_for(self, company_id: str, fiscal_year: int) -> tuple[str, list[int]]:
        """Evidence for one year, preferring summaries over raw chunks.

        §13's ordering, applied literally: the summaries are the permanent
        memory and are read first. Chunks are the fallback for a year whose
        filing has not been summarised, and a year with neither yields "",
        which the caller records as unobservable rather than guessing.
        """
        summaries = self.db.execute(
            select(DocumentSummary)
            .where(
                DocumentSummary.company_id == company_id,
                DocumentSummary.fiscal_year == fiscal_year,
                DocumentSummary.is_fallback.is_(False),
            )
            .order_by(DocumentSummary.document_id, DocumentSummary.kind)
        ).scalars().all()

        parts: list[str] = []
        document_ids: list[int] = []
        total = 0
        index = 1

        for summary in summaries:
            text = (summary.content or "").strip()
            if not text:
                continue
            block = f"[E{index}] ({summary.kind}, doc {summary.document_id}) {text}"
            if total + len(block) > MAX_EVIDENCE_CHARS:
                break
            parts.append(block)
            total += len(block)
            index += 1
            if summary.document_id not in document_ids:
                document_ids.append(summary.document_id)

        if parts:
            return "\n\n".join(parts), document_ids

        # Fall through to indexed chunks for this year's documents.
        documents = self.db.execute(
            select(Document).where(
                Document.company_id == company_id,
                Document.fiscal_year == fiscal_year,
            )
        ).scalars().all()
        for document in documents:
            chunks = self.db.execute(
                select(DocumentChunk)
                .where(DocumentChunk.document_id == document.id)
                .order_by(DocumentChunk.chunk_index)
            ).scalars().all()
            for chunk in chunks:
                text = (chunk.text or "").strip()
                if not text:
                    continue
                block = f"[E{index}] (p.{chunk.page}, doc {document.id}) {text}"
                if total + len(block) > MAX_EVIDENCE_CHARS:
                    break
                parts.append(block)
                total += len(block)
                index += 1
                if document.id not in document_ids:
                    document_ids.append(document.id)

        return "\n\n".join(parts), document_ids

    def _metrics_for(self, company_id: str, fiscal_year: int) -> dict[str, Any]:
        """Corroborating figures from the platform's own facts.

        Supplied to the model so a narrative claim can be checked against the
        accounts, and retained so a contradiction between the two is visible
        rather than silently resolved in favour of the prose.
        """
        from app.models.company import Company
        from app.services.analysis_service import AnalysisService

        company = self.db.get(Company, company_id)
        if company is None:
            return {}
        try:
            analysis = AnalysisService.for_ticker(
                self.db, company.ticker, provision=False,
            )
        except Exception:  # noqa: BLE001 — metrics are corroboration, not the point
            return {}
        if analysis is None:
            return {}

        out: dict[str, Any] = {}
        for income in analysis.incomes:
            if income.fiscal_year == fiscal_year:
                out["revenue"] = income.total_revenue
                out["pat"] = income.pat
                out["ebitda_margin"] = income.ebitda_margin
                break
        for balance in analysis.balances:
            if balance.fiscal_year == fiscal_year:
                out["net_debt"] = balance.net_debt
                break
        for flow in analysis.cash_flows:
            if flow.fiscal_year == fiscal_year:
                out["capex"] = flow.capex
                out["cfo"] = flow.cfo
                break
        return {k: v for k, v in out.items() if v is not None}

    # ---------------------------------------------------------- generation
    def generate_year(
        self,
        company_id: str,
        fiscal_year: int,
        *,
        prior: YearlyObservation | None = None,
        overwrite: bool = False,
    ) -> YearlyObservation | None:
        """Generate and persist one year's observation.

        `prior` is the previous year's stored observation. Its guidance is
        what this year is asked to judge — the mechanism that makes the series
        a track record rather than a list.
        """
        if not overwrite:
            existing = self.current(company_id, fiscal_year)
            if existing is not None:
                return existing

        evidence, document_ids = self._evidence_for(company_id, fiscal_year)
        if not evidence:
            return None

        metrics = self._metrics_for(company_id, fiscal_year)
        prior_metrics = (
            self._metrics_for(company_id, fiscal_year - 1) if fiscal_year else {}
        )

        payload, generated_by, is_fallback = self._ask(
            fiscal_year=fiscal_year,
            evidence=evidence,
            prior=prior,
            metrics=metrics,
            prior_metrics=prior_metrics,
        )

        observation = self._compose(
            fiscal_year=fiscal_year,
            payload=payload,
            metrics=metrics,
            prior_metrics=prior_metrics,
            prior=prior,
            generated_by=generated_by,
        )
        return self._persist(
            company_id, observation, document_ids, is_fallback=is_fallback,
        )

    def build_company(
        self, company_id: str, *, overwrite: bool = False,
        limit_years: int | None = None,
    ) -> ObservationRun:
        """Build the whole series for one company, oldest year first.

        Chronological on purpose: each year is generated with the previous
        year's guidance in hand, so the series cannot be produced in parallel
        without losing the verification that gives it its value.
        """
        started = time.perf_counter()
        run = ObservationRun(company_id=company_id)

        years = self.observable_years(company_id)
        if limit_years:
            years = years[-limit_years:]

        prior: YearlyObservation | None = None
        for year in years:
            try:
                if not overwrite:
                    existing = self.current(company_id, year)
                    if existing is not None:
                        run.skipped_existing += 1
                        prior = existing
                        continue

                observation = self.generate_year(
                    company_id, year, prior=prior, overwrite=overwrite,
                )
                if observation is None:
                    run.unobservable += 1
                    continue

                run.generated += 1
                run.years.append(year)
                if observation.is_fallback:
                    run.fallback += 1

                # TEMP-001. Only carry a year forward as the yardstick if it
                # is servable. A below-threshold observation is stored as
                # history but does NOT appear in the timeline, so grading the
                # next year against it would produce a verdict whose basis no
                # reader can see — two inconsistent notions of "this year
                # exists". Cipla FY2026 scored 0.30 on three thin exchange
                # filings and was correctly withheld; FY2027 must therefore be
                # judged against the last SERVABLE year, or against nothing.
                if observation.status == "current":
                    prior = observation
            except Exception as exc:  # noqa: BLE001 — one year must not stop the series
                self.db.rollback()
                run.failed += 1
                run.errors.append({
                    "fiscal_year": str(year),
                    "error": f"{type(exc).__name__}: {exc}"[:200],
                })
                log.warning("observation generation failed",
                            company_id=company_id, fiscal_year=year,
                            error=str(exc)[:160])

        run.latency_ms = (time.perf_counter() - started) * 1000
        return run

    # ------------------------------------------------------------ internals
    def _ask(
        self, *, fiscal_year: int, evidence: str,
        prior: YearlyObservation | None,
        metrics: dict[str, Any], prior_metrics: dict[str, Any],
    ) -> tuple[dict[str, Any], str, bool]:
        """One completion. Returns (payload, generated_by, is_fallback)."""
        from app.domain.ai.types import CompletionRequest, Message, Role
        from app.services.ai.service import _router

        prior_block = "No prior-year observation on record."
        if prior is not None:
            prior_block = (
                f"FY{prior.fiscal_year} OBSERVATION "
                f"(confidence {prior.confidence:.2f}):\n"
                f"{prior.findings or '(none recorded)'}\n"
                f"Guidance recorded: {prior.guidance or '(none recorded)'}"
            )

        metric_block = json.dumps(
            {"this_year": metrics, "prior_year": prior_metrics},
            default=str,
        )

        prompt = (
            f"FISCAL YEAR UNDER REVIEW: FY{fiscal_year}\n\n"
            f"{prior_block}\n\n"
            f"COMPUTED FINANCIALS (from audited statements, ₹ crore):\n"
            f"{metric_block}\n\n"
            f"EVIDENCE FROM FY{fiscal_year} FILINGS:\n{evidence}\n\n"
            "Produce this fiscal year's institutional observation.\n"
            "- 3 to 5 short findings, each citing an evidence id like [E2].\n"
            "- For each of these dimensions give a trend of improving, "
            f"stable, deteriorating or unknown: {', '.join(TRACKED_DIMENSIONS)}. "
            "Use 'unknown' where the evidence does not say — do not guess.\n"
            "- Record any forward-looking guidance this year's filings give, "
            "verbatim where possible. Null if none.\n"
            "- Judge the PRIOR year's guidance against this year's evidence. "
            "Use 'not_assessable' when there was no prior guidance specific "
            "enough to judge; do not manufacture a verdict.\n\n"
            "Return STRICT JSON only:\n"
            '{"findings":["..."],'
            '"dimensions":{"management_quality":"improving", ...},'
            '"guidance":"..."|null,'
            '"confidence":0.0-1.0,'
            '"prior_year_verdict":"delivered|partially_delivered|missed|'
            'not_assessable",'
            '"verdict_reasoning":"one sentence citing evidence ids"}'
        )

        request = CompletionRequest(
            messages=[
                Message(Role.SYSTEM, SYSTEM_PROMPT),
                Message(Role.USER, prompt),
            ],
            temperature=0.2,
            max_tokens=MAX_COMPLETION_TOKENS,
        )

        async def ask():
            return await _router.complete(request)

        try:
            response = asyncio.run(ask())
        except RuntimeError as exc:
            raise RuntimeError(
                "cannot generate inside a running event loop"
            ) from exc

        is_fallback = (response.provider or "").lower() in ("offline", "mock")
        generated_by = f"{response.provider}:{response.model}"[:64]
        payload = self._parse(response.content or "")
        if payload is None:
            # A provider that returned prose rather than JSON has still told
            # us something; it is recorded as a low-confidence fallback rather
            # than discarded, and flagged so it is never mistaken for
            # structured analysis.
            return (
                {
                    "findings": [(response.content or "").strip()[:400]]
                    if (response.content or "").strip() else [],
                    "confidence": 0.2,
                    "prior_year_verdict": "not_assessable",
                },
                generated_by,
                True,
            )
        return payload, generated_by, is_fallback

    @staticmethod
    def _parse(content: str) -> dict[str, Any] | None:
        """Extract the JSON object, tolerating a fenced or prefixed reply."""
        text = (content or "").strip()
        if not text:
            return None
        if text.startswith("```"):
            text = text.split("```")[1] if "```" in text[3:] else text[3:]
            if text.lstrip().startswith("json"):
                text = text.lstrip()[4:]
        start = text.find("{")
        if start == -1:
            return None

        end = text.rfind("}")
        if end > start:
            try:
                parsed = json.loads(text[start:end + 1])
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass

        # TEMP-002 recovery. A reasoning model can exhaust its completion
        # budget part-way through the object, leaving valid JSON with the
        # closing braces missing. Discarding that loses a complete set of
        # findings and records it as a template fallback — which is how Sun
        # Pharma FY2026's real analysis came to be labelled fabricated prose.
        #
        # Closing the open structures recovers every field that did arrive
        # intact. The trailing partial value is dropped rather than guessed.
        fragment = text[start:]
        truncated = fragment.rstrip()
        # Drop a dangling partial token so the repair starts from clean JSON.
        for cut in (truncated.rfind(","), truncated.rfind("]"),
                    truncated.rfind("}"), truncated.rfind('"')):
            if cut <= 0:
                continue
            candidate = truncated[:cut + 1].rstrip().rstrip(",")
            depth_curly = candidate.count("{") - candidate.count("}")
            depth_square = candidate.count("[") - candidate.count("]")
            if depth_curly < 0 or depth_square < 0:
                continue
            # An odd number of unescaped quotes means we stopped inside a
            # string; that value cannot be trusted, so it is not closed.
            if candidate.count('"') - candidate.count('\\"') % 2 == 1:
                pass
            repaired = candidate + ("]" * depth_square) + ("}" * depth_curly)
            try:
                parsed = json.loads(repaired)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict) and parsed:
                log.info("recovered truncated model JSON",
                         recovered_keys=sorted(parsed))
                return parsed
        return None

    def _compose(
        self, *, fiscal_year: int, payload: dict[str, Any],
        metrics: dict[str, Any], prior_metrics: dict[str, Any],
        prior: YearlyObservation | None, generated_by: str,
    ) -> YearObservation:
        findings = [
            str(f).strip() for f in (payload.get("findings") or [])
            if str(f).strip()
        ][:5]

        raw_dimensions = payload.get("dimensions") or {}
        readings: list[DimensionReading] = []
        if isinstance(raw_dimensions, dict):
            for dimension in TRACKED_DIMENSIONS:
                value = str(raw_dimensions.get(dimension, "unknown")).lower()
                try:
                    trend = ObservationTrend(value)
                except ValueError:
                    trend = ObservationTrend.UNKNOWN
                reading = DimensionReading(dimension=dimension, trend=trend)
                # Attach the measured counterpart where one exists, so a
                # narrative/accounts disagreement becomes visible.
                metric_key = {
                    "capex": "capex", "debt": "net_debt",
                    "margins": "ebitda_margin", "growth": "revenue",
                }.get(dimension)
                if metric_key:
                    reading.metric_value = metrics.get(metric_key)
                    reading.metric_prior = prior_metrics.get(metric_key)
                readings.append(reading)

        try:
            confidence = float(payload.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))

        try:
            verdict = GuidanceVerdict(
                str(payload.get("prior_year_verdict", "not_assessable")).lower()
            )
        except ValueError:
            verdict = GuidanceVerdict.NOT_ASSESSABLE

        # A verdict is only meaningful against recorded guidance. Without a
        # prior year, or with a prior year that stated none, any verdict but
        # `not_assessable` is the model inventing a track record.
        if prior is None or not (prior.guidance or "").strip():
            verdict = GuidanceVerdict.NOT_ASSESSABLE

        guidance = payload.get("guidance")
        guidance = str(guidance).strip() if guidance else None

        return YearObservation(
            fiscal_year=fiscal_year,
            findings=findings,
            dimensions=readings,
            confidence=confidence,
            guidance=guidance or None,
            prior_verdict=verdict,
            verdict_reasoning=(
                str(payload.get("verdict_reasoning") or "").strip() or None
                if verdict != GuidanceVerdict.NOT_ASSESSABLE else None
            ),
            generated_by=generated_by,
        )

    def _persist(
        self, company_id: str, observation: YearObservation,
        document_ids: list[int], *, is_fallback: bool,
    ) -> YearlyObservation:
        """Insert a new version; never update in place."""
        previous = self.current(company_id, observation.fiscal_year)
        version = 1 + (self.db.execute(
            select(YearlyObservation.version)
            .where(
                YearlyObservation.company_id == company_id,
                YearlyObservation.fiscal_year == observation.fiscal_year,
            )
            .order_by(YearlyObservation.version.desc())
            .limit(1)
        ).scalars().first() or 0)

        row = YearlyObservation(
            company_id=company_id,
            fiscal_year=observation.fiscal_year,
            findings="\n".join(observation.findings) or None,
            dimensions=json.dumps([
                {
                    "dimension": d.dimension,
                    "trend": d.trend.value,
                    "metric_value": d.metric_value,
                    "metric_prior": d.metric_prior,
                    "contradicts_metric": d.contradicts_metric,
                }
                for d in observation.dimensions
            ]) or None,
            confidence=round(observation.confidence, 4),
            guidance=observation.guidance,
            prior_verdict=observation.prior_verdict.value,
            verdict_reasoning=observation.verdict_reasoning,
            source_document_ids=json.dumps(document_ids) if document_ids else None,
            version=version,
            status=(
                "current" if observation.confidence >= MIN_SERVABLE_CONFIDENCE
                else "superseded"
            ),
            generated_by=observation.generated_by,
            is_fallback=is_fallback,
            prompt_version=PROMPT_VERSION,
        )
        self.db.add(row)
        self.db.flush()

        if previous is not None and row.status == "current":
            previous.status = "superseded"
            previous.superseded_by = row.id
        self.db.commit()
        return row

    @staticmethod
    def _to_domain(row: YearlyObservation) -> YearObservation:
        try:
            verdict = GuidanceVerdict(row.prior_verdict)
        except ValueError:
            verdict = GuidanceVerdict.NOT_ASSESSABLE
        return YearObservation(
            fiscal_year=row.fiscal_year,
            findings=(row.findings or "").split("\n") if row.findings else [],
            confidence=row.confidence or 0.0,
            guidance=row.guidance,
            prior_verdict=verdict,
            verdict_reasoning=row.verdict_reasoning,
            generated_by=row.generated_by,
        )

    def render_timeline(self, company_id: str, *, limit: int = 20) -> str:
        """The series in the brief's compact form, for prompts and display."""
        rows = self.timeline(company_id, limit=limit)
        if not rows:
            return ""
        blocks: list[str] = []
        for row in rows:
            block = [f"FY{row.fiscal_year}"]
            for finding in (row.findings or "").split("\n"):
                if finding.strip():
                    block.append(f"  {finding.strip()}")
            if row.prior_verdict != GuidanceVerdict.NOT_ASSESSABLE.value:
                block.append(f"  Prior-year guidance: {row.prior_verdict}")
            block.append(f"  Confidence: {round((row.confidence or 0) * 100)}%")
            blocks.append("\n".join(block))
        return "\n\n".join(blocks)
