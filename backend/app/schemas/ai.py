"""API contracts for the AI layer."""
from __future__ import annotations

from pydantic import BaseModel, Field

from app.schemas.common import CompanyRef


class CitationOut(BaseModel):
    key: str
    label: str
    kind: str
    value: float | str | None = None
    unit: str = ""
    source: str = ""
    fiscal_year: int | None = None

    # Retrieval provenance, present only on document evidence. A claim about
    # narrative prose is auditable only if the reader can reach the exact
    # paragraph: the page alone is not enough when a page holds several.
    document_id: int | None = None
    chunk_id: int | None = None
    page: int | None = None
    confidence: float | None = None
    snippet: str | None = None


class ClaimBlockOut(BaseModel):
    text: str
    claim_type: str
    has_citation: bool
    hedged: bool


class GuardrailOut(BaseModel):
    passed: bool
    violations: list[str] = Field(default_factory=list)
    disclosure: str
    composition: dict[str, int] = Field(default_factory=dict)
    blocks: list[ClaimBlockOut] = Field(default_factory=list)


class CitationAuditOut(BaseModel):
    resolved_count: int
    unknown_keys: list[str] = Field(default_factory=list)
    uncited_numbers: list[str] = Field(default_factory=list)
    coverage: float
    is_supported: bool
    summary: str


class AnalysisResponse(BaseModel):
    company: CompanyRef
    capability: str
    content: str
    display_content: str
    provider: str
    model: str
    prompt_key: str
    prompt_version: int
    citations: list[CitationOut] = Field(default_factory=list)
    citation_audit: CitationAuditOut | None = None
    guardrails: GuardrailOut | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    cached: bool = False
    fell_back_from: str | None = None
    warnings: list[str] = Field(default_factory=list)


class CapabilityOut(BaseModel):
    key: str
    label: str
    description: str
    evidence_kinds: list[str] = Field(default_factory=list)
    style: str
    version: int


class CapabilityListResponse(BaseModel):
    capabilities: list[CapabilityOut]
    providers_available: list[str]
    ai_enabled: bool


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    session_id: str = Field(default="default", max_length=64)
    provider: str | None = None
    stream: bool = False


class ChatResponse(AnalysisResponse):
    session_id: str
    turn_count: int
    session_state: str = ""


class AnalysisRequest(BaseModel):
    capability: str
    provider: str | None = None
    style: str | None = None
    question: str = ""
    save: bool = True


class ReportRequest(BaseModel):
    """A multi-section research report."""

    capabilities: list[str] = Field(default_factory=list)
    style: str = "report_section"
    provider: str | None = None


class ReportSection(BaseModel):
    capability: str
    label: str
    content: str
    citations: list[CitationOut] = Field(default_factory=list)
    is_supported: bool = False
    warnings: list[str] = Field(default_factory=list)


class ReportResponse(BaseModel):
    company: CompanyRef
    sections: list[ReportSection] = Field(default_factory=list)
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    generated_with: str = ""
    disclosure: str = ""


class PromptOut(BaseModel):
    key: str
    version: int
    label: str
    description: str | None = None
    task: str
    template: str
    evidence: list[str] = Field(default_factory=list)
    style: str
    max_tokens: int
    temperature: float
    is_active: bool
    is_builtin: bool


class PromptListResponse(BaseModel):
    prompts: list[PromptOut]


class PromptUpdateRequest(BaseModel):
    task: str | None = None
    template: str | None = None
    label: str | None = None
    max_tokens: int | None = Field(None, ge=100, le=8000)
    temperature: float | None = Field(None, ge=0.0, le=1.5)


class PromptActivateRequest(BaseModel):
    version: int = Field(ge=1)


class UsageResponse(BaseModel):
    persisted: dict
    session: dict
    providers_available: list[str]


class ProviderOut(BaseModel):
    name: str
    payload_shape: str
    default_model: str
    configured: bool
    endpoint: str


class ProviderListResponse(BaseModel):
    providers: list[ProviderOut]
    preferred: str | None = None
    ai_enabled: bool
