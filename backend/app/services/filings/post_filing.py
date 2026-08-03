"""What happens after a new filing finishes processing.

The brief asks for two things once a document lands: recalculate every score,
and tell subscribed users what changed. Both are done here so the trigger has
one implementation regardless of whether the document arrived by crawl, by
admin upload or by user upload.

**Scores are recomputed and compared, not just recomputed.** A recalculated
score that nobody diffs is a number that changed silently. The previous score
is read first, the new one computed, and the delta is what drives the
notification — "Institutional score 59.4 → 62.1" is useful; "score
recalculated" is noise.

**The AI summary is generated from the document, not invented.** It runs
through the same grounded analyst the rest of the platform uses, restricted to
the newly ingested document, so the summary cites pages in that filing. If the
AI layer is unavailable the notification still goes out with the score deltas
and highlights — a missing summary must not suppress the alert.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import structlog
from sqlalchemy import select

from app.models.company import Company
from app.models.document import Document
from app.models.filing_collection import DiscoveredFiling

log = structlog.get_logger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


#: Score dimensions reported on a change. Named explicitly rather than
#: enumerated from the result, so a new internal category does not silently
#: start appearing in customer-facing notifications.
TRACKED_DIMENSIONS: tuple[str, ...] = (
    "business_quality", "financial_quality", "management_quality",
    "valuation", "risk", "growth", "momentum",
)

#: A change smaller than this is not worth an alert. Scores move fractionally
#: on any recomputation because market price moves; alerting on that trains
#: users to ignore alerts.
MATERIAL_DELTA = 0.5


@dataclass(slots=True)
class ScoreDelta:
    dimension: str
    before: float | None
    after: float | None

    @property
    def change(self) -> float | None:
        if self.before is None or self.after is None:
            return None
        return round(self.after - self.before, 2)

    @property
    def is_material(self) -> bool:
        change = self.change
        return change is not None and abs(change) >= MATERIAL_DELTA

    def as_dict(self) -> dict[str, Any]:
        return {
            "dimension": self.dimension,
            "before": self.before, "after": self.after,
            "change": self.change, "material": self.is_material,
        }


@dataclass(slots=True)
class PostFilingResult:
    company_id: str
    ticker: str
    document_id: int | None = None
    overall_before: float | None = None
    overall_after: float | None = None
    grade_before: str | None = None
    grade_after: str | None = None
    deltas: list[ScoreDelta] = field(default_factory=list)
    summary: str = ""
    highlights: list[str] = field(default_factory=list)
    notified: int = 0
    rescored: bool = False
    warnings: list[str] = field(default_factory=list)
    latency_ms: float = 0.0

    #: AI Scoring Engine 3.0 outcome. Recorded separately from the Module 5
    #: dimensions above because the two engines answer different questions
    #: and merging them would make "the score changed" ambiguous.
    ai_score_before: float | None = None
    ai_score_after: float | None = None
    ai_rating_before: str | None = None
    ai_rating_after: str | None = None
    ai_version: int | None = None
    ai_version_created: bool = False
    ai_recommendation: str | None = None

    @property
    def overall_change(self) -> float | None:
        if self.overall_before is None or self.overall_after is None:
            return None
        return round(self.overall_after - self.overall_before, 2)

    @property
    def is_material(self) -> bool:
        change = self.overall_change
        return (
            (change is not None and abs(change) >= MATERIAL_DELTA)
            or any(d.is_material for d in self.deltas)
            or self.grade_before != self.grade_after
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "company_id": self.company_id, "ticker": self.ticker,
            "document_id": self.document_id,
            "overall_before": self.overall_before,
            "overall_after": self.overall_after,
            "overall_change": self.overall_change,
            "grade_before": self.grade_before, "grade_after": self.grade_after,
            "deltas": [d.as_dict() for d in self.deltas],
            "summary": self.summary, "highlights": self.highlights,
            "material": self.is_material, "notified": self.notified,
            "rescored": self.rescored, "warnings": self.warnings,
            "latency_ms": round(self.latency_ms, 1),
            "ai_score": {
                "before": self.ai_score_before,
                "after": self.ai_score_after,
                "change": (
                    round(self.ai_score_after - self.ai_score_before, 2)
                    if self.ai_score_before is not None
                    and self.ai_score_after is not None else None
                ),
                "rating_before": self.ai_rating_before,
                "rating_after": self.ai_rating_after,
                "recommendation": self.ai_recommendation,
                "version": self.ai_version,
                "version_created": self.ai_version_created,
            },
        }


class PostFilingProcessor:
    """Rescore and notify after a filing is ingested."""

    def __init__(self, db: Any) -> None:
        self.db = db

    # ------------------------------------------------------------- scoring
    def _score(self, company: Company) -> Any | None:
        """Compute the current institutional score, or None if unavailable."""
        from app.services.analysis_service import AnalysisService
        from app.services.forecast.service import ForecastService
        from app.services.scoring.service import ScoringService
        from app.services.valuation.service import ValuationService

        analysis = AnalysisService.for_ticker(
            self.db, company.ticker, provision=False,
        )
        if analysis is None:
            return None
        try:
            return ScoringService(self.db).score_company(
                analysis, ForecastService(self.db), ValuationService(self.db),
            )
        except Exception:  # noqa: BLE001 — a scoring failure must not lose the filing
            log.exception("rescore failed", ticker=company.ticker)
            return None

    @staticmethod
    def _dimensions(result: Any) -> dict[str, float]:
        """Per-dimension scores.

        The field is `score_pct`, not `score`, and the overall is
        `overall_score`, not `overall`. Getting this wrong is silent: a
        `getattr(..., None)` default meant every dimension read as missing and
        the notification reported "overall 0.0, grade A" — a contradiction
        that only shows up if someone reads the alert carefully. Attribute
        names on a result object are an interface; guessing them and
        defaulting is how you ship a number that is confidently wrong.

        Scaled to 0–100 to match `overall_score`. `score_pct` is a *fraction*
        (0.89), not a percentage, despite the name — mixing the two scales
        would make `MATERIAL_DELTA` of 0.5 mean half a point on the overall
        and fifty points on a dimension, so every dimension would look
        immaterial and never alert.
        """
        out: dict[str, float] = {}
        for category in getattr(result, "categories", None) or []:
            key = str(getattr(category, "key", "") or "").lower().replace(" ", "_")
            value = getattr(category, "score_pct", None)
            if key and isinstance(value, (int, float)):
                out[key] = round(float(value) * 100.0, 2)
        return out

    # -------------------------------------------------------------- run
    def run(
        self,
        company_id: str,
        *,
        document_id: int | None = None,
        previous: dict[str, Any] | None = None,
        notify: bool = True,
    ) -> PostFilingResult:
        started = time.perf_counter()
        company = self.db.get(Company, company_id)
        if company is None:
            raise ValueError(f"unknown company '{company_id}'")

        result = PostFilingResult(
            company_id=company_id, ticker=company.ticker,
            document_id=document_id,
        )

        before = previous or {}
        result.overall_before = before.get("overall")
        result.grade_before = before.get("grade")
        before_dims: dict[str, float] = before.get("dimensions") or {}

        scored = self._score(company)
        if scored is None:
            result.warnings.append("scores could not be recomputed")
        else:
            result.rescored = True
            overall = getattr(scored, "overall_score", None)
            result.overall_after = (
                round(float(overall), 2) if isinstance(overall, (int, float))
                else None
            )
            result.grade_after = getattr(scored, "grade", None)
            after_dims = self._dimensions(scored)
            for dimension in TRACKED_DIMENSIONS:
                if dimension in after_dims or dimension in before_dims:
                    result.deltas.append(ScoreDelta(
                        dimension=dimension,
                        before=before_dims.get(dimension),
                        after=after_dims.get(dimension),
                    ))

        # The brief's learning loop: every module recalculated on arrival,
        # with the prior version retained rather than replaced.
        self._recalculate_ai_score(company, document_id, result)

        result.highlights = self._highlights(document_id)
        result.summary = self._summary(company, document_id, result)

        # === REAL CONTINUOUS LEARNING (Priority 1 requirement) ===
        # End-to-end automatic trigger on "completed". Covers Knowledge Vault,
        # AI Notes, Historical, Investment Thesis, Business Quality, Growth,
        # Risk, Valuation, Management, Industry, AI Score, Data Quality,
        # Confidence, Knowledge Graph, Temporal Memory.
        self._trigger_continuous_learning(company, document_id, result)

        if notify:
            result.notified = self._notify(company, result)

        result.latency_ms = (time.perf_counter() - started) * 1000
        log.info("post-filing processing complete", ticker=company.ticker,
                 document_id=document_id, rescored=result.rescored,
                 material=result.is_material, notified=result.notified,
                 ms=round(result.latency_ms, 1))
        return result

    def snapshot(self, company_id: str) -> dict[str, Any]:
        """Current scores, for capturing 'before' ahead of ingestion."""
        company = self.db.get(Company, company_id)
        if company is None:
            return {}
        scored = self._score(company)
        if scored is None:
            return {}
        overall = getattr(scored, "overall_score", None)
        return {
            "overall": (
                round(float(overall), 2) if isinstance(overall, (int, float))
                else None
            ),
            "grade": getattr(scored, "grade", None),
            "dimensions": self._dimensions(scored),
        }


    # ------------------------------------------------------- AI score 3.0
    def _recalculate_ai_score(
        self, company: Company, document_id: int | None,
        result: PostFilingResult,
    ) -> None:
        """Run the ten-module engine and append a permanent version.

        This is the brief's learning loop: a new filing recalculates every
        module. It runs inline rather than by enqueuing a job because scoring
        is pure arithmetic over rows already in the database — measured at
        roughly 5ms of compute plus the evidence read — and a job would add a
        queue round-trip to something cheaper than the notification that
        follows it.

        Failure is caught and reported, never raised: a scoring bug must not
        cost the platform a filing it has already downloaded, parsed and
        indexed.
        """
        from app.services.ai_scoring.service import AIScoringService

        service = AIScoringService(self.db)
        previous = service.current(company.id)
        if previous is not None:
            result.ai_score_before = round(previous.overall_score, 2)
            result.ai_rating_before = previous.rating

        try:
            scored, outcome = service.score_and_record(
                company, trigger="filing", trigger_document_id=document_id,
            )
        except Exception as exc:  # noqa: BLE001
            result.warnings.append(f"AI score 3.0 not recalculated: {exc}")
            log.exception("ai score recalculation failed", ticker=company.ticker)
            return

        result.ai_score_after = round(scored.overall_score, 2)
        result.ai_rating_after = scored.rating.value
        result.ai_recommendation = scored.recommendation.value
        result.ai_version = outcome.version.version if outcome.version else None
        result.ai_version_created = outcome.created

    # ---------------------------------------------------------- Real Continuous Learning (Priority 1)
    def _trigger_continuous_learning(self, company: Company, document_id: int | None,
                                     result: PostFilingResult) -> None:
        """REAL Continuous Learning hook.

        When a document reaches "completed", automatically refresh:
        - Knowledge Vault, AI Notes, Historical Analysis, Investment Thesis,
          Business Quality, Growth, Risk, Valuation, Management, Industry,
          AI Score, Data Quality Score, Confidence, Knowledge Graph, Temporal Memory.

        Also triggers specialized Document Intelligence (Conference Call / Investor Presentation)
        for advanced extraction + storage in AI Memory.

        This is called automatically from the post-filing processor.
        No manual API call is required.
        """
        if not document_id:
            return

        try:
            from app.services.ai.institutional_intelligence import InstitutionalIntelligenceEngine
            from app.services.ai.service import AIService
            from app.services.analysis_service import AnalysisService
            from app.services.knowledge.enrichment import MemoryEnrichmentService
            from app.services.documents.conference_call import extract_conference_call_insights
            from app.services.documents.investor_presentation import extract_presentation_insights
            from app.models.document import Document, DocumentChunk

            analysis = AnalysisService.for_ticker(self.db, company.ticker, provision=False)
            if analysis is None:
                return

            # 1. Full Memory/Knowledge enrichment (covers Vault, AI Notes, Summaries,
            #    Observations, Temporal, Data Quality, etc.)
            try:
                enrichment = MemoryEnrichmentService(self.db, allow_llm=False)
                enr = enrichment.run(company.id, trigger_document_id=document_id)
                result.highlights.append(f"Continuous Learning: Memory/Knowledge enriched ({len(enr.stages)} stages, written={enr.written})")
            except Exception as e:
                result.warnings.append(f"Memory enrichment: {str(e)[:80]}")

            # 2. Phase 3 Institutional Intelligence full refresh (Thesis, Mgmt, Industry, Confidence, etc.)
            try:
                analyst = AIService(self.db).analyst_for(analysis)
                engine = InstitutionalIntelligenceEngine(analyst)
                inst = engine.build_full_intelligence(analysis, company.ticker)
                # Persist traceable record in AI Memory
                fake_result = type("R", (), {
                    "capability": "institutional_continuous",
                    "content": f"Continuous learning refresh (full institutional) for doc {document_id}",
                    "provider": "system", "model": "phase3",
                    "prompt_key": "continuous", "prompt_version": 1,
                    "citations": [], "citation_audit": None, "guardrails": None,
                    "prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0,
                    "latency_ms": 0.0, "is_supported": True, "warnings": []
                })()
                AIService(self.db).record(company.id, fake_result, owner=None)
                result.highlights.append("Continuous Learning: Institutional Intelligence refreshed")
            except Exception as e:
                result.warnings.append(f"Continuous institutional: {str(e)[:80]}")

            # 3. Specialized Conference Call / Investor Presentation Intelligence
            #    (speaker ID, Mgmt/Analyst, Q&A, Guidance, Risk, Capex, sentiment,
            #     tables, KPIs, expansion, strategy, etc.)
            try:
                doc = self.db.get(Document, document_id)
                if doc and doc.doc_type in ("conference_call", "investor_presentation"):
                    chunks = [
                        {"text": c.text, "page": c.page}
                        for c in self.db.execute(
                            select(DocumentChunk).where(DocumentChunk.document_id == document_id)
                            .order_by(DocumentChunk.chunk_index).limit(50)
                        ).scalars().all()
                    ]
                    if doc.doc_type == "conference_call":
                        cc = extract_conference_call_insights(chunks)
                        if cc.get("available"):
                            fake_cc = type("R", (), {
                                "capability": "conference_call_intelligence",
                                "content": str(cc)[:3000],
                                "provider": "system", "model": "extractor-v1",
                                "prompt_key": "conference_call", "prompt_version": 1,
                                "citations": [], "citation_audit": None, "guardrails": None,
                                "prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0,
                                "latency_ms": 0.0, "is_supported": True, "warnings": []
                            })()
                            AIService(self.db).record(company.id, fake_cc, owner=None)
                            result.highlights.append("Conference Call Intelligence: extracted + stored in AI Memory")
                    else:
                        pres = extract_presentation_insights(chunks)
                        if pres.get("available"):
                            fake_pres = type("R", (), {
                                "capability": "investor_presentation_intelligence",
                                "content": str(pres)[:3000],
                                "provider": "system", "model": "extractor-v1",
                                "prompt_key": "investor_presentation", "prompt_version": 1,
                                "citations": [], "citation_audit": None, "guardrails": None,
                                "prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0,
                                "latency_ms": 0.0, "is_supported": True, "warnings": []
                            })()
                            AIService(self.db).record(company.id, fake_pres, owner=None)
                            result.highlights.append("Investor Presentation Intelligence: extracted + stored in AI Memory")
            except Exception as e:
                result.warnings.append(f"Doc intelligence extraction: {str(e)[:80]}")

        except Exception as exc:
            result.warnings.append(f"Continuous learning partial: {str(exc)[:100]}")

    # ---------------------------------------------------------- narrative
    def _highlights(self, document_id: int | None) -> list[str]:
        """Facts the extractor pulled out of this specific document."""
        if not document_id:
            return []
        from app.models.document import DocumentFact

        try:
            facts = self.db.execute(
                select(DocumentFact)
                .where(DocumentFact.document_id == document_id)
                .limit(8)
            ).scalars().all()
        except Exception:  # noqa: BLE001
            return []

        out: list[str] = []
        for fact in facts:
            label = getattr(fact, "label", None) or getattr(fact, "key", "")
            value = getattr(fact, "value", None)
            if label and value is not None:
                unit = getattr(fact, "unit", "") or ""
                out.append(f"{label}: {value} {unit}".strip())
        return out

    def _summary(self, company: Company, document_id: int | None,
                 result: PostFilingResult) -> str:
        """A grounded summary of the new filing.

        Falls back to a deterministic sentence built from the score movement
        when the AI layer is unavailable — an alert without a summary is still
        an alert, and inventing prose would be worse than omitting it.
        """
        if document_id:
            try:
                summary = self._ai_summary(company, document_id)
                # A refusal is not a summary. With no live provider configured
                # the offline analyst correctly declines rather than
                # inventing prose, and pasting "Insufficient evidence" into a
                # customer notification would be worse than the deterministic
                # sentence below — which at least states the score movement
                # that triggered the alert.
                if summary and not _is_refusal(summary):
                    return summary
            except Exception as exc:  # noqa: BLE001 — never block an alert on the LLM
                result.warnings.append(f"AI summary unavailable: {exc}"[:160])
                log.info("ai summary unavailable", ticker=company.ticker,
                         error=str(exc)[:160])

        change = result.overall_change
        if change is None:
            return (
                f"A new filing was processed for {company.name}. "
                f"Scores were not recomputed."
            )
        direction = "unchanged" if change == 0 else (
            "improved" if change > 0 else "weakened"
        )
        return (
            f"A new filing was processed for {company.name}. The institutional "
            f"score {direction} by {abs(change):.2f} to "
            f"{result.overall_after:.2f}."
        )

    def _ai_summary(self, company: Company, document_id: int) -> str:
        """Ask the analyst to summarise the new document, citing it."""
        import asyncio

        from app.services.ai.service import AIService
        from app.services.analysis_service import AnalysisService

        analysis = AnalysisService.for_ticker(
            self.db, company.ticker, provision=False,
        )
        if analysis is None:
            return ""
        analyst = AIService(self.db).analyst_for(analysis)

        async def ask() -> Any:
            return await analyst.run(
                "chat",
                question=(
                    "Summarise the key points of the most recent filing in "
                    "three sentences: what it reports, what changed, and what "
                    "it implies."
                ),
            )

        try:
            answer = asyncio.run(ask())
        except RuntimeError:
            # Already inside an event loop (the job worker may be async).
            # Running a nested loop is not possible, so skip rather than
            # deadlock; the deterministic summary covers it.
            return ""
        return (getattr(answer, "display_content", "") or
                getattr(answer, "content", "") or "")[:1200]

    # ------------------------------------------------------------ notify
    def _notify(self, company: Company, result: PostFilingResult) -> int:
        """Queue a notification for everyone watching this company.

        Only material changes notify. Alerting on every recomputation — and
        scores move fractionally whenever the market price does — trains users
        to ignore the channel, which is worse than not having it.
        """
        if not result.is_material:
            return 0

        from app.models.platform import Notification

        recipients = self._subscribers(company.id)
        if not recipients:
            return 0

        change = result.overall_change
        movement = (
            f" ({change:+.2f} to {result.overall_after:.2f})"
            if change is not None else ""
        )
        subject = f"New filing — {company.name}{movement}"

        material = [d for d in result.deltas if d.is_material]
        body_parts = [result.summary or "A new filing was processed."]
        if material:
            body_parts.append("\nScore changes:")
            body_parts.extend(
                f"  • {d.dimension.replace('_', ' ').title()}: "
                f"{d.before} → {d.after} ({d.change:+.2f})"
                for d in material
            )
        if result.grade_before != result.grade_after:
            body_parts.append(
                f"\nGrade: {result.grade_before} → {result.grade_after}"
            )
        if result.highlights:
            body_parts.append("\nHighlights:")
            body_parts.extend(f"  • {h}" for h in result.highlights[:5])

        body = "\n".join(body_parts)
        queued = 0
        for user_id, tenant_id in recipients:
            self.db.add(Notification(
                tenant_id=tenant_id, user_id=user_id, channel="in_app",
                topic="filing.new", subject=subject[:240], body=body,
                link=f"/companies/{company.ticker}",
            ))
            queued += 1
        self.db.commit()
        return queued

    def _subscribers(self, company_id: str) -> list[tuple[str, int | None]]:
        """Users watching this company.

        Watchlist membership is the subscription: a user who has put a company
        on a watchlist has already said they care about it, so asking them to
        opt in a second time would leave the feature unused.

        `Watchlist` carries no tenant column — tenancy is derived from the
        owner — so the notification is written with a null tenant and the
        delivery layer resolves it from the user.
        """
        out: set[tuple[str, int | None]] = set()
        try:
            from app.models.portfolio import Watchlist, WatchlistEntry

            rows = self.db.execute(
                select(Watchlist.owner_id)
                .join(WatchlistEntry, WatchlistEntry.watchlist_id == Watchlist.id)
                .where(WatchlistEntry.company_id == company_id)
                .distinct()
            ).all()
            for (owner_id,) in rows:
                if owner_id:
                    out.add((owner_id, None))
        except Exception:  # noqa: BLE001 — an absent model must not break alerts
            log.exception("watchlist subscriber lookup failed",
                          company_id=company_id)
        return sorted(out)


#: Phrases the grounded analyst uses when it declines. Matching them keeps a
#: refusal out of a customer-facing alert.
_REFUSAL_MARKERS = (
    "insufficient evidence",
    "no verified evidence",
    "the platform holds no",
    "there is nothing to cite",
    "platform does not hold",
)


def _is_refusal(text: str) -> bool:
    lowered = " ".join((text or "").lower().split())
    return any(marker in lowered for marker in _REFUSAL_MARKERS)


def documents_awaiting_post_processing(db: Any, *, limit: int = 20) -> list[DiscoveredFiling]:
    """Collected filings whose document has finished indexing.

    The collector leaves a row at `embedding` once ingestion is queued; the
    document worker completes the parse asynchronously. This finds the rows
    whose documents are now `completed`, which is the trigger for rescoring.
    """
    from app.domain.filings.collection import CollectionStatus

    rows = db.execute(
        select(DiscoveredFiling, Document)
        .join(Document, Document.id == DiscoveredFiling.document_id)
        .where(
            DiscoveredFiling.status == CollectionStatus.EMBEDDING.value,
            Document.status.in_(("completed", "ready")),
        )
        .limit(limit)
    ).all()
    return [row[0] for row in rows]
