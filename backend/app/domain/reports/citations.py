"""Report citation engine.

The brief requires every factual statement to reference its evidence, naming
five engines. This module does three things:

1. **Registers** evidence as the report is built, so the appendix is assembled
   from what was actually used rather than from a hand-maintained list.
2. **Audits** the finished report, finding numeric claims that carry no
   citation. This is the check that matters: prose is easy to cite loosely and
   a report that cites its headline and nothing else has met the letter of the
   requirement and none of its intent.
3. **Annotates** — swaps `[key]` markers for readable references at render time.

The auditor's sentence splitter is decimal-aware. Module 6 shipped a version
that split `₹33,543.00` into two sentences, orphaned the citation attached to
it, and reported 50% coverage on a perfectly cited answer. That lesson is
reused here rather than relearned.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from app.domain.reports.blocks import (
    Block, BlockKind, Callout, Evidence, EvidenceSource, Paragraph,
    ReportDocument, Section, Table,
)

#: Sentence terminator followed by whitespace and an opening character. The
#: lookbehind excludes a full stop that sits between digits, so "33,543.00"
#: and "2.5x" survive intact.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z\u2018\u201c\"'(\[])")

#: A number worth citing: currency amounts, percentages, multiples, ratios.
#: Deliberately excludes bare small integers, which are usually counts in prose
#: ("three pillars") rather than claims about the company.
_NUMERIC_CLAIM = re.compile(
    r"(?<![\w.])"
    r"(?:₹\s?[\d,]+(?:\.\d+)?"
    r"|[\d,]+(?:\.\d+)?\s?(?:%|x\b|bps\b|crore\b|cr\b|lakh\b)"
    r"|[\d,]{4,}(?:\.\d+)?"
    r"|\d+\.\d+)",
    re.IGNORECASE,
)

_MARKER = re.compile(r"\[([a-z0-9_]+)\]")

#: Phrases that mark a sentence as explicitly unquantified, so a number inside
#: one is illustrative rather than a claim.
_HEDGES = (
    "insufficient evidence", "not available", "could not be", "unavailable",
    "no data", "we cannot", "the platform does not",
)


@dataclass(slots=True)
class ClaimCheck:
    """One sentence and whether it is supported."""

    sentence: str
    numbers: list[str]
    markers: list[str]
    supported: bool
    section: str = ""

    @property
    def is_claim(self) -> bool:
        return bool(self.numbers)


@dataclass(slots=True)
class CitationAudit:
    """The result of auditing a report's citations."""

    total_claims: int = 0
    supported_claims: int = 0
    unsupported: list[ClaimCheck] = field(default_factory=list)
    #: Markers used in prose that resolve to no registered evidence.
    dangling_markers: list[str] = field(default_factory=list)
    #: Registered evidence never referenced. Not an error — a table cites via
    #: its block, not a marker — but useful for spotting dead registrations.
    unused_evidence: list[str] = field(default_factory=list)
    sources_used: set[str] = field(default_factory=set)

    @property
    def coverage(self) -> float:
        return (
            self.supported_claims / self.total_claims
            if self.total_claims else 1.0
        )

    @property
    def is_clean(self) -> bool:
        """No fabricated references, and every numeric claim is cited."""
        return not self.dangling_markers and not self.unsupported

    def summary(self) -> dict[str, object]:
        return {
            "total_claims": self.total_claims,
            "supported_claims": self.supported_claims,
            "coverage": round(self.coverage, 4),
            "unsupported": len(self.unsupported),
            "dangling_markers": self.dangling_markers,
            "unused_evidence": len(self.unused_evidence),
            "sources": sorted(self.sources_used),
            "clean": self.is_clean,
        }


class EvidenceRegistry:
    """Collects evidence as a report is built.

    Keys must be unique and stable: a key reused with a different value would
    make two statements cite the same reference and mean different things. The
    registry refuses that rather than letting the last write win.
    """

    def __init__(self) -> None:
        self._items: dict[str, Evidence] = {}

    def add(
        self,
        key: str,
        label: str,
        source: EvidenceSource,
        value: float | str | None = None,
        unit: str = "",
        detail: str = "",
        fiscal_year: int | None = None,
    ) -> Evidence:
        evidence = Evidence(
            key=key, label=label, source=source, value=value, unit=unit,
            detail=detail, fiscal_year=fiscal_year,
        )
        existing = self._items.get(key)
        if existing is not None and existing.value != evidence.value:
            raise ValueError(
                f"evidence key '{key}' already registered with a different "
                f"value ({existing.value!r} vs {evidence.value!r})"
            )
        self._items[key] = evidence
        return evidence

    def get(self, key: str) -> Evidence | None:
        return self._items.get(key)

    def many(self, *keys: str) -> list[Evidence]:
        """Evidence for the given keys, silently skipping ones never registered.

        Skipping rather than raising is right here: a block frequently cites a
        figure that was unavailable for this company, and the audit reports the
        gap as a dangling marker if the prose referenced it.
        """
        return [self._items[k] for k in keys if k in self._items]

    def all(self) -> list[Evidence]:
        return list(self._items.values())

    def __contains__(self, key: str) -> bool:
        return key in self._items

    def __len__(self) -> int:
        return len(self._items)


def split_sentences(text: str) -> list[str]:
    """Decimal-aware sentence split. Defined once; the auditor's only splitter."""
    cleaned = " ".join(text.split())
    if not cleaned:
        return []
    return [s.strip() for s in _SENTENCE_SPLIT.split(cleaned) if s.strip()]


def _citable_text(block: Block) -> str:
    """The prose of a block, if it has any.

    Tables and metric grids are excluded deliberately: their figures are cited
    by the block's own evidence list, and treating every cell as a sentence
    would flood the audit with claims that are already accounted for.
    """
    if isinstance(block, Paragraph):
        return block.text
    if isinstance(block, Callout):
        return f"{block.title}. {block.text}"
    return ""


def audit_report(document: ReportDocument) -> CitationAudit:
    """Check every numeric claim in the report's prose carries a citation.

    A sentence is supported when it contains an evidence marker, or when its
    block carries evidence, or when it is explicitly hedged as unquantified.
    The middle case matters: a paragraph introducing a cited table should not
    have to repeat the markers inline to count as supported.
    """
    registered = {e.key for e in document.evidence()}
    audit = CitationAudit()
    referenced: set[str] = set()

    for section, block in document.iter_blocks():
        for item in block.evidence:
            audit.sources_used.add(item.source.value)

        text = _citable_text(block)
        if not text:
            continue
        block_has_evidence = bool(block.evidence)

        for sentence in split_sentences(text):
            markers = _MARKER.findall(sentence)
            referenced.update(markers)
            for marker in markers:
                if marker not in registered:
                    audit.dangling_markers.append(marker)

            lowered = sentence.lower()
            if any(hedge in lowered for hedge in _HEDGES):
                continue

            # Strip markers before hunting numbers, or a key such as
            # `revenue_fy25` would register "25" as an uncited claim.
            without_markers = _MARKER.sub(" ", sentence)
            numbers = _NUMERIC_CLAIM.findall(without_markers)
            if not numbers:
                continue

            audit.total_claims += 1
            supported = bool(markers) or block_has_evidence
            if supported:
                audit.supported_claims += 1
            else:
                audit.unsupported.append(ClaimCheck(
                    sentence=sentence, numbers=numbers, markers=markers,
                    supported=False, section=section.title,
                ))

    audit.dangling_markers = sorted(set(audit.dangling_markers))
    audit.unused_evidence = sorted(registered - referenced)
    return audit


def annotate(text: str, registry: EvidenceRegistry | Sequence[Evidence]) -> str:
    """Replace `[key]` markers with readable references.

    An unresolvable marker is left visible rather than stripped. Removing it
    would hide a broken reference and make the prose read as though it were
    never cited at all.
    """
    lookup = (
        {e.key: e for e in registry}
        if not isinstance(registry, EvidenceRegistry)
        else {e.key: e for e in registry.all()}
    )

    def replace(match: re.Match) -> str:
        evidence = lookup.get(match.group(1))
        return f"[{evidence.label}]" if evidence else match.group(0)

    return _MARKER.sub(replace, text)


def strip_markers(text: str) -> str:
    """Remove markers entirely — for word counts and plain-text export."""
    return " ".join(_MARKER.sub("", text).split())


def evidence_by_source(
    items: Iterable[Evidence],
) -> dict[EvidenceSource, list[Evidence]]:
    """Group evidence for the appendix, which lists it engine by engine."""
    grouped: dict[EvidenceSource, list[Evidence]] = {}
    for item in items:
        grouped.setdefault(item.source, []).append(item)
    for entries in grouped.values():
        entries.sort(key=lambda e: e.key)
    return grouped
