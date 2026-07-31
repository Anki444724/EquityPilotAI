"""Section-aware orchestration for research reports.

The defect this fixes: every section of a report was answered from one
undifferentiated pool of evidence, and because the scoring engine contributed
the largest single block — 17 of 60 citations, 28% — the relevance ranking
handed scoring output to nearly every question. A reader asking about the
business model received a summary of quality scores.

That is wrong in a way that is hard to see. Every figure quoted is real and
correctly cited, so nothing downstream flags it; the answer is simply about
the wrong thing, dressed as an answer about the right thing.

The fix is to make each section declare which sources may answer it, in
priority order, and to restrict its evidence to those sources before the
prompt is built. Scoring is confined to the three sections it genuinely
speaks to. Everything else is routed to the provider that actually holds the
information.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

import structlog

from app.data.filings.base import SourceCategory
from app.domain.ai.types import Citation, EvidenceKind

log = structlog.get_logger(__name__)


class Section(StrEnum):
    """The sections of a research report, in the order they are written."""

    EXECUTIVE_SUMMARY = "executive_summary"
    CATALYSTS = "catalysts"
    BUSINESS_MODEL = "business_model"
    REVENUE_SEGMENTS = "revenue_segments"
    FINANCIAL_PERFORMANCE = "financial_performance"
    VALUATION = "valuation"
    RISKS = "risks"
    LATEST_NEWS = "latest_news"
    MANAGEMENT_COMMENTARY = "management_commentary"
    QUALITY_SCORES = "quality_scores"
    RISK_SCORES = "risk_scores"
    INSTITUTIONAL_SCORE = "institutional_score"
    BULL_THESIS = "bull_thesis"
    BEAR_THESIS = "bear_thesis"
    INVESTMENT_VERDICT = "investment_verdict"


class Provider(StrEnum):
    """Where a section's evidence may come from."""

    RAG = "Annual Report (RAG)"
    FINANCIAL_DB = "Financial Database"
    VALUATION_ENGINE = "Valuation Engine"
    SCORING_ENGINE = "Scoring Engine"
    MARKET_DATA = "Market Data (Finnhub/FMP)"
    FILINGS = "Official Filings"
    SYNTHESIS = "Synthesis of retrieved evidence"


#: Which evidence kinds each provider supplies. The mapping is what makes a
#: routing rule enforceable rather than advisory: a section restricted to RAG
#: has document evidence and nothing else placed in front of the model.
PROVIDER_KINDS: dict[Provider, frozenset[EvidenceKind]] = {
    Provider.RAG: frozenset({EvidenceKind.DOCUMENT}),
    Provider.FILINGS: frozenset({EvidenceKind.DOCUMENT}),
    Provider.FINANCIAL_DB: frozenset({
        EvidenceKind.STATEMENT, EvidenceKind.RATIO, EvidenceKind.FORECAST,
    }),
    Provider.VALUATION_ENGINE: frozenset({EvidenceKind.VALUATION}),
    Provider.SCORING_ENGINE: frozenset({EvidenceKind.SCORING}),
    Provider.MARKET_DATA: frozenset({EvidenceKind.MARKET}),
    Provider.SYNTHESIS: frozenset(EvidenceKind),
}


@dataclass(frozen=True, slots=True)
class SectionRoute:
    """Which providers may answer one section, in priority order."""

    section: Section
    title: str
    providers: tuple[Provider, ...]
    category: SourceCategory
    #: The question put to the model for this section.
    prompt: str
    #: A section that synthesises rather than retrieves sees everything that
    #: earlier sections gathered, because that is the work it is doing.
    synthesises: bool = False

    @property
    def kinds(self) -> frozenset[EvidenceKind]:
        allowed: set[EvidenceKind] = set()
        for provider in self.providers:
            allowed |= PROVIDER_KINDS[provider]
        return frozenset(allowed)


#: The routing table, exactly as the brief specifies it.
#:
#: Scoring appears in three places only. It is a derived opinion about a
#: company, not a source of fact about one, so letting it answer "what is the
#: business model?" was the defect.
ROUTES: tuple[SectionRoute, ...] = (
    SectionRoute(
        Section.BUSINESS_MODEL, "Business Model",
        (Provider.RAG, Provider.FILINGS, Provider.FINANCIAL_DB),
        SourceCategory.ANNUAL_REPORT,
        "Describe this company's business model: what it sells, to whom, and "
        "how it earns money.",
    ),
    SectionRoute(
        Section.REVENUE_SEGMENTS, "Revenue Segments",
        (Provider.RAG, Provider.FINANCIAL_DB),
        SourceCategory.ANNUAL_REPORT,
        "Break down revenue by segment and describe how the mix has shifted.",
    ),
    SectionRoute(
        Section.FINANCIAL_PERFORMANCE, "Financial Performance",
        (Provider.FINANCIAL_DB,),
        SourceCategory.INTERNAL_DATABASE,
        "Summarise revenue, margins, profitability and cash generation, and "
        "how they have moved.",
    ),
    SectionRoute(
        Section.VALUATION, "Valuation",
        (Provider.VALUATION_ENGINE, Provider.MARKET_DATA),
        SourceCategory.INTERNAL_DATABASE,
        "What is this company worth on the platform's methodologies, and how "
        "does that compare with the market price?",
    ),
    SectionRoute(
        Section.RISKS, "Risks",
        (Provider.RAG, Provider.FILINGS, Provider.FINANCIAL_DB),
        SourceCategory.ANNUAL_REPORT,
        "What are the principal risks disclosed by the company and evident in "
        "its financials?",
    ),
    SectionRoute(
        Section.CATALYSTS, "Catalysts",
        (Provider.RAG, Provider.MARKET_DATA, Provider.FILINGS,
         Provider.FINANCIAL_DB),
        SourceCategory.ANNUAL_REPORT,
        "What identifiable events, announcements or trends could re-rate this "
        "company over the next twelve months?",
    ),
    SectionRoute(
        Section.LATEST_NEWS, "Latest News",
        (Provider.MARKET_DATA, Provider.FILINGS),
        SourceCategory.MARKET_DATA,
        "What has been reported about this company recently?",
    ),
    SectionRoute(
        Section.MANAGEMENT_COMMENTARY, "Management Commentary",
        (Provider.RAG, Provider.FILINGS),
        SourceCategory.ANNUAL_REPORT,
        "What does management say about performance and outlook, in the "
        "chairman's statement and management discussion?",
    ),
    SectionRoute(
        Section.QUALITY_SCORES, "Quality Scores",
        (Provider.SCORING_ENGINE,),
        SourceCategory.INTERNAL_DATABASE,
        "Report the quality dimensions of the institutional score.",
    ),
    SectionRoute(
        Section.RISK_SCORES, "Risk Scores",
        (Provider.SCORING_ENGINE,),
        SourceCategory.INTERNAL_DATABASE,
        "Report the risk dimensions of the institutional score.",
    ),
    SectionRoute(
        Section.INSTITUTIONAL_SCORE, "Institutional Score",
        (Provider.SCORING_ENGINE,),
        SourceCategory.INTERNAL_DATABASE,
        "Report the overall institutional score, grade and recommendation.",
    ),
    SectionRoute(
        Section.BULL_THESIS, "Bull Thesis",
        (Provider.SYNTHESIS,),
        SourceCategory.INTERNAL_DATABASE,
        "Make the strongest evidence-based case for owning this company.",
        synthesises=True,
    ),
    SectionRoute(
        Section.BEAR_THESIS, "Bear Thesis",
        (Provider.SYNTHESIS,),
        SourceCategory.INTERNAL_DATABASE,
        "Make the strongest evidence-based case against owning this company.",
        synthesises=True,
    ),
    SectionRoute(
        Section.INVESTMENT_VERDICT, "Investment Verdict",
        (Provider.SYNTHESIS,),
        SourceCategory.INTERNAL_DATABASE,
        "Weigh every preceding section and state a verdict.",
        synthesises=True,
    ),
    # Executed last, presented first — see PRESENTATION_ORDER below.
    SectionRoute(
        Section.EXECUTIVE_SUMMARY, "Executive Summary",
        (Provider.SYNTHESIS,),
        SourceCategory.INTERNAL_DATABASE,
        "State the investment case in five sentences: what the company does, "
        "how it is performing, what it is worth, the single largest risk, and "
        "the conclusion.",
        synthesises=True,
    ),
)

ROUTES_BY_SECTION: dict[Section, SectionRoute] = {r.section: r for r in ROUTES}

#: The order a reader sees, which is not the order the sections are written.
#:
#: An executive summary that leads a report must be composed *after* the
#: material it summarises, or it is a summary of nothing. `ROUTES` is
#: therefore execution order and this is presentation order; the orchestrator
#: sorts by this before returning. Getting these the same way round was the
#: alternative — write the summary first from raw evidence — and it produces
#: exactly the generic opening paragraph this platform exists to avoid.
PRESENTATION_ORDER: tuple[Section, ...] = (
    Section.EXECUTIVE_SUMMARY,
    Section.BUSINESS_MODEL,
    Section.REVENUE_SEGMENTS,
    Section.FINANCIAL_PERFORMANCE,
    Section.VALUATION,
    Section.BULL_THESIS,
    Section.BEAR_THESIS,
    Section.RISKS,
    Section.CATALYSTS,
    Section.MANAGEMENT_COMMENTARY,
    Section.LATEST_NEWS,
    Section.QUALITY_SCORES,
    Section.RISK_SCORES,
    Section.INSTITUTIONAL_SCORE,
    Section.INVESTMENT_VERDICT,
)


def presentation_rank(section: Section) -> int:
    """Sort key for display order; unlisted sections fall to the end."""
    try:
        return PRESENTATION_ORDER.index(section)
    except ValueError:
        return len(PRESENTATION_ORDER)


@dataclass(slots=True)
class SectionResult:
    """One answered section, with everything needed to audit it."""

    section: Section
    title: str
    content: str
    provider_used: Provider | None = None
    source_category: SourceCategory | None = None
    confidence: float = 0.0
    citations: list[Citation] = field(default_factory=list)
    #: Providers tried and what each returned. A section is never omitted;
    #: when nothing answers, that is itself reported.
    attempted: list[dict[str, Any]] = field(default_factory=list)
    timestamp: str = ""
    evidence_count: int = 0

    # --- writing layer -------------------------------------------------
    # Deliberately distinct from `provider_used`, which names where the
    # *evidence* came from. Conflating the two is how a reader ends up
    # believing OpenRouter sourced the annual report. Both are reported:
    # "Annual Report (RAG), written by OpenRouter/gpt-4o-mini" is the honest
    # description of a section.
    writer_provider: str = "none"
    writer_model: str = "none"
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def has_evidence(self) -> bool:
        return bool(self.citations)

    def references(self) -> list[str]:
        """Page and chunk where the evidence supports it."""
        out: list[str] = []
        for citation in self.citations[:6]:
            if citation.page is not None:
                out.append(
                    f"{citation.label} (p.{citation.page}"
                    + (f", chunk {citation.chunk_id}" if citation.chunk_id else "")
                    + ")"
                )
            else:
                out.append(f"{citation.label} — {citation.source}")
        return out

    def as_dict(self) -> dict[str, Any]:
        return {
            "section": self.section.value,
            "title": self.title,
            "content": self.content,
            "provider_used": self.provider_used.value if self.provider_used else None,
            "source_used": (
                self.source_category.value if self.source_category else None
            ),
            "confidence_score": round(self.confidence, 3),
            "citations": self.references(),
            "citation_count": len(self.citations),
            "evidence_count": self.evidence_count,
            "timestamp": self.timestamp,
            "providers_attempted": self.attempted,
            "has_evidence": self.has_evidence,
            "writer_provider": self.writer_provider,
            "writer_model": self.writer_model,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": round(self.cost_usd, 6),
        }


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def select_evidence(
    citations: list[Citation], route: SectionRoute,
) -> tuple[list[Citation], Provider | None, list[dict[str, Any]]]:
    """Pick the highest-priority provider that actually has evidence.

    Walks the section's providers in order and returns the first with
    anything. Falling through rather than omitting is deliberate: a section
    with weaker evidence is more useful than a missing section, provided the
    weaker source is named.
    """
    attempted: list[dict[str, Any]] = []

    if route.synthesises:
        # A thesis section reasons over everything already gathered.
        return list(citations), Provider.SYNTHESIS, [
            {"provider": Provider.SYNTHESIS.value, "outcome": "served",
             "count": len(citations)}
        ]

    for provider in route.providers:
        kinds = PROVIDER_KINDS[provider]
        matched = [c for c in citations if c.kind in kinds]
        if matched:
            attempted.append({"provider": provider.value, "outcome": "served",
                              "count": len(matched)})
            return matched, provider, attempted
        attempted.append({"provider": provider.value, "outcome": "no_evidence"})

    return [], None, attempted


#: How far each provider is trusted for a section, before recency and
#: completeness. Mirrors the filings layer: a company's own report outranks a
#: derived score, which outranks a third-party quote.
PROVIDER_CONFIDENCE: dict[Provider, float] = {
    Provider.RAG: 1.00,
    Provider.FILINGS: 0.95,
    Provider.FINANCIAL_DB: 0.85,
    Provider.VALUATION_ENGINE: 0.80,
    Provider.SCORING_ENGINE: 0.75,
    Provider.MARKET_DATA: 0.60,
    Provider.SYNTHESIS: 0.70,
}


def score_section(provider: Provider | None, citations: list[Citation]) -> float:
    """Confidence in one section's answer.

    Zero without evidence — a section the model wrote from nothing should not
    inherit the credibility of one grounded in a filing.
    """
    if provider is None or not citations:
        return 0.0
    base = PROVIDER_CONFIDENCE.get(provider, 0.5)
    # Six citations is treated as a full complement; beyond that the marginal
    # citation adds little.
    depth = min(len(citations) / 6.0, 1.0)
    return round(base * (0.6 + 0.4 * depth), 3)
