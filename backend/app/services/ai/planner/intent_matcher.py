"""Multi-intent matching for planning.

The counterpart to :class:`FinancialIntentResolver`, and deliberately a
separate object. The resolver answers "may I execute exactly one of these?",
where returning ``None`` is the safe answer and multi-intent questions fall
through to the provider. The planner answers "what is this question about?",
where dropping the second intent would defeat the entire exercise. Same
vocabulary, different question, different contract.

Matching proceeds in four stages, each of which exists because of a
specific observed failure:

1. **Negative evidence.** Some wording cancels an intent outright. The Hindi
   postposition "pe" fires the P/E pattern; "sell software" fires the
   recommendation pattern. Suppressing them requires reading the *raw*
   question, because normalisation rewrites "pe kya" into "pe what" and the
   postposition stops being visible.

2. **Earliest match per intent.** One intent produces at most one match, so
   an intent that appears three times in a question is still one thing the
   question asks for.

3. **Overlap resolution.** Two patterns frequently match the same words:
   "debt risk" matches both DEBT and FINANCIAL_RISK, and "revenue growth"
   matches both REVENUE_GROWTH and the generic GROWTH_QUALITY. The longer,
   higher-precedence span absorbs the shorter one it contains. This is what
   keeps "debt risk" from planning as two intents when the user asked one
   question.

4. **Specificity overrides**, applied last as a second belt. The canonical
   table in ``financial_intent.py`` names the same general/specific pairs
   the resolver uses, so the planner reaches the same conclusion by the same
   published rule rather than by a planner-private opinion.

The result is ordered by first occurrence, because the order a user listed
things in is part of what they asked.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.services.ai.financial_intent import (
    SPECIFICITY_OVERRIDES, FinancialIntent,
)

from .types import IntentFamily, IntentMatch
from .vocabulary import INTENT_VOCABULARY, compiled, negative_patterns


@dataclass(frozen=True, slots=True)
class _Candidate:
    """A match before ordering, with the span that produced it."""

    intent: FinancialIntent
    matched_on: str
    start: int
    end: int
    precedence: int
    family: IntentFamily

    @property
    def length(self) -> int:
        return self.end - self.start

    def contains(self, other: "_Candidate") -> bool:
        return self.start <= other.start and self.end >= other.end


class IntentMatcher:
    """Finds every intent a question asks for, in the order asked.

    Planning only. Nothing here decides whether an intent will be answered,
    and nothing here is consulted by the deterministic execution path —
    that path keeps using ``FinancialIntentResolver``, unchanged.
    """

    def match(
        self,
        text: str,
        *,
        raw_text: str | None = None,
    ) -> tuple[IntentMatch, ...]:
        """Return the ordered intents present in `text`.

        `text` is the scrutiny text — the raw question on the first pass,
        the normalised question on the fallback pass. `raw_text` is the
        question exactly as the user typed it, and is what negative
        evidence is tested against; it defaults to `text` for callers that
        have only one string.
        """
        if not text or not text.strip():
            return ()

        raw = raw_text if raw_text is not None else text
        lowered = text.lower()

        candidates: list[_Candidate] = []
        for intent, spec in INTENT_VOCABULARY.items():
            if self._is_suppressed(intent, raw):
                continue
            found = self._earliest(spec.rules(), lowered)
            if found is None:
                continue
            start, end, rule = found
            candidates.append(_Candidate(
                intent=intent, matched_on=rule, start=start, end=end,
                precedence=spec.precedence, family=spec.family,
            ))

        kept = self._resolve_overlaps(candidates)
        kept = self._apply_specificity(kept)
        kept.sort(key=lambda c: (c.start, -c.precedence, c.intent.value))

        return tuple(
            IntentMatch(
                intent=c.intent, matched_on=c.matched_on, position=c.start,
                family=c.family, precedence=c.precedence,
            )
            for c in kept
        )

    # ------------------------------------------------------------- stages
    @staticmethod
    def _is_suppressed(intent: FinancialIntent, raw_text: str) -> bool:
        """True when negative evidence cancels this intent.

        Tested against the raw question only. The normalised form has
        already discarded the Hindi function words that identify a
        postposition, so it is precisely the wrong place to look.
        """
        if not raw_text:
            return False
        return any(p.search(raw_text) for p in negative_patterns(intent))

    @staticmethod
    def _earliest(
        rules: tuple[tuple[str, bool], ...], lowered: str,
    ) -> tuple[int, int, str] | None:
        """The leftmost span produced by any rule, with the rule that made it.

        Rules are ordered canonical-first, so when several fire the
        canonical pattern is the one reported as the reason.
        """
        best: tuple[int, int, str] | None = None
        for rule, is_regex in rules:
            if is_regex:
                found = compiled(rule).search(lowered)
                if found is None:
                    continue
                start, end = found.start(), found.end()
            else:
                start = lowered.find(rule.lower())
                if start < 0:
                    continue
                end = start + len(rule)
            if best is None or start < best[0]:
                best = (start, end, rule)
        return best

    @staticmethod
    def _resolve_overlaps(candidates: list[_Candidate]) -> list[_Candidate]:
        """Drop a match that is contained in a stronger one.

        Sorted longest-and-highest-precedence first, then kept unless an
        already-kept match swallows it. "debt risk" therefore survives as
        FINANCIAL_RISK and absorbs the DEBT match inside its span, and
        "revenue growth" absorbs the generic growth match, rather than the
        plan reporting two intents for one question.
        """
        ordered = sorted(
            candidates, key=lambda c: (-c.length, -c.precedence, c.start),
        )
        kept: list[_Candidate] = []
        for candidate in ordered:
            if any(k.contains(candidate) for k in kept):
                continue
            kept.append(candidate)
        return kept

    @staticmethod
    def _apply_specificity(candidates: list[_Candidate]) -> list[_Candidate]:
        """The canonical general/specific table, as a second belt.

        Span containment already handles the common cases. This applies the
        resolver's published ``SPECIFICITY_OVERRIDES`` on top so the planner
        cannot disagree with the resolver about which of the two was meant.
        """
        intents = {c.intent for c in candidates}
        demoted: set[FinancialIntent] = set()
        for general, specifics in SPECIFICITY_OVERRIDES.items():
            if general in intents and any(s in intents for s in specifics):
                demoted.add(general)
        return [c for c in candidates if c.intent not in demoted]
