"""The AI Research Analyst — orchestration.

Ties the layer together: ground the question in platform data, assemble a
versioned prompt, route it to a provider, then verify what comes back.

The post-generation verification is the part that distinguishes this from a
chatbot wrapper. A response is not returned raw; it is audited against the
evidence that was supplied, classified into fact / model output /
interpretation / opinion, and annotated with its own support level. A confident
answer that cites nothing is surfaced as exactly that.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import structlog

from app.domain.ai.types import (
    Citation, ClaimType, CompletionResponse, EvidenceKind, NoProviderConfigured,
    Role,
)
from app.domain.ai.sourcing import (
    SCOPE_KINDS, SourceDirective, SourceScope, parse_directive,
)
from app.services.ai.citation_engine import CitationAudit, annotate, audit
from app.services.ai.context_builder import ContextBuilder, GroundedContext
from app.services.ai.guardrails import GuardrailReport, check, enforce
from app.services.ai.memory import ConversationMemory
from app.services.ai.prompt_builder import BuiltPrompt, PromptBuilder
from app.services.ai.prompt_library import (
    BUILTIN_PROMPTS, Capability, OutputStyle, PromptTemplate, get_prompt,
)
from app.services.ai.providers.router import ProviderRouter

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class AnalystResult:
    """A completed analysis, with everything needed to judge its reliability."""

    capability: str
    content: str
    #: Content with citation keys replaced by human labels.
    display_content: str
    provider: str
    model: str
    prompt_key: str
    prompt_version: int

    citations: list[Citation] = field(default_factory=list)
    citation_audit: CitationAudit | None = None
    guardrails: GuardrailReport | None = None

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    cached: bool = False
    fell_back_from: str | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def is_supported(self) -> bool:
        return bool(self.citation_audit and self.citation_audit.is_supported)


class ResearchAnalyst:
    """Runs grounded analyses and conversation."""

    def __init__(
        self,
        builder: ContextBuilder,
        router: ProviderRouter | None = None,
        prompt_builder: PromptBuilder | None = None,
    ) -> None:
        self.builder = builder
        self.router = router or ProviderRouter()
        self.prompts = prompt_builder or PromptBuilder()
        self._context: GroundedContext | None = None

    def context(self, *, refresh: bool = False) -> GroundedContext:
        """Ground once per analyst instance; every capability reuses it."""
        if self._context is None or refresh:
            self._context = self.builder.build()
        return self._context

    # ------------------------------------------------------------- analysis
    async def run(
        self,
        capability: str,
        *,
        question: str = "",
        memory: ConversationMemory | None = None,
        style: OutputStyle | None = None,
        provider: str | None = None,
        template: PromptTemplate | None = None,
        source: SourceDirective | None = None,
    ) -> AnalystResult:
        """Produce one grounded analysis."""
        prompt_template = template or get_prompt(capability)
        context = self.context()

        # --- retrieval-augmented generation -----------------------------
        #
        # The missing link. `ContextBuilder._add_documents` contributes only
        # the handful of regex-extracted *fields* (headcount, principal risks)
        # and a one-line summary per document. It never touches the chunks, so
        # every word of narrative prose in an uploaded report — the chairman's
        # statement, the MD&A, the auditor's opinion — was invisible to the
        # model. A `document_search` tool existed and worked, and nothing ever
        # called it.
        #
        # Retrieval runs per question, before the prompt is built, because the
        # relevant passages depend on what was asked. Cached context cannot.
        retrieved = self._retrieve(question, capability)
        if retrieved:
            context = context.with_citations(retrieved)

        # --- source routing --------------------------------------------
        #
        # A restriction is a claim about provenance, and answering from a
        # different source is not a partial answer — it is the wrong answer
        # wearing the right clothes. Because every figure in it is real and
        # correctly cited, nothing downstream flags it, which is what makes
        # this failure mode worth a hard control rather than a prompt
        # instruction.
        #
        # Enforced by *removing* inadmissible evidence before the prompt is
        # built. Asking the model to ignore what it can see is not a control.
        directive = source or parse_directive(question)
        if directive.scope.is_restricted:
            context = context.restricted_to(SCOPE_KINDS[directive.scope])
            log.info(
                "source scope applied",
                scope=directive.scope.value, inferred=directive.inferred,
                admitted_evidence=len(context.citations),
                capability=capability,
            )
            if not context.citations:
                # Fail closed. No provider call at all: a model handed an
                # empty context and told not to invent will usually comply,
                # and "usually" is not a guarantee worth shipping.
                log.info(
                    "source scope unsatisfied — refusing",
                    scope=directive.scope.value, question=question[:160],
                )
                return self._refuse(
                    capability, directive, context, memory, question,
                )

        built = self.prompts.build(
            prompt_template, context, question=question, memory=memory, style=style,
            include_history=capability == Capability.CHAT.value,
        )

        log.info(
            "ai prompt assembled",
            capability=capability,
            question=question[:160] or None,
            retrieved_chunks=len(retrieved),
            total_evidence=len(context.citations),
            prompt_chars=sum(len(m.content or "") for m in built.request.messages),
        )

        started = time.perf_counter()
        response = await self.router.complete(built.request, preferred=provider)
        elapsed = (time.perf_counter() - started) * 1000

        return self._finalise(
            capability, built, response, context, elapsed, memory, question
        )

    #: Passages fetched per question. Ten is the brief's figure and comfortably
    #: within the context window: ten 500-character passages is ~1,250 tokens.
    RETRIEVAL_TOP_K = 10

    def _retrieve(self, question: str, capability: str) -> list[Citation]:
        """Fetch the passages that bear on this question.

        Returns citations carrying the chunk id, page and retrieval score, so
        the answer can be audited back to the exact paragraph rather than to a
        document as a whole.
        """
        if not question.strip():
            # A fixed capability (bull_case, swot…) has no query to retrieve
            # against; those are served from the computed context as before.
            return []

        service = getattr(self.builder, "document_service", None)
        if service is None:
            return []

        company = self.builder.analysis.company
        try:
            answer = service.search(
                question, company_id=company.id, top_k=self.RETRIEVAL_TOP_K,
            )
        except Exception:  # noqa: BLE001 — retrieval must never break a chat
            log.exception("document retrieval failed", company_id=company.id)
            return []

        hits = list(getattr(answer, "hits", []) or [])
        log.info(
            "document retrieval",
            company_id=company.id, ticker=company.ticker,
            question=question[:160], top_k=self.RETRIEVAL_TOP_K,
            hits=len(hits),
            chunks=[
                {"chunk_id": h.chunk_id, "page": h.page,
                 "score": round(h.score, 4),
                 "lexical": round(h.lexical_score, 4),
                 "semantic": round(h.semantic_score, 4),
                 "preview": h.text[:80]}
                for h in hits[:self.RETRIEVAL_TOP_K]
            ],
        )
        if not hits:
            log.info(
                "no document evidence", company_id=company.id,
                question=question[:160],
            )
            return []

        citations: list[Citation] = []
        for index, hit in enumerate(hits, start=1):
            section = hit.section.value.replace("_", " ")
            # Collapse whitespace. The evidence block is parsed line by line —
            # `[key] label: value — source: …` — so a passage containing the
            # newlines every PDF paragraph carries silently fails to match and
            # the model never sees it. That is precisely how ten successfully
            # retrieved passages produced an answer citing none of them.
            passage = " ".join((hit.text or "").split())
            citations.append(Citation(
                key=f"doc_p{hit.page}_c{hit.chunk_id}",
                # The category, not just the document name. A reader needs to
                # know they are being shown an annual report rather than an
                # aggregator's summary — those carry very different weight.
                label=f"[Annual Report] {hit.document_title} p.{hit.page}",
                kind=EvidenceKind.DOCUMENT,
                # The passage itself is the value: a citation whose value were
                # a score would give the model nothing to quote.
                value=passage[:600],
                unit="",
                source=(
                    f"{hit.document_title}, page {hit.page}"
                    + (f", {section}" if section != "unknown" else "")
                ),
                document_id=hit.document_id,
                chunk_id=hit.chunk_id,
                page=hit.page,
                confidence=round(hit.score, 4),
                snippet=passage,
            ))
        return citations

    def _refuse(
        self,
        capability: str,
        directive: SourceDirective,
        context: GroundedContext,
        memory: ConversationMemory | None,
        question: str,
    ) -> AnalystResult:
        """Decline, in the caller's own words, without calling a provider.

        Returned verbatim when the caller specified exact wording, because an
        integration that branches on that string must be able to rely on it.
        No provider is consulted: a model given an empty context and told not
        to invent will usually comply, and "usually" is the wrong standard for
        a control whose entire purpose is to prevent invention.
        """
        text = directive.refusal_text
        if memory is not None:
            memory.add(Role.USER, question)
            memory.add(Role.ASSISTANT, text)
        return AnalystResult(
            capability=capability,
            content=text,
            display_content=text,
            # Named honestly. This did not come from a model, and reporting a
            # provider that was never called would corrupt the usage figures.
            provider="source-router",
            model="none",
            prompt_key=capability,
            prompt_version=0,
            citations=[],
            citation_audit=None,
            guardrails=None,
            warnings=[
                f"Restricted to {directive.scope.value}; no evidence from that "
                "source bears on the question. No other source was consulted."
            ],
        )

    def _finalise(
        self,
        capability: str,
        built: BuiltPrompt,
        response: CompletionResponse,
        context: GroundedContext,
        elapsed_ms: float,
        memory: ConversationMemory | None,
        question: str,
    ) -> AnalystResult:
        """Verify, classify and record."""
        citation_audit = audit(response.content, built.citations)
        guardrails = check(response.content, citation_audit)
        content = enforce(response.content, guardrails)

        warnings: list[str] = list(guardrails.violations)
        if not citation_audit.is_supported:
            warnings.append(citation_audit.summary)
        if citation_audit.uncited_numbers:
            warnings.append(
                f"{len(citation_audit.uncited_numbers)} figure(s) in the answer do "
                "not match any platform evidence."
            )
        if context.unavailable:
            warnings.append(
                f"{len(context.unavailable)} data source(s) were unavailable when "
                "this was generated."
            )

        if memory is not None:
            if question:
                memory.add(Role.USER, question)
            memory.add(
                Role.ASSISTANT, response.content,
                citations=[c.key for c in citation_audit.resolved],
            )

        return AnalystResult(
            capability=capability,
            content=content,
            display_content=annotate(content, built.citations),
            provider=response.provider, model=response.model,
            prompt_key=built.prompt_key, prompt_version=built.prompt_version,
            citations=citation_audit.resolved,
            citation_audit=citation_audit, guardrails=guardrails,
            prompt_tokens=response.usage.prompt_tokens or built.approx_prompt_tokens,
            completion_tokens=response.usage.completion_tokens,
            cost_usd=response.cost_usd, latency_ms=elapsed_ms,
            cached=response.cached, fell_back_from=response.fell_back_from,
            warnings=warnings,
        )

    # ------------------------------------------------------------------ chat
    async def chat(
        self,
        question: str,
        memory: ConversationMemory,
        *,
        provider: str | None = None,
        source: SourceDirective | None = None,
    ) -> AnalystResult:
        return await self.run(
            Capability.CHAT.value, question=question, memory=memory,
            provider=provider, source=source,
        )

    async def stream_chat(self, question: str, memory: ConversationMemory):
        """Stream a chat answer token by token."""
        context = self.context()
        built = self.prompts.build(
            get_prompt(Capability.CHAT.value), context,
            question=question, memory=memory, include_history=True,
        )
        collected: list[str] = []
        async for token in self.router.stream(built.request):
            collected.append(token)
            yield token

        answer = "".join(collected)
        memory.add(Role.USER, question)
        memory.add(Role.ASSISTANT, answer)

    # ----------------------------------------------------------------- batch
    async def run_many(
        self, capabilities: list[str], *, provider: str | None = None
    ) -> list[AnalystResult]:
        """Run several capabilities against one grounding pass."""
        results: list[AnalystResult] = []
        for capability in capabilities:
            try:
                results.append(await self.run(capability, provider=provider))
            except NoProviderConfigured:
                raise
            except Exception as exc:  # a single failure must not sink the report
                results.append(AnalystResult(
                    capability=capability, content="", display_content="",
                    provider="none", model="none",
                    prompt_key=capability, prompt_version=0,
                    warnings=[f"Generation failed: {exc}"],
                ))
        return results
