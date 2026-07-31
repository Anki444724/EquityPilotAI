"""Core document-intelligence types.

The organising idea of Module 7 is that a document is not text — it is a
*provenance chain*. Every fact the platform learns from a filing must be able
to answer: which document, which version, which page, which section, which
paragraph. A number that cannot answer those questions is not evidence, it is
a rumour, and the citation framework refuses it.

Everything here is transport-, parser- and vendor-agnostic. A PDF, a DOCX and
a spreadsheet all reduce to the same :class:`ParsedPage` / :class:`ExtractedTable`
shape, so nothing downstream of the parsers knows what the source format was.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------
class DocumentType(StrEnum):
    """The document classes the brief and the workbook register both name.

    The workbook lists 12 register rows; the brief names eight analytical
    classes. The union is modelled here, with ``OTHER`` as the honest fallback
    rather than a silent misclassification.
    """

    ANNUAL_REPORT = "annual_report"
    QUARTERLY_REPORT = "quarterly_report"
    INVESTOR_PRESENTATION = "investor_presentation"
    CONFERENCE_CALL = "conference_call"
    CREDIT_RATING = "credit_rating"
    DRHP = "drhp"
    ESG_REPORT = "esg_report"
    EXCHANGE_FILING = "exchange_filing"
    RESEARCH_NOTE = "research_note"
    OTHER = "other"


class FileFormat(StrEnum):
    """Supported upload formats."""

    PDF = "pdf"
    DOCX = "docx"
    TXT = "txt"
    MARKDOWN = "md"
    HTML = "html"
    CSV = "csv"
    XLSX = "xlsx"


#: Extension → format. Single source of truth; the API validates against this.
EXTENSION_MAP: dict[str, FileFormat] = {
    ".pdf": FileFormat.PDF,
    ".docx": FileFormat.DOCX,
    ".txt": FileFormat.TXT,
    ".md": FileFormat.MARKDOWN,
    ".markdown": FileFormat.MARKDOWN,
    ".html": FileFormat.HTML,
    ".htm": FileFormat.HTML,
    ".csv": FileFormat.CSV,
    ".xlsx": FileFormat.XLSX,
    ".xlsm": FileFormat.XLSX,
}


class DocumentStatus(StrEnum):
    """Lifecycle of an uploaded document.

    Previously these were bare strings written at four call sites, which is
    how a document could be marked "ready" while holding zero chunks. The
    vocabulary is now declared once and the transitions are checked.

    UPLOADED is distinct from QUEUED on purpose: the bytes are durably stored
    the moment the request returns, before any job exists. If enqueueing
    itself fails the document is still safe on disk and can be retried, which
    is the whole point of storing the original.
    """

    UPLOADED = "uploaded"
    QUEUED = "queued"
    PROCESSING = "processing"
    OCR_COMPLETE = "ocr_complete"
    CHUNKED = "chunked"
    EMBEDDED = "embedded"
    COMPLETED = "completed"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        return self in {DocumentStatus.COMPLETED, DocumentStatus.FAILED}

    @property
    def is_indexed(self) -> bool:
        """Is this document usable by search and the AI layer?"""
        return self is DocumentStatus.COMPLETED


#: Fraction complete when a status is reached, for the progress bar. Parsing
#: and OCR dominate a scanned report, so the early stages carry most of the
#: weight; embedding is fast by comparison.
STATUS_PROGRESS: dict[DocumentStatus, float] = {
    DocumentStatus.UPLOADED: 0.0,
    DocumentStatus.QUEUED: 0.02,
    DocumentStatus.PROCESSING: 0.05,
    DocumentStatus.OCR_COMPLETE: 0.45,
    DocumentStatus.CHUNKED: 0.70,
    DocumentStatus.EMBEDDED: 0.90,
    DocumentStatus.COMPLETED: 1.0,
    DocumentStatus.FAILED: 1.0,
}


class ProcessingStage(StrEnum):
    """The pipeline stages, in execution order.

    Declared as an enum rather than strings so the queue, the progress bar and
    the tests all agree on the vocabulary.
    """

    QUEUED = "queued"
    PARSE = "parse"
    OCR = "ocr"
    LAYOUT = "layout"
    TABLES = "tables"
    SECTIONS = "sections"
    ENTITIES = "entities"
    FINANCIALS = "financials"
    CHUNKING = "chunking"
    EMBEDDING = "embedding"
    INDEXING = "indexing"
    KNOWLEDGE = "knowledge"
    DONE = "done"
    FAILED = "failed"


#: Ordered stages actually executed by the worker (excludes terminal states).
PIPELINE_STAGES: tuple[ProcessingStage, ...] = (
    ProcessingStage.PARSE,
    ProcessingStage.OCR,
    ProcessingStage.LAYOUT,
    ProcessingStage.TABLES,
    ProcessingStage.SECTIONS,
    ProcessingStage.ENTITIES,
    ProcessingStage.FINANCIALS,
    ProcessingStage.CHUNKING,
    ProcessingStage.EMBEDDING,
    ProcessingStage.INDEXING,
    ProcessingStage.KNOWLEDGE,
)


class SectionKind(StrEnum):
    """Sections the brief requires the engine to identify."""

    BUSINESS_OVERVIEW = "business_overview"
    CHAIRMAN_LETTER = "chairman_letter"
    MANAGEMENT_DISCUSSION = "management_discussion"
    RISK_FACTORS = "risk_factors"
    FINANCIAL_STATEMENTS = "financial_statements"
    NOTES_TO_ACCOUNTS = "notes_to_accounts"
    CORPORATE_GOVERNANCE = "corporate_governance"
    SHAREHOLDING = "shareholding"
    ESG = "esg"
    AUDITOR_REPORT = "auditor_report"
    CONFERENCE_QA = "conference_qa"
    MANAGEMENT_GUIDANCE = "management_guidance"
    DIRECTORS_REPORT = "directors_report"
    UNKNOWN = "unknown"


class EntityKind(StrEnum):
    """Entity classes the brief enumerates."""

    COMPANY = "company"
    PROMOTER = "promoter"
    DIRECTOR = "director"
    SUBSIDIARY = "subsidiary"
    CUSTOMER = "customer"
    SUPPLIER = "supplier"
    COMPETITOR = "competitor"
    PRODUCT = "product"
    SEGMENT = "segment"
    COUNTRY = "country"
    CAPEX = "capex"
    DEBT = "debt"
    GUIDANCE = "guidance"
    ACQUISITION = "acquisition"
    RISK = "risk"
    AUDITOR = "auditor"


class RelationKind(StrEnum):
    """Knowledge-graph edge types."""

    SUBSIDIARY_OF = "subsidiary_of"
    PROMOTER_OF = "promoter_of"
    DIRECTOR_OF = "director_of"
    COMPETES_WITH = "competes_with"
    SUPPLIES_TO = "supplies_to"
    CUSTOMER_OF = "customer_of"
    SELLS_PRODUCT = "sells_product"
    OPERATES_SEGMENT = "operates_segment"
    OPERATES_IN = "operates_in"
    EXPOSED_TO_RISK = "exposed_to_risk"
    ACQUIRED = "acquired"
    AUDITED_BY = "audited_by"
    GUIDES = "guides"
    INVESTS_IN = "invests_in"


class TextSource(StrEnum):
    """How the text of a page was obtained — decides trust and cost."""

    NATIVE = "native"      # embedded text layer, lossless
    OCR = "ocr"            # rasterised and recognised, lossy
    MIXED = "mixed"        # native text plus OCR of image regions
    STRUCTURED = "structured"  # CSV/XLSX cells, no free text at all


# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------
class Unit(StrEnum):
    """Units the workbook's extraction store recognises.

    Unit preservation is a hard requirement of the brief. A table read as
    ₹ million and stored as ₹ crore silently overstates by 10x — exactly the
    class of quiet, plausible error that Module 6's percentage bug taught us
    to design against.
    """

    INR_CRORE = "inr_cr"
    INR_LAKH = "inr_lakh"
    INR_MILLION = "inr_mn"
    INR_BILLION = "inr_bn"
    INR = "inr"
    PERCENT = "percent"
    TIMES = "x"
    YEARS = "years"
    MONTHS = "months"
    COUNT = "count"
    TONNES_CO2 = "tco2e"
    SCORE = "score"
    INDEX = "index"
    BOOLEAN = "yes_no"
    TEXT = "text"
    UNITS = "units"
    PERCENT_OF_REVENUE = "pct_of_revenue"
    UNKNOWN = "unknown"


#: Multiplier converting a unit into ₹ crore. Only monetary units appear.
_TO_CRORE: dict[Unit, float] = {
    Unit.INR_CRORE: 1.0,
    Unit.INR_LAKH: 0.01,
    Unit.INR_MILLION: 0.1,
    Unit.INR_BILLION: 100.0,
    Unit.INR: 1e-7,
}


def to_crore(value: float, unit: Unit) -> float | None:
    """Convert a monetary value to ₹ crore, or ``None`` if not monetary.

    Returning ``None`` rather than the raw value is deliberate: a caller that
    forgets to check gets a visible failure instead of a wrong number.
    """
    factor = _TO_CRORE.get(unit)
    return None if factor is None else value * factor


def is_monetary(unit: Unit) -> bool:
    return unit in _TO_CRORE


# ---------------------------------------------------------------------------
# Parsed primitives
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class TextBlock:
    """A laid-out region of a page.

    ``bbox`` is (x0, y0, x1, y1) in the source coordinate space. It is optional
    because TXT and CSV have no geometry, and pretending otherwise would force
    every consumer to trust coordinates that were invented.
    """

    text: str
    page: int
    bbox: tuple[float, float, float, float] | None = None
    font_size: float | None = None
    bold: bool = False
    block_index: int = 0

    @property
    def is_empty(self) -> bool:
        return not self.text.strip()


@dataclass(slots=True)
class ExtractedTable:
    """A table recovered from a document, with units and headers preserved."""

    page: int
    rows: list[list[str]]
    header: list[str] = field(default_factory=list)
    caption: str | None = None
    unit: Unit = Unit.UNKNOWN
    #: Cells that were spanned in the source, as (row, col) → (rowspan, colspan).
    merged: dict[tuple[int, int], tuple[int, int]] = field(default_factory=dict)
    table_index: int = 0
    confidence: float = 0.0

    @property
    def n_rows(self) -> int:
        return len(self.rows)

    @property
    def n_cols(self) -> int:
        return max((len(r) for r in self.rows), default=0)

    def cell(self, row: int, col: int) -> str:
        """Bounds-safe cell access; ragged tables are the norm, not the exception."""
        if 0 <= row < len(self.rows) and 0 <= col < len(self.rows[row]):
            return self.rows[row][col]
        return ""

    def to_grid(self) -> list[list[str]]:
        """Rectangularise, padding short rows so consumers can index freely."""
        width = self.n_cols
        return [row + [""] * (width - len(row)) for row in self.rows]


@dataclass(slots=True)
class ParsedPage:
    """One page of a document, however 'page' is defined for the format.

    A DOCX has no pages, so the parser synthesises them at a fixed paragraph
    count; a CSV is one page per sheet. The abstraction holds because every
    citation only needs a stable, reproducible page ordinal.
    """

    number: int
    text: str
    blocks: list[TextBlock] = field(default_factory=list)
    tables: list[ExtractedTable] = field(default_factory=list)
    source: TextSource = TextSource.NATIVE
    #: Ratio of characters recovered by OCR, where OCR ran.
    ocr_confidence: float | None = None
    width: float | None = None
    height: float | None = None

    @property
    def char_count(self) -> int:
        return len(self.text)


@dataclass(slots=True)
class ParsedDocument:
    """The complete parser output, before any interpretation."""

    pages: list[ParsedPage] = field(default_factory=list)
    file_format: FileFormat = FileFormat.PDF
    title: str | None = None
    author: str | None = None
    producer: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)
    #: True when the parser had to rasterise and OCR at least one page.
    used_ocr: bool = False

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def full_text(self) -> str:
        return "\n".join(p.text for p in self.pages)

    @property
    def tables(self) -> list[ExtractedTable]:
        return [t for p in self.pages for t in p.tables]

    @property
    def char_count(self) -> int:
        return sum(p.char_count for p in self.pages)


# ---------------------------------------------------------------------------
# Interpreted structures
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class DetectedSection:
    """A contiguous span of a document classified as one of the known sections.

    The span is recorded twice: as a page range, which is what a citation
    shows a reader, and as a *document-order* range over block ordinals, which
    is what actually assigns content to sections. Pages alone are too coarse —
    an annual report routinely starts three sections on one page, and a
    page-granular lookup then attributes two of them to the third.
    """

    kind: SectionKind
    title: str
    start_page: int
    end_page: int
    #: Confidence in the *classification*, not in the text.
    confidence: float = 0.0
    heading_level: int = 1
    #: Global block ordinal of the heading, and of the last block in the span.
    #: ``None`` when the document has no block structure (a scan, a CSV).
    start_order: int | None = None
    end_order: int | None = None

    def contains(self, page: int) -> bool:
        return self.start_page <= page <= self.end_page

    def contains_order(self, order: int) -> bool:
        """Whether a block ordinal falls inside this section."""
        if self.start_order is None:
            return False
        end = self.end_order if self.end_order is not None else order
        return self.start_order <= order <= end

    @property
    def page_span(self) -> int:
        return self.end_page - self.start_page + 1

    @property
    def order_span(self) -> int:
        if self.start_order is None or self.end_order is None:
            return 1 << 30
        return self.end_order - self.start_order + 1


@dataclass(slots=True)
class Chunk:
    """A semantically coherent slice of a document, sized for retrieval.

    Chunks carry their section and page so a retrieved passage can cite itself
    without a second lookup. ``paragraph`` is the ordinal within the page,
    which is what the brief's citation requirement ("Document, page, section,
    paragraph") actually needs.
    """

    text: str
    page: int
    paragraph: int
    section: SectionKind = SectionKind.UNKNOWN
    section_title: str | None = None
    chunk_index: int = 0
    token_estimate: int = 0
    #: SHA-1 of the normalised text — the deduplication key.
    fingerprint: str = ""

    def __post_init__(self) -> None:
        if not self.fingerprint:
            self.fingerprint = text_fingerprint(self.text)
        if not self.token_estimate:
            self.token_estimate = estimate_tokens(self.text)


@dataclass(slots=True)
class ExtractedEntity:
    """A named thing found in a document, with the evidence that found it."""

    kind: EntityKind
    name: str
    page: int
    context: str = ""
    confidence: float = 0.0
    #: Normalised form used for graph identity and deduplication.
    normalised: str = ""
    attributes: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.normalised:
            self.normalised = normalise_entity(self.name)


@dataclass(slots=True)
class ExtractedFact:
    """A structured value pulled from a document — a row of the workbook's AI-2 store.

    This is the type that crosses back into the rest of the platform: it can be
    promoted into the canonical financial store, and it is what lifts scoring
    confidence out of the qualitative gap Module 5 left open.
    """

    category: str
    field_key: str
    label: str
    value: float | None = None
    text: str | None = None
    unit: Unit = Unit.UNKNOWN
    period: str | None = None
    page: int = 0
    section: SectionKind = SectionKind.UNKNOWN
    confidence: float = 0.0
    #: Verbatim source span, so a reviewer can check the extraction by eye.
    evidence: str = ""

    @property
    def is_numeric(self) -> bool:
        return self.value is not None

    def value_in_crore(self) -> float | None:
        if self.value is None:
            return None
        return to_crore(self.value, self.unit)


@dataclass(slots=True)
class GraphNode:
    key: str
    kind: EntityKind
    label: str
    weight: float = 1.0
    attributes: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class GraphEdge:
    source: str
    target: str
    relation: RelationKind
    weight: float = 1.0
    #: Pages on which the relationship was observed — the edge's own citation.
    pages: list[int] = field(default_factory=list)
    confidence: float = 0.0


@dataclass(slots=True)
class SearchHit:
    """One retrieved passage."""

    chunk_id: int
    document_id: int
    document_title: str
    page: int
    paragraph: int
    section: SectionKind
    text: str
    score: float
    #: Component scores, exposed so a low-confidence answer can be explained.
    lexical_score: float = 0.0
    semantic_score: float = 0.0


@dataclass(slots=True)
class SearchAnswer:
    """The search API's response shape: answer, evidence, pages, confidence."""

    query: str
    answer: str
    hits: list[SearchHit] = field(default_factory=list)
    confidence: float = 0.0
    #: Present when the corpus genuinely has nothing — never a guess.
    unavailable_reason: str | None = None


# ---------------------------------------------------------------------------
# Helpers shared across the module — each defined exactly once
# ---------------------------------------------------------------------------
_WHITESPACE = re.compile(r"\s+")
_NON_WORD = re.compile(r"[^\w\s%₹.\-/&]")
#: Legal-form suffixes stripped when normalising a company-like entity name.
_SUFFIXES = (
    "private limited", "pvt ltd", "pvt. ltd.", "limited", "ltd", "ltd.",
    "inc", "inc.", "llp", "plc", "corporation", "corp", "corp.", "company",
    "co.", "&amp; co",
)


def normalise_whitespace(text: str) -> str:
    """Collapse runs of whitespace. Used everywhere; defined here only."""
    return _WHITESPACE.sub(" ", text).strip()


def text_fingerprint(text: str) -> str:
    """Stable hash of normalised text — the deduplication and versioning key.

    Case- and whitespace-insensitive, because the same boilerplate paragraph
    reformatted across two annual reports is the same paragraph.
    """
    canonical = normalise_whitespace(_NON_WORD.sub(" ", text.lower()))
    return hashlib.sha1(canonical.encode("utf-8"), usedforsecurity=False).hexdigest()


def content_hash(payload: bytes) -> str:
    """SHA-256 of raw bytes — identifies an exact file, for version detection."""
    return hashlib.sha256(payload).hexdigest()


def estimate_tokens(text: str) -> int:
    """Cheap token estimate.

    Deliberately approximate: this budgets prompt space, it does not bill.
    Four characters per token is the conventional English heuristic and errs
    slightly high for financial prose, which is the safe direction.
    """
    return max(1, (len(text) + 3) // 4)


def normalise_entity(name: str) -> str:
    """Canonical key for an entity, so 'Acme Ltd.' and 'ACME Limited' unify."""
    cleaned = normalise_whitespace(name.lower().replace(",", " "))
    for suffix in sorted(_SUFFIXES, key=len, reverse=True):
        if cleaned.endswith(" " + suffix):
            cleaned = cleaned[: -len(suffix) - 1]
            break
    return normalise_whitespace(cleaned.strip(" .")) or normalise_whitespace(name.lower())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class DocumentError(Exception):
    """Base class for document-pipeline failures."""


class UnsupportedFormat(DocumentError):
    """The upload's extension is not in :data:`EXTENSION_MAP`."""


class ParseFailure(DocumentError):
    """A parser could not read the file at all."""


class OcrUnavailable(DocumentError):
    """OCR was required but no engine is installed.

    Raised rather than silently returning empty text: a scanned document that
    yields nothing must look like a failure, not like an empty filing.
    """
