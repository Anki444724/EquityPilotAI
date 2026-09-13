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
"""
from __future__ import annotations

import re
from enum import StrEnum


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
}

_COMPILED: dict[FinancialIntent, tuple[re.Pattern[str], ...]] = {
    intent: tuple(re.compile(p) for p in patterns)
    for intent, patterns in _PATTERNS.items()
}


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
        if len(matched) != 1:
            return None
        return matched[0]
