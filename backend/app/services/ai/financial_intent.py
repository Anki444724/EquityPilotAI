"""Canonical financial intent detection.

The resolver maps a free-form question onto ONE of the platform's canonical
financial intents — or onto nothing at all. Detection is the entire job of
this module, and the boundary is deliberate:

* it does NOT resolve a company or a ticker;
* it does NOT fetch any company or financial data;
* it does NOT call any provider;
* it does NOT use RAG;
* it does NOT perform any financial calculation.

Company resolution stays with ``AnalysisService``, exactly as before.

The single-intent rule is a safety decision, not a limitation. A question
that contains two financial intents ("What is the P/E and the ROE?") cannot
be reduced to one deterministic answer without silently dropping half the
question, so it resolves to ``None`` and the normal provider path answers it
in full. When in doubt, the resolver declines.

The intent set spans two deterministic engines: the Phase 1 canonical
financial-figure intents (valuation, P/E, ROE, …) and the Phase 2A
investment-intelligence intents (``INVESTMENT_INTENTS``), which interpret
the existing ScoreResult. Both families share this one resolver and its
exactly-one-intent rule, so a question that mixes a Phase 1 intent with a
Phase 2A intent ("What is the P/E and what is the overall assessment?")
falls through to the provider path rather than getting a partial answer.
"""
from __future__ import annotations

import re
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping


class FinancialIntent(StrEnum):
    """The canonical financial questions the deterministic engine answers."""

    VALUATION = "valuation"
    PE = "pe"
    PB = "pb"
    DEBT = "debt"
    ROE = "roe"
    ROCE = "roce"
    PROFIT_GROWTH = "profit_growth"
    REVENUE_GROWTH = "revenue_growth"
    EPS = "eps"
    MARKET_PRICE = "market_price"

    # ------------------------------------------------------------------
    # Phase 2A — deterministic investment intelligence.
    #
    # These interpret the EXISTING ScoreResult; none of them names a figure
    # the scoring engine did not compute, and none is a second score.
    # ------------------------------------------------------------------
    OVERALL_ASSESSMENT = "overall_assessment"
    FINANCIAL_QUALITY = "financial_quality"
    GROWTH_QUALITY = "growth_quality"
    FINANCIAL_RISK = "financial_risk"
    STRENGTHS = "strengths"
    WEAKNESSES = "weaknesses"
    INVESTMENT_CASE = "investment_case"
    RECOMMENDATION = "recommendation"


#: The Phase 2A intents. The resolver treats them like any other intent for
#: the exactly-one rule; only the analyst's dispatch on this set decides
#: which deterministic engine renders the answer.
INVESTMENT_INTENTS = frozenset({
    FinancialIntent.OVERALL_ASSESSMENT,
    FinancialIntent.FINANCIAL_QUALITY,
    FinancialIntent.GROWTH_QUALITY,
    FinancialIntent.FINANCIAL_RISK,
    FinancialIntent.STRENGTHS,
    FinancialIntent.WEAKNESSES,
    FinancialIntent.INVESTMENT_CASE,
    FinancialIntent.RECOMMENDATION,
})


#: Phrases that identify each intent, matched on normalised (lower-cased,
#: whitespace-collapsed) question text. A question is a single intent only
#: when EXACTLY one intent's phrases match; two or more matches means the
#: question asks for several things and is handed back to the provider path.
#:
#: The market-price phrases carry a lookahead so "price to earnings" or
#: "price to book" is never also read as a question about the share price.
#: "EPS growth" is read as a profit-growth question (it is the growth of
#: earnings), and the plain EPS pattern is guarded against the same phrase.
_PATTERNS: dict[FinancialIntent, tuple[str, ...]] = {
    FinancialIntent.MARKET_PRICE: (
        # "price" alone — but not inside the names of other ratios
        # ("price to earnings", "price to book", "price target" and its
        # reversed form, "price per share").
        r"(?<!target )\bprice\b(?!\s*(?:to\b|per\b|/|-?target|-?earnings|-?book))",
    ),
    FinancialIntent.PE: (
        r"\bp\s*/\s*e\b",
        r"\bprice\s*(?:to|-)\s*earnings\b",
        r"\bearnings multiple\b",
        r"\bpe ratio\b",
        r"\bpe\b",
    ),
    FinancialIntent.PB: (
        r"\bp\s*/\s*b\b",
        r"\bprice\s*(?:to|-)\s*book\b",
        r"\bbook value multiple\b",
        r"\bp\s*b\b",
    ),
    FinancialIntent.DEBT: (
        r"\bdebt\b",
        r"\bborrowings?\b",
        r"\bleverag(?:e|ed|ing)\b",
        r"\bgearing\b",
    ),
    FinancialIntent.ROE: (
        r"\broe\b",
        r"\breturn on equity\b",
    ),
    FinancialIntent.ROCE: (
        r"\broce\b",
        r"\breturn on capital employed\b",
    ),
    FinancialIntent.PROFIT_GROWTH: (
        r"\bprofit\b[^.?!]{0,30}\bgrow(?:th|ing)\b",
        r"\bpat\b[^.?!]{0,30}\bgrow(?:th|ing)\b",
        r"\bearnings\b[^.?!]{0,30}\bgrow(?:th|ing)\b",
        r"\bnet (?:profit|income)\b[^.?!]{0,30}\bgrow(?:th|ing)\b",
        r"\beps\b[^.?!]{0,20}\bgrow(?:th|ing)\b",
    ),
    FinancialIntent.REVENUE_GROWTH: (
        r"\brevenue\b[^.?!]{0,30}\bgrow(?:th|ing)\b",
        r"\bsales\b[^.?!]{0,30}\bgrow(?:th|ing)\b",
        r"\bturnover\b[^.?!]{0,30}\bgrow(?:th|ing)\b",
        r"\btop ?line\b[^.?!]{0,30}\bgrow(?:th|ing)\b",
    ),
    FinancialIntent.EPS: (
        r"\beps\b(?!\s*(?:grow(?:th|ing)|cagr))",
        r"\bearnings per share\b",
        r"\bper share earnings\b",
    ),
    FinancialIntent.VALUATION: (
        r"\bvaluation\b",
        r"\bintrinsic value\b",
        r"\bfair value\b",
        r"\bdcf\b",
        r"\bdiscounted cash flow\b",
        r"\bovervalued\b",
        r"\bundervalued\b",
        r"\boverrated\b",
        r"\bunderrated\b",
        r"\btarget price\b",
        r"\bprice target\b",
        r"\bvalued\b",
        r"\bhow to value\b",
        r"\bis it worth\b",
        r"\bwhat is it worth\b",
        r"\bhow much is (?:it|the stock|the share) worth\b",
    ),
    # ------------------------------------------------------------------
    # Phase 2A. The phrases are written for the text the resolver actually
    # sees: raw English, or — for a non-English question — the Language
    # Adapter's normalised English form, where Hindi function words are
    # dropped ("ki", "hai") and a few are mapped ("kaisi" → "how"). The
    # raw-Hinglish alternatives cover requests that reach the resolver
    # unnormalised (no language requested at all).
    # ------------------------------------------------------------------
    FinancialIntent.OVERALL_ASSESSMENT: (
        r"\boverall assessment\b",
        r"\boverall\b",
        r"\bcompany overall\b",
        r"\bhow is\b[^.?!]{0,12}\bcompany\b",
        r"\bhow company\b",
        r"\bkind of company\b",
        r"\bwhat company\b",
        r"\bkaisi company\b",
        r"\bkya company hai\b",
        r"\bkaisi lagti hai\b",
    ),
    FinancialIntent.FINANCIAL_QUALITY: (
        r"\bfinancial quality\b",
    ),
    FinancialIntent.GROWTH_QUALITY: (
        r"\bgrowth quality\b",
        # A bare "growth" question is about the growth-quality category. The
        # lookahead keeps "growth in revenue"-style phrasing out: that names a
        # specific metric, and if it matches no intent the provider path
        # answers it in full rather than the engine answering a different
        # (albeit related) question.
        r"\bgrowth\b(?!\s+(?:in|of|for)\s+(?:revenue|sales|profit|pat\b|"
        r"earnings|top ?line|turnover))",
    ),
    FinancialIntent.FINANCIAL_RISK: (
        r"\bfinancial risk\b",
        r"\bbalance sheet risk\b",
    ),
    FinancialIntent.STRENGTHS: (
        r"\bstrengths?\b",
        r"\bstrongest\b",
    ),
    FinancialIntent.WEAKNESSES: (
        r"\bweaknesses?\b",
        r"\bweakest\b",
    ),
    FinancialIntent.INVESTMENT_CASE: (
        r"\binvestment case\b",
        r"\bcase for (?:investing|buying)\b",
        r"\bcase to invest\b",
        # "invest karne ka case" survives normalisation as "invest karne case".
        r"\binvest\w*\b[^.?!]{0,25}\bcase\b",
        r"\bpositives?\b[^.?!]{0,20}\b(?:risks?|negatives?|cons)\b",
    ),
    FinancialIntent.RECOMMENDATION: (
        r"\brecommend(?:ation|ed)?\b",
        r"\bbuy\b",
        r"\bhold\b",
        r"\bsell\b",
        r"\bsold\b",
    ),
}

_COMPILED: dict[FinancialIntent, tuple[re.Pattern[str], ...]] = {
    intent: tuple(re.compile(p) for p in patterns)
    for intent, patterns in _PATTERNS.items()
}

#: Specificity overrides, applied after matching and before the exactly-one
#: rule. "Profit growth" and "revenue growth" are the Phase 1 growth intents;
#: the bare word "growth" also matches them, and on its own it means the
#: growth-QUALITY category. When a specific growth intent and the general
#: one both fire on one question, the question is about that specific growth
#: — so the general intent steps aside, the match is no longer multi-intent,
#: and Phase 1 keeps answering "profit growth" exactly as before.
_SPECIFICITY: dict[FinancialIntent, tuple[FinancialIntent, ...]] = {
    FinancialIntent.GROWTH_QUALITY: (
        FinancialIntent.PROFIT_GROWTH,
        FinancialIntent.REVENUE_GROWTH,
    ),
}


# ---------------------------------------------------------------------------
# Part 2B: the shared vocabulary surface.
#
# The Question Planner (app/services/ai/planner/) needs the same intent
# patterns this resolver matches on. It does NOT get a second copy: a copy
# would drift, and a drifted planner would produce plans that disagree with
# the engine that has to execute them — the one failure mode this layering
# cannot tolerate.
#
# These are read-only VIEWS over the mappings above, not copies, so editing a
# pattern here changes the resolver and the planner together. Both are exposed
# as mapping proxies because the resolver's behaviour is the contract: the
# planner may read the vocabulary, never rewrite it.
#
# Nothing about `FinancialIntentResolver.resolve()` changes. It still applies
# the exactly-one rule on top of these same patterns, and the planner is a
# separate, planning-only matcher that is allowed to return several intents.
# ---------------------------------------------------------------------------
INTENT_PATTERNS: Mapping[FinancialIntent, tuple[str, ...]] = MappingProxyType(_PATTERNS)

#: The specificity overrides, exposed for the same reason.
SPECIFICITY_OVERRIDES: Mapping[FinancialIntent, tuple[FinancialIntent, ...]] = (
    MappingProxyType(_SPECIFICITY)
)


class FinancialIntentResolver:
    """Detects at most one canonical financial intent per question."""

    def resolve(self, question: str) -> FinancialIntent | None:
        """Return the single intent the question asks for, or ``None``.

        ``None`` is the honest answer for three different situations and all
        three must fall through to the provider path unchanged:

        * the question contains no supported financial intent;
        * the question contains several (a deterministic answer would then be
          a partial answer to a multi-part question, which is worse than no
          deterministic answer);
        * the question is empty.
        """
        if not question:
            return None
        text = " ".join(question.lower().split())

        matched = [
            intent
            for intent, patterns in _COMPILED.items()
            if any(p.search(text) for p in patterns)
        ]
        for general, specific in _SPECIFICITY.items():
            if general in matched and any(s in matched for s in specific):
                matched.remove(general)
        if len(matched) != 1:
            return None
        return matched[0]
