"""Multilingual prompt templates (Phase 2).

Composes the capability-aware guidance that is appended to the base
`RESPONSE LANGUAGE` instruction the adapter builds. The base instruction
already fixes the register (see `LanguageSpec.style_instruction`) and the
identifier-preservation rule; these templates add what a *capability* implies
about shape — a chat turn and a risk section are different artifacts even in
the same language.

Two contracts matter.

**English returns the instruction byte-identical.** The canonical path must
not change shape or length at all, so for `ENGLISH`, for planned languages
and for an empty instruction this function is the identity.

**The capability is a hint, not a filter.** An unknown or omitted capability
falls back to the instruction unchanged rather than inventing guidance — a
template that mis-describes the task does more harm than none.
"""
from __future__ import annotations

from app.domain.language.types import CANONICAL_LANGUAGE, Language, spec_for

#: Per-capability guidance, in the language-neutral English the writing model
#: reads. Kept short on purpose: this block rides on top of a prompt that
#: already carries the evidence, the style and the citation rules, and every
#: extra sentence is one more the model can trade away for its first answer.
_CAPABILITY_GUIDANCE: dict[str, str] = {
    "chat": (
        "This is a chat conversation, not a report: answer the question "
        "directly and keep it as short as the question allows — a few "
        "sentences for a direct question. If you must caveat, put the caveat "
        "after the answer, never instead of it."
    ),
    "document_qa": (
        "Answer strictly from the supplied document passages; if a passage "
        "does not answer the question, say so in the response language."
    ),
    "business_summary": "Keep the section structure: an opening line, then the sections.",
    "investment_thesis": "State the thesis in the first sentence, then the supporting points.",
    "bull_case": "Present the strongest arguments for the investment, each tied to its evidence.",
    "bear_case": "Present the strongest arguments against, each tied to its evidence.",
    "swot": "Use the four labelled sections — Strengths, Weaknesses, Opportunities, Threats.",
    "moat_analysis": "Identify the source of the moat before assessing its durability.",
    "management_analysis": (
        "Judge management on evidence (track record, capital allocation, "
        "disclosure quality), not on tone."
    ),
    "capital_allocation": "Walk through the allocation decisions in order of size.",
    "risk_analysis": "Enumerate the risks; do not merge distinct risks into one line.",
    "valuation_commentary": (
        "State the valuation view, then the multiples and assumptions it "
        "rests on."
    ),
    "dcf_interpretation": (
        "Explain what the DCF output is and is not; the sensitivity matters "
        "as much as the number."
    ),
    "scoring_explanation": "Explain the score band by band; a score is a verdict with reasons.",
    "peer_comparison": "Compare companies side by side on the same dimensions.",
    "conference_call_summary": "Summarise what management said, not what they were asked.",
    "annual_report_summary": "Lead with the year's headline figures, then the sections.",
    "portfolio_commentary": "Comment on the position's contribution, not the company alone.",
}


def get_multilingual_prompt(
    language: Language | str,
    capability: str = "",
    instruction: str = "",
) -> str:
    """Return `instruction` with capability guidance appended where useful.

    Identity for English, for planned languages, for an empty instruction and
    for capabilities without a template — the caller's instruction is never
    lost, only extended.
    """
    if instruction == "":
        return instruction

    try:
        lang = language if isinstance(language, Language) else Language(str(language).strip().lower())
    except ValueError:
        return instruction

    if lang is CANONICAL_LANGUAGE or not spec_for(lang).is_supported:
        return instruction

    guidance = _CAPABILITY_GUIDANCE.get((capability or "").strip().lower())
    if guidance is None:
        return instruction

    return f"{instruction.rstrip()} {guidance}"
