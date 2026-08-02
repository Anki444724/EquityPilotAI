"""Document service — persistence, queue, versioning and search.

This is the only module that knows both the pipeline and the database. The
pipeline stays pure; the API stays thin; everything transactional lives here.

Three behaviours are worth reading carefully, because they are where a document
platform usually goes quietly wrong:

**Duplicate detection.** Identical bytes uploaded twice are one document. The
content hash is unique per company, so the second upload returns the first
rather than re-indexing it. Re-processing a 300-page report because a user
double-clicked is expensive; showing it twice in search results is worse.

**Version detection.** The *same filename* with *different bytes* is a new
version. The predecessor is marked superseded and excluded from search, but is
never deleted, because a citation issued last quarter must still resolve to
the text it actually quoted.

**Incremental re-indexing.** Re-indexing rebuilds vectors from stored chunks
without re-parsing the file. Changing the embedding model then costs an
embedding pass, not an OCR pass, which is the difference between minutes and
hours over a real corpus.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Sequence

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.domain.documents.fields import FIELD_COUNT, FIELDS_BY_KEY, FieldCategory
from app.domain.documents.types import (
    DocumentError, DocumentType, EntityKind, ProcessingStage, RelationKind,
    SearchAnswer, SearchHit, SectionKind, Unit, UnsupportedFormat,
    content_hash as hash_bytes,
)
from app.models.company import Company
from app.models.document import (
    Document, DocumentChunk, DocumentEntity, DocumentFact, DocumentJob,
    DocumentPage, DocumentRelation, DocumentSection, DocumentTable,
)
from app.services.documents.extractors.base import DocumentParser
from app.services.documents.extractors.ocr import OcrEngine
from app.services.documents.pipeline.embeddings import (
    EmbeddingProvider, HashingEmbeddingProvider,
)
from app.services.documents.pipeline.knowledge_graph import (
    KnowledgeGraph, KnowledgeGraphBuilder, node_key,
)
from app.services.documents.pipeline.orchestrator import (
    IngestionPipeline, IngestionResult,
)
from app.services.documents.pipeline.search import (
    DocumentCitation, DocumentSearch, SearchConfig, cite_all,
)
from app.services.documents.pipeline.vector_store import (
    InMemoryVectorStore, VectorRecord,
)
from app.services.platform.cache import Namespace, cache

logger = logging.getLogger(__name__)

#: Upload ceiling. Annual reports run to 30–40 MB; 64 MB leaves headroom
#: without letting a mis-click consume the disk.
MAX_UPLOAD_BYTES = 64 * 1024 * 1024

#: Terminal-success statuses. "completed" is current; "ready" is the
#: pre-migration spelling retained so an upgraded database keeps working.
INDEXED_STATUSES = frozenset({"completed", "ready"})


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class UploadOutcome:
    """What an upload did — created, duplicate, or a new version."""

    document: Document
    created: bool
    duplicate_of: int | None = None
    superseded: int | None = None

    @property
    def action(self) -> str:
        if self.duplicate_of is not None:
            return "duplicate"
        if self.superseded is not None:
            return "new_version"
        return "created"


class DocumentService:
    """All document operations. One instance per request."""

    def __init__(
        self,
        db: Session,
        *,
        embedder: EmbeddingProvider | None = None,
        pipeline: IngestionPipeline | None = None,
    ) -> None:
        self.db = db
        self.embedder = embedder or HashingEmbeddingProvider()
        self.pipeline = pipeline or IngestionPipeline(self.embedder)

    # ================================================================
    # Upload
    # ================================================================
    def upload(
        self,
        company_id: str,
        payload: bytes,
        filename: str,
        *,
        doc_type: DocumentType | None = None,
        uploaded_by: str | None = None,
        process: bool = True,
    ) -> UploadOutcome:
        """Register an upload, deduplicate it, and queue it for processing."""
        company = self.db.get(Company, company_id)
        if company is None:
            raise DocumentError(f"unknown company '{company_id}'")
        if not payload:
            raise DocumentError("uploaded file is empty")
        if len(payload) > MAX_UPLOAD_BYTES:
            raise DocumentError(
                f"file exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit"
            )
        # Validate the extension before storing anything, so an unsupported
        # file never occupies a row it can never populate.
        file_format = DocumentParser.format_for(filename)

        digest = hash_bytes(payload)
        existing = self.db.scalar(
            select(Document).where(
                Document.company_id == company_id,
                Document.content_hash == digest,
            )
        )
        if existing is not None:
            # Byte-identical re-upload. Return what is already there.
            return UploadOutcome(existing, created=False, duplicate_of=existing.id)

        predecessor = self._find_predecessor(company_id, filename)
        version = (predecessor.version + 1) if predecessor else 1

        document = Document(
            company_id=company_id,
            filename=filename,
            title=filename,
            doc_type=(doc_type or DocumentType.OTHER).value,
            file_format=file_format.value,
            size_bytes=len(payload),
            content_hash=digest,
            version=version,
            status="queued",
            stage=ProcessingStage.QUEUED.value,
            uploaded_by=uploaded_by,
        )
        self.db.add(document)
        self.db.flush()

        superseded: int | None = None
        if predecessor is not None:
            predecessor.superseded_by = document.id
            superseded = predecessor.id

        job = DocumentJob(
            document_id=document.id, company_id=company_id, status="queued",
        )
        self.db.add(job)
        self.db.commit()

        if process:
            self.process(document.id, payload, company_name=company.name,
                         company_ticker=company.ticker)
            self.db.refresh(document)

        return UploadOutcome(document, created=True, superseded=superseded)

    def _find_predecessor(self, company_id: str, filename: str) -> Document | None:
        """The current document with this filename, if any.

        Filename identity is a pragmatic choice, not a perfect one: two
        genuinely different documents saved under the same name would chain as
        versions. It is recorded as a known limitation, and the content hash
        means no information is ever lost when it happens.
        """
        return self.db.scalar(
            select(Document)
            .where(
                Document.company_id == company_id,
                Document.filename == filename,
                Document.superseded_by.is_(None),
            )
            .order_by(Document.version.desc())
        )

    # ================================================================
    # Processing
    # ================================================================
    def process(
        self,
        document_id: int,
        payload: bytes,
        *,
        company_name: str | None = None,
        company_ticker: str | None = None,
    ) -> Document:
        """Run the pipeline and persist everything it produced."""
        document = self.db.get(Document, document_id)
        if document is None:
            raise DocumentError(f"unknown document {document_id}")
        job = self.db.scalar(
            select(DocumentJob).where(DocumentJob.document_id == document_id)
        )

        if company_name is None:
            company = self.db.get(Company, document.company_id)
            company_name = company.name if company else document.company_id
            company_ticker = company.ticker if company else None

        started = time.perf_counter()
        self._mark(document, job, "processing", ProcessingStage.PARSE, 0.05)

        try:
            declared = (
                DocumentType(document.doc_type)
                if document.doc_type and document.doc_type != DocumentType.OTHER.value
                else None
            )
            result = self.pipeline.run(
                payload, document.filename,
                company_name=company_name, company_ticker=company_ticker,
                doc_type=declared,
            )
        except Exception as exc:
            logger.exception("processing failed for document %s", document_id)
            self._fail(document, job, str(exc))
            self.db.commit()
            raise

        self._persist(document, result)
        elapsed = (time.perf_counter() - started) * 1000.0
        document.processing_ms = round(elapsed, 3)
        document.processed_at = _utcnow()
        self._mark(document, job, "ready", ProcessingStage.DONE, 1.0)
        if job is not None:
            job.duration_ms = round(elapsed, 3)
            job.finished_at = _utcnow()
            job.timings = result.timing_map()
        self.db.commit()
        return document

    def _persist(self, document: Document, result: IngestionResult) -> None:
        """Replace all derived rows for a document with this run's output."""
        self._clear_derived(document.id)

        document.title = result.title or document.filename
        document.doc_type = result.doc_type.value
        document.file_format = result.file_format.value
        document.page_count = result.page_count
        document.char_count = result.parsed.char_count
        document.used_ocr = result.parsed.used_ocr
        document.ocr_pages = result.ocr_pages
        document.period = result.period
        document.fiscal_year = result.fiscal_year
        document.embedding_spec = result.embedding_spec
        document.duplicate_ratio = result.duplicate_ratio
        document.doc_metadata = dict(result.parsed.metadata or {})

        for page in result.parsed.pages:
            self.db.add(DocumentPage(
                document_id=document.id, page_number=page.number,
                text=page.text, text_source=page.source.value,
                ocr_confidence=page.ocr_confidence, char_count=page.char_count,
            ))

        for section in result.sections:
            self.db.add(DocumentSection(
                document_id=document.id, kind=section.kind.value,
                title=section.title, start_page=section.start_page,
                end_page=section.end_page, confidence=section.confidence,
            ))

        for table in result.tables:
            self.db.add(DocumentTable(
                document_id=document.id, page=table.page,
                table_index=table.table_index, caption=table.caption,
                unit=table.unit.value, header=list(table.header),
                rows=[list(r) for r in table.rows],
                # JSON cannot key on tuples, so spans are flattened to a list
                # of [row, col, rowspan, colspan] rather than stringified.
                merged=[[r, c, rs, cs] for (r, c), (rs, cs) in table.merged.items()],
                n_rows=table.n_rows, n_cols=table.n_cols,
                confidence=table.confidence,
            ))
        document.table_count = len(result.tables)

        for entity in result.entities:
            self.db.add(DocumentEntity(
                document_id=document.id, company_id=document.company_id,
                kind=entity.kind.value, name=entity.name,
                normalised=entity.normalised, page=entity.page,
                context=entity.context, confidence=entity.confidence,
                mentions=int(entity.attributes.get("mentions", 1)),
                attributes=dict(entity.attributes),
            ))
        document.entity_count = len(result.entities)

        extraction = result.extraction
        if extraction is not None:
            from app.services.documents.pipeline.financials import fiscal_year_of

            for fact in extraction.facts:
                self.db.add(DocumentFact(
                    document_id=document.id, company_id=document.company_id,
                    category=fact.category, field_key=fact.field_key,
                    label=fact.label, value=fact.value, text_value=fact.text,
                    unit=fact.unit.value, period=fact.period,
                    fiscal_year=fiscal_year_of(fact.period),
                    page=fact.page, section=fact.section.value,
                    confidence=fact.confidence, evidence=fact.evidence,
                ))
            document.fact_count = len(extraction.facts)
            document.coverage = extraction.coverage
            document.avg_confidence = extraction.average_confidence

        for chunk, vector in zip(result.chunks, result.embeddings):
            self.db.add(DocumentChunk(
                document_id=document.id, chunk_index=chunk.chunk_index,
                text=chunk.text, page=chunk.page, paragraph=chunk.paragraph,
                section=chunk.section.value, section_title=chunk.section_title,
                token_estimate=chunk.token_estimate,
                fingerprint=chunk.fingerprint, embedding=vector,
            ))
        document.chunk_count = len(result.chunks)

        if result.graph is not None:
            self._persist_graph(document, result.graph)
        self.db.flush()

    def _persist_graph(self, document: Document, graph: KnowledgeGraph) -> None:
        for edge in graph.edges.values():
            source = graph.nodes.get(edge.source)
            target = graph.nodes.get(edge.target)
            if source is None or target is None:
                continue
            existing = self.db.scalar(
                select(DocumentRelation).where(
                    DocumentRelation.company_id == document.company_id,
                    DocumentRelation.source_key == edge.source,
                    DocumentRelation.target_key == edge.target,
                    DocumentRelation.relation == edge.relation.value,
                )
            )
            if existing is not None:
                # An edge seen in a second document is the same edge, better
                # evidenced. Merge rather than duplicate.
                existing.weight += edge.weight
                existing.confidence = max(existing.confidence, edge.confidence)
                existing.pages = sorted(set((existing.pages or []) + edge.pages))
                continue
            self.db.add(DocumentRelation(
                company_id=document.company_id, document_id=document.id,
                source_key=edge.source, target_key=edge.target,
                source_label=source.label, target_label=target.label,
                source_kind=source.kind.value, target_kind=target.kind.value,
                relation=edge.relation.value, weight=edge.weight,
                confidence=edge.confidence, pages=list(edge.pages),
            ))

    def _clear_derived(self, document_id: int) -> None:
        """Delete everything derived, so reprocessing is idempotent."""
        for model in (
            DocumentPage, DocumentSection, DocumentTable, DocumentEntity,
            DocumentFact, DocumentChunk,
        ):
            for row in self.db.scalars(
                select(model).where(model.document_id == document_id)
            ).all():
                self.db.delete(row)
        for row in self.db.scalars(
            select(DocumentRelation).where(
                DocumentRelation.document_id == document_id
            )
        ).all():
            self.db.delete(row)
        self.db.flush()

    def _mark(
        self, document: Document, job: DocumentJob | None,
        status: str, stage: ProcessingStage, progress: float,
    ) -> None:
        document.status = status
        document.stage = stage.value
        document.progress = progress
        if job is not None:
            job.status = status
            job.stage = stage.value
            job.progress = progress
            if status == "processing" and job.started_at is None:
                job.started_at = _utcnow()
                job.attempts += 1

    def _fail(self, document: Document, job: DocumentJob | None, error: str) -> None:
        document.status = "failed"
        document.stage = ProcessingStage.FAILED.value
        document.error = error[:2000]
        if job is not None:
            job.status = "failed"
            job.stage = ProcessingStage.FAILED.value
            job.error = error[:2000]
            job.finished_at = _utcnow()

    # ================================================================
    # Queue
    # ================================================================
    def claim_next_job(self) -> DocumentJob | None:
        """Atomically take the next queued job.

        A conditional UPDATE ... WHERE status='queued' is atomic on both SQLite
        and Postgres, so two workers racing cannot both win. Selecting then
        updating would let them.
        """
        candidate = self.db.scalar(
            select(DocumentJob)
            .where(DocumentJob.status == "queued")
            .order_by(DocumentJob.priority.desc(), DocumentJob.id)
        )
        if candidate is None:
            return None
        claimed = self.db.execute(
            update(DocumentJob)
            .where(DocumentJob.id == candidate.id, DocumentJob.status == "queued")
            .values(status="claimed", started_at=_utcnow())
        )
        self.db.commit()
        if claimed.rowcount == 0:
            return None  # another worker took it
        self.db.refresh(candidate)
        return candidate

    def queue_depth(self) -> dict[str, int]:
        rows = self.db.execute(
            select(DocumentJob.status, func.count(DocumentJob.id))
            .group_by(DocumentJob.status)
        ).all()
        return {status: count for status, count in rows}

    # ================================================================
    # Retrieval
    # ================================================================
    def build_index(
        self, company_id: str | None = None, *, include_superseded: bool = False
    ) -> InMemoryVectorStore:
        """Load persisted chunks into a searchable index.

        Superseded versions are excluded by default. Their chunks remain in the
        database — an old citation must still resolve — but a search should
        return what the company says now, not what it said two filings ago.
        """
        store = InMemoryVectorStore()
        query = (
            select(DocumentChunk, Document)
            .join(Document, DocumentChunk.document_id == Document.id)
            .where(Document.status.in_(INDEXED_STATUSES))
        )
        if company_id is not None:
            query = query.where(Document.company_id == company_id)
        if not include_superseded:
            query = query.where(Document.superseded_by.is_(None))

        records: list[VectorRecord] = []
        for chunk, document in self.db.execute(query).all():
            records.append(VectorRecord(
                chunk_id=chunk.id,
                document_id=document.id,
                text=chunk.text,
                page=chunk.page,
                paragraph=chunk.paragraph,
                section=_section_or_unknown(chunk.section),
                document_title=document.title or document.filename,
                document_version=document.version,
                vector=list(chunk.embedding or []),
                metadata={
                    "doc_type": document.doc_type,
                    "period": document.period or "",
                },
            ))
        if records:
            store.add(records, self.embedder.spec)
        return store

    def search(
        self,
        query: str,
        *,
        company_id: str | None = None,
        top_k: int = 8,
        document_ids: list[int] | None = None,
        sections: list[SectionKind] | None = None,
    ) -> SearchAnswer:
        """Answer a question from the indexed corpus, with citations.

        Cached (Phase 2). Retrieval over an unchanged corpus is deterministic,
        and `build_index` reloads and re-embeds every chunk for the company on
        each call — so the fifteen sections of a report paid that cost fifteen
        times over for a corpus that could not have changed between them.
        Ingestion and reindexing invalidate the namespace explicitly, so a
        newly uploaded report is visible immediately rather than after the TTL.
        """
        def compute() -> SearchAnswer:
            hybrid = self._hybrid_answer(
                query, company_id, top_k, document_ids,
            )
            if hybrid is not None:
                return hybrid
            store = self.build_index(company_id)
            engine = DocumentSearch(
                store, self.embedder, SearchConfig(top_k=top_k),
            )
            return engine.answer(
                query, top_k=top_k, document_ids=document_ids,
                sections=sections,
            )

        return cache.get_or_set(
            Namespace.RAG, compute,
            company_id, query, top_k,
            # Both narrow the result set, so they must be part of the key or a
            # filtered search would serve an unfiltered answer.
            sorted(document_ids) if document_ids else None,
            sorted(s.value for s in sections) if sections else None,
            # The embedding model is part of the identity of a result: change
            # it and every cached answer is from a different index.
            self.embedder.spec,
        )

    def _hybrid_answer(
        self, query: str, company_id: str | None, top_k: int,
        document_ids: list[int] | None,
    ) -> SearchAnswer | None:
        """Answer via the pgvector hybrid engine, or None to fall back.

        Returns None rather than an empty answer whenever the new engine
        cannot serve the query — disabled, no semantic vectors yet, or no
        pgvector. The caller then uses the legacy in-memory index, so the
        migration is invisible to every consumer and a half-embedded corpus
        still answers questions.
        """
        from app.core.config import settings

        if not getattr(settings, "HYBRID_RETRIEVAL_ENABLED", True):
            return None

        try:
            from app.services.retrieval.engine import HybridRetrievalEngine

            engine = HybridRetrievalEngine(self.db)
            results = engine.retrieve(
                query, company_id=company_id, top_k=top_k,
                document_ids=document_ids,
            )
        except Exception:  # noqa: BLE001 — never lose an answer to the new path
            log.exception("hybrid retrieval failed; using legacy index")
            return None

        if not results:
            return None

        hits = [
            SearchHit(
                chunk_id=r.chunk_id, document_id=r.document_id,
                document_title=r.document_title, text=r.text,
                page=r.page, paragraph=r.paragraph,
                section=_section_or_unknown(r.section),
                score=r.score,
                lexical_score=r.raw.get("lexical", 0.0),
                semantic_score=r.raw.get("semantic", 0.0),
            )
            for r in results
        ]
        return SearchAnswer(
            query=query, hits=hits,
            answer=hits[0].text[:1200] if hits else "",
            confidence=results[0].confidence if results else 0.0,
        )

    def citations_for(self, answer: SearchAnswer, limit: int = 8) -> list[DocumentCitation]:
        return cite_all(answer.hits, limit=limit)

    def reindex(self, company_id: str | None = None) -> int:
        """Re-embed stored chunks without re-parsing the source files.

        This is the incremental path the brief asks for. Changing the embedding
        model, or recovering from a corrupt index, costs one embedding pass
        rather than a full OCR-and-parse of the whole corpus.
        """
        query = (
            select(DocumentChunk)
            .join(Document, DocumentChunk.document_id == Document.id)
            .where(Document.status.in_(INDEXED_STATUSES))
        )
        if company_id is not None:
            query = query.where(Document.company_id == company_id)
        chunks = list(self.db.scalars(query).all())
        if not chunks:
            return 0

        vectors = self.embedder.embed([c.text for c in chunks])
        for chunk, vector in zip(chunks, vectors):
            chunk.embedding = vector

        spec = self.embedder.spec.key
        documents = {c.document_id for c in chunks}
        for document_id in documents:
            document = self.db.get(Document, document_id)
            if document is not None:
                document.embedding_spec = spec
        self.db.commit()
        # The corpus these answers were drawn from no longer exists in the
        # same form. Serving a cached hit from the previous embedding model
        # would return citations into an index that has been replaced.
        cache.invalidate(Namespace.RAG)
        return len(chunks)

    # ================================================================
    # Reads
    # ================================================================
    def list_documents(
        self,
        company_id: str | None = None,
        *,
        doc_type: DocumentType | None = None,
        status: str | None = None,
        include_superseded: bool = True,
    ) -> list[Document]:
        query = select(Document)
        if company_id is not None:
            query = query.where(Document.company_id == company_id)
        if doc_type is not None:
            query = query.where(Document.doc_type == doc_type.value)
        if status is not None:
            query = query.where(Document.status == status)
        if not include_superseded:
            query = query.where(Document.superseded_by.is_(None))
        return list(self.db.scalars(query.order_by(Document.id.desc())).all())

    def get(self, document_id: int) -> Document | None:
        return self.db.get(Document, document_id)

    def chunks(
        self, document_id: int, *, limit: int = 200, offset: int = 0
    ) -> list[DocumentChunk]:
        return list(self.db.scalars(
            select(DocumentChunk)
            .where(DocumentChunk.document_id == document_id)
            .order_by(DocumentChunk.chunk_index)
            .offset(offset).limit(limit)
        ).all())

    def tables(self, document_id: int) -> list[DocumentTable]:
        return list(self.db.scalars(
            select(DocumentTable)
            .where(DocumentTable.document_id == document_id)
            .order_by(DocumentTable.page, DocumentTable.table_index)
        ).all())

    def entities(
        self,
        *,
        company_id: str | None = None,
        document_id: int | None = None,
        kind: EntityKind | None = None,
        min_confidence: float = 0.0,
    ) -> list[DocumentEntity]:
        query = select(DocumentEntity).where(
            DocumentEntity.confidence >= min_confidence
        )
        if company_id is not None:
            query = query.where(DocumentEntity.company_id == company_id)
        if document_id is not None:
            query = query.where(DocumentEntity.document_id == document_id)
        if kind is not None:
            query = query.where(DocumentEntity.kind == kind.value)
        return list(self.db.scalars(
            query.order_by(DocumentEntity.confidence.desc())
        ).all())

    def facts(
        self,
        *,
        company_id: str | None = None,
        document_id: int | None = None,
        category: FieldCategory | None = None,
        field_key: str | None = None,
        period: str | None = None,
    ) -> list[DocumentFact]:
        query = select(DocumentFact)
        if company_id is not None:
            query = query.where(DocumentFact.company_id == company_id)
        if document_id is not None:
            query = query.where(DocumentFact.document_id == document_id)
        if category is not None:
            query = query.where(DocumentFact.category == category.value)
        if field_key is not None:
            query = query.where(DocumentFact.field_key == field_key)
        if period is not None:
            query = query.where(DocumentFact.period == period)
        return list(self.db.scalars(
            query.order_by(
                DocumentFact.category, DocumentFact.field_key, DocumentFact.period
            )
        ).all())

    def sections(self, document_id: int) -> list[DocumentSection]:
        return list(self.db.scalars(
            select(DocumentSection)
            .where(DocumentSection.document_id == document_id)
            .order_by(DocumentSection.start_page)
        ).all())

    def pages(self, document_id: int) -> list[DocumentPage]:
        return list(self.db.scalars(
            select(DocumentPage)
            .where(DocumentPage.document_id == document_id)
            .order_by(DocumentPage.page_number)
        ).all())

    # ================================================================
    # Knowledge graph
    # ================================================================
    def graph(self, company_id: str, *, min_confidence: float = 0.0) -> dict:
        """The company's graph, assembled from every document processed."""
        company = self.db.get(Company, company_id)
        if company is None:
            raise DocumentError(f"unknown company '{company_id}'")

        edges = list(self.db.scalars(
            select(DocumentRelation).where(
                DocumentRelation.company_id == company_id,
                DocumentRelation.confidence >= min_confidence,
            )
        ).all())

        builder = KnowledgeGraphBuilder(company.name, company.ticker)
        graph = builder.graph
        for row in edges:
            graph.add_node(
                EntityKind(row.source_kind), row.source_label, weight=0.0
            )
            graph.add_node(
                EntityKind(row.target_kind), row.target_label, weight=0.0
            )
            graph.add_edge(
                row.source_key, row.target_key, RelationKind(row.relation),
                pages=list(row.pages or []), confidence=row.confidence,
                weight=row.weight,
            )
        payload = graph.to_dict()
        payload["company"] = {
            "id": company.id, "name": company.name, "ticker": company.ticker,
            "subject_key": node_key(EntityKind.COMPANY, company.name),
        }
        return payload

    # ================================================================
    # Coverage
    # ================================================================
    def coverage(self, company_id: str) -> dict:
        """Extraction coverage across the 73 spec fields.

        This is the workbook's Section 3 panel, computed from real data. It
        reports what was *not* found as prominently as what was, because a
        coverage figure without its complement invites the reader to assume the
        remainder does not exist.
        """
        facts = self.facts(company_id=company_id)
        best: dict[str, float] = {}
        for fact in facts:
            best[fact.field_key] = max(best.get(fact.field_key, 0.0), fact.confidence)

        per_category: list[dict] = []
        for category in FieldCategory:
            specs = [s for s in FIELDS_BY_KEY.values() if s.category is category]
            found = [s.key for s in specs if s.key in best]
            confidences = [best[k] for k in found]
            per_category.append({
                "category": category.value,
                "defined": len(specs),
                "extracted": len(found),
                "coverage": round(len(found) / len(specs), 4) if specs else 0.0,
                "avg_confidence": round(sum(confidences) / len(confidences), 4)
                if confidences else 0.0,
                "missing": sorted(s.key for s in specs if s.key not in best),
            })

        documents = self.list_documents(company_id, include_superseded=False)
        return {
            "company_id": company_id,
            "fields_defined": FIELD_COUNT,
            "fields_extracted": len(best),
            "coverage": round(len(best) / FIELD_COUNT, 4) if FIELD_COUNT else 0.0,
            "avg_confidence": round(sum(best.values()) / len(best), 4) if best else 0.0,
            "documents": len(documents),
            "documents_ready": sum(1 for d in documents if d.status in INDEXED_STATUSES),
            "categories": per_category,
        }

    def statistics(self, company_id: str | None = None) -> dict:
        """Corpus-level counters for the dashboard."""
        documents = self.list_documents(company_id)
        current = [d for d in documents if d.superseded_by is None]
        ocr = OcrEngine()
        return {
            "documents": len(documents),
            "current_documents": len(current),
            "superseded": len(documents) - len(current),
            "pages": sum(d.page_count for d in documents),
            "chunks": sum(d.chunk_count for d in documents),
            "tables": sum(d.table_count for d in documents),
            "entities": sum(d.entity_count for d in documents),
            "facts": sum(d.fact_count for d in documents),
            "ocr_documents": sum(1 for d in documents if d.used_ocr),
            "by_type": _tally(d.doc_type for d in documents),
            "by_status": _tally(d.status for d in documents),
            "queue": self.queue_depth(),
            "embedding": {
                "provider": self.embedder.spec.provider,
                "model": self.embedder.spec.model,
                "dimension": self.embedder.spec.dimension,
            },
            "ocr": ocr.describe(),
            "supported_formats": sorted(
                {fmt.value for fmt in _supported_formats()}
            ),
        }


# ---------------------------------------------------------------------------
def _tally(values: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def _section_or_unknown(value: str | None) -> SectionKind:
    try:
        return SectionKind(value) if value else SectionKind.UNKNOWN
    except ValueError:
        return SectionKind.UNKNOWN


def _supported_formats():
    from app.services.documents.extractors.base import registered_formats

    return registered_formats()
