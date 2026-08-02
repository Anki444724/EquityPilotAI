"""Automatic memory enrichment — the path from a new document to memory.

This is the component the audit found missing. Every service it calls already
existed and was tested; none of them was ever invoked automatically, so 163
documents sat outside the vault.

Runs as a background job (`JobKind.MEMORY_ENRICHMENT`), never inside the
document worker. That separation is not tidiness: production has crashed three
times in a 1 GB container while the document worker held a large PDF, most
recently a 62-page file that produced 516 chunks and occupied the process for
293 seconds. Adding LLM work to that loop would guarantee a fourth crash.

Every stage is independently guarded. A stage that fails records why and the
pass continues, because a rate-limited summariser must not prevent the vault
from being updated — the structural half of memory is the half that works
without a provider.
"""

from __future__ import annotations

import time
from typing import Any

import structlog
from sqlalchemy import func, select

from app.domain.knowledge.enrichment import (
    LLM_STAGES, MAX_OBSERVATION_YEARS_PER_PASS,
    MAX_SUMMARY_DOCUMENTS_PER_PASS, PROMOTABLE_FIELDS, STAGE_ORDER,
    EnrichmentResult, EnrichmentStage, StageOutcome, should_promote,
)
from app.domain.knowledge.vault import VaultSection
from app.models.company import Company, FinancialFact
from app.models.document import Document, DocumentFact
from app.models.knowledge import DocumentSummary, YearlyObservation

log = structlog.get_logger(__name__)

#: Extracted facts enter BELOW the tier screener.in and the US pipeline write
#: (`Precedence.STORE` = 2), so a regex reading of a PDF can fill a gap but can
#: never silently overwrite an audited figure. The canonical resolver prefers
#: the lower number.
EXTRACTION_PRECEDENCE = 3          # Precedence.ALIAS

MAX_AI_NOTES_PER_PASS = 6


class MemoryEnrichmentService:
    """Runs the full memory pass for one company."""

    def __init__(self, db: Any, *, allow_llm: bool = True) -> None:
        self.db = db
        # Set False to run only the structural stages. Used by tests and by
        # the degraded path when no provider is configured.
        self.allow_llm = allow_llm

    # ------------------------------------------------------------------ run
    def run(
        self, company_id: str, *, trigger_document_id: int | None = None,
    ) -> EnrichmentResult:
        started = time.perf_counter()
        result = EnrichmentResult(
            company_id=company_id, trigger_document_id=trigger_document_id,
        )

        handlers = {
            EnrichmentStage.FINANCIAL_PROMOTION: self._promote_financials,
            EnrichmentStage.VAULT: self._build_vault,
            EnrichmentStage.AI_NOTES: self._write_ai_notes,
            EnrichmentStage.SUMMARIES: self._summarise,
            EnrichmentStage.OBSERVATIONS: self._observe,
            EnrichmentStage.TEMPORAL_LINK: self._link_temporal,
        }

        for stage in STAGE_ORDER:
            if stage in LLM_STAGES and not self.allow_llm:
                result.stages.append(StageOutcome(
                    stage=stage, skipped=True,
                    detail="LLM stages disabled for this pass",
                ))
                continue

            stage_started = time.perf_counter()
            try:
                outcome = handlers[stage](company_id)
            except Exception as exc:  # noqa: BLE001 — one stage must not stop the pass
                # Roll back so the next stage starts from a clean session; a
                # failed flush otherwise poisons everything after it.
                self.db.rollback()
                outcome = StageOutcome(
                    stage=stage, ok=False,
                    detail=f"{type(exc).__name__}: {exc}"[:300],
                )
                log.warning("enrichment stage failed", stage=stage.value,
                            company_id=company_id, error=str(exc)[:200])
            outcome.ms = (time.perf_counter() - stage_started) * 1000
            result.stages.append(outcome)

        # The Data Quality Score is a function of everything above, so it is
        # recomputed here rather than on a separate schedule. This is what
        # makes the brief's "no manual updates" true: a document arriving
        # raises the score without anyone asking it to.
        #
        # Guarded and last. A scoring failure must not undo a successful
        # enrichment pass, and the score is always recomputable on read.
        try:
            from app.models.company import Company
            from app.services.quality.service import QualitySnapshotService

            company = self.db.get(Company, company_id)
            if company is not None:
                QualitySnapshotService(self.db).refresh(company)
        except Exception as exc:  # noqa: BLE001
            self.db.rollback()
            log.warning("quality refresh after enrichment failed",
                        company_id=company_id, error=str(exc)[:200])

        result.total_ms = (time.perf_counter() - started) * 1000
        log.info("memory enrichment complete", company_id=company_id,
                 written=result.written, ms=round(result.total_ms, 1),
                 failed=[s.stage.value for s in result.failed_stages])
        return result

    # ------------------------------------------------- 1. financial promotion
    def _promote_financials(self, company_id: str) -> StageOutcome:
        """Promote high-confidence extracted figures into canonical facts.

        Writes at `Precedence.ALIAS`, strictly below the tier screener.in
        occupies, so this fills gaps without ever displacing a filed figure.
        The `promoted` flag on `DocumentFact` — present in the schema since
        the beginning and never set by anything — makes the pass idempotent.
        """
        rows = self.db.execute(
            select(DocumentFact).where(
                DocumentFact.company_id == company_id,
                DocumentFact.promoted.is_(False),
                DocumentFact.value.is_not(None),
                DocumentFact.fiscal_year.is_not(None),
                DocumentFact.field_key.in_(tuple(PROMOTABLE_FIELDS)),
            )
        ).scalars().all()

        if not rows:
            return StageOutcome(
                stage=EnrichmentStage.FINANCIAL_PROMOTION, skipped=True,
                detail="no unpromoted extracted figures",
            )

        written = 0
        considered = 0
        for fact in rows:
            considered += 1
            if not should_promote(fact.confidence or 0.0, fact.field_key):
                # Marked promoted regardless, so a low-confidence fact is
                # examined once rather than on every future pass.
                fact.promoted = True
                continue

            line_item = PROMOTABLE_FIELDS[fact.field_key]
            existing = self.db.scalar(
                select(FinancialFact).where(
                    FinancialFact.company_id == company_id,
                    FinancialFact.fiscal_year == fact.fiscal_year,
                    FinancialFact.line_item == line_item,
                    FinancialFact.precedence == EXTRACTION_PRECEDENCE,
                )
            )
            if existing is not None:
                # A later filing restating the same period wins, but only
                # within this precedence tier.
                existing.value = float(fact.value)
                existing.source = f"document:{fact.document_id}"
            else:
                self.db.add(FinancialFact(
                    company_id=company_id,
                    fiscal_year=fact.fiscal_year,
                    line_item=line_item,
                    value=float(fact.value),
                    precedence=EXTRACTION_PRECEDENCE,
                    source=f"document:{fact.document_id}",
                ))
                written += 1
            fact.promoted = True

        self.db.commit()
        return StageOutcome(
            stage=EnrichmentStage.FINANCIAL_PROMOTION, written=written,
            detail=f"{considered} extracted figures examined",
        )

    # ------------------------------------------------------------- 2. vault
    def _build_vault(self, company_id: str) -> StageOutcome:
        from app.services.knowledge.ingest import KnowledgeIngestor

        report = KnowledgeIngestor(self.db).ingest_company(company_id)
        self.db.commit()
        return StageOutcome(
            stage=EnrichmentStage.VAULT,
            written=report.asserted,
            detail=f"{getattr(report, 'facts_seen', 0)} facts considered",
        )

    # ---------------------------------------------------------- 3. AI notes
    def _write_ai_notes(self, company_id: str) -> StageOutcome:
        """Populate `VaultSection.AI_NOTES`.

        The audit found this section declared and permanently empty: it had no
        producer anywhere in the codebase. It now records what the platform
        itself concluded — as distinct from what a filing stated — so a reader
        can tell an inference from a disclosure.

        Sourced from the temporal observations rather than from a fresh LLM
        call. Those are already generated, already cited and already carry a
        confidence; re-deriving the same judgement through a second prompt
        would cost money to produce a less grounded answer.
        """
        from app.services.knowledge.vault import KnowledgeVault
        from app.domain.knowledge.vault import Provenance

        observations = self.db.execute(
            select(YearlyObservation)
            .where(
                YearlyObservation.company_id == company_id,
                YearlyObservation.status == "current",
                YearlyObservation.is_fallback.is_(False),
            )
            .order_by(YearlyObservation.fiscal_year.desc())
            .limit(MAX_AI_NOTES_PER_PASS)
        ).scalars().all()

        if not observations:
            return StageOutcome(
                stage=EnrichmentStage.AI_NOTES, skipped=True,
                detail="no non-fallback observations to record",
            )

        vault = KnowledgeVault(self.db)
        written = 0
        for observation in observations:
            findings = [
                f.strip() for f in (observation.findings or "").split("\n")
                if f.strip()
            ]
            if not findings:
                continue
            result = vault.assert_knowledge(
                company_id,
                VaultSection.AI_NOTES,
                f"observation_fy{observation.fiscal_year}",
                label=f"AI observation FY{observation.fiscal_year}",
                value_text=" ".join(findings)[:4000],
                confidence=observation.confidence or 0.0,
                provenance=Provenance(
                    fiscal_year=observation.fiscal_year,
                    doc_type="ai_observation",
                ),
                evidence=observation.verdict_reasoning,
                generated_by=observation.generated_by,
            )
            if result.entry is not None:
                written += 1

        self.db.commit()
        return StageOutcome(stage=EnrichmentStage.AI_NOTES, written=written)

    # --------------------------------------------------------- 4. summaries
    def _summarise(self, company_id: str) -> StageOutcome:
        """Summarise documents that have none yet, newest first.

        Capped per pass. The summariser is the most expensive stage, and a
        company with eighty back-filled filings would otherwise issue eighty
        LLM calls inside one lease and time it out. The remainder is picked up
        by the next pass, so the backlog drains rather than stalling.
        """
        from app.services.knowledge.summaries import SummaryService

        summarised = (
            select(DocumentSummary.document_id)
            .where(DocumentSummary.company_id == company_id)
            .distinct()
        )
        pending = self.db.execute(
            select(Document)
            .where(
                Document.company_id == company_id,
                Document.status == "completed",
                Document.id.not_in(summarised),
            )
            .order_by(Document.created_at.desc())
            .limit(MAX_SUMMARY_DOCUMENTS_PER_PASS)
        ).scalars().all()

        if not pending:
            return StageOutcome(
                stage=EnrichmentStage.SUMMARIES, skipped=True,
                detail="every completed document already has summaries",
            )

        service = SummaryService(self.db)
        written = 0
        errors: list[str] = []
        for document in pending:
            run = service.generate_for_document(document)
            written += getattr(run, "generated", 0)
            if getattr(run, "errors", None):
                errors.append(str(run.errors[0])[:120])

        return StageOutcome(
            stage=EnrichmentStage.SUMMARIES, written=written,
            ok=not (errors and written == 0),
            detail=(
                f"{len(pending)} documents"
                + (f"; first error: {errors[0]}" if errors else "")
            ),
        )

    # ------------------------------------------------------ 5. observations
    def _observe(self, company_id: str) -> StageOutcome:
        """Generate observations for fiscal years that lack one.

        Only the most recent uncovered years, for the same cost reason as
        summaries. `build_company` is chronological by design — each year is
        judged against the previous year's guidance — so the window is taken
        from the end of the series rather than sampled.
        """
        from app.services.knowledge.temporal import TemporalMemoryService

        service = TemporalMemoryService(self.db)
        observable = service.observable_years(company_id)
        if not observable:
            return StageOutcome(
                stage=EnrichmentStage.OBSERVATIONS, skipped=True,
                detail="no fiscal year has evidence yet",
            )

        covered = {
            row for row in self.db.execute(
                select(YearlyObservation.fiscal_year).where(
                    YearlyObservation.company_id == company_id,
                    YearlyObservation.status == "current",
                )
            ).scalars().all()
        }
        missing = [y for y in observable if y not in covered]
        if not missing:
            return StageOutcome(
                stage=EnrichmentStage.OBSERVATIONS, skipped=True,
                detail=f"all {len(observable)} observable years covered",
            )

        window = missing[-MAX_OBSERVATION_YEARS_PER_PASS:]
        written = 0
        for year in window:
            prior = service.current(company_id, year - 1)
            row = service.generate_year(company_id, year, prior=prior)
            if row is not None:
                written += 1

        return StageOutcome(
            stage=EnrichmentStage.OBSERVATIONS, written=written,
            detail=f"{len(missing)} years uncovered, {len(window)} attempted",
        )

    # ----------------------------------------------------- 6. temporal link
    def _link_temporal(self, company_id: str) -> StageOutcome:
        """Re-judge the latest year once its predecessor exists.

        Observations are generated in whatever order evidence arrives. A year
        generated before its predecessor was known carries
        `prior_verdict = not_assessable` for a reason that has since stopped
        being true. This re-runs the newest year when a usable predecessor now
        exists and the verdict is still unset — turning a stale
        `not_assessable` into a real judgement without touching years that
        were correctly judged.
        """
        from app.services.knowledge.temporal import TemporalMemoryService

        service = TemporalMemoryService(self.db)
        rows = service.timeline(company_id)
        if len(rows) < 2:
            return StageOutcome(
                stage=EnrichmentStage.TEMPORAL_LINK, skipped=True,
                detail="fewer than two observed years",
            )

        latest = rows[-1]
        prior = service.current(company_id, latest.fiscal_year - 1)
        if prior is None:
            return StageOutcome(
                stage=EnrichmentStage.TEMPORAL_LINK, skipped=True,
                detail=f"no observation for FY{latest.fiscal_year - 1}",
            )
        if not (prior.guidance or "").strip():
            return StageOutcome(
                stage=EnrichmentStage.TEMPORAL_LINK, skipped=True,
                detail=f"FY{prior.fiscal_year} recorded no guidance to judge",
            )
        if latest.prior_verdict != "not_assessable":
            return StageOutcome(
                stage=EnrichmentStage.TEMPORAL_LINK, skipped=True,
                detail=f"FY{latest.fiscal_year} already judged "
                       f"'{latest.prior_verdict}'",
            )

        row = service.generate_year(
            company_id, latest.fiscal_year, prior=prior, overwrite=True,
        )
        judged = row is not None and row.prior_verdict != "not_assessable"
        return StageOutcome(
            stage=EnrichmentStage.TEMPORAL_LINK,
            written=1 if judged else 0,
            detail=(
                f"FY{latest.fiscal_year} re-judged against FY{prior.fiscal_year}"
                + ("" if judged else " — still not assessable on the evidence")
            ),
        )


# --------------------------------------------------------------- selection
def companies_needing_enrichment(db: Any, *, limit: int = 20) -> list[str]:
    """Companies whose documents have outrun their memory.

    The audit's headline number came from exactly this comparison: documents
    newer than the newest vault entry. Used by the sweep that back-fills the
    163-document gap, and as a safety net if an enqueue is ever lost.
    """
    newest_document = (
        select(
            Document.company_id.label("cid"),
            func.max(Document.created_at).label("doc_at"),
        )
        .where(Document.status == "completed")
        .group_by(Document.company_id)
        .subquery()
    )
    from app.models.knowledge import KnowledgeEntry

    newest_entry = (
        select(
            KnowledgeEntry.company_id.label("cid"),
            func.max(KnowledgeEntry.created_at).label("vault_at"),
        )
        .group_by(KnowledgeEntry.company_id)
        .subquery()
    )

    rows = db.execute(
        select(newest_document.c.cid)
        .outerjoin(newest_entry, newest_entry.c.cid == newest_document.c.cid)
        .where(
            (newest_entry.c.vault_at.is_(None))
            | (newest_document.c.doc_at > newest_entry.c.vault_at)
        )
        .limit(limit)
    ).scalars().all()
    return list(rows)
