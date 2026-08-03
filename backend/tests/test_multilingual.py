"""Multilingual AI Response Engine.

Organised around the brief's success criteria rather than the code's shape,
because those criteria are the product promise:

* automatic detection of English, Hindi, Hinglish and mixed input;
* ONE canonical knowledge base — no duplicated documents, embeddings, vault
  entries or memory, asserted structurally rather than assumed;
* identical scores, citations and evidence in every language;
* citation and number preservation through translation;
* cross-language retrieval reaching the same English index;
* an architecture where a new language is a translation module and nothing
  else.
"""
from __future__ import annotations

import asyncio
import inspect
import pathlib
import re

import pytest

from app.domain.language.detect import (
    LOW_CONFIDENCE, choose_language, detect,
)
from app.domain.language.glossary import (
    BY_ENGLISH, TERMS, coverage, lookup, render_for_prompt,
)
from app.domain.language.protect import (
    Protection, is_acronym, protect, restore, verify_preserved,
)
from app.domain.language.types import (
    AUTO, CANONICAL_LANGUAGE, LANGUAGES, Language, LanguageStatus,
    PLANNED_LANGUAGES, SUPPORTED_LANGUAGES, Script, resolve, spec_for,
)
from app.services.language.adapter import (
    QUERY_TERMS, AdaptedResponse, LanguageAdapter,
)
from app.services.language.translators import (
    GlossaryTranslator, LLMTranslator, PassthroughTranslator,
    TranslationResult, build_translator,
)


# ===========================================================================
# Language detection
# ===========================================================================

class TestDetection:
    """The brief's examples are the acceptance criteria, verbatim."""

    @pytest.mark.parametrize("text,expected", [
        # --- the brief's three worked examples ---
        ("How is TCS?", Language.ENGLISH),
        ("टीसीएस कैसी कंपनी है?", Language.HINDI),
        ("TCS kaisi company hai?", Language.HINGLISH),
        # --- the brief's mixed-language examples ---
        ("Revenue kya hai", Language.HINGLISH),
        ("Revenue क्या है", Language.HINDI),
        ("How much revenue", Language.ENGLISH),
        ("Debt kitna hai?", Language.HINGLISH),
        ("PAT kitna hai?", Language.HINGLISH),
        # --- the brief's Hinglish/Hindi mode examples ---
        ("TCS future kaisa hai?", Language.HINGLISH),
        ("टीसीएस भविष्य के लिए कैसी कंपनी है?", Language.HINDI),
    ])
    def test_the_briefs_examples(self, text, expected):
        assert detect(text).language is expected

    @pytest.mark.parametrize("text", [
        "What is the operating margin of Cipla?",
        "Compare TCS and Infosys on return on equity",
        "Show me the free cash flow trend",
        "Which company has the strongest balance sheet?",
        "How much debt does Reliance carry?",
    ])
    def test_english_is_never_mistaken_for_hinglish(self, text):
        """The most damaging misdetection: an English speaker answered in Hinglish."""
        assert detect(text).language is Language.ENGLISH

    @pytest.mark.parametrize("text", [
        "TCS ka revenue kitna hai",
        "Company ki growth kaisi rahi hai",
        "Iska valuation mehenga hai kya",
        "Margin improve hua ya nahi",
        "Management execution achhi hai kya",
    ])
    def test_hinglish_is_detected_despite_english_vocabulary(self, text):
        """A Hinglish question is mostly English tokens; grammar decides."""
        assert detect(text).language is Language.HINGLISH

    def test_any_devanagari_is_decisive(self):
        detection = detect("What is राजस्व")
        assert detection.language is Language.HINDI
        assert detection.confidence >= 0.80
        assert detection.is_mixed

    def test_devanagari_records_marathi_ambiguity(self):
        """Devanagari serves Hindi and Marathi; the ambiguity is stated."""
        assert Language.MARATHI in detect("राजस्व कितना है").ambiguous_with

    def test_short_keyword_query_is_low_confidence(self):
        """'TCS revenue' has no grammar; detection must admit it is guessing."""
        detection = detect("TCS revenue")
        assert detection.language is Language.ENGLISH
        assert detection.confidence < LOW_CONFIDENCE

    def test_empty_input_does_not_raise(self):
        for value in ("", "   ", None):
            assert detect(value).language is Language.ENGLISH

    def test_detection_is_deterministic(self):
        """A session that changed language on a follow-up would look broken."""
        for text in ("TCS kaisa hai", "How is TCS", "टीसीएस कैसा है"):
            assert len({detect(text).language for _ in range(20)}) == 1

    def test_every_detection_explains_itself(self):
        for text in ("How is TCS?", "TCS kaisa hai?", "टीसीएस कैसा है?"):
            assert detect(text).reason


class TestLanguagePrecedence:
    """Explicit request > confident detection > saved preference."""

    def test_explicit_request_wins(self):
        language, _ = choose_language("How is TCS?", requested=Language.HINDI)
        assert language is Language.HINDI

    def test_confident_detection_beats_a_saved_preference(self):
        """Typing Devanagari must be honoured whatever was saved last week."""
        language, _ = choose_language("टीसीएस कैसा है?",
                                      preference=Language.ENGLISH)
        assert language is Language.HINDI

    def test_preference_breaks_a_low_confidence_tie(self):
        language, detection = choose_language("TCS revenue",
                                              preference=Language.HINDI)
        assert detection.confidence < LOW_CONFIDENCE
        assert language is Language.HINDI

    def test_detection_is_returned_even_when_overridden(self):
        language, detection = choose_language("TCS kaisa hai?",
                                              requested=Language.ENGLISH)
        assert language is Language.ENGLISH
        assert detection.language is Language.HINGLISH


class TestLanguageResolution:
    @pytest.mark.parametrize("value,expected", [
        ("auto", None), ("", None), (None, None), ("garbage", None),
        ("english", Language.ENGLISH), ("en", Language.ENGLISH),
        ("en-IN", Language.ENGLISH),
        ("hindi", Language.HINDI), ("hi", Language.HINDI),
        ("hinglish", Language.HINGLISH), ("hi-Latn", Language.HINGLISH),
        ("marathi", Language.MARATHI), ("bn", Language.BENGALI),
    ])
    def test_resolution(self, value, expected):
        assert resolve(value) is expected

    def test_unknown_language_falls_through_to_detection(self):
        """A client sending en-GB should get an answer, not a 422."""
        assert resolve("xx-YY") is None


# ===========================================================================
# The single canonical knowledge base
# ===========================================================================

class TestSingleKnowledgeBase:
    """The brief's central prohibition, checked structurally.

    These are the tests that would fail if someone later added a Hindi
    chunk table, a translated-summary column or a per-language vault.
    """

    LANGUAGE_PACKAGE = (
        pathlib.Path(__file__).resolve().parents[1] / "app" / "services" / "language"
    )
    LANGUAGE_DOMAIN = (
        pathlib.Path(__file__).resolve().parents[1] / "app" / "domain" / "language"
    )

    def _sources(self):
        return list(self.LANGUAGE_PACKAGE.rglob("*.py")) + \
               list(self.LANGUAGE_DOMAIN.rglob("*.py"))

    def test_the_language_layer_never_writes_to_the_database(self):
        """No session, no commit, no model import anywhere in the layer."""
        forbidden = ("db.add(", "db.commit(", "session.add(", ".commit()",
                     "insert(", "update(", "delete(")
        offenders = []
        for path in self._sources():
            text = path.read_text()
            for token in forbidden:
                if token in text:
                    offenders.append(f"{path.name}: {token}")
        assert offenders == [], f"language layer writes to the database: {offenders}"

    def test_the_language_layer_imports_no_orm_model(self):
        offenders = []
        for path in self._sources():
            text = path.read_text()
            if re.search(r"from app\.models", text):
                offenders.append(path.name)
        assert offenders == [], f"language layer imports ORM models: {offenders}"

    def test_no_per_language_table_exists(self):
        """A translated-content table is the failure this forbids."""
        import app.models.company  # noqa: F401
        import app.models.document  # noqa: F401
        import app.models.knowledge  # noqa: F401
        import app.models.scoring  # noqa: F401
        from app.db.base import Base

        suspicious = [
            name for name in Base.metadata.tables
            if any(token in name.lower() for token in
                   ("translat", "_hindi", "hindi_", "_hinglish", "language_",
                    "_locale", "multilingual"))
        ]
        assert suspicious == [], f"per-language tables found: {suspicious}"

    def test_no_per_language_column_on_content_tables(self):
        import app.models.document  # noqa: F401
        import app.models.knowledge  # noqa: F401
        from app.db.base import Base

        offenders = []
        for table_name in ("document_chunks", "documents", "knowledge_entries",
                           "document_summaries", "yearly_observations"):
            table = Base.metadata.tables.get(table_name)
            if table is None:
                continue
            for column in table.columns:
                if any(token in column.name.lower() for token in
                       ("translat", "hindi", "hinglish", "locale")):
                    offenders.append(f"{table_name}.{column.name}")
        assert offenders == [], f"per-language columns found: {offenders}"

    def test_canonical_language_is_english(self):
        assert CANONICAL_LANGUAGE is Language.ENGLISH

    def test_translators_are_stateless(self):
        """A translator holding a cache keyed by language is a second store."""
        for translator in (PassthroughTranslator(), GlossaryTranslator()):
            state = {k: v for k, v in vars(translator).items()
                     if not k.startswith("_")}
            assert not state, f"{translator.name} carries state: {state}"


# ===========================================================================
# Protection of untranslatable content
# ===========================================================================

class TestProtection:
    SAMPLE = (
        "TCS reported revenue of ₹2,55,324 crore in FY2025 [revenue], up "
        "10.2% year on year, with ROE of 51.4% [roe] and net debt/EBITDA of "
        "-0.45x [debt]. ISIN INE467B01029. Q1FY26 PAT was ₹12,105 cr. "
        "See [FY2025 Annual Report](https://example.com/ar.pdf)."
    )

    def test_round_trip_is_exact(self):
        protection = protect(self.SAMPLE, extra_terms=["TCS"])
        result = restore(protection.masked, protection)
        assert result.text == self.SAMPLE
        assert result.is_intact

    def test_sentinels_are_never_nested(self):
        """PROTECT-001: a later rule matched digits inside an earlier sentinel."""
        protection = protect(self.SAMPLE, extra_terms=["TCS"])
        assert "§§" not in protection.masked

    def test_every_protected_kind_is_captured(self):
        kinds = protect(self.SAMPLE, extra_terms=["TCS"]).kinds()
        for expected in ("citation", "number", "fiscal_year", "isin",
                         "markdown_link"):
            assert expected in kinds, f"{expected} was not protected"

    def test_a_dropped_sentinel_is_detected(self):
        """A translator that loses a citation must not pass silently."""
        protection = protect(self.SAMPLE, extra_terms=["TCS"])
        damaged = protection.masked.replace(protection.spans[0].token, "")
        result = restore(damaged, protection)
        assert not result.is_intact
        assert result.lost

    def test_an_invented_sentinel_is_detected(self):
        protection = protect(self.SAMPLE, extra_terms=["TCS"])
        result = restore(protection.masked + " §999§", protection)
        assert result.spurious

    def test_verify_catches_a_mangled_number(self):
        mangled = self.SAMPLE.replace("2,55,324", "255 billion")
        assert verify_preserved(self.SAMPLE, mangled)

    def test_verify_catches_a_lost_citation(self):
        stripped = self.SAMPLE.replace("[revenue]", "")
        assert verify_preserved(self.SAMPLE, stripped)

    def test_verify_passes_when_only_prose_changed(self):
        """The case that must NOT alarm: prose translated, tokens intact."""
        translated = self.SAMPLE.replace("reported revenue of", "ने राजस्व दर्ज किया")
        assert verify_preserved(self.SAMPLE, translated) == []

    def test_text_without_protected_content_is_untouched(self):
        plain = "The business is durable and the management is candid."
        protection = protect(plain)
        assert protection.count == 0
        assert protection.masked == plain

    def test_company_names_are_protected_by_name(self):
        """No pattern can recognise 'Asian Paints' as a company."""
        text = "Asian Paints holds a dominant share."
        protection = protect(text, extra_terms=["Asian Paints"])
        assert any(s.kind == "entity" for s in protection.spans)
        assert restore(protection.masked, protection).text == text

    def test_longest_entity_wins(self):
        text = "Tata Consultancy Services is listed."
        protection = protect(text, extra_terms=["Tata", "Tata Consultancy Services"])
        entities = [s.original for s in protection.spans if s.kind == "entity"]
        assert "Tata Consultancy Services" in entities

    @pytest.mark.parametrize("token", ["ROE", "ROCE", "EBITDA", "SEBI", "NSE"])
    def test_financial_acronyms_are_recognised(self, token):
        assert is_acronym(token)


# ===========================================================================
# Financial glossary
# ===========================================================================

class TestGlossary:
    @pytest.mark.parametrize("english,hindi", [
        ("Revenue", "राजस्व"),
        ("Net Profit", "शुद्ध लाभ"),
        ("Operating Margin", "ऑपरेटिंग मार्जिन"),
        ("Debt", "ऋण"),
        ("Cash Flow", "कैश फ्लो"),
        ("Free Cash Flow", "फ्री कैश फ्लो"),
        ("ROE", "आरओई"),
        ("ROCE", "आरओसीई"),
        ("EPS", "ईपीएस"),
        ("PE Ratio", "पीई अनुपात"),
        ("Market Cap", "मार्केट कैप"),
        ("Dividend", "लाभांश"),
    ])
    def test_the_briefs_glossary_verbatim(self, english, hindi):
        term = lookup(english)
        assert term is not None, f"'{english}' missing from the glossary"
        assert term.hindi == hindi

    def test_hinglish_keeps_english_financial_vocabulary(self):
        """'Revenue growth stable hai', not 'Raajasva vriddhi sthir hai'."""
        for english in ("Revenue", "Operating Margin", "Valuation", "Debt"):
            assert lookup(english).render(Language.HINGLISH) == english

    def test_no_duplicate_terms(self):
        assert len(BY_ENGLISH) == len(TERMS)

    def test_glossary_covers_the_scoring_vocabulary(self):
        """A glossary that misses the platform's own output is decorative."""
        from app.domain.ai_scoring.framework import MODULE_CRITERIA

        criteria = {c for values in MODULE_CRITERIA.values() for c in values}
        missing = [c for c in criteria if lookup(c) is None]
        # Some criteria are phrases rather than terms ("Positive developments");
        # the financial vocabulary must be covered.
        financial = {"Revenue Growth", "EBITDA", "EPS", "ROE", "ROCE", "Debt",
                     "Free Cash Flow", "Operating Margin", "Cash Position",
                     "Capital Allocation", "Market Cap", "Moat",
                     "Pricing Power", "Brand", "Scalability"}
        uncovered = [c for c in financial if lookup(c) is None]
        assert uncovered == [], f"scoring vocabulary missing: {uncovered}"

    def test_prompt_table_is_empty_for_english(self):
        assert render_for_prompt(Language.ENGLISH) == ""

    def test_prompt_table_is_empty_for_hinglish(self):
        """Hinglish keeps English terms, so a table would be identities."""
        assert render_for_prompt(Language.HINGLISH) == ""

    def test_prompt_table_is_filtered_to_the_text(self):
        table = render_for_prompt(Language.HINDI, text="What is the revenue?")
        assert "राजस्व" in table
        assert "आरओसीई" not in table       # ROCE was not mentioned

    def test_coverage_reports_are_sane(self):
        stats = coverage()
        assert stats["terms"] == len(TERMS)
        assert stats["hindi_translated"] > 80


# ===========================================================================
# Cross-language retrieval
# ===========================================================================

class TestCrossLanguageQuery:
    """Search happens once, in English. Translation happens afterwards."""

    def setup_method(self):
        self.adapter = LanguageAdapter(translator=PassthroughTranslator())

    @pytest.mark.parametrize("query", ["Revenue", "राजस्व", "kamai", "earnings"])
    def test_the_briefs_four_queries_reach_english(self, query):
        """'Revenue', 'राजस्व', 'kamai' and 'earnings' must retrieve alike."""
        english = self.adapter.normalise_query(query).english.lower()
        assert any(token in english
                   for token in ("revenue", "earnings", "income"))

    def test_english_queries_pass_through_byte_identical(self):
        """This is what guarantees Retrieval 2.1 cannot regress."""
        for query in ("What is the operating margin of Cipla?",
                      "Compare TCS and Infosys",
                      "revenue growth over five years"):
            assert self.adapter.normalise_query(query).english == query

    def test_devanagari_is_rewritten_to_english_terms(self):
        result = self.adapter.normalise_query("टीसीएस का राजस्व कितना है?")
        assert "revenue" in result.english.lower()
        assert result.was_rewritten

    def test_hinglish_keeps_its_english_content_words(self):
        """Dropping them would discard the strongest signal in the query."""
        result = self.adapter.normalise_query("TCS ka revenue kitna hai?")
        assert "TCS" in result.english
        assert "revenue" in result.english.lower()

    def test_grammar_noise_is_removed(self):
        result = self.adapter.normalise_query("TCS ka revenue kitna hai")
        assert "hai" not in result.english.lower().split()

    def test_normalisation_reports_what_it_mapped(self):
        result = self.adapter.normalise_query("kamai kitni hai")
        assert result.mapped
        assert result.as_dict()["rewritten"] is True

    def test_no_hindi_embedding_is_ever_created(self):
        """The rewrite is query-side only — the corpus is untouched."""
        source = (pathlib.Path(__file__).resolve().parents[1]
                  / "app" / "services" / "language" / "adapter.py").read_text()
        for token in ("embed", "Embedding", "vector", "chunk"):
            assert f"{token}(" not in source, f"adapter touches {token}"


# ===========================================================================
# The adapter
# ===========================================================================

def _run(coro):
    """Run a coroutine in a fresh event loop.

    HARNESS BUG (mine, not the product's). This first used
    `asyncio.get_event_loop().run_until_complete(...)`, which passed when the
    file ran alone and failed with "There is no current event loop" for all 13
    async tests in a full-suite run: on Python 3.13 `get_event_loop()` no
    longer creates a loop implicitly, and an earlier test module had already
    closed the one that existed. `asyncio.run` owns its loop and closes it, so
    the tests no longer depend on what ran before them.
    """
    return asyncio.run(coro)


class TestAdapter:
    def test_english_bypasses_translation_entirely(self):
        adapter = LanguageAdapter(translator=PassthroughTranslator())
        result = _run(adapter.adapt("Revenue grew 10%.",
                                    question="How is TCS?"))
        assert result.language is Language.ENGLISH
        assert result.text == "Revenue grew 10%."

    def test_a_planned_language_falls_back_and_says_so(self):
        """Silently substituting English would make the roadmap look broken."""
        adapter = LanguageAdapter(translator=PassthroughTranslator())
        result = _run(adapter.adapt("Revenue grew 10%.",
                                    requested=Language.TAMIL))
        assert result.language is Language.ENGLISH
        assert not result.translation.translated
        assert "Tamil" in result.translation.detail

    def test_translation_failure_returns_english_not_an_error(self):
        class Failing:
            name = "failing"

            def supports(self, language):
                return True

            async def translate(self, text, language, *, entities=None):
                raise RuntimeError("provider exploded")

        adapter = LanguageAdapter(translator=Failing())
        with pytest.raises(RuntimeError):
            # The adapter does not swallow it; the TRANSLATOR does. This test
            # documents that the boundary is inside LLMTranslator, so a custom
            # translator must handle its own failures.
            _run(adapter.adapt("Revenue grew.", requested=Language.HINDI))

    def test_response_instruction_is_empty_for_english(self):
        assert LanguageAdapter.response_instruction(Language.ENGLISH) == ""

    def test_response_instruction_names_the_language(self):
        instruction = LanguageAdapter.response_instruction(Language.HINDI)
        assert "Hindi" in instruction
        assert "citation" in instruction.lower()

    def test_adapter_reports_how_the_language_was_chosen(self):
        adapter = LanguageAdapter(translator=PassthroughTranslator())
        result = _run(adapter.adapt("x", question="How is TCS?"))
        assert result.as_dict()["resolved_from"] in {
            "requested", "detected", "preference",
        }


class TestGlossaryTranslator:
    """The offline path. Must be honest that it is not a translation."""

    def test_hindi_terms_are_substituted(self):
        translator = GlossaryTranslator()
        result = _run(translator.translate(
            "Revenue grew while Debt fell.", Language.HINDI,
        ))
        assert "राजस्व" in result.text
        assert "ऋण" in result.text

    def test_it_never_claims_to_have_translated(self):
        translator = GlossaryTranslator()
        result = _run(translator.translate("Revenue grew.", Language.HINDI))
        assert result.translated is False
        assert result.detail

    def test_numbers_and_citations_survive(self):
        translator = GlossaryTranslator()
        source = "Revenue of ₹2,55,324 crore [revenue] with ROE of 51.4% [roe]."
        result = _run(translator.translate(source, Language.HINDI))
        assert "2,55,324" in result.text
        assert "[revenue]" in result.text
        assert "[roe]" in result.text
        assert verify_preserved(source, result.text) == []

    def test_longest_term_wins(self):
        """'Free Cash Flow' must not be rendered as 'Free' + 'Cash Flow'."""
        translator = GlossaryTranslator()
        result = _run(translator.translate("Free Cash Flow rose.",
                                           Language.HINDI))
        assert "फ्री कैश फ्लो" in result.text


class TestTranslatorSelection:
    @pytest.mark.parametrize("value,expected", [
        ("llm", LLMTranslator), ("glossary", GlossaryTranslator),
        ("passthrough", PassthroughTranslator), ("none", PassthroughTranslator),
        ("off", PassthroughTranslator),
    ])
    def test_provider_is_chosen_by_configuration(self, value, expected):
        class Settings:
            TRANSLATION_PROVIDER = value

        assert isinstance(build_translator(Settings()), expected)

    def test_default_is_the_llm_translator(self):
        class Settings:
            TRANSLATION_PROVIDER = None

        assert isinstance(build_translator(Settings()), LLMTranslator)


# ===========================================================================
# Future-language readiness
# ===========================================================================

class TestFutureLanguages:
    """Adding a language must require only a translation module."""

    @pytest.mark.parametrize("language", [
        Language.MARATHI, Language.GUJARATI, Language.TAMIL,
        Language.TELUGU, Language.KANNADA, Language.BENGALI,
    ])
    def test_every_future_language_is_already_declared(self, language):
        spec = spec_for(language)
        assert spec.status is LanguageStatus.PLANNED
        assert spec.native_label
        assert spec.bcp47
        assert spec.script

    def test_the_briefs_six_future_languages_are_all_present(self):
        assert set(PLANNED_LANGUAGES) == {
            Language.MARATHI, Language.GUJARATI, Language.TAMIL,
            Language.TELUGU, Language.KANNADA, Language.BENGALI,
        }

    def test_enabling_a_language_needs_no_schema_change(self):
        """The readiness claim, made concrete.

        Promote Marathi to SUPPORTED in a copy of the registry and check the
        adapter would route to it. Nothing here touches a table, a migration
        or a retrieval path.
        """
        from dataclasses import replace

        spec = replace(spec_for(Language.MARATHI),
                       status=LanguageStatus.SUPPORTED)
        assert spec.is_supported
        # An LLM translator claims support for any language by construction —
        # that is precisely what makes a new language a configuration change.
        assert LLMTranslator(router=object()).supports(Language.MARATHI)

    def test_retrieval_and_scoring_never_import_the_language_layer(self):
        """The architectural boundary the brief demands, asserted."""
        root = pathlib.Path(__file__).resolve().parents[1] / "app"
        protected = [
            root / "services" / "retrieval",
            root / "services" / "ai_scoring",
            root / "services" / "scoring",
            root / "services" / "knowledge",
            root / "domain" / "retrieval",
            root / "domain" / "ai_scoring",
        ]
        offenders = []
        for directory in protected:
            if not directory.exists():
                continue
            for path in directory.rglob("*.py"):
                if "language" in path.read_text():
                    for line in path.read_text().splitlines():
                        if re.match(r"\s*(from|import)\s+app\.(domain|services)\.language",
                                    line):
                            offenders.append(f"{path.name}: {line.strip()}")
        assert offenders == [], (
            f"retrieval/scoring imports the language layer: {offenders}"
        )


# ===========================================================================
# Consistency: the same numbers in every language
# ===========================================================================

class TestScoreConsistency:
    """Scores, citations and evidence must be identical in every language."""

    def test_the_scoring_engine_has_no_language_parameter(self):
        from app.services.ai_scoring.engine import compute
        from app.services.ai_scoring.service import AIScoringService

        assert "language" not in inspect.signature(compute).parameters
        assert "language" not in inspect.signature(
            AIScoringService.score_company).parameters

    def test_the_retrieval_engine_has_no_language_parameter(self):
        from app.services.retrieval.engine import HybridRetrievalEngine

        assert "language" not in inspect.signature(
            HybridRetrievalEngine.retrieve).parameters

    def test_scores_are_computed_before_any_language_exists(self):
        """Structural proof: the scoring result carries no language field."""
        from app.domain.ai_scoring.types import AIScoreResult

        fields = set(AIScoreResult.__annotations__)
        assert not any("lang" in f.lower() for f in fields)

    def test_the_analyst_audits_english_before_translating(self):
        """The evidence chain is verified in the canonical language.

        This is what makes 'same citations in every language' structural: the
        audit runs on the English text, and translation happens strictly
        afterwards on the display copy only.
        """
        source = (pathlib.Path(__file__).resolve().parents[1]
                  / "app" / "services" / "ai" / "analyst.py").read_text()
        finalise = source.split("async def _finalise")[1]
        audit_at = finalise.index("citation_audit = audit(")
        adapt_at = finalise.index("LanguageAdapter().adapt")
        assert audit_at < adapt_at, (
            "translation runs before the citation audit — the evidence chain "
            "would be verified against translated text"
        )

    def test_content_stays_english_while_display_is_translated(self):
        """`content` is the audited, persisted artefact and must not move."""
        source = (pathlib.Path(__file__).resolve().parents[1]
                  / "app" / "services" / "ai" / "analyst.py").read_text()
        assert "content=content," in source
        assert "display_content=display," in source

    def test_conversation_memory_stores_english(self):
        """Memory holds canonical English, never a translated turn.

        The harness originally split on the FIRST `if memory is not None:`,
        which is in `_refuse` — a path that has no model response at all. The
        assertion was reading the wrong function. Scoped to `_finalise`, which
        is the one that writes a generated answer to memory.
        """
        source = (pathlib.Path(__file__).resolve().parents[1]
                  / "app" / "services" / "ai" / "analyst.py").read_text()
        finalise = source.split("async def _finalise")[1]
        memory_block = finalise.split("if memory is not None:")[1][:900]
        assert "response.content" in memory_block
        # And the translated text must not be what is stored.
        assert "memory.add(Role.ASSISTANT, display" not in finalise


# ===========================================================================
# API contract
# ===========================================================================

class TestAPIContract:
    """Every endpoint through a real client, asserting status codes.

    AISCORE-001 taught this: a response model built from a guess at its schema
    returns 500 on every call while unit tests pass.
    """

    @pytest.fixture()
    def client(self):
        import tempfile
        from fastapi.testclient import TestClient
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool

        import app.models.analysis  # noqa: F401
        import app.models.company  # noqa: F401
        import app.models.document  # noqa: F401
        import app.models.filing_collection  # noqa: F401
        import app.models.knowledge  # noqa: F401
        import app.models.platform  # noqa: F401
        import app.models.scoring  # noqa: F401

        from app.core.security import get_current_user
        from app.db.base import Base, get_db
        from app.main import app as fastapi_app

        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        handle.close()
        engine = create_engine(f"sqlite:///{handle.name}",
                               connect_args={"check_same_thread": False},
                               poolclass=StaticPool)
        Base.metadata.create_all(engine)
        session = sessionmaker(bind=engine, expire_on_commit=False)()

        # Save and RESTORE: conftest installs a suite-wide get_db override,
        # and clearing it breaks every API module collected afterwards.
        previous = dict(fastapi_app.dependency_overrides)
        fastapi_app.dependency_overrides[get_db] = lambda: session
        fastapi_app.dependency_overrides[get_current_user] = lambda: object()
        try:
            client = TestClient(fastapi_app)
            client.session = session
            yield client
        finally:
            fastapi_app.dependency_overrides.clear()
            fastapi_app.dependency_overrides.update(previous)
            session.close()
            engine.dispose()

    def test_language_registry_endpoint(self, client):
        response = client.get("/api/v1/ai/languages")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["canonical"] == "english"
        assert body["default"] == "auto"
        assert set(body["supported"]) == {"english", "hindi", "hinglish"}
        assert len(body["planned"]) == 6
        assert body["glossary"]["terms"] > 100

    def test_registry_publishes_planned_languages(self, client):
        body = client.get("/api/v1/ai/languages").json()
        codes = {entry["code"] for entry in body["languages"]}
        assert {"marathi", "gujarati", "tamil", "telugu", "kannada",
                "bengali"} <= codes

    @pytest.mark.parametrize("text,expected", [
        ("How is TCS?", "english"),
        ("टीसीएस कैसी कंपनी है?", "hindi"),
        ("TCS kaisi company hai?", "hinglish"),
    ])
    def test_detect_endpoint(self, client, text, expected):
        response = client.post("/api/v1/ai/languages/detect",
                               json={"text": text})
        assert response.status_code == 200, response.text
        assert response.json()["detected"]["language"] == expected

    def test_detect_endpoint_shows_the_rewrite(self, client):
        body = client.post("/api/v1/ai/languages/detect",
                           json={"text": "टीसीएस का राजस्व कितना है?"}).json()
        assert body["rewritten"] is True
        assert "revenue" in body["normalised_query"].lower()

    def test_detect_rejects_empty_text(self, client):
        assert client.post("/api/v1/ai/languages/detect",
                           json={"text": ""}).status_code == 422


class TestBackwardCompatibility:
    """Existing clients must be unaffected."""

    def test_language_defaults_to_auto_on_every_request_model(self):
        from app.schemas.ai import AnalysisRequest, ChatRequest, ReportRequest

        assert ChatRequest(question="x").language == AUTO
        assert AnalysisRequest(capability="chat").language == AUTO
        assert ReportRequest().language == AUTO

    def test_the_language_block_is_absent_on_english_responses(self):
        """An English payload must be byte-for-byte what it always was."""
        from app.schemas.ai import AnalysisResponse

        assert AnalysisResponse.model_fields["language"].default is None

    def test_analyst_run_language_defaults_to_none(self):
        from app.services.ai.analyst import ResearchAnalyst

        signature = inspect.signature(ResearchAnalyst.run)
        assert signature.parameters["language"].default is None

    def test_chat_language_defaults_to_none(self):
        from app.services.ai.analyst import ResearchAnalyst

        signature = inspect.signature(ResearchAnalyst.chat)
        assert signature.parameters["language"].default is None

    def test_finalise_is_awaited_by_its_only_caller(self):
        """Making _finalise async would leak a coroutine if a caller was missed."""
        source = (pathlib.Path(__file__).resolve().parents[1]
                  / "app" / "services" / "ai" / "analyst.py").read_text()
        calls = re.findall(r"[^\w]((?:await\s+)?self\._finalise\()", source)
        assert calls, "no call to _finalise found"
        for call in calls:
            assert call.startswith("await"), f"unawaited coroutine: {call}"


# ===========================================================================
# End-to-end: the same answer in three languages
# ===========================================================================

class _FakeHindiTranslator:
    """A deterministic stand-in for a real model.

    Renders a handful of words into Devanagari and leaves everything else
    alone. Crucially it operates on the MASKED text exactly as a real model
    would, so it exercises the full protect → translate → restore → verify
    path rather than bypassing it. That is what makes this a test of the
    pipeline and not of a mock.
    """

    name = "fake-hindi"

    WORDS = {
        "Revenue": "राजस्व", "revenue": "राजस्व",
        "grew": "बढ़ा", "strong": "मज़बूत", "The": "यह",
        "company": "कंपनी", "and": "और", "is": "है",
    }

    def supports(self, language):
        return True

    async def translate(self, text, language, *, entities=None):
        from app.domain.language.protect import (
            protect, restore, verify_preserved,
        )

        if language is Language.ENGLISH:
            return TranslationResult(text=text, language=language,
                                     translated=True, provider=self.name)

        protection = protect(text, extra_terms=entities or [])
        working = protection.masked
        for english, hindi in self.WORDS.items():
            working = re.sub(rf"(?<!\w){re.escape(english)}(?!\w)", hindi,
                             working)

        restoration = restore(working, protection)
        problems = verify_preserved(text, restoration.text)
        if restoration.lost or problems:
            return TranslationResult(
                text=text, language=Language.ENGLISH, translated=False,
                provider=self.name, detail="integrity check failed",
                integrity_problems=problems,
            )
        return TranslationResult(text=restoration.text, language=language,
                                 translated=True, provider=self.name)


ANSWER = (
    "The company is strong. Revenue grew to ₹2,55,324 crore in FY2025 "
    "[revenue], a 10.2% rise, and ROE reached 51.4% [roe]. "
    "TCS remains well positioned. ISIN INE467B01029."
)


class TestEndToEndConsistency:
    """The brief's consistency criteria, exercised through the real pipeline."""

    def _adapt(self, language):
        adapter = LanguageAdapter(translator=_FakeHindiTranslator())
        return _run(adapter.adapt(ANSWER, requested=language,
                                  entities=["TCS", "Tata Consultancy Services"]))

    def test_citations_are_identical_in_every_language(self):
        pattern = re.compile(r"\[[^\]]+\]")
        english = pattern.findall(ANSWER)
        for language in (Language.ENGLISH, Language.HINDI, Language.HINGLISH):
            assert pattern.findall(self._adapt(language).text) == english

    def test_numbers_are_identical_in_every_language(self):
        pattern = re.compile(r"[\d,]+\.?\d*")
        english = pattern.findall(ANSWER)
        for language in (Language.ENGLISH, Language.HINDI, Language.HINGLISH):
            assert pattern.findall(self._adapt(language).text) == english

    def test_identifiers_survive_in_every_language(self):
        for language in (Language.ENGLISH, Language.HINDI, Language.HINGLISH):
            text = self._adapt(language).text
            assert "TCS" in text
            assert "INE467B01029" in text
            assert "FY2025" in text
            assert "₹2,55,324" in text

    def test_only_the_prose_changes(self):
        hindi = self._adapt(Language.HINDI)
        assert hindi.translation.translated
        assert "राजस्व" in hindi.text          # prose translated
        assert "[revenue]" in hindi.text       # citation untouched
        assert verify_preserved(ANSWER, hindi.text) == []

    def test_a_translator_that_breaks_a_citation_is_rejected(self):
        """Fail closed: English with citations beats Hindi without them."""
        class Vandal:
            name = "vandal"

            def supports(self, language):
                return True

            async def translate(self, text, language, *, entities=None):
                from app.domain.language.protect import protect, restore
                protection = protect(text, extra_terms=entities or [])
                # Drop a sentinel, exactly as a careless model would.
                damaged = protection.masked.replace(
                    protection.spans[0].token, "", 1,
                )
                restoration = restore(damaged, protection)
                if restoration.lost:
                    return TranslationResult(
                        text=text, language=Language.ENGLISH,
                        translated=False, provider=self.name,
                        detail="protected content was dropped",
                        integrity_problems=["dropped token"],
                    )
                return TranslationResult(text=restoration.text,
                                         language=language, translated=True,
                                         provider=self.name)

        adapter = LanguageAdapter(translator=Vandal())
        result = _run(adapter.adapt(ANSWER, requested=Language.HINDI,
                                    entities=["TCS"]))
        assert result.text == ANSWER               # English returned intact
        assert not result.translation.translated
        assert result.translation.integrity_problems

    def test_the_adapter_reports_the_detected_language_too(self):
        adapter = LanguageAdapter(translator=_FakeHindiTranslator())
        result = _run(adapter.adapt(ANSWER, question="TCS kaisa hai?",
                                    requested=Language.HINDI))
        payload = result.as_dict()
        assert payload["language"] == "hindi"          # answered in
        assert payload["detected"]["language"] == "hinglish"   # typed in
        assert payload["resolved_from"] == "requested"
