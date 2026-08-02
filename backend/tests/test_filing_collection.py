"""Automated Indian filing collection.

The failure modes worth guarding here are quiet ones: a crawler that
re-downloads the same annual report every night, a classifier that files a
board-meeting notice as an annual report so it gets quoted as one, a
downloader that ingests an HTML login page as a "filing", and a notifier that
alerts on noise until users stop reading it.

Network access is stubbed throughout. The live providers are exercised
separately in the verification harness; these tests must pass on a machine
with no internet and must not depend on NSE's mood.
"""
from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta, timezone

import pytest

from app.data.filings.base import (
    Filing, FilingResult, FilingType, SourceCategory,
)
from app.domain.documents.types import DocumentType
from app.domain.filings.collection import (
    MAX_DOWNLOAD_ATTEMPTS, CollectionStatus, CollectionTier, classify,
    due_for_crawl, fiscal_year_for, is_noise, quarter_for, should_retry,
)


# ===========================================================================
class TestClassification:
    """Titles are free text written by humans. Guessing wrongly is expensive."""

    @pytest.mark.parametrize("title,expected", [
        ("Integrated Annual Report 2024-25", DocumentType.ANNUAL_REPORT),
        ("Annual Report FY2026", DocumentType.ANNUAL_REPORT),
        ("Earnings Call Transcript Q2 FY26", DocumentType.CONFERENCE_CALL),
        ("Investor Presentation - Q1 FY2026", DocumentType.INVESTOR_PRESENTATION),
        ("Unaudited Financial Results for the quarter", DocumentType.QUARTERLY_REPORT),
        ("Credit Rating Update by CRISIL", DocumentType.CREDIT_RATING),
        ("Business Responsibility and Sustainability Report",
         DocumentType.ESG_REPORT),
        ("Outcome of Board Meeting", DocumentType.EXCHANGE_FILING),
        ("Intimation of Dividend", DocumentType.EXCHANGE_FILING),
        ("Bonus Issue of Equity Shares", DocumentType.EXCHANGE_FILING),
    ])
    def test_known_titles_are_classified(self, title, expected):
        assert classify(title).doc_type is expected

    def test_an_annual_report_beats_the_generic_report_rule(self):
        """Rule order matters; a specific pattern must win."""
        assert classify("Annual Report").doc_type is DocumentType.ANNUAL_REPORT

    def test_a_transcript_beats_a_conference_call_intimation(self):
        transcript = classify("Conference Call Transcript")
        assert transcript.doc_type is DocumentType.CONFERENCE_CALL
        assert transcript.confidence >= 0.9

    def test_an_unrecognised_title_is_not_forced_into_a_category(self):
        """A misfiled annual report gets quoted as an annual report.

        Falling through to OTHER at low confidence is the safe outcome.
        """
        result = classify("Zqxj notification 44/2026")
        assert result.doc_type is DocumentType.OTHER
        assert not result.is_confident

    def test_the_url_contributes_when_the_title_is_empty(self):
        result = classify("", url="https://x.com/files/annual-report-2025.pdf")
        assert result.doc_type is DocumentType.ANNUAL_REPORT


class TestNoiseFilter:
    """Exchanges emit hundreds of procedural notices."""

    @pytest.mark.parametrize("title", [
        "Closure of Trading Window",
        "Loss of Share Certificate",
        "Newspaper Publication of Financial Results",
        "Transfer of unclaimed dividend to IEPF",
    ])
    def test_procedural_notices_are_filtered(self, title):
        assert is_noise(title)

    @pytest.mark.parametrize("title", [
        "Integrated Annual Report 2024-25",
        "Financial Results for Q2",
        "Investor Presentation",
        "Acquisition of subsidiary",
    ])
    def test_substantive_documents_are_kept(self, title):
        """A false exclusion silently loses a filing, which is the worse error."""
        assert not is_noise(title)


class TestPeriodInference:
    """Indian fiscal years run April to March."""

    def test_an_explicit_range_in_the_title_wins(self):
        assert fiscal_year_for(date(2026, 8, 1), "Annual Report 2024-25") == 2025

    def test_an_explicit_fy_in_the_title_wins(self):
        assert fiscal_year_for(date(2026, 8, 1), "Results FY2026") == 2026

    def test_april_starts_the_next_fiscal_year(self):
        assert fiscal_year_for(date(2026, 4, 15), "Results") == 2027

    def test_march_still_belongs_to_the_current_fiscal_year(self):
        assert fiscal_year_for(date(2026, 3, 20), "Results") == 2026

    def test_an_absent_date_yields_no_year_rather_than_a_guess(self):
        assert fiscal_year_for(None, "Some announcement") is None

    def test_a_named_quarter_wins_over_the_date(self):
        assert quarter_for(date(2026, 8, 1), "Results for Q4") == "Q4"

    def test_the_quarter_follows_the_april_convention(self):
        assert quarter_for(date(2026, 5, 1), "Results") == "Q1"
        assert quarter_for(date(2026, 11, 1), "Results") == "Q3"


class TestCrawlScheduling:
    """Tiering keeps the nightly run bounded."""

    def test_a_company_never_crawled_is_due(self):
        assert due_for_crawl(CollectionTier.DAILY, None)

    def test_a_daily_company_is_due_after_a_day(self):
        stale = datetime.now(timezone.utc) - timedelta(hours=25)
        assert due_for_crawl(CollectionTier.DAILY, stale)

    def test_a_daily_company_is_not_due_after_an_hour(self):
        recent = datetime.now(timezone.utc) - timedelta(hours=1)
        assert not due_for_crawl(CollectionTier.DAILY, recent)

    def test_a_weekly_company_is_not_due_after_a_day(self):
        yesterday = datetime.now(timezone.utc) - timedelta(hours=25)
        assert not due_for_crawl(CollectionTier.WEEKLY, yesterday)

    def test_a_paused_company_is_never_due(self):
        assert not due_for_crawl(CollectionTier.PAUSED, None)

    def test_a_naive_timestamp_is_treated_as_utc(self):
        """SQLite returns naive datetimes; comparing them raises otherwise."""
        naive = datetime.utcnow() - timedelta(days=2)
        assert due_for_crawl(CollectionTier.DAILY, naive)

    def test_retry_is_bounded(self):
        assert should_retry(0, CollectionStatus.FAILED)
        assert not should_retry(MAX_DOWNLOAD_ATTEMPTS, CollectionStatus.FAILED)

    def test_only_failures_are_retried(self):
        assert not should_retry(0, CollectionStatus.COMPLETED)
        assert not should_retry(0, CollectionStatus.DUPLICATE)


# ===========================================================================
class TestDownloaderGuards:
    """Downloading URLs found on third-party pages is the least trusted path."""

    def test_a_non_web_scheme_is_refused(self):
        from app.services.filings.downloader import DownloadError, FilingDownloader

        with pytest.raises(DownloadError) as exc:
            FilingDownloader().fetch("file:///etc/passwd")
        assert not exc.value.retryable

    def test_an_empty_url_is_refused(self):
        from app.services.filings.downloader import DownloadError, FilingDownloader

        with pytest.raises(DownloadError):
            FilingDownloader().fetch("")

    def test_nse_requests_carry_a_referer(self):
        """Verified against the live archive: without it, NSE answers 403."""
        from app.services.filings.downloader import _headers_for

        headers = _headers_for("https://nsearchives.nseindia.com/corporate/x.pdf")
        assert "nseindia.com" in headers["Referer"]

    def test_gzip_is_never_advertised(self):
        """urllib does not transparently decompress, so the body fails to
        decode. This cost a session's debugging on SEC EDGAR."""
        from app.services.filings.downloader import _headers_for

        assert _headers_for("https://example.com/x.pdf")["Accept-Encoding"] == "identity"


# ===========================================================================
class _StubProvider:
    """An exchange that returns whatever the test hands it."""

    def __init__(self, name: str, filings: list[Filing] | None = None,
                 error: str | None = None, explode: bool = False) -> None:
        self.name = name
        self._filings = filings or []
        self._error = error
        self._explode = explode
        self.calls = 0

    def available(self) -> bool:
        return True

    def fetch(self, ticker: str, limit: int = 25, **kwargs):
        self.calls += 1
        if self._explode:
            raise RuntimeError("provider exploded")
        return FilingResult(
            filings=list(self._filings), source=self.name,
            category=SourceCategory.NSE_FILING, error=self._error,
        )


class _StubDownloader:
    """Returns deterministic PDF bytes without a network."""

    def __init__(self, payloads: dict[str, bytes] | None = None) -> None:
        self.payloads = payloads or {}
        self.fetched: list[str] = []

    def fetch(self, url: str):
        from app.services.filings.downloader import DownloadedFile

        self.fetched.append(url)
        content = self.payloads.get(url, b"%PDF-1.4\ndefault\n%%EOF")
        return DownloadedFile(
            content=content, sha256=hashlib.sha256(content).hexdigest(),
            size=len(content), content_type="application/pdf",
            final_url=url, latency_ms=1.0,
        )


def _make_collector(db, *, nse=None, downloader=None, bse=None, ir=None):
    """A collector with disk headroom assumed.

    `_has_headroom` inspects the real document volume, which does not exist in
    a test environment, so it correctly returns False and would suppress every
    download. The storage tests override it the other way to prove the guard.
    """
    from app.services.filings.collector import FilingCollector

    collector = FilingCollector(
        db, downloader=downloader or _StubDownloader(),
        nse=nse or _StubProvider("NSE Corporate Filings"),
        bse=bse or _StubProvider("BSE Corporate Announcements",
                                 error="No Record Found!"),
        ir=ir or _StubProvider("Investor Relations", error="no url"),
        polite_delay=0,
    )
    collector._has_headroom = lambda: True  # noqa: SLF001
    return collector


def _filing(title: str, reference: str, url: str) -> Filing:
    return Filing(
        category=SourceCategory.NSE_FILING,
        filing_type=FilingType.CORPORATE_ANNOUNCEMENT,
        title=title, reference=reference, url=url, filed_on=date(2026, 5, 1),
    )


@pytest.fixture()
def collector_env():
    """A collector wired to stubs, with a real database session.

    The session is shared across the suite, so the fixture creates a company
    with a unique ticker and removes it and its collection rows afterwards.
    An earlier version reused one ticker and the second test in the class hit
    the (ticker, exchange) unique constraint — the fixture was wrong, not the
    model.
    """
    import uuid

    from tests.conftest import TestingSession

    from app.models.company import Company
    from app.models.filing_collection import CompanyCrawlState, DiscoveredFiling

    db = TestingSession()
    suffix = uuid.uuid4().hex[:8].upper()
    company = Company(
        id=f"c-collect-{suffix}", name="Test Industries Ltd",
        ticker=f"TST{suffix}"[:12], exchange="NSE",
    )
    db.add(company)
    db.commit()
    try:
        yield db, company
    finally:
        db.query(DiscoveredFiling).filter_by(company_id=company.id).delete()
        db.query(CompanyCrawlState).filter_by(company_id=company.id).delete()
        db.query(Company).filter_by(id=company.id).delete()
        db.commit()
        db.close()


class TestCollectorDedup:
    """The property that makes a nightly crawl affordable."""

    def _collector(self, db, nse, downloader):
        return _make_collector(db, nse=nse, downloader=downloader)

    def test_a_second_pass_downloads_nothing(self, collector_env):
        """The single most important behaviour: an unchanged company must
        cost one indexed lookup per announcement, not a re-download."""
        db, company = collector_env
        nse = _StubProvider("NSE Corporate Filings", [
            _filing("Annual Report 2025-26", "REF-1", "http://x/ar.pdf"),
        ])
        downloader = _StubDownloader()
        collector = self._collector(db, nse, downloader)

        first = collector.crawl_company(company, download=True)
        assert first.new_documents == 1
        assert len(downloader.fetched) == 1

        second = collector.crawl_company(company, download=True)
        assert second.new_documents == 0
        assert len(downloader.fetched) == 1, "re-downloaded an unchanged filing"

    def test_the_same_pdf_at_two_urls_is_stored_once(self, collector_env):
        """NSE and BSE publish identical PDFs at different URLs.

        URL-level dedup alone stores every annual report twice and then
        retrieves it twice in every RAG answer.
        """
        db, company = collector_env
        shared = b"%PDF-1.4\nidentical bytes\n%%EOF"
        nse = _StubProvider("NSE Corporate Filings", [
            _filing("Annual Report", "NSE-1", "http://nse/ar.pdf"),
            _filing("Annual Report", "NSE-2", "http://bse/ar.pdf"),
        ])
        downloader = _StubDownloader({
            "http://nse/ar.pdf": shared, "http://bse/ar.pdf": shared,
        })
        result = self._collector(db, nse, downloader).crawl_company(
            company, download=True,
        )
        assert result.ingested == 1
        assert result.duplicates == 1

    def test_noise_is_recorded_as_skipped_not_dropped(self, collector_env):
        """So the dashboard can show what the filter removed."""
        db, company = collector_env
        nse = _StubProvider("NSE Corporate Filings", [
            _filing("Closure of Trading Window", "REF-N", "http://x/n.pdf"),
        ])
        downloader = _StubDownloader()
        result = self._collector(db, nse, downloader).crawl_company(
            company, download=True,
        )
        assert result.skipped == 1
        assert not downloader.fetched


class TestCollectorResilience:
    """One broken thing must not stop the rest."""

    def test_a_failing_source_does_not_stop_the_others(self, collector_env):
        from app.services.filings.collector import FilingCollector

        db, company = collector_env
        good = _StubProvider("NSE Corporate Filings", [
            _filing("Annual Report", "R1", "http://x/a.pdf"),
        ])
        collector = _make_collector(
            db, nse=good,
            bse=_StubProvider("BSE Corporate Announcements", explode=True),
            ir=_StubProvider("Investor Relations", explode=True),
        )
        result = collector.crawl_company(company, download=True)
        assert result.ingested == 1
        assert result.succeeded

    def test_repeated_total_failure_pauses_a_company(self, collector_env):
        """A company whose sources are permanently broken must not consume
        the nightly budget forever."""
        from app.services.filings.collector import FilingCollector
        from app.models.filing_collection import CompanyCrawlState

        db, company = collector_env
        collector = FilingCollector(
            db, downloader=_StubDownloader(),
            nse=_StubProvider("NSE Corporate Filings", explode=True),
            bse=_StubProvider("BSE Corporate Announcements", explode=True),
            ir=_StubProvider("Investor Relations", explode=True),
            polite_delay=0,
        )
        for _ in range(10):
            collector.crawl_company(company, download=False)

        state = db.query(CompanyCrawlState).filter_by(
            company_id=company.id
        ).one()
        assert state.tier == CollectionTier.PAUSED.value
        assert state.consecutive_failures >= 10

    def test_success_resets_the_failure_count(self, collector_env):
        from app.services.filings.collector import FilingCollector
        from app.models.filing_collection import CompanyCrawlState

        db, company = collector_env
        broken = FilingCollector(
            db, downloader=_StubDownloader(),
            nse=_StubProvider("NSE Corporate Filings", explode=True),
            bse=_StubProvider("BSE Corporate Announcements", explode=True),
            ir=_StubProvider("Investor Relations", explode=True),
            polite_delay=0,
        )
        broken.crawl_company(company, download=False)
        working = FilingCollector(
            db, downloader=_StubDownloader(),
            nse=_StubProvider("NSE Corporate Filings", []),
            bse=_StubProvider("BSE Corporate Announcements", []),
            ir=_StubProvider("Investor Relations", []),
            polite_delay=0,
        )
        working.crawl_company(company, download=False)

        state = db.query(CompanyCrawlState).filter_by(
            company_id=company.id
        ).one()
        assert state.consecutive_failures == 0


class TestStorageGuard:
    """STORAGE-001 — the crawler must yield the disk before a user does."""

    def test_a_download_is_deferred_when_storage_is_low(self, collector_env,
                                                        monkeypatch):
        """Observed in production: 229 MB of a 500 MB volume consumed within
        minutes of the first real run. Left unchecked the failure lands on
        whatever writes next, quite possibly a user's upload."""
        from app.services.filings.collector import FilingCollector

        db, company = collector_env
        nse = _StubProvider("NSE Corporate Filings", [
            _filing("Annual Report", "S1", "http://x/a.pdf"),
        ])
        downloader = _StubDownloader()
        collector = FilingCollector(
            db, downloader=downloader, nse=nse,
            bse=_StubProvider("BSE", error="none"),
            ir=_StubProvider("IR", error="none"), polite_delay=0,
        )
        monkeypatch.setattr(collector, "_has_headroom", lambda: False)

        result = collector.crawl_company(company, download=True)
        assert not downloader.fetched, "downloaded despite low storage"
        assert result.ingested == 0

    def test_a_deferred_filing_stays_collectable(self, collector_env,
                                                 monkeypatch):
        """Deferred, not failed: it must be picked up once space is freed
        rather than needing a manual retry."""
        from app.models.filing_collection import DiscoveredFiling
        from app.services.filings.collector import FilingCollector

        db, company = collector_env
        nse = _StubProvider("NSE Corporate Filings", [
            _filing("Annual Report", "S2", "http://x/b.pdf"),
        ])
        collector = FilingCollector(
            db, downloader=_StubDownloader(), nse=nse,
            bse=_StubProvider("BSE", error="none"),
            ir=_StubProvider("IR", error="none"), polite_delay=0,
        )
        monkeypatch.setattr(collector, "_has_headroom", lambda: False)
        collector.crawl_company(company, download=True)

        row = db.query(DiscoveredFiling).filter_by(
            company_id=company.id, source_reference="S2"
        ).one()
        assert row.status == CollectionStatus.DISCOVERED.value
        assert "storage" in (row.error or "").lower()

    def test_the_check_fails_open(self, collector_env, monkeypatch):
        """A broken telemetry call must not halt collection."""
        from app.services.filings.collector import FilingCollector

        db, _ = collector_env
        collector = FilingCollector(db, downloader=_StubDownloader(),
                                    nse=_StubProvider("NSE"),
                                    bse=_StubProvider("BSE"),
                                    ir=_StubProvider("IR"), polite_delay=0)
        monkeypatch.setattr(
            "app.services.documents.storage.free_disk_bytes",
            lambda p: (_ for _ in ()).throw(OSError("boom")),
        )
        assert collector._has_headroom() is True


class TestDashboard:
    def test_it_reports_counts_and_storage(self, collector_env):
        from app.services.filings.collector import FilingCollector

        db, company = collector_env
        nse = _StubProvider("NSE Corporate Filings", [
            _filing("Annual Report", "D1", "http://x/a.pdf"),
        ])
        collector = _make_collector(db, nse=nse,
                                    bse=_StubProvider("BSE", error="none"),
                                    ir=_StubProvider("IR", error="none"))
        collector.crawl_company(company, download=True)

        payload = collector.dashboard()
        assert payload["total_documents"] >= 1
        assert payload["storage_bytes"] > 0
        assert set(payload["by_status"]) == {s.value for s in CollectionStatus}


# ===========================================================================
class TestScheduling:
    """The system must run itself."""

    def test_the_crawl_covers_the_universe_within_a_day(self):
        """Twice daily, not once.

        Updated from `== 24 * 3600` when the schedule changed. A single daily
        pass at 25 companies left 340 of 501 never crawled, because the WEEKLY
        tier re-queued the head of the list before the tail was reached. The
        assertion is now the property that matters — full coverage inside 24
        hours — rather than a specific interval.
        """
        from app.domain.platform.jobs import SCHEDULES, JobKind

        crawl = [s for s in SCHEDULES if s.kind is JobKind.FILING_CRAWL]
        assert crawl, "no filing crawl on the schedule"
        assert crawl[0].every_seconds <= 24 * 3600

        passes_per_day = (24 * 3600) // crawl[0].every_seconds
        assert passes_per_day * 260 >= 500, (
            "the schedule cannot reach 500 companies in 24 hours"
        )

    def test_post_processing_runs_more_often_than_the_crawl(self):
        """Ingestion is asynchronous, so the rescore must follow the document
        rather than the crawl that fetched it."""
        from app.domain.platform.jobs import SCHEDULES, JobKind

        by_kind = {s.kind: s.every_seconds for s in SCHEDULES}
        assert by_kind[JobKind.FILING_POST_PROCESS] < by_kind[JobKind.FILING_CRAWL]

    def test_both_job_kinds_have_handlers(self):
        from app.domain.platform.jobs import JobKind
        from app.services.platform.jobs.handlers import handler_for

        assert handler_for(JobKind.FILING_CRAWL) is not None
        assert handler_for(JobKind.FILING_POST_PROCESS) is not None

    def test_both_job_kinds_have_retry_policies(self):
        from app.domain.platform.jobs import JobKind, policy_for

        assert policy_for(JobKind.FILING_CRAWL).max_attempts >= 1
        assert policy_for(JobKind.FILING_POST_PROCESS).max_attempts >= 1

    def test_a_crawl_backs_off_hard(self):
        """A failing crawl is usually the exchange rate-limiting us."""
        from app.domain.platform.jobs import JobKind, policy_for

        assert policy_for(JobKind.FILING_CRAWL).base_seconds >= 60


class TestNotificationDiscipline:
    """An alert channel that cries wolf stops being read."""

    def test_a_trivial_score_move_is_not_material(self):
        from app.services.filings.post_filing import PostFilingResult, ScoreDelta

        result = PostFilingResult(
            company_id="c", ticker="T", overall_before=70.0,
            overall_after=70.1, grade_before="A", grade_after="A",
            deltas=[ScoreDelta("risk", 60.0, 60.05)],
        )
        assert not result.is_material

    def test_a_meaningful_move_is_material(self):
        from app.services.filings.post_filing import PostFilingResult

        result = PostFilingResult(
            company_id="c", ticker="T", overall_before=70.0,
            overall_after=74.0, grade_before="A", grade_after="A",
        )
        assert result.is_material

    def test_a_grade_change_is_always_material(self):
        """Even a fractional move across a grade boundary matters."""
        from app.services.filings.post_filing import PostFilingResult

        result = PostFilingResult(
            company_id="c", ticker="T", overall_before=70.0,
            overall_after=70.1, grade_before="BBB", grade_after="A",
        )
        assert result.is_material

    def test_a_refusal_never_reaches_a_notification(self):
        """With no live provider the analyst declines, correctly. Pasting
        'Insufficient evidence' into a customer alert would be worse than the
        deterministic summary."""
        from app.services.filings.post_filing import _is_refusal

        assert _is_refusal("**Insufficient evidence.** The platform holds no…")
        assert _is_refusal("No verified evidence available for this section.")
        assert not _is_refusal("Revenue rose to 267,021 crore [revenue].")

    def test_dimensions_and_overall_share_one_scale(self):
        """`score_pct` is a fraction and `overall_score` is 0-100. Mixing them
        makes a 0.5 threshold mean half a point on one and fifty on the other,
        so no dimension would ever alert."""
        from app.services.filings.post_filing import PostFilingProcessor

        class _Cat:
            def __init__(self, key, pct):
                self.key, self.score_pct = key, pct

        class _Result:
            categories = [_Cat("financial_quality", 0.8435)]

        dims = PostFilingProcessor._dimensions(_Result())
        assert dims["financial_quality"] == pytest.approx(84.35, abs=0.01)


class TestUniverseScaleFixes:
    """Defects that only appear at 500 companies rather than 26."""

    def test_delisted_companies_are_not_crawled(self, collector_env):
        """DELIST-001. Tata Motors has demerged and Zomato has renamed; their
        old symbols resolve to nothing at NSE, so each burned a request and a
        failure counter on every nightly pass."""
        db, company = collector_env
        collector = _make_collector(db)
        assert company.id in {c.id for c in collector.due_companies()}

        company.listing_status = "delisted"
        db.commit()
        assert company.id not in {c.id for c in collector.due_companies()}

    def test_the_bse_code_is_adopted_from_the_company(self, collector_env):
        """BSE-001. The Nifty 500 import populated 498 BSE codes on the
        company, but crawl state was only ever set by hand, so every BSE fetch
        reported 'no scrip code mapped'."""
        db, company = collector_env
        company.bse_code = "500325"
        db.commit()

        state = _make_collector(db).state_for(company)
        assert state.bse_scrip_code == "500325"

    def test_an_operator_set_scrip_code_is_never_overwritten(self,
                                                             collector_env):
        from app.models.filing_collection import CompanyCrawlState

        db, company = collector_env
        db.add(CompanyCrawlState(company_id=company.id, tier="weekly",
                                 bse_scrip_code="999999"))
        db.commit()
        company.bse_code = "500325"
        db.commit()

        assert _make_collector(db).state_for(company).bse_scrip_code == "999999"

    def test_object_primary_uses_the_transit_headroom(self, monkeypatch,
                                                      collector_env):
        """STORAGE-002. With R2 primary a document does not stay on the
        volume, so reserving 512 MB against an already-full 500 MB volume
        refused every download forever."""
        from app.core.config import settings
        from app.services.filings.collector import TRANSIT_HEADROOM_BYTES

        db, _ = collector_env
        collector = _make_collector(db)
        del collector._has_headroom      # restore the real implementation

        monkeypatch.setattr(settings, "DOCUMENT_STORAGE_BACKEND", "r2")
        monkeypatch.setattr(settings, "DOCUMENT_STORAGE_PATH", "/tmp")
        monkeypatch.setattr(
            "app.services.documents.storage.free_disk_bytes",
            lambda p: TRANSIT_HEADROOM_BYTES + 1,
        )
        assert collector._has_headroom(), (
            "object-primary collection blocked despite adequate transit space"
        )

    def test_volume_primary_still_reserves_the_durable_floor(self, monkeypatch,
                                                             collector_env):
        """The stricter reservation must survive for volume-primary
        deployments, where the volume really does hold the corpus."""
        from app.core.config import settings
        from app.services.filings.collector import TRANSIT_HEADROOM_BYTES

        db, _ = collector_env
        collector = _make_collector(db)
        del collector._has_headroom

        monkeypatch.setattr(settings, "DOCUMENT_STORAGE_BACKEND", "local")
        monkeypatch.setattr(settings, "DOCUMENT_STORAGE_PATH", "/tmp")
        monkeypatch.setattr(
            "app.services.documents.storage.free_disk_bytes",
            lambda p: TRANSIT_HEADROOM_BYTES + 1,
        )
        assert not collector._has_headroom(), (
            "volume-primary accepted a download below the durable floor"
        )
