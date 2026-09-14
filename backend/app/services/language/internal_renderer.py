"""The internal language renderer.

Phase 2A. This module is the platform's own outbound language layer: it renders
a finished, audited, citation-backed English answer into Hindi or Hinglish
without calling any external provider.

**Why it exists.** The outbound pipeline used to end at
``LanguageAdapter → external translation provider → display``. When that
provider returned HTTP 402 the request fell back to English, which meant a user
who asked in Hindi got an English answer — a dependency the platform does not
want and, per the Phase 2A brief, is now building its way out of. This renderer
is the internal capability that replaces it for the deterministic answer
engines, whose output is structured and therefore renderable without a model.

**Where it sits.** Immediately where the translator used to sit: after citation
audit, guardrails, enforcement and the memory write, and only on the display
copy. ``content`` stays the audited canonical English artefact, memory keeps
English, and nothing about scoring, retrieval, evidence or the audit changes. A
rendering failure can therefore never corrupt anything the platform relies on.

**How it works — mask first, then frame.**

1. ``protect()`` masks every untranslatable span (citations, numbers, ISINs,
   fiscal years, tickers and caller-supplied company names) into ``§n§``
   sentinels. This reuses the existing protection layer rather than inventing a
   second one, and it makes the text *safe to segment*: sentinels contain no
   periods, so sentence splitting can no longer cut a figure in half.
2. The masked text is split into structural segments (headings, separators,
   disclosure blocks) and sentences.
3. Each sentence is matched against a closed set of **frames** — the sentence
   shapes the deterministic engines actually emit, each with a Hindi and a
   Hinglish template that places the captured material in target-language word
   order.
4. ``restore()`` puts every protected span back, and ``verify_preserved()``
   re-checks the result independently. Anything that fails verification is
   rejected and the English original is returned.

**The two contracts that keep this honest.**

*The grammar contract.* Hindi inflects: ``राजस्व`` and ``राजस्व के`` differ, and
a blind find-and-replace produces ungrammatical prose every time. Templates here
therefore contain only **fixed morpheme sequences** — ``है``, ``के लिए``,
``का स्कोर``, ``की वृद्धि हुई``. A variable slot may appear only in a nominative,
locative or otherwise invariant position, so no agreement has to be computed from
arbitrary words and none can be broken. Where a possessive is needed it is the
*template* that owns the agreement (``का स्कोर`` — स्कोर is masculine, always),
never a guess about the subject's gender.

*The vocabulary contract.* The renderer may produce only (a) protected spans,
(b) glossary renderings, and (c) the closed set of function and platform words in
this module's frame table. It may **not coin financial vocabulary**: a term the
glossary does not know stays in English inline, exactly as Hinglish keeps
English terms. :meth:`InternalLanguageRenderer.vocabulary` publishes the whole
allowed Devanagari set, and the test suite asserts that everything the renderer
emits is inside it — so invented terminology fails the build rather than
reaching a reader.

**What it refuses to do.** Arbitrary prose — a scoring engine's free-text note,
a research sentence the provider wrote — is *not* transformed. It is preserved
in canonical English, in place, and counted. The result reports its own
fidelity: ``full`` (everything rendered), ``partial`` (how much was rendered,
and how much was not) or ``none`` (nothing could be rendered safely). This
module never presents a partial rendering as a complete one, and it is not a
language model; it is a structured renderer that knows its own limits.

The external providers are deliberately untouched by this phase. ``LLMTranslator``
and the whole provider stack remain in place and remain the default; this
renderer is selected with ``TRANSLATION_PROVIDER=internal`` (renderer only) or
``=hybrid`` (renderer when it can complete an answer, provider otherwise, and
the renderer again if the provider fails). Removing the external dependency
entirely is the later Phase 2E step.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Callable, Iterable

import structlog

from app.domain.language.glossary import BY_ENGLISH, TERMS
from app.domain.language.protect import (
    Protection, protect, restore, verify_preserved,
)
from app.domain.language.types import CANONICAL_LANGUAGE, Language, spec_for
from app.services.language.translators import TranslationResult

log = structlog.get_logger(__name__)

#: Languages this renderer produces itself.
INTERNAL_LANGUAGES: frozenset[Language] = frozenset({
    Language.ENGLISH, Language.HINDI, Language.HINGLISH,
})

#: A masked span, e.g. ``§0§``.
_TICK = r"§\d+§"

#: Any run of text, optionally interleaved with masked spans. Used for slots
#: whose content may or may not contain protected material (a subject that is a
#: company name, a subject that carries an acronym).
_SLOT = rf"(?:{_TICK}|[^§])"

#: What a *value* may consist of. `protect()` masks the figure itself, so what
#: remains literally in the string is punctuation, currency marks and unit
#: words. Letters are admitted only as whole unit words, and that restriction is
#: load-bearing: without it a frame's value slot swallows the prose following a
#: figure and emits a sentence stitched from unrelated fragments.
_VALUE_UNIT = r"(?:cr|bn|mn|bps|times|per|share|crore|lakh|x)"
_VALUE_RUN = rf"(?:{_TICK}|[\s\d.,/%₹$€£+\-()]|\b{_VALUE_UNIT}\b)"

#: Devanagari word characters, danda excluded so a sentence-final ``।`` does not
#: end up glued to its last word when the vocabulary is checked.
_DEVANAGARI_WORD = re.compile(r"[\u0900-\u0963\u0966-\u097F]+")

#: Any Devanagari, danda included. Hinglish must contain none of it.
_DEVANAGARI = re.compile(r"[\u0900-\u097F]")

#: Sentence boundary in *masked* text. Safe because figures, fiscal years and
#: company names are sentinels by this point and contain no sentence-ending
#: punctuation, so a boundary found here is a real one.
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+(?=[A-Z§\"'(\[]|\d)")

#: Markdown horizontal rule — structural, never rendered.
_RULE = re.compile(r"^(?:[-=_*]\s*){3,}$")

#: An italic disclosure block, the guardrail text. Rendered verbatim: it is a
#: legal statement and paraphrasing it in another language is not this
#: module's call to make.
_ITALIC_BLOCK = re.compile(r"^_[^_].*_$", re.DOTALL)

#: ``- item`` / ``1. item`` / ``* item``.
_BULLET = re.compile(r"^(?P<marker>[-*•]|\d+[.)])\s+(?P<rest>.*)$")

#: ``Label: rest`` where ``Label`` is a short capitalised phrase. Only labels in
#: :data:`_LABELS` (or in the glossary) are touched; everything else is prose.
_LABELLED = re.compile(r"^(?P<label>[A-Z][A-Za-z'’/ ]{1,40}?):\s*(?P<rest>.*)$")

#: The same shape, but for a label that opens a *sentence* rather than a line —
#: "Strongest: Net cash at 0.58x EBITDA …" appears mid-paragraph.
_SENTENCE_LABEL = re.compile(r"^(?P<label>[A-Z][A-Za-z'’/ ]{1,30}?):\s*(?P<rest>.+)$")


class Fidelity(StrEnum):
    """How much of an answer the renderer actually produced.

    ``translated`` is a boolean, and a boolean cannot express "half of this is
    Hindi and the rest is English because a deterministic renderer could not
    safely do better". This can, and it is reported to the caller.
    """

    FULL = "full"
    PARTIAL = "partial"
    NONE = "none"


@dataclass(slots=True)
class RenderResult:
    """A rendered answer, with an honest account of how it was produced."""

    text: str
    language: Language
    fidelity: Fidelity
    #: Rendered sentences / prose sentences. 1.0 when there was no prose.
    coverage: float = 1.0
    prose_sentences_rendered: int = 0
    prose_sentences_preserved: int = 0
    #: Structural lines rendered: headings, bullet markers, labels.
    labels_rendered: int = 0
    #: Sentences whose label was rendered while a quoted payload — an engine's
    #: own note — was kept verbatim in English.
    quoted_payloads: int = 0
    #: Glossary terms substituted, and terms kept in English because the
    #: glossary has no rendering for them.
    terms_rendered: int = 0
    terms_kept_english: int = 0
    #: Why sentences were left in canonical English, by reason.
    preserved_reasons: dict[str, int] = field(default_factory=dict)
    #: What was protected, by kind — the same view `protect()` reports.
    protected_spans: dict[str, int] = field(default_factory=dict)
    #: Non-empty means the rendering was rejected. Any entry is a broken
    #: guarantee, not a warning: the caller gets the English original.
    problems: list[str] = field(default_factory=list)
    detail: str = ""
    latency_ms: float = 0.0

    @property
    def translated(self) -> bool:
        """True only when the whole answer was rendered internally."""
        return self.fidelity is Fidelity.FULL

    @property
    def is_usable(self) -> bool:
        """True when something was rendered and nothing was corrupted."""
        return not self.problems and self.fidelity in {Fidelity.FULL, Fidelity.PARTIAL}

    def as_dict(self) -> dict[str, Any]:
        return {
            "language": self.language.value,
            "fidelity": self.fidelity.value,
            "coverage": round(self.coverage, 3),
            "sentences_rendered": self.prose_sentences_rendered,
            "sentences_preserved": self.prose_sentences_preserved,
            "labels_rendered": self.labels_rendered,
            "quoted_payloads": self.quoted_payloads,
            "terms_rendered": self.terms_rendered,
            "terms_kept_english": self.terms_kept_english,
            "preserved_reasons": dict(self.preserved_reasons),
            "protected": dict(self.protected_spans),
            "problems": list(self.problems),
        }


# ---------------------------------------------------------------------------
# Frame grammar: the closed set of function and platform phrases
# ---------------------------------------------------------------------------
#
# Everything below is *grammar*, not terminology. The financial vocabulary —
# राजस्व, शुद्ध लाभ, ऋण, मूल्यांकन — lives in the glossary and is read from
# there at render time, so the two can never drift apart. The only words that
# live here are connectives, copulas, prepositions and the names of platform
# components ("scoring engine", "platform"), none of which can mislead a reader
# about a financial concept.
#
# A phrase the glossary does not know and this table does not contain is kept in
# English inline. That is deliberate: coining Hindi terminology silently is a
# worse failure than a sentence that carries an English technical term, which is
# how Indian financial prose in both Hindi and Hinglish already works.

#: Labels the answers of the deterministic engines put before a colon.
_LABELS: dict[str, dict[Language, str]] = {
    "composite": {Language.HINDI: "समग्र", Language.HINGLISH: "Composite"},
    "why consider": {Language.HINDI: "क्यों विचार करें",
                     Language.HINGLISH: "Why consider"},
    "risks / what can go wrong": {
        Language.HINDI: "जोखिम / क्या गलत हो सकता है",
        Language.HINGLISH: "Risks / what can go wrong",
    },
    "bottom line": {Language.HINDI: "अंतिम निष्कर्ष",
                    Language.HINGLISH: "Bottom line"},
    "strongest": {Language.HINDI: "सबसे मज़बूत", Language.HINGLISH: "Strongest"},
    "weakest": {Language.HINDI: "सबसे कमज़ोर", Language.HINGLISH: "Weakest"},
    "not assessed": {Language.HINDI: "मूल्यांकन नहीं किया गया",
                     Language.HINGLISH: "Not assessed"},
    "key points": {Language.HINDI: "मुख्य बिंदु", Language.HINGLISH: "Key points"},
    "summary": {Language.HINDI: "सारांश", Language.HINGLISH: "Summary"},
    "conclusion": {Language.HINDI: "निष्कर्ष", Language.HINGLISH: "Conclusion"},
    "strengths": {Language.HINDI: "मज़बूतियाँ", Language.HINGLISH: "Strengths"},
    "weaknesses": {Language.HINDI: "कमज़ोरियाँ", Language.HINGLISH: "Weaknesses"},
    "risks": {Language.HINDI: "जोखिम", Language.HINGLISH: "Risks"},
}

#: Platform-component phrasing used inside frames.
_PLATFORM = {
    "scoring_engine": {
        Language.HINDI: "स्कोरिंग इंजन",
        Language.HINGLISH: "scoring engine",
    },
    "platform": {Language.HINDI: "प्लेटफ़ॉर्म", Language.HINGLISH: "platform"},
    "institutional_scoring": {
        Language.HINDI: "संस्थागत स्कोरिंग",
        Language.HINGLISH: "institutional scoring",
    },
    "score": {Language.HINDI: "स्कोर", Language.HINGLISH: "score"},
    "grade": {Language.HINDI: "ग्रेड", Language.HINGLISH: "grade"},
    "valuation_engine": {
        Language.HINDI: "मूल्यांकन इंजन", Language.HINGLISH: "valuation engine",
    },
}

#: Connectives and copulas, per language.
_WORDS = {
    "is": {Language.HINDI: "है", Language.HINGLISH: "hai"},
    "is_plural": {Language.HINDI: "हैं", Language.HINGLISH: "hain"},
    "and": {Language.HINDI: "और", Language.HINGLISH: "aur"},
    "for": {Language.HINDI: "के लिए", Language.HINGLISH: "ke liye"},
    "of": {Language.HINDI: "का", Language.HINGLISH: "ka"},
    "in": {Language.HINDI: "में", Language.HINGLISH: "me"},
    "at": {Language.HINDI: "पर", Language.HINGLISH: "par"},
    "compared_with": {Language.HINDI: "की तुलना में", Language.HINGLISH: "ke muqable"},
    "this_is": {Language.HINDI: "यह", Language.HINGLISH: "yeh"},
    "from": {Language.HINDI: "से", Language.HINGLISH: "se"},
    "derived": {Language.HINDI: "निकाला गया", Language.HINGLISH: "nikala gaya"},
    "own_view": {Language.HINDI: "का अपना दृष्टिकोण", Language.HINGLISH: "ka apna view"},
    "it_has": {Language.HINDI: "ने", Language.HINGLISH: "ne"},
    "scored": {Language.HINDI: "स्कोर किया", Language.HINGLISH: "score kiya"},
    "among_strongest": {
        Language.HINDI: "सबसे मज़बूत क्षेत्रों में शामिल है",
        Language.HINGLISH: "sabse mazboot areas me se ek hai",
    },
    "among_weakest": {
        Language.HINDI: "सबसे कमज़ोर क्षेत्रों में शामिल है",
        Language.HINGLISH: "sabse kamzor areas me se ek hai",
    },
    "its_score": {Language.HINDI: "इसका स्कोर", Language.HINGLISH: "iska score"},
    "strong": {Language.HINDI: "मज़बूत", Language.HINGLISH: "mazboot"},
    "weak": {Language.HINDI: "कमज़ोर", Language.HINGLISH: "kamzor"},
    "growth": {Language.HINDI: "वृद्धि", Language.HINGLISH: "growth"},
    "decline": {Language.HINDI: "गिरावट", Language.HINGLISH: "girawat"},
    "happened": {Language.HINDI: "हुई", Language.HINGLISH: "hui"},
    # --- words for the platform's own fixed sentences and category sentences --
    "category": {Language.HINDI: "श्रेणी", Language.HINGLISH: "category"},
    "possessive": {Language.HINDI: "के", Language.HINGLISH: "ke"},
    "to": {Language.HINDI: "को", Language.HINGLISH: "ko"},
    "rates": {Language.HINDI: "आंकता है", Language.HINGLISH: "aankta hai"},
    "inputs": {Language.HINDI: "इनपुट", Language.HINGLISH: "inputs"},
    "confidence": {Language.HINDI: "भरोसा", Language.HINGLISH: "bharosa"},
    "assessment": {Language.HINDI: "मूल्यांकन", Language.HINGLISH: "assessment"},
    "not": {Language.HINDI: "नहीं", Language.HINGLISH: "nahi"},
    "could": {Language.HINDI: "हो सका", Language.HINGLISH: "ho saka"},
    "no": {Language.HINDI: "कोई", Language.HINGLISH: "koi"},
    "supporting": {Language.HINDI: "समर्थक", Language.HINGLISH: "supporting"},
    "data": {Language.HINDI: "डेटा", Language.HINGLISH: "data"},
    "available": {Language.HINDI: "उपलब्ध", Language.HINGLISH: "available"},
    "expected": {Language.HINDI: "रहने का अनुमान है", Language.HINGLISH: "rehne ka anuman hai"},
    "these_areas": {Language.HINDI: "इन क्षेत्रों", Language.HINGLISH: "in areas"},
    "highest": {Language.HINDI: "सबसे ऊपर", Language.HINGLISH: "sabse upar"},
    "lowest": {Language.HINDI: "सबसे नीचे", Language.HINGLISH: "sabse neeche"},
    "ranks": {Language.HINDI: "रखता है", Language.HINGLISH: "rakhta hai"},
}

#: Possessives the *template* owns, so their agreement can never be wrong —
#: each is fixed by the noun the template itself supplies. `स्कोर` is
#: masculine; `अनुशंसा` is feminine. Nothing here is derived from the subject.
_SCORE_OF = {Language.HINDI: "का स्कोर", Language.HINGLISH: "ka score"}
#: Sentences this platform composes *verbatim*, mapped to their renderings.
#: Matched exactly, which is what makes them safe: they are our own fixed
#: strings, not prose a model wrote, so rendering them invents nothing — and a
#: change to any of them simply stops matching and falls back to English rather
#: than half-rendering a claim about the platform.
_FIXED: dict[str, dict[Language, str]] = {
    "These are the platform's computed category results; no qualitative judgement is added.":
        {Language.HINDI: 'ये प्लेटफ़ॉर्म के परिकलित श्रेणी परिणाम हैं; कोई गुणात्मक निर्णय नहीं जोड़ा गया है।', Language.HINGLISH: 'yeh platform ke computed category results hain; koi qualitative judgement add nahi kiya gaya hai.'},
    "These are the platform's computed category results and the scoring engine's own warnings; no weakness is added.":
        {Language.HINDI: 'ये प्लेटफ़ॉर्म के परिकलित श्रेणी परिणाम और स्कोरिंग इंजन की चेतावनियाँ हैं; कोई कमज़ोरी नहीं जोड़ी गई है।', Language.HINGLISH: 'yeh platform ke computed category results aur scoring engine ki warnings hain; koi weakness add nahi ki gayi hai.'},
    "This is the platform's scoring output, stated as the engine computed it; it is not a fresh call.":
        {Language.HINDI: 'यह प्लेटफ़ॉर्म का स्कोरिंग आउटपुट है, जैसा इंजन ने परिकलित किया; यह नया मूल्यांकन नहीं है।', Language.HINGLISH: 'yeh platform ka scoring output hai, jaise engine ne compute kiya; yeh naya call nahi hai.'},
    "The score, grade, recommendation and category results above are the scoring engine's own outputs; they are not re-derived here.":
        {Language.HINDI: 'ऊपर दिए स्कोर, ग्रेड, अनुशंसा और श्रेणी परिणाम स्कोरिंग इंजन के ही आउटपुट हैं; यहाँ उन्हें दोबारा निकाला नहीं गया है।', Language.HINGLISH: 'upar diye score, grade, recommendation aur category results scoring engine ke hi outputs hain; yahan unhe dobara derive nahi kiya gaya hai.'},
}

#: The illustrative-data disclosure, kept verbatim in every language. It is a
#: required legal statement about the data behind the answer, and paraphrasing
#: it — in any language, including English — is not this renderer's call.
_DISCLOSURE_SENTENCES: frozenset[str] = frozenset({
    "Illustrative valuation only.",
    "Real filings are required for investment-grade outputs.",
})

#: Verdicts a category sentence can carry, each rendered as a phrase whose own
#: head noun fixes the agreement. `मज़बूत`/`कमज़ोर` are invariant adjectives and
#: can agree with the subject directly; `अच्छा स्तर` agrees with `स्तर`
#: (masculine, supplied by this table) and never with the category, whose gender
#: is unknown. A verdict not listed here declines to English rather than risking
#: a wrong inflection.
_GRADE_CASE: dict[str, dict[Language, str]] = {
    "strong": {Language.HINDI: "मज़बूत", Language.HINGLISH: "mazboot"},
    "weak": {Language.HINDI: "कमज़ोर", Language.HINGLISH: "kamzor"},
    "good": {Language.HINDI: "अच्छा स्तर", Language.HINGLISH: "achha level"},
    "average": {Language.HINDI: "साधारण स्तर", Language.HINGLISH: "average level"},
    "fair": {Language.HINDI: "साधारण स्तर", Language.HINGLISH: "theek level"},
    "poor": {Language.HINDI: "कमज़ोर स्तर", Language.HINGLISH: "kamzor level"},
    "excellent": {Language.HINDI: "बेहतरीन स्तर", Language.HINGLISH: "excellent level"},
}

#: The trend phrase is feminine in Hindi (वृद्धि, गिरावट), so its possessive is
#: fixed to की here rather than taken from the general table.
_TREND_OF = {Language.HINDI: "की", Language.HINGLISH: "ki"}
_RECOMMENDATION_OF = {Language.HINDI: "की अनुशंसा", Language.HINGLISH: "ki recommendation"}

#: Sentinel padding, used to build the view the frames are matched against.
#: `protect()` can absorb the whitespace around a masked span — "Revenue is
#: 10 %" masks to "Revenue is§2§" — which would break every literal anchor. The
#: match view restores a single space wherever a sentinel touches a word
#: character, and nowhere else: padding next to punctuation would break the
#: sentence-final anchors, and padding inside a value would change the figure's
#: own spacing.
_PAD_BEFORE = re.compile(r"(?<=\w)(§\d+§)")
_PAD_AFTER = re.compile(r"(§\d+§)(?=\w)")


def _match_view(masked: str) -> str:
    """Spacing-normalised view of masked text, for frame matching only."""
    return _PAD_AFTER.sub(r"\1 ", _PAD_BEFORE.sub(r" \1", masked))


def _g(match: re.Match[str], name: str) -> str:
    """A stripped capture group. Padding and masking both leave stray spaces."""
    return (match.group(name) or "").strip()


#: Runs of spaces inside a *rendered* sentence, collapsed after restoration.
#: A masked figure frequently carries its own surrounding whitespace, so
#: reinserting it into a template can leave two spaces; canonical English is
#: never collapsed, and preserved sentences are never touched.
_COLLAPSE = re.compile(r"[ \t]{2,}")


def _copula(subject: str, language: Language) -> str:
    """``है`` or ``हैं``.

    Copula number is the one agreement this renderer has to compute, because
    the copula sits at the end of a Hindi sentence while its subject is at the
    start. The check is deliberately narrow: a Hindi subject ending in a plural
    marker takes ``हैं``, everything else takes ``है``. Hindi genitive agreement
    is never computed — templates own it.
    """
    if language is Language.HINDI and re.search(r"(?:याँ|ियाँ|ें|एँ)$", subject.strip()):
        return _WORDS["is_plural"][language]
    return _WORDS["is"][language]


@dataclass(frozen=True, slots=True)
class Frame:
    """One recognised sentence shape and its templates.

    ``builder`` receives the regex match, the target language and a callable
    that renders a term through the glossary, and returns the sentence in the
    target language — or ``None`` to decline, which makes the sentence fall
    through to the next frame and, failing all of them, to canonical English.
    """

    name: str
    pattern: re.Pattern[str]
    builder: Callable[["_RenderContext", re.Match[str]], str | None]
    #: True for frames that render a label but deliberately leave a quoted
    #: payload in English (an engine note). The sentence is still emitted and
    #: still counts as rendered; the payload is counted separately, so a reader
    #: of the metadata can see exactly how much of the answer is quoted rather
    #: than rendered.
    quotes_payload: bool = False


@dataclass(slots=True)
class _RenderContext:
    """Per-call state: the target language and the term renderer."""

    language: Language
    terms_rendered: int = 0
    terms_kept_english: int = 0

    def term(self, phrase: str, language: Language | None = None) -> str:
        """Render a financial term through the glossary, or keep it English.

        This is the only route by which financial vocabulary reaches the
        output. ``lookup`` is exact (with a leading determiner stripped), so a
        phrase the glossary does not know is returned unchanged and counted —
        never guessed at.
        """
        target = language or self.language
        cleaned = re.sub(r"^(?:the|The)\s+", "", (phrase or "").strip()).strip()
        cleaned = cleaned.rstrip(":").strip()
        if not cleaned:
            return phrase

        entry = BY_ENGLISH.get(cleaned.lower())
        if entry is None:
            # Unknown to the glossary: keep it in English, minus a leading
            # determiner, which is grammar rather than terminology — "The
            # financial risk score" reads as "financial risk score" inside a
            # Hindi frame.
            self.terms_kept_english += 1
            return cleaned

        rendered = entry.render(target)
        if rendered.strip().lower() == cleaned.lower():
            self.terms_kept_english += 1
            return phrase
        self.terms_rendered += 1
        return rendered

    def word(self, key: str) -> str:
        return _WORDS[key][self.language]

    def say(self, key: str) -> str:
        return _PLATFORM[key][self.language]


# ---------------------------------------------------------------------------
# The frames
# ---------------------------------------------------------------------------

def _build_among_areas(ctx: _RenderContext, m: re.Match[str]) -> str | None:
    rank = m.group("rank")
    key = "among_strongest" if rank == "strongest" else "among_weakest"
    subject = ctx.term(_g(m, "subject"))
    end = "।" if ctx.language is Language.HINDI else "."
    # Both copulas belong to phrases the template itself supplies — the rank
    # phrase, and "इसका स्कोर" — so neither depends on the subject's gender.
    return (
        f"{subject} {ctx.word(key)}; {ctx.word('its_score')} "
        f"{_g(m, 'value')} {_g(m, 'cite')} {ctx.word('is')}{end}"
    )


def _build_category_strength(ctx: _RenderContext, m: re.Match[str]) -> str | None:
    verdict = _GRADE_CASE.get(m.group("grade").lower())
    if verdict is None:
        return None
    subject = ctx.term(_g(m, "subject"))
    end = "।" if ctx.language is Language.HINDI else "."
    return (
        f"{subject} {_g(m, 'value')} {_g(m, 'cite')} {ctx.word('at')} "
        f"{verdict[ctx.language]} {_copula(subject, ctx.language)}{end}"
    )


def _build_value_for_fiscal_year(ctx: _RenderContext, m: re.Match[str]) -> str | None:
    subject = ctx.term(_g(m, "subject"))
    end = "।" if ctx.language is Language.HINDI else "."
    tail = _g(m, "tail")
    return (
        f"{subject} {_g(m, 'fy')} {ctx.word('for')} {_g(m, 'value')} "
        f"{_g(m, 'cite')}{' ' + tail if tail else ''} "
        f"{_copula(subject, ctx.language)}{end}"
    )


def _build_value_for_company(ctx: _RenderContext, m: re.Match[str]) -> str | None:
    subject = ctx.term(_g(m, "subject"))
    company = _g(m, "company")
    end = "।" if ctx.language is Language.HINDI else "."
    return (
        f"{company} {ctx.word('for')} {subject} {_g(m, 'value')} "
        f"{_g(m, 'cite')} {_copula(subject, ctx.language)}{end}"
    )


def _build_composite_score(ctx: _RenderContext, m: re.Match[str]) -> str | None:
    subject = _g(m, "subject")
    grade = _g(m, "grade")
    # The provenance clause is literal in the engine's output, so the frame
    # matches it exactly and renders the platform phrases it is made of. If it
    # ever changes, the frame simply stops matching and the sentence stays in
    # English — which is the correct failure for a clause we cannot place.
    provenance = bool(m.groupdict().get("provenance"))

    # The ergative construction ("ने … स्कोर किया") is used deliberately: with an
    # unmarked object the verb takes its default form, so the company's own
    # gender never has to be known.
    where = (
        f"{ctx.say('platform')} {ctx.say('institutional_scoring')} {ctx.word('at')} "
        if provenance and ctx.language is Language.HINDI
        else f"{ctx.say('institutional_scoring')} {ctx.word('at')} "
        if provenance
        else f"{ctx.word('at')} "
    )
    end = "।" if ctx.language is Language.HINDI else "."

    return (
        f"{subject} {ctx.word('it_has')} {where}{_g(m, 'value')} "
        f"{_g(m, 'cite')} {ctx.word('scored')}; {ctx.say('grade')} "
        f"'{grade}' {_g(m, 'grade_cite')} {ctx.word('is')}{end}"
    )


def _build_scores_out_of_ten(ctx: _RenderContext, m: re.Match[str]) -> str | None:
    subject = ctx.term(_g(m, "subject"))
    end = "।" if ctx.language is Language.HINDI else "."
    # "का स्कोर" is the template's own possessive: स्कोर is masculine, so the
    # agreement is fixed here and the copula agrees with स्कोर for the same
    # reason — never with the subject.
    return (
        f"{subject} {_SCORE_OF[ctx.language]} {_g(m, 'value')} "
        f"{_g(m, 'cite')} {ctx.word('is')}{end}"
    )


def _build_recommendation_for_company(ctx: _RenderContext, m: re.Match[str]) -> str | None:
    company = _g(m, "company")
    end = "।" if ctx.language is Language.HINDI else "."
    # "की अनुशंसा": अनुशंसा is feminine, and the template supplies the noun, so
    # the feminine possessive is written here rather than inferred.
    return (
        f"{company} {ctx.word('for')} {ctx.say('scoring_engine')} "
        f"{_RECOMMENDATION_OF[ctx.language]} '{_g(m, 'rec')}' {_g(m, 'cite')} "
        f"{ctx.word('is')}{end}"
    )


def _build_recommendation_confidence(ctx: _RenderContext, m: re.Match[str]) -> str | None:
    end = "।" if ctx.language is Language.HINDI else "."
    return (
        f"{ctx.say('scoring_engine')} {_RECOMMENDATION_OF[ctx.language]} "
        f"'{_g(m, 'rec')}' {_g(m, 'cite')} {ctx.word('is')}, {ctx.word('and')} "
        f"Data confidence {_g(m, 'conf')} {_g(m, 'conf_cite')} "
        f"{ctx.word('is')}{end}"
    )


def _build_weighted_value(ctx: _RenderContext, m: re.Match[str]) -> str | None:
    upside = ctx.term("Upside")
    end = "।" if ctx.language is Language.HINDI else "."
    return (
        f"{ctx.say('platform')} {ctx.word('at')} weighted intrinsic value "
        f"{_g(m, 'value')} {_g(m, 'cite')} {ctx.word('is')}; current market price "
        f"{_g(m, 'market_cite')} {ctx.word('compared_with')} "
        f"{_g(m, 'upside')} {upside} {ctx.word('is')}{end}"
    )


def _build_valuation_view(ctx: _RenderContext, m: re.Match[str]) -> str | None:
    end = "।" if ctx.language is Language.HINDI else "."
    return (
        f"{ctx.say('valuation_engine')} {ctx.word('own_view')} "
        f"'{_g(m, 'view')}' {_g(m, 'cite')} {ctx.word('is')}{end}"
    )


def _build_data_confidence(ctx: _RenderContext, m: re.Match[str]) -> str | None:
    end = "।" if ctx.language is Language.HINDI else "."
    return (
        f"Data confidence {_g(m, 'value')} {_g(m, 'cite')} "
        f"{ctx.word('is')}{end}"
    )


def _build_derived_from(ctx: _RenderContext, m: re.Match[str]) -> str | None:
    end = "।" if ctx.language is Language.HINDI else "."
    return (
        f"{ctx.word('this_is')} Composite score {_g(m, 'value')} "
        f"{_g(m, 'cite')} {ctx.word('from')} {ctx.word('derived')} "
        f"{ctx.word('is')}{end}"
    )


def _build_growth_for_company(ctx: _RenderContext, m: re.Match[str]) -> str | None:
    """``Revenue grew 10% for TCS [revenue]`` → a Hindi/Hinglish equivalent.

    Composed as ``… में 10% की वृद्धि हुई``: the locative (में) and the
    possessive (की) are invariantly marked, and ``वृद्धि हुई`` is an internally
    consistent feminine pair owned by the template. Nothing here has to agree
    with the metric, which may be any noun phrase at all.
    """
    metric = ctx.term(_g(m, "metric"))
    company = _g(m, "company")
    rising = m.group("verb").lower() in {"grew", "rose", "increased", "improved"}
    end = "।" if ctx.language is Language.HINDI else "."
    return (
        f"{company} {ctx.word('for')} {metric} {ctx.word('in')} "
        f"{_g(m, 'value')} {_TREND_OF[ctx.language]} {_growth_word(ctx, rising)} "
        f"{ctx.word('happened')} {_g(m, 'cite')}{end}"
    )


def _growth_word(ctx: _RenderContext, rising: bool) -> str:
    """The trend noun, from the glossary when it has one.

    "Growth" is in the glossary; a fall is not, so the frame supplies its own
    word — a connective-level choice rather than a financial one, and published
    in :meth:`InternalLanguageRenderer.vocabulary` like the rest.
    """
    if not rising:
        return ctx.word("decline")
    if ctx.language is Language.HINDI:
        rendered = ctx.term("Growth")
        return rendered if rendered != "Growth" else ctx.word("growth")
    return ctx.word("growth")


def _build_engine_notes(ctx: _RenderContext, m: re.Match[str]) -> str | None:
    """Render the label of an engine note; the note itself stays English.

    The payload is free text a scoring module wrote. It is quoted evidence, not
    prose this platform composed, so the renderer does not translate it — it
    keeps it verbatim and the sentence is reported as preserved.
    """
    engine = ctx.say("scoring_engine")
    if ctx.language is Language.HINDI:
        return f"{engine} की टिप्पणी:{m.group('payload')}"
    return f"{engine} note:{m.group('payload')}"



def _build_category_rated(ctx: _RenderContext, m: re.Match[str]) -> str | None:
    """``The scoring engine rates the category 'Good'.``"""
    grade = _g(m, "grade")
    end = "।" if ctx.language is Language.HINDI else "."
    return (
        f"{ctx.say('scoring_engine')} {ctx.word('category')} {ctx.word('to')} "
        f"'{grade}' {ctx.word('rates')}{end}"
    )


def _build_category_confidence(ctx: _RenderContext, m: re.Match[str]) -> str | None:
    """``Confidence in the category's inputs is 'High'.``"""
    level = _g(m, "level")
    end = "।" if ctx.language is Language.HINDI else "."
    return (
        f"{ctx.word('category')} {ctx.word('possessive')} {ctx.word('inputs')} "
        f"{ctx.word('at')} {ctx.word('confidence')} '{level}' "
        f"{ctx.word('is')}{end}"
    )


def _build_not_assessed(ctx: _RenderContext, m: re.Match[str]) -> str | None:
    """``Momentum could not be assessed — no supporting data available.``

    A refusal, not a score: the rendering has to say the same thing as the
    English, which is why it states that the assessment could not be made
    rather than that nothing was found.
    """
    subject = ctx.term(_g(m, "subject"))
    end = "।" if ctx.language is Language.HINDI else "."
    return (
        f"{subject} {ctx.word('of')} {ctx.word('assessment')} "
        f"{ctx.word('not')} {ctx.word('could')} — {ctx.word('no')} "
        f"{ctx.word('supporting')} {ctx.word('data')} {ctx.word('available')} "
        f"{ctx.word('not')} {ctx.word('is')}{end}"
    )


def _build_forecast_cagr(ctx: _RenderContext, m: re.Match[str]) -> str | None:
    """``The forecast projects revenue CAGR of 12.00 % [forecast_revenue_cagr].``

    ``CAGR`` arrives as a masked acronym (``§19§``), so it is captured as a span
    and restored untouched — the renderer never spells an acronym out.
    """
    metric = ctx.term("Revenue")
    acronym = _g(m, "acronym")
    end = "।" if ctx.language is Language.HINDI else "."
    return (
        f"{metric} {acronym} {_g(m, 'value')} {_g(m, 'cite')} "
        f"{ctx.word('expected')}{end}"
    )


def _build_ranks_areas(ctx: _RenderContext, m: re.Match[str]) -> str | None:
    """``The scoring engine ranks these areas highest for X: <sentence>``

    The heading and the sentence after the colon are handled as one unit: if the
    inner sentence cannot be rendered, the whole thing is left in English rather
    than emitting a heading in Hindi over an English paragraph.
    """
    company = _g(m, "company")
    tail = _g(m, "tail")
    inner, quotes, _ = _sentence_text(tail, ctx)
    if inner is None:
        return None
    rank = "highest" if m.group("rank") == "highest" else "lowest"
    return (
        f"{ctx.say('scoring_engine')} {company} {ctx.word('for')} "
        f"{ctx.word('these_areas')} {ctx.word('to')} {ctx.word(rank)} "
        f"{ctx.word('ranks')}: {inner}"
    )


def _frames() -> tuple[Frame, ...]:
    """The recognised sentence shapes, most specific first.

    Every pattern is written against the *masked* sentence as `protect()`
    actually produces it, and matched against the padded view
    (:func:`_match_view`). Three consequences of masking are load-bearing here:

    * a figure absorbs the whitespace around it, so masked text contains
      ``is§2§for`` — frames therefore use ``\s*`` at every figure boundary
      rather than assuming a space;
    * a figure and the citation that follows can end up adjacent
      (``§2§/§3§§0§``), so the same applies before every citation;
    * an all-caps quoted value such as ``'HOLD'`` is itself a protected span.

    A pattern that does not match leaves the sentence in English, so a frame
    that is too narrow costs coverage and can never corrupt prose. A reckless
    one would do the reverse, which is why the anchors are literal phrases
    rather than "a noun phrase is some other noun phrase".
    """
    def pattern(body: str) -> re.Pattern[str]:
        return re.compile(body)

    # A value run: the figure plus whatever unit and punctuation masking left in
    # place, stopping short of the citation that must follow it.
    value = rf"(?P<value>{_VALUE_RUN}{{1,30}}?)"

    return (
        Frame("composite_score", pattern(
            rf"^(?P<subject>{_SLOT}{{1,90}}?) scores {value}\s*(?P<cite>{_TICK})"
            rf"(?P<provenance> on the platform's institutional scoring)?, graded "
            rf"'(?P<grade>[^']{{1,4}})' (?P<grade_cite>{_TICK})\.$",
        ), _build_composite_score),

        Frame("engine_recommendation_at_confidence", pattern(
            rf"^The scoring engine's recommendation is '(?P<rec>{_TICK}|[^']{{1,24}})' "
            rf"(?P<cite>{_TICK}), at (?P<conf>{_TICK})\s*data confidence "
            rf"(?P<conf_cite>{_TICK})\.$",
        ), _build_recommendation_confidence),

        Frame("engine_recommendation_for_company", pattern(
            rf"^The scoring engine's recommendation for (?P<company>{_SLOT}{{1,80}}?) "
            rf"is '(?P<rec>{_TICK}|[^']{{1,24}})' (?P<cite>{_TICK})\.$",
        ), _build_recommendation_for_company),

        Frame("weighted_intrinsic_value", pattern(
            rf"^The platform's weighted intrinsic value is {value}\s*"
            rf"(?P<cite>{_TICK}), an upside of (?P<upside>{_TICK}) versus the "
            rf"current market price (?P<market_cite>{_TICK})\.$",
        ), _build_weighted_value),

        Frame("valuation_view", pattern(
            rf"^The valuation engine's own view is '(?P<view>[^']{{1,24}})' "
            rf"(?P<cite>{_TICK})\.$",
        ), _build_valuation_view),

        Frame("among_areas", pattern(
            rf"^(?P<subject>{_SLOT}{{1,80}}?) (?:is|are) among the "
            rf"(?P<rank>strongest|weakest) areas, scoring {value}\s*"
            rf"(?P<cite>{_TICK})\.$",
        ), _build_among_areas),

        Frame("category_strength", pattern(
            rf"^(?P<subject>{_SLOT}{{1,60}}?) (?:is|are) (?P<grade>strong|weak|good|fair|poor|average|excellent) at "
            rf"{value}\s*(?P<cite>{_TICK})\.$",
        ), _build_category_strength),

        Frame("growth_for_company", pattern(
            rf"^(?P<metric>{_SLOT}{{1,50}}?) "
            rf"(?P<verb>grew|rose|increased|improved|declined|fell|decreased) "
            rf"{value} for (?P<company>{_SLOT}{{1,60}}?)\s*(?P<cite>{_TICK})\.$",
        ), _build_growth_for_company),

        Frame("derived_from_composite", pattern(
            rf"^It is derived from the composite score of {value}\s*"
            rf"(?P<cite>{_TICK})\.$",
        ), _build_derived_from),

        Frame("scores_out_of_ten", pattern(
            rf"^(?P<subject>{_SLOT}{{1,70}}?) scores {value}\s*(?P<cite>{_TICK})\.$",
        ), _build_scores_out_of_ten),

        Frame("data_confidence", pattern(
            rf"^Data confidence is {value}\s*(?P<cite>{_TICK})\.$",
        ), _build_data_confidence),

        Frame("value_for_company", pattern(
            rf"^(?P<subject>{_SLOT}{{1,70}}?) for (?P<company>{_SLOT}{{1,70}}?) "
            rf"(?:is|are) {value}\s*(?P<cite>{_TICK})\.$",
        ), _build_value_for_company),

        Frame("value_for_fiscal_year", pattern(
            rf"^(?P<subject>{_SLOT}{{1,70}}?) (?:is|are) {value} for "
            rf"(?P<fy>{_TICK})\s*(?P<cite>{_TICK})(?P<tail>[^§]{{0,80}}?)\.$",
        ), _build_value_for_fiscal_year),

        Frame("category_rated", pattern(
            rf"^The scoring engine rates the category '(?P<grade>[^']{{1,12}})'\.$",
        ), _build_category_rated),

        Frame("category_confidence", pattern(
            rf"^Confidence in the category's inputs is '(?P<level>[^']{{1,12}})'\.$",
        ), _build_category_confidence),

        Frame("ranks_areas", pattern(
            rf"^The scoring engine ranks these areas (?P<rank>highest|lowest) "
            rf"for (?P<company>{_SLOT}{{1,80}}?): (?P<tail>.+)$",
        ), _build_ranks_areas),

        Frame("not_assessed", pattern(
            rf"^(?P<subject>{_SLOT}{{1,60}}?) could not be assessed "
            rf"— no supporting data available\.$",
        ), _build_not_assessed),

        Frame("forecast_cagr", pattern(
            rf"^The forecast projects revenue (?P<acronym>{_TICK}) of\s*"
            rf"{value}\s*(?P<cite>{_TICK})\.$",
        ), _build_forecast_cagr),

        Frame("engine_notes", pattern(
            r"^The scoring engine notes:(?P<payload>.+)$",
        ), _build_engine_notes, quotes_payload=True),
    )


FRAMES: tuple[Frame, ...] = _frames()


# ---------------------------------------------------------------------------
# The renderer
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class _Segment:
    """One line of the answer, classified."""

    kind: str
    text: str


class InternalLanguageRenderer:
    """Renders canonical English answers into Hindi and Hinglish.

    Stateless and synchronous by design: it opens no session, reads no table and
    calls no network, which is what makes it safe to run on the response path
    and cheap enough to run on every non-English request.
    """

    name = "internal"

    def supports(self, language: Language) -> bool:
        return language in INTERNAL_LANGUAGES

    # ------------------------------------------------------------------ api
    def render(
        self,
        text: str,
        language: Language,
        *,
        entities: Iterable[str] | None = None,
    ) -> RenderResult:
        """Render ``text`` into ``language``.

        Returns a :class:`RenderResult` whose ``text`` is the rendered answer,
        whose ``fidelity`` says how complete that rendering is, and whose
        ``problems`` is non-empty only when the rendering was rejected — in
        which case ``text`` is the untouched English original.
        """
        started = time.perf_counter()
        source = text or ""

        if language is CANONICAL_LANGUAGE or not source.strip():
            return self._english(source, language, started)

        if not self.supports(language):
            # Planned languages are the adapter's business; this renderer
            # declines rather than substituting a language it was not asked for.
            return self._rejected(
                source, language, started,
                [f"{spec_for(language).label} is not rendered internally."],
                detail=(
                    f"{spec_for(language).label} is declared in the architecture "
                    "but this renderer has no module for it; the response is in "
                    "canonical English."
                ),
            )

        entities = [e for e in (entities or []) if e and e.strip()]
        protection = protect(source, extra_terms=entities)
        ctx = _RenderContext(language=language)

        rendered_lines: list[str] = []
        #: Whether each line contains a sentence this renderer composed. Only
        #: those may have their whitespace normalised; a sentence that was left
        #: in English must come back byte-identical to how it went in.
        composed_lines: list[bool] = []
        reasons: dict[str, int] = {}
        rendered_sentences = preserved_sentences = labels = quoted = 0

        def emit(text: str, *, rendered: bool) -> None:
            rendered_lines.append(text)
            composed_lines.append(rendered)

        for segment in self._segment(protection.masked):
            if segment.kind in {"blank", "separator"}:
                emit(segment.text, rendered=False)
                continue

            if segment.kind == "disclosure":
                emit(segment.text, rendered=False)
                preserved_sentences += 1
                reasons["disclosure"] = reasons.get("disclosure", 0) + 1
                continue

            if segment.kind == "labelled":
                head, _, rest = segment.text.partition(":")
                label = self._label(head, language)
                if label:
                    emit(f"{label}:{rest}", rendered=True)
                    labels += 1
                else:
                    emit(segment.text, rendered=False)
                # A label line's payload may itself be a frame ("Composite:
                # Bharat Consumer Products Ltd scores 71.05 /100 …"), so the
                # remainder is offered to the frames as well.
                if rest.strip():
                    rendered, quotes, reason = self._render_sentence(
                        _match_view(rest), ctx,
                    )
                    prefix = f"{label}:" if label else f"{head}:"
                    if rendered is not None:
                        rendered_lines[-1] = f"{prefix} {rendered}"
                        rendered_sentences += 1
                    else:
                        # Nothing matched inside: the original remainder, with
                        # its own spacing, is what stays.
                        rendered_lines[-1] = (
                            f"{prefix}{rest}" if label else segment.text
                        )
                        preserved_sentences += 1
                        reasons[reason] = reasons.get(reason, 0) + 1
                    if quotes:
                        quoted += 1
                continue

            if segment.kind == "bullet":
                marker, _, rest = segment.text.partition(" ")
                rendered, quotes, reason = self._render_sentence(
                    _match_view(rest), ctx,
                )
                if rendered is not None:
                    emit(f"{marker} {rendered}", rendered=True)
                    rendered_sentences += 1
                else:
                    emit(segment.text, rendered=False)
                    preserved_sentences += 1
                    reasons[reason] = reasons.get(reason, 0) + 1
                if quotes:
                    quoted += 1
                continue

            for padded, original in self._sentences(segment.text):
                if original.strip() in _DISCLOSURE_SENTENCES:
                    # The disclosure is a required statement about the data
                    # behind the answer. It is preserved verbatim and reported
                    # as a disclosure, not as a sentence we failed on.
                    emit(original, rendered=False)
                    preserved_sentences += 1
                    reasons["disclosure"] = reasons.get("disclosure", 0) + 1
                    continue
                rendered, quotes, reason = self._render_sentence(padded, ctx)
                if rendered is not None:
                    emit(rendered, rendered=True)
                    rendered_sentences += 1
                else:
                    emit(original, rendered=False)
                    preserved_sentences += 1
                    reasons[reason] = reasons.get(reason, 0) + 1
                if quotes:
                    quoted += 1

        composed = "\n".join(rendered_lines)
        restoration = restore(composed, protection)
        cleaned = self._normalise(restoration.text, composed_lines)

        problems: list[str] = []
        if restoration.lost:
            kinds = sorted({span.kind for span in restoration.lost})
            problems.append(
                f"{len(restoration.lost)} protected token(s) "
                f"({', '.join(kinds)}) were lost while rendering."
            )
        if restoration.spurious:
            problems.append(
                f"{len(restoration.spurious)} sentinel(s) were invented while "
                "rendering."
            )
        # Verification runs on the text that will actually be served, not on
        # the intermediate: whitespace normalisation happens first so the
        # figures and citations are checked as the reader will see them.
        problems.extend(verify_preserved(source, cleaned))
        if language is Language.HINGLISH and _DEVANAGARI.search(cleaned):
            # Hard invariant: the register is Roman script. A Devanagari
            # character here means a template leaked or a term was rendered in
            # the wrong language, and the answer would reach a reader in a
            # script they did not ask for.
            problems.append("Devanagari was produced for a Hinglish answer.")

        if problems:
            log.warning("internal rendering rejected", language=language.value,
                        problems=problems)
            return self._rejected(source, language, started, problems)

        total = rendered_sentences + preserved_sentences
        coverage = rendered_sentences / total if total else 1.0
        if rendered_sentences == 0:
            fidelity = Fidelity.NONE
        elif preserved_sentences == 0:
            fidelity = Fidelity.FULL
        else:
            fidelity = Fidelity.PARTIAL

        result = RenderResult(
            text=cleaned if fidelity is not Fidelity.NONE else source,
            language=language,
            fidelity=fidelity,
            coverage=coverage,
            prose_sentences_rendered=rendered_sentences,
            prose_sentences_preserved=preserved_sentences,
            labels_rendered=labels,
            quoted_payloads=quoted,
            terms_rendered=ctx.terms_rendered,
            terms_kept_english=ctx.terms_kept_english,
            preserved_reasons=reasons,
            protected_spans=protection.kinds(),
            detail=self._detail(language, fidelity, rendered_sentences,
                                preserved_sentences),
            latency_ms=(time.perf_counter() - started) * 1000,
        )
        log.debug("internal rendering", **result.as_dict())
        return result

    def vocabulary(self) -> frozenset[str]:
        """Every Devanagari word this renderer may emit.

        The union of the frame templates, the label and connective tables, and
        every Hindi rendering in the glossary. Published so the guarantee can be
        tested rather than asserted: if a template ever coins a word, the word
        is not in here and the test suite fails.
        """
        words: set[str] = set()

        def collect(text: str) -> None:
            nonlocal words
            words |= set(_DEVANAGARI_WORD.findall(text or ""))

        # Nested tables map a key to a per-language phrase; flat ones are keyed
        # by language directly.
        for table in (_PLATFORM, _WORDS, _GRADE_CASE):
            for values in table.values():
                collect(values[Language.HINDI])
        for flat in (_SCORE_OF, _RECOMMENDATION_OF, _TREND_OF):
            collect(flat[Language.HINDI])
        for labels in _LABELS.values():
            collect(labels[Language.HINDI])
        for sentence in _FIXED.values():
            collect(sentence[Language.HINDI])
        collect("।")
        for term in TERMS:
            collect(term.hindi)
        # Literal fragments the builders interleave with the tables above.
        for template in _TEMPLATE_TEXT:
            collect(template)
        return frozenset(words)

    # ------------------------------------------------------------ internals
    def _english(self, source: str, language: Language,
                 started: float) -> RenderResult:
        return RenderResult(
            text=source, language=CANONICAL_LANGUAGE, fidelity=Fidelity.FULL,
            coverage=1.0,
            detail="English is the canonical language; nothing was transformed.",
            latency_ms=(time.perf_counter() - started) * 1000,
        )

    def _rejected(self, source: str, language: Language, started: float,
                  problems: list[str], detail: str = "") -> RenderResult:
        """Fail closed: the English original, and the reason."""
        return RenderResult(
            text=source, language=language, fidelity=Fidelity.NONE,
            coverage=0.0, problems=problems,
            detail=detail or (
                "The answer could not be rendered internally without risking "
                "the protected content it carries, so the canonical English "
                "original is returned unchanged."
            ),
            latency_ms=(time.perf_counter() - started) * 1000,
        )

    def _detail(self, language: Language, fidelity: Fidelity, rendered: int,
                preserved: int) -> str:
        label = spec_for(language).label
        if fidelity is Fidelity.FULL:
            return (
                f"Rendered internally in {label} by the platform's "
                "deterministic renderer; no external provider was called."
            )
        return (
            f"{rendered} of {rendered + preserved} sentences were rendered "
            f"internally in {label}; the remaining {preserved} are retained in "
            "canonical English because the deterministic renderer cannot "
            "translate them safely."
        )

    @staticmethod
    def _split_sentences(text: str) -> list[str]:
        """Split masked text into sentences, never inside a protected span."""
        parts = _SENTENCE_BOUNDARY.split(text.strip())
        return [part for part in (p.strip() for p in parts) if part]

    def _sentences(self, masked: str) -> list[tuple[str, str]]:
        """Pair each sentence's match view with its original masked text.

        Padding adds spaces to a sentinel's neighbourhood, never punctuation, so
        both views split into the same sentences in the same order — and a
        sentence that no frame matches is then emitted from the *original*, which
        is what keeps untouched English byte-identical. The length check is a
        guard rather than an expectation: if the two views ever disagreed, the
        original is used for both roles and nothing is reordered on a guess.
        """
        originals = self._split_sentences(masked)
        padded = self._split_sentences(_match_view(masked))
        if len(padded) != len(originals):
            return [(sentence, sentence) for sentence in originals]
        return list(zip(padded, originals))

    @staticmethod
    def _normalise(text: str, composed_lines: list[bool]) -> str:
        """Collapse whitespace runs on rendered lines only.

        A masked figure often carries its own surrounding spaces, so putting it
        back into a template can leave two spaces where there was one. Lines
        containing nothing this renderer composed are returned untouched.
        """
        lines = text.split("\n")
        if len(lines) != len(composed_lines):
            return text
        return "\n".join(
            _COLLAPSE.sub(" ", line).strip() if rendered else line
            for line, rendered in zip(lines, composed_lines)
        )

    def _render_sentence(self, sentence: str, ctx: _RenderContext,
                         ) -> tuple[str | None, bool, str]:
        """Render one sentence, or decline. See :func:`_sentence_text`."""
        return _sentence_text(sentence, ctx)


    def _segment(self, masked: str) -> list[_Segment]:
        """Classify the masked answer into structural lines and prose."""
        segments: list[_Segment] = []
        lines = masked.split("\n")
        index = 0

        while index < len(lines):
            line = lines[index]
            stripped = line.strip()

            if not stripped:
                segments.append(_Segment("blank", ""))
                index += 1
                continue
            if _RULE.match(stripped):
                segments.append(_Segment("separator", stripped))
                index += 1
                continue
            if _ITALIC_BLOCK.match(stripped):
                segments.append(_Segment("disclosure", stripped))
                index += 1
                continue

            bullet = _BULLET.match(stripped)
            if bullet:
                segments.append(_Segment("bullet", stripped))
                index += 1
                continue

            labelled = _LABELLED.match(stripped)
            if labelled and self._label(labelled.group("label"), Language.HINDI):
                segments.append(_Segment("labelled", stripped))
                index += 1
                continue

            segments.append(_Segment("prose", stripped))
            index += 1

        return segments

    @staticmethod
    def _label(label: str, language: Language) -> str:
        """Render a structural label. See :func:`_label_for`."""
        return _label_for(label, language)



def _label_for(label: str, language: Language) -> str:
    """Render a structural label, from the glossary first.

    Returning an empty string means "not a label this renderer owns" and the
    line is treated as prose.
    """
    key = label.strip().lower()
    entry = BY_ENGLISH.get(key)
    if entry is not None:
        rendered = entry.render(language)
        if rendered.strip().lower() != key:
            return rendered
    table = _LABELS.get(key)
    if table is None:
        return ""
    return table[language]

def _sentence_text(sentence: str, ctx: _RenderContext,
                   ) -> tuple[str | None, bool, str]:
    """Render one sentence, or decline.

    Returns ``(text, quotes_payload, reason)``, where ``text is None`` means
    no frame matched and the caller must emit the original sentence
    unchanged. ``quotes_payload`` distinguishes a sentence that was fully
    rendered from one whose label was rendered while a quoted payload — an
    engine's own note — stayed in English, so the metadata never claims more
    than was actually rendered.
    """
    stripped = sentence.strip()
    if not stripped:
        return "", False, ""

    fixed = _FIXED.get(stripped)
    if fixed is not None:
        return fixed[ctx.language], False, "fixed_sentence"

    # A structural label is rendered wherever it appears; the payload after
    # it is then offered to the frames on its own, and kept verbatim if none
    # matches. This is how "Strongest: …" becomes Hindi without touching the
    # engine sentence that follows the colon.
    labelled = _SENTENCE_LABEL.match(stripped)
    if labelled:
        label = _label_for(labelled.group("label"), ctx.language)
        if label:
            rest = labelled.group("rest").strip()
            inner, quotes, reason = _sentence_text(rest, ctx)
            if inner is None:
                return (
                    f"{label}:{labelled.group('rest')}", True,
                    "labelled_payload",
                )
            return f"{label}: {inner}", quotes, reason

    for frame in FRAMES:
        match = frame.pattern.match(stripped)
        if match is None:
            continue
        try:
            rendered = frame.builder(ctx, match)
        except Exception:  # noqa: BLE001 — a frame bug must not lose an answer
            log.exception("frame failed", frame=frame.name)
            continue
        if not rendered:
            continue
        return rendered, frame.quotes_payload, frame.name

    return None, False, "unsupported_sentence"

#: The Hindi templates this module can produce, for the vocabulary guarantee.
#: Kept next to the builders rather than extracted from source so the published
#: set is explicit about what is allowed.
_TEMPLATE_TEXT: tuple[str, ...] = (
    "सबसे मज़बूत क्षेत्रों में शामिल है", "सबसे कमज़ोर क्षेत्रों में शामिल है",
    "इसका स्कोर", "का स्कोर", "मज़बूत", "कमज़ोर", "स्कोर किया", "ग्रेड",
    "प्लेटफ़ॉर्म", "संस्थागत स्कोरिंग", "स्कोरिंग इंजन", "मूल्यांकन इंजन",
    "टिप्पणी", "का अपना दृष्टिकोण", "के लिए", "और", "यह", "से", "निकाला गया",
    "की तुलना में", "वृद्धि", "गिरावट", "हुई", "है", "हैं", "में", "पर", "ने",
    "का", "की", "समग्र", "क्यों विचार करें", "जोखिम", "अंतिम निष्कर्ष", "मूल्यांकन नहीं किया गया",
    "मुख्य बिंदु", "सारांश", "निष्कर्ष", "मज़बूतियाँ", "कमज़ोरियाँ",
)


# ---------------------------------------------------------------------------
# Pipeline bridge: the renderer as a Translator
# ---------------------------------------------------------------------------

class InternalRendererTranslator:
    """The renderer behind the existing ``Translator`` protocol.

    Deliberately a thin adapter: the pipeline keeps calling ``translate``, so
    nothing about the adapter, the analyst or the audit changes, and the
    renderer stays a separate abstraction from both the glossary translator and
    the provider-backed one.

    A partial rendering is reported with ``translated=False`` — the boolean is
    the honest answer to "is this fully in the target language?" — while the
    partial text is still returned for a caller that can use it, with
    ``fidelity="partial"`` and the coverage in ``detail``.
    """

    name = "internal"

    def __init__(self, renderer: InternalLanguageRenderer | None = None) -> None:
        self.renderer = renderer or InternalLanguageRenderer()

    def supports(self, language: Language) -> bool:
        return self.renderer.supports(language)

    async def translate(self, text: str, language: Language, *,
                        entities: list[str] | None = None) -> TranslationResult:
        result = self.renderer.render(text, language, entities=entities)

        if result.fidelity is Fidelity.NONE:
            return TranslationResult(
                text=text, language=CANONICAL_LANGUAGE, translated=False,
                provider=self.name, detail=result.detail,
                integrity_problems=list(result.problems),
                fidelity=Fidelity.NONE.value,
                coverage=0.0,
                latency_ms=result.latency_ms,
            )

        return TranslationResult(
            text=result.text, language=language,
            translated=result.translated,
            provider=self.name,
            detail=result.detail,
            integrity_problems=list(result.problems),
            fidelity=result.fidelity.value,
            # Coverage is the renderer's own count of rendered sentences; the
            # adapter reports it so "partial" is never a bare label.
            coverage=result.coverage,
            latency_ms=result.latency_ms,
        )


class InternalFallbackTranslator:
    """Internal renderer first, external provider when it is needed.

    Policy, in order:

    1. If the renderer can produce the *whole* answer internally, use it. This
       is the deterministic answer path — structured, citation-backed output —
       and it now costs nothing and depends on nothing.
    2. Otherwise ask the configured provider, which remains the quality path for
       arbitrary prose and is left exactly as it was.
    3. If the provider fails — HTTP 402, exhausted free tier, no key — serve the
       renderer's partial rendering rather than English, and say so. A reader
       who asked in Hindi is better served by a partly-rendered Hindi answer
       that states how much was rendered than by an English one.

    Selecting this is one configuration value (``TRANSLATION_PROVIDER=hybrid``);
    the default stays the provider so Phase 2A changes no production behaviour
    on its own.
    """

    name = "hybrid"

    def __init__(self, primary: Any, internal: InternalRendererTranslator | None = None
                 ) -> None:
        self.primary = primary
        self.internal = internal or InternalRendererTranslator()

    def supports(self, language: Language) -> bool:
        return self.internal.supports(language) or bool(
            getattr(self.primary, "supports", lambda _l: False)(language)
        )

    async def translate(self, text: str, language: Language, *,
                        entities: list[str] | None = None) -> TranslationResult:
        started = time.perf_counter()

        internal = await self.internal.translate(text, language, entities=entities)
        if internal.translated:
            return internal

        primary = await self.primary.translate(text, language, entities=entities)
        if primary.translated:
            return primary

        # Both declined. Prefer whatever the renderer managed, because it is in
        # the reader's language; failing that, the provider's result, which
        # carries its own explanation of why it returned English.
        best = internal if internal.text.strip() != (text or "").strip() else primary
        reason = primary.detail or "the translation provider was unavailable"
        best.detail = (
            f"{reason} The deterministic renderer produced {internal.text and 'a partial' or 'no'} "
            f"rendering; {internal.detail or ''}".strip()
        )
        best.latency_ms = (time.perf_counter() - started) * 1000
        log.info("hybrid translation fell back internally",
                 language=language.value, provider=best.provider)
        return best
