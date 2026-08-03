"""Module 5 — Management Commentary (weight 10).

Sources: conference calls, annual reports, investor presentations.
Tracked: guidance, capital allocation, credibility, execution.

This module is the platform's temporal memory turned into a score. Credibility
is not a judgement about tone — it is the recorded outcome of testing each
year's guidance against the following year's results, which the temporal memory
engine has already computed and stored. Reusing that verdict rather than
re-deriving it here matters: the verdict was reached with the evidence
available at the time, and recomputing it now with hindsight would quietly
rewrite the track record it is meant to measure.

**The three source factors score availability, not sentiment.** Whether a
company holds earnings calls and publishes presentations is a fact about its
disclosure practice, and a company that does is genuinely more assessable. What
management *says* is scored through guidance and credibility, which test the
statements against outcomes.

**Fallback summaries are excluded throughout.** A template-composed summary
written when no model was reachable is not management commentary, and crediting
it would let the score rise because the platform wrote prose to itself.
"""
from __future__ import annotations

from app.domain.ai_scoring.framework import Module
from app.domain.ai_scoring.types import FactorScore, Origin, band, scale
from app.domain.knowledge.vault import SummaryKind, VaultSection
from app.services.ai_scoring.evidence import ScoringEvidence
from app.services.ai_scoring.modules.common import build_module

KEY = Module.MANAGEMENT_COMMENTARY
SERVICE = "ai_scoring.management_commentary"

#: Document types that count as each commentary source.
_CALL_TYPES = frozenset({"earnings_call", "concall", "transcript"})
_REPORT_TYPES = frozenset({"annual_report"})
_PRESENTATION_TYPES = frozenset({"investor_presentation", "presentation"})


def _missing(key: str, label: str, weight: float, what: str) -> FactorScore:
    return FactorScore(
        key=key, label=label, weight=weight, score=5.0, origin=Origin.MISSING,
        reason=f"Not assessed: {what}", computed_by=SERVICE,
    )


def _documents_of(evidence: ScoringEvidence, types: frozenset[str]):
    return [d for d in evidence.documents
            if (d.doc_type or "").lower() in types]


def _source_factor(
    evidence: ScoringEvidence, key: str, label: str, weight: float,
    types: frozenset[str], what: str, bands: list[tuple[float, float]],
) -> FactorScore:
    documents = _documents_of(evidence, types)
    if not documents:
        return _missing(
            key, label, weight,
            f"the platform holds no {what} for this company.",
        )
    years = sorted({d.fiscal_year for d in documents if d.fiscal_year})
    span = (f"covering FY{years[0]}–FY{years[-1]}" if len(years) > 1
            else f"for FY{years[0]}" if years else "with no fiscal year recorded")
    return FactorScore(
        key=key, label=label, weight=weight,
        score=band(float(len(documents)), bands),
        origin=Origin.REPORTED, value=float(len(documents)), unit="documents",
        reason=(
            f"{len(documents)} {what} held {span}. Scored on availability: a "
            "company that discloses through this channel is materially more "
            "assessable than one that does not."
        ),
        evidence="; ".join((d.title or d.filename)[:110] for d in documents[:3]),
        citations=tuple(ScoringEvidence.document_citation(d)
                        for d in documents[:4]),
        computed_by=SERVICE,
    )


def score(evidence: ScoringEvidence):
    factors: list[FactorScore] = []

    # --- conference calls -------------------------------------------------
    factors.append(_source_factor(
        evidence, "conference_calls", "Conference Calls", 0.13,
        _CALL_TYPES, "earnings call transcripts",
        [(8, 10), (4, 8.5), (2, 7.0), (1, 6.0)],
    ))

    # --- annual reports ---------------------------------------------------
    factors.append(_source_factor(
        evidence, "annual_reports", "Annual Reports", 0.15,
        _REPORT_TYPES, "annual reports",
        [(5, 10), (3, 8.5), (2, 7.5), (1, 6.5)],
    ))

    # --- investor presentations -------------------------------------------
    factors.append(_source_factor(
        evidence, "investor_presentations", "Investor Presentations", 0.12,
        _PRESENTATION_TYPES, "investor presentations",
        [(6, 10), (3, 8.5), (2, 7.5), (1, 6.5)],
    ))

    # --- guidance ----------------------------------------------------------
    with_guidance = [o for o in evidence.observations
                     if (o.guidance or "").strip() and not o.is_fallback]
    if with_guidance:
        latest = with_guidance[-1]
        factors.append(FactorScore(
            key="guidance", label="Guidance", weight=0.20,
            score=band(float(len(with_guidance)),
                       [(5, 9.5), (3, 8.5), (2, 7.5), (1, 6.5)]),
            origin=Origin.EXTRACTED, value=float(len(with_guidance)),
            unit="years with guidance",
            reason=(
                f"{len(with_guidance)} fiscal years carry forward-looking "
                f"statements specific enough to record, most recently "
                f"FY{latest.fiscal_year}. Guidance that can be written down "
                "is guidance that can be tested next year; vague optimism "
                "cannot be, and is not counted."
            ),
            evidence=(latest.guidance or "")[:240],
            citations=tuple(ScoringEvidence.observation_citation(o)
                            for o in with_guidance[-3:]),
            computed_by=SERVICE,
        ))
    else:
        factors.append(_missing(
            "guidance", "Guidance", 0.20,
            "no fiscal year carries forward-looking statements specific "
            "enough to record and test.",
        ))

    # --- capital allocation commentary -------------------------------------
    allocation_entries = evidence.vault(VaultSection.CAPITAL_ALLOCATION.value)
    allocation_summaries = evidence.summaries_of(
        SummaryKind.CAPITAL_ALLOCATION.value
    )
    if allocation_entries or allocation_summaries:
        count = len(allocation_entries) + len(allocation_summaries)
        citations = tuple(ScoringEvidence.vault_citation(e)
                          for e in allocation_entries[:2])
        citations += tuple(ScoringEvidence.summary_citation(s)
                           for s in allocation_summaries[:2])
        factors.append(FactorScore(
            key="capital_allocation", label="Capital Allocation", weight=0.16,
            score=band(float(count), [(5, 9.0), (3, 8.0), (2, 7.0), (1, 6.0)]),
            origin=Origin.EXTRACTED, value=float(count), unit="sources",
            reason=(
                f"{len(allocation_entries)} vault assertions and "
                f"{len(allocation_summaries)} document summaries record how "
                "management describes its capital allocation. This scores the "
                "stated policy; whether the cash actually went there is "
                "measured in the Financial Statements module."
            ),
            evidence="; ".join(
                (e.value_text or e.label)[:120] for e in allocation_entries[:2]
            ) or (allocation_summaries[0].content[:200]
                  if allocation_summaries else ""),
            citations=citations,
            computed_by=SERVICE,
        ))
    else:
        factors.append(_missing(
            "capital_allocation", "Capital Allocation", 0.16,
            "no capital-allocation commentary has been extracted or "
            "summarised from this company's filings.",
        ))

    # --- credibility -------------------------------------------------------
    credibility, years_assessed = evidence.credibility
    if credibility is not None and years_assessed > 0:
        graded = [o for o in evidence.observations
                  if o.prior_verdict and o.prior_verdict != "not_assessable"]
        factors.append(FactorScore(
            key="credibility", label="Credibility", weight=0.14,
            score=scale(credibility, 0.0, 1.0),
            origin=Origin.EXTRACTED, value=credibility, unit="0-1",
            reason=(
                f"Across {years_assessed} assessable years the platform "
                f"tested each year's guidance against the following year's "
                f"results and scored management {credibility:.0%}. This is a "
                "recorded track record, not an impression: each verdict was "
                "reached with the evidence available at the time and is "
                "never recomputed with hindsight."
            ),
            evidence="; ".join(
                f"FY{o.fiscal_year}: {o.prior_verdict}" for o in graded[-4:]
            ),
            citations=tuple(ScoringEvidence.observation_citation(o)
                            for o in graded[-3:]),
            computed_by=SERVICE,
        ))
    else:
        factors.append(_missing(
            "credibility", "Credibility", 0.14,
            "no year carried guidance specific enough to test against the "
            "following year's outcome, so management has no measurable track "
            "record here. An ungraded company is not an average one.",
        ))

    # --- execution ---------------------------------------------------------
    # Execution is measured, not narrated: did the reported numbers improve
    # while management said they would? The observation dimensions carry the
    # per-year readings the temporal engine recorded.
    graded_years = [o for o in evidence.observations
                    if o.prior_verdict in {"met", "exceeded"}]
    all_graded = [o for o in evidence.observations
                  if o.prior_verdict and o.prior_verdict != "not_assessable"]
    if all_graded:
        hit_rate = len(graded_years) / len(all_graded)
        factors.append(FactorScore(
            key="execution", label="Execution", weight=0.10,
            score=scale(hit_rate, 0.0, 1.0),
            origin=Origin.EXTRACTED, value=hit_rate, unit="hit rate",
            reason=(
                f"Management met or exceeded its own guidance in "
                f"{len(graded_years)} of {len(all_graded)} testable years, a "
                f"{hit_rate:.0%} hit rate. Execution is scored against what "
                "management themselves said, not against an external forecast."
            ),
            evidence="; ".join(
                f"FY{o.fiscal_year}: {o.prior_verdict}" for o in all_graded[-4:]
            ),
            citations=tuple(ScoringEvidence.observation_citation(o)
                            for o in all_graded[-3:]),
            computed_by=SERVICE,
        ))
    else:
        factors.append(_missing(
            "execution", "Execution", 0.10,
            "no testable guidance exists, so delivery against it cannot be "
            "measured.",
        ))

    return build_module(KEY, factors)
