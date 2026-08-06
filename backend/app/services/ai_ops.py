"""AI operations service (Phase 5).

Manages manual AI-score overrides and aggregates AI model registry, cost
dashboard, prompt catalog, queue, learning, RAG and log state for the AI
Operations Center.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.ai import AIUsageRecord, PromptRecord
from app.models.ai_ops import AIOverride
from app.models.company import Company


class AIOpsError(Exception):
    """Raised when an AI-operations action cannot be honoured."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


#: The AI model providers surfaced in the operations center, matching the
#: existing ProviderRouter registry.
AI_MODEL_REGISTRY: list[dict[str, Any]] = [
    {"name": "Gemini", "env_key": "GEMINI_API_KEY"},
    {"name": "OpenRouter", "env_key": "OPENROUTER_API_KEY"},
    {"name": "Claude", "env_key": "ANTHROPIC_API_KEY"},
    {"name": "OpenAI", "env_key": "OPENAI_API_KEY"},
    {"name": "Local LLM", "env_key": "LOCAL_LLM_ENABLED"},
]


class AIOpsService:
    def __init__(self, db: Session) -> None:
        self.db = db

    # ==================================================================
    # AI score overrides
    # ==================================================================
    def create_override(
        self, company_id: str, payload: dict[str, Any], *, actor_id=None, actor_email=None,
    ) -> AIOverride:
        company = self.db.get(Company, company_id)
        if company is None or company.deleted_at is not None:
            raise AIOpsError("company not found")

        # Expire any existing override for this company.
        for existing in self.active_for(company_id):
            existing.expires_at = _utcnow()

        expires_at = None
        if payload.get("expires_in_minutes"):
            expires_at = _utcnow() + timedelta(minutes=int(payload["expires_in_minutes"]))

        ov = AIOverride(
            company_id=company.id, ticker=company.ticker,
            mode=payload.get("mode", "manual"),
            manual_score=payload.get("manual_score"),
            manual_confidence=payload.get("manual_confidence"),
            manual_risk=payload.get("manual_risk"),
            manual_summary=payload.get("manual_summary"),
            manual_bull_case=payload.get("manual_bull_case"),
            manual_bear_case=payload.get("manual_bear_case"),
            manual_recommendation=payload.get("manual_recommendation"),
            reason=payload.get("reason"),
            expires_at=expires_at,
            created_by=actor_id, created_by_email=actor_email,
        )
        self.db.add(ov)
        self.db.flush()
        return ov

    def active_for(self, company_id: str) -> list[AIOverride]:
        return list(self.db.execute(
            select(AIOverride).where(
                AIOverride.company_id == company_id,
                AIOverride.mode == "manual",
                (AIOverride.expires_at.is_(None))
                | (AIOverride.expires_at > _utcnow()),
            )
        ).scalars())

    def list_overrides(self, *, active_only: bool = False) -> list[AIOverride]:
        stmt = select(AIOverride)
        if active_only:
            stmt = stmt.where(
                AIOverride.mode == "manual",
                (AIOverride.expires_at.is_(None))
                | (AIOverride.expires_at > _utcnow()),
            )
        return list(self.db.execute(
            stmt.order_by(AIOverride.created_at.desc())
        ).scalars())

    def get_override(self, override_id: int) -> AIOverride:
        ov = self.db.get(AIOverride, override_id)
        if ov is None:
            raise AIOpsError("override not found")
        return ov

    def clear_override(self, override_id: int) -> None:
        ov = self.get_override(override_id)
        ov.expires_at = _utcnow()  # immediate revert to auto

    def resolve_override(self, company: Company) -> AIOverride | None:
        active = self.active_for(company.id)
        return active[0] if active else None

    def apply_override(self, company: Company, score: dict[str, Any]) -> dict[str, Any] | None:
        """Return an overridden score view if a manual override is active."""
        ov = self.resolve_override(company)
        if ov is None:
            return None
        out = dict(score)
        if ov.manual_score is not None:
            out["overall_score"] = ov.manual_score
        if ov.manual_recommendation is not None:
            out["recommendation"] = ov.manual_recommendation
        if ov.manual_summary is not None:
            out["summary"] = ov.manual_summary
        if ov.manual_confidence is not None:
            out["manual_confidence"] = ov.manual_confidence
        if ov.manual_risk is not None:
            out["manual_risk"] = ov.manual_risk
        out["bull_case"] = ov.manual_bull_case
        out["bear_case"] = ov.manual_bear_case
        out["overridden"] = True
        out["override_reason"] = ov.reason
        out["override_expires"] = ov.expires_at.isoformat() if ov.expires_at else None
        return out

    # ==================================================================
    # AI model registry
    # ==================================================================
    def models(self) -> list[dict[str, Any]]:
        import os
        out = []
        for i, spec in enumerate(AI_MODEL_REGISTRY):
            configured = bool((os.environ.get(spec["env_key"]) or "").strip())
            out.append({
                "name": spec["name"], "priority": i + 1, "configured": configured,
                "status": "live" if configured else "disabled",
            })
        return out

    # ==================================================================
    # Prompt catalog
    # ==================================================================
    def prompts(self) -> list[dict[str, Any]]:
        rows = self.db.execute(
            select(PromptRecord).order_by(PromptRecord.key, PromptRecord.version.desc())
        ).scalars().all()
        # Return the active version per key.
        by_key: dict[str, PromptRecord] = {}
        for r in rows:
            if r.key not in by_key and r.is_active:
                by_key[r.key] = r
        return [
            {"key": r.key, "version": r.version, "label": r.label,
             "task": r.task, "template": r.template, "max_tokens": r.max_tokens,
             "temperature": r.temperature, "is_active": r.is_active,
             "is_builtin": r.is_builtin, "edited_by": r.edited_by}
            for r in by_key.values()
        ]

    # ==================================================================
    # Cost dashboard (aggregated from ai_usage)
    # ==================================================================
    def cost_dashboard(self, *, days: int = 30) -> dict[str, Any]:
        since = _utcnow() - timedelta(days=days)
        rows = self.db.execute(
            select(AIUsageRecord).where(AIUsageRecord.created_at >= since)
        ).scalars().all()
        total_tokens = sum(r.prompt_tokens + r.completion_tokens for r in rows)
        total_cost = sum(r.cost_usd for r in rows)
        requests = len(rows)
        latencies = [r.latency_ms for r in rows if r.latency_ms]
        avg_latency = (sum(latencies) / len(latencies)) if latencies else 0.0
        by_provider: dict[str, dict[str, float | int]] = {}
        for r in rows:
            p = by_provider.setdefault(r.provider, {"tokens": 0, "cost": 0.0, "requests": 0})
            p["tokens"] += r.prompt_tokens + r.completion_tokens
            p["cost"] += r.cost_usd
            p["requests"] += 1
        return {
            "days": days,
            "total_tokens": total_tokens,
            "requests": requests,
            "avg_latency_ms": round(avg_latency, 1),
            "total_cost_usd": round(total_cost, 4),
            "daily_cost_usd": round(total_cost / max(days, 1), 4),
            "by_provider": {k: {kk: (round(v, 2) if kk == "cost" else v) for kk, v in p.items()} for k, p in by_provider.items()},
        }

    # ==================================================================
    # Queue / learning / RAG / logs — lightweight state
    # ==================================================================
    def queue_status(self) -> dict[str, Any]:
        return {
            "pending": 0, "running": 0, "completed": 0, "failed": 0,
        }

    def learning_status(self) -> dict[str, Any]:
        return {
            "feedback_count": 0, "correct": 0, "wrong": 0, "retrain_queue": 0,
        }

    def rag_status(self) -> dict[str, Any]:
        try:
            from app.models.document import DocumentChunk
            docs = self.db.execute(select(func.count())
                .select_from(__import__("app.models.document", fromlist=["Document"]).Document)
            ).scalar_one()
            chunks = self.db.execute(select(func.count()).select_from(DocumentChunk)).scalar_one()
        except Exception:  # noqa: BLE001
            docs = chunks = 0
        return {
            "documents": docs, "chunks": chunks, "embeddings": chunks,
            "vector_count": chunks,
        }

    def logs(self, *, limit: int = 100) -> dict[str, Any]:
        rows = self.db.execute(
            select(AIUsageRecord).order_by(AIUsageRecord.created_at.desc()).limit(limit)
        ).scalars().all()
        return {
            "items": [
                {"id": r.id, "provider": r.provider, "model": r.model,
                 "prompt_tokens": r.prompt_tokens, "completion_tokens": r.completion_tokens,
                 "cost_usd": r.cost_usd, "latency_ms": r.latency_ms, "succeeded": r.succeeded}
                for r in rows
            ],
            "total": len(rows),
        }
