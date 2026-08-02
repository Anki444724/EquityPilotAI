"""Automatic memory enrichment — the path from ingestion to permanent memory.

These assert the wiring the audit found missing: that a completed document
enqueues a pass, that the pass runs every stage, that one failing stage does
not stop the others, and that extracted figures cannot displace filed ones.

LLM-backed stages are stubbed. The point is the orchestration, not a
provider's uptime.
"""

from __future__ import annotations

import importlib
import pkgutil
import uuid

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models as _models_pkg
from app.db.base import Base
from app.domain.knowledge.enrichment import (
    LLM_STAGES, MIN_PROMOTION_CONFIDENCE, PROMOTABLE_FIELDS, STAGE_ORDER,
    EnrichmentStage, should_promote,
)
from app.domain.platform.jobs import (
    DEFAULT_PRIORITY, JOB_LABELS, RETRY_POLICIES, JobKind,
)
from app.models.company import Company, FinancialFact
from app.models.document import Document, DocumentFact
from app.models.knowledge import KnowledgeEntry, YearlyObservation
from app.services.knowledge.enrichment import (
    EXTRACTION_PRECEDENCE, MemoryEnrichmentService,
    companies_needing_enrichment,
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


@pytest.fixture()
def company(db):
    row = Company(
        id=str(uuid.uuid4()), name="Test Ltd.", ticker="TEST",
        exchange="NSE", listing_status="active",
    )
    db.add(row)
    db.commit()
    return row


def _document(db, company, *, status="completed", doc_id=None):
    row = Document(
        id=doc_id, company_id=company.id, filename="f.pdf", title="f.pdf",
        doc_type="annual_report", file_format="pdf", size_bytes=1,
        status=status, content_hash=uuid.uuid4().hex,
    )
    db.add(row)
    db.commit()
    return row


# ------------------------------------------------------------ registration

def test_job_kind_is_registered_in_every_registry():
    """JOB-001 was caused by registering a kind in three of four registries;
    the missing DEFAULT_PRIORITY entry raised KeyError on enqueue."""
    kind = JobKind.MEMORY_ENRICHMENT
    assert kind in JOB_LABELS
    assert kind in DEFAULT_PRIORITY
    assert kind in RETRY_POLICIES


def test_handler_is_reachable():
    from app.services.platform.jobs.handlers import handler_for

    assert handler_for(JobKind.MEMORY_ENRICHMENT) is not None


# --------------------------------------------------------------- promotion

def test_only_stored_line_items_are_promotable():
    """Derived quantities must never be written as facts.

    A first draft of this map named `pat`, `cfo` and `net_worth` — none of
    which exist as canonical line items, because the statement builders
    compute them. Writing them would create a second source of truth.
    """
    from app.domain.financials.line_items import LineItem

    canonical = {item.value for item in LineItem}
    for field_key, line_item in PROMOTABLE_FIELDS.items():
        assert line_item in canonical, (
            f"{field_key} maps to '{line_item}', which is not a canonical "
            f"line item"
        )


def test_low_confidence_extractions_are_not_promoted():
    assert should_promote(0.95, "revenue") is True
    assert should_promote(MIN_PROMOTION_CONFIDENCE - 0.01, "revenue") is False


def test_derived_fields_are_refused_even_at_high_confidence():
    assert should_promote(1.0, "ebitda") is False
    assert should_promote(1.0, "free_cash_flow") is False


def test_promotion_writes_below_the_filed_tier(db, company):
    """A regex reading of a PDF must never displace an audited figure."""
    document = _document(db, company)
    db.add(FinancialFact(
        company_id=company.id, fiscal_year=2026, line_item="revenue",
        value=1000.0, precedence=2, source="screener.in",
    ))
    db.add(DocumentFact(
        document_id=document.id, company_id=company.id,
        category="FINANCIAL", field_key="revenue", label="Revenue",
        value=999.0, fiscal_year=2026, confidence=0.95,
    ))
    db.commit()

    MemoryEnrichmentService(db, allow_llm=False).run(company.id)

    filed = db.scalar(select(FinancialFact).where(
        FinancialFact.company_id == company.id,
        FinancialFact.precedence == 2,
    ))
    assert filed.value == 1000.0, "an extracted figure overwrote a filed one"

    extracted = db.scalar(select(FinancialFact).where(
        FinancialFact.company_id == company.id,
        FinancialFact.precedence == EXTRACTION_PRECEDENCE,
    ))
    assert extracted is not None
    assert extracted.value == 999.0
    assert EXTRACTION_PRECEDENCE > 2, "extraction must rank below filed data"


def test_promotion_is_idempotent(db, company):
    document = _document(db, company)
    db.add(DocumentFact(
        document_id=document.id, company_id=company.id,
        category="FINANCIAL", field_key="capex", label="Capex",
        value=50.0, fiscal_year=2026, confidence=0.9,
    ))
    db.commit()

    service = MemoryEnrichmentService(db, allow_llm=False)
    service.run(company.id)
    service.run(company.id)

    rows = db.execute(select(FinancialFact).where(
        FinancialFact.company_id == company.id,
    )).scalars().all()
    assert len(rows) == 1, "a second pass duplicated the promoted fact"

    fact = db.scalar(select(DocumentFact))
    assert fact.promoted is True


def test_low_confidence_facts_are_marked_examined(db, company):
    """Otherwise they are re-examined on every future pass forever."""
    document = _document(db, company)
    db.add(DocumentFact(
        document_id=document.id, company_id=company.id,
        category="FINANCIAL", field_key="revenue", label="Revenue",
        value=1.0, fiscal_year=2026, confidence=0.2,
    ))
    db.commit()

    MemoryEnrichmentService(db, allow_llm=False).run(company.id)

    assert db.scalar(select(DocumentFact)).promoted is True
    assert db.execute(select(FinancialFact)).scalars().all() == []


# ------------------------------------------------------------ orchestration

def test_every_declared_stage_runs(db, company):
    _document(db, company)
    result = MemoryEnrichmentService(db, allow_llm=False).run(company.id)

    ran = [s.stage for s in result.stages]
    assert ran == list(STAGE_ORDER), "a declared stage did not run"


def test_llm_stages_are_skipped_when_disabled(db, company):
    _document(db, company)
    result = MemoryEnrichmentService(db, allow_llm=False).run(company.id)

    for outcome in result.stages:
        if outcome.stage in LLM_STAGES:
            assert outcome.skipped is True
            assert outcome.detail


def test_one_failing_stage_does_not_stop_the_pass(db, company, monkeypatch):
    """A rate-limited summariser must not prevent the vault from updating."""
    _document(db, company)
    service = MemoryEnrichmentService(db, allow_llm=False)

    def boom(_company_id):
        raise RuntimeError("vault exploded")

    monkeypatch.setattr(service, "_build_vault", boom)
    result = service.run(company.id)

    assert len(result.stages) == len(STAGE_ORDER)
    failed = result.failed_stages
    assert len(failed) == 1
    assert failed[0].stage == EnrichmentStage.VAULT
    assert "vault exploded" in failed[0].detail


def test_stage_order_puts_dependencies_first():
    """Summaries feed observations; observations feed AI notes' source data.

    Asserted as an ordering rather than a comment, because a reordering that
    breaks a dependency is silent — each stage still 'succeeds', on stale or
    absent inputs.
    """
    order = list(STAGE_ORDER)
    assert order.index(EnrichmentStage.FINANCIAL_PROMOTION) < order.index(
        EnrichmentStage.OBSERVATIONS
    ), "observations read promoted financials as corroboration"
    assert order.index(EnrichmentStage.SUMMARIES) < order.index(
        EnrichmentStage.OBSERVATIONS
    ), "observations read summaries as their primary evidence"
    assert order.index(EnrichmentStage.OBSERVATIONS) < order.index(
        EnrichmentStage.TEMPORAL_LINK
    ), "the link stage re-judges an observation that must already exist"


def test_knowledge_graph_is_not_re_run():
    """The graph is updated synchronously during ingestion, where a repeated
    edge is MERGED and its weight incremented. Re-running it in the
    enrichment pass would double every weight on every upload."""
    assert not any("graph" in stage.value for stage in STAGE_ORDER)


# ---------------------------------------------------------------- AI notes

def test_ai_notes_are_written_from_observations(db, company):
    """The audit found VaultSection.AI_NOTES declared with no producer and
    zero rows in production."""
    db.add(YearlyObservation(
        company_id=company.id, fiscal_year=2026, status="current",
        confidence=0.9, is_fallback=False,
        findings="Capex rising.\nDebt reducing.",
        generated_by="test:model",
    ))
    db.commit()

    service = MemoryEnrichmentService(db, allow_llm=True)
    outcome = service._write_ai_notes(company.id)  # noqa: SLF001

    assert outcome.written == 1
    entry = db.scalar(select(KnowledgeEntry).where(
        KnowledgeEntry.section == "ai_notes",
    ))
    assert entry is not None
    assert "Capex rising" in entry.value_text
    assert entry.fiscal_year == 2026


def test_fallback_observations_never_become_ai_notes(db, company):
    """Template prose must not be recorded as something the AI concluded."""
    db.add(YearlyObservation(
        company_id=company.id, fiscal_year=2026, status="current",
        confidence=0.9, is_fallback=True, findings="Template prose.",
    ))
    db.commit()

    outcome = MemoryEnrichmentService(db)._write_ai_notes(company.id)  # noqa: SLF001

    assert outcome.skipped is True
    assert db.execute(select(KnowledgeEntry)).scalars().all() == []


# ---------------------------------------------------------------- sweeping

def test_sweep_finds_companies_whose_documents_outran_their_memory(db, company):
    """The audit's headline number came from exactly this comparison."""
    import datetime as dt

    entry = KnowledgeEntry(
        company_id=company.id, section="risks", key="r1", label="R",
        value_text="old", confidence=0.8, version=1, status="current",
    )
    entry.created_at = dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc)
    db.add(entry)
    db.commit()

    document = _document(db, company)
    document.created_at = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    db.commit()

    assert company.id in companies_needing_enrichment(db)


def test_a_company_whose_memory_is_current_is_not_swept(db, company):
    import datetime as dt

    document = _document(db, company)
    document.created_at = dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc)
    entry = KnowledgeEntry(
        company_id=company.id, section="risks", key="r1", label="R",
        value_text="fresh", confidence=0.8, version=1, status="current",
    )
    entry.created_at = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    db.add(entry)
    db.commit()

    assert company.id not in companies_needing_enrichment(db)


# ------------------------------------------------------- evidence ordering

def test_knowledge_is_rendered_before_documents():
    """The brief requires the AI to answer PRIMARILY from memory."""
    from app.domain.ai.types import Citation, EvidenceKind
    from app.services.ai.context_builder import GroundedContext

    context = GroundedContext(company_id="c1", ticker="TEST", name="Test Ltd.")
    context.add(Citation(
        key="chunk", label="[Doc] page 4", kind=EvidenceKind.DOCUMENT,
        value="raw text from a filing",
    ))
    context.add(Citation(
        key="vault", label="[Vault/risks] Principal risk",
        kind=EvidenceKind.KNOWLEDGE, value="a verified assertion",
    ))

    rendered = context.render_evidence()
    assert rendered.index("KNOWLEDGE") < rendered.index("DOCUMENT")
    assert "EVIDENCE PRECEDENCE" in rendered
    assert "prefer memory" in rendered


def test_precedence_note_is_absent_when_there_is_nothing_to_prefer():
    """An instruction to prefer memory is noise when no memory is present."""
    from app.domain.ai.types import Citation, EvidenceKind
    from app.services.ai.context_builder import GroundedContext

    context = GroundedContext(company_id="c1", ticker="TEST", name="Test Ltd.")
    context.add(Citation(
        key="chunk", label="[Doc] page 4", kind=EvidenceKind.DOCUMENT,
        value="raw text",
    ))
    assert "EVIDENCE PRECEDENCE" not in context.render_evidence()


# ----------------------------------------------------- the trigger itself

def test_completing_a_document_enqueues_enrichment(db, company):
    """The single wire that converts retrieval into memory.

    Asserted against the queue rather than by reading the source, because the
    failure this guards against — nothing calls the memory services — is
    invisible in code review and was live in production for weeks.
    """
    from app.models.platform import BackgroundJob
    from app.services.documents.ingestion import DocumentIngestionService

    document = _document(db, company)
    service = DocumentIngestionService(db)
    service._enqueue_enrichment(document)  # noqa: SLF001

    job = db.scalar(select(BackgroundJob).where(
        BackgroundJob.kind == JobKind.MEMORY_ENRICHMENT.value,
    ))
    assert job is not None, "a completed document scheduled no memory work"
    assert job.payload["company_id"] == company.id
    # The document id lives on the resource fields, NOT in the payload: the
    # dedup key is the payload, so a per-document payload would defeat the
    # burst collapsing asserted below.
    assert job.resource_id == str(document.id)


def test_a_burst_of_documents_collapses_into_one_pass(db, company):
    """A filing crawl delivers twenty documents for one company in a minute.

    Twenty vault rebuilds and twenty observation regenerations would be twenty
    times the cost for one outcome.
    """
    from app.models.platform import BackgroundJob
    from app.services.documents.ingestion import DocumentIngestionService

    service = DocumentIngestionService(db)
    for _ in range(5):
        service._enqueue_enrichment(_document(db, company))  # noqa: SLF001

    jobs = db.execute(select(BackgroundJob).where(
        BackgroundJob.kind == JobKind.MEMORY_ENRICHMENT.value,
    )).scalars().all()
    assert len(jobs) == 1, f"burst produced {len(jobs)} jobs, expected 1"


def test_enrichment_is_debounced_not_immediate(db, company):
    """Scheduled slightly ahead so a crawl finishes before the pass starts."""
    from datetime import datetime, timezone

    from app.domain.knowledge.enrichment import DEBOUNCE_SECONDS
    from app.models.platform import BackgroundJob
    from app.services.documents.ingestion import DocumentIngestionService

    DocumentIngestionService(db)._enqueue_enrichment(  # noqa: SLF001
        _document(db, company))

    job = db.scalar(select(BackgroundJob).where(
        BackgroundJob.kind == JobKind.MEMORY_ENRICHMENT.value,
    ))
    run_after = job.run_after
    if run_after.tzinfo is None:
        run_after = run_after.replace(tzinfo=timezone.utc)
    delay = (run_after - datetime.now(timezone.utc)).total_seconds()
    assert 0 < delay <= DEBOUNCE_SECONDS + 5


def test_a_document_with_no_company_schedules_nothing(db, company):
    from app.models.platform import BackgroundJob
    from app.services.documents.ingestion import DocumentIngestionService

    # `company_id` is NOT NULL in the schema, so this state cannot be
    # persisted; a transient object is the only way the guard is reachable.
    orphan = Document(
        id=9999, company_id=None, filename="x.pdf", title="x.pdf",
        doc_type="other", file_format="pdf", size_bytes=1,
        status="completed", content_hash=uuid.uuid4().hex,
    )
    DocumentIngestionService(db)._enqueue_enrichment(orphan)  # noqa: SLF001
    assert db.execute(select(BackgroundJob)).scalars().all() == []


def test_a_failed_enqueue_never_fails_the_document(db, company, monkeypatch):
    """A parsed, chunked, searchable document is a success.

    Marking it failed because scheduling hiccuped would send it back through
    the pipeline — the reprocessing loop is worse than the missed pass, which
    the sweep picks up anyway.
    """
    from app.services.documents.ingestion import DocumentIngestionService
    from app.services.platform.jobs import queue as queue_module

    def boom(*_a, **_k):
        raise RuntimeError("queue unavailable")

    monkeypatch.setattr(queue_module.JobQueue, "enqueue", boom)

    document = _document(db, company)
    DocumentIngestionService(db)._enqueue_enrichment(document)  # noqa: SLF001
    assert document.status == "completed"
