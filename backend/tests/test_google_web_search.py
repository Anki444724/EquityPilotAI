"""General-topic Google web search — discovery, planner scope, cited answers.

The company-pinned ``WebSearchService`` is not an arbitrary URL searcher.
This module pins the separate Custom Search path: one bounded JSON request,
snippets discarded, pages fetched through the existing safety stack, and a
general topic that does not inherit ``company_id``.
"""
from __future__ import annotations

import ast
import asyncio
import socket
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.core.config import Settings, settings
from app.domain.language.types import Language
from app.domain.web.types import WebRejectionReason
from app.services.ai.analyst import ResearchAnalyst, _company_identity_is_safe
from app.services.ai.context_builder import GroundedContext
from app.services.ai.internal_composer import InternalComposer
from app.services.ai.internal_open_ended import InternalOpenEndedEngine
from app.services.ai.internal_web_research import (
    InternalWebResearchEngine,
    WebResearchStatus,
)
from app.services.ai.memory import ConversationMemory
from app.services.ai.planner import (
    EntityStatus,
    ExecutionRoute,
    QueryType,
    QuestionPlanner,
    ResearchScope,
    is_commodity_move,
)
from app.services.ai.prompt_builder import PromptBuilder
from app.services.language.translators import TranslationResult
from app.services.web.fetcher import HostPoliteness, TransportResponse
from app.services.web.robots import RobotsPolicy
from tests.test_deterministic_analyst_path import SpyDocumentService, SpyRouter
from tests.test_internal_web_research import (
    NOW as COMPANY_NOW,
    FakeIndex,
    candidate,
    index_result,
    jsw_context,
    plan_for,
)

BACKEND = Path(__file__).resolve().parents[1]
APP = BACKEND / "app"
GOOGLE_PATH = APP / "services" / "web" / "google_search.py"
ENGINE_PATH = APP / "services" / "ai" / "internal_web_research.py"
ENV_EXAMPLE = BACKEND / ".env.example"

NOW = datetime(2026, 9, 20, 9, 0, tzinfo=timezone.utc)
ADDRESS = "93.184.216.34"
KEY = "super-secret-google-key"
CX = "super-secret-engine-id"
CSE = "https://www.googleapis.com/customsearch/v1"
PUBLISHED = "2026-09-17T08:00:00Z"

PAD = (
    "Additional background on the same subject follows so the extract clears "
    "the minimum length the platform requires before a fetched page can be "
    "treated as evidence. These sentences repeat the point in ordinary words "
    "and do not add a separate claim, a date, or a figure."
)


def _run(coro):
    return asyncio.run(coro)


class ApiResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status_code = status
        self.content = body


class ApiTransport:
    def __init__(self, response: ApiResponse | None = None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[dict] = []

    def get(self, url, *, params, headers, timeout):
        self.calls.append({
            "url": url, "params": dict(params), "headers": dict(headers),
            "timeout": timeout,
        })
        if self.error is not None:
            raise self.error
        return self.response


class PageTransport:
    def __init__(self, pages: dict[str, TransportResponse] | None = None) -> None:
        self.pages = dict(pages or {})
        self.calls: list[str] = []

    def get(self, url, *, headers, timeout, max_bytes):
        self.calls.append(url)
        found = self.pages.get(url)
        if found is None:
            return TransportResponse(
                status_code=404, headers={"content-type": "text/plain"},
                content=b"not found", final_url=url, elapsed_ms=1.0,
                truncated=False, peer_address=(ADDRESS, 443),
            )
        return found


class Resolver:
    def __init__(self, addresses: list[str] | None = None) -> None:
        self.addresses = addresses or [ADDRESS]
        self.calls: list[str] = []

    def __call__(self, host, port):
        self.calls.append(host)
        return list(self.addresses)


def html_page(url: str, title: str, paragraph: str, *, published: str | None = PUBLISHED) -> TransportResponse:
    meta = ""
    if published:
        meta = f'<meta property="article:published_time" content="{published}">'
    body = (
        f"<html><head><title>{title}</title>{meta}</head><body><main>"
        f"<h1>{title}</h1><p>{paragraph}</p><p>{PAD}</p></main></body></html>"
    ).encode()
    return TransportResponse(
        status_code=200,
        headers={"content-type": "text/html; charset=utf-8"},
        content=body, final_url=url, elapsed_ms=1.0, truncated=False,
        peer_address=(ADDRESS, 443),
    )


def redirect(url: str, location: str) -> TransportResponse:
    return TransportResponse(
        status_code=302, headers={"location": location, "content-type": "text/plain"},
        content=b"redirect", final_url=url, elapsed_ms=1.0, truncated=False,
        peer_address=(ADDRESS, 443),
    )


def items(*links: str, snippet: str = "SECRET-SNIPPET must never be cited") -> list[dict]:
    return [
        {"title": f"Result {i}", "link": link, "snippet": snippet,
         "pagemap": {"metatags": [{"datePublished": "1999-01-01"}]}}
        for i, link in enumerate(links, start=1)
    ]


def payload(*links: str, snippet: str = "SECRET-SNIPPET must never be cited") -> ApiResponse:
    import json
    body = json.dumps({"items": items(*links, snippet=snippet)}).encode()
    return ApiResponse(200, body)


def allow_robots(url, *, timeout, max_bytes, user_agent):
    if "blocked.example" in url:
        return 200, b"User-agent: *\nDisallow: /\n"
    return 200, b"User-agent: *\nDisallow: /private\n"


def discovery(
    api: ApiTransport,
    pages: PageTransport | None = None,
    *,
    enabled: bool = True,
    api_key: str | None = KEY,
    engine_id: str | None = CX,
    resolver: Resolver | None = None,
):
    from app.services.web.google_search import GoogleWebDiscovery, PublicUrlSafetyPolicy

    resolved = resolver or Resolver()
    return GoogleWebDiscovery(
        enabled=enabled,
        api_key=api_key,
        engine_id=engine_id,
        api_transport=api,
        page_transport=pages or PageTransport(),
        safety=PublicUrlSafetyPolicy(resolver=resolved),
        robots=RobotsPolicy(fetch=allow_robots),
        politeness=HostPoliteness(default_delay=0.0, sleep=lambda _s: None),
        resolver=resolved,
        clock=lambda: NOW,
    ), resolved


def engine_for(google, *, index=None, generator=None):
    return InternalWebResearchEngine(
        index=index if index is not None else _BoomIndex(),
        discovery=None,
        generator=generator if generator is not None else _BoomGenerator(),
        google=google,
        clock=lambda: NOW,
    )


class _BoomIndex:
    def search(self, *args, **kwargs):
        raise AssertionError("general research searched the company index")


class _BoomGenerator:
    def generate(self, plan):
        raise AssertionError("general research generated a company query")


class _SpyGoogle:
    def __init__(self, *, enabled: bool = True, error: Exception | None = None, result=None) -> None:
        self.enabled = enabled
        self.error = error
        self.result = result
        self.calls: list[str] = []

    def research(self, question):
        self.calls.append(question)
        if self.error is not None:
            raise self.error
        return self.result


class _NamedCompany:
    def __init__(self, company_id: str, ticker: str, name: str) -> None:
        self.id = company_id
        self.ticker = ticker
        self.name = name


def _resolve_jsw(text: str):
    if "jsw" in (text or "").lower():
        return [_NamedCompany("c-jsw", "JSWSTEEL", "JSW Steel Limited")]
    return []


def general_plan(question: str = "Python kya hai?"):
    return QuestionPlanner().plan(question)


def bound_context() -> GroundedContext:
    return GroundedContext(
        company_id="c-jsw", ticker="JSWSTEEL", name="JSW Steel Limited",
        sector="Steel",
    )


def analyst_for(web, *, router=None, docs=None):
    return ResearchAnalyst(
        _Builder(bound_context(), docs),
        router=router or SpyRouter("forbid"),
        prompt_builder=PromptBuilder(),
        planner=QuestionPlanner(),
        composer=InternalComposer(),
        open_ended=InternalOpenEndedEngine(),
        web_research=web,
    )


class _Builder:
    def __init__(self, context, docs=None) -> None:
        self._context = context
        self.document_service = docs
        self.analysis = type("A", (), {"company": type("C", (), {
            "id": "c-jsw", "ticker": "JSWSTEEL", "name": "JSW Steel Limited",
        })()})()

    def build(self):
        return self._context


def _module_imports(source: str) -> set[str]:
    names: set[str] = set()
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


# ---------------------------------------------------------------------------
# Planner scope — no network
# ---------------------------------------------------------------------------
class TestPlannerScope:
    @pytest.mark.parametrize("question", [
        "What is the order book?",
        "Tell me about the company",
        "What is the sector?",
        "Who are the promoters?",
        "What does the company deal in?",
        "Can you expand on that?",
        "Reliance ke baare me vistar se batao",
        "₹500 se ₹650 kitna percent increase hai?",
        "Sector kya hai?",
        "सेक्टर क्या है?",
    ])
    def test_open_ended_pins_stay_internal(self, question):
        plan = QuestionPlanner().plan(question)
        assert plan.execution_route is ExecutionRoute.INTERNAL_REASONING, question
        assert plan.query_type is not QueryType.WEB_RESEARCH, question
        assert plan.research_scope is ResearchScope.NONE, question

    def test_current_market_price_stays_deterministic(self):
        plan = QuestionPlanner().plan("What is the current market price of JSW Steel?")
        assert plan.intent_values == ("market_price",)
        assert plan.execution_route is ExecutionRoute.DETERMINISTIC_FINANCIAL
        assert plan.research_scope is ResearchScope.NONE
        assert is_commodity_move("What is the current market price of JSW Steel?") is False

    def test_a_company_price_fall_stays_financial(self):
        question = "JSW Steel price kyu gira?"
        plan = QuestionPlanner().plan(question)
        assert is_commodity_move(question) is False
        assert plan.intent_values == ("market_price",)
        assert plan.execution_route is ExecutionRoute.DETERMINISTIC_FINANCIAL

    def test_latest_revenue_still_declines(self):
        plan = QuestionPlanner().plan("JSW Steel ka latest revenue kya hai?")
        assert plan.execution_route is ExecutionRoute.DECLINE
        assert plan.research_scope is ResearchScope.NONE

    def test_company_expansion_stays_company_scoped(self):
        plan = plan_for()
        assert plan.execution_route is ExecutionRoute.WEB_RESEARCH
        assert plan.entity.status is EntityStatus.RESOLVED
        assert plan.research_scope is ResearchScope.COMPANY

    @pytest.mark.parametrize("question", [
        "latest order kya hai?",
        "Acme Widgets Corp ka latest order kya hai?",
        "latest news kya hai?",
        "expansion status kya hai?",
    ])
    def test_unresolved_company_events_do_not_become_general(self, question):
        plan = QuestionPlanner().plan(question)
        assert plan.execution_route is ExecutionRoute.WEB_RESEARCH, question
        assert plan.entity.status is EntityStatus.UNRESOLVED, question
        assert plan.research_scope is ResearchScope.COMPANY, question

    @pytest.mark.parametrize("question", [
        "Python kya hai?",
        "What is Python?",
        "India me UPI kaise kaam karta hai?",
        "2026 me latest AI developments kya hain?",
        "Aaj gold price kyu move hua?",
    ])
    def test_general_topics_do_not_require_a_company(self, question):
        plan = QuestionPlanner().plan(question)
        assert plan.execution_route is ExecutionRoute.WEB_RESEARCH, question
        assert plan.research_scope is ResearchScope.GENERAL, question
        assert plan.entity.status is not EntityStatus.RESOLVED, question

    def test_gold_move_is_not_a_market_price_route(self):
        assert is_commodity_move("Aaj gold price kyu move hua?") is True
        plan = QuestionPlanner().plan("Aaj gold price kyu move hua?")
        assert plan.execution_route is ExecutionRoute.WEB_RESEARCH
        assert plan.is_executable is False

    def test_scope_serialises_and_defaults_to_none(self):
        plan = QuestionPlanner().plan("")
        assert plan.research_scope is ResearchScope.NONE
        assert plan.as_dict()["research_scope"] == "none"
        general = QuestionPlanner().plan("Python kya hai?")
        assert general.as_dict()["research_scope"] == "general"

    def test_a_resolved_company_price_beside_nifty_is_not_general_research(self):
        question = "Nifty gira. What is the current market price of JSW Steel?"
        plan = QuestionPlanner(company_resolver=_resolve_jsw).plan(question)
        assert plan.entity.status is EntityStatus.RESOLVED
        assert plan.entity.ticker == "JSWSTEEL"
        assert plan.intent_values == ("market_price",)
        assert plan.execution_route is ExecutionRoute.DETERMINISTIC_FINANCIAL
        assert plan.research_scope is ResearchScope.NONE

    @pytest.mark.parametrize("question", [
        "What is JSW Steel?",
        "JSW Steel kya hai?",
        "How does JSW Steel work?",
    ])
    def test_explanatory_wording_does_not_research_a_resolved_company(self, question):
        plan = QuestionPlanner(company_resolver=_resolve_jsw).plan(question)
        assert plan.entity.status is EntityStatus.RESOLVED, question
        assert plan.execution_route is ExecutionRoute.INTERNAL_REASONING, question
        assert plan.query_type is QueryType.OPEN_ENDED, question
        assert plan.research_scope is ResearchScope.NONE, question


# ---------------------------------------------------------------------------
# Google discovery — injected transports, no sockets
# ---------------------------------------------------------------------------
class TestGoogleDiscoveryFailsClosed:
    def test_disabled_flag_makes_no_request(self, monkeypatch):
        def refuse(*_a, **_k):
            raise AssertionError("DNS during a disabled search")

        monkeypatch.setattr(socket, "getaddrinfo", refuse)
        api = ApiTransport(error=AssertionError("CSE called while disabled"))
        pages = PageTransport()
        service, _resolver = discovery(api, pages, enabled=False)
        from app.services.web.google_search import GoogleSearchDisabled

        with pytest.raises(GoogleSearchDisabled) as caught:
            service.research("Python kya hai?")
        assert api.calls == [] and pages.calls == []
        assert KEY not in str(caught.value)
        assert caught.value.__cause__ is None

    def test_construction_and_from_settings_do_no_network(self, monkeypatch):
        def refuse(*_a, **_k):
            raise AssertionError("network during construction")

        monkeypatch.setattr(socket, "getaddrinfo", refuse)
        monkeypatch.setattr(settings, "GOOGLE_SEARCH_ENABLED", False)
        from app.services.web.google_search import GoogleWebDiscovery

        built = GoogleWebDiscovery.from_settings()
        assert built.enabled is False
        assert KEY not in repr(built)
        GoogleWebDiscovery()

    def test_missing_credentials_do_not_call_google(self):
        api = ApiTransport(error=AssertionError("CSE called without credentials"))
        service, _resolver = discovery(api, enabled=True, api_key="", engine_id="")
        from app.services.web.google_search import GoogleSearchRefused

        with pytest.raises(GoogleSearchRefused):
            service.research("Python kya hai?")
        assert api.calls == []

    def test_empty_and_overlong_queries_are_refused_before_the_call(self):
        api = ApiTransport(error=AssertionError("CSE called for a refused query"))
        service, _resolver = discovery(api)
        from app.services.web.google_search import GoogleSearchRefused

        with pytest.raises(GoogleSearchRefused):
            service.research("   ")
        with pytest.raises(GoogleSearchRefused):
            service.research("a" * 257)
        assert api.calls == []

    def test_one_request_caps_results_and_does_not_paginate(self):
        links = [f"https://news{i}.example/item" for i in range(8)]
        pages = PageTransport({
            link: html_page(link, "Python", "Python is a programming language used to explain ideas clearly.")
            for link in links
        })
        api = ApiTransport(payload(*links))
        service, _resolver = discovery(api, pages)
        found = service.research("  Python   kya   hai?  ")
        assert len(api.calls) == 1
        call = api.calls[0]
        assert call["url"] == CSE
        assert call["params"]["key"] == KEY
        assert call["params"]["cx"] == CX
        assert call["params"]["q"] == "Python kya hai?"
        assert call["params"]["num"] == 5
        assert "start" not in call["params"]
        assert len(pages.calls) == 5
        assert len(found.candidates) == 5

    @pytest.mark.parametrize("status,body", [
        (429, b'{"error":{"message":"rate limit super-secret-google-key"}}'),
        (500, b'{"error":"super-secret-google-key"}'),
        (200, b"not-json super-secret-google-key"),
        (200, b'{"items":{"link":"https://news.example/x","snippet":"super-secret-google-key"}}'),
    ])
    def test_api_errors_are_sanitized_and_do_not_fetch(self, status, body):
        api = ApiTransport(ApiResponse(status, body))
        pages = PageTransport()
        service, _resolver = discovery(api, pages)
        from app.services.web.google_search import GoogleSearchRefused

        with pytest.raises(GoogleSearchRefused) as caught:
            service.research("Python kya hai?")
        assert pages.calls == []
        assert KEY not in str(caught.value)
        assert CX not in str(caught.value)
        assert "super-secret" not in str(caught.value)
        assert caught.value.__cause__ is None
        assert caught.value.__context__ is None

    def test_a_transport_error_does_not_chain_the_request_url(self):
        api = ApiTransport(error=RuntimeError(f"failed {CSE}?key={KEY}&cx={CX}"))
        service, _resolver = discovery(api, PageTransport())
        from app.services.web.google_search import GoogleSearchRefused

        with pytest.raises(GoogleSearchRefused) as caught:
            service.research("Python kya hai?")
        assert KEY not in str(caught.value)
        assert CSE not in str(caught.value)
        assert caught.value.__cause__ is None
        assert caught.value.__context__ is None


class TestGoogleDiscoveryEvidence:
    def test_snippets_are_not_evidence_and_pages_are_fetched(self):
        url = "https://news.example/python"
        pages = PageTransport({
            url: html_page(
                url, "Python",
                "Python is a programming language used to explain ideas clearly.",
                published=None,
            ),
        })
        api = ApiTransport(payload(
            url, snippet="SECRET-SNIPPET says Python moved 99 percent overnight",
        ))
        service, _resolver = discovery(api, pages)
        found = service.research("Python kya hai?")
        assert pages.calls == [url]
        assert found.snippets_discarded == 1
        assert len(found.candidates) == 1
        candidate_row = found.candidates[0]
        assert "SECRET-SNIPPET" not in candidate_row.snippet
        assert "99" not in candidate_row.snippet
        assert "Python is a programming language" in candidate_row.snippet
        assert candidate_row.published_at is None
        assert candidate_row.retrieved_at == NOW
        assert candidate_row.origin == "google_live_fetch"
        assert candidate_row.company_id is None
        assert candidate_row.document_id is None
        assert candidate_row.relevance == 1.0

    def test_a_snippet_without_a_page_is_not_a_candidate(self):
        api = ApiTransport(payload(
            "https://news.example/missing",
            snippet="UNFETCHED-SNIPPET-CLAIM Python is fully explained here",
        ))
        pages = PageTransport()
        service, _resolver = discovery(api, pages)
        found = service.research("Python kya hai?")
        assert pages.calls == ["https://news.example/missing"]
        assert found.candidates == ()
        assert found.snippets_discarded == 1

    def test_duplicate_canonical_urls_are_fetched_once(self):
        url = "https://news.example/story"
        pages = PageTransport({
            url: html_page(url, "Python", "Python is a programming language with a stable definition."),
            url + "?utm_source=google": html_page(
                url, "Python", "Python is a programming language with a stable definition.",
            ),
        })
        api = ApiTransport(payload(url + "?utm_source=google", url))
        service, _resolver = discovery(api, pages)
        found = service.research("What is Python?")
        assert pages.calls == [url + "?utm_source=google"]
        assert len(found.candidates) == 1

    def test_unsafe_urls_are_not_fetched_and_a_safe_one_is(self):
        safe = "https://news.example/python"
        pages = PageTransport({
            safe: html_page(safe, "Python", "Python is a programming language used across many fields."),
        })
        # The search cap is five. The safe URL has to be inside that window
        # or the test would pass by never looking at it.
        unsafe = [
            "https://user:pass@news.example/secret",
            "http://127.0.0.1/latest/meta-data",
            "http://169.254.169.254/latest/meta-data",
            "http://metadata.google.internal/computeMetadata/v1/",
        ]
        api = ApiTransport(payload(*unsafe, safe))
        service, resolver = discovery(api, pages)
        found = service.research("Python kya hai?")
        assert pages.calls == [safe]
        assert [row.source_url for row in found.candidates] == [safe]
        assert "pass" not in " ".join(found.refusals)
        assert "127.0.0.1" not in resolver.calls
        assert "localhost" not in resolver.calls

    @pytest.mark.parametrize("url", [
        "http://10.0.0.5/admin",
        "http://localhost/secret",
        "http://intranet/admin",
        "file:///etc/passwd",
        "http://evil.example.local/x",
        "http://evil.example.internal/x",
        "http://[::1]/secret",
        "https://user:pass@news.example/secret",
    ])
    def test_unsafe_shapes_are_refused_before_a_fetch(self, url):
        from app.services.web.google_search import PublicUrlSafetyPolicy
        from app.services.web.safety import UrlSafetyError

        policy = PublicUrlSafetyPolicy(resolver=Resolver())
        with pytest.raises(UrlSafetyError):
            policy.check(url)

    def test_a_redirect_to_a_private_address_is_not_followed(self):
        start = "https://news.example/jump"
        pages = PageTransport({start: redirect(start, "http://127.0.0.1/secret")})
        api = ApiTransport(payload(start))
        service, _resolver = discovery(api, pages)
        found = service.research("Python kya hai?")
        assert pages.calls == [start]
        assert found.candidates == ()

    def test_a_public_redirect_is_rechecked_and_cited_at_the_final_url(self):
        start = "https://news.example/jump"
        final = "https://news.example/python"
        pages = PageTransport({
            start: redirect(start, final),
            final: html_page(final, "Python", "Python is a programming language with readable syntax."),
        })
        api = ApiTransport(payload(start))
        service, _resolver = discovery(api, pages)
        found = service.research("What is Python?")
        assert pages.calls == [start, final]
        assert found.candidates[0].source_url == final

    def test_robots_disallow_skips_the_page(self):
        url = "https://blocked.example/python"
        pages = PageTransport({
            url: html_page(url, "Python", "Python is a programming language that this host refuses."),
        })
        api = ApiTransport(payload(url))
        service, _resolver = discovery(api, pages)
        found = service.research("Python kya hai?")
        assert pages.calls == []
        assert found.candidates == ()

    def test_a_short_page_is_not_evidence(self):
        url = "https://news.example/short"
        pages = PageTransport({
            url: html_page(url, "Python", "Python."),
        })
        # The helper always pads. Build a genuinely short page.
        pages.pages[url] = TransportResponse(
            status_code=200,
            headers={"content-type": "text/html; charset=utf-8"},
            content=b"<html><body><p>Python.</p></body></html>",
            final_url=url, elapsed_ms=1.0, truncated=False,
            peer_address=(ADDRESS, 443),
        )
        api = ApiTransport(payload(url, snippet="SECRET-SNIPPET Python explained"))
        service, _resolver = discovery(api, pages)
        found = service.research("Python kya hai?")
        assert found.candidates == ()

    def test_a_hostname_that_resolves_privately_is_not_fetched(self):
        url = "https://rebind.example/python"
        pages = PageTransport({
            url: html_page(url, "Python", "Python is a programming language that must not be fetched."),
        })
        api = ApiTransport(payload(url))
        service, resolver = discovery(api, pages, resolver=Resolver(["10.1.2.3"]))
        found = service.research("Python kya hai?")
        assert pages.calls == []
        assert found.candidates == ()
        assert resolver.calls == ["rebind.example"]

    def test_an_unsafe_userinfo_url_does_not_suppress_its_safe_canonical(self):
        safe = "https://news.example/python"
        unsafe = "https://user:pass@news.example/python"
        pages = PageTransport({
            safe: html_page(
                safe, "Python",
                "Python is a programming language used across many fields.",
            ),
        })
        api = ApiTransport(payload(unsafe, safe))
        service, _resolver = discovery(api, pages)
        found = service.research("Python kya hai?")
        assert pages.calls == [safe]
        assert [row.source_url for row in found.candidates] == [safe]
        assert "pass" not in " ".join(found.refusals)

    def test_credentials_are_a_malformed_url_and_are_not_echoed(self):
        from app.services.web.google_search import PublicUrlSafetyPolicy
        from app.services.web.safety import UrlSafetyError

        policy = PublicUrlSafetyPolicy(resolver=Resolver())
        with pytest.raises(UrlSafetyError) as caught:
            policy.check("https://user:pass@news.example/secret")
        assert caught.value.reason is WebRejectionReason.MALFORMED_URL
        assert "pass" not in str(caught.value)
        assert len(WebRejectionReason) == 21


# ---------------------------------------------------------------------------
# Engine + analyst
# ---------------------------------------------------------------------------
class TestGeneralResearchEngine:
    def _pages(self):
        first = "https://news.example/python"
        second = "https://docs.example/python"
        text = "Python is a programming language used to explain ideas clearly."
        return PageTransport({
            first: html_page(first, "Python", text),
            second: html_page(second, "Python", text),
        }), [first, second]

    def test_disabled_google_is_an_honest_gap_without_a_call(self):
        google = _SpyGoogle(enabled=False, error=AssertionError("research called"))
        answer = engine_for(google).research(general_plan(), bound_context())
        assert google.calls == []
        assert answer.status is WebResearchStatus.DISCOVERY_DISABLED
        assert answer.company_id is None
        lowered = answer.content.lower()
        assert "this company" not in lowered
        assert "stored web index" not in lowered
        assert "web_evidence_enabled" not in lowered
        assert "python" in lowered

    def test_a_refused_search_does_not_leak_and_does_not_invent(self, monkeypatch):
        import app.services.ai.internal_web_research as engine_module

        events: list = []
        monkeypatch.setattr(
            engine_module.log, "info", lambda *args, **kwargs: events.append((args, kwargs)),
        )
        google = _SpyGoogle(error=RuntimeError(f"key={KEY}"))
        answer = engine_for(google).research(general_plan(), bound_context())
        assert answer.status is WebResearchStatus.DISCOVERY_REFUSED
        blob = answer.content + answer.reason + repr(events)
        assert KEY not in blob
        assert "this company" not in answer.content.lower()
        assert "web_evidence_enabled" not in answer.content.lower()

    def test_two_agreeing_sources_are_cited_without_a_company(self):
        pages, links = self._pages()
        api = ApiTransport(payload(*links))
        service, _resolver = discovery(api, pages)
        answer = engine_for(service).research(general_plan("What is Python?"), bound_context())
        assert answer.status is WebResearchStatus.EVIDENCE_FOUND
        assert answer.company_id is None
        assert len(answer.used_citations) == 2
        assert answer.content.startswith("Web evidence on Python")
        assert "this company" not in answer.content.lower()
        assert "SECRET-SNIPPET" not in answer.content
        assert "JSW" not in answer.content
        assert "stored web index" not in answer.content.lower()

    def test_conflicting_sources_are_both_shown(self):
        up = "https://news.example/gold"
        down = "https://markets.example/gold"
        pages = PageTransport({
            up: html_page(
                up, "Gold",
                "Gold price move is on track after buyers returned to the physical market.",
            ),
            down: html_page(
                down, "Gold",
                "Gold price move is delayed and remains under review after the session.",
            ),
        })
        api = ApiTransport(payload(up, down))
        service, _resolver = discovery(api, pages)
        answer = engine_for(service).research(
            general_plan("Aaj gold price kyu move hua?"), bound_context(),
        )
        assert answer.status is WebResearchStatus.CONFLICTING_EVIDENCE
        assert answer.company_id is None
        assert len(answer.used_citations) == 2
        assert "on track" in answer.content
        assert "delayed" in answer.content
        assert "does not reconcile" in answer.content or "choose" in answer.content
        assert "SECRET-SNIPPET" not in answer.content

    def test_insufficient_pages_do_not_use_the_snippet(self):
        api = ApiTransport(payload(
            "https://news.example/missing",
            snippet="UNFETCHED-SNIPPET-CLAIM Python is fully explained here",
        ))
        service, _resolver = discovery(api, PageTransport())
        answer = engine_for(service).research(general_plan(), bound_context())
        assert answer.status is WebResearchStatus.INSUFFICIENT_EVIDENCE
        assert "UNFETCHED-SNIPPET-CLAIM" not in answer.content
        assert "this company" not in answer.content.lower()
        assert answer.company_id is None

    def test_company_path_does_not_call_google_and_keeps_its_wording(self):
        class Google:
            enabled = True

            def research(self, question):
                raise AssertionError(question)

        eng = InternalWebResearchEngine(
            index=FakeIndex(index_result(candidate())),
            discovery=None,
            google=Google(),
            clock=lambda: COMPANY_NOW,
        )
        answer = eng.research(plan_for(), jsw_context())
        assert answer.status is WebResearchStatus.EVIDENCE_FOUND
        assert answer.content.startswith("Web evidence on JSW Steel Limited")
        assert answer.company_id == "c-jsw"


class TestAnalystPath:
    def test_identity_allows_general_without_a_company_and_still_refuses_a_mismatch(self):
        plan = general_plan()
        empty = GroundedContext(company_id="", ticker="", name="")
        safe, _reason = _company_identity_is_safe(plan, empty)
        assert safe is True

        ambiguous = replace(
            plan,
            entity=replace(plan.entity, status=EntityStatus.AMBIGUOUS, candidates=("A", "B")),
        )
        assert _company_identity_is_safe(ambiguous, bound_context())[0] is False

        mismatched = plan_for()
        other = GroundedContext(company_id="c-other", ticker="OTHER", name="Other Ltd")
        assert _company_identity_is_safe(mismatched, other)[0] is False

    def test_a_commodity_move_does_not_take_the_company_price_path(self):
        pages = PageTransport({
            "https://news.example/gold": html_page(
                "https://news.example/gold", "Gold",
                "Gold price move is on track after buyers returned to the physical market.",
            ),
        })
        api = ApiTransport(payload("https://news.example/gold"))
        service, _resolver = discovery(api, pages)
        docs = SpyDocumentService(fail_on_search=True)
        router = SpyRouter("forbid")
        analyst = analyst_for(engine_for(service), router=router, docs=docs)
        result = _run(analyst.chat(
            "Aaj gold price kyu move hua?",
            ConversationMemory(session_id="gold"),
        ))
        assert result.provider == "deterministic"
        assert "gold" in result.content.lower()
        assert router.complete_calls == []
        assert docs.search_calls == []
        assert api.calls

    @pytest.mark.parametrize("question,needle", [
        ("The rupee fell. What is the P/E of JSW Steel?", "p/e"),
        ("Crude fell. What is the P/B of JSW Steel?", "p/b"),
        ("Silver crashed. What is the EPS of JSW Steel?", "earnings per share"),
        ("The rupee fell. What is the financial quality of JSW Steel?", "financial quality"),
        ("Nifty gira. What is the current market price of JSW Steel?", "market price"),
        ("What is the current market price of JSW Steel?", "market price"),
    ])
    def test_a_commodity_move_does_not_steal_a_deterministic_intent(self, question, needle):
        class Google:
            enabled = True
            calls: list[str] = []

            def research(self, asked):
                self.calls.append(asked)
                raise AssertionError(asked)

        google = Google()
        docs = SpyDocumentService(fail_on_search=True)
        router = SpyRouter("forbid")
        analyst = analyst_for(engine_for(google), router=router, docs=docs)
        result = _run(analyst.chat(question, ConversationMemory(session_id="keep")))
        assert google.calls == []
        assert router.complete_calls == []
        assert docs.search_calls == []
        assert result.provider == "deterministic"
        assert result.model == "none"
        assert result.prompt_tokens == 0
        assert needle in result.content.lower()

    def test_explanatory_company_profiles_stay_internal_and_do_not_call_google(self):
        class Google:
            enabled = True
            calls: list[str] = []

            def research(self, asked):
                self.calls.append(asked)
                raise AssertionError(asked)

        class OpenEnded:
            def __init__(self) -> None:
                self.routes: list = []
                self.inner = InternalOpenEndedEngine()

            def answer(self, plan, context):
                self.routes.append(plan.execution_route)
                return self.inner.answer(plan, context)

        google = Google()
        opened = OpenEnded()
        router = SpyRouter("offline")
        analyst = ResearchAnalyst(
            _Builder(bound_context()),
            router=router,
            prompt_builder=PromptBuilder(),
            planner=QuestionPlanner(company_resolver=_resolve_jsw),
            composer=InternalComposer(),
            open_ended=opened,
            web_research=engine_for(google),
        )
        for question in (
            "What is JSW Steel?",
            "JSW Steel kya hai?",
            "How does JSW Steel work?",
        ):
            result = _run(analyst.chat(question, ConversationMemory(session_id="profile")))
            assert google.calls == [], question
            assert "web evidence" not in result.content.lower(), question
            assert result.provider != "deterministic" or "steel" in result.content.lower()
        assert opened.routes
        assert set(opened.routes) == {ExecutionRoute.INTERNAL_REASONING}

    def test_a_market_price_question_does_not_search_google(self):
        class Google:
            enabled = True
            calls: list[str] = []

            def research(self, question):
                self.calls.append(question)
                raise AssertionError(question)

        google = Google()
        docs = SpyDocumentService(fail_on_search=True)
        analyst = analyst_for(engine_for(google), docs=docs)
        result = _run(analyst.chat(
            "What is the current market price of JSW Steel?",
            ConversationMemory(session_id="price"),
        ))
        assert google.calls == []
        assert result.provider == "deterministic"
        assert docs.search_calls == []

    def test_sector_stays_on_the_internal_engine(self):
        class Google:
            enabled = True

            def research(self, question):
                raise AssertionError(question)

        analyst = analyst_for(engine_for(Google()))
        result = _run(analyst.chat(
            "What is the sector?", ConversationMemory(session_id="sector"),
        ))
        assert result.provider == "deterministic"
        assert "Steel" in result.content

    def test_hindi_and_hinglish_use_the_existing_language_pipeline(self, monkeypatch):
        seen: dict = {}

        class Recording:
            name = "recording"

            def is_available(self):
                return True

            async def translate(self, text, language, *, entities=None):
                seen["text"] = text
                seen["language"] = language
                return TranslationResult(
                    text=f"(rendered) {text}", language=language,
                    translated=True, provider=self.name,
                )

        monkeypatch.setattr(
            "app.services.language.adapter.build_translator",
            lambda *_a, **_k: Recording(),
        )
        url = "https://news.example/python"
        pages = PageTransport({
            url: html_page(url, "Python", "Python is a programming language used to explain ideas clearly."),
        })
        api = ApiTransport(payload(url))
        service, _resolver = discovery(api, pages)
        router = SpyRouter("forbid")
        analyst = analyst_for(engine_for(service), router=router)
        result = _run(analyst.chat(
            "Python kya hai?", ConversationMemory(session_id="hindi"),
            language=Language.HINDI,
        ))
        assert result.provider == "deterministic"
        assert result.prompt_tokens == 0
        assert router.complete_calls == []
        assert "Python is a programming language" in result.content
        assert result.display_content.startswith("(rendered) ")
        assert seen["language"] is Language.HINDI
        assert "Python" in seen["text"]

    def test_provider_isolation_and_no_second_search_provider(self):
        source = GOOGLE_PATH.read_text()
        imported = _module_imports(source)
        for banned in (
            "openai", "anthropic", "google.generativeai", "httpx", "requests",
            "app.services.ai.planner", "app.services.ai.internal_web_research",
        ):
            assert banned not in imported
            assert not any(name.startswith(banned + ".") for name in imported)
        for token in (
            "WebSearchService", "TargetedWebDiscovery", "targeted_discovery",
            "WebQueryGenerator", "SelfOwnedWebIndex", "InternalWebResearchEngine",
            "openai", "openrouter", "gemini",
        ):
            assert token not in source
        engine = ENGINE_PATH.read_text()
        assert "googleapis" not in engine
        assert "import httpx" not in engine


class TestReviewBounds:
    def test_page_fetches_use_the_interactive_policy_not_the_default(self, monkeypatch):
        from app.domain.web.types import WebFetchPolicy
        from app.services.ai.internal_web_research import INTERACTIVE_FETCH_POLICY
        from app.services.web.fetcher import WebFetcher
        from app.services.web.google_search import GoogleWebDiscovery, PublicUrlSafetyPolicy

        captured: dict = {}
        real = WebFetcher

        def spy(**kwargs):
            captured.update(kwargs)
            return real(**kwargs)

        monkeypatch.setattr("app.services.web.fetcher.WebFetcher", spy)
        robot_calls: list[str] = []

        def allow(url, *, timeout, max_bytes, user_agent):
            robot_calls.append(url)
            return 200, b"User-agent: *\nDisallow: /private\n"

        monkeypatch.setattr("app.services.web.robots._default_fetch", allow)
        url = "https://news.example/python"
        pages = PageTransport({
            url: html_page(
                url, "Python",
                "Python is a programming language used to explain ideas clearly.",
            ),
        })
        resolved = Resolver()
        service = GoogleWebDiscovery(
            enabled=True,
            api_key=KEY,
            engine_id=CX,
            api_transport=ApiTransport(payload(url)),
            page_transport=pages,
            safety=PublicUrlSafetyPolicy(resolver=resolved),
            resolver=resolved,
            politeness=HostPoliteness(default_delay=0.0, sleep=lambda _s: None),
            clock=lambda: NOW,
        )
        found = service.research("Python kya hai?")
        policy = captured["policy"]
        assert policy is INTERACTIVE_FETCH_POLICY
        assert policy.timeout_seconds == 10.0
        assert policy.max_attempts == 1
        default = WebFetchPolicy()
        assert default.timeout_seconds == 20.0
        assert default.max_attempts == 2
        assert policy.timeout_seconds != default.timeout_seconds
        assert captured["robots"]._resolver is resolved
        assert captured["robots"]._check_addresses is True
        assert pages.calls == [url]
        assert robot_calls == ["https://news.example/robots.txt"]
        assert len(found.candidates) == 1

    def test_google_robots_refuses_a_private_address_before_the_socket(self, monkeypatch):
        from app.services.web.google_search import GoogleWebDiscovery, PublicUrlSafetyPolicy

        robot_calls: list[str] = []

        def fetch(url, *, timeout, max_bytes, user_agent):
            robot_calls.append(url)
            raise AssertionError(url)

        monkeypatch.setattr("app.services.web.robots._default_fetch", fetch)
        url = "https://rebind.example/python"
        pages = PageTransport({
            url: html_page(
                url, "Python",
                "Python is a programming language that must not be fetched.",
            ),
        })
        service = GoogleWebDiscovery(
            enabled=True,
            api_key=KEY,
            engine_id=CX,
            api_transport=ApiTransport(payload(url)),
            page_transport=pages,
            safety=PublicUrlSafetyPolicy(resolver=Resolver(["93.184.216.34"])),
            resolver=Resolver(["10.1.2.3"]),
            politeness=HostPoliteness(default_delay=0.0, sleep=lambda _s: None),
            clock=lambda: NOW,
        )
        found = service.research("Python kya hai?")
        assert pages.calls == []
        assert robot_calls == []
        assert found.candidates == ()

    def test_an_oversized_search_body_is_refused_and_not_fetched(self):
        from app.services.web.google_search import MAX_CSE_BODY_BYTES, GoogleSearchRefused

        body = b"{" + KEY.encode() + b"x" * (MAX_CSE_BODY_BYTES + 8)
        api = ApiTransport(ApiResponse(200, body))
        pages = PageTransport()
        service, _resolver = discovery(api, pages)
        with pytest.raises(GoogleSearchRefused) as caught:
            service.research("Python kya hai?")
        assert pages.calls == []
        assert KEY not in str(caught.value)
        assert caught.value.__cause__ is None
        assert caught.value.__context__ is None

    def test_a_streamed_search_body_stops_at_the_cap(self):
        from app.services.web.google_search import (
            MAX_CSE_BODY_BYTES, GoogleSearchRefused, _bounded_body,
        )

        chunk = 64 * 1024
        pulled = {"n": 0}

        def chunks():
            while pulled["n"] < 80:
                pulled["n"] += 1
                yield b"a" * chunk

        with pytest.raises(GoogleSearchRefused):
            _bounded_body(chunks())
        assert pulled["n"] == (MAX_CSE_BODY_BYTES // chunk) + 1
        assert pulled["n"] < 80

    def test_the_search_client_does_not_enable_wire_logging(self, monkeypatch, caplog):
        import logging

        from app.services.web.google_search import _HttpxSearchTransport

        captured: dict = {}

        class _Stream:
            status_code = 200

            def iter_bytes(self):
                yield b'{"items":[]}'

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        class _Client:
            def __init__(self, **kwargs):
                captured["kwargs"] = kwargs

            def stream(self, method, url, *, params, headers):
                captured["params"] = dict(params)
                captured["url"] = url
                return _Stream()

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        import httpx

        monkeypatch.setattr(httpx, "Client", _Client)
        caplog.set_level(logging.DEBUG)
        _HttpxSearchTransport().get(
            CSE, params={"key": KEY, "cx": CX, "q": "Python"},
            headers={"Accept": "application/json"}, timeout=10.0,
        )
        kwargs = captured["kwargs"]
        assert kwargs.get("follow_redirects") is False
        assert not kwargs.get("event_hooks")
        assert kwargs.get("debug") in (None, False)
        assert KEY not in caplog.text
        assert CSE not in caplog.text
        source = GOOGLE_PATH.read_text()
        assert "event_hooks" not in source
        assert "basicConfig" not in source
        assert "setLevel" not in source


class TestConfig:
    def test_settings_fail_closed_and_are_documented(self):
        fresh = Settings(_env_file=None)
        assert fresh.GOOGLE_SEARCH_ENABLED is False
        assert not fresh.GOOGLE_SEARCH_API_KEY
        assert not fresh.GOOGLE_SEARCH_ENGINE_ID
        text = ENV_EXAMPLE.read_text()
        assert "WEB_EVIDENCE_ENABLED=false" in text
        assert "GOOGLE_SEARCH_ENABLED=false" in text
        assert "GOOGLE_SEARCH_API_KEY=" in text
        assert "GOOGLE_SEARCH_ENGINE_ID=" in text
        assert text.index("WEB_EVIDENCE_ENABLED=false") < text.index("GOOGLE_SEARCH_ENABLED=false")
