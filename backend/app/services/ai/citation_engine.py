"""Citation engine.

Verifies, after the fact, that the model actually did what it was told. Asking
politely in a system prompt is necessary but not sufficient — models drift, and
an uncited number in a research report is indistinguishable from a fabricated
one.

Three checks run on every response:

1. **Resolution** — every `[key]` the model emitted must exist in the evidence
   that was supplied. An unknown key means the model invented a source.
2. **Coverage** — numeric claims should carry a citation. A paragraph full of
   figures and no keys is flagged.
3. **Fabrication** — numbers appearing in the answer that appear nowhere in the
   evidence are surfaced for review.

The result is advisory metadata attached to the response, not a hard block:
suppressing an answer entirely would hide the problem, whereas showing the
reader "3 unsupported figures" is actionable.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.domain.ai.types import Citation

#: Inline markers the prompt instructs the model to emit.
_MARKER = re.compile(r"\[([a-z][a-z0-9_.]{1,60})\]")
#: Numbers with at least two significant digits — single digits are usually
#: list numbering or ordinals rather than financial claims.
_NUMBER = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d{2,}(?:\.\d+)?|\d+\.\d+)")
#: Sentence terminator: .!? followed by whitespace/end, but NOT a decimal
#: point inside a number. Splitting naively on "." breaks "33,543.00" in two
#: and orphans the citation that follows, producing false coverage failures.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])(?=\s|$)(?<!\d\.\d)")


def _sentences(text: str) -> list[str]:
    """Split into sentences without breaking on decimal points."""
    parts: list[str] = []
    for line in text.split("\n"):
        buffer = ""
        for chunk in _SENTENCE_SPLIT.split(line):
            buffer += chunk
            # A real terminator is .!? not preceded by a digit-dot-digit run.
            if re.search(r"[.!?]\s*$", buffer) and not re.search(r"\d\.\d*\s*$", buffer):
                parts.append(buffer.strip())
                buffer = ""
        if buffer.strip():
            parts.append(buffer.strip())
    return [p for p in parts if p]


@dataclass(frozen=True, slots=True)
class CitationAudit:
    """The verdict on one response."""

    resolved: list[Citation] = field(default_factory=list)
    unknown_keys: list[str] = field(default_factory=list)
    uncited_numbers: list[str] = field(default_factory=list)
    numeric_sentences: int = 0
    cited_sentences: int = 0

    @property
    def coverage(self) -> float:
        """Share of numeric sentences that carry a citation."""
        if self.numeric_sentences == 0:
            return 1.0
        return self.cited_sentences / self.numeric_sentences

    @property
    def is_supported(self) -> bool:
        """A response is supported when it cites real keys and covers its claims."""
        return not self.unknown_keys and self.coverage >= 0.6

    @property
    def summary(self) -> str:
        if self.unknown_keys:
            return (
                f"{len(self.unknown_keys)} citation(s) reference evidence that was "
                "not supplied."
            )
        if self.coverage < 0.6:
            return (
                f"Only {self.coverage:.0%} of numeric statements carry a citation."
            )
        return f"{len(self.resolved)} citations verified against platform data."


def _numbers_in(text: str) -> set[str]:
    return {m.replace(",", "") for m in _NUMBER.findall(text)}


def audit(answer: str, available: list[Citation]) -> CitationAudit:
    """Verify a model response against the evidence it was given."""
    index = {c.key: c for c in available}
    used_keys = _MARKER.findall(answer)

    resolved: list[Citation] = []
    unknown: list[str] = []
    seen: set[str] = set()
    for key in used_keys:
        if key in index:
            if key not in seen:
                resolved.append(index[key])
                seen.add(key)
        elif key not in unknown:
            unknown.append(key)

    # Sentence-level coverage.
    numeric_sentences = cited_sentences = 0
    for sentence in _sentences(answer):
        if not _NUMBER.search(sentence):
            continue
        numeric_sentences += 1
        if _MARKER.search(sentence):
            cited_sentences += 1

    # Numbers with no counterpart anywhere in the evidence.
    evidence_numbers: set[str] = set()
    for citation in available:
        evidence_numbers |= _numbers_in(citation.render())

    uncited: list[str] = []
    for number in sorted(_numbers_in(answer)):
        if number in evidence_numbers:
            continue
        # Percentages and rounded restatements are legitimate paraphrase, so
        # only flag a figure that matches nothing at any sensible rounding.
        try:
            value = float(number)
        except ValueError:
            continue
        if any(
            abs(value - float(candidate)) < max(0.51, abs(value) * 0.02)
            for candidate in evidence_numbers
            if _is_float(candidate)
        ):
            continue
        uncited.append(number)

    return CitationAudit(
        resolved=resolved, unknown_keys=unknown,
        uncited_numbers=uncited[:10],
        numeric_sentences=numeric_sentences, cited_sentences=cited_sentences,
    )


def _is_float(text: str) -> bool:
    try:
        float(text)
        return True
    except ValueError:
        return False


def annotate(answer: str, available: list[Citation]) -> str:
    """Replace bare `[key]` markers with a readable reference.

    The model writes `[revenue]`; the reader sees `[Revenue]`. Unknown keys are
    left visible rather than silently removed — hiding them would conceal
    exactly the failure the audit exists to detect.
    """
    index = {c.key: c for c in available}

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        citation = index.get(key)
        return f"[{citation.label}]" if citation else match.group(0)

    return _MARKER.sub(replace, answer)
