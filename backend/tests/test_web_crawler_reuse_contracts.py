"""Reuse contracts for Part-2 discovery: what it may import, call and *not* be.

`app/services/web/discovery.py` claims to add reach without adding
infrastructure: one robots evaluator (the fetcher's), one canonicalizer (the
extractor's), one quality judge (the quality module's), and no persistence at
all. A claim like that survives as documentation only if something checks the
structure — a reimplementation two refactors from now is otherwise invisible.

These tests read the module's AST (imports, attribute touches, forbidden
names), wrap its collaborators to count actual use, and pin the closed
vocabulary of Phase 1 against being quietly edited to accommodate Phase 2.
"""
from __future__ import annotations

import ast
import collections
import dataclasses
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import app.services.web.discovery as discovery
from app.domain.ai.types import mint_web_citation_key
from app.domain.web.types import (
    CITATION_KEY_PATTERN,
    WebDocumentRef,
    WebRejectionReason,
    WebSearchResult,
)
from app.services.web.discovery import (
    CrawlRefusal,
    CrawlRejectionReason,
    CrawlReport,
    CrawlSeed,
    WebCrawlerDiscovery,
)
from app.services.web.fetcher import HostPoliteness, TransportResponse, WebFetcher
from app.services.web.robots import RobotsPolicy
from app.services.web.safety import UrlSafetyPolicy

HOST = "www.acme.example"
SEED = f"https://{HOST}/investors"
NOW = datetime(2026, 9, 18, 9, 30, tzinfo=timezone.utc)

DISCOVERY_SOURCE = Path(discovery.__file__).read_text(encoding="utf-8")
DISCOVERY_TREE = ast.parse(DISCOVERY_SOURCE)

#: Roots this module may import. The standard library plus the two things the
#: Phase-1 web package itself leans on at module level (structlog), and bs4
#: lazily inside one function (the same import site `extract.py` uses).
ALLOWED_STDLIB_ROOTS = frozenset({
    "__future__", "collections", "dataclasses", "datetime", "enum",
    "hashlib", "re", "typing", "urllib", "xml",
})
ALLOWED_THIRD_PARTY_ROOTS = frozenset({"structlog"})
#: bs4 is only ever imported *inside* `_page_hrefs`, mirroring
#: `app.services.web.extract.extract_html`. It must not become module-level:
#: an import-time hard dependency would split the web package's failure
#: surface into "needs bs4" and "does not".
ALLOWED_LAZY_ROOTS = frozenset({"bs4"})

NOT_ALLOWED_ANYWHERE = frozenset({
    # persistence + the Phase-1 ingest path: the crawler hands refs back and
    # stops there.
    "sqlalchemy", "alembic", "psycopg", "redis", "boto3",
    # HTTP/transport: the injected fetcher owns every socket.
    "httpx", "requests", "urllib3", "aiohttp", "socket", "ssl", "http",
    # providers and models: no retrieval or generation may ride this path.
    "openai", "anthropic", "google", "gemini", "tiktoken",
    "sentence_transformers", "moto",
    # search engines: the explicit non-goal of the whole Part 3 design.
    "searx", "searxng", "tavily", "serpapi", "brave", "duckduckgo",
})


def _top_level_import_roots() -> set[str]:
    roots: set[str] = set()
    for node in DISCOVERY_TREE.body:  # module body only — nothing nested
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add((alias.name or "").split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            roots.add((node.module or "").split(".")[0])
    return roots


def _lazy_import_roots() -> set[str]:
    """Imports inside function bodies (the bs4 pattern extract.py uses)."""
    roots: set[str] = set()
    for node in ast.walk(DISCOVERY_TREE):
        if not isinstance(node, ast.FunctionDef):
            continue
        for child in ast.walk(node):
            if isinstance(child, ast.Import):
                for alias in child.names:
                    roots.add((alias.name or "").split(".")[0])
            elif isinstance(child, ast.ImportFrom):
                roots.add((child.module or "").split(".")[0])
    return roots


def _all_imported_modules() -> set[str]:
    """Full dotted module names, top level and lazy."""
    modules: set[str] = set()
    for node in ast.walk(DISCOVERY_TREE):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def _identifier_names() -> set[str]:
    names: set[str] = set()
    for node in ast.walk(DISCOVERY_TREE):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            pass
    return names


class TestModuleHygiene:
    def test_top_level_imports_are_only_stdlib_structlog_and_the_web_package(self):
        roots = _top_level_import_roots()
        app_roots = {r for r in roots if r == "app"}
        others = roots - app_roots
        assert others <= (ALLOWED_STDLIB_ROOTS | ALLOWED_THIRD_PARTY_ROOTS), (
            f"unexpected top-level imports: {sorted(others)}"
        )
        # and the app imports are all within the web layer it is built on
        for module in _all_imported_modules():
            if not module.startswith("app."):
                continue
            assert module.startswith(
                ("app.services.web.", "app.domain.web.")
            ), (
                "discovery may import the web package and its domain types "
                f"only; found '{module}'"
            )

    def test_the_only_lazy_import_is_bs4(self):
        assert _lazy_import_roots() <= ALLOWED_LAZY_ROOTS

    def test_no_forbidden_dependency_is_imported(self):
        roots = _top_level_import_roots() | _lazy_import_roots()
        assert not (roots & NOT_ALLOWED_ANYWHERE)

    def test_the_source_does_not_name_an_engine_or_a_provider(self):
        lowered = DISCOVERY_SOURCE.lower()
        for token in ("httpx", "requests.", "searx", "tavily", "serpapi",
                      "openai", "anthropic", "gemini", "getaddrinfo",
                      "urlopen"):
            assert token not in lowered, f"'{token}' must not appear in discovery"

    def test_there_is_no_second_robots_evaluator(self):
        # robots decisions arrive only via the injected fetcher. Importing
        # the robots module here would be the first step toward re-deciding.
        assert "app.services.web.robots" not in _all_imported_modules()
        names = _identifier_names()
        for banned in ("RobotFileParser", "outcome_for", "crawl_delay_for",
                       "rules_decision", "precedence_decision"):
            assert banned not in names

    def test_the_crawler_touches_no_persistence_or_cache_surface(self):
        names = _identifier_names()
        for banned in (
            "DocumentIngestionService", "IngestionError", "Session",
            "Namespace", "cache", "ingestion", "commit", "flush",
            "default_cache", "run_job",
        ):
            assert banned not in names

    def test_the_crawler_never_wires_its_own_safety_or_politeness(self):
        # These belong to the fetcher; touching them from here would be a
        # second place that decides, and two places always drift.
        names = _identifier_names()
        for banned in ("UrlSafetyPolicy", "HostPoliteness", "HttpxTransport",
                       "politeness", "transport", "safety", "sleep"):
            assert banned not in names

    def test_no_time_or_threading_surface(self):
        # politeness pacing and the crawl clock belong to injected
        # collaborators; a crawler that sleeps or parallelises has invented
        # a second politeness implementation.
        assert _top_level_import_roots().isdisjoint({"time", "threading", "asyncio"})
        names = _identifier_names()
        assert "time" not in names and "monotonic" not in names


class TestUsesTheRealCollaborators:
    """The reuse is exercised, not just imported."""

    @staticmethod
    def _mini_world(responses):
        class _Transport:
            def get(self, url, *, headers, timeout, max_bytes):
                answer = responses.get(url)
                if answer is None:
                    raise AssertionError(f"unexpected transport call: {url}")
                return answer

        def robots_fetch(url, *, timeout, max_bytes, user_agent):
            return 200, b"User-agent: *\nDisallow: /private\n"

        fetcher = WebFetcher(
            safety=UrlSafetyPolicy(allowed_hosts=(HOST,),
                                   resolver=lambda name, port: ["93.184.216.34"]),
            robots=RobotsPolicy(fetch=robots_fetch),
            transport=_Transport(),
            politeness=HostPoliteness(default_delay=0.0, sleep=lambda s: None,
                                      clock=lambda: 0.0),
            now=lambda: NOW,
        )
        return fetcher

    @staticmethod
    def _html(url: str, text: str, links=()) -> TransportResponse:
        anchors = "".join(f'<a href="{h}">{h}</a>' for h in links)
        # Padded past the quality floor so acceptance is never the variable
        # these contracts are testing.
        body = text.ljust(240, "z")
        body = (f"<html><head><title>T</title></head><body><main><p>{body}"
                f"</p>{anchors}</main></body></html>")
        return TransportResponse(
            status_code=200,
            headers={"content-type": "text/html; charset=utf-8"},
            content=body.encode(), final_url=url,
            peer_address=("93.184.216.34", 443), elapsed_ms=1.0,
            truncated=False,
        )

    def test_phase_one_functions_are_actually_called(self, monkeypatch):
        calls: collections.Counter = collections.Counter()

        def wrap(name):
            original = getattr(discovery, name)

            def inner(*args, **kwargs):
                calls[name] += 1
                return original(*args, **kwargs)

            return inner

        for name in ("canonicalize_url", "extract_page", "assess",
                     "near_duplicate_key", "build_clean_html",
                     "collapse_whitespace"):
            monkeypatch.setattr(discovery, name, wrap(name))

        responses = {
            SEED: self._html(SEED, "hub content that is comfortably over the "
                                   "two-hundred-character quality floor for "
                                   "accepted evidence pages in this package.",
                             ("/linked",)),
            f"https://{HOST}/linked": self._html(
                f"https://{HOST}/linked",
                "linked page content that comfortably passes the same "
                "quality floor with room to spare.",
            ),
        }
        fetcher = self._mini_world(responses)
        crawler = WebCrawlerDiscovery(fetcher=fetcher)
        report = crawler.crawl(CrawlSeed(url=SEED, max_depth=1,
                                         include_sitemap=False))

        assert len(report.pages) == 2
        assert calls["canonicalize_url"] >= 1  # seed + link + refs
        assert calls["extract_page"] == 2
        assert calls["assess"] == 2
        assert calls["near_duplicate_key"] == 2
        assert calls["build_clean_html"] == 2
        assert calls["collapse_whitespace"] == 2

    def test_fetcher_is_the_only_collaborator_surface(self):
        """The crawler talks to the fetcher's public `fetch`/`policy` only.

        Anything else — reaching for `.transport`, `.safety`, `.politeness`
        — would be reaching past the one component that owns the order of
        operations.
        """
        responses = {
            SEED: self._html(SEED, "body text long enough to clear the "
                                   "quality floor for an evidence page in "
                                   "this package, with margin.", ()),
        }
        inner = self._mini_world(responses)

        class Spy:
            def __init__(self, fetcher):
                self._fetcher = fetcher
                self.attr_seen: set[str] = set()
                self.fetch_calls: list[str] = []

            def fetch(self, url):
                self.fetch_calls.append(url)
                return self._fetcher.fetch(url)

            def __getattr__(self, name):
                self.attr_seen.add(name)
                return getattr(self._fetcher, name)

        spy = Spy(inner)
        crawler = WebCrawlerDiscovery(fetcher=spy)
        report = crawler.crawl(CrawlSeed(url=SEED, include_sitemap=False))

        assert spy.attr_seen == {"policy"}  # the live ceiling read
        assert spy.fetch_calls == [SEED]
        assert report.pages_fetched == len(spy.fetch_calls)

    def test_robots_policy_cache_is_reused_not_reimplemented(self):
        """One robots.txt fetch for many URLs on a host — the fetcher's cache,
        used from a crawl, not a second lookup layer."""
        robots_seen: list[str] = []

        def robots_fetch(url, *, timeout, max_bytes, user_agent):
            robots_seen.append(url)
            return 200, b"User-agent: *\nDisallow: /private\n"

        def html(url, links=()):
            anchors = "".join(f'<a href="{h}">{h}</a>' for h in links)
            return TransportResponse(
                status_code=200,
                headers={"content-type": "text/html; charset=utf-8"},
                content=(f"<html><head><title>t</title></head><body><main>"
                         f"<p>Body comfortably over the quality floor: "
                         f"padding text that repeats until the assessment "
                         f"length check is content, not a stub, anywhere."
                         f"</p>{anchors}</main></body></html>").encode(),
                final_url=url, peer_address=("93.184.216.34", 443),
                elapsed_ms=1.0, truncated=False,
            )

        responses = {
            SEED: html(SEED, ("/a", "/b", "/c")),
            f"https://{HOST}/a": html(f"https://{HOST}/a"),
            f"https://{HOST}/b": html(f"https://{HOST}/b"),
            f"https://{HOST}/c": html(f"https://{HOST}/c"),
        }

        class TableTransport:
            def get(self, url, *, headers, timeout, max_bytes):
                return responses[url]

        fetcher = WebFetcher(
            safety=UrlSafetyPolicy(allowed_hosts=(HOST,),
                                   resolver=lambda name, port: ["93.184.216.34"]),
            robots=RobotsPolicy(fetch=robots_fetch),
            transport=TableTransport(),
            politeness=HostPoliteness(default_delay=0.0, sleep=lambda s: None,
                                      clock=lambda: 0.0),
            now=lambda: NOW,
        )
        crawler = WebCrawlerDiscovery(fetcher=fetcher)
        report = crawler.crawl(CrawlSeed(url=SEED, max_depth=1,
                                         include_sitemap=False))
        assert report.pages_fetched == 4
        assert len(robots_seen) == 1  # one host, one policy fetch, cached


class TestClosedVocabularies:
    def test_phase_one_rejection_vocabulary_is_unchanged_and_unextended(self):
        # Phase 2 may add *its* enum; it may not grow Phase 1's. 21 members
        # is the Phase-1 count — an edit to the enum fails here first.
        assert len(list(WebRejectionReason)) == 21
        assert "MALFORMED_HREF" not in WebRejectionReason.__members__
        for node in ast.walk(DISCOVERY_TREE):
            assert not (isinstance(node, ast.ClassDef)
                        and node.name == "WebRejectionReason")

    def test_the_two_vocabulary_enums_do_not_collide(self):
        assert not ({r.value for r in CrawlRejectionReason}
                    & {r.value for r in WebRejectionReason})

    def test_no_production_module_imports_the_crawler_yet(self):
        """Additive by construction: discovery is reachable only through tests.

        Wiring it into the request path is a later, separately-reviewed
        change — if it is ever wired, this test tells the author they changed
        the contract deliberately.
        """
        app_root = Path(discovery.__file__).resolve().parent.parent.parent
        importable = {Path(discovery.__file__).resolve().name}
        offenders = []
        for path in app_root.rglob("*.py"):
            if path.resolve().name in importable:
                continue
            text = path.read_text(encoding="utf-8")
            if "web.discovery" in text or "web import discovery" in text:
                offenders.append(str(path.relative_to(app_root)))
        assert offenders == []


class TestDataclassContracts:
    @pytest.mark.parametrize("cls", [CrawlSeed, CrawlRefusal, CrawlReport,
                                     WebDocumentRef])
    def test_the_types_are_frozen_and_slotted(self, cls):
        # ``__dataclass_params__.frozen`` is the supported flag on 3.11;
        # ``slots`` is visible as the class attribute the decorator sets.
        assert cls.__dataclass_params__.frozen, (
            f"{cls.__name__} must be frozen like the Phase-1 types it mirrors"
        )
        assert hasattr(cls, "__slots__") and cls.__slots__, (
            f"{cls.__name__} must be slotted — a crawler report that can be "
            "mutated in flight is not evidence of anything"
        )

    def test_seed_defaults_sit_inside_every_ceiling(self):
        seed = CrawlSeed(url=SEED)
        assert 1 <= seed.max_pages <= CrawlSeed.MAX_PAGES_CEILING
        assert 0 <= seed.max_depth <= CrawlSeed.MAX_DEPTH_CEILING
        assert 1 <= seed.max_links_per_page <= CrawlSeed.MAX_LINKS_CEILING
        assert 0 <= seed.max_query_urls <= CrawlSeed.MAX_QUERY_URLS_CEILING
        assert 0 <= seed.max_sitemap_urls <= CrawlSeed.MAX_SITEMAP_URLS_CEILING
        assert seed.same_host_only is True and seed.respect_robots is True

    def test_a_frozen_seed_refuses_mutation_through_the_front_door(self):
        seed = CrawlSeed(url=SEED)
        with pytest.raises(dataclasses.FrozenInstanceError):
            seed.url = f"https://{HOST}/elsewhere"


class TestInteropWithPhaseOneResults:
    """The report is shaped so the *existing* result type can swallow it."""

    @staticmethod
    def _crawl_one_page():
        body = ("<html><head><title>Acme FY26</title></head><body><main>"
                "<p>Acme reported revenue of 1,234 crore for FY2026 with a "
                "margin of 18.4 per cent, reaffirming guidance for the year "
                "ahead in its results deck presented to the exchange. The "
                "statement also covered operating cash flow and the order "
                "book position carried into the new fiscal year.</p>"
                '<a href="/private/x">secret</a></main></body></html>').encode()
        responses = {
            SEED: TransportResponse(
                status_code=200,
                headers={"content-type": "text/html; charset=utf-8"},
                content=body, final_url=SEED,
                peer_address=("93.184.216.34", 443), elapsed_ms=1.0,
                truncated=False,
            ),
        }

        class _T:
            def get(self, url, *, headers, timeout, max_bytes):
                return responses[url]

        fetcher = WebFetcher(
            safety=UrlSafetyPolicy(allowed_hosts=(HOST,),
                                   resolver=lambda name, port: ["93.184.216.34"]),
            robots=RobotsPolicy(fetch=lambda url, **kw: (
                200, b"User-agent: *\nDisallow: /private\n")),
            transport=_T(),
            politeness=HostPoliteness(default_delay=0.0, sleep=lambda s: None,
                                      clock=lambda: 0.0),
            now=lambda: NOW,
        )
        crawler = WebCrawlerDiscovery(fetcher=fetcher)
        return crawler.crawl(CrawlSeed(url=SEED, max_depth=1,
                                       include_sitemap=False))

    def test_report_pages_are_web_document_refs_with_no_document_id(self):
        report = self._crawl_one_page()
        (ref,) = report.pages
        assert isinstance(ref, WebDocumentRef)
        assert ref.document_id is None
        key = ref.citation_key()
        assert CITATION_KEY_PATTERN.fullmatch(f"[{key}]")
        assert key == mint_web_citation_key(ref.canonical_url or ref.url)

    def test_a_search_result_can_absorb_the_report_without_an_adapter(self):
        report = self._crawl_one_page()
        result = WebSearchResult(
            company_id="acme",
            query="",
            documents=report.pages,
            rejected=report.rejected,
            details=report.details,
        )
        assert result.rejections_for(WebRejectionReason.ROBOTS_DISALLOWED) == (
            f"https://{HOST}/private/x",
        )
        payload = json.dumps(result.as_dict())
        assert "robots_disallowed" in payload
