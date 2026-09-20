"""Part 2B — the Internal Question Planner.

The planner runs in shadow mode, so these tests are the only thing
currently standing between a correct plan and an unnoticed regression. They
are therefore written as behavioural pins rather than as coverage: each
class below exists because of something that could silently go wrong.

The suite deliberately never touches the database. The company resolver is
injected, which is what makes the entity tests possible without a session
and what keeps the planner honest about never resolving a company itself.

What is pinned:

* every intent the platform supports, in English, Hindi, Hinglish and
  Roman Hindi;
* multi-intent detection, including the order the intents were asked in;
* entity resolution through ``CompanyService.named_in``'s contract — one
  candidate resolves, two do not, and none is never guessed;
* the five query types that exist to stop the planner from guessing:
* evidence requirements, each of which names a key the ContextBuilder
  actually publishes;
* the five false positives that would otherwise make the planner actively
  wrong rather than merely unhelpful;
* and that ``revenue`` / ``net_profit`` are deferred, not invented.
"""
from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass

import pytest

from app.services.ai.financial_intent import (
    INVESTMENT_INTENTS, FinancialIntent,
)
from app.services.ai.memory import ConversationMemory
from app.services.ai.planner import (
    Confidence, EntityStatus, ExecutionRoute, QueryType, QuestionPlanner,
)
from app.services.ai.planner.evidence import REQUIRED_EVIDENCE, evidence_for_all
from app.services.ai.planner.types import IntentFamily, QuestionPlan


# ---------------------------------------------------------------------------
# A stand-in company and resolver
#
# Duck-typed on purpose: the planner reads id/ticker/name and nothing else,
# so the tests assert that contract rather than a SQLAlchemy model.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class FakeCompany:
    id: str
    ticker: str
    name: str


UNIVERSE = (
    FakeCompany("c-rely", "RELIANCE", "Reliance Industries Limited"),
    FakeCompany("c-tcs", "TCS", "Tata Consultancy Services Limited"),
    FakeCompany("c-bel", "BEL", "Bharat Electronics Limited"),
)


def make_resolver(*tickers: str):
    """A resolver over the given subset — stands in for ``named_in``."""
    wanted = {t.upper() for t in tickers}

    def resolve(text: str):
        lowered = (text or "").lower()
        return [
            c for c in UNIVERSE
            if c.ticker.upper() in wanted
            and (c.ticker.lower() in lowered or c.name.lower() in lowered)
        ]

    return resolve


@pytest.fixture()
def planner():
    """A planner that can resolve Reliance and TCS, with no memory."""
    return QuestionPlanner(company_resolver=make_resolver("RELIANCE", "TCS"))


@pytest.fixture()
def memory():
    mem = ConversationMemory("test-session")
    mem.set_company("c-rely", "RELIANCE", "Reliance Industries Limited")
    return mem


def intents_of(plan) -> list[str]:
    return [m.intent.value for m in plan.intents]


# ===========================================================================
class TestEnglishIntentDetection:
    """Every intent the platform supports, asked for in plain English."""

    CASES = [
        ("What is the P/E?", "pe"),
        ("What is the P/B?", "pb"),
        ("What is the EPS?", "eps"),
        ("What is the debt?", "debt"),
        ("What is the ROE?", "roe"),
        ("What is the ROCE?", "roce"),
        ("What is the market price?", "market_price"),
        ("What is the intrinsic value?", "valuation"),
        ("What is the revenue growth?", "revenue_growth"),
        ("What is the profit growth?", "profit_growth"),
        ("How is the overall assessment?", "overall_assessment"),
        ("What is the financial quality?", "financial_quality"),
        ("What is the growth quality?", "growth_quality"),
        ("What is the financial risk?", "financial_risk"),
        ("What are the strengths?", "strengths"),
        ("What are the weaknesses?", "weaknesses"),
        ("What is the investment case?", "investment_case"),
        ("What is the investment recommendation?", "recommendation"),
    ]

    @pytest.mark.parametrize("question,intent", CASES)
    def test_single_intent_recognised(self, planner, question, intent):
        plan = planner.plan(question)
        assert intents_of(plan) == [intent]

    def test_every_supported_intent_is_covered(self):
        """No intent is missing from the English matrix above."""
        covered = {intent for _, intent in self.CASES}
        assert covered == {i.value for i in FinancialIntent}

    @pytest.mark.parametrize("question,intent", CASES)
    def test_single_intent_is_deterministic(self, planner, question, intent):
        plan = planner.plan(question)
        assert plan.query_type is QueryType.DETERMINISTIC
        assert plan.execution_route in {
            ExecutionRoute.DETERMINISTIC_FINANCIAL,
            ExecutionRoute.DETERMINISTIC_INVESTMENT,
        }


# ===========================================================================
class TestHindiAndHinglish:
    """The same understanding in Devanagari, Hinglish and Roman Hindi."""

    CASES = [
        # Hinglish — English financial vocabulary, Hindi grammar.
        ("Reliance ki financial quality kaisi hai?", "financial_quality"),
        ("Reliance ki growth kaisi hai?", "growth_quality"),
        ("Reliance me kya recommendation hai?", "recommendation"),
        ("Reliance financially strong hai?", "financial_quality"),
        ("Reliance ki financial health kaisi hai?", "financial_quality"),
        ("Reliance ka karz kitna hai?", "debt"),
        ("Reliance ka P/E kitna hai?", "pe"),
        ("Growth quality batao", "growth_quality"),
        # Devanagari.
        ("रिलायंस की वित्तीय गुणवत्ता कैसी है?", "financial_quality"),
        ("रिलायंस की वित्तीय जोखिम क्या है?", "financial_risk"),
        ("रिलायंस की ताकत क्या है?", "strengths"),
        ("रिलायंस की कमजोरी क्या है?", "weaknesses"),
        # Roman Hindi.
        ("Reliance mehnga hai kya?", "valuation"),
    ]

    @pytest.mark.parametrize("question,intent", CASES)
    def test_recognised(self, planner, question, intent):
        assert intents_of(planner.plan(question)) == [intent]

    def test_roman_hindi_and_hinglish_agree(self, planner):
        """'karz' and 'debt' are the same question."""
        a = planner.plan("Reliance ka karz kitna hai?")
        b = planner.plan("Reliance ka debt kitna hai?")
        assert intents_of(a) == intents_of(b) == ["debt"]


# ===========================================================================
class TestCrossLanguageEquivalence:
    """The same question in three scripts produces the same plan semantics."""

    TRIPLES = [
        (
            "What is Reliance's financial quality?",
            "Reliance ki financial quality kaisi hai?",
            "रिलायंस की financial quality कैसी है?",
        ),
        (
            "What is Reliance's net profit?",
            "Reliance ka profit kitna hai?",
            "रिलायंस का मुनाफा कितना है?",
        ),
    ]

    @pytest.mark.parametrize("english,hinglish,hindi", TRIPLES)
    def test_plans_agree_on_everything_but_language(
        self, planner, english, hinglish, hindi,
    ):
        plans = [planner.plan(q) for q in (english, hinglish, hindi)]
        signatures = [
            (
                tuple(m.intent.value for m in p.intents),
                p.query_type, p.execution_route, p.evidence_keys(),
            )
            for p in plans
        ]
        assert signatures[0] == signatures[1] == signatures[2]

    def test_language_is_still_recorded_per_question(self, planner):
        english = planner.plan("What is Reliance's financial quality?")
        hinglish = planner.plan("Reliance ki financial quality kaisi hai?")
        assert english.language is not hinglish.language
        assert english.original_question != hinglish.original_question


# ===========================================================================
class TestMultiIntent:
    """Several intents in one question, in the order they were asked."""

    def test_two_investment_intents(self, planner):
        plan = planner.plan("Reliance ki financial quality aur growth kaisi hai?")
        assert intents_of(plan) == ["financial_quality", "growth_quality"]
        assert plan.query_type is QueryType.MULTI_INTENT
        assert plan.execution_route is ExecutionRoute.COMPOSITION_REQUIRED

    def test_three_intents_keep_the_order_asked(self, planner):
        plan = planner.plan("Company ki strengths, weaknesses aur recommendation kya hai?")
        assert intents_of(plan) == ["strengths", "weaknesses", "recommendation"]
        positions = [m.position for m in plan.intents]
        assert positions == sorted(positions)

    def test_reversed_order_is_preserved(self, planner):
        plan = planner.plan("Company ki recommendation, weaknesses aur strengths batao")
        assert intents_of(plan) == ["recommendation", "weaknesses", "strengths"]

    def test_mixed_phase1_intents(self, planner):
        plan = planner.plan("What is the P/E and the ROE?")
        assert intents_of(plan) == ["pe", "roe"]

    def test_multiple_intents_are_not_executable_by_one_engine(self, planner):
        """A multi-intent plan must not claim a deterministic route."""
        plan = planner.plan("Reliance ki financial quality aur growth kaisi hai?")
        assert not plan.is_executable

    def test_multi_intent_carries_union_evidence(self, planner):
        plan = planner.plan("Reliance ki financial quality aur growth kaisi hai?")
        keys = set(plan.evidence_keys())
        assert {"score_financial_quality", "score_growth_quality"} <= keys


# ===========================================================================
class TestEntityResolution:
    """The planner resolves through ``named_in`` and never invents."""

    def test_named_company_resolves(self, planner):
        plan = planner.plan("Reliance ki financial quality kaisi hai?")
        assert plan.entity.status is EntityStatus.RESOLVED
        assert plan.entity.ticker == "RELIANCE"
        assert plan.entity.company_id == "c-rely"

    def test_two_companies_named_is_ambiguous(self):
        planner = QuestionPlanner(company_resolver=make_resolver("RELIANCE", "TCS"))
        plan = planner.plan("Compare Reliance and TCS")
        assert plan.entity.status is EntityStatus.AMBIGUOUS
        assert set(plan.entity.candidates) == {"RELIANCE", "TCS"}
        # Ambiguity is reported, never resolved by picking one.
        assert any("more than one company" in a for a in plan.ambiguity)

    def test_no_company_and_no_memory_is_unresolved(self):
        plan = QuestionPlanner().plan("What is the financial quality?")
        assert plan.entity.status is EntityStatus.UNRESOLVED
        assert plan.entity.ticker is None
        assert any(m.startswith("entity:") for m in plan.missing_requirements)

    def test_conversation_context_is_used_and_marked_weaker(self, memory):
        planner = QuestionPlanner(company_resolver=make_resolver("RELIANCE"), memory=memory)
        plan = planner.plan("What is the financial quality?")
        assert plan.entity.status is EntityStatus.CONTEXT_ONLY
        assert plan.entity.ticker == "RELIANCE"
        assert any("conversation" in a for a in plan.ambiguity)

    def test_no_resolver_never_invents_a_company(self):
        """Without a resolver there is simply no entity — not a guess."""
        plan = QuestionPlanner().plan("Reliance ki financial quality kaisi hai?")
        assert plan.entity.status is EntityStatus.UNRESOLVED
        assert plan.entity.company_id is None
        assert plan.entity.ticker is None
        # The intent is still understood: the plan describes what was asked
        # even when it cannot say who it was asked about.
        assert intents_of(plan) == ["financial_quality"]

    def test_company_is_never_created(self):
        """The resolver is called exactly once, read-only."""
        calls = []

        def spy(text):
            calls.append(text)
            return []

        QuestionPlanner(company_resolver=spy).plan("Reliance ki financial quality?")
        assert calls == ["Reliance ki financial quality?"]

    def test_resolver_failure_is_reported_not_guessed(self):
        def broken(_text):
            raise RuntimeError("database unavailable")

        plan = QuestionPlanner(company_resolver=broken).plan("Reliance ki financial quality?")
        assert plan.entity.status is EntityStatus.UNRESOLVED
        assert any("resolver failed" in n for n in plan.notes)


# ===========================================================================
class TestQueryClassification:
    """The types that exist so the planner can decline instead of guess."""

    def test_comparison_is_not_forced_into_one_company(self, planner):
        plan = planner.plan("Compare Reliance and TCS")
        assert plan.query_type is QueryType.COMPARISON
        # No single-company deterministic intent was invented for it.
        assert plan.intents == ()
        assert plan.execution_route is ExecutionRoute.INTERNAL_REASONING
        assert not plan.is_executable

    def test_comparison_is_detected_without_the_word_compare(self):
        plan = QuestionPlanner().plan("Reliance is better than TCS, should I switch?")
        assert plan.query_type is QueryType.COMPARISON

    def test_unsupported_financial_question_declines(self, planner):
        plan = planner.plan("What is the tax rate on capital gains for Reliance?")
        assert plan.query_type is QueryType.UNSUPPORTED
        assert plan.execution_route is ExecutionRoute.DECLINE
        assert plan.confidence is Confidence.NONE

    def test_open_ended_routes_to_reasoning(self, planner):
        plan = planner.plan("Tell me about Reliance")
        assert plan.query_type is QueryType.OPEN_ENDED
        assert plan.execution_route is ExecutionRoute.INTERNAL_REASONING

    def test_ambiguous_evaluative_question_declines(self, planner):
        plan = planner.plan("Good company hai?")
        assert plan.query_type is QueryType.AMBIGUOUS
        assert plan.execution_route is ExecutionRoute.DECLINE
        assert plan.confidence is Confidence.LOW
        # It explains itself instead of silently choosing an intent.
        assert any("overall_assessment" in a for a in plan.ambiguity)
        assert any(m.startswith("disambiguation:") for m in plan.missing_requirements)

    def test_empty_question_is_unsupported(self, planner):
        plan = planner.plan("")
        assert plan.query_type is QueryType.UNSUPPORTED
        assert plan.confidence is Confidence.NONE
        assert plan.missing_requirements

    @pytest.mark.parametrize("question", [
        "only uploaded documents me debt kya hai?",
        "use only the financial database and tell me the ROE",
    ])
    def test_source_directed(self, planner, question):
        plan = planner.plan(question)
        assert plan.query_type is QueryType.SOURCE_DIRECTED
        assert plan.execution_route is ExecutionRoute.SOURCE_ROUTER
        assert plan.source_directive is not None
        assert plan.source_directive.scope.is_restricted

    def test_source_restriction_outranks_everything_else(self, planner):
        """Provenance outranks intent: a restriction is a hard constraint."""
        plan = planner.plan("only uploaded documents me financial quality aur growth batao")
        assert plan.query_type is QueryType.SOURCE_DIRECTED
        assert plan.execution_route is ExecutionRoute.SOURCE_ROUTER

    def test_unrestricted_question_has_a_hybrid_directive(self, planner):
        plan = planner.plan("What is the ROE?")
        assert plan.source_directive is not None
        assert not plan.source_directive.scope.is_restricted


# ===========================================================================
class TestFalsePositives:
    """The cases where a naive matcher is not merely unhelpful, but wrong."""

    def test_hindi_postposition_pe_is_not_the_pe_ratio(self, planner):
        plan = planner.plan("Reliance pe kya bolte ho?")
        assert "pe" not in intents_of(plan)

    def test_real_pe_question_still_matches(self, planner):
        """The guard must not suppress the intent it protects."""
        assert intents_of(planner.plan("Reliance ka P/E kitna hai?")) == ["pe"]
        assert intents_of(planner.plan("P/E zyada hai kya?")) == ["pe"]

    def test_sell_software_is_not_a_sell_recommendation(self, planner):
        assert "recommendation" not in intents_of(
            planner.plan("Does the company sell software?")
        )

    def test_buyback_is_not_a_buy_recommendation(self, planner):
        assert "recommendation" not in intents_of(
            planner.plan("Company ka buyback hua?")
        )

    def test_holdings_is_not_a_hold_recommendation(self, planner):
        assert "recommendation" not in intents_of(
            planner.plan("What are the promoter holdings?")
        )

    def test_genuine_recommendation_questions_still_match(self, planner):
        for question in ("Should I buy this stock?", "Should I hold or sell?",
                         "Reliance me kya recommendation hai?"):
            assert intents_of(planner.plan(question)) == ["recommendation"]

    def test_financially_strong_is_quality_not_risk(self, planner):
        """One question, one intent — not quality plus risk."""
        assert intents_of(planner.plan("Reliance financially strong hai?")) == [
            "financial_quality"
        ]

    def test_debt_risk_is_risk_not_debt(self, planner):
        """The risk phrase absorbs the debt word inside its own span."""
        assert intents_of(planner.plan("debt risk kya hai?")) == ["financial_risk"]

    def test_asking_for_both_still_yields_both(self, planner):
        """Genuine multi-intent is preserved; suppression is not blanket."""
        plan = planner.plan("Reliance ki financial health aur debt risk dono batao")
        assert set(intents_of(plan)) == {"financial_quality", "financial_risk"}

    def test_revenue_growth_is_not_also_generic_growth(self, planner):
        assert intents_of(planner.plan("What is the revenue growth?")) == [
            "revenue_growth"
        ]

    def test_profit_growth_is_not_also_generic_growth(self, planner):
        assert intents_of(planner.plan("What is the profit growth?")) == [
            "profit_growth"
        ]

    def test_bare_growth_still_means_growth_quality(self, planner):
        assert intents_of(planner.plan("Reliance ki growth kaisi hai?")) == [
            "growth_quality"
        ]

    def test_expensive_is_valuation_not_recommendation(self, planner):
        """A single adjective about price must not become a BUY/SELL call."""
        plan = planner.plan("Is stock expensive?")
        assert intents_of(plan) == ["valuation"]
        assert plan.execution_route is ExecutionRoute.DETERMINISTIC_FINANCIAL


# ===========================================================================
class TestRequiredEvidence:
    """Evidence is described, never computed — and never invented."""

    def test_pe_evidence(self, planner):
        assert plan_keys(planner, "What is the P/E?") == {
            "pe_ratio", "price", "eps",
        }  # includes the two supporting keys

    def test_pb_is_required_but_flagged_unpublished(self, planner):
        plan = planner.plan("What is the P/B?")
        req = next(e for e in plan.required_evidence if e.key == "pb")
        assert req.required is True
        assert req.published is False
        # The gap is stated rather than papered over.
        assert any("pb" in m for m in plan.missing_requirements)

    @pytest.mark.parametrize("question,expected", [
        ("What is the EPS?", {"eps"}),
        ("What is the debt?", {"gross_debt", "net_debt", "net_debt_ebitda"}),
        ("What is the ROE?", {"roe_avg"}),
        ("What is the ROCE?", {"roce"}),
        ("What is the market price?", {"price", "market_cap"}),
        ("What is the revenue growth?", {"revenue_growth", "revenue_history"}),
        ("What is the profit growth?", {"pat_growth"}),
        ("What is the financial quality?",
         {"score_financial_quality", "roe_avg", "ebitda_margin", "pat_margin"}),
        ("What is the growth quality?",
         {"score_growth_quality", "revenue_growth", "pat_growth",
          "forecast_revenue_cagr"}),
        ("What is the financial risk?",
         {"score_financial_risk", "net_debt_ebitda", "interest_coverage",
          "current_ratio", "altman_z", "gross_debt", "net_debt"}),
        ("What are the strengths?", {"score.strongest", "score_*"}),
        ("What are the weaknesses?", {"score.weakest", "score_*"}),
        ("What is the investment recommendation?",
         {"recommendation", "overall_score", "grade", "confidence",
          "score.recommendation_rationale"}),
    ])
    def test_evidence_mapping(self, planner, question, expected):
        assert expected <= plan_keys(planner, question)

    def test_valuation_evidence(self, planner):
        keys = plan_keys(planner, "What is the intrinsic value?")
        assert {"weighted_value", "valuation_upside", "dcf_value", "dcf_upside",
                "pe_ratio", "ev_ebitda", "relative_target", "wacc",
                "cost_of_equity", "terminal_value_pct",
                "valuation_recommendation", "data_quality"} <= keys

    def test_every_intent_has_evidence(self):
        assert set(REQUIRED_EVIDENCE) == set(FinancialIntent)

    def test_evidence_carries_no_values(self, planner):
        """A requirement is metadata; the numbers come later, or never."""
        plan = planner.plan("What is the financial quality?")
        for req in plan.required_evidence:
            for field in dataclasses.fields(req):
                value = getattr(req, field.name)
                # bool is a subclass of int: `required`/`published` are
                # flags, not figures. Only real numbers are banned.
                assert isinstance(value, bool) or not isinstance(
                    value, (int, float)
                ), field.name

    def test_union_deduplicates_and_keeps_the_stricter_requirement(self):
        merged = evidence_for_all((FinancialIntent.PE, FinancialIntent.VALUATION))
        keys = [e.key for e in merged]
        assert len(keys) == len(set(keys))
        pe_ratio = next(e for e in merged if e.key == "pe_ratio")
        # Supporting in one intent, required in the other: required wins.
        assert pe_ratio.required is True


def plan_keys(planner, question) -> set:
    """All evidence keys, including the supporting (optional) ones."""
    return set(planner.plan(question).evidence_keys(required_only=False))


# ===========================================================================
class TestExecutionRoute:
    """The route is named, never taken."""

    @pytest.mark.parametrize("question,route", [
        ("What is the P/E?", ExecutionRoute.DETERMINISTIC_FINANCIAL),
        ("What is the debt?", ExecutionRoute.DETERMINISTIC_FINANCIAL),
        ("What is the intrinsic value?", ExecutionRoute.DETERMINISTIC_FINANCIAL),
        ("What is the financial quality?", ExecutionRoute.DETERMINISTIC_INVESTMENT),
        ("What is the growth quality?", ExecutionRoute.DETERMINISTIC_INVESTMENT),
        ("What is the financial risk?", ExecutionRoute.DETERMINISTIC_INVESTMENT),
        ("What are the strengths?", ExecutionRoute.DETERMINISTIC_INVESTMENT),
        ("What is the investment case?", ExecutionRoute.DETERMINISTIC_INVESTMENT),
    ])
    def test_route_by_family(self, planner, question, route):
        assert planner.plan(question).execution_route is route

    def test_phase1_intents_route_to_the_financial_engine(self, planner):
        for question in ("What is the P/E?", "What is the ROE?", "What is the EPS?"):
            plan = planner.plan(question)
            assert plan.intents[0].family is IntentFamily.PHASE1
            assert plan.execution_route is ExecutionRoute.DETERMINISTIC_FINANCIAL

    def test_investment_intents_route_to_the_investment_engine(self, planner):
        for question in ("What is the financial quality?", "What are the strengths?"):
            plan = planner.plan(question)
            assert plan.intents[0].family is IntentFamily.INVESTMENT
            assert plan.execution_route is ExecutionRoute.DETERMINISTIC_INVESTMENT

    def test_family_membership_matches_the_existing_registry(self):
        """The planner's family split is ``INVESTMENT_INTENTS``, not opinion."""
        from app.services.ai.planner.vocabulary import INTENT_VOCABULARY
        for intent, spec in INTENT_VOCABULARY.items():
            expected = (
                IntentFamily.INVESTMENT if intent in INVESTMENT_INTENTS
                else IntentFamily.PHASE1
            )
            assert spec.family is expected


# ===========================================================================
class TestConfidence:
    def test_high_for_a_clear_question_about_a_known_company(self, planner):
        plan = planner.plan("Reliance ki financial quality kaisi hai?")
        assert plan.confidence is Confidence.HIGH

    def test_lower_when_no_company_was_resolved(self):
        planner = QuestionPlanner(company_resolver=make_resolver("RELIANCE"))
        plan = planner.plan("What is the financial quality?")
        assert plan.entity.status is EntityStatus.UNRESOLVED
        assert plan.confidence is Confidence.MEDIUM

    def test_medium_for_multi_intent(self, planner):
        plan = planner.plan("Reliance ki financial quality aur growth kaisi hai?")
        assert plan.confidence is Confidence.MEDIUM

    def test_none_for_unsupported(self, planner):
        assert planner.plan("What is the tax rate?").confidence is Confidence.NONE

    def test_low_for_ambiguous_and_comparison(self, planner):
        assert planner.plan("Good company hai?").confidence is Confidence.LOW
        assert planner.plan("Compare Reliance and TCS").confidence is Confidence.LOW


# ===========================================================================
class TestRevenueAndNetProfitAreDeferred:
    """Part 2C's job. The planner must not quietly invent these intents."""

    def test_they_are_not_intent_members(self):
        values = {i.value for i in FinancialIntent}
        assert "revenue" not in values
        assert "net_profit" not in values

    @pytest.mark.parametrize("question", [
        "Reliance ka profit kitna hai?",
        "What is Reliance's net profit?",
        "रिलायंस का मुनाफा कितना है?",
        "What is the revenue of Reliance?",
    ])
    def test_no_intent_is_invented(self, planner, question):
        plan = planner.plan(question)
        assert plan.intents == ()
        assert plan.query_type is QueryType.UNSUPPORTED
        assert plan.execution_route is ExecutionRoute.DECLINE

    def test_the_deferral_is_documented_in_the_plan(self, planner):
        plan = planner.plan("Reliance ka profit kitna hai?")
        assert any("Part 2C" in n for n in plan.notes)
        assert any("deferred" in m for m in plan.missing_requirements)


# ===========================================================================
class TestPlannerProducesNoAnswer:
    """The planner describes; it never answers."""

    def test_no_prose_field_exists(self):
        names = {f.name for f in dataclasses.fields(QuestionPlan)}
        assert not {"content", "answer", "text", "prose", "summary",
                    "response", "display_content"} & names

    def test_plan_carries_no_financial_figures(self, planner):
        plan = planner.plan("Reliance ki financial quality kaisi hai?")
        blob = repr(plan.as_dict())
        # The detection block legitimately carries a confidence float;
        # nothing else in a plan may carry a number.
        assert re.findall(r"\b\d+\.\d+\b", blob) == re.findall(
            r"\b\d+\.\d+\b", repr(plan.detection.as_dict())
        )

    def test_plan_is_frozen(self, planner):
        plan = planner.plan("What is the financial quality?")
        with pytest.raises(Exception):
            plan.query_type = QueryType.OPEN_ENDED  # type: ignore[misc]

    def test_plan_is_serialisable(self, planner):
        import json
        blob = json.dumps(
            planner.plan("Reliance ki financial quality aur growth kaisi hai?").as_dict()
        )
        assert "financial_quality" in blob and "growth_quality" in blob


# ===========================================================================
class TestWebResearchRouting:
    """Part 3 Phase 4A — a current development is routed to web research.

    The route exists so a question the canonical store cannot answer (news,
    an order win, an expansion, a deal) is named for what it is, instead of
    being folded into "open-ended" and reasoned about from financials that
    do not contain the answer. The planner only *names* the route: nothing
    here fetches, searches or ranks.
    """

    @pytest.fixture()
    def jsw(self):
        universe = (FakeCompany("c-jsw", "JSWSTEEL", "JSW Steel Limited"),)

        def resolve(text: str):
            lowered = (text or "").lower()
            return [c for c in universe if "jsw" in lowered]

        return QuestionPlanner(company_resolver=resolve)

    # --- the four pinned behaviours -----------------------------------
    def test_arithmetic_question_stays_internal_reasoning(self, jsw):
        plan = jsw.plan("₹500 se ₹650 kitna percent increase hai?")
        assert plan.query_type is QueryType.OPEN_ENDED
        assert plan.execution_route is ExecutionRoute.INTERNAL_REASONING

    def test_expansion_status_is_web_research(self, jsw):
        plan = jsw.plan("JSW Steel expansion status kya hai?")
        assert plan.query_type is QueryType.WEB_RESEARCH
        assert plan.execution_route is ExecutionRoute.WEB_RESEARCH
        assert plan.entity.status is EntityStatus.RESOLVED
        assert plan.entity.ticker == "JSWSTEEL"

    def test_latest_order_is_web_research(self, jsw):
        plan = jsw.plan("JSW Steel ka latest order kya hai?")
        assert plan.query_type is QueryType.WEB_RESEARCH
        assert plan.execution_route is ExecutionRoute.WEB_RESEARCH

    def test_latest_news_without_a_company_is_web_research(self, jsw):
        plan = jsw.plan("latest news kya hai?")
        assert plan.query_type is QueryType.WEB_RESEARCH
        assert plan.execution_route is ExecutionRoute.WEB_RESEARCH
        assert plan.entity.status is EntityStatus.UNRESOLVED

    # --- three languages ----------------------------------------------
    @pytest.mark.parametrize("question", [
        "What is the latest news on JSW Steel?",
        "Has JSW Steel announced any acquisition recently?",
        "JSW Steel ne kaunsa naya order jeeta?",
        "JSW Steel ka expansion kab tak complete hoga?",
        "JSW स्टील की ताज़ा खबर क्या है?",
        "JSW स्टील का विस्तार कब पूरा होगा?",
        "JSW स्टील ने कौन सा अधिग्रहण किया?",
    ])
    def test_web_research_in_english_hindi_and_hinglish(self, jsw, question):
        plan = jsw.plan(question)
        assert plan.query_type is QueryType.WEB_RESEARCH, question
        assert plan.execution_route is ExecutionRoute.WEB_RESEARCH, question

    # --- what outranks it ---------------------------------------------
    @pytest.mark.parametrize("question,intent", [
        ("JSW Steel ki latest P/E kya hai?", "pe"),
        ("What is the current market price of JSW Steel?", "market_price"),
        ("JSW Steel ka latest ROE kya hai?", "roe"),
        ("Should I buy JSW Steel today?", "recommendation"),
        ("Recent revenue growth of JSW Steel?", "revenue_growth"),
    ])
    def test_a_supported_intent_stays_deterministic(self, jsw, question, intent):
        """A recency word beside a supported intent is that intent."""
        plan = jsw.plan(question)
        assert intents_of(plan) == [intent]
        assert plan.query_type is QueryType.DETERMINISTIC
        assert plan.is_executable

    def test_two_intents_with_a_recency_word_still_compose(self, jsw):
        plan = jsw.plan("JSW Steel ki latest financial quality aur growth kaisi hai?")
        assert plan.query_type is QueryType.MULTI_INTENT
        assert plan.execution_route is ExecutionRoute.COMPOSITION_REQUIRED

    def test_a_financial_figure_with_a_recency_word_is_still_unsupported(self, jsw):
        """A level figure is declined, never looked up on the web instead."""
        plan = jsw.plan("JSW Steel ka latest revenue kya hai?")
        assert plan.query_type is QueryType.UNSUPPORTED
        assert plan.execution_route is ExecutionRoute.DECLINE

    def test_a_comparison_with_a_development_is_still_a_comparison(self):
        plan = QuestionPlanner().plan(
            "Compare JSW Steel and Tata Steel expansion plans"
        )
        assert plan.query_type is QueryType.COMPARISON
        assert plan.execution_route is ExecutionRoute.INTERNAL_REASONING

    def test_a_source_restriction_still_outranks_web_research(self, jsw):
        plan = jsw.plan("From the uploaded documents only, what is the latest news?")
        assert plan.query_type is QueryType.SOURCE_DIRECTED
        assert plan.execution_route is ExecutionRoute.SOURCE_ROUTER

    # --- what it must not capture -------------------------------------
    @pytest.mark.parametrize("question", [
        "What is the order book?",             # defined term, Part 2D
        "Tell me about the company",
        "What is the sector?",
        "Who are the promoters?",
        "Compare revenue and pat",
        "Reliance ke baare me vistar se batao", # "in detail", not expansion
        "रिलायंस के बारे में विस्तार से बताओ",
        "Can you expand on that?",
        "What does the company deal in?",
    ])
    def test_open_ended_questions_are_not_captured(self, planner, question):
        plan = planner.plan(question)
        assert plan.query_type is not QueryType.WEB_RESEARCH, question
        assert plan.execution_route is ExecutionRoute.INTERNAL_REASONING

    def test_a_vague_evaluation_is_still_ambiguous(self, planner):
        plan = planner.plan("Good company hai?")
        assert plan.query_type is QueryType.AMBIGUOUS

    # --- the plan's own account of itself -----------------------------
    def test_web_research_is_not_executable_by_a_deterministic_engine(self, jsw):
        plan = jsw.plan("JSW Steel ka latest order kya hai?")
        assert not plan.is_executable
        assert plan.intents == ()
        assert plan.required_evidence == ()

    def test_confidence_reflects_whether_a_subject_was_named(self, jsw):
        scoped = jsw.plan("JSW Steel ka latest order kya hai?")
        topic = jsw.plan("latest news kya hai?")
        assert scoped.confidence is Confidence.MEDIUM
        assert topic.confidence is Confidence.LOW

    def test_the_plan_says_web_evidence_is_missing(self, jsw):
        plan = jsw.plan("JSW Steel ka latest order kya hai?")
        assert any(m.startswith("web_evidence:") for m in plan.missing_requirements)
        assert any("no page is fetched" in n for n in plan.notes)

    def test_an_unscoped_search_is_recorded_as_ambiguity(self, jsw):
        plan = jsw.plan("latest news kya hai?")
        assert any("topic search" in a for a in plan.ambiguity)
        assert any(m.startswith("entity:") for m in plan.missing_requirements)

    def test_pinned_company_scopes_the_research(self, memory):
        planner = QuestionPlanner(company_resolver=make_resolver("RELIANCE"),
                                  memory=memory)
        plan = planner.plan("latest news kya hai?")
        assert plan.query_type is QueryType.WEB_RESEARCH
        assert plan.entity.status is EntityStatus.CONTEXT_ONLY
        assert plan.entity.ticker == "RELIANCE"
        assert plan.confidence is Confidence.MEDIUM

    def test_serialises_with_the_new_values(self, jsw):
        import json
        blob = json.loads(json.dumps(
            jsw.plan("JSW Steel ka latest order kya hai?").as_dict()
        ))
        assert blob["query_type"] == "web_research"
        assert blob["execution_route"] == "web_research"

    def test_same_plan_regardless_of_declared_language(self, jsw):
        from app.domain.language.types import Language
        question = "JSW Steel ka latest order kya hai?"
        detected = jsw.plan(question)
        forced = jsw.plan(question, language=Language.HINDI)
        assert (detected.query_type, detected.execution_route) == (
            forced.query_type, forced.execution_route,
        )

    def test_is_deterministic(self, jsw):
        first = jsw.plan("JSW Steel expansion status kya hai?").as_dict()
        second = jsw.plan("JSW Steel expansion status kya hai?").as_dict()
        assert first == second
