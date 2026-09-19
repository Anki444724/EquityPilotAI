"""Part 3 Phase 3: the job-triggered bounded web-evidence crawl, end to end.

These tests drive `handle_web_evidence_crawl` — the only production caller of
`app.services.web.discovery` — through the *real* objects rather than around
them: the real `WebCrawlerDiscovery` walking seeds, the real
`WebSearchService` pinning and ingesting, the real
`DocumentIngestionService` writing rows. Only the things a test run cannot
have are substituted: the socket (a table-backed transport), the robots.txt
fetch, DNS resolution, politeness sleep, and the storage root (tmp). Every
production decision between the job and the database is real.

What the suite pins, because each is a claim the phase makes:

* the feature is off by default and off means *nothing happens*;
* seeds come only from the company record — its website and its *verified*
  IR URL — never from a caller, a model, or an unverified guess;
* the crawl stays bounded (8 pages a seed, no query URLs) even when a
  payload asks for more;
* nothing leaves the pinned host, end to end, with external providers off;
* dry-run reads everything and persists nothing;
* refusals are recorded up to fifty with the true total preserved;
* a re-run is a no-op at the database, and an UPLOADED_DOCUMENTS_ONLY answer
  remains blind to fetched pages.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.domain.ai.sourcing import SCOPE_KINDS, SourceScope
from app.domain.ai.types import EvidenceKind
from app.domain.documents.types import DocumentType
from app.domain.platform.jobs import JobKind
from app.models.company import Company
from app.models.document import Document, DocumentJob
from app.models.filing_collection import CompanyCrawlState
from app.services.platform.jobs.handlers import (
    handle_web_evidence_crawl,
    handler_for,
)
from app.services.web.fetcher import HostPoliteness, TransportResponse
from app.services.web.service import WEB_UPLOADER

REF = "BHARATCP"
HOST = "www.acme.example"
IR_HOST = "ir.acme.example"
SEED_URL = f"https://{HOST}/"
IR_URL = f"https://{IR_HOST}/investors"
ADDRESS = "93.184.216.34"
ROBOTS_ALLOW = b"User-agent: *\nDisallow: /private\n"
#: The frozen retrieval clock — see `sockets`. "Retrieved at" is this, for
#: every fetch in every test, so two runs of the same world are byte-identical.
NOW = datetime(2026, 9, 19, 9, 30, tzinfo=timezone.utc)

#: Providers the platform may legitimately call elsewhere. This path must
#: never reach one, and the assertion is on hosts actually contacted.
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


def page(url: str, text: str, links: tuple[str, ...] = (),
         *, status: int = 200,
         content_type: str = "text/html; charset=utf-8") -> TransportResponse:
    """A fetchable page over the 200-character quality floor, with anchors."""
    anchors = "".join(f'<a href="{h}">{h}</a>' for h in links)
    body_text = text.ljust(240, "z")
    body = (
        "<html><head><title>p</title></head><body><main>"
        f"<p>{body_text}</p>{anchors}</main></body></html>"
    ).encode()
    return TransportResponse(
        status_code=status,
        headers={"content-type": content_type, "content-length": str(len(body))},
        content=body, final_url=url,
        elapsed_ms=1.0, truncated=False, peer_address=(ADDRESS, 443),
    )


NOT_FOUND = TransportResponse(
    status_code=404,
    headers={"content-type": "text/plain"},
    content=b"not found", final_url="https://invalid.invalid/missing",
    elapsed_ms=1.0, truncated=False, peer_address=(ADDRESS, 443),
)


@pytest.fixture()
def db_session():
    """The shared seeded database, purged around each test.

    The web path commits (the worker runs in another transaction), so a
    rollback cannot isolate these tests; documents, jobs and crawl-state rows
    are removed before and after instead — the pattern the Phase-1 web suite
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
        session.query(CompanyCrawlState).filter(
            CompanyCrawlState.company_id == company.id
        ).delete()
        session.commit()

    purge()
    company.website = f"https://{HOST}"
    session.commit()
    try:
        yield session
    finally:
        purge()
        session.rollback()
        row = session.get(Company, company.id)
        if row is not None:
            row.website = previous_website
            session.commit()
        session.close()


@pytest.fixture()
def sockets(monkeypatch, tmp_path):
    """Every external seam the job's service chain can reach, substituted.

    The handler is not taught about any of this: the patches sit on the web
    package's own module-level seams, so what runs is production code — one
    fetcher order of operations, one pinning code path — over fake sockets.
    """
    import app.services.web.fetcher as fetcher_module
    import app.services.web.robots as robots_module
    import app.services.web.safety as safety_module
    import app.services.web.service as service_module

    responses: dict[str, object] = {}
    transport = FakeTransport(responses, default=NOT_FOUND)

    monkeypatch.setattr(
        fetcher_module, "HttpxTransport", lambda *a, **kw: transport
    )
    monkeypatch.setattr(
        robots_module, "_default_fetch",
        lambda url, *, timeout, max_bytes, user_agent: (200, ROBOTS_ALLOW),
    )
    monkeypatch.setattr(
        safety_module, "default_resolver", lambda host, port: [ADDRESS]
    )
    monkeypatch.setattr(
        service_module, "HostPoliteness",
        lambda **kw: HostPoliteness(
            default_delay=0.0, sleep=lambda s: None, clock=lambda: 0.0
        ),
    )
    # The retrieval clock. It is an injectable collaborator in production
    # (WebSearchService takes `now=`; the Phase-1 suite pins it at the
    # constructor). These tests go through the job, which constructs the
    # service itself — so the clock is pinned at the module seam instead,
    # which is exactly what the constructor injects. A frozen "retrieved at"
    # is also what makes a rerun byte-identical, so idempotency is provable.
    monkeypatch.setattr(
        service_module, "datetime",
        SimpleNamespace(now=lambda tz=None: NOW),
    )

    from app.services.documents.ingestion import DocumentIngestionService
    from app.services.documents.storage import LocalFileStorage

    storage = LocalFileStorage(tmp_path / "documents")
    monkeypatch.setattr(
        service_module, "DocumentIngestionService",
        lambda db: DocumentIngestionService(db, storage=storage),
    )
    monkeypatch.setattr(settings, "WEB_EVIDENCE_ENABLED", True)
    return SimpleNamespace(
        transport=transport, responses=responses, storage=storage
    )


def _company(db_session):
    return db_session.scalars(select(Company).where(Company.ticker == REF)).first()


def _stored_documents(db_session) -> list[Document]:
    return list(db_session.scalars(select(Document).order_by(Document.id)))


def _set_ir(db_session, company, ir_url, confidence) -> None:
    db_session.add(CompanyCrawlState(
        company_id=company.id, ir_url=ir_url, ir_url_confidence=confidence,
    ))
    db_session.commit()


# ===========================================================================
class TestTheJobEndToEnd:
    def test_the_full_run_ingests_web_pages_without_any_provider(
        self, monkeypatch, db_session, sockets,
    ):
        """The vertical slice, through the job: crawl → search → WEB_PAGE
        rows — with external providers disabled and every contacted host on
        the company's own pin."""
        monkeypatch.setattr(
            settings, "AI_EXTERNAL_PROVIDERS_ENABLED", False, raising=False,
        )
        linked = f"https://{HOST}/investors/results"
        sockets.responses[SEED_URL] = page(
            SEED_URL,
            "Acme Industries investor hub: results, presentations and "
            "filings for shareholders and analysts for the fiscal year.",
            ("/investors/results", "https://offsite.example/x"),
        )
        sockets.responses[linked] = page(
            linked,
            "Acme Industries reported consolidated revenue of 1,234 crore "
            "for the quarter ended 30 June 2026, up 18.4 per cent on the "
            "prior year with a declared interim dividend.",
        )

        company = _company(db_session)
        result = handle_web_evidence_crawl(
            db_session, {"company_ids": [company.id], "scheduled": True},
        )

        # Handler registration and payload shape.
        assert handler_for(JobKind.WEB_EVIDENCE_CRAWL) is handle_web_evidence_crawl
        for key in ("companies", "results", "dry_run", "max_pages"):
            assert key in result
        assert result["companies"] == 1
        assert result["dry_run"] is False
        assert result["max_pages"] == 8

        entry = result["results"][0]
        for key in (
            "company_id", "ticker", "seeds", "pages_fetched",
            "pages_accepted", "urls_submitted", "documents", "document_ids",
            "truncated", "refusals_total", "refusals", "refusals_recorded",
            "dry_run",
        ):
            assert key in entry, key
        assert entry["seeds"] == [f"https://{HOST}"]
        assert entry["pages_accepted"] == 2
        assert entry["urls_submitted"] == 2
        assert entry["documents"] == 2
        assert len(entry["document_ids"]) == 2

        # The offsite link was refused as a finding, never fetched.
        refused = {r["url"]: r["reason"] for r in entry["refusals"]}
        assert refused["https://offsite.example/x"] == "offsite_link"
        assert not any("offsite.example" in call for call in sockets.transport.calls)

        # Two WEB_PAGE documents, with provenance, through the existing path.
        documents = _stored_documents(db_session)
        assert len(documents) == 2
        for document in documents:
            assert document.doc_type == DocumentType.WEB_PAGE.value
            assert document.uploaded_by == WEB_UPLOADER
            assert document.source_class == "company_website"
            assert document.doc_metadata["web"]["source_url"].startswith(
                f"https://{HOST}"
            )

        # Pinned-host safety and provider independence, asserted on the wire.
        assert sockets.transport.hosts == {HOST}
        assert sockets.transport.hosts.isdisjoint(PROVIDER_HOSTS)

    def test_seeds_preserve_website_then_ir_order(
        self, db_session, sockets,
    ):
        """Both origins seed when both exist: website first, verified IR
        second, duplicates folded — the report shows the real entry points."""
        sockets.responses[SEED_URL] = page(
            SEED_URL, "Acme Industries home page for investors and analysts "
            "covering the company's operations, segments and governance.",
        )
        sockets.responses[IR_URL] = page(
            IR_URL, "Acme Industries investor relations: quarterly results, "
            "annual reports and exchange filings for shareholders.",
        )
        company = _company(db_session)
        _set_ir(db_session, company, IR_URL, 0.95)

        result = handle_web_evidence_crawl(db_session, {"company_ids": [company.id]})

        entry = result["results"][0]
        assert entry["seeds"] == [f"https://{HOST}", IR_URL]
        assert entry["documents"] == 2
        by_url = {d.source_url: d for d in _stored_documents(db_session)}
        assert by_url[SEED_URL].source_class == "company_website"
        assert by_url[IR_URL].source_class == "verified_ir"


class TestTheKillSwitch:
    def test_disabled_returns_before_any_work(
        self, monkeypatch, db_session, sockets,
    ):
        """WEB_EVIDENCE_ENABLED=false: real database, armed sockets — and the
        job still does nothing at all. No fetch, no row, no service build."""
        assert settings.WEB_EVIDENCE_ENABLED is True  # fixture opened the gate
        monkeypatch.setattr(settings, "WEB_EVIDENCE_ENABLED", False)

        company = _company(db_session)
        result = handle_web_evidence_crawl(
            db_session, {"company_ids": [company.id]},
        )

        assert result == {
            "skipped": True, "reason": "web evidence crawl is disabled",
        }
        assert sockets.transport.calls == []
        assert _stored_documents(db_session) == []

    def test_the_default_configuration_is_disabled(self, db_session):
        """The flag's out-of-the-box value: a fresh Settings object (the
        shape .env ships) has the crawl off."""
        from app.core.config import Settings

        assert Settings.model_fields["WEB_EVIDENCE_ENABLED"].default is False
        # The handler is registered, though: the kind exists in every
        # registry, so an enqueue never dies on a missing handler.
        from app.domain.platform.jobs import (
            DEFAULT_PRIORITY, JOB_LABELS, RETRY_POLICIES,
        )
        kind = JobKind.WEB_EVIDENCE_CRAWL
        assert kind in JOB_LABELS and kind in DEFAULT_PRIORITY
        assert kind in RETRY_POLICIES
        assert handler_for(kind) is handle_web_evidence_crawl


class TestDryRun:
    def test_dry_run_reads_everything_and_persists_nothing(
        self, db_session, sockets,
    ):
        linked = f"https://{HOST}/investors/results"
        sockets.responses[SEED_URL] = page(
            SEED_URL, "Acme Industries investor hub with results decks and "
            "annual reports for shareholders and research analysts.",
            ("/investors/results",),
        )
        sockets.responses[linked] = page(
            linked, "Acme Industries Q2 FY26 results presentation covering "
            "revenue growth, margin expansion and the dividend declaration.",
        )
        company = _company(db_session)

        result = handle_web_evidence_crawl(
            db_session, {"company_ids": [company.id], "dry_run": True},
        )

        entry = result["results"][0]
        assert result["dry_run"] is True
        assert entry["dry_run"] is True
        # The crawl and the search really ran — the operator gets the same
        # counts a wet run would report.
        assert entry["pages_accepted"] == 2
        assert entry["urls_submitted"] == 2
        assert entry["documents"] == 2
        # …and persisted zero rows, jobs, and references.
        assert entry["document_ids"] == []
        assert _stored_documents(db_session) == []
        assert db_session.query(DocumentJob).count() == 0


class TestBudgetAndRefusals:
    def test_the_seed_budget_is_capped_and_query_urls_are_refused(
        self, db_session, sockets,
    ):
        """A payload asking for 99 pages gets the audited 8: seven pages
        accepted through the wall, the rest queued behind `truncated`, and a
        query-URL link refused without a fetch."""
        links = tuple(f"/p{i}" for i in range(1, 21))
        sockets.responses[SEED_URL] = page(
            SEED_URL, "Acme Industries investor hub indexing every result "
            "deck the company has published for analysts.",
            links + ("/search?q=revenue",),
        )
        for i in range(1, 21):
            url = f"https://{HOST}/p{i}"
            sockets.responses[url] = page(
                url, f"Acme Industries published results deck number {i} of "
                "the fiscal year with segment detail and guidance.",
            )
        company = _company(db_session)

        result = handle_web_evidence_crawl(
            db_session, {"company_ids": [company.id], "max_pages": 99},
        )

        assert result["max_pages"] == 8  # clamped, never the payload's 99
        entry = result["results"][0]
        # 8 fetch attempts total: the sitemap probe, the seed, six links —
        # then the queue hit the wall.
        assert entry["pages_fetched"] == 8
        assert entry["truncated"] is True
        assert entry["pages_accepted"] == 7
        assert entry["documents"] == 7
        reasons = {r["url"]: r["reason"] for r in entry["refusals"]}
        assert reasons[f"https://{HOST}/search?q=revenue"] \
            == "query_budget_exceeded"
        assert not any("q=revenue" in call for call in sockets.transport.calls)
        # The never-reached links were never fetched, by either phase.
        for i in range(7, 21):
            assert f"https://{HOST}/p{i}" not in sockets.transport.calls

    def test_refusal_recording_is_capped_at_50_with_totals_preserved(
        self, db_session, sockets,
    ):
        """A hostile sitemap names 55 offsite pages: all 55 refusals are
        counted, fifty are recorded, and not one offsite host is touched."""
        locs = "".join(
            f"<url><loc>https://evil-{i}.example/x</loc></url>"
            for i in range(55)
        )
        sitemap = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            f"{locs}</urlset>"
        ).encode()
        sockets.responses[SEED_URL] = page(
            SEED_URL, "Acme Industries investor hub for shareholders and "
            "analysts tracking the company's disclosures this year.",
        )
        sockets.responses[f"https://{HOST}/sitemap.xml"] = TransportResponse(
            status_code=200,
            headers={"content-type": "text/plain; charset=utf-8"},
            content=sitemap, final_url=f"https://{HOST}/sitemap.xml",
            elapsed_ms=1.0, truncated=False, peer_address=(ADDRESS, 443),
        )
        company = _company(db_session)

        result = handle_web_evidence_crawl(
            db_session, {"company_ids": [company.id]},
        )

        entry = result["results"][0]
        assert entry["refusals_total"] == 55
        assert entry["refusals_recorded"] == 50
        assert len(entry["refusals"]) == 50
        assert {r["reason"] for r in entry["refusals"]} == {"offsite_link"}
        # The crawl itself was unaffected: the seed still became a document.
        assert entry["documents"] == 1
        assert sockets.transport.hosts == {HOST}


class TestSeeding:
    def test_a_verified_ir_url_seeds_and_is_cited_as_verified(
        self, db_session, sockets,
    ):
        sockets.responses[IR_URL] = page(
            IR_URL, "Acme Industries investor relations: quarterly results "
            "presentations and annual reports lodged with the exchange.",
        )
        company = _company(db_session)
        company.website = None
        db_session.commit()
        _set_ir(db_session, company, IR_URL, 0.95)

        result = handle_web_evidence_crawl(
            db_session, {"company_ids": [company.id]},
        )

        entry = result["results"][0]
        assert entry["seeds"] == [IR_URL]
        assert entry["documents"] == 1
        (document,) = _stored_documents(db_session)
        assert document.source_class == "verified_ir"
        assert document.doc_metadata["web"]["source_class"] == "verified_ir"
        assert sockets.transport.hosts == {IR_HOST}

    def test_an_unverified_ir_url_is_never_seeded(
        self, db_session, sockets,
    ):
        """Discovery's guess at 0.50 confidence steers nothing: with no
        website the company is skipped and not one socket is opened."""
        company = _company(db_session)
        company.website = None
        db_session.commit()
        _set_ir(db_session, company, IR_URL, 0.50)

        result = handle_web_evidence_crawl(
            db_session, {"company_ids": [company.id]},
        )

        entry = result["results"][0]
        assert entry["skipped"] is True
        assert "no website or verified IR URL" in entry["reason"]
        assert sockets.transport.calls == []
        assert _stored_documents(db_session) == []


class TestReruns:
    def test_a_rerun_ingests_nothing_twice(self, db_session, sockets):
        """Same world in, same rows out — the Phase-1 rerun contract, held.

        With the retrieval clock frozen (the constructor seam the Phase-1
        suite pins the same way), an unchanged page hashes identically on
        the rerun, so the existing ingestion path returns the very document
        it made the first time: no new row, no new document job, and the
        page is still fetched — idempotency is the content hash, never a
        skipped look.
        """
        sockets.responses[SEED_URL] = page(
            SEED_URL, "Acme Industries investor hub: results presentations "
            "and annual reports for the current fiscal year.",
        )
        company = _company(db_session)
        payload = {"company_ids": [company.id]}

        first = handle_web_evidence_crawl(db_session, payload)
        jobs_after_first = db_session.query(DocumentJob).count()

        second = handle_web_evidence_crawl(db_session, payload)

        assert first["results"][0]["document_ids"] \
            == second["results"][0]["document_ids"]
        assert len(_stored_documents(db_session)) == 1
        assert db_session.query(DocumentJob).count() == jobs_after_first
        # The pages were still fetched — idempotency is the content hash,
        # not a skipped fetch, so a changed page would be picked up.
        assert sockets.transport.calls.count(SEED_URL) >= 2


class TestScopes:
    def _context(self, db_session):
        from app.services.ai.context_builder import ContextBuilder
        from app.services.analysis_service import AnalysisService
        from app.services.documents.service import DocumentService
        from app.services.forecast.service import ForecastService
        from app.services.scoring.service import ScoringService
        from app.services.valuation.service import ValuationService

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

    def test_uploaded_documents_only_never_sees_a_crawled_page(
        self, monkeypatch, db_session, sockets,
    ):
        """A page the job ingested is WEB evidence: present to an ordinary
        question, withheld entirely from an uploaded-documents-only one —
        the property Phase 1 pinned, preserved through the job path."""
        monkeypatch.setattr(
            settings, "AI_EXTERNAL_PROVIDERS_ENABLED", False, raising=False,
        )
        sockets.responses[SEED_URL] = page(
            SEED_URL, "Acme Industries reported consolidated revenue of "
            "1,234 crore for the quarter ended 30 June 2026 with an interim "
            "dividend declared for shareholders of record.",
        )
        company = _company(db_session)

        result = handle_web_evidence_crawl(
            db_session, {"company_ids": [company.id]},
        )
        (document_id,) = result["results"][0]["document_ids"]

        # The existing worker parses and indexes the stored bytes — no
        # web-specific branch anywhere in it.
        from app.services.documents.worker import DocumentWorker

        assert DocumentWorker(
            lambda: db_session, storage=sockets.storage
        ).run_once() is True
        db_session.expire_all()

        context = self._context(db_session)
        web = context.by_kind(EvidenceKind.WEB)
        assert len(web) == 1
        assert web[0].document_id == document_id

        restricted = context.restricted_to(
            SCOPE_KINDS[SourceScope.UPLOADED_DOCUMENTS_ONLY]
        )
        assert restricted.by_kind(EvidenceKind.WEB) == []
        assert [
            c for c in restricted.citations if c.document_id == document_id
        ] == []
        assert any(
            "outside the requested source" in gap
            for gap in restricted.unavailable
        )
        assert sockets.transport.hosts.isdisjoint(PROVIDER_HOSTS)
