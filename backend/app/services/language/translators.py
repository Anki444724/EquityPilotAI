"""Translation providers behind one interface.

The brief's architectural requirement is that adding a language needs "only a
translation module". This file is where that promise is kept: a
:class:`Translator` implementation is the entire surface area a new language
touches. Retrieval, RAG, scoring, the vault, temporal memory and the scheduler
never learn that another language exists.

Three implementations ship.

:class:`PassthroughTranslator` — returns English unchanged. Used when the target
*is* English, and as the honest fallback when nothing else is reachable. It
never pretends: :attr:`TranslationResult.translated` stays ``False``, and the
adapter surfaces that to the caller rather than presenting English prose as
though it were Hindi.

:class:`LLMTranslator` — the production path. Reuses the existing
:class:`ProviderRouter`, so it inherits the routing, caching, fallback and cost
accounting the AI layer already has, and needs no new credentials, no new
rate-limit budget and no new failure mode.

:class:`GlossaryTranslator` — a deterministic, offline renderer. It cannot
produce fluent prose and does not claim to; it applies the glossary and a small
set of structural rules so the platform degrades to *comprehensible* rather than
to English when no model is reachable. This matters because the OpenRouter free
tier is 50 requests a day and is routinely exhausted.

**Nothing here writes to the database.** A translation is computed per response
and discarded. There is no translated-content table, no Hindi chunk, no second
embedding — which is the brief's central prohibition and the reason this layer
is stateless by construction.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

import structlog

from app.domain.ai.types import (
    CompletionRequest, Message, NoProviderConfigured, ProviderError, Role,
)
from app.domain.language.glossary import render_for_prompt
from app.domain.language.protect import (
    Protection, protect, restore, verify_preserved,
)
from app.domain.language.types import Language, spec_for

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class TranslationResult:
    """The outcome of one translation attempt.

    ``translated`` is the field that matters. A caller must be able to tell
    the difference between "this is Hindi" and "this is English because Hindi
    was unavailable", and a bare string cannot express that.
    """

    text: str
    language: Language
    translated: bool
    provider: str
    #: Why, when `translated` is False. Surfaced to the API caller.
    detail: str = ""
    latency_ms: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    #: Protected spans that did not survive. Non-empty means the translation
    #: was rejected and the English text returned instead.
    integrity_problems: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "language": self.language.value,
            "translated": self.translated,
            "provider": self.provider,
            "detail": self.detail,
            "latency_ms": round(self.latency_ms, 1),
            "cost_usd": round(self.cost_usd, 6),
            "integrity_problems": self.integrity_problems,
        }


class Translator(Protocol):
    """The whole interface a new language has to satisfy."""

    name: str

    def supports(self, language: Language) -> bool: ...

    async def translate(
        self, text: str, language: Language, *, entities: list[str] = ...,
    ) -> TranslationResult: ...


# ---------------------------------------------------------------------------
# Passthrough
# ---------------------------------------------------------------------------

class PassthroughTranslator:
    """Returns the English text unchanged, and says so."""

    name = "passthrough"

    def supports(self, language: Language) -> bool:
        return language is Language.ENGLISH

    async def translate(
        self, text: str, language: Language, *, entities: list[str] | None = None,
    ) -> TranslationResult:
        return TranslationResult(
            text=text, language=Language.ENGLISH,
            translated=language is Language.ENGLISH,
            provider=self.name,
            detail=(
                "" if language is Language.ENGLISH else
                f"No translator is available for {language.value}; the "
                "response is in English."
            ),
        )


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------

#: Instruction given to the writing model. The sentinel rule is stated three
#: times in three different ways because it is the one instruction whose
#: violation silently corrupts the evidence chain, and models comply with
#: repeated, concrete, example-bearing instructions far more reliably than
#: with a single polite request.
_SYSTEM = """You are a professional financial translator for an institutional \
equity research platform. You render English research prose into {label} \
({native}).

{style}

ABSOLUTE RULES:

1. The text contains placeholder tokens of the form §0§, §1§, §2§ and so on. \
These stand for company names, tickers, ISINs, numbers, percentages, fiscal \
years, document titles and citation markers. You MUST reproduce every single \
one EXACTLY as it appears, in the same order, unchanged. Do not translate \
them. Do not renumber them. Do not add spaces inside them. Do not invent new \
ones. If the input has §7§, your output must contain §7§.

2. Translate ONLY the explanatory prose around those tokens.

3. Preserve the markdown structure exactly: headings, bullet points, bold \
markers, blockquotes, horizontal rules and line breaks.

4. Do not add commentary, do not summarise, do not omit anything, and do not \
answer the content. You are translating, not analysing.

5. Return ONLY the translated text. No preamble such as "Here is the \
translation".

{glossary}"""


class LLMTranslator:
    """Translates through the platform's existing provider router."""

    name = "llm"

    #: Above this, the text is split on paragraph boundaries. A long research
    #: report in one request risks truncation, and a truncated translation
    #: loses citations from the tail — which is the failure the protection
    #: layer exists to prevent.
    MAX_CHARS = 6000

    def __init__(self, router: Any | None = None, *,
                 temperature: float = 0.15) -> None:
        # Imported lazily so the language layer does not drag the whole AI
        # provider stack into modules that only need detection.
        if router is None:
            from app.services.ai.providers.router import ProviderRouter
            router = ProviderRouter()
        self.router = router
        self.temperature = temperature

    def supports(self, language: Language) -> bool:
        # Any language with a spec. The model does the work; the platform only
        # supplies the instruction and the glossary. This is precisely the
        # property that makes a new language a configuration change.
        return True

    async def translate(
        self, text: str, language: Language, *, entities: list[str] | None = None,
    ) -> TranslationResult:
        if language is Language.ENGLISH or not (text or "").strip():
            return TranslationResult(
                text=text, language=Language.ENGLISH, translated=True,
                provider=self.name,
            )

        started = time.perf_counter()

        # TRANS-001. Observed in production: with the LLM quota exhausted the
        # router falls through to the OFFLINE provider, which is a
        # template composer, not a model. Handed masked text it returns
        # unrelated boilerplate, so all 30 sentinels vanish, the integrity
        # check correctly rejects the result, and the user gets pure English
        # after paying for a round trip that could never have succeeded.
        #
        # Detecting this up front costs nothing and lets the glossary
        # translator produce partially-rendered Hindi instead — which is
        # strictly more useful to someone who asked in Hindi, and is labelled
        # as partial rather than presented as a translation.
        if self._offline_only():
            log.info("no live model for translation; using the glossary",
                     language=language.value)
            result = await GlossaryTranslator().translate(
                text, language, entities=entities,
            )
            result.detail = (
                "No live language model was reachable (the configured "
                "provider is the offline composer, which cannot translate). "
                + result.detail
            )
            return result

        spec = spec_for(language)
        protection = protect(text, extra_terms=entities or [])

        system = _SYSTEM.format(
            label=spec.label, native=spec.native_label,
            style=spec.style_instruction,
            glossary=render_for_prompt(language, text=text),
        )

        chunks = self._split(protection.masked)
        pieces: list[str] = []
        prompt_tokens = completion_tokens = 0
        cost = 0.0

        try:
            for chunk in chunks:
                request = CompletionRequest(
                    messages=[
                        Message(Role.SYSTEM, system),
                        Message(Role.USER, chunk),
                    ],
                    temperature=self.temperature,
                    # Generous headroom: Devanagari costs materially more
                    # tokens per character than Latin, and a translation
                    # truncated mid-sentence loses the citations after it.
                    max_tokens=min(4000, max(700, int(len(chunk) / 1.4))),
                )
                response = await self.router.complete(request)

                # TRANS-001 (second half). Checking the configured chain up
                # front is not enough: production has a live provider
                # CONFIGURED but exhausted, so the chain looks healthy and the
                # router falls through to the offline composer at request
                # time. The only reliable signal is which provider actually
                # served the response, and that is only knowable afterwards.
                #
                # Abandon immediately rather than translating the remaining
                # chunks against a composer that cannot translate any of them.
                if self._is_offline(response):
                    log.info("router fell through to the offline composer; "
                             "using the glossary instead",
                             language=language.value)
                    result = await GlossaryTranslator().translate(
                        text, language, entities=entities,
                    )
                    result.detail = (
                        "The configured language model was unavailable and "
                        "the request fell through to the offline composer, "
                        "which cannot translate. " + result.detail
                    )
                    result.latency_ms = (time.perf_counter() - started) * 1000
                    return result

                pieces.append((response.content or "").strip())
                usage = getattr(response, "usage", None)
                prompt_tokens += getattr(usage, "prompt_tokens", 0) or 0
                completion_tokens += getattr(usage, "completion_tokens", 0) or 0
                cost += getattr(response, "cost_usd", 0.0) or 0.0

        except (NoProviderConfigured, ProviderError) as exc:
            return TranslationResult(
                text=text, language=Language.ENGLISH, translated=False,
                provider=self.name,
                detail=f"Translation provider unavailable: {exc}",
                latency_ms=(time.perf_counter() - started) * 1000,
            )
        except Exception as exc:  # noqa: BLE001 — a translation failure must
            # never lose the analysis it was translating
            log.exception("translation failed", language=language.value)
            return TranslationResult(
                text=text, language=Language.ENGLISH, translated=False,
                provider=self.name,
                detail=f"Translation failed: {type(exc).__name__}",
                latency_ms=(time.perf_counter() - started) * 1000,
            )

        restoration = restore("\n\n".join(pieces), protection)
        elapsed = (time.perf_counter() - started) * 1000

        problems: list[str] = []
        if restoration.lost:
            kinds = {span.kind for span in restoration.lost}
            problems.append(
                f"{len(restoration.lost)} protected token(s) "
                f"({', '.join(sorted(kinds))}) were dropped by the model."
            )
        if restoration.spurious:
            problems.append(
                f"{len(restoration.spurious)} placeholder(s) were invented."
            )
        problems.extend(verify_preserved(text, restoration.text))

        if problems:
            # Fail closed. Prose with a missing citation marker looks correct
            # and is not: the audit trail the platform sells is broken, and
            # nothing downstream would notice. English with intact citations
            # is strictly better than Hindi without them.
            log.warning("translation rejected on integrity grounds",
                        language=language.value, problems=problems)
            return TranslationResult(
                text=text, language=Language.ENGLISH, translated=False,
                provider=self.name,
                detail=(
                    "Translation was discarded because protected content "
                    "(citations, figures or identifiers) did not survive it. "
                    "The English original is returned so the evidence chain "
                    "stays intact."
                ),
                latency_ms=elapsed, integrity_problems=problems,
                prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                cost_usd=cost,
            )

        return TranslationResult(
            text=restoration.text, language=language, translated=True,
            provider=self.name, latency_ms=elapsed,
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
            cost_usd=cost,
        )

    @staticmethod
    def _is_offline(response: Any) -> bool:
        """Did the offline composer actually serve this response?

        Matched on the provider name the response reports, which is the one
        piece of evidence that cannot be wrong — the configured chain can look
        healthy while the live provider is rate-limited and the router quietly
        falls back.
        """
        name = str(getattr(response, "provider", "") or "").strip().lower()
        return name in {"offline", "mock"}

    def _offline_only(self) -> bool:
        """True when the only reachable provider cannot actually translate.

        Deliberately conservative: any doubt resolves to False and the normal
        path runs, because wrongly skipping a live model would silently
        downgrade every translation. Router internals are read defensively —
        a private attribute that disappears must degrade to "try anyway",
        never to an exception on the response path.
        """
        try:
            chain = getattr(self.router, "chain", None)
            if callable(chain):
                chain = chain()
            if not chain:
                return False
            names = [
                str(getattr(c, "payload_shape", "")
                    or getattr(c, "name", "")).lower()
                for c in chain
            ]
            return bool(names) and all(
                "offline" in n or "mock" in n for n in names
            )
        except Exception:  # noqa: BLE001
            return False

    def _split(self, text: str) -> list[str]:
        """Split on blank lines, never mid-paragraph."""
        if len(text) <= self.MAX_CHARS:
            return [text]

        chunks: list[str] = []
        current = ""
        for paragraph in text.split("\n\n"):
            candidate = f"{current}\n\n{paragraph}" if current else paragraph
            if len(candidate) > self.MAX_CHARS and current:
                chunks.append(current)
                current = paragraph
            else:
                current = candidate
        if current:
            chunks.append(current)
        return chunks


# ---------------------------------------------------------------------------
# Glossary — deterministic offline fallback
# ---------------------------------------------------------------------------

#: Structural phrases worth rendering without a model. Not a translation
#: system: enough to make an English answer navigable to a Hindi reader when
#: no provider is reachable, and honest about being partial.
_HINDI_PHRASES: tuple[tuple[str, str], ...] = (
    ("Strengths:", "मज़बूतियाँ:"),
    ("Weaknesses:", "कमज़ोरियाँ:"),
    ("Risks:", "जोखिम:"),
    ("Summary:", "सारांश:"),
    ("Conclusion:", "निष्कर्ष:"),
    ("Outlook:", "दृष्टिकोण:"),
    ("Recommendation:", "अनुशंसा:"),
    ("Key points:", "मुख्य बिंदु:"),
    ("Not assessed:", "मूल्यांकन नहीं किया गया:"),
)


class GlossaryTranslator:
    """Deterministic glossary substitution. Comprehensible, not fluent.

    Exists because the platform's LLM budget is a hard 50 requests a day on
    the free tier and is routinely exhausted. When that happens the choice is
    between English prose and partially-rendered Hindi, and a reader who asked
    in Hindi is better served by the latter — provided it is labelled as such,
    which it is.
    """

    name = "glossary"

    def supports(self, language: Language) -> bool:
        return language in {Language.ENGLISH, Language.HINDI, Language.HINGLISH}

    async def translate(
        self, text: str, language: Language, *, entities: list[str] | None = None,
    ) -> TranslationResult:
        if language is Language.ENGLISH:
            return TranslationResult(text=text, language=language,
                                     translated=True, provider=self.name)

        if language is Language.HINGLISH:
            # Hinglish keeps English financial vocabulary, so a glossary pass
            # would change almost nothing. Returning the English text and
            # declaring it untranslated is the honest outcome.
            return TranslationResult(
                text=text, language=Language.ENGLISH, translated=False,
                provider=self.name,
                detail=("Hinglish requires a language model; no provider was "
                        "reachable, so the English response is returned."),
            )

        started = time.perf_counter()
        protection = protect(text, extra_terms=entities or [])
        working = protection.masked

        from app.domain.language.glossary import TERMS

        substitutions = 0
        for phrase, rendered in _HINDI_PHRASES:
            if phrase in working:
                working = working.replace(phrase, rendered)
                substitutions += 1

        # Longest first, so "Free Cash Flow" is matched before "Cash Flow".
        for term in sorted(TERMS, key=lambda t: -len(t.english)):
            target = term.render(language)
            if target.lower() == term.english.lower():
                continue
            pattern = re.compile(rf"(?<!\w){re.escape(term.english)}(?!\w)",
                                 re.IGNORECASE)
            working, count = pattern.subn(target, working)
            substitutions += count

        restoration = restore(working, protection)
        return TranslationResult(
            text=restoration.text, language=language,
            # Deliberately False: this is terminology substitution, not
            # translation, and claiming otherwise would misrepresent it.
            translated=False,
            provider=self.name,
            detail=(
                f"No language model was reachable. {substitutions} financial "
                "term(s) were rendered from the glossary; the surrounding "
                "prose remains in English."
            ),
            latency_ms=(time.perf_counter() - started) * 1000,
        )


def build_translator(settings: Any | None = None) -> Translator:
    """Select a translator from configuration.

    Environment-driven, so switching provider is a Railway variable rather
    than a deploy — the same pattern the embedding and rerank layers use.
    """
    if settings is None:
        from app.core.config import settings as app_settings
        settings = app_settings

    choice = (getattr(settings, "TRANSLATION_PROVIDER", None) or "llm").lower()
    if choice in {"none", "off", "disabled", "passthrough"}:
        return PassthroughTranslator()
    if choice == "glossary":
        return GlossaryTranslator()
    return LLMTranslator()
