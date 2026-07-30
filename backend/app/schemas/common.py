"""Shared API response primitives for the analysis modules."""
from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class Unit:
    """Canonical unit tokens. The UI renders these verbatim — it never guesses."""

    CRORE = "₹ cr"
    RUPEES = "₹"
    PERCENT = "%"
    MULTIPLE = "x"
    DAYS = "days"
    COUNT = "count"
    BPS = "bps"
    RATIO = "ratio"


class CompanyRef(BaseModel):
    """Identity block echoed by every analysis endpoint."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    ticker: str
    exchange: str
    sector: str | None = None


class MetricRow(BaseModel):
    """One labelled metric across the reported periods.

    A row is the unit of rendering: the frontend receives label, unit and an
    ordered value list, and draws it. It performs no calculation and applies no
    financial knowledge of its own.
    """

    key: str
    label: str
    unit: str = Unit.CRORE
    values: list[float | None]
    #: Emphasised as a subtotal/total line in the grid.
    is_subtotal: bool = False
    #: Rendered as a section heading rather than data.
    is_header: bool = False
    #: Indent depth for hierarchical statements.
    indent: int = 0
    #: Optional per-row explanation surfaced as a tooltip.
    note: str | None = None


class MetricSection(BaseModel):
    """A titled group of metric rows."""

    key: str
    title: str
    rows: list[MetricRow] = Field(default_factory=list)


class PeriodMeta(BaseModel):
    """Which periods the payload covers."""

    fiscal_years: list[int]
    labels: list[str]
    latest_fiscal_year: int | None = None
    currency: str = "INR"
    unit: str = Unit.CRORE


class AnalysisResponse(BaseModel):
    """Envelope shared by every Module 2 endpoint."""

    company: CompanyRef
    periods: PeriodMeta
    sections: list[MetricSection]
    has_data: bool = True
    warnings: list[str] = Field(default_factory=list)


class Flag(BaseModel):
    """A diagnostic signal derived from the numbers."""

    key: str
    label: str
    triggered: bool
    severity: str = "info"  # info | warn | alert
    detail: str | None = None
