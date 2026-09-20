"""Part 3 Phase 4D — grounded web evidence → internal synthesis → cited answer.

The runtime seam: a question the planner routes to ``WEB_RESEARCH`` is
answered by the platform's own web evidence — the 4B stored-page index
first, the 4C bounded live discovery when the sufficiency policy says the
local corpus is absent, thin or stale — composed deterministically by
``InternalWebResearchEngine`` into an answer that quotes its sources, cites
them as ``EvidenceKind.WEB`` citations, and passes through the one existing
verification funnel (CitationEngine → Guardrails → LanguageAdapter).

Four things are pinned here and nowhere else:

1. **Routing precedence.** Source directives, the deterministic financial
   engines, the composer and the internal open-ended engine all still answer
   first. Web research is reached only for the ``WEB_RESEARCH`` route, and
   it never intercepts arithmetic, a supported metric or a multi-intent
   question.
2. **Evidence discipline.** Only fetched, verified pages become citations.
   Exchange references that were never fetched are counted, not cited. A
   publication date is never invented, and a retrieval date is never
   presented as one. Sources that disagree are both quoted; none is chosen.
3. **Provider isolation.** The whole path works with the external providers
   disabled: no ``ProviderRouter`` call, no OpenAI/Gemini/OpenRouter import,
   no provider fallback inside the web path — an evidence gap is reported
   honestly instead.
4. **Additivity.** No migration, no API route change, no citation contract
   change; the existing retrieval/provider fallback stays for the routes the
   internal layers do not own.
"""
from __future__ import annotations

import ast
import asyncio
import importlib
import inspect
import pkgutil
import re
import socket
import types
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.db.base import Base
from app.domain.ai.types import Citation, EvidenceKind, mint_web_citation_key
from app.domain.documents.types import DocumentType
from app.domain.language.types import Language
from app.domain.web.types import WebFetchPolicy
from app.models.company import Company
from app.models.document import Document
from app.services.ai import internal_web_research as module
from app.services.ai.analyst import ResearchAnalyst
from app.services.ai.citation_engine import audit
from app.services.ai.context_builder import GroundedContext
from app.services.ai.guardrails import check
from app.services.ai.internal_composer import InternalComposer
from app.services.ai.internal_open_ended import InternalOpenEndedEngine
from app.services.ai.internal_web_research import (
    InternalWebResearchEngine,
    WebResearchAnswer,
    WebResearchPolicy,
    WebResearchStatus,
    citation_for_candidate,
)
from app.services.ai.memory import ConversationMemory
from app.services.ai.planner import ExecutionRoute, QuestionPlanner
from app.services.ai.planner.web_query import WebQueryGenerator
from app.services.ai.prompt_builder import PromptBuilder
from app.services.documents.ingestion import DocumentIngestionService
from app.services.documents.storage import LocalFileStorage
from app.services.language.translators import TranslationResult
from app.services.web.fetcher import HostPoliteness, TransportResponse
from app.services.web.index import (
    LOCAL_INDEX_ORIGIN,
    WebEvidenceCandidate,
    WebIndexSearchResult,
)
from app.services.web.robots import RobotsPolicy
from app.services.web.service import WebSearchService
from app.services.web.targeted_discovery import (
    EXCHANGE_REFERENCE_ORIGIN,
    LIVE_FETCH_PERSISTED_ORIGIN,
    DiscoveryPolicy,
    DiscoveryStatus,
    TargetedWebDiscovery,
)
from tests.test_deterministic_analyst_path import SpyDocumentService, SpyRouter

BACKEND = Path(__file__).resolve().parents[1]
APP = BACKEND / "app"
MODULE_PATH = APP / "services" / "ai" / "internal_web_research.py"
ANALYST_PATH = APP / "services" / "ai" / "analyst.py"
SERVICE_PATH = APP / "services" / "ai" / "service.py"
CODE = MODULE_PATH.read_text()

NOW = datetime(2026, 9, 20, 9, 0, tzinfo=timezone.utc)
HOST = "www.jsw.example"
ORIGIN = f"https://{HOST}"
ADDRESS = "93.184.216.34"
ROBOTS_ALLOW = "User-agent: *\nDisallow: /private\n"

JSW_ID = "c-jsw"
JSW = types.SimpleNamespace(id=JSW_ID, ticker="JSWSTEEL", name="JSW Steel Limited")
COMPANIES = [JSW, types.SimpleNamespace(id="c-tata", ticker="TATASTEEL", name="Tata Steel Limited")]

QUESTION = "JSW Steel ka latest expansion status kya hai?"

COMMISSIONED = (
    "JSW Steel said the expansion status is on track: the company has "
    "commissioned the 5 MTPA capacity expansion at Vijayanagar, taking total "
    "installed capacity to 34.2 MTPA, and the latest phase is now operational."
)
DELAYED = (
    "The expansion status update notes that commissioning of the 5 MTPA "
    "Vijayanagar expansion has been delayed to the next quarter after "
    "equipment deliveries slipped; the latest guidance is under review."
)
DIVIDEND = (
    "The board recommended a final dividend of 7.3 rupees per share for the "
    "year and fixed the record date; the annual general meeting notice was "
    "also published to shareholders on the company website."
)


def _run(coro):
    return asyncio.run(coro)


# ===========================================================================
# Company resolver, planner and context — the JSW Steel example end to end
# ===========================================================================
def resolve(text: str):
    """The platform's company-resolver contract, for a fixed universe."""
    lowered = (text or "").lower()
    return [c for c in COMPANIES if c.ticker.lower() in lowered
            or c.name.lower().split(" ")[0] in lowered]


def planner() -> QuestionPlanner:
    return QuestionPlanner(company_resolver=resolve)


def statement(key: str, value: float, unit: str = "₹ crore") -> Citation:
    return Citation(key=key, label=key.replace("_", " ").title(),
                    kind=EvidenceKind.STATEMENT, value=value, unit=unit,
                    source="06 Historical IS", fiscal_year=2025)


def jsw_context(*extra: Citation) -> GroundedContext:
    return GroundedContext(
        company_id=JSW_ID, ticker="JSWSTEEL", name="JSW Steel Limited",
        sector="Steel", citations=[statement("revenue", 175006.0), *extra],
    )


# ===========================================================================
# Evidence fakes: the 4B index and the 4C discovery, duck-typed
# ===========================================================================
def candidate(
    *, text: str = COMMISSIONED, title: str = "JSW Steel – Investor Relations",
    url: str = f"{ORIGIN}/investors", canonical: str | None = None,
    published: datetime | None = NOW - timedelta(days=2),
    retrieved: datetime | None = NOW, relevance: float = 0.9,
    document_id: int | None = 11, chunk_id: int | None = 110,
    source_class: str = "company_website", origin: str = LOCAL_INDEX_ORIGIN,
    content_hash: str | None = "sha-11", company_id: str | None = JSW_ID,
) -> WebEvidenceCandidate:
    """A stored-page candidate exactly as the 4B index returns it."""
    return WebEvidenceCandidate(
        document_id=document_id, chunk_id=chunk_id, company_id=company_id,
        source_url=url, canonical_url=canonical if canonical is not None else url,
        title=title, source_class=source_class, published_at=published,
        retrieved_at=retrieved, snippet=text, relevance=relevance,
        authority=0.55, freshness=1.0,
        freshness_basis="published_at" if published else "retrieved_at",
        score=round(0.7 * relevance + 0.2, 6), matched_queries=("q",),
        signals=("lexical",), content_hash=content_hash, origin=origin,
    )


def index_result(*candidates: WebEvidenceCandidate, company_id=JSW_ID,
                 corpus: int | None = None) -> WebIndexSearchResult:
    return WebIndexSearchResult(
        queries=("q",), company_id=company_id, candidates=tuple(candidates),
        corpus_documents=len(candidates) if corpus is None else corpus,
        engine="hybrid" if candidates else "none", semantic_used=False,
        notes=() if candidates else ("no stored web pages in scope",),
    )


class FakeIndex:
    """Answers with a fixed result; records every search."""

    def __init__(self, result: WebIndexSearchResult | None = None,
                 *, error: Exception | None = None) -> None:
        self.result = result if result is not None else index_result()
        self.error = error
        self.calls: list[tuple[tuple[str, ...], str | None]] = []

    def search(self, queries, *, company_id=None, limit=None):
        self.calls.append((tuple(getattr(queries, "texts", queries)), company_id))
        if self.error is not None:
            raise self.error
        return self.result


class FakeDiscovery:
    """Stands in for the 4C layer: records calls, answers from a table."""

    def __init__(self, status: DiscoveryStatus = DiscoveryStatus.DISABLED,
                 *, merged=None, fetched=(), references=(), refusals=(),
                 pages_attempted: int = 0, error: Exception | None = None) -> None:
        self.status = status
        self.merged = merged
        self.fetched = tuple(fetched)
        self.references = tuple(references)
        self.refusals = tuple(refusals)
        self.pages_attempted = pages_attempted
        self.error = error
        self.calls: list[dict] = []

    def discover(self, queries, *, company_id, local=None, recency_sensitive=None,
                 persist=None, force=False):
        self.calls.append({"queries": tuple(getattr(queries, "texts", queries)),
                           "company_id": company_id, "local": local})
        if self.error is not None:
            raise self.error
        local_candidates = tuple(getattr(local, "candidates", ()) or ())
        merged = (tuple(self.merged) if self.merged is not None
                  else local_candidates + self.fetched + self.references)
        return types.SimpleNamespace(
            status=self.status, merged=merged, fetched=self.fetched,
            exchange_references=self.references, refusals=self.refusals,
            pages_attempted=self.pages_attempted, company_id=company_id,
            queries=tuple(getattr(queries, "texts", queries)), notes=(),
            decision=types.SimpleNamespace(trigger=None, reason="fake"),
            persisted=False,
        )


class SpyWebEngine:
    """Wraps the real engine; records what the analyst hands it."""

    def __init__(self, inner: InternalWebResearchEngine) -> None:
        self.inner = inner
        self.calls: list[tuple] = []
        self.results: list[WebResearchAnswer] = []

    def research(self, plan, context):
        self.calls.append((plan, context))
        result = self.inner.research(plan, context)
        self.results.append(result)
        return result


def engine(index: FakeIndex | None = None, discovery=None, *,
           policy: WebResearchPolicy | None = None) -> InternalWebResearchEngine:
    return InternalWebResearchEngine(
        index=index if index is not None else FakeIndex(),
        discovery=discovery, generator=WebQueryGenerator(), policy=policy,
        clock=lambda: NOW,
    )


def plan_for(question: str = QUESTION):
    return planner().plan(question)


def queries_for(question: str = QUESTION):
    return WebQueryGenerator().generate(plan_for(question))


# ===========================================================================
# The analyst, bound to the JSW context, with every collaborator injected
# ===========================================================================
class StaticBuilder:
    """A ContextBuilder stand-in: the analyst only ever calls ``build()``."""

    def __init__(self, context: GroundedContext, docs=None) -> None:
        self._context = context
        self.document_service = docs
        self.analysis = types.SimpleNamespace(company=JSW)

    def build(self) -> GroundedContext:
        return self._context


def memory() -> ConversationMemory:
    m = ConversationMemory(session_id="web-research-test")
    m.set_company(JSW.id, JSW.ticker, JSW.name)
    return m


def analyst(web: SpyWebEngine | InternalWebResearchEngine | None, *,
            context: GroundedContext | None = None, router=None, docs=None,
            open_ended=None, composer=None) -> ResearchAnalyst:
    return ResearchAnalyst(
        StaticBuilder(context or jsw_context(), docs), router=router or SpyRouter("forbid"),
        prompt_builder=PromptBuilder(), planner=planner(),
        composer=composer if composer is not None else InternalComposer(),
        open_ended=open_ended if open_ended is not None else InternalOpenEndedEngine(),
        web_research=web,
    )


def chat(a: ResearchAnalyst, question: str = QUESTION, **kwargs):
    return _run(a.chat(question, memory(), **kwargs))


# ===========================================================================
# Real 4C discovery over a private database and a fake transport
# ===========================================================================
class FakeTransport:
    def __init__(self, responses=None) -> None:
        self.responses = dict(responses or {})
        self.calls: list[str] = []

    def get(self, url, *, headers, timeout, max_bytes):
        self.calls.append(url)
        answer = self.responses.get(url)
        if answer is None:
            return TransportResponse(
                status_code=404, headers={"content-type": "text/plain"},
                content=b"not found", final_url=url, elapsed_ms=1.0,
                truncated=False, peer_address=(ADDRESS, 443),
            )
        return answer


def page(url: str, text: str, *, title: str, published: str | None = None) -> TransportResponse:
    head = f"<title>{title}</title>"
    if published:
        head += f'<meta property="article:published_time" content="{published}">'
    body = (
        f"<html><head>{head}</head><body><main><h1>{title}</h1>"
        f"<p>{text}</p></main></body></html>"
    ).encode()
    return TransportResponse(
        status_code=200,
        headers={"content-type": "text/html; charset=utf-8", "content-length": str(len(body))},
        content=body, final_url=url, elapsed_ms=1.0, truncated=False,
        peer_address=(ADDRESS, 443),
    )


class DictCache:
    def __init__(self) -> None:
        self.values: dict = {}

    def get(self, namespace, *parts):
        return self.values.get((namespace, parts))

    def set(self, namespace, value, *parts):
        self.values[(namespace, parts)] = value

    def invalidate(self, namespace):
        return 0


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Any real connection attempt fails loudly. ``socket.socket`` itself is
    left alone because asyncio's event loop needs a local socketpair."""
    def _refuse(*_a, **_k):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "create_connection", _refuse)
    monkeypatch.setattr(socket, "getaddrinfo", _refuse)
    yield


@pytest.fixture()
def db():
    import app.models as models

    for entry in pkgutil.iter_modules(models.__path__):
        importlib.import_module(f"app.models.{entry.name}")
    sql_engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=sql_engine)
    session = sessionmaker(bind=sql_engine, autoflush=False, expire_on_commit=False)()
    session.add(Company(id=JSW_ID, name=JSW.name, ticker=JSW.ticker, website=ORIGIN))
    session.commit()
    try:
        yield session
    finally:
        session.close()
        sql_engine.dispose()


def real_discovery(db, transport: FakeTransport, tmp_path, *, enabled: bool = True,
                   policy: DiscoveryPolicy | None = None) -> TargetedWebDiscovery:
    def robots_fetch(url, *, timeout, max_bytes, user_agent):
        return 200, ROBOTS_ALLOW.encode()

    service = WebSearchService(
        db,
        ingestion=DocumentIngestionService(db, storage=LocalFileStorage(tmp_path / "docs")),
        policy=WebFetchPolicy(),
        robots=RobotsPolicy(fetch=robots_fetch, user_agent="EquityPilotAI/1.0"),
        transport=transport, resolver=lambda host, port: [ADDRESS],
        politeness=HostPoliteness(default_delay=0.0, sleep=lambda _s: None, clock=lambda: 0.0),
        cache_service=DictCache(), now=lambda: NOW,
    )
    return TargetedWebDiscovery(db, search_service=service, policy=policy,
                                clock=lambda: NOW, enabled=enabled)


# ===========================================================================
# 1 / 18 / 19 / 20 / 21 / 24 — routing precedence at the analyst seam
# ===========================================================================
class TestRoutingPrecedence:

    def test_web_research_route_reaches_the_web_research_path(self):
        """(1) The JSW Steel question is planned WEB_RESEARCH and answered
        by the web path — provider-free, retrieval-free."""
        web = SpyWebEngine(engine(FakeIndex(index_result(
            candidate(), candidate(document_id=12, url=f"{ORIGIN}/investors/updates",
                      title="Capacity expansion update", text=DELAYED,
                      content_hash="sha-12"),
        ))))
        docs = SpyDocumentService(fail_on_search=True)
        a = analyst(web, docs=docs)

        result = chat(a)

        assert plan_for().execution_route is ExecutionRoute.WEB_RESEARCH
        assert len(web.calls) == 1
        assert web.results[0].status is WebResearchStatus.CONFLICTING_EVIDENCE or \
            web.results[0].answered
        assert result.provider == "deterministic" and result.model == "none"
        assert a.router.complete_calls == []
        assert docs.search_calls == []
        assert any(c.kind is EvidenceKind.WEB for c in result.citations)

    def test_arithmetic_still_goes_to_the_internal_open_ended_engine(self):
        """(18) ₹500 → ₹650 is INTERNAL_REASONING: offered to the Part 2D
        engine, never to the web path. What happens after the Part 2D engine
        decides is exactly what happened before Phase 4D — here the engine
        declines a literal-number calculation, so the existing fallback
        serves it; no web evidence is consulted or cited either way."""

        class SpyOpenEnded:
            def __init__(self) -> None:
                self.inner = InternalOpenEndedEngine()
                self.calls: list = []

            def answer(self, plan, context):
                self.calls.append(plan)
                return self.inner.answer(plan, context)

        question = "₹500 se ₹650 kitna percent increase hai?"
        web = SpyWebEngine(engine(FakeIndex(index_result(candidate()))))
        open_ended = SpyOpenEnded()
        a = analyst(web, router=SpyRouter("offline"), open_ended=open_ended,
                    docs=SpyDocumentService(fail_on_search=False))
        result = chat(a, question)

        assert plan_for(question).execution_route is ExecutionRoute.INTERNAL_REASONING
        assert len(open_ended.calls) == 1
        assert web.calls == []
        assert not any(c.kind is EvidenceKind.WEB for c in result.citations)
        assert result.provider in {"deterministic", SpyRouter.NAME}

    def test_deterministic_financial_intents_still_use_the_existing_engines(self):
        """(19) A supported metric is the resolver's; the web path is not consulted."""
        web = SpyWebEngine(engine())
        context = jsw_context(statement("pe", 18.4, "x"))
        result = chat(analyst(web, context=context), "JSW Steel ka P/E kya hai?")

        assert web.calls == []
        assert result.provider == "deterministic"
        assert "P/E" in result.content
        assert not any(c.kind is EvidenceKind.WEB for c in result.citations)

    def test_source_directives_outrank_web_research(self):
        """(20) 'uploaded documents only' restricts before any internal layer runs."""
        web = SpyWebEngine(engine(FakeIndex(index_result(candidate()))))
        result = chat(analyst(web), "Latest expansion news about JSW Steel from uploaded documents only")

        assert web.calls == []
        # Nothing uploaded → the existing fail-closed refusal, not a web answer.
        assert result.provider == "source-router"
        assert not any(c.kind is EvidenceKind.WEB for c in result.citations)

    def test_multi_intent_questions_are_still_composed(self):
        """(21) Two recognised intents go to the composer; web research is not consulted."""
        web = SpyWebEngine(engine(FakeIndex(index_result(candidate()))))
        context = jsw_context(statement("pe", 18.4, "x"), statement("roe", 0.12, "%"))
        result = chat(analyst(web, context=context), "JSW Steel ka latest P/E aur ROE kya hai?")

        assert plan_for("JSW Steel ka latest P/E aur ROE kya hai?").execution_route \
            is ExecutionRoute.COMPOSITION_REQUIRED
        assert web.calls == []
        assert result.provider == "deterministic"
        assert "P/E" in result.content and "ROE" in result.content
        assert not any(c.kind is EvidenceKind.WEB for c in result.citations)

    def test_the_existing_rag_and_provider_fallback_is_unchanged_elsewhere(self):
        """(24) A route the internal layers do not own still retrieves and
        calls the provider exactly as before."""
        web = SpyWebEngine(engine(FakeIndex(index_result(candidate()))))
        docs = SpyDocumentService(fail_on_search=False)
        a = analyst(web, router=SpyRouter("offline"), docs=docs)
        question = "JSW Steel ki latest quarterly revenue kya hai?"

        assert plan_for(question).execution_route is ExecutionRoute.DECLINE
        result = chat(a, question)

        assert web.calls == []
        assert docs.search_calls == [question]
        assert len(a.router.complete_calls) == 1
        assert result.provider == SpyRouter.NAME

    def test_web_research_is_not_consulted_when_the_company_is_not_this_one(self):
        """The same identity gate the composer and open-ended layer use."""
        web = SpyWebEngine(engine(FakeIndex(index_result(candidate()))))
        a = analyst(web, router=SpyRouter("offline"), docs=SpyDocumentService(fail_on_search=False))
        result = chat(a, "Tata Steel ka latest expansion status kya hai?")

        assert web.calls == []
        assert result.provider == SpyRouter.NAME

    def test_an_engine_failure_falls_back_instead_of_breaking_the_chat(self):
        class Exploding:
            def research(self, plan, context):
                raise RuntimeError("index is on fire")

        a = analyst(Exploding(), router=SpyRouter("offline"),
                    docs=SpyDocumentService(fail_on_search=False))
        result = chat(a)
        assert result.provider == SpyRouter.NAME

    def test_no_engine_injected_means_the_route_behaves_as_before(self):
        a = analyst(None, router=SpyRouter("offline"),
                    docs=SpyDocumentService(fail_on_search=False))
        result = chat(a)
        assert result.provider == SpyRouter.NAME


# ===========================================================================
# 2 / 3 / 4 — the evidence flow: local index first, bounded discovery second
# ===========================================================================
class TestEvidenceFlow:

    def test_local_web_evidence_is_used(self):
        """(2) Two fresh stored passages answer the question outright."""
        index = FakeIndex(index_result(
            candidate(),
            candidate(document_id=12, url=f"{ORIGIN}/investors/presentations",
                      title="Investor presentation", content_hash="sha-12",
                      text="Latest expansion status: the Vijayanagar expansion "
                           "phase was commissioned and the plant is operational."),
        ))
        answer = engine(index).research(plan_for(), jsw_context())

        assert index.calls == [(queries_for().texts, JSW_ID)]
        assert answer.status is WebResearchStatus.EVIDENCE_FOUND
        assert answer.answered and answer.applicable
        assert {c.key for c in answer.used_citations} == {
            mint_web_citation_key(f"{ORIGIN}/investors"),
            mint_web_citation_key(f"{ORIGIN}/investors/presentations"),
        }
        assert "commissioned the 5 MTPA capacity expansion" in answer.content
        assert "Investor presentation" in answer.content

    def test_insufficient_local_evidence_triggers_targeted_discovery(self, db, tmp_path):
        """(3) An empty local corpus → the real 4C layer fetches the company's
        own IR page through the real safety stack, and the fetched page is
        what gets cited."""
        transport = FakeTransport({
            f"{ORIGIN}/investors": page(
                f"{ORIGIN}/investors", COMMISSIONED + " " + DIVIDEND,
                title="JSW Steel investors", published="2026-09-17T08:00:00+00:00",
            ),
        })
        discovery = real_discovery(db, transport, tmp_path)
        index = FakeIndex(index_result(corpus=0))
        answer = engine(index, discovery).research(plan_for(), jsw_context())

        assert f"{ORIGIN}/investors" in transport.calls
        assert answer.status is WebResearchStatus.EVIDENCE_FOUND
        [citation] = answer.used_citations
        assert citation.kind is EvidenceKind.WEB
        assert citation.web.url == f"{ORIGIN}/investors"
        assert citation.key == mint_web_citation_key(citation.web.canonical_url or citation.web.url)
        assert citation.web.published_at is not None
        assert citation.web.published_at.strftime("%d %b %Y") == "17 Sep 2026"
        assert answer.discovery_status == DiscoveryStatus.DISCOVERED.value
        # The page was ingested as a web_page document: stored, not transient.
        docs = list(db.scalars(select(Document)).all())
        assert [d.doc_type for d in docs] == [DocumentType.WEB_PAGE.value]
        assert citation.document_id == docs[0].id
        assert "live_fetch_persisted" in answer.content

    def test_fresh_local_evidence_avoids_live_discovery(self, db, tmp_path):
        """(4) Two strong, fresh stored passages satisfy the policy: the
        transport is never asked for anything."""
        transport = FakeTransport({
            f"{ORIGIN}/investors": page(f"{ORIGIN}/investors", COMMISSIONED, title="x"),
        })
        discovery = real_discovery(db, transport, tmp_path)
        index = FakeIndex(index_result(
            candidate(published=NOW - timedelta(days=1)),
            candidate(document_id=12, url=f"{ORIGIN}/news/expansion", text=DELAYED,
                      title="Expansion news", content_hash="sha-12",
                      published=NOW - timedelta(days=3)),
        ))
        answer = engine(index, discovery).research(plan_for(), jsw_context())

        assert transport.calls == []
        assert answer.discovery_status == DiscoveryStatus.LOCAL_SUFFICIENT.value
        assert answer.answered
        assert list(db.scalars(select(Document)).all()) == []

    def test_stale_local_evidence_triggers_discovery_for_a_current_question(self):
        """The 4C sufficiency policy is the arbiter, and this engine honours it."""
        discovery = FakeDiscovery(DiscoveryStatus.NOTHING_FOUND, pages_attempted=2)
        index = FakeIndex(index_result(
            candidate(published=NOW - timedelta(days=90)),
            candidate(document_id=12, url=f"{ORIGIN}/b", published=NOW - timedelta(days=95),
                      content_hash="sha-12", text=DELAYED),
        ))
        answer = engine(index, discovery).research(plan_for(), jsw_context())

        assert len(discovery.calls) == 1
        assert discovery.calls[0]["company_id"] == JSW_ID
        assert discovery.calls[0]["local"] is index.result
        # Old evidence still answers — with its date and the caveat.
        assert answer.status is WebResearchStatus.CONFLICTING_EVIDENCE
        assert "after that date" in answer.content

    def test_the_bound_company_scopes_the_search_even_when_the_plan_is_unresolved(self):
        index = FakeIndex(index_result(candidate(), candidate(
            document_id=12, url=f"{ORIGIN}/b", content_hash="sha-12", text=DELAYED)))
        discovery = FakeDiscovery(DiscoveryStatus.LOCAL_SUFFICIENT)
        answer = engine(index, discovery).research(
            plan_for("Latest expansion status kya hai?"), jsw_context(),
        )
        assert index.calls[0][1] == JSW_ID
        assert discovery.calls[0]["company_id"] == JSW_ID
        assert answer.company_id == JSW_ID

    def test_a_failing_index_is_reported_and_discovery_still_runs(self):
        discovery = FakeDiscovery(DiscoveryStatus.DISABLED)
        answer = engine(FakeIndex(error=RuntimeError("db down")), discovery).research(
            plan_for(), jsw_context(),
        )
        assert len(discovery.calls) == 1 and discovery.calls[0]["local"] is None
        assert answer.status is WebResearchStatus.DISCOVERY_DISABLED
        assert any("index" in note for note in answer.notes)

    def test_a_failing_discovery_leaves_local_evidence_in_charge(self):
        index = FakeIndex(index_result(candidate(), candidate(
            document_id=12, url=f"{ORIGIN}/b", content_hash="sha-12",
            text="Expansion status: the second phase remains on schedule.")))
        answer = engine(index, FakeDiscovery(error=RuntimeError("boom"))).research(
            plan_for(), jsw_context(),
        )
        assert answer.answered
        assert any("discovery" in note for note in answer.notes)

    def test_non_web_routes_are_not_applicable(self):
        web = engine(FakeIndex(index_result(candidate())))
        answer = web.research(plan_for("₹500 se ₹650 kitna percent increase hai?"), jsw_context())
        assert answer.status is WebResearchStatus.NOT_APPLICABLE
        assert not answer.applicable and answer.content == ""
        assert web.index.calls == []


# ===========================================================================
# 5 / 6 / 7 / 8 — citations: injected, present, verified, never invented
# ===========================================================================
class TestCitations:

    def test_web_citations_are_added_to_the_grounded_context_never_replacing(self):
        """(5) The context handed to the funnel carries the web citations in
        addition to everything it already held; the analyst's cached context
        is untouched."""
        seen: dict = {}
        web = SpyWebEngine(engine(FakeIndex(index_result(candidate()))))
        a = analyst(web)
        original = a._deterministic

        async def spy(capability, answer, context, *args, **kwargs):
            seen["context"] = context
            seen["answer"] = answer
            return await original(capability, answer, context, *args, **kwargs)

        a._deterministic = spy  # type: ignore[method-assign]
        result = chat(a)

        keys = [c.key for c in seen["context"].citations]
        assert "revenue" in keys                       # pre-existing evidence kept
        assert mint_web_citation_key(f"{ORIGIN}/investors") in keys
        assert isinstance(seen["answer"], WebResearchAnswer)
        # The analyst's own context was copied, not mutated.
        assert [c.key for c in a.context().citations] == ["revenue"]
        assert result.citation_audit.unknown_keys == []

    def test_the_answer_cites_every_web_claim(self):
        """(6) Every quoted claim line carries a marker that resolves."""
        answer = engine(FakeIndex(index_result(
            candidate(),
            candidate(document_id=12, url=f"{ORIGIN}/news", title="News", text=DELAYED,
                      content_hash="sha-12"),
        ))).research(plan_for(), jsw_context())

        claim_lines = [line for line in answer.content.splitlines() if line.startswith("- ")]
        assert len(claim_lines) == 2
        for line in claim_lines:
            assert re.search(r"\[web_[a-z0-9]+_[0-9a-f]{8}\]", line), line
        available = list(answer.web_citations)
        verdict = audit(answer.content, available)
        assert verdict.unknown_keys == []
        assert {c.key for c in verdict.resolved} == {c.key for c in answer.used_citations}

    def test_the_citation_engine_accepts_the_web_citations(self):
        """(7) Valid keys, full numeric coverage, no unsupported figures."""
        answer = engine(FakeIndex(index_result(
            candidate(),
            candidate(document_id=12, url=f"{ORIGIN}/news", title="News",
                      text="Expansion status: phase two of the 12 MTPA programme "
                           "is on schedule for FY27 completion.",
                      content_hash="sha-12"),
        ))).research(plan_for(), jsw_context())

        verdict = audit(answer.content, list(answer.web_citations))
        assert verdict.unknown_keys == []
        assert verdict.uncited_numbers == []
        assert verdict.coverage >= 0.6 and verdict.is_supported
        assert check(answer.content, verdict).passed

    def test_unknown_citation_keys_are_rejected_by_the_funnel(self):
        """(8) A key the evidence did not supply is flagged, and the guardrail
        turns the flag into a violation — so a synthetic citation can never
        pass verification."""
        answer = engine(FakeIndex(index_result(candidate()))).research(plan_for(), jsw_context())
        [real] = answer.used_citations
        forged = answer.content.replace(real.key, "web_wwwjswexample_deadbeef")

        verdict = audit(forged, list(answer.web_citations))
        assert verdict.unknown_keys == ["web_wwwjswexample_deadbeef"]
        assert not verdict.is_supported
        assert not check(forged, verdict).passed

    def test_the_engine_only_ever_emits_keys_it_was_given(self):
        for texts in ([COMMISSIONED], [COMMISSIONED, DELAYED], [DELAYED, DIVIDEND]):
            cands = [candidate(document_id=20 + i, url=f"{ORIGIN}/p{i}", text=t,
                               content_hash=f"sha-{20 + i}") for i, t in enumerate(texts)]
            answer = engine(FakeIndex(index_result(*cands))).research(plan_for(), jsw_context())
            emitted = set(re.findall(r"\[([a-z][a-z0-9_.]+)\]", answer.content))
            assert emitted <= {c.key for c in answer.web_citations}
            assert {c.key for c in answer.used_citations} == emitted

    def test_the_final_result_carries_verified_web_citations_through_the_api_shape(self):
        result = chat(analyst(SpyWebEngine(engine(FakeIndex(index_result(candidate()))))))
        [web] = [c for c in result.citations if c.kind is EvidenceKind.WEB]
        assert web.key == mint_web_citation_key(f"{ORIGIN}/investors")
        assert web.label.startswith("[Company Website]")
        assert "published 18 Sep 2026" in web.source and "retrieved 20 Sep 2026" in web.source
        assert web.web is not None and web.web.url == f"{ORIGIN}/investors"
        # The display copy shows the readable label where the marker was.
        assert f"[{web.label}]" in result.display_content
        assert result.citation_audit.unknown_keys == []
        assert result.guardrails.passed


# ===========================================================================
# 9 / 10 / 11 / 12 — the answer policy: no fabrication, honest gaps
# ===========================================================================
class TestAnswerPolicy:

    def test_unsupported_claims_are_not_fabricated(self):
        """(9) Pages about something else do not become an expansion answer."""
        answer = engine(FakeIndex(index_result(
            candidate(text=DIVIDEND, title="Dividend notice"),
            candidate(document_id=12, url=f"{ORIGIN}/agm", text=DIVIDEND, title="AGM",
                      content_hash="sha-12"),
        ))).research(plan_for(), jsw_context())

        assert answer.status is WebResearchStatus.INSUFFICIENT_EVIDENCE
        assert not answer.answered and answer.applicable
        assert "insufficient" in answer.content.lower()
        assert "dividend" not in answer.content.lower()
        assert not [l for l in answer.content.splitlines() if l.startswith("- ")]
        assert answer.used_citations == ()

    def test_missing_evidence_produces_an_honest_insufficiency_response(self):
        """(10) Nothing stored, no discovery layer → says so, through the funnel."""
        web = SpyWebEngine(engine(FakeIndex(index_result(corpus=0))))
        a = analyst(web, router=SpyRouter("forbid"))
        result = chat(a)

        assert web.results[0].status is WebResearchStatus.INSUFFICIENT_EVIDENCE
        assert result.provider == "deterministic"
        assert "insufficient" in result.content.lower()
        assert "expansion" in result.content.lower()
        assert a.router.complete_calls == []
        assert result.citation_audit.unknown_keys == []

    def test_discovery_disabled_is_reported_as_itself(self):
        answer = engine(FakeIndex(index_result(corpus=0)),
                        FakeDiscovery(DiscoveryStatus.DISABLED)).research(plan_for(), jsw_context())
        assert answer.status is WebResearchStatus.DISCOVERY_DISABLED
        assert "WEB_EVIDENCE_ENABLED" in answer.content
        assert "insufficient" in answer.content.lower()

    def test_discovery_refused_is_reported_as_itself(self):
        answer = engine(FakeIndex(index_result(corpus=0)),
                        FakeDiscovery(DiscoveryStatus.NO_TARGET)).research(plan_for(), jsw_context())
        assert answer.status is WebResearchStatus.DISCOVERY_REFUSED
        assert "company-owned" in answer.content

    def test_old_evidence_carries_a_temporal_caveat(self):
        """(11) A 'latest' question answered from 100-day-old pages says so."""
        old = NOW - timedelta(days=100)
        answer = engine(FakeIndex(index_result(
            candidate(published=old),
            candidate(document_id=12, url=f"{ORIGIN}/b", published=old - timedelta(days=5),
                      content_hash="sha-12",
                      text="Expansion status remains unchanged from the prior update."),
        )), FakeDiscovery(DiscoveryStatus.NOTHING_FOUND)).research(plan_for(), jsw_context())

        assert answer.status is WebResearchStatus.STALE_EVIDENCE
        assert answer.answered
        assert old.strftime("%d %b %Y") in answer.content
        assert "after that date" in answer.content
        assert answer.freshest_published_at == old

    def test_conflicting_sources_are_attributed_not_chosen(self):
        """(12) Commissioned vs delayed: both quoted, both cited, neither picked."""
        answer = engine(FakeIndex(index_result(
            candidate(title="JSW Steel – Investor Relations"),
            candidate(document_id=12, url=f"{ORIGIN}/news/delay", title="Expansion delay note",
                      text=DELAYED, content_hash="sha-12", published=NOW - timedelta(days=1)),
        ))).research(plan_for(), jsw_context())

        assert answer.status is WebResearchStatus.CONFLICTING_EVIDENCE
        assert "Sources differ" in answer.content
        assert "commissioned" in answer.content and "delayed" in answer.content
        assert "JSW Steel – Investor Relations" in answer.content
        assert "Expansion delay note" in answer.content
        assert len(answer.used_citations) == 2
        assert "does not reconcile" in answer.content

    def test_a_fresh_single_source_answers_with_the_gap_stated(self):
        answer = engine(FakeIndex(index_result(candidate()))).research(plan_for(), jsw_context())
        assert answer.status is WebResearchStatus.EVIDENCE_FOUND
        assert "one verified page" in answer.content
        assert "Source-reported" in answer.content

    def test_verified_facts_and_source_claims_and_unknowns_are_labelled(self):
        answer = engine(FakeIndex(index_result(candidate()))).research(plan_for(), jsw_context())
        assert "Verified by the platform" in answer.content
        assert "Source-reported claims" in answer.content
        assert "Not established" in answer.content

    def test_the_synthesis_is_deterministic(self):
        index = FakeIndex(index_result(candidate(), candidate(
            document_id=12, url=f"{ORIGIN}/b", text=DELAYED, content_hash="sha-12")))
        first = engine(index).research(plan_for(), jsw_context())
        second = engine(index).research(plan_for(), jsw_context())
        assert first == second


# ===========================================================================
# 13 / 14 / 15 — provenance is preserved and never embellished
# ===========================================================================
class TestProvenance:

    def test_url_and_canonical_url_are_preserved(self):
        """(13) The citation carries what was fetched, keyed on the canonical URL."""
        raw = f"{ORIGIN}/investors?utm_source=x"
        canonical = f"{ORIGIN}/investors"
        cit = citation_for_candidate(candidate(url=raw, canonical=canonical, content_hash="abc123"))

        assert cit is not None and cit.kind is EvidenceKind.WEB
        assert cit.web.url == raw
        assert cit.web.canonical_url == canonical
        assert cit.key == mint_web_citation_key(canonical)
        assert cit.web.title == "JSW Steel – Investor Relations"
        assert cit.web.content_hash == "abc123"
        assert cit.label == "[Company Website] JSW Steel – Investor Relations"
        assert cit.document_id == 11 and cit.chunk_id == 110

    def test_published_at_is_never_fabricated(self):
        """(14) An undated page stays undated, in the citation and in the prose."""
        undated = candidate(published=None)
        cit = citation_for_candidate(undated)
        assert cit.web.published_at is None
        assert "published" not in cit.source

        answer = engine(FakeIndex(index_result(undated))).research(plan_for(), jsw_context())
        assert "publication date not stated" in answer.content
        assert "published" not in answer.content.replace("publication", "")
        assert answer.freshest_published_at is None

    def test_retrieved_at_is_not_presented_as_a_publication_date(self):
        """(15) Retrieval time is labelled as retrieval, and it never feeds the
        'most recent evidence' line."""
        undated = candidate(published=None, retrieved=NOW)
        answer = engine(FakeIndex(index_result(undated))).research(plan_for(), jsw_context())

        assert "retrieved 20 Sep 2026" in answer.content
        assert "from 20 Sep 2026" not in answer.content
        assert "published 20 Sep 2026" not in answer.content
        assert "cannot establish how current" in answer.content
        cit = answer.used_citations[0]
        assert cit.web.retrieved_at == NOW and cit.web.published_at is None

    def test_exchange_references_are_counted_but_never_cited(self):
        reference = candidate(
            document_id=None, chunk_id=None, url="https://www.nseindia.com/ann/1",
            title="Expansion status intimation", text="", retrieved=None,
            origin=EXCHANGE_REFERENCE_ORIGIN, source_class="exchange", content_hash=None,
        )
        assert citation_for_candidate(reference) is None
        answer = engine(FakeIndex(index_result(candidate())),
                        FakeDiscovery(DiscoveryStatus.DISCOVERED, references=(reference,))
                        ).research(plan_for(), jsw_context())
        assert all(c.web.url != reference.source_url for c in answer.web_citations)
        assert answer.unfetched_references == 1
        assert "not fetched" in answer.content

    def test_unverifiable_candidates_never_become_citations(self):
        assert citation_for_candidate(candidate(url=None, canonical=None)) is None  # type: ignore[arg-type]
        assert citation_for_candidate(candidate(retrieved=None)) is None
        assert citation_for_candidate(candidate(text="   ")) is None

    def test_duplicate_pages_collapse_to_one_citation(self):
        answer = engine(FakeIndex(index_result(
            candidate(chunk_id=1), candidate(chunk_id=2, text=DELAYED),
        ))).research(plan_for(), jsw_context())
        assert len(answer.web_citations) == 1
        assert len(answer.used_citations) == 1


# ===========================================================================
# 16 / 17 / 22 — provider isolation and language rendering
# ===========================================================================
class TestProviderIsolationAndLanguage:

    def test_the_path_works_with_external_providers_disabled(self, monkeypatch):
        """(16) A real ProviderRouter with the flag off — never consulted."""
        monkeypatch.setattr(settings, "AI_EXTERNAL_PROVIDERS_ENABLED", False, raising=False)
        web = SpyWebEngine(engine(FakeIndex(index_result(candidate()))))
        a = ResearchAnalyst(
            StaticBuilder(jsw_context()), router=None, prompt_builder=PromptBuilder(),
            planner=planner(), composer=InternalComposer(),
            open_ended=InternalOpenEndedEngine(), web_research=web,
        )
        result = chat(a)
        assert result.provider == "deterministic"
        assert len(web.calls) == 1
        assert any(c.kind is EvidenceKind.WEB for c in result.citations)

    def test_no_external_llm_is_called_anywhere_on_the_path(self, monkeypatch):
        """(17) Every provider adapter's completion is a tripwire."""
        import app.services.ai.providers.router as router_module

        def tripwire(*_a, **_k):
            raise AssertionError("external provider invoked on the web research path")

        for name in ("openai", "gemini", "openrouter", "claude"):
            try:
                provider = importlib.import_module(f"app.services.ai.providers.{name}")
            except Exception:  # pragma: no cover - adapter optional
                continue
            for attr in dir(provider):
                obj = getattr(provider, attr)
                if inspect.isclass(obj) and hasattr(obj, "complete"):
                    monkeypatch.setattr(obj, "complete", tripwire, raising=False)
        monkeypatch.setattr(router_module.ProviderRouter, "complete", tripwire, raising=False)

        result = chat(analyst(SpyWebEngine(engine(FakeIndex(index_result(candidate())))),
                              router=router_module.ProviderRouter()))
        assert result.provider == "deterministic"

    def test_an_insufficient_result_never_falls_through_to_a_provider(self):
        """No WEB_RESEARCH → evidence gap → Gemini/OpenAI. The gap is the answer."""
        web = SpyWebEngine(engine(FakeIndex(index_result(corpus=0)),
                                  FakeDiscovery(DiscoveryStatus.DISABLED)))
        docs = SpyDocumentService(fail_on_search=True)
        a = analyst(web, router=SpyRouter("forbid"), docs=docs)
        result = chat(a)
        assert result.provider == "deterministic"
        assert web.results[0].status is WebResearchStatus.DISCOVERY_DISABLED
        assert a.router.complete_calls == [] and docs.search_calls == []

    def test_hindi_rendering_receives_the_annotated_answer_with_citations_and_numbers(self, monkeypatch):
        """(22) The existing adapter renders the display copy; the audited
        English artefact, its markers and its figures are what it is handed."""
        seen: dict = {}

        class Recording:
            name = "recording"

            def is_available(self):
                return True

            async def translate(self, text, language, *, entities=None):
                seen["text"] = text
                seen["entities"] = list(entities or [])
                return TranslationResult(text=f"(rendered) {text}", language=language,
                                         translated=True, provider=self.name)

        monkeypatch.setattr("app.services.language.adapter.build_translator",
                            lambda *a, **k: Recording())
        result = chat(analyst(SpyWebEngine(engine(FakeIndex(index_result(candidate()))))),
                      language=Language.HINDI)

        assert "[Company Website] JSW Steel – Investor Relations" in seen["text"]
        assert "34.2 MTPA" in seen["text"] and "18 Sep 2026" in seen["text"]
        assert JSW.name in seen["entities"] and JSW.ticker in seen["entities"]
        assert result.display_content.startswith("(rendered) ")
        assert "34.2 MTPA" in result.content   # canonical English untouched

    def test_the_internal_renderer_preserves_markers_and_figures_in_hinglish(self, monkeypatch):
        """The provider-free renderer keeps every protected span or leaves
        the sentence in English — it never drops a citation or a number."""
        from app.services.language.internal_renderer import InternalRendererTranslator

        monkeypatch.setattr("app.services.language.adapter.build_translator",
                            lambda *a, **k: InternalRendererTranslator())
        result = chat(analyst(SpyWebEngine(engine(FakeIndex(index_result(candidate()))))),
                      language=Language.HINGLISH)

        [web] = [c for c in result.citations if c.kind is EvidenceKind.WEB]
        assert f"[{web.label}]" in result.display_content
        for token in ("34.2", "5 MTPA", "18 Sep 2026", "20 Sep 2026"):
            assert token in result.display_content, token


# ===========================================================================
# 23 — filing crawl and web evidence crawl untouched
# ===========================================================================
class TestCrawlJobsUntouched:

    def test_filing_crawl_and_web_evidence_crawl_are_not_referenced(self):
        for token in ("filing_crawl", "web_evidence_crawl", "services.platform.jobs",
                      "filing_collection", "web.discovery", "web import discovery"):
            assert token not in CODE, token

    def test_the_job_handlers_do_not_reach_into_the_web_research_engine(self):
        handlers = (APP / "services" / "platform" / "jobs" / "handlers.py").read_text()
        assert "internal_web_research" not in handlers
        assert "InternalWebResearchEngine" not in handlers


# ===========================================================================
# Architecture
# ===========================================================================
def _imports(source: str) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def _analyst_method(name: str) -> str:
    source = ANALYST_PATH.read_text()
    start = source.index(f"def {name}(")
    end = source.find("\n    def ", start + 1)
    end2 = source.find("\n    async def ", start + 1)
    ends = [e for e in (end, end2) if e != -1]
    return source[start:min(ends) if ends else len(source)]


class TestArchitecture:

    def test_the_engine_imports_no_external_llm_provider(self):
        """(A1) Nothing that can reach a model or the network."""
        imported = _imports(CODE)
        for banned in ("openai", "anthropic", "google.generativeai", "google",
                       "httpx", "requests", "aiohttp", "urllib.request",
                       "app.services.ai.providers", "app.services.ai.providers.router",
                       "app.services.language.translators"):
            assert not any(n == banned or n.startswith(banned + ".") for n in imported), banned
        assert "ProviderRouter" not in CODE
        assert "CompletionRequest" not in CODE

    def test_the_web_research_route_needs_no_provider_router(self):
        """(A2) The analyst's seam never touches the router."""
        body = _analyst_method("_web_research")
        assert "router" not in body
        assert "self.prompts" not in body
        assert "_retrieve(" not in body

    def test_web_evidence_passes_through_the_citation_engine(self):
        """(A3) One funnel: the web answer goes to `_deterministic`, which is
        the only door to `_verify_and_record`."""
        source = ANALYST_PATH.read_text()
        seam = source[source.index("web = self._web_research("):]
        assert "await self._deterministic(" in seam[:700]
        assert "with_citations(" in seam[:700]
        assert source.count("def _verify_and_record(") == 1
        assert source.count("citation_audit = audit(") == 1
        assert "WebResearchAnswer" in source[
            source.index("async def _deterministic("):source.index("async def _deterministic(") + 500
        ]

    def test_the_engine_never_verifies_or_guardrails_anything_itself(self):
        for token in ("audit(", "check(", "enforce(", "annotate(", "GuardrailReport",
                      "CitationAudit", "LanguageAdapter"):
            assert token not in CODE, token

    def test_no_synthetic_citation_can_pass_verification(self):
        """(A4) Keys are minted from URLs by the one platform function; the
        engine has no minting rule of its own."""
        assert "mint_web_citation_key" in CODE or "citation_key()" in CODE
        assert 'f"web_' not in CODE and "'web_" not in CODE
        cit = citation_for_candidate(candidate())
        assert cit.key == cit.web.citation_key() == mint_web_citation_key(f"{ORIGIN}/investors")

    def test_the_grounded_context_receives_only_verified_web_evidence(self):
        """(A5) Every citation the engine publishes is a fetched page with a
        URL and a retrieval time; references and stubs are filtered before
        `with_citations` is ever reached."""
        mixed = (
            candidate(),
            candidate(document_id=None, chunk_id=None, url="https://www.bseindia.com/x",
                      title="Expansion status filing", text="", retrieved=None,
                      origin=EXCHANGE_REFERENCE_ORIGIN, source_class="exchange",
                      content_hash=None),
            candidate(document_id=13, url=f"{ORIGIN}/c", retrieved=None, content_hash="sha-13"),
        )
        answer = engine(FakeIndex(index_result(*mixed))).research(plan_for(), jsw_context())
        assert len(answer.web_citations) == 1
        for cit in answer.web_citations:
            assert cit.kind is EvidenceKind.WEB and cit.web is not None
            assert cit.web.url and cit.web.retrieved_at is not None

    def test_the_existing_deterministic_engines_are_untouched(self):
        """(A6) The engine reuses none of them and the analyst's order stands:
        resolver → composer → open-ended → web research → retrieval."""
        for token in ("FinancialAnswerEngine", "InvestmentAnswerEngine", "InternalComposer",
                      "InternalOpenEndedEngine", "FinancialIntentResolver"):
            assert token not in CODE, token
        source = ANALYST_PATH.read_text()
        order = [
            source.index("FinancialIntentResolver().resolve("),
            source.index("composed = self._compose("),
            source.index("internal = self._open_ended("),
            source.index("web = self._web_research("),
            source.index("retrieved = self._retrieve("),
        ]
        assert order == sorted(order)
        assert source.count("self._web_research(") == 1
        assert source.index("and context_override is None") < order[0]
        assert source.index("not directive.scope.is_restricted") < order[0]

    def test_the_provider_fallback_remains_for_unsupported_routes(self):
        """(A7) Nothing about the retrieval/provider path was edited away."""
        source = ANALYST_PATH.read_text()
        assert "retrieved = self._retrieve(" in source
        assert "await self.router.complete(" in source
        assert "from app.services.ai.providers.router import ProviderRouter" in source
        assert "self.router.stream(" in source

    def test_no_migration_was_added(self):
        """(A8) The engine reads; it never writes; no table backs it."""
        versions = BACKEND / "alembic" / "versions"
        for path in versions.glob("*.py"):
            assert "web_research" not in path.read_text().lower(), path.name
        for token in ("db.add(", "session.add(", ".commit(", ".flush(", ".delete(",
                      "Base.metadata", "create_all"):
            assert token not in CODE, token
        assert "app.models" not in CODE

    def test_no_api_route_or_schema_changed(self):
        """(A9) No new route; no request/response field; the chat endpoint is
        wired through the existing `analyst_for` opt-in only."""
        api = (APP / "api" / "v1" / "ai.py").read_text()
        schemas = (APP / "schemas" / "ai.py").read_text()
        for token in ("web_research", "WebResearch", "internal_web_research"):
            assert token not in api and token not in schemas, token

    def test_the_citation_contract_is_unchanged(self):
        """(A10) The serialised citation shape the frontend reads is exactly
        what it was: web citations ride on it (kind='web', source, snippet)."""
        from app.schemas.ai import CitationOut

        assert set(CitationOut.model_fields) == {
            "key", "label", "kind", "value", "unit", "source", "fiscal_year",
            "document_id", "chunk_id", "page", "confidence", "snippet",
        }
        cit = citation_for_candidate(candidate())
        out = CitationOut(key=cit.key, label=cit.label, kind=cit.kind.value, value=cit.value,
                          unit=cit.unit, source=cit.source, fiscal_year=cit.fiscal_year,
                          document_id=cit.document_id, chunk_id=cit.chunk_id, page=cit.page,
                          confidence=cit.confidence, snippet=cit.snippet)
        assert out.kind == "web" and "retrieved 20 Sep 2026" in out.source

    def test_the_engine_is_constructor_injected_and_off_by_default(self):
        params = inspect.signature(ResearchAnalyst.__init__).parameters
        assert "web_research" in params
        assert params["web_research"].kind is inspect.Parameter.KEYWORD_ONLY
        assert params["web_research"].default is None

    def test_the_composition_root_wires_it_only_with_composition(self):
        """`analyst_for(enable_composition=True)` is the one production
        construction site; the default analyst stays exactly as it was."""
        from app.services.ai.service import AIService
        from app.services.analysis_service import AnalysisService
        from tests.conftest import TestingSession

        service_source = SERVICE_PATH.read_text()
        assert "InternalWebResearchEngine.for_session(" in service_source
        # Every other production module stays clear of the engine.
        offenders = []
        for path in sorted(APP.rglob("*.py")):
            if "__pycache__" in path.parts or path in (MODULE_PATH, SERVICE_PATH, ANALYST_PATH):
                continue
            if "InternalWebResearchEngine" in path.read_text():
                offenders.append(str(path.relative_to(APP)))
        assert offenders == []

        session = TestingSession()
        try:
            analysis = AnalysisService.for_ticker(session, "BHARATCP", provision=False)
            assert analysis is not None
            plain = AIService(session).analyst_for(analysis)
            assert plain.web_research is None and plain.planner is None
            composed = AIService(session).analyst_for(analysis, enable_composition=True)
            assert isinstance(composed.web_research, InternalWebResearchEngine)
            assert isinstance(composed.planner, QuestionPlanner)
            # Built over the same session: the 4B index and 4C discovery.
            assert composed.web_research.index.db is session
            assert composed.web_research.discovery.db is session
        finally:
            session.rollback()
            session.close()

    def test_the_engine_module_is_pure_of_arithmetic_on_evidence(self):
        """No figure is ever computed from evidence: the module quotes."""
        tree = ast.parse(CODE)
        for node in ast.walk(tree):
            if isinstance(node, ast.BinOp):
                assert not isinstance(node.op, (ast.Div, ast.Mult, ast.Pow, ast.FloorDiv, ast.Mod))

    def test_the_web_query_generator_and_index_are_wired_only_here(self):
        for token in ("WebQueryGenerator", "SelfOwnedWebIndex", "TargetedWebDiscovery"):
            assert token in CODE, token
        offenders = []
        for path in sorted(APP.rglob("*.py")):
            if "__pycache__" in path.parts or path.parent.name == "planner":
                continue
            if path.parent.name == "web" or path == MODULE_PATH:
                continue
            source = path.read_text()
            if "WebQueryGenerator" in source or "SelfOwnedWebIndex" in source:
                offenders.append(str(path.relative_to(APP)))
        assert offenders == []

    def test_the_result_contract_is_explicit(self):
        assert {s.value for s in WebResearchStatus} == {
            "evidence_found", "stale_evidence", "conflicting_evidence",
            "insufficient_evidence", "discovery_disabled", "discovery_refused",
            "not_applicable",
        }
        found = WebResearchAnswer(status=WebResearchStatus.EVIDENCE_FOUND, content="x")
        gap = WebResearchAnswer(status=WebResearchStatus.INSUFFICIENT_EVIDENCE, content="x")
        na = WebResearchAnswer(status=WebResearchStatus.NOT_APPLICABLE)
        assert found.answered and found.applicable
        assert not gap.answered and gap.applicable
        assert not na.answered and not na.applicable
        assert gap.as_dict()["status"] == "insufficient_evidence"
