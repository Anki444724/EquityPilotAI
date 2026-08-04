"""The Language Adapter.

    User
      ↓
    Retriever → RAG → Knowledge Graph → Scoring → Reasoning
      ↓
    Language Adapter          ← this module
      ↓
    English · Hindi · Hinglish · future languages

The adapter sits at both ends of the pipeline and is the only component that
knows a language other than English exists:

**Inbound** — :meth:`LanguageAdapter.normalise_query` converts a question in any
language into an English retrieval query. This is what makes "राजस्व", "kamai"
and "revenue" return the same documents from the same index. It is a query
rewrite, not a corpus change: no Hindi vector, no Hindi chunk, no second
knowledge base.

**Outbound** — :meth:`LanguageAdapter.adapt` renders a finished English answer
into the user's language, with every citation, figure and identifier masked
during the process and verified afterwards.

Everything between the two is untouched. The reasoning engine receives an
English query and emits English prose, exactly as it did before this package
existed, which is the property the brief asks for and the reason this feature
cannot regress Retrieval 2.1 or Scoring 3.0.

**The adapter is stateless.** It reads no table and writes none. A translation
exists for the duration of one response and is then discarded.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

import structlog

from app.domain.language.detect import Detection, choose_language, detect, detect_mixed
from app.domain.language.glossary import BY_ENGLISH, TERMS
from app.domain.language.translation_memory import get_translation_memory, apply_translation_memory
from app.domain.language.types import (
    CANONICAL_LANGUAGE, Language, LanguageSpec, spec_for,
)
from app.services.language.prompt_templates import get_multilingual_prompt
from app.services.language.translators import (
    PassthroughTranslator, TranslationResult, Translator, build_translator,
)

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Inbound: query normalisation
# ---------------------------------------------------------------------------

#: Devanagari and romanised Hindi financial vocabulary mapped to the English
#: term the corpus actually contains.
#:
#: This is the cross-language search mechanism, and it is deliberately a
#: *query-side* dictionary rather than a translated index. Translating 11,485
#: chunks into Hindi would create the second knowledge base the brief
#: forbids, cost a full re-embed, and double every subsequent ingestion. A
#: query dictionary is ~200 entries, changes nothing downstream, and is
#: exactly reversible.
QUERY_TERMS: dict[str, str] = {
    # --- Devanagari ---
    "राजस्व": "revenue", "आय": "income earnings", "कमाई": "earnings",
    "बिक्री": "sales revenue", "मुनाफा": "profit", "लाभ": "profit",
    "शुद्ध लाभ": "net profit", "घाटा": "loss", "नुकसान": "loss",
    "ऋण": "debt", "कर्ज": "debt", "कर्ज़": "debt", "उधार": "borrowings",
    "नकद": "cash", "नकदी": "cash", "पूंजी": "capital", "पूँजी": "capital",
    "मार्जिन": "margin", "अनुपात": "ratio", "वृद्धि": "growth",
    "विकास": "growth", "जोखिम": "risk", "मूल्यांकन": "valuation",
    "मूल्य": "price value", "कीमत": "price", "शेयर": "share",
    "शेयरधारक": "shareholder", "निवेश": "investment", "निवेशक": "investor",
    "लाभांश": "dividend", "प्रबंधन": "management", "कंपनी": "company",
    "व्यवसाय": "business", "व्यापार": "business", "उद्योग": "industry",
    "क्षेत्र": "sector", "बाजार": "market", "बाज़ार": "market",
    "प्रतिस्पर्धा": "competition", "ग्राहक": "customer",
    "कर": "tax", "ब्याज": "interest", "संपत्ति": "assets",
    "देनदारी": "liabilities", "परिसंपत्ति": "assets",
    "तिमाही": "quarterly", "वार्षिक": "annual", "रिपोर्ट": "report",
    "भविष्य": "future outlook", "प्रदर्शन": "performance",
    "गुणवत्ता": "quality", "रणनीति": "strategy", "योजना": "plan",
    "उत्पाद": "product", "सेवा": "service", "कर्मचारी": "employees",
    "अधिग्रहण": "acquisition", "विलय": "merger", "विस्तार": "expansion",
    "क्षमता": "capacity", "आदेश": "order", "ऑर्डर": "order",
    "नियामक": "regulatory", "अभिशासन": "governance", "प्रवर्तक": "promoter",
    "गिरवी": "pledge", "मज़बूत": "strong", "मजबूत": "strong",
    "कमजोर": "weak", "कमज़ोर": "weak", "अच्छा": "good", "बुरा": "bad",
    "कितना": "how much", "कैसा": "how", "कैसी": "how", "क्या": "what",
    "कब": "when", "कहाँ": "where", "कौन": "who", "क्यों": "why",

    # --- romanised Hindi ---
    "kamai": "earnings", "kamayi": "earnings", "munafa": "profit",
    "munaafa": "profit", "nuksan": "loss", "ghata": "loss",
    "karz": "debt", "karja": "debt", "karj": "debt", "udhar": "borrowings",
    "paisa": "cash money", "paise": "cash money", "nakad": "cash",
    "punji": "capital", "poonji": "capital",
    "vriddhi": "growth", "badhotri": "growth", "jokhim": "risk",
    "keemat": "price", "kimat": "price", "mulya": "value",
    "sheyar": "share", "nivesh": "investment", "niveshak": "investor",
    "prabandhan": "management", "kampani": "company", "vyavsay": "business",
    "vyapar": "business", "udyog": "industry", "bazaar": "market",
    "bazar": "market", "grahak": "customer", "pratispardha": "competition",
    "sampatti": "assets", "bhavishya": "future outlook",
    "pradarshan": "performance", "gunvatta": "quality",
    "vistar": "expansion", "kshamta": "capacity",
    "mazboot": "strong", "majboot": "strong", "kamzor": "weak",
    "acha": "good", "accha": "good", "achha": "good", "achhi": "good",
    "bura": "bad", "kharab": "bad",
    "kitna": "how much", "kitni": "how much", "kaisa": "how", "kaisi": "how",
    "kya": "what", "kab": "when", "kahan": "where", "kaun": "who",
    "kyun": "why", "kyon": "why", "konsa": "which", "kaunsa": "which",
}

#: Romanised Hindi grammar words that carry no retrieval signal. Removing them
#: stops "hai" and "ka" from diluting the lexical query — the corpus contains
#: neither, so every one is a term that can only fail to match.
_QUERY_NOISE: frozenset[str] = frozenset("""
hai hain hota hoti hote tha thi the ka ki ke ko se me mein par
aur ya lekin magar toh phir bhi hi na nahi nahin
mera meri tera teri uska uski iska iski unka inka apna apni
batao bataye bataiye samjhao dekho please
है हैं का की के को से में पर और या लेकिन तो भी ही नहीं
""".split())

_WORD_RE = re.compile(r"[\w\u0900-\u097F]+", re.UNICODE)


@dataclass(slots=True)
class NormalisedQuery:
    """A user question rewritten for the English retrieval index."""

    original: str
    #: What is actually handed to the retriever.
    english: str
    detection: Detection
    #: Terms that were mapped, for the validation report and for debugging a
    #: query that returned nothing.
    mapped: list[tuple[str, str]] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)

    @property
    def was_rewritten(self) -> bool:
        return self.english.strip().lower() != self.original.strip().lower()

    def as_dict(self) -> dict[str, Any]:
        return {
            "original": self.original,
            "english": self.english,
            "rewritten": self.was_rewritten,
            "mapped_terms": [{"from": a, "to": b} for a, b in self.mapped],
            "removed_terms": self.removed,
        }


# ---------------------------------------------------------------------------
# Outbound: the adapted response
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class AdaptedResponse:
    """An answer rendered into the user's language, with provenance."""

    text: str
    language: Language
    spec: LanguageSpec
    detection: Detection
    translation: TranslationResult
    #: How the language was decided: "requested", "detected" or "preference".
    source: str = "detected"
    latency_ms: float = 0.0

    @property
    def is_translated(self) -> bool:
        return self.translation.translated and self.language is not CANONICAL_LANGUAGE

    def as_dict(self) -> dict[str, Any]:
        return {
            "language": self.language.value,
            "label": self.spec.label,
            "native_label": self.spec.native_label,
            "script": self.spec.script.value,
            "bcp47": self.spec.bcp47,
            "resolved_from": self.source,
            "detected": self.detection.as_dict(),
            "translation": self.translation.as_dict(),
            "latency_ms": round(self.latency_ms, 1),
        }


class LanguageAdapter:
    """Inbound query normalisation and outbound response rendering."""

    def __init__(self, translator: Translator | None = None) -> None:
        self._translator = translator

    @property
    def translator(self) -> Translator:
        # Built lazily so constructing an adapter costs nothing — it is
        # instantiated per request, including on paths that never translate.
        if self._translator is None:
            self._translator = build_translator()
        return self._translator

    # ------------------------------------------------------------- inbound
    def normalise_query(self, question: str) -> NormalisedQuery:
        """Rewrite a question in any language into English retrieval terms.

        Phase 2 improvements:
        - Mixed language detection (Hindi + English + Hinglish) now influences
          downstream language choice and prompt selection.
        - All other behaviour identical to Phase 1 (byte-identical for English).

        Two properties matter.

        **English questions pass through untouched.** The mapping only fires
        on tokens that are actually Hindi, so an English query reaches the
        retriever byte-identical to how it arrived. That is what guarantees
        Retrieval 2.1's measured known-item accuracy cannot regress: the input
        is the same string it was before this layer existed.

        **The original terms are kept alongside the mapped ones.** A Hinglish
        question often mixes both — "Revenue growth kaisi hai" — and dropping
        the English words would discard the strongest signal in the query.
        """
        detection = detect(question)
        mixed = detect_mixed(question)
        raw = question or ""

        # Phase 2: enrich detection with mixed signal
        if mixed.get("is_mixed"):
            detection.is_mixed = True
            if detection.confidence < 0.82:
                detection.confidence = min(0.92, detection.confidence + mixed.get("confidence_adjustment", 0.0))

        # NORM-001 ... (original logic preserved exactly)
        has_mappable_term = any(
            token.lower() in QUERY_TERMS or token in QUERY_TERMS
            for token in _WORD_RE.findall(raw)
        ) or any(phrase in raw for phrase in QUERY_TERMS if " " in phrase)

        if detection.language is Language.ENGLISH and not has_mappable_term:
            return NormalisedQuery(original=raw, english=raw,
                                   detection=detection)

        mapped: list[tuple[str, str]] = []
        removed: list[str] = []
        pieces: list[str] = []

        # Multi-word Devanagari phrases first ("शुद्ध लाभ" before "लाभ").
        working = raw
        for phrase in sorted(
            (t for t in QUERY_TERMS if " " in t), key=len, reverse=True,
        ):
            if phrase in working:
                working = working.replace(phrase, f" {QUERY_TERMS[phrase]} ")
                mapped.append((phrase, QUERY_TERMS[phrase]))

        for token in _WORD_RE.findall(working):
            lowered = token.lower()
            replacement = QUERY_TERMS.get(lowered) or QUERY_TERMS.get(token)
            if replacement:
                mapped.append((token, replacement))
                pieces.append(replacement)
                continue
            if lowered in _QUERY_NOISE:
                removed.append(token)
                continue
            # Unrecognised tokens are kept. In a Hinglish query these are
            # overwhelmingly the English content words — "revenue", "TCS",
            # "margin" — and they carry most of the retrieval signal.
            pieces.append(token)

        english = " ".join(dict.fromkeys(pieces)).strip() or raw

        log.debug("query normalised", original=raw[:120], english=english[:120],
                  language=detection.language.value, mapped=len(mapped), mixed=mixed.get("is_mixed"))

        return NormalisedQuery(original=raw, english=english,
                               detection=detection, mapped=mapped,
                               removed=removed)

    # ------------------------------------------------------------ outbound
    async def adapt(
        self,
        text: str,
        *,
        question: str = "",
        requested: Language | None = None,
        preference: Language | None = None,
        entities: list[str] | None = None,
    ) -> AdaptedResponse:
        """Render an English answer into the user's language."""
        started = time.perf_counter()

        language, detection = choose_language(
            question, requested=requested, preference=preference,
        )
        source = (
            "requested" if requested is not None
            else "detected" if detection.confidence >= 0.50
            else "preference" if preference is not None
            else "detected"
        )
        spec = spec_for(language)

        # English needs no work at all — the common path stays free.
        if language is CANONICAL_LANGUAGE:
            return AdaptedResponse(
                text=text, language=language, spec=spec, detection=detection,
                translation=TranslationResult(
                    text=text, language=language, translated=True,
                    provider="none",
                ),
                source=source,
                latency_ms=(time.perf_counter() - started) * 1000,
            )

        if not spec.is_supported:
            # A planned language is answered in English and labelled. Silently
            # substituting English would make the roadmap look like a bug.
            return AdaptedResponse(
                text=text, language=CANONICAL_LANGUAGE,
                spec=spec_for(CANONICAL_LANGUAGE), detection=detection,
                translation=TranslationResult(
                    text=text, language=CANONICAL_LANGUAGE, translated=False,
                    provider="none",
                    detail=(
                        f"{spec.label} is declared in the architecture but no "
                        "translation module is installed yet; the response is "
                        "in English."
                    ),
                ),
                source=source,
                latency_ms=(time.perf_counter() - started) * 1000,
            )

        result = await self.translator.translate(
            text, language, entities=entities or [],
        )

        # Phase 2: apply translation memory for repeated phrases (improves consistency)
        final_text = result.text
        if result.translated:
            final_text = apply_translation_memory(result.text, language)

        return AdaptedResponse(
            text=final_text,
            language=result.language if result.translated else CANONICAL_LANGUAGE,
            spec=spec_for(result.language if result.translated
                          else CANONICAL_LANGUAGE),
            detection=detection, translation=result, source=source,
            latency_ms=(time.perf_counter() - started) * 1000,
        )

    # ------------------------------------------------------------- prompts
    @staticmethod
    def response_instruction(language: Language) -> str:
        """Instruction appended to the writing prompt.

        Asking the model to write directly in the target language, where it
        can, produces markedly better prose than translating English output —
        the sentence structure is native rather than calqued. Translation
        remains as the fallback and as the guarantee, since a model told to
        write Hindi will sometimes write English anyway.
        """
        if language is CANONICAL_LANGUAGE:
            return ""
        spec = spec_for(language)
        if not spec.is_supported:
            return ""
        return (
            f"\n\nRESPONSE LANGUAGE: {spec.label} ({spec.native_label}). "
            f"{spec.style_instruction} Keep every citation marker in square "
            "brackets, every number, every ticker and every company name "
            "exactly as it appears in the evidence — those are identifiers, "
            "not words to translate."
        )
