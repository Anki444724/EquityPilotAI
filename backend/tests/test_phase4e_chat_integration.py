"""Part 3 Phase 4E — end-to-end chat integration and production hardening.

Every test here drives the REAL application wiring:

    POST /company/{ticker}/ai/chat
      -> AIService.analyst_for(enable_composition=True)
      -> ResearchAnalyst.run -> QuestionPlanner -> WEB_RESEARCH
      -> InternalWebResearchEngine.for_session(db)
           -> WebQueryGenerator -> SelfOwnedWebIndex -> TargetedWebDiscovery
           -> WebSearchService -> WebFetcher (UrlSafetyPolicy, RobotsPolicy,
              HostPoliteness, redirect re-validation, byte/MIME limits)
           -> DocumentIngestionService (web_page documents)
      -> GroundedContext.with_citations -> citation audit -> guardrails
      -> LanguageAdapter -> ChatResponse

Nothing on the path is constructor-injected by these tests. The only seams
substituted are the three that reach a network in production — DNS
(``socket.getaddrinfo``), the ``robots.txt`` GET and the ``httpx`` client
behind ``HttpxTransport`` — plus document storage (a temp directory) and the
provider router (a tripwire that fails the test if any LLM is asked
anything). Every production object in between is the real one, observed
through spies that record calls and pass them straight through.
"""
from __future__ import annotations

import ast
import inspect
import re
import socket
import time
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import app.models as _models  # noqa: F401  (create_all must see every table)
from app.core.config import Settings, settings
from app.db.base import Base, get_db
from app.domain.ai.types import EvidenceKind
from app.domain.documents.types import DocumentStatus, DocumentType
from app.domain.filings.collection import CollectionStatus
from app.domain.platform.identity import Role
from app.domain.platform.plans import PlanTier
from app.domain.web.types import WebFetchPolicy, WebRejectionReason
from app.main import app
from app.models.company import Company
from app.models.document import Document, DocumentChunk, DocumentJob
from app.models.filing_collection import DiscoveredFiling
from app.schemas.ai import ChatResponse, CitationOut
from app.services.ai import analyst as analyst_module
from app.services.ai.financial_answer_engine import FinancialAnswerEngine
from app.services.ai.internal_open_ended import InternalOpenEndedEngine
from app.services.ai.internal_web_research import (
    InternalWebResearchEngine, WebResearchAnswer, WebResearchStatus,
)
from app.services.ai.planner import ExecutionRoute, QuestionPlanner
from app.services.ai.planner.web_query import WebQueryGenerator, WebQueryLimits
from app.services.ai.providers.router import ProviderRouter
from app.services.ai.service import AIService
from app.services.analysis_service import AnalysisService
from app.services.documents.ingestion import DocumentIngestionService
from app.services.documents.storage import LocalFileStorage
from app.services.documents.worker import DocumentWorker
from app.services.language.adapter import LanguageAdapter
from app.services.platform.cache import Namespace
from app.services.platform.entitlements import EntitlementService
from app.services.platform.identity_service import IdentityService
from app.services.platform.tenancy import TenantService
from app.services.web import fetcher as fetcher_module
from app.services.web.index import LOCAL_INDEX_ORIGIN, SelfOwnedWebIndex, WebIndexLimits
from app.services.web.service import WebSearchService
from app.services.web.targeted_discovery import (
    DiscoveryPolicy, DiscoveryStatus, DiscoveryTrigger, EXCHANGE_REFERENCE_ORIGIN,
    LIVE_FETCH_PERSISTED_ORIGIN, LIVE_FETCH_TRANSIENT_ORIGIN, TargetedWebDiscovery,
)
from tests.test_web_query_and_index import add_document

BACKEND = Path(__file__).resolve().parents[1]
APP = BACKEND / "app"

PASSWORD = "a-strong-test-password-1"
EMAIL = "phase4e@test.com"

HOST = "www.jsw.example"
ORIGIN = f"https://{HOST}"
ADDRESS = "93.184.216.34"          # a public address, as the resolver reports it
ROBOTS_ALLOW = "User-agent: *\nDisallow: /private\n"
NOW = datetime(2026, 9, 20, 9, 0, tzinfo=timezone.utc)

INVESTORS_TEXT = (
    "JSW Steel has commissioned the 5 MTPA expansion at Vijayanagar in "
    "September 2026. The expansion status was confirmed by the company at "
    "its investor meet. The Dolvi phase two expansion remains on track for "
    "completion by March 2027."
)
IR_TEXT = (
    "Investor relations contact details and the annual report archive are "
    "listed here for shareholders, analysts and institutional investors of "
    "the company. Quarterly results and presentations are published on this "
    "page after each board meeting."
)
FACTS = {
    "revenue": 12_000.0, "net_block_ppe": 6_000.0, "cash_and_bank": 1_000.0,
    "equity_share_capital": 500.0, "long_term_borrowings": 2_500.0,
    "short_term_borrowings": 250.0, "current_maturities_ltd": 100.0,
    "trade_receivables": 600.0, "inventories": 400.0, "trade_payables": 500.0,
    "raw_materials": 4_000.0, "employee_benefit": 800.0,
}
COMPANIES = {
    "JSWSTEEL": {"name": "JSW Steel Limited", "isin": "INE019A01038",
                 "website": ORIGIN, "sector": "Steel"},
    "TATASTEEL": {"name": "Tata Steel Limited", "isin": "INE081A01020",
                  "website": None, "sector": "Steel"},
    "NOWEB": {"name": "Noweb Industries Limited", "isin": "INE999Z01011",
              "website": None, "sector": "Chemicals"},
}


# ===========================================================================
# helpers
# ===========================================================================
def html(title: str, text: str, published: str | None = None) -> bytes:
    head = f"<title>{title}</title>"
    if published:
        head += f'<meta property="article:published_time" content="{published}">'
    return (
        f"<html><head>{head}</head><body><nav><a href='/about'>About</a>"
        f"<a href='https://www.other.example/x'>Partner</a></nav>"
        f"<main><h1>{title}</h1><p>{text}</p></main></body></html>"
    ).encode()


def _run(coro):
    import asyncio
    return asyncio.run(coro)


class Spy:
    """Records ``(args, kwargs, result)`` for every pass-through call."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple, dict, object]] = []

    @property
    def count(self) -> int:
        return len(self.calls)

    @property
    def last(self):
        return self.calls[-1][2]

    def results(self) -> list:
        return [r for _, _, r in self.calls]


def spy_on(monkeypatch, owner, name: str) -> Spy:
    original = getattr(owner, name)
    spy = Spy()
    if inspect.iscoroutinefunction(original):
        async def wrapper(*args, **kwargs):
            result = await original(*args, **kwargs)
            spy.calls.append((args, kwargs, result))
            return result
    else:
        def wrapper(*args, **kwargs):
            result = original(*args, **kwargs)
            spy.calls.append((args, kwargs, result))
            return result
    monkeypatch.setattr(owner, name, wrapper)
    return spy


class Net:
    """The three network seams, faked at the lowest level that exists."""

    def __init__(self) -> None:
        self.hosts: dict[str, list[str]] = {HOST: [ADDRESS]}
        self.pages: dict[str, httpx.Response | tuple] = {
            f"{ORIGIN}/investors": (
                "JSW Steel Investors", INVESTORS_TEXT, "2026-09-17T09:00:00+05:30",
            ),
            f"{ORIGIN}/investor-relations": ("Investor Relations", IR_TEXT, None),
        }
        self.robots: dict[str, str] = {HOST: ROBOTS_ALLOW}
        self.fetched: list[str] = []
        self.dns: list[str] = []
        self.robots_calls: list[str] = []
        self.connections: list = []
        self.sleeps: list[float] = []

    # -- seams ------------------------------------------------------------
    def getaddrinfo(self, host, port, *args, **kwargs):
        self.dns.append(host)
        addresses = self.hosts.get(host)
        if addresses is None:
            raise socket.gaierror(f"refused: unexpected DNS lookup for {host!r}")
        return [
            (socket.AF_INET6 if ":" in a else socket.AF_INET, socket.SOCK_STREAM,
             socket.IPPROTO_TCP, "", (a, port))
            for a in addresses
        ]

    def create_connection(self, *args, **kwargs):
        self.connections.append(args)
        raise AssertionError(f"a real socket connection was attempted: {args!r}")

    def robots_fetch(self, url, *, timeout, max_bytes, user_agent):
        self.robots_calls.append(url)
        host = httpx.URL(url).host
        body = self.robots.get(host)
        if body is None:
            return 404, b""
        return 200, body.encode()

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.fetched.append(url)
        spec = self.pages.get(url)
        if spec is None:
            return httpx.Response(404, text="not found")
        if isinstance(spec, httpx.Response):
            return spec
        title, text, published = spec
        return httpx.Response(
            200, headers={"content-type": "text/html; charset=utf-8"},
            content=html(title, text, published),
        )

    def fetched_hosts(self) -> set[str]:
        return {httpx.URL(u).host for u in self.fetched}


# ===========================================================================
# fixtures
# ===========================================================================
@pytest.fixture(scope="module")
def world(tmp_path_factory):
    """Authenticated client + three seeded companies on an isolated DB."""
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    SessionFactory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    with SessionFactory() as db:
        EntitlementService(db).sync_catalogue()
        tenant = TenantService(db).create("Phase 4E Capital", tier=PlanTier.ENTERPRISE)
        IdentityService(db).register(
            email=EMAIL, password=PASSWORD, name="Phase 4E",
            tenant_id=tenant.id, role=Role.ADMIN, auto_verify=True,
        )

    def _override():
        db = SessionFactory()
        try:
            yield db
        finally:
            db.close()

    prev_override = app.dependency_overrides.get(get_db)
    prev_native = settings.NATIVE_AUTH
    app.dependency_overrides[get_db] = _override
    settings.NATIVE_AUTH = True
    # Creating a company through the admin API schedules the platform's
    # pre-existing background live-quote refresh (a daemon thread that would
    # try Yahoo). That thread is not on the chat path and would only add
    # timing noise to the "no socket was opened" assertions, so it is parked
    # for this module.
    mp = pytest.MonkeyPatch()
    mp.setattr("app.services.live_market._REFRESHER.schedule", lambda symbol: None)

    client = TestClient(app)
    login = client.post("/api/v1/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert login.status_code == 200, login.text
    client.headers.update({"Authorization": f"Bearer {login.json()['access_token']}"})

    ids: dict[str, str] = {}
    for ticker, spec in COMPANIES.items():
        created = client.post("/api/v1/admin/companies", json={
            "name": spec["name"], "ticker": ticker, "isin": spec["isin"],
            "website": spec["website"], "sector": spec["sector"],
        })
        assert created.status_code == 201, created.text
        ids[ticker] = created.json()["id"]
        facts = [{"fiscal_year": 2024, "line_item": k, "value": v} for k, v in FACTS.items()]
        seeded = client.put(f"/api/v1/admin/financials/{ids[ticker]}/facts", json=facts)
        assert seeded.status_code == 200, seeded.text

    storage = LocalFileStorage(tmp_path_factory.mktemp("phase4e-docs"))
    yield types.SimpleNamespace(client=client, ids=ids, Session=SessionFactory,
                                storage=storage, engine=engine)

    mp.undo()
    settings.NATIVE_AUTH = prev_native
    if prev_override is not None:
        app.dependency_overrides[get_db] = prev_override
    else:
        app.dependency_overrides.pop(get_db, None)
    engine.dispose()


@pytest.fixture()
def net(monkeypatch, world) -> Net:
    """Fake DNS, robots and the httpx client; refuse every real socket.

    ``HostPoliteness`` keeps its real pacing logic — only the sleep it asks
    for is recorded instead of slept, so a run costs milliseconds while the
    delay it *would* have imposed stays assertable.
    """
    n = Net()
    monkeypatch.setattr(socket, "getaddrinfo", n.getaddrinfo)
    monkeypatch.setattr(socket, "create_connection", n.create_connection)
    monkeypatch.setattr("app.services.web.robots._default_fetch", n.robots_fetch)

    def _get_client(self):
        if self._client is None:
            self._client = httpx.Client(
                transport=httpx.MockTransport(n.handler), follow_redirects=False,
            )
        return self._client

    monkeypatch.setattr(fetcher_module.HttpxTransport, "_get_client", _get_client)
    monkeypatch.setattr(
        fetcher_module, "time",
        types.SimpleNamespace(
            sleep=lambda s: n.sleeps.append(s), monotonic=time.monotonic,
            perf_counter=time.perf_counter, time=time.time,
        ),
    )
    monkeypatch.setattr(
        "app.services.documents.ingestion.get_storage", lambda: world.storage,
    )
    # Production-safe defaults for this suite: the web path must work with
    # external providers OFF, and the internal renderer is the deterministic
    # language module that needs no key.
    monkeypatch.setattr(settings, "AI_EXTERNAL_PROVIDERS_ENABLED", False)
    monkeypatch.setattr(settings, "TRANSLATION_PROVIDER", "internal")
    monkeypatch.setattr(settings, "WEB_EVIDENCE_ENABLED", False)
    return n


class ProviderTripwire:
    """Fails the test if a provider is asked anything, unless ``allow`` is set
    (the tests of the pre-existing fallback routes set it and count calls)."""

    def __init__(self) -> None:
        self.calls: list = []
        self.allow = False

    def install(self, monkeypatch) -> None:
        original = ProviderRouter.complete
        tripwire = self

        async def complete(router, request, *, preferred=None, use_cache=True):
            tripwire.calls.append(request)
            if not tripwire.allow:
                raise AssertionError(
                    "ProviderRouter.complete was called on the internal web research path"
                )
            return await original(router, request, preferred=preferred, use_cache=use_cache)

        monkeypatch.setattr(ProviderRouter, "complete", complete)


@pytest.fixture()
def spies(monkeypatch, net):
    """Pass-through spies on every production object along the path."""
    tripwire = ProviderTripwire()
    tripwire.install(monkeypatch)
    return types.SimpleNamespace(
        plan=spy_on(monkeypatch, QuestionPlanner, "plan"),
        generate=spy_on(monkeypatch, WebQueryGenerator, "generate"),
        index=spy_on(monkeypatch, SelfOwnedWebIndex, "search"),
        discover=spy_on(monkeypatch, TargetedWebDiscovery, "discover"),
        research=spy_on(monkeypatch, InternalWebResearchEngine, "research"),
        open_ended=spy_on(monkeypatch, InternalOpenEndedEngine, "answer"),
        financial=spy_on(monkeypatch, FinancialAnswerEngine, "answer"),
        audit=spy_on(monkeypatch, analyst_module, "audit"),
        check=spy_on(monkeypatch, analyst_module, "check"),
        adapt=spy_on(monkeypatch, LanguageAdapter, "adapt"),
        ingest=spy_on(monkeypatch, DocumentIngestionService, "accept"),
        search=spy_on(monkeypatch, WebSearchService, "search"),
        provider=tripwire,
    )


def purge_documents(world, company_id: str) -> None:
    with world.Session() as db:
        ids = [d.id for d in db.scalars(select(Document).where(Document.company_id == company_id))]
        if ids:
            db.query(DocumentChunk).filter(DocumentChunk.document_id.in_(ids)).delete(synchronize_session=False)
            db.query(DocumentJob).filter(DocumentJob.document_id.in_(ids)).delete(synchronize_session=False)
            db.query(Document).filter(Document.id.in_(ids)).delete(synchronize_session=False)
        db.commit()


def documents_for(world, company_id: str) -> list[Document]:
    with world.Session() as db:
        return list(db.scalars(
            select(Document).where(Document.company_id == company_id).order_by(Document.id)
        ))


def chat(world, ticker: str, question: str, *, session_id: str = "phase4e", **extra) -> dict:
    response = world.client.post(
        f"/api/v1/company/{ticker}/ai/chat",
        json={"question": question, "session_id": session_id, **extra},
    )
    assert response.status_code == 200, response.text[:600]
    return response.json()


def analyst_for(world, db: Session, ticker: str):
    """The production analyst — `AIService.analyst_for` with the chat opt-in."""
    analysis = AnalysisService.for_ticker(db, ticker, provision=False)
    assert analysis is not None
    return AIService(db).analyst_for(analysis, enable_composition=True)


def memory_for(world, db, ticker: str):
    from app.services.ai.memory import ConversationMemory
    analysis = AnalysisService.for_ticker(db, ticker, provision=False)
    memory = ConversationMemory(session_id=f"phase4e-{ticker}")
    memory.set_company(analysis.company.id, analysis.company.ticker, analysis.company.name)
    return memory


def web_citations(body: dict) -> list[dict]:
    return [c for c in body["citations"] if c["kind"] == "web"]


def make_company(db, ticker: str, *, website: str | None, name: str | None = None) -> Company:
    company = Company(
        id=f"c-{ticker.lower()}", name=name or f"{ticker.title()} Limited",
        ticker=ticker, website=website, sector="Steel",
    )
    db.add(company)
    db.commit()
    return company


# ===========================================================================
# PART 1 — the runtime path is the production path
# ===========================================================================
class TestRuntimeWiring:
    def test_the_chat_endpoint_is_the_only_opt_in_and_it_wires_web_research(self):
        source = (APP / "api/v1/ai.py").read_text()
        chat_body = source[source.index("async def chat("):source.index("async def chat_stream(")]
        assert chat_body.count("analyst_for(analysis, enable_composition=True)") == 1
        # No other endpoint opts in; the streaming, analyse, report and
        # context endpoints keep the default analyst (no planner, no web).
        assert source.count("enable_composition=True") == 1
        for token in ("internal_web_research", "web_research", "WebResearch",
                      "targeted_discovery", "SelfOwnedWebIndex", "WebQueryGenerator"):
            assert token not in source, token
            assert token not in (APP / "schemas/ai.py").read_text(), token

    def test_for_session_builds_the_real_index_discovery_and_generator(self, world, net):
        with world.Session() as db:
            analyst = analyst_for(world, db, "JSWSTEEL")
            engine = analyst.web_research
            assert isinstance(engine, InternalWebResearchEngine)
            assert isinstance(engine.index, SelfOwnedWebIndex) and engine.index.db is db
            assert isinstance(engine.discovery, TargetedWebDiscovery) and engine.discovery.db is db
            assert isinstance(engine.generator, WebQueryGenerator)
            # The discovery layer's service is the production WebSearchService
            # with NOTHING injected: no transport, fetcher, safety or robots
            # override. The security stack it builds per search is the real one.
            service = engine.discovery.service
            assert isinstance(service, WebSearchService)
            assert service._transport is None
            assert service._fetcher_override is None
            assert service._safety_override is None
            assert service._resolver is None
            assert isinstance(service.ingestion, DocumentIngestionService)
            # The kill switch is read from settings, not frozen at construction.
            assert engine.discovery.enabled is False
            settings.WEB_EVIDENCE_ENABLED = True
            assert engine.discovery.enabled is True

    def test_the_default_analyst_has_no_web_research_at_all(self, world, net):
        with world.Session() as db:
            analysis = AnalysisService.for_ticker(db, "JSWSTEEL", provision=False)
            plain = AIService(db).analyst_for(analysis)
            assert plain.web_research is None and plain.planner is None
            assert plain.open_ended is None and plain.composer is None

    def test_no_second_construction_site_and_no_test_only_wiring(self):
        """The engine is constructed in exactly one production place, and the
        analyst dispatches it from exactly one seam."""
        constructors = []
        for path in sorted(APP.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            text = path.read_text()
            if "InternalWebResearchEngine(" in text or "InternalWebResearchEngine.for_session(" in text:
                constructors.append(str(path.relative_to(APP)))
        assert constructors == ["services/ai/service.py"]
        factory = (APP / "services/ai/internal_web_research.py").read_text()
        assert factory.count("def for_session(") == 1 and "return cls(" in factory
        analyst_source = (APP / "services/ai/analyst.py").read_text()
        assert analyst_source.count("self._web_research(") == 1
        assert analyst_source.count("engine.research(") == 1

    def test_nothing_on_the_path_talks_to_a_search_api_or_an_llm(self):
        banned = (
            "serpapi", "serper", "tavily", "searxng", "perplexity", "bing.com",
            "googleapis", "duckduckgo", "openai", "anthropic", "google.generativeai",
            "openrouter", "app.services.ai.providers",
        )
        for rel in ("services/ai/internal_web_research.py", "services/web/targeted_discovery.py",
                    "services/web/index.py", "services/ai/planner/web_query.py",
                    "services/web/service.py", "services/web/fetcher.py",
                    "services/web/safety.py", "services/web/robots.py"):
            text = (APP / rel).read_text().lower()
            tree = ast.parse((APP / rel).read_text())
            imported = {
                (n.module or "") if isinstance(n, ast.ImportFrom) else a.name
                for n in ast.walk(tree)
                if isinstance(n, (ast.Import, ast.ImportFrom))
                for a in (n.names if isinstance(n, ast.Import) else [None])
            }
            for token in banned:
                assert not any(token in (m or "") for m in imported), (rel, token)
                assert f"https://{token}" not in text and f"http://{token}" not in text, (rel, token)


# ===========================================================================
# PART 2 — the six required end-to-end questions, over HTTP
# ===========================================================================
class TestEndToEndQuestions:
    def test_a_expansion_status_is_answered_from_verified_web_evidence(self, world, net, spies):
        settings.WEB_EVIDENCE_ENABLED = True
        purge_documents(world, world.ids["JSWSTEEL"])

        body = chat(world, "JSWSTEEL", "JSW Steel expansion status kya hai?", session_id="4e-a")

        # 1. The planner named the route and the generator ran once.
        plan = spies.plan.last
        assert plan.execution_route is ExecutionRoute.WEB_RESEARCH
        assert spies.generate.count == 1
        queries = spies.generate.last
        assert list(queries.texts)[0].startswith("JSW Steel expansion status")
        # 2. The local index was searched first, scoped to the bound company.
        assert spies.index.count == 1
        assert spies.index.calls[0][1]["company_id"] == world.ids["JSWSTEEL"]
        # 3. Local evidence was absent, so bounded discovery ran and fetched.
        assert spies.discover.count == 1
        discovery = spies.discover.last
        assert discovery.status is DiscoveryStatus.DISCOVERED
        assert discovery.decision.trigger is DiscoveryTrigger.ABSENT
        assert net.fetched_hosts() == {HOST}
        assert net.fetched == [f"{ORIGIN}/investors", f"{ORIGIN}/investor-relations"]
        assert net.robots_calls == [f"https://{HOST}/robots.txt"]
        assert set(net.dns) == {HOST} and net.connections == []
        # 4. Evidence became verified WEB citations that reached the context
        #    the audit saw — added, with the computed evidence still present.
        answer = spies.research.last
        assert answer.status is WebResearchStatus.EVIDENCE_FOUND
        assert answer.web_citations and all(c.kind is EvidenceKind.WEB for c in answer.web_citations)
        audited = spies.audit.calls[-1][0][1]
        assert {c.key for c in answer.web_citations} <= {c.key for c in audited}
        assert any(c.kind is not EvidenceKind.WEB for c in audited)
        # 5. The citation engine and guardrails verified the answer.
        assert spies.audit.count == 1 and spies.check.count == 1
        assert spies.audit.last.unknown_keys == [] and spies.audit.last.uncited_numbers == []
        assert spies.check.last.passed is True
        # 6. The response: provider-free, cited, verified.
        assert body["provider"] == "deterministic" and body["model"] == "none"
        assert body["total_tokens"] == 0 and body["cost_usd"] == 0.0
        assert body["content"].startswith("Web evidence on JSW Steel Limited")
        assert "Verified by the platform: one verified page" in body["content"]
        assert "5 MTPA" in body["content"] and "Vijayanagar" in body["content"]
        web = web_citations(body)
        assert len(web) == 1
        assert web[0]["key"].startswith("web_") and web[0]["key"] in body["content"]
        assert web[0]["label"] == "[Company Website] JSW Steel Investors"
        assert "published 17 Sep 2026" in web[0]["source"] and "retrieved" in web[0]["source"]
        assert HOST in web[0]["source"]
        assert web[0]["document_id"] is not None
        assert body["citation_audit"]["is_supported"] is True
        assert body["citation_audit"]["unknown_keys"] == []
        assert body["guardrails"]["passed"] is True
        # 7. No provider was asked anything.
        assert spies.provider.calls == []

    def test_a_second_turn_is_served_from_the_local_index_once_indexed(self, world, net, spies):
        """Local first: after the real worker indexes the fetched page, the
        same question is answered from the stored index — nothing fetched."""
        settings.WEB_EVIDENCE_ENABLED = True
        purge_documents(world, world.ids["JSWSTEEL"])
        # The 4C sufficiency policy wants two strong, fresh local candidates
        # before it declines to look again, so both company pages speak to
        # the topic here.
        net.pages[f"{ORIGIN}/investor-relations"] = (
            "Investor Relations",
            "Expansion status update for investors: the Vijayanagar expansion "
            "phase was commissioned in September 2026 and the Dolvi expansion "
            "remains on schedule, the company said in its investor presentation.",
            "2026-09-18T09:00:00+05:30",
        )
        chat(world, "JSWSTEEL", "JSW Steel expansion status kya hai?", session_id="4e-a2")
        worker = DocumentWorker(world.Session, storage=world.storage)
        while worker.run_once():
            pass
        docs = documents_for(world, world.ids["JSWSTEEL"])
        assert docs and all(d.status == DocumentStatus.COMPLETED.value for d in docs), \
            [(d.id, d.status) for d in docs]

        net.fetched.clear(); net.dns.clear(); net.robots_calls.clear()
        body = chat(world, "JSWSTEEL", "JSW Steel expansion status kya hai?", session_id="4e-a2")

        discovery = spies.discover.last
        assert discovery.status is DiscoveryStatus.LOCAL_SUFFICIENT
        assert net.fetched == [] and net.dns == [] and net.robots_calls == []
        answer = spies.research.last
        assert answer.status is WebResearchStatus.EVIDENCE_FOUND
        assert LOCAL_INDEX_ORIGIN in body["content"]
        web = web_citations(body)
        assert web and {str(c["document_id"]) for c in web} <= {str(d.id) for d in docs}
        assert spies.provider.calls == []

    def test_b_latest_order_is_web_research_only_for_a_safely_bound_company(self, world, net, spies):
        settings.WEB_EVIDENCE_ENABLED = True
        purge_documents(world, world.ids["JSWSTEEL"])

        # (i) the URL binds the company; an unresolved entity defers to it.
        body = chat(world, "JSWSTEEL", "latest order kya hai?", session_id="4e-b1")
        plan = spies.plan.last
        assert plan.execution_route is ExecutionRoute.WEB_RESEARCH
        assert plan.entity.status.value == "unresolved"
        assert spies.research.count == 1
        assert spies.index.calls[-1][1]["company_id"] == world.ids["JSWSTEEL"]
        assert net.fetched_hosts() == {HOST}
        assert body["provider"] == "deterministic"
        assert body["content"].startswith(
            "Current web evidence is insufficient to answer this about JSW Steel Limited"
        )
        assert spies.provider.calls == []

        # (ii) a question naming a company the platform does not know: no
        #      domain is derived from the name, the bound company still scopes.
        net.fetched.clear(); net.dns.clear()
        body = chat(world, "JSWSTEEL", "Acme Widgets Corp ka latest order kya hai?", session_id="4e-b2")
        assert spies.plan.last.entity.status.value == "unresolved"
        assert set(net.dns) <= {HOST}
        assert "acme" not in " ".join(net.dns).lower()
        assert body["company"]["ticker"] == "JSWSTEEL"

        # (iii) two companies named: the planner reports ambiguity, the web
        #       layer is refused, and the EXISTING provider fallback serves it.
        net.fetched.clear(); net.dns.clear()
        research_before = spies.research.count
        spies.provider.allow = True
        body = chat(world, "JSWSTEEL", "JSWSTEEL aur TATASTEEL ka latest order kya hai?",
                    session_id="4e-b3")
        assert spies.plan.last.entity.status.value == "ambiguous"
        assert spies.research.count == research_before      # never consulted
        assert net.fetched == [] and net.dns == []
        assert len(spies.provider.calls) == 1
        assert body["provider"] != "deterministic"

    def test_c_a_source_restriction_wins_and_nothing_is_fetched(self, world, net, spies):
        settings.WEB_EVIDENCE_ENABLED = True
        body = chat(world, "JSWSTEEL", "only uploaded documents me recent announcement batao",
                    session_id="4e-c")
        assert body["provider"] == "source-router"
        assert body["content"] == "No uploaded document matching this question is currently indexed."
        assert spies.plan.count == 0 and spies.research.count == 0
        assert spies.discover.count == 0 and spies.index.count == 0
        assert net.fetched == [] and net.dns == [] and net.robots_calls == []
        assert spies.provider.calls == []
        assert web_citations(body) == []

    def test_d_a_financial_intent_takes_the_existing_deterministic_path(self, world, net, spies):
        settings.WEB_EVIDENCE_ENABLED = True
        body = chat(world, "JSWSTEEL", "company ka P/E kya hai?", session_id="4e-d")
        assert spies.financial.count == 1
        assert spies.financial.calls[0][0][1].value == "pe"
        assert body["provider"] == "deterministic"
        assert "P/E" in body["content"]
        assert spies.plan.count == 0 and spies.research.count == 0
        assert net.fetched == [] and net.dns == []
        assert spies.provider.calls == []

    def test_e_an_open_ended_question_stays_with_the_internal_engine(self, world, net, spies):
        settings.WEB_EVIDENCE_ENABLED = True
        body = chat(world, "JSWSTEEL", "What is the sector?", session_id="4e-e")
        assert spies.plan.last.execution_route is ExecutionRoute.INTERNAL_REASONING
        assert spies.open_ended.count == 1
        assert body["provider"] == "deterministic"
        assert "[company_sector]" in body["content"] and "Steel" in body["content"]
        assert spies.research.count == 0 and spies.generate.count == 0
        assert net.fetched == [] and net.dns == []
        assert spies.provider.calls == []

    def test_f_no_verified_origin_means_an_honest_insufficiency_and_no_guessing(self, world, net, spies):
        settings.WEB_EVIDENCE_ENABLED = True
        body = chat(world, "NOWEB", "Noweb Industries expansion status kya hai?", session_id="4e-f")
        assert spies.plan.last.execution_route is ExecutionRoute.WEB_RESEARCH
        assert spies.discover.last.status is DiscoveryStatus.NO_TARGET
        assert spies.research.last.status is WebResearchStatus.DISCOVERY_REFUSED
        assert body["provider"] == "deterministic"
        assert body["content"].startswith(
            "Current web evidence is insufficient to answer this about Noweb Industries Limited"
        )
        assert "no verified company-owned origin" in body["content"]
        assert "never guesses a URL" in body["content"]
        assert net.dns == [] and net.fetched == [] and net.robots_calls == []
        assert web_citations(body) == []
        assert body["citation_audit"]["unknown_keys"] == []
        assert body["guardrails"]["passed"] is True
        assert spies.provider.calls == []

    def test_f_pages_that_do_not_speak_to_the_topic_are_not_stretched(self, world, net, spies):
        settings.WEB_EVIDENCE_ENABLED = True
        purge_documents(world, world.ids["JSWSTEEL"])
        body = chat(world, "JSWSTEEL", "JSW Steel latest acquisition news kya hai?", session_id="4e-f2")
        assert spies.plan.last.execution_route is ExecutionRoute.WEB_RESEARCH
        # The company's pages were fetched (through the safety stack) and
        # none speaks to the topic: reported as such, never stretched.
        # ("news" selects other fixed-table paths — press releases, media —
        # which this site does not serve; nothing outside the table is tried.)
        assert net.fetched and net.fetched_hosts() == {HOST}
        assert all(httpx.URL(u).path.lstrip("/") in
                   {"investors", "investor-relations", "press-releases", "media", "news", "newsroom"}
                   for u in net.fetched), net.fetched
        assert spies.discover.last.status is DiscoveryStatus.NOTHING_FOUND
        assert all(r.reason == WebRejectionReason.HTTP_ERROR.value for r in spies.discover.last.refusals)
        assert spies.research.last.status is WebResearchStatus.DISCOVERY_REFUSED
        assert body["content"].startswith(
            "Current web evidence is insufficient to answer this about JSW Steel Limited"
        )
        # A missing page is reported as unavailable — not dressed up as a
        # safety refusal, and never as evidence.
        assert "no page was obtained from the company's own origins" in body["content"]
        assert "acquisition" in body["content"]
        assert "No answer is inferred beyond the evidence" in body["content"]
        assert body["provider"] == "deterministic"

    def test_f_fetched_pages_off_the_topic_are_reported_not_stretched(self, world, net, spies):
        settings.WEB_EVIDENCE_ENABLED = True
        purge_documents(world, world.ids["JSWSTEEL"])
        body = chat(world, "JSWSTEEL", "JSW Steel latest order kya hai?", session_id="4e-f3")
        assert spies.plan.last.execution_route is ExecutionRoute.WEB_RESEARCH
        assert net.fetched == [f"{ORIGIN}/investors", f"{ORIGIN}/investor-relations"]
        assert spies.research.last.status is WebResearchStatus.INSUFFICIENT_EVIDENCE
        assert "none carried a passage about" in body["content"] and "order" in body["content"]
        assert web_citations(body) == [] and body["provider"] == "deterministic"
        assert web_citations(body) == []
        assert spies.provider.calls == []


# ===========================================================================
# PART 3 — provider isolation
# ===========================================================================
class TestProviderIsolation:
    def test_the_whole_web_path_runs_with_external_providers_disabled(self, world, net, spies):
        assert settings.AI_EXTERNAL_PROVIDERS_ENABLED is False
        settings.WEB_EVIDENCE_ENABLED = True
        purge_documents(world, world.ids["JSWSTEEL"])
        body = chat(world, "JSWSTEEL", "What is the latest expansion status of JSW Steel?",
                    session_id="4e-iso")
        assert body["provider"] == "deterministic"
        assert web_citations(body)
        assert spies.provider.calls == []
        # Nothing that could reach a vendor was resolved or connected to.
        assert set(net.dns) == {HOST} and net.connections == []

    def test_the_default_settings_object_keeps_the_offline_only_registry(self, monkeypatch):
        """With providers disabled the router registry holds no vendor row, so
        no `preferred` value can resurrect one."""
        monkeypatch.setattr(settings, "AI_EXTERNAL_PROVIDERS_ENABLED", False)
        router = ProviderRouter()
        assert all(c.payload_shape == "offline" for c in router.configs)

    def test_unsupported_non_web_routes_keep_the_existing_fallback(self, world, net, spies):
        settings.WEB_EVIDENCE_ENABLED = True
        spies.provider.allow = True
        body = chat(world, "JSWSTEEL", "Is this a good company?", session_id="4e-fallback")
        assert spies.plan.last.execution_route is ExecutionRoute.DECLINE
        assert spies.research.count == 0
        assert len(spies.provider.calls) == 1
        assert body["provider"] != "deterministic" and body["content"]
        assert net.fetched == []

    def test_the_documented_arithmetic_limitation_still_takes_the_provider_path(self, world, net, spies):
        """Known and deliberately unchanged in 4E: literal-number arithmetic
        routes to INTERNAL_REASONING, the Part 2D engine declines it, and the
        pre-existing provider fallback answers. Web research is never asked."""
        settings.WEB_EVIDENCE_ENABLED = True
        spies.provider.allow = True
        body = chat(world, "JSWSTEEL", "₹500 se ₹650 kitna percent increase hai?",
                    session_id="4e-arith")
        assert spies.plan.last.execution_route is ExecutionRoute.INTERNAL_REASONING
        assert spies.open_ended.count == 1 and not spies.open_ended.last.answered
        assert spies.research.count == 0
        assert len(spies.provider.calls) == 1
        assert body["provider"] != "deterministic"
        assert net.fetched == []

    def test_an_evidence_gap_never_falls_through_to_a_provider(self, world, net, spies):
        settings.WEB_EVIDENCE_ENABLED = False
        purge_documents(world, world.ids["JSWSTEEL"])
        body = chat(world, "JSWSTEEL", "JSW Steel latest expansion update batao", session_id="4e-gap")
        assert spies.research.last.status is WebResearchStatus.DISCOVERY_DISABLED
        assert body["provider"] == "deterministic"
        assert spies.provider.calls == []


# ===========================================================================
# PART 4 — feature flags
# ===========================================================================
class TestFeatureFlags:
    def test_disabled_means_no_discovery_no_fetch_no_network_no_fake_answer(self, world, net, spies):
        settings.WEB_EVIDENCE_ENABLED = False
        purge_documents(world, world.ids["JSWSTEEL"])
        before = len(documents_for(world, world.ids["JSWSTEEL"]))

        body = chat(world, "JSWSTEEL", "JSW Steel expansion status kya hai?", session_id="4e-off")

        assert spies.discover.count == 1
        assert spies.discover.last.status is DiscoveryStatus.DISABLED
        assert spies.search.count == 0 and spies.ingest.count == 0
        assert net.dns == [] and net.robots_calls == [] and net.fetched == []
        assert net.connections == []
        assert spies.research.last.status is WebResearchStatus.DISCOVERY_DISABLED
        assert "WEB_EVIDENCE_ENABLED is off" in body["content"]
        assert "insufficient" in body["content"]
        assert web_citations(body) == [] and body["citations"] == []
        assert body["provider"] == "deterministic"
        assert len(documents_for(world, world.ids["JSWSTEEL"])) == before
        assert spies.provider.calls == []

    def test_disabled_still_answers_from_stored_pages_without_a_network(self, world, net, spies):
        settings.WEB_EVIDENCE_ENABLED = False
        purge_documents(world, world.ids["JSWSTEEL"])
        with world.Session() as db:
            add_document(
                db, world.ids["JSWSTEEL"], "JSW Steel – Investor Relations",
                DocumentType.WEB_PAGE.value,
                ["JSW Steel expansion status: the 5 MTPA Vijayanagar expansion was "
                 "commissioned in September 2026 and is fully operational."],
                url=f"{ORIGIN}/investors", source_class="company_website",
                published=datetime(2026, 9, 15, tzinfo=timezone.utc),
                retrieved=datetime(2026, 9, 19, tzinfo=timezone.utc),
                status=DocumentStatus.COMPLETED.value,
            )
            add_document(
                db, world.ids["JSWSTEEL"], "JSW Steel – Press Release",
                DocumentType.WEB_PAGE.value,
                ["JSW Steel expansion status update: the Dolvi expansion phase two "
                 "remains on track for completion by March 2027."],
                url=f"{ORIGIN}/press-releases", source_class="company_website",
                published=datetime(2026, 9, 16, tzinfo=timezone.utc),
                retrieved=datetime(2026, 9, 19, tzinfo=timezone.utc),
                status=DocumentStatus.COMPLETED.value,
            )
        body = chat(world, "JSWSTEEL", "JSW Steel expansion status kya hai?", session_id="4e-off2")
        assert spies.discover.last.status is DiscoveryStatus.LOCAL_SUFFICIENT
        assert net.dns == [] and net.fetched == []
        assert spies.research.last.status is WebResearchStatus.EVIDENCE_FOUND
        assert web_citations(body) and "5 MTPA" in body["content"]
        assert spies.provider.calls == []
        purge_documents(world, world.ids["JSWSTEEL"])

    def test_enabled_allows_discovery_only_through_the_safety_stack(self, world, net, spies):
        settings.WEB_EVIDENCE_ENABLED = True
        purge_documents(world, world.ids["JSWSTEEL"])
        net.robots[HOST] = "User-agent: *\nDisallow: /investors\n"
        body = chat(world, "JSWSTEEL", "JSW Steel expansion status kya hai?", session_id="4e-on")
        # robots was consulted before any page and honoured on the runtime path
        assert net.robots_calls == [f"https://{HOST}/robots.txt"]
        assert f"{ORIGIN}/investors" not in net.fetched
        assert f"{ORIGIN}/investor-relations" in net.fetched
        discovery = spies.discover.last
        assert any(r.reason == WebRejectionReason.ROBOTS_DISALLOWED.value for r in discovery.refusals)
        assert web_citations(body) == []
        assert spies.provider.calls == []

    def test_the_setting_defaults_are_safe(self):
        defaults = Settings(_env_file=None)
        assert defaults.WEB_EVIDENCE_ENABLED is False
        assert isinstance(defaults.AI_EXTERNAL_PROVIDERS_ENABLED, bool)
        example = (BACKEND / ".env.example").read_text()
        assert re.search(r"^WEB_EVIDENCE_ENABLED=false$", example, re.M)


# ===========================================================================
# PART 5 — security hardening on the runtime path (service level, production
# wiring, per-scenario companies)
# ===========================================================================
@pytest.fixture()
def scenario(world, net, spies):
    """One company per scenario, answered through `AIService.analyst_for`."""
    settings.WEB_EVIDENCE_ENABLED = True
    created: list[str] = []

    def run(ticker: str, website: str | None, question: str = "expansion status kya hai?",
            *, language=None):
        with world.Session() as db:
            company = make_company(db, ticker, website=website)
            created.append(company.id)
            analyst = analyst_for(world, db, ticker)
            memory = memory_for(world, db, ticker)
            result = _run(analyst.chat(question, memory, language=language))
            return result, company

    yield run

    with world.Session() as db:
        for company_id in created:
            purge_documents(world, company_id)
            db.query(Company).filter(Company.id == company_id).delete()
        db.commit()


class TestSecurityOnTheRuntimePath:
    def _refusal_reasons(self, spies) -> set[str]:
        return {r.reason for r in spies.discover.last.refusals}

    @pytest.mark.parametrize("website,reason", [
        ("http://169.254.169.254", WebRejectionReason.ADDRESS_NOT_PUBLIC.value),   # cloud metadata
        ("http://10.0.0.5", WebRejectionReason.ADDRESS_NOT_PUBLIC.value),          # private range
        ("http://127.0.0.1", WebRejectionReason.ADDRESS_NOT_PUBLIC.value),         # loopback
        ("http://[fd00:ec2::254]", WebRejectionReason.ADDRESS_NOT_PUBLIC.value),   # IMDS v6
    ])
    def test_a_company_row_pointing_at_a_non_public_address_is_refused(self, scenario, net, spies, website, reason):
        result, _ = scenario("SSRF1", website)
        assert net.fetched == [] and net.connections == [] and net.dns == []
        reasons = self._refusal_reasons(spies)
        # A bracketed IPv6 literal is additionally rejected by host pinning
        # (the canonical host form differs from the row); either way the
        # address is never connected to.
        assert reason in reasons or reasons == {WebRejectionReason.HOST_NOT_PINNED.value}
        assert spies.research.last.status in {
            WebResearchStatus.DISCOVERY_REFUSED, WebResearchStatus.INSUFFICIENT_EVIDENCE,
        }
        assert "insufficient" in result.content
        assert result.citations == []
        assert spies.provider.calls == []

    def test_a_pinned_host_resolving_to_a_private_address_is_refused_before_connecting(self, scenario, net, spies):
        """DNS rebinding, first form: the name is on the allowlist, the answer
        is not public. Checked on every address before a socket exists."""
        net.hosts["ir.rebind.example"] = ["93.184.216.34", "10.0.0.9"]
        net.pages["https://ir.rebind.example/investors"] = ("Rebind", INVESTORS_TEXT, None)
        result, _ = scenario("SSRF2", "https://ir.rebind.example")
        assert "ir.rebind.example" in net.dns
        assert net.fetched == [] and net.connections == []
        assert WebRejectionReason.ADDRESS_NOT_PUBLIC.value in self._refusal_reasons(spies)
        assert result.citations == []

    def test_a_redirect_off_the_pinned_host_is_not_followed(self, scenario, net, spies):
        net.hosts["www.redirect.example"] = [ADDRESS]
        net.robots["www.redirect.example"] = ROBOTS_ALLOW
        net.pages["https://www.redirect.example/investors"] = httpx.Response(
            302, headers={"location": "http://127.0.0.1/admin"},
        )
        net.pages["https://www.redirect.example/investor-relations"] = httpx.Response(
            302, headers={"location": "https://www.other.example/steal"},
        )
        result, _ = scenario("REDIR", "https://www.redirect.example")
        assert set(net.fetched_hosts()) == {"www.redirect.example"}
        assert "www.other.example" not in net.dns
        reasons = self._refusal_reasons(spies)
        assert reasons & {WebRejectionReason.ADDRESS_NOT_PUBLIC.value,
                          WebRejectionReason.HOST_NOT_PINNED.value}
        assert result.citations == []

    def test_a_redirect_loop_and_redirect_limit_are_enforced(self, scenario, net, spies):
        net.hosts["www.loop.example"] = [ADDRESS]
        net.robots["www.loop.example"] = ROBOTS_ALLOW
        net.pages["https://www.loop.example/investors"] = httpx.Response(
            301, headers={"location": "https://www.loop.example/investors?x=1"},
        )
        net.pages["https://www.loop.example/investors?x=1"] = httpx.Response(
            301, headers={"location": "https://www.loop.example/investors?x=2"},
        )
        net.pages["https://www.loop.example/investors?x=2"] = httpx.Response(
            301, headers={"location": "https://www.loop.example/investors?x=3"},
        )
        net.pages["https://www.loop.example/investors?x=3"] = httpx.Response(
            301, headers={"location": "https://www.loop.example/investors?x=4"},
        )
        scenario("LOOP", "https://www.loop.example")
        hops = [u for u in net.fetched if u.startswith("https://www.loop.example/investors")]
        assert len(hops) <= WebFetchPolicy().max_redirects + 1
        assert WebRejectionReason.REDIRECT_LIMIT.value in self._refusal_reasons(spies)

    def test_robots_disallow_and_crawl_delay_are_honoured(self, scenario, net, spies):
        net.hosts["www.polite.example"] = [ADDRESS]
        net.robots["www.polite.example"] = "User-agent: *\nCrawl-delay: 3\nDisallow: /investor-relations\n"
        net.pages["https://www.polite.example/investors"] = ("Polite", INVESTORS_TEXT, None)
        scenario("POLITE", "https://www.polite.example")
        assert "https://www.polite.example/investor-relations" not in net.fetched
        assert WebRejectionReason.ROBOTS_DISALLOWED.value in self._refusal_reasons(spies)

    def test_host_politeness_paces_requests_to_one_host(self, scenario, net, spies):
        """Two pages on one origin: the second waits the default crawl delay.
        The sleep is recorded rather than slept, so its size is assertable."""
        scenario("PACE", ORIGIN)
        assert len(net.fetched) == 2
        assert net.sleeps and max(net.sleeps) >= WebFetchPolicy().default_crawl_delay * 0.9

    def test_the_byte_cap_is_enforced_on_bytes_received(self, scenario, net, spies):
        big = b"<html><body><p>" + b"x" * (WebFetchPolicy().max_bytes + 1024) + b"</p></body></html>"
        net.hosts["www.huge.example"] = [ADDRESS]
        net.robots["www.huge.example"] = ROBOTS_ALLOW
        net.pages["https://www.huge.example/investors"] = httpx.Response(
            200, headers={"content-type": "text/html", "content-length": "10"}, content=big,
        )
        result, _ = scenario("HUGE", "https://www.huge.example")
        assert WebRejectionReason.TOO_LARGE.value in self._refusal_reasons(spies)
        assert result.citations == []

    def test_unsupported_mime_types_are_refused(self, scenario, net, spies):
        net.hosts["www.mime.example"] = [ADDRESS]
        net.robots["www.mime.example"] = ROBOTS_ALLOW
        net.pages["https://www.mime.example/investors"] = httpx.Response(
            200, headers={"content-type": "application/octet-stream"}, content=b"\x00\x01binary",
        )
        net.pages["https://www.mime.example/investor-relations"] = httpx.Response(
            200, headers={"content-type": "application/zip"}, content=b"PK\x03\x04",
        )
        result, _ = scenario("MIME", "https://www.mime.example")
        assert WebRejectionReason.CONTENT_TYPE_NOT_ALLOWED.value in self._refusal_reasons(spies)
        assert result.citations == []

    def test_links_in_fetched_pages_are_never_followed_and_depth_is_zero(self, scenario, net, spies):
        scenario("LINKS", ORIGIN)
        # The seed pages link to /about and to www.other.example — neither is fetched.
        assert all(u in {f"{ORIGIN}/investors", f"{ORIGIN}/investor-relations"} for u in net.fetched)
        assert "www.other.example" not in net.dns
        assert DiscoveryPolicy().max_depth == 0 and DiscoveryPolicy.MAX_DEPTH_CEILING == 0

    def test_only_conventional_paths_on_the_company_origin_are_ever_planned(self, scenario, net, spies):
        scenario("PATHS", ORIGIN, "JSW ka latest annual report aur expansion update batao")
        seeds = spies.discover.last.seeds
        assert seeds and all(s.host == HOST for s in seeds)
        assert len(seeds) <= DiscoveryPolicy().max_seed_urls
        assert all(s.basis.startswith("path:") or s.basis == "verified_ir_url" for s in seeds)
        assert len(net.fetched) <= DiscoveryPolicy().max_fetched_pages

    def test_a_non_http_scheme_on_the_company_row_is_not_fetched(self, scenario, net, spies):
        result, _ = scenario("FILE", "file:///etc/passwd")
        assert net.fetched == [] and net.dns == [] and net.connections == []
        assert spies.discover.last.status is DiscoveryStatus.NO_TARGET
        assert result.citations == []

    def test_duplicate_content_across_two_urls_is_cited_once(self, scenario, net, spies):
        net.hosts["www.dupe.example"] = [ADDRESS]
        net.robots["www.dupe.example"] = ROBOTS_ALLOW
        net.pages["https://www.dupe.example/investors"] = ("Same", INVESTORS_TEXT, None)
        net.pages["https://www.dupe.example/investor-relations"] = ("Same", INVESTORS_TEXT, None)
        result, _ = scenario("DUPE", "https://www.dupe.example")
        assert WebRejectionReason.DUPLICATE_CONTENT.value in self._refusal_reasons(spies)
        assert len([c for c in result.citations if c.kind is EvidenceKind.WEB]) == 1

    def test_the_fetcher_on_the_chat_path_is_built_with_the_pinned_allowlist_only(self, world, net):
        with world.Session() as db:
            analyst = analyst_for(world, db, "JSWSTEEL")
            service = analyst.web_research.discovery.service
            company = db.get(Company, world.ids["JSWSTEEL"])
            state, pinned = service.allowlist_for(company)
            fetcher = service._fetcher_for(pinned)
            assert fetcher.safety.allowed_hosts == frozenset(pinned)
            assert HOST in fetcher.safety.allowed_hosts
            assert not fetcher.safety.host_is_pinned("www.google.com")
            assert not fetcher.safety.host_is_pinned("api.openai.com")
            assert fetcher.safety.allowed_ports == frozenset({80, 443})
            assert fetcher.policy.max_redirects == WebFetchPolicy().max_redirects
            assert fetcher.robots is service._robots


# ===========================================================================
# PART 6 — citation hardening through the funnel
# ===========================================================================
class TestCitationHardening:
    def test_a_forged_or_unknown_key_is_flagged_by_the_endpoint(self, world, net, spies, monkeypatch):
        settings.WEB_EVIDENCE_ENABLED = True
        purge_documents(world, world.ids["JSWSTEEL"])
        original = InternalWebResearchEngine.synthesise

        def forged(self, *args, **kwargs):
            answer = original(self, *args, **kwargs)
            content = answer.content + (
                "\nA forged claim about 9 MTPA of capacity [web_forged_deadbeef]."
            )
            return WebResearchAnswer(
                status=answer.status, content=content, web_citations=answer.web_citations,
                used_citations=answer.used_citations, missing=answer.missing,
                queries=answer.queries, topic_terms=answer.topic_terms,
                company_id=answer.company_id, recency_sensitive=answer.recency_sensitive,
                freshest_published_at=answer.freshest_published_at,
                discovery_status=answer.discovery_status,
                local_candidates=answer.local_candidates,
                unfetched_references=answer.unfetched_references,
                notes=answer.notes, reason=answer.reason,
            )

        monkeypatch.setattr(InternalWebResearchEngine, "synthesise", forged)
        body = chat(world, "JSWSTEEL", "JSW Steel expansion status kya hai?", session_id="4e-forged")
        audit = body["citation_audit"]
        assert audit["unknown_keys"] == ["web_forged_deadbeef"]
        assert audit["is_supported"] is False
        assert all(c["key"] != "web_forged_deadbeef" for c in body["citations"])
        # The forged marker is left visible in the display rather than dressed up.
        assert "[web_forged_deadbeef]" in body["display_content"]

    def test_urls_canonical_urls_and_document_rows_agree(self, world, net, spies):
        settings.WEB_EVIDENCE_ENABLED = True
        purge_documents(world, world.ids["JSWSTEEL"])
        body = chat(world, "JSWSTEEL", "JSW Steel expansion status kya hai?", session_id="4e-url")
        answer = spies.research.last
        cited = answer.used_citations[0]
        assert cited.web is not None
        assert cited.web.url == f"{ORIGIN}/investors"
        assert cited.web.canonical_url == f"{ORIGIN}/investors"
        assert cited.key == cited.web.citation_key()
        assert cited.web.published_at is not None and cited.web.retrieved_at is not None
        assert cited.web.published_at.date() == datetime(2026, 9, 17).date()
        docs = {d.id: d for d in documents_for(world, world.ids["JSWSTEEL"])}
        row = docs[int(cited.document_id)]
        assert row.source_url == f"{ORIGIN}/investors"
        assert row.doc_type == DocumentType.WEB_PAGE.value
        assert row.doc_metadata["web"]["canonical_url"] == f"{ORIGIN}/investors"
        assert row.published_at is not None
        out = web_citations(body)[0]
        assert str(out["document_id"]) == str(row.id) and HOST in out["source"]

    def test_published_at_is_never_fabricated_and_retrieved_at_is_labelled(self, scenario, net, spies):
        net.hosts["www.undated.example"] = [ADDRESS]
        net.robots["www.undated.example"] = ROBOTS_ALLOW
        net.pages["https://www.undated.example/investors"] = ("Undated Investors", INVESTORS_TEXT, None)
        result, _ = scenario("UNDATED", "https://www.undated.example",
                             "Undated ka latest expansion status kya hai?")
        cited = [c for c in result.citations if c.kind is EvidenceKind.WEB]
        assert cited and cited[0].web.published_at is None
        assert "published" not in cited[0].source.split("retrieved")[0].replace("Undated", "")
        assert "publication date not stated" in result.content
        assert "the retrieval date is when the platform read the page" in result.content
        assert "cannot establish how current" in result.content

    def test_exchange_references_that_were_not_fetched_are_never_cited(self, world, net, spies):
        settings.WEB_EVIDENCE_ENABLED = True
        purge_documents(world, world.ids["JSWSTEEL"])
        with world.Session() as db:
            db.add(DiscoveredFiling(
                company_id=world.ids["JSWSTEEL"], source="bse",
                source_reference="4e-ref-1",
                source_url="https://www.bseindia.com/xml-data/corpfiling/AttachLive/jsw-expansion.pdf",
                title="JSW Steel expansion status update: Vijayanagar 5 MTPA commissioned",
                status=CollectionStatus.DISCOVERED.value if hasattr(CollectionStatus, "DISCOVERED") else "discovered",
                published_on=datetime(2026, 9, 10, tzinfo=timezone.utc),
            ))
            db.commit()
        try:
            body = chat(world, "JSWSTEEL", "JSW Steel expansion status kya hai?", session_id="4e-exch")
            discovery = spies.discover.last
            assert any(c.origin == EXCHANGE_REFERENCE_ORIGIN for c in discovery.merged)
            answer = spies.research.last
            assert answer.unfetched_references >= 1
            assert all("bseindia" not in (c.web.url if c.web else "") for c in answer.web_citations)
            assert all("bseindia" not in c["source"] for c in body["citations"])
            assert "not fetched" in body["content"] and "not used as evidence" in body["content"]
            assert "www.bseindia.com" not in net.dns and not any("bseindia" in u for u in net.fetched)
        finally:
            with world.Session() as db:
                db.query(DiscoveredFiling).filter(
                    DiscoveredFiling.company_id == world.ids["JSWSTEEL"]).delete()
                db.commit()

    def test_conflicting_sources_are_attributed_not_reconciled(self, scenario, net, spies):
        net.hosts["www.conflict.example"] = [ADDRESS]
        net.robots["www.conflict.example"] = ROBOTS_ALLOW
        net.pages["https://www.conflict.example/investors"] = (
            "Conflict Investors",
            "The Vijayanagar expansion has been commissioned and is operational as "
            "of September 2026, the company told investors at its annual meeting. "
            "The new blast furnace and the downstream mills are producing at their "
            "rated capacity and dispatches to customers have begun.",
            "2026-09-16T09:00:00+05:30",
        )
        net.pages["https://www.conflict.example/investor-relations"] = (
            "Conflict IR",
            "The Vijayanagar expansion has been delayed and commissioning is now "
            "expected in 2027, the investor relations page states. Equipment "
            "deliveries slipped during the monsoon and the revised schedule will "
            "be shared with shareholders once the contractor confirms it.",
            "2026-09-18T09:00:00+05:30",
        )
        result, _ = scenario("CONFLICT", "https://www.conflict.example",
                             "Conflict Vijayanagar expansion status kya hai?")
        assert spies.research.last.status is WebResearchStatus.CONFLICTING_EVIDENCE
        assert "Sources differ" in result.content
        assert "does not reconcile" in result.content
        assert "commissioned" in result.content and "delayed" in result.content
        keys = {c.key for c in result.citations if c.kind is EvidenceKind.WEB}
        assert len(keys) == 2 and all(k in result.content for k in keys)
        assert result.citation_audit.unknown_keys == []

    def test_stale_stored_evidence_carries_a_temporal_caveat(self, world, net, spies):
        settings.WEB_EVIDENCE_ENABLED = False      # nothing fresh can be fetched
        purge_documents(world, world.ids["JSWSTEEL"])
        with world.Session() as db:
            add_document(
                db, world.ids["JSWSTEEL"], "JSW Steel – Old Investor Update",
                DocumentType.WEB_PAGE.value,
                ["JSW Steel expansion status: the Vijayanagar expansion was commissioned "
                 "and the Dolvi expansion is on track."],
                url=f"{ORIGIN}/investors", source_class="company_website",
                published=datetime(2025, 1, 10, tzinfo=timezone.utc),
                retrieved=datetime(2025, 1, 12, tzinfo=timezone.utc),
                status=DocumentStatus.COMPLETED.value,
            )
        try:
            body = chat(world, "JSWSTEEL", "JSW Steel ka latest expansion status kya hai?",
                        session_id="4e-stale")
            assert spies.research.last.status is WebResearchStatus.STALE_EVIDENCE
            assert "Temporal note" in body["content"]
            assert web_citations(body) and body["citation_audit"]["unknown_keys"] == []
            assert net.fetched == []
        finally:
            purge_documents(world, world.ids["JSWSTEEL"])

    def test_every_web_claim_sentence_carries_a_marker_and_every_figure_is_covered(self, world, net, spies):
        settings.WEB_EVIDENCE_ENABLED = True
        purge_documents(world, world.ids["JSWSTEEL"])
        body = chat(world, "JSWSTEEL", "JSW Steel expansion status kya hai?", session_id="4e-claims")
        claims = [line for line in body["content"].splitlines() if line.startswith("- ")]
        assert claims
        assert all(re.search(r"\[web_[a-z0-9_]+\]", line) for line in claims)
        assert body["citation_audit"]["uncited_numbers"] == []
        assert body["citation_audit"]["coverage"] == 1.0


# ===========================================================================
# PART 7 — language hardening
# ===========================================================================
class TestLanguageHardening:
    QUESTIONS = [
        ("english", "What is the latest expansion status of JSW Steel?", None),
        ("hinglish", "JSW Steel ka latest expansion status kya hai?", None),
        ("hindi", "JSW Steel का latest expansion status क्या है?", None),
        ("hinglish-explicit", "JSW Steel latest expansion update batao", "hinglish"),
    ]

    @pytest.mark.parametrize("tag,question,requested", QUESTIONS)
    def test_evidence_numbers_dates_markers_and_sources_survive_every_language(
        self, world, net, spies, tag, question, requested,
    ):
        settings.WEB_EVIDENCE_ENABLED = True
        purge_documents(world, world.ids["JSWSTEEL"])
        extra = {"language": requested} if requested else {}
        body = chat(world, "JSWSTEEL", question, session_id=f"4e-lang-{tag}", **extra)

        web = web_citations(body)
        assert len(web) == 1 and web[0]["key"] in body["content"]
        # The audited artefact is canonical English in every language.
        assert body["content"].startswith("Web evidence on JSW Steel Limited")
        assert "5 MTPA" in body["content"] and "17 Sep 2026" in body["content"]
        # The display keeps the evidence intact: label, figure, dates, host.
        display = body["display_content"]
        assert "[Company Website] JSW Steel Investors" in display
        assert "5 MTPA" in display and "17 Sep 2026" in display
        assert HOST in display
        assert "Vijayanagar" in display
        assert body["citation_audit"]["unknown_keys"] == []
        if tag == "english":
            assert body["language"] is None
            assert spies.adapt.count == 0
        else:
            assert spies.adapt.count == 1
            assert body["language"]["translation"]["integrity_problems"] == []
            assert body["language"]["translation"]["provider"] == "internal"
        assert spies.provider.calls == []

    def test_the_language_adapter_receives_the_annotated_answer_and_the_entities(self, world, net, spies):
        settings.WEB_EVIDENCE_ENABLED = True
        purge_documents(world, world.ids["JSWSTEEL"])
        chat(world, "JSWSTEEL", "JSW Steel का latest expansion status क्या है?", session_id="4e-lang-ent")
        args, kwargs, adapted = spies.adapt.calls[-1]
        assert "[Company Website] JSW Steel Investors" in args[1]
        assert kwargs["entities"] == ["JSW Steel Limited", "JSWSTEEL"]
        assert kwargs["requested"].value == "hindi"


# ===========================================================================
# PART 8 — existing behaviour is not hijacked
# ===========================================================================
class TestExistingBehaviourRegression:
    def test_a_multi_intent_question_is_still_composed_not_web_researched(self, world, net, spies):
        settings.WEB_EVIDENCE_ENABLED = True
        body = chat(world, "JSWSTEEL", "JSW Steel ka P/E aur ROE kya hai?", session_id="4e-comp")
        assert spies.plan.last.execution_route is ExecutionRoute.COMPOSITION_REQUIRED
        assert body["provider"] == "deterministic"
        assert spies.research.count == 0 and net.fetched == []
        assert spies.provider.calls == []

    def test_an_investment_intent_is_still_deterministic(self, world, net, spies):
        settings.WEB_EVIDENCE_ENABLED = True
        body = chat(world, "JSWSTEEL", "JSW Steel kaisi company hai?", session_id="4e-inv")
        assert body["provider"] == "deterministic"
        assert spies.research.count == 0 and net.fetched == []

    def test_cross_company_retargeting_is_unchanged_and_scopes_the_web_search(self, world, net, spies):
        settings.WEB_EVIDENCE_ENABLED = True
        purge_documents(world, world.ids["JSWSTEEL"])
        body = chat(world, "TATASTEEL", "JSWSTEEL expansion status kya hai?", session_id="4e-cross")
        assert body["company"]["ticker"] == "JSWSTEEL"
        assert any("chat URL is scoped to TATASTEEL" in w for w in body["warnings"])
        assert spies.index.calls[-1][1]["company_id"] == world.ids["JSWSTEEL"]
        assert net.fetched_hosts() <= {HOST}

    def test_the_job_handlers_and_filing_collector_do_not_reach_the_chat_web_layer(self):
        for rel in ("services/platform/jobs/handlers.py",):
            text = (APP / rel).read_text()
            for token in ("internal_web_research", "targeted_discovery", "InternalWebResearchEngine"):
                assert token not in text, (rel, token)
        assert "web.discovery" not in (APP / "services/web/targeted_discovery.py").read_text()

    def test_the_streaming_endpoint_and_report_keep_the_default_analyst(self):
        source = (APP / "api/v1/ai.py").read_text()
        for name in ("async def chat_stream(", "async def report(", "def context("):
            block = source[source.index(name):]
            block = block[:block.index("analyst_for(") + 40]
            assert "enable_composition" not in block, name


# ===========================================================================
# PART 9 — performance and bounds
# ===========================================================================
class TestBounds:
    def test_every_layer_declares_a_hard_ceiling(self):
        limits = WebQueryLimits()
        assert 1 <= limits.max_queries <= 8
        index = WebIndexLimits()
        assert index.max_results <= 8 and index.max_queries <= 8 and index.per_query <= 10
        policy = DiscoveryPolicy()
        assert policy.max_queries <= DiscoveryPolicy.MAX_QUERIES_CEILING == 8
        assert policy.max_seed_urls <= DiscoveryPolicy.MAX_SEEDS_CEILING == 16
        assert policy.max_fetched_pages <= DiscoveryPolicy.MAX_FETCH_CEILING == 8
        assert policy.max_results <= DiscoveryPolicy.MAX_RESULTS_CEILING == 16
        assert policy.max_depth == 0
        with pytest.raises(ValueError):
            DiscoveryPolicy(max_fetched_pages=9)
        with pytest.raises(ValueError):
            DiscoveryPolicy(max_depth=1)
        fetch = WebFetchPolicy()
        assert fetch.max_bytes <= 8 * 1024 * 1024 and fetch.max_redirects <= 3
        assert fetch.max_attempts <= 2 and fetch.timeout_seconds <= 20.0

    def test_the_chat_wiring_bounds_one_question_to_an_interactive_time_budget(self, world, net):
        """A single chat turn must not be able to hold the request open for
        minutes because a company site hangs: the wiring hands discovery a
        fetch budget whose worst case — every page timing out on every
        attempt, plus retry sleeps and per-host pacing — stays under a
        minute. The job-triggered crawl keeps its own, looser policy."""
        from app.services.ai.internal_web_research import INTERACTIVE_DISCOVERY_POLICY
        with world.Session() as db:
            discovery = analyst_for(world, db, "JSWSTEEL").web_research.discovery
        policy = discovery.policy
        assert policy is INTERACTIVE_DISCOVERY_POLICY
        fetch = policy.effective_fetch_policy() or WebFetchPolicy()
        per_page = fetch.max_attempts * fetch.timeout_seconds + sum(
            min(2.0 * attempt, 5.0) for attempt in range(1, fetch.max_attempts)
        )
        worst_case = policy.max_fetched_pages * per_page + (
            policy.max_fetched_pages - 1
        ) * fetch.default_crawl_delay + 10.0    # robots.txt fetch timeout
        assert worst_case <= 60.0, (worst_case, policy.as_dict())
        assert fetch.max_bytes <= WebFetchPolicy().max_bytes
        assert policy.max_fetched_pages <= DiscoveryPolicy().max_fetched_pages
        # Security parameters are never loosened by the interactive budget.
        assert fetch.max_redirects <= WebFetchPolicy().max_redirects
        assert fetch.allowed_ports == WebFetchPolicy().allowed_ports
        assert fetch.default_crawl_delay >= WebFetchPolicy().default_crawl_delay

    def test_one_question_costs_a_bounded_number_of_fetches_and_writes(self, world, net, spies):
        settings.WEB_EVIDENCE_ENABLED = True
        purge_documents(world, world.ids["JSWSTEEL"])
        chat(world, "JSWSTEEL", "JSW Steel expansion status kya hai?", session_id="4e-bound")
        assert len(net.fetched) <= DiscoveryPolicy().max_fetched_pages
        assert len(net.robots_calls) == 1
        assert spies.search.count == 1 and spies.discover.count == 1
        assert spies.ingest.count <= DiscoveryPolicy().max_fetched_pages
        assert len(spies.generate.last.texts) <= WebQueryLimits().max_queries
        assert len(spies.research.last.web_citations) <= 4

    def test_the_synthesis_is_deterministic_across_identical_turns(self, world, net, spies):
        settings.WEB_EVIDENCE_ENABLED = True
        purge_documents(world, world.ids["JSWSTEEL"])
        first = chat(world, "JSWSTEEL", "JSW Steel expansion status kya hai?", session_id="4e-det")
        second = chat(world, "JSWSTEEL", "JSW Steel expansion status kya hai?", session_id="4e-det")
        assert first["content"] == second["content"]
        assert [c["key"] for c in first["citations"]] == [c["key"] for c in second["citations"]]


# ===========================================================================
# PART 10 — database and transaction safety
# ===========================================================================
class TestDatabaseSafety:
    def test_no_model_or_table_was_added_by_the_web_research_layers(self):
        for rel in ("services/ai/internal_web_research.py", "services/web/targeted_discovery.py",
                    "services/web/index.py", "services/ai/planner/web_query.py"):
            tree = ast.parse((APP / rel).read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    assert not any(getattr(b, "id", "") == "Base" for b in node.bases), (rel, node.name)
        assert not list((BACKEND / "alembic" / "versions").glob("*web_research*"))
        assert not list((BACKEND / "alembic" / "versions").glob("*targeted*"))

    def test_persistence_uses_the_existing_ingestion_and_yields_ordinary_web_page_documents(self, world, net, spies):
        settings.WEB_EVIDENCE_ENABLED = True
        purge_documents(world, world.ids["JSWSTEEL"])
        chat(world, "JSWSTEEL", "JSW Steel expansion status kya hai?", session_id="4e-persist")
        assert spies.ingest.count == 2
        docs = documents_for(world, world.ids["JSWSTEEL"])
        assert [d.doc_type for d in docs] == [DocumentType.WEB_PAGE.value] * 2
        assert {d.source_url for d in docs} == {f"{ORIGIN}/investors", f"{ORIGIN}/investor-relations"}
        assert all(d.status == DocumentStatus.QUEUED.value for d in docs)
        assert all(d.uploaded_by == "web-evidence" or d.uploaded_by for d in docs)
        with world.Session() as db:
            jobs = db.query(DocumentJob).filter(DocumentJob.document_id.in_([d.id for d in docs])).count()
        assert jobs == 2

    def test_repeated_questions_do_not_duplicate_unchanged_pages(self, world, net, spies):
        """Idempotency on the runtime path, with a MOVING clock.

        Each turn fetches the page again (a changed page must be picked up)
        but an unchanged page must resolve to the document the platform
        already holds — no second row, no second document job, no second
        pipeline run. The retrieval timestamp differs between turns; that is
        provenance, not content, and must not defeat the comparison.
        """
        settings.WEB_EVIDENCE_ENABLED = True
        purge_documents(world, world.ids["JSWSTEEL"])
        first = chat(world, "JSWSTEEL", "JSW Steel expansion status kya hai?", session_id="4e-idem")
        docs_after_first = documents_for(world, world.ids["JSWSTEEL"])
        assert len(docs_after_first) == 2
        with world.Session() as db:
            jobs_after_first = db.query(DocumentJob).count()

        time.sleep(0.01)
        second = chat(world, "JSWSTEEL", "JSW Steel expansion status kya hai?", session_id="4e-idem")
        third = chat(world, "JSWSTEEL", "JSW Steel latest expansion update batao", session_id="4e-idem")

        docs = documents_for(world, world.ids["JSWSTEEL"])
        assert [d.id for d in docs] == [d.id for d in docs_after_first], \
            [(d.id, d.source_url, d.retrieved_at) for d in docs]
        with world.Session() as db:
            assert db.query(DocumentJob).count() == jobs_after_first
        # The pages were still fetched each turn — idempotency is the content
        # comparison, never a skipped look.
        assert net.fetched.count(f"{ORIGIN}/investors") == 3
        # And every turn cites the SAME stored document.
        ids = {str(web_citations(b)[0]["document_id"]) for b in (first, second, third)}
        assert ids == {str(docs_after_first[0].id)}

    def test_unchanged_page_resolution_survives_a_cold_cache_a_moving_clock_and_a_deleted_row(self, world, net):
        """The service-level contract behind the runtime idempotency.

        * moving clock, cold cache: the second fetch resolves to the held row;
        * a cache entry naming a row that no longer exists (or now holds a
          different page) is not trusted — the page is ingested afresh;
        * a row written before the text hash existed is matched on its raw
          response hash, so upgrading does not re-ingest every page once.
        """
        from app.domain.web.types import WebSearchQuery
        from app.services.platform.cache import CacheService, MemoryCache

        settings.WEB_EVIDENCE_ENABLED = True
        company_id = world.ids["JSWSTEEL"]
        purge_documents(world, company_id)
        clock = {"now": NOW}
        url = f"{ORIGIN}/investors"
        query = WebSearchQuery(company_id=company_id, query="expansion status",
                               candidate_urls=(url,), limit=1, max_urls=1)

        with world.Session() as db:
            cold = CacheService(MemoryCache())
            service = WebSearchService(db, cache_service=cold, now=lambda: clock["now"])
            first = service.search(query)
            assert len(first.documents) == 1
            first_id = first.documents[0].document_id
            row = db.get(Document, first_id)
            assert row.doc_metadata["web"]["text_sha256"]
            assert row.doc_metadata["web"]["retrieved_at"] == NOW.isoformat()

            # 1. Cold cache + later clock → same document, no new row.
            clock["now"] = NOW + timedelta(hours=3)
            again = WebSearchService(db, cache_service=CacheService(MemoryCache()),
                                     now=lambda: clock["now"]).search(query)
            assert again.documents[0].document_id == first_id
            assert again.documents[0].retrieved_at == clock["now"]     # the look was real
            assert len(documents_for(world, company_id)) == 1

            # 2. A cache hint naming a vanished row is rejected, not cited.
            hinting = CacheService(MemoryCache())
            hinting.set(Namespace.WEB, {"document_id": first_id, "content_hash": "x",
                                        "text_sha256": row.doc_metadata["web"]["text_sha256"]},
                        "page", company_id, url)
            purge_documents(world, company_id)
            db.expire_all()
            fresh = WebSearchService(db, cache_service=hinting, now=lambda: clock["now"]).search(query)
            new_id = fresh.documents[0].document_id
            assert new_id is not None and db.get(Document, new_id) is not None
            assert len(documents_for(world, company_id)) == 1

            # 3. A legacy row (no text hash) matches on the raw response hash.
            legacy = db.get(Document, new_id)
            meta = dict(legacy.doc_metadata); web = dict(meta["web"]); web.pop("text_sha256")
            meta["web"] = web; legacy.doc_metadata = meta
            db.commit()
            clock["now"] = NOW + timedelta(days=1)
            third = WebSearchService(db, cache_service=CacheService(MemoryCache()),
                                     now=lambda: clock["now"]).search(query)
            assert third.documents[0].document_id == new_id
            assert len(documents_for(world, company_id)) == 1
        purge_documents(world, company_id)

    def test_a_changed_page_is_still_picked_up_as_a_new_version(self, world, net, spies):
        settings.WEB_EVIDENCE_ENABLED = True
        purge_documents(world, world.ids["JSWSTEEL"])
        chat(world, "JSWSTEEL", "JSW Steel expansion status kya hai?", session_id="4e-changed")
        before = documents_for(world, world.ids["JSWSTEEL"])
        net.pages[f"{ORIGIN}/investors"] = (
            "JSW Steel Investors",
            INVESTORS_TEXT.replace("5 MTPA", "7 MTPA"), "2026-09-19T09:00:00+05:30",
        )
        body = chat(world, "JSWSTEEL", "JSW Steel expansion status kya hai?", session_id="4e-changed")
        after = documents_for(world, world.ids["JSWSTEEL"])
        assert len(after) == len(before) + 1
        assert "7 MTPA" in body["content"]

    def test_a_failure_inside_persistence_leaves_an_honest_answer_and_a_usable_session(self, world, net, spies, monkeypatch):
        settings.WEB_EVIDENCE_ENABLED = True
        purge_documents(world, world.ids["JSWSTEEL"])

        def explode(self, *args, **kwargs):
            raise RuntimeError("simulated storage/database failure during ingestion")

        monkeypatch.setattr(DocumentIngestionService, "accept", explode)
        body = chat(world, "JSWSTEEL", "JSW Steel expansion status kya hai?", session_id="4e-fail")
        assert body["provider"] == "deterministic"
        assert "insufficient" in body["content"]
        assert web_citations(body) == []
        assert documents_for(world, world.ids["JSWSTEEL"]) == []
        assert spies.provider.calls == []
        # The endpoint's later reads on the same session still worked.
        assert body["data_quality"] is not None
        assert body["company"]["ticker"] == "JSWSTEEL"
        # And the next request on the same database is unaffected.
        monkeypatch.undo()
        again = chat(world, "JSWSTEEL", "company ka P/E kya hai?", session_id="4e-fail")
        assert again["provider"] == "deterministic"

    def test_an_exception_escaping_discovery_does_not_poison_the_session(self, world, net, spies, monkeypatch):
        """The whole discovery call raising is caught by the engine; the
        question is still answered from what is held, and the same session
        remains usable for the reads the endpoint performs afterwards."""
        settings.WEB_EVIDENCE_ENABLED = True

        def boom(self, *args, **kwargs):
            raise RuntimeError("simulated discovery failure")

        monkeypatch.setattr(TargetedWebDiscovery, "discover", boom)
        body = chat(world, "JSWSTEEL", "JSW Steel expansion status kya hai?", session_id="4e-boom")
        assert body["provider"] == "deterministic"
        assert "insufficient" in body["content"]
        assert body["data_quality"] is not None
        assert spies.provider.calls == []

    def test_disabled_discovery_performs_no_writes_at_all(self, world, net, spies):
        settings.WEB_EVIDENCE_ENABLED = False
        purge_documents(world, world.ids["JSWSTEEL"])
        from sqlalchemy import event
        flushed: list[tuple] = []

        def before_flush(session, flush_context, instances):
            for obj in list(session.new) + list(session.dirty) + list(session.deleted):
                if isinstance(obj, (Document, DocumentChunk, DocumentJob)):
                    flushed.append((type(obj).__name__, getattr(obj, "id", None)))

        event.listen(Session, "before_flush", before_flush)
        try:
            chat(world, "JSWSTEEL", "JSW Steel expansion status kya hai?", session_id="4e-nowrite")
        finally:
            event.remove(Session, "before_flush", before_flush)
        assert flushed == []
        assert spies.ingest.count == 0 and spies.search.count == 0
        assert documents_for(world, world.ids["JSWSTEEL"]) == []

    def test_transient_discovery_leaves_no_persistence(self, world, net, spies):
        """The 4C `persist=False` contract, exercised through the 4D engine:
        evidence is cited as transient and no row is written."""
        settings.WEB_EVIDENCE_ENABLED = True
        purge_documents(world, world.ids["JSWSTEEL"])
        with world.Session() as db:
            analyst = analyst_for(world, db, "JSWSTEEL")
            analyst.web_research.discovery = TargetedWebDiscovery(
                db, policy=DiscoveryPolicy(persist=False),
            )
            memory = memory_for(world, db, "JSWSTEEL")
            result = _run(analyst.chat("JSW Steel expansion status kya hai?", memory))
        cited = [c for c in result.citations if c.kind is EvidenceKind.WEB]
        assert cited and all(c.document_id is None for c in cited)
        assert LIVE_FETCH_TRANSIENT_ORIGIN in result.content
        assert LIVE_FETCH_PERSISTED_ORIGIN not in result.content
        assert documents_for(world, world.ids["JSWSTEEL"]) == []
        assert spies.ingest.count == 0

    def test_existing_filing_and_upload_rows_are_untouched(self, world, net, spies):
        settings.WEB_EVIDENCE_ENABLED = True
        purge_documents(world, world.ids["JSWSTEEL"])
        with world.Session() as db:
            upload = add_document(
                db, world.ids["JSWSTEEL"], "Annual Report FY2024", "annual_report",
                ["Annual report narrative about the expansion status of the company."],
                status=DocumentStatus.COMPLETED.value, filename="ar-2024.pdf",
            )
            upload_id, upload_hash = upload.id, upload.content_hash
        try:
            chat(world, "JSWSTEEL", "JSW Steel expansion status kya hai?", session_id="4e-uploads")
            with world.Session() as db:
                row = db.get(Document, upload_id)
                assert row is not None and row.content_hash == upload_hash
                assert row.doc_type == "annual_report" and row.superseded_by is None
                assert row.source_url is None
        finally:
            purge_documents(world, world.ids["JSWSTEEL"])


# ===========================================================================
# PART 11 — API / frontend contract
# ===========================================================================
class TestApiContract:
    def test_web_and_financial_answers_share_one_response_schema(self, world, net, spies):
        settings.WEB_EVIDENCE_ENABLED = True
        purge_documents(world, world.ids["JSWSTEEL"])
        web = chat(world, "JSWSTEEL", "JSW Steel expansion status kya hai?", session_id="4e-api")
        fin = chat(world, "JSWSTEEL", "company ka P/E kya hai?", session_id="4e-api")
        assert set(web) == set(fin) == set(ChatResponse.model_fields)
        assert set(CitationOut.model_fields) == {
            "key", "label", "kind", "value", "unit", "source", "fiscal_year",
            "document_id", "chunk_id", "page", "confidence", "snippet",
        }
        citation = web_citations(web)[0]
        assert set(citation) == set(CitationOut.model_fields)
        assert citation["kind"] == "web"
        assert isinstance(citation["value"], str) and citation["value"]
        assert citation["label"].startswith("[Company Website]")
        assert citation["source"] and citation["document_id"]
        # A frontend reading only the historical fields still renders it.
        for field in ("key", "label", "kind", "value", "unit", "source", "fiscal_year"):
            assert field in citation
        # `content` is English and `display_content` exists in both.
        assert web["content"] and web["display_content"]
        assert web["session_id"] == "4e-api" and web["turn_count"] >= 2

    def test_the_frontend_renders_unknown_citation_kinds_neutrally(self):
        panels = (BACKEND.parent / "frontend/src/components/ai/panels.tsx").read_text()
        assert 'KIND_TONE[c.kind] ?? "neutral"' in panels
        types_ts = (BACKEND.parent / "frontend/src/lib/types.ts").read_text()
        assert "export interface CitationOut" in types_ts
        assert "kind: string" in types_ts.split("export interface CitationOut")[1][:200]

    def test_a_financial_answer_is_byte_identical_with_and_without_the_web_layer(self, world, net, spies):
        settings.WEB_EVIDENCE_ENABLED = True
        with world.Session() as db:
            analysis = AnalysisService.for_ticker(db, "JSWSTEEL", provision=False)
            plain = AIService(db).analyst_for(analysis)
            wired = AIService(db).analyst_for(analysis, enable_composition=True)
            a = _run(plain.chat("company ka P/E kya hai?", memory_for(world, db, "JSWSTEEL")))
            b = _run(wired.chat("company ka P/E kya hai?", memory_for(world, db, "JSWSTEEL")))
        assert a.content == b.content and a.provider == b.provider == "deterministic"
        assert [c.key for c in a.citations] == [c.key for c in b.citations]
        assert net.fetched == []


# ===========================================================================
# PART 12 — production configuration
# ===========================================================================
class TestProductionConfig:
    def test_the_kill_switch_documentation_states_that_chat_discovery_is_gated_too(self):
        """An operator reading the setting must learn that enabling it also
        permits bounded on-demand fetches from the chat path — not only the
        background crawl job."""
        config = (APP / "core/config.py").read_text()
        block = config[config.index("# --- Web evidence crawl"):config.index("WEB_EVIDENCE_ENABLED: bool = False")]
        assert "chat" in block.lower() and "targeted" in block.lower()
        assert "no request path can ever reach" not in block
        example = (BACKEND / ".env.example").read_text()
        section = example[example.index("WEB EVIDENCE CRAWL"):example.index("WEB_EVIDENCE_ENABLED=false")]
        assert "chat" in section.lower()

    def test_a_configuration_mistake_cannot_widen_the_crawl(self, world, net, spies):
        """Even with the switch on and providers on, the chat path fetches only
        the company's own pinned origins through the same policy ceilings."""
        settings.WEB_EVIDENCE_ENABLED = True
        settings.AI_EXTERNAL_PROVIDERS_ENABLED = True
        purge_documents(world, world.ids["JSWSTEEL"])
        chat(world, "JSWSTEEL", "JSW Steel expansion status kya hai?", session_id="4e-cfg")
        assert net.fetched_hosts() == {HOST}
        assert len(net.fetched) <= DiscoveryPolicy.MAX_FETCH_CEILING
        assert spies.provider.calls == []
        policy = spies.discover.calls[-1][0][0].policy
        assert policy.max_depth == 0 and policy.max_fetched_pages <= DiscoveryPolicy.MAX_FETCH_CEILING
