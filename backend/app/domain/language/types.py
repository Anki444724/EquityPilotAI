"""Language vocabulary for the Multilingual AI Response Engine.

The engine's governing constraint, taken directly from the brief: **one
canonical knowledge base, stored in English, translated only at response
generation.** Nothing in this package creates a document, a chunk, an
embedding, a vault entry or a memory row. It describes languages and the rules
for rendering into them.

`Language` is deliberately open at the edges: adding Marathi means adding an
enum member and a `LanguageSpec`, with no change to retrieval, scoring, RAG or
the schema. The six future languages named in the brief are already declared
here as `planned` — declared but not enabled — so the architecture can be
inspected for readiness rather than taken on trust.

Two ideas are worth stating up front.

**Script and language are different questions.** Hindi and Hinglish are the
same language in two scripts, and the answer must come back in the script the
user typed. Treating "Hinglish" as a dialect of Hindi would send Devanagari to
someone who wrote in Roman letters, which is the single most likely way this
feature disappoints a user.

**A language the platform cannot serve must say so.** `LanguageSpec.status`
distinguishes a supported language from a planned one, and the adapter refuses
to silently fall back to English without recording that it did.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Script(StrEnum):
    """The writing system a response is rendered in.

    Kept separate from `Language` because Hindi and Hinglish share a language
    and differ only here, and because a future Marathi request will reuse
    DEVANAGARI without any new logic.
    """

    LATIN = "latin"
    DEVANAGARI = "devanagari"
    GUJARATI = "gujarati"
    TAMIL = "tamil"
    TELUGU = "telugu"
    KANNADA = "kannada"
    BENGALI = "bengali"


class Language(StrEnum):
    """Every language the architecture knows about, enabled or not."""

    # --- Phase 1: supported now ---
    ENGLISH = "english"
    HINDI = "hindi"
    HINGLISH = "hinglish"

    # --- Declared for the roadmap; adding a translation module enables them ---
    MARATHI = "marathi"
    GUJARATI = "gujarati"
    TAMIL = "tamil"
    TELUGU = "telugu"
    KANNADA = "kannada"
    BENGALI = "bengali"


class LanguageStatus(StrEnum):
    SUPPORTED = "supported"
    #: Declared in the enum and in the registry, but no translation module is
    #: installed. A request for one is answered in English with an explicit
    #: note, never silently.
    PLANNED = "planned"


#: The sentinel meaning "detect from the request".
AUTO = "auto"


@dataclass(frozen=True, slots=True)
class LanguageSpec:
    """Everything the adapter needs to render into one language."""

    language: Language
    label: str
    #: Endonym — what speakers call it. Shown in the UI selector.
    native_label: str
    script: Script
    status: LanguageStatus
    #: BCP-47 tag, for HTTP content negotiation and the `lang` attribute.
    bcp47: str
    #: Instruction handed to the writing model. The single most important
    #: field: it is what actually produces the register the brief asks for.
    style_instruction: str
    #: When true the response keeps English technical vocabulary inline rather
    #: than translating it — how educated Indian speech actually works, and
    #: what makes Hinglish read naturally instead of like a dictionary.
    keeps_english_terms: bool = False

    @property
    def is_supported(self) -> bool:
        return self.status is LanguageStatus.SUPPORTED

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.language.value,
            "label": self.label,
            "native_label": self.native_label,
            "script": self.script.value,
            "status": self.status.value,
            "bcp47": self.bcp47,
            "keeps_english_terms": self.keeps_english_terms,
        }


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------

_ENGLISH = LanguageSpec(
    language=Language.ENGLISH,
    label="English",
    native_label="English",
    script=Script.LATIN,
    status=LanguageStatus.SUPPORTED,
    bcp47="en-IN",
    style_instruction=(
        "Write in professional British-Indian institutional English, as an "
        "equity research analyst would."
    ),
)

_HINDI = LanguageSpec(
    language=Language.HINDI,
    label="Hindi",
    native_label="हिन्दी",
    script=Script.DEVANAGARI,
    status=LanguageStatus.SUPPORTED,
    bcp47="hi-IN",
    style_instruction=(
        "Write entirely in Hindi using Devanagari script. Use the financial "
        "glossary supplied for technical terms. Keep company names, tickers, "
        "all numbers and all citation markers exactly as given in Latin "
        "script — never transliterate them. Write naturally, as a Hindi "
        "financial journal would, not as a literal translation of English "
        "sentence structure."
    ),
)

_HINGLISH = LanguageSpec(
    language=Language.HINGLISH,
    label="Hinglish",
    native_label="Hinglish",
    script=Script.LATIN,
    status=LanguageStatus.SUPPORTED,
    bcp47="hi-Latn-IN",
    style_instruction=(
        "Write in Hinglish: Hindi grammar and sentence structure written in "
        "Roman script, with English financial vocabulary kept in English. "
        "This is how Indian investors actually speak — 'Revenue growth "
        "stable hai', not 'Raajasva vriddhi sthir hai'. Never use Devanagari. "
        "Keep technical terms (revenue, margin, valuation, ROE) in English. "
        "Keep company names, tickers, numbers and citation markers exactly "
        "as given."
    ),
    keeps_english_terms=True,
)


def _planned(language: Language, label: str, native: str, script: Script,
             bcp47: str) -> LanguageSpec:
    """A roadmap language: declared, inspectable, not yet enabled.

    Declaring these now is the architectural claim the brief asks for — that
    adding a language is a translation module and nothing else. They appear in
    `GET /ai/languages` with `status: planned`, so the readiness of the design
    can be checked rather than believed.
    """
    return LanguageSpec(
        language=language, label=label, native_label=native, script=script,
        status=LanguageStatus.PLANNED, bcp47=bcp47,
        style_instruction=(
            f"Write entirely in {label}. Use the financial glossary for "
            "technical terms. Keep company names, tickers, numbers and "
            "citation markers exactly as given."
        ),
    )


LANGUAGES: dict[Language, LanguageSpec] = {
    Language.ENGLISH: _ENGLISH,
    Language.HINDI: _HINDI,
    Language.HINGLISH: _HINGLISH,
    Language.MARATHI: _planned(Language.MARATHI, "Marathi", "मराठी",
                               Script.DEVANAGARI, "mr-IN"),
    Language.GUJARATI: _planned(Language.GUJARATI, "Gujarati", "ગુજરાતી",
                                Script.GUJARATI, "gu-IN"),
    Language.TAMIL: _planned(Language.TAMIL, "Tamil", "தமிழ்",
                             Script.TAMIL, "ta-IN"),
    Language.TELUGU: _planned(Language.TELUGU, "Telugu", "తెలుగు",
                              Script.TELUGU, "te-IN"),
    Language.KANNADA: _planned(Language.KANNADA, "Kannada", "ಕನ್ನಡ",
                               Script.KANNADA, "kn-IN"),
    Language.BENGALI: _planned(Language.BENGALI, "Bengali", "বাংলা",
                               Script.BENGALI, "bn-IN"),
}

#: The canonical storage language. Everything the platform persists — chunks,
#: embeddings, vault entries, summaries, observations, scores — is in this
#: language and only this language. Named as a constant so the invariant is
#: greppable rather than implicit.
CANONICAL_LANGUAGE = Language.ENGLISH

SUPPORTED_LANGUAGES: tuple[Language, ...] = tuple(
    lang for lang, spec in LANGUAGES.items() if spec.is_supported
)

PLANNED_LANGUAGES: tuple[Language, ...] = tuple(
    lang for lang, spec in LANGUAGES.items() if not spec.is_supported
)

# Every enum member must be registered, or a request for it raises KeyError
# deep inside the adapter rather than being rejected at the edge.
assert set(LANGUAGES) == set(Language), "a Language has no LanguageSpec"
assert CANONICAL_LANGUAGE in SUPPORTED_LANGUAGES


def spec_for(language: Language | str) -> LanguageSpec:
    """Look up a spec, accepting an enum or its string code."""
    if isinstance(language, str):
        try:
            language = Language(language.strip().lower())
        except ValueError as exc:
            raise KeyError(f"unknown language '{language}'") from exc
    return LANGUAGES[language]


def resolve(requested: str | None) -> Language | None:
    """Map an API `language` parameter to a Language, or None for auto.

    Returns ``None`` for ``auto``, an empty value, or anything unrecognised.
    Unrecognised input falls through to detection rather than raising: a
    client sending `language=en-GB` should get a sensible answer, not a 422.
    """
    if not requested:
        return None
    value = requested.strip().lower()
    if value in {AUTO, "", "detect"}:
        return None

    try:
        return Language(value)
    except ValueError:
        pass

    # Common aliases and BCP-47 tags, so a browser's Accept-Language header
    # and a hand-written client both work.
    aliases = {
        "en": Language.ENGLISH, "en-in": Language.ENGLISH,
        "en-gb": Language.ENGLISH, "en-us": Language.ENGLISH,
        "hi": Language.HINDI, "hi-in": Language.HINDI,
        "devanagari": Language.HINDI,
        "hi-latn": Language.HINGLISH, "hi-latn-in": Language.HINGLISH,
        "hinglish": Language.HINGLISH, "roman-hindi": Language.HINGLISH,
        "mr": Language.MARATHI, "gu": Language.GUJARATI,
        "ta": Language.TAMIL, "te": Language.TELUGU,
        "kn": Language.KANNADA, "bn": Language.BENGALI,
    }
    return aliases.get(value)
