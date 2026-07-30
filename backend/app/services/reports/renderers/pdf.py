"""PDF renderer — print-ready institutional layout.

ReportLab's platypus flowables rather than an HTML-to-PDF converter. The reason
is control over the three things the brief asks for and HTML converters handle
badly: **page numbers** that know the total, a **table of contents** with real
page references, and **PDF bookmarks** in the reader's navigation pane. A
headless browser would also have been a heavyweight dependency for a job that
is fundamentally typesetting.

The two-pass build is the mechanism. Page numbers and TOC destinations are not
known until the document is laid out, so ReportLab lays it out twice: the first
pass records where each heading landed, the second renders the contents page
with those numbers. That is why `multiBuild` is used rather than `build`.
"""
from __future__ import annotations

import io
import time
from typing import Any

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    BaseDocTemplate, Frame, HRFlowable, Image, KeepTogether, ListFlowable,
    ListItem, NextPageTemplate, PageBreak as RLPageBreak, PageTemplate,
    Paragraph as RLParagraph, Spacer, Table as RLTable, TableStyle,
)
from reportlab.platypus.tableofcontents import TableOfContents

from app.domain.reports.blocks import (
    Block, BlockKind, Bullets, Callout, CalloutTone, Chart, CitationList,
    Divider, Heading, Insufficient, KeyValue, MetricGrid, PageBreak,
    Paragraph, Quote, ReportDocument, Section, SectionKey, Table, Theme,
)
from app.domain.reports.citations import annotate
from app.services.reports.renderers.base import (
    OutputFormat, RenderResult, ReportRenderer, cover_pairs, register,
)

PAGE = A4
MARGIN = 18 * mm
CONTENT_WIDTH = PAGE[0] - 2 * MARGIN

#: Base-14 Helvetica has no rupee glyph, so "₹268.00" printed as a black box on
#: every monetary figure in the report. A Unicode TrueType face is registered
#: instead, with Helvetica retained as a fallback for environments that lack
#: one — a report with the wrong typeface is recoverable, a report full of
#: tofu boxes is not.
_FONT_CANDIDATES = (
    (
        "DejaVuSans",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf",
    ),
    (
        "LiberationSans",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Italic.ttf",
    ),
)

BODY_FONT = "Helvetica"
BOLD_FONT = "Helvetica-Bold"
ITALIC_FONT = "Helvetica-Oblique"


def _register_fonts() -> None:
    """Register a Unicode font once per process, if one is installed."""
    global BODY_FONT, BOLD_FONT, ITALIC_FONT
    import os

    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.lib.fonts import addMapping

    if BODY_FONT != "Helvetica":
        return
    for name, regular, bold, italic in _FONT_CANDIDATES:
        if not (os.path.exists(regular) and os.path.exists(bold)):
            continue
        try:
            pdfmetrics.registerFont(TTFont(name, regular))
            pdfmetrics.registerFont(TTFont(f"{name}-Bold", bold))
            if os.path.exists(italic):
                pdfmetrics.registerFont(TTFont(f"{name}-Oblique", italic))
            # The mapping is what lets <b> and <i> inside a paragraph resolve
            # to the right face rather than silently staying regular.
            addMapping(name, 0, 0, name)
            addMapping(name, 1, 0, f"{name}-Bold")
            addMapping(name, 0, 1, f"{name}-Oblique" if os.path.exists(italic) else name)
            addMapping(name, 1, 1, f"{name}-Bold")
            BODY_FONT = name
            BOLD_FONT = f"{name}-Bold"
            ITALIC_FONT = f"{name}-Oblique" if os.path.exists(italic) else name
            return
        except Exception:  # pragma: no cover - font loading is environmental
            continue


_register_fonts()


class Palette:
    """Theme colours. Institutional navy, matched to the chart engine."""

    def __init__(self, theme: Theme) -> None:
        if theme is Theme.DARK:
            self.background = colors.HexColor("#0b1220")
            self.surface = colors.HexColor("#131c2e")
            self.text = colors.HexColor("#e2e8f0")
            self.muted = colors.HexColor("#94a3b8")
            self.rule = colors.HexColor("#1e293b")
            self.accent = colors.HexColor("#60a5fa")
            self.positive = colors.HexColor("#34d399")
            self.negative = colors.HexColor("#f87171")
            self.warning = colors.HexColor("#fbbf24")
            self.header_bg = colors.HexColor("#1b2740")
        else:
            self.background = colors.white
            self.surface = colors.HexColor("#f5f7fa")
            self.text = colors.HexColor("#0f172a")
            self.muted = colors.HexColor("#64748b")
            self.rule = colors.HexColor("#e2e8f0")
            self.accent = colors.HexColor("#1e3a8a")
            self.positive = colors.HexColor("#059669")
            self.negative = colors.HexColor("#dc2626")
            self.warning = colors.HexColor("#b45309")
            self.header_bg = colors.HexColor("#1e3a8a")

    def tone(self, tone: CalloutTone):
        return {
            CalloutTone.POSITIVE: self.positive,
            CalloutTone.NEGATIVE: self.negative,
            CalloutTone.WARNING: self.warning,
            CalloutTone.NEUTRAL: self.accent,
        }[tone]


class _DocTemplate(BaseDocTemplate):
    """Adds bookmark registration and the TOC hook."""

    def __init__(self, buffer, document: ReportDocument, palette: Palette, **kw):
        super().__init__(buffer, pagesize=PAGE, **kw)
        self.report = document
        self.palette = palette
        self._bookmark_seq = 0
        #: Deepest outline level emitted so far. PDF outlines are a strict
        #: tree: jumping from level 0 to level 2 has no parent to attach to and
        #: ReportLab raises. A section (H1) followed directly by a caption (H3)
        #: does exactly that, so each entry is clamped to at most one level
        #: deeper than the last.
        self._outline_depth = -1

        frame = Frame(
            MARGIN, MARGIN + 10 * mm, CONTENT_WIDTH,
            PAGE[1] - 2 * MARGIN - 10 * mm, id="body",
        )
        cover_frame = Frame(
            MARGIN, MARGIN, CONTENT_WIDTH, PAGE[1] - 2 * MARGIN, id="cover",
        )
        self.addPageTemplates([
            PageTemplate(id="cover", frames=[cover_frame],
                         onPage=self._paint_cover_page),
            PageTemplate(id="body", frames=[frame], onPage=self._paint_page),
        ])

    # -- chrome --------------------------------------------------------
    def _paint_background(self, canvas) -> None:
        if self.palette.background != colors.white:
            canvas.setFillColor(self.palette.background)
            canvas.rect(0, 0, PAGE[0], PAGE[1], stroke=0, fill=1)

    def _paint_cover_page(self, canvas, doc) -> None:
        canvas.saveState()
        self._paint_background(canvas)
        # A full-bleed band gives the cover an institutional feel without
        # needing artwork the platform does not have.
        canvas.setFillColor(self.palette.header_bg)
        canvas.rect(0, PAGE[1] - 62 * mm, PAGE[0], 62 * mm, stroke=0, fill=1)
        canvas.restoreState()

    def _paint_page(self, canvas, doc) -> None:
        canvas.saveState()
        self._paint_background(canvas)
        cover = self.report.cover

        canvas.setFont(BODY_FONT, 7.5)
        canvas.setFillColor(self.palette.muted)
        canvas.drawString(
            MARGIN, PAGE[1] - MARGIN + 3 * mm,
            f"{cover.company_name} ({cover.ticker}) · {cover.title}",
        )
        canvas.setStrokeColor(self.palette.rule)
        canvas.setLineWidth(0.4)
        canvas.line(
            MARGIN, PAGE[1] - MARGIN, PAGE[0] - MARGIN, PAGE[1] - MARGIN,
        )

        canvas.line(MARGIN, MARGIN + 6 * mm, PAGE[0] - MARGIN, MARGIN + 6 * mm)
        canvas.drawString(
            MARGIN, MARGIN,
            cover.as_of.isoformat() if cover.as_of else "",
        )
        # Page N of M. The total is only known after the first pass, so it is
        # read from the page count ReportLab accumulated.
        canvas.drawRightString(
            PAGE[0] - MARGIN, MARGIN, f"Page {canvas.getPageNumber()}",
        )
        canvas.drawCentredString(
            PAGE[0] / 2, MARGIN, "Not investment advice",
        )
        canvas.restoreState()

    def beforeDocument(self) -> None:
        """Reset per-pass state.

        `multiBuild` lays the document out repeatedly until the TOC page
        numbers stabilise. State that accumulates across passes makes each pass
        differ from the last, so the TOC never converges and ReportLab gives up
        after ten attempts. Both counters must start fresh every pass.
        """
        self._bookmark_seq = 0
        self._outline_depth = -1

    def afterFlowable(self, flowable) -> None:
        """Register headings with the TOC and the bookmark outline."""
        if not isinstance(flowable, RLParagraph):
            return
        style = flowable.style.name
        level = {"PDFH1": 0, "PDFH2": 1, "PDFH3": 2}.get(style)
        if level is None:
            return
        text = flowable.getPlainText()
        if not text.strip():
            return
        self._bookmark_seq += 1
        key = f"bm{self._bookmark_seq}"
        self.canv.bookmarkPage(key)

        outline_level = min(level, self._outline_depth + 1)
        self._outline_depth = outline_level
        self.canv.addOutlineEntry(text, key, level=outline_level, closed=False)
        self.notify("TOCEntry", (level, text, self.page, key))


@register
class PdfRenderer(ReportRenderer):
    """Renders a report to a print-ready PDF."""

    fmt = OutputFormat.PDF

    def render(self, document: ReportDocument) -> RenderResult:
        started = time.perf_counter()
        palette = Palette(document.theme)
        styles = self._styles(palette)
        engine = self.chart_engine(document.theme)

        buffer = io.BytesIO()
        template = _DocTemplate(
            buffer, document, palette,
            leftMargin=MARGIN, rightMargin=MARGIN,
            topMargin=MARGIN, bottomMargin=MARGIN + 10 * mm,
            title=f"{document.cover.company_name} — {document.cover.title}",
            author=document.cover.institution,
            subject=document.cover.report_type.value,
        )

        story: list[Any] = []
        story.extend(self._cover(document, styles, palette))
        story.append(NextPageTemplate("body"))
        story.append(RLPageBreak())

        evidence = document.evidence()
        for index, section in enumerate(document.ordered()):
            if section.key is SectionKey.COVER:
                continue
            if section.key is SectionKey.TOC:
                story.extend(self._toc(styles, palette))
                story.append(RLPageBreak())
                continue
            if index > 0 and story and not isinstance(story[-1], RLPageBreak):
                story.append(Spacer(1, 6 * mm))
            story.append(RLParagraph(section.title, styles["PDFH1"]))
            story.append(HRFlowable(
                width="100%", thickness=0.6, color=palette.rule,
                spaceBefore=2, spaceAfter=6,
            ))
            for block in section.blocks:
                story.extend(
                    self._block(block, styles, palette, engine, evidence)
                )

        template.multiBuild(story)
        payload = buffer.getvalue()
        return RenderResult(
            payload=payload, fmt=self.fmt,
            filename=self.filename(document, self.fmt),
            page_count=template.page,
            took_ms=round((time.perf_counter() - started) * 1000, 2),
        )

    # ------------------------------------------------------------------
    def _styles(self, palette: Palette) -> dict[str, ParagraphStyle]:
        base = getSampleStyleSheet()
        common = {"textColor": palette.text, "fontName": BODY_FONT}
        return {
            "PDFTitle": ParagraphStyle(
                "PDFTitle", parent=base["Title"], fontSize=24, leading=28,
                textColor=colors.white, fontName=BOLD_FONT,
                alignment=TA_LEFT, spaceAfter=4,
            ),
            "PDFSubtitle": ParagraphStyle(
                "PDFSubtitle", parent=base["Normal"], fontSize=12, leading=16,
                textColor=colors.HexColor("#cbd5e1"), alignment=TA_LEFT,
                fontName=BODY_FONT,
            ),
            "PDFH1": ParagraphStyle(
                "PDFH1", parent=base["Heading1"], fontSize=14, leading=18,
                textColor=palette.accent, fontName=BOLD_FONT,
                spaceBefore=8, spaceAfter=2,
            ),
            "PDFH2": ParagraphStyle(
                "PDFH2", parent=base["Heading2"], fontSize=11.5, leading=15,
                textColor=palette.text, fontName=BOLD_FONT,
                spaceBefore=8, spaceAfter=3,
            ),
            "PDFH3": ParagraphStyle(
                "PDFH3", parent=base["Heading3"], fontSize=10, leading=13,
                textColor=palette.muted, fontName=BOLD_FONT,
                spaceBefore=7, spaceAfter=3,
            ),
            "PDFBody": ParagraphStyle(
                "PDFBody", parent=base["Normal"], fontSize=9, leading=13.5,
                alignment=TA_JUSTIFY, spaceAfter=5, **common,
            ),
            "PDFSmall": ParagraphStyle(
                "PDFSmall", parent=base["Normal"], fontSize=7.5, leading=10,
                textColor=palette.muted, fontName=BODY_FONT,
            ),
            "PDFCell": ParagraphStyle(
                "PDFCell", parent=base["Normal"], fontSize=7.8, leading=10.5,
                **common,
            ),
            "PDFCellRight": ParagraphStyle(
                "PDFCellRight", parent=base["Normal"], fontSize=7.8,
                leading=10.5, alignment=2, **common,
            ),
            "PDFCellHead": ParagraphStyle(
                "PDFCellHead", parent=base["Normal"], fontSize=7.8,
                leading=10.5, fontName=BOLD_FONT,
                textColor=colors.white,
            ),
            "PDFQuote": ParagraphStyle(
                "PDFQuote", parent=base["Normal"], fontSize=8.5, leading=12.5,
                leftIndent=8, textColor=palette.muted,
                fontName=ITALIC_FONT,
            ),
        }

    # ------------------------------------------------------------- cover
    def _cover(self, document, styles, palette) -> list[Any]:
        cover = document.cover
        out: list[Any] = [
            Spacer(1, 12 * mm),
            RLParagraph(cover.institution.upper(), styles["PDFSubtitle"]),
            Spacer(1, 3 * mm),
            RLParagraph(cover.company_name, styles["PDFTitle"]),
            RLParagraph(
                f"{cover.ticker} · {cover.title}", styles["PDFSubtitle"],
            ),
            Spacer(1, 26 * mm),
        ]

        pairs = cover_pairs(document)
        rows = [
            [
                RLParagraph(f"<b>{k}</b>", styles["PDFCell"]),
                RLParagraph(v, styles["PDFCell"]),
            ]
            for k, v in pairs
        ]
        table = RLTable(rows, colWidths=[CONTENT_WIDTH * 0.38, CONTENT_WIDTH * 0.62])
        table.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("LINEBELOW", (0, 0), (-1, -2), 0.3, palette.rule),
        ]))
        out.append(table)

        if cover.data_warning:
            out.append(Spacer(1, 8 * mm))
            out.append(self._callout_table(
                Callout("Data quality", cover.data_warning, CalloutTone.WARNING),
                styles, palette,
            ))

        out.append(Spacer(1, 10 * mm))
        out.append(RLParagraph(document.disclaimer, styles["PDFSmall"]))
        return out

    def _toc(self, styles, palette) -> list[Any]:
        toc = TableOfContents()
        toc.levelStyles = [
            ParagraphStyle(
                "TOC1", fontSize=9.5, leading=16, fontName=BODY_FONT,
                textColor=palette.text,
            ),
            ParagraphStyle(
                "TOC2", fontSize=8.5, leading=13, leftIndent=12,
                fontName=BODY_FONT, textColor=palette.muted,
            ),
            ParagraphStyle(
                "TOC3", fontSize=8, leading=12, leftIndent=24,
                fontName=BODY_FONT, textColor=palette.muted,
            ),
        ]
        return [
            RLParagraph("Contents", styles["PDFH1"]),
            HRFlowable(width="100%", thickness=0.6, color=palette.rule,
                       spaceBefore=2, spaceAfter=8),
            toc,
        ]

    # ------------------------------------------------------------ blocks
    def _block(
        self, block: Block, styles, palette, engine, evidence
    ) -> list[Any]:
        kind = block.kind
        if kind is BlockKind.HEADING:
            style = styles["PDFH2"] if block.level <= 2 else styles["PDFH3"]
            return [RLParagraph(_esc(block.text), style)]

        if kind is BlockKind.PARAGRAPH:
            return [RLParagraph(
                self._inline(block.text, evidence, palette), styles["PDFBody"],
            )]

        if kind is BlockKind.BULLETS:
            items = [
                ListItem(
                    RLParagraph(
                        self._inline(text, evidence, palette), styles["PDFBody"],
                    ),
                    leftIndent=10,
                )
                for text in block.items
            ]
            if not items:
                return []
            return [ListFlowable(
                items, bulletType="1" if block.ordered else "bullet",
                bulletColor=palette.accent, bulletFontSize=7,
                leftIndent=12, spaceAfter=4,
            )]

        if kind is BlockKind.KEY_VALUE:
            return [self._kv_table(block, styles, palette)]

        if kind is BlockKind.TABLE:
            return self._table(block, styles, palette)

        if kind is BlockKind.METRIC_GRID:
            return [self._metric_grid(block, styles, palette)]

        if kind is BlockKind.CHART:
            return self._chart(block, styles, engine)

        if kind is BlockKind.CALLOUT:
            return [self._callout_table(block, styles, palette)]

        if kind is BlockKind.QUOTE:
            out = [RLParagraph(_esc(block.text), styles["PDFQuote"])]
            if block.attribution:
                out.append(RLParagraph(
                    f"— {_esc(block.attribution)}", styles["PDFSmall"],
                ))
            return out

        if kind is BlockKind.DIVIDER:
            return [HRFlowable(
                width="100%", thickness=0.5, color=palette.rule,
                spaceBefore=6, spaceAfter=6,
            )]

        if kind is BlockKind.PAGE_BREAK:
            return [RLPageBreak()]

        if kind is BlockKind.INSUFFICIENT:
            return [self._callout_table(
                Callout("Insufficient evidence", block.reason or "",
                        CalloutTone.NEUTRAL),
                styles, palette,
            )]

        if kind is BlockKind.CITATION_LIST:
            rows = [[e.key, e.render()] for e in block.entries]
            if not rows:
                return []
            return self._table(
                Table(["Reference", "Evidence"], rows), styles, palette,
            )

        return []  # pragma: no cover - vocabulary is exhaustive

    def _inline(self, text: str, evidence, palette) -> str:
        """Escape, then render citation markers as small coloured superscripts."""
        import re

        escaped = _esc(text)
        lookup = {e.key: e for e in evidence}

        def swap(match: re.Match) -> str:
            key = match.group(1)
            if key not in lookup:
                return match.group(0)
            return (
                f'<font size="6" color="{_hex(palette.accent)}">'
                f' [{_esc(lookup[key].label)}]</font>'
            )

        return re.sub(r"\[([a-z0-9_]+)\]", swap, escaped)

    def _kv_table(self, block: KeyValue, styles, palette):
        rows = [
            [
                RLParagraph(f"<b>{_esc(k)}</b>", styles["PDFCell"]),
                RLParagraph(_esc(v), styles["PDFCell"]),
            ]
            for k, v in block.pairs
        ]
        table = RLTable(rows, colWidths=[CONTENT_WIDTH * 0.34, CONTENT_WIDTH * 0.66])
        table.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 2.5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
            ("LINEBELOW", (0, 0), (-1, -2), 0.25, palette.rule),
        ]))
        return table

    def _table(self, block: Table, styles, palette) -> list[Any]:
        if not block.rows and not block.header:
            return []
        head = [
            RLParagraph(_esc(h), styles["PDFCellHead"]) for h in block.header
        ]
        body = []
        for row in block.rows:
            cells = []
            for index, cell in enumerate(row):
                align = block.align[index] if index < len(block.align) else "l"
                style = styles["PDFCellRight"] if align == "r" else styles["PDFCell"]
                cells.append(RLParagraph(_esc(cell), style))
            body.append(cells)

        data = ([head] if head else []) + body
        columns = block.n_cols or 1
        # First column wider: it holds labels, the rest hold figures.
        first = CONTENT_WIDTH * (0.30 if columns > 2 else 0.5)
        rest = (CONTENT_WIDTH - first) / max(1, columns - 1)
        widths = [first] + [rest] * (columns - 1)

        table = RLTable(data, colWidths=widths, repeatRows=1 if head else 0)
        style = [
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("GRID", (0, 0), (-1, -1), 0.25, palette.rule),
        ]
        if head:
            style.append(("BACKGROUND", (0, 0), (-1, 0), palette.header_bg))
        offset = 1 if head else 0
        for row_index in range(len(body)):
            if row_index % 2 == 1:
                style.append((
                    "BACKGROUND", (0, row_index + offset),
                    (-1, row_index + offset), palette.surface,
                ))
        for emphasised in block.emphasis_rows:
            if 0 <= emphasised < len(body):
                style.append((
                    "FONTNAME", (0, emphasised + offset),
                    (-1, emphasised + offset), BOLD_FONT,
                ))
        table.setStyle(TableStyle(style))

        out: list[Any] = []
        if block.caption:
            out.append(RLParagraph(_esc(block.caption), styles["PDFH3"]))
        out.append(table)
        if block.note:
            out.append(RLParagraph(_esc(block.note), styles["PDFSmall"]))
        out.append(Spacer(1, 4 * mm))
        return out

    def _metric_grid(self, block: MetricGrid, styles, palette):
        # Choose a column count that divides evenly where possible. Five
        # metrics in four columns leaves a row three-quarters empty, which
        # reads as missing data rather than as layout.
        count = len(block.metrics)
        columns = max(1, min(block.columns, 4))
        if count and count % columns:
            for candidate in (5, 4, 3, 2):
                if candidate <= max(block.columns, 5) and count % candidate == 0:
                    columns = candidate
                    break
        cells: list[Any] = []
        for label, value, hint in block.metrics:
            cells.append(RLParagraph(
                f'<font size="6.5" color="{_hex(palette.muted)}">'
                f"{_esc(label.upper())}</font><br/>"
                f'<font size="12"><b>{_esc(value)}</b></font><br/>'
                f'<font size="6.5" color="{_hex(palette.muted)}">'
                f"{_esc(hint)}</font>",
                styles["PDFCell"],
            ))
        while len(cells) % columns:
            cells.append(RLParagraph("", styles["PDFCell"]))

        rows = [cells[i:i + columns] for i in range(0, len(cells), columns)]
        width = CONTENT_WIDTH / columns
        table = RLTable(rows, colWidths=[width] * columns)
        table.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("BOX", (0, 0), (-1, -1), 0.4, palette.rule),
            ("INNERGRID", (0, 0), (-1, -1), 0.25, palette.rule),
            ("BACKGROUND", (0, 0), (-1, -1), palette.surface),
        ]))
        return KeepTogether([table, Spacer(1, 4 * mm)])

    def _chart(self, block: Chart, styles, engine) -> list[Any]:
        png = engine.render(block)
        if png is None:
            return []
        # Preserve aspect ratio from the engine's own figure geometry rather
        # than assuming: a radar is square and a bar chart is not.
        from reportlab.lib.utils import ImageReader

        reader = ImageReader(io.BytesIO(png))
        native_w, native_h = reader.getSize()
        width = CONTENT_WIDTH
        height = width * native_h / native_w
        image = Image(io.BytesIO(png), width=width, height=height)
        out: list[Any] = [image]
        if block.note:
            out.append(RLParagraph(_esc(block.note), styles["PDFSmall"]))
        out.append(Spacer(1, 4 * mm))
        return out

    def _callout_table(self, block: Callout, styles, palette):
        accent = palette.tone(block.tone)
        inner = RLParagraph(
            f'<font color="{_hex(accent)}"><b>{_esc(block.title)}</b></font>'
            f"<br/>{_esc(block.text)}",
            styles["PDFBody"],
        )
        table = RLTable([[inner]], colWidths=[CONTENT_WIDTH])
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), palette.surface),
            ("LINEBEFORE", (0, 0), (0, -1), 2.2, accent),
            ("TOPPADDING", (0, 0), (-1, -1), 7),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ("LEFTPADDING", (0, 0), (-1, -1), 9),
            ("RIGHTPADDING", (0, 0), (-1, -1), 9),
        ]))
        return KeepTogether([table, Spacer(1, 4 * mm)])


def _hex(colour) -> str:
    """ReportLab colour to a mini-HTML hex string.

    `hexval()` returns "0xrrggbb". Slicing off the "0x" and passing the digits
    alone raises `Invalid color value` — the tag parser needs a leading hash.
    Defined once so the conversion cannot be got wrong in three places.
    """
    return "#" + colour.hexval()[2:]


def _esc(text: str) -> str:
    """Escape for ReportLab's mini-HTML. Ampersand first, or it double-escapes."""
    return (
        str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )
