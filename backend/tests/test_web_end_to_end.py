"""End to end: a page from a pinned host becomes a cited, auditable answer.

The vertical slice, exercised through the real objects rather than around
them: ``WebSearchService`` against an injected transport,
``DocumentIngestionService`` writing bytes to a temporary volume,
``DocumentWorker`` parsing and indexing them, and ``ContextBuilder`` citing
the stored row. Only three things are injected — the transport (there is no
network in a test run), the robots fetch (which rides the same transport) and
the clock (so "retrieved at" is assertable) — and none of them skips a
production step.

What the run must preserve, because each is a claim the platform makes to a
reader:

* the URL the page came from, and the canonical form the page declares;
* when the platform read it — retrieval time, never the page's own date;
* the class of host, so a marketing page cannot pass as a filing;
* the fact that it is a *fetched* page, so an uploaded-documents-only
  question cannot be answered from it.

The publisher date is metadata, not evidence: the fixture is published in July
2026 and fetched in September 2026, and both survive into the citation.
"""
from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlsplit

import pytest
from sqlalchemy import select

from app.domain.ai.sourcing import SCOPE_KINDS, SourceScope
from app.domain.ai.types import EvidenceKind, mint_web_citation_key
from app.domain.documents.types import DocumentType
from app.domain.web.types import (
    CITATION_KEY_PATTERN,
    WebContentClass,
    WebFetchPolicy,
    WebRejectionReason,
    WebSearchQuery,
    WebSourceClass,
)
from app.models.company import Company
from app.models.document import Document, DocumentJob
from app.services.ai.citation_engine import audit
from app.services.ai.context_builder import ContextBuilder
from app.services.analysis_service import AnalysisService
from app.services.documents.ingestion import DocumentIngestionService
from app.services.documents.service import DocumentService
from app.services.documents.storage import LocalFileStorage
from app.services.documents.worker import DocumentWorker
from app.services.forecast.service import ForecastService
from app.services.platform.cache import Namespace
from app.services.scoring.service import ScoringService
from app.services.valuation.service import ValuationService
from app.services.web.fetcher import (
    HostPoliteness,
    TransportResponse,
    WebFetcher,
)
from app.services.web.robots import RobotsPolicy
from app.services.web.service import WEB_UPLOADER, WebSearchService

REF = "BHARATCP"
HOST = "www.acme.example"
URL = f"https://{HOST}/investors/results"
WALL_URL = f"https://{HOST}/investors/press"
BLOCKED_URL = f"https://{HOST}/investors/blocked"

#: The clock the fetch runs on. Deliberately months after the page's own
#: publication date, so a citation that reported the page's date as the
#: retrieval date would be visible rather than plausible.
NOW = datetime(2026, 9, 18, 9, 30, tzinfo=timezone.utc)
PUBLISHED = datetime(2026, 7, 24, 5, 35, tzinfo=timezone.utc)
ADDRESS = "93.184.216.34"

#: A page shaped like a real IR results page: a menu, a cookie banner, a
#: footer and a script around the content, a canonical link carrying campaign
#: parameters, and publication metadata. Everything outside the content is
#: what must not reach the corpus.
HTML = """<!DOCTYPE html>
<html lang="en"><head>
<title>Acme Industries \u2014 Q2 FY26 results</title>
<link rel="canonical"
      href="https://www.acme.example/investors/results?utm_source=newsletter&amp;utm_medium=email">
<meta property="article:published_time" content="2026-07-24T05:35:00+00:00">
<meta name="author" content="Acme Investor Relations">
<script>window.cookieConsent = "We use cookies. Accept all cookies.";</script>
</head><body>
<nav><a href="/">Home</a> <a href="/about">About us</a> <a href="/careers">Careers</a></nav>
<div class="cookie-banner">We use cookies to improve your experience. Accept all
cookies to continue browsing this website.</div>
<main>
<h1>Q2 FY26 results</h1>
<p>Acme Industries Limited reported consolidated revenue of 1,234 crore rupees
for the quarter ended 30 June 2026, up 18.4 per cent from the same quarter a
year earlier.</p>
<p>Operating margin expanded to 21.2 per cent, and the board approved an
interim dividend of four rupees per share payable to shareholders on record as
of 5 August 2026.</p>
</main>
<footer>Copyright 2026 Acme Industries Limited. All rights reserved.
Registered office: Mumbai.</footer>
</body></html>
""".encode("utf-8")

WALL = (
    "<html><head><title>Just a moment</title></head><body><main>"
    "We use cookies to improve your experience. Accept all cookies to "
    "continue browsing this website.</main></body></html>"
).encode("utf-8")

ROBOTS_ALLOW = "User-agent: *\nDisallow: /private\n"

#: Providers the platform may legitimately call elsewhere. This path must
#: never reach one, and the assertion is on the hosts actually contacted
#: rather than on an import list, so a future refactor that quietly dials out
#: fails the test.
PROVIDER_HOSTS = frozenset({
    "api.openai.com", "api.anthropic.com", "generativelanguage.googleapis.com",
    "openrouter.ai", "api.cohere.ai", "api.jina.ai", "finnhub.io",
    "financialmodelingprep.com",
})


class FakeTransport:
    """Answers from a table; records every URL it was asked for."""

    def __init__(self, responses, *, default=None):
        self.responses = responses
        self.default = default
        self.calls: list[str] = []

    def get(self, url, *, headers, timeout, max_bytes):
        self.calls.append(url)
        answer = self.responses.get(url, self.default)
        if answer is None:
            raise AssertionError(f"unexpected transport call for {url}")
        if isinstance(answer, Exception):
            raise answer
        return answer

    @property
    def hosts(self) -> set[str]:
        return {(urlsplit(url).hostname or "").lower() for url in self.calls}


def page(body: bytes = HTML, *, url: str = URL, status: int = 200,
         content_type: str = "text/html; charset=utf-8") -> TransportResponse:
    return TransportResponse(
        status_code=status,
        headers={"content-type": content_type, "content-length": str(len(body))},
        content=body,
        final_url=url,
        elapsed_ms=18.0,
        truncated=False,
        peer_address=(ADDRESS, 443),
    )


class SpyCache:
    """The cache surface the service uses, with every write recorded."""

    def __init__(self) -> None:
        self.values: dict[tuple, object] = {}
        self.invalidated: list[Namespace] = []

    def get(self, namespace, *parts):
        return self.values.get((namespace, parts))

    def set(self, namespace, value, *parts):
        self.values[(namespace, parts)] = value

    def invalidate(self, namespace):
        self.invalidated.append(namespace)
        return 0


@pytest.fixture()
def db_session():
    """A session on the shared seeded database, purged around each test.

    The web path commits (the worker runs in another transaction), so a
    rollback cannot isolate these tests. Documents and jobs are removed
    before and after instead — the same pattern the async-ingestion suite
    uses, for the same reason.
    """
    from tests.conftest import TestingSession

    session = TestingSession()
    company = session.scalars(
        select(Company).where(Company.ticker == REF)
    ).first()
    assert company is not None, "the seeded reference company is missing"
    previous_website = company.website

    def purge() -> None:
        session.rollback()
        session.query(DocumentJob).delete()
        session.query(Document).delete()
        session.commit()

    purge()
    company.website = f"https://{HOST}"
    session.commit()
    try:
        yield session
    finally:
        purge()
        session.rollback()
        company = session.get(Company, company.id)
        if company is not None:
            company.website = previous_website
            session.commit()
        session.close()


@pytest.fixture()
def storage(tmp_path) -> LocalFileStorage:
    return LocalFileStorage(tmp_path / "documents")


@pytest.fixture()
def cache() -> SpyCache:
    return SpyCache()


def _service(db_session, storage, cache, *, responses=None, now=NOW,
             policy=None):
    """The real service over a fake transport, wired the production way.

    No fetcher and no safety policy are injected: the service derives the
    allowlist from the company's own records, which is the behaviour under
    test. Only the socket, the robots fetch and the clock are substituted.
    """
    transport = FakeTransport(
        responses if responses is not None else {URL: page()},
        default=page(b"not found", url=f"https://{HOST}/missing",
                     status=404, content_type="text/plain"),
    )

    def robots_fetch(url, *, timeout, max_bytes, user_agent):
        return 200, ROBOTS_ALLOW.encode()

    service = WebSearchService(
        db_session,
        ingestion=DocumentIngestionService(db_session, storage=storage),
        policy=policy or WebFetchPolicy(),
        robots=RobotsPolicy(fetch=robots_fetch, user_agent="EquityPilotAI/1.0"),
        transport=transport,
        resolver=lambda host, port: [ADDRESS],
        politeness=HostPoliteness(
            default_delay=0.0, sleep=lambda _seconds: None, clock=lambda: 0.0,
        ),
        cache_service=cache,
        now=lambda: now,
    )
    return service, transport


def _query(company_id: str, **overrides) -> WebSearchQuery:
    fields = {"company_id": company_id, "query": "latest quarterly results",
              "candidate_urls": (URL,), "limit": 5, "max_urls": 3,
              "persist": True}
    fields.update(overrides)
    return WebSearchQuery(**fields)


def _stored(db_session) -> list[Document]:
    return list(db_session.scalars(select(Document).order_by(Document.id)).all())


def _context(db_session) -> object:
    analysis = AnalysisService.for_ticker(db_session, REF, provision=False)
    assert analysis is not None and analysis.has_data
    builder = ContextBuilder(
        analysis,
        ForecastService(db_session),
        ValuationService(db_session),
        ScoringService(db_session),
        DocumentService(db_session),
    )
    return builder.build()


# ===========================================================================
class TestFetchedPageIsPersistedWithItsProvenance:
    def test_the_page_is_ingested_as_a_web_page_on_a_pinned_host(
        self, db_session, storage, cache,
    ):
        company = db_session.scalars(
            select(Company).where(Company.ticker == REF)
        ).first()
        service, transport = _service(db_session, storage, cache)

        result = service.search(_query(company.id))

        assert result.any_accepted, result.details
        assert len(result.documents) == 1
        reference = result.documents[0]
        assert reference.document_id is not None
        assert reference.source_class is WebSourceClass.COMPANY_WEBSITE
        assert reference.content_class is WebContentClass.HTML
        assert reference.url == URL

        document = db_session.get(Document, reference.document_id)
        assert document is not None
        assert document.doc_type == DocumentType.WEB_PAGE.value
        assert document.uploaded_by == WEB_UPLOADER
        assert document.source_url == URL
        assert document.source_class == WebSourceClass.COMPANY_WEBSITE.value
        # Contacted hosts stay inside the pin: the company's own site, and
        # nothing that answers for somebody else.
        assert transport.hosts == {HOST}

    def test_the_allowlist_for_a_search_is_derived_from_the_company(
        self, db_session, storage, cache,
    ):
        """The pinned hosts are per company, so the policy must be too.

        A service that held one safety policy from construction could only
        hold an empty allowlist, and would refuse every pinned host — the
        failure this shape exists to make impossible.
        """
        service, _ = _service(db_session, storage, cache)

        fetcher = service._fetcher_for({HOST: WebSourceClass.COMPANY_WEBSITE})

        assert fetcher.safety.host_is_pinned(HOST) is True
        assert fetcher.safety.host_is_pinned("evil.example") is False
        # With no company to read hosts from, nothing is reachable — the
        # allowlist is never widened to compensate.
        unconfigured = WebSearchService(db_session)._fetcher_for({})
        assert unconfigured.safety.host_is_pinned(HOST) is False

    def test_the_retrieval_time_is_when_the_platform_read_it(self, db_session,
                                                             storage, cache):
        company = db_session.scalars(
            select(Company).where(Company.ticker == REF)
        ).first()
        service, _ = _service(db_session, storage, cache)

        result = service.search(_query(company.id))
        document = db_session.get(Document, result.documents[0].document_id)

        assert result.documents[0].retrieved_at == NOW
        assert document.retrieved_at.replace(tzinfo=timezone.utc) == NOW
        # Two different dates, each labelled: published by the company,
        # retrieved by this platform.
        assert document.published_at.replace(tzinfo=timezone.utc) == PUBLISHED
        assert result.documents[0].published_at == PUBLISHED

    def test_the_citation_value_is_collapsed_and_capped(self, db_session,
                                                        storage, cache):
        """The value reaches a prompt, so it is collapsed and bounded.

        A page's own line breaks would otherwise become prompt structure, and
        an unbounded value would let one page crowd out the evidence block.
        """
        company = db_session.scalars(
            select(Company).where(Company.ticker == REF)
        ).first()
        service, _ = _service(
            db_session, storage, cache,
            policy=WebFetchPolicy(citation_value_chars=120),
        )

        result = service.search(_query(company.id))
        reference = result.documents[0]

        assert len(reference.preview) <= 120
        assert "\n" not in reference.preview
        assert "  " not in reference.preview
        assert "1,234 crore" in reference.preview
        # The citation carries exactly what the reference published.
        assert result.citations[0].value == reference.preview

    def test_the_canonical_form_drops_campaign_parameters(self, db_session,
                                                          storage, cache):
        company = db_session.scalars(
            select(Company).where(Company.ticker == REF)
        ).first()
        service, _ = _service(db_session, storage, cache)

        result = service.search(_query(company.id))
        reference = result.documents[0]

        assert reference.canonical_url == URL
        assert "utm_" not in reference.canonical_url
        assert reference.title == "Acme Industries \u2014 Q2 FY26 results"
        assert reference.author == "Acme Investor Relations"

    def test_what_is_stored_is_content_without_the_boilerplate(self, db_session,
                                                               storage, cache):
        company = db_session.scalars(
            select(Company).where(Company.ticker == REF)
        ).first()
        service, _ = _service(db_session, storage, cache)

        result = service.search(_query(company.id))
        document = db_session.get(Document, result.documents[0].document_id)
        cleaned = storage.read(document.storage_key)

        assert b"1,234 crore" in cleaned
        assert b"21.2 per cent" in cleaned
        # Navigation, the cookie banner and the footer are not evidence.
        assert b"About us" not in cleaned
        assert b"Accept all" not in cleaned
        assert b"All rights reserved" not in cleaned
        # The provenance travels in the row's metadata, written at accept
        # time so the row describes itself while it is still queued.
        web = document.doc_metadata["web"]
        assert web["source_url"] == URL
        assert web["retrieved_at"] == NOW.isoformat()
        assert web["content_type"].startswith("text/html")
        assert web["query"] == "latest quarterly results"


class TestTheStoredPageBecomesACitedAnswer:
    def test_the_builder_cites_it_with_its_url_and_retrieval_time(
        self, db_session, storage, cache,
    ):
        company = db_session.scalars(
            select(Company).where(Company.ticker == REF)
        ).first()
        service, _ = _service(db_session, storage, cache)
        result = service.search(_query(company.id))
        document_id = result.documents[0].document_id

        # The existing worker parses and indexes the stored bytes — the same
        # path an uploaded document takes, with no web-specific branch.
        assert DocumentWorker(lambda: db_session, storage=storage).run_once() is True
        db_session.expire_all()
        document = db_session.get(Document, document_id)
        assert document.status in {"completed", "ready"}
        assert document.doc_metadata["web"]["source_url"] == URL

        context = _context(db_session)
        web = context.by_kind(EvidenceKind.WEB)
        assert len(web) == 1, [c.key for c in context.citations]
        citation = web[0]

        assert citation.key == mint_web_citation_key(URL)
        # The key must satisfy the marker the citation audit enforces.
        assert CITATION_KEY_PATTERN.fullmatch(f"[{citation.key}]")
        assert citation.document_id == document_id
        assert citation.web is not None
        assert citation.web.url == URL
        assert citation.web.canonical_url == URL
        assert citation.web.published_at == PUBLISHED
        # The timestamp is the platform's own reading time, not the page's.
        assert citation.web.retrieved_at == NOW
        assert citation.web.content_hash == result.documents[0].content_hash
        assert "1,234 crore" in citation.value
        assert HOST in citation.source
        assert "[Company Website]" in citation.label

    def test_an_answer_that_cites_the_page_audits_as_supported(
        self, db_session, storage, cache,
    ):
        company = db_session.scalars(
            select(Company).where(Company.ticker == REF)
        ).first()
        service, _ = _service(db_session, storage, cache)
        service.search(_query(company.id))
        DocumentWorker(lambda: db_session, storage=storage).run_once()
        db_session.expire_all()

        context = _context(db_session)
        citation = context.by_kind(EvidenceKind.WEB)[0]
        answer = (
            "Acme reported consolidated revenue of 1,234 crore rupees for the "
            f"quarter ended 30 June 2026 [{citation.key}], up 18.4 per cent "
            "year on year."
        )

        result = audit(answer, context.citations)

        assert result.unknown_keys == []
        assert result.is_supported is True
        assert "1,234" in result.resolved[0].value

    def test_the_evidence_block_marks_the_web_section_as_weaker(
        self, db_session, storage, cache,
    ):
        company = db_session.scalars(
            select(Company).where(Company.ticker == REF)
        ).first()
        service, _ = _service(db_session, storage, cache)
        service.search(_query(company.id))
        DocumentWorker(lambda: db_session, storage=storage).run_once()
        db_session.expire_all()

        rendered = _context(db_session).render_evidence()

        assert "--- WEB ---" in rendered
        assert "weaker evidence than uploaded filings" in rendered

    def test_a_documents_only_question_cannot_be_answered_from_it(
        self, db_session, storage, cache,
    ):
        company = db_session.scalars(
            select(Company).where(Company.ticker == REF)
        ).first()
        service, _ = _service(db_session, storage, cache)
        result = service.search(_query(company.id))
        document_id = result.documents[0].document_id
        DocumentWorker(lambda: db_session, storage=storage).run_once()
        db_session.expire_all()

        context = _context(db_session)
        restricted = context.restricted_to(
            SCOPE_KINDS[SourceScope.UPLOADED_DOCUMENTS_ONLY]
        )

        assert restricted.by_kind(EvidenceKind.WEB) == []
        # The passage the retrieval layer would label DOCUMENT is withheld
        # too: the fetched page is identified by the document *row*.
        assert [c for c in restricted.citations
                if c.document_id == document_id] == []
        assert any("outside the requested source" in gap
                   for gap in restricted.unavailable)


class TestRefusals:
    def test_a_cookie_wall_is_refused_and_nothing_is_stored(
        self, db_session, storage, cache,
    ):
        company = db_session.scalars(
            select(Company).where(Company.ticker == REF)
        ).first()
        service, _ = _service(
            db_session, storage, cache,
            responses={WALL_URL: page(WALL, url=WALL_URL)},
        )

        result = service.search(_query(company.id, candidate_urls=(WALL_URL,)))

        assert result.documents == ()
        assert result.rejections_for(WebRejectionReason.UNSUPPORTED_CONTENT) \
            == (WALL_URL,)
        assert _stored(db_session) == []

    def test_a_403_is_reported_and_never_ingested(self, db_session, storage,
                                                  cache):
        company = db_session.scalars(
            select(Company).where(Company.ticker == REF)
        ).first()
        service, _ = _service(
            db_session, storage, cache,
            responses={BLOCKED_URL: page(b"forbidden", url=BLOCKED_URL,
                                         status=403, content_type="text/plain")},
        )

        result = service.search(
            _query(company.id, candidate_urls=(BLOCKED_URL,))
        )

        assert result.documents == ()
        assert result.rejections_for(WebRejectionReason.FORBIDDEN_BY_SERVER) \
            == (BLOCKED_URL,)
        assert _stored(db_session) == []


class TestRepeatSearches:
    def test_unchanged_bytes_are_not_ingested_twice(self, db_session, storage,
                                                    cache):
        company = db_session.scalars(
            select(Company).where(Company.ticker == REF)
        ).first()
        service, transport = _service(db_session, storage, cache)

        first = service.search(_query(company.id))
        first_id = first.documents[0].document_id
        jobs_after_first = db_session.query(DocumentJob).count()

        second = service.search(_query(company.id))

        assert second.documents[0].document_id == first_id
        assert len(_stored(db_session)) == 1
        # No second job means no second pipeline run, and therefore no
        # Namespace.RAG invalidation from a page that did not change.
        assert db_session.query(DocumentJob).count() == jobs_after_first
        # The page was still fetched — the comparison is on real bytes.
        assert transport.calls.count(URL) == 2

    def test_the_namespace_it_writes_is_its_own(self, db_session, storage,
                                                cache):
        company = db_session.scalars(
            select(Company).where(Company.ticker == REF)
        ).first()
        service, _ = _service(db_session, storage, cache)

        service.search(_query(company.id))

        written = {namespace for namespace, _ in cache.values}
        assert written == {Namespace.WEB}
        # The service invalidates nothing itself: the ingest path owns the
        # RAG invalidation, and duplicating it here would be a second source
        # of truth for when the corpus changed.
        assert cache.invalidated == []


class TestProviderIndependence:
    def test_the_whole_search_runs_with_external_providers_disabled(
        self, monkeypatch, db_session, storage, cache,
    ):
        from app.core.config import settings

        monkeypatch.setattr(
            settings, "AI_EXTERNAL_PROVIDERS_ENABLED", False, raising=False,
        )
        company = db_session.scalars(
            select(Company).where(Company.ticker == REF)
        ).first()
        service, transport = _service(db_session, storage, cache)

        result = service.search(_query(company.id))
        DocumentWorker(lambda: db_session, storage=storage).run_once()
        db_session.expire_all()
        context = _context(db_session)

        assert settings.AI_EXTERNAL_PROVIDERS_ENABLED is False
        assert result.any_accepted
        assert context.by_kind(EvidenceKind.WEB)
        # The only socket this path may open is to the pinned host.
        assert transport.hosts.isdisjoint(PROVIDER_HOSTS)
        assert transport.hosts == {HOST}
