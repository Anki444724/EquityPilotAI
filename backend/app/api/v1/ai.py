"""AI endpoints.

    GET  /ai/capabilities                     the 17 analyst capabilities
    GET  /ai/providers                        provider registry and status
    GET  /ai/usage                            token and cost accounting
    GET  /ai/prompts                          the versioned prompt library
    PUT  /ai/prompts/{key}                    save a new prompt version
    POST /ai/prompts/{key}/activate           roll back to a version
    POST /company/{ticker}/ai/analyse         run one capability
    POST /company/{ticker}/ai/chat            grounded conversation
    POST /company/{ticker}/ai/chat/stream     streaming conversation
    POST /company/{ticker}/ai/report          multi-section research report
    GET  /company/{ticker}/ai/history         previously generated analyses
    GET  /company/{ticker}/ai/context         the grounded evidence itself
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.api.v1.analysis import get_analysis
from app.core.config import settings
from app.core.security import CurrentUser, get_current_user
from app.db.base import get_db
from app.domain.ai.types import NoProviderConfigured, ProviderError
from app.schemas.ai import (
    AnalysisRequest, AnalysisResponse, CapabilityListResponse, CapabilityOut,
    ChatRequest, ChatResponse, CitationAuditOut, CitationOut, ClaimBlockOut,
    GuardrailOut, PromptActivateRequest, PromptListResponse, PromptOut,
    PromptUpdateRequest, ProviderListResponse, ProviderOut, ReportRequest,
    ReportResponse, ReportSection, UsageResponse,
)
from app.services.ai.analyst import AnalystResult
from app.services.ai.guardrails import DISCLOSURE
from app.services.ai.prompt_library import BUILTIN_PROMPTS, OutputStyle
from app.services.ai.service import AIError, AIService
from app.services.analysis_service import AnalysisService

router = APIRouter(tags=["ai"])

#: Default report layout when the caller does not specify sections.
DEFAULT_REPORT = [
    "business_summary", "investment_thesis", "moat_analysis",
    "risk_analysis", "valuation_commentary", "scoring_explanation",
]


def _service(db: Session = Depends(get_db)) -> AIService:
    return AIService(db)


def _result_out(analysis: AnalysisService, result: AnalystResult) -> AnalysisResponse:
    audit = result.citation_audit
    guardrails = result.guardrails
    return AnalysisResponse(
        company=analysis.company_ref(),
        capability=result.capability, content=result.content,
        display_content=result.display_content,
        provider=result.provider, model=result.model,
        prompt_key=result.prompt_key, prompt_version=result.prompt_version,
        citations=[
            CitationOut(
                key=c.key, label=c.label, kind=c.kind.value, value=c.value,
                unit=c.unit, source=c.source, fiscal_year=c.fiscal_year,
                document_id=c.document_id, chunk_id=c.chunk_id, page=c.page,
                confidence=c.confidence, snippet=c.snippet,
            )
            for c in result.citations
        ],
        citation_audit=CitationAuditOut(
            resolved_count=len(audit.resolved), unknown_keys=audit.unknown_keys,
            uncited_numbers=audit.uncited_numbers, coverage=audit.coverage,
            is_supported=audit.is_supported, summary=audit.summary,
        ) if audit else None,
        guardrails=GuardrailOut(
            passed=guardrails.passed, violations=guardrails.violations,
            disclosure=guardrails.disclosure,
            composition=guardrails.composition(),
            blocks=[
                ClaimBlockOut(text=b.text, claim_type=b.claim_type.value,
                              has_citation=b.has_citation, hedged=b.hedged)
                for b in guardrails.blocks
            ],
        ) if guardrails else None,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
        total_tokens=result.total_tokens, cost_usd=result.cost_usd,
        latency_ms=result.latency_ms, cached=result.cached,
        fell_back_from=result.fell_back_from, warnings=result.warnings,
    )


def _require_data(analysis: AnalysisService) -> None:
    if not analysis.has_data:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "no financial data; the AI analyst has nothing to reason over",
        )


# ------------------------------------------------------------------ catalogue
@router.get("/ai/capabilities", response_model=CapabilityListResponse,
            summary="Analyst capabilities")
def list_capabilities(
    service: AIService = Depends(_service),
    _: CurrentUser = Depends(get_current_user),
) -> CapabilityListResponse:
    return CapabilityListResponse(
        capabilities=[
            CapabilityOut(
                key=p.key, label=p.label, description=p.description,
                evidence_kinds=[k.value for k in p.evidence],
                style=p.style.value, version=p.version,
            )
            for p in BUILTIN_PROMPTS.values()
        ],
        providers_available=service.router.available,
        ai_enabled=bool(service.router.available),
    )


@router.get("/ai/providers", response_model=ProviderListResponse,
            summary="Provider registry and status")
def list_providers(
    service: AIService = Depends(_service),
    _: CurrentUser = Depends(get_current_user),
) -> ProviderListResponse:
    return ProviderListResponse(
        providers=[
            ProviderOut(
                name=c.name, payload_shape=c.payload_shape,
                default_model=c.default_model, configured=c.configured,
                endpoint=c.endpoint.split("?")[0],
            )
            for c in service.router.configs
        ],
        preferred=settings.AI_PREFERRED_PROVIDER,
        ai_enabled=bool(service.router.available),
    )


@router.get("/ai/health", summary="Live provider reachability")
async def provider_health(
    service: AIService = Depends(_service),
    _: CurrentUser = Depends(get_current_user),
) -> dict[str, object]:
    """Probe each configured provider with a one-token completion.

    `/ai/providers` reports what is *configured*; this reports what actually
    answers. The distinction matters in production: a key can be present,
    correctly formatted and completely out of quota, and only a real call
    tells them apart. That is not hypothetical — the Gemini key this platform
    ships against authenticates fine and returns 429 on every generation
    because its free-tier daily allowance is spent.

    Never returns a key, a URL with a query string, or a provider error body
    verbatim — only a status, a latency and a short reason.
    """
    import time as _time

    from app.domain.ai.types import CompletionRequest, Message, RateLimitError, Role

    probe = CompletionRequest(
        messages=[Message(Role.USER, "ok")], model=None,
        temperature=0.0, max_tokens=1,
    )

    results: list[dict[str, object]] = []
    for config in service.router.chain():
        if config.payload_shape == "offline":
            results.append({
                "provider": config.name, "status": "ready",
                "detail": "deterministic offline provider", "latency_ms": 0.0,
            })
            continue

        started = _time.perf_counter()
        try:
            await service.router.build(config).complete(probe)
            status, detail = "ok", "completion succeeded"
        except RateLimitError as exc:
            status = "quota_exhausted" if exc.quota_exhausted else "rate_limited"
            detail = "allowance spent" if exc.quota_exhausted else "throttled"
        except Exception as exc:  # noqa: BLE001 — a probe must never 500
            status = "error"
            detail = type(exc).__name__
        results.append({
            "provider": config.name, "status": status, "detail": detail,
            "latency_ms": round((_time.perf_counter() - started) * 1000, 1),
        })

    healthy = [r for r in results if r["status"] in ("ok", "ready")]
    live = [r for r in results if r["status"] == "ok"]
    return {
        "chain": [c.name for c in service.router.chain()],
        "providers": results,
        "degraded": not live,
        "serving": healthy[0]["provider"] if healthy else None,
    }


@router.get("/ai/usage", response_model=UsageResponse, summary="Token and cost accounting")
def usage(
    service: AIService = Depends(_service),
    user: CurrentUser = Depends(get_current_user),
) -> UsageResponse:
    return UsageResponse(**service.usage_summary())


# ------------------------------------------------------------ prompt library
@router.get("/ai/prompts", response_model=PromptListResponse,
            summary="Versioned prompt library")
def list_prompts(
    active_only: bool = Query(True),
    service: AIService = Depends(_service),
    _: CurrentUser = Depends(get_current_user),
) -> PromptListResponse:
    return PromptListResponse(prompts=[
        PromptOut(
            key=p.key, version=p.version, label=p.label, description=p.description,
            task=p.task, template=p.template, evidence=p.evidence or [],
            style=p.style, max_tokens=p.max_tokens, temperature=p.temperature,
            is_active=p.is_active, is_builtin=p.is_builtin,
        )
        for p in service.list_prompts(active_only=active_only)
    ])


@router.put("/ai/prompts/{key}", response_model=PromptOut,
            summary="Save a new prompt version")
def update_prompt(
    key: str,
    body: PromptUpdateRequest,
    service: AIService = Depends(_service),
    user: CurrentUser = Depends(get_current_user),
) -> PromptOut:
    try:
        record = service.save_prompt_version(
            key, task=body.task, template=body.template, label=body.label,
            max_tokens=body.max_tokens, temperature=body.temperature,
            editor=user.id,
        )
    except AIError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    return PromptOut(
        key=record.key, version=record.version, label=record.label,
        description=record.description, task=record.task, template=record.template,
        evidence=record.evidence or [], style=record.style,
        max_tokens=record.max_tokens, temperature=record.temperature,
        is_active=record.is_active, is_builtin=record.is_builtin,
    )


@router.post("/ai/prompts/{key}/activate", response_model=PromptOut,
             summary="Roll back to an earlier prompt version")
def activate_prompt(
    key: str,
    body: PromptActivateRequest,
    service: AIService = Depends(_service),
    _: CurrentUser = Depends(get_current_user),
) -> PromptOut:
    try:
        record = service.activate_version(key, body.version)
    except AIError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return PromptOut(
        key=record.key, version=record.version, label=record.label,
        description=record.description, task=record.task, template=record.template,
        evidence=record.evidence or [], style=record.style,
        max_tokens=record.max_tokens, temperature=record.temperature,
        is_active=record.is_active, is_builtin=record.is_builtin,
    )


# --------------------------------------------------------------------- analyse
@router.post("/company/{ticker}/ai/analyse", response_model=AnalysisResponse,
             summary="Run one analyst capability")
async def analyse(
    body: AnalysisRequest,
    analysis: AnalysisService = Depends(get_analysis),
    service: AIService = Depends(_service),
    user: CurrentUser = Depends(get_current_user),
) -> AnalysisResponse:
    _require_data(analysis)
    if body.capability not in BUILTIN_PROMPTS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            f"unknown capability '{body.capability}'")

    analyst = service.analyst_for(analysis)
    template = service.get_active_prompt(body.capability)
    style = OutputStyle(body.style) if body.style else None

    try:
        result = await analyst.run(
            body.capability, question=body.question, style=style,
            provider=body.provider, template=template,
        )
    except NoProviderConfigured as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    except ProviderError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    if body.save:
        service.record(analysis.company.id, result, owner=user.id)
    return _result_out(analysis, result)


# ------------------------------------------------------------------------ chat
@router.post("/company/{ticker}/ai/chat", response_model=ChatResponse,
             summary="Grounded conversation with memory")
async def chat(
    body: ChatRequest,
    analysis: AnalysisService = Depends(get_analysis),
    service: AIService = Depends(_service),
    user: CurrentUser = Depends(get_current_user),
) -> ChatResponse:
    _require_data(analysis)
    memory = service.memory(f"{user.id}:{body.session_id}")
    memory.set_company(analysis.company.id, analysis.company.ticker,
                       analysis.company.name)

    analyst = service.analyst_for(analysis)
    try:
        result = await analyst.chat(body.question, memory, provider=body.provider)
    except NoProviderConfigured as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    except ProviderError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    base = _result_out(analysis, result)
    return ChatResponse(
        **base.model_dump(), session_id=body.session_id,
        turn_count=memory.turn_count, session_state=memory.state_summary(),
    )


@router.post("/company/{ticker}/ai/chat/stream", summary="Streaming conversation")
async def chat_stream(
    body: ChatRequest,
    analysis: AnalysisService = Depends(get_analysis),
    service: AIService = Depends(_service),
    user: CurrentUser = Depends(get_current_user),
):
    _require_data(analysis)
    memory = service.memory(f"{user.id}:{body.session_id}")
    memory.set_company(analysis.company.id, analysis.company.ticker,
                       analysis.company.name)
    analyst = service.analyst_for(analysis)

    async def events():
        try:
            async for token in analyst.stream_chat(body.question, memory):
                yield f"data: {json.dumps({'token': token})}\n\n"
        except NoProviderConfigured as exc:
            yield f"data: {json.dumps({'error': str(exc)})}\n\n"
        yield f"data: {json.dumps({'done': True})}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")


# ---------------------------------------------------------------------- report
@router.post("/company/{ticker}/ai/report", response_model=ReportResponse,
             summary="Multi-section research report")
async def report(
    body: ReportRequest,
    analysis: AnalysisService = Depends(get_analysis),
    service: AIService = Depends(_service),
    user: CurrentUser = Depends(get_current_user),
) -> ReportResponse:
    _require_data(analysis)
    capabilities = body.capabilities or DEFAULT_REPORT
    unknown = [c for c in capabilities if c not in BUILTIN_PROMPTS]
    if unknown:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            f"unknown capabilities: {unknown}")

    analyst = service.analyst_for(analysis)
    try:
        results = await analyst.run_many(capabilities, provider=body.provider)
    except NoProviderConfigured as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc

    sections = [
        ReportSection(
            capability=r.capability,
            label=BUILTIN_PROMPTS[r.capability].label if r.capability in BUILTIN_PROMPTS
            else r.capability,
            content=r.content,
            citations=[
                CitationOut(key=c.key, label=c.label, kind=c.kind.value,
                            value=c.value, unit=c.unit, source=c.source,
                            fiscal_year=c.fiscal_year,
                            document_id=c.document_id, chunk_id=c.chunk_id,
                            page=c.page, confidence=c.confidence,
                            snippet=c.snippet)
                for c in r.citations
            ],
            is_supported=r.is_supported, warnings=r.warnings,
        )
        for r in results
    ]
    for result in results:
        if result.content:
            service.record(analysis.company.id, result, owner=user.id)

    providers = {r.provider for r in results if r.provider != "none"}
    return ReportResponse(
        company=analysis.company_ref(), sections=sections,
        total_tokens=sum(r.total_tokens for r in results),
        total_cost_usd=round(sum(r.cost_usd for r in results), 6),
        generated_with=", ".join(sorted(providers)) or "none",
        disclosure=DISCLOSURE,
    )


# --------------------------------------------------------------------- context
@router.get("/company/{ticker}/ai/context", summary="The grounded evidence")
def context(
    analysis: AnalysisService = Depends(get_analysis),
    service: AIService = Depends(_service),
    _: CurrentUser = Depends(get_current_user),
) -> dict:
    """Exactly what the model is permitted to see. Useful for auditing."""
    analyst = service.analyst_for(analysis)
    grounded = analyst.context()
    return {
        "company": {"ticker": grounded.ticker, "name": grounded.name},
        "citation_count": len(grounded.citations),
        "unavailable": grounded.unavailable,
        "citations": [
            {
                "key": c.key, "label": c.label, "kind": c.kind.value,
                "value": c.value, "unit": c.unit, "source": c.source,
                "rendered": c.render(),
            }
            for c in grounded.citations
        ],
    }


@router.get("/company/{ticker}/ai/history", summary="Previously generated analyses")
def history(
    capability: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    analysis: AnalysisService = Depends(get_analysis),
    service: AIService = Depends(_service),
    _: CurrentUser = Depends(get_current_user),
) -> dict:
    records = service.history(analysis.company.id, capability, limit)
    return {
        "company": {"ticker": analysis.company.ticker},
        "analyses": [
            {
                "capability": r.capability, "provider": r.provider,
                "model": r.model, "prompt_version": r.prompt_version,
                "is_supported": r.is_supported,
                "citation_coverage": r.citation_coverage,
                "total_tokens": r.prompt_tokens + r.completion_tokens,
                "cost_usd": r.cost_usd,
                "created_at": str(r.created_at),
                "content": r.content,
            }
            for r in records
        ],
    }
