"""Persistence for the AI layer.

Three things are worth storing:

* **Prompts** — versioned, so a report can be reproduced against the exact
  wording that produced it. Edits create a new version; nothing is mutated.
* **Analyses** — the generated output plus the citations and audit metadata, so
  a claim can be re-examined months later.
* **Usage** — token and cost accounting per call, for budgeting and rate limits.
"""
from __future__ import annotations

from sqlalchemy import (
    Boolean, Float, ForeignKey, Index, Integer, JSON, String, Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.company import Company


class PromptRecord(Base):
    """A versioned prompt. Built-ins are seeded; edits create new versions."""

    __tablename__ = "ai_prompts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    label: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    task: Mapped[str] = mapped_column(Text, nullable=False)
    template: Mapped[str] = mapped_column(Text, nullable=False)

    #: Evidence kinds this capability requires, as a JSON list.
    evidence: Mapped[list | None] = mapped_column(JSON)
    style: Mapped[str] = mapped_column(String(32), default="markdown")
    max_tokens: Mapped[int] = mapped_column(Integer, default=1400)
    temperature: Mapped[float] = mapped_column(Float, default=0.2)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_builtin: Mapped[bool] = mapped_column(Boolean, default=False)
    edited_by: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (
        UniqueConstraint("key", "version", name="uq_prompt_key_version"),
        Index("ix_prompt_active", "key", "is_active"),
    )


class AIAnalysis(Base):
    """A generated analysis, retained with its evidence."""

    __tablename__ = "ai_analyses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[str] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    capability: Mapped[str] = mapped_column(String(64), nullable=False)

    content: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(96), nullable=False)
    prompt_key: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_version: Mapped[int] = mapped_column(Integer, default=1)

    #: Resolved citation keys, so a claim can be traced back.
    citation_keys: Mapped[list | None] = mapped_column(JSON)
    citation_coverage: Mapped[float] = mapped_column(Float, default=0.0)
    is_supported: Mapped[bool] = mapped_column(Boolean, default=False)
    guardrails_passed: Mapped[bool] = mapped_column(Boolean, default=True)
    warnings: Mapped[list | None] = mapped_column(JSON)

    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0)

    company: Mapped[Company] = relationship()

    __table_args__ = (
        Index("ix_analysis_company_capability", "company_id", "capability"),
    )


class AIUsageRecord(Base):
    """Per-call accounting, for cost control and rate limiting."""

    __tablename__ = "ai_usage"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner: Mapped[str | None] = mapped_column(String(64), index=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(96), nullable=False)
    capability: Mapped[str | None] = mapped_column(String(64))

    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    cached: Mapped[bool] = mapped_column(Boolean, default=False)
    succeeded: Mapped[bool] = mapped_column(Boolean, default=True)

    __table_args__ = (Index("ix_usage_owner_provider", "owner", "provider"),)
