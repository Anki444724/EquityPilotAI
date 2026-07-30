"""Report content model — the block tree.

The brief's hardest constraint is "never use static templates". The way to
honour that is to make a report a **data structure**, not a document: a tree of
typed blocks that each renderer walks. A PDF, a Word file, a spreadsheet and an
HTML page are then four traversals of one tree, not four templates that must be
kept in agreement.

The consequence worth stating: adding a section means emitting blocks, and
every output format gains it at once. Nothing in this module contains a
per-format layout of a section, and a test enforces that by asserting each
renderer handles the full block vocabulary.

The second constraint — "only include sections that have sufficient evidence"
— is why every block carries `evidence` and why :class:`Section` can be
`INSUFFICIENT`. A section is not silently dropped when data is missing; it is
rendered with an explicit statement that the evidence was not there. Silence
would let a reader assume the analysis was done and found nothing notable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Any, Iterator, Sequence


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------
class BlockKind(StrEnum):
    """The complete block vocabulary. Every renderer must handle all of these."""

    HEADING = "heading"
    PARAGRAPH = "paragraph"
    BULLETS = "bullets"
    KEY_VALUE = "key_value"
    TABLE = "table"
    METRIC_GRID = "metric_grid"
    CHART = "chart"
    CALLOUT = "callout"
    QUOTE = "quote"
    DIVIDER = "divider"
    PAGE_BREAK = "page_break"
    INSUFFICIENT = "insufficient"
    CITATION_LIST = "citation_list"


class CalloutTone(StrEnum):
    NEUTRAL = "neutral"
    POSITIVE = "positive"
    WARNING = "warning"
    NEGATIVE = "negative"


class EvidenceSource(StrEnum):
    """Which engine produced a figure.

    The brief requires every factual statement to reference its evidence, and
    names these five. A citation without a source is not a citation.
    """

    FINANCIAL = "financial_engine"
    VALUATION = "valuation_engine"
    SCORING = "scoring_engine"
    FORECAST = "forecast_engine"
    DOCUMENT = "document_engine"
    PORTFOLIO = "portfolio_engine"
    AI = "ai_layer"
    MARKET = "market_data"


class ReportType(StrEnum):
    """The six report types the brief specifies."""

    QUICK = "quick"
    INSTITUTIONAL = "institutional"
    IC_MEMO = "ic_memo"
    QUARTERLY_UPDATE = "quarterly_update"
    INITIATION = "initiation"
    DEEP_RESEARCH = "deep_research"


class SectionKey(StrEnum):
    """Every section the brief enumerates, in canonical report order."""

    COVER = "cover"
    TOC = "table_of_contents"
    EXECUTIVE_SUMMARY = "executive_summary"
    INVESTMENT_THESIS = "investment_thesis"
    BUSINESS_OVERVIEW = "business_overview"
    INDUSTRY_ANALYSIS = "industry_analysis"
    FINANCIAL_ANALYSIS = "financial_analysis"
    FORECAST = "forecast"
    VALUATION = "valuation"
    DCF = "dcf"
    RELATIVE_VALUATION = "relative_valuation"
    INSTITUTIONAL_SCORE = "institutional_score"
    MANAGEMENT = "management"
    MOAT = "moat"
    RISK_ANALYSIS = "risk_analysis"
    SCENARIO_ANALYSIS = "scenario_analysis"
    PEER_COMPARISON = "peer_comparison"
    PORTFOLIO_FIT = "portfolio_fit"
    APPENDIX = "appendix"


#: Canonical order. A report renders its sections in this sequence regardless
#: of the order they were requested or built in, so two reports of the same
#: type are always comparable page by page.
SECTION_ORDER: tuple[SectionKey, ...] = tuple(SectionKey)


class ChartKind(StrEnum):
    """The ten charts the brief lists."""

    REVENUE = "revenue"
    EBITDA = "ebitda"
    PAT = "pat"
    MARGINS = "margins"
    CASH_FLOW = "cash_flow"
    DCF = "dcf"
    SENSITIVITY = "sensitivity"
    PEER_COMPARISON = "peer_comparison"
    SCORE_RADAR = "score_radar"
    PORTFOLIO_ALLOCATION = "portfolio_allocation"


class Theme(StrEnum):
    LIGHT = "light"
    DARK = "dark"


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Evidence:
    """A reference from a statement back to the engine that produced it.

    Deliberately close to Module 6's `Citation` but not the same type: an AI
    citation points at a figure in a grounded prompt, whereas this points at a
    *report* fact and must survive being rendered into a static PDF that has no
    access to the platform. It carries its own value.
    """

    key: str
    label: str
    source: EvidenceSource
    value: float | str | None = None
    unit: str = ""
    detail: str = ""
    fiscal_year: int | None = None

    @property
    def marker(self) -> str:
        return f"[{self.key}]"

    def render(self) -> str:
        parts = [self.label]
        if self.value is not None:
            formatted = (
                f"{self.value:,.2f}" if isinstance(self.value, float)
                else str(self.value)
            )
            parts.append(f"{formatted}{f' {self.unit}' if self.unit else ''}")
        parts.append(self.source.value.replace("_", " "))
        if self.detail:
            parts.append(self.detail)
        return " · ".join(parts)


# ---------------------------------------------------------------------------
# Blocks
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class Block:
    """Base block. Every block may carry evidence and a note."""

    kind: BlockKind
    evidence: list[Evidence] = field(default_factory=list)
    #: Rendered in a smaller face beneath the block — units, caveats, sourcing.
    note: str = ""

    def evidence_keys(self) -> set[str]:
        return {e.key for e in self.evidence}


@dataclass(slots=True)
class Heading(Block):
    text: str = ""
    level: int = 2

    def __init__(self, text: str, level: int = 2, **kwargs):
        super().__init__(BlockKind.HEADING, **kwargs)
        self.text = text
        self.level = level


@dataclass(slots=True)
class Paragraph(Block):
    """Body prose. `text` may contain `[evidence_key]` markers."""

    text: str = ""

    def __init__(self, text: str, **kwargs):
        super().__init__(BlockKind.PARAGRAPH, **kwargs)
        self.text = text


@dataclass(slots=True)
class Bullets(Block):
    items: list[str] = field(default_factory=list)
    ordered: bool = False

    def __init__(self, items: Sequence[str], ordered: bool = False, **kwargs):
        super().__init__(BlockKind.BULLETS, **kwargs)
        self.items = list(items)
        self.ordered = ordered


@dataclass(slots=True)
class KeyValue(Block):
    """Label/value pairs — the workbook's snapshot panels."""

    pairs: list[tuple[str, str]] = field(default_factory=list)
    columns: int = 2

    def __init__(self, pairs: Sequence[tuple[str, str]], columns: int = 2, **kwargs):
        super().__init__(BlockKind.KEY_VALUE, **kwargs)
        self.pairs = [(str(k), str(v)) for k, v in pairs]
        self.columns = columns


@dataclass(slots=True)
class Table(Block):
    """A data table. `align` is per column: 'l', 'r' or 'c'."""

    header: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)
    align: list[str] = field(default_factory=list)
    caption: str = ""
    #: Rows rendered in bold — totals, blended figures.
    emphasis_rows: set[int] = field(default_factory=set)

    def __init__(
        self,
        header: Sequence[str],
        rows: Sequence[Sequence[Any]],
        *,
        align: Sequence[str] | None = None,
        caption: str = "",
        emphasis_rows: Sequence[int] = (),
        **kwargs,
    ):
        super().__init__(BlockKind.TABLE, **kwargs)
        self.header = [str(h) for h in header]
        self.rows = [[str(cell) for cell in row] for row in rows]
        # Default: first column left, the rest right. Financial tables are read
        # down their numeric columns, and ragged decimal points defeat that.
        self.align = list(align) if align else (
            ["l"] + ["r"] * (len(self.header) - 1) if self.header else []
        )
        self.caption = caption
        self.emphasis_rows = set(emphasis_rows)

    @property
    def n_cols(self) -> int:
        return max([len(self.header)] + [len(r) for r in self.rows], default=0)


@dataclass(slots=True)
class MetricGrid(Block):
    """Headline figures in a tiled grid."""

    metrics: list[tuple[str, str, str]] = field(default_factory=list)
    columns: int = 4

    def __init__(
        self, metrics: Sequence[tuple[str, str, str]], columns: int = 4, **kwargs
    ):
        super().__init__(BlockKind.METRIC_GRID, **kwargs)
        #: (label, value, hint)
        self.metrics = [(str(a), str(b), str(c)) for a, b, c in metrics]
        self.columns = columns


@dataclass(slots=True)
class Chart(Block):
    """A chart specification. Rendered to PNG by the chart engine.

    The block holds the *data*, never an image. That keeps the tree
    serialisable, lets HTML render an inline SVG while PDF embeds a raster, and
    means a chart can be regenerated at a different size or theme without
    rebuilding the report.
    """

    chart_kind: ChartKind = ChartKind.REVENUE
    title: str = ""
    labels: list[str] = field(default_factory=list)
    series: list[tuple[str, list[float | None]]] = field(default_factory=list)
    #: Series drawn as a line on a secondary axis — margins over bars.
    secondary: set[str] = field(default_factory=set)
    y_unit: str = ""
    #: For heat-map style charts (sensitivity): row labels and a value matrix.
    matrix: list[list[float | None]] = field(default_factory=list)
    row_labels: list[str] = field(default_factory=list)

    def __init__(
        self,
        chart_kind: ChartKind,
        title: str,
        *,
        labels: Sequence[str] = (),
        series: Sequence[tuple[str, Sequence[float | None]]] = (),
        secondary: Sequence[str] = (),
        y_unit: str = "",
        matrix: Sequence[Sequence[float | None]] = (),
        row_labels: Sequence[str] = (),
        **kwargs,
    ):
        super().__init__(BlockKind.CHART, **kwargs)
        self.chart_kind = chart_kind
        self.title = title
        self.labels = [str(x) for x in labels]
        self.series = [(str(n), list(v)) for n, v in series]
        self.secondary = set(secondary)
        self.y_unit = y_unit
        self.matrix = [list(r) for r in matrix]
        self.row_labels = [str(x) for x in row_labels]

    @property
    def has_data(self) -> bool:
        """A chart with no non-null points must not be rendered.

        An empty axis reads as "the value was zero" rather than "we had
        nothing", which is exactly the fabrication the brief forbids.
        """
        if self.matrix:
            return any(v is not None for row in self.matrix for v in row)
        return any(v is not None for _, values in self.series for v in values)


@dataclass(slots=True)
class Callout(Block):
    """A boxed statement — a recommendation, a warning, a disclosure."""

    title: str = ""
    text: str = ""
    tone: CalloutTone = CalloutTone.NEUTRAL

    def __init__(
        self, title: str, text: str, tone: CalloutTone = CalloutTone.NEUTRAL, **kwargs
    ):
        super().__init__(BlockKind.CALLOUT, **kwargs)
        self.title = title
        self.text = text
        self.tone = tone


@dataclass(slots=True)
class Quote(Block):
    """A verbatim extract from a source document, with its citation."""

    text: str = ""
    attribution: str = ""

    def __init__(self, text: str, attribution: str = "", **kwargs):
        super().__init__(BlockKind.QUOTE, **kwargs)
        self.text = text
        self.attribution = attribution


@dataclass(slots=True)
class Divider(Block):
    def __init__(self, **kwargs):
        super().__init__(BlockKind.DIVIDER, **kwargs)


@dataclass(slots=True)
class PageBreak(Block):
    def __init__(self, **kwargs):
        super().__init__(BlockKind.PAGE_BREAK, **kwargs)


@dataclass(slots=True)
class Insufficient(Block):
    """The brief's required statement when evidence is absent.

    Carries *why*, because "Insufficient evidence." on its own tells a reader
    nothing about whether to go and find the data or accept the gap.
    """

    reason: str = ""
    STATEMENT = "Insufficient evidence."

    def __init__(self, reason: str = "", **kwargs):
        super().__init__(BlockKind.INSUFFICIENT, **kwargs)
        self.reason = reason

    @property
    def text(self) -> str:
        return (
            f"{self.STATEMENT} {self.reason}" if self.reason else self.STATEMENT
        )


@dataclass(slots=True)
class CitationList(Block):
    """The evidence appendix for a section or the whole report."""

    entries: list[Evidence] = field(default_factory=list)

    def __init__(self, entries: Sequence[Evidence], **kwargs):
        super().__init__(BlockKind.CITATION_LIST, **kwargs)
        self.entries = list(entries)


# ---------------------------------------------------------------------------
# Sections and documents
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class Section:
    """One report section."""

    key: SectionKey
    title: str
    blocks: list[Block] = field(default_factory=list)
    #: False when the section could not be built from available evidence.
    sufficient: bool = True
    reason: str = ""
    #: Sections a reader may skip; excluded from shorter report types.
    optional: bool = False

    def add(self, block: Block | None) -> "Section":
        if block is not None:
            self.blocks.append(block)
        return self

    def extend(self, blocks: Sequence[Block | None]) -> "Section":
        for block in blocks:
            self.add(block)
        return self

    def mark_insufficient(self, reason: str) -> "Section":
        """Replace the section's content with the required statement."""
        self.sufficient = False
        self.reason = reason
        self.blocks = [Insufficient(reason)]
        return self

    @property
    def order(self) -> int:
        return SECTION_ORDER.index(self.key)

    @property
    def is_empty(self) -> bool:
        return not self.blocks

    def evidence(self) -> list[Evidence]:
        """Every distinct piece of evidence used in this section."""
        seen: dict[str, Evidence] = {}
        for block in self.blocks:
            for item in block.evidence:
                seen.setdefault(item.key, item)
        return list(seen.values())

    def charts(self) -> list[Chart]:
        return [b for b in self.blocks if isinstance(b, Chart)]

    def tables(self) -> list[Table]:
        return [b for b in self.blocks if isinstance(b, Table)]

    def word_count(self) -> int:
        total = 0
        for block in self.blocks:
            if isinstance(block, Paragraph):
                total += len(block.text.split())
            elif isinstance(block, Bullets):
                total += sum(len(item.split()) for item in block.items)
            elif isinstance(block, Callout):
                total += len(block.text.split())
        return total


@dataclass(slots=True)
class CoverMeta:
    """Cover-page facts, from `01 Cover`."""

    company_name: str
    ticker: str
    report_type: ReportType
    title: str
    subtitle: str = ""
    as_of: date | None = None
    analyst: str = ""
    institution: str = "Institutional Equity Research"
    exchange: str = ""
    sector: str | None = None
    industry: str | None = None
    recommendation: str | None = None
    target_price: float | None = None
    current_price: float | None = None
    upside: float | None = None
    rating: str | None = None
    score: float | None = None
    market_cap: float | None = None
    currency: str = "INR"
    #: Set when the underlying data is not investment grade. Rendered on the
    #: cover, not buried in an appendix.
    data_warning: str | None = None


@dataclass(slots=True)
class ReportDocument:
    """A complete, renderer-agnostic report."""

    cover: CoverMeta
    sections: list[Section] = field(default_factory=list)
    theme: Theme = Theme.LIGHT
    generated_at: datetime | None = None
    version: int = 1
    #: Free-form provenance: engine versions, prompt versions, data grades.
    provenance: dict[str, str] = field(default_factory=dict)
    disclaimer: str = ""

    def add(self, section: Section | None) -> "ReportDocument":
        if section is not None and not section.is_empty:
            self.sections.append(section)
        return self

    def ordered(self) -> list[Section]:
        """Sections in canonical order, so two reports are comparable."""
        return sorted(self.sections, key=lambda s: (s.order, s.title))

    def section(self, key: SectionKey) -> Section | None:
        return next((s for s in self.sections if s.key is key), None)

    def iter_blocks(self) -> Iterator[tuple[Section, Block]]:
        for section in self.ordered():
            for block in section.blocks:
                yield section, block

    def evidence(self) -> list[Evidence]:
        """Every distinct piece of evidence in the report, in first-use order."""
        seen: dict[str, Evidence] = {}
        for _, block in self.iter_blocks():
            for item in block.evidence:
                seen.setdefault(item.key, item)
        return list(seen.values())

    def charts(self) -> list[Chart]:
        return [b for _, b in self.iter_blocks() if isinstance(b, Chart)]

    def tables(self) -> list[Table]:
        return [b for _, b in self.iter_blocks() if isinstance(b, Table)]

    @property
    def insufficient_sections(self) -> list[Section]:
        return [s for s in self.sections if not s.sufficient]

    @property
    def word_count(self) -> int:
        return sum(s.word_count() for s in self.sections)

    def statistics(self) -> dict[str, int]:
        return {
            "sections": len(self.sections),
            "sections_insufficient": len(self.insufficient_sections),
            "blocks": sum(len(s.blocks) for s in self.sections),
            "charts": len(self.charts()),
            "tables": len(self.tables()),
            "evidence": len(self.evidence()),
            "words": self.word_count,
        }


# ---------------------------------------------------------------------------
# Report type composition
# ---------------------------------------------------------------------------
#: Which sections each report type asks for. Composition is data, so adding a
#: report type is a row here rather than a new builder — the "no duplicated
#: templates" requirement, enforced structurally.
REPORT_SECTIONS: dict[ReportType, tuple[SectionKey, ...]] = {
    ReportType.QUICK: (
        SectionKey.COVER,
        SectionKey.EXECUTIVE_SUMMARY,
        SectionKey.FINANCIAL_ANALYSIS,
        SectionKey.VALUATION,
        SectionKey.INSTITUTIONAL_SCORE,
    ),
    ReportType.INSTITUTIONAL: (
        SectionKey.COVER, SectionKey.TOC,
        SectionKey.EXECUTIVE_SUMMARY, SectionKey.INVESTMENT_THESIS,
        SectionKey.BUSINESS_OVERVIEW, SectionKey.FINANCIAL_ANALYSIS,
        SectionKey.FORECAST, SectionKey.VALUATION, SectionKey.DCF,
        SectionKey.RELATIVE_VALUATION, SectionKey.INSTITUTIONAL_SCORE,
        SectionKey.MOAT, SectionKey.RISK_ANALYSIS,
        SectionKey.SCENARIO_ANALYSIS, SectionKey.PEER_COMPARISON,
        SectionKey.APPENDIX,
    ),
    ReportType.IC_MEMO: (
        SectionKey.COVER, SectionKey.EXECUTIVE_SUMMARY,
        SectionKey.INVESTMENT_THESIS, SectionKey.VALUATION,
        SectionKey.INSTITUTIONAL_SCORE, SectionKey.RISK_ANALYSIS,
        SectionKey.SCENARIO_ANALYSIS, SectionKey.PORTFOLIO_FIT,
        SectionKey.APPENDIX,
    ),
    ReportType.QUARTERLY_UPDATE: (
        SectionKey.COVER, SectionKey.EXECUTIVE_SUMMARY,
        SectionKey.FINANCIAL_ANALYSIS, SectionKey.FORECAST,
        SectionKey.VALUATION, SectionKey.RISK_ANALYSIS, SectionKey.APPENDIX,
    ),
    ReportType.INITIATION: (
        SectionKey.COVER, SectionKey.TOC, SectionKey.EXECUTIVE_SUMMARY,
        SectionKey.INVESTMENT_THESIS, SectionKey.BUSINESS_OVERVIEW,
        SectionKey.INDUSTRY_ANALYSIS, SectionKey.FINANCIAL_ANALYSIS,
        SectionKey.FORECAST, SectionKey.VALUATION, SectionKey.DCF,
        SectionKey.RELATIVE_VALUATION, SectionKey.INSTITUTIONAL_SCORE,
        SectionKey.MANAGEMENT, SectionKey.MOAT, SectionKey.RISK_ANALYSIS,
        SectionKey.SCENARIO_ANALYSIS, SectionKey.PEER_COMPARISON,
        SectionKey.APPENDIX,
    ),
    ReportType.DEEP_RESEARCH: tuple(SectionKey),
}

REPORT_TITLES: dict[ReportType, str] = {
    ReportType.QUICK: "Quick Report",
    ReportType.INSTITUTIONAL: "Institutional Research Report",
    ReportType.IC_MEMO: "Investment Committee Memorandum",
    ReportType.QUARTERLY_UPDATE: "Quarterly Update",
    ReportType.INITIATION: "Initiation of Coverage",
    ReportType.DEEP_RESEARCH: "Deep Research Report",
}

#: AI narratives each report type asks the analyst for. Kept small for the
#: quick report because each capability is a model round-trip.
REPORT_NARRATIVES: dict[ReportType, tuple[str, ...]] = {
    ReportType.QUICK: ("business_summary",),
    ReportType.INSTITUTIONAL: (
        "business_summary", "investment_thesis", "bull_case", "bear_case",
        "moat_analysis", "risk_analysis", "valuation_commentary",
        "scoring_explanation",
    ),
    ReportType.IC_MEMO: (
        "investment_thesis", "bull_case", "bear_case", "risk_analysis",
        "valuation_commentary",
    ),
    ReportType.QUARTERLY_UPDATE: (
        "business_summary", "valuation_commentary", "risk_analysis",
    ),
    ReportType.INITIATION: (
        "business_summary", "investment_thesis", "bull_case", "bear_case",
        "moat_analysis", "management_analysis", "risk_analysis",
        "valuation_commentary", "scoring_explanation", "peer_comparison",
    ),
    ReportType.DEEP_RESEARCH: (
        "business_summary", "investment_thesis", "bull_case", "bear_case",
        "swot", "moat_analysis", "management_analysis", "capital_allocation",
        "risk_analysis", "valuation_commentary", "dcf_interpretation",
        "scoring_explanation", "peer_comparison",
    ),
}


def sections_for(report_type: ReportType) -> tuple[SectionKey, ...]:
    return REPORT_SECTIONS[report_type]


def narratives_for(report_type: ReportType) -> tuple[str, ...]:
    return REPORT_NARRATIVES[report_type]


DEFAULT_DISCLAIMER = (
    "This report is produced by an analytical platform for research purposes. "
    "It is not investment advice and not an offer to buy or sell any security. "
    "Figures labelled as model outputs are projections, not facts, and depend "
    "on assumptions that are disclosed in the appendix. Past performance does "
    "not indicate future results."
)
