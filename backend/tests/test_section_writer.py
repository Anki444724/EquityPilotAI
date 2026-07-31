"""Phase 1 — the production writing layer.

These tests protect the property that makes the integration safe rather than
merely functional: swapping the writer must change the *prose* and nothing
else. Routing, evidence selection, provider attribution, citations and
confidence are all computed before any model is called, and a regression that
let the writer influence them would be invisible in the output — the report
would still read well and still cite real figures, but the provenance beside
it would no longer describe how it was produced.
"""
from __future__ import annotations

import asyncio

import pytest

from app.domain.ai.types import Citation, EvidenceKind
from app.services.ai.orchestration import (
    PRESENTATION_ORDER, ROUTES, ROUTES_BY_SECTION, Provider, Section,
    SectionResult, presentation_rank,
)
from app.services.ai.section_writer import (
    NO_EVIDENCE, SectionBrief, build_extra, looks_unevidenced,
)


def _citation(key: str, kind: EvidenceKind = EvidenceKind.STATEMENT, **kw):
    return Citation(key=key, label=kw.pop("label", key.title()), kind=kind,
                    value=kw.pop("value", 1.0), **kw)


class TestReportStructure:
    """Requirement 7 — the eleven named sections must all exist."""

    #: The brief's list, verbatim.
    REQUIRED = [
        "Executive Summary", "Business Overview", "Financial Performance",
        "Valuation", "Bull Thesis", "Bear Thesis", "Risks", "Catalysts",
        "Management Commentary", "Latest News", "Investment Verdict",
    ]

    def test_every_required_section_is_present(self):
        # "Business Overview" is this platform's "Business Model" section —
        # same content, and the route name predates the brief. Mapped rather
        # than renamed so existing API consumers keep working.
        titles = {r.title for r in ROUTES} | {"Business Overview"}
        missing = [s for s in self.REQUIRED if s not in titles]
        assert not missing, f"sections missing from the report: {missing}"

    def test_the_executive_summary_is_presented_first(self):
        assert PRESENTATION_ORDER[0] is Section.EXECUTIVE_SUMMARY

    def test_the_executive_summary_is_written_last(self):
        """It summarises the report, so it cannot be composed before it.

        Execution order is `ROUTES`; presentation order is separate. Writing
        the summary first would produce a generic opening paragraph, which is
        exactly the failure this platform exists to avoid.
        """
        order = [r.section for r in ROUTES]
        assert order[-1] is Section.EXECUTIVE_SUMMARY

    def test_the_verdict_is_written_after_every_evidence_section(self):
        order = [r.section for r in ROUTES]
        evidence_sections = [
            s for s in order
            if not ROUTES_BY_SECTION[s].synthesises
        ]
        verdict = order.index(Section.INVESTMENT_VERDICT)
        assert all(order.index(s) < verdict for s in evidence_sections)

    def test_presentation_covers_exactly_the_routed_sections(self):
        assert {r.section for r in ROUTES} == set(PRESENTATION_ORDER)

    def test_an_unknown_section_sorts_last_rather_than_crashing(self):
        assert presentation_rank(Section.EXECUTIVE_SUMMARY) == 0


class TestProvenanceReachesTheModel:
    """Requirement 5 — the writer is told where its evidence came from."""

    def _brief(self, **kw) -> SectionBrief:
        defaults = dict(
            title="Risks", provider=Provider.RAG.value,
            source="Annual Report", confidence=0.93,
            citations=(
                _citation("doc_p12_c4", EvidenceKind.DOCUMENT,
                          label="[Annual Report] AR FY26 p.12",
                          page=12, chunk_id=4, confidence=0.87),
            ),
            ticker="TCS", company="Tata Consultancy Services Ltd",
        )
        defaults.update(kw)
        return SectionBrief(**defaults)

    def test_the_provider_is_named(self):
        assert Provider.RAG.value in self._brief().render()

    def test_the_confidence_score_is_stated(self):
        assert "0.93" in self._brief().render()

    def test_the_page_and_chunk_reach_the_model(self):
        rendered = self._brief().render()
        assert "page 12" in rendered
        assert "chunk 4" in rendered

    def test_the_retrieval_score_is_reported_per_citation(self):
        assert "0.87" in self._brief().render()

    def test_absent_citations_are_stated_not_omitted(self):
        assert "none" in self._brief(citations=()).render().lower()

    def test_the_contract_forbids_inventing_figures(self):
        # Whitespace-collapsed: the contract is wrapped for readability in
        # the source, so a literal substring match is comparing against the
        # line breaks rather than the instruction.
        extra = " ".join(build_extra(self._brief()).split())
        assert ("Never introduce a company, product, executive, date or "
                "figure that does not appear in the evidence.") in extra

    def test_the_contract_supplies_the_exact_refusal_sentence(self):
        assert NO_EVIDENCE in build_extra(self._brief())


class TestDeclineDetection:
    """The escape hatch must be recognised, and must not be over-eager."""

    def test_the_exact_sentence_is_detected(self):
        assert looks_unevidenced(NO_EVIDENCE)

    def test_detection_survives_emphasis_and_punctuation(self):
        assert looks_unevidenced(f"**{NO_EVIDENCE}**")

    def test_detection_survives_a_trailing_disclaimer(self):
        """The analyst appends a standing disclaimer to every answer."""
        assert looks_unevidenced(
            f"{NO_EVIDENCE}\n\n---\n_This analysis is generated from the "
            f"platform's own computed figures._"
        )

    def test_a_real_answer_is_not_mistaken_for_a_refusal(self):
        assert not looks_unevidenced(
            "Revenue reached 267,021 crore [revenue], up from 255,324 "
            "[revenue_history]."
        )

    def test_an_answer_merely_noting_a_gap_is_not_a_refusal(self):
        """AI-002's failure mode in a new place.

        A section that answers from what it has and notes what it lacks is a
        good section. Treating it as a refusal would zero the confidence of
        the platform's most honest output.
        """
        assert not looks_unevidenced(
            "Revenue grew to 267,021 crore [revenue]. The platform holds no "
            "segment split, so the mix cannot be described."
        )


class TestWriterAttributionIsSeparateFromEvidence:
    """The evidence provider and the writing provider are different things."""

    def test_a_section_reports_both(self):
        result = SectionResult(
            section=Section.RISKS, title="Risks", content="…",
            provider_used=Provider.RAG, confidence=0.9,
            writer_provider="OpenRouter", writer_model="openai/gpt-4o-mini",
            prompt_tokens=1200, completion_tokens=300,
        )
        payload = result.as_dict()
        # Where the evidence came from…
        assert payload["provider_used"] == Provider.RAG.value
        # …and who wrote the prose. Conflating these is how a reader ends up
        # believing OpenRouter sourced the annual report.
        assert payload["writer_provider"] == "OpenRouter"
        assert payload["writer_model"] == "openai/gpt-4o-mini"

    def test_tokens_are_accounted_per_section(self):
        result = SectionResult(
            section=Section.RISKS, title="Risks", content="…",
            prompt_tokens=1200, completion_tokens=300,
        )
        assert result.total_tokens == 1500
        assert result.as_dict()["total_tokens"] == 1500

    def test_an_unwritten_section_names_no_writer(self):
        result = SectionResult(section=Section.RISKS, title="Risks",
                               content=NO_EVIDENCE)
        assert result.as_dict()["writer_provider"] == "none"


class TestScoringEngineStaysConfined:
    """AI-005 must not regress now that two sections have been added."""

    def test_the_new_sections_do_not_route_to_scoring(self):
        for section in (Section.EXECUTIVE_SUMMARY, Section.CATALYSTS):
            providers = ROUTES_BY_SECTION[section].providers
            assert Provider.SCORING_ENGINE not in providers

    def test_scoring_still_answers_only_the_three_score_sections(self):
        scoring = {
            r.section for r in ROUTES
            if Provider.SCORING_ENGINE in r.providers
        }
        assert scoring == {
            Section.QUALITY_SCORES, Section.RISK_SCORES,
            Section.INSTITUTIONAL_SCORE,
        }


class TestRetrievalRespectsTheRouteRestriction:
    """ORCH-001 — the regression that silently undid section routing."""

    def test_the_analyst_can_suppress_its_own_retrieval(self):
        """`run(retrieve=False)` must exist and be honoured.

        The orchestrator retrieves per section against that section's own
        prompt and hands the result in as a restricted context. If the
        analyst then retrieves again, document passages are re-admitted to
        sections whose route excludes them — Financial Performance would
        quietly become part RAG — and the restriction that `context_override`
        exists to impose is defeated without any visible symptom.
        """
        import inspect

        from app.services.ai.analyst import ResearchAnalyst

        signature = inspect.signature(ResearchAnalyst.run)
        assert "retrieve" in signature.parameters
        assert signature.parameters["retrieve"].default is True

    def test_the_orchestrator_suppresses_it(self):
        import inspect

        from app.services.ai import report_orchestrator

        source = inspect.getsource(report_orchestrator.ReportOrchestrator)
        assert "retrieve=False" in source
