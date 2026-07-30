"""Renderer contract and registry.

Every renderer walks the same block tree. Nothing above this layer knows which
format is being produced — the same discipline applied to LLM providers in
Module 6 and file parsers in Module 7.

The contract is deliberately narrow: a renderer receives a `ReportDocument` and
returns bytes. It may not fetch data, call an engine, or decide what a section
contains. A test asserts every registered renderer handles the complete block
vocabulary, which is what stops a new block type from silently rendering as
nothing in one format.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar

from app.domain.reports.blocks import BlockKind, ReportDocument, Theme
from app.services.reports.charts.engine import ChartEngine


class OutputFormat(StrEnum):
    """The five outputs the brief requires."""

    PDF = "pdf"
    DOCX = "docx"
    XLSX = "xlsx"
    HTML = "html"
    MARKDOWN = "md"

    @property
    def media_type(self) -> str:
        return {
            OutputFormat.PDF: "application/pdf",
            OutputFormat.DOCX: (
                "application/vnd.openxmlformats-officedocument"
                ".wordprocessingml.document"
            ),
            OutputFormat.XLSX: (
                "application/vnd.openxmlformats-officedocument"
                ".spreadsheetml.sheet"
            ),
            OutputFormat.HTML: "text/html; charset=utf-8",
            OutputFormat.MARKDOWN: "text/markdown; charset=utf-8",
        }[self]

    @property
    def extension(self) -> str:
        return self.value


@dataclass(slots=True)
class RenderResult:
    payload: bytes
    fmt: OutputFormat
    filename: str
    #: Populated where the format has a concept of pages.
    page_count: int | None = None
    took_ms: float = 0.0

    @property
    def size_bytes(self) -> int:
        return len(self.payload)


class ReportRenderer(ABC):
    """Turns a `ReportDocument` into bytes."""

    fmt: ClassVar[OutputFormat]
    #: Block kinds this renderer implements. Checked by test against the full
    #: vocabulary, so a new block cannot be added without every format
    #: acquiring a handler for it.
    handles: ClassVar[frozenset[BlockKind]] = frozenset(BlockKind)

    def __init__(self, charts: ChartEngine | None = None) -> None:
        self.charts = charts

    def chart_engine(self, theme: Theme) -> ChartEngine:
        """Reuse the supplied engine when its theme matches, else make one.

        Sharing matters: the same chart rendered for a PDF and an HTML preview
        should be one image, and the engine's cache only helps if the instance
        survives across renders.
        """
        if self.charts is not None and self.charts.theme is theme:
            return self.charts
        engine = ChartEngine(theme)
        self.charts = engine
        return engine

    @abstractmethod
    def render(self, document: ReportDocument) -> RenderResult: ...

    @staticmethod
    def filename(document: ReportDocument, fmt: OutputFormat) -> str:
        ticker = document.cover.ticker or "report"
        stamp = (document.cover.as_of or "").isoformat() if document.cover.as_of else ""
        parts = [ticker, document.cover.report_type.value]
        if stamp:
            parts.append(stamp)
        return f"{'_'.join(parts)}.{fmt.extension}"


_REGISTRY: dict[OutputFormat, type[ReportRenderer]] = {}


def register(cls: type[ReportRenderer]) -> type[ReportRenderer]:
    _REGISTRY[cls.fmt] = cls
    return cls


def renderer_for(
    fmt: OutputFormat, charts: ChartEngine | None = None
) -> ReportRenderer:
    cls = _REGISTRY.get(fmt)
    if cls is None:
        raise ValueError(f"no renderer registered for '{fmt.value}'")
    return cls(charts)


def registered_formats() -> tuple[OutputFormat, ...]:
    return tuple(sorted(_REGISTRY, key=lambda f: f.value))


# ---------------------------------------------------------------------------
# Shared text helpers — defined once so every format formats identically
# ---------------------------------------------------------------------------
def toc_entries(document: ReportDocument) -> list[tuple[int, str]]:
    """(level, title) pairs for the table of contents.

    Cover and the TOC itself are excluded: a contents page that lists itself is
    noise, and the cover is not navigated to.
    """
    from app.domain.reports.blocks import SectionKey

    out: list[tuple[int, str]] = []
    for section in document.ordered():
        if section.key in {SectionKey.COVER, SectionKey.TOC}:
            continue
        out.append((1, section.title))
    return out


def cover_pairs(document: ReportDocument) -> list[tuple[str, str]]:
    """Cover-page facts, formatted identically across every renderer."""
    cover = document.cover
    pairs: list[tuple[str, str]] = [
        ("Company", cover.company_name),
        ("Ticker", f"{cover.ticker}{f' · {cover.exchange}' if cover.exchange else ''}"),
    ]
    if cover.sector:
        pairs.append(("Sector", cover.sector))
    if cover.industry:
        pairs.append(("Industry", cover.industry))
    if cover.recommendation:
        pairs.append(("Recommendation", cover.recommendation))
    if cover.rating:
        pairs.append(("Institutional rating", cover.rating))
    if cover.score is not None:
        pairs.append(("Score", f"{cover.score:.1f} / 100"))
    if cover.current_price is not None:
        pairs.append(("Current price", f"₹{cover.current_price:,.2f}"))
    if cover.target_price is not None:
        pairs.append(("Target price", f"₹{cover.target_price:,.2f}"))
    if cover.upside is not None:
        pairs.append(("Upside", f"{cover.upside * 100:.1f}%"))
    if cover.market_cap is not None:
        pairs.append(("Market capitalisation", f"₹{cover.market_cap:,.0f} cr"))
    if cover.as_of:
        pairs.append(("Report date", cover.as_of.isoformat()))
    if cover.analyst:
        pairs.append(("Analyst", cover.analyst))
    pairs.append(("Prepared by", cover.institution))
    return pairs
