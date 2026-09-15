"""What each intent needs in order to be answered.

The planner states *requirements*, never results. Nothing in this module
holds a figure, and nothing here is computed: every key below is one the
:class:`ContextBuilder` already publishes, and the mapping was taken by
reading what the existing deterministic engines actually look up.

That direction matters. A plan that named evidence the platform cannot
produce would be a plan the execution layer cannot keep, and the failure
would surface as a missing citation at answer time rather than as an
honest gap at planning time — which is exactly the surprise this layer is
meant to remove.

The ``published`` flag exists for the one case where an intent legitimately
requires evidence the platform does not yet emit: P/B. The ContextBuilder
publishes no ``pb`` citation and ``FinancialAnswerEngine._build_pb`` already
reports it as unavailable rather than deriving it. The planner records the
same gap instead of implying the figure is there.

Values are never carried. ``EvidenceRequirement`` has no field that could
hold one.
"""
from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

from app.domain.ai.types import EvidenceKind
from app.services.ai.financial_intent import FinancialIntent

from .types import EvidenceRequirement


# Source labels, exactly as the ContextBuilder stamps them. Reused rather
# than reworded so a requirement can be traced back to its evidence block.
_IS = "06 Historical IS"
_BS = "07 Historical BS"
_RATIO = "10 Ratio Analysis"
_FORECAST = "Forecast engine"
_VALUATION = "Valuation engine"
_SCORING = "Scoring engine"
_MARKET = "Market data"
_SCORE_RESULT = "ScoreResult"


def _statement(key: str, label: str, source: str = _IS) -> EvidenceRequirement:
    return EvidenceRequirement(key, label, EvidenceKind.STATEMENT, source=source)


def _ratio(key: str, label: str) -> EvidenceRequirement:
    return EvidenceRequirement(key, label, EvidenceKind.RATIO, source=_RATIO)


def _valuation(key: str, label: str) -> EvidenceRequirement:
    return EvidenceRequirement(key, label, EvidenceKind.VALUATION,
                               source=_VALUATION)


def _scoring(key: str, label: str) -> EvidenceRequirement:
    return EvidenceRequirement(key, label, EvidenceKind.SCORING, source=_SCORING)


def _score_object(key: str, label: str) -> EvidenceRequirement:
    """A field of ``ScoreResult`` rather than a published citation.

    The Phase 2A engine reads these from ``GroundedContext.score`` — the
    same object the scoring citations were rendered from — so they are
    evidence in every meaningful sense while not being citation keys.
    """
    return EvidenceRequirement(key, label, EvidenceKind.SCORING,
                               source=_SCORE_RESULT)


def _optional(req: EvidenceRequirement, note: str = "") -> EvidenceRequirement:
    """Supporting context the answer can be given without."""
    return EvidenceRequirement(
        req.key, req.label, req.kind, required=False, source=req.source,
        published=req.published, note=note or req.note,
    )


#: Category scores whose key is only known per run — the ContextBuilder
#: emits one ``score_{category}`` citation per scoring category. A plan
#: cannot name them all without duplicating the profile, so it names the
#: shape and lets the execution layer resolve the set.
_CATEGORY_SCORES = _scoring("score_*", "Per-category score citation")

_VALUATION_SET: tuple[EvidenceRequirement, ...] = (
    _valuation("weighted_value", "Weighted intrinsic value"),
    _valuation("valuation_upside", "Upside to intrinsic value"),
    _valuation("dcf_value", "DCF intrinsic value per share"),
    _valuation("dcf_upside", "DCF upside"),
    _valuation("pe_ratio", "Trailing P/E"),
    _valuation("ev_ebitda", "EV/EBITDA"),
    _valuation("relative_target", "Blended relative target price"),
    _valuation("wacc", "WACC"),
    _valuation("cost_of_equity", "Cost of equity"),
    _valuation("terminal_value_pct", "Terminal value share of EV"),
    _valuation("valuation_recommendation", "Valuation recommendation"),
    _valuation("data_quality", "Data quality grade"),
)


#: Intent -> evidence required to answer it.
REQUIRED_EVIDENCE: Mapping[FinancialIntent, tuple[EvidenceRequirement, ...]] = (
    MappingProxyType({
        # ------------------------------------------------------- Phase 1 --
        FinancialIntent.PE: (
            _valuation("pe_ratio", "Trailing P/E"),
            _optional(EvidenceRequirement(
                "price", "Current market price", EvidenceKind.MARKET,
                source=_MARKET,
            ), "Quoted with the P/E as its numerator."),
            _optional(_statement("eps", "EPS (basic)"),
                      "Quoted with the P/E as its denominator."),
        ),
        FinancialIntent.PB: (
            # Required, and honestly flagged: the ContextBuilder does not
            # publish this key today. The plan says so instead of letting
            # the execution layer discover it mid-answer.
            EvidenceRequirement(
                "pb", "Price-to-book (P/B)", EvidenceKind.VALUATION,
                source=_VALUATION, published=False,
                note=(
                    "The ContextBuilder does not currently publish a 'pb' "
                    "citation; FinancialAnswerEngine reports P/B as "
                    "unavailable rather than deriving it from price and book "
                    "value. Planned as required evidence, not available yet."
                ),
            ),
        ),
        FinancialIntent.EPS: (_statement("eps", "EPS (basic)"),),
        FinancialIntent.DEBT: (
            _statement("gross_debt", "Gross debt", _BS),
            _statement("net_debt", "Net debt", _BS),
            _ratio("net_debt_ebitda", "Net debt / EBITDA"),
        ),
        FinancialIntent.ROE: (_ratio("roe_avg", "Return on equity"),),
        FinancialIntent.ROCE: (_ratio("roce", "Return on capital employed"),),
        FinancialIntent.MARKET_PRICE: (
            EvidenceRequirement("price", "Current market price",
                                EvidenceKind.MARKET, source=_MARKET),
            EvidenceRequirement("market_cap", "Market capitalisation",
                                EvidenceKind.MARKET, source=_MARKET),
        ),
        FinancialIntent.REVENUE_GROWTH: (
            _statement("revenue_growth", "Year-on-year revenue growth"),
            _optional(_statement("revenue_history", "Revenue history"),
                      "A level without a trend invites inferred direction."),
        ),
        FinancialIntent.PROFIT_GROWTH: (
            _statement("pat_growth", "Year-on-year profit growth"),
        ),
        FinancialIntent.VALUATION: _VALUATION_SET,

        # ------------------------------------------------------ Phase 2A ---
        FinancialIntent.OVERALL_ASSESSMENT: (
            _scoring("overall_score", "Institutional score"),
            _scoring("grade", "Institutional grade"),
            _scoring("recommendation", "Scoring recommendation"),
            _scoring("confidence", "Score confidence"),
            _CATEGORY_SCORES,
            _optional(_score_object("score.strongest", "Strongest areas")),
            _optional(_score_object("score.weakest", "Weakest areas")),
        ),
        FinancialIntent.FINANCIAL_QUALITY: (
            _scoring("score_financial_quality", "Financial Quality score"),
            _ratio("roe_avg", "Return on equity"),
            _statement("ebitda_margin", "EBITDA margin"),
            _statement("pat_margin", "Net margin"),
        ),
        FinancialIntent.GROWTH_QUALITY: (
            _scoring("score_growth_quality", "Growth Quality score"),
            _statement("revenue_growth", "Year-on-year revenue growth"),
            _statement("pat_growth", "Year-on-year profit growth"),
            _optional(EvidenceRequirement(
                "forecast_revenue_cagr", "Forecast revenue CAGR",
                EvidenceKind.FORECAST, source=_FORECAST,
            ), "Quoted when the forecast engine produced one."),
        ),
        FinancialIntent.FINANCIAL_RISK: (
            _scoring("score_financial_risk", "Financial Risk score"),
            _ratio("net_debt_ebitda", "Net debt / EBITDA"),
            _ratio("interest_coverage", "Interest coverage"),
            _ratio("current_ratio", "Current ratio"),
            _ratio("altman_z", "Altman Z-score"),
            _statement("gross_debt", "Gross debt", _BS),
            _statement("net_debt", "Net debt", _BS),
        ),
        FinancialIntent.STRENGTHS: (
            _score_object("score.strongest", "Strongest areas"),
            _CATEGORY_SCORES,
        ),
        FinancialIntent.WEAKNESSES: (
            _score_object("score.weakest", "Weakest areas"),
            _CATEGORY_SCORES,
        ),
        FinancialIntent.INVESTMENT_CASE: (
            _scoring("overall_score", "Institutional score"),
            _scoring("grade", "Institutional grade"),
            _scoring("recommendation", "Scoring recommendation"),
            _score_object("score.strongest", "Strongest areas"),
            _score_object("score.weakest", "Weakest areas"),
            *_VALUATION_SET,
        ),
        FinancialIntent.RECOMMENDATION: (
            _scoring("recommendation", "Scoring recommendation"),
            _scoring("overall_score", "Institutional score"),
            _scoring("grade", "Institutional grade"),
            _scoring("confidence", "Score confidence"),
            _score_object("score.recommendation_rationale",
                          "Recommendation rationale"),
        ),
    })
)


def evidence_for(intent: FinancialIntent) -> tuple[EvidenceRequirement, ...]:
    return REQUIRED_EVIDENCE.get(intent, ())


def evidence_for_all(
    intents: tuple[FinancialIntent, ...],
) -> tuple[EvidenceRequirement, ...]:
    """The union of several intents' evidence, de-duplicated by key.

    Order follows the intents, so a multi-intent plan lists evidence in the
    order the user asked for things. A key required by one intent and merely
    supporting in another stays required — the stricter requirement wins,
    because dropping it would mean dropping part of an answer.
    """
    merged: dict[str, EvidenceRequirement] = {}
    for intent in intents:
        for req in evidence_for(intent):
            existing = merged.get(req.key)
            if existing is None or (req.required and not existing.required):
                merged[req.key] = req
    return tuple(merged.values())
