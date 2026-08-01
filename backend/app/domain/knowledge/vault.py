"""Domain rules for the Company Knowledge Vault.

The vault is the platform's permanent institutional memory: everything it has
ever concluded about a company, versioned, attributed and never overwritten.

Three rules govern it, and each exists because the obvious alternative is
quietly destructive.

**Nothing is ever overwritten.** A new filing supersedes an earlier assertion;
it does not replace it. `UPDATE knowledge SET value = ...` would make the
question "what did we believe about this company in FY2021, and why?"
permanently unanswerable — which is precisely the question an institutional
memory exists to answer. Supersession is therefore a new row plus a pointer,
and the old row keeps its evidence.

**Every entry carries its provenance and its confidence.** A vault entry that
cannot name the document, page and paragraph it came from is an opinion
wearing the clothes of a fact. Confidence is stored rather than implied so a
reader can distinguish "the annual report states" from "the model inferred".

**Freshness is a property of the source, not of the write.** A revenue figure
from the FY2026 annual report outranks one from an FY2024 report even if the
older document was ingested yesterday. Ordering by `created_at` would let a
late-arriving old filing overwrite current knowledge, which is the subtlest
way to corrupt a knowledge base.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum


class VaultSection(StrEnum):
    """The permanent sections of a company's vault.

    Exactly the list the brief specifies. A closed enum rather than free text
    because a typo in a section name creates a silent second vault section
    that nothing ever reads.
    """

    COMPANY_PROFILE = "company_profile"
    BUSINESS_MODEL = "business_model"
    PRODUCTS = "products"
    REVENUE_SEGMENTS = "revenue_segments"
    GEOGRAPHY = "geography"
    CUSTOMERS = "customers"
    SUPPLIERS = "suppliers"
    COMPETITORS = "competitors"
    MANAGEMENT = "management"
    PROMOTERS = "promoters"
    SUBSIDIARIES = "subsidiaries"
    FINANCIAL_STATEMENTS = "financial_statements"
    RATIOS = "ratios"
    HISTORICAL_AI_ANALYSIS = "historical_ai_analysis"
    RISKS = "risks"
    OPPORTUNITIES = "opportunities"
    VALUATION = "valuation"
    ESG = "esg"
    CAPITAL_ALLOCATION = "capital_allocation"
    AI_NOTES = "ai_notes"


class SummaryKind(StrEnum):
    """The nine permanent summaries generated per document.

    These are the memory that makes the platform fast: a later question reads
    these rather than re-parsing a 300-page PDF.
    """

    BRIEF_100 = "brief_100"
    DETAILED_500 = "detailed_500"
    INSTITUTIONAL = "institutional"
    INVESTMENT = "investment"
    BULL_THESIS = "bull_thesis"
    BEAR_THESIS = "bear_thesis"
    RISK = "risk"
    MANAGEMENT = "management"
    CAPITAL_ALLOCATION = "capital_allocation"


#: Target length in words, and the instruction that produces it.
SUMMARY_SPECS: dict[SummaryKind, tuple[int, str]] = {
    SummaryKind.BRIEF_100: (
        100,
        "Summarise this filing in about 100 words: what it reports and what "
        "changed.",
    ),
    SummaryKind.DETAILED_500: (
        500,
        "Summarise this filing in about 500 words, covering performance, "
        "segments, management commentary and outlook.",
    ),
    SummaryKind.INSTITUTIONAL: (
        350,
        "Write an institutional research note on this filing: what a "
        "portfolio manager needs to know, stated precisely and without "
        "promotional language.",
    ),
    SummaryKind.INVESTMENT: (
        300,
        "State the investment implications of this filing: what it changes "
        "about the case for owning the company.",
    ),
    SummaryKind.BULL_THESIS: (
        250,
        "Make the strongest evidence-based case FOR owning this company, "
        "drawn only from this filing.",
    ),
    SummaryKind.BEAR_THESIS: (
        250,
        "Make the strongest evidence-based case AGAINST owning this company, "
        "drawn only from this filing.",
    ),
    SummaryKind.RISK: (
        250,
        "Summarise the risks this filing discloses or implies, most material "
        "first.",
    ),
    SummaryKind.MANAGEMENT: (
        250,
        "Summarise what management says in this filing: their commentary, "
        "guidance, and how candid it appears.",
    ),
    SummaryKind.CAPITAL_ALLOCATION: (
        250,
        "Summarise capital allocation in this filing: capex, dividends, "
        "buybacks, debt and acquisitions.",
    ),
}


class EntryStatus(StrEnum):
    CURRENT = "current"
    #: Replaced by a later, better-sourced entry. Retained forever.
    SUPERSEDED = "superseded"
    #: Contradicted by another source and awaiting human judgement. Not served
    #: as current knowledge, not deleted.
    DISPUTED = "disputed"


#: How far each kind of source is trusted, before recency.
#:
#: Mirrors the filings layer: a company's audited annual report outranks a
#: quarterly release, which outranks a presentation, which outranks a model's
#: own inference. This is what stops a slide deck's rounded figure from
#: superseding the audited one.
SOURCE_AUTHORITY: dict[str, float] = {
    "annual_report": 1.00,
    "quarterly_report": 0.90,
    "exchange_filing": 0.85,
    "credit_rating": 0.80,
    "conference_call": 0.75,
    "investor_presentation": 0.70,
    "esg_report": 0.70,
    "press_release": 0.60,
    "research_note": 0.50,
    "other": 0.40,
    #: Generated by the platform's own model rather than read from a filing.
    "ai_inference": 0.35,
}


def authority_of(doc_type: str | None) -> float:
    return SOURCE_AUTHORITY.get((doc_type or "other").lower(), 0.40)


@dataclass(frozen=True, slots=True)
class Provenance:
    """Where a vault entry came from. Required, not optional."""

    document_id: int | None = None
    page: int | None = None
    paragraph: int | None = None
    fiscal_year: int | None = None
    quarter: str | None = None
    doc_type: str | None = None

    @property
    def is_citable(self) -> bool:
        """Can a reader reach the exact source of this claim?"""
        return self.document_id is not None and self.page is not None

    def as_dict(self) -> dict:
        return {
            "document_id": self.document_id, "page": self.page,
            "paragraph": self.paragraph, "fiscal_year": self.fiscal_year,
            "quarter": self.quarter, "doc_type": self.doc_type,
            "citable": self.is_citable,
        }


def supersedes(
    *,
    new_fiscal_year: int | None,
    new_authority: float,
    new_confidence: float,
    old_fiscal_year: int | None,
    old_authority: float,
    old_confidence: float,
) -> bool:
    """Should the new assertion replace the old one as *current*?

    Ordered by fiscal period first, then source authority, then confidence —
    deliberately **not** by ingestion time. A filing loaded today that reports
    FY2024 must not supersede knowledge from the FY2026 annual report; sorting
    by `created_at` would let a backfill silently rewind the vault, and the
    corruption would be invisible because every individual entry is correct.
    """
    if new_fiscal_year is not None and old_fiscal_year is not None:
        if new_fiscal_year != old_fiscal_year:
            return new_fiscal_year > old_fiscal_year
    elif new_fiscal_year is not None and old_fiscal_year is None:
        # A dated assertion beats an undated one.
        return True
    elif new_fiscal_year is None and old_fiscal_year is not None:
        return False

    if abs(new_authority - old_authority) > 1e-9:
        return new_authority > old_authority
    return new_confidence > old_confidence


#: Below this, an extracted assertion is recorded but never served as current
#: knowledge. It is kept because a later corroboration may raise it.
MIN_CURRENT_CONFIDENCE = 0.35


def is_servable(confidence: float, status: str | EntryStatus) -> bool:
    """May this entry be presented to a user as current knowledge?"""
    try:
        state = EntryStatus(status)
    except ValueError:
        return False
    return state is EntryStatus.CURRENT and confidence >= MIN_CURRENT_CONFIDENCE


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)
