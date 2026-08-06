"""API contracts for the Market Operations Center (Phase 4)."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class MarketOverrideIn(BaseModel):
    manual_price: float | None = None
    manual_volume: float | None = None
    manual_market_cap: float | None = None
    manual_pe: float | None = None
    manual_pb: float | None = None
    reason: str | None = Field(default=None, max_length=500)
    expires_in_minutes: int | None = Field(default=None, ge=1, le=525600)
    auto_revert: bool = False


class MarketOverrideOut(BaseModel):
    id: int
    company_id: str
    ticker: str
    manual_price: float | None = None
    manual_volume: float | None = None
    manual_market_cap: float | None = None
    manual_pe: float | None = None
    manual_pb: float | None = None
    reason: str | None = None
    expires_at: datetime | None = None
    auto_revert: bool
    created_by_email: str | None = None
    created_at: datetime
    is_active: bool


class ProviderInfoOut(BaseModel):
    name: str
    priority: int
    configured: bool
    implemented: bool
    available: bool
    latency_ms: float | None = None
    last_success: str | None = None
    calls: int = 0
    rate_limit_remaining: int | None = None
    status: str


class ProviderHealthOut(BaseModel):
    name: str
    status: str
    configured: bool
    available: bool
    latency_ms: float | None = None
    last_success: str | None = None
    calls: int = 0
    rate_limit_remaining: int | None = None
    priority: int
