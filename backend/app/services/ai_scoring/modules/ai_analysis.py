"""Module 6 — AI Analysis Engine (weight 10).

Business moat, competitive advantage, risks, opportunities, ESG, AI reasoning.

This is the module most likely to be misread, so its rule is stated plainly:
**the AI does not score anything here.** What is scored is the *presence,
provenance and depth of grounded analytical evidence* the platform has
accumulated — vault assertions with citations, non-fallback summaries, and
temporal observations. Those artefacts were produced by a model reading a
filing and are traceable to a page; they are evidence in the same sense a
broker note is evidence.

What is deliberately not scored is the model's *opinion*. No factor here reads
sentiment, and no factor calls an LLM. A prompt asking "how strong is this
company's moat, 0-10?" would be exactly the black box the brief forbids, and it
would also be unstable: the same question asked twice returns two numbers.

The "AI reasoning" factor is therefore a measure of analytical coverage — how
much of the company the platform has actually reasoned about, and how much of
that reasoning is genuine model output rather than template fallback. A
company with 40 grounded assertions across eight vault sections is better
understood than one with three, and that is a fact about the evidence base, not
a judgement about the company.
"""
from __future__ import annotations

from app.domain.ai_scoring.framework import Module
from app.domain.ai_scoring.types import FactorScore, Origin, band, scale
from app.domain.knowledge.vault import SummaryKind, VaultSection
from app.services.ai_scoring.evidence import ScoringEvidence
from app.services.ai_scoring.modules.common import build_module

KEY = Module.AI_ANALYSIS
SERVICE = "ai_scoring.ai_analysis"

#: Vault sections that constitute analytical (rather than descriptive) work.
_ANALYTICAL_SECTIONS = (
    VaultSection.RISKS.value,
    VaultSection.OPPORTUNITIES.value,
    VaultSection.COMPETITORS.value,
    VaultSection.CAPITAL_ALLOCATION.value,
    VaultSection.ESG.value,
    VaultSection.VALUATION.value,
    VaultSection.HISTORICAL_AI_ANALYSIS.value,
    VaultSection.AI_NOTES.value,
)

#: Keywords that mark a moat or advantage assertion, wherever it sits.
_MOAT_KEYWORDS = ("moat", "barrier to entry", "switching cost", "network effect",
                  "brand", "patent", "proprietary", "exclusive", "licence",
                  "license", "distribution network", "scale advantage")
_ADVANTAGE_KEYWORDS = ("competitive advantage", "market leader", "leadership",
                       "largest", "differentiat", "cost advantage",
                       "first mover", "installed base", "market share")


def _missing(key: str, label: str, weight: float, what: str) -> FactorScore:
    return FactorScore(
        key=key, label=label, weight=weight, score=5.0, origin=Origin.MISSING,
        reason=f"Not assessed: {what}", computed_by=SERVICE,
    )


def _keyword_entries(evidence: ScoringEvidence, keywords: tuple[str, ...]):
    lowered = tuple(k.lower() for k in keywords)
    out = []
    for entry in evidence.vault_entries:
        haystack = " ".join(filter(None, (
            entry.key, entry.label, entry.value_text, entry.evidence
        ))).lower()
        if any(k in haystack for k in lowered):
            out.append(entry)
    return out


def _cited(entries) -> list:
    """Entries carrying a resolvable document reference.

    An assertion with no document behind it may still be true, but it cannot
    be verified, and this module's whole claim is that its evidence is
    traceable. Uncited assertions are counted separately and stated.
    """
    return [e for e in entries if e.document_id is not None]


def score(evidence: ScoringEvidence):
    factors: list[FactorScore] = []

    # --- business moat ----------------------------------------------------
    moat_entries = _keyword_entries(evidence, _MOAT_KEYWORDS)
    if moat_entries:
        cited = _cited(moat_entries)
        factors.append(FactorScore(
            key="business_moat", label="Business Moat", weight=0.20,
            score=band(float(len(cited) or len(moat_entries) * 0.5),
                       [(5, 9.5), (3, 8.5), (2, 7.5), (1, 6.5)]),
            origin=Origin.EXTRACTED, value=float(len(moat_entries)),
            unit="assertions",
            reason=(
                f"{len(moat_entries)} extracted assertions describe barriers "
                f"to entry, switching costs, brand or proprietary position, "
                f"of which {len(cited)} cite a specific document. Scored on "
                "the weight of traceable evidence, not on a model's opinion "
                "of how wide the moat is."
            ),
            evidence="; ".join(
                (e.value_text or e.label)[:120] for e in moat_entries[:3]
            ),
            citations=tuple(ScoringEvidence.vault_citation(e)
                            for e in (cited or moat_entries)[:4]),
            computed_by=SERVICE,
        ))
    else:
        factors.append(_missing(
            "business_moat", "Business Moat", 0.20,
            "no moat-related assertion has been extracted from this "
            "company's filings.",
        ))

    # --- competitive advantage ---------------------------------------------
    advantage_entries = _keyword_entries(evidence, _ADVANTAGE_KEYWORDS)
    if advantage_entries:
        cited = _cited(advantage_entries)
        factors.append(FactorScore(
            key="competitive_advantage", label="Competitive Advantage",
            weight=0.16,
            score=band(float(len(cited) or len(advantage_entries) * 0.5),
                       [(4, 9.0), (3, 8.0), (2, 7.0), (1, 6.0)]),
            origin=Origin.EXTRACTED, value=float(len(advantage_entries)),
            unit="assertions",
            reason=(
                f"{len(advantage_entries)} assertions describe market "
                f"position, differentiation or cost advantage "
                f"({len(cited)} document-cited)."
            ),
            evidence="; ".join(
                (e.value_text or e.label)[:120] for e in advantage_entries[:3]
            ),
            citations=tuple(ScoringEvidence.vault_citation(e)
                            for e in (cited or advantage_entries)[:4]),
            computed_by=SERVICE,
        ))
    else:
        factors.append(_missing(
            "competitive_advantage", "Competitive Advantage", 0.16,
            "no competitive-position assertion has been extracted.",
        ))

    # --- risks -------------------------------------------------------------
    risk_entries = evidence.vault(VaultSection.RISKS.value)
    risk_summaries = evidence.summaries_of(SummaryKind.RISK.value)
    if risk_entries or risk_summaries:
        count = len(risk_entries) + len(risk_summaries)
        citations = tuple(ScoringEvidence.vault_citation(e)
                          for e in risk_entries[:3])
        citations += tuple(ScoringEvidence.summary_citation(s)
                           for s in risk_summaries[:2])
        factors.append(FactorScore(
            key="risks", label="Risks", weight=0.18,
            score=band(float(count), [(6, 9.5), (4, 8.5), (2, 7.5), (1, 6.5)]),
            origin=Origin.EXTRACTED, value=float(count), unit="sources",
            reason=(
                f"{len(risk_entries)} vault risk assertions and "
                f"{len(risk_summaries)} model-written risk summaries are "
                "held. Scored on how thoroughly the risks have been "
                "identified and cited — a well-documented risk register is a "
                "strength of the analysis, not a weakness of the company. "
                "Whether those risks are severe is measured in the Risk "
                "module, which reads the balance sheet."
            ),
            evidence="; ".join(
                (e.value_text or e.label)[:120] for e in risk_entries[:3]
            ) or (risk_summaries[0].content[:200] if risk_summaries else ""),
            citations=citations,
            computed_by=SERVICE,
        ))
    else:
        factors.append(_missing(
            "risks", "Risks", 0.18,
            "no risk assertions or risk summaries have been produced for "
            "this company.",
        ))

    # --- opportunities -----------------------------------------------------
    opportunity_entries = evidence.vault(VaultSection.OPPORTUNITIES.value)
    bull_summaries = evidence.summaries_of(SummaryKind.BULL_THESIS.value)
    if opportunity_entries or bull_summaries:
        count = len(opportunity_entries) + len(bull_summaries)
        citations = tuple(ScoringEvidence.vault_citation(e)
                          for e in opportunity_entries[:3])
        citations += tuple(ScoringEvidence.summary_citation(s)
                           for s in bull_summaries[:2])
        factors.append(FactorScore(
            key="opportunities", label="Opportunities", weight=0.16,
            score=band(float(count), [(5, 9.0), (3, 8.0), (2, 7.0), (1, 6.0)]),
            origin=Origin.EXTRACTED, value=float(count), unit="sources",
            reason=(
                f"{len(opportunity_entries)} opportunity assertions and "
                f"{len(bull_summaries)} evidence-based bull cases have been "
                "written from this company's own filings."
            ),
            evidence="; ".join(
                (e.value_text or e.label)[:120] for e in opportunity_entries[:3]
            ) or (bull_summaries[0].content[:200] if bull_summaries else ""),
            citations=citations,
            computed_by=SERVICE,
        ))
    else:
        factors.append(_missing(
            "opportunities", "Opportunities", 0.16,
            "no opportunity assertions or bull-case summaries have been "
            "produced.",
        ))

    # --- ESG ----------------------------------------------------------------
    esg_entries = evidence.vault(VaultSection.ESG.value)
    if esg_entries:
        cited = _cited(esg_entries)
        factors.append(FactorScore(
            key="esg", label="ESG", weight=0.14,
            score=band(float(len(esg_entries)),
                       [(6, 9.0), (4, 8.0), (2, 7.0), (1, 6.0)]),
            origin=Origin.EXTRACTED, value=float(len(esg_entries)),
            unit="assertions",
            reason=(
                f"{len(esg_entries)} ESG assertions extracted from filings "
                f"({len(cited)} document-cited). Indian issuers file BRSR "
                "disclosures, so ESG is scored from what the company reported "
                "rather than from a third-party rating the platform cannot "
                "audit."
            ),
            evidence="; ".join(
                (e.value_text or e.label)[:120] for e in esg_entries[:3]
            ),
            citations=tuple(ScoringEvidence.vault_citation(e)
                            for e in (cited or esg_entries)[:4]),
            computed_by=SERVICE,
        ))
    else:
        factors.append(_missing(
            "esg", "ESG", 0.14,
            "no ESG or BRSR disclosure has been extracted for this company.",
        ))

    # --- AI reasoning -------------------------------------------------------
    # Analytical coverage: how much of the company the platform has actually
    # reasoned about, and how much of that is genuine model output.
    analytical = [e for e in evidence.vault_entries
                  if e.section in _ANALYTICAL_SECTIONS]
    sections_covered = {e.section for e in analytical}
    genuine_summaries = [s for s in evidence.summaries if not s.is_fallback]
    fallback_summaries = [s for s in evidence.summaries if s.is_fallback]
    genuine_observations = [o for o in evidence.observations if not o.is_fallback]

    if analytical or genuine_summaries or genuine_observations:
        # Three components, each on 0-10, blended: breadth of sections,
        # depth of assertions, and the share of output that is real analysis
        # rather than template fallback.
        breadth = band(float(len(sections_covered)),
                       [(6, 10), (4, 8.5), (3, 7.5), (2, 6.5), (1, 5.5)])
        depth = band(float(len(analytical)),
                     [(20, 10), (12, 8.5), (6, 7.5), (3, 6.5), (1, 5.5)])
        total_summaries = len(genuine_summaries) + len(fallback_summaries)
        authenticity = (
            scale(len(genuine_summaries) / total_summaries, 0.0, 1.0)
            if total_summaries else 5.0
        )
        reasoning = (breadth * 0.35 + depth * 0.40 + authenticity * 0.25)

        citations = tuple(ScoringEvidence.vault_citation(e)
                          for e in analytical[:3])
        citations += tuple(ScoringEvidence.observation_citation(o)
                           for o in genuine_observations[-2:])

        factors.append(FactorScore(
            key="ai_reasoning", label="AI reasoning", weight=0.16,
            score=min(10.0, reasoning), origin=Origin.EXTRACTED,
            value=float(len(analytical)), unit="analytical assertions",
            reason=(
                f"The platform holds {len(analytical)} analytical assertions "
                f"across {len(sections_covered)} vault sections, "
                f"{len(genuine_summaries)} model-written summaries and "
                f"{len(genuine_observations)} yearly observations. "
                f"{len(fallback_summaries)} summaries are template fallbacks "
                "and are excluded — prose the platform wrote to itself when "
                "no model was reachable is not analysis. This factor measures "
                "how much of the company has been reasoned about, not what "
                "the reasoning concluded."
            ),
            evidence=(
                f"Sections covered: {', '.join(sorted(sections_covered)) or 'none'}."
            ),
            citations=citations,
            computed_by=SERVICE,
        ))
    else:
        factors.append(_missing(
            "ai_reasoning", "AI reasoning", 0.16,
            "no analytical assertions, non-fallback summaries or yearly "
            "observations exist for this company — the platform has not yet "
            "reasoned about it.",
        ))

    return build_module(KEY, factors)
