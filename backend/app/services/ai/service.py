"""AI service — orchestration, persistence and prompt versioning.

The single entry point the API and any future agent call. It builds an analyst
for a company, records what was generated, and manages the prompt library's
versions.
"""
from __future__ import annotations

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from app.domain.ai.types import EvidenceKind
from app.models.ai import AIAnalysis, AIUsageRecord, PromptRecord
from app.services.ai.analyst import AnalystResult, ResearchAnalyst
from app.services.ai.context_builder import ContextBuilder
from app.services.ai.memory import memory_store
from app.services.ai.prompt_builder import PromptBuilder
from app.services.ai.prompt_library import (
    BUILTIN_PROMPTS, OutputStyle, PromptTemplate,
)
from app.services.ai.providers.router import ProviderRouter
from app.services.analysis_service import AnalysisService
from app.services.documents.service import DocumentService
from app.services.forecast.service import ForecastService
from app.services.scoring.service import ScoringService
from app.services.valuation.service import ValuationService

#: Shared across requests so the ledger and cache accumulate.
_router = ProviderRouter()


class AIError(ValueError):
    """Invalid AI configuration or request."""


class AIService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.router = _router

    # ------------------------------------------------------------- analysts
    def analyst_for(self, analysis: AnalysisService) -> ResearchAnalyst:
        builder = ContextBuilder(
            analysis,
            ForecastService(self.db),
            ValuationService(self.db),
            ScoringService(self.db),
            # Module 7. Uploaded filings become citable evidence alongside the
            # computed engines, and `document_search` becomes a real retrieval.
            DocumentService(self.db),
        )
        return ResearchAnalyst(builder, self.router, PromptBuilder())

    # ------------------------------------------------------- prompt library
    def seed_builtin_prompts(self) -> int:
        """Write built-in prompts into the database on first use."""
        existing = {
            key for (key,) in self.db.execute(select(PromptRecord.key).distinct()).all()
        }
        written = 0
        for template in BUILTIN_PROMPTS.values():
            if template.key in existing:
                continue
            self.db.add(PromptRecord(
                key=template.key, version=template.version, label=template.label,
                description=template.description, task=template.task,
                template=template.template,
                evidence=[k.value for k in template.evidence],
                style=template.style.value, max_tokens=template.max_tokens,
                temperature=template.temperature, is_active=True, is_builtin=True,
            ))
            written += 1
        if written:
            self.db.commit()
        return written

    def list_prompts(self, *, active_only: bool = True) -> list[PromptRecord]:
        self.seed_builtin_prompts()
        stmt = select(PromptRecord)
        if active_only:
            stmt = stmt.where(PromptRecord.is_active.is_(True))
        return list(
            self.db.execute(stmt.order_by(PromptRecord.key, desc(PromptRecord.version)))
            .scalars().all()
        )

    def get_active_prompt(self, key: str) -> PromptTemplate:
        """Resolve a prompt: the newest active DB version, else the built-in."""
        self.seed_builtin_prompts()
        record = self.db.execute(
            select(PromptRecord)
            .where(PromptRecord.key == key)
            .where(PromptRecord.is_active.is_(True))
            .order_by(desc(PromptRecord.version))
            .limit(1)
        ).scalar_one_or_none()

        if record is None:
            template = BUILTIN_PROMPTS.get(key)
            if template is None:
                raise AIError(f"unknown prompt '{key}'")
            return template

        return PromptTemplate(
            key=record.key, version=record.version, label=record.label,
            description=record.description or "", task=record.task,
            template=record.template,
            evidence=tuple(EvidenceKind(e) for e in (record.evidence or [])),
            style=OutputStyle(record.style), max_tokens=record.max_tokens,
            temperature=record.temperature, is_builtin=record.is_builtin,
        )

    def save_prompt_version(
        self, key: str, *, task: str | None = None, template: str | None = None,
        label: str | None = None, max_tokens: int | None = None,
        temperature: float | None = None, editor: str | None = None,
    ) -> PromptRecord:
        """Create a new version. Existing versions are deactivated, never edited.

        Immutability matters here: a report generated last quarter must remain
        reproducible against the prompt that produced it.
        """
        current = self.get_active_prompt(key)

        latest = self.db.execute(
            select(func.max(PromptRecord.version)).where(PromptRecord.key == key)
        ).scalar() or 0

        self.db.execute(
            PromptRecord.__table__.update()
            .where(PromptRecord.key == key)
            .values(is_active=False)
        )

        record = PromptRecord(
            key=key, version=latest + 1,
            label=label or current.label, description=current.description,
            task=task or current.task, template=template or current.template,
            evidence=[k.value for k in current.evidence],
            style=current.style.value,
            max_tokens=max_tokens or current.max_tokens,
            temperature=current.temperature if temperature is None else temperature,
            is_active=True, is_builtin=False, edited_by=editor,
        )
        self.db.add(record)
        self.db.commit()
        return record

    def activate_version(self, key: str, version: int) -> PromptRecord:
        """Roll back to an earlier version."""
        record = self.db.execute(
            select(PromptRecord)
            .where(PromptRecord.key == key)
            .where(PromptRecord.version == version)
        ).scalar_one_or_none()
        if record is None:
            raise AIError(f"prompt '{key}' has no version {version}")

        self.db.execute(
            PromptRecord.__table__.update()
            .where(PromptRecord.key == key).values(is_active=False)
        )
        record.is_active = True
        self.db.commit()
        return record

    # ---------------------------------------------------------- persistence
    def record(self, company_id: str, result: AnalystResult,
               owner: str | None = None) -> AIAnalysis:
        analysis = AIAnalysis(
            company_id=company_id, capability=result.capability,
            content=result.content, provider=result.provider, model=result.model,
            prompt_key=result.prompt_key, prompt_version=result.prompt_version,
            citation_keys=[c.key for c in result.citations],
            citation_coverage=(
                result.citation_audit.coverage if result.citation_audit else 0.0
            ),
            is_supported=result.is_supported,
            guardrails_passed=bool(result.guardrails and result.guardrails.passed),
            warnings=result.warnings,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            cost_usd=result.cost_usd, latency_ms=result.latency_ms,
        )
        self.db.add(analysis)
        self.db.add(AIUsageRecord(
            owner=owner, provider=result.provider, model=result.model,
            capability=result.capability, prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens, cost_usd=result.cost_usd,
            latency_ms=result.latency_ms, cached=result.cached, succeeded=True,
        ))
        self.db.commit()
        return analysis

    def history(self, company_id: str, capability: str | None = None,
                limit: int = 20) -> list[AIAnalysis]:
        stmt = select(AIAnalysis).where(AIAnalysis.company_id == company_id)
        if capability:
            stmt = stmt.where(AIAnalysis.capability == capability)
        return list(
            self.db.execute(stmt.order_by(desc(AIAnalysis.created_at)).limit(limit))
            .scalars().all()
        )

    # ---------------------------------------------------------------- usage
    def usage_summary(self, owner: str | None = None) -> dict:
        """Persisted accounting, merged with the in-process ledger."""
        stmt = select(
            func.count(AIUsageRecord.id), func.sum(AIUsageRecord.prompt_tokens),
            func.sum(AIUsageRecord.completion_tokens), func.sum(AIUsageRecord.cost_usd),
        )
        if owner:
            stmt = stmt.where(AIUsageRecord.owner == owner)
        calls, prompt_tokens, completion_tokens, cost = self.db.execute(stmt).one()

        return {
            "persisted": {
                "calls": calls or 0,
                "prompt_tokens": int(prompt_tokens or 0),
                "completion_tokens": int(completion_tokens or 0),
                "total_tokens": int((prompt_tokens or 0) + (completion_tokens or 0)),
                "cost_usd": round(float(cost or 0.0), 6),
            },
            "session": self.router.ledger.snapshot(),
            "providers_available": self.router.available,
        }

    # --------------------------------------------------------------- memory
    @staticmethod
    def memory(session_id: str):
        return memory_store.get(session_id)
