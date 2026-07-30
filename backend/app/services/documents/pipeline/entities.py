"""Entity extraction.

No NER model ships with this platform, and pretending otherwise would be the
wrong trade: a general-purpose model trained on news text mislabels Indian
corporate prose badly, and a fine-tuned one is not something a deterministic
test suite can pin down.

What is used instead is *cue-phrase extraction* — patterns built around the
formulaic language that regulated filings are obliged to use. "wholly-owned
subsidiary", "Independent Director", "the Company has entered into" are not
stylistic choices; they are near-mandatory constructions, which makes them
reliable anchors.

The honest limitation, stated here and in the docs: recall is bounded by the
cue list. Precision is high, recall is partial, and every entity carries a
confidence and the sentence it came from so a human can check it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from app.domain.documents.types import (
    EntityKind, ExtractedEntity, ParsedDocument, normalise_entity,
    normalise_whitespace,
)

# A proper-noun run: capitalised words, optionally joined by lowercase glue
# ("Bharat Consumer Products", "Tata Sons and Company").
#
# The (?-i:...) scope is essential and was a real defect when it was missing.
# Several rules below compile with re.I so their cue words match any casing —
# but re.I also makes [A-Z] match lowercase, which destroys the proper-noun
# anchor entirely. The extractor then captured "Limited in our core categories.
# Bharat Nutrition Private Limited" as a subsidiary name, having matched
# backwards through a sentence boundary. Scoping case-sensitivity to this
# fragment keeps the cue words case-insensitive and the names anchored.
_PROPER = r"(?-i:[A-Z][\w&.'-]*(?:\s+(?:of|and|&|de|van|the)\s+[A-Z][\w&.'-]*|\s+[A-Z][\w&.'-]*){0,6})"
#: Legal forms that confirm a token run is a company rather than a heading.
_LEGAL = r"(?-i:(?:Limited|Ltd\.?|Pvt\.?\s*Ltd\.?|Private\s+Limited|LLP|Inc\.?|PLC|GmbH|B\.?V\.?|Pte\.?\s*Ltd\.?))"

#: Sentence terminators, decimal-aware — the same lesson as Module 6's citation
#: auditor, where splitting on every full stop tore "33,543.00" in half.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'(])")

_STOPWORD_NAMES = {
    "the company", "the group", "the board", "the committee", "the management",
    "our company", "the corporation", "annual report", "financial statements",
    "balance sheet", "the year", "india", "the parent", "the holding company",
}

#: Single capitalised words that are ordinary vocabulary rather than names.
#: A cue pattern can capture the word that *precedes* it when that word starts
#: a sentence — "Key customers include ..." yielded an entity called "Key",
#: and a transcript's "Managing Director: Thank you." yielded "Thank". The
#: patterns above were tightened to stop producing these, and this list is the
#: second line of defence, because cue-phrase extraction will always throw up
#: a new variant of the same mistake.
_COMMON_WORDS = frozenset("""
    key major principal largest top main other others new some many few
    thank thanks yes no palm total net gross our their its the this that
    these those first second third final overall further however moreover
    revenue profit loss margin growth demand supply price prices cost costs
    management board company group business market markets year quarter
    during under above below within about approximately including include
    """.split())


@dataclass(frozen=True, slots=True)
class EntityRule:
    """One cue pattern producing entities of a given kind."""

    kind: EntityKind
    pattern: re.Pattern[str]
    #: Regex group holding the entity name.
    group: int = 1
    base_confidence: float = 0.6
    #: When set, the captured text is the whole finding (a risk, a guidance
    #: statement) rather than a name to be normalised as a proper noun.
    is_phrase: bool = False


ENTITY_RULES: tuple[EntityRule, ...] = (
    # --- corporate structure ----------------------------------------
    EntityRule(
        EntityKind.SUBSIDIARY,
        re.compile(
            rf"({_PROPER}\s+{_LEGAL})[^.]{{0,60}}?\b(?:is|,)?\s*a?\s*"
            r"(?:wholly[- ]owned|material|step[- ]down|direct|indirect)?\s*subsidiar",
            re.I | re.M,
        ),
        base_confidence=0.82,
    ),
    EntityRule(
        EntityKind.SUBSIDIARY,
        re.compile(
            rf"subsidiar(?:y|ies)[^.]{{0,40}}?[,:]\s*({_PROPER}\s+{_LEGAL})",
            re.I,
        ),
        base_confidence=0.7,
    ),
    EntityRule(
        EntityKind.PROMOTER,
        re.compile(
            rf"(?:promoter|promoter\s+group|promoters?)\s*(?:group)?\s*"
            rf"(?:comprises|includes|consists\s+of|is|are|:)\s+({_PROPER})",
            re.I,
        ),
        base_confidence=0.72,
    ),
    EntityRule(
        EntityKind.DIRECTOR,
        re.compile(
            rf"\b(?:Mr\.?|Ms\.?|Mrs\.?|Dr\.?|Shri|Smt\.?)\s+({_PROPER})[^.]{{0,50}}?"
            r"\b(?:Independent\s+Director|Non-?Executive\s+Director|Managing\s+Director|"
            r"Whole[- ]time\s+Director|Executive\s+Director|Chairman|Chairperson|"
            r"Chief\s+Executive\s+Officer|Chief\s+Financial\s+Officer|CEO|CFO)\b",
        ),
        base_confidence=0.8,
    ),
    EntityRule(
        EntityKind.DIRECTOR,
        # "Kavita Raman, Managing Director:" — the name *precedes* the role.
        #
        # The mirror-image rule ("Managing Director: Kavita Raman") was tried
        # first and had to be removed. In a conference-call transcript the text
        # after a role and a colon is the person's *speech*, not their name, so
        # it extracted "Palm" from "Chief Financial Officer: Palm oil has been
        # the principal pressure point" and "Thank" from "Managing Director:
        # Thank you." Both were then rendered on the knowledge graph as
        # directors of the company.
        re.compile(
            rf"(?:Mr\.?|Ms\.?|Mrs\.?|Dr\.?|Shri|Smt\.?)?\s*({_PROPER})\s*,\s*"
            r"(?:Independent\s+Director|Non-?Executive\s+Director|Managing\s+Director|"
            r"Whole[- ]time\s+Director|Executive\s+Director|Chairman|Chairperson|"
            r"Chief\s+Executive\s+Officer|Chief\s+Financial\s+Officer|CEO|CFO)\b",
        ),
        base_confidence=0.72,
    ),
    EntityRule(
        EntityKind.AUDITOR,
        re.compile(
            rf"(?:statutory\s+auditors?|auditors?)\s*(?:of\s+the\s+Company\s*)?"
            rf"(?:,|is|are|:)\s*(?:M/s\.?\s*)?({_PROPER}(?:\s*&\s*(?:Co\.?|Associates|LLP))?)",
            re.I,
        ),
        base_confidence=0.68,
    ),
    # --- commercial relationships -----------------------------------
    EntityRule(
        EntityKind.COMPETITOR,
        re.compile(
            rf"(?:compet(?:e|es|itors?|ing)\s+(?:with|against)|principal\s+competitors?\s*"
            rf"(?:are|include|:))\s+({_PROPER})",
            re.I,
        ),
        base_confidence=0.65,
    ),
    EntityRule(
        EntityKind.CUSTOMER,
        re.compile(
            rf"\bcustomers?\s+(?:are|include|comprise)\s+({_PROPER})",
            re.I,
        ),
        base_confidence=0.66,
    ),
    EntityRule(
        EntityKind.SUPPLIER,
        re.compile(
            rf"\bsuppliers?\s+(?:are|include|comprise)\s+({_PROPER})",
            re.I,
        ),
        base_confidence=0.66,
    ),
    EntityRule(
        EntityKind.ACQUISITION,
        re.compile(
            rf"(?:acquired|acquisition\s+of|completed\s+the\s+acquisition\s+of|"
            rf"agreed\s+to\s+acquire)\s+(?:a\s+\d+(?:\.\d+)?%\s+stake\s+in\s+)?"
            rf"({_PROPER}(?:\s+{_LEGAL})?)",
            re.I,
        ),
        base_confidence=0.7,
    ),
    # --- business shape ---------------------------------------------
    EntityRule(
        EntityKind.SEGMENT,
        re.compile(
            rf"(?:business\s+segments?|reportable\s+segments?|operating\s+segments?)\s*"
            rf"(?:are|comprise|include|:)\s+({_PROPER}(?:\s*,\s*{_PROPER})*)",
            re.I,
        ),
        base_confidence=0.68,
    ),
    EntityRule(
        EntityKind.PRODUCT,
        re.compile(
            rf"(?:brands?|products?)\s*(?:such\s+as|include|includes|comprise|:)\s+"
            rf"({_PROPER}(?:\s*,\s*{_PROPER})*)",
            re.I,
        ),
        base_confidence=0.6,
    ),
    # --- phrase findings --------------------------------------------
    EntityRule(
        EntityKind.RISK,
        re.compile(
            r"((?:volatility|fluctuation|disruption|shortage|dependence|dependency|"
            r"concentration|exposure|litigation|non-?compliance|cyber|obsolescence|"
            r"competition|slowdown|adverse)\b[^.]{10,180}\.)",
            re.I,
        ),
        base_confidence=0.55,
        is_phrase=True,
    ),
    EntityRule(
        EntityKind.GUIDANCE,
        re.compile(
            r"((?:we\s+(?:expect|anticipate|target|guide|are\s+targeting)|"
            r"guidance\s+(?:of|for)|expects?\s+to\s+(?:achieve|deliver|reach)|"
            r"outlook\s+(?:for|of)|aim\s+to\s+(?:achieve|reach))\b[^.]{10,200}\.)",
            re.I,
        ),
        base_confidence=0.62,
        is_phrase=True,
    ),
    EntityRule(
        EntityKind.CAPEX,
        re.compile(
            r"((?:capital\s+expenditure|capex|capital\s+outlay|investment)\s+"
            r"(?:of|programme|program|plan)?[^.]{0,60}"
            r"(?:₹|rs\.?|inr)\s*[\d,]+(?:\.\d+)?\s*(?:cr|crore|lakh|mn|million|bn|billion)[^.]{0,120}\.)",
            re.I,
        ),
        base_confidence=0.72,
        is_phrase=True,
    ),
    EntityRule(
        EntityKind.DEBT,
        re.compile(
            r"((?:total\s+(?:debt|borrowings)|gross\s+debt|net\s+debt|term\s+loan|"
            r"non-?convertible\s+debentures?|working\s+capital\s+facilit)"
            r"[^.]{0,60}(?:₹|rs\.?|inr)\s*[\d,]+(?:\.\d+)?[^.]{0,120}\.)",
            re.I,
        ),
        base_confidence=0.7,
        is_phrase=True,
    ),
)

#: Countries worth recognising in an Indian filing's geography disclosure.
COUNTRY_TERMS: tuple[str, ...] = (
    "India", "United States", "USA", "United Kingdom", "UK", "China", "Japan",
    "Germany", "France", "Singapore", "UAE", "United Arab Emirates", "Australia",
    "Brazil", "Canada", "Bangladesh", "Sri Lanka", "Nepal", "Indonesia",
    "Vietnam", "Thailand", "Malaysia", "Saudi Arabia", "South Africa", "Kenya",
    "Netherlands", "Switzerland", "Italy", "Spain", "Russia", "Mexico",
    "South Korea", "Taiwan", "Philippines", "Nigeria", "Egypt", "Turkey",
)
_COUNTRY_RE = re.compile(
    r"\b(" + "|".join(re.escape(c) for c in sorted(COUNTRY_TERMS, key=len, reverse=True)) + r")\b"
)


class EntityExtractor:
    """Runs the cue rules across a parsed document."""

    #: Below this an entity is dropped rather than shown as junk.
    MIN_CONFIDENCE = 0.45
    #: Cap per kind per document; annual reports repeat endlessly.
    MAX_PER_KIND = 60

    def __init__(self, company_name: str | None = None) -> None:
        self.company_name = company_name
        self._company_key = normalise_entity(company_name) if company_name else None

    def extract(self, document: ParsedDocument) -> list[ExtractedEntity]:
        found: list[ExtractedEntity] = []
        for page in document.pages:
            if not page.text.strip():
                continue
            found.extend(self._extract_page(page.text, page.number))
        if self.company_name:
            found.insert(0, self._self_entity(document))
        return self._consolidate(found)

    # ------------------------------------------------------------------
    def _extract_page(self, text: str, page: int) -> list[ExtractedEntity]:
        out: list[ExtractedEntity] = []
        for rule in ENTITY_RULES:
            for match in rule.pattern.finditer(text):
                raw = normalise_whitespace(match.group(rule.group) or "")
                if not raw:
                    continue
                if rule.is_phrase:
                    entity = self._phrase_entity(rule, raw, page, text, match)
                    if entity is not None:
                        out.append(entity)
                    continue
                for part in self._split_names(raw):
                    entity = self._name_entity(rule, part, page, text, match)
                    if entity is not None:
                        out.append(entity)
        out.extend(self._countries(text, page))
        return out

    #: Splits "A Limited and B Limited" / "A, B and C" into separate names.
    #: A cue like "customers include X and Y" captures both in one span, and
    #: storing that as a single entity produces a graph node no reader would
    #: recognise and no lookup would ever match.
    _CONJUNCTION = re.compile(r"\s*(?:,|\band\b|&)\s*", re.I)

    def _split_names(self, raw: str) -> list[str]:
        """Break a conjunction list into individual names.

        Only splits where the parts look independently substantial, so
        "Bhattacharya & Associates" and "Larsen & Toubro" survive intact —
        an ampersand inside a firm's own name is not a list separator.
        """
        parts = [p.strip() for p in self._CONJUNCTION.split(raw) if p.strip()]
        if len(parts) < 2:
            return [raw]
        # Every part must stand alone as a name, or this was not a list.
        if all(len(p) >= 6 and p[0].isupper() and " " in p for p in parts):
            return parts
        return [raw]

    def _name_entity(
        self, rule: EntityRule, raw: str, page: int, text: str, match: re.Match
    ) -> ExtractedEntity | None:
        name = self._clean_name(raw)
        if not self._plausible_name(name):
            return None
        key = normalise_entity(name)
        if self._company_key and key == self._company_key:
            # The filer naming itself is not a subsidiary or a competitor.
            return None
        return ExtractedEntity(
            kind=rule.kind,
            name=name,
            page=page,
            context=self._context(text, match),
            confidence=rule.base_confidence,
            normalised=key,
        )

    def _phrase_entity(
        self, rule: EntityRule, raw: str, page: int, text: str, match: re.Match
    ) -> ExtractedEntity | None:
        phrase = normalise_whitespace(raw)
        if len(phrase) < 25:
            return None
        return ExtractedEntity(
            kind=rule.kind,
            name=phrase[:300],
            page=page,
            context=self._context(text, match),
            confidence=rule.base_confidence,
            # Phrases are not proper nouns; a lowercase hash of the text is the
            # right identity, and normalise_entity would strip meaningful words.
            normalised=phrase.lower()[:120],
        )

    def _countries(self, text: str, page: int) -> list[ExtractedEntity]:
        seen: set[str] = set()
        out: list[ExtractedEntity] = []
        for match in _COUNTRY_RE.finditer(text):
            name = match.group(1)
            key = normalise_entity(name)
            if key in seen:
                continue
            seen.add(key)
            out.append(
                ExtractedEntity(
                    kind=EntityKind.COUNTRY,
                    name=name,
                    page=page,
                    context=self._context(text, match),
                    confidence=0.55,
                    normalised=key,
                )
            )
        return out

    def _self_entity(self, document: ParsedDocument) -> ExtractedEntity:
        return ExtractedEntity(
            kind=EntityKind.COMPANY,
            name=self.company_name or "",
            page=1,
            context="Subject company of this document.",
            confidence=1.0,
            normalised=self._company_key or "",
        )

    # ------------------------------------------------------------------
    #: Words that end a preceding sentence and are capitalised, so a greedy
    #: proper-noun run can swallow them: "…core categories. Bharat Nutrition".
    #: The section-heading terms are here because a heading carries no
    #: terminating full stop, so the sentence-boundary trim cannot see it and
    #: "Management Guidance Kavita Raman" was extracted as a director's name.
    _LEADING_NOISE = re.compile(
        r"^(?:(?:The|Our)\s+)?(?:Company|Group|Board|Limited|Ltd\.?|Committee|"
        r"Management\s+Guidance|Management\s+Discussion(?:\s+and\s+Analysis)?|"
        r"Corporate\s+Governance|Business\s+Overview|Risk\s+Factors|"
        r"Financial\s+Statements|Chairman's\s+Letter|Moderator)\b[\s.,;:—-]*",
        re.I,
    )

    @classmethod
    def _clean_name(cls, raw: str) -> str:
        name = raw.strip(" ,;:.-—–")
        name = re.sub(r"\s+", " ", name)
        # A full stop inside the capture means the run crossed a sentence
        # boundary. Keep only the fragment after the last one — that is where
        # the actual name begins.
        if ". " in name:
            name = name.rsplit(". ", 1)[1].strip()
        name = cls._LEADING_NOISE.sub("", name).strip()
        # Trim a trailing conjunction left by a greedy proper-noun run.
        name = re.sub(r"\s+(?:and|&|of|the)$", "", name, flags=re.I)
        return name.strip(" ,;:.-—–")

    @staticmethod
    def _plausible_name(name: str) -> bool:
        if not (3 <= len(name) <= 90):
            return False
        if name.lower() in _STOPWORD_NAMES:
            return False
        if not any(ch.isalpha() for ch in name):
            return False
        # A lone common word is vocabulary, not a name. Multi-word names are
        # allowed through even if they begin with one, because "Key Industries
        # Limited" is a plausible company and "Key" alone is not.
        words = name.split()
        if len(words) == 1 and words[0].lower() in _COMMON_WORDS:
            return False
        # Must begin with a capital: cue patterns are anchored on proper nouns.
        return name[0].isupper()

    @staticmethod
    def _context(text: str, match: re.Match, width: int = 200) -> str:
        """The sentence around a match — the entity's own evidence."""
        start = max(0, match.start() - width)
        end = min(len(text), match.end() + width)
        window = normalise_whitespace(text[start:end])
        sentences = _SENTENCE_SPLIT.split(window)
        target = normalise_whitespace(match.group(0))[:40]
        for sentence in sentences:
            if target and target[:25] in sentence:
                return sentence[:400]
        return window[:400]

    def _consolidate(self, entities: Iterable[ExtractedEntity]) -> list[ExtractedEntity]:
        """Deduplicate by (kind, normalised), keeping the best evidence.

        Repetition is corroboration: an entity named on eight pages is more
        likely real than one named once, so confidence rises with mentions —
        but asymptotically, and never to certainty.
        """
        best: dict[tuple[EntityKind, str], ExtractedEntity] = {}
        counts: dict[tuple[EntityKind, str], int] = {}
        for entity in entities:
            key = (entity.kind, entity.normalised)
            counts[key] = counts.get(key, 0) + 1
            current = best.get(key)
            if current is None or entity.confidence > current.confidence:
                best[key] = entity

        out: list[ExtractedEntity] = []
        for key, entity in best.items():
            mentions = counts[key]
            entity.attributes["mentions"] = str(mentions)
            entity.confidence = round(
                min(0.97, entity.confidence + 0.05 * min(4, mentions - 1)), 4
            )
            if entity.confidence >= self.MIN_CONFIDENCE:
                out.append(entity)

        out.sort(key=lambda e: (e.kind.value, -e.confidence, e.name))
        capped: list[ExtractedEntity] = []
        per_kind: dict[EntityKind, int] = {}
        for entity in out:
            count = per_kind.get(entity.kind, 0)
            if count >= self.MAX_PER_KIND:
                continue
            per_kind[entity.kind] = count + 1
            capped.append(entity)
        return capped
