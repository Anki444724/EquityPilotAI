"""Part 3 Phase 4B — WebQueryGenerator + SelfOwnedWebIndex.

Two components, one contract: a ``WEB_RESEARCH`` plan becomes a bounded,
deterministic set of search strings (the generator), and those strings are
answered from the web pages the platform has *already stored* (the index).
Nothing here reaches the network, calls a provider, or ranks a company.

Sections
    A  query generation      G  dedupe
    B  index scope           H  ranking (authority / freshness / relevance)
    C  lexical search        I  missing metadata is reported, never invented
    D  semantic when present J  no provider, no network
    E  lexical fallback      K  architecture (static + subprocess)
    F  company scoping
"""
from __future__ import annotations

import ast
import hashlib
import importlib
import inspect
import json
import pkgutil
import re
import socket
import subprocess
import sys
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.domain.retrieval.types import RetrievalResult
from app.domain.web.types import WebSourceClass
from app.models.company import Company
from app.models.document import Document, DocumentChunk
from app.services.ai.planner import (
    EntityResolution, EntityStatus, ExecutionRoute, QuestionPlanner,
    WebQuery, WebQueryGenerator, WebQueryLimits, WebQuerySet, WebQueryStatus,
    is_web_research,
)
from app.services.documents.pipeline.embeddings import HashingEmbeddingProvider
from app.services.retrieval.engine import HybridRetrievalEngine
from app.services.web import index as index_module
from app.services.web.index import (
    INDEXED_STATUSES, SEMANTIC_ONLY_FLOOR, WEB_PAGE_DOC_TYPE,
    SelfOwnedWebIndex, WebEvidenceCandidate, WebIndexLimits,
    WebIndexSearchResult,
)
from app.services.web.quality import web_authority

BACKEND = Path(__file__).resolve().parent.parent
APP = BACKEND / "app"
WEB_QUERY_PATH = APP / "services" / "ai" / "planner" / "web_query.py"
INDEX_PATH = APP / "services" / "web" / "index.py"
ENGINE_PATH = APP / "services" / "retrieval" / "engine.py"

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)


# ===========================================================================
# Shared helpers
# ===========================================================================
@dataclass(frozen=True)
class _Co:
    id: str
    ticker: str
    name: str


UNIVERSE = (
    _Co("c-jsw", "JSWSTEEL", "JSW Steel Limited"),
    _Co("c-tcs", "TCS", "Tata Consultancy Services Limited"),
    _Co("c-rel", "RELIANCE", "Reliance Industries Limited"),
)


def _resolver(text: str) -> list[_Co]:
    lowered = (text or "").lower()
    found = []
    for co in UNIVERSE:
        first = co.name.lower().split()[0]
        if co.ticker.lower() in lowered or first in lowered:
            found.append(co)
    if "रिलायंस" in (text or ""):
        found.append(UNIVERSE[2])
    return found


def _nothing(_text: str) -> list[_Co]:
    return []


@pytest.fixture()
def planner() -> QuestionPlanner:
    return QuestionPlanner(company_resolver=_resolver)


@pytest.fixture()
def blind_planner() -> QuestionPlanner:
    return QuestionPlanner(company_resolver=_nothing)


@pytest.fixture()
def generator() -> WebQueryGenerator:
    return WebQueryGenerator()


def _plan_and_generate(planner, generator, question):
    plan = planner.plan(question)
    return plan, generator.generate(plan)


@pytest.fixture()
def no_network(monkeypatch):
    """Every socket constructor raises: any network attempt fails loudly."""
    def _refuse(*_a, **_k):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", _refuse)
    monkeypatch.setattr(socket, "create_connection", _refuse)
    monkeypatch.setattr(socket, "getaddrinfo", _refuse)
    yield


# ---------------------------------------------------------------------------
# A private SQLite database for the index tests. The shared seeded database
# is deliberately not used: these tests create, supersede and delete web
# pages, and the assertions are about exactly which rows exist.
# ---------------------------------------------------------------------------
def _import_all_models() -> None:
    import app.models as models

    for module in pkgutil.iter_modules(models.__path__):
        importlib.import_module(f"app.models.{module.name}")


@pytest.fixture()
def db():
    _import_all_models()
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    session.add(Company(id="c-jsw", name="JSW Steel Limited", ticker="JSWSTEEL"))
    session.add(Company(id="c-tata", name="Tata Steel Limited", ticker="TATASTEEL"))
    session.commit()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


EMBEDDER = HashingEmbeddingProvider()


def add_document(
    db, company_id: str, title: str | None, doc_type: str, texts: list[str], *,
    url: str | None = None, canonical: str | None = None,
    source_class: str | None = None, published=None, retrieved=None,
    status: str = "ready", superseded_by: int | None = None,
    embed: bool = True, spec: str | None = "match", content: str | None = None,
    filename: str | None = None,
) -> Document:
    payload = (content if content is not None else "\n".join(texts)).encode("utf-8")
    web_meta = {}
    if url:
        web_meta["source_url"] = url
        web_meta["canonical_url"] = canonical or url
    document = Document(
        company_id=company_id,
        filename=filename or (title or "page") + ".html",
        title=title,
        doc_type=doc_type,
        file_format="html",
        size_bytes=len(payload),
        content_hash=hashlib.sha256(payload).hexdigest(),
        status=status,
        source_url=url,
        source_class=source_class,
        published_at=published,
        retrieved_at=retrieved,
        embedding_spec=(EMBEDDER.spec.key if spec == "match" else spec),
        doc_metadata={"web": web_meta} if web_meta else None,
    )
    db.add(document)
    db.flush()
    for i, text in enumerate(texts):
        db.add(DocumentChunk(
            document_id=document.id, chunk_index=i, text=text, page=1,
            paragraph=i, section="unknown",
            fingerprint=hashlib.sha1(text.encode("utf-8")).hexdigest()[:40],
            embedding=EMBEDDER.embed_one(text) if embed else None,
        ))
    if superseded_by is not None:
        document.superseded_by = superseded_by
    db.commit()
    return document


@pytest.fixture()
def corpus(db):
    """A small, deliberately mixed corpus.

    * three current JSW web pages of different classes and ages;
    * a JSW *filing* that contains the exact query words (must never appear);
    * an ``other`` upload with the same words (must never appear);
    * a superseded and a still-queued JSW web page (must never appear);
    * one Tata web page that also mentions JSW (appears only unscoped).
    """
    docs = {}
    docs["exchange"] = add_document(
        db, "c-jsw", "JSW Steel Dolvi expansion filing", WEB_PAGE_DOC_TYPE,
        ["JSW Steel expansion status: the exchange filing confirms the Dolvi "
         "expansion is complete and commissioned."],
        url="https://www.nseindia.com/corporate/jsw-dolvi",
        source_class=WebSourceClass.EXCHANGE.value,
        published=NOW - timedelta(days=30), retrieved=NOW - timedelta(days=2),
    )
    docs["company"] = add_document(
        db, "c-jsw", "JSW Steel expansion update", WEB_PAGE_DOC_TYPE,
        ["JSW Steel expansion at Vijayanagar: the brownfield expansion is on "
         "track for commissioning this year.",
         "Capex guidance for the Vijayanagar expansion remains unchanged."],
        url="https://www.jsw.in/investors/expansion",
        source_class=WebSourceClass.COMPANY_WEBSITE.value,
        published=NOW - timedelta(days=20), retrieved=NOW - timedelta(days=1),
    )
    docs["unknown"] = add_document(
        db, "c-jsw", "Steel sector blog", WEB_PAGE_DOC_TYPE,
        ["A blog post: JSW Steel expansion plans and the Indian steel cycle."],
        url="https://steelblog.example.com/jsw-expansion",
        source_class=WebSourceClass.UNKNOWN.value,
        published=NOW - timedelta(days=800), retrieved=NOW - timedelta(days=10),
    )
    docs["filing"] = add_document(
        db, "c-jsw", "Annual Report FY25", "annual_report",
        ["JSW Steel expansion status and capacity expansion are described in "
         "this annual report; Dolvi expansion complete."],
    )
    docs["other"] = add_document(
        db, "c-jsw", "Analyst note upload", "other",
        ["JSW Steel expansion status note: Dolvi expansion complete."],
    )
    docs["superseded"] = add_document(
        db, "c-jsw", "JSW Steel expansion (old snapshot)", WEB_PAGE_DOC_TYPE,
        ["JSW Steel expansion status superseded snapshot; Dolvi expansion."],
        url="https://www.jsw.in/investors/old-expansion",
        source_class=WebSourceClass.COMPANY_WEBSITE.value,
        superseded_by=docs["company"].id,
        retrieved=NOW - timedelta(days=200),
    )
    docs["queued"] = add_document(
        db, "c-jsw", "JSW Steel queued page", WEB_PAGE_DOC_TYPE,
        ["JSW Steel expansion status queued page; Dolvi expansion."],
        url="https://www.jsw.in/investors/queued",
        source_class=WebSourceClass.COMPANY_WEBSITE.value, status="queued",
    )
    docs["tata"] = add_document(
        db, "c-tata", "Tata Steel Kalinganagar expansion", WEB_PAGE_DOC_TYPE,
        ["Tata Steel expansion at Kalinganagar progressing; JSW Steel "
         "expansion mentioned in passing."],
        url="https://www.tatasteel.com/newsroom/kalinganagar",
        source_class=WebSourceClass.COMPANY_WEBSITE.value,
        published=NOW - timedelta(days=5), retrieved=NOW,
    )
    return docs


@pytest.fixture()
def index(db) -> SelfOwnedWebIndex:
    return SelfOwnedWebIndex(db, clock=lambda: NOW)


def _ids(result: WebIndexSearchResult) -> list[int]:
    return [c.document_id for c in result.candidates]


# ---------------------------------------------------------------------------
# A duck-typed stand-in for the shared engine, for the hybrid-path tests.
# It returns ``RetrievalResult`` rows exactly as the real engine does.
# ---------------------------------------------------------------------------
class _FakeEngine:
    def __init__(self, rows_by_query=None, *, raise_on_call: bool = False,
                 default=()):
        self.rows_by_query = dict(rows_by_query or {})
        self.default = list(default)
        self.calls: list[dict] = []
        self.raise_on_call = raise_on_call
        self.available = True

    def retrieve(self, query, *, company_id=None, top_k=10,
                 document_ids=None, rerank=True, doc_types=None):
        self.calls.append({
            "query": query, "company_id": company_id, "top_k": top_k,
            "document_ids": document_ids, "rerank": rerank,
            "doc_types": tuple(doc_types) if doc_types else doc_types,
        })
        if self.raise_on_call:
            raise RuntimeError("engine down")
        return list(self.rows_by_query.get(query, self.default))


def _result(chunk_id, document_id, text, *, score=1.0, signals=None, raw=None,
            title="") -> RetrievalResult:
    signals = dict(signals or {"lexical": 1})
    return RetrievalResult(
        chunk_id=chunk_id, document_id=document_id, text=text, page=1,
        paragraph=0, section="unknown", document_title=title, score=score,
        confidence=0.5, signals=signals, raw=dict(raw or {}),
        metadata={"doc_type": WEB_PAGE_DOC_TYPE},
    )


# ===========================================================================
# A. Query generation
# ===========================================================================
class TestQueryGenerationLanguages:
    def test_english_question_with_company(self, planner, generator):
        plan, qs = _plan_and_generate(
            planner, generator, "What is the latest news on JSW Steel?",
        )
        assert plan.execution_route is ExecutionRoute.WEB_RESEARCH
        assert qs.status is WebQueryStatus.GENERATED
        assert qs.subject == "JSW Steel"
        assert qs.ticker == "JSWSTEEL"
        assert qs.company_id == "c-jsw"
        assert qs.texts[0] == "JSW Steel latest news"
        assert "JSW Steel news" in qs.texts
        assert all(q.text.startswith(("JSW Steel", "JSWSTEEL")) for q in qs.queries)

    def test_hinglish_question_is_the_spec_example(self, planner, generator):
        _, qs = _plan_and_generate(
            planner, generator, "JSW Steel ka latest expansion status kya hai?",
        )
        assert qs.status is WebQueryStatus.GENERATED
        assert qs.texts == (
            "JSW Steel latest expansion status",
            "JSW Steel expansion status",
            "JSW Steel expansion",
            "JSWSTEEL expansion status",
        )
        for text in qs.texts:
            words = {w.casefold() for w in text.split()}
            assert not words & {"ka", "kya", "hai"}

    def test_hinglish_order_question(self, planner, generator):
        _, qs = _plan_and_generate(
            planner, generator, "JSW Steel ka latest order kya hai?",
        )
        assert qs.texts[0] == "JSW Steel latest order"
        assert "JSW Steel order" in qs.texts

    def test_hindi_question_unresolved_keeps_devanagari_terms(self, blind_planner, generator):
        _, qs = _plan_and_generate(
            blind_planner, generator, "रिलायंस की ताज़ा खबर क्या है?",
        )
        assert qs.status is WebQueryStatus.GENERATED
        assert qs.subject is None
        assert qs.texts[0] == "रिलायंस ताज़ा खबर"
        assert "रिलायंस खबर" in qs.texts
        for text in qs.texts:
            assert not {"की", "क्या", "है"} & set(text.split())

    def test_hindi_question_resolved_produces_both_scripts(self, planner, generator):
        plan, qs = _plan_and_generate(
            planner, generator, "JSW स्टील का विस्तार कब पूरा होगा?",
        )
        assert plan.execution_route is ExecutionRoute.WEB_RESEARCH
        assert qs.status is WebQueryStatus.GENERATED
        assert qs.subject == "JSW Steel"
        assert "JSW Steel expansion" in qs.texts
        assert any("विस्तार" in t for t in qs.texts)
        assert all(t.startswith(("JSW Steel", "JSWSTEEL")) for t in qs.texts)
        for text in qs.texts:
            assert not {"का", "कब", "होगा"} & set(text.split())

    def test_english_recency_word_is_dropped_in_the_core_variant(self, planner, generator):
        _, qs = _plan_and_generate(
            planner, generator, "Has TCS announced any acquisition recently?",
        )
        assert qs.texts[0] == "Tata Consultancy Services announced acquisition recently"
        assert "Tata Consultancy Services announced acquisition" in qs.texts
        assert "TCS announced acquisition" in qs.texts


class TestQueryGenerationCompanyContext:
    def test_legal_suffix_is_stripped_but_the_name_is_otherwise_verbatim(self, planner, generator):
        _, qs = _plan_and_generate(planner, generator, "Reliance latest news kya hai?")
        assert qs.subject == "Reliance Industries"
        assert qs.texts[0].startswith("Reliance Industries ")
        assert "Limited" not in " ".join(qs.texts)

    def test_ticker_variant_is_scoped_and_uses_the_resolved_ticker(self, planner, generator):
        _, qs = _plan_and_generate(
            planner, generator, "JSW Steel ka latest expansion status kya hai?",
        )
        by_basis = {q.basis: q for q in qs.queries}
        assert by_basis["ticker + topic"].text == "JSWSTEEL expansion status"
        assert all(q.scoped for q in qs.queries)

    def test_subject_only_when_no_content_words_survive(self, planner, generator):
        _, qs = _plan_and_generate(planner, generator, "JSW Steel me kya chal raha hai?")
        assert qs.status is WebQueryStatus.GENERATED
        assert qs.texts == ("JSW Steel",)
        assert qs.queries[0].basis == "subject only"

    def test_ambiguous_entity_produces_no_query_and_names_no_company(self, planner, generator):
        plan = planner.plan("JSW Steel ka latest expansion status kya hai?")
        ambiguous = replace(plan, entity=EntityResolution(
            status=EntityStatus.AMBIGUOUS, candidates=("TATASTEEL", "TATAMOTORS"),
            basis="test",
        ))
        qs = generator.generate(ambiguous)
        assert qs.status is WebQueryStatus.AMBIGUOUS_SUBJECT
        assert qs.queries == ()
        assert qs.subject is None and qs.ticker is None and qs.company_id is None
        assert "TATASTEEL" in qs.reason and "TATAMOTORS" in qs.reason

    def test_missing_entity_generates_unscoped_queries(self, blind_planner, generator):
        _, qs = _plan_and_generate(blind_planner, generator, "latest news kya hai?")
        assert qs.status is WebQueryStatus.GENERATED
        assert qs.subject is None and qs.ticker is None and qs.company_id is None
        assert all(not q.scoped for q in qs.queries)
        assert qs.texts == ("latest news", "news")

    def test_unresolved_company_words_are_kept_but_never_promoted_to_a_subject(self, blind_planner, generator):
        _, qs = _plan_and_generate(
            blind_planner, generator, "Adani Ports ne kaunsa naya order jeeta?",
        )
        assert qs.subject is None and qs.ticker is None
        assert qs.texts == ("Adani Ports naya order",)
        assert not qs.queries[0].scoped

    @pytest.mark.parametrize("question", [
        "latest news kya hai?",
        "koi naya order mila kya?",
        "What is the latest expansion status?",
        "ताज़ा खबर क्या है?",
    ])
    def test_never_invents_a_company_or_ticker(self, blind_planner, generator, question):
        _, qs = _plan_and_generate(blind_planner, generator, question)
        assert qs.subject is None and qs.ticker is None and qs.company_id is None
        joined = " ".join(qs.texts).casefold()
        for co in UNIVERSE:
            assert co.ticker.casefold() not in joined
            assert co.name.casefold() not in joined

    def test_unusable_entity_statuses_yield_no_subject(self, planner, generator):
        plan = planner.plan("JSW Steel ka latest expansion status kya hai?")
        for status in EntityStatus:
            entity = EntityResolution(status=status, company_id="c-x", ticker="XYZ",
                                      name="Xyz Limited", basis="test")
            qs = generator.generate(replace(plan, entity=entity))
            if entity.is_usable:
                assert qs.subject == "Xyz"
            elif status is EntityStatus.AMBIGUOUS:
                assert qs.status is WebQueryStatus.AMBIGUOUS_SUBJECT
            else:
                assert qs.subject is None
                assert "Xyz" not in " ".join(qs.texts)
                assert "XYZ" not in " ".join(qs.texts)


class TestQueryGenerationBounds:
    QUESTIONS = [
        "JSW Steel ka latest expansion status kya hai?",
        "What is the latest news on JSW Steel?",
        "रिलायंस की ताज़ा खबर क्या है?",
        "latest news kya hai?",
        "TCS ke latest large deal wins aur partnership announcements ke baare mein batao",
    ]

    def test_deterministic_across_calls_and_instances(self, planner):
        first = WebQueryGenerator()
        second = WebQueryGenerator()
        for question in self.QUESTIONS:
            plan = planner.plan(question)
            baseline = first.generate(plan).as_dict()
            for _ in range(10):
                assert first.generate(plan).as_dict() == baseline
                assert second.generate(plan).as_dict() == baseline

    def test_queries_are_deduplicated_case_insensitively(self, planner, generator):
        for question in self.QUESTIONS:
            _, qs = _plan_and_generate(planner, generator, question)
            keys = [t.casefold() for t in qs.texts]
            assert len(keys) == len(set(keys))

    def test_default_query_count_limit(self, planner, generator):
        long_question = (
            "JSW Steel ke latest expansion status, naya order, acquisition, "
            "plant capacity, merger aur partnership ke baare mein poori "
            "jaankari do"
        )
        _, qs = _plan_and_generate(planner, generator, long_question)
        assert 1 <= len(qs.queries) <= generator.limits.max_queries == 4

    def test_custom_query_count_limit(self, planner):
        generator = WebQueryGenerator(limits=WebQueryLimits(max_queries=2))
        _, qs = _plan_and_generate(
            planner, generator, "JSW Steel ka latest expansion status kya hai?",
        )
        assert len(qs.queries) == 2
        assert qs.texts == ("JSW Steel latest expansion status", "JSW Steel expansion status")

    def test_length_and_token_limits_hold_on_a_long_question(self, planner, generator):
        long_question = (
            "TCS ke latest large deal wins, partnership announcements, "
            "acquisition news, expansion plans, plant capacity, order book, "
            "appointment updates aur buyback status ke baare mein batao"
        )
        _, qs = _plan_and_generate(planner, generator, long_question)
        limits = generator.limits
        assert qs.queries
        for q in qs.queries:
            assert len(q.text) <= limits.max_query_chars
            assert len(q.text.split()) <= limits.max_query_tokens
            assert "\n" not in q.text
        assert len(qs.topic_terms) <= limits.max_topic_terms

    def test_tight_custom_limits_are_enforced(self, planner):
        generator = WebQueryGenerator(
            limits=WebQueryLimits(max_query_tokens=3, max_query_chars=24, max_topic_terms=2),
        )
        _, qs = _plan_and_generate(
            planner, generator, "JSW Steel ka latest expansion status kya hai?",
        )
        assert qs.queries
        for q in qs.queries:
            assert len(q.text.split()) <= 3
            assert len(q.text) <= 24
        assert len(qs.topic_terms) <= 2

    @pytest.mark.parametrize("field", [
        "max_queries", "max_query_tokens", "max_query_chars", "max_topic_terms",
        "max_term_chars",
    ])
    def test_limits_reject_zero(self, field):
        with pytest.raises(ValueError):
            WebQueryLimits(**{field: 0})

    def test_no_query_is_a_paragraph(self, planner, generator):
        for question in self.QUESTIONS:
            _, qs = _plan_and_generate(planner, generator, question)
            for q in qs.queries:
                assert "?" not in q.text
                assert "\n" not in q.text
                assert len(q.text) <= 96


class TestQueryGenerationEdges:
    def test_none_plan_is_empty(self, generator):
        qs = generator.generate(None)
        assert qs.status is WebQueryStatus.EMPTY
        assert qs.is_empty and qs.texts == ()

    @pytest.mark.parametrize("question", ["", "   ", "\n\t"])
    def test_blank_question_is_empty(self, planner, generator, question):
        plan = planner.plan(question)
        qs = generator.generate(plan)
        assert qs.status in {WebQueryStatus.EMPTY, WebQueryStatus.NOT_APPLICABLE}
        assert qs.is_empty

    @pytest.mark.parametrize("question", [
        "What is the P/E of TCS?",
        "₹500 se ₹650 kitna percent increase hai?",
        "Compare JSW Steel and Reliance latest expansion",
    ])
    def test_unsupported_routes_are_not_applicable(self, planner, generator, question):
        plan, qs = _plan_and_generate(planner, generator, question)
        assert plan.execution_route is not ExecutionRoute.WEB_RESEARCH
        assert qs.status is WebQueryStatus.NOT_APPLICABLE
        assert qs.is_empty
        assert plan.execution_route.value in qs.reason

    def test_served_routes_can_be_widened_explicitly(self, planner):
        generator = WebQueryGenerator(
            served_routes=(ExecutionRoute.WEB_RESEARCH, ExecutionRoute.INTERNAL_REASONING),
        )
        plan, qs = _plan_and_generate(
            planner, generator, "Compare JSW Steel and Reliance latest expansion",
        )
        assert plan.execution_route is ExecutionRoute.INTERNAL_REASONING
        assert qs.status is WebQueryStatus.AMBIGUOUS_SUBJECT or qs.status is WebQueryStatus.GENERATED

    def test_as_dict_is_json_serialisable(self, planner, generator):
        _, qs = _plan_and_generate(
            planner, generator, "JSW Steel ka latest expansion status kya hai?",
        )
        payload = json.loads(json.dumps(qs.as_dict()))
        assert payload["status"] == "generated"
        assert payload["queries"][0]["text"] == "JSW Steel latest expansion status"
        assert set(payload["queries"][0]) == {"text", "terms", "basis", "scoped"}

    def test_result_types_are_frozen(self, planner, generator):
        _, qs = _plan_and_generate(planner, generator, "latest news kya hai?")
        with pytest.raises(Exception):
            qs.status = WebQueryStatus.EMPTY  # type: ignore[misc]
        with pytest.raises(Exception):
            qs.queries[0].text = "x"  # type: ignore[misc]

    def test_generation_never_touches_the_network(self, planner, generator, no_network):
        for question in TestQueryGenerationBounds.QUESTIONS:
            plan = planner.plan(question)
            assert isinstance(generator.generate(plan), WebQuerySet)


# ===========================================================================
# B. Index scope
# ===========================================================================
class TestIndexScope:
    def test_only_web_pages_are_searchable(self, index, corpus):
        result = index.search("JSW Steel expansion status Dolvi complete", company_id="c-jsw")
        ids = set(_ids(result))
        assert ids, result.notes
        assert corpus["filing"].id not in ids
        assert corpus["other"].id not in ids
        assert ids <= {corpus["exchange"].id, corpus["company"].id, corpus["unknown"].id}

    def test_superseded_and_unready_pages_are_excluded(self, index, corpus):
        result = index.search("JSW Steel expansion status Dolvi", company_id="c-jsw")
        ids = set(_ids(result))
        assert corpus["superseded"].id not in ids
        assert corpus["queued"].id not in ids

    def test_corpus_size_counts_current_ready_web_pages_only(self, index, corpus):
        assert index.corpus_size("c-jsw") == 3
        assert index.corpus_size("c-tata") == 1
        assert index.corpus_size() == 4
        assert index.corpus_size("c-none") == 0

    def test_result_reports_the_scoped_corpus(self, index, corpus):
        result = index.search("expansion", company_id="c-jsw")
        assert result.corpus_documents == 3
        assert result.company_id == "c-jsw"

    def test_scope_query_filters_on_the_web_page_type(self, index):
        compiled = str(index._scope_query("c-jsw").compile(
            compile_kwargs={"literal_binds": True},
        ))
        assert "documents.doc_type = 'web_page'" in compiled
        assert "documents.superseded_by IS NULL" in compiled
        assert "documents.company_id = 'c-jsw'" in compiled
        assert "documents.status IN" in compiled

    def test_indexed_statuses_match_the_document_service(self):
        from app.services.documents.service import INDEXED_STATUSES as SERVICE_STATUSES

        assert INDEXED_STATUSES == SERVICE_STATUSES

    def test_empty_corpus_is_an_honest_empty_result(self, index):
        result = index.search("JSW Steel expansion", company_id="c-jsw")
        assert result.is_empty
        assert result.engine == "none"
        assert result.corpus_documents == 0
        assert "no stored web pages in scope" in result.notes

    def test_hybrid_rows_outside_the_scope_are_dropped(self, db, corpus):
        """Even if an engine returned a filing chunk, the index refuses it."""
        filing = corpus["filing"]
        engine = _FakeEngine(default=[
            _result(9001, filing.id, "filing text"),
            _result(9002, corpus["exchange"].id, "web text"),
        ])
        index = SelfOwnedWebIndex(db, engine=engine, clock=lambda: NOW)
        result = index.search("JSW Steel expansion", company_id="c-jsw")
        assert _ids(result) == [corpus["exchange"].id]
        assert result.engine == "hybrid"


# ===========================================================================
# C. Lexical search
# ===========================================================================
class TestLexicalSearch:
    def test_exact_terms_find_the_right_page(self, index, corpus):
        result = index.search("Dolvi expansion commissioned", company_id="c-jsw")
        assert _ids(result)[0] == corpus["exchange"].id
        top = result.candidates[0]
        assert "lexical" in top.signals
        assert top.matched_queries == ("Dolvi expansion commissioned",)
        assert top.snippet in "JSW Steel expansion status: the exchange filing confirms the Dolvi expansion is complete and commissioned."

    def test_snippet_is_the_stored_chunk_text(self, index, corpus):
        result = index.search("Capex guidance Vijayanagar", company_id="c-jsw")
        top = result.candidates[0]
        assert top.document_id == corpus["company"].id
        stored = db_texts(index.db, corpus["company"].id)
        assert top.snippet in stored

    def test_unmatched_query_returns_nothing(self, index, corpus):
        result = index.search("dividend policy nonsense zzz", company_id="c-jsw")
        assert result.is_empty
        assert result.engine in {"none", "in_memory"}
        assert "no stored web passage matched" in result.notes

    def test_query_set_object_is_accepted(self, index, corpus, planner, generator):
        _, qs = _plan_and_generate(
            planner, generator, "JSW Steel ka latest expansion status kya hai?",
        )
        result = index.search(qs, company_id="c-jsw")
        assert result.queries == qs.texts
        assert not result.is_empty
        assert all(q in qs.texts for c in result.candidates for q in c.matched_queries)

    def test_sequence_of_web_query_objects_is_accepted(self, index, corpus):
        queries = [WebQuery("Dolvi expansion", ("Dolvi", "expansion"), "test", False)]
        result = index.search(queries, company_id="c-jsw")
        assert result.queries == ("Dolvi expansion",)
        assert _ids(result)[0] == corpus["exchange"].id

    def test_snippet_is_bounded(self, db):
        long_text = " ".join(f"expansion{i}" for i in range(400)) + " expansion"
        add_document(db, "c-jsw", "Long page", WEB_PAGE_DOC_TYPE, [long_text],
                     url="https://www.jsw.in/long", source_class="company_website")
        index = SelfOwnedWebIndex(db, clock=lambda: NOW, limits=WebIndexLimits(snippet_chars=120))
        result = index.search("expansion", company_id="c-jsw")
        assert result.candidates
        assert len(result.candidates[0].snippet) <= 121  # 120 + ellipsis
        assert result.candidates[0].snippet.endswith("…")


def db_texts(db, document_id: int) -> str:
    return "\n".join(
        db.scalars(select(DocumentChunk.text).where(DocumentChunk.document_id == document_id))
    )


# ===========================================================================
# D. Semantic search when the infrastructure supports it
# ===========================================================================
class TestSemanticWhenAvailable:
    def test_hybrid_semantic_signal_is_carried_through(self, db, corpus):
        engine = _FakeEngine(default=[
            _result(1, corpus["exchange"].id, "Dolvi expansion complete",
                    score=1.0, signals={"semantic": 1, "lexical": 1},
                    raw={"semantic": 0.82, "lexical": 0.4}),
        ])
        index = SelfOwnedWebIndex(db, engine=engine, clock=lambda: NOW)
        result = index.search("JSW Steel expansion", company_id="c-jsw")
        assert result.engine == "hybrid"
        assert result.semantic_used is True
        assert result.candidates[0].signals == ("lexical", "semantic")

    def test_semantic_only_hit_above_the_floor_counts(self, db, corpus):
        engine = _FakeEngine(default=[
            _result(1, corpus["exchange"].id, "Dolvi expansion complete",
                    signals={"semantic": 1}, raw={"semantic": SEMANTIC_ONLY_FLOOR + 0.1}),
        ])
        result = SelfOwnedWebIndex(db, engine=engine, clock=lambda: NOW).search(
            "JSW Steel expansion", company_id="c-jsw",
        )
        assert _ids(result) == [corpus["exchange"].id]
        assert result.candidates[0].signals == ("semantic",)

    def test_semantic_only_nearest_neighbour_below_the_floor_is_not_a_match(self, db, corpus):
        engine = _FakeEngine(default=[
            _result(1, corpus["exchange"].id, "Dolvi expansion complete",
                    signals={"semantic": 1}, raw={"semantic": SEMANTIC_ONLY_FLOOR - 0.1}),
        ])
        index = SelfOwnedWebIndex(db, engine=engine, clock=lambda: NOW)
        result = index.search("quarterly dividend zzz", company_id="c-jsw")
        # The hybrid result was rejected; the local store finds nothing either.
        assert corpus["exchange"].id not in _ids(result)
        assert result.engine != "hybrid"

    def test_local_vectors_reorder_but_never_admit(self, index, corpus):
        result = index.search("Dolvi expansion", company_id="c-jsw")
        assert result.engine == "in_memory"
        assert result.semantic_used is True
        for candidate in result.candidates:
            assert "lexical" in candidate.signals


# ===========================================================================
# E. Lexical fallback
# ===========================================================================
class TestLexicalFallback:
    def test_engine_failure_falls_back_to_the_local_store(self, db, corpus):
        engine = _FakeEngine(raise_on_call=True)
        index = SelfOwnedWebIndex(db, engine=engine, clock=lambda: NOW)
        result = index.search("Dolvi expansion", company_id="c-jsw")
        assert engine.calls, "the shared engine was tried first"
        assert result.engine == "in_memory"
        assert _ids(result)[0] == corpus["exchange"].id
        assert "shared retrieval engine returned no web passages" in result.notes

    def test_engine_returning_nothing_falls_back(self, db, corpus):
        engine = _FakeEngine(default=[])
        result = SelfOwnedWebIndex(db, engine=engine, clock=lambda: NOW).search(
            "Dolvi expansion", company_id="c-jsw",
        )
        assert result.engine == "in_memory"
        assert not result.is_empty

    def test_sqlite_never_constructs_the_postgres_engine(self, db, corpus, monkeypatch):
        def _boom(*_a, **_k):
            raise AssertionError("HybridRetrievalEngine must not be built on SQLite")

        monkeypatch.setattr(HybridRetrievalEngine, "__init__", _boom)
        index = SelfOwnedWebIndex(db, clock=lambda: NOW)
        result = index.search("Dolvi expansion", company_id="c-jsw")
        assert result.engine == "in_memory"
        assert "shared retrieval engine not used on this database" in result.notes

    def test_use_hybrid_false_skips_the_engine(self, db, corpus):
        engine = _FakeEngine(default=[_result(1, corpus["exchange"].id, "x")])
        index = SelfOwnedWebIndex(db, engine=engine, use_hybrid=False, clock=lambda: NOW)
        result = index.search("Dolvi expansion", company_id="c-jsw")
        assert engine.calls == []
        assert result.engine == "in_memory"

    def test_pages_without_vectors_are_lexical_only(self, db):
        add_document(db, "c-jsw", "No vectors", WEB_PAGE_DOC_TYPE,
                     ["JSW Steel expansion at Dolvi without any vector."],
                     url="https://www.jsw.in/nv", source_class="company_website", embed=False)
        result = SelfOwnedWebIndex(db, clock=lambda: NOW).search("Dolvi expansion", company_id="c-jsw")
        assert result.engine == "in_memory"
        assert result.semantic_used is False
        assert result.candidates[0].signals == ("lexical",)

    def test_foreign_embedding_space_is_not_compared(self, db):
        add_document(db, "c-jsw", "Other space", WEB_PAGE_DOC_TYPE,
                     ["JSW Steel expansion at Dolvi embedded elsewhere."],
                     url="https://www.jsw.in/os", source_class="company_website",
                     spec="jina:jina-embeddings-v3:1024")
        result = SelfOwnedWebIndex(db, clock=lambda: NOW).search("Dolvi expansion", company_id="c-jsw")
        assert result.candidates
        assert result.candidates[0].signals == ("lexical",)


# ===========================================================================
# F. Company scoping
# ===========================================================================
class TestCompanyScoping:
    def test_company_scope_excludes_other_companies(self, index, corpus):
        result = index.search("JSW Steel expansion", company_id="c-jsw")
        assert corpus["tata"].id not in _ids(result)
        assert all(c.company_id == "c-jsw" for c in result.candidates)

    def test_unscoped_search_covers_every_company(self, index, corpus):
        result = index.search("JSW Steel expansion")
        ids = set(_ids(result))
        assert corpus["tata"].id in ids
        assert {corpus["exchange"].id, corpus["company"].id} <= ids
        assert result.company_id is None

    def test_unknown_company_is_empty_not_widened(self, index, corpus):
        result = index.search("JSW Steel expansion", company_id="c-nobody")
        assert result.is_empty
        assert result.corpus_documents == 0

    def test_engine_receives_company_and_web_page_scope(self, db, corpus):
        engine = _FakeEngine(default=[_result(1, corpus["exchange"].id, "Dolvi")])
        index = SelfOwnedWebIndex(db, engine=engine, clock=lambda: NOW)
        index.search(["JSW Steel expansion", "JSW Steel Dolvi"], company_id="c-jsw")
        assert [c["query"] for c in engine.calls] == ["JSW Steel expansion", "JSW Steel Dolvi"]
        for call in engine.calls:
            assert call["company_id"] == "c-jsw"
            assert call["doc_types"] == (WEB_PAGE_DOC_TYPE,)
            assert call["top_k"] == index.limits.per_query


# ===========================================================================
# G. Dedupe
# ===========================================================================
class TestDedupe:
    def test_same_document_across_queries_is_one_candidate(self, index, corpus):
        result = index.search(
            ["JSW Steel expansion status", "JSW Steel expansion", "Dolvi expansion"],
            company_id="c-jsw",
        )
        ids = _ids(result)
        assert len(ids) == len(set(ids))
        exchange = next(c for c in result.candidates if c.document_id == corpus["exchange"].id)
        assert len(exchange.matched_queries) >= 2

    def test_same_canonical_url_keeps_the_latest_snapshot(self, db, corpus):
        older = add_document(
            db, "c-jsw", "JSW Steel expansion update (tracked link)", WEB_PAGE_DOC_TYPE,
            ["JSW Steel expansion at Vijayanagar: brownfield expansion on track."],
            url="https://www.jsw.in/investors/expansion?utm_source=newsletter",
            canonical="https://www.jsw.in/investors/expansion",
            source_class="company_website",
            retrieved=NOW - timedelta(days=120),
        )
        result = SelfOwnedWebIndex(db, clock=lambda: NOW).search(
            "Vijayanagar expansion", company_id="c-jsw",
        )
        ids = _ids(result)
        assert corpus["company"].id in ids
        assert older.id not in ids
        urls = [c.canonical_url for c in result.candidates]
        assert len(urls) == len(set(urls))

    def test_identical_bytes_under_two_companies_is_one_candidate(self, db, corpus):
        text = "Joint statement: JSW Steel expansion venture with Tata Steel announced."
        a = add_document(db, "c-jsw", "Joint statement", WEB_PAGE_DOC_TYPE, [text],
                         url="https://www.jsw.in/joint", source_class="company_website",
                         retrieved=NOW - timedelta(days=3))
        b = add_document(db, "c-tata", "Joint statement", WEB_PAGE_DOC_TYPE, [text],
                         url="https://www.tatasteel.com/joint", source_class="company_website",
                         retrieved=NOW - timedelta(days=4))
        assert a.content_hash == b.content_hash
        result = SelfOwnedWebIndex(db, clock=lambda: NOW).search("joint statement venture")
        ids = _ids(result)
        assert len({a.id, b.id} & set(ids)) == 1

    def test_hybrid_duplicates_across_queries_collapse(self, db, corpus):
        row = _result(1, corpus["exchange"].id, "Dolvi expansion complete")
        engine = _FakeEngine({"q1": [row], "q2": [row]})
        result = SelfOwnedWebIndex(db, engine=engine, clock=lambda: NOW).search(
            ["q1", "q2"], company_id="c-jsw",
        )
        assert _ids(result) == [corpus["exchange"].id]
        assert result.candidates[0].matched_queries == ("q1", "q2")

    def test_duplicate_query_strings_are_searched_once(self, db, corpus):
        engine = _FakeEngine(default=[_result(1, corpus["exchange"].id, "Dolvi")])
        SelfOwnedWebIndex(db, engine=engine, clock=lambda: NOW).search(
            ["Dolvi expansion", "dolvi expansion", " Dolvi   expansion "], company_id="c-jsw",
        )
        assert [c["query"] for c in engine.calls] == ["Dolvi expansion"]


# ===========================================================================
# H. Ranking: relevance, authority, freshness — documents only
# ===========================================================================
def _hybrid_index(db, rows):
    return SelfOwnedWebIndex(db, engine=_FakeEngine(default=rows), clock=lambda: NOW)


class TestRanking:
    def test_equal_relevance_orders_by_source_authority(self, db):
        pages = {}
        for cls in ("unknown", "company_website", "exchange", "regulator"):
            pages[cls] = add_document(
                db, "c-jsw", f"{cls} page", WEB_PAGE_DOC_TYPE, ["JSW Steel expansion update."],
                url=f"https://{cls}.example.com/p", source_class=cls,
                published=NOW - timedelta(days=10), retrieved=NOW - timedelta(days=1),
                content=f"{cls} unique bytes",
            )
        rows = [_result(i + 1, pages[cls].id, "JSW Steel expansion update.", score=1.0)
                for i, cls in enumerate(pages)]
        result = _hybrid_index(db, rows).search("JSW Steel expansion", company_id="c-jsw")
        assert _ids(result) == [
            pages["regulator"].id, pages["exchange"].id,
            pages["company_website"].id, pages["unknown"].id,
        ]
        authorities = [c.authority for c in result.candidates]
        assert authorities == sorted(authorities, reverse=True)

    def test_equal_relevance_and_class_orders_by_freshness(self, db):
        old = add_document(db, "c-jsw", "old", WEB_PAGE_DOC_TYPE, ["JSW Steel expansion."],
                           url="https://www.jsw.in/old", source_class="company_website",
                           published=NOW - timedelta(days=900), retrieved=NOW - timedelta(days=1),
                           content="old bytes")
        new = add_document(db, "c-jsw", "new", WEB_PAGE_DOC_TYPE, ["JSW Steel expansion."],
                           url="https://www.jsw.in/new", source_class="company_website",
                           published=NOW - timedelta(days=10), retrieved=NOW - timedelta(days=1),
                           content="new bytes")
        rows = [_result(1, old.id, "JSW Steel expansion."), _result(2, new.id, "JSW Steel expansion.")]
        result = _hybrid_index(db, rows).search("JSW Steel expansion", company_id="c-jsw")
        assert _ids(result) == [new.id, old.id]
        assert result.candidates[0].freshness > result.candidates[1].freshness

    def test_relevance_dominates_authority(self, db):
        regulator = add_document(db, "c-jsw", "reg", WEB_PAGE_DOC_TYPE, ["passing mention"],
                                 url="https://sebi.gov.in/x", source_class="regulator",
                                 published=NOW - timedelta(days=1), retrieved=NOW, content="reg bytes")
        blog = add_document(db, "c-jsw", "blog", WEB_PAGE_DOC_TYPE, ["direct answer"],
                            url="https://blog.example.com/x", source_class="unknown",
                            published=NOW - timedelta(days=1), retrieved=NOW, content="blog bytes")
        rows = [_result(1, regulator.id, "passing mention", score=0.2),
                _result(2, blog.id, "direct answer", score=1.0)]
        result = _hybrid_index(db, rows).search("direct answer", company_id="c-jsw")
        assert _ids(result) == [blog.id, regulator.id]

    def test_authority_is_the_existing_web_authority_contract(self, db, corpus):
        result = SelfOwnedWebIndex(db, clock=lambda: NOW).search(
            "JSW Steel expansion", company_id="c-jsw",
        )
        for candidate in result.candidates:
            cls = WebSourceClass(candidate.source_class)
            assert candidate.authority == round(
                web_authority(cls, published_at=candidate.published_at), 6,
            )

    def test_ordering_is_deterministic_and_independent_of_input_order(self, db):
        pages = [
            add_document(db, "c-jsw", f"p{i}", WEB_PAGE_DOC_TYPE, ["JSW Steel expansion."],
                         url=f"https://www.jsw.in/p{i}", source_class="company_website",
                         published=NOW - timedelta(days=10), retrieved=NOW - timedelta(days=1),
                         content=f"bytes {i}")
            for i in range(5)
        ]
        rows = [_result(i + 1, p.id, "JSW Steel expansion.") for i, p in enumerate(pages)]
        forward = _hybrid_index(db, rows).search("JSW Steel expansion", company_id="c-jsw")
        backward = _hybrid_index(db, list(reversed(rows))).search("JSW Steel expansion", company_id="c-jsw")
        assert _ids(forward) == _ids(backward) == sorted(p.id for p in pages)
        for _ in range(5):
            again = _hybrid_index(db, rows).search("JSW Steel expansion", company_id="c-jsw")
            assert again.as_dict() == forward.as_dict()

    def test_limit_caps_the_candidates(self, db):
        pages = [
            add_document(db, "c-jsw", f"p{i}", WEB_PAGE_DOC_TYPE, ["JSW Steel expansion."],
                         url=f"https://www.jsw.in/p{i}", source_class="company_website",
                         content=f"bytes {i}")
            for i in range(6)
        ]
        rows = [_result(i + 1, p.id, "JSW Steel expansion.") for i, p in enumerate(pages)]
        index = _hybrid_index(db, rows)
        assert len(index.search("JSW Steel expansion", company_id="c-jsw", limit=2).candidates) == 2
        assert len(index.search("JSW Steel expansion", company_id="c-jsw", limit=99).candidates) == 6
        assert len(index.search("JSW Steel expansion", company_id="c-jsw").candidates) == 6

    def test_multi_query_agreement_lifts_relevance(self, db, corpus):
        top = _result(1, corpus["company"].id, "Vijayanagar", score=1.0)
        twice = _result(2, corpus["exchange"].id, "Dolvi", score=0.6)
        once = _result(3, corpus["unknown"].id, "blog", score=0.6)
        engine = _FakeEngine({"a": [top, twice, once], "b": [top, twice]})
        result = SelfOwnedWebIndex(db, engine=engine, clock=lambda: NOW).search(
            ["a", "b"], company_id="c-jsw",
        )
        by_id = {c.document_id: c for c in result.candidates}
        assert by_id[corpus["company"].id].relevance == 1.0
        assert by_id[corpus["exchange"].id].relevance == pytest.approx(0.65)
        assert by_id[corpus["unknown"].id].relevance == pytest.approx(0.6)
        assert by_id[corpus["exchange"].id].matched_queries == ("a", "b")

    def test_results_rank_documents_never_companies(self, db, corpus):
        result = SelfOwnedWebIndex(db, clock=lambda: NOW).search("JSW Steel expansion")
        payload = result.as_dict()
        assert set(payload) == {
            "queries", "company_id", "candidates", "corpus_documents", "engine",
            "semantic_used", "notes",
        }
        for candidate in payload["candidates"]:
            assert "document_id" in candidate
            assert "ticker" not in candidate
            assert "rank" not in candidate


# ===========================================================================
# I. Missing metadata is reported, never invented
# ===========================================================================
class TestMissingMetadata:
    def test_missing_fields_stay_none(self, db):
        bare = add_document(
            db, "c-jsw", None, WEB_PAGE_DOC_TYPE,
            ["JSW Steel expansion page with no provenance recorded."],
            filename="bare.html",
        )
        result = SelfOwnedWebIndex(db, clock=lambda: NOW).search("expansion provenance", company_id="c-jsw")
        assert _ids(result) == [bare.id]
        candidate = result.candidates[0]
        assert candidate.title is None
        assert candidate.source_url is None
        assert candidate.canonical_url is None
        assert candidate.source_class is None
        assert candidate.published_at is None
        assert candidate.retrieved_at is None
        assert candidate.freshness_basis == "unknown"
        assert candidate.authority == round(web_authority(WebSourceClass.UNKNOWN), 6)
        payload = candidate.as_dict()
        for key in ("title", "source_url", "canonical_url", "source_class", "published_at", "retrieved_at"):
            assert payload[key] is None

    def test_retrieved_at_is_the_freshness_basis_when_publish_date_is_unknown(self, db):
        page = add_document(
            db, "c-jsw", "Undated", WEB_PAGE_DOC_TYPE, ["JSW Steel expansion undated page."],
            url="https://www.jsw.in/undated", source_class="company_website",
            retrieved=NOW - timedelta(days=3),
        )
        result = SelfOwnedWebIndex(db, clock=lambda: NOW).search("undated expansion", company_id="c-jsw")
        candidate = result.candidates[0]
        assert candidate.document_id == page.id
        assert candidate.published_at is None
        assert candidate.freshness_basis == "retrieved_at"
        assert candidate.freshness == 1.0

    def test_metadata_fallback_reads_only_what_the_row_holds(self, db):
        page = add_document(
            db, "c-jsw", "Meta only", WEB_PAGE_DOC_TYPE, ["JSW Steel expansion meta page."],
        )
        page.doc_metadata = {"web": {
            "source_url": "https://www.jsw.in/meta", "canonical_url": "https://www.jsw.in/meta",
            "title": "From metadata", "source_class": "exchange",
            "published_at": "2026-09-01T00:00:00+00:00",
        }}
        db.commit()
        result = SelfOwnedWebIndex(db, clock=lambda: NOW).search("meta page expansion", company_id="c-jsw")
        candidate = result.candidates[0]
        assert candidate.title == "Meta only"  # the column wins over metadata
        assert candidate.source_url == "https://www.jsw.in/meta"
        assert candidate.source_class == "exchange"
        assert candidate.published_at == datetime(2026, 9, 1, tzinfo=timezone.utc)
        assert candidate.retrieved_at is None

    def test_candidate_fields_are_explicit_and_frozen(self, db, corpus):
        result = SelfOwnedWebIndex(db, clock=lambda: NOW).search("Dolvi expansion", company_id="c-jsw")
        candidate = result.candidates[0]
        assert isinstance(candidate, WebEvidenceCandidate)
        expected = {
            "document_id", "chunk_id", "company_id", "content_hash", "source_url",
            "canonical_url", "title", "source_class", "published_at", "retrieved_at",
            "snippet", "relevance", "authority", "freshness", "freshness_basis",
            "score", "matched_queries", "signals", "page", "section", "origin",
        }
        assert set(candidate.as_dict()) == expected
        # Everything this index returns is a stored page; other producers
        # label theirs differently (Phase 4C's targeted discovery).
        assert candidate.origin == "local_index"
        assert candidate.document_id is not None
        with pytest.raises(Exception):
            candidate.score = 0.0  # type: ignore[misc]
        json.dumps(result.as_dict())


# ===========================================================================
# J. No provider, no network
# ===========================================================================
class TestNoProviderNoNetwork:
    def test_local_search_runs_with_sockets_disabled(self, db, corpus, no_network):
        result = SelfOwnedWebIndex(db, clock=lambda: NOW).search(
            ["JSW Steel expansion status", "Dolvi expansion"], company_id="c-jsw",
        )
        assert not result.is_empty

    def test_hybrid_path_runs_with_sockets_disabled(self, db, corpus, no_network):
        engine = _FakeEngine(default=[_result(1, corpus["exchange"].id, "Dolvi")])
        result = SelfOwnedWebIndex(db, engine=engine, clock=lambda: NOW).search(
            "Dolvi expansion", company_id="c-jsw",
        )
        assert result.engine == "hybrid"

    def test_end_to_end_plan_to_candidates_offline(self, db, corpus, planner, generator, no_network):
        plan = planner.plan("JSW Steel ka latest expansion status kya hai?")
        qs = generator.generate(plan)
        result = SelfOwnedWebIndex(db, clock=lambda: NOW).search(qs, company_id=qs.company_id)
        assert result.queries == qs.texts
        assert corpus["exchange"].id in _ids(result)
        assert corpus["filing"].id not in _ids(result)

    def test_importing_the_index_loads_no_provider_or_fetch_module(self):
        code = (
            "import sys\n"
            "import app.services.web.index\n"
            "print('\\n'.join(sorted(sys.modules)))\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code], cwd=BACKEND, capture_output=True, text=True,
            check=True,
        )
        loaded = set(proc.stdout.split())
        banned = (
            "openai", "anthropic", "google.generativeai", "app.services.ai.providers",
            "app.services.web.fetcher", "app.services.web.discovery",
            "app.services.web.crawler", "app.services.web.service",
            "app.services.retrieval.embeddings", "app.services.retrieval.rerank",
            "tavily", "serpapi", "serper", "searx",
        )
        for name in banned:
            assert name not in loaded, name
            assert not any(m.startswith(name + ".") for m in loaded), name

    def test_importing_the_planner_still_loads_no_web_or_retrieval_module(self):
        code = (
            "import sys\n"
            "import app.services.ai.planner\n"
            "print('\\n'.join(sorted(sys.modules)))\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code], cwd=BACKEND, capture_output=True, text=True,
            check=True,
        )
        loaded = set(proc.stdout.split())
        assert "app.services.ai.planner.web_query" in loaded
        for prefix in ("app.services.web", "app.services.retrieval", "app.services.ai.providers",
                       "httpx", "requests", "aiohttp", "openai"):
            assert not any(m == prefix or m.startswith(prefix + ".") for m in loaded), prefix


# ===========================================================================
# K. Architecture
# ===========================================================================
def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def _code_without_docstrings(path: Path) -> str:
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(
                getattr(body[0], "value", None), ast.Constant,
            ) and isinstance(body[0].value.value, str):
                body[0].value.value = ""
    return ast.unparse(tree)


class TestArchitectureGenerator:
    NETWORK_MODULES = ("httpx", "requests", "aiohttp", "urllib.request", "socket", "http.client")

    def test_generator_imports_nothing_that_can_reach_the_network(self):
        imports = _imports(WEB_QUERY_PATH)
        for banned in self.NETWORK_MODULES:
            assert not any(i == banned or i.startswith(banned + ".") for i in imports), banned

    def test_generator_imports_no_provider_db_web_or_retrieval_module(self):
        imports = _imports(WEB_QUERY_PATH)
        for banned in ("app.services.ai.providers", "app.services.web", "app.services.retrieval",
                       "app.models", "app.db", "app.services.documents", "openai", "anthropic"):
            assert not any(i == banned or i.startswith(banned + ".") for i in imports), banned
        # Only relative planner imports and the standard library.
        assert all(i.startswith(".") or i in {"re", "enum", "dataclasses", "__future__", "typing"}
                   or i.startswith(("types", "vocabulary")) for i in imports), imports

    def test_generator_code_has_no_llm_or_search_provider_tokens(self):
        code = _code_without_docstrings(WEB_QUERY_PATH).lower()
        for token in ("openai", "openrouter", "gemini", "anthropic", "providerrouter", "llm",
                      "tavily", "serper", "serpapi", "searx", "perplexity", "httpx", "fetch"):
            assert token not in code, token

    def test_generator_never_writes_or_retrieves(self):
        code = _code_without_docstrings(WEB_QUERY_PATH)
        for token in ("retrieve(", "session.", "db.", ".commit(", ".flush(", "embed"):
            assert token not in code, token

    def test_query_types_are_frozen_dataclasses(self):
        for cls in (WebQuery, WebQuerySet, WebQueryLimits):
            assert cls.__dataclass_params__.frozen  # type: ignore[attr-defined]


class TestArchitectureIndex:
    def test_index_imports_no_fetcher_crawler_or_network_client(self):
        imports = _imports(INDEX_PATH)
        for banned in ("httpx", "requests", "aiohttp", "urllib.request", "socket",
                       "app.services.web.fetcher", "app.services.web.discovery",
                       "app.services.web.crawler", "app.services.web.service",
                       "app.services.web.crawl", "app.services.ai.providers",
                       "app.services.ai", "openai", "anthropic", "google"):
            assert not any(i == banned or i.startswith(banned + ".") for i in imports), banned

    def test_index_code_has_no_external_search_or_llm_tokens(self):
        code = _code_without_docstrings(INDEX_PATH).lower()
        for token in ("webfetcher", "webcrawlerdiscovery", "crawlerdiscovery", "httpx",
                      "openai", "openrouter", "gemini", "anthropic", "providerrouter",
                      "tavily", "serper", "serpapi", "searx", "perplexity", "bing.com",
                      "google.com", "brave", "duckduckgo", "urlopen", "http://", "https://"):
            assert token not in code, token

    def test_index_searches_only_the_web_page_type(self):
        source = INDEX_PATH.read_text()
        assert 'WEB_PAGE_DOC_TYPE: str = DocumentType.WEB_PAGE.value' in source
        assert source.count("Document.doc_type == WEB_PAGE_DOC_TYPE") == 2
        assert "doc_types=(WEB_PAGE_DOC_TYPE,)" in source
        assert WEB_PAGE_DOC_TYPE == "web_page"

    def test_index_reuses_the_shared_retrieval_engine_and_store(self):
        source = INDEX_PATH.read_text()
        assert "from app.services.retrieval.engine import HybridRetrievalEngine" in source
        assert "InMemoryVectorStore" in source
        assert "HashingEmbeddingProvider" in source
        assert "web_authority" in source and "recency_factor" in source
        code = _code_without_docstrings(INDEX_PATH)
        for token in ("class BM25", "def cosine", "def reciprocal_rank", "faiss", "chromadb",
                      "sentence_transformers", "create_engine("):
            assert token not in code, token

    def test_index_adds_no_schema(self):
        code = _code_without_docstrings(INDEX_PATH)
        for token in ("__tablename__", "mapped_column", "Table(", "create_all", "CREATE TABLE",
                      "ALTER TABLE", "alembic", "op.create"):
            assert token not in code, token
        tables_before = set(Base.metadata.tables)
        importlib.reload(index_module)
        assert set(Base.metadata.tables) == tables_before

    def test_index_never_writes(self):
        code = _code_without_docstrings(INDEX_PATH)
        # Session writes, by any spelling the codebase uses.
        for token in ("db.add(", "session.add(", ".commit(", ".flush(", ".delete(",
                      "db.execute(insert", "db.execute(update", "sqlalchemy import insert",
                      "sqlalchemy import update", "bulk_save", "db.merge(", "session.merge("):
            assert token not in code, token
        # And the only SQLAlchemy construct imported is ``select``.
        imports = _imports(INDEX_PATH)
        assert "sqlalchemy" in imports
        tree = ast.parse(INDEX_PATH.read_text())
        sqlalchemy_names = {
            alias.name for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module == "sqlalchemy"
            for alias in node.names
        }
        assert sqlalchemy_names == {"select"}

    def test_index_result_types_are_frozen_and_not_citations(self):
        for cls in (WebEvidenceCandidate, WebIndexSearchResult, WebIndexLimits):
            assert cls.__dataclass_params__.frozen  # type: ignore[attr-defined]
        code = _code_without_docstrings(INDEX_PATH)
        assert "Citation(" not in code
        assert "CitationEngine" not in code

    def test_index_limits_reject_zero(self):
        for field in ("max_queries", "max_query_chars", "per_query", "max_results", "snippet_chars"):
            with pytest.raises(ValueError):
                WebIndexLimits(**{field: 0})


class _RecordingDB:
    """Executes the engine's SQL against canned rows and records every call."""

    def __init__(self, lexical_rows=(), hydrate_rows=()):
        self.lexical_rows = list(lexical_rows)
        self.hydrate_rows = list(hydrate_rows)
        self.calls: list[tuple[str, dict]] = []

    def execute(self, sql, params=None):
        query = sql.text if hasattr(sql, "text") else str(sql)
        self.calls.append((query, dict(params or {})))
        if "ts_rank_cd" in query:
            return [tuple(r) for r in self.lexical_rows]
        if "c.id = ANY" in query:
            return [tuple(r) for r in self.hydrate_rows]
        return []


class _StubEmbedder:
    def embed_one(self, _text):
        return [0.1, 0.2, 0.3]


class _NoRerank:
    def rerank(self, _query, candidates):
        return {}


class TestArchitectureRetrievalEngineChange:
    def test_doc_types_is_additive_with_a_none_default(self):
        params = inspect.signature(HybridRetrievalEngine.retrieve).parameters
        assert "doc_types" in params
        assert params["doc_types"].default is None
        assert params["doc_types"].kind is inspect.Parameter.KEYWORD_ONLY
        assert "language" not in params

    def test_default_call_emits_no_scope_clause(self):
        db = _RecordingDB(
            lexical_rows=[(1, 0.9)],
            hydrate_rows=[(1, 10, "text", 1, 0, "unknown", "T", "annual_report", 2025, "f.pdf")],
        )
        engine = HybridRetrievalEngine(db, embedder=_StubEmbedder(), reranker=_NoRerank())
        results = engine.retrieve("expansion status", company_id="c-jsw", rerank=False)
        assert [r.chunk_id for r in results] == [1]
        for sql, params in db.calls:
            assert "scope_doc_types" not in sql
            assert "scope_doc_types" not in params

    def test_scoped_call_adds_the_clause_to_every_signal(self):
        db = _RecordingDB(
            lexical_rows=[(1, 0.9)],
            hydrate_rows=[(1, 10, "text", 1, 0, "unknown", "T", "web_page", None, "p.html")],
        )
        engine = HybridRetrievalEngine(db, embedder=_StubEmbedder(), reranker=_NoRerank())
        results = engine.retrieve(
            "latest expansion status", company_id="c-jsw", rerank=False,
            doc_types=("web_page",),
        )
        assert [r.chunk_id for r in results] == [1]
        signal_calls = [(s, p) for s, p in db.calls if "c.id = ANY" not in s]
        assert signal_calls
        for sql, params in signal_calls:
            assert "d.doc_type = ANY(:scope_doc_types)" in sql
            assert params["scope_doc_types"] == ["web_page"]

    def test_explicit_empty_scope_returns_nothing_and_queries_nothing(self):
        db = _RecordingDB(lexical_rows=[(1, 0.9)])
        engine = HybridRetrievalEngine(db, embedder=_StubEmbedder(), reranker=_NoRerank())
        assert engine.retrieve("expansion", doc_types=()) == []
        assert db.calls == []

    def test_engine_source_change_is_the_one_helper(self):
        source = ENGINE_PATH.read_text()
        assert source.count("def _scope_doc_types(") == 1
        assert source.count("self._scope_doc_types(where, params, doc_types)") == 4
        assert 'where.append("d.doc_type = ANY(:scope_doc_types)")' in source


class TestPhase4AUnchanged:
    @pytest.mark.parametrize("question, route", [
        ("₹500 se ₹650 kitna percent increase hai?", ExecutionRoute.INTERNAL_REASONING),
        ("JSW Steel expansion status kya hai?", ExecutionRoute.WEB_RESEARCH),
        ("JSW Steel ka latest order kya hai?", ExecutionRoute.WEB_RESEARCH),
        ("latest news kya hai?", ExecutionRoute.WEB_RESEARCH),
        ("What is the P/E of TCS?", ExecutionRoute.DETERMINISTIC_FINANCIAL),
    ])
    def test_routes_still_hold(self, planner, question, route):
        assert planner.plan(question).execution_route is route

    @pytest.mark.parametrize("text, expected", [
        ("latest", True), ("news", True), ("expansion", True), ("khabar", True),
        ("विस्तार", True), ("ताज़ा खबर", True), ("kya chal raha hai", True),
        ("increase", False), ("revenue", False), ("percent", False), ("", False),
    ])
    def test_is_web_research_vocabulary_unchanged(self, text, expected):
        assert is_web_research(text) is expected

    def test_planner_constructor_unchanged(self):
        params = list(inspect.signature(QuestionPlanner.__init__).parameters)
        assert params == ["self", "company_resolver", "memory", "matcher", "adapter"]

    def test_generator_is_not_wired_into_any_production_module(self):
        offenders = []
        for path in sorted(APP.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            if path.parent.name == "planner" or path == INDEX_PATH:
                continue
            source = path.read_text()
            if "WebQueryGenerator" in source or "SelfOwnedWebIndex" in source:
                offenders.append(str(path.relative_to(APP)))
        assert offenders == []

    def test_web_query_module_obeys_the_planner_arithmetic_rule(self):
        tree = ast.parse(WEB_QUERY_PATH.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.BinOp):
                assert not isinstance(node.op, (ast.Div, ast.Mult, ast.Pow, ast.FloorDiv, ast.Mod))
                for side in (node.left, node.right):
                    assert not (isinstance(side, ast.Constant) and isinstance(side.value, (int, float)))
        assert not re.search(r"\bimport (httpx|requests|aiohttp|openai)\b", WEB_QUERY_PATH.read_text())
