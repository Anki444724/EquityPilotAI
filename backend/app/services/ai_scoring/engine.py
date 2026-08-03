"""The AI Scoring Engine 3.0 orchestrator.

Runs the ten module scorers over one shared :class:`ScoringEvidence`, applies
the framework weights, derives the rating, recommendation and probabilities,
and assembles an :class:`AIScoreResult`.

**Determinism is the contract.** Given the same evidence this function returns
the same result, byte for byte, every time. No randomness, no clock, no network
call, no model. The ``input_fingerprint`` on the result is the proof: two runs
over an unchanged corpus produce the same digest, which is how the learning
loop distinguishes "recalculated and nothing moved" from "recalculated and the
view changed" without overwriting anything.

**AI commentary is attached after the arithmetic is complete.** The optional
narrator receives a finished result and may write prose about it. It is
physically incapable of altering a score, because by the time it runs every
number is already inside a frozen dataclass.
"""
from __future__ import annotations

import time
from dataclasses import replace
from typing import Any, Callable, Sequence

import structlog

from app.domain.ai_scoring.framework import (
    FRAMEWORK_VERSION, MIN_COVERAGE_FOR_DIRECTION, Module, MODULE_ORDER,
    MODULE_WEIGHTS, PROVISIONAL_COVERAGE, apply_guardrails,
)
from app.domain.ai_scoring.probability import estimate_all
from app.domain.ai_scoring.types import (
    AIScoreResult, ModuleScore, Recommendation, fingerprint, rating_for,
    recommendation_for,
)
from app.services.ai_scoring.evidence import ScoringEvidence
from app.services.ai_scoring.modules import (
    ai_analysis, business_quality, company_data, financial_statements, growth,
    industry_analysis, latest_news, management_commentary, risk, valuation,
)

log = structlog.get_logger(__name__)

#: Module scorers, in the brief's presentation order.
#:
#: Three take an extra `sector_stats` argument. They are marked here rather
#: than inspected at call time, because introspecting a signature to decide
#: how to call it is how a renamed parameter becomes a silent behaviour change.
_SCORERS: tuple[tuple[Module, Callable[..., ModuleScore], bool], ...] = (
    (Module.COMPANY_DATA, company_data.score, False),
    (Module.FINANCIAL_STATEMENTS, financial_statements.score, False),
    (Module.LATEST_NEWS, latest_news.score, False),
    (Module.INDUSTRY_ANALYSIS, industry_analysis.score, True),
    (Module.MANAGEMENT_COMMENTARY, management_commentary.score, False),
    (Module.AI_ANALYSIS, ai_analysis.score, False),
    (Module.BUSINESS_QUALITY, business_quality.score, True),
    (Module.GROWTH, growth.score, False),
    (Module.RISK, risk.score, False),
    (Module.VALUATION, valuation.score, True),
)

# Presentation order must match the framework's declared order exactly, or the
# panel shows the modules in one sequence and the API documents another.
assert tuple(m for m, _, _ in _SCORERS) == MODULE_ORDER, (
    "scorer order has drifted from the framework's declared module order"
)


def compute(
    evidence: ScoringEvidence,
    *,
    sector_stats: dict[str, Any] | None = None,
) -> AIScoreResult:
    """Run the ten modules and assemble the explainable result."""
    started = time.perf_counter()
    stats = sector_stats or {}

    modules: list[ModuleScore] = []
    for module, scorer, wants_stats in _SCORERS:
        result = scorer(evidence, sector_stats=stats) if wants_stats else scorer(evidence)
        if result.key != module.value:
            # A scorer returning the wrong module key would silently attach
            # one module's factors to another's weight.
            raise RuntimeError(
                f"scorer for {module.value} returned key '{result.key}'"
            )
        modules.append(result)

    # --- composite ---------------------------------------------------------
    # Points out of 100. `contribution` is score/10 * weight, and the weights
    # sum to 100 by assertion in the framework module, so this is already on
    # the 0-100 scale and needs no rescaling.
    composite = sum(m.contribution for m in modules)
    composite = max(0.0, min(100.0, composite))

    # --- coverage ----------------------------------------------------------
    # Weighted by framework weight: an unobservable factor inside a 15-point
    # module damages coverage more than one inside a 7-point module, which a
    # simple average across modules would miss.
    total_weight = sum(MODULE_WEIGHTS[Module(m.key)] for m in modules)
    coverage = (
        sum(m.coverage * MODULE_WEIGHTS[Module(m.key)] for m in modules)
        / total_weight if total_weight else 0.0
    )

    module_scores = {m.key: m.score for m in modules}
    module_coverage = {m.key: m.coverage for m in modules}

    rating, rating_description = rating_for(composite)
    base_recommendation = recommendation_for(composite)
    recommendation, guardrail_reasons = apply_guardrails(
        base_recommendation, module_scores, coverage
    )

    reason_parts = [
        f"A composite of {composite:.1f}/100 maps to "
        f"{base_recommendation.value}."
    ]
    reason_parts.extend(guardrail_reasons)
    if recommendation is not base_recommendation:
        reason_parts.append(
            f"Final recommendation: {recommendation.value}."
        )

    probabilities = estimate_all(module_scores, module_coverage)

    warnings = _warnings(evidence, modules, coverage)
    summary = _summarise(evidence, composite, rating.value, recommendation.value,
                         modules, coverage, probabilities)

    result = AIScoreResult(
        company_id=evidence.company.id,
        ticker=evidence.company.ticker,
        name=evidence.company.name,
        overall_score=composite,
        rating=rating,
        rating_description=rating_description,
        recommendation=recommendation,
        recommendation_reason=" ".join(reason_parts),
        modules=tuple(modules),
        probabilities=probabilities,
        coverage=coverage,
        summary=summary,
        warnings=tuple(warnings),
        guardrails=tuple(guardrail_reasons),
        framework_version=FRAMEWORK_VERSION,
        input_fingerprint=fingerprint(evidence.fingerprint_payload()),
    )

    log.info("ai score computed", ticker=result.ticker,
             score=round(composite, 2), rating=rating.value,
             recommendation=recommendation.value,
             coverage=round(coverage, 3),
             citations=result.total_citations,
             ms=round((time.perf_counter() - started) * 1000, 1))
    return result


# ---------------------------------------------------------------------------
# Narrative — deterministic, never model-written
# ---------------------------------------------------------------------------

def _warnings(
    evidence: ScoringEvidence,
    modules: Sequence[ModuleScore],
    coverage: float,
) -> list[str]:
    warnings: list[str] = []

    # The Module 4 constraint, propagated verbatim.
    if evidence.valuation_is_illustrative:
        warnings.append(
            evidence.valuation_disclosure
            or "Illustrative valuation only. Real filings are required for "
               "investment-grade outputs."
        )

    if coverage < PROVISIONAL_COVERAGE:
        warnings.append(
            f"Provisional: only {coverage:.0%} of weighted inputs were "
            "observable. The score reflects what the platform holds, which "
            "is not the same as what is true."
        )

    unassessed = [m for m in modules if m.coverage < 0.20]
    if unassessed:
        warnings.append(
            "Effectively unassessed modules ("
            + ", ".join(f"{m.label} {m.weight:.0f}pt" for m in unassessed)
            + "): these scored the neutral midpoint on missing inputs and "
              f"account for {sum(m.weight for m in unassessed):.0f} of the "
              "100 available points."
        )

    if evidence.data_quality is not None and evidence.data_quality < 40:
        warnings.append(
            f"Data Quality Score for this company is "
            f"{evidence.data_quality:.0f}/100, which bounds how much any "
            "score here can be relied upon."
        )

    if not evidence.incomes:
        warnings.append(
            "No financial statements are held. Every quantitative factor is "
            "missing and the score rests entirely on reference and document "
            "evidence."
        )

    return warnings


def _summarise(
    evidence: ScoringEvidence,
    composite: float,
    rating: str,
    recommendation: str,
    modules: Sequence[ModuleScore],
    coverage: float,
    probabilities: Sequence[Any],
) -> str:
    ranked = sorted(modules, key=lambda m: -m.score)
    strongest = ranked[:2]
    weakest = ranked[-2:]

    # Contribution, not raw score, identifies what actually drove the number.
    # A 9.2 inside a 7-point module moves the composite less than a 6.8 inside
    # a 15-point one, and reporting the former as the "driver" would be wrong.
    by_contribution = sorted(modules, key=lambda m: -m.contribution)

    parts = [
        f"{evidence.company.name} scores {composite:.1f}/100 (rating "
        f"{rating}), supporting a {recommendation} recommendation on "
        f"{coverage:.0%} weighted evidence coverage.",
        "Largest contributors: "
        + "; ".join(f"{m.label} {m.contribution:.1f} of {m.weight:.0f} points"
                    for m in by_contribution[:3]) + ".",
        "Strongest modules: "
        + "; ".join(f"{m.label} {m.score:.1f}/10" for m in strongest) + ".",
        "Weakest modules: "
        + "; ".join(f"{m.label} {m.score:.1f}/10" for m in weakest) + ".",
    ]

    overall = next((p for p in probabilities
                    if p.key == "overall_investment"), None)
    if overall is not None:
        parts.append(
            f"Overall investment probability {overall.probability:.0%}."
        )

    missing_total = sum(len(m.missing_factors) for m in modules)
    factor_total = sum(len(m.factors) for m in modules)
    if missing_total:
        parts.append(
            f"{missing_total} of {factor_total} factors could not be "
            "observed and scored the neutral midpoint; each is named in its "
            "module."
        )

    return " ".join(parts)
