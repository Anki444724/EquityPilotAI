"""The scoring framework: ten modules, fixed weights, and the guardrails.

Weights are declared here as data and asserted to sum to 100 at import. That
assertion is not decoration — the composite is expressed as points out of 100,
and a framework summing to 97 would silently cap every company at 97 while
looking entirely reasonable in the panel.

**The framework is versioned.** ``FRAMEWORK_VERSION`` changes whenever a weight
or a module changes, and every stored score records the version that produced
it. Comparing a score computed under v1 with one computed under v2 is comparing
two different questions, and the version is what makes that visible instead of
producing a phantom trend.

The guardrails below exist because a weighted mean is not a recommendation.
A company can score 74 on the blend while carrying a balance sheet that will
not survive a bad year, and a framework that says "Buy" on that arithmetic is
worse than useless. Each guardrail states the condition, the cap, and the
reason it fired — the reason is what appears in the panel.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.domain.ai_scoring.types import Recommendation

#: Bumped on any change to module membership or weights.
FRAMEWORK_VERSION = "3.0.0"


class Module(StrEnum):
    """The ten modules of the framework, in the brief's order."""

    COMPANY_DATA = "company_data"
    FINANCIAL_STATEMENTS = "financial_statements"
    LATEST_NEWS = "latest_news"
    INDUSTRY_ANALYSIS = "industry_analysis"
    MANAGEMENT_COMMENTARY = "management_commentary"
    AI_ANALYSIS = "ai_analysis"
    BUSINESS_QUALITY = "business_quality"
    GROWTH = "growth"
    RISK = "risk"
    VALUATION = "valuation"


MODULE_LABELS: dict[Module, str] = {
    Module.COMPANY_DATA: "Company Data",
    Module.FINANCIAL_STATEMENTS: "Financial Statements",
    Module.LATEST_NEWS: "Latest News",
    Module.INDUSTRY_ANALYSIS: "Industry Analysis",
    Module.MANAGEMENT_COMMENTARY: "Management Commentary",
    Module.AI_ANALYSIS: "AI Analysis Engine",
    Module.BUSINESS_QUALITY: "Business Quality Score",
    Module.GROWTH: "Growth Score",
    Module.RISK: "Risk Score",
    Module.VALUATION: "Valuation Score",
}

#: Fixed framework weights, out of 100, exactly as the brief specifies.
MODULE_WEIGHTS: dict[Module, float] = {
    Module.COMPANY_DATA: 10.0,
    Module.FINANCIAL_STATEMENTS: 15.0,
    Module.LATEST_NEWS: 8.0,
    Module.INDUSTRY_ANALYSIS: 8.0,
    Module.MANAGEMENT_COMMENTARY: 10.0,
    Module.AI_ANALYSIS: 10.0,
    Module.BUSINESS_QUALITY: 14.0,
    Module.GROWTH: 10.0,
    Module.RISK: 8.0,
    Module.VALUATION: 7.0,
}

#: The brief's own evaluation checklist per module. Held as data so the API can
#: publish what each module claims to assess, and so a factor set that drifts
#: from the brief is detectable by a test rather than by reading the code.
MODULE_CRITERIA: dict[Module, tuple[str, ...]] = {
    Module.COMPANY_DATA: (
        "Business Profile", "Market Position", "Market Cap", "Sector",
        "Industry", "Competitive Landscape",
    ),
    Module.FINANCIAL_STATEMENTS: (
        "Revenue Growth", "EBITDA", "PAT", "EPS", "ROE", "ROCE", "Debt",
        "Free Cash Flow", "Operating Margin", "Cash Position",
        "Capital Allocation",
    ),
    Module.LATEST_NEWS: (
        "Positive developments", "Negative developments",
        "Regulatory changes", "M&A", "Large orders",
        "Management announcements",
    ),
    Module.INDUSTRY_ANALYSIS: (
        "Industry growth", "Competition", "Market size",
        "Government policy", "Demand outlook",
    ),
    Module.MANAGEMENT_COMMENTARY: (
        "Conference Calls", "Annual Reports", "Investor Presentations",
        "Guidance", "Capital Allocation", "Credibility", "Execution",
    ),
    Module.AI_ANALYSIS: (
        "Business Moat", "Competitive Advantage", "Risks", "Opportunities",
        "ESG", "AI reasoning",
    ),
    Module.BUSINESS_QUALITY: (
        "Moat", "Pricing Power", "Brand", "Scalability",
        "Customer Retention", "Capital Efficiency",
    ),
    Module.GROWTH: (
        "Revenue CAGR", "EPS CAGR", "PAT CAGR", "Market Expansion",
        "Capacity Expansion",
    ),
    Module.RISK: (
        "Debt Risk", "Regulatory Risk", "Customer Concentration",
        "Commodity Risk", "Governance Risk",
    ),
    Module.VALUATION: (
        "PE", "PB", "EV/EBITDA", "DCF", "Relative Valuation",
        "Margin of Safety",
    ),
}

# The composite is points out of 100. If the weights do not sum to 100 the
# maximum attainable score silently is not 100, and every rating band shifts.
_TOTAL = sum(MODULE_WEIGHTS.values())
assert abs(_TOTAL - 100.0) < 1e-9, (
    f"framework weights sum to {_TOTAL}, not 100"
)
assert set(MODULE_WEIGHTS) == set(Module), "a module has no declared weight"
assert set(MODULE_CRITERIA) == set(Module), "a module has no declared criteria"

#: Presentation order, which is the brief's order.
MODULE_ORDER: tuple[Module, ...] = tuple(Module)


# ---------------------------------------------------------------------------
# Guardrails
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Guardrail:
    """A condition that caps the recommendation regardless of the composite."""

    key: str
    #: Module whose score is tested.
    module: Module
    #: Fires when the module scores at or below this, on the 0-10 scale.
    at_or_below: float
    cap: Recommendation
    #: Written into the panel verbatim, with the score interpolated.
    reason_template: str

    def fires(self, module_score: float) -> bool:
        return module_score <= self.at_or_below

    def reason(self, module_score: float) -> str:
        return self.reason_template.format(score=module_score)


#: Ordered most-severe first. All are evaluated; the tightest cap wins.
GUARDRAILS: tuple[Guardrail, ...] = (
    Guardrail(
        key="fragile_balance_sheet",
        module=Module.RISK,
        at_or_below=3.0,
        cap=Recommendation.REDUCE,
        reason_template=(
            "Capped at Reduce: the risk module scores {score:.1f}/10, which "
            "indicates balance-sheet or governance fragility that quality "
            "elsewhere does not offset."
        ),
    ),
    Guardrail(
        key="expensive",
        module=Module.VALUATION,
        at_or_below=3.0,
        cap=Recommendation.HOLD,
        reason_template=(
            "Capped at Hold: valuation scores {score:.1f}/10, so the shares "
            "are expensive on every multiple the engine can observe. A good "
            "business at a bad price is a bad investment."
        ),
    ),
    Guardrail(
        key="weak_financials",
        module=Module.FINANCIAL_STATEMENTS,
        at_or_below=3.0,
        cap=Recommendation.HOLD,
        reason_template=(
            "Capped at Hold: reported financials score {score:.1f}/10, so the "
            "case rests on a recovery the statements do not yet show."
        ),
    ),
)

#: Below this weighted coverage the recommendation is capped at Hold: the
#: engine should be less decisive when it has seen less. Set at 0.45 rather
#: than a rounder number because the corpus median coverage is around 0.5 and
#: a threshold above it would cap the entire universe, which communicates
#: nothing.
MIN_COVERAGE_FOR_DIRECTION = 0.45

#: Coverage below which the result carries an explicit provisional warning.
PROVISIONAL_COVERAGE = 0.60

#: Ordered weakest-to-strongest, for applying caps.
RECOMMENDATION_ORDER: tuple[Recommendation, ...] = (
    Recommendation.AVOID,
    Recommendation.REDUCE,
    Recommendation.HOLD,
    Recommendation.BUY,
    Recommendation.STRONG_BUY,
)


def apply_guardrails(
    base: Recommendation,
    module_scores: dict[str, float],
    coverage: float,
) -> tuple[Recommendation, list[str]]:
    """Cap a recommendation, returning it with every reason that applied.

    Every firing guardrail is reported even when a tighter one supersedes it.
    Reporting only the binding constraint hides the fact that three separate
    things were wrong, which is materially different from one.
    """
    index = RECOMMENDATION_ORDER.index(base)
    reasons: list[str] = []

    for rail in GUARDRAILS:
        score = module_scores.get(rail.module.value)
        if score is None or not rail.fires(score):
            continue
        reasons.append(rail.reason(score))
        ceiling = RECOMMENDATION_ORDER.index(rail.cap)
        index = min(index, ceiling)

    if coverage < MIN_COVERAGE_FOR_DIRECTION:
        reasons.append(
            f"Capped at Hold: only {coverage:.0%} of weighted inputs were "
            "observable, which does not support a directional call."
        )
        index = min(index, RECOMMENDATION_ORDER.index(Recommendation.HOLD))

    return RECOMMENDATION_ORDER[index], reasons
