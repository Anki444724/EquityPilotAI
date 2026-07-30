"""Table engine: normalisation, merged-cell recovery, unit inference.

The brief asks for three preservations — units, headers, merged cells — and
they are listed in ascending order of difficulty and descending order of how
often anyone bothers.

**Units** matter most. An Indian annual report will state "(₹ in lakhs)" once,
in six-point type above the table, and every number below it is then a hundredth
of what a naive reader assumes. Getting this wrong produces figures that are
wrong by exactly 100x and look entirely plausible.

**Merged cells** are the reason a header row often appears half-empty: a
"FY2025" spanning two sub-columns is emitted once, with a blank beside it.
Forward-filling recovers the intended header without inventing data, and the
span is recorded so the reconstruction can be audited.
"""
from __future__ import annotations

import re
from typing import Sequence

from app.domain.documents.types import ExtractedTable, Unit, normalise_whitespace

# ---------------------------------------------------------------------------
# Unit inference
# ---------------------------------------------------------------------------
#: Currency token. The word boundaries are load-bearing, not decoration: an
#: unanchored "rs" matches inside "Particulars", "Others" and "Reserves", so a
#: perfectly ordinary header label would be read as a currency declaration and
#: the whole table silently mis-scaled. Caught by the parser round-trip test.
_CCY = r"(?:₹|\brs\.?(?![a-z])|\binr\b)"

#: Ordered longest-context-first: "₹ in million" must beat a bare "₹".
_UNIT_PATTERNS: tuple[tuple[re.Pattern[str], Unit], ...] = (
    (re.compile(rf"\bin\s*{_CCY}?\s*\bcr(?:ore)?s?\b", re.I), Unit.INR_CRORE),
    (re.compile(rf"{_CCY}\s*(?:in\s*)?\bcr(?:ore)?s?\b", re.I), Unit.INR_CRORE),
    (re.compile(r"\bcrores?\b", re.I), Unit.INR_CRORE),
    (re.compile(rf"\bin\s*{_CCY}?\s*\blakhs?\b", re.I), Unit.INR_LAKH),
    (re.compile(rf"{_CCY}\s*(?:in\s*)?\blakhs?\b", re.I), Unit.INR_LAKH),
    (re.compile(r"\blakhs?\b", re.I), Unit.INR_LAKH),
    (re.compile(rf"\b(?:in\s*)?{_CCY}?\s*\b(?:mn|million)\b", re.I), Unit.INR_MILLION),
    (re.compile(rf"\b(?:in\s*)?{_CCY}?\s*\b(?:bn|billion)\b", re.I), Unit.INR_BILLION),
    (re.compile(r"\btco2e?\b", re.I), Unit.TONNES_CO2),
    (re.compile(r"%|\bper\s*cent\b|\bpercent(?:age)?\b", re.I), Unit.PERCENT),
    (re.compile(r"(?<![a-z0-9])x(?![a-z])|\btimes\b", re.I), Unit.TIMES),
    (re.compile(_CCY, re.I), Unit.INR),
)

#: Thousands separators in both Indian (1,23,456) and Western (123,456) styles.
_NUMBER = re.compile(
    r"^[\s(]*[-+]?(?:₹|rs\.?|inr)?\s*"
    r"(\d{1,3}(?:,\d{2,3})*(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"\s*(%|x|cr|crore|crores|lakh|lakhs|mn|million|bn|billion)?[\s)]*$",
    re.I,
)
_PAREN_NEGATIVE = re.compile(r"^\s*\(.*\)\s*$")
_SUFFIX_UNITS: dict[str, Unit] = {
    "%": Unit.PERCENT,
    "x": Unit.TIMES,
    "cr": Unit.INR_CRORE,
    "crore": Unit.INR_CRORE,
    "crores": Unit.INR_CRORE,
    "lakh": Unit.INR_LAKH,
    "lakhs": Unit.INR_LAKH,
    "mn": Unit.INR_MILLION,
    "million": Unit.INR_MILLION,
    "bn": Unit.INR_BILLION,
    "billion": Unit.INR_BILLION,
}


def detect_unit(text: str) -> Unit:
    """Infer a unit from free text such as a caption or header cell.

    Returns :attr:`Unit.UNKNOWN` when nothing matches. Guessing ₹ crore because
    it is the commonest Indian convention would be exactly the silent-default
    behaviour that makes a 100x error invisible.
    """
    if not text:
        return Unit.UNKNOWN
    for pattern, unit in _UNIT_PATTERNS:
        if pattern.search(text):
            return unit
    return Unit.UNKNOWN


def infer_table_unit(header: Sequence[str], rows: Sequence[Sequence[str]]) -> Unit:
    """Infer the unit governing a table's numeric cells.

    Precedence is deliberate: the header wins, because that is where the
    convention is declared. Only if the header is silent do we look at the
    first few body rows, where an inline "(₹ cr)" in a label sometimes appears.
    """
    for cell in header:
        unit = detect_unit(cell)
        if unit is not Unit.UNKNOWN:
            return unit
    for row in list(rows)[:4]:
        for cell in row:
            unit = detect_unit(cell)
            if unit is not Unit.UNKNOWN:
                return unit
    return Unit.UNKNOWN


# ---------------------------------------------------------------------------
# Numbers
# ---------------------------------------------------------------------------
def parse_number(text: str) -> tuple[float, Unit] | None:
    """Parse a financial cell into a value and any unit it carries inline.

    Accounting parentheses denote negatives; both Indian and Western digit
    grouping are accepted. Returns ``None`` for anything that is not a number,
    which is how a label column is told apart from a data column.
    """
    if not text:
        return None
    raw = text.strip()
    if not raw or raw in {"-", "–", "—", "NA", "N/A", "nil", "Nil", "NIL"}:
        return None
    negative = bool(_PAREN_NEGATIVE.match(raw)) or raw.lstrip().startswith("-")
    match = _NUMBER.match(raw)
    if not match:
        return None
    digits = match.group(1).replace(",", "")
    try:
        value = float(digits)
    except ValueError:  # pragma: no cover - regex already guarantees this
        return None
    if negative:
        value = -value
    suffix = (match.group(2) or "").lower()
    return value, _SUFFIX_UNITS.get(suffix, Unit.UNKNOWN)


def is_numeric_cell(text: str) -> bool:
    return parse_number(text) is not None


# ---------------------------------------------------------------------------
# Normalisation and merged cells
# ---------------------------------------------------------------------------
def unpack_multiline_rows(
    raw: Sequence[Sequence[str | None]],
) -> list[list[str | None]]:
    """Split cells containing newlines back into separate rows.

    pdfplumber's lattice mode returns the region between two ruling lines as a
    single cell. A financial statement drawn with only an outer border and a
    header rule therefore arrives as *one* body row whose every cell holds
    thirteen newline-separated values — the whole statement, collapsed.

    Where every populated cell in a row splits into the same number of parts,
    that row is unambiguously N stacked rows and is expanded. Where the counts
    disagree the row is left alone, because zipping mismatched columns would
    silently pair a label with the wrong number, which is far worse than
    leaving a lumpy cell for the extractor to skip.
    """
    out: list[list[str | None]] = []
    for row in raw:
        cells = [("" if c is None else str(c)) for c in row]
        counts = {cell.count("\n") + 1 for cell in cells if cell.strip()}
        if len(counts) != 1:
            out.append(list(row))
            continue
        depth = counts.pop()
        if depth < 2:
            out.append(list(row))
            continue
        split = [cell.split("\n") if cell.strip() else [""] * depth for cell in cells]
        for index in range(depth):
            out.append([parts[index] if index < len(parts) else "" for parts in split])
    return out


def normalise_table(raw: Sequence[Sequence[str | None]]) -> list[list[str]]:
    """Clean a raw grid: collapse whitespace, drop wholly empty rows and columns.

    ``None`` from pdfplumber means "no text found here", which is both a truly
    empty cell and a merged continuation. Distinguishing them is the job of
    :func:`recover_merges`; here they simply become empty strings.
    """
    grid = [
        [normalise_whitespace(str(cell)) if cell is not None else "" for cell in row]
        for row in unpack_multiline_rows(raw)
    ]
    grid = [row for row in grid if any(cell for cell in row)]
    if not grid:
        return []
    width = max(len(row) for row in grid)
    grid = [row + [""] * (width - len(row)) for row in grid]
    keep = [c for c in range(width) if any(row[c] for row in grid)]
    return [[row[c] for c in keep] for row in grid]


def recover_merges(
    grid: Sequence[Sequence[str]], *, header_rows: int = 1
) -> tuple[list[list[str]], dict[tuple[int, int], tuple[int, int]]]:
    """Forward-fill spanned header cells and record the spans.

    Only header rows are filled. Forward-filling the body would fabricate data:
    a blank cell in a financial table usually means nil or not-applicable, not
    "same as the cell to my left". Restricting the repair to the header is the
    conservative reading, and the recorded spans let a reviewer verify it.
    """
    filled = [list(row) for row in grid]
    merged: dict[tuple[int, int], tuple[int, int]] = {}
    for r in range(min(header_rows, len(filled))):
        row = filled[r]
        c = 0
        while c < len(row):
            if not row[c]:
                c += 1
                continue
            span = 1
            while c + span < len(row) and not row[c + span]:
                row[c + span] = row[c]
                span += 1
            if span > 1:
                merged[(r, c)] = (1, span)
            c += span
    return filled, merged


def flatten_header(grid: Sequence[Sequence[str]], header_rows: int) -> list[str]:
    """Collapse a multi-row header into one label per column.

    "FY2025 / Q1" beats either half alone, and it is what a reader would call
    the column. Duplicate fragments are dropped so a fully spanned parent does
    not repeat itself in every child.
    """
    if header_rows <= 0 or not grid:
        return []
    width = max(len(row) for row in grid[:header_rows])
    labels: list[str] = []
    for c in range(width):
        parts: list[str] = []
        for r in range(header_rows):
            cell = grid[r][c] if c < len(grid[r]) else ""
            if cell and cell not in parts:
                parts.append(cell)
        labels.append(" / ".join(parts))
    return labels


def detect_header_rows(grid: Sequence[Sequence[str]], *, limit: int = 3) -> int:
    """How many leading rows are header.

    A header row is one with no numeric cells beyond the first column — the
    first column being the label column, which may legitimately read "2025".
    """
    count = 0
    for row in list(grid)[:limit]:
        if any(is_numeric_cell(cell) for cell in row[1:]):
            break
        if not any(cell for cell in row):
            break
        count += 1
    return max(count, 1) if grid else 0


def table_confidence(table: ExtractedTable) -> float:
    """How much to trust a recovered table, in [0, 1].

    Four independent signals, equally weighted. This is a heuristic and is
    labelled as one: it ranks tables for review, it does not certify them.
    """
    grid = table.to_grid()
    if not grid:
        return 0.0

    cells = [cell for row in grid for cell in row]
    filled = sum(1 for cell in cells if cell)
    density = filled / len(cells) if cells else 0.0

    widths = {len(row) for row in table.rows}
    rectangular = 1.0 if len(widths) <= 1 else 0.5

    numeric_cols = 0
    width = table.n_cols
    for c in range(1, width):
        column = [row[c] for row in grid if c < len(row)]
        present = [cell for cell in column if cell]
        if present and sum(1 for cell in present if is_numeric_cell(cell)) / len(present) > 0.6:
            numeric_cols += 1
    numeric = min(1.0, numeric_cols / max(1, width - 1)) if width > 1 else 0.0

    has_header = 1.0 if table.header and any(table.header) else 0.4

    return round((density + rectangular + numeric + has_header) / 4.0, 4)


def build_table(
    raw: Sequence[Sequence[str | None]], *, page: int, index: int = 0,
    caption: str | None = None,
) -> ExtractedTable | None:
    """Full pipeline for one raw grid: normalise → merges → header → unit → score.

    This is the single construction path for an :class:`ExtractedTable` from
    raw cells. Every parser calls it, so unit inference and merge recovery
    cannot drift between formats.
    """
    grid = normalise_table(raw)
    if len(grid) < 2 or max((len(r) for r in grid), default=0) < 2:
        return None

    header_rows = detect_header_rows(grid)
    filled, merged = recover_merges(grid, header_rows=header_rows)
    header = flatten_header(filled, header_rows)
    body = filled[header_rows:]
    if not body:
        header, body = [], filled

    table = ExtractedTable(
        page=page,
        rows=body,
        header=header,
        caption=caption,
        unit=infer_table_unit(header, body) if header or body else Unit.UNKNOWN,
        merged=merged,
        table_index=index,
    )
    if table.unit is Unit.UNKNOWN and caption:
        table.unit = detect_unit(caption)
    table.confidence = table_confidence(table)
    return table
