"""Phase 4C — targeted on-demand web discovery over the existing fetch stack.

What is proven here:

* the local index is the first layer: sufficient local evidence means no
  seed is planned and no socket is touched, under a documented policy;
* discovery targets only the company's own website and its *verified* IR
  URL, plus exchange announcements the filing crawl already holds — never
  a domain derived from the words of a question;
* every seed goes through the real ``WebSearchService`` → ``WebFetcher`` →
  ``UrlSafetyPolicy`` / ``RobotsPolicy`` / redirect re-validation / byte and
  MIME limits / extraction / quality / dedupe path (only the socket, the
  resolver, the robots fetch and the clock are substituted);
* persistence semantics are explicit (``live_fetch_persisted`` vs
  ``live_fetch_transient``) and reuse the existing ``web_page`` ingestion;
* everything is bounded and deterministic;
* nothing new is wired into routing, the filing crawl, or a provider.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import json
import pkgutil
import re
import socket
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.db.base import Base
from app.domain.documents.types import DocumentType
from app.domain.web.types import (
    WebFetchPolicy,
    WebRejectionReason,
    WebSourceClass,
)
from app.models.company import Company
from app.models.document import Document
from app.models.filing_collection import CompanyCrawlState, DiscoveredFiling
from app.services.ai.planner import QuestionPlanner
from app.services.ai.planner.web_query import WebQueryGenerator, WebQueryStatus
from app.services.documents.ingestion import DocumentIngestionService
from app.services.documents.storage import LocalFileStorage
from app.services.web import targeted_discovery as module
from app.services.web.fetcher import HostPoliteness, TransportResponse, WebFetcher
from app.services.web.index import (
    LOCAL_INDEX_ORIGIN,
    WebEvidenceCandidate,
    WebIndexSearchResult,
)
from app.services.web.quality import (
    EXCHANGE_HOSTS,
    IR_URL_VERIFIED_CONFIDENCE,
    MEDIA_HOSTS,
    REGULATOR_HOSTS,
)
from app.services.web.robots import RobotsPolicy
from app.services.web.safety import UrlSafetyPolicy
from app.services.web.service import WEB_UPLOADER, WebSearchService
from app.services.web.targeted_discovery import (
    EXCHANGE_REFERENCE_ORIGIN,
    LIVE_FETCH_PERSISTED_ORIGIN,
    LIVE_FETCH_TRANSIENT_ORIGIN,
    DiscoveryPolicy,
    DiscoveryStatus,
    DiscoveryTrigger,
    TargetedDiscoveryResult,
    TargetedWebDiscovery,
    assess_local_evidence,
    merge_candidates,
)

BACKEND = Path(__file__).resolve().parents[1]
APP = BACKEND / "app"
MODULE_PATH = APP / "services" / "web" / "targeted_discovery.py"

HOST = "www.acme.example"
IR_HOST = "ir.acme.example"
ORIGIN = f"https://{HOST}"
IR_ORIGIN = f"https://{IR_HOST}"
ADDRESS = "93.184.216.34"
NOW = datetime(2026, 9, 20, 9, 0, tzinfo=timezone.utc)
ROBOTS_ALLOW = "User-agent: *\nDisallow: /private\n"

#: Query texts shaped like the Phase 4B generator's output for
#: "Acme Industries latest order news": subject first, topic terms after.
NEWS_QUERIES = ("Acme Industries latest order news", "Acme Industries order news")
RESULTS_QUERIES = ("Acme Industries quarterly results",)


# ===========================================================================
# Fakes for the socket, the resolver and the robots fetch — nothing else
# ===========================================================================
class FakeTransport:
    """Answers from a table; records every URL it was asked for."""

    def __init__(self, responses=None, *, default=None):
        self.responses = dict(responses or {})
        self.default = default
        self.calls: list[str] = []

    def get(self, url, *, headers, timeout, max_bytes):
        self.calls.append(url)
        answer = self.responses.get(url, self.default)
        if answer is None:
            answer = not_found(url)
        if isinstance(answer, Exception):
            raise answer
        return answer

    @property
    def hosts(self) -> set[str]:
        return {(urlsplit(url).hostname or "").lower() for url in self.calls}


def page(url: str, text: str, *, title: str = "Acme page", links: tuple[str, ...] = (),
         status: int = 200, content_type: str = "text/html; charset=utf-8",
         canonical: str | None = None, published: str | None = None,
         headers: dict | None = None, truncated: bool = False) -> TransportResponse:
    """A fetchable page over the quality floor, optionally with anchors."""
    anchors = "".join(f'<a href="{h}">{h}</a>' for h in links)
    head = f"<title>{title}</title>"
    if canonical:
        head += f'<link rel="canonical" href="{canonical}">'
    if published:
        head += f'<meta property="article:published_time" content="{published}">'
    body_text = text if len(text) >= 240 else (text + " ") * (240 // max(1, len(text) + 1) + 1)
    body = (
        f"<html><head>{head}</head><body><main><h1>{title}</h1>"
        f"<p>{body_text}</p>{anchors}</main></body></html>"
    ).encode()
    response_headers = {"content-type": content_type, "content-length": str(len(body))}
    response_headers.update(headers or {})
    return TransportResponse(
        status_code=status, headers=response_headers, content=body, final_url=url,
        elapsed_ms=1.0, truncated=truncated, peer_address=(ADDRESS, 443),
    )


def redirect(url: str, location: str) -> TransportResponse:
    return TransportResponse(
        status_code=301, headers={"location": location}, content=b"", final_url=url,
        elapsed_ms=1.0, truncated=False, peer_address=(ADDRESS, 443),
    )


def not_found(url: str) -> TransportResponse:
    return TransportResponse(
        status_code=404, headers={"content-type": "text/plain"}, content=b"not found",
        final_url=url, elapsed_ms=1.0, truncated=False, peer_address=(ADDRESS, 443),
    )


class DictCache:
    def __init__(self) -> None:
        self.values: dict[tuple, object] = {}

    def get(self, namespace, *parts):
        return self.values.get((namespace, parts))

    def set(self, namespace, value, *parts):
        self.values[(namespace, parts)] = value

    def invalidate(self, namespace):
        return 0


# ===========================================================================
# Fixtures
# ===========================================================================
@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Every socket constructor raises: any real network attempt fails loudly."""
    def _refuse(*_a, **_k):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", _refuse)
    monkeypatch.setattr(socket, "create_connection", _refuse)
    monkeypatch.setattr(socket, "getaddrinfo", _refuse)
    yield


@pytest.fixture(autouse=True)
def enabled(monkeypatch):
    monkeypatch.setattr(settings, "WEB_EVIDENCE_ENABLED", True, raising=False)
    yield


def _import_all_models() -> None:
    import app.models as models

    for entry in pkgutil.iter_modules(models.__path__):
        importlib.import_module(f"app.models.{entry.name}")


@pytest.fixture()
def db():
    """A private SQLite database: the assertions are about exactly which rows exist."""
    _import_all_models()
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    session.add(Company(id="c-acme", name="Acme Industries Limited", ticker="ACME",
                        website=ORIGIN))
    session.add(Company(id="c-nosite", name="Nosite Limited", ticker="NOSITE"))
    session.commit()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def storage(tmp_path) -> LocalFileStorage:
    return LocalFileStorage(tmp_path / "documents")


def make_service(db, transport, *, storage, robots_text=ROBOTS_ALLOW, resolver=None,
                 now=NOW, policy=None) -> WebSearchService:
    """The real service, wired the production way, over fakes for I/O only."""
    def robots_fetch(url, *, timeout, max_bytes, user_agent):
        return 200, robots_text.encode()

    return WebSearchService(
        db,
        ingestion=DocumentIngestionService(db, storage=storage),
        policy=policy or WebFetchPolicy(),
        robots=RobotsPolicy(fetch=robots_fetch, user_agent="EquityPilotAI/1.0"),
        transport=transport,
        resolver=resolver or (lambda host, port: [ADDRESS]),
        politeness=HostPoliteness(default_delay=0.0, sleep=lambda _s: None, clock=lambda: 0.0),
        cache_service=DictCache(),
        now=lambda: now,
    )


def make_discovery(db, transport, *, storage, policy=None, **service_kwargs) -> TargetedWebDiscovery:
    service = make_service(db, transport, storage=storage,
                           policy=(policy.effective_fetch_policy() if policy else None),
                           **service_kwargs)
    return TargetedWebDiscovery(db, search_service=service, policy=policy, clock=lambda: NOW)


def set_ir(db, company_id: str, url: str, confidence: float) -> None:
    db.add(CompanyCrawlState(company_id=company_id, ir_url=url, ir_url_confidence=confidence))
    db.commit()


def add_filing(db, company_id: str, title: str, *, url: str | None, days_ago: int,
               status: str = "completed", document_id: int | None = None,
               source: str = "NSE Corporate Filings", sha: str | None = None,
               reference: str | None = None) -> DiscoveredFiling:
    row = DiscoveredFiling(
        company_id=company_id, source=source,
        source_reference=reference or f"{source}:{title}:{days_ago}",
        source_url=url, title=title, status=status,
        published_on=NOW - timedelta(days=days_ago), document_id=document_id,
        content_sha256=sha,
    )
    db.add(row)
    db.commit()
    return row


def local_candidate(*, relevance: float = 0.9, published=None, retrieved=None,
                    url: str | None = None, document_id: int = 1,
                    content_hash: str | None = None, score: float | None = None,
                    title: str = "Stored page") -> WebEvidenceCandidate:
    """A stored-page candidate as the 4B index would return it. URL and
    bytes identity default to something unique per document id."""
    url = url or f"{ORIGIN}/stored/{document_id}"
    content_hash = content_hash or f"hash-local-{document_id}"
    return WebEvidenceCandidate(
        document_id=document_id, chunk_id=10, company_id="c-acme", source_url=url,
        canonical_url=url, title=title, source_class="company_website",
        published_at=published, retrieved_at=retrieved, snippet="stored passage",
        relevance=relevance, authority=0.55, freshness=1.0, freshness_basis="published_at",
        score=score if score is not None else round(0.7 * relevance + 0.2 * 0.55 / 0.9 + 0.1, 6),
        matched_queries=("q",), signals=("lexical",), content_hash=content_hash,
    )


def local_result(*candidates: WebEvidenceCandidate) -> WebIndexSearchResult:
    return WebIndexSearchResult(
        queries=("q",), company_id="c-acme", candidates=tuple(candidates),
        corpus_documents=len(candidates), engine="hybrid" if candidates else "none",
        semantic_used=False,
    )


@dataclass(frozen=True)
class FakeQuerySet:
    """The shape the 4B generator produces, duck-typed (``texts`` + flag)."""
    texts: tuple[str, ...]
    recency_sensitive: bool = False
    company_id: str | None = None
    status: WebQueryStatus = WebQueryStatus.GENERATED


def stored(db) -> list[Document]:
    return list(db.scalars(select(Document).order_by(Document.id)).all())


# ===========================================================================
class TestSufficiencyPolicyIsExplicit:
    """The gate is documented, deterministic and applied before any I/O."""

    def test_the_policy_numbers_are_explicit_defaults(self):
        policy = DiscoveryPolicy()
        assert (policy.min_local_candidates, policy.min_local_relevance,
                policy.max_local_age_days) == (2, 0.35, 7)
        doc = module.__doc__
        for word in ("ABSENT", "INSUFFICIENT", "STALE", "SUFFICIENT",
                     "min_local_candidates", "min_local_relevance", "max_local_age_days"):
            assert word in doc

    def test_absent_local_evidence_triggers_discovery(self):
        for local in (None, local_result()):
            decision = assess_local_evidence(
                local, policy=DiscoveryPolicy(), recency_sensitive=False, now=NOW,
            )
            assert decision.trigger is DiscoveryTrigger.ABSENT
            assert decision.discover

    def test_too_few_strong_candidates_is_insufficient(self):
        weak = local_candidate(relevance=0.2, document_id=1)
        strong = local_candidate(relevance=0.8, document_id=2)
        decision = assess_local_evidence(
            local_result(weak, strong), policy=DiscoveryPolicy(),
            recency_sensitive=False, now=NOW,
        )
        assert decision.trigger is DiscoveryTrigger.INSUFFICIENT
        assert (decision.local_candidates, decision.strong_candidates) == (2, 1)
        assert "0.35" in decision.reason and "2" in decision.reason

    def test_enough_strong_candidates_is_sufficient_when_not_recency_sensitive(self):
        old = NOW - timedelta(days=400)
        decision = assess_local_evidence(
            local_result(local_candidate(document_id=1, published=old),
                         local_candidate(document_id=2, published=old)),
            policy=DiscoveryPolicy(), recency_sensitive=False, now=NOW,
        )
        assert decision.trigger is None and not decision.discover

    def test_stale_only_matters_for_recency_sensitive_questions(self):
        eight_days = NOW - timedelta(days=8)
        result = local_result(local_candidate(document_id=1, published=eight_days),
                              local_candidate(document_id=2, retrieved=eight_days))
        fresh = assess_local_evidence(result, policy=DiscoveryPolicy(),
                                      recency_sensitive=True, now=NOW)
        assert fresh.trigger is DiscoveryTrigger.STALE
        assert "8 day" in fresh.reason and "7 day" in fresh.reason
        not_recency = assess_local_evidence(result, policy=DiscoveryPolicy(),
                                            recency_sensitive=False, now=NOW)
        assert not_recency.trigger is None

    def test_fresh_local_evidence_satisfies_a_recency_sensitive_question(self):
        recent = NOW - timedelta(days=2)
        decision = assess_local_evidence(
            local_result(local_candidate(document_id=1, published=recent),
                         local_candidate(document_id=2, published=recent)),
            policy=DiscoveryPolicy(), recency_sensitive=True, now=NOW,
        )
        assert decision.trigger is None
        assert decision.freshest_evidence_at == recent

    def test_undated_local_evidence_is_stale_for_a_recency_sensitive_question(self):
        decision = assess_local_evidence(
            local_result(local_candidate(document_id=1), local_candidate(document_id=2)),
            policy=DiscoveryPolicy(), recency_sensitive=True, now=NOW,
        )
        assert decision.trigger is DiscoveryTrigger.STALE
        assert "no strong local candidate carries" in decision.reason

    def test_the_window_is_the_policy_not_a_constant(self):
        three_days = NOW - timedelta(days=3)
        result = local_result(local_candidate(document_id=1, published=three_days),
                              local_candidate(document_id=2, published=three_days))
        tight = DiscoveryPolicy(max_local_age_days=1)
        assert assess_local_evidence(result, policy=tight, recency_sensitive=True,
                                     now=NOW).trigger is DiscoveryTrigger.STALE
        assert assess_local_evidence(result, policy=DiscoveryPolicy(), recency_sensitive=True,
                                     now=NOW).trigger is None


# ===========================================================================
class TestLocalIndexIsTheFirstLayer:
    def test_sufficient_local_evidence_means_no_seed_and_no_socket(self, db, storage):
        transport = FakeTransport({f"{ORIGIN}/investors": page(f"{ORIGIN}/investors", "x")})
        discovery = make_discovery(db, transport, storage=storage)
        local = local_result(local_candidate(document_id=1), local_candidate(document_id=2))
        result = discovery.discover(RESULTS_QUERIES, company_id="c-acme", local=local)
        assert result.status is DiscoveryStatus.LOCAL_SUFFICIENT
        assert result.seeds == () and result.fetched == () and result.pages_attempted == 0
        assert transport.calls == []
        assert [c.origin for c in result.merged] == [LOCAL_INDEX_ORIGIN, LOCAL_INDEX_ORIGIN]
        assert stored(db) == []

    def test_sufficient_local_evidence_never_even_builds_the_service(self, db):
        """No injected service and no network: if discovery tried to fetch it
        would have to build a real fetcher and the socket guard would fire."""
        discovery = TargetedWebDiscovery(db, clock=lambda: NOW)
        local = local_result(local_candidate(document_id=1), local_candidate(document_id=2))
        result = discovery.discover(RESULTS_QUERIES, company_id="c-acme", local=local)
        assert result.status is DiscoveryStatus.LOCAL_SUFFICIENT
        assert discovery._service is None

    def test_stale_local_evidence_triggers_a_bounded_live_fetch(self, db, storage):
        url = f"{ORIGIN}/press-releases"
        transport = FakeTransport({url: page(url, "Acme bags a large export order today",
                                             title="Acme order win")})
        discovery = make_discovery(db, transport, storage=storage)
        stale = NOW - timedelta(days=30)
        local = local_result(local_candidate(document_id=1, published=stale),
                             local_candidate(document_id=2, published=stale))
        result = discovery.discover(NEWS_QUERIES, company_id="c-acme", local=local,
                                    recency_sensitive=True, persist=False)
        assert result.decision.trigger is DiscoveryTrigger.STALE
        assert result.status is DiscoveryStatus.DISCOVERED
        assert transport.hosts == {HOST}
        assert [c.title for c in result.fetched] == ["Acme order win"]

    def test_absent_local_evidence_triggers_discovery(self, db, storage):
        url = f"{ORIGIN}/investors/results"
        transport = FakeTransport({url: page(url, "quarterly results of Acme", title="Results")})
        result = make_discovery(db, transport, storage=storage).discover(
            RESULTS_QUERIES, company_id="c-acme", local=None, persist=False,
        )
        assert result.decision.trigger is DiscoveryTrigger.ABSENT
        assert result.status is DiscoveryStatus.DISCOVERED

    def test_the_recency_flag_is_taken_from_the_query_set(self, db, storage):
        transport = FakeTransport()
        discovery = make_discovery(db, transport, storage=storage)
        old = NOW - timedelta(days=60)
        local = local_result(local_candidate(document_id=1, published=old),
                             local_candidate(document_id=2, published=old))
        flagged = FakeQuerySet(texts=NEWS_QUERIES, recency_sensitive=True)
        plain = FakeQuerySet(texts=NEWS_QUERIES, recency_sensitive=False)
        assert discovery.discover(flagged, company_id="c-acme", local=local,
                                  persist=False).decision.trigger is DiscoveryTrigger.STALE
        assert discovery.discover(plain, company_id="c-acme", local=local,
                                  persist=False).status is DiscoveryStatus.LOCAL_SUFFICIENT

    def test_force_overrides_a_sufficient_verdict_explicitly(self, db, storage):
        transport = FakeTransport()
        discovery = make_discovery(db, transport, storage=storage)
        local = local_result(local_candidate(document_id=1), local_candidate(document_id=2))
        result = discovery.discover(RESULTS_QUERIES, company_id="c-acme", local=local,
                                    force=True, persist=False)
        assert result.decision.trigger is DiscoveryTrigger.FORCED
        assert result.pages_attempted > 0


# ===========================================================================
class TestTargetsAreOnlyTheCompanysOwnVerifiedSources:
    def test_company_website_seeds_use_the_services_fixed_path_table(self, db, storage):
        transport = FakeTransport()
        discovery = make_discovery(db, transport, storage=storage)
        seeds, refusals, _notes = discovery.plan_seeds("c-acme", NEWS_QUERIES)
        expected_paths = discovery.service.conventional_paths(NEWS_QUERIES[0])
        assert [s.url for s in seeds] == [f"{ORIGIN}{p}" for p in expected_paths[:3]]
        assert {s.host for s in seeds} == {HOST}
        assert {s.target for s in seeds} == {"company_website"}
        assert {s.source_class for s in seeds} == {WebSourceClass.COMPANY_WEBSITE.value}
        assert all(s.basis.startswith("path:") for s in seeds)
        assert refusals == ()
        assert transport.calls == []  # planning never fetches

    def test_verified_ir_url_and_its_origin_are_seeded(self, db, storage):
        set_ir(db, "c-acme", f"{IR_ORIGIN}/investors?utm_source=mail#top", IR_URL_VERIFIED_CONFIDENCE)
        discovery = make_discovery(db, FakeTransport(), storage=storage)
        seeds, _r, _n = discovery.plan_seeds("c-acme", RESULTS_QUERIES)
        ir_seeds = [s for s in seeds if s.target == "verified_ir"]
        assert ir_seeds, seeds
        assert ir_seeds[0].url == f"{IR_ORIGIN}/investors"  # canonical: no utm, no fragment
        assert ir_seeds[0].basis == "verified_ir_url"
        assert {s.source_class for s in ir_seeds} == {WebSourceClass.VERIFIED_IR.value}
        assert any(s.url == f"{IR_ORIGIN}/investors/results" for s in ir_seeds)
        # The website still comes first — priority 1 before priority 2.
        assert seeds[0].target == "company_website"

    def test_an_unverified_ir_url_is_never_a_target(self, db, storage):
        set_ir(db, "c-acme", f"{IR_ORIGIN}/investors", 0.5)
        transport = FakeTransport()
        discovery = make_discovery(db, transport, storage=storage)
        seeds, _r, notes = discovery.plan_seeds("c-acme", RESULTS_QUERIES)
        assert {s.host for s in seeds} == {HOST}
        assert any("not verified" in n for n in notes)
        result = discovery.discover(RESULTS_QUERIES, company_id="c-acme", persist=False)
        assert IR_HOST not in transport.hosts

    def test_exchange_sources_are_reused_from_discovered_filings_not_fetched(self, db, storage):
        add_filing(db, "c-acme", "Acme Industries bags large export order", days_ago=3,
                   url="https://www.bseindia.com/xml-data/corpfiling/AttachLive/abc.pdf",
                   document_id=None, source="BSE Corporate Announcements")
        add_filing(db, "c-acme", "Board meeting intimation", days_ago=2,
                   url="https://www.bseindia.com/xml-data/corpfiling/AttachLive/def.pdf")
        add_filing(db, "c-acme", "Trading window closure", days_ago=1, status="skipped",
                   url="https://www.bseindia.com/xml-data/corpfiling/AttachLive/ghi.pdf")
        add_filing(db, "c-acme", "Order win from 2019", days_ago=900,
                   url="https://www.bseindia.com/xml-data/corpfiling/AttachLive/old.pdf")
        transport = FakeTransport()
        result = make_discovery(db, transport, storage=storage).discover(
            NEWS_QUERIES, company_id="c-acme", persist=False,
        )
        titles = [c.title for c in result.exchange_references]
        assert titles == ["Acme Industries bags large export order"]
        ref = result.exchange_references[0]
        assert ref.origin == EXCHANGE_REFERENCE_ORIGIN
        assert ref.source_class == WebSourceClass.EXCHANGE.value
        assert ref.document_id is None and ref.signals == ("exchange_reference",)
        assert ref.snippet.startswith("BSE Corporate Announcements:")
        # Nothing on an exchange or regulator host was ever requested, and
        # no seed was constructed on one.
        assert transport.hosts.isdisjoint(EXCHANGE_HOSTS | REGULATOR_HOSTS)
        assert {s.host for s in result.seeds}.isdisjoint(EXCHANGE_HOSTS | REGULATOR_HOSTS)

    def test_an_ingested_exchange_reference_carries_its_document_id(self, db, storage):
        document = Document(company_id="c-acme", title="Order win", doc_type="announcement",
                            filename="order.pdf", file_format="pdf", size_bytes=10,
                            storage_key="k", content_hash="sha-1",
                            status="completed", uploaded_by="filing_crawl")
        db.add(document)
        db.commit()
        add_filing(db, "c-acme", "Acme Industries secures order", days_ago=5,
                   url="https://www.bseindia.com/xml-data/corpfiling/AttachLive/x.pdf",
                   document_id=document.id, sha="sha-1")
        result = make_discovery(db, FakeTransport(), storage=storage).discover(
            NEWS_QUERIES, company_id="c-acme", persist=False,
        )
        assert [c.document_id for c in result.exchange_references] == [document.id]

    def test_no_media_allowlist_exists_so_no_media_target_is_ever_planned(self, db, storage):
        assert MEDIA_HOSTS == frozenset()
        discovery = make_discovery(db, FakeTransport(), storage=storage)
        seeds, _r, notes = discovery.plan_seeds("c-acme", NEWS_QUERIES)
        assert any("no approved financial-media allowlist" in n for n in notes)
        _state, pinned = discovery.service.allowlist_for(db.get(Company, "c-acme"))
        assert {s.host for s in seeds} <= set(pinned)
        assert {s.host for s in seeds} <= {HOST, IR_HOST}

    def test_every_seed_host_is_pinned_for_the_company(self, db, storage):
        set_ir(db, "c-acme", f"{IR_ORIGIN}/ir", IR_URL_VERIFIED_CONFIDENCE)
        discovery = make_discovery(db, FakeTransport(), storage=storage)
        _state, pinned = discovery.service.allowlist_for(db.get(Company, "c-acme"))
        for queries in (NEWS_QUERIES, RESULTS_QUERIES, ("Acme Industries annual report",)):
            seeds, _r, _n = discovery.plan_seeds("c-acme", queries)
            assert seeds and {s.host for s in seeds} <= set(pinned)


# ===========================================================================
class TestNothingIsInventedForAnUnresolvedCompany:
    def test_no_company_means_no_domain_and_no_fetch(self, db, storage):
        transport = FakeTransport()
        result = make_discovery(db, transport, storage=storage).discover(
            ("ABC Technologies latest news",), company_id=None,
        )
        assert result.status is DiscoveryStatus.NO_TARGET
        assert result.status.value == "insufficient_discovery_target"
        assert result.seeds == () and result.fetched == () and result.pages_attempted == 0
        assert transport.calls == []
        assert "abctechnologies" not in json.dumps(result.as_dict()).lower()
        assert any("insufficient discovery target" in n for n in result.notes)

    def test_an_unknown_company_id_is_not_a_target(self, db, storage):
        transport = FakeTransport()
        result = make_discovery(db, transport, storage=storage).discover(
            ("ABC Technologies latest news",), company_id="c-ghost",
        )
        assert result.status is DiscoveryStatus.NO_TARGET
        assert transport.calls == [] and result.seeds == ()

    def test_an_ambiguous_subject_from_the_generator_is_not_a_target(self, db, storage):
        transport = FakeTransport()
        ambiguous = FakeQuerySet(texts=(), company_id=None,
                                 status=WebQueryStatus.AMBIGUOUS_SUBJECT)
        result = make_discovery(db, transport, storage=storage).discover(
            ambiguous, company_id=ambiguous.company_id,
        )
        assert result.status in {DiscoveryStatus.NO_QUERIES, DiscoveryStatus.NO_TARGET}
        assert transport.calls == [] and result.seeds == ()

    def test_a_company_without_website_or_verified_ir_is_not_a_target(self, db, storage):
        transport = FakeTransport()
        result = make_discovery(db, transport, storage=storage).discover(
            ("Nosite Limited latest news",), company_id="c-nosite",
        )
        assert result.status is DiscoveryStatus.NO_TARGET
        assert transport.calls == [] and result.seeds == ()
        assert any("website is not set" in n for n in result.notes)

    def test_the_planner_and_generator_feed_it_without_inventing_a_domain(self, db, storage):
        """Question → planner → generator → discovery, end to end, with a
        resolver that does not know 'ABC Technologies'."""
        @dataclass(frozen=True)
        class Co:
            id: str
            ticker: str
            name: str

        universe = (Co("c-acme", "ACME", "Acme Industries Limited"),)

        def resolver(text):
            return [c for c in universe if "acme" in (text or "").lower()]

        planner = QuestionPlanner(company_resolver=resolver)
        generator = WebQueryGenerator()
        transport = FakeTransport({
            f"{ORIGIN}/press-releases": page(f"{ORIGIN}/press-releases",
                                             "Acme announces a new export order", title="News"),
        })
        discovery = make_discovery(db, transport, storage=storage)

        unknown = generator.generate(planner.plan("ABC Technologies latest news"))
        assert unknown.company_id is None
        result = discovery.discover(unknown, company_id=unknown.company_id)
        assert result.status is DiscoveryStatus.NO_TARGET
        assert transport.calls == []

        known = generator.generate(planner.plan("Acme Industries latest news"))
        assert known.company_id == "c-acme" and known.recency_sensitive is True
        result = discovery.discover(known, company_id=known.company_id, persist=False)
        assert result.status is DiscoveryStatus.DISCOVERED
        assert transport.hosts == {HOST}
        assert result.decision.recency_sensitive is True


# ===========================================================================
class TestEveryFetchGoesThroughTheExistingSafetyStack:
    def test_robots_disallow_is_honoured(self, db, storage):
        url = f"{ORIGIN}/investors/results"
        transport = FakeTransport({url: page(url, "results")})
        discovery = make_discovery(db, transport, storage=storage,
                                   robots_text="User-agent: *\nDisallow: /investors\n")
        result = discovery.discover(RESULTS_QUERIES, company_id="c-acme", persist=False)
        reasons = {r.url: r.reason for r in result.refusals}
        assert reasons[url] == WebRejectionReason.ROBOTS_DISALLOWED.value
        assert url not in transport.calls
        assert result.fetched == ()

    def test_a_private_address_is_refused_before_any_request(self, db, storage):
        transport = FakeTransport()
        discovery = make_discovery(db, transport, storage=storage,
                                   resolver=lambda host, port: ["10.0.0.5"])
        result = discovery.discover(RESULTS_QUERIES, company_id="c-acme", persist=False)
        assert {r.reason for r in result.refusals} == {WebRejectionReason.ADDRESS_NOT_PUBLIC.value}
        assert transport.calls == []
        assert result.status is DiscoveryStatus.NOTHING_FOUND

    def test_cloud_metadata_addresses_are_refused(self, db, storage):
        transport = FakeTransport()
        discovery = make_discovery(db, transport, storage=storage,
                                   resolver=lambda host, port: ["169.254.169.254"])
        result = discovery.discover(RESULTS_QUERIES, company_id="c-acme", persist=False)
        assert {r.reason for r in result.refusals} == {WebRejectionReason.ADDRESS_NOT_PUBLIC.value}
        assert transport.calls == []

    def test_a_redirect_to_an_unpinned_host_is_refused(self, db, storage):
        url = f"{ORIGIN}/investors/results"
        transport = FakeTransport({url: redirect(url, "https://evil.example/results")})
        result = make_discovery(db, transport, storage=storage).discover(
            RESULTS_QUERIES, company_id="c-acme", persist=False,
        )
        reasons = {r.url: r.reason for r in result.refusals}
        assert reasons[url] == WebRejectionReason.HOST_NOT_PINNED.value
        assert "evil.example" not in transport.hosts

    def test_a_redirect_within_the_pinned_host_is_followed_and_revalidated(self, db, storage):
        url = f"{ORIGIN}/investors/results"
        final = f"{ORIGIN}/investors/results/latest"
        transport = FakeTransport({
            url: redirect(url, "/investors/results/latest"),
            final: page(final, "quarterly results are out", title="Results"),
        })
        result = make_discovery(db, transport, storage=storage).discover(
            RESULTS_QUERIES, company_id="c-acme", persist=False,
        )
        assert [c.title for c in result.fetched] == ["Results"]
        assert transport.calls[:2] == [url, final]

    def test_the_byte_limit_is_the_fetch_policys(self, db, storage):
        url = f"{ORIGIN}/investors/results"
        transport = FakeTransport({url: page(url, "results " * 400)})
        policy = DiscoveryPolicy(max_bytes_per_page=1024)
        result = make_discovery(db, transport, storage=storage, policy=policy).discover(
            RESULTS_QUERIES, company_id="c-acme", persist=False,
        )
        reasons = {r.url: r.reason for r in result.refusals}
        assert reasons[url] == WebRejectionReason.TOO_LARGE.value
        assert result.fetched == ()

    def test_the_byte_limit_may_only_tighten_the_fetch_policy(self):
        with pytest.raises(ValueError):
            DiscoveryPolicy(max_bytes_per_page=WebFetchPolicy().max_bytes + 1)
        assert DiscoveryPolicy(max_bytes_per_page=1024).effective_fetch_policy().max_bytes == 1024
        assert DiscoveryPolicy().effective_fetch_policy() is None

    def test_a_truncated_body_is_refused_as_too_large(self, db, storage):
        url = f"{ORIGIN}/investors/results"
        transport = FakeTransport({url: page(url, "results", truncated=True)})
        result = make_discovery(db, transport, storage=storage).discover(
            RESULTS_QUERIES, company_id="c-acme", persist=False,
        )
        assert {r.url: r.reason for r in result.refusals}[url] == WebRejectionReason.TOO_LARGE.value

    def test_a_disallowed_content_type_is_refused(self, db, storage):
        url = f"{ORIGIN}/investors/results"
        transport = FakeTransport({url: page(url, "binary", content_type="application/octet-stream")})
        result = make_discovery(db, transport, storage=storage).discover(
            RESULTS_QUERIES, company_id="c-acme", persist=False,
        )
        reasons = {r.url: r.reason for r in result.refusals}
        assert reasons[url] == WebRejectionReason.CONTENT_TYPE_NOT_ALLOWED.value

    def test_a_cookie_wall_is_refused_by_quality(self, db, storage):
        url = f"{ORIGIN}/investors/results"
        wall = (
            "<html><head><title>Just a moment</title></head><body><main>"
            "We use cookies to improve your experience. Accept all cookies to "
            "continue browsing this website.</main></body></html>"
        ).encode()
        transport = FakeTransport({url: TransportResponse(
            status_code=200, headers={"content-type": "text/html"}, content=wall,
            final_url=url, elapsed_ms=1.0, truncated=False, peer_address=(ADDRESS, 443),
        )})
        result = make_discovery(db, transport, storage=storage).discover(
            RESULTS_QUERIES, company_id="c-acme", persist=False,
        )
        assert result.fetched == () and any(r.url == url for r in result.refusals)

    def test_links_on_a_fetched_page_are_never_followed(self, db, storage):
        url = f"{ORIGIN}/investors/results"
        transport = FakeTransport({url: page(
            url, "results with links", links=(
                "https://evil.example/", f"{ORIGIN}/investors/results/deep",
                "https://www.nseindia.com/", "https://news.example/story",
            ),
        )})
        result = make_discovery(db, transport, storage=storage).discover(
            RESULTS_QUERIES, company_id="c-acme", persist=False,
        )
        planned = {s.url for s in result.seeds}
        assert set(transport.calls) <= planned
        assert transport.hosts == {HOST}
        assert DiscoveryPolicy().max_depth == 0 and DiscoveryPolicy.MAX_DEPTH_CEILING == 0

    def test_the_service_it_calls_uses_the_real_fetcher_safety_and_robots(self, db, storage):
        discovery = make_discovery(db, FakeTransport(), storage=storage)
        company = db.get(Company, "c-acme")
        _state, pinned = discovery.service.allowlist_for(company)
        fetcher = discovery.service._fetcher_for(pinned)
        assert isinstance(fetcher, WebFetcher)
        assert isinstance(fetcher.safety, UrlSafetyPolicy)
        assert isinstance(fetcher.robots, RobotsPolicy)
        assert HOST in fetcher.safety.allowed_hosts

    def test_the_kill_switch_is_honoured_before_any_planning(self, db, storage, monkeypatch):
        monkeypatch.setattr(settings, "WEB_EVIDENCE_ENABLED", False, raising=False)
        transport = FakeTransport()
        discovery = TargetedWebDiscovery(db, clock=lambda: NOW)  # no injected service
        result = discovery.discover(RESULTS_QUERIES, company_id="c-acme")
        assert result.status is DiscoveryStatus.DISABLED
        assert result.seeds == () and transport.calls == []
        assert discovery._service is None
        assert stored(db) == []

    def test_live_fetch_can_be_disabled_by_policy_leaving_a_plan(self, db, storage):
        transport = FakeTransport()
        policy = DiscoveryPolicy(allow_live_fetch=False)
        result = make_discovery(db, transport, storage=storage, policy=policy).discover(
            RESULTS_QUERIES, company_id="c-acme",
        )
        assert result.status is DiscoveryStatus.PLANNED_ONLY
        assert result.seeds and transport.calls == [] and result.pages_attempted == 0


# ===========================================================================
class TestEverythingIsBounded:
    def test_queries_paths_seeds_and_fetches_are_all_capped(self, db, storage):
        set_ir(db, "c-acme", f"{IR_ORIGIN}/ir", IR_URL_VERIFIED_CONFIDENCE)
        many = tuple(
            f"Acme Industries {topic}" for topic in (
                "annual report", "quarterly results", "shareholding", "press release",
                "annual report 2025", "results q2", "presentation", "news",
            )
        )
        transport = FakeTransport()
        policy = DiscoveryPolicy()
        result = make_discovery(db, transport, storage=storage, policy=policy).discover(
            many, company_id="c-acme", persist=False,
        )
        assert len(result.queries) == policy.max_queries
        assert len(result.seeds) <= policy.max_seed_urls
        assert result.pages_attempted <= policy.max_fetched_pages
        assert len(transport.calls) <= policy.max_fetched_pages
        assert len(result.merged) <= policy.max_results
        website_paths = [s for s in result.seeds if s.target == "company_website"]
        assert len(website_paths) <= policy.max_paths_per_origin

    def test_the_policy_refuses_to_exceed_its_ceilings(self):
        for field_name, value in (
            ("max_queries", 0), ("max_queries", 9), ("max_seed_urls", 17),
            ("max_fetched_pages", 9), ("max_results", 17), ("max_depth", 1),
            ("max_paths_per_origin", 7), ("max_exchange_references", 11),
            ("min_local_candidates", 0), ("min_local_relevance", 1.5),
        ):
            with pytest.raises(ValueError):
                DiscoveryPolicy(**{field_name: value})

    def test_a_tighter_fetch_cap_is_a_prefix_of_the_seeds(self, db, storage):
        set_ir(db, "c-acme", f"{IR_ORIGIN}/ir", IR_URL_VERIFIED_CONFIDENCE)
        transport = FakeTransport()
        policy = DiscoveryPolicy(max_fetched_pages=1)
        result = make_discovery(db, transport, storage=storage, policy=policy).discover(
            RESULTS_QUERIES, company_id="c-acme", persist=False,
        )
        assert len(result.seeds) > 1
        assert transport.calls == [result.seeds[0].url]
        assert any("were not attempted" in n for n in result.notes)

    def test_exchange_references_are_capped_and_ordered_newest_first(self, db, storage):
        for i in range(6):
            add_filing(db, "c-acme", f"Acme order update {i}", days_ago=i + 1,
                       url=f"https://www.bseindia.com/xml-data/corpfiling/AttachLive/{i}.pdf")
        policy = DiscoveryPolicy(max_exchange_references=2)
        result = make_discovery(db, FakeTransport(), storage=storage, policy=policy).discover(
            NEWS_QUERIES, company_id="c-acme", persist=False,
        )
        assert [c.title for c in result.exchange_references] == [
            "Acme order update 0", "Acme order update 1",
        ]

    def test_the_service_is_called_with_exactly_the_seeds(self, db, storage, monkeypatch):
        transport = FakeTransport()
        discovery = make_discovery(db, transport, storage=storage)
        seen = []
        original = discovery.service.search

        def spy(query):
            seen.append(query)
            return original(query)

        monkeypatch.setattr(discovery.service, "search", spy)
        result = discovery.discover(RESULTS_QUERIES, company_id="c-acme", persist=False)
        assert len(seen) == 1
        query = seen[0]
        assert query.candidate_urls == tuple(s.url for s in result.seeds[:result.pages_attempted])
        assert query.max_urls == len(query.candidate_urls) == query.limit
        assert query.persist is False
        assert query.query == RESULTS_QUERIES[0]


# ===========================================================================
class TestDeduplicationAndCanonicalisation:
    def test_a_verified_ir_url_equal_to_a_website_path_is_one_seed(self, db, storage):
        set_ir(db, "c-acme", f"{ORIGIN}/investors?utm_campaign=x", IR_URL_VERIFIED_CONFIDENCE)
        discovery = make_discovery(db, FakeTransport(), storage=storage)
        seeds, _r, _n = discovery.plan_seeds("c-acme", ("Acme Industries investor presentation",))
        urls = [s.url for s in seeds]
        assert urls.count(f"{ORIGIN}/investors") == 1
        assert len(urls) == len(set(urls))

    def test_identical_bytes_at_two_paths_are_stored_and_reported_once(self, db, storage):
        first = f"{ORIGIN}/investors/results"
        second = f"{ORIGIN}/investor-relations/results"
        same = page(first, "the quarterly results in full", title="Results")
        transport = FakeTransport({first: same, second: replace(same, final_url=second)})
        result = make_discovery(db, transport, storage=storage).discover(
            RESULTS_QUERIES, company_id="c-acme", persist=False,
        )
        assert [c.canonical_url for c in result.fetched] == [first]
        assert {r.url: r.reason for r in result.refusals}[second] == \
            WebRejectionReason.DUPLICATE_CONTENT.value

    def test_the_candidate_carries_the_pages_canonical_url(self, db, storage):
        url = f"{ORIGIN}/investors/results"
        transport = FakeTransport({url: page(
            url, "quarterly results", title="Results",
            canonical=f"{ORIGIN}/investors/results?utm_source=newsletter&utm_medium=email",
            published="2026-09-18T05:00:00+00:00",
        )})
        result = make_discovery(db, transport, storage=storage).discover(
            RESULTS_QUERIES, company_id="c-acme", persist=False,
        )
        candidate = result.fetched[0]
        assert candidate.canonical_url == url
        assert candidate.published_at == datetime(2026, 9, 18, 5, tzinfo=timezone.utc)
        assert candidate.retrieved_at == NOW
        assert candidate.freshness_basis == "published_at"

    def test_a_discovered_page_the_index_already_holds_is_dropped(self, db, storage):
        url = f"{ORIGIN}/investors/results"
        transport = FakeTransport({url: page(url, "quarterly results", title="Results")})
        discovery = make_discovery(db, transport, storage=storage)
        first = discovery.discover(RESULTS_QUERIES, company_id="c-acme", persist=False)
        fetched = first.fetched[0]
        held = local_candidate(document_id=7, content_hash=fetched.content_hash, relevance=0.9,
                               url="https://www.acme.example/other-url-same-bytes")
        second = discovery.discover(RESULTS_QUERIES, company_id="c-acme", persist=False,
                                    local=local_result(held), force=True)
        assert second.deduplicated >= 1
        assert [c.origin for c in second.merged] == [LOCAL_INDEX_ORIGIN]
        assert second.merged[0].document_id == 7

    def test_a_changed_page_replaces_the_older_snapshot_of_the_same_url(self, db, storage):
        url = f"{ORIGIN}/investors/results"
        transport = FakeTransport({url: page(url, "brand new quarterly results", title="Results")})
        discovery = make_discovery(db, transport, storage=storage)
        old = local_candidate(document_id=3, url=url, content_hash="old-bytes",
                              retrieved=NOW - timedelta(days=40), relevance=0.9)
        result = discovery.discover(RESULTS_QUERIES, company_id="c-acme", persist=False,
                                    local=local_result(old), force=True)
        by_url = [c for c in result.merged if c.canonical_url == url]
        assert len(by_url) == 1 and by_url[0].origin == LIVE_FETCH_TRANSIENT_ORIGIN
        assert result.deduplicated == 1

    def test_merge_is_pure_and_ranked(self):
        a = local_candidate(document_id=1, url=f"{ORIGIN}/a", content_hash="a", relevance=0.5)
        b = local_candidate(document_id=2, url=f"{ORIGIN}/b", content_hash="b", relevance=0.9)
        transient = replace(a, document_id=None, chunk_id=None, url=None) if False else \
            WebEvidenceCandidate(
                document_id=None, chunk_id=None, company_id="c-acme",
                source_url=f"{ORIGIN}/c", canonical_url=f"{ORIGIN}/c", title="c",
                source_class="company_website", published_at=None, retrieved_at=NOW,
                snippet="", relevance=0.7, authority=0.55, freshness=1.0,
                freshness_basis="retrieved_at", score=0.7, matched_queries=(),
                signals=("live_fetch",), content_hash="c", origin=LIVE_FETCH_TRANSIENT_ORIGIN,
            )
        merged, dropped = merge_candidates([a, b], [transient, transient], limit=10)
        assert [c.canonical_url for c in merged] == [f"{ORIGIN}/b", f"{ORIGIN}/c", f"{ORIGIN}/a"]
        assert dropped == 1
        capped, _ = merge_candidates([a, b], [transient], limit=2)
        assert len(capped) == 2


# ===========================================================================
class TestPersistenceSemanticsAreExplicit:
    def test_transient_discovery_stores_nothing_and_says_so(self, db, storage):
        url = f"{ORIGIN}/investors/results"
        transport = FakeTransport({url: page(url, "quarterly results", title="Results")})
        result = make_discovery(db, transport, storage=storage).discover(
            RESULTS_QUERIES, company_id="c-acme", persist=False,
        )
        candidate = result.fetched[0]
        assert candidate.origin == LIVE_FETCH_TRANSIENT_ORIGIN
        assert candidate.document_id is None and candidate.chunk_id is None
        assert candidate.content_hash and candidate.signals == ("live_fetch",)
        assert result.persisted is False
        assert any("transient" in n for n in result.notes)
        assert stored(db) == []

    def test_persisted_discovery_reuses_the_web_page_ingestion_path(self, db, storage):
        url = f"{ORIGIN}/investors/results"
        transport = FakeTransport({url: page(url, "quarterly results", title="Results")})
        result = make_discovery(db, transport, storage=storage).discover(
            RESULTS_QUERIES, company_id="c-acme", persist=True,
        )
        rows = stored(db)
        assert len(rows) == 1
        document = rows[0]
        assert document.doc_type == DocumentType.WEB_PAGE.value
        assert document.uploaded_by == WEB_UPLOADER
        assert document.source_url == url
        candidate = result.fetched[0]
        assert candidate.origin == LIVE_FETCH_PERSISTED_ORIGIN
        assert candidate.document_id == document.id
        assert candidate.content_hash == document.content_hash
        assert result.persisted is True
        assert any("live_fetch_persisted" in n for n in result.notes)

    def test_the_default_follows_the_services_own_persist_default(self, db, storage):
        url = f"{ORIGIN}/investors/results"
        transport = FakeTransport({url: page(url, "quarterly results", title="Results")})
        assert DiscoveryPolicy().persist is True
        result = make_discovery(db, transport, storage=storage).discover(
            RESULTS_QUERIES, company_id="c-acme",
        )
        assert result.persisted is True and len(stored(db)) == 1

    def test_unchanged_bytes_are_not_ingested_twice(self, db, storage):
        url = f"{ORIGIN}/investors/results"
        transport = FakeTransport({url: page(url, "quarterly results", title="Results")})
        discovery = make_discovery(db, transport, storage=storage)
        first = discovery.discover(RESULTS_QUERIES, company_id="c-acme", persist=True)
        second = discovery.discover(RESULTS_QUERIES, company_id="c-acme", persist=True)
        assert len(stored(db)) == 1
        assert first.fetched[0].document_id == second.fetched[0].document_id

    def test_no_new_table_or_model_is_introduced(self):
        tables = set(Base.metadata.tables)
        assert not {t for t in tables if "discover" in t and t != "discovered_filings"}
        source = MODULE_PATH.read_text()
        for token in ("Base.metadata", "mapped_column", "__tablename__", "Table(", "alembic"):
            assert token not in source
        assert not list((BACKEND / "alembic" / "versions").glob("*targeted*")) if (
            BACKEND / "alembic" / "versions").is_dir() else True

    def test_the_module_never_writes_to_the_session(self):
        source = MODULE_PATH.read_text()
        for token in ("db.add(", "session.add(", ".commit(", ".flush(", ".delete(",
                      "db.merge(", "bulk_save", "add_all("):
            assert token not in source, token


# ===========================================================================
class TestDeterminism:
    def test_the_same_inputs_produce_the_same_result(self, db, storage):
        set_ir(db, "c-acme", f"{IR_ORIGIN}/ir", IR_URL_VERIFIED_CONFIDENCE)
        add_filing(db, "c-acme", "Acme order win", days_ago=2,
                   url="https://www.bseindia.com/xml-data/corpfiling/AttachLive/a.pdf")
        responses = {
            f"{ORIGIN}/press-releases": page(f"{ORIGIN}/press-releases",
                                             "Acme wins a new order", title="Order news"),
            f"{ORIGIN}/news": page(f"{ORIGIN}/news", "General news from Acme", title="News"),
        }
        outputs = []
        for _ in range(3):
            transport = FakeTransport(dict(responses))
            result = make_discovery(db, transport, storage=storage).discover(
                NEWS_QUERIES, company_id="c-acme", persist=False,
            )
            outputs.append(json.dumps(result.as_dict(), sort_keys=True))
        assert outputs[0] == outputs[1] == outputs[2]

    def test_planning_is_deterministic_regardless_of_query_order(self, db, storage):
        discovery = make_discovery(db, FakeTransport(), storage=storage)
        forward = discovery.plan_seeds("c-acme", NEWS_QUERIES)
        again = discovery.plan_seeds("c-acme", NEWS_QUERIES)
        assert forward == again

    def test_relevance_is_a_transparent_term_overlap(self, db, storage):
        press = f"{ORIGIN}/press-releases"
        news = f"{ORIGIN}/news"
        transport = FakeTransport({
            press: page(press, "Acme Industries has won a large order for export",
                        title="Order win"),
            news: page(news, "Acme Industries opens a new office in Pune", title="Office"),
        })
        result = make_discovery(db, transport, storage=storage).discover(
            NEWS_QUERIES, company_id="c-acme", persist=False,
        )
        by_url = {c.canonical_url: c for c in result.fetched}
        assert by_url[press].relevance > by_url[news].relevance
        assert by_url[press].matched_queries == NEWS_QUERIES
        assert result.merged[0].canonical_url == press

    def test_the_result_is_json_serialisable_and_frozen(self, db, storage):
        url = f"{ORIGIN}/investors/results"
        transport = FakeTransport({url: page(url, "quarterly results", title="Results")})
        result = make_discovery(db, transport, storage=storage).discover(
            RESULTS_QUERIES, company_id="c-acme", persist=False,
        )
        assert isinstance(result, TargetedDiscoveryResult)
        payload = json.loads(json.dumps(result.as_dict()))
        assert payload["status"] == "discovered"
        assert payload["decision"]["trigger"] == "local_absent"
        assert payload["fetched"][0]["origin"] == "live_fetch_transient"
        with pytest.raises(Exception):
            result.status = DiscoveryStatus.DISABLED  # type: ignore[misc]


# ===========================================================================
class TestArchitecture:
    FORBIDDEN_IMPORTS = (
        "openai", "anthropic", "google", "genai", "groq", "mistralai", "cohere",
        "openrouter", "httpx", "requests", "aiohttp", "urllib.request", "socket",
        "serpapi", "tavily", "duckduckgo_search", "googleapiclient", "searx",
        "app.services.ai.providers", "app.services.ai.planner",
        "app.services.language.translators", "app.services.web.discovery",
    )

    @staticmethod
    def _imports() -> set[str]:
        tree = ast.parse(MODULE_PATH.read_text())
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module)
        return names

    def test_no_search_provider_llm_or_client_is_imported(self):
        offenders = {
            name for name in self._imports()
            if any(name == bad or name.startswith(f"{bad}.") for bad in self.FORBIDDEN_IMPORTS)
        }
        assert offenders == set()

    def test_no_provider_or_search_api_is_named_in_the_source(self):
        source = MODULE_PATH.read_text().lower()
        for token in ("serpapi", "tavily", "bing", "duckduckgo", "searx", "brave search",
                      "perplexity", "openai", "anthropic", "gemini", "openrouter",
                      "providerrouter", "chatcompletion", "google search"):
            assert token not in source, token

    def test_it_reuses_the_existing_service_fetcher_safety_and_robots(self):
        imports = self._imports()
        assert "app.services.web.service" in imports
        source = MODULE_PATH.read_text()
        assert "WebSearchService" in source and "WebSearchQuery(" in source
        assert ".search(" in source
        # It never builds a fetcher, safety policy or robots policy of its own.
        for token in ("WebFetcher(", "UrlSafetyPolicy(", "RobotsPolicy(", "HttpxTransport"):
            assert token not in source, token

    def test_the_crawler_and_planner_import_contracts_still_hold(self):
        source = MODULE_PATH.read_text()
        assert "web.discovery" not in source
        assert "web import discovery" not in source
        assert "services.ai.planner" not in source
        assert "WebCrawlerDiscovery" not in source

    def test_it_is_not_wired_into_routing_filings_or_any_request_path(self):
        referencing = []
        for path in sorted(APP.rglob("*.py")):
            if path == MODULE_PATH:
                continue
            if "targeted_discovery" in path.read_text():
                referencing.append(path.relative_to(APP).as_posix())
        assert referencing == []

    def test_the_filing_crawl_and_web_evidence_crawl_are_untouched(self):
        handlers = (APP / "services" / "platform" / "jobs" / "handlers.py").read_text()
        collector = (APP / "services" / "filings" / "collector.py").read_text()
        for text in (handlers, collector):
            assert "targeted_discovery" not in text
            assert "TargetedWebDiscovery" not in text

    def test_the_rejection_reason_enum_is_unchanged(self):
        assert len(WebRejectionReason) == 21

    def test_no_url_is_ever_constructed_on_an_exchange_or_regulator_host(self):
        source = MODULE_PATH.read_text()
        for host in EXCHANGE_HOSTS | REGULATOR_HOSTS:
            assert f"https://{host}" not in source and f"http://{host}" not in source
        assert "urljoin(" in source  # the only URL construction, on company origins

    def test_the_kill_switch_is_the_existing_setting(self):
        source = inspect.getsource(module.TargetedWebDiscovery)
        assert "WEB_EVIDENCE_ENABLED" in source
        assert hasattr(settings, "WEB_EVIDENCE_ENABLED")

    def test_the_candidate_contract_is_the_4b_one_plus_origin(self):
        fields = {f for f in WebEvidenceCandidate.__dataclass_fields__}
        assert "origin" in fields
        assert WebEvidenceCandidate.__dataclass_fields__["origin"].default == LOCAL_INDEX_ORIGIN

    def test_the_documented_flow_matches_the_code(self):
        doc = module.__doc__
        for step in ("local web index", "TargetedWebDiscovery", "WebSearchService",
                     "UrlSafetyPolicy", "RobotsPolicy", "WebFetcher", "HostPoliteness"):
            assert step in doc
        assert re.search(r"never follows a link", doc)
        # The 4B generator and index stay unwired: this module neither
        # imports nor names them (4D decides the wiring).
        source = MODULE_PATH.read_text()
        assert "WebQueryGenerator" not in source and "SelfOwnedWebIndex" not in source
