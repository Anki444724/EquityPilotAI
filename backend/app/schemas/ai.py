"""API contracts for the AI layer."""
from __future__ import annotations

from pydantic import BaseModel, Field

from app.domain.ai.sourcing import SourceScope

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


class DataQualityOut(BaseModel):
    """Data-quality context attached to every AI response.

    Present on every answer, not only poor ones. A reader who sees the score
    only when it is bad learns to treat its absence as reassurance, which is
    exactly the inference the field exists to prevent.
    """

    score: float = Field(description="0-100")
    grade: str
    #: Days since the newest evidence of any kind for this company.
    knowledge_freshness_days: int | None = None
    #: Set when the score is below the warning threshold. The AI layer also
    #: prepends the warning to `display_content`, so a client that ignores
    #: this field still shows it to the user.
    warning: str | None = None
    missing_items: list[str] = Field(default_factory=list)


class DetectionOut(BaseModel):
    language: str
    confidence: float
    script: str
    reason: str
    is_mixed: bool = False
    ambiguous_with: list[str] = Field(default_factory=list)


class TranslationOut(BaseModel):
    language: str
    translated: bool
    provider: str
    detail: str = ""
    latency_ms: float = 0.0
    cost_usd: float = 0.0
    integrity_problems: list[str] = Field(default_factory=list)


class LanguageOut(BaseModel):
    """How the response language was chosen, and what happened."""

    language: str
    label: str
    native_label: str
    script: str
    bcp47: str
    #: "requested" | "detected" | "preference".
    resolved_from: str
    detected: DetectionOut
    translation: TranslationOut
    latency_ms: float = 0.0


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
    providers_attempted: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    #: Never optional in practice — populated for every response so a client
    #: can always state how good the underlying data is.
    data_quality: DataQualityOut | None = None
    #: Present only when the response went through the Language Adapter, i.e.
    #: when a non-English language was requested or detected. Absent on the
    #: English path, which keeps every existing client's payload byte-for-byte
    #: unchanged.
    language: LanguageOut | None = None


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
    #: "auto" | "english" | "hindi" | "hinglish" | a BCP-47 tag.
    #: Defaults to auto-detection, so an existing client that never sets it
    #: gets English for English questions exactly as before.
    language: str = "auto"
    provider: str | None = None
    stream: bool = False
    #: Restrict which evidence may answer. Omitted means the scope is read
    #: from the question, so "use ONLY uploaded documents" typed into a chat
    #: box is honoured exactly as the explicit parameter would be.
    source: SourceScope | None = None
    #: Wording to return verbatim when the requested source has no evidence.
    #: An integration branching on that string must be able to rely on it.
    refusal_text: str | None = Field(default=None, max_length=300)


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
    language: str = "auto"


class ReportRequest(BaseModel):
    """A multi-section research report."""

    capabilities: list[str] = Field(default_factory=list)
    style: str = "report_section"
    provider: str | None = None
    language: str = "auto"


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


class LanguageSpecOut(BaseModel):
    code: str
    label: str
    native_label: str
    script: str
    status: str
    bcp47: str
    keeps_english_terms: bool = False


class LanguageListResponse(BaseModel):
    """The language registry, including languages not yet enabled."""

    languages: list[LanguageSpecOut] = Field(default_factory=list)
    default: str = "auto"
    canonical: str = "english"
    supported: list[str] = Field(default_factory=list)
    planned: list[str] = Field(default_factory=list)
    glossary: dict = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)


class DetectRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


class DetectResponse(BaseModel):
    detected: DetectionOut
    #: The English query the retriever would actually receive.
    normalised_query: str
    rewritten: bool = False
    mapped_terms: list[dict] = Field(default_factory=list)
