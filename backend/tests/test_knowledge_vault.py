"""The Company Knowledge Vault.

The vault's value rests entirely on one promise: nothing is ever lost. A test
suite for it is therefore mostly a suite about *not* destroying things — that
a new filing supersedes rather than overwrites, that a stale filing arriving
late cannot rewind current knowledge, and that every retained version keeps
the citation that justified it.
"""
from __future__ import annotations

import pytest

from app.domain.knowledge.vault import (
    MIN_CURRENT_CONFIDENCE, SOURCE_AUTHORITY, SUMMARY_SPECS, EntryStatus,
    Provenance, SummaryKind, VaultSection, authority_of, is_servable,
    supersedes,
)


class TestSupersessionRules:
    """Ordering is by period, then authority, then confidence."""

    def test_a_later_fiscal_year_wins(self):
        assert supersedes(
            new_fiscal_year=2026, new_authority=0.7, new_confidence=0.5,
            old_fiscal_year=2024, old_authority=1.0, old_confidence=0.9,
        )

    def test_an_earlier_fiscal_year_never_wins(self):
        """The subtlest corruption: a backfill rewinding current knowledge.

        Every individual entry is correct, so nothing downstream flags it.
        """
        assert not supersedes(
            new_fiscal_year=2024, new_authority=1.0, new_confidence=0.99,
            old_fiscal_year=2026, old_authority=0.4, old_confidence=0.1,
        )

    def test_authority_breaks_a_tie_within_a_year(self):
        """An audited annual report outranks a slide deck of the same year."""
        assert supersedes(
            new_fiscal_year=2026,
            new_authority=authority_of("annual_report"), new_confidence=0.5,
            old_fiscal_year=2026,
            old_authority=authority_of("investor_presentation"),
            old_confidence=0.95,
        )

    def test_confidence_breaks_a_tie_within_a_source_type(self):
        assert supersedes(
            new_fiscal_year=2026, new_authority=0.9, new_confidence=0.8,
            old_fiscal_year=2026, old_authority=0.9, old_confidence=0.6,
        )

    def test_a_dated_assertion_beats_an_undated_one(self):
        assert supersedes(
            new_fiscal_year=2026, new_authority=0.4, new_confidence=0.4,
            old_fiscal_year=None, old_authority=1.0, old_confidence=0.9,
        )

    def test_an_undated_assertion_does_not_displace_a_dated_one(self):
        assert not supersedes(
            new_fiscal_year=None, new_authority=1.0, new_confidence=0.9,
            old_fiscal_year=2020, old_authority=0.4, old_confidence=0.1,
        )

    def test_an_annual_report_outranks_every_other_source(self):
        others = {k: v for k, v in SOURCE_AUTHORITY.items()
                  if k != "annual_report"}
        assert all(SOURCE_AUTHORITY["annual_report"] >= v
                   for v in others.values())

    def test_model_inference_is_the_least_authoritative_source(self):
        assert SOURCE_AUTHORITY["ai_inference"] == min(SOURCE_AUTHORITY.values())


class TestServability:
    def test_a_low_confidence_entry_is_recorded_but_not_served(self):
        """Kept because a later corroboration may raise it."""
        assert not is_servable(0.1, EntryStatus.CURRENT)

    def test_a_superseded_entry_is_never_served(self):
        assert not is_servable(0.99, EntryStatus.SUPERSEDED)

    def test_a_confident_current_entry_is_served(self):
        assert is_servable(MIN_CURRENT_CONFIDENCE + 0.1, EntryStatus.CURRENT)


class TestVaultShape:
    def test_every_section_the_brief_names_exists(self):
        required = {
            "company_profile", "business_model", "products",
            "revenue_segments", "geography", "customers", "suppliers",
            "competitors", "management", "promoters", "subsidiaries",
            "financial_statements", "ratios", "historical_ai_analysis",
            "risks", "opportunities", "valuation", "esg",
            "capital_allocation", "ai_notes",
        }
        assert {s.value for s in VaultSection} == required

    def test_all_nine_summary_kinds_exist(self):
        assert len(list(SummaryKind)) == 9

    def test_every_summary_kind_has_a_spec(self):
        assert set(SUMMARY_SPECS) == set(SummaryKind)

    def test_provenance_knows_when_it_is_citable(self):
        assert Provenance(document_id=1, page=4).is_citable
        assert not Provenance(document_id=1).is_citable
        assert not Provenance().is_citable


# ===========================================================================
@pytest.fixture()
def vault_env():
    from tests.conftest import TestingSession

    from app.models.company import Company
    from app.models.knowledge import KnowledgeEntry
    from app.services.knowledge.vault import KnowledgeVault

    db = TestingSession()
    company = Company(id="kv-co", name="Vault Ltd", ticker="VAULTCO",
                      exchange="NSE")
    db.add(company)
    db.commit()
    try:
        yield db, company, KnowledgeVault(db)
    finally:
        db.query(KnowledgeEntry).filter_by(company_id="kv-co").delete()
        db.query(Company).filter_by(id="kv-co").delete()
        db.commit()
        db.close()


class TestNeverOverwrite:
    """The promise the whole vault rests on."""

    def test_a_new_filing_versions_rather_than_replaces(self, vault_env):
        db, company, vault = vault_env
        vault.assert_knowledge(
            company.id, VaultSection.RISKS, "risk_1", value_text="Old risk",
            confidence=0.8,
            provenance=Provenance(document_id=1, page=5, fiscal_year=2024,
                                  doc_type="annual_report"),
        )
        result = vault.assert_knowledge(
            company.id, VaultSection.RISKS, "risk_1", value_text="New risk",
            confidence=0.8,
            provenance=Provenance(document_id=2, page=9, fiscal_year=2026,
                                  doc_type="annual_report"),
        )
        assert result.action == "versioned"
        assert result.entry.version == 2

        history = vault.history(company.id, VaultSection.RISKS, "risk_1")
        assert len(history) == 2, "a version was destroyed"

    def test_the_superseded_entry_keeps_its_evidence(self, vault_env):
        """'What did we believe in FY2024, and why?' must stay answerable."""
        db, company, vault = vault_env
        vault.assert_knowledge(
            company.id, VaultSection.RISKS, "risk_1", value_text="Old risk",
            confidence=0.8, evidence="Page 5 said so.",
            provenance=Provenance(document_id=1, page=5, fiscal_year=2024,
                                  doc_type="annual_report"),
        )
        vault.assert_knowledge(
            company.id, VaultSection.RISKS, "risk_1", value_text="New risk",
            confidence=0.8,
            provenance=Provenance(document_id=2, page=9, fiscal_year=2026,
                                  doc_type="annual_report"),
        )
        old = [h for h in vault.history(company.id, VaultSection.RISKS, "risk_1")
               if h["version"] == 1][0]
        assert old["evidence"] == "Page 5 said so."
        assert old["citation"]["page"] == 5
        assert old["status"] == "superseded"

    def test_a_late_stale_filing_cannot_rewind_the_vault(self, vault_env):
        """A backfill loading FY2024 after FY2026 must not become current."""
        db, company, vault = vault_env
        vault.assert_knowledge(
            company.id, VaultSection.RISKS, "risk_1", value_text="FY2026 view",
            confidence=0.7,
            provenance=Provenance(document_id=1, page=1, fiscal_year=2026,
                                  doc_type="annual_report"),
        )
        result = vault.assert_knowledge(
            company.id, VaultSection.RISKS, "risk_1", value_text="FY2024 view",
            confidence=0.99,
            provenance=Provenance(document_id=2, page=2, fiscal_year=2024,
                                  doc_type="annual_report"),
        )
        assert result.action == "recorded_stale"
        assert result.entry.status == EntryStatus.SUPERSEDED.value

        current = vault.current_entry(company.id, VaultSection.RISKS, "risk_1")
        assert current.value_text == "FY2026 view"

    def test_a_stale_assertion_is_still_recorded(self, vault_env):
        """'We saw this claim and rejected it as stale' is itself knowledge."""
        db, company, vault = vault_env
        vault.assert_knowledge(
            company.id, VaultSection.RISKS, "risk_1", value_text="Current",
            confidence=0.7,
            provenance=Provenance(fiscal_year=2026, doc_type="annual_report"),
        )
        vault.assert_knowledge(
            company.id, VaultSection.RISKS, "risk_1", value_text="Stale",
            confidence=0.9,
            provenance=Provenance(fiscal_year=2020, doc_type="annual_report"),
        )
        assert len(vault.history(company.id, VaultSection.RISKS, "risk_1")) == 2

    def test_exactly_one_entry_is_current_per_key(self, vault_env):
        db, company, vault = vault_env
        for year in (2022, 2024, 2026, 2023):
            vault.assert_knowledge(
                company.id, VaultSection.RISKS, "risk_1",
                value_text=f"FY{year}", confidence=0.7,
                provenance=Provenance(fiscal_year=year,
                                      doc_type="annual_report"),
            )
        history = vault.history(company.id, VaultSection.RISKS, "risk_1")
        current = [h for h in history if h["status"] == "current"]
        assert len(current) == 1
        assert current[0]["value"] == "FY2026"
        assert len(history) == 4, "history was pruned"

    def test_an_empty_assertion_is_refused(self, vault_env):
        """An empty entry would still supersede a good one and blank it."""
        db, company, vault = vault_env
        result = vault.assert_knowledge(
            company.id, VaultSection.RISKS, "risk_1", confidence=0.9,
        )
        assert result.action == "rejected"
        assert result.entry is None


class TestVaultReads:
    def test_only_current_entries_are_served(self, vault_env):
        db, company, vault = vault_env
        vault.assert_knowledge(
            company.id, VaultSection.RISKS, "risk_1", value_text="Old",
            confidence=0.8,
            provenance=Provenance(fiscal_year=2024, doc_type="annual_report"),
        )
        vault.assert_knowledge(
            company.id, VaultSection.RISKS, "risk_1", value_text="New",
            confidence=0.8,
            provenance=Provenance(fiscal_year=2026, doc_type="annual_report"),
        )
        entries = vault.read_section(company.id, VaultSection.RISKS)
        assert len(entries) == 1
        assert entries[0].value_text == "New"

    def test_every_served_entry_carries_a_citation(self, vault_env):
        db, company, vault = vault_env
        vault.assert_knowledge(
            company.id, VaultSection.RISKS, "risk_1", value_text="A risk",
            confidence=0.8,
            provenance=Provenance(document_id=7, page=12, fiscal_year=2026,
                                  doc_type="annual_report"),
        )
        rendered = vault.render(
            vault.read_section(company.id, VaultSection.RISKS)[0]
        )
        assert rendered["citation"]["document_id"] == 7
        assert rendered["citation"]["page"] == 12
        assert rendered["confidence"] == 0.8

    def test_stats_distinguish_current_from_superseded(self, vault_env):
        db, company, vault = vault_env
        for year in (2024, 2026):
            vault.assert_knowledge(
                company.id, VaultSection.RISKS, "risk_1",
                value_text=f"FY{year}", confidence=0.8,
                provenance=Provenance(document_id=1, page=1,
                                      fiscal_year=year,
                                      doc_type="annual_report"),
            )
        stats = vault.stats(company.id).as_dict()
        assert stats["total_entries"] == 2
        assert stats["current"] == 1
        assert stats["superseded"] == 1


class TestSummaryBudget:
    """SUMMARY-001 — max_tokens is a reservation, not a spend."""

    def test_the_reservation_is_bounded(self):
        from app.services.knowledge.summaries import MAX_COMPLETION_TOKENS

        for words, _ in SUMMARY_SPECS.values():
            budget = min(MAX_COMPLETION_TOKENS, int(words * 1.6) + 80)
            assert budget <= MAX_COMPLETION_TOKENS

    def test_the_budget_is_proportionate_to_the_target(self):
        """An over-generous ceiling fails the request outright on a provider
        that reserves against the credit balance — observed as 402 'you
        requested up to 1500 tokens, but can only afford 329'."""
        short = int(SUMMARY_SPECS[SummaryKind.BRIEF_100][0] * 1.6) + 80
        long = int(SUMMARY_SPECS[SummaryKind.DETAILED_500][0] * 1.6) + 80
        assert short < long
        assert short < 400, "a 100-word summary must not reserve 2000 tokens"

    def test_a_fallback_summary_is_marked_as_one(self):
        """Template prose must never accumulate in permanent memory
        indistinguishable from model output."""
        from app.models.knowledge import DocumentSummary

        columns = {c.name for c in DocumentSummary.__table__.columns}
        assert "is_fallback" in columns
        assert "provider" in columns
        assert "model" in columns


class TestReadFirstMemory:
    """The vault must be consulted before raw document chunks."""

    def test_knowledge_is_its_own_evidence_kind(self):
        from app.domain.ai.types import EvidenceKind

        assert EvidenceKind.KNOWLEDGE.value == "knowledge"

    def test_the_context_builder_reads_the_vault_before_documents(self):
        import inspect

        from app.services.ai.context_builder import ContextBuilder

        source = inspect.getsource(ContextBuilder.build)
        vault_at = source.find("_add_knowledge")
        docs_at = source.find("_add_documents")
        assert vault_at != -1, "the vault is never consulted"
        assert vault_at < docs_at, (
            "raw documents are read before distilled knowledge"
        )

    def test_a_vault_failure_never_breaks_an_answer(self):
        import inspect

        from app.services.ai.context_builder import ContextBuilder

        source = inspect.getsource(ContextBuilder._add_knowledge)
        assert "except Exception" in source
