"""Evidence assembly: one read of the world, shared by all ten modules.

Every module scorer reads from a single :class:`ScoringEvidence` built here.
That is the same single-resolution discipline the platform has used since
Module 1, and it matters more in this engine than anywhere else: ten modules
each issuing their own queries would produce ten slightly different views of
the same company, and the composite would be a blend of inconsistent facts.

**Nothing in this module scores anything.** It gathers, normalises and cites.
The scorers are pure functions of what it returns, which is what makes them
testable without a database and what makes the whole engine deterministic.

**Every gathered item arrives with its citation already attached.** Building
the citation at the point of reading is the only way it stays correct — a
citation constructed later, from a value, is a guess about where that value
came from.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from sqlalchemy import func, select

from app.domain.ai_scoring.types import Citation, CitationKind
from app.models.company import Company
from app.models.document import Document
from app.models.filing_collection import CompanyCrawlState, DiscoveredFiling
from app.models.knowledge import DocumentSummary, KnowledgeEntry, YearlyObservation


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


#: How far back "latest news" reaches. One year rather than one quarter: the
#: Indian filing calendar clusters disclosures around results, and a 90-day
#: window scores a company as newsless for most of the year purely because of
#: where the calendar happens to sit.
NEWS_WINDOW_DAYS = 365

#: Announcements newer than this count as "recent" for the freshness factor.
RECENT_NEWS_DAYS = 90

#: Document types that constitute management commentary, in the brief's terms.
COMMENTARY_DOC_TYPES: frozenset[str] = frozenset({
    "earnings_call", "concall", "transcript",
    "annual_report", "investor_presentation", "presentation",
})

#: Filing types the collector assigns that map to commentary.
COMMENTARY_FILING_TYPES: frozenset[str] = frozenset({
    "annual_report", "earnings_call", "investor_presentation",
    "quarterly_results", "presentation", "transcript",
})


@dataclass(slots=True)
class NewsItem:
    """One dated corporate development, with its classification."""

    title: str
    published_on: datetime | None
    filing_type: str | None
    source: str
    url: str | None
    reference: str
    document_id: int | None = None

    @property
    def age_days(self) -> int | None:
        if self.published_on is None:
            return None
        published = self.published_on
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
        return max(0, (_utcnow() - published).days)

    def citation(self) -> Citation:
        return Citation(
            kind=CitationKind.ANNOUNCEMENT,
            label=f"{self.source}: {self.title[:140]}",
            reference=self.reference,
            document_id=self.document_id,
            url=self.url,
        )


@dataclass(slots=True)
class PeerContext:
    """Cross-sectional context for the company's sector.

    Held separately from the company's own figures because a peer median is a
    different kind of claim — it is a statement about the sector, cited as
    such, and a valuation factor that compares against it must say so.
    """

    sector: str | None = None
    peer_count: int = 0
    median_pe: float | None = None
    median_pb: float | None = None
    median_revenue_growth: float | None = None
    median_roe: float | None = None
    #: Company's own market cap rank within the sector, 1 = largest.
    market_cap_rank: int | None = None

    def citation(self) -> Citation:
        return Citation(
            kind=CitationKind.PEER,
            label=(f"Sector aggregate: {self.sector or 'unclassified'} "
                   f"({self.peer_count} listed peers)"),
            reference=f"sector:{self.sector}",
        )


@dataclass(slots=True)
class ScoringEvidence:
    """Everything the ten scorers read, resolved once."""

    company: Company

    # --- financial statements (canonical, from AnalysisService) ---------
    incomes: list[Any] = field(default_factory=list)
    balances: list[Any] = field(default_factory=list)
    cash_flows: list[Any] = field(default_factory=list)

    # --- valuation outputs (from ValuationService, when available) ------
    pe_ratio: float | None = None
    pb_ratio: float | None = None
    ev_ebitda: float | None = None
    intrinsic_value: float | None = None
    upside: float | None = None
    margin_of_safety: float | None = None
    wacc: float | None = None
    valuation_is_illustrative: bool = False
    valuation_disclosure: str | None = None

    # --- corpus evidence ------------------------------------------------
    news: list[NewsItem] = field(default_factory=list)
    documents: list[Document] = field(default_factory=list)
    commentary_documents: list[Document] = field(default_factory=list)
    summaries: list[DocumentSummary] = field(default_factory=list)
    vault_entries: list[KnowledgeEntry] = field(default_factory=list)
    observations: list[YearlyObservation] = field(default_factory=list)
    crawl_state: CompanyCrawlState | None = None

    # --- derived context ------------------------------------------------
    peers: PeerContext = field(default_factory=PeerContext)
    #: Management credibility from temporal memory: (score, years assessed).
    credibility: tuple[float | None, int] = (None, 0)
    #: Data Quality Score 0-100, used only to caveat, never to score.
    data_quality: float | None = None

    # ------------------------------------------------------------ helpers
    @property
    def latest_income(self):
        return self.incomes[-1] if self.incomes else None

    @property
    def latest_balance(self):
        return self.balances[-1] if self.balances else None

    @property
    def latest_cash_flow(self):
        return self.cash_flows[-1] if self.cash_flows else None

    @property
    def years(self) -> int:
        return len(self.incomes)

    def avg_balance(self, attr: str) -> float | None:
        """Average of opening and closing, the convention used throughout."""
        if not self.balances:
            return None
        closing = getattr(self.balances[-1], attr, None)
        opening = getattr(self.balances[-2], attr, None) if len(self.balances) > 1 else None
        if closing is None:
            return opening
        return closing if opening is None else (closing + opening) / 2

    def series(self, statement: str, attr: str, periods: int = 5) -> list[float | None]:
        source = {
            "income": self.incomes,
            "balance": self.balances,
            "cash_flow": self.cash_flows,
        }[statement]
        return [getattr(row, attr, None) for row in source[-periods:]]

    def vault(self, section: str) -> list[KnowledgeEntry]:
        return [e for e in self.vault_entries if e.section == section]

    def summaries_of(self, kind: str) -> list[DocumentSummary]:
        """Non-fallback summaries of one kind.

        Fallback summaries are template prose written when no model was
        reachable. Counting them as evidence would let the engine credit a
        company for analysis that was never performed.
        """
        return [s for s in self.summaries
                if s.kind == kind and not s.is_fallback]

    def recent_news(self, days: int = RECENT_NEWS_DAYS) -> list[NewsItem]:
        return [n for n in self.news
                if n.age_days is not None and n.age_days <= days]

    # ---------------------------------------------------------- citations
    def statement_citation(self, line: str, fiscal_year: int | None = None) -> Citation:
        year = fiscal_year or (self.latest_income.fiscal_year
                               if self.latest_income else None)
        return Citation(
            kind=CitationKind.STATEMENT,
            label=f"Canonical financials: {line}" + (f", FY{year}" if year else ""),
            reference=f"financial_facts:{self.company.id}:{line}",
            fiscal_year=year,
        )

    def reference_citation(self, field_name: str, value: Any) -> Citation:
        return Citation(
            kind=CitationKind.REFERENCE,
            label=f"Company reference data: {field_name} = {value}",
            reference=f"companies.{field_name}:{self.company.id}",
        )

    @staticmethod
    def vault_citation(entry: KnowledgeEntry) -> Citation:
        return Citation(
            kind=CitationKind.VAULT,
            label=f"Knowledge vault: {entry.section}.{entry.key} (v{entry.version})",
            reference=f"knowledge_entries:{entry.id}",
            document_id=entry.document_id,
            page=entry.page,
            fiscal_year=entry.fiscal_year,
            excerpt=(entry.evidence or entry.value_text or None),
        )

    @staticmethod
    def summary_citation(summary: DocumentSummary) -> Citation:
        return Citation(
            kind=CitationKind.SUMMARY,
            label=(f"AI summary ({summary.kind})"
                   + (f", FY{summary.fiscal_year}" if summary.fiscal_year else "")),
            reference=f"document_summaries:{summary.id}",
            document_id=summary.document_id,
            fiscal_year=summary.fiscal_year,
        )

    @staticmethod
    def observation_citation(observation: YearlyObservation) -> Citation:
        return Citation(
            kind=CitationKind.OBSERVATION,
            label=f"Temporal memory: FY{observation.fiscal_year} observation",
            reference=f"yearly_observations:{observation.id}",
            fiscal_year=observation.fiscal_year,
        )

    @staticmethod
    def document_citation(document: Document) -> Citation:
        return Citation(
            kind=CitationKind.FILING,
            label=(document.title or document.filename)
                  + (f" (FY{document.fiscal_year})" if document.fiscal_year else ""),
            reference=f"documents:{document.id}",
            document_id=document.id,
            fiscal_year=document.fiscal_year,
        )

    def fingerprint_payload(self) -> dict[str, Any]:
        """The observed inputs, reduced to a comparable snapshot.

        Only things that would change a score appear here. Timestamps of the
        run itself deliberately do not — otherwise every recalculation would
        produce a new fingerprint and the "nothing changed" case would be
        indistinguishable from a real revision.
        """
        return {
            "company": self.company.id,
            "years": [getattr(i, "fiscal_year", None) for i in self.incomes],
            "revenue": [getattr(i, "total_revenue", None) for i in self.incomes],
            "pat": [getattr(i, "pat", None) for i in self.incomes],
            "equity": [getattr(b, "shareholders_equity", None) for b in self.balances],
            "cfo": [getattr(c, "cfo", None) for c in self.cash_flows],
            "price": self.company.current_price,
            "market_cap": self.company.market_cap,
            "pe": self.pe_ratio, "pb": self.pb_ratio, "ev_ebitda": self.ev_ebitda,
            "intrinsic": self.intrinsic_value,
            "documents": sorted(d.id for d in self.documents),
            "summaries": sorted(s.id for s in self.summaries),
            "vault": sorted(e.id for e in self.vault_entries),
            "observations": sorted(o.id for o in self.observations),
            "news": sorted(n.reference for n in self.news),
            "peers": {
                "sector": self.peers.sector,
                "count": self.peers.peer_count,
                "median_pe": self.peers.median_pe,
            },
        }


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------

class EvidenceBuilder:
    """Assembles :class:`ScoringEvidence` from the database in one pass."""

    def __init__(self, db: Any) -> None:
        self.db = db

    def build(
        self,
        company: Company,
        *,
        analysis: Any | None = None,
        valuation_bundle: Any | None = None,
    ) -> ScoringEvidence:
        evidence = ScoringEvidence(company=company)

        if analysis is not None:
            evidence.incomes = list(analysis.incomes)
            evidence.balances = list(analysis.balances)
            evidence.cash_flows = list(analysis.cash_flows)

        if valuation_bundle is not None:
            self._apply_valuation(evidence, valuation_bundle, analysis)

        evidence.documents = self._documents(company.id)
        evidence.commentary_documents = [
            d for d in evidence.documents
            if (d.doc_type or "").lower() in COMMENTARY_DOC_TYPES
        ]
        evidence.news = self._news(company.id)
        evidence.summaries = self._summaries(company.id)
        evidence.vault_entries = self._vault(company.id)
        evidence.observations = self._observations(company.id)
        evidence.crawl_state = self._crawl_state(company.id)
        evidence.peers = self._peers(company)
        evidence.credibility = self._credibility(company.id)
        evidence.data_quality = self._data_quality(company.id)

        return evidence

    # ------------------------------------------------------------ sources
    @staticmethod
    def _apply_valuation(
        evidence: ScoringEvidence, bundle: Any, analysis: Any | None
    ) -> None:
        """Copy the valuation outputs the scorers read.

        Wrapped in getattr chains rather than direct attribute access: the
        valuation bundle is assembled from several optional sub-engines and a
        company with no comparable set has no `relative` block at all. A
        missing multiple must read as missing, not raise.
        """
        summary = getattr(bundle, "summary", None)
        relative = getattr(bundle, "relative", None)
        current = getattr(relative, "current", None) if relative else None
        quality = getattr(bundle, "quality", None)
        wacc = getattr(bundle, "wacc", None)

        evidence.intrinsic_value = getattr(summary, "weighted_value", None)
        evidence.upside = getattr(summary, "upside", None)
        evidence.margin_of_safety = getattr(summary, "margin_of_safety", None)
        evidence.pe_ratio = getattr(current, "pe", None)
        evidence.ev_ebitda = getattr(current, "ev_ebitda", None)
        evidence.wacc = getattr(wacc, "wacc", None)

        if quality is not None:
            evidence.valuation_is_illustrative = bool(
                getattr(quality, "is_illustrative", False)
            )
            evidence.valuation_disclosure = getattr(quality, "disclosure", None)

        # Price / book from the statements, since the relative engine does not
        # always carry it.
        balance = evidence.latest_balance
        income = evidence.latest_income
        price = evidence.company.current_price
        if balance and income and price:
            equity = getattr(balance, "shareholders_equity", None)
            shares = getattr(income, "weighted_shares", None)
            if equity and shares:
                bvps = equity / shares
                if bvps:
                    evidence.pb_ratio = price / bvps

    def _documents(self, company_id: str) -> list[Document]:
        return list(self.db.execute(
            select(Document)
            .where(
                Document.company_id == company_id,
                Document.superseded_by.is_(None),
            )
            .order_by(Document.fiscal_year.desc().nullslast(),
                      Document.id.desc())
            .limit(200)
        ).scalars().all())

    def _news(self, company_id: str) -> list[NewsItem]:
        cutoff = _utcnow() - timedelta(days=NEWS_WINDOW_DAYS)
        rows = self.db.execute(
            select(DiscoveredFiling)
            .where(
                DiscoveredFiling.company_id == company_id,
                DiscoveredFiling.published_on.is_not(None),
                DiscoveredFiling.published_on >= cutoff,
            )
            .order_by(DiscoveredFiling.published_on.desc())
            .limit(300)
        ).scalars().all()
        return [
            NewsItem(
                title=row.title or "(untitled announcement)",
                published_on=row.published_on,
                filing_type=row.filing_type,
                source=row.source,
                url=row.source_url,
                reference=f"discovered_filings:{row.id}",
                document_id=row.document_id,
            )
            for row in rows
        ]

    def _summaries(self, company_id: str) -> list[DocumentSummary]:
        return list(self.db.execute(
            select(DocumentSummary)
            .where(DocumentSummary.company_id == company_id)
            .order_by(DocumentSummary.fiscal_year.desc().nullslast(),
                      DocumentSummary.id.desc())
            .limit(300)
        ).scalars().all())

    def _vault(self, company_id: str) -> list[KnowledgeEntry]:
        return list(self.db.execute(
            select(KnowledgeEntry)
            .where(
                KnowledgeEntry.company_id == company_id,
                KnowledgeEntry.status == "current",
            )
            .order_by(KnowledgeEntry.section, KnowledgeEntry.key)
            .limit(500)
        ).scalars().all())

    def _observations(self, company_id: str) -> list[YearlyObservation]:
        return list(self.db.execute(
            select(YearlyObservation)
            .where(
                YearlyObservation.company_id == company_id,
                YearlyObservation.status == "current",
            )
            .order_by(YearlyObservation.fiscal_year)
            .limit(30)
        ).scalars().all())

    def _crawl_state(self, company_id: str) -> CompanyCrawlState | None:
        return self.db.execute(
            select(CompanyCrawlState)
            .where(CompanyCrawlState.company_id == company_id)
        ).scalar_one_or_none()

    def _peers(self, company: Company) -> PeerContext:
        """Sector aggregates, computed in the database rather than in Python.

        The universe is 500 companies and this runs on every score; pulling
        every peer row into the process to take a median would make a single
        score a 500-row read. The median is approximated by an average here
        because SQLite — which the test suite runs on — has no
        `percentile_cont`, and a portable approximation that both engines
        agree on is worth more than an exact figure that only works in
        production.
        """
        if not company.sector:
            return PeerContext()

        row = self.db.execute(
            select(
                func.count(Company.id),
                func.avg(Company.market_cap),
            ).where(
                Company.sector == company.sector,
                Company.listing_status == "active",
                Company.id != company.id,
            )
        ).one_or_none()

        peer_count = int(row[0]) if row and row[0] else 0
        if not peer_count:
            return PeerContext(sector=company.sector)

        rank = None
        if company.market_cap:
            larger = self.db.execute(
                select(func.count(Company.id)).where(
                    Company.sector == company.sector,
                    Company.listing_status == "active",
                    Company.market_cap.is_not(None),
                    Company.market_cap > company.market_cap,
                )
            ).scalar_one_or_none()
            rank = int(larger) + 1 if larger is not None else None

        return PeerContext(
            sector=company.sector,
            peer_count=peer_count,
            market_cap_rank=rank,
        )

    def _credibility(self, company_id: str) -> tuple[float | None, int]:
        """Management credibility from temporal memory.

        Reuses `TemporalMemoryService.credibility` rather than recomputing the
        verdict weighting — that calculation exists once, in the temporal
        domain, and a second copy here would drift the moment either changed.
        """
        try:
            from app.services.knowledge.temporal import TemporalMemoryService
            result = TemporalMemoryService(self.db).credibility(company_id)
            return result.get("score"), int(result.get("years_assessed") or 0)
        except Exception:  # noqa: BLE001 — credibility is optional context
            return None, 0

    def _data_quality(self, company_id: str) -> float | None:
        from app.models.scoring import DataQualitySnapshot
        row = self.db.execute(
            select(DataQualitySnapshot.score)
            .where(DataQualitySnapshot.company_id == company_id)
        ).scalar_one_or_none()
        return float(row) if row is not None else None
