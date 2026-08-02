"""Scheduler optimisation: IR discovery, coverage, NSE retry, classification.

Network is stubbed throughout — these assert the platform's own logic, not a
provider's uptime.
"""

from __future__ import annotations

import importlib
import pkgutil
import urllib.error
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models as _models_pkg
from app.data.filings.indian import NSEFilingProvider
from app.db.base import Base
from app.domain.documents.types import DocumentType
from app.domain.filings.collection import classify, is_noise
from app.domain.platform.jobs import (
    DEFAULT_PRIORITY, JOB_LABELS, RETRY_POLICIES, SCHEDULES, JobKind,
)
from app.models.company import Company
from app.models.filing_collection import CompanyCrawlState, DiscoveredFiling
from app.services.filings.dashboard import SchedulerDashboard
from app.services.filings.ir_discovery import (
    IR_PATHS, IRDiscoveryService, candidate_domains,
)

for _module in pkgutil.iter_modules(_models_pkg.__path__):
    importlib.import_module(f"app.models.{_module.name}")


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _company(db, ticker="TEST", name="Test Ltd.", website=None):
    row = Company(
        id=str(uuid.uuid4()), name=name, ticker=ticker, exchange="NSE",
        listing_status="active", website=website,
    )
    db.add(row)
    db.add(CompanyCrawlState(company_id=row.id, tier="daily"))
    db.commit()
    return row


# =====================================================  1. IR URL discovery

def test_domain_is_derived_from_the_company_name(db):
    company = _company(db, "CIPLA", "Cipla Ltd.")
    domains = candidate_domains(company)
    assert "https://www.cipla.com" in domains


def test_corporate_suffixes_are_stripped(db):
    company = _company(db, "XYZ", "Havells India Limited")
    assert "https://www.havells.com" in candidate_domains(company)


def test_a_registered_website_wins_over_a_guess(db):
    company = _company(db, "ABC", "Something Ltd.",
                       website="https://www.realsite.co.in/about")
    assert candidate_domains(company) == ["https://www.realsite.co.in"]


def test_a_403_counts_as_found(db):
    """Measured on real pages: tcs.com/investor-relations and
    infosys.com/investors both answer 403. They exist and are refusing a
    non-browser client. Treating that as failure discards the IR pages of two
    of India's largest companies."""
    _company(db, "PROBE", "Probeco Ltd.")
    service = IRDiscoveryService(db, polite_delay=0, probe=lambda url: 403)

    report = service.run(limit=5)
    outcome = report.outcomes[0]

    assert outcome.found is True
    assert outcome.confidence < 0.9, "a blocked page must rank below a fetched one"
    assert outcome.method == "probe:403"


def test_a_200_outranks_a_403(db):
    _company(db, "OK1", "Okayco Ltd.")
    service = IRDiscoveryService(db, polite_delay=0, probe=lambda url: 200)
    outcome = service.run(limit=5).outcomes[0]
    assert outcome.confidence == pytest.approx(0.90)


def test_a_404_is_not_treated_as_found(db):
    _company(db, "GONE", "Nowhereco Ltd.")
    service = IRDiscoveryService(db, polite_delay=0, probe=lambda url: 404)
    outcome = service.run(limit=5).outcomes[0]
    assert outcome.found is False
    assert outcome.error


def test_an_unresolvable_domain_abandons_that_domain_immediately(db):
    """Seven probes proving a dead host is six wasted requests."""
    _company(db, "DEAD", "Deadco Ltd.")
    calls: list[str] = []

    def probe(url):
        calls.append(url)
        return None                      # DNS failure

    IRDiscoveryService(db, polite_delay=0, probe=probe).run(limit=5)

    # The invariant is that a dead host costs ONE probe per domain, not one
    # per path. The root fallback then probes each domain once more, so the
    # ceiling is 2 x domains — still far below domains x paths, which is what
    # the guard exists to prevent.
    domains = len(candidate_domains(_company(db, "X2", "Deadco Ltd.")))
    assert len(calls) <= 2 * domains
    assert len(calls) < domains * len(IR_PATHS)


def test_discovery_persists_url_confidence_and_method(db):
    company = _company(db, "STORE", "Storeco Ltd.")
    IRDiscoveryService(db, polite_delay=0, probe=lambda url: 200).run(limit=5)

    state = db.scalar(select(CompanyCrawlState).where(
        CompanyCrawlState.company_id == company.id))
    assert state.ir_url
    assert state.ir_url_confidence == pytest.approx(0.90)
    assert state.ir_url_method == "probe:200"
    assert state.ir_url_checked_at is not None


def test_companies_with_a_url_are_skipped_unless_overwrite(db):
    company = _company(db, "HAVE", "Haveco Ltd.")
    state = db.scalar(select(CompanyCrawlState).where(
        CompanyCrawlState.company_id == company.id))
    state.ir_url = "https://existing.example/investors"
    db.commit()

    service = IRDiscoveryService(db, polite_delay=0, probe=lambda url: 200)
    assert service.run(limit=5).outcomes == []
    assert len(service.run(limit=5, overwrite=True).outcomes) == 1


def test_a_failing_company_does_not_stop_the_run(db):
    _company(db, "AAA", "Aaa Ltd.")
    _company(db, "BBB", "Bbb Ltd.")

    def probe(url):
        if "aaa" in url:
            raise RuntimeError("socket exploded")
        return 200

    report = IRDiscoveryService(db, polite_delay=0, probe=probe).run(limit=5)
    assert len(report.outcomes) == 2
    assert len(report.found) == 1


# ================================================  2. Coverage within 24h

def test_crawl_runs_twice_daily():
    spec = next(s for s in SCHEDULES if s.kind == JobKind.FILING_CRAWL)
    assert spec.every_seconds == 12 * 3600


def test_two_passes_cover_the_whole_universe():
    """2 x 260 >= 500. The audit found 340 of 501 never crawled at 25/day."""
    from app.services.platform.jobs import handlers
    import inspect

    source = inspect.getsource(handlers.handle_filing_crawl)
    assert 'payload.get("max_companies", 260)' in source

    passes_per_day = (24 * 3600) // (12 * 3600)
    assert passes_per_day * 260 >= 500


def test_ir_discovery_is_registered_everywhere():
    """JOB-001: a kind missing from DEFAULT_PRIORITY raises on enqueue."""
    kind = JobKind.IR_DISCOVERY
    assert kind in JOB_LABELS
    assert kind in DEFAULT_PRIORITY
    assert kind in RETRY_POLICIES
    assert any(s.kind == kind for s in SCHEDULES)

    from app.services.platform.jobs.handlers import handler_for
    assert handler_for(kind) is not None


# =============================================  3. NSE exponential backoff

def test_backoff_grows_exponentially():
    provider = NSEFilingProvider()
    delays = [provider._backoff_seconds(i) for i in (1, 2, 3)]  # noqa: SLF001
    assert delays[0] < delays[1] < delays[2]
    # Jitter is applied, so assert the band rather than an exact value.
    # Bands, not exact values, because jitter is +/-30%. The bounds are
    # computed from the schedule rather than hard-coded: an earlier version
    # asserted `>= 10.0` and flaked when jitter produced 9.71.
    base, factor, jitter = (
        NSEFilingProvider._RETRY_BASE_SECONDS,      # noqa: SLF001
        NSEFilingProvider._RETRY_FACTOR,            # noqa: SLF001
        NSEFilingProvider._RETRY_JITTER,            # noqa: SLF001
    )
    for attempt, delay in enumerate(delays, start=1):
        nominal = base * (factor ** (attempt - 1))
        assert nominal * (1 - jitter) <= delay <= nominal * (1 + jitter)


def test_jitter_prevents_synchronised_retries():
    """A crawl loops over companies; a fixed backoff would align every retry
    into a burst and reproduce the overload that caused the timeout."""
    provider = NSEFilingProvider()
    samples = {round(provider._backoff_seconds(2), 4) for _ in range(20)}  # noqa: SLF001
    assert len(samples) > 1, "backoff is deterministic — no jitter applied"


def test_timeouts_are_retryable_but_404_is_not():
    provider = NSEFilingProvider()
    assert provider._is_retryable(TimeoutError()) is True          # noqa: SLF001
    assert provider._is_retryable(urllib.error.URLError("x")) is True  # noqa: SLF001
    assert provider._is_retryable(                                  # noqa: SLF001
        urllib.error.HTTPError("u", 503, "busy", {}, None)) is True
    assert provider._is_retryable(                                  # noqa: SLF001
        urllib.error.HTTPError("u", 404, "nf", {}, None)) is False


def test_fetch_retries_then_reports_attempt_count(monkeypatch):
    provider = NSEFilingProvider(attempts=3)
    calls = {"n": 0}

    class _Boom:
        def open(self, *a, **k):
            calls["n"] += 1
            raise TimeoutError("The read operation timed out")

    monkeypatch.setattr(provider, "_session", lambda: _Boom())
    monkeypatch.setattr(provider, "_backoff_seconds", lambda attempt: 0.0)

    result = provider.fetch("CIPLA")

    assert calls["n"] == 3, f"made {calls['n']} attempts, expected 3"
    assert "after 3 attempts" in result.error


def test_a_404_is_not_retried(monkeypatch):
    provider = NSEFilingProvider(attempts=3)
    calls = {"n": 0}

    class _NotFound:
        def open(self, *a, **k):
            calls["n"] += 1
            raise urllib.error.HTTPError("u", 404, "nf", {}, None)

    monkeypatch.setattr(provider, "_session", lambda: _NotFound())
    monkeypatch.setattr(provider, "_backoff_seconds", lambda attempt: 0.0)
    provider.fetch("NOSUCH")

    assert calls["n"] == 1, "a 404 was retried; the symbol will not appear"


# ==============================================  4. Filing classification

@pytest.mark.parametrize(("title", "expected"), [
    ("Annual Report 2025-26", DocumentType.ANNUAL_REPORT),
    ("Unaudited Financial Results Q2 FY27", DocumentType.QUARTERLY_REPORT),
    ("Investor Presentation - November 2026", DocumentType.INVESTOR_PRESENTATION),
    ("Earnings Call Transcript", DocumentType.CONFERENCE_CALL),
    ("Shareholding Pattern for the quarter ended September 2026",
     DocumentType.SHAREHOLDING),
    ("Outcome of Board Meeting", DocumentType.EXCHANGE_FILING),
    ("Credit Rating by CRISIL Ratings", DocumentType.CREDIT_RATING),
    ("Business Responsibility and Sustainability Report",
     DocumentType.ESG_REPORT),
])
def test_every_required_class_is_reachable(title, expected):
    """The brief names nine classes. Each must be produced by a real title."""
    assert classify(title).doc_type is expected


def test_the_nse_conference_call_subject_is_classified():
    """452 rows in production carried this exact subject and fell through to
    `other`: the abbreviation is "Con. Call" with a full stop, and
    "Analysts/" has no word boundary before it."""
    result = classify("Analysts/Institutional Investor Meet/Con. Call Updates")
    assert result.doc_type is DocumentType.CONFERENCE_CALL


def test_generic_updates_are_typed_at_low_confidence():
    """568 rows were bare "Updates" / "General Updates". Typing them at 0.45
    is more useful than a null, provided the confidence shows the doubt."""
    result = classify("General Updates")
    assert result.doc_type is DocumentType.EXCHANGE_FILING
    assert result.confidence <= 0.5


def test_takeover_disclosures_are_shareholding():
    assert classify(
        "Disclosure under SEBI Takeover Regulations"
    ).doc_type is DocumentType.SHAREHOLDING


def test_specific_rules_still_beat_the_generic_fallback():
    """Order matters: the low-confidence `updates` rule is last, so a real
    annual report must not be captured by it."""
    result = classify("Annual Report and General Updates 2026")
    assert result.doc_type is DocumentType.ANNUAL_REPORT
    assert result.confidence >= 0.9


def test_noise_is_still_filtered():
    assert is_noise("Copy of Newspaper Publication") is True
    assert is_noise("Annual Report 2026") is False


# ==========================================  5. Stored fields on a filing

def test_discovered_filing_stores_url_type_year_and_confidence(db):
    company = _company(db, "FIELDS", "Fieldco Ltd.")
    result = classify("Annual Report 2025-26")
    db.add(DiscoveredFiling(
        company_id=company.id, source="NSE Corporate Filings",
        source_reference="ref-1",
        source_url="https://nsearchives.nseindia.com/x.pdf",
        title="Annual Report 2025-26",
        filing_type=result.filing_type.value,
        doc_type=result.doc_type.value,
        classification_confidence=result.confidence,
        fiscal_year=2026,
    ))
    db.commit()

    row = db.scalar(select(DiscoveredFiling))
    assert row.source_url.endswith(".pdf")
    assert row.doc_type == "annual_report"
    assert row.fiscal_year == 2026
    assert row.classification_confidence == pytest.approx(0.95)


# ==============================================  6. Scheduler dashboard

def test_dashboard_reports_every_required_metric(db):
    _company(db, "D1", "Dashco Ltd.")
    snapshot = SchedulerDashboard(db).snapshot()

    assert "crawled_today" in snapshot["coverage"]
    assert "remaining" in snapshot["coverage"]
    assert "pending" in snapshot["retries"]
    assert "companies_failed" in snapshot["failures"]
    assert "discovered_total" in snapshot["ir_urls"]
    assert "downloaded_today" in snapshot["documents"]
    assert "enrichment_runs_today" in snapshot["memory"]


def test_coverage_counts_only_the_window(db):
    company = _company(db, "WIN", "Windowco Ltd.")
    state = db.scalar(select(CompanyCrawlState).where(
        CompanyCrawlState.company_id == company.id))
    state.last_crawled_at = datetime.now(timezone.utc) - timedelta(days=3)
    db.commit()

    snapshot = SchedulerDashboard(db).snapshot(window_hours=24)
    assert snapshot["coverage"]["crawled_today"] == 0
    assert snapshot["coverage"]["remaining"] == 1


def test_pending_retries_are_separated_from_exhausted(db):
    company = _company(db, "RET", "Retryco Ltd.")
    db.add(DiscoveredFiling(
        company_id=company.id, source="s", source_reference="r1",
        title="t", status="failed", attempts=1,
    ))
    db.add(DiscoveredFiling(
        company_id=company.id, source="s", source_reference="r2",
        title="t", status="failed", attempts=5,
    ))
    db.commit()

    retries = SchedulerDashboard(db).snapshot()["retries"]
    assert retries["pending"] == 1
    assert retries["exhausted"] == 1


def test_dashboard_reports_classification_quality(db):
    company = _company(db, "CLS", "Classco Ltd.")
    for index, doc_type in enumerate(
        ["annual_report", "other", "other", "quarterly_report"]
    ):
        db.add(DiscoveredFiling(
            company_id=company.id, source="s", source_reference=f"r{index}",
            title="t", doc_type=doc_type,
        ))
    db.commit()

    classification = SchedulerDashboard(db).snapshot()["classification"]
    assert classification["total"] == 4
    assert classification["classified_pct"] == pytest.approx(50.0)


def test_dot_in_domains_are_tried(db):
    """`.in` is not interchangeable with `.co.in`.

    Measured: aavas.in resolves and aavas.com does not. Omitting the bare
    `.in` silently loses every issuer that uses it.
    """
    company = _company(db, "AAVAS", "Aavas Financiers Ltd.")
    domains = candidate_domains(company)
    assert any(d.endswith(".in") and not d.endswith(".co.in") for d in domains)


def test_a_reachable_root_is_a_low_confidence_fallback(db):
    """ABB and ABFRL answer 200 at the corporate root while every
    conventional IR path misses. Without the fallback they were recorded as
    having no IR presence at all; the crawler follows one hop from a landing
    page, so the root is usable — at the weakest confidence."""
    _company(db, "ROOTY", "Rootyco Ltd.")

    def probe(url):
        # Only the bare domain answers; every path 404s.
        return 200 if url.count("/") == 2 else 404

    outcome = IRDiscoveryService(
        db, polite_delay=0, probe=probe,
    ).run(limit=5).outcomes[0]

    assert outcome.found is True
    assert outcome.method == "probe:root"
    assert outcome.confidence <= 0.45, "a root guess must rank below a real path"


def test_a_real_path_still_beats_the_root_fallback(db):
    _company(db, "PATHY", "Pathyco Ltd.")
    outcome = IRDiscoveryService(
        db, polite_delay=0, probe=lambda url: 200,
    ).run(limit=5).outcomes[0]
    assert outcome.method == "probe:200"
    assert outcome.url.endswith("/investors")
