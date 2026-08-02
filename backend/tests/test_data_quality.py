"""Data Quality Score: weights, checks, grading, warnings and automation."""

from __future__ import annotations

import importlib
import pkgutil
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models as _models_pkg
from app.db.base import Base
from app.domain.platform.jobs import (
    DEFAULT_PRIORITY, JOB_LABELS, RETRY_POLICIES, SCHEDULES, JobKind,
)
from app.domain.quality.score import (
    CHECKS, GRADE_BANDS, MISSING_LABELS, WARN_BELOW, WEIGHTS, Dimension,
    Grade, grade_for,
)
from app.models.analysis import QuarterlyResult
from app.models.company import Company, FinancialFact
from app.models.document import Document
from app.models.filing_collection import CompanyCrawlState
from app.models.knowledge import KnowledgeEntry, YearlyObservation
from app.models.scoring import DataQualitySnapshot
from app.services.quality.service import (
    DataQualityService, QualitySnapshotService,
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


def _company(db, ticker="TEST", **kw):
    row = Company(
        id=str(uuid.uuid4()), name=kw.pop("name", "Test Ltd."), ticker=ticker,
        exchange="NSE", listing_status="active", **kw,
    )
    db.add(row)
    db.commit()
    return row


def _document(db, company, doc_type, *, age_days=1, status="completed"):
    row = Document(
        company_id=company.id, filename=f"{doc_type}.pdf", title=doc_type,
        doc_type=doc_type, file_format="pdf", size_bytes=1, status=status,
        content_hash=uuid.uuid4().hex,
    )
    db.add(row)
    db.commit()
    row.created_at = datetime.now(timezone.utc) - timedelta(days=age_days)
    db.commit()
    return row


# ================================================================== weights

def test_weights_sum_to_exactly_100():
    """Asserted at import too. A silent drift to 95 would make every score in
    the platform wrong by a margin nobody can see."""
    assert sum(WEIGHTS.values()) == 100


def test_every_dimension_the_brief_names_is_weighted():
    assert WEIGHTS[Dimension.IDENTITY] == 5
    assert WEIGHTS[Dimension.FINANCIAL_STATEMENTS] == 20
    assert WEIGHTS[Dimension.DOCUMENTS] == 20
    assert WEIGHTS[Dimension.KNOWLEDGE_VAULT] == 15
    assert WEIGHTS[Dimension.AI_COVERAGE] == 15
    assert WEIGHTS[Dimension.FRESHNESS] == 10
    assert WEIGHTS[Dimension.SOURCE_QUALITY] == 10
    assert WEIGHTS[Dimension.SYSTEM_HEALTH] == 5


def test_every_check_has_a_human_label():
    """The missing panel must never show a raw key: a user cannot act on
    `conference_call_transcripts`, only on the sentence."""
    for dimension, keys in CHECKS.items():
        for key in keys:
            assert key in MISSING_LABELS, f"{dimension.value}.{key} has no label"


def test_every_dimension_declares_its_checks():
    assert set(CHECKS) == set(WEIGHTS)


# =================================================================== grading

@pytest.mark.parametrize(("score", "expected"), [
    (94.0, Grade.A_PLUS), (90.0, Grade.A_PLUS),
    (89.9, Grade.A), (80.0, Grade.A),
    (70.0, Grade.B_PLUS), (60.0, Grade.B),
    (50.0, Grade.C), (40.0, Grade.D), (0.0, Grade.F),
])
def test_grade_bands(score, expected):
    assert grade_for(score) is expected


def test_grade_bands_are_ordered_and_cover_zero():
    floors = [floor for floor, _ in GRADE_BANDS]
    assert floors == sorted(floors, reverse=True)
    assert floors[-1] == 0.0, "a score of 0 must still grade"


# ==================================================================== scoring

def test_an_empty_company_scores_near_zero(db):
    """The point of the exercise: a company the platform has barely seen must
    look that way, not be flattered by a floor."""
    company = _company(db, "EMPTY")
    result = DataQualityService(db).score_company(company)

    assert result.score < 15
    assert result.grade is Grade.F
    assert result.needs_warning is True


def test_identity_scores_what_is_present(db):
    company = _company(db, "IDENT", isin="INE000A01001", sector="Pharma",
                       industry="Generics", website="https://x.example")
    dimension = next(
        d for d in DataQualityService(db).score_company(company).dimensions
        if d.dimension is Dimension.IDENTITY
    )
    assert dimension.ratio == pytest.approx(1.0)
    assert dimension.points == pytest.approx(5.0)


def test_a_discovered_ir_url_counts_as_a_website(db):
    """Refusing it would penalise a company the platform researched
    successfully."""
    company = _company(db, "IRONLY", isin="I", sector="S", industry="I2")
    db.add(CompanyCrawlState(company_id=company.id,
                             ir_url="https://x.example/investors"))
    db.commit()

    dimension = next(
        d for d in DataQualityService(db).score_company(company).dimensions
        if d.dimension is Dimension.IDENTITY
    )
    assert dimension.ratio == pytest.approx(1.0)


def test_ten_year_history_is_proportional(db):
    company = _company(db, "HIST")
    for year in range(2022, 2027):          # five years
        db.add(FinancialFact(company_id=company.id, fiscal_year=year,
                             line_item="revenue", value=1.0, precedence=2))
    db.commit()

    check = next(
        c for d in DataQualityService(db).score_company(company).dimensions
        if d.dimension is Dimension.FINANCIAL_STATEMENTS
        for c in d.checks if c.key == "ten_year_history"
    )
    assert check.value == pytest.approx(0.5)


def test_ttm_needs_four_quarters(db):
    company = _company(db, "TTM")
    for quarter in (1, 2):
        db.add(QuarterlyResult(company_id=company.id, fiscal_year=2026,
                               quarter=quarter, revenue=1.0))
    db.commit()

    check = next(
        c for d in DataQualityService(db).score_company(company).dimensions
        if d.dimension is Dimension.FINANCIAL_STATEMENTS
        for c in d.checks if c.key == "ttm"
    )
    assert check.value == pytest.approx(0.5)


def test_a_stale_annual_report_scores_less_than_a_current_one(db):
    """"We hold an annual report" is not the same claim as "we hold a CURRENT
    one". A 2019 report does not describe the company being asked about."""
    fresh_co = _company(db, "FRESH")
    stale_co = _company(db, "STALE")
    _document(db, fresh_co, "annual_report", age_days=10)
    _document(db, stale_co, "annual_report", age_days=380)

    def annual(company):
        return next(
            c for d in DataQualityService(db).score_company(company).dimensions
            if d.dimension is Dimension.DOCUMENTS
            for c in d.checks if c.key == "latest_annual_report"
        ).value

    assert annual(fresh_co) > annual(stale_co)
    assert annual(stale_co) < 0.2


def test_no_documents_is_not_scored_as_a_pipeline_failure(db):
    """SYSTEM_HEALTH measures whether processing WORKED. Blaming the platform
    for a company nobody uploaded anything for would double-count the gap the
    DOCUMENTS dimension already records."""
    company = _company(db, "NODOCS")
    health = next(
        d for d in DataQualityService(db).score_company(company).dimensions
        if d.dimension is Dimension.SYSTEM_HEALTH
    )
    assert all(c.detail == "no documents held" for c in health.checks)


def test_system_health_reflects_processing_success(db):
    company = _company(db, "HEALTH")
    _document(db, company, "annual_report", status="completed")
    _document(db, company, "quarterly_report", status="failed")

    parsing = next(
        c for d in DataQualityService(db).score_company(company).dimensions
        if d.dimension is Dimension.SYSTEM_HEALTH
        for c in d.checks if c.key == "successful_parsing"
    )
    assert parsing.value == pytest.approx(0.5)


def test_score_is_the_weighted_sum_of_dimensions(db):
    company = _company(db, "SUM", isin="I", sector="S", industry="I2",
                       website="https://x.example")
    result = DataQualityService(db).score_company(company)
    assert result.score == pytest.approx(
        round(sum(d.points for d in result.dimensions), 1)
    )
    assert 0.0 <= result.score <= 100.0


def test_score_can_never_exceed_100(db):
    """Every proportional check is capped, so an unusually well-covered
    company cannot overflow the scale."""
    company = _company(db, "RICH", isin="I", sector="S", industry="I2",
                       website="https://x.example", current_price=100.0)
    for index in range(30):
        _document(db, company, "annual_report", age_days=1)
    for year in range(2000, 2027):
        db.add(FinancialFact(company_id=company.id, fiscal_year=year,
                             line_item="revenue", value=1.0, precedence=2))
    db.commit()

    assert DataQualityService(db).score_company(company).score <= 100.0


# ============================================================ missing panel

def test_missing_items_are_named_not_counted(db):
    company = _company(db, "MISS")
    result = DataQualityService(db).score_company(company)

    assert result.missing_items
    assert all(item.startswith(("Missing", "No", "Low", "Document",
                                "Field", "Embeddings", "Retrieval"))
               for item in result.missing_items)
    assert "Missing latest annual report" in result.missing_items


def test_a_partially_satisfied_check_is_not_reported_missing(db):
    """A company with two of four quarters has not got "missing quarterly
    results" — it has incomplete ones, and the ratio already says so."""
    company = _company(db, "PARTIAL")
    for quarter in (1, 2):
        db.add(QuarterlyResult(company_id=company.id, fiscal_year=2026,
                               quarter=quarter, revenue=1.0))
    db.commit()

    result = DataQualityService(db).score_company(company)
    assert "Missing quarterly results" not in result.missing_items


def test_explanation_states_both_strengths_and_gaps(db):
    company = _company(db, "EXPL")
    lines = DataQualityService(db).score_company(company).explanation()
    assert lines
    assert any("annual report" in line.lower() for line in lines)


# =================================================================== warning

def test_below_70_triggers_a_warning(db):
    company = _company(db, "WARN")
    result = DataQualityService(db).score_company(company)
    assert result.score < WARN_BELOW
    assert result.needs_warning is True


def test_the_threshold_is_the_brief_s():
    assert WARN_BELOW == 70.0


# ================================================================ snapshots

def test_refresh_persists_and_is_idempotent(db):
    company = _company(db, "SNAP")
    service = QualitySnapshotService(db)
    service.refresh(company)
    service.refresh(company)

    rows = db.execute(select(DataQualitySnapshot)).scalars().all()
    assert len(rows) == 1, "a second refresh created a duplicate row"
    assert rows[0].grade == grade_for(rows[0].score).value


def test_snapshot_stores_per_dimension_points(db):
    company = _company(db, "DIMS", isin="I", sector="S", industry="I2",
                       website="https://x.example")
    QualitySnapshotService(db).refresh(company)

    row = db.scalar(select(DataQualitySnapshot))
    assert row.identity_points == pytest.approx(5.0)
    assert row.missing_count == len(row.missing_items)


def test_dashboard_reports_thresholds_and_leaderboards(db):
    for ticker, isin in [("HI", "I1"), ("LO", None)]:
        company = _company(db, ticker, isin=isin, sector="S", industry="I")
        QualitySnapshotService(db).refresh(company)

    board = QualitySnapshotService(db).dashboard()
    assert board["companies_scored"] == 2
    assert set(board["above"]) == {"90", "80", "70", "60"}
    assert board["highest"][0]["score"] >= board["lowest"][0]["score"]
    assert board["weights"]["documents"] == 20


def test_dashboard_explains_an_empty_universe(db):
    board = QualitySnapshotService(db).dashboard()
    assert board["companies_scored"] == 0
    assert board["unavailable_reason"]


def test_a_failing_company_does_not_stop_the_sweep(db, monkeypatch):
    _company(db, "OK1")
    _company(db, "BAD")
    service = QualitySnapshotService(db)
    original = service.scorer.score_company

    def flaky(company):
        if company.ticker == "BAD":
            raise RuntimeError("scorer exploded")
        return original(company)

    monkeypatch.setattr(service.scorer, "score_company", flaky)
    report = service.refresh_all()

    assert report["scored"] == 1
    assert report["failed"] == 1


# =============================================================== automation

def test_quality_refresh_is_registered_and_scheduled():
    """JOB-001: a kind missing from DEFAULT_PRIORITY raises on enqueue."""
    kind = JobKind.QUALITY_REFRESH
    assert kind in JOB_LABELS
    assert kind in DEFAULT_PRIORITY
    assert kind in RETRY_POLICIES
    assert any(s.kind == kind for s in SCHEDULES)

    from app.services.platform.jobs.handlers import handler_for
    assert handler_for(kind) is not None


def test_enrichment_refreshes_the_score(db):
    """The brief's "no manual updates": a document arriving must raise the
    score without anyone asking it to."""
    from app.services.knowledge.enrichment import MemoryEnrichmentService

    company = _company(db, "AUTO")
    MemoryEnrichmentService(db, allow_llm=False).run(company.id)

    row = db.scalar(select(DataQualitySnapshot).where(
        DataQualitySnapshot.company_id == company.id))
    assert row is not None, "enrichment did not refresh the quality score"


def test_a_scoring_failure_does_not_break_enrichment(db, monkeypatch):
    from app.services.knowledge import enrichment as module
    from app.services.quality import service as quality_module

    company = _company(db, "SAFE")

    def boom(self, company):
        raise RuntimeError("scorer down")

    monkeypatch.setattr(quality_module.QualitySnapshotService, "refresh", boom)
    result = module.MemoryEnrichmentService(db, allow_llm=False).run(company.id)

    # The pass still completed every stage it was asked to.
    assert result.stages


# ============================================================== AI response

def test_ai_responses_carry_data_quality(db):
    from app.schemas.ai import AnalysisResponse, DataQualityOut

    assert "data_quality" in AnalysisResponse.model_fields
    payload = DataQualityOut(score=42.0, grade="D", warning="incomplete")
    assert payload.score == 42.0
    assert payload.missing_items == []
