"""The writing layer for one report section.

Phase 1 replaced the deterministic offline composer with a live model for
*final answer generation only*. Nothing about how evidence is found changes
here: retrieval, section routing, provider selection, confidence scoring and
citation capture all run first and are handed to this module as settled facts.
Its single job is to turn that evidence into prose an analyst would recognise.

Two properties matter more than fluency.

**The model may not invent.** It receives the evidence and nothing else, and
the brief below tells it that any figure absent from the evidence must be
described as not held rather than supplied from memory. That instruction is
necessary but not sufficient, so it is backed by the existing citation audit
downstream — an assertion carrying no evidence key is reported as unsupported
regardless of how confident it reads.

**The model may not overwrite provenance.** Source, provider, confidence and
page references are computed by the orchestrator before the model is called
and are attached to the `SectionResult` afterwards. The model is *told* what
they are, so it can write "the annual report states…" rather than a vague
"reportedly", but it cannot change them: a section's declared confidence is
arithmetic over retrieval scores, not a number the writer chose.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.domain.ai.types import Citation

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.services.ai.orchestration import Provider, SectionRoute

#: Stated verbatim when a section has no admissible evidence.
#:
#: Exact wording is contractual: the brief specifies this sentence, the UI
#: keys an "unevidenced" badge off it, and the orchestration tests assert it,
#: so it lives in one place rather than being retyped at each site.
NO_EVIDENCE = "No verified evidence available for this section."


@dataclass(frozen=True, slots=True)
class SectionBrief:
    """What the writer is told about a section beyond the evidence itself."""

    title: str
    provider: str
    source: str
    confidence: float
    citations: tuple[Citation, ...]
    ticker: str
    company: str

    def render(self) -> str:
        """The provenance block appended to the section's task.

        Requirement 5 of the brief: the model must receive provider metadata,
        confidence scores and citations, not merely the evidence values. A
        writer that knows its evidence came from the annual report at 0.93
        confidence writes differently — and more honestly — than one handed
        the same sentences with no indication of where they came from.
        """
        lines = [
            "PROVENANCE — this section's evidence, already resolved. Report "
            "these facts; do not contradict or recompute them.",
            f"- Section: {self.title}",
            f"- Company: {self.company} ({self.ticker})",
            f"- Provider used: {self.provider}",
            f"- Source used: {self.source}",
            f"- Confidence score: {self.confidence:.2f}",
        ]
        if self.citations:
            lines.append("- Citations available to you:")
            for citation in self.citations[:8]:
                reference = f"  - [{citation.key}] {citation.label}"
                if citation.page is not None:
                    reference += f" (page {citation.page}"
                    if citation.chunk_id is not None:
                        reference += f", chunk {citation.chunk_id}"
                    reference += ")"
                if citation.confidence is not None:
                    reference += f" — retrieval score {citation.confidence:.2f}"
                lines.append(reference)
        else:
            lines.append("- Citations available to you: none.")
        return "\n".join(lines)


#: Appended to every section task. Complements the shared system preamble
#: rather than repeating it: the preamble establishes grounding and citation
#: rules for the whole platform, this adds the constraints specific to writing
#: one section of a multi-section report.
SECTION_CONTRACT = f"""
SECTION WRITING RULES:

- Write the body of this section only. Do not write a heading — the report
  renders headings itself — and do not write a provenance footer, since the
  platform attaches source, provider, confidence and citations from its own
  records rather than from your text.
- Two to four short paragraphs, or a tight bulleted list where the content is
  genuinely enumerable (segments, risks, catalysts). No filler.
- Every figure must carry its evidence key in square brackets. A number
  without a key is a defect.
- Do not restate the evidence block line by line. Interpret it: say what the
  figures mean for the investment case, and mark interpretation as such.
- Never introduce a company, product, executive, date or figure that does not
  appear in the evidence.

WHEN — AND ONLY WHEN — TO DECLINE:

  If the EVIDENCE block above contains at least one item bearing on this
  section, you must write the section. Write it from what is there, however
  partial, and note what is missing in a closing sentence.

  Only if the EVIDENCE block is empty, or holds nothing whatsoever relevant to
  this section, reply with exactly this sentence and nothing else:
  "{NO_EVIDENCE}"

  An UNAVAILABLE list is not a reason to decline. It records what the platform
  does *not* hold, and it appears on almost every section; the evidence that is
  present is still evidence, and declining to use it because something else is
  absent discards work the platform did correctly.
"""


def build_extra(brief: SectionBrief) -> str:
    """The full addendum handed to `analyst.run(extra=…)`."""
    return f"{brief.render()}\n{SECTION_CONTRACT}"


def looks_unevidenced(text: str) -> bool:
    """Did the writer decline for want of evidence?

    Checked so a declining section is scored as unevidenced rather than
    inheriting the confidence its provider would otherwise have carried. The
    comparison is loose because a model asked for an exact sentence will
    occasionally add a full stop or wrap it in emphasis.
    """
    stripped = "".join(ch for ch in text.lower() if ch.isalnum() or ch.isspace())
    return NO_EVIDENCE.lower().rstrip(".") in " ".join(stripped.split())
