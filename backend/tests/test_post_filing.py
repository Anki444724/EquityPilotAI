"""Integration and orchestration tests for PostFilingProcessor.

Verifies post-filing scoring, classification, notifications, continuous learning triggers,
failure recovery, and retrieval of documents awaiting post-processing.
"""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch
from sqlalchemy import select, func

from app.db.base import Base
from app.core.config import settings
from app.models.company import Company
from app.models.document import Document, DocumentFact
from app.models.filing_collection import DiscoveredFiling
from app.models.platform import Tenant, User, Notification
from app.models.portfolio import Watchlist, WatchlistEntry
from app.services.filings.post_filing import (
    PostFilingProcessor, PostFilingResult, ScoreDelta,
    documents_awaiting_post_processing, TRACKED_DIMENSIONS
)

@pytest.fixture
def db_session_isolated():
    """Builds an isolated SQLite in-memory session for test cases."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    import importlib
    import pkgutil

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    # Register all models
    import app.models as _models
    for _module in pkgutil.iter_modules(_models.__path__):
        importlib.import_module(f"app.models.{_module.name}")

    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with Session() as session:
        yield session


def test_score_delta_properties():
    """Verifies properties of ScoreDelta."""
    delta = ScoreDelta(dimension="valuation", before=10.0, after=12.5)
    assert delta.change == 2.5
    assert delta.is_material is True

    immaterial = ScoreDelta(dimension="growth", before=10.0, after=10.2)
    assert immaterial.change == 0.2
    assert immaterial.is_material is False

    null_delta = ScoreDelta(dimension="risk", before=None, after=10.0)
    assert null_delta.change is None
    assert null_delta.is_material is False


@patch("app.services.analysis_service.AnalysisService")
@patch("app.services.scoring.service.ScoringService")
@patch("app.services.ai_scoring.service.AIScoringService")
@patch("app.services.ai.service.AIService")
@patch("app.services.knowledge.enrichment.MemoryEnrichmentService")
def test_successful_orchestration_and_notifications(
    mock_memory_service, mock_ai_service, mock_ai_scoring_service, mock_scoring_service, mock_analysis_service,
    db_session_isolated
):
    """Orchestrates successful post-filing flow with rescoring, AI summary, and material change notifications."""
    # Seed Company, Tenant, User and Watchlist
    company = Company(id="company-1", name="Bharat Corp", ticker="BHARAT")
    tenant = Tenant(id=1, slug="demo", name="Demo Org")
    user = User(id="user-1", email="test@localhost", name="Test User", role="admin", tenant_id=tenant.id)
    watchlist = Watchlist(id=1, owner_id="user-1", name="My Watchlist")
    watchlist_entry = WatchlistEntry(watchlist_id=1, company_id="company-1", ticker="BHARAT")
    document = Document(
        id=10, company_id="company-1", filename="fy25.pdf", doc_type="annual_report",
        file_format="pdf", content_hash="hash123", status="completed"
    )

    db_session_isolated.add_all([company, tenant, user, watchlist, watchlist_entry, document])
    db_session_isolated.commit()

    # Setup Mocks
    mock_analysis_service.for_ticker.return_value = MagicMock()
    
    # Mock institutional score
    mock_scored = MagicMock()
    mock_scored.overall_score = 75.0
    mock_scored.grade = "A"
    cat1 = MagicMock(key="business_quality", score_pct=0.8)
    cat2 = MagicMock(key="valuation", score_pct=0.7)
    mock_scored.categories = [cat1, cat2]
    mock_scoring_service.return_value.score_company.return_value = mock_scored

    # Mock AI score 3.0
    mock_ai_scoring = MagicMock()
    mock_ai_scoring.overall_score = 82.5
    mock_ai_scoring.rating = MagicMock(value="buy")
    mock_ai_scoring.recommendation = MagicMock(value="strong_buy")
    
    mock_outcome = MagicMock()
    mock_outcome.version.version = 2
    mock_outcome.created = True
    
    mock_ai_scoring_service.return_value.current.return_value = None
    mock_ai_scoring_service.return_value.score_and_record.return_value = (mock_ai_scoring, mock_outcome)

    # Mock AI summary
    mock_analyst = MagicMock()
    async def fake_run(*args, **kwargs):
        return type("Answer", (), {"content": "This is a great annual report summary."})()
    mock_analyst.run = fake_run
    mock_ai_service.return_value.analyst_for.return_value = mock_analyst

    # Mock continuous learning
    mock_memory_service.return_value.run.return_value = type("Enr", (), {"stages": [1, 2], "written": 12})()

    # Previous snapshot
    previous_snapshot = {
        "overall": 70.0,
        "grade": "B",
        "dimensions": {
            "business_quality": 60.0,
            "valuation": 70.0
        }
    }

    # Run processor
    processor = PostFilingProcessor(db_session_isolated)
    result = processor.run(
        company_id="company-1",
        document_id=10,
        previous=previous_snapshot,
        notify=True
    )

    # Assertions on PostFilingResult
    assert result.company_id == "company-1"
    assert result.ticker == "BHARAT"
    assert result.overall_before == 70.0
    assert result.overall_after == 75.0
    assert result.overall_change == 5.0
    assert result.grade_before == "B"
    assert result.grade_after == "A"
    assert result.rescored is True
    assert result.is_material is True
    assert result.ai_score_after == 82.5
    assert result.ai_version == 2

    # Check notification persisted in DB
    notifications = db_session_isolated.scalars(select(Notification)).all()
    assert len(notifications) == 1
    assert notifications[0].user_id == "user-1"
    assert "Bharat Corp" in notifications[0].subject
    assert "business_quality" in result.as_dict()["deltas"][0]["dimension"]


def test_idempotency_and_immaterial_change(db_session_isolated):
    """Verifies that immaterial score changes do not trigger notifications."""
    company = Company(id="company-2", name="RIL", ticker="RELIANCE")
    tenant = Tenant(id=2, slug="demo2", name="Demo Org 2")
    user = User(id="user-2", email="user2@localhost", name="User 2", role="admin", tenant_id=tenant.id)
    watchlist = Watchlist(id=2, owner_id="user-2", name="Reliance Watch")
    watchlist_entry = WatchlistEntry(watchlist_id=2, company_id="company-2", ticker="RELIANCE")

    db_session_isolated.add_all([company, tenant, user, watchlist, watchlist_entry])
    db_session_isolated.commit()

    # If scores are identical/immaterial (change < 0.5)
    previous_snapshot = {
        "overall": 75.0,
        "grade": "A",
        "dimensions": {
            "business_quality": 80.0
        }
    }

    with patch("app.services.analysis_service.AnalysisService") as mock_analysis, \
         patch("app.services.scoring.service.ScoringService") as mock_scoring, \
         patch("app.services.ai_scoring.service.AIScoringService") as mock_ai_scoring:
        
        mock_analysis.for_ticker.return_value = MagicMock()
        
        # Scoring returns 75.2 (diff is 0.2, immaterial)
        mock_scored = MagicMock()
        mock_scored.overall_score = 75.2
        mock_scored.grade = "A"
        mock_scored.categories = [MagicMock(key="business_quality", score_pct=0.8)]
        mock_scoring.return_value.score_company.return_value = mock_scored

        mock_ai_scoring.return_value.current.return_value = None
        mock_ai_scoring.return_value.score_and_record.side_effect = Exception("No AI score recalculated")

        processor = PostFilingProcessor(db_session_isolated)
        result = processor.run(
            company_id="company-2",
            previous=previous_snapshot,
            notify=True
        )

        assert result.is_material is False
        assert result.notified == 0

        # Verify no notification created
        notif_count = db_session_isolated.scalar(select(func.count(Notification.id)))
        assert notif_count == 0


def test_failure_recovery_and_resiliency(db_session_isolated):
    """Verifies failure recovery: exceptions during scoring or AI scoring do not crash the pipeline."""
    company = Company(id="company-3", name="Infy", ticker="INFY")
    db_session_isolated.add(company)
    db_session_isolated.commit()

    with patch("app.services.analysis_service.AnalysisService") as mock_analysis, \
         patch("app.services.scoring.service.ScoringService") as mock_scoring, \
         patch("app.services.ai_scoring.service.AIScoringService") as mock_ai_scoring:

        mock_analysis.for_ticker.return_value = MagicMock()
        
        # Scoring throws exception
        mock_scoring.return_value.score_company.side_effect = RuntimeError("Database timeout during rescore")
        
        # AI Scoring throws exception
        mock_ai_scoring.return_value.current.return_value = None
        mock_ai_scoring.return_value.score_and_record.side_effect = ValueError("AI pricing API disconnected")

        processor = PostFilingProcessor(db_session_isolated)
        
        # Pipeline must run without raising exceptions
        result = processor.run(company_id="company-3", notify=False)

        assert result.rescored is False
        assert len(result.warnings) >= 2
        assert "scores could not be recomputed" in result.warnings
        assert any("AI score 3.0 not recalculated" in w for w in result.warnings)


def test_highlights_extraction_persistence(db_session_isolated):
    """Verifies formatting of highlights from DocumentFact rows."""
    company = Company(id="company-4", name="Wipro", ticker="WIPRO")
    document = Document(
        id=20, company_id="company-4", filename="wipro.pdf", doc_type="annual_report",
        file_format="pdf", content_hash="hash222", status="completed"
    )
    fact1 = DocumentFact(
        document_id=20, company_id="company-4", category="ratios", field_key="roe",
        label="Return on Equity", value=18.5, unit="%"
    )
    fact2 = DocumentFact(
        document_id=20, company_id="company-4", category="ratios", field_key="debt_to_equity",
        label="Debt to Equity", value=0.12, unit="x"
    )

    db_session_isolated.add_all([company, document, fact1, fact2])
    db_session_isolated.commit()

    processor = PostFilingProcessor(db_session_isolated)
    highlights = processor._highlights(document_id=20)

    assert len(highlights) == 2
    assert "Return on Equity: 18.5 %" in highlights
    assert "Debt to Equity: 0.12 x" in highlights


def test_documents_awaiting_post_processing(db_session_isolated):
    """Checks queries for filings with completed/ready status awaiting post processing."""
    company = Company(id="company-5", name="TCS", ticker="TCS")
    db_session_isolated.add(company)
    db_session_isolated.commit()

    # Awaiting post-processing: status='embedding', document status='completed'
    doc1 = Document(
        id=101, company_id="company-5", filename="doc1.pdf", doc_type="annual_report",
        file_format="pdf", content_hash="hash301", status="completed"
    )
    filing1 = DiscoveredFiling(
        id=201, company_id="company-5", document_id=101,
        source="NSE Corporate Filings", source_reference="ref1",
        status="embedding"
    )

    # Not awaiting: status='completed', document status='completed'
    doc2 = Document(
        id=102, company_id="company-5", filename="doc2.pdf", doc_type="annual_report",
        file_format="pdf", content_hash="hash302", status="completed"
    )
    filing2 = DiscoveredFiling(
        id=202, company_id="company-5", document_id=102,
        source="NSE Corporate Filings", source_reference="ref2",
        status="completed"
    )

    # Not awaiting: status='embedding', document status='processing'
    doc3 = Document(
        id=103, company_id="company-5", filename="doc3.pdf", doc_type="annual_report",
        file_format="pdf", content_hash="hash303", status="processing"
    )
    filing3 = DiscoveredFiling(
        id=203, company_id="company-5", document_id=103,
        source="NSE Corporate Filings", source_reference="ref3",
        status="embedding"
    )

    db_session_isolated.add_all([doc1, filing1, doc2, filing2, doc3, filing3])
    db_session_isolated.commit()

    awaiting = documents_awaiting_post_processing(db_session_isolated)
    assert len(awaiting) == 1
    assert awaiting[0].id == 201


@patch("app.services.analysis_service.AnalysisService")
@patch("app.services.ai.service.AIService")
@patch("app.services.knowledge.enrichment.MemoryEnrichmentService")
@patch("app.services.documents.conference_call.extract_conference_call_insights")
@patch("app.services.documents.investor_presentation.extract_presentation_insights")
def test_continuous_learning_on_specialized_documents(
    mock_pres_insights, mock_cc_insights, mock_memory_service, mock_ai_service, mock_analysis_service,
    db_session_isolated
):
    """Verifies Continuous Learning orchestrates conference call and investor presentation intelligence correctly."""
    company = Company(id="company-6", name="Tata Motors", ticker="TATAMOTORS")
    doc_cc = Document(
        id=50, company_id="company-6", filename="call.txt", doc_type="conference_call",
        file_format="txt", content_hash="hash_cc", status="completed"
    )
    doc_pres = Document(
        id=51, company_id="company-6", filename="pres.pdf", doc_type="investor_presentation",
        file_format="pdf", content_hash="hash_pres", status="completed"
    )

    db_session_isolated.add_all([company, doc_cc, doc_pres])
    db_session_isolated.commit()

    # Mocks
    mock_analysis_service.for_ticker.return_value = MagicMock()
    mock_memory_service.return_value.run.return_value = type("Enr", (), {"stages": [], "written": 0})()
    
    # Mock AI Service record
    mock_ai_service_inst = MagicMock()
    mock_ai_service.return_value = mock_ai_service_inst

    # Specialized Extraction outputs
    mock_cc_insights.return_value = {"available": True, "speaker_id": "CEO", "sentiment": "positive"}
    mock_pres_insights.return_value = {"available": True, "kpi": "margins improved"}

    processor = PostFilingProcessor(db_session_isolated)

    # 1. Test conference call triggers continuous learning
    result_cc = PostFilingResult(company_id="company-6", ticker="TATAMOTORS")
    processor._trigger_continuous_learning(company, document_id=50, result=result_cc)
    assert any("Conference Call Intelligence" in h for h in result_cc.highlights)
    assert mock_cc_insights.called

    # 2. Test investor presentation triggers continuous learning
    result_pres = PostFilingResult(company_id="company-6", ticker="TATAMOTORS")
    processor._trigger_continuous_learning(company, document_id=51, result=result_pres)
    assert any("Investor Presentation Intelligence" in h for h in result_pres.highlights)
    assert mock_pres_insights.called
