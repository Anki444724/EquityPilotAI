"""API contracts for the AI Operations Center (Phase 5)."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class AIOverrideIn(BaseModel):
    mode: str = Field(default="manual", pattern="^(auto|manual)$")
    manual_score: float | None = Field(default=None, ge=0, le=100)
    manual_confidence: float | None = Field(default=None, ge=0, le=1)
    manual_risk: float | None = Field(default=None, ge=0, le=100)
    manual_summary: str | None = Field(default=None, max_length=2000)
    manual_bull_case: str | None = Field(default=None, max_length=2000)
    manual_bear_case: str | None = Field(default=None, max_length=2000)
    manual_recommendation: str | None = Field(default=None, max_length=32)
    reason: str | None = Field(default=None, max_length=500)
    expires_in_minutes: int | None = Field(default=None, ge=1, le=525600)


class AIOverrideOut(BaseModel):
    id: int
    company_id: str
    ticker: str
    mode: str
    manual_score: float | None = None
    manual_confidence: float | None = None
    manual_risk: float | None = None
    manual_summary: str | None = None
    manual_bull_case: str | None = None
    manual_bear_case: str | None = None
    manual_recommendation: str | None = None
    reason: str | None = None
    expires_at: datetime | None = None
    created_by_email: str | None = None
    created_at: datetime
    is_active: bool
