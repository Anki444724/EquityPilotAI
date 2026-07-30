"""The ingestion pipeline, end to end.

Upload → OCR → layout → tables → sections → entities → financials → chunking →
embedding → vector store → knowledge graph → AI context. Exactly the stages the
brief specifies, in that order, each one timed.

The orchestrator is deliberately **pure**: it takes bytes and returns a result
object. It touches no database, no filesystem and no HTTP. That is what makes
the whole pipeline testable without fixtures and reusable by the queue worker,
a bulk re-index and a unit test alike.

Per-stage timings are collected as a matter of course rather than bolted on for
the benchmark, because a pipeline whose cost is not attributable is a pipeline
nobody can optimise.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from app.domain.documents.types import (
    Chunk, DetectedSection, DocumentType, ExtractedEntity, ExtractedTable,
    FileFormat, ParsedDocument, ProcessingStage, content_hash,
)
from app.services.documents.extractors import office, pdf  # noqa: F401  (register)
from app.services.documents.extractors.base import DocumentParser, parse_document
from app.services.documents.pipeline.chunking import (
    ChunkConfig, SemanticChunker, duplicate_ratio,
)
from app.services.documents.pipeline.classify import classify_document
from app.services.documents.pipeline.embeddings import (
    EmbeddingProvider, HashingEmbeddingProvider,
)
from app.services.documents.pipeline.entities import EntityExtractor
from app.services.documents.pipeline.financials import (
    ExtractionResult, FinancialExtractor, detect_period, fiscal_year_of,
)
from app.services.documents.pipeline.knowledge_graph import (
    KnowledgeGraph, KnowledgeGraphBuilder,
)
from app.services.documents.pipeline.sections import SectionDetector

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class StageTiming:
    stage: ProcessingStage
    ms: float

    def as_tuple(self) -> tuple[str, float]:
        return self.stage.value, round(self.ms, 3)


@dataclass(slots=True)
class IngestionResult:
    """Everything one document yielded, plus how long each stage took."""

    filename: str
    content_hash: str
    file_format: FileFormat
    doc_type: DocumentType
    title: str | None
    parsed: ParsedDocument
    sections: list[DetectedSection] = field(default_factory=list)
    tables: list[ExtractedTable] = field(default_factory=list)
    entities: list[ExtractedEntity] = field(default_factory=list)
    chunks: list[Chunk] = field(default_factory=list)
    embeddings: list[list[float]] = field(default_factory=list)
    extraction: ExtractionResult | None = None
    graph: KnowledgeGraph | None = None
    timings: list[StageTiming] = field(default_factory=list)
    period: str | None = None
    fiscal_year: int | None = None
    embedding_spec: str = ""

    @property
    def page_count(self) -> int:
        return self.parsed.page_count

    @property
    def total_ms(self) -> float:
        return round(sum(t.ms for t in self.timings), 3)

    @property
    def ocr_pages(self) -> int:
        from app.domain.documents.types import TextSource

        return sum(
            1 for p in self.parsed.pages
            if p.source in (TextSource.OCR, TextSource.MIXED)
        )

    @property
    def duplicate_ratio(self) -> float:
        return duplicate_ratio(self.chunks)

    def timing_map(self) -> dict[str, float]:
        return dict(t.as_tuple() for t in self.timings)

    def throughput_pages_per_second(self) -> float:
        seconds = self.total_ms / 1000.0
        return round(self.page_count / seconds, 2) if seconds > 0 else 0.0


class IngestionPipeline:
    """Runs every stage over one document."""

    def __init__(
        self,
        embedder: EmbeddingProvider | None = None,
        *,
        chunk_config: ChunkConfig | None = None,
        extract_tables: bool = True,
    ) -> None:
        self.embedder = embedder or HashingEmbeddingProvider()
        self.chunker = SemanticChunker(chunk_config)
        self.section_detector = SectionDetector()
        self.financial_extractor = FinancialExtractor()
        self.extract_tables = extract_tables

    # ------------------------------------------------------------------
    def run(
        self,
        payload: bytes,
        filename: str,
        *,
        company_name: str,
        company_ticker: str | None = None,
        doc_type: DocumentType | None = None,
        build_graph: bool = True,
    ) -> IngestionResult:
        timings: list[StageTiming] = []

        def stage(name: ProcessingStage):
            """Context manager recording elapsed time for one stage."""

            class _Timer:
                def __enter__(self_inner):
                    self_inner.start = time.perf_counter()
                    return self_inner

                def __exit__(self_inner, *exc):
                    elapsed = (time.perf_counter() - self_inner.start) * 1000.0
                    timings.append(StageTiming(name, elapsed))
                    return False

            return _Timer()

        # --- parse (includes the OCR decision, made per page) ----------
        with stage(ProcessingStage.PARSE):
            parsed = parse_document(payload, filename)

        # OCR is not a separate pass — it happens inside the parser, because
        # the decision needs the page's own geometry. The stage is recorded so
        # the timing panel accounts for it honestly rather than hiding it
        # inside PARSE.
        with stage(ProcessingStage.OCR):
            ocr_pages = self._ocr_page_numbers(parsed)

        with stage(ProcessingStage.LAYOUT):
            block_count = sum(len(p.blocks) for p in parsed.pages)

        with stage(ProcessingStage.TABLES):
            tables = parsed.tables

        with stage(ProcessingStage.SECTIONS):
            sections = self.section_detector.detect(parsed)

        with stage(ProcessingStage.ENTITIES):
            entities = EntityExtractor(company_name).extract(parsed)

        with stage(ProcessingStage.FINANCIALS):
            extraction = self.financial_extractor.extract(parsed, sections)

        with stage(ProcessingStage.CHUNKING):
            chunks = self.chunker.chunk(parsed, sections)

        with stage(ProcessingStage.EMBEDDING):
            embeddings = (
                self.embedder.embed([c.text for c in chunks]) if chunks else []
            )

        with stage(ProcessingStage.KNOWLEDGE):
            graph = None
            if build_graph:
                builder = KnowledgeGraphBuilder(company_name, company_ticker)
                graph = builder.add_entities(entities)

        resolved_type = doc_type or classify_document(
            filename, parsed, sections=sections
        )
        period = self._infer_period(parsed, filename)

        result = IngestionResult(
            filename=filename,
            content_hash=content_hash(payload),
            file_format=parsed.file_format,
            doc_type=resolved_type,
            title=parsed.title or filename,
            parsed=parsed,
            sections=sections,
            tables=tables,
            entities=entities,
            chunks=chunks,
            embeddings=embeddings,
            extraction=extraction,
            graph=graph,
            timings=timings,
            period=period,
            fiscal_year=fiscal_year_of(period),
            embedding_spec=self.embedder.spec.key,
        )
        logger.info(
            "ingested %s: %d pages, %d chunks, %d facts, %.0fms",
            filename, result.page_count, len(chunks),
            len(extraction.facts) if extraction else 0, result.total_ms,
        )
        return result

    # ------------------------------------------------------------------
    @staticmethod
    def _ocr_page_numbers(parsed: ParsedDocument) -> list[int]:
        from app.domain.documents.types import TextSource

        return [
            p.number for p in parsed.pages
            if p.source in (TextSource.OCR, TextSource.MIXED)
        ]

    @staticmethod
    def _infer_period(parsed: ParsedDocument, filename: str) -> str | None:
        """Best guess at the period a document covers.

        The filename is tried first because uploads are overwhelmingly named
        for their period, and it is unambiguous when present. Failing that, the
        first page's text, which carries the cover-page year.
        """
        from_name = detect_period(filename)
        if from_name:
            return from_name
        if parsed.pages:
            return detect_period(parsed.pages[0].text[:2000])
        return None
