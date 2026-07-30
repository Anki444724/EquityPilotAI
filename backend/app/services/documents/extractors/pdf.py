"""PDF parser: text layer, layout, tables, and OCR only where warranted.

Two libraries, each for what it is best at:

* **PyMuPDF (fitz)** — fast text and layout extraction, image inventory, and
  rasterisation for OCR. It gives per-span font size and weight, which is what
  makes heading detection possible at all.
* **pdfplumber** — table recovery. Its ruling-line and word-alignment
  strategies recover the bordered tables Indian filings use for financial
  statements far better than a naive text-column split.

pdfplumber is materially slower, so it is applied selectively: to pages whose
text suggests a table, not to every page of a 300-page annual report.
"""
from __future__ import annotations

import logging
import re
from typing import Any, ClassVar

from app.domain.documents.types import (
    ExtractedTable, FileFormat, ParseFailure, ParsedDocument, ParsedPage,
    TextBlock, TextSource, normalise_whitespace,
)
from app.services.documents.extractors.base import DocumentParser, register
from app.services.documents.extractors.ocr import OcrEngine, OcrPolicy
from app.services.documents.extractors.tables import build_table

logger = logging.getLogger(__name__)

#: A page with several of these is worth handing to pdfplumber.
_TABLE_HINTS = re.compile(
    r"(₹|rs\.?|inr|crore|lakh|million|%|\bfy\s?\d{2}\b|\bq[1-4]\b|total|"
    r"particulars|as at|year ended)",
    re.IGNORECASE,
)
#: Runs of two or more spaces or a tab — the signature of a columnar layout.
_COLUMN_GAP = re.compile(r"(?:\s{2,}|\t)")
#: A line consisting of nothing but a figure — the signature of a ruled table
#: read cell-by-cell. Continuous prose effectively never produces one.
_BARE_NUMBER = re.compile(r"[-+(]?\s*(?:₹|rs\.?|inr)?\s*\d[\d,]*(?:\.\d+)?\s*[%x)]?", re.I)
#: A cell that is purely a figure (with accounting parentheses, %, x, dash).
_NUMERIC_CELL = re.compile(
    r"[-+(]?\s*(?:₹|rs\.?|inr)?\s*\d[\d,]*(?:\.\d+)?\s*[%x)]?|[-–—]|n\.?a\.?|nil",
    re.I,
)


class PdfParser(DocumentParser):
    """Parses PDFs into pages, blocks and tables."""

    formats: ClassVar[tuple[FileFormat, ...]] = (FileFormat.PDF,)

    def __init__(
        self,
        ocr: OcrEngine | None = None,
        policy: OcrPolicy | None = None,
        *,
        extract_tables: bool = True,
    ) -> None:
        self.policy = policy or OcrPolicy()
        self.ocr = ocr or OcrEngine(self.policy)
        self.extract_tables = extract_tables

    # ------------------------------------------------------------------
    def parse(self, payload: bytes, *, filename: str = "") -> ParsedDocument:
        try:
            import fitz
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise ParseFailure("PyMuPDF is not installed") from exc

        try:
            doc = fitz.open(stream=payload, filetype="pdf")
        except Exception as exc:
            raise ParseFailure(f"cannot open PDF: {exc}") from exc

        parsed = ParsedDocument(file_format=FileFormat.PDF)
        meta = doc.metadata or {}
        parsed.title = (meta.get("title") or "").strip() or None
        parsed.author = (meta.get("author") or "").strip() or None
        parsed.producer = (meta.get("producer") or "").strip() or None
        parsed.metadata = {
            k: str(v) for k, v in meta.items() if v and k not in {"title", "author"}
        }

        ocr_pages: list[int] = []
        try:
            for index, page in enumerate(doc, start=1):
                parsed_page = self._parse_page(page, index)
                if parsed_page.source is TextSource.OCR:
                    ocr_pages.append(index)
                parsed.pages.append(parsed_page)
        finally:
            doc.close()

        parsed.used_ocr = bool(ocr_pages)
        if ocr_pages:
            parsed.metadata["ocr_pages"] = ",".join(str(p) for p in ocr_pages)

        if self.extract_tables:
            self._attach_tables(payload, parsed)
        return parsed

    # ------------------------------------------------------------------
    def _parse_page(self, page: Any, number: int) -> ParsedPage:
        rect = page.rect
        width, height = float(rect.width), float(rect.height)
        blocks = self._layout_blocks(page, number)
        text = "\n".join(b.text for b in blocks if not b.is_empty)

        area = width * height
        image_ratio = self._image_ratio(page, area)
        source = TextSource.NATIVE
        ocr_confidence: float | None = None

        if self.policy.needs_ocr(
            char_count=len(text.strip()), page_area=area, image_ratio=image_ratio
        ):
            recovered = self._ocr_page(page)
            if recovered is not None:
                if text.strip():
                    # A page with both a thin text layer and a large image:
                    # keep both rather than discarding either.
                    text = f"{text}\n{recovered.text}".strip()
                    source = TextSource.MIXED
                else:
                    text = recovered.text
                    source = TextSource.OCR
                ocr_confidence = recovered.confidence
                if not blocks and text.strip():
                    blocks = [TextBlock(text=text, page=number, block_index=0)]

        return ParsedPage(
            number=number,
            text=text,
            blocks=blocks,
            source=source,
            ocr_confidence=ocr_confidence,
            width=width,
            height=height,
        )

    def _layout_blocks(self, page: Any, number: int) -> list[TextBlock]:
        """Recover blocks with font size and weight — the basis of layout detection."""
        try:
            raw = page.get_text("dict")
        except Exception:  # pragma: no cover - defensive
            return []

        blocks: list[TextBlock] = []
        for order, block in enumerate(raw.get("blocks", [])):
            if block.get("type") != 0:  # 0 = text, 1 = image
                continue
            lines: list[str] = []
            sizes: list[float] = []
            bold_chars = 0
            total_chars = 0
            for line in block.get("lines", []):
                parts: list[str] = []
                for span in line.get("spans", []):
                    content = span.get("text", "")
                    if not content:
                        continue
                    parts.append(content)
                    sizes.append(float(span.get("size", 0.0)))
                    total_chars += len(content)
                    if self._is_bold(span):
                        bold_chars += len(content)
                if parts:
                    lines.append("".join(parts))
            text = "\n".join(lines).strip()
            if not text:
                continue
            bbox = tuple(float(v) for v in block.get("bbox", (0, 0, 0, 0)))
            blocks.append(
                TextBlock(
                    text=text,
                    page=number,
                    bbox=bbox,  # type: ignore[arg-type]
                    font_size=round(max(sizes), 2) if sizes else None,
                    # Majority-bold, not any-bold: a single bold word inside a
                    # sentence must not promote a paragraph to a heading.
                    bold=total_chars > 0 and bold_chars / total_chars > 0.6,
                    block_index=order,
                )
            )
        return blocks

    @staticmethod
    def _is_bold(span: dict) -> bool:
        """Bold via the font flag bit, with a name fallback for odd producers."""
        if int(span.get("flags", 0)) & 2 ** 4:
            return True
        return "bold" in str(span.get("font", "")).lower()

    @staticmethod
    def _image_ratio(page: Any, page_area: float) -> float:
        """Fraction of the page covered by raster images, capped at 1.0."""
        if page_area <= 0:
            return 0.0
        covered = 0.0
        try:
            for info in page.get_image_info():
                bbox = info.get("bbox")
                if not bbox:
                    continue
                x0, y0, x1, y1 = (float(v) for v in bbox)
                covered += max(0.0, x1 - x0) * max(0.0, y1 - y0)
        except Exception:  # pragma: no cover - defensive
            return 0.0
        return min(1.0, covered / page_area)

    def _ocr_page(self, page: Any):
        """Rasterise and recognise. Returns ``None`` when OCR is unavailable."""
        if not self.ocr.available:
            return None
        try:
            import fitz

            matrix = fitz.Matrix(self.policy.render_scale, self.policy.render_scale)
            pixmap = page.get_pixmap(matrix=matrix)
            return self.ocr.recognise(pixmap.tobytes("png"))
        except Exception as exc:
            logger.warning("OCR failed on page %s: %s", page.number, exc)
            return None

    # ------------------------------------------------------------------
    def _attach_tables(self, payload: bytes, parsed: ParsedDocument) -> None:
        """Recover tables on pages that look like they hold one."""
        candidates = [p.number for p in parsed.pages if self._looks_tabular(p)]
        if not candidates:
            return
        try:
            import pdfplumber
        except ImportError:  # pragma: no cover - dependency guard
            logger.info("pdfplumber unavailable; skipping table extraction")
            return

        import io

        by_number = {p.number: p for p in parsed.pages}
        try:
            with pdfplumber.open(io.BytesIO(payload)) as plumbed:
                for number in candidates:
                    if number > len(plumbed.pages):
                        continue
                    page = plumbed.pages[number - 1]
                    for order, raw in enumerate(self._find_tables(page)):
                        table = self._build_table(raw, number, order)
                        if table is not None:
                            by_number[number].tables.append(table)
        except Exception as exc:
            logger.warning("table extraction failed: %s", exc)

    @staticmethod
    def _looks_tabular(page: ParsedPage) -> bool:
        """Pre-filter deciding which pages are worth handing to pdfplumber.

        This is a **latency** optimisation, not a correctness gate. Getting it
        wrong in the permissive direction costs milliseconds; getting it wrong
        in the strict direction loses a real financial statement. Correctness is
        enforced downstream by :meth:`_plausible_grid`, which judges the
        recovered grid rather than guessing from the text.

        Three signals, any of which suffices:

        * **Columnar lines** — whitespace runs splitting a line into 3+ fields.
        * **Bare numeric lines** — a line that is nothing but a figure. Prose
          never does this; a table read cell-by-cell does it constantly, which
          is exactly how PyMuPDF emits a ruled table.
        * **Financial vocabulary** alongside several numeric lines.
        """
        if page.source is TextSource.OCR:
            return False  # ruling lines are gone; word alignment is unreliable
        text = page.text
        if not text.strip():
            return False
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            return False

        columnar = sum(1 for line in lines if len(_COLUMN_GAP.split(line)) >= 3)
        if columnar >= 4:
            return True

        # Bare numeric lines, as an absolute count *and* a share of the page.
        # The share is what makes this discriminating: a page of continuous
        # prose that happens to contain a few figures is not a table, and an
        # earlier version without the ratio passed every page of a 45-page
        # report to pdfplumber. That cost 7.1 seconds against 107ms of actual
        # parsing — a 67x tax to find tables on pages that had none.
        bare_numeric = sum(1 for line in lines if _BARE_NUMBER.fullmatch(line))
        if bare_numeric >= 4 and bare_numeric / len(lines) >= 0.25:
            return True

        # Short, figure-dense lines — a borderless table read row by row.
        # Requires the lines to be *short*, which excludes prose sentences that
        # merely mention two numbers.
        dense = sum(
            1 for line in lines
            if len(line) < 60
            and len(re.findall(r"\d[\d,]*(?:\.\d+)?", line)) >= 2
        )
        return dense >= 4 and bool(_TABLE_HINTS.search(text))

    def _find_tables(self, page: Any) -> list[list[list[str | None]]]:
        """Ruled extraction first; text alignment only if it yields a real table.

        The text strategy is close to unconditionally productive — it will
        happily shred a page of paragraphs into a 63×6 grid — so its output is
        gated on :meth:`_plausible_grid` rather than trusted because it parsed.
        """
        try:
            ruled = page.extract_tables() or []
        except Exception:  # pragma: no cover - defensive
            ruled = []
        if ruled:
            return ruled
        try:
            loose = page.extract_tables(
                {
                    "vertical_strategy": "text",
                    "horizontal_strategy": "text",
                    "intersection_tolerance": 5,
                }
            ) or []
        except Exception:  # pragma: no cover - defensive
            return []
        return [grid for grid in loose if self._plausible_grid(grid)]

    @staticmethod
    def _plausible_grid(grid: list[list[str | None]]) -> bool:
        """Reject text-strategy output that is really prose cut into columns.

        A genuine financial table has numbers in its non-label columns and
        short cells. Prose sliced on whitespace has long cells, few numbers,
        and words split mid-token across column boundaries.
        """
        rows = [[(c or "").strip() for c in row] for row in grid]
        rows = [row for row in rows if any(row)]
        if len(rows) < 2:
            return False
        body = rows[1:]
        cells = [c for row in body for c in row[1:] if c]
        if not cells:
            return False
        numeric = sum(1 for c in cells if _NUMERIC_CELL.fullmatch(c))
        if numeric / len(cells) < 0.5:
            return False
        # Prose fragments are long; data cells are short.
        long_cells = sum(1 for c in cells if len(c) > 24)
        return long_cells / len(cells) < 0.25

    @staticmethod
    def _build_table(
        raw: list[list[str | None]], page: int, order: int
    ) -> ExtractedTable | None:
        # Delegated so merge recovery, header flattening and unit inference
        # have exactly one implementation across every format.
        return build_table(raw, page=page, index=order)


register(PdfParser)


def _caption_hint(text: str) -> str | None:
    """First line that reads like a table caption, if any."""
    for line in text.splitlines():
        stripped = normalise_whitespace(line)
        if 8 <= len(stripped) <= 120 and _TABLE_HINTS.search(stripped):
            return stripped
    return None
