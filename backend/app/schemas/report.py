"""Typed contracts for the report API."""
from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from app.domain.reports.blocks import ReportType, SectionKey, Theme
from app.services.reports.renderers.base import OutputFormat


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class GenerateRequest(BaseModel):
    company_id: str = Field(min_length=1, max_length=36)
    report_type: ReportType = ReportType.INSTITUTIONAL
    formats: list[OutputFormat] = Field(
        default_factory=lambda: [OutputFormat.HTML, OutputFormat.PDF]
    )
    theme: Theme = Theme.LIGHT
    analyst: str = Field(default="", max_length=120)
    portfolio_id: int | None = None
    #: AI narratives are model round-trips; a caller wanting only the numbers
    #: can skip them and save most of the generation cost.
    include_ai: bool = True
    use_cache: bool = True


class ArtifactOut(ORMModel):
    id: int
    fmt: str
    filename: str
    media_type: str
    size_bytes: int
    page_count: int | None = None
    render_ms: float


class ReportOut(ORMModel):
    id: int
    company_id: str
    ticker: str
    company_name: str
    owner_id: str
    report_type: str
    title: str
    theme: str
    version: int
    superseded_by: int | None = None
    status: str
    error: str | None = None
    analyst: str | None = None
    portfolio_id: int | None = None

    section_count: int
    insufficient_count: int
    block_count: int
    chart_count: int
    table_count: int
    evidence_count: int
    word_count: int

    citation_coverage: float
    citation_clean: bool
    audit: dict | None = None
    provenance: dict | None = None
    build_ms: float
    generated_at: datetime | None = None
    created_at: datetime | None = None

    artifacts: list[ArtifactOut] = Field(default_factory=list)


class SectionOut(BaseModel):
    key: str
    title: str
    sufficient: bool
    reason: str = ""
    block_count: int
    chart_count: int
    table_count: int
    evidence_count: int
    word_count: int


class ReportDetailOut(ReportOut):
    sections: list[SectionOut] = Field(default_factory=list)
    #: The full block tree, for the in-app preview.
    document: dict | None = None


class GenerateResponse(BaseModel):
    report: ReportOut
    cached: bool
    timings: dict[str, float] = Field(default_factory=dict)
    #: Engine failures during gathering. A report can be complete and still
    #: have these — the affected sections say "Insufficient evidence".
    errors: dict[str, str] = Field(default_factory=dict)
    message: str


class JobOut(ORMModel):
    id: int
    report_id: int
    owner_id: str
    status: str
    stage: str
    progress: float
    attempts: int
    error: str | None = None
    duration_ms: float
    timings: dict | None = None


class StatisticsOut(BaseModel):
    reports: int
    current: int
    superseded: int
    artifacts: int
    bytes_stored: int
    by_type: dict[str, int] = Field(default_factory=dict)
    by_format: dict[str, int] = Field(default_factory=dict)
    citation_clean: int
    mean_coverage: float
    mean_build_ms: float


class ReportTypeOut(BaseModel):
    key: str
    label: str
    sections: list[str]
    narratives: list[str]


class CapabilitiesOut(BaseModel):
    report_types: list[ReportTypeOut]
    formats: list[dict]
    sections: list[str]
    chart_kinds: list[str]
    themes: list[str]
    evidence_sources: list[str]
    block_kinds: list[str]
