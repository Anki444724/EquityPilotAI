"""Section-aware orchestration.

AI-005: every section of a report was answered from one undifferentiated pool
of evidence. The scoring engine was the largest single contributor to that
pool — 17 of 60 citations, 28% — so relevance ranking handed scoring output to
nearly every section, and a reader asking about the business model received a
summary of quality scores.

Hard to catch, because every figure quoted was real and correctly cited. The
answer was simply about the wrong thing.
"""
from __future__ import annotations

import pytest

from app.data.filings.base import SourceCategory
from app.domain.ai.types import Citation, EvidenceKind
from app.services.ai.orchestration import (
    PROVIDER_CONFIDENCE, PROVIDER_KINDS, ROUTES, ROUTES_BY_SECTION, Provider,
    Section, score_section, select_evidence,
)


def _citation(key: str, kind: EvidenceKind) -> Citation:
    return Citation(key=key, label=key, kind=kind, value=1.0)


class TestRoutingTable:
    def test_every_section_the_brief_names_is_routed(self):
        for section in Section:
            assert section in ROUTES_BY_SECTION, section

    @pytest.mark.parametrize("section,expected", [
        (Section.BUSINESS_MODEL, Provider.RAG),
        (Section.REVENUE_SEGMENTS, Provider.RAG),
        (Section.FINANCIAL_PERFORMANCE, Provider.FINANCIAL_DB),
        (Section.VALUATION, Provider.VALUATION_ENGINE),
        (Section.RISKS, Provider.RAG),
        (Section.LATEST_NEWS, Provider.MARKET_DATA),
        (Section.MANAGEMENT_COMMENTARY, Provider.RAG),
    ])
    def test_each_section_prefers_the_provider_the_brief_assigns(
        self, section, expected,
    ):
        assert ROUTES_BY_SECTION[section].providers[0] is expected

    def test_revenue_segments_may_use_rag_and_the_database(self):
        providers = ROUTES_BY_SECTION[Section.REVENUE_SEGMENTS].providers
        assert Provider.RAG in providers
        assert Provider.FINANCIAL_DB in providers

    def test_risks_may_use_the_annual_report_and_the_database(self):
        providers = ROUTES_BY_SECTION[Section.RISKS].providers
        assert Provider.RAG in providers
        assert Provider.FINANCIAL_DB in providers

    def test_theses_synthesise_over_everything(self):
        for section in (Section.BULL_THESIS, Section.BEAR_THESIS,
                        Section.INVESTMENT_VERDICT):
            route = ROUTES_BY_SECTION[section]
            assert route.synthesises
            assert route.kinds == frozenset(EvidenceKind)


class TestScoringIsConfined:
    """The heart of the defect."""

    def test_scoring_answers_exactly_three_sections(self):
        scoring_sections = {
            route.section for route in ROUTES
            if Provider.SCORING_ENGINE in route.providers and not route.synthesises
        }
        assert scoring_sections == {
            Section.QUALITY_SCORES, Section.RISK_SCORES,
            Section.INSTITUTIONAL_SCORE,
        }

    @pytest.mark.parametrize("section", [
        Section.BUSINESS_MODEL, Section.REVENUE_SEGMENTS,
        Section.FINANCIAL_PERFORMANCE, Section.VALUATION, Section.RISKS,
        Section.LATEST_NEWS, Section.MANAGEMENT_COMMENTARY,
    ])
    def test_narrative_sections_cannot_be_answered_by_scoring(self, section):
        route = ROUTES_BY_SECTION[section]
        assert Provider.SCORING_ENGINE not in route.providers
        # Enforced structurally: scoring evidence is not even admitted.
        assert EvidenceKind.SCORING not in route.kinds

    def test_a_business_model_question_never_selects_scoring(self):
        """The exact failure the user reported, as a test."""
        pool = [
            _citation("institutional_score", EvidenceKind.SCORING),
            _citation("score_grade", EvidenceKind.SCORING),
            _citation("doc_p1_c1", EvidenceKind.DOCUMENT),
        ]
        selected, provider, _ = select_evidence(
            pool, ROUTES_BY_SECTION[Section.BUSINESS_MODEL],
        )
        assert provider is Provider.RAG
        assert all(c.kind is EvidenceKind.DOCUMENT for c in selected)


class TestFallThrough:
    def test_a_section_falls_through_rather_than_being_omitted(self):
        """Weaker evidence beats a missing section, provided it is named."""
        pool = [_citation("revenue", EvidenceKind.STATEMENT)]
        selected, provider, attempted = select_evidence(
            pool, ROUTES_BY_SECTION[Section.BUSINESS_MODEL],
        )
        assert provider is Provider.FINANCIAL_DB      # RAG had nothing
        assert selected
        assert attempted[0]["outcome"] == "no_evidence"
        assert attempted[0]["provider"] == Provider.RAG.value

    def test_the_first_provider_with_evidence_wins(self):
        pool = [
            _citation("doc_p1_c1", EvidenceKind.DOCUMENT),
            _citation("revenue", EvidenceKind.STATEMENT),
        ]
        _, provider, _ = select_evidence(
            pool, ROUTES_BY_SECTION[Section.BUSINESS_MODEL],
        )
        assert provider is Provider.RAG

    def test_no_evidence_anywhere_is_reported_honestly(self):
        selected, provider, attempted = select_evidence(
            [], ROUTES_BY_SECTION[Section.BUSINESS_MODEL],
        )
        assert selected == []
        assert provider is None
        assert all(a["outcome"] == "no_evidence" for a in attempted)


class TestConfidence:
    def test_the_annual_report_is_trusted_above_scoring(self):
        assert (PROVIDER_CONFIDENCE[Provider.RAG]
                > PROVIDER_CONFIDENCE[Provider.SCORING_ENGINE]
                > PROVIDER_CONFIDENCE[Provider.MARKET_DATA])

    def test_an_ungrounded_section_scores_zero(self):
        """A section written from nothing must not inherit the credibility
        of one grounded in a filing."""
        assert score_section(None, []) == 0.0
        assert score_section(Provider.RAG, []) == 0.0

    def test_depth_raises_confidence_within_bounds(self):
        thin = score_section(Provider.RAG, [_citation("a", EvidenceKind.DOCUMENT)])
        deep = score_section(
            Provider.RAG,
            [_citation(f"c{i}", EvidenceKind.DOCUMENT) for i in range(8)],
        )
        assert 0.0 < thin < deep <= 1.0


class TestProviderKinds:
    def test_every_provider_declares_its_evidence_kinds(self):
        for provider in Provider:
            assert provider in PROVIDER_KINDS
            assert PROVIDER_KINDS[provider]

    def test_rag_admits_only_document_evidence(self):
        assert PROVIDER_KINDS[Provider.RAG] == frozenset({EvidenceKind.DOCUMENT})

    def test_the_scoring_engine_admits_only_scoring(self):
        assert PROVIDER_KINDS[Provider.SCORING_ENGINE] == frozenset(
            {EvidenceKind.SCORING}
        )


class TestSectionResult:
    def test_a_section_reports_provenance_and_a_timestamp(self):
        from app.services.ai.orchestration import SectionResult, utc_now

        result = SectionResult(
            section=Section.RISKS, title="Risks", content="…",
            provider_used=Provider.RAG,
            source_category=SourceCategory.ANNUAL_REPORT,
            confidence=0.95, timestamp=utc_now(),
        )
        payload = result.as_dict()
        for field_name in ("source_used", "provider_used", "confidence_score",
                           "citations", "timestamp"):
            assert field_name in payload
        assert payload["source_used"] == "Annual Report"

    def test_citations_carry_a_page_when_the_evidence_has_one(self):
        from app.services.ai.orchestration import SectionResult

        citation = Citation(
            key="doc_p3_c9", label="AR p.3", kind=EvidenceKind.DOCUMENT,
            value="text", page=3, chunk_id=9,
        )
        result = SectionResult(
            section=Section.RISKS, title="Risks", content="…",
            citations=[citation],
        )
        rendered = result.references()[0]
        assert "p.3" in rendered
        assert "chunk 9" in rendered
