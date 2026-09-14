"""Intent resolution for the Phase 2A investment-intelligence intents.

The resolver sees raw English, or — for a non-English question — the
Language Adapter's normalised English form (Hindi function words dropped,
a few terms mapped). The Hinglish/Hindi cases below therefore run the REAL
adapter first, exactly as the analyst does, and then resolve; the raw forms
cover callers that reach the resolver unnormalised.

Pinned:
* each of the eight intents is recognised in English, Hindi and Hinglish;
* the Phase 1 intents are unaffected — including the "profit growth" /
  "revenue growth" questions, which must keep resolving to the specific
  Phase 1 growth intent even though the word "growth" also matches the
  general growth-quality intent;
* more than one matched intent — Phase 1 plus Phase 1, Phase 2A plus
  Phase 2A, or either mixed with the other — resolves to None and falls
  through to the provider path;
* ambiguous questions ("growth in revenue") decline rather than guess.
"""
from __future__ import annotations

import pytest

from app.services.ai.financial_intent import (
    INVESTMENT_INTENTS, FinancialIntent, FinancialIntentResolver,
)
from app.services.language.adapter import LanguageAdapter

RESOLVER = FinancialIntentResolver()
ADAPTER = LanguageAdapter()


def resolved(question: str) -> FinancialIntent | None:
    """Resolve the way the analyst does: normalise first when the question
    is not canonical English, then run the intent resolver."""
    return RESOLVER.resolve(ADAPTER.normalise_query(question).english)


# ---------------------------------------------------------------------------
# The eight Phase 2A intents — positive matches
# ---------------------------------------------------------------------------
class TestPhase2APositives:
    @pytest.mark.parametrize("question,intent", [
        # overall_assessment — English
        ("What is the overall assessment?", FinancialIntent.OVERALL_ASSESSMENT),
        ("How is the company?", FinancialIntent.OVERALL_ASSESSMENT),
        ("What kind of company is this?", FinancialIntent.OVERALL_ASSESSMENT),
        # overall_assessment — Hinglish / Hindi (as the user types them)
        ("BEL kaisi company hai?", FinancialIntent.OVERALL_ASSESSMENT),
        ("BEL ka overall assessment kya hai?", FinancialIntent.OVERALL_ASSESSMENT),
        ("Company overall kaisi lagti hai?", FinancialIntent.OVERALL_ASSESSMENT),
        ("BEL kya company hai?", FinancialIntent.OVERALL_ASSESSMENT),
        # financial_quality
        ("What is the financial quality score?", FinancialIntent.FINANCIAL_QUALITY),
        ("BEL ki financial quality kaisi hai?", FinancialIntent.FINANCIAL_QUALITY),
        # growth_quality
        ("What is the growth quality score?", FinancialIntent.GROWTH_QUALITY),
        ("BEL ki growth kaisi hai?", FinancialIntent.GROWTH_QUALITY),
        # financial_risk
        ("How much financial risk is there?", FinancialIntent.FINANCIAL_RISK),
        ("BEL me financial risk kitna hai?", FinancialIntent.FINANCIAL_RISK),
        ("What is the balance sheet risk?", FinancialIntent.FINANCIAL_RISK),
        # strengths
        ("What are the company's strengths?", FinancialIntent.STRENGTHS),
        ("BEL ki strengths kya hain?", FinancialIntent.STRENGTHS),
        ("What are the strongest areas?", FinancialIntent.STRENGTHS),
        # weaknesses
        ("What are the company's weaknesses?", FinancialIntent.WEAKNESSES),
        ("BEL ki weaknesses kya hain?", FinancialIntent.WEAKNESSES),
        ("What are the weakest areas?", FinancialIntent.WEAKNESSES),
        # investment_case
        ("What is the investment case?", FinancialIntent.INVESTMENT_CASE),
        ("BEL me invest karne ka case kya hai?", FinancialIntent.INVESTMENT_CASE),
        ("What are the positives and risks?", FinancialIntent.INVESTMENT_CASE),
        ("Company ke positives aur risks kya hain?", FinancialIntent.INVESTMENT_CASE),
        # recommendation
        ("What is the investment recommendation?", FinancialIntent.RECOMMENDATION),
        ("BEL buy hai ya hold?", FinancialIntent.RECOMMENDATION),
        ("BEL me kya recommendation hai?", FinancialIntent.RECOMMENDATION),
        ("Should the shares be sold?", FinancialIntent.RECOMMENDATION),
    ])
    def test_single_intent_recognised(self, question, intent):
        assert intent in INVESTMENT_INTENTS
        assert resolved(question) is intent

    def test_eight_distinct_intents(self):
        assert len(INVESTMENT_INTENTS) == 8
        assert INVESTMENT_INTENTS.isdisjoint({
            FinancialIntent.VALUATION, FinancialIntent.PE, FinancialIntent.PB,
            FinancialIntent.DEBT, FinancialIntent.ROE, FinancialIntent.ROCE,
            FinancialIntent.PROFIT_GROWTH, FinancialIntent.REVENUE_GROWTH,
            FinancialIntent.EPS, FinancialIntent.MARKET_PRICE,
        })


# ---------------------------------------------------------------------------
# Phase 1 behaviour is preserved, including growth consolidation
# ---------------------------------------------------------------------------
class TestPhase1Unaffected:
    @pytest.mark.parametrize("question,intent", [
        ("What is the current P/E?", FinancialIntent.PE),
        ("What is the total debt?", FinancialIntent.DEBT),
        ("What is the ROE?", FinancialIntent.ROE),
        ("What is the valuation?", FinancialIntent.VALUATION),
        ("How to value the company?", FinancialIntent.VALUATION),
        # The word "growth" also matches the general intent; the SPECIFIC
        # Phase 1 growth intent must still win, so these stay deterministic
        # Phase 1 answers exactly as before Phase 2A existed.
        ("What is the profit growth?", FinancialIntent.PROFIT_GROWTH),
        ("How fast is profit growing?", FinancialIntent.PROFIT_GROWTH),
        ("What is the EPS growth?", FinancialIntent.PROFIT_GROWTH),
        ("What is the revenue growth?", FinancialIntent.REVENUE_GROWTH),
        ("How is sales growth looking?", FinancialIntent.REVENUE_GROWTH),
    ])
    def test_phase1_intents_resolve_as_before(self, question, intent):
        assert intent not in INVESTMENT_INTENTS
        assert resolved(question) is intent

    @pytest.mark.parametrize("question", [
        "What are the main risks for the company?",
        "How is the business doing?",
        "Reliance ka business kaisa hai?",
        "Tell me about the company's business model.",
        "BEL buyback kya hai?",
        "BEL ke holdings kya hain?",
        "Who is the CEO?",
    ])
    def test_unsupported_stays_unsupported(self, question):
        assert resolved(question) is None

    def test_growth_in_revenue_declines(self):
        # "growth in revenue" names a metric, not the growth-quality
        # category: better to fall through than answer a different question.
        assert resolved("growth in revenue kya hai?") is None
        assert resolved("What is the growth of profit?") is None


# ---------------------------------------------------------------------------
# Multi-intent rejection — no partial deterministic answers
# ---------------------------------------------------------------------------
class TestMultiIntentRejection:
    @pytest.mark.parametrize("question", [
        # Phase 2A + Phase 2A
        "BEL ki financial quality aur growth kaisi hai?",
        "What is the financial quality and the growth quality?",
        "BEL ki strengths aur weaknesses kya hain?",
        "What is the recommendation and the overall assessment?",
        # Phase 1 + Phase 1 (regression)
        "What is the P/E and the ROE?",
        "BEL ka P/E aur ROE kya hai?",
        # Phase 1 + Phase 2A — mixing families is multi-intent too
        "BEL ka P/E aur overall assessment kya hai?",
        "What is the debt and the financial risk?",
        "What is the valuation and the investment case?",
        # Phase 2A inside a growth mix: the specific growth wins over the
        # general one, leaving a genuine two-intent question.
        "What is the profit growth and the financial quality?",
    ])
    def test_more_than_one_intent_falls_through(self, question):
        assert resolved(question) is None

    def test_empty_question(self):
        assert resolved("") is None
        assert resolved("   ") is None
