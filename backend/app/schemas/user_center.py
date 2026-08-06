"""API contracts for the User Management & Subscription Center (Phase 7)."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, EmailStr, Field


class UserCreate(BaseModel):
    email: EmailStr
    name: str = Field(min_length=1, max_length=160)
    role: str = "read_only"
    tenant_id: int | None = None
    status: str = "active"
    password: str | None = Field(default=None, min_length=8)


class UserUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=160)
    role: str | None = None


class UserSessionOut(BaseModel):
    session_id: str
    ip_address: str | None = None
    user_agent: str | None = None
    issued_at: str
    expires_at: str


class InvoiceOut(BaseModel):
    id: int
    tenant_id: int
    number: str
    plan_tier: str
    period_start: str | None = None
    period_end: str | None = None
    subtotal_paise: int = 0
    tax_paise: int = 0
    total_paise: int = 0
    currency: str = "INR"
    status: str
    issued_at: datetime | None = None
    paid_at: datetime | None = None


class NotificationOut(BaseModel):
    id: int
    tenant_id: int | None = None
    user_id: str | None = None
    channel: str
    topic: str
    subject: str
    body: str = ""
    link: str | None = None
    read_at: datetime | None = None
    sent_at: datetime | None = None
    delivery_status: str = "pending"


class UserAnalyticsOut(BaseModel):
    days: int
    total_users: int
    active_users: int
    new_users: int
    premium_users: int
    free_users: int
    revenue_inr: float = 0.0
    tenants: int = 0
    retention_pct: float = 0.0
