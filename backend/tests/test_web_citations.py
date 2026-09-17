"""Citation keys, provenance, rendering, and the scope web must not satisfy.

Four properties, each of which has a silent failure mode if it breaks:

* a minted key must satisfy the marker pattern the audit enforces — otherwise
  a correct answer is reported as citing invented evidence;
* the same page must mint the same key from a reference and from stored
  provenance, or the citation cannot be re-derived;
* a documents-only scope must not be satisfiable by a fetched page, whether it
  arrives as a WEB citation or as a retrieved passage labelled DOCUMENT;
* rendering must place the WEB block where the evidence precedence expects it.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from app.domain.ai.types import (
    Citation,
    EvidenceKind,
    WebProvenance,
    mint_web_citation_key,
)
from app.domain.ai.sourcing import SCOPE_KINDS, SourceScope
from app.domain.web.types import (
    CITATION_KEY_PATTERN,
    WebContentClass,
    WebDocumentRef,
    WebSourceClass,
    source_class_label,
)
from app.services.ai.citation_engine import audit
from app.services.ai.context_builder import GroundedContext

RETRIEVED = datetime(2026, 9, 18, 9, 30, tzinfo=timezone.utc)
PUBLISHED = datetime(2026, 7, 24, 5, 35, tzinfo=timezone.utc)


def _ref(**overrides) -> WebDocumentRef:
    base = dict(
        url="https://www.acme.example/investors/results",
        canonical_url="https://www.acme.example/investors/results",
        host="www.acme.example",
        source_class=WebSourceClass.VERIFIED_IR,
        content_class=WebContentClass.HTML,
        title="Acme Q2 FY26 results",
        retrieved_at=RETRIEVED,
        published_at=PUBLISHED,
        content_hash="a" * 64,
        document_id=7,
        preview="Acme reported consolidated revenue of 1,234 crore.",
    )
    base.update(overrides)
    return WebDocumentRef(**base)


# ===========================================================================
class TestCitationKeys:
    @pytest.mark.parametrize("url", [
        "https://www.acme.example/investors",
        "https://ir.acme-holdings.example/investor-relations/annual-report-fy26",
        "https://www.nseindia.com/get-quotes/equity?symbol=ACME",
        "https://data.sec.gov/submissions/CIK0000320193.json",
        "https://xn--80ak6aa92e.example/путь/документ",
        "https://a.example/" + "x" * 400,
    ])
    def test_a_minted_key_always_satisfies_the_marker_pattern(self, url):
        key = mint_web_citation_key(url)
        assert CITATION_KEY_PATTERN.fullmatch(f"[{key}]"), key
        assert 2 <= len(key) <= 61

    def test_the_same_page_mints_the_same_key_twice(self):
        assert mint_web_citation_key("https://a.example/x") == (
            mint_web_citation_key("https://a.example/x")
        )

    def test_two_pages_on_one_host_are_distinguishable(self):
        first = mint_web_citation_key("https://a.example/one")
        second = mint_web_citation_key("https://a.example/two")
        assert first != second

    def test_a_reference_and_stored_provenance_mint_the_same_key(self):
        """One implementation: the key survives a round trip through storage."""
        ref = _ref()
        stored = WebProvenance(
            url=ref.url, title=ref.title, canonical_url=ref.canonical_url,
            published_at=ref.published_at, retrieved_at=ref.retrieved_at,
            content_hash=ref.content_hash,
        )
        assert ref.citation_key() == stored.citation_key()
        assert ref.citation_key().startswith("web_")

    def test_the_canonical_url_is_what_identity_is_derived_from(self):
        plain = _ref(canonical_url="https://www.acme.example/investors")
        canonical = _ref(
            url="https://www.acme.example/investors?utm_source=x",
            canonical_url="https://www.acme.example/investors",
        )
        assert plain.citation_key() == canonical.citation_key()


class TestProvenanceAndRendering:
    def test_a_web_citation_carries_the_url_and_both_timestamps(self):
        citation = Citation(
            key=mint_web_citation_key(_ref().url),
            label="[Investor Relations] Acme Q2 FY26 results",
            kind=EvidenceKind.WEB,
            value=_ref().preview,
            source="Acme Q2 FY26 results — Investor Relations",
            document_id=7,
            web=WebProvenance(
                url=_ref().url, title=_ref().title,
                canonical_url=_ref().canonical_url,
                published_at=PUBLISHED, retrieved_at=RETRIEVED,
                content_hash="a" * 64,
            ),
        )
        assert citation.web is not None
        assert citation.web.url.endswith("/investors/results")
        assert citation.web.published_at == PUBLISHED
        assert citation.web.retrieved_at == RETRIEVED
        as_dict = citation.web.as_dict()
        assert as_dict["retrieved_at"].startswith("2026-09-18")
        assert as_dict["published_at"].startswith("2026-07-24")

    def test_the_rendered_line_states_where_the_page_came_from(self):
        citation = Citation(
            key="web_acmeexample_12345678", label="[Investor Relations] Results",
            kind=EvidenceKind.WEB, value="Revenue was 1,234 crore.",
            source="Acme results — Investor Relations (www.acme.example)",
            web=WebProvenance(
                url="https://www.acme.example/investors/results",
                retrieved_at=RETRIEVED,
            ),
        )
        rendered = citation.render()
        assert "web_acmeexample_12345678" in rendered
        assert "1,234 crore" in rendered

    def test_a_web_citations_marker_survives_the_audit(self):
        """The failure this prevents: a correct answer reported as invented."""
        citation = Citation(
            key=mint_web_citation_key("https://www.acme.example/investors"),
            label="[Investor Relations] Acme investors",
            kind=EvidenceKind.WEB,
            value="Acme reported revenue of 1,234 crore.",
            web=WebProvenance(
                url="https://www.acme.example/investors", retrieved_at=RETRIEVED,
            ),
        )
        result = audit(
            f"Acme's own investor page reports revenue of 1,234 crore "
            f"[{citation.key}].", [citation],
        )
        assert result.unknown_keys == []
        assert result.is_supported is True

    def test_an_invented_web_key_is_still_reported_as_unknown(self):
        result = audit("Revenue rose [web_nothing_deadbeef].", [])
        assert result.unknown_keys == ["web_nothing_deadbeef"]

    def test_the_class_label_is_the_one_the_reader_sees(self):
        assert source_class_label("verified_ir") == "Investor Relations"
        assert source_class_label("exchange") == "Exchange"
        assert source_class_label("not-a-class") == "Web"
        assert source_class_label(None) == "Web"


# ===========================================================================
class TestScopes:
    def test_uploaded_documents_only_does_not_include_web(self):
        allowed = SCOPE_KINDS[SourceScope.UPLOADED_DOCUMENTS_ONLY]
        assert EvidenceKind.WEB not in allowed
        assert allowed == frozenset({EvidenceKind.DOCUMENT})

    def test_hybrid_includes_web(self):
        assert EvidenceKind.WEB in SCOPE_KINDS[SourceScope.HYBRID]

    def test_restricted_scopes_other_than_hybrid_never_include_web(self):
        for scope, kinds in SCOPE_KINDS.items():
            if scope is SourceScope.HYBRID:
                continue
            assert EvidenceKind.WEB not in kinds, scope

    def test_a_web_citation_is_removed_by_a_documents_only_restriction(self):
        context = GroundedContext(company_id="c", ticker="T", name="T Ltd")
        context.add(Citation(
            key="web_acmeexample_12345678", label="[IR] Results",
            kind=EvidenceKind.WEB, value="Revenue 1,234 crore.",
            document_id=7,
        ))
        restricted = context.restricted_to(
            SCOPE_KINDS[SourceScope.UPLOADED_DOCUMENTS_ONLY]
        )
        assert restricted.citations == []
        assert any(
            "outside the requested source" in line
            for line in restricted.unavailable
        )

    def test_a_retrieved_passage_from_a_fetched_page_is_removed_too(self):
        """The leak the audit would not catch: retrieval labels it DOCUMENT."""
        context = GroundedContext(
            company_id="c", ticker="T", name="T Ltd",
            web_document_ids=frozenset({7}), web_ids_complete=True,
        )
        context.add(Citation(
            key="doc_p1_c1", label="[Web Page] Acme results p.1",
            kind=EvidenceKind.DOCUMENT, value="Revenue 1,234 crore.",
            document_id=7,
        ))
        context.add(Citation(
            key="doc_p2_c9", label="[Annual Report] Acme AR p.2",
            kind=EvidenceKind.DOCUMENT, value="Revenue 1,300 crore.",
            document_id=3,
        ))
        restricted = context.restricted_to(
            SCOPE_KINDS[SourceScope.UPLOADED_DOCUMENTS_ONLY]
        )
        assert [c.key for c in restricted.citations] == ["doc_p2_c9"]

    def test_an_unclassifiable_passage_is_withheld_rather_than_assumed(self):
        """Fail closed: unknown provenance must not satisfy an upload scope."""
        context = GroundedContext(company_id="c", ticker="T", name="T Ltd")
        context.add(Citation(
            key="doc_p1_c1", label="[Document] p.1",
            kind=EvidenceKind.DOCUMENT, value="text", document_id=7,
        ))
        restricted = context.restricted_to(
            SCOPE_KINDS[SourceScope.UPLOADED_DOCUMENTS_ONLY]
        )
        assert restricted.citations == []
        assert any("uploaded documents" in line for line in restricted.unavailable)

    def test_a_document_fact_without_an_id_is_unaffected(self):
        """Facts never come from fetched pages, so they are not withheld."""
        context = GroundedContext(company_id="c", ticker="T", name="T Ltd")
        context.add(Citation(
            key="doc_revenue_fy26", label="Revenue (FY26)",
            kind=EvidenceKind.DOCUMENT, value=1234.0,
        ))
        restricted = context.restricted_to(
            SCOPE_KINDS[SourceScope.UPLOADED_DOCUMENTS_ONLY]
        )
        assert [c.key for c in restricted.citations] == ["doc_revenue_fy26"]

    def test_hybrid_keeps_the_web_citation(self):
        context = GroundedContext(company_id="c", ticker="T", name="T Ltd")
        context.add(Citation(
            key="web_acmeexample_12345678", label="[IR] Results",
            kind=EvidenceKind.WEB, value="Revenue 1,234 crore.",
        ))
        hybrid = context.restricted_to(SCOPE_KINDS[SourceScope.HYBRID])
        assert len(hybrid.citations) == 1


# ===========================================================================
class TestEvidenceOrdering:
    def _context_with(self, *citations) -> GroundedContext:
        context = GroundedContext(company_id="c", ticker="T", name="T Ltd")
        for citation in citations:
            context.add(citation)
        return context

    def test_the_web_block_renders_after_the_document_block(self):
        context = self._context_with(
            Citation(key="web_a_1", label="[IR] page", kind=EvidenceKind.WEB,
                     value="web text"),
            Citation(key="doc_p1_c1", label="[AR] p.1",
                     kind=EvidenceKind.DOCUMENT, value="filing text"),
        )
        rendered = context.render_evidence()
        assert rendered.index("--- WEB ---") > rendered.index("--- DOCUMENT ---")

    def test_the_web_note_is_absent_when_there_is_no_web_evidence(self):
        context = self._context_with(
            Citation(key="doc_p1_c1", label="[AR] p.1",
                     kind=EvidenceKind.DOCUMENT, value="filing text"),
        )
        rendered = context.render_evidence()
        assert "WEB block" not in rendered

    def test_the_web_note_is_present_and_states_the_ranking_when_it_is(self):
        context = self._context_with(
            Citation(key="web_a_1", label="[IR] page", kind=EvidenceKind.WEB,
                     value="web text"),
        )
        rendered = context.render_evidence()
        assert "WEB block" in rendered
        assert "weaker evidence than uploaded filings" in rendered

    def test_the_evidence_kind_is_reachable_by_kind_lookup(self):
        context = self._context_with(
            Citation(key="web_a_1", label="[IR] page", kind=EvidenceKind.WEB,
                     value="web text"),
        )
        assert [c.key for c in context.by_kind(EvidenceKind.WEB)] == ["web_a_1"]

    def test_a_web_citation_without_a_value_is_not_added(self):
        """`add` refuses valueless citations, and web evidence is never one."""
        context = GroundedContext(company_id="c", ticker="T", name="T Ltd")
        context.add(Citation(key="web_a_1", label="[IR]", kind=EvidenceKind.WEB))
        assert context.citations == []


# ===========================================================================
class TestProviderIndependence:
    def test_the_flag_default_is_read_through_the_a2_gate(self):
        from types import SimpleNamespace

        from app.services.ai.external_gate import (
            external_providers_enabled, gate_detail,
        )

        disabled = SimpleNamespace(AI_EXTERNAL_PROVIDERS_ENABLED=False)
        enabled = SimpleNamespace(AI_EXTERNAL_PROVIDERS_ENABLED=True)
        assert external_providers_enabled(disabled) is False
        assert external_providers_enabled(enabled) is True
        # The string form is what an environment variable actually produces.
        assert external_providers_enabled(
            SimpleNamespace(AI_EXTERNAL_PROVIDERS_ENABLED="false")
        ) is False
        assert "AI_EXTERNAL_PROVIDERS_ENABLED" in gate_detail("web evidence")

    def test_no_provider_other_than_synthesis_claims_web_evidence(self):
        """Web evidence is not produced by a provider.

        ``Provider.SYNTHESIS`` is defined as ``frozenset(EvidenceKind)`` — a
        catch-all over every kind — so WEB is inside it by construction rather
        than by an edit. No provider set was changed to add web evidence, and
        ``orchestration.py`` was not modified by this phase.
        """
        from app.services.ai.orchestration import PROVIDER_KINDS, Provider

        for provider, kinds in PROVIDER_KINDS.items():
            if provider is Provider.SYNTHESIS:
                assert kinds == frozenset(EvidenceKind)
                continue
            assert EvidenceKind.WEB not in kinds, provider

    def test_the_context_builder_does_not_require_a_provider(self):
        """`_add_web` reads the database and nothing else."""
        from app.services.ai.context_builder import _web_provenance

        class Row:
            doc_metadata = {
                "web": {
                    "source_url": "https://www.acme.example/investors",
                    "retrieved_at": "2026-09-18T09:30:00+00:00",
                    "title": "Investors",
                }
            }
            title = None
            source_url = None
            published_at = None
            retrieved_at = None
            content_hash = "b" * 64
            processed_at = None
            created_at = None

        provenance = _web_provenance(Row())
        assert provenance is not None
        assert provenance.retrieved_at.year == 2026
        assert provenance.citation_key().startswith("web_")

    def test_a_row_with_no_retrieval_time_is_not_cited(self):
        """When the platform does not know when it read the page, it says so."""
        from app.services.ai.context_builder import _web_provenance

        class Row:
            doc_metadata = {"web": {"source_url": "https://a.example/x"}}
            title = None
            source_url = "https://a.example/x"
            published_at = None
            retrieved_at = None
            content_hash = ""
            processed_at = None
            created_at = None

        assert _web_provenance(Row()) is None

    def test_a_row_with_no_url_is_not_cited(self):
        from app.services.ai.context_builder import _web_provenance

        class Row:
            doc_metadata = {}
            title = "Something"
            source_url = None
            published_at = None
            retrieved_at = None
            content_hash = ""
            processed_at = None
            created_at = None

        assert _web_provenance(Row()) is None
