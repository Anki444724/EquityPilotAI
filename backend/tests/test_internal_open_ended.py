"""Part 2D — the internal open-ended answer engine.

`test_question_planner_architecture.py` proves the planner plans and
computes nothing. `test_composition_wiring.py` proves the Part 2C composer
is wired only where it is entitled to be. This module proves the same two
things about the layer that `ExecutionRoute.INTERNAL_REASONING` always
named and never had, plus the properties that are specific to it.

What makes this layer different from the ones before it is that it
*computes*: it subtracts one platform figure from another and states the
result. That is where a new failure mode appears — a number in an answer
that no citation contains — and the tests here are organised around it.
Every derived figure must resolve through the existing citation audit,
every refusal must carry no text, and every question this layer declines
must still be answered by the path that answered it before Part 2D
existed.

Nothing here modifies the production answer path; the last classes assert
that the path is, in fact, still the same one.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.domain.ai.types import Citation, EvidenceKind
from app.domain.language.types import Language
from app.main import app
from app.services.ai.analyst import ResearchAnalyst
from app.services.ai.citation_engine import annotate, audit
from app.services.ai.context_builder import ContextBuilder, GroundedContext
from app.services.ai.internal_open_ended import (
    ATTRIBUTE_SPECS, EXPLANATION_SPECS, MAX_OPERATIONS, METRIC_SPECS,
    InternalAnswer, InternalAnswerStatus, InternalOpenEndedEngine,
    OpenEndedCapability, OperationKind,
)
from app.services.ai.memory import ConversationMemory
from app.services.ai.planner import (
    EntityResolution, EntityStatus, ExecutionRoute, QueryType, QuestionPlanner,
)
from app.services.ai.prompt_builder import PromptBuilder
from app.services.ai.service import AIService
from app.services.analysis_service import AnalysisService
from app.services.company_service import CompanyService
from app.services.forecast.service import ForecastService
from app.services.scoring.service import ScoringService
from app.services.valuation.service import ValuationService
from tests.test_deterministic_analyst_path import SpyDocumentService, SpyRouter

REF = "BHARATCP"
client = TestClient(app)


def _run(coro):
    return asyncio.run(coro)


# ===========================================================================
# Collaborators
# ===========================================================================
class SpyEngine:
    """Records open-ended calls; the failure modes are switchable.

    ``inner`` is the real engine unless a test replaces the behaviour, so
    the happy path exercises the production implementation rather than a
    stand-in for it.
    """

    def __init__(self, *, raises: bool = False, returns=None,
                 inner: InternalOpenEndedEngine | None = None) -> None:
        self.raises = raises
        self.forced = returns
        self.inner = inner or InternalOpenEndedEngine()
        self.calls: list[tuple] = []
        self.results: list[InternalAnswer] = []

    def answer(self, plan, context):
        self.calls.append((plan, context))
        if self.raises:
            raise RuntimeError("internal engine exploded")
        result = (self.forced if self.forced is not None
                  else self.inner.answer(plan, context))
        self.results.append(result)
        return result


class StubPlanner:
    """Returns a fixed plan, or raises."""

    def __init__(self, plan=None, error: Exception | None = None) -> None:
        self._plan = plan
        self.error = error
        self.calls: list[str] = []

    def plan(self, question, *, language=None, source=None):
        self.calls.append(question)
        if self.error is not None:
            raise self.error
        return self._plan


# ===========================================================================
# Fixtures and helpers
# ===========================================================================
@pytest.fixture()
def db_session():
    from tests.conftest import TestingSession

    session = TestingSession()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture()
def analysis(db_session):
    service = AnalysisService.for_ticker(db_session, REF, provision=False)
    assert service is not None and service.has_data
    return service


def _real_planner(db_session, resolver=None) -> QuestionPlanner:
    return QuestionPlanner(
        company_resolver=resolver or CompanyService(db_session).named_in,
    )


def _analyst(db_session, analysis, *, mode="forbid", fail_on_search=True,
             planner=None, composer=None, open_ended=None):
    """A real analyst over the seeded company, with provider/RAG spies."""
    docs = SpyDocumentService(fail_on_search=fail_on_search)
    builder = ContextBuilder(
        analysis,
        ForecastService(db_session),
        ValuationService(db_session),
        ScoringService(db_session),
        docs,
    )
    analyst = ResearchAnalyst(
        builder, router=SpyRouter(mode), prompt_builder=PromptBuilder(),
        planner=planner, composer=composer, open_ended=open_ended,
    )
    return analyst, analyst.router, docs


def _internal_analyst(db_session, analysis, *, mode="forbid",
                      fail_on_search=True, planner=None, open_ended=None):
    """The shape `analyst_for(..., enable_composition=True)` builds."""
    return _analyst(
        db_session, analysis, mode=mode, fail_on_search=fail_on_search,
        planner=planner if planner is not None else _real_planner(db_session),
        open_ended=open_ended if open_ended is not None else SpyEngine(),
    )


def _memory() -> ConversationMemory:
    return ConversationMemory(session_id="open-ended-test")


def _prime(memory: ConversationMemory, analysis) -> ConversationMemory:
    memory.set_company(analysis.company.id, analysis.company.ticker,
                       analysis.company.name)
    return memory


def _context(analysis, db_session) -> GroundedContext:
    """The production context, built the way the analyst builds it."""
    return ContextBuilder(
        analysis, ForecastService(db_session), ValuationService(db_session),
        ScoringService(db_session), SpyDocumentService(fail_on_search=False),
    ).build()


def _slim_context(*, sector="Textiles", **values) -> GroundedContext:
    """A hand-built context, for the cases the seeded company cannot make.

    Division by zero and conflicting evidence are not states the seeded
    company is in, and manufacturing them in the real context would mean
    editing the seed data every other test depends on.
    """
    citations = [
        Citation(key=key, label=key.replace("_", " ").title(),
                 kind=EvidenceKind.STATEMENT, value=value, unit=unit,
                 source="test", fiscal_year=2025)
        for key, (value, unit) in values.items()
    ]
    return GroundedContext(
        company_id="c-slim", ticker="SLIMCO", name="Slim Evidence Ltd",
        sector=sector, citations=citations,
    )


#: Questions the planner routes to internal reasoning and this layer answers
#: from the seeded company's real evidence.
ANSWERABLE = (
    "What is the sector?",
    "Compare revenue and pat",
    "Compare the ebitda margin and the net margin.",
    "What is the order book?",
)

#: Questions that reach the internal route and must be refused — each is
#: answered by the existing provider path, exactly as before Part 2D.
REFUSED = (
    "Tell me about the company",
    "Compare Reliance with TCS.",
    "Who is the best CEO in the sector?",
)


# ===========================================================================
class TestRouteOwnership:
    """Part 2D owns INTERNAL_REASONING, and nothing else."""

    def test_the_engine_refuses_a_route_that_is_not_its_own(self, analysis,
                                                            db_session):
        """Every other route keeps its existing owner."""
        plan = _real_planner(db_session).plan("What is the P/E?")
        assert plan.execution_route is not ExecutionRoute.INTERNAL_REASONING

        answer = InternalOpenEndedEngine().answer(plan, _context(analysis, db_session))

        assert answer.status is InternalAnswerStatus.NOT_SUPPORTED
        assert answer.capability is OpenEndedCapability.UNSUPPORTED_OPEN_ENDED
        assert answer.content == ""

    @pytest.mark.parametrize("route", [
        ExecutionRoute.DETERMINISTIC_FINANCIAL,
        ExecutionRoute.DETERMINISTIC_INVESTMENT,
        ExecutionRoute.COMPOSITION_REQUIRED,
        ExecutionRoute.SOURCE_ROUTER,
        ExecutionRoute.DECLINE,
    ])
    def test_every_existing_route_is_refused(self, route, analysis, db_session):
        """Enumerated rather than sampled: no route is inherited by accident."""
        plan = replace(
            _real_planner(db_session).plan("What is the sector?"),
            execution_route=route,
        )
        answer = InternalOpenEndedEngine().answer(
            plan, _context(analysis, db_session),
        )
        assert answer.status is InternalAnswerStatus.NOT_SUPPORTED

    def test_internal_reasoning_is_the_route_it_serves(self, db_session):
        """The planner's own routing, not a route this module invents."""
        for question in ANSWERABLE:
            plan = _real_planner(db_session).plan(question)
            assert plan.execution_route is ExecutionRoute.INTERNAL_REASONING, question
            assert plan.query_type in {QueryType.COMPARISON, QueryType.OPEN_ENDED}


# ===========================================================================
class TestFactLookup:
    """1–2. A fact from authoritative context, or an honest absence."""

    def test_sector_is_answered_from_the_context(self, analysis, db_session):
        context = _context(analysis, db_session)
        plan = _real_planner(db_session).plan("What is the sector?")

        answer = InternalOpenEndedEngine().answer(plan, context)

        assert answer.status is InternalAnswerStatus.ANSWERED
        assert answer.capability is OpenEndedCapability.FACT_LOOKUP
        assert analysis.company.sector in answer.content
        assert "[company_sector]" in answer.content
        # The claim is auditable through the existing machinery.
        verdict = audit(answer.content, list(answer.all_citations))
        assert verdict.unknown_keys == []
        assert verdict.is_supported

    def test_a_missing_attribute_is_not_invented(self, db_session):
        context = _slim_context(sector=None)
        plan = QuestionPlanner().plan("What is the sector?")

        answer = InternalOpenEndedEngine().answer(plan, context)

        assert answer.status is InternalAnswerStatus.NOT_SUPPORTED
        assert answer.content == ""
        assert "company_sector" in answer.missing

    @pytest.mark.parametrize("question", [
        "What is the EBITDA?", "What is the revenue?", "What is the net profit?",
    ])
    def test_a_level_figure_question_never_reaches_this_layer(self, question):
        """Level figures stay on the provider path — documented, not silent.

        The planner classifies these UNSUPPORTED and routes them to DECLINE,
        so the internal engine is never offered them. Asserted rather than
        assumed, because the alternative — routing them here — would be a
        Part 2B behaviour change, and the Part 2D document lists them as a
        known limitation instead.
        """
        plan = QuestionPlanner().plan(question)

        assert plan.execution_route is ExecutionRoute.DECLINE
        answer = InternalOpenEndedEngine().answer(plan, _slim_context())
        assert answer.status is InternalAnswerStatus.NOT_SUPPORTED
        assert answer.content == ""


# ===========================================================================
class TestCalculation:
    """3–5. Arithmetic only where every input is explicit."""

    def test_a_difference_is_computed_from_two_citations(self, analysis,
                                                         db_session):
        context = _context(analysis, db_session)
        plan = _real_planner(db_session).plan("Compare revenue and pat")

        answer = InternalOpenEndedEngine().answer(plan, context)
        revenue = InternalOpenEndedEngine._find(context, "revenue")
        pat = InternalOpenEndedEngine._find(context, "pat")

        assert answer.status is InternalAnswerStatus.ANSWERED
        derived = {c.key: c for c in answer.derived_citations}
        key = "derived_difference_revenue_pat"
        assert key in derived
        # Reproducible from the two cited inputs, to the cent.
        assert derived[key].value == pytest.approx(revenue.value - pat.value)
        # The difference is the platform's own unit, never a naked number.
        assert derived[key].unit == revenue.unit == pat.unit
        assert f"[{key}]" in answer.content

    def test_a_ratio_is_computed_alongside_the_difference(self, analysis,
                                                          db_session):
        context = _context(analysis, db_session)
        answer = InternalOpenEndedEngine().answer(
            _real_planner(db_session).plan("Compare revenue and pat"), context,
        )
        revenue = InternalOpenEndedEngine._find(context, "revenue")
        pat = InternalOpenEndedEngine._find(context, "pat")
        derived = {c.key: c for c in answer.derived_citations}

        assert derived["derived_ratio_revenue_pat"].value == pytest.approx(
            revenue.value / pat.value,
        )

    def test_a_missing_input_yields_no_result(self):
        context = _slim_context(revenue=(1000.0, "₹ cr"))  # no pat

        answer = InternalOpenEndedEngine().answer(
            QuestionPlanner().plan("Compare revenue and pat"), context,
        )

        assert answer.status is InternalAnswerStatus.NOT_SUPPORTED
        assert answer.content == ""
        assert "pat" in answer.missing
        assert answer.derived_citations == ()

    def test_division_by_zero_is_undefined_not_zero(self):
        """A zero denominator yields no figure, and never a silent 0."""
        context = _slim_context(revenue=(1000.0, "₹ cr"), pat=(0.0, "₹ cr"))

        answer = InternalOpenEndedEngine().answer(
            QuestionPlanner().plan("Compare revenue and pat"), context,
        )

        # The answer is still given: the difference is well defined. Only the
        # multiple is withheld, and it is withheld rather than reported as 0.
        assert answer.status is InternalAnswerStatus.ANSWERED
        keys = {c.key for c in answer.derived_citations}
        assert "derived_difference_revenue_pat" in keys
        assert "derived_ratio_revenue_pat" not in keys
        ratios = [o for o in answer.operations if o.kind is OperationKind.RATIO]
        assert ratios and ratios[0].result is None
        assert "zero" in ratios[0].reason

    def test_operations_are_bounded(self, analysis, db_session):
        """Multi-step reasoning is capped; nothing loops without a limit."""
        answer = InternalOpenEndedEngine().answer(
            _real_planner(db_session).plan("Compare revenue and pat"),
            _context(analysis, db_session),
        )
        assert len(answer.operations) <= MAX_OPERATIONS

    def test_figures_in_different_units_are_not_subtracted(self, analysis,
                                                           db_session):
        """A percentage and a money figure have no meaningful difference."""
        context = _context(analysis, db_session)
        answer = InternalOpenEndedEngine().answer(
            _real_planner(db_session).plan(
                "Compare the return on equity and the revenue.",
            ),
            context,
        )
        # ROE is a percentage and revenue is money, so the arithmetic is
        # withheld and both measurements are still stated.
        if answer.answered:
            assert answer.derived_citations == ()
            assert "different units" in answer.content


# ===========================================================================
class TestComparison:
    """6–8. Factual measurement, never a ranking."""

    def test_a_comparison_states_both_measurements(self, analysis, db_session):
        context = _context(analysis, db_session)
        answer = InternalOpenEndedEngine().answer(
            _real_planner(db_session).plan(
                "Compare the ebitda margin and the net margin.",
            ),
            context,
        )

        assert answer.status is InternalAnswerStatus.ANSWERED
        assert "[ebitda_margin]" in answer.content
        assert "[pat_margin]" in answer.content
        assert "[derived_difference_ebitda_margin_pat_margin]" in answer.content

    def test_a_comparison_never_declares_a_winner(self, analysis, db_session):
        """The output is a measurement; the guardrails would flag a verdict."""
        from app.services.ai.guardrails import check
        from app.services.ai.citation_engine import audit as _audit

        context = _context(analysis, db_session)
        for question in ANSWERABLE:
            answer = InternalOpenEndedEngine().answer(
                _real_planner(db_session).plan(question), context,
            )
            if not answer.answered:
                continue
            lowered = answer.content.lower()
            for word in ("better", "worse", "best", "winner", "superior",
                         "should buy", "attractive", "recommend"):
                assert word not in lowered, (question, word)
            # And the platform's own guardrails agree.
            report = check(answer.content, _audit(
                answer.content, list(answer.all_citations),
            ))
            assert report.violations == [], (question, report.violations)

    def test_a_comparison_with_missing_evidence_is_refused(self):
        context = _slim_context(revenue=(1000.0, "₹ cr"))  # pat absent

        answer = InternalOpenEndedEngine().answer(
            QuestionPlanner().plan("Compare revenue and pat"), context,
        )

        assert answer.status is InternalAnswerStatus.NOT_SUPPORTED
        assert "pat" in answer.missing
        # Nothing was estimated to complete the comparison.
        assert answer.content == ""

    def test_a_one_sided_comparison_is_not_completed_by_guessing(self,
                                                                 db_session):
        """'Compare the ROE and the debt' names debt, which is two figures."""
        context = _slim_context(roe_avg=(0.18, "%"), gross_debt=(400.0, "₹ cr"))

        answer = InternalOpenEndedEngine().answer(
            QuestionPlanner().plan("Compare the ROE and the debt."), context,
        )

        assert answer.status is InternalAnswerStatus.NOT_SUPPORTED
        assert answer.capability is OpenEndedCapability.AMBIGUOUS
        assert answer.content == ""

    def test_no_part_of_the_question_is_silently_dropped(self, analysis,
                                                         db_session):
        """A comparison and an attribute asked together are both answered."""
        context = _context(analysis, db_session)
        answer = InternalOpenEndedEngine().answer(
            _real_planner(db_session).plan(
                "Compare revenue and pat, and what is the sector?",
            ),
            context,
        )

        assert answer.status is InternalAnswerStatus.ANSWERED
        assert answer.capability is OpenEndedCapability.MULTI_STEP_ANALYSIS
        assert "[revenue]" in answer.content
        assert "[pat]" in answer.content
        assert analysis.company.sector in answer.content

    def test_multi_step_analysis_records_every_step(self, analysis, db_session):
        """All intermediate values are traceable, not just the last one."""
        answer = InternalOpenEndedEngine().answer(
            _real_planner(db_session).plan(
                "Compare revenue and pat, and what is the sector?",
            ),
            _context(analysis, db_session),
        )

        assert answer.capability is OpenEndedCapability.MULTI_STEP_ANALYSIS
        kinds = [o.kind for o in answer.operations]
        assert kinds.count(OperationKind.LOOKUP) >= 3
        assert OperationKind.DIFFERENCE in kinds
        # Every derived figure names the inputs it came from.
        for op in answer.operations:
            if op.kind is not OperationKind.LOOKUP:
                assert len(op.inputs) == 2
                assert op.output_key.startswith("derived_")


# ===========================================================================
class TestExplanation:
    """9–10. A bounded vocabulary, and a refusal outside it."""

    def test_a_term_in_the_vocabulary_is_defined(self, db_session):
        answer = InternalOpenEndedEngine().answer(
            QuestionPlanner().plan("What is the order book?"),
            _slim_context(),
        )

        assert answer.status is InternalAnswerStatus.ANSWERED
        assert answer.capability is OpenEndedCapability.EXPLANATION
        assert "order book" in answer.content.lower()
        # A definition carries no company figure, so it cites none.
        assert answer.derived_citations == ()

    def test_a_definition_says_when_the_platform_holds_no_value(self, db_session):
        """The definition does not imply a measurement exists."""
        answer = InternalOpenEndedEngine().answer(
            QuestionPlanner().plan("Who are the promoters?"), _slim_context(),
        )

        assert answer.answered
        assert "does not currently hold" in answer.content

    def test_a_term_outside_the_vocabulary_is_refused(self, db_session):
        """No giant encyclopedia: an unknown term falls back."""
        answer = InternalOpenEndedEngine().answer(
            QuestionPlanner().plan("Tell me about the company"),
            _slim_context(sector="Textiles"),
        )

        assert answer.status is InternalAnswerStatus.NOT_SUPPORTED
        assert answer.capability is OpenEndedCapability.UNSUPPORTED_OPEN_ENDED
        assert answer.content == ""

    def test_the_vocabulary_is_small_and_explicit(self):
        """Bounded by construction, and reviewable as data."""
        assert len(EXPLANATION_SPECS) <= 25
        for spec in EXPLANATION_SPECS:
            assert spec.term and spec.definition and spec.patterns
        # Every vocabulary table is a literal in the module, not something
        # fetched, learned or generated.
        assert len(METRIC_SPECS) <= 40
        assert len(ATTRIBUTE_SPECS) <= 5


# ===========================================================================
class TestFailClosed:
    """11–14, 29. Every unsafe condition refuses rather than fabricates."""

    def test_an_ambiguous_company_is_never_resolved_by_choosing(self, db_session):
        plan = replace(
            QuestionPlanner().plan("Compare revenue and pat"),
            entity=EntityResolution(
                status=EntityStatus.AMBIGUOUS,
                candidates=("RELIANCE", "TCS"),
                basis="two companies named",
            ),
        )

        answer = InternalOpenEndedEngine().answer(plan, _slim_context())

        assert answer.status is InternalAnswerStatus.NOT_SUPPORTED
        assert answer.capability is OpenEndedCapability.AMBIGUOUS
        assert answer.content == ""
        assert any("RELIANCE" in m for m in answer.missing)

    def test_an_ambiguous_company_over_http_falls_back(self, db_session,
                                                       analysis):
        """End to end: two companies named, so the provider path answers."""
        analyst, router, docs = _internal_analyst(
            db_session, analysis, mode="offline", fail_on_search=False,
        )
        result = _run(analyst.chat(
            "Compare revenue and pat of Reliance and TCS.",
            _prime(_memory(), analysis),
        ))

        assert result.provider != "deterministic"
        assert len(router.complete_calls) == 1

    def test_conflicting_evidence_is_refused(self):
        """Two citations, one key, different values: neither is chosen."""
        context = _slim_context(
            revenue=(1000.0, "₹ cr"), pat=(250.0, "₹ cr"),
        )
        context.citations.append(Citation(
            key="revenue", label="Revenue", kind=EvidenceKind.DOCUMENT,
            value=9999.0, unit="₹ cr", source="a retrieved passage",
        ))

        answer = InternalOpenEndedEngine().answer(
            QuestionPlanner().plan("Compare revenue and pat"), context,
        )

        assert answer.status is InternalAnswerStatus.NOT_SUPPORTED
        assert "conflicting" in answer.reason
        assert answer.content == ""

    def test_an_unsupported_operation_cannot_be_requested(self):
        """The operation set is a closed enum, not an expression language."""
        members = {o.value for o in OperationKind}
        assert members == {"lookup", "difference", "percentage_change", "ratio"}
        with pytest.raises(ValueError):
            OperationKind("arbitrary_expression")

    def test_missing_evidence_is_stated_not_estimated(self, analysis,
                                                      db_session):
        """A comparison against an absent figure says so in the answer."""
        context = _context(analysis, db_session)
        # A sector the platform does not hold, alongside a real comparison.
        context = replace(context, sector=None)

        answer = InternalOpenEndedEngine().answer(
            _real_planner(db_session).plan(
                "Compare revenue and pat, and what is the sector?",
            ),
            context,
        )

        assert answer.answered  # the comparison is complete
        assert "company_sector" in answer.missing
        assert "sector is not available" in answer.content
        # ...and the missing figure was not invented.
        assert "company_sector" not in {c.key for c in answer.derived_citations}

    def test_a_refusal_carries_no_text(self, db_session):
        """The structural guarantee: no partial internal answer can escape."""
        context = _slim_context()
        for question in REFUSED:
            answer = InternalOpenEndedEngine().answer(
                QuestionPlanner().plan(question), context,
            )
            assert answer.content == "", question
            assert answer.status is InternalAnswerStatus.NOT_SUPPORTED


# ===========================================================================
class TestCitationIntegrity:
    """26–28. Every figure resolves; nothing is manufactured."""

    def test_every_derived_figure_resolves_through_the_existing_audit(
        self, analysis, db_session,
    ):
        """The failure mode this layer is built to avoid."""
        context = _context(analysis, db_session)
        for question in ANSWERABLE:
            answer = InternalOpenEndedEngine().answer(
                _real_planner(db_session).plan(question), context,
            )
            if not answer.answered:
                continue
            # Verified against the context plus what the layer derived —
            # exactly what the analyst hands the funnel.
            verdict = audit(
                answer.content, [*context.citations, *answer.derived_citations],
            )
            assert verdict.unknown_keys == [], (question, verdict.unknown_keys)
            assert verdict.uncited_numbers == [], (question, verdict.uncited_numbers)
            assert verdict.is_supported, question

    def test_the_audit_is_not_vacuous(self, analysis, db_session):
        """Proof the audit does real work, so passing it means something.

        A derived figure is a number no original citation contains, so the
        only reason the audit accepts it is that the layer published it as
        evidence. This pins both halves of that: the figure is flagged
        without its citation, and accepted with it.
        """
        context = _context(analysis, db_session)
        claim = "The gap is 4,24,242.13 ₹ cr [revenue]."

        flagged = audit(claim, context.citations)
        assert "424242.13" in flagged.uncited_numbers

        published = Citation(
            key="derived_difference_revenue_pat", label="Difference",
            kind=EvidenceKind.RATIO, value=424242.13, unit="₹ cr",
            source="internal arithmetic on platform figures",
        )
        accepted = audit(claim, [*context.citations, published])
        assert "424242.13" not in accepted.uncited_numbers
        assert accepted.unknown_keys == []

    def test_no_citation_is_manufactured(self, analysis, db_session):
        """Every citation used came from the context; every derived one
        names its arithmetic source."""
        context = _context(analysis, db_session)
        known = {c.key for c in context.citations}

        answer = InternalOpenEndedEngine().answer(
            _real_planner(db_session).plan("Compare revenue and pat"), context,
        )

        for citation in answer.used_citations:
            assert citation.key in known
        for citation in answer.derived_citations:
            assert citation.key.startswith("derived_")
            assert "internal arithmetic" in citation.source
            # A derived figure cites the platform's own evidence kind rather
            # than inventing a new one.
            assert citation.kind is EvidenceKind.RATIO

    def test_citations_are_deduplicated(self, analysis, db_session):
        """One key appears once, however many sentences cite it."""
        context = _context(analysis, db_session)
        answer = InternalOpenEndedEngine().answer(
            _real_planner(db_session).plan(
                "Compare revenue and pat, and what is the sector?",
            ),
            context,
        )
        keys = [c.key for c in answer.all_citations]
        assert len(keys) == len(set(keys))

    def test_markers_annotate_to_readable_labels(self, analysis, db_session):
        """The existing annotation, on this layer's markers."""
        context = _context(analysis, db_session)
        answer = InternalOpenEndedEngine().answer(
            _real_planner(db_session).plan("Compare revenue and pat"), context,
        )
        display = annotate(answer.content, list(answer.all_citations))

        assert "[revenue]" not in display
        assert "[Revenue]" in display
        assert "[derived_difference_revenue_pat]" not in display


# ===========================================================================
class TestProductionWiring:
    """15–17, 42–43. Where Part 2D sits in the real answer path."""

    @pytest.mark.parametrize("question", ANSWERABLE)
    def test_the_internal_route_bypasses_the_provider(self, db_session,
                                                      analysis, question):
        analyst, router, docs = _internal_analyst(db_session, analysis)
        engine = analyst.open_ended

        result = _run(analyst.chat(question, _prime(_memory(), analysis)))

        assert len(engine.calls) == 1
        assert result.provider == "deterministic"
        assert result.prompt_tokens == 0
        assert result.completion_tokens == 0
        # The spies raise on a provider or RAG call, so reaching the
        # assertions at all proves neither happened.
        assert router.complete_calls == []
        assert docs.search_calls == []

    @pytest.mark.parametrize("question", REFUSED)
    def test_an_unsupported_question_falls_back_to_the_provider(
        self, db_session, analysis, question,
    ):
        analyst, router, docs = _internal_analyst(
            db_session, analysis, mode="offline", fail_on_search=False,
        )
        result = _run(analyst.chat(question, _prime(_memory(), analysis)))

        assert result.provider != "deterministic"
        # The existing retrieval is still performed, unchanged.
        assert len(docs.search_calls) == 1
        assert len(router.complete_calls) == 1

    def test_the_retrieval_fallback_is_unchanged_for_other_questions(
        self, db_session, analysis,
    ):
        """A question no internal layer claims still retrieves and asks."""
        analyst, router, docs = _internal_analyst(
            db_session, analysis, mode="offline", fail_on_search=False,
        )
        result = _run(analyst.chat(
            "Summarise the chairman's statement.", _prime(_memory(), analysis),
        ))

        assert len(docs.search_calls) == 1
        assert result.provider == SpyRouter.NAME

    @pytest.mark.parametrize("question", [
        "What is the P/E?", "What is the ROE?", "What is the debt?",
        "What is the financial quality?",
    ])
    def test_a_single_intent_never_reaches_the_internal_layer(
        self, db_session, analysis, question,
    ):
        """Order is the regression protection: the resolver answers first."""
        analyst, router, docs = _internal_analyst(db_session, analysis)

        result = _run(analyst.chat(question, _prime(_memory(), analysis)))

        assert analyst.open_ended.calls == []
        assert result.provider == "deterministic"
        assert router.complete_calls == []
        assert docs.search_calls == []

    @pytest.mark.parametrize("question", [
        "What is the P/E and the ROE?",
        "What are the strengths and weaknesses?",
    ])
    def test_a_multi_intent_question_still_uses_the_part_2c_composer(
        self, db_session, analysis, question,
    ):
        """Part 2C keeps its route; Part 2D does not take it over."""
        from app.services.ai.internal_composer import InternalComposer

        engine = SpyEngine()
        analyst, router, docs = _analyst(
            db_session, analysis,
            planner=_real_planner(db_session),
            composer=InternalComposer(),
            open_ended=engine,
        )
        result = _run(analyst.chat(question, _prime(_memory(), analysis)))

        assert engine.calls == []  # the composer answered it
        assert result.provider == "deterministic"
        assert router.complete_calls == []

    def test_one_plan_serves_both_internal_layers(self, db_session, analysis):
        """Planning is not duplicated: one question, one plan."""
        planner = _real_planner(db_session)
        calls: list[str] = []
        original = planner.plan

        def counting_plan(question, **kwargs):
            calls.append(question)
            return original(question, **kwargs)

        planner.plan = counting_plan  # type: ignore[method-assign]
        analyst, _, _ = _analyst(
            db_session, analysis, planner=planner, open_ended=SpyEngine(),
        )

        _run(analyst.chat("What is the sector?", _prime(_memory(), analysis)))

        assert len(calls) == 1

    def test_an_analyst_without_the_engine_behaves_as_before(
        self, db_session, analysis,
    ):
        """No engine injected: the internal route takes the provider path."""
        analyst, router, docs = _analyst(
            db_session, analysis, mode="offline", fail_on_search=False,
            planner=_real_planner(db_session), open_ended=None,
        )
        assert analyst.open_ended is None

        result = _run(analyst.chat(
            "What is the sector?", _prime(_memory(), analysis),
        ))

        assert result.provider != "deterministic"
        assert len(router.complete_calls) == 1


# ===========================================================================
class TestFailureHandling:
    """An internal failure costs a fallback, never an answer."""

    def test_an_engine_that_raises_falls_back(self, db_session, analysis):
        analyst, router, _ = _internal_analyst(
            db_session, analysis, mode="offline", fail_on_search=False,
            open_ended=SpyEngine(raises=True),
        )
        result = _run(analyst.chat(
            "What is the sector?", _prime(_memory(), analysis),
        ))

        assert result.provider != "deterministic"
        assert len(router.complete_calls) == 1
        # No internal detail reaches the user.
        assert "exploded" not in result.content
        assert "Traceback" not in result.content

    def test_a_planner_that_raises_falls_back(self, db_session, analysis):
        analyst, router, _ = _internal_analyst(
            db_session, analysis, mode="offline", fail_on_search=False,
            planner=StubPlanner(error=RuntimeError("planner exploded")),
        )
        result = _run(analyst.chat(
            "What is the sector?", _prime(_memory(), analysis),
        ))

        assert result.provider != "deterministic"
        assert "exploded" not in result.content

    def test_a_refusing_engine_records_nothing_in_memory(self, db_session,
                                                         analysis):
        """No partial internal answer is persisted."""
        analyst, _, _ = _internal_analyst(
            db_session, analysis, mode="offline", fail_on_search=False,
        )
        memory = _prime(_memory(), analysis)
        _run(analyst.chat("Tell me about the company", memory))

        stored = memory.turns[1].content
        assert "classified in the" not in stored


# ===========================================================================
class TestVerificationPipeline:
    """33–37. The existing funnel, unchanged, on an internal answer."""

    def test_metadata_is_the_deterministic_convention(self, db_session,
                                                      analysis):
        analyst, _, _ = _internal_analyst(db_session, analysis)
        result = _run(analyst.chat(
            "Compare revenue and pat", _prime(_memory(), analysis),
        ))

        assert result.provider == "deterministic"
        assert result.model == "none"
        assert result.prompt_tokens == 0
        assert result.completion_tokens == 0
        assert result.cost_usd == 0.0
        assert result.latency_ms > 0  # measured, not invented

    def test_the_citation_audit_and_guardrails_ran(self, db_session, analysis):
        analyst, _, _ = _internal_analyst(db_session, analysis)
        result = _run(analyst.chat(
            "Compare revenue and pat", _prime(_memory(), analysis),
        ))

        assert result.citation_audit is not None
        assert result.citation_audit.unknown_keys == []
        assert result.citation_audit.is_supported is True
        assert result.guardrails is not None
        assert result.guardrails.passed is True
        # Guardrail enforcement still appends its disclosure.
        assert "not investment advice" in result.display_content

    def test_annotation_replaced_the_markers(self, db_session, analysis):
        analyst, _, _ = _internal_analyst(db_session, analysis)
        result = _run(analyst.chat(
            "Compare revenue and pat", _prime(_memory(), analysis),
        ))

        assert "[revenue]" in result.content      # audited artefact
        assert "[Revenue]" in result.display_content  # annotated display

    def test_memory_records_the_internal_turn(self, db_session, analysis):
        analyst, _, _ = _internal_analyst(db_session, analysis)
        memory = _prime(_memory(), analysis)
        result = _run(analyst.chat("Compare revenue and pat", memory))

        assert len(memory.turns) == 2
        assert memory.turns[0].role.value == "user"
        assert memory.turns[1].role.value == "assistant"
        stored = memory.turns[1].content
        assert result.content.startswith(stored)
        assert "not investment advice" not in stored  # canonical, no footer
        assert "revenue" in memory.turns[1].citations

    def test_protected_content_survives_rendering(self, db_session, analysis,
                                                  monkeypatch):
        """The audited artefact keeps the name and the markers intact.

        Entity protection itself belongs to the translator implementations,
        which wrap the text in ``protect(text, extra_terms=entities)`` before
        translating. What Part 2D is responsible for is that the *canonical
        English* answer — the one audited, annotated and written to memory —
        is never the thing that gets translated, so a rendering failure
        cannot corrupt the evidence chain. That is asserted here, along with
        the fact that the entities this analyst protects are the bound
        company's.
        """
        from app.services.language.translators import TranslationResult

        seen: dict = {}

        class _Recording:
            name = "recording"

            def supports(self, language):
                return True

            async def translate(self, text, language, *, entities=None):
                seen["entities"] = list(entities or [])
                seen["text"] = text
                return TranslationResult(
                    text=f"(rendered) {text}", language=language,
                    translated=True, provider=self.name,
                )

        monkeypatch.setattr(
            "app.services.language.adapter.build_translator",
            lambda *args, **kwargs: _Recording(),
        )
        analyst, _, _ = _internal_analyst(db_session, analysis)
        result = _run(analyst.chat(
            "Compare revenue and pat", _prime(_memory(), analysis),
            language=Language.HINDI,
        ))

        # The bound company's name and ticker are what get protected.
        assert analysis.company.name in seen["entities"]
        assert analysis.company.ticker in seen["entities"]
        # What was handed to the renderer is the annotated English answer,
        # and it still carries the company name and resolved markers.
        assert analysis.company.name in seen["text"]
        assert "[Revenue]" in seen["text"]
        # The audited artefact is untouched by rendering.
        assert analysis.company.name in result.content
        assert result.display_content.startswith("(rendered) ")

    @pytest.mark.parametrize("language", [
        Language.ENGLISH, Language.HINDI, Language.HINGLISH,
    ])
    def test_the_audited_answer_is_english_in_every_language(
        self, db_session, analysis, language,
    ):
        """Reasoning is canonical English; rendering is the adapter's job."""
        analyst, router, _ = _internal_analyst(db_session, analysis)
        result = _run(analyst.chat(
            "Compare revenue and pat", _prime(_memory(), analysis),
            language=language,
        ))

        assert result.provider == "deterministic"
        assert router.complete_calls == []
        # One knowledge base, rendered per language — never a second answer.
        assert result.content.startswith("Revenue for")
        assert result.citation_audit.unknown_keys == []


# ===========================================================================
class TestMultilingualRouting:
    """30–32. Hindi and Hinglish reach the same internal answer."""

    @pytest.mark.parametrize("question,language", [
        ("Sector kya hai?", Language.HINGLISH),
        ("सेक्टर क्या है?", Language.HINDI),
        ("What is the sector?", Language.ENGLISH),
    ])
    def test_a_fact_lookup_is_language_independent(self, db_session, analysis,
                                                   question, language):
        analyst, router, docs = _internal_analyst(db_session, analysis)
        result = _run(analyst.chat(
            question, _prime(_memory(), analysis), language=language,
        ))

        assert len(analyst.open_ended.calls) == 1
        assert result.provider == "deterministic"
        assert router.complete_calls == []
        assert docs.search_calls == []
        # The canonical answer is the same English sentence in all three.
        assert result.content.startswith(analysis.company.name)
        assert analysis.company.sector in result.content


# ===========================================================================
class TestApiSurface:
    """44. The HTTP contract, end to end."""

    def test_an_open_ended_question_is_answered_deterministically_over_http(
        self,
    ):
        r = client.post(
            f"/api/v1/company/{REF}/ai/chat",
            json={"question": "Compare revenue and pat",
                  "session_id": "open-ended-http"},
        )
        assert r.status_code == 200, r.text[:300]
        body = r.json()

        assert body["provider"] == "deterministic"
        assert body["model"] == "none"
        assert body["prompt_tokens"] == 0
        assert body["completion_tokens"] == 0
        assert body["cost_usd"] == 0.0
        assert "[revenue]" in body["content"]
        assert "[pat]" in body["content"]
        assert body["citation_audit"]["is_supported"] is True
        assert body["citation_audit"]["unknown_keys"] == []
        assert body["guardrails"]["passed"] is True

    def test_a_sector_question_is_answered_over_http(self):
        body = client.post(
            f"/api/v1/company/{REF}/ai/chat",
            json={"question": "What is the sector?",
                  "session_id": "open-ended-sector"},
        ).json()

        assert body["provider"] == "deterministic"
        assert "[company_sector]" in body["content"]

    def test_a_refused_question_still_gets_an_answer_over_http(self):
        """Fail-closed means fallback, never an error to the user."""
        r = client.post(
            f"/api/v1/company/{REF}/ai/chat",
            json={"question": "Tell me about the company",
                  "session_id": "open-ended-refused"},
        )
        assert r.status_code == 200, r.text[:300]
        assert r.json()["content"]

    def test_opt_in_is_still_exactly_one_call_site(self, monkeypatch):
        """Part 2D did not add a second opt-in."""
        calls: list[bool] = []
        original = AIService.analyst_for

        def spy(self, analysis, *, enable_composition=False):
            calls.append(enable_composition)
            return original(self, analysis,
                            enable_composition=enable_composition)

        monkeypatch.setattr(AIService, "analyst_for", spy)
        r = client.post(
            f"/api/v1/company/{REF}/ai/chat",
            json={"question": "What is the sector?", "session_id": "optin-2d"},
        )
        assert r.status_code == 200, r.text[:300]
        assert calls == [True]

    def test_the_analyst_for_default_still_builds_no_internal_layers(
        self, db_session, analysis,
    ):
        analyst = AIService(db_session).analyst_for(analysis)
        assert analyst.planner is None
        assert analyst.composer is None
        assert analyst.open_ended is None

    def test_the_engine_is_injected_when_composition_is_enabled(
        self, db_session, analysis,
    ):
        analyst = AIService(db_session).analyst_for(
            analysis, enable_composition=True,
        )
        assert isinstance(analyst.open_ended, InternalOpenEndedEngine)
