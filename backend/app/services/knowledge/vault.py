"""The Company Knowledge Vault service.

Writes assertions into permanent, versioned storage and reads current
knowledge back out. This is the layer that turns a pile of extracted document
facts into institutional memory.

The one operation that matters is `assert_knowledge`. It never updates a row.
A new assertion about an existing key inserts version N+1 and marks the
previous entry `superseded`; if the new assertion is *older or weaker* than
what the vault already holds, it is still recorded — as a superseded entry
from the outset — because "we saw this claim and rejected it as stale" is
itself knowledge worth keeping, and discarding it would make the vault's
reasoning unauditable.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import structlog
from sqlalchemy import desc, func, select

from app.domain.knowledge.vault import (
    EntryStatus, MIN_CURRENT_CONFIDENCE, Provenance, VaultSection,
    authority_of, is_servable, supersedes,
)
from app.models.knowledge import KnowledgeEntry

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class AssertResult:
    entry: KnowledgeEntry | None
    action: str            # created | versioned | recorded_stale | rejected
    superseded_id: int | None = None
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry.id if self.entry else None,
            "action": self.action,
            "version": self.entry.version if self.entry else None,
            "status": self.entry.status if self.entry else None,
            "superseded_id": self.superseded_id,
            "detail": self.detail,
        }


@dataclass(slots=True)
class VaultStats:
    sections: dict[str, int] = field(default_factory=dict)
    current: int = 0
    superseded: int = 0
    total: int = 0
    citable: int = 0
    mean_confidence: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_entries": self.total,
            "current": self.current,
            "superseded": self.superseded,
            "citable": self.citable,
            "citable_percent": (
                round(self.citable / self.current * 100, 1) if self.current else 0.0
            ),
            "mean_confidence": round(self.mean_confidence, 3),
            "sections": self.sections,
        }


class KnowledgeVault:
    """Permanent, versioned knowledge for one platform's companies."""

    def __init__(self, db: Any) -> None:
        self.db = db

    # ------------------------------------------------------------- writing
    def current_entry(
        self, company_id: str, section: VaultSection, key: str,
    ) -> KnowledgeEntry | None:
        return self.db.scalar(
            select(KnowledgeEntry).where(
                KnowledgeEntry.company_id == company_id,
                KnowledgeEntry.section == section.value,
                KnowledgeEntry.key == key,
                KnowledgeEntry.status == EntryStatus.CURRENT.value,
            ).order_by(desc(KnowledgeEntry.version)).limit(1)
        )

    def _next_version(
        self, company_id: str, section: VaultSection, key: str,
    ) -> int:
        highest = self.db.scalar(
            select(func.max(KnowledgeEntry.version)).where(
                KnowledgeEntry.company_id == company_id,
                KnowledgeEntry.section == section.value,
                KnowledgeEntry.key == key,
            )
        )
        return int(highest or 0) + 1

    def assert_knowledge(
        self,
        company_id: str,
        section: VaultSection,
        key: str,
        *,
        label: str = "",
        value_text: str | None = None,
        value_number: float | None = None,
        unit: str | None = None,
        confidence: float = 0.5,
        provenance: Provenance | None = None,
        evidence: str | None = None,
        generated_by: str | None = None,
    ) -> AssertResult:
        """Record an assertion. Never overwrites; always versions."""
        provenance = provenance or Provenance()
        authority = authority_of(provenance.doc_type)

        if value_text is None and value_number is None:
            # An assertion with no content is not knowledge. Refused rather
            # than stored, because an empty entry would still supersede a good
            # one and quietly blank the vault.
            return AssertResult(None, "rejected", detail="no value supplied")

        existing = self.current_entry(company_id, section, key)

        # Identical restatement — the same claim in a later filing. Recorded
        # as a fresh version rather than skipped, because "the FY2026 report
        # repeats this" is corroboration and raises the reader's confidence in
        # a way a silent no-op would hide.
        wins = True
        if existing is not None:
            wins = supersedes(
                new_fiscal_year=provenance.fiscal_year,
                new_authority=authority,
                new_confidence=confidence,
                old_fiscal_year=existing.fiscal_year,
                old_authority=existing.authority or 0.4,
                old_confidence=existing.confidence or 0.0,
            )

        version = self._next_version(company_id, section, key)
        status = (
            EntryStatus.CURRENT if wins and confidence >= MIN_CURRENT_CONFIDENCE
            else EntryStatus.SUPERSEDED
        )

        entry = KnowledgeEntry(
            company_id=company_id,
            section=section.value,
            key=key,
            label=label or key.replace("_", " ").title(),
            value_text=value_text,
            value_number=value_number,
            unit=unit,
            confidence=round(float(confidence), 4),
            authority=authority,
            document_id=provenance.document_id,
            page=provenance.page,
            paragraph=provenance.paragraph,
            fiscal_year=provenance.fiscal_year,
            quarter=provenance.quarter,
            doc_type=provenance.doc_type,
            evidence=(evidence or "")[:4000] or None,
            version=version,
            status=status.value,
            generated_by=generated_by,
        )
        self.db.add(entry)
        self.db.flush()

        if existing is not None and wins:
            # The old entry is retained in full — only its status changes and
            # a pointer is set. Its evidence, page and confidence survive.
            existing.status = EntryStatus.SUPERSEDED.value
            existing.superseded_by = entry.id
            self.db.flush()
            return AssertResult(entry, "versioned", superseded_id=existing.id)

        if not wins:
            return AssertResult(
                entry, "recorded_stale",
                detail=(
                    "kept as history: an existing entry is from a later period "
                    "or a more authoritative source"
                ),
            )
        return AssertResult(entry, "created")

    # ------------------------------------------------------------- reading
    def read_section(
        self, company_id: str, section: VaultSection, *, limit: int = 50,
    ) -> list[KnowledgeEntry]:
        """Current, servable knowledge for one section."""
        rows = self.db.execute(
            select(KnowledgeEntry).where(
                KnowledgeEntry.company_id == company_id,
                KnowledgeEntry.section == section.value,
                KnowledgeEntry.status == EntryStatus.CURRENT.value,
            ).order_by(
                desc(KnowledgeEntry.confidence), desc(KnowledgeEntry.fiscal_year)
            ).limit(limit)
        ).scalars().all()
        return [r for r in rows if is_servable(r.confidence, r.status)]

    def read_vault(
        self, company_id: str, *, per_section: int = 20,
    ) -> dict[str, list[dict[str, Any]]]:
        """The whole current vault, section by section."""
        out: dict[str, list[dict[str, Any]]] = {}
        for section in VaultSection:
            entries = self.read_section(company_id, section, limit=per_section)
            if entries:
                out[section.value] = [self.render(e) for e in entries]
        return out

    def history(
        self, company_id: str, section: VaultSection, key: str,
    ) -> list[dict[str, Any]]:
        """Every version of one assertion, newest first.

        The question the vault exists to answer: what did we believe, when,
        and on what evidence?
        """
        rows = self.db.execute(
            select(KnowledgeEntry).where(
                KnowledgeEntry.company_id == company_id,
                KnowledgeEntry.section == section.value,
                KnowledgeEntry.key == key,
            ).order_by(desc(KnowledgeEntry.version))
        ).scalars().all()
        return [self.render(r) for r in rows]

    @staticmethod
    def render(entry: KnowledgeEntry) -> dict[str, Any]:
        return {
            "id": entry.id,
            "section": entry.section,
            "key": entry.key,
            "label": entry.label,
            "value": entry.value_text if entry.value_text is not None
            else entry.value_number,
            "unit": entry.unit,
            "confidence": entry.confidence,
            "authority": entry.authority,
            "version": entry.version,
            "status": entry.status,
            "citation": {
                "document_id": entry.document_id,
                "page": entry.page,
                "paragraph": entry.paragraph,
                "fiscal_year": entry.fiscal_year,
                "quarter": entry.quarter,
                "doc_type": entry.doc_type,
            },
            "evidence": entry.evidence,
            "generated_by": entry.generated_by,
            "recorded_at": (
                entry.created_at.isoformat() if entry.created_at else None
            ),
        }

    # --------------------------------------------------------------- stats
    def stats(self, company_id: str | None = None) -> VaultStats:
        query = select(KnowledgeEntry)
        if company_id:
            query = query.where(KnowledgeEntry.company_id == company_id)
        rows = self.db.execute(query).scalars().all()

        stats = VaultStats(total=len(rows))
        confidences: list[float] = []
        for row in rows:
            if row.status == EntryStatus.CURRENT.value:
                stats.current += 1
                confidences.append(row.confidence or 0.0)
                stats.sections[row.section] = stats.sections.get(row.section, 0) + 1
                if row.document_id is not None and row.page is not None:
                    stats.citable += 1
            elif row.status == EntryStatus.SUPERSEDED.value:
                stats.superseded += 1
        if confidences:
            stats.mean_confidence = sum(confidences) / len(confidences)
        return stats
