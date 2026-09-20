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
from typing import TYPE_CHECKING

import structlog

from app.domain.ai.types import (
    Citation, ClaimType, CompletionResponse, EvidenceKind, NoProviderConfigured,
    Role,
)
from app.domain.ai.sourcing import (
    SCOPE_KINDS, SourceDirective, SourceScope, parse_directive,
)
from app.domain.language.types import CANONICAL_LANGUAGE, Language
from app.services.ai.citation_engine import CitationAudit, annotate, audit
from app.services.ai.context_builder import ContextBuilder, GroundedContext
from app.services.ai.financial_answer_engine import (
    DeterministicAnswer, FinancialAnswerEngine,
)
from app.services.ai.financial_intent import INVESTMENT_INTENTS, FinancialIntentResolver
from app.services.ai.investment_answer_engine import InvestmentAnswerEngine
from app.services.ai.guardrails import GuardrailReport, check, enforce
from app.services.ai.internal_composer import ComposedAnswer, InternalComposer
from app.services.ai.internal_open_ended import (
    InternalAnswer, InternalOpenEndedEngine,
)
from app.services.ai.internal_web_research import (
    InternalWebResearchEngine, WebResearchAnswer,
)
from app.services.ai.memory import ConversationMemory
from app.services.ai.planner import (
    EntityStatus, ExecutionRoute, QuestionPlan, QuestionPlanner,
)
from app.services.ai.prompt_builder import BuiltPrompt, PromptBuilder
from app.services.ai.prompt_library import (
    BUILTIN_PROMPTS, Capability, OutputStyle, PromptTemplate, get_prompt,
)
from app.services.ai.providers.router import ProviderRouter

if TYPE_CHECKING:  # pragma: no cover - annotation only
    from app.domain.documents.types import SearchHit

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

    #: Language metadata, populated by the Language Adapter. `None` means the
    #: response was produced on the English path and never went near the
    #: adapter — which is the default and costs nothing.
    language: dict | None = None

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def is_supported(self) -> bool:
        return bool(self.citation_audit and self.citation_audit.is_supported)


#: Sentence terminator shaved off a stored company name before comparison.
_NAME_NOISE = "."


def _normalise_identity(value: str | None) -> str:
    """A company identifier reduced to the form identity is compared in.

    Case, surrounding whitespace and a trailing period are typography rather
    than identity, so they are the only things removed. In particular two
    names that differ by a word — "Reliance Industries" and "Reliance
    Industries Ltd" — are deliberately NOT conflated here: this predicate
    decides whether an answer may be composed at all, so a false positive is
    a wrong-company answer, which is the one outcome the whole check exists
    to prevent.
    """
    return " ".join((value or "").casefold().split()).rstrip(_NAME_NOISE)


def _company_identity_is_safe(
    plan: QuestionPlan, context: GroundedContext,
) -> tuple[bool, str]:
    """Whether ``plan``'s subject may be answered from ``context``.

    The context is authoritative. It was bound to exactly one company by
    ``AnalysisService`` before this analyst existed, every citation in it
    belongs to that company, and the planner can neither change nor widen
    that binding — an ``EntityResolution`` is a claim about the *question*,
    never a re-binding of the evidence. This predicate is the single place
    the two are compared, and it is what makes "the answer was composed from
    the right company's data" structural rather than hopeful.

    Four cases, each fail-closed:

    * ``RESOLVED`` / ``CONTEXT_ONLY`` — the plan names a subject, so that
      subject must be the bound company, matched on company id, ticker or
      normalised name. Anything else is a mismatch and composition is
      refused: the planner may not retarget an answer onto a company the
      context does not carry.
    * ``AMBIGUOUS`` — the question named several companies and the planner
      deliberately did not choose. Composition is refused rather than
      resolved by guessing, because the guess is invisible in the output.
    * ``UNRESOLVED`` — nothing in the question identified a company. The
      bound company remains the subject, which is the rule the existing
      single-intent path has always applied: the resolver plays no part in
      company identity there either. A resolver *failure* lands here too,
      and is the reason it cannot retarget an answer — it reports that it
      identified nobody, and an identification of nobody cannot substitute
      a different company for the one the endpoint was scoped to.
    * a context carrying no company at all — nothing to validate against, so
      there is nothing safe to answer from.
    """
    # A context that does not identify a company cannot be validated, so it
    # cannot be composed from. This is the "grounded context exists" gate.
    if not (context.company_id or context.ticker):
        return False, "the grounded context does not identify a company"

    entity = plan.entity

    if entity.status is EntityStatus.AMBIGUOUS:
        return False, (
            "the question names more than one company and the planner does "
            "not choose between them"
        )

    if entity.status is EntityStatus.UNRESOLVED:
        return True, (
            "no company was identified in the question; the company bound to "
            "this analysis is authoritative"
        )

    for planned, bound, label in (
        (entity.company_id, context.company_id, "company id"),
        (entity.ticker, context.ticker, "ticker"),
        (entity.name, context.name, "company name"),
    ):
        if not (planned and bound):
            continue
        if _normalise_identity(planned) == _normalise_identity(bound):
            return True, f"the plan's company matches the bound context by {label}"

    return False, (
        "the plan's company does not match the company bound to this "
        f"analysis ({entity.ticker or entity.name or entity.company_id!r} "
        f"vs {context.ticker or context.name or context.company_id!r})"
    )


class ResearchAnalyst:
    """Runs grounded analyses and conversation."""

    def __init__(
        self,
        builder: ContextBuilder,
        router: ProviderRouter | None = None,
        prompt_builder: PromptBuilder | None = None,
        *,
        planner: QuestionPlanner | None = None,
        composer: InternalComposer | None = None,
        open_ended: InternalOpenEndedEngine | None = None,
        web_research: InternalWebResearchEngine | None = None,
    ) -> None:
        """Build an analyst over one company's grounded context.

        ``planner`` and ``composer`` are the Part 2C collaborators, injected
        rather than constructed here so that composition is an explicit
        choice at the composition root (``AIService.analyst_for``) and so a
        test can supply either without a database. They are built once per
        analyst, not once per question.

        ``open_ended`` is the Part 2D internal open-ended engine. It is
        injected on the same terms and shares the planner: one plan is
        computed per question and offered first to the composer and then,
        only if the composer declines, to this engine. Planning twice would
        cost a company-resolution pass and a language-normalisation pass for
        nothing, and two plans for one question could disagree.

        ``web_research`` is the Part 3 Phase 4D internal web research
        engine. Injected on the same terms and consulted last of the
        internal layers, for the ``WEB_RESEARCH`` route only: it answers a
        question about a current development from the platform's own web
        evidence (the stored-page index, then bounded discovery of the
        company's verified origins) and never from a provider.

        **All of them being absent is the default and means composition does
        not exist for this analyst**: the internal paths below are never
        entered, no plan is computed, and the behaviour is exactly what it
        was before Part 2C. That default is what keeps the blogger, report,
        analysis, streaming and batch paths untouched — none of them opts
        in.
        """
        self.builder = builder
        self.router = router or ProviderRouter()
        self.prompts = prompt_builder or PromptBuilder()
        self.planner = planner
        self.composer = composer
        self.open_ended = open_ended
        self.web_research = web_research
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
        context_override: GroundedContext | None = None,
        extra: str = "",
        retrieve: bool = True,
        language: "Language | None" = None,
    ) -> AnalystResult:
        """Produce one grounded analysis.

        `language` is the Language Adapter's only point of contact with this
        method, and it is deliberately inert on the English path. When set to
        a non-English language two things happen and nothing else changes:
        the retrieval query is normalised to English first, and the finished
        English answer is rendered afterwards. The reasoning in between is
        byte-identical either way, which is what keeps scores, citations and
        evidence the same in every language.

        `context_override` lets the report orchestrator hand in a context
        already restricted to one section's permitted sources, so a section
        about the business model cannot be answered from scoring output that
        happens to rank first.

        `extra` is appended verbatim to the task block. The orchestrator uses
        it to hand the model the section's provenance — which provider served
        the evidence, at what confidence, and with which page references — so
        the writer can state its own sourcing instead of the surrounding code
        stapling a provenance footer onto prose that never mentioned it.

        `retrieve=False` suppresses the per-question RAG call. The orchestrator
        has already retrieved against the section's own prompt and restricted
        the context accordingly; retrieving again here would re-admit document
        passages to a section whose route excludes them, quietly defeating the
        restriction that `context_override` was passed to impose.
        """
        prompt_template = template or get_prompt(capability)
        context = context_override if context_override is not None else self.context()

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
        #
        # ORCH-001. This unconditionally re-admitted document passages to a
        # context the caller had already restricted. The report orchestrator
        # hands in `context_override` precisely to confine a section to its
        # route's sources — Financial Performance to the financial database,
        # for instance — and this line then put RAG passages back in front of
        # the model for every section, because retrieval is keyed on the
        # question text and every section prompt is question text. The
        # restriction was real when the context was built and gone by the time
        # the prompt was assembled.
        # Inbound adaptation: a non-English question is rewritten into
        # English retrieval terms BEFORE it reaches the retriever, so
        # "राजस्व", "kamai" and "revenue" hit the same English index. The
        # retriever, the corpus and the embeddings are unchanged — this is a
        # query rewrite, not a second knowledge base.
        retrieval_query = question
        normalised = None
        if language is not None and language is not CANONICAL_LANGUAGE and question:
            from app.services.language.adapter import LanguageAdapter
            normalised = LanguageAdapter().normalise_query(question)
            retrieval_query = normalised.english
            log.info("multilingual query normalised",
                     language=language.value,
                     original=question[:120], english=retrieval_query[:120])

        # The source restriction is decided before anything answers, because
        # it also decides whether the deterministic path below is permitted
        # to run at all (see below).
        directive = source or parse_directive(question)

        # --- deterministic answer path ------------------------------------
        #
        # A canonical financial question ("What is the P/E?") names a figure
        # the platform has already computed. Answering it from the same
        # citations, without a model, is cheaper, faster and cannot drift
        # from the evidence — and it is provider-free, so a deployment with
        # no API key can still answer these questions.
        #
        # Phase 2A extends the same path to investment-intelligence
        # questions ("BEL kaisi company hai?", "BEL ki financial quality
        # kaisi hai?"). Those interpret the EXISTING ScoreResult and its
        # citations — the InvestmentAnswerEngine reads the scoring output
        # the ContextBuilder already computed; it does not score anything
        # itself. Both engines reach this code because the resolver hands
        # back exactly one intent or none, exactly as before.
        #
        # What this path must NOT touch: company resolution (already done
        # through AnalysisService), RAG (no retrieval call), and the prompt/
        # provider machinery. What it must still run: the citation audit,
        # the guardrail check and enforcement, the annotation and the
        # language/display pipeline — everything a provider answer runs,
        # via the same funnel, with zero-token accounting.
        #
        # It declines — and the provider path below takes over unchanged —
        # whenever the resolver cannot identify exactly one supported
        # intent, the question restricts its sources (a deterministic
        # answer from the financial database would violate a
        # "documents only" restriction), or the context was handed in by a
        # caller that restricted it itself.
        if (
            capability == Capability.CHAT.value
            and question.strip()
            and context_override is None
            and not directive.scope.is_restricted
        ):
            intent = FinancialIntentResolver().resolve(retrieval_query)
            if intent is not None:
                started = time.perf_counter()
                if intent in INVESTMENT_INTENTS:
                    answer = InvestmentAnswerEngine().answer(intent, context)
                    log.info(
                        "deterministic investment answer",
                        intent=intent.value, question=question[:160],
                        used_evidence=[c.key for c in answer.used_citations],
                        missing_evidence=answer.missing,
                    )
                else:
                    answer = FinancialAnswerEngine().answer(intent, context)
                    log.info(
                        "deterministic financial answer",
                        intent=intent.value, question=question[:160],
                        used_evidence=[c.key for c in answer.used_citations],
                        missing_evidence=answer.missing,
                    )
                return await self._deterministic(
                    capability, answer, context,
                    (time.perf_counter() - started) * 1000, memory, question,
                    language=language,
                )

            # --- Part 2C / Part 2D: the internal reasoning layers --------
            #
            # Reached only when the resolver above declined, and that
            # ordering is the regression protection: a question with exactly
            # one supported intent never arrives here, because it has already
            # been answered by the engine that has always answered it, through
            # the same funnel. What is left is the question the deterministic
            # engines are structurally unable to serve — two or more
            # recognised intents, or a shape the planner routes to internal
            # reasoning (a comparison, or a genuinely open-ended question).
            #
            # One plan is computed and offered to the layers in precedence
            # order: the Part 2C composer, which joins several intents, and
            # then the Part 2D internal open-ended engine, which answers a
            # comparison or an open-ended question from the same evidence.
            # Planning once rather than once per layer matters: a plan is a
            # company-resolution pass and a language-normalisation pass, and
            # two plans for one question could disagree about what was asked.
            #
            # Both layers are provider-free and retrieval-free by
            # construction: neither retrieves nor calls a router, both render
            # from the context's existing citations, and both hand their
            # result to `_deterministic` — so the citation audit, the
            # guardrails, the annotation, the memory write and the language
            # rendering are the same code that verifies every other answer.
            #
            # Both return None for every question they are not entitled to
            # answer (no collaborators injected, an unrecognised route, a
            # company that cannot be shown to be this analyst's company, an
            # engine that declined or raised), and the existing
            # retrieval/provider path below then serves the whole question
            # unchanged. No partial answer can escape: the internal text
            # exists only if the layer produced all of it.
            internal_started = time.perf_counter()
            plan = self._plan(question, directive)
            if plan is not None:
                composed = self._compose(plan, context)
                if composed is not None:
                    return await self._deterministic(
                        capability, composed, context,
                        (time.perf_counter() - internal_started) * 1000,
                        memory, question, language=language,
                    )
                internal = self._open_ended(plan, context)
                if internal is not None:
                    return await self._deterministic(
                        capability, internal, context,
                        (time.perf_counter() - internal_started) * 1000,
                        memory, question, language=language,
                        extra_citations=internal.derived_citations,
                    )

                # --- Part 3 Phase 4D: internal web research ---------------
                #
                # Last of the internal layers and only for the WEB_RESEARCH
                # route, so it cannot intercept a question any earlier layer
                # owns. The engine's verified web citations are ADDED to the
                # context (`with_citations` copies; nothing already held is
                # replaced or dropped) so the audit below can resolve every
                # marker the answer carries against the page it came from.
                # An honest evidence gap is an answer too: it goes through
                # the same funnel, and it never falls through to a provider.
                web = self._web_research(plan, context)
                if web is not None:
                    return await self._deterministic(
                        capability, web,
                        context.with_citations(list(web.web_citations)),
                        (time.perf_counter() - internal_started) * 1000,
                        memory, question, language=language,
                    )

        retrieved = self._retrieve(retrieval_query, capability) if retrieve else []
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
        # `directive` was resolved before the deterministic path above, which
        # declines to run under a restriction.
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

        # Writing directly in the target language produces better prose than
        # translating afterwards — native sentence structure rather than a
        # calque. Translation still runs as the guarantee, because a model
        # told to write Hindi will sometimes write English anyway.
        #
        # The instruction is decoration on the prompt, never a dependency:
        # a failure building it must degrade to the bare instruction — or to
        # no instruction at all — rather than 500 the request. This exact
        # class of failure shipped once before (a Phase 2 method the analyst
        # called but the adapter never defined), and it turned every
        # non-English chat into an unhandled AttributeError.
        task_extra = extra
        if language is not None and language is not CANONICAL_LANGUAGE:
            from app.services.language.adapter import LanguageAdapter
            try:
                # Phase 2: capability-aware multilingual instruction.
                instruction = LanguageAdapter.response_instruction_phase2(
                    language, capability,
                ) or LanguageAdapter.response_instruction(language)
            except Exception:  # noqa: BLE001 — see the comment above
                log.exception(
                    "language response instruction failed; continuing without it",
                    language=language.value, capability=capability,
                )
                instruction = LanguageAdapter.response_instruction(language)
            task_extra = f"{extra}{instruction}"

        built = self.prompts.build(
            prompt_template, context, question=question, memory=memory, style=style,
            include_history=capability == Capability.CHAT.value,
            extra=task_extra,
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

        return await self._finalise(
            capability, built, response, context, elapsed, memory, question,
            language=language,
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

        kinds = self._evidence_kinds(hits)
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
                # Which is also why it cannot be a constant: a Blogger post or a
                # conference-call transcript carries a different weight again,
                # and calling either an "Annual Report" teaches the model to
                # describe third-party commentary as a filed document.
                label=f"[{kinds.get(hit.document_id, 'Document')}] "
                      f"{hit.document_title} p.{hit.page}",
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

    def _evidence_kinds(self, hits: list[SearchHit]) -> dict[int, str]:
        """What kind of document each hit came from, by document id.

        One batched lookup for the whole hit list rather than one per citation:
        a retrieval returns at most a handful of documents, and the label is
        only needed when there IS evidence — the empty-hits path above never
        reaches here.

        A document whose provenance says it came from a Blogger feed is labelled
        as such rather than by its stored type. The type is still true — a
        research note — but "Blogger post" is the fact a reader needs in order
        to weigh it, and the fact the model needs in order not to describe the
        author's commentary as a company filing.

        Failures are swallowed and reported as absent: the caller falls back to
        a generic label, and a label lookup must never cost the user an answer
        they were about to get.
        """
        from sqlalchemy import select

        from app.domain.documents.types import document_type_label
        from app.models.document import Document as DocumentRow

        ids = sorted({hit.document_id for hit in hits if hit.document_id})
        if not ids:
            return {}
        try:
            rows = self.builder.document_service.db.execute(
                select(DocumentRow.id, DocumentRow.doc_type, DocumentRow.doc_metadata)
                .where(DocumentRow.id.in_(ids))
            ).all()
        except Exception:  # noqa: BLE001 — labels are a refinement, not a gate
            log.exception("could not resolve evidence document kinds")
            return {}

        kinds: dict[int, str] = {}
        for row_id, doc_type, metadata in rows:
            source = metadata.get("source") if isinstance(metadata, dict) else None
            kinds[row_id] = (
                "Blogger post" if source == "blogger"
                else document_type_label(doc_type)
            )
        return kinds

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
            # Deliberately NOT translated. A refusal is returned verbatim
            # because integrations branch on its exact wording — the docstring
            # above states that contract, and rendering it into Hindi would
            # break every caller that relies on it. The language block records
            # that the text is English so a client can label it.
            language=None,
        )

    async def _finalise(
        self,
        capability: str,
        built: BuiltPrompt,
        response: CompletionResponse,
        context: GroundedContext,
        elapsed_ms: float,
        memory: ConversationMemory | None,
        question: str,
        *,
        language: "Language | None" = None,
    ) -> AnalystResult:
        """Verify, classify and record.

        The single funnel every provider response passes through. It is a
        thin adapter over :meth:`_verify_and_record`, which the deterministic
        path shares — so a provider answer and a provider-free answer are
        verified, guarded, annotated, remembered and rendered by identical
        code. For provider responses the token accounting is unchanged: the
        reported prompt tokens are the provider's, falling back to the
        estimate exactly as before.
        """
        return await self._verify_and_record(
            capability, context, elapsed_ms, memory, question,
            raw_content=response.content,
            citations=built.citations,
            provider=response.provider,
            model=response.model,
            prompt_key=built.prompt_key,
            prompt_version=built.prompt_version,
            prompt_tokens=response.usage.prompt_tokens or built.approx_prompt_tokens,
            completion_tokens=response.usage.completion_tokens,
            cost_usd=response.cost_usd,
            cached=response.cached,
            fell_back_from=response.fell_back_from,
            language=language,
        )

    async def _deterministic(
        self,
        capability: str,
        answer: DeterministicAnswer | ComposedAnswer | InternalAnswer | WebResearchAnswer,
        context: GroundedContext,
        elapsed_ms: float,
        memory: ConversationMemory | None,
        question: str,
        *,
        language: "Language | None" = None,
        extra_citations: tuple[Citation, ...] = (),
    ) -> AnalystResult:
        """Finalise a provider-free answer through the same funnel.

        The deterministic engine composes its text from the context's own
        citations, so the citation audit, guardrail check, annotation, memory
        write and language adaptation all run exactly as for a provider
        response. The accounting reflects what actually happened: no model,
        no prompt, no tokens, no cost — and no invented
        ``CompletionResponse`` that would smuggle an estimated prompt-token
        count into the ledger. Latency is measured for real.

        Three provider-free answer shapes reach this funnel and nothing
        else about them differs once they arrive: a single-intent
        ``DeterministicAnswer`` from one engine, the multi-intent
        ``ComposedAnswer`` the composer built from those same engines, and
        the Part 2D ``InternalAnswer``. All three carry canonical English
        content and the evidence they used; the ``citations`` handed to the
        audit are the context's own, exactly as for the single-intent case,
        so the answer is verified against the same evidence block its
        sentences were drawn from.

        The Part 3 Phase 4D ``WebResearchAnswer`` is the fourth shape. It
        arrives with a context that already carries its verified web
        citations (added by ``with_citations`` at the call site), so the
        audit resolves its markers against the fetched pages exactly as it
        resolves a retrieved passage.

        ``extra_citations`` is the one addition, and only the Part 2D
        internal path uses it. A figure that layer *derived* from real
        citations — a difference, a multiple — is not in the context, so
        without it the audit would correctly report a number the evidence
        does not contain. Publishing the derived figure as a ``Citation``
        and handing it here keeps the audit honest in both directions: the
        figure resolves, and it resolves to an arithmetic step whose inputs
        are cited beside it. Nothing else is widened; the answer is still
        verified against the context it was built from.
        """
        return await self._verify_and_record(
            capability, context, elapsed_ms, memory, question,
            raw_content=answer.content,
            citations=[*context.citations, *extra_citations],
            provider="deterministic",
            model="none",
            prompt_key=capability,
            prompt_version=0,
            prompt_tokens=0,
            completion_tokens=0,
            cost_usd=0.0,
            cached=False,
            fell_back_from=None,
            language=language,
        )

    def _plan(
        self, question: str, directive: SourceDirective,
    ) -> QuestionPlan | None:
        """The plan for this question, or ``None`` if planning is unavailable.

        The single place the internal layers are planned from, so one
        question yields exactly one plan. ``None`` is returned when no
        planner was injected — composition was not opted into — and when
        planning raised, because planning must never cost a user their
        answer: an internal step that fails falls back, and says so in the
        log rather than silently.
        """
        planner = self.planner
        if planner is None:
            return None
        try:
            return planner.plan(question, source=directive)
        except Exception:  # noqa: BLE001 - planning must never break a chat
            log.exception("question planning failed", question=question[:160])
            return None

    def _compose(
        self,
        plan: QuestionPlan,
        context: GroundedContext,
    ) -> ComposedAnswer | None:
        """A provider-free answer to a multi-intent question, or ``None``.

        ``None`` is the fail-closed answer to every condition that is not
        satisfied - no composer injected, the plan is not a composition
        plan, the plan's company cannot be shown to be this analyst's
        company, the composer raised, or the composer declined. In each case
        the caller continues to the next internal layer and then into the
        existing retrieval/provider path with the whole question, so a
        question this layer is not entitled to answer is answered by the
        layer that always could, and a question it *is* entitled to answer
        is never answered partially.

        Composition is wrapped because it may not cost a user their answer:
        an internal step that fails falls back, and it says so in the log
        rather than silently.
        """
        composer = self.composer
        if composer is None:
            # Composition was not opted into for this analyst.
            return None

        if (
            plan.execution_route is not ExecutionRoute.COMPOSITION_REQUIRED
            or len(plan.intents) < 2
        ):
            # The single-intent deterministic routes were handled above and
            # the remaining routes (SOURCE_ROUTER, DECLINE, INTERNAL_REASONING)
            # belong to other layers; none of them is intercepted here.
            return None

        safe, reason = _company_identity_is_safe(plan, context)
        if not safe:
            log.info(
                "composition declined",
                question=plan.original_question[:160],
                reason=reason, entity_status=plan.entity.status.value,
                bound_company=context.ticker,
            )
            return None

        try:
            answer = composer.compose_answer(plan, context)
        except Exception:  # noqa: BLE001 - fail closed, never partially
            log.exception(
                "internal composition failed",
                question=plan.original_question[:160],
                intents=list(plan.intent_values),
            )
            return None

        if answer is None:
            log.info(
                "internal composition produced no answer",
                question=plan.original_question[:160],
                intents=list(plan.intent_values),
            )
            return None

        log.info(
            "deterministic composed answer",
            intents=[intent.value for intent in answer.intents],
            question=plan.original_question[:160], identity=reason,
            used_evidence=[c.key for c in answer.used_citations],
            missing_evidence=list(answer.missing),
        )
        return answer

    def _open_ended(
        self,
        plan: QuestionPlan,
        context: GroundedContext,
    ) -> InternalAnswer | None:
        """A provider-free answer to an open-ended question, or ``None``.

        Part 2D. Consulted only after the composer declined, so it can never
        intercept a question an existing route owns: the resolver has
        already answered every single-intent question, and the composer has
        already joined every multi-intent one. What reaches this layer is
        the ``INTERNAL_REASONING`` route - a comparison, or a genuinely
        open-ended question - which the engine itself re-checks before it
        answers anything.

        ``None`` is the fail-closed answer to every condition that is not
        satisfied: no engine injected, a company that cannot be shown to be
        this analyst's company, an engine that raised, or an engine that
        returned ``NOT_SUPPORTED`` - which it does for an unsupported
        question, a missing figure, an ambiguous subject or metric,
        conflicting evidence, and an operation it will not perform. In every
        one of those cases the caller continues into the existing
        retrieval/provider path with the whole question, so a refusal here
        costs the user nothing but the deterministic answer they would not
        have had anyway.

        A refusal carries no text by construction, so there is no partial
        internal answer to leak: ``InternalAnswer.content`` is empty unless
        the engine produced all of it.
        """
        engine = self.open_ended
        if engine is None:
            # Part 2D was not opted into for this analyst.
            return None

        if plan.execution_route is not ExecutionRoute.INTERNAL_REASONING:
            # Not this layer's route. Every other route keeps its existing
            # owner, and the engine would refuse it anyway; declining here
            # keeps the ownership rule visible at the call site.
            return None

        # The same identity gate the composer passes through. Reused rather
        # than reimplemented: a second, subtly different identity rule would
        # be a second way to answer about the wrong company.
        safe, reason = _company_identity_is_safe(plan, context)
        if not safe:
            log.info(
                "internal open-ended declined",
                question=plan.original_question[:160],
                reason=reason, entity_status=plan.entity.status.value,
                bound_company=context.ticker,
            )
            return None

        try:
            answer = engine.answer(plan, context)
        except Exception:  # noqa: BLE001 - fail closed, never partially
            # The user's answer must not depend on an internal layer being
            # bug-free. The failure is logged in full here and the question
            # falls back; no internal detail reaches the response.
            log.exception(
                "internal open-ended execution failed",
                question=plan.original_question[:160],
            )
            return None

        if not answer.answered:
            log.info(
                "internal open-ended produced no answer",
                question=plan.original_question[:160],
                capability=answer.capability.value,
                reason=answer.reason, missing_evidence=list(answer.missing),
            )
            return None

        log.info(
            "deterministic open-ended answer",
            capability=answer.capability.value,
            question=plan.original_question[:160], identity=reason,
            operations=[o.kind.value for o in answer.operations],
            used_evidence=[c.key for c in answer.used_citations],
            derived_evidence=[c.key for c in answer.derived_citations],
            missing_evidence=list(answer.missing),
        )
        return answer

    def _web_research(
        self,
        plan: QuestionPlan,
        context: GroundedContext,
    ) -> WebResearchAnswer | None:
        """A provider-free answer from the platform's web evidence, or ``None``.

        Part 3 Phase 4D. Consulted only after the composer and the internal
        open-ended engine declined, and only for the ``WEB_RESEARCH`` route
        — a question about a current development that no deterministic
        engine owns. The same identity gate as the other internal layers
        applies: the plan's company must be this analyst's company, or
        unresolved, in which case the bound company is authoritative and the
        engine scopes its search to it.

        ``None`` — and the existing retrieval/provider path — for every
        condition under which this layer is not entitled to run: no engine
        injected, another route, a company that cannot be shown to be this
        one, an engine that raised, or an engine that reports the question
        is not applicable (no query could be formed). What is **not**
        ``None`` is an honest evidence gap: if the local corpus and the
        bounded discovery produced nothing usable, the engine says so and
        that statement is the answer. A web-research question never falls
        through to a provider on the strength of an evidence gap — that
        would be the second AI pipeline this design refuses to build.
        """
        engine = self.web_research
        if engine is None:
            return None

        if plan.execution_route is not ExecutionRoute.WEB_RESEARCH:
            return None

        safe, reason = _company_identity_is_safe(plan, context)
        if not safe:
            log.info(
                "internal web research declined",
                question=plan.original_question[:160],
                reason=reason, entity_status=plan.entity.status.value,
                bound_company=context.ticker,
            )
            return None

        try:
            answer = engine.research(plan, context)
        except Exception:  # noqa: BLE001 - fail closed, never partially
            log.exception(
                "internal web research failed",
                question=plan.original_question[:160],
            )
            return None

        if answer is None or not answer.applicable:
            log.info(
                "internal web research not applicable",
                question=plan.original_question[:160],
                reason=getattr(answer, "reason", ""),
            )
            return None

        log.info(
            "internal web research answer",
            status=answer.status.value, answered=answer.answered,
            question=plan.original_question[:160], identity=reason,
            queries=list(answer.queries),
            used_evidence=[c.key for c in answer.used_citations],
            discovery=answer.discovery_status,
            missing_evidence=list(answer.missing),
        )
        return answer

    async def _verify_and_record(
        self,
        capability: str,
        context: GroundedContext,
        elapsed_ms: float,
        memory: ConversationMemory | None,
        question: str,
        *,
        raw_content: str,
        citations: list[Citation],
        provider: str,
        model: str,
        prompt_key: str,
        prompt_version: int,
        prompt_tokens: int,
        completion_tokens: int,
        cost_usd: float,
        cached: bool,
        fell_back_from: str | None,
        language: "Language | None" = None,
    ) -> AnalystResult:
        """Verify, classify and record.

        The single funnel every capability, chat turn and report section
        passes through — provider-generated and deterministic alike — which
        is why the Language Adapter is invoked here and nowhere else.
        Crucially the audit, the guardrail check and the citation annotation
        all run on the ENGLISH text first: the evidence chain is verified in
        the canonical language and only then rendered, so a translation can
        never change what was audited.

        `raw_content` is the answer exactly as produced (provider response or
        deterministic composition): it is what the audit sees and what memory
        stores. Guardrail enforcement may append the disclosure on top of it
        for the RESULT, but memory keeps the raw canonical English turn.
        """
        citation_audit = audit(raw_content, citations)
        guardrails = check(raw_content, citation_audit)
        content = enforce(raw_content, guardrails)

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
            # Memory stores the ENGLISH text, always. Conversation memory is
            # part of the canonical knowledge base, and storing a Hindi turn
            # would create exactly the per-language state the brief forbids —
            # it would also mean a user who switched language mid-session
            # carried untranslatable history forward.
            memory.add(
                Role.ASSISTANT, raw_content,
                citations=[c.key for c in citation_audit.resolved],
            )

        # --- outbound language adaptation --------------------------------
        #
        # Runs LAST, after audit(), check(), enforce() and the memory write,
        # so everything the platform verifies and stores is canonical English.
        # A translation cannot alter what was audited, which is what makes
        # "same evidence and citations in every language" structural rather
        # than merely tested.
        display = annotate(content, citations)
        language_block: dict | None = None

        if language is not None and language is not CANONICAL_LANGUAGE:
            from app.services.language.adapter import LanguageAdapter

            entities: list[str] = []
            company = getattr(getattr(self.builder, "analysis", None),
                              "company", None)
            if company is not None:
                entities = [n for n in (getattr(company, "name", None),
                                        getattr(company, "ticker", None)) if n]

            adapted = await LanguageAdapter().adapt(
                display, question=question, requested=language,
                entities=entities,
            )
            display = adapted.text
            language_block = adapted.as_dict()
            if not adapted.translation.translated:
                warnings.append(
                    adapted.translation.detail
                    or f"Response could not be rendered in {language.value}."
                )

        return AnalystResult(
            capability=capability,
            # `content` stays English: it is the audited artefact, it is what
            # gets persisted by AIService.record(), and a downstream consumer
            # comparing two responses must be comparing like with like.
            content=content,
            display_content=display,
            provider=provider, model=model,
            prompt_key=prompt_key, prompt_version=prompt_version,
            citations=citation_audit.resolved,
            citation_audit=citation_audit, guardrails=guardrails,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost_usd, latency_ms=elapsed_ms,
            cached=cached, fell_back_from=fell_back_from,
            warnings=warnings,
            language=language_block,
        )

    # ------------------------------------------------------------------ chat
    async def chat(
        self,
        question: str,
        memory: ConversationMemory,
        *,
        provider: str | None = None,
        source: SourceDirective | None = None,
        language: "Language | None" = None,
    ) -> AnalystResult:
        return await self.run(
            Capability.CHAT.value, question=question, memory=memory,
            provider=provider, source=source, language=language,
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
