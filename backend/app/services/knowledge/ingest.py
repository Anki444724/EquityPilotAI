"""Promote extracted document facts into the permanent Knowledge Vault.

The extraction pipeline already produces `DocumentFact` rows — 73 declared
fields with a confidence, a page and a verbatim evidence string. What it never
did was decide *what the company is currently believed to be true of*: facts
accumulated per document, so "the principal risk" existed once per filing with
nothing saying which was current.

This module is that decision. It maps each extractor category onto a vault
section, replays facts in period order, and lets `KnowledgeVault.assert_
knowledge` version them. The result is a single current view with full history
behind it.

**Replay order matters and is not the row order.** Facts are sorted by fiscal
year before assertion, so a backfill that ingests FY2024 after FY2026 still
leaves FY2026 as current. Feeding them in database order would make the vault
depend on ingestion sequence, which is the subtlest way to get a plausible and
wrong answer.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import structlog
from sqlalchemy import select

from app.domain.knowledge.vault import Provenance, VaultSection
from app.models.document import Document, DocumentFact
from app.services.knowledge.vault import KnowledgeVault

log = structlog.get_logger(__name__)

#: Extractor category -> vault section.
#:
#: The extractor's taxonomy was designed for document parsing and the vault's
#: for institutional memory, so they are close but not identical. Mapping them
#: explicitly is what keeps both free to change.
CATEGORY_TO_SECTION: dict[str, VaultSection] = {
    "BUSINESS": VaultSection.BUSINESS_MODEL,
    "FINANCIAL": VaultSection.FINANCIAL_STATEMENTS,
    "METRICS": VaultSection.RATIOS,
    "GUIDANCE": VaultSection.OPPORTUNITIES,
    "MD&A": VaultSection.HISTORICAL_AI_ANALYSIS,
    "RISKS": VaultSection.RISKS,
    "OPPORTUNITIES": VaultSection.OPPORTUNITIES,
    "MOAT": VaultSection.BUSINESS_MODEL,
    "CUSTOMERS": VaultSection.CUSTOMERS,
    "SUBSIDIARIES": VaultSection.SUBSIDIARIES,
    "GOVERNANCE": VaultSection.MANAGEMENT,
    "ESG": VaultSection.ESG,
    "DEBT": VaultSection.CAPITAL_ALLOCATION,
    "CAPEX": VaultSection.CAPITAL_ALLOCATION,
    "CAPACITY": VaultSection.PRODUCTS,
    "ORDER BOOK": VaultSection.PRODUCTS,
}

#: Fields whose meaning is a *segment* or *geography* breakdown rather than the
#: business model generally. Routed separately so those vault sections are
#: populated rather than left empty while the data sits under BUSINESS.
FIELD_OVERRIDES: dict[str, VaultSection] = {
    "segment_revenue_split": VaultSection.REVENUE_SEGMENTS,
    "geographic_revenue_split": VaultSection.GEOGRAPHY,
    "product_brand_portfolio": VaultSection.PRODUCTS,
    "business_description": VaultSection.COMPANY_PROFILE,
    "credit_rating": VaultSection.CAPITAL_ALLOCATION,
}


@dataclass(slots=True)
class IngestReport:
    documents: int = 0
    facts_seen: int = 0
    created: int = 0
    versioned: int = 0
    recorded_stale: int = 0
    rejected: int = 0
    unmapped: list[str] = field(default_factory=list)
    latency_ms: float = 0.0

    @property
    def asserted(self) -> int:
        return self.created + self.versioned + self.recorded_stale

    def as_dict(self) -> dict[str, Any]:
        return {
            "documents": self.documents,
            "facts_seen": self.facts_seen,
            "asserted": self.asserted,
            "created": self.created,
            "versioned": self.versioned,
            "recorded_stale": self.recorded_stale,
            "rejected": self.rejected,
            "unmapped_categories": sorted(set(self.unmapped)),
            "latency_ms": round(self.latency_ms, 1),
        }


class KnowledgeIngestor:
    """Replays extracted facts into the vault, in period order."""

    def __init__(self, db: Any) -> None:
        self.db = db
        self.vault = KnowledgeVault(db)

    def _section_for(self, fact: DocumentFact) -> VaultSection | None:
        override = FIELD_OVERRIDES.get(fact.field_key)
        if override is not None:
            return override
        return CATEGORY_TO_SECTION.get((fact.category or "").upper())

    def ingest_company(self, company_id: str) -> IngestReport:
        """Promote every extracted fact for one company into the vault."""
        started = time.perf_counter()
        report = IngestReport()

        rows = self.db.execute(
            select(DocumentFact, Document)
            .join(Document, Document.id == DocumentFact.document_id)
            .where(DocumentFact.company_id == company_id)
        ).all()
        report.facts_seen = len(rows)
        report.documents = len({d.id for _, d in rows})

        # Period order, not row order. A backfill that loads FY2024 after
        # FY2026 must still leave FY2026 current; sorting by id would make the
        # vault depend on ingestion sequence.
        def sort_key(pair: tuple[DocumentFact, Document]) -> tuple:
            fact, document = pair
            year = fact.fiscal_year or document.fiscal_year or 0
            return (year, document.id, fact.id)

        for fact, document in sorted(rows, key=sort_key):
            section = self._section_for(fact)
            if section is None:
                report.unmapped.append(fact.category or "?")
                continue

            value_text = fact.text_value or (
                None if fact.value is None else None
            )
            value_number = fact.value

            result = self.vault.assert_knowledge(
                company_id,
                section,
                fact.field_key,
                label=fact.label or fact.field_key,
                value_text=value_text,
                value_number=value_number,
                unit=fact.unit,
                confidence=float(fact.confidence or 0.0),
                provenance=Provenance(
                    document_id=fact.document_id,
                    page=fact.page,
                    fiscal_year=fact.fiscal_year or document.fiscal_year,
                    quarter=fact.period,
                    doc_type=document.doc_type,
                ),
                evidence=fact.evidence,
                generated_by="document-extractor",
            )
            if result.action == "created":
                report.created += 1
            elif result.action == "versioned":
                report.versioned += 1
            elif result.action == "recorded_stale":
                report.recorded_stale += 1
            else:
                report.rejected += 1

        self.db.commit()
        report.latency_ms = (time.perf_counter() - started) * 1000
        log.info("vault ingest complete", company_id=company_id,
                 **{k: v for k, v in report.as_dict().items()
                    if k != "unmapped_categories"})
        return report

    def ingest_all(self, *, limit: int | None = None) -> dict[str, Any]:
        """Promote facts for every company that has any."""
        started = time.perf_counter()
        company_ids = [
            row[0] for row in self.db.execute(
                select(DocumentFact.company_id).distinct()
            ).all()
        ]
        if limit:
            company_ids = company_ids[:limit]

        totals = IngestReport()
        for company_id in company_ids:
            one = self.ingest_company(company_id)
            totals.documents += one.documents
            totals.facts_seen += one.facts_seen
            totals.created += one.created
            totals.versioned += one.versioned
            totals.recorded_stale += one.recorded_stale
            totals.rejected += one.rejected
            totals.unmapped.extend(one.unmapped)

        totals.latency_ms = (time.perf_counter() - started) * 1000
        payload = totals.as_dict()
        payload["companies"] = len(company_ids)
        return payload
