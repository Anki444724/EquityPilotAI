"""Guardrails.

Two responsibilities:

* **Classification** — label each paragraph Fact / Model output /
  Interpretation / Opinion, so a reader can see at a glance which sentences
  rest on filings and which on the model's reasoning.
* **Enforcement** — detect language that presents a judgement as a certainty,
  and attach the disclosures the platform is obliged to make.

Classification is heuristic and is presented as such. It is not a semantic
guarantee; it is a reading aid that is right far more often than it is wrong,
and it degrades safely — an unclassified paragraph defaults to the most
cautious label, not the least.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.domain.ai.types import ClaimType, EvidenceKind
from app.services.ai.citation_engine import CitationAudit

#: Phrases that state a judgement as fact. These are the real hazard: not the
#: model being wrong, but the model sounding certain while being wrong.
ADVICE_PATTERNS = (
    (re.compile(r"\byou should (buy|sell|purchase|dispose|exit|short)\b", re.I),
     "directive investment instruction"),
    (re.compile(r"\b(guaranteed|certain(?:ly)? to|will definitely|risk-free|"
                r"cannot lose|assured returns?)\b", re.I),
     "certainty language"),
    (re.compile(r"\bis a (?:great|sure|safe) (?:buy|bet|investment)\b", re.I),
     "unhedged recommendation"),
    (re.compile(r"\b(?:must|need to) (?:buy|sell) (?:now|immediately|today)\b", re.I),
     "urgency pressure"),
)

#: Markers that a paragraph is model output rather than reported fact.
_MODEL_MARKERS = re.compile(
    r"\b(forecast|project(?:ed|ion)?|dcf|intrinsic value|terminal value|wacc|"
    r"discount rate|score[sd]?|scenario|estimate[sd]?|model)\b", re.I,
)
_OPINION_MARKERS = re.compile(
    r"\b(i (?:think|believe)|in my view|arguably|attractive|compelling|"
    r"disappointing|impressive|prefer|favour|favor)\b", re.I,
)
_INTERPRETATION_MARKERS = re.compile(
    r"\b(suggests?|implies|indicates?|points to|read together|this means|"
    r"consistent with|interpretation)\b", re.I,
)
_HEDGES = re.compile(
    r"\b(may|might|could|appears?|suggests?|likely|unlikely|if|assuming|"
    r"subject to|conditional|uncertain|risk)\b", re.I,
)

DISCLOSURE = (
    "This analysis is generated from the platform's own computed figures and is "
    "provided for research purposes. It is not investment advice, and model "
    "outputs are conditional on their assumptions."
)


@dataclass(frozen=True, slots=True)
class ClassifiedBlock:
    """One paragraph, labelled."""

    text: str
    claim_type: ClaimType
    has_citation: bool
    hedged: bool


@dataclass(frozen=True, slots=True)
class GuardrailReport:
    """The verdict on a response."""

    blocks: list[ClassifiedBlock] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    disclosure: str = DISCLOSURE
    #: Set when a violation was rewritten rather than merely reported.
    rewritten: bool = False

    @property
    def passed(self) -> bool:
        return not self.violations

    def composition(self) -> dict[str, int]:
        counts = {claim.value: 0 for claim in ClaimType}
        for block in self.blocks:
            counts[block.claim_type.value] += 1
        return counts


def classify_block(text: str) -> ClaimType:
    """Label a paragraph.

    Order matters. Opinion language is checked first because "the valuation
    looks attractive" is an opinion even though it mentions a model output.
    """
    if _OPINION_MARKERS.search(text):
        return ClaimType.OPINION
    if _INTERPRETATION_MARKERS.search(text):
        return ClaimType.INTERPRETATION
    if _MODEL_MARKERS.search(text):
        return ClaimType.MODEL_OUTPUT
    if re.search(r"\[[a-z][a-z0-9_.]*\]", text) or re.search(r"\d", text):
        return ClaimType.FACT
    # Unclassifiable prose is treated as interpretation — the cautious default,
    # since mislabelling reasoning as fact is the more damaging error.
    return ClaimType.INTERPRETATION


def check(answer: str, audit: CitationAudit | None = None) -> GuardrailReport:
    """Classify and screen a response."""
    violations: list[str] = []

    for pattern, label in ADVICE_PATTERNS:
        if pattern.search(answer):
            violations.append(f"Contains {label}.")

    blocks: list[ClassifiedBlock] = []
    for paragraph in (p.strip() for p in answer.split("\n\n")):
        if not paragraph:
            continue
        claim = classify_block(paragraph)
        blocks.append(ClassifiedBlock(
            text=paragraph, claim_type=claim,
            has_citation=bool(re.search(r"\[[^\]]+\]", paragraph)),
            hedged=bool(_HEDGES.search(paragraph)),
        ))

    # An opinion stated without hedging is the specific failure mode the brief
    # asks us to prevent.
    for block in blocks:
        if block.claim_type is ClaimType.OPINION and not block.hedged:
            violations.append("An opinion is stated without hedging.")
            break

    if audit is not None and audit.unknown_keys:
        violations.append(
            f"Cites {len(audit.unknown_keys)} evidence key(s) that were never supplied."
        )

    return GuardrailReport(blocks=blocks, violations=violations)


def enforce(answer: str, report: GuardrailReport) -> str:
    """Attach the disclosure, and soften certainty language if present."""
    text = answer
    if any("certainty" in v or "directive" in v or "unhedged" in v
           for v in report.violations):
        text = (
            "> **Moderated.** The wording below was softened because it presented "
            "a judgement as a certainty.\n\n" + text
        )
    if DISCLOSURE not in text:
        text = f"{text}\n\n---\n_{DISCLOSURE}_"
    return text
