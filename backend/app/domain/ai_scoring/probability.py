"""Future probability estimates, derived from module scores.

The brief asks for five probabilities. They are computed here as an explicit,
inspectable mapping from module scores onto a 0-1 range — not sampled from a
model, and not asserted by a prompt.

Three properties are deliberate.

**Every probability names its drivers and their influence.** ``Probability``
carries the module keys and signed coefficients that produced it, so a reader
can reconstruct the figure with a calculator. A probability nobody can
reconstruct is an opinion with a percent sign.

**Estimates shrink toward 50% when evidence is thin.** A logistic on a module
score of 8.4 gives 78% whether that 8.4 rested on twelve years of filings or
on two reference fields. Shrinkage proportional to missing coverage is what
keeps the second case honest. The shrinkage applied is reported, not hidden.

**Nothing reaches 0% or 100%.** The logistic is bounded and then clipped to
[0.02, 0.98]. A platform that tells a portfolio manager a company will
certainly beat the index is making a claim no evidence supports.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

from app.domain.ai_scoring.framework import Module
from app.domain.ai_scoring.types import Probability

#: Hard bounds. Certainty is not available from this evidence base.
PROBABILITY_FLOOR = 0.02
PROBABILITY_CEILING = 0.98

#: Logistic steepness. At k=0.62 a module composite of 8.0/10 (three points
#: above the 5.0 neutral) maps to ~0.86 before shrinkage, and 3.0/10 maps to
#: ~0.14 — a spread wide enough to be useful and narrow enough that a single
#: strong module cannot manufacture a near-certainty.
STEEPNESS = 0.62

#: The point on the 0-10 scale that maps to 50%.
NEUTRAL_POINT = 5.0

#: Maximum share of the distance to 50% that thin evidence can claw back.
#: At coverage 0 the estimate is pulled 85% of the way to a coin flip; the
#: remaining 15% preserves the sign, because "we know almost nothing but what
#: little we saw was bad" is still information.
MAX_SHRINKAGE = 0.85


@dataclass(frozen=True, slots=True)
class ProbabilitySpec:
    """A named probability and the weighted modules that determine it."""

    key: str
    label: str
    #: (module, coefficient). Coefficients are relative and normalised;
    #: a negative coefficient inverts the module (risk raises probability of
    #: a bad outcome, so it enters positively only where that is intended).
    drivers: tuple[tuple[Module, float], ...]
    reason_template: str


#: The five probabilities the brief specifies.
#:
#: The driver sets are not arbitrary. Outperformance is a joint claim about
#: quality, price and growth, so it reads all three plus risk. Earnings growth
#: leans on margins and management execution rather than on valuation, which
#: has no bearing on whether profits rise. Multiple expansion is the one that
#: reads valuation most heavily — a stock already on a full multiple has less
#: room to re-rate regardless of how good the business is, which is why
#: valuation carries the largest single coefficient there.
PROBABILITY_SPECS: tuple[ProbabilitySpec, ...] = (
    ProbabilitySpec(
        key="outperform_nifty",
        label="Probability of Outperforming the Nifty",
        drivers=(
            (Module.BUSINESS_QUALITY, 0.24),
            (Module.VALUATION, 0.22),
            (Module.GROWTH, 0.20),
            (Module.FINANCIAL_STATEMENTS, 0.14),
            (Module.RISK, 0.12),
            (Module.LATEST_NEWS, 0.08),
        ),
        reason_template=(
            "Relative performance is a joint claim about franchise quality "
            "({business_quality:.1f}/10), the price paid ({valuation:.1f}/10) "
            "and the growth runway ({growth:.1f}/10)."
        ),
    ),
    ProbabilitySpec(
        key="earnings_growth",
        label="Probability of Earnings Growth",
        drivers=(
            (Module.GROWTH, 0.30),
            (Module.FINANCIAL_STATEMENTS, 0.24),
            (Module.BUSINESS_QUALITY, 0.18),
            (Module.MANAGEMENT_COMMENTARY, 0.16),
            (Module.INDUSTRY_ANALYSIS, 0.12),
        ),
        reason_template=(
            "Earnings growth rests on the historical growth record "
            "({growth:.1f}/10), reported profitability "
            "({financial_statements:.1f}/10) and management's execution "
            "against its own guidance ({management_commentary:.1f}/10)."
        ),
    ),
    ProbabilitySpec(
        key="revenue_growth",
        label="Probability of Revenue Growth",
        drivers=(
            (Module.GROWTH, 0.32),
            (Module.INDUSTRY_ANALYSIS, 0.24),
            (Module.BUSINESS_QUALITY, 0.18),
            (Module.COMPANY_DATA, 0.14),
            (Module.LATEST_NEWS, 0.12),
        ),
        reason_template=(
            "Revenue growth is driven more by the industry "
            "({industry_analysis:.1f}/10) and the company's position within "
            "it ({company_data:.1f}/10) than by its balance sheet."
        ),
    ),
    ProbabilitySpec(
        key="multiple_expansion",
        label="Probability of Multiple Expansion",
        drivers=(
            (Module.VALUATION, 0.36),
            (Module.GROWTH, 0.20),
            (Module.BUSINESS_QUALITY, 0.16),
            (Module.MANAGEMENT_COMMENTARY, 0.14),
            (Module.LATEST_NEWS, 0.14),
        ),
        reason_template=(
            "Re-rating requires room to re-rate: valuation scores "
            "{valuation:.1f}/10, and a stock already on a full multiple has "
            "less of it however good the business."
        ),
    ),
    ProbabilitySpec(
        key="overall_investment",
        label="Overall Investment Probability",
        drivers=(
            (Module.BUSINESS_QUALITY, 0.20),
            (Module.FINANCIAL_STATEMENTS, 0.18),
            (Module.VALUATION, 0.16),
            (Module.GROWTH, 0.14),
            (Module.RISK, 0.12),
            (Module.MANAGEMENT_COMMENTARY, 0.10),
            (Module.AI_ANALYSIS, 0.10),
        ),
        reason_template=(
            "The composite view across quality, statements, price, growth "
            "and risk."
        ),
    ),
)


def _logistic(module_composite: float) -> float:
    """Map a 0-10 blended module score onto (0, 1)."""
    return 1.0 / (1.0 + math.exp(-STEEPNESS * (module_composite - NEUTRAL_POINT)))


def _shrink(raw: float, coverage: float) -> tuple[float, float]:
    """Pull an estimate toward 50% in proportion to missing evidence.

    Returns ``(adjusted, shrinkage_applied)``.
    """
    shrinkage = MAX_SHRINKAGE * (1.0 - max(0.0, min(1.0, coverage)))
    adjusted = 0.5 + (raw - 0.5) * (1.0 - shrinkage)
    return adjusted, shrinkage


def estimate(
    spec: ProbabilitySpec,
    module_scores: dict[str, float],
    module_coverage: dict[str, float],
) -> Probability:
    """Compute one probability from the module scores it depends on.

    Modules absent from ``module_scores`` are skipped and their coefficient
    removed from the normaliser, rather than being read as zero. Reading a
    missing module as zero would score it as catastrophic rather than as
    unobserved — the single most common way an evidence gap turns into a
    confident negative claim.
    """
    present = [
        (module, coefficient)
        for module, coefficient in spec.drivers
        if module.value in module_scores
    ]
    if not present:
        return Probability(
            key=spec.key, label=spec.label, probability=0.5, drivers=(),
            reason=("Not estimated: none of the modules this probability "
                    "depends on could be scored."),
            shrinkage=1.0,
        )

    total = sum(coefficient for _, coefficient in present)
    composite = sum(
        module_scores[module.value] * coefficient for module, coefficient in present
    ) / total
    coverage = sum(
        module_coverage.get(module.value, 0.0) * coefficient
        for module, coefficient in present
    ) / total

    raw = _logistic(composite)
    adjusted, shrinkage = _shrink(raw, coverage)
    bounded = max(PROBABILITY_FLOOR, min(PROBABILITY_CEILING, adjusted))

    reason = spec.reason_template.format(
        **{module.value: module_scores.get(module.value, 0.0)
           for module, _ in spec.drivers}
    )
    if shrinkage > 0.05:
        reason += (
            f" Pulled {shrinkage:.0%} toward an even chance because only "
            f"{coverage:.0%} of the supporting evidence was observable."
        )

    return Probability(
        key=spec.key, label=spec.label, probability=bounded,
        drivers=tuple((module.value, coefficient / total)
                      for module, coefficient in present),
        reason=reason, shrinkage=shrinkage,
    )


def estimate_all(
    module_scores: dict[str, float],
    module_coverage: dict[str, float],
) -> tuple[Probability, ...]:
    return tuple(
        estimate(spec, module_scores, module_coverage)
        for spec in PROBABILITY_SPECS
    )
