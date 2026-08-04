"""Temporal memory: per-year observations, prior-year verification, timelines.

The LLM is stubbed throughout. These assert the engine's own rules — year
anchoring, versioning, verdict discipline and credibility arithmetic — not a
provider's uptime.
"""

from __future__ import annotations

import importlib
import json
import pkgutil
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models as _models_pkg
from app.db.base import Base
from app.domain.knowledge.temporal import (
    GuidanceVerdict, ObservationTrend, YearObservation, credibility_score,
    trend_of,
)
from app.models.company import Company
from app.models.knowledge import YearlyObservation
from app.services.knowledge.temporal import TemporalMemoryService

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


@pytest.fixture()
def company(db):
    row = Company(
        id=str(uuid.uuid4()), name="Test Ltd.", ticker="TEST",
        exchange="NSE", listing_status="active",
    )
    db.add(row)
    db.commit()
    return row


def _service(db, replies, *, evidence="[E1] some evidence"):
    """A service whose LLM and evidence lookup are both stubbed.

    `replies` maps fiscal year -> payload dict, so a test can script what the
    model 'says' in each year and assert what the engine does with it.
    """
    service = TemporalMemoryService(db)
    service._evidence_for = lambda cid, year: (  # noqa: SLF001
        (evidence, [1]) if year in replies else ("", [])
    )
    service._metrics_for = lambda cid, year: {}  # noqa: SLF001

    def ask(*, fiscal_year, evidence, prior, metrics, prior_metrics):
        return replies[fiscal_year], "stub:model", False

    service._ask = ask  # noqa: SLF001
    return service


# ------------------------------------------------------------ domain rules

def test_trend_has_a_dead_band():
    """Without it a 0.1% move reads as 'improving' and a ten-year timeline
    becomes an alternating description of noise."""
    assert trend_of(100.5, 100.0) == ObservationTrend.STABLE
    assert trend_of(110.0, 100.0) == ObservationTrend.IMPROVING
    assert trend_of(90.0, 100.0) == ObservationTrend.DETERIORATING
    assert trend_of(None, 100.0) == ObservationTrend.UNKNOWN


def test_credibility_is_none_when_no_year_is_assessable():
    """A company nobody can grade is not an average company. Returning 0.5
    would invent a track record."""
    observations = [
        YearObservation(fiscal_year=y,
                        prior_verdict=GuidanceVerdict.NOT_ASSESSABLE)
        for y in (2024, 2025, 2026)
    ]
    score, assessed = credibility_score(observations)
    assert score is None
    assert assessed == 0


def test_credibility_excludes_unassessable_years_from_the_denominator():
    observations = [
        YearObservation(fiscal_year=2024,
                        prior_verdict=GuidanceVerdict.DELIVERED),
        YearObservation(fiscal_year=2025,
                        prior_verdict=GuidanceVerdict.NOT_ASSESSABLE),
        YearObservation(fiscal_year=2026,
                        prior_verdict=GuidanceVerdict.MISSED),
    ]
    score, assessed = credibility_score(observations)
    assert assessed == 2, "an unjudgeable year was counted"
    assert score == pytest.approx(0.5)


def test_dimension_contradiction_is_flagged_not_corrected():
    """A narrative claim that disagrees with the accounts is surfaced, not
    smoothed over — the filing may mean a segment or a normalised figure."""
    from app.domain.knowledge.temporal import DimensionReading

    # "margins improving" while the measured margin fell.
    reading = DimensionReading(
        dimension="margins", trend=ObservationTrend.IMPROVING,
        metric_value=0.14, metric_prior=0.20,
    )
    assert reading.contradicts_metric is True


def test_debt_polarity_is_inverted():
    """"Debt improving" means debt FELL.

    A single polarity marks every genuine deleveraging as a contradiction and
    every increase in borrowing as agreement — inverted on the one dimension
    a credit analyst cares most about.
    """
    from app.domain.knowledge.temporal import DimensionReading

    deleveraging = DimensionReading(
        dimension="debt", trend=ObservationTrend.IMPROVING,
        metric_value=500.0, metric_prior=900.0,
    )
    assert deleveraging.contradicts_metric is False, "deleveraging flagged"

    claims_better_but_borrowed_more = DimensionReading(
        dimension="debt", trend=ObservationTrend.IMPROVING,
        metric_value=900.0, metric_prior=500.0,
    )
    assert claims_better_but_borrowed_more.contradicts_metric is True


# ------------------------------------------------------------- generation

def test_an_observation_is_anchored_to_its_fiscal_year(db, company):
    service = _service(db, {
        2025: {"findings": ["a"], "confidence": 0.9,
               "prior_year_verdict": "not_assessable"},
    })
    row = service.generate_year(company.id, 2025)
    assert row.fiscal_year == 2025


def test_a_year_with_no_evidence_is_unobservable_not_invented(db, company):
    service = _service(db, {})
    assert service.generate_year(company.id, 2025) is None
    assert db.query(YearlyObservation).count() == 0


def test_a_verdict_requires_recorded_prior_guidance(db, company):
    """The model may volunteer 'delivered' for a first year. Without prior
    guidance to judge, any verdict but not_assessable is invented."""
    service = _service(db, {
        2025: {"findings": ["a"], "confidence": 0.9,
               "prior_year_verdict": "delivered",
               "verdict_reasoning": "made up"},
    })
    row = service.generate_year(company.id, 2025, prior=None)
    assert row.prior_verdict == GuidanceVerdict.NOT_ASSESSABLE.value
    assert row.verdict_reasoning is None


def test_a_verdict_is_kept_when_the_prior_year_recorded_guidance(db, company):
    service = _service(db, {
        2025: {"findings": ["capex rising"], "confidence": 0.92,
               "guidance": "commission Unit-3 during FY2026",
               "prior_year_verdict": "not_assessable"},
        2026: {"findings": ["plant commissioned"], "confidence": 0.95,
               "prior_year_verdict": "delivered",
               "verdict_reasoning": "Unit-3 commissioned [E1]"},
    })
    service.observable_years = lambda cid: [2025, 2026]  # noqa: SLF001
    run = service.build_company(company.id)
    assert run.generated == 2

    fy26 = service.current(company.id, 2026)
    assert fy26.prior_verdict == GuidanceVerdict.DELIVERED.value
    assert "Unit-3" in fy26.verdict_reasoning


def test_years_are_generated_chronologically(db, company):
    """FY2026 must see FY2025's guidance, so the series cannot be built out
    of order or in parallel."""
    seen: list[tuple[int, str | None]] = []
    service = TemporalMemoryService(db)
    service._evidence_for = lambda cid, y: ("[E1] x", [1])  # noqa: SLF001
    service._metrics_for = lambda cid, y: {}  # noqa: SLF001

    def ask(*, fiscal_year, evidence, prior, metrics, prior_metrics):
        seen.append((fiscal_year, prior.guidance if prior else None))
        return ({"findings": ["f"], "confidence": 0.9,
                 "guidance": f"guidance from FY{fiscal_year}",
                 "prior_year_verdict": "delivered"}, "stub:model", False)

    service._ask = ask  # noqa: SLF001
    service.observable_years = lambda cid: [2024, 2025, 2026]  # noqa: SLF001
    service.build_company(company.id)

    assert [y for y, _ in seen] == [2024, 2025, 2026]
    assert seen[1][1] == "guidance from FY2024"
    assert seen[2][1] == "guidance from FY2025"


def test_low_confidence_year_is_stored_but_not_served(db, company):
    service = _service(db, {
        2025: {"findings": ["thin"], "confidence": 0.20,
               "prior_year_verdict": "not_assessable"},
    })
    row = service.generate_year(company.id, 2025)

    assert row.status == "superseded", "a weak observation was served"
    assert db.query(YearlyObservation).count() == 1, "history was discarded"
    assert service.current(company.id, 2025) is None
    assert service.timeline(company.id) == []


def test_temp_001_a_withheld_year_is_not_used_as_the_yardstick(db, company):
    """Regression for TEMP-001, found on live Cipla data.

    A below-threshold year is stored as history and does NOT appear in the
    timeline. Carrying it forward as `prior` anyway would grade the next year
    against an observation no reader can see — two inconsistent notions of
    "this year exists". Cipla FY2026 scored 0.30 on three thin exchange
    filings; FY2027 must be judged against the last SERVABLE year, or nothing.
    """
    priors: list[int | None] = []
    service = TemporalMemoryService(db)
    service._evidence_for = lambda cid, y: ("[E1] x", [1])  # noqa: SLF001
    service._metrics_for = lambda cid, y: {}  # noqa: SLF001
    service.observable_years = lambda cid: [2025, 2026]  # noqa: SLF001

    payloads = {
        # Weak year: stored, withheld from the timeline.
        2025: {"findings": ["thin"], "confidence": 0.20,
               "guidance": "we will do things",
               "prior_year_verdict": "not_assessable"},
        2026: {"findings": ["strong"], "confidence": 0.95,
               "prior_year_verdict": "delivered",
               "verdict_reasoning": "against a year nobody can see"},
    }

    def ask(*, fiscal_year, evidence, prior, metrics, prior_metrics):
        priors.append(prior.fiscal_year if prior else None)
        return payloads[fiscal_year], "stub:model", False

    service._ask = ask  # noqa: SLF001
    service.build_company(company.id)

    assert priors == [None, None], f"withheld year leaked forward: {priors}"
    fy26 = service.current(company.id, 2026)
    assert fy26.prior_verdict == GuidanceVerdict.NOT_ASSESSABLE.value


# -------------------------------------------------------------- versioning

def test_regenerating_a_year_versions_rather_than_overwrites(db, company):
    service = _service(db, {
        2025: {"findings": ["first"], "confidence": 0.8,
               "prior_year_verdict": "not_assessable"},
    })
    first = service.generate_year(company.id, 2025)

    service2 = _service(db, {
        2025: {"findings": ["revised"], "confidence": 0.9,
               "prior_year_verdict": "not_assessable"},
    })
    second = service2.generate_year(company.id, 2025, overwrite=True)

    assert second.version == first.version + 1
    rows = service.history(company.id, 2025)
    assert len(rows) == 2, "history was overwritten"

    db.refresh(first)
    assert first.status == "superseded"
    assert first.superseded_by == second.id
    assert first.findings == "first", "prior version's content was mutated"


def test_existing_years_are_skipped_unless_overwrite(db, company):
    service = _service(db, {
        2025: {"findings": ["x"], "confidence": 0.9,
               "prior_year_verdict": "not_assessable"},
    })
    service.observable_years = lambda cid: [2025]  # noqa: SLF001

    service.build_company(company.id)
    run = service.build_company(company.id)

    assert run.generated == 0
    assert run.skipped_existing == 1


# ---------------------------------------------------------------- reading

def test_timeline_is_oldest_first(db, company):
    for year in (2026, 2024, 2025):
        db.add(YearlyObservation(
            company_id=company.id, fiscal_year=year, confidence=0.9,
            findings="f", status="current",
        ))
    db.commit()

    years = [r.fiscal_year for r in TemporalMemoryService(db).timeline(company.id)]
    assert years == [2024, 2025, 2026]


def test_render_matches_the_briefs_compact_form(db, company):
    db.add(YearlyObservation(
        company_id=company.id, fiscal_year=2025, confidence=0.92,
        findings="Management quality improving.\nCapex increasing.",
        status="current", prior_verdict="not_assessable",
    ))
    db.commit()

    rendered = TemporalMemoryService(db).render_timeline(company.id)
    assert "FY2025" in rendered
    assert "Management quality improving." in rendered
    assert "Confidence: 92%" in rendered


def test_dimensions_round_trip_as_json(db, company):
    service = _service(db, {
        2025: {
            "findings": ["x"], "confidence": 0.9,
            "dimensions": {"debt": "improving", "roce": "improving",
                           "moat": "nonsense-value"},
            "prior_year_verdict": "not_assessable",
        },
    })
    row = service.generate_year(company.id, 2025)
    stored = {d["dimension"]: d["trend"] for d in json.loads(row.dimensions)}

    assert stored["debt"] == "improving"
    # An unrecognised trend degrades to unknown rather than being stored raw.
    assert stored["moat"] == "unknown"
    # Every tracked dimension is present, so a ten-year series stays aligned.
    assert stored["management_quality"] == "unknown"


def test_malformed_model_output_is_recorded_as_fallback(db, company):
    service = TemporalMemoryService(db)
    service._evidence_for = lambda cid, y: ("[E1] x", [1])  # noqa: SLF001
    service._metrics_for = lambda cid, y: {}  # noqa: SLF001

    assert service._parse("not json at all") is None  # noqa: SLF001
    assert service._parse('```json\n{"a":1}\n```') == {"a": 1}  # noqa: SLF001
    assert service._parse('Here you go: {"a":2} cheers') == {"a": 2}  # noqa: SLF001


def test_credibility_reports_the_years_it_rests_on(db, company):
    db.add_all([
        YearlyObservation(company_id=company.id, fiscal_year=2025,
                          confidence=0.9, status="current",
                          prior_verdict="delivered"),
        YearlyObservation(company_id=company.id, fiscal_year=2026,
                          confidence=0.9, status="current",
                          prior_verdict="missed"),
        YearlyObservation(company_id=company.id, fiscal_year=2027,
                          confidence=0.9, status="current",
                          prior_verdict="not_assessable"),
    ])
    db.commit()

    result = TemporalMemoryService(db).credibility(company.id)
    assert result["years_total"] == 3
    assert result["years_assessed"] == 2
    assert result["score"] == pytest.approx(0.5)


# ------------------------------------------------------ TEMP-002 recovery

def test_temp_002_truncated_json_is_recovered_not_discarded(db, company):
    """Regression for TEMP-002, found on live Sun Pharma data.

    The free-tier models on this key are REASONING models: they spend most of
    the completion budget on hidden reasoning before emitting any JSON. With
    a full evidence block the reply was cut off part-way through the object,
    `json.loads` failed, and a complete and correct set of findings was
    discarded and stored as a template fallback — analysis mislabelled as
    fabricated prose.
    """
    service = TemporalMemoryService(db)
    truncated = (
        '{\n "findings": [\n  "Revenue grew 11.9% to 582bn.[E18]",\n'
        '  "EBITDA rose 16.1% to 177bn.[E18]"\n ],\n'
        ' "confidence": 0.88,\n "guidance": "margin expansion in FY27'
    )

    parsed = service._parse(truncated)  # noqa: SLF001

    assert parsed is not None, "a recoverable reply was thrown away"
    assert parsed["confidence"] == 0.88
    assert len(parsed["findings"]) == 2
    assert "Revenue grew 11.9%" in parsed["findings"][0]


def test_recovery_drops_the_incomplete_trailing_value(db, company):
    """The cut-off field must be dropped, never guessed at."""
    service = TemporalMemoryService(db)
    parsed = service._parse(  # noqa: SLF001
        '{"confidence": 0.9, "findings": ["complete"], "guidance": "half a sent'
    )
    assert parsed["findings"] == ["complete"]
    assert parsed["confidence"] == 0.9


def test_unrecoverable_output_still_returns_none(db, company):
    service = TemporalMemoryService(db)
    assert service._parse("I am afraid I cannot help with that.") is None  # noqa: SLF001
    assert service._parse("") is None  # noqa: SLF001


def test_completion_budget_accounts_for_reasoning_tokens():
    """A budget sized for the visible answer starves a reasoning model."""
    from app.services.knowledge.temporal import MAX_COMPLETION_TOKENS

    assert MAX_COMPLETION_TOKENS >= 8000, (
        "budget too small for a model that reasons before answering"
    )


def test_observation_run_as_dict():
    from app.services.knowledge.temporal import ObservationRun
    run = ObservationRun(company_id="company-abc")
    run.generated = 3
    run_dict = run.as_dict()
    assert run_dict["company_id"] == "company-abc"
    assert run_dict["generated"] == 3


def test_real_evidence_for_summaries_and_chunks(db, company):
    from app.models.knowledge import DocumentSummary
    from app.models.document import Document, DocumentChunk

    doc = Document(id=33, company_id=company.id, filename="fy25.pdf", doc_type="annual_report", file_format="pdf", content_hash="hash33")
    db.add(doc)
    db.commit()

    # Create summary
    summary = DocumentSummary(
        document_id=33, company_id=company.id, kind="business_overview",
        content="This is the summary content for FY2025.", fiscal_year=2025, word_count=100
    )
    db.add(summary)
    db.commit()

    service = TemporalMemoryService(db)
    evidence, doc_ids = service._evidence_for(company.id, 2025)
    assert "This is the summary content" in evidence
    assert doc_ids == [33]

    # Test fallback to chunks when no summaries exist
    doc2 = Document(id=34, company_id=company.id, filename="fy26.pdf", doc_type="annual_report", file_format="pdf", content_hash="hash34", fiscal_year=2026)
    db.add(doc2)
    db.commit()

    chunk = DocumentChunk(
        document_id=34, chunk_index=0, text="This is fallback chunk text for FY2026.",
        fingerprint="fingerprint34"
    )
    db.add(chunk)
    db.commit()

    evidence2, doc_ids2 = service._evidence_for(company.id, 2026)
    assert "This is fallback chunk" in evidence2
    assert doc_ids2 == [34]


def test_real_metrics_for(db, company):
    service = TemporalMemoryService(db)
    metrics = service._metrics_for(company.id, 2025)
    assert isinstance(metrics, dict)

