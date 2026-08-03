"""Automatic language detection for incoming questions.

No language selector is required, so this runs on every request. It has to be
right on very short inputs — "Revenue kya hai" is fourteen characters and must
resolve to Hinglish, not English — which rules out the usual n-gram identifiers.
Those are trained on prose and are close to a coin flip at this length.

The approach is a scored decision over three signals:

1. **Script.** Any Devanagari at all is decisive for Hindi, because nobody
   types Devanagari by accident. This is checked first and short-circuits.
2. **Romanised Hindi function words.** `kya`, `hai`, `kaisa`, `kitna`, `ka`,
   `ki`, `ke`, `mein`, `nahi`, `bata` and their spelling variants. Function
   words rather than content words, because Indian financial vocabulary is
   English even in Hindi speech — the give-away is the grammar, not the nouns.
3. **English function words.** `how`, `what`, `is`, `the`, `does`, and so on.

The critical design decision is the **asymmetry between signals**. A Hinglish
question is mostly English tokens by count — "Revenue growth kaisa hai" is two
English words and two Hindi ones — so a majority vote returns English and the
feature fails on precisely the examples in the brief. Romanised Hindi markers
therefore carry far more weight than English ones: their presence is strong
positive evidence, while an English word is only weak evidence of English
because it appears just as readily inside Hinglish.

Everything is deterministic. The same string always yields the same language,
because a chat session that answered in Hindi and then switched to English on a
follow-up would look broken.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from app.domain.language.types import AUTO, Language, Script

# ---------------------------------------------------------------------------
# Unicode ranges
# ---------------------------------------------------------------------------

_DEVANAGARI = re.compile(r"[\u0900-\u097F]")
_GUJARATI = re.compile(r"[\u0A80-\u0AFF]")
_TAMIL = re.compile(r"[\u0B80-\u0BFF]")
_TELUGU = re.compile(r"[\u0C00-\u0C7F]")
_KANNADA = re.compile(r"[\u0C80-\u0CFF]")
_BENGALI = re.compile(r"[\u0980-\u09FF]")

#: Script probes in priority order. Devanagari serves both Hindi and Marathi;
#: distinguishing them needs a Marathi module, so it resolves to Hindi today
#: and the ambiguity is recorded on the result rather than hidden.
_SCRIPT_PROBES: tuple[tuple[re.Pattern[str], Script, Language], ...] = (
    (_DEVANAGARI, Script.DEVANAGARI, Language.HINDI),
    (_GUJARATI, Script.GUJARATI, Language.GUJARATI),
    (_TAMIL, Script.TAMIL, Language.TAMIL),
    (_TELUGU, Script.TELUGU, Language.TELUGU),
    (_KANNADA, Script.KANNADA, Language.KANNADA),
    (_BENGALI, Script.BENGALI, Language.BENGALI),
)

_WORD = re.compile(r"[a-z\u0900-\u097F]+")

# ---------------------------------------------------------------------------
# Romanised Hindi markers
# ---------------------------------------------------------------------------

#: Function words and verb forms. Spelling in Roman Hindi is unstandardised —
#: "hai"/"hain", "kya"/"kyaa", "nahi"/"nahin"/"nahee" — so variants are
#: enumerated rather than stemmed. A stemmer tuned for English mangles these.
#:
#: Deliberately EXCLUDES words that are also ordinary English: "he", "to",
#: "so", "me", "is", "hi", "do", "in", "at", "ho". Including them made
#: "How much is the revenue" score as Hinglish, which is the failure mode that
#: matters most — an English speaker must never be answered in Hinglish.
_HINDI_MARKERS: frozenset[str] = frozenset("""
kya kyaa kyu kyun kyon kaise kaisa kaisi kaisey kitna kitni kitne
hai hain hota hoti hote hona huaa hua hui hue tha thi the thay
nahi nahin nahee mat bina
mera meri mere tera teri tumhara aapka apna apne apni
uska uski unka unke iska iski inka inke
mein mai maine hum hamara humara tum aap unhone
aur lekin magar kyunki isliye taki agar toh phir bhi
acha accha achha achhi achha behtar sahi galat theek thik
zyada jyada kam thoda thodi bahut bohot bahot
batao bataye bataiye batana samjhao samjha dekho dekhiye
chahiye chaahiye sakta sakti sakte paye paayi
raha rahi rahe rakha rakhi
karna karta karti karte kiya kare karo kijiye
lagta lagti lagte laga lagi
milta milti mile mila
jaisa jaise jitna wala wali wale
konsa kaunsa konsi kaunsi kahan kahaan kab kaun
paisa paise kamai kamayi munafa nuksan
""".split())

#: Strong markers: their presence is close to decisive because they have no
#: English homograph and appear in almost every Hindi question. Weighted
#: higher than the general list.
_HINDI_STRONG: frozenset[str] = frozenset("""
kya kyaa kaise kaisa kaisi kitna kitni kitne hai hain nahi nahin
batao bataiye samjhao kyun kyon chahiye kaunsa konsa kahan
""".split())

#: English function words. Weak evidence by design — they occur throughout
#: Hinglish too, so a high count means little on its own.
_ENGLISH_MARKERS: frozenset[str] = frozenset("""
the a an is are was were be been being am
what which who whom whose how why when where whether
does do did done doing has have had having
this that these those there here
of in on at to for from by with about into over under
and or but if then than so such because
can could should would will shall may might must
show tell give explain compare analyse analyze summarise summarize
me my your our their its it his her
good bad better worse best worst more less most least
much many any all some none
""".split())

#: Weights. Hindi markers dominate because a Hinglish sentence is mostly
#: English tokens by count; see the module docstring.
_STRONG_HINDI_WEIGHT = 3.0
_HINDI_WEIGHT = 2.0
_ENGLISH_WEIGHT = 0.6

#: Score above which romanised Hindi is accepted. One strong marker ("hai")
#: reaches 3.0 and clears it — correct, since "TCS kaisa hai" is unambiguously
#: Hinglish and is exactly the brief's example.
_HINGLISH_THRESHOLD = 2.5


@dataclass(frozen=True, slots=True)
class Detection:
    """The detected language and why.

    `reason` is carried so a misdetection can be diagnosed from a log line
    rather than reproduced by hand, and so the API can explain itself.
    """

    language: Language
    #: 0-1. Not a probability — a self-assessment used to decide whether to
    #: trust detection over a stored user preference.
    confidence: float
    script: Script
    reason: str
    #: True when the text mixed scripts or mixed vocabularies. Recorded
    #: because the brief calls out mixed language explicitly.
    is_mixed: bool = False
    hindi_score: float = 0.0
    english_score: float = 0.0
    #: Set when a script maps to several languages (Devanagari → Hindi or
    #: Marathi) and the platform cannot yet tell them apart.
    ambiguous_with: tuple[Language, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "language": self.language.value,
            "confidence": round(self.confidence, 3),
            "script": self.script.value,
            "reason": self.reason,
            "is_mixed": self.is_mixed,
            "ambiguous_with": [lang.value for lang in self.ambiguous_with],
        }


def _tokens(text: str) -> list[str]:
    """Lowercased word tokens, Latin and Devanagari.

    NFC normalisation first: Devanagari can arrive decomposed, and a
    decomposed string fails a naive range check on characters that are
    visually identical to composed ones.
    """
    return _WORD.findall(unicodedata.normalize("NFC", text or "").lower())


def detect(text: str, *, default: Language = Language.ENGLISH) -> Detection:
    """Detect the language of a user's question.

    Deterministic and cheap — pure string work, no model, no network — so it
    can run on every request including streaming ones.
    """
    raw = (text or "").strip()
    if not raw:
        return Detection(
            language=default, confidence=0.0, script=Script.LATIN,
            reason="Empty input; fell back to the default language.",
        )

    normalised = unicodedata.normalize("NFC", raw)

    # --- 1. Script is decisive -----------------------------------------
    for pattern, script, language in _SCRIPT_PROBES:
        matches = pattern.findall(normalised)
        if not matches:
            continue

        # Proportion of letters in this script, ignoring digits, spaces and
        # punctuation. A single Devanagari word inside an English sentence is
        # still a Hindi signal, but the mixed flag records it.
        letters = [c for c in normalised if c.isalpha()]
        share = len(matches) / len(letters) if letters else 1.0
        latin = sum(1 for c in letters if "a" <= c.lower() <= "z")

        ambiguous: tuple[Language, ...] = ()
        if script is Script.DEVANAGARI:
            # Devanagari is shared with Marathi. Recorded, not guessed at.
            ambiguous = (Language.MARATHI,)

        return Detection(
            language=language,
            # Script evidence is strong even at a low share, because nobody
            # types Devanagari by accident.
            confidence=min(0.99, 0.80 + share * 0.19),
            script=script,
            reason=(
                f"{len(matches)} {script.value} character(s) "
                f"({share:.0%} of letters) — script is decisive."
                + (" Mixed with Latin text." if latin else "")
            ),
            is_mixed=bool(latin) and share < 0.95,
            hindi_score=float(len(matches)),
            ambiguous_with=ambiguous,
        )

    # --- 2. Romanised Hindi vs English ----------------------------------
    tokens = _tokens(normalised)
    if not tokens:
        return Detection(
            language=default, confidence=0.0, script=Script.LATIN,
            reason="No word characters found; fell back to the default.",
        )

    strong = [t for t in tokens if t in _HINDI_STRONG]
    hindi = [t for t in tokens if t in _HINDI_MARKERS and t not in _HINDI_STRONG]
    english = [t for t in tokens if t in _ENGLISH_MARKERS]

    hindi_score = len(strong) * _STRONG_HINDI_WEIGHT + len(hindi) * _HINDI_WEIGHT
    english_score = len(english) * _ENGLISH_WEIGHT

    if hindi_score >= _HINGLISH_THRESHOLD:
        markers = strong + hindi
        # Confidence rises with the count but saturates: five Hindi markers is
        # not meaningfully more certain than four.
        confidence = min(0.96, 0.62 + 0.10 * len(markers))
        return Detection(
            language=Language.HINGLISH,
            confidence=confidence,
            script=Script.LATIN,
            reason=(
                f"Romanised Hindi markers in Latin script "
                f"({', '.join(markers[:5])})"
                + (f" alongside {len(english)} English function word(s)"
                   if english else "")
                + " — Hinglish."
            ),
            is_mixed=bool(english),
            hindi_score=hindi_score,
            english_score=english_score,
        )

    if english_score > 0:
        return Detection(
            language=Language.ENGLISH,
            confidence=min(0.95, 0.60 + 0.08 * len(english)),
            script=Script.LATIN,
            reason=(
                f"{len(english)} English function word(s) and no romanised "
                "Hindi grammar — English."
            ),
            is_mixed=False,
            hindi_score=hindi_score,
            english_score=english_score,
        )

    # --- 3. Latin script, no function words either way -------------------
    #
    # "TCS revenue" or "Reliance debt". Financial vocabulary is English in
    # every Indian language, so English is the right answer, but confidence is
    # low and stated as such — this is precisely the case where a stored user
    # preference should win over detection.
    return Detection(
        language=default,
        confidence=0.35,
        script=Script.LATIN,
        reason=(
            "Latin script with no grammatical markers in either language — "
            "too short to classify, defaulted. A saved preference should "
            "take precedence here."
        ),
        hindi_score=hindi_score,
        english_score=english_score,
    )


#: Below this, detection is treated as a guess and a stored preference wins.
#: Set just above the 0.35 assigned to the no-marker case, so bare keyword
#: queries defer to preference while a single clear marker does not.
LOW_CONFIDENCE = 0.50


def choose_language(
    text: str,
    *,
    requested: Language | None = None,
    preference: Language | None = None,
) -> tuple[Language, Detection]:
    """Resolve the response language from all three inputs.

    Precedence, highest first:

    1. **An explicit request.** `language=hindi` is an instruction, not a hint.
    2. **Confident detection.** Someone who types Devanagari wants Devanagari,
       whatever they saved last week.
    3. **A stored preference.** Used when detection is a guess — a bare
       "TCS revenue" from a Hindi-preferring user should come back in Hindi.
    4. **Detection anyway**, as the final fallback.

    Detection always runs and is always returned, even when overridden, so the
    response can report what the user appeared to write as well as what it
    answered in.
    """
    detection = detect(text)

    if requested is not None:
        return requested, detection
    if detection.confidence >= LOW_CONFIDENCE:
        return detection.language, detection
    if preference is not None:
        return preference, detection
    return detection.language, detection
