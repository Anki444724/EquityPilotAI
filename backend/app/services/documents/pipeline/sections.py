"""Section detection.

Two stages, because either alone is unreliable:

1. **Heading candidacy** — which blocks *look* like headings, judged on typography
   (font size against the document's body baseline, weight, case) and shape
   (short, no terminal full stop). This is format-agnostic: the DOCX parser
   supplies real style information, the PDF parser infers from font metrics,
   and both arrive here as the same :class:`TextBlock` fields.

2. **Classification** — which of the brief's twelve sections a heading names,
   by scored keyword match rather than first-hit-wins, so "Management Discussion
   and Analysis" is not captured by a stray "management" rule.

Sections then span from their heading to the next heading, which is what makes
a page-to-section lookup possible for every chunk in the document.
"""
from __future__ import annotations

import re
import statistics
from dataclasses import dataclass

from app.domain.documents.types import (
    DetectedSection, ParsedDocument, SectionKind, TextBlock, normalise_whitespace,
)


@dataclass(frozen=True, slots=True)
class SectionRule:
    """Keyword evidence for one section kind."""

    kind: SectionKind
    #: Phrases that on their own strongly identify the section.
    strong: tuple[str, ...]
    #: Phrases that contribute but do not decide.
    weak: tuple[str, ...] = ()
    #: Phrases that veto the match outright.
    exclude: tuple[str, ...] = ()

    def score(self, heading: str) -> float:
        text = f" {heading.lower()} "
        if any(term in text for term in self.exclude):
            return 0.0
        best = 0.0
        for term in self.strong:
            if term in text:
                # Longer phrases are more specific, so they score higher; a
                # 30-character match is near-certain, a 10-character one is not.
                best = max(best, 0.72 + min(0.23, len(term) / 130.0))
        for term in self.weak:
            if term in text:
                best = max(best, 0.5)
        return round(min(best, 0.98), 4)


#: Ordered by specificity where phrases overlap. Every section the brief names.
SECTION_RULES: tuple[SectionRule, ...] = (
    SectionRule(
        SectionKind.MANAGEMENT_DISCUSSION,
        ("management discussion and analysis", "management discussion & analysis",
         "management's discussion and analysis", "md&a", "mda report"),
        ("management discussion",),
    ),
    SectionRule(
        SectionKind.CHAIRMAN_LETTER,
        ("chairman's letter", "chairmans letter", "letter to shareholders",
         "letter from the chairman", "message from the chairman",
         "chairman's message", "chairman's statement", "managing director's message",
         "ceo's message", "letter to the shareholders"),
        ("chairman", "from the desk of",),
    ),
    SectionRule(
        SectionKind.RISK_FACTORS,
        ("risk factors", "risk management", "principal risks",
         "risks and concerns", "risks & concerns", "key risks",
         "risk management framework"),
        ("threats", "internal control"),
    ),
    SectionRule(
        SectionKind.NOTES_TO_ACCOUNTS,
        ("notes to accounts", "notes to the accounts",
         "notes to the financial statements", "notes forming part of",
         "significant accounting policies", "notes to financial statements"),
        ("accounting policies",),
    ),
    SectionRule(
        SectionKind.AUDITOR_REPORT,
        ("independent auditor's report", "independent auditors report",
         "auditor's report", "auditors' report", "report of the auditors",
         "basis for opinion", "key audit matters"),
        ("audit report",),
    ),
    SectionRule(
        SectionKind.FINANCIAL_STATEMENTS,
        ("balance sheet", "statement of profit and loss",
         "statement of cash flows", "cash flow statement", "financial statements",
         "consolidated financial statements", "standalone financial statements",
         "statement of changes in equity", "profit and loss account"),
        ("financial highlights", "financial performance"),
        exclude=("notes to",),
    ),
    SectionRule(
        SectionKind.CORPORATE_GOVERNANCE,
        ("corporate governance report", "corporate governance",
         "report on corporate governance", "board of directors",
         "board composition", "governance framework"),
        ("directors' responsibility", "committees of the board"),
    ),
    SectionRule(
        SectionKind.SHAREHOLDING,
        ("shareholding pattern", "distribution of shareholding",
         "shareholder information", "general shareholder information",
         "pattern of shareholding"),
        ("shareholding",),
    ),
    SectionRule(
        SectionKind.ESG,
        ("business responsibility and sustainability report",
         "business responsibility report", "sustainability report", "brsr",
         "environmental social and governance", "esg report",
         "sustainability highlights"),
        ("sustainability", "environment", "csr", "corporate social responsibility"),
    ),
    SectionRule(
        SectionKind.CONFERENCE_QA,
        ("question and answer session", "questions and answers", "q&a session",
         "analyst q&a", "question-and-answer session", "moderator:"),
        ("q & a", "questions from"),
    ),
    SectionRule(
        SectionKind.MANAGEMENT_GUIDANCE,
        ("outlook and guidance", "guidance for", "management guidance",
         "future outlook", "business outlook", "outlook for the year"),
        ("outlook", "guidance"),
    ),
    SectionRule(
        SectionKind.DIRECTORS_REPORT,
        ("directors' report", "director's report", "board's report",
         "report of the board of directors"),
    ),
    SectionRule(
        SectionKind.BUSINESS_OVERVIEW,
        ("business overview", "about the company", "company overview",
         "our business", "corporate overview", "company profile",
         "at a glance", "business segments", "our operations"),
        ("overview", "introduction", "who we are"),
    ),
)

_ENDS_SENTENCE = re.compile(r"[.:;,]$")
_MOSTLY_DIGITS = re.compile(r"^[\d\s.,()%₹/-]+$")
#: A numbered heading such as "3.1 Risk Factors" or "II. Auditor's Report".
_NUMBERED = re.compile(r"^\s*(\d+(\.\d+)*|[IVXLC]+)[.)]\s+\S", re.IGNORECASE)


def classify_heading(heading: str) -> tuple[SectionKind, float]:
    """Best-scoring section for a heading, or UNKNOWN.

    Scored rather than first-match so overlapping vocabularies resolve to the
    most specific rule: "Notes to the Financial Statements" must reach
    NOTES_TO_ACCOUNTS, not FINANCIAL_STATEMENTS.
    """
    cleaned = normalise_whitespace(heading)
    if not cleaned:
        return SectionKind.UNKNOWN, 0.0
    best_kind, best_score = SectionKind.UNKNOWN, 0.0
    for rule in SECTION_RULES:
        score = rule.score(cleaned)
        if score > best_score:
            best_kind, best_score = rule.kind, score
    return best_kind, best_score


class SectionDetector:
    """Finds headings, classifies them, and turns them into page spans."""

    #: A block must exceed the body baseline by this factor to read as a heading.
    HEADING_SIZE_RATIO = 1.15
    #: Headings are short. Beyond this many characters it is a paragraph.
    MAX_HEADING_CHARS = 120
    MIN_HEADING_CHARS = 3

    def detect(self, document: ParsedDocument) -> list[DetectedSection]:
        # Document order, not page order. Every block gets a global ordinal so
        # a section can end partway down a page — which is the normal case.
        blocks = [b for page in document.pages for b in page.blocks if not b.is_empty]
        if not blocks:
            return self._fallback(document)
        order_of = {id(block): index for index, block in enumerate(blocks)}

        baseline = self._body_size(blocks)
        headings = [
            (block, *classify_heading(block.text))
            for block in blocks
            if self._is_heading(block, baseline)
        ]
        # Keep only headings that actually name a known section; an annual
        # report has hundreds of headings and we claim none of the rest.
        named = [(b, k, s) for b, k, s in headings if k is not SectionKind.UNKNOWN and s > 0]
        if not named:
            return self._fallback(document)

        sections: list[DetectedSection] = []
        last_page = document.page_count or 1
        last_order = len(blocks) - 1
        for index, (block, kind, score) in enumerate(named):
            start_order = order_of[id(block)]
            if index + 1 < len(named):
                next_order = order_of[id(named[index + 1][0])]
                end_order = max(start_order, next_order - 1)
                end_page = max(block.page, blocks[end_order].page)
            else:
                end_order = last_order
                end_page = last_page
            sections.append(
                DetectedSection(
                    kind=kind,
                    title=normalise_whitespace(block.text)[: self.MAX_HEADING_CHARS],
                    start_page=block.page,
                    end_page=end_page,
                    confidence=score,
                    heading_level=1 if block.font_size and block.font_size >= baseline * 1.5 else 2,
                    start_order=start_order,
                    end_order=end_order,
                )
            )
        return self._merge_adjacent(sections)

    # ------------------------------------------------------------------
    def _is_heading(self, block: TextBlock, baseline: float) -> bool:
        text = normalise_whitespace(block.text)
        length = len(text)
        if not (self.MIN_HEADING_CHARS <= length <= self.MAX_HEADING_CHARS):
            return False
        if _MOSTLY_DIGITS.match(text):
            return False
        if "\n" in block.text.strip():
            return False
        if _ENDS_SENTENCE.search(text) and not text.endswith(":"):
            return False

        larger = bool(block.font_size and block.font_size >= baseline * self.HEADING_SIZE_RATIO)
        # Typography is the primary signal; case and numbering carry the
        # documents whose producers emit a single font size throughout.
        upper = text.isupper() and length > 5
        titled = text.istitle() and length <= 70
        return larger or block.bold or upper or bool(_NUMBERED.match(text)) and titled

    @staticmethod
    def _body_size(blocks: list[TextBlock]) -> float:
        """Median font size of substantial blocks — the body-text baseline.

        Median, not mean: a cover page in 48pt would drag a mean upward far
        enough that genuine headings stopped clearing the threshold.
        """
        sizes = [
            b.font_size for b in blocks
            if b.font_size and len(b.text) > 80
        ]
        if not sizes:
            sizes = [b.font_size for b in blocks if b.font_size]
        return statistics.median(sizes) if sizes else 11.0

    @staticmethod
    def _merge_adjacent(sections: list[DetectedSection]) -> list[DetectedSection]:
        """Collapse consecutive runs of the same kind into one span.

        A 60-page MD&A with a heading on every page is one section, not sixty.
        """
        merged: list[DetectedSection] = []
        for section in sections:
            if merged and merged[-1].kind is section.kind and \
                    section.start_page <= merged[-1].end_page + 1:
                previous = merged[-1]
                previous.end_page = max(previous.end_page, section.end_page)
                previous.confidence = max(previous.confidence, section.confidence)
                if section.end_order is not None:
                    previous.end_order = max(previous.end_order or 0, section.end_order)
                continue
            merged.append(section)
        return merged

    def _fallback(self, document: ParsedDocument) -> list[DetectedSection]:
        """No headings survived — classify on page text instead.

        Scanned documents lose typography entirely, so this path keeps section
        detection working at reduced, and clearly reduced, confidence.
        """
        sections: list[DetectedSection] = []
        for page in document.pages:
            head = normalise_whitespace(page.text[:400])
            if not head:
                continue
            kind, score = classify_heading(head)
            if kind is SectionKind.UNKNOWN or score <= 0:
                continue
            sections.append(
                DetectedSection(
                    kind=kind,
                    title=head[:80],
                    start_page=page.number,
                    end_page=page.number,
                    # Halved: this is inference from body text, not a heading.
                    confidence=round(score * 0.5, 4),
                    heading_level=3,
                )
            )
        return self._merge_adjacent(sections)


def section_for_page(sections: list[DetectedSection], page: int) -> DetectedSection | None:
    """Innermost section covering a page.

    Narrowest span wins, so a chunk inside "Notes to Accounts" nested within
    "Financial Statements" cites the more precise of the two.

    Prefer :func:`section_for_order` wherever a block ordinal is available:
    page granularity cannot separate three sections that begin on one page.
    """
    covering = [s for s in sections if s.contains(page)]
    if not covering:
        return None
    return min(covering, key=lambda s: (s.page_span, -s.confidence))


def section_for_order(
    sections: list[DetectedSection], order: int, page: int | None = None
) -> DetectedSection | None:
    """Section covering a block ordinal, falling back to the page lookup.

    This is the accurate path. It resolves the case that page granularity gets
    wrong: governance text and ESG text sharing a page were previously both
    attributed to whichever section the page lookup happened to pick.
    """
    covering = [s for s in sections if s.contains_order(order)]
    if covering:
        return min(covering, key=lambda s: (s.order_span, -s.confidence))
    return section_for_page(sections, page) if page is not None else None
