"""Semantic chunking and deduplication.

Chunking is a retrieval decision disguised as a text-processing one. Too small
and a chunk loses the context that makes it answerable; too large and the
embedding averages several topics into a vector that matches nothing well.

The rules here:

* Never split a sentence. A half-sentence retrieved as evidence is worse than
  no evidence, because it reads as complete.
* Never cross a section boundary. A chunk that straddles the end of Risk
  Factors and the start of the Auditor's Report cannot cite either honestly.
* Overlap by a sentence or two, so a fact stated across a chunk seam is still
  retrievable from one side.

Deduplication matters more in filings than almost anywhere else: an annual
report repeats its safe-harbour paragraph, its segment boilerplate and its
governance recitals verbatim, sometimes dozens of times. Left in, they crowd
out real content in every search result.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.domain.documents.types import (
    Chunk, DetectedSection, ParsedDocument, SectionKind, estimate_tokens,
    normalise_whitespace, text_fingerprint,
)
from app.services.documents.pipeline.sections import section_for_order, section_for_page

#: Decimal-aware sentence splitter. "₹33,543.00 crore" is one sentence, and
#: Module 6's citation auditor was reporting 50% coverage until it learned that.
_SENTENCE_SPLIT = re.compile(
    r"(?<=[.!?])\s+(?=[A-Z\u2018\u201c\"'(\[])"
)
_PARAGRAPH_SPLIT = re.compile(r"\n\s*\n")
#: Boilerplate that should never be indexed as substantive content.
_BOILERPLATE = re.compile(
    r"^(?:page\s+\d+(?:\s+of\s+\d+)?|annual\s+report\s+\d{4}(?:[-–]\d{2,4})?|"
    r"\d+\s*\|\s*.{0,60}|contents|index)$",
    re.I,
)


@dataclass(frozen=True, slots=True)
class ChunkConfig:
    """Chunk sizing, in estimated tokens."""

    target_tokens: int = 220
    max_tokens: int = 380
    #: Chunks below this are merged forward rather than indexed alone.
    min_tokens: int = 40
    overlap_sentences: int = 1
    #: Duplicate suppression threshold: a fingerprint seen this often is boilerplate.
    duplicate_threshold: int = 2


DEFAULT_CHUNK_CONFIG = ChunkConfig()


def split_sentences(text: str) -> list[str]:
    """Sentence split that survives financial notation.

    Defined once and used by the chunker and the answer composer alike, so the
    two can never disagree about where a sentence ends.
    """
    cleaned = normalise_whitespace(text)
    if not cleaned:
        return []
    return [s.strip() for s in _SENTENCE_SPLIT.split(cleaned) if s.strip()]


class SemanticChunker:
    """Turns a parsed document into retrievable, citable chunks."""

    def __init__(self, config: ChunkConfig | None = None) -> None:
        self.config = config or DEFAULT_CHUNK_CONFIG

    def chunk(
        self,
        document: ParsedDocument,
        sections: list[DetectedSection] | None = None,
    ) -> list[Chunk]:
        sections = sections or []
        chunks: list[Chunk] = []

        # Walk blocks in document order so each chunk is attributed to the
        # section it actually sits in. Falling back to page text (a scan, a
        # CSV) costs accuracy where several sections share a page, and that is
        # stated rather than hidden.
        order = 0
        for page in document.pages:
            if not page.text.strip():
                continue
            page_section = section_for_page(sections, page.number)
            if page.blocks:
                for paragraph_index, block in enumerate(page.blocks):
                    if block.is_empty:
                        order += 1
                        continue
                    section = section_for_order(sections, order, page.number)
                    order += 1
                    kind = section.kind if section else SectionKind.UNKNOWN
                    title = section.title if section else None
                    for paragraph in self._paragraphs(block.text):
                        chunks.extend(
                            self._chunk_paragraph(
                                paragraph, page.number, paragraph_index, kind, title
                            )
                        )
            else:
                kind = page_section.kind if page_section else SectionKind.UNKNOWN
                title = page_section.title if page_section else None
                for paragraph_index, paragraph in enumerate(self._paragraphs(page.text)):
                    chunks.extend(
                        self._chunk_paragraph(
                            paragraph, page.number, paragraph_index, kind, title
                        )
                    )

            table_section = section_for_page(sections, page.number)
            chunks.extend(self._table_chunks(
                page,
                table_section.kind if table_section else SectionKind.UNKNOWN,
                table_section.title if table_section else None,
            ))

        chunks = self._merge_short(chunks)
        chunks = self.deduplicate(chunks)
        for index, chunk in enumerate(chunks):
            chunk.chunk_index = index
        return chunks

    # ------------------------------------------------------------------
    @staticmethod
    def _paragraphs(text: str) -> list[str]:
        parts: list[str] = []
        for block in _PARAGRAPH_SPLIT.split(text):
            for line_group in [block]:
                cleaned = normalise_whitespace(line_group)
                if cleaned and not _BOILERPLATE.match(cleaned):
                    parts.append(cleaned)
        return parts

    def _chunk_paragraph(
        self,
        paragraph: str,
        page: int,
        paragraph_index: int,
        section: SectionKind,
        title: str | None,
    ) -> list[Chunk]:
        sentences = split_sentences(paragraph)
        if not sentences:
            return []

        out: list[Chunk] = []
        buffer: list[str] = []
        tokens = 0
        for sentence in sentences:
            cost = estimate_tokens(sentence)
            # A single sentence longer than the cap is emitted whole rather than
            # cut: an over-long chunk retrieves imperfectly, a severed sentence
            # misleads.
            if buffer and tokens + cost > self.config.max_tokens:
                out.append(self._make(buffer, page, paragraph_index, section, title))
                buffer = buffer[-self.config.overlap_sentences :] if self.config.overlap_sentences else []
                tokens = sum(estimate_tokens(s) for s in buffer)
            buffer.append(sentence)
            tokens += cost
            if tokens >= self.config.target_tokens:
                out.append(self._make(buffer, page, paragraph_index, section, title))
                buffer = buffer[-self.config.overlap_sentences :] if self.config.overlap_sentences else []
                tokens = sum(estimate_tokens(s) for s in buffer)

        if buffer and (not out or estimate_tokens(" ".join(buffer)) >= self.config.min_tokens):
            out.append(self._make(buffer, page, paragraph_index, section, title))
        return out

    def _table_chunks(
        self, page, section: SectionKind, title: str | None
    ) -> list[Chunk]:
        """Render each table as its own chunk.

        Tables are indexed as linearised text rather than skipped: a question
        about a number in a table should retrieve that table, and the rendering
        keeps the row label beside its value so the match is meaningful.
        """
        out: list[Chunk] = []
        for table in page.tables:
            lines: list[str] = []
            if table.caption:
                lines.append(table.caption)
            if table.header:
                lines.append(" | ".join(table.header))
            for row in table.rows[:60]:
                lines.append(" | ".join(row))
            text = "\n".join(line for line in lines if line.strip())
            if len(text) < 20:
                continue
            out.append(
                Chunk(
                    text=text[:4000],
                    page=page.number,
                    paragraph=1000 + table.table_index,  # tables sort after prose
                    section=section,
                    section_title=title,
                )
            )
        return out

    @staticmethod
    def _make(
        sentences: list[str],
        page: int,
        paragraph: int,
        section: SectionKind,
        title: str | None,
    ) -> Chunk:
        return Chunk(
            text=" ".join(sentences).strip(),
            page=page,
            paragraph=paragraph,
            section=section,
            section_title=title,
        )

    def _merge_short(self, chunks: list[Chunk]) -> list[Chunk]:
        """Fold undersized chunks into the next chunk of the same page and section."""
        out: list[Chunk] = []
        for chunk in chunks:
            if (
                out
                and chunk.token_estimate < self.config.min_tokens
                and out[-1].page == chunk.page
                and out[-1].section is chunk.section
                and out[-1].token_estimate + chunk.token_estimate <= self.config.max_tokens
            ):
                previous = out[-1]
                previous.text = f"{previous.text} {chunk.text}".strip()
                previous.token_estimate = estimate_tokens(previous.text)
                previous.fingerprint = text_fingerprint(previous.text)
                continue
            out.append(chunk)
        return out

    def deduplicate(self, chunks: list[Chunk]) -> list[Chunk]:
        """Drop exact repeats beyond the threshold.

        The first occurrence of a boilerplate paragraph is kept — it is real
        content on the page it appears — and the repeats are dropped. Dropping
        all copies would lose a genuine disclosure that happens to be short.
        """
        counts: dict[str, int] = {}
        out: list[Chunk] = []
        for chunk in chunks:
            seen = counts.get(chunk.fingerprint, 0)
            if seen >= self.config.duplicate_threshold:
                continue
            counts[chunk.fingerprint] = seen + 1
            out.append(chunk)
        return out


def duplicate_ratio(chunks: list[Chunk]) -> float:
    """Share of chunks whose text is not unique — a document-quality signal."""
    if not chunks:
        return 0.0
    unique = len({c.fingerprint for c in chunks})
    return round(1.0 - unique / len(chunks), 4)
