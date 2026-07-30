"""Persistence for forecasts and their assumptions.

Only *inputs* are stored. Projected figures are never persisted — they are
recomputed from the assumption set on demand, which guarantees the stored
forecast and the displayed forecast can never disagree. It also means an engine
improvement immediately benefits every saved forecast.

``ForecastAssumptionRecord`` stores one driver per row rather than a JSON blob,
so individual assumptions can be queried, audited and attributed. That row-level
provenance is what lets the AI layer write a single driver — with its citation —
without touching anything else.
"""
from __future__ import annotations

from enum import StrEnum

from sqlalchemy import (
    Boolean, Float, ForeignKey, Index, Integer, JSON, String, Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.company import Company


class ForecastStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    ARCHIVED = "archived"


class Forecast(Base):
    """A saved forecast: a horizon, a method and an owner."""

    __tablename__ = "forecasts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    company_id: Mapped[str] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )

    name: Mapped[str] = mapped_column(String(160), nullable=False, default="Base forecast")
    #: 3, 5 or 10 — validated at the API boundary.
    horizon_years: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    revenue_method: Mapped[str] = mapped_column(String(32), nullable=False, default="cagr")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=ForecastStatus.ACTIVE)

    created_by: Mapped[str | None] = mapped_column(String(64))
    notes: Mapped[str | None] = mapped_column(Text)

    #: Segment definitions for a bottom-up build, when that method is selected.
    segments: Mapped[list | None] = mapped_column(JSON)

    #: Bumped on every assumption change so cached projections invalidate.
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    company: Mapped[Company] = relationship()
    assumptions: Mapped[list["ForecastAssumptionRecord"]] = relationship(
        back_populates="forecast", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_forecast_company", "company_id", "status"),
    )


class ForecastAssumptionRecord(Base):
    """One driver, for one scenario, on one forecast.

    ``scenario`` is nullable: a null row is a *base* assumption that all three
    scenarios derive from. A non-null row is an explicit per-scenario override
    that suppresses the derived shift.
    """

    __tablename__ = "forecast_assumptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    forecast_id: Mapped[str] = mapped_column(
        ForeignKey("forecasts.id", ondelete="CASCADE"), nullable=False
    )

    #: Driver field name on ForecastAssumptions, e.g. "ebitda_margin".
    driver: Mapped[str] = mapped_column(String(64), nullable=False)
    scenario: Mapped[str | None] = mapped_column(String(16))

    value: Mapped[float] = mapped_column(Float, nullable=False)
    #: Optional {period: value} overrides, as JSON.
    by_year: Mapped[dict | None] = mapped_column(JSON)

    #: Provenance — the field that makes the engine AI-ready.
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="analyst")
    citation: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    #: Set when written by the AI layer, for review workflows.
    requires_review: Mapped[bool] = mapped_column(Boolean, default=False)

    forecast: Mapped[Forecast] = relationship(back_populates="assumptions")

    __table_args__ = (
        UniqueConstraint("forecast_id", "driver", "scenario", name="uq_assumption_driver_scenario"),
        Index("ix_assumption_forecast", "forecast_id", "scenario"),
    )
