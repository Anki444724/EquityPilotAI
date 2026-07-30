"""Document-type classification.

Which of the eight classes the brief names is this file? The answer drives
which extraction rules are trusted and how the document is presented, so
guessing badly is worse than declining to guess.

Two evidence sources, weighted:

* **Filename** — strong when it follows a convention ("BHARATCP_AR_FY25.pdf"),
  worthless when it is "document(3).pdf". Scored, not trusted.
* **Content** — the first pages plus the detected sections. A transcript has a
  moderator and a Q&A section; a rating report names a rating agency and an
  action. These are near-definitional and outweigh a filename.

Where the evidence is thin the result is :attr:`DocumentType.OTHER`, which the
UI shows as "unclassified" and invites the user to correct. Silently labelling
an unknown file an annual report would propagate a wrong assumption into every
extraction that followed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.domain.documents.types import (
    DetectedSection, DocumentType, ParsedDocument, SectionKind,
)


@dataclass(frozen=True, slots=True)
class TypeRule:
    doc_type: DocumentType
    #: Filename fragments, matched case-insensitively.
    filename_cues: tuple[str, ...] = ()
    #: Content phrases. Weighted more heavily than filenames.
    content_cues: tuple[str, ...] = ()
    #: Sections whose presence corroborates this type.
    sections: tuple[SectionKind, ...] = ()
    #: Phrases that rule the type out.
    exclude: tuple[str, ...] = ()


TYPE_RULES: tuple[TypeRule, ...] = (
    TypeRule(
        DocumentType.CONFERENCE_CALL,
        ("concall", "transcript", "earnings_call", "earnings call", "_call_"),
        ("earnings conference call", "conference call transcript", "moderator:",
         "ladies and gentlemen, welcome", "question-and-answer session",
         "thank you. we will now begin the question"),
        (SectionKind.CONFERENCE_QA,),
    ),
    TypeRule(
        DocumentType.CREDIT_RATING,
        ("rating", "crisil", "icra", "care_", "india_ratings", "brickwork"),
        ("rating action", "rating rationale", "crisil ratings limited",
         "icra limited", "care ratings", "india ratings and research",
         "key rating drivers", "rating sensitivities", "reaffirmed the rating"),
    ),
    TypeRule(
        DocumentType.DRHP,
        ("drhp", "rhp", "prospectus", "red_herring"),
        ("draft red herring prospectus", "red herring prospectus",
         "this offer is being made", "book running lead manager",
         "offer for sale", "the issue price"),
    ),
    TypeRule(
        DocumentType.INVESTOR_PRESENTATION,
        ("investor", "presentation", "_ppt", "deck", "investor_update"),
        ("investor presentation", "safe harbour statement",
         "this presentation may contain forward-looking",
         "investor update", "earnings presentation"),
    ),
    TypeRule(
        DocumentType.ESG_REPORT,
        ("esg", "brsr", "sustainability", "responsibility"),
        ("business responsibility and sustainability report",
         "sustainability report", "brsr core", "our esg framework",
         "scope 1 and scope 2 emissions"),
        (SectionKind.ESG,),
    ),
    TypeRule(
        DocumentType.QUARTERLY_REPORT,
        ("q1", "q2", "q3", "q4", "quarterly", "quarter"),
        ("unaudited financial results for the quarter",
         "quarterly financial results", "for the quarter ended",
         "quarter and year ended"),
        exclude=("annual report",),
    ),
    TypeRule(
        DocumentType.ANNUAL_REPORT,
        ("annual_report", "annualreport", "annual report", "_ar_", "ar_fy"),
        ("annual report", "directors' report", "board's report",
         "notice of annual general meeting",
         "management discussion and analysis",
         "independent auditor's report"),
        (SectionKind.MANAGEMENT_DISCUSSION, SectionKind.AUDITOR_REPORT,
         SectionKind.DIRECTORS_REPORT, SectionKind.NOTES_TO_ACCOUNTS),
    ),
    TypeRule(
        DocumentType.EXCHANGE_FILING,
        ("filing", "bse", "nse", "intimation", "disclosure", "outcome"),
        ("pursuant to regulation", "listing obligations and disclosure",
         "outcome of the board meeting", "we wish to inform the exchange",
         "sebi (listing obligations"),
    ),
    TypeRule(
        DocumentType.RESEARCH_NOTE,
        ("research", "note", "initiating_coverage"),
        ("initiating coverage", "target price", "we maintain our",
         "analyst certification"),
    ),
)

#: Weights. Content is trusted far above the filename, and a corroborating
#: section above a single phrase, because sections are structural evidence.
_FILENAME_WEIGHT = 0.30
_CONTENT_WEIGHT = 0.55
_SECTION_WEIGHT = 0.25
#: Below this the classifier declines rather than guesses.
MIN_SCORE = 0.28

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _normalise_filename(filename: str) -> str:
    return _NON_ALNUM.sub("_", filename.lower())


def score_type(
    rule: TypeRule,
    filename_key: str,
    content: str,
    section_kinds: set[SectionKind],
) -> float:
    """Score one candidate type against the evidence."""
    if any(term in content for term in rule.exclude):
        return 0.0

    score = 0.0
    if any(_NON_ALNUM.sub("_", cue) in filename_key for cue in rule.filename_cues):
        score += _FILENAME_WEIGHT
    hits = sum(1 for cue in rule.content_cues if cue in content)
    if hits:
        # Saturating: three distinct cues is decisive, ten is not thrice as so.
        score += _CONTENT_WEIGHT * min(1.0, hits / 3.0)
    matched_sections = len(set(rule.sections) & section_kinds)
    if matched_sections:
        score += _SECTION_WEIGHT * min(1.0, matched_sections / 2.0)
    return round(score, 4)


def classify_document(
    filename: str,
    parsed: ParsedDocument,
    *,
    sections: list[DetectedSection] | None = None,
    sample_chars: int = 12_000,
) -> DocumentType:
    """Best-supported document type, or :attr:`DocumentType.OTHER`.

    Only the first few pages are sampled. A 300-page annual report contains the
    phrase "conference call" somewhere; the cover and contents pages are where
    a document declares what it is.
    """
    return classify_with_confidence(
        filename, parsed, sections=sections, sample_chars=sample_chars
    )[0]


def classify_with_confidence(
    filename: str,
    parsed: ParsedDocument,
    *,
    sections: list[DetectedSection] | None = None,
    sample_chars: int = 12_000,
) -> tuple[DocumentType, float]:
    """As :func:`classify_document`, but also returns the winning score."""
    filename_key = _normalise_filename(filename)
    content = parsed.full_text[:sample_chars].lower()
    section_kinds = {s.kind for s in (sections or [])}

    best_type, best_score = DocumentType.OTHER, 0.0
    for rule in TYPE_RULES:
        score = score_type(rule, filename_key, content, section_kinds)
        if score > best_score:
            best_type, best_score = rule.doc_type, score

    if best_score < MIN_SCORE:
        return DocumentType.OTHER, round(best_score, 4)
    return best_type, round(min(1.0, best_score), 4)
