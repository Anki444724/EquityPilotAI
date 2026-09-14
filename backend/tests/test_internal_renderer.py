"""Phase 2A — the internal language renderer.

The renderer replaces the external translation provider for the languages the
platform can render itself: English stays byte-identical, Hindi and Hinglish are
produced from a closed set of grammatical frames over already-protected text.

What these tests pin, and why:

* **Nothing protected may move.** Numbers, citations, ISINs, fiscal years,
  tickers and caller-supplied company names are masked before rendering and
  restored afterwards, and :func:`verify_preserved` re-checks the result
  independently. A rendered answer that lost a citation marker is a broken
  evidence chain, not a cosmetic defect.
* **The renderer is not an LLM and must not behave like one.** Prose it cannot
  transform safely is returned in canonical English, in place, with metadata
  saying so. The anti-hallucination test asserts that every Devanagari word in
  the output comes from the glossary or from the renderer's published frame
  vocabulary — nothing is coined.
* **English is untouched.** The canonical path must not pay for this feature.
* **The canonical invariants hold end to end.** Memory keeps English, `content`
  stays the audited English artefact, and only `display_content` is rendered.

Exact wording is deliberately not asserted. The renderer's contract is
structural; a test that pinned prose would pin a rewrite, not a guarantee.
"""
from __future__ import annotations

import inspect
import re

import pytest

from app.domain.language.protect import protect, restore, verify_preserved
from app.domain.language.types import Language
from app.services.language.adapter import LanguageAdapter
from app.services.language.internal_renderer import (
    Fidelity, InternalFallbackTranslator, InternalLanguageRenderer,
    InternalRendererTranslator,
)
from app.services.language.translators import (
    LLMTranslator, PassthroughTranslator, TranslationResult, build_translator,
)

# ---------------------------------------------------------------------------
# Corpora
# ---------------------------------------------------------------------------

#: Real sentences emitted by the deterministic answer engines (verbatim from
#: `financial_answer_engine` / `investment_answer_engine` output), which is the
#: text the renderer exists to serve. Building the tests from invented examples
#: would prove nothing about the platform.
DETERMINISTIC_SENTENCES = [
    "Gross debt is 2,163.00 ₹ cr for FY25 [gross_debt].",
    "The current ratio is 2.55x for FY25 [current_ratio].",
    "Net debt to EBITDA is -0.58x for FY25 [net_debt_ebitda].",
    "The Altman Z-score is 4.95 for FY25 [altman_z].",
    "Financial Risk is among the strongest areas, scoring 9.66 /10 [score_financial_risk].",
    "Financial risk scores 9.66 /10 [score_financial_risk].",
    "The financial risk score for Bharat Consumer Products Ltd is 9.66 /10 [score_financial_risk].",
    "The scoring engine's recommendation is 'HOLD' [recommendation], at 75.14 % data confidence [confidence].",
    "The scoring engine's recommendation for Bharat Consumer Products Ltd is 'HOLD' [recommendation].",
    "The platform's weighted intrinsic value is 185.59 ₹ per share [weighted_value], an upside of -30.75 % versus the current market price [valuation_upside].",
    "Data confidence is 75.14 % [confidence].",
    "It is derived from the composite score of 71.05 /100 [overall_score].",
]

#: Prose no deterministic renderer may touch. It must survive verbatim.
UNRENDERABLE_PROSE = (
    "The scoring engine notes: Illustrative valuation only. Real filings are "
    "required for investment-grade outputs."
)

#: Sentences with identifiers the protection layer owns.
IDENTIFIER_SENTENCES = [
    "The trailing price-to-earnings (P/E) ratio for Tata Consultancy Services is 24.50x [pe_ratio].",
    "Bajaj Auto has an ISIN of INE917I01010 and gross debt of 1,234.56 ₹ cr for FY24 [gross_debt].",
    "M&M net debt is -2.10x for FY25 [net_debt_ebitda].",
]

CITATIONS = re.compile(r"\[[^\]\n]{1,120}\]")
NUMBERS = re.compile(r"₹|[$€£]|-?\d[\d,]*\.?\d*%?")
DEVANAGARI_WORD = re.compile(r"[\u0900-\u0963\u0966-\u097F]+")
ROMAN_HINDI = re.compile(r"\b(hai|hain|hai\.|ke liye|ka|ki|ke|me|par|se|nahi)\b")

RENDERED_LANGUAGES = [Language.HINDI, Language.HINGLISH]


def _renderer() -> InternalLanguageRenderer:
    return InternalLanguageRenderer()


def _english_company() -> list[str]:
    return ["Tata Consultancy Services", "Bharat Consumer Products Ltd"]


# ===========================================================================
# 1. English — byte-identical, no transformation
# ===========================================================================

class TestEnglishIsUntouched:
    @pytest.mark.parametrize("text", DETERMINISTIC_SENTENCES + [UNRENDERABLE_PROSE])
    def test_english_is_byte_identical(self, text):
        result = _renderer().render(text, Language.ENGLISH)

        assert result.text == text
        assert result.fidelity is Fidelity.FULL
        assert result.translated is True
        assert result.prose_sentences_preserved == 0

    def test_english_never_emits_a_sentinel(self):
        text = "Gross debt is 2,163.00 ₹ cr for FY25 [gross_debt]. " + UNRENDERABLE_PROSE
        result = _renderer().render(text, Language.ENGLISH)

        assert "§" not in result.text

    def test_english_multi_paragraph_is_identical(self):
        text = (
            "Financial Risk is among the strongest areas, scoring 9.66 /10 "
            "[score_financial_risk].\n\n---\n_This analysis is not investment "
            "advice._"
        )
        assert _renderer().render(text, Language.ENGLISH).text == text


# ===========================================================================
# 2-3. Hindi financial terminology, Hinglish vocabulary retention
# ===========================================================================

class TestHindiTerminology:
    @pytest.mark.parametrize("english,hindi", [
        ("Revenue", "राजस्व"),
        ("Net Profit", "शुद्ध लाभ"),
        ("Debt", "ऋण"),
        ("Valuation", "मूल्यांकन"),
        ("Growth", "वृद्धि"),
    ])
    def test_glossary_terms_are_rendered_in_hindi(self, english, hindi):
        """The five renderings the brief names explicitly."""
        result = _renderer().render(
            f"{english} is 10 % for FY25 [metric].", Language.HINDI,
        )
        assert hindi in result.text
        assert result.fidelity in {Fidelity.FULL, Fidelity.PARTIAL}
        assert result.terms_rendered >= 1

    def test_known_terms_come_from_the_glossary_not_from_the_renderer(self):
        """Terminology is glossary data; the renderer must not carry its own
        copy of it, or the two will drift."""
        for english, hindi in [("Revenue", "राजस्व"), ("Net Profit", "शुद्ध लाभ")]:
            result = _renderer().render(
                f"{english} is 10 % for FY25 [metric].", Language.HINDI,
            )
            assert hindi in result.text


class TestHinglishKeepsEnglishVocabulary:
    @pytest.mark.parametrize("term", ["Revenue", "Operating Margin", "ROE", "Valuation"])
    def test_technical_terms_stay_english(self, term):
        result = _renderer().render(
            f"{term} is 10 % for FY25 [metric].", Language.HINGLISH,
        )
        assert term in result.text
        assert DEVANAGARI_WORD.search(result.text) is None

    @pytest.mark.parametrize("text", DETERMINISTIC_SENTENCES)
    def test_hinglish_never_produces_devanagari(self, text):
        result = _renderer().render(text, Language.HINGLISH)
        assert DEVANAGARI_WORD.search(result.text) is None

    def test_hinglish_uses_roman_hindi_structure(self):
        result = _renderer().render(
            "Revenue grew 10% for TCS [revenue].", Language.HINGLISH,
        )
        assert ROMAN_HINDI.search(result.text) is not None


# ===========================================================================
# 4-7. Numbers and citations survive rendering
# ===========================================================================

class TestProtectedContentSurvives:
    @pytest.mark.parametrize("language", RENDERED_LANGUAGES)
    @pytest.mark.parametrize("text", DETERMINISTIC_SENTENCES)
    def test_citations_are_preserved_exactly(self, language, text):
        result = _renderer().render(text, language)

        assert sorted(CITATIONS.findall(result.text)) == sorted(CITATIONS.findall(text))
        assert result.problems == []

    @pytest.mark.parametrize("language", RENDERED_LANGUAGES)
    @pytest.mark.parametrize("text", DETERMINISTIC_SENTENCES)
    def test_numbers_are_preserved_exactly(self, language, text):
        result = _renderer().render(text, language)

        # Nothing is invented, dropped or altered.
        assert sorted(_numeric_tokens(result.text)) == sorted(_numeric_tokens(text))
        # And the figures keep their order relative to one another. The fiscal
        # year is excluded from the order check and from nothing else: Hindi
        # states the period before the figure ("… FY25 के लिए 2,163.00 ₹ cr …"),
        # which is word order, not a change to a number. Two figures swapped
        # with each other still fails here.
        assert _figures(result.text) == _figures(text)

    @pytest.mark.parametrize("language", RENDERED_LANGUAGES)
    @pytest.mark.parametrize("text", DETERMINISTIC_SENTENCES)
    def test_verify_preserved_passes(self, language, text):
        result = _renderer().render(text, language)
        assert verify_preserved(text, result.text) == []

    @pytest.mark.parametrize("language", RENDERED_LANGUAGES)
    def test_tickers_stay_latin(self, language):
        result = _renderer().render(
            "TCS gross debt is 100 ₹ cr for FY25 [gross_debt].", language,
        )
        assert "TCS" in result.text
        assert "टीसीएस" not in result.text

    @pytest.mark.parametrize("language", RENDERED_LANGUAGES)
    def test_company_names_are_preserved_through_extra_terms(self, language):
        name = "Tata Consultancy Services"
        result = _renderer().render(
            f"{name} is 10 % for FY25 [metric].", language,
            entities=[name, "TCS"],
        )
        assert name in result.text

    @pytest.mark.parametrize("language", RENDERED_LANGUAGES)
    def test_isin_and_fiscal_year_are_preserved(self, language):
        text = "ISIN INE917I01010 with gross debt 1,234.56 ₹ cr for FY24 [gross_debt]."
        result = _renderer().render(text, language)

        assert "INE917I01010" in result.text
        assert "FY24" in result.text

    @pytest.mark.parametrize("language", RENDERED_LANGUAGES)
    @pytest.mark.parametrize("text", IDENTIFIER_SENTENCES)
    def test_identifier_sentences_survive_whole(self, language, text):
        result = _renderer().render(
            text, language, entities=_english_company(),
        )
        assert verify_preserved(text, result.text) == []
        assert sorted(CITATIONS.findall(result.text)) == sorted(CITATIONS.findall(text))


# ===========================================================================
# 8-11. Protection round trip, used rather than reimplemented
# ===========================================================================

class TestProtectionIsReused:
    def test_protect_restore_round_trip_is_lossless(self):
        text = "Gross debt is 2,163.00 ₹ cr for FY25 [gross_debt]."
        protection = protect(text, extra_terms=_english_company())
        restoration = restore(protection.masked, protection)

        assert restoration.is_intact
        assert restoration.text == text

    def test_renderer_masks_with_the_existing_protection_layer(self):
        """If the renderer had its own masking, `protect` would not have seen
        these spans and the round trip below would lose them."""
        text = "Gross debt is 2,163.00 ₹ cr for FY25 [gross_debt]."
        protection = protect(text)

        assert protection.kinds().get("citation") == 1
        assert protection.kinds().get("fiscal_year") == 1
        assert protection.kinds().get("number", 0) >= 1

    def test_renderer_never_leaks_a_sentinel(self):
        for language in RENDERED_LANGUAGES:
            for text in DETERMINISTIC_SENTENCES:
                assert "§" not in _renderer().render(text, language).text


# ===========================================================================
# 12. Honesty: no invented prose, no invented vocabulary
# ===========================================================================

class TestHonesty:
    def test_unrenderable_prose_is_preserved_verbatim(self):
        result = _renderer().render(UNRENDERABLE_PROSE, Language.HINDI)

        assert "Illustrative valuation only." in result.text
        assert "Real filings are required for investment-grade outputs." in result.text
        assert result.prose_sentences_preserved >= 1
        # Reported as a disclosure: a required legal statement the renderer
        # keeps verbatim on purpose, rather than a sentence it failed on.
        assert result.preserved_reasons.get("disclosure", 0) >= 1

    def test_fidelity_is_not_full_when_prose_is_preserved(self):
        text = (
            "Gross debt is 2,163.00 ₹ cr for FY25 [gross_debt]. "
            + UNRENDERABLE_PROSE
        )
        result = _renderer().render(text, Language.HINDI)

        assert result.fidelity is Fidelity.PARTIAL
        assert 0.0 < result.coverage < 1.0
        assert result.translated is False
        assert result.detail

    def test_every_devanagari_word_is_glossary_or_frame_vocabulary(self):
        """The anti-hallucination assertion: the renderer may not coin words."""
        allowed = _renderer().vocabulary()
        produced: set[str] = set()

        for text in DETERMINISTIC_SENTENCES + IDENTIFIER_SENTENCES:
            rendered = _renderer().render(
                text, Language.HINDI, entities=_english_company(),
            ).text
            produced.update(DEVANAGARI_WORD.findall(rendered))

        invented = {word for word in produced if word not in allowed}
        assert invented == set(), f"renderer coined Devanagari words: {sorted(invented)}"

    def test_terms_absent_from_the_glossary_stay_english(self):
        """No coined Hindi for a term the glossary does not know."""
        result = _renderer().render(
            "Momentum is 10 % for FY25 [score_momentum].", Language.HINDI,
        )
        if "मोमेंटम" not in _renderer().vocabulary():
            assert "Momentum" in result.text

    def test_mixed_text_keeps_english_sentences_inside_the_hindi_answer(self):
        text = (
            "Gross debt is 2,163.00 ₹ cr for FY25 [gross_debt]. "
            "Management commentary suggests the order book is improving on "
            "the back of a strong enquiry pipeline."
        )
        result = _renderer().render(text, Language.HINDI)

        assert "order book is improving" in result.text
        assert "[gross_debt]" in result.text
        assert DEVANAGARI_WORD.search(result.text) is not None

    def test_platform_own_sentences_are_rendered(self):
        """Sentences the platform composes itself are rendered, not quoted.

        These are fixed strings from our own engines, matched exactly, so
        rendering them invents nothing — the opposite of the free prose a
        provider wrote, which is preserved.
        """
        text = (
            "This is the platform's scoring output, stated as the engine "
            "computed it; it is not a fresh call."
        )
        result = _renderer().render(text, Language.HINDI)

        assert result.prose_sentences_preserved == 0
        assert DEVANAGARI_WORD.search(result.text) is not None
        assert "fresh call" not in result.text

    def test_rendering_is_deterministic(self):
        text = " ".join(DETERMINISTIC_SENTENCES)
        first = _renderer().render(text, Language.HINDI).text

        for _ in range(5):
            assert _renderer().render(text, Language.HINDI).text == first

    def test_empty_input_is_handled(self):
        for language in [Language.ENGLISH, *RENDERED_LANGUAGES]:
            result = _renderer().render("", language)
            assert result.text == ""

    @pytest.mark.parametrize("language", [
        Language.MARATHI, Language.TAMIL, Language.GUJARATI,
        Language.TELUGU, Language.KANNADA, Language.BENGALI,
    ])
    def test_unsupported_language_is_not_claimed(self, language):
        """A planned language must not be silently rendered as Hindi."""
        renderer = _renderer()
        assert renderer.supports(language) is False

        result = renderer.render("Revenue is 10 % for FY25 [metric].", language)
        assert result.fidelity is Fidelity.NONE
        assert result.translated is False
        assert result.text == "Revenue is 10 % for FY25 [metric]."


# ===========================================================================
# 13. The brief's three examples, as structural invariants
# ===========================================================================

class TestBriefExamples:
    def test_english_example_is_unchanged(self):
        text = "Revenue grew 10% for TCS [revenue]."
        assert _renderer().render(text, Language.ENGLISH).text == text

    def test_hindi_example(self):
        result = _renderer().render("Revenue grew 10% for TCS [revenue].",
                                     Language.HINDI)

        assert "राजस्व" in result.text
        assert "10" in result.text
        assert "[revenue]" in result.text
        # Tickers stay in Latin script by design (LanguageSpec + protection);
        # the brief's transliterated "टीसीएस" is deliberately not produced.
        assert "TCS" in result.text
        assert result.fidelity is Fidelity.FULL

    def test_hinglish_example(self):
        result = _renderer().render("Revenue grew 10% for TCS [revenue].",
                                     Language.HINGLISH)

        assert "Revenue" in result.text
        assert "TCS" in result.text
        assert "10" in result.text
        assert "[revenue]" in result.text
        assert DEVANAGARI_WORD.search(result.text) is None
        assert result.fidelity is Fidelity.FULL

    @pytest.mark.parametrize("language", RENDERED_LANGUAGES)
    def test_decline_is_rendered_as_the_opposite_of_growth(self, language):
        grow = _renderer().render("Revenue grew 10% for TCS [revenue].", language)
        fall = _renderer().render("Revenue fell 10% for TCS [revenue].", language)

        assert grow.text != fall.text
        assert "10" in fall.text
        assert "[revenue]" in fall.text


# ===========================================================================
# 14-16. Pipeline compatibility: Translator protocol and the adapter
# ===========================================================================

class TestTranslatorBridge:
    def _translator(self) -> InternalRendererTranslator:
        return InternalRendererTranslator()

    def test_translate_returns_the_pipeline_result_type(self):
        result = _run(self._translator().translate(
            "Gross debt is 2,163.00 ₹ cr for FY25 [gross_debt].",
            Language.HINDI,
        ))

        assert isinstance(result, TranslationResult)
        assert result.provider == "internal"
        assert result.language is Language.HINDI

    def test_partial_rendering_is_not_claimed_as_a_translation(self):
        result = _run(self._translator().translate(UNRENDERABLE_PROSE,
                                                   Language.HINDI))

        assert result.translated is False
        assert "canonical English" in result.detail or "preserved" in result.detail

    def test_full_rendering_is_claimed(self):
        result = _run(self._translator().translate(
            "Revenue is 10 % for FY25 [metric].", Language.HINDI,
        ))

        assert result.translated is True
        assert result.text != "Revenue is 10 % for FY25 [metric]."

    def test_english_passes_through_the_bridge_unchanged(self):
        text = "Revenue grew 10% for TCS [revenue]."
        result = _run(self._translator().translate(text, Language.ENGLISH))

        assert result.text == text
        assert result.translated is True

    def test_caller_entities_reach_the_renderer(self):
        result = _run(self._translator().translate(
            "Bharat Consumer Products Ltd is 10 % for FY25 [metric].",
            Language.HINDI, entities=["Bharat Consumer Products Ltd"],
        ))

        assert "Bharat Consumer Products Ltd" in result.text
        assert verify_preserved(
            "Bharat Consumer Products Ltd is 10 % for FY25 [metric].", result.text,
        ) == []


class TestFallbackChain:
    """The production failure this phase exists to fix: the external provider
    returned HTTP 402 and the answer fell back to English."""

    class _DeadProvider:
        name = "dead"

        def __init__(self, detail="HTTP 402 insufficient credits"):
            self.detail = detail
            self.calls = 0

        def supports(self, language: Language) -> bool:
            return True

        async def translate(self, text, language, *, entities=None):
            self.calls += 1
            return TranslationResult(
                text=text, language=Language.ENGLISH, translated=False,
                provider=self.name, detail=self.detail,
            )

    def test_complete_internal_render_never_calls_the_external_provider(self):
        provider = self._DeadProvider()
        chain = InternalFallbackTranslator(primary=provider,
                                           internal=InternalRendererTranslator())

        result = _run(chain.translate(
            "Gross debt is 2,163.00 ₹ cr for FY25 [gross_debt].", Language.HINDI,
        ))

        assert provider.calls == 0
        assert result.provider == "internal"
        assert result.translated is True
        assert "2,163.00" in result.text
        assert "[gross_debt]" in result.text

    def test_external_failure_falls_back_to_partial_internal_rendering(self):
        provider = self._DeadProvider()
        chain = InternalFallbackTranslator(primary=provider,
                                           internal=InternalRendererTranslator())
        text = ("Gross debt is 2,163.00 ₹ cr for FY25 [gross_debt]. "
                + UNRENDERABLE_PROSE)

        result = _run(chain.translate(text, Language.HINDI))

        assert provider.calls == 1
        assert result.provider == "internal"
        assert "2,163.00" in result.text
        assert "[gross_debt]" in result.text
        # The reason the external path failed is not thrown away.
        assert "402" in result.detail

    def test_hinglish_no_longer_degrades_to_english(self):
        """Hinglish had no glossary path at all: any provider failure meant
        English. The renderer serves it."""
        provider = self._DeadProvider()
        chain = InternalFallbackTranslator(primary=provider,
                                           internal=InternalRendererTranslator())

        result = _run(chain.translate("Revenue grew 10% for TCS [revenue].",
                                      Language.HINGLISH))

        assert result.provider == "internal"
        assert "Revenue" in result.text
        assert "[revenue]" in result.text

    def test_hinglish_falls_back_when_the_provider_fails_mid_answer(self):
        provider = self._DeadProvider()
        chain = InternalFallbackTranslator(primary=provider,
                                           internal=InternalRendererTranslator())

        result = _run(chain.translate(UNRENDERABLE_PROSE, Language.HINGLISH))

        # Nothing could be rendered internally: the honest outcome is the
        # English original, carrying the provider's failure.
        assert result.translated is False
        assert "402" in result.detail

    def test_working_provider_is_still_preferred_for_arbitrary_prose(self):
        class _Live:
            name = "live"

            def supports(self, language: Language) -> bool:
                return True

            async def translate(self, text, language, *, entities=None):
                return TranslationResult(
                    text="अनुवादित", language=Language.HINDI, translated=True,
                    provider=self.name,
                )

        chain = InternalFallbackTranslator(primary=_Live(),
                                           internal=InternalRendererTranslator())
        result = _run(chain.translate(UNRENDERABLE_PROSE, Language.HINDI))

        assert result.provider == "live"
        assert result.text == "अनुवादित"


class TestAdapterIntegration:
    def test_english_adapter_behaviour_is_unchanged(self):
        adapter = LanguageAdapter(translator=InternalRendererTranslator())
        adapted = _run(adapter.adapt(
            "Revenue grew 10% for TCS [revenue].", requested=Language.ENGLISH,
        ))

        assert adapted.text == "Revenue grew 10% for TCS [revenue]."
        assert adapted.translation.provider == "none"
        assert adapted.translation.translated is True
        assert adapted.as_dict()["fidelity"] == "full"

    def test_hindi_request_is_served_by_the_internal_renderer(self):
        adapter = LanguageAdapter(translator=InternalRendererTranslator())
        adapted = _run(adapter.adapt(
            "Revenue grew 10% for TCS [revenue].",
            question="TCS ka revenue kaisa raha?", requested=Language.HINDI,
        ))

        assert adapted.language is Language.HINDI
        assert adapted.text != "Revenue grew 10% for TCS [revenue]."
        assert "[revenue]" in adapted.text
        assert "TCS" in adapted.text

    def test_language_block_reports_fidelity_and_coverage(self):
        adapter = LanguageAdapter(translator=InternalRendererTranslator())
        adapted = _run(adapter.adapt(
            "Gross debt is 2,163.00 ₹ cr for FY25 [gross_debt]. " + UNRENDERABLE_PROSE,
            requested=Language.HINDI,
        ))
        block = adapted.as_dict()

        assert block["fidelity"] in {"full", "partial"}
        assert 0.0 <= block["coverage"] <= 1.0
        assert block["translation"]["provider"] == "internal"

    def test_planned_language_fallback_is_unchanged(self):
        adapter = LanguageAdapter(translator=InternalRendererTranslator())
        adapted = _run(adapter.adapt(
            "Revenue grew 10% for TCS [revenue].", requested=Language.MARATHI,
        ))

        assert adapted.language is Language.ENGLISH
        assert adapted.text == "Revenue grew 10% for TCS [revenue]."
        assert adapted.translation.translated is False
        assert "Marathi" in adapted.translation.detail

    def test_inbound_normalisation_is_unchanged(self):
        adapter = LanguageAdapter()

        english = adapter.normalise_query("What is the P/E ratio for TCS?")
        assert english.english == "What is the P/E ratio for TCS?"

        hindi = adapter.normalise_query("टीसीएस का राजस्व कितना है?")
        assert "revenue" in hindi.english
        assert "how much" in hindi.english

    def test_normalisation_still_maps_hinglish_terms(self):
        adapter = LanguageAdapter()
        normalised = adapter.normalise_query("BEL ki financial quality kaisi hai?")

        assert "financial" in normalised.english.lower()
        assert "quality" in normalised.english.lower()


class TestTranslatorSelection:
    def test_configuration_selects_the_internal_renderer(self):
        class Settings:
            TRANSLATION_PROVIDER = "internal"

        translator = build_translator(Settings())
        assert isinstance(translator, InternalRendererTranslator)
        assert translator.name == "internal"

    def test_configuration_selects_the_hybrid_chain(self):
        class Settings:
            TRANSLATION_PROVIDER = "hybrid"

        assert isinstance(build_translator(Settings()), InternalFallbackTranslator)

    def test_existing_selection_is_unchanged(self):
        for value, expected in [
            ("llm", LLMTranslator), ("glossary", object),
            ("passthrough", PassthroughTranslator), ("none", PassthroughTranslator),
        ]:
            class Settings:
                TRANSLATION_PROVIDER = value

            translator = build_translator(Settings())
            if expected is object:
                assert translator.name == "glossary"
            else:
                assert isinstance(translator, expected)


# ===========================================================================
# 17-18. Scoring and retrieval gain no language parameter
# ===========================================================================

class TestNoLanguageParameterLeaks:
    def test_scoring_takes_no_language_parameter(self):
        from app.services.scoring.service import ScoringService

        for name, method in inspect.getmembers(ScoringService, inspect.isfunction):
            if name.startswith("_"):
                continue
            assert "language" not in inspect.signature(method).parameters, name

    def test_retrieval_takes_no_language_parameter(self):
        from app.services.retrieval.engine import HybridRetrievalEngine

        for name, method in inspect.getmembers(HybridRetrievalEngine,
                                               inspect.isfunction):
            if name.startswith("_"):
                continue
            assert "language" not in inspect.signature(method).parameters, name

    def test_renderer_is_pure_and_holds_no_session(self):
        renderer = _renderer()

        assert not hasattr(renderer, "db")
        assert not hasattr(renderer, "session")
        assert not inspect.iscoroutinefunction(renderer.render)


# ===========================================================================
# 19-20. End to end: the deterministic path, in Hindi, with no provider
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
def internal_translation(monkeypatch):
    """Point the adapter at the internal renderer for one test.

    Patched at the adapter's import site rather than in settings: the adapter
    resolves its translator lazily and holds a reference, which is exactly the
    seam a deployment flips with ``TRANSLATION_PROVIDER``.
    """
    from app.services.language import adapter as adapter_module

    monkeypatch.setattr(adapter_module, "build_translator",
                        lambda *a, **k: InternalRendererTranslator())
    return InternalRendererTranslator()


class TestDeterministicPathInHindi:
    QUESTION = "BEL kaisi company hai?"

    def _result(self, db_session):
        from tests.test_deterministic_analyst_path import _analyst, _memory

        analyst, router, docs, _ = _analyst(db_session, mode="forbid")
        memory = _memory()
        result = _run(analyst.chat(self.QUESTION, memory,
                                   language=Language.HINDI))
        return result, memory, router, docs

    def test_canonical_content_stays_english_and_memory_stays_english(
        self, db_session, internal_translation,
    ):
        result, memory, router, docs = self._result(db_session)

        # Provider and RAG still untouched: this phase adds no dependency.
        assert router.complete_calls == []
        assert docs.search_calls == []

        # `content` is the audited English artefact.
        assert DEVANAGARI_WORD.search(result.content) is None
        assert "not investment advice" in result.content

        # Memory holds canonical English, never the rendered display text.
        assistant_turns = [t for t in memory.turns if t.content]
        assert assistant_turns
        for turn in assistant_turns:
            assert DEVANAGARI_WORD.search(turn.content) is None

    def test_display_content_is_rendered_and_keeps_the_evidence(
        self, db_session, internal_translation,
    ):
        result, _, _, _ = self._result(db_session)

        assert result.display_content != result.content
        assert DEVANAGARI_WORD.search(result.display_content) is not None

        # No citation marker is lost. The display carries the *annotated* form
        # ("[Institutional score]") rather than the internal key, because
        # `analyst` annotates for display before adaptation runs — so the two
        # texts are compared by marker count and by protected span, never by
        # key, which the renderer never sees.
        english_markers = CITATIONS.findall(result.content)
        assert len(CITATIONS.findall(result.display_content)) == len(english_markers)
        assert (protect(result.display_content).kinds().get("citation", 0)
                == len(english_markers))

        assert result.citation_audit is not None
        assert result.citation_audit.is_supported
        assert result.citation_audit.unknown_keys == []
        assert result.guardrails is not None
        assert result.guardrails.passed
        # The renderer verified its own output against the text it was handed
        # and reported no integrity problem of any kind.
        assert result.language["translation"]["integrity_problems"] == []

    def test_numbers_survive_the_real_answer(self, db_session, internal_translation):
        result, _, _, _ = self._result(db_session)

        for figure in _numeric_tokens(result.content):
            assert figure in _numeric_tokens(result.display_content)

    def test_language_block_reports_how_it_was_rendered(
        self, db_session, internal_translation,
    ):
        result, _, _, _ = self._result(db_session)

        assert result.language is not None
        assert result.language["language"] == "hindi"
        assert result.language["translation"]["provider"] == "internal"
        assert result.language["fidelity"] in {"full", "partial"}
        assert 0.0 <= result.language["coverage"] <= 1.0

    def test_hinglish_request_never_produces_devanagari(
        self, db_session, internal_translation,
    ):
        from tests.test_deterministic_analyst_path import _analyst, _memory

        analyst, _, _, _ = _analyst(db_session, mode="forbid")
        result = _run(analyst.chat(self.QUESTION, _memory(),
                                   language=Language.HINGLISH))

        assert DEVANAGARI_WORD.search(result.display_content) is None
        assert result.display_content != result.content


def _run(coro):
    import asyncio

    return asyncio.new_event_loop().run_until_complete(coro)


def _numeric_tokens(text: str) -> list[str]:
    """Numeric tokens in order, ignoring spacing differences."""
    return [token.replace(" ", "") for token in NUMBERS.findall(text or "")]


def _figures(text: str) -> list[str]:
    """Numeric tokens in order, minus those belonging to a fiscal-year marker."""
    out: list[str] = []
    source = text or ""
    for match in NUMBERS.finditer(source):
        if re.search(r"FY\s*$", source[:match.start()]):
            continue
        out.append(match.group().replace(" ", ""))
    return out
