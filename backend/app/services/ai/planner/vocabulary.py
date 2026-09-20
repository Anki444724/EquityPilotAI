"""The planner's intent vocabulary.

**This is not a second intent registry.** The canonical patterns live in
``app.services.ai.financial_intent`` and are read here through
:data:`INTENT_PATTERNS` — the same mapping the resolver matches on, exposed
as a read-only view. A pattern is therefore defined exactly once, and the
planner cannot drift from the engine that will execute the plan it produces.

What this module adds, on top of the canonical patterns, is what the
resolver deliberately does not have:

* **phrases / aliases** — wording the canonical regexes never covered,
  including Hindi and Hinglish. The resolver could afford to miss these
  because missing meant "fall through to the provider". The planner cannot:
  its job is to say what the question is *about*, and "financially strong"
  is plainly about financial quality.
* **negative evidence** — wording that suppresses an intent even when a
  pattern fires. This is what stops the Hindi postposition "pe" from being
  read as the P/E ratio, and "sell software" from being read as a
  recommendation.
* **precedence** — which intent wins when two matches overlap, so "debt
  risk" is a financial-risk question rather than a debt question that
  happens to mention risk.

The resolver is untouched by all three. It keeps its exactly-one rule and
its fail-closed ``None``, because that rule protects the *execution* path:
answering one intent of a two-intent question is a partial answer, and a
partial answer is a wrong answer. The planner may return several intents
precisely because it never answers — it hands the whole set upstream.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping

from app.services.ai.financial_intent import (
    INVESTMENT_INTENTS, INTENT_PATTERNS, FinancialIntent,
)

from .types import IntentFamily


@dataclass(frozen=True, slots=True)
class IntentSpec:
    """Everything the planner knows about one intent.

    ``patterns`` is DERIVED from the canonical registry in
    ``__post_init__``; it is never supplied by the caller and must never be
    re-declared here. A spec that carried its own copy of a pattern would
    be the drift this layering exists to prevent.

    ``phrases`` are regexes; ``aliases`` are literal substrings. The split
    is because Devanagari does not sit reliably on regex word boundaries —
    a matra is a combining mark, so ``\\b`` can land in the middle of what a
    reader sees as a single word.
    """

    intent: FinancialIntent
    family: IntentFamily
    #: Derived from ``INTENT_PATTERNS``. Do not pass; see above.
    patterns: tuple[str, ...] = field(default=())
    #: Additional regexes the planner recognises.
    phrases: tuple[str, ...] = field(default=())
    #: Literal case-insensitive substrings — Devanagari and romanised.
    aliases: tuple[str, ...] = field(default=())
    #: Regexes that SUPPRESS this intent when they match the raw question.
    negative: tuple[str, ...] = field(default=())
    #: Higher wins when two matches overlap. Generic intents score low.
    precedence: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "patterns", tuple(INTENT_PATTERNS.get(self.intent, ())),
        )

    def rules(self) -> tuple[tuple[str, bool], ...]:
        """Every matching rule as ``(rule, is_regex)``.

        Canonical patterns first, so that when an intent matches on both a
        canonical pattern and a planner phrase the audit reports the
        canonical reason.
        """
        return (
            tuple((p, True) for p in self.patterns)
            + tuple((p, True) for p in self.phrases)
            + tuple((a, False) for a in self.aliases)
        )


# ---------------------------------------------------------------------------
# Negative evidence
#
# Every entry exists because of a real false positive, observed in this
# repository. They are matched against the RAW question, never the
# normalised text: normalisation is what turns "pe kya" into "pe what", and
# the Hindi postposition is only visible before that rewrite.
# ---------------------------------------------------------------------------

#: "Reliance pe kya bolte ho?" — "pe" is the Hindi postposition (on/about),
#: not the price-to-earnings ratio. The tell is a Hindi interrogative or
# verb immediately after it, which "P/E zyada hai kya?" never has.
_PE_AS_POSTPOSITION = (
    r"\bpe\s+(?:kya|kyun|kyon|kaun|kaise|kaisa|kaisi|kab|kahan|kaunsa|konsa)\b",
    r"\bpe\s+\w*(?:te|ta|ti|na|ne|nge|ngi|oge|ogi)\b\s*(?:ho|hai|hain|the|thi)\b",
    r"\bpe\s+(?:bol\w*|kehte?|kehta|kaho|batao|bataiye|vichar|raye|raaye)\b",
    r"\bpe\s+(?:ka|ki|ke|par|me|mein|aur|se|hi|bhi)\b",
)

#: "Does the company sell software?" — the verb "sell" about a product is not
#: a SELL recommendation. The tell is a product noun after it, or a
#: third-person company subject before it.
#:
#: The auxiliary is restricted to a company subject on purpose. A first
#: auxiliary ("should I buy") is the opposite of evidence: "Should I buy
#: this stock?" is exactly the recommendation question this intent exists
#: for, and an earlier rule that matched any "should/does … buy" silently
#: suppressed it.
#:
#: "buyback" is a corporate action and "holdings" a shareholding fact;
#: neither is a call on the stock.
_NOT_A_RECOMMENDATION = (
    r"\b(?:does|do|did)\s+(?:the\s+)?"
    r"(?:company|firm|business|it|they|ye|kya)\s+(?:sell|sells|buy|buys)\b",
    r"\b(?:what|which)\s+\w*\s*(?:does|do)\s+\w+\s+sell\b",
    r"\b(?:sells?|buys?)\s+(?:software|products?|services?|goods|items?|insurance|"
    r"data|licen\w*|subscriptions?|hardware|chemicals?|steel|cement|oil|gas|power|"
    r"coal|sugar|tea|coffee|fabric|yarn|paper|drugs?|medicines?|vehicles?|cars?|"
    r"bikes?|phones?|chips?|semiconductors?|electricity|fertilizers?)\b",
    r"\bbuy\s*-?\s*backs?\b",
    r"\bholdings?\b",
    r"\bpromoters?\s+hold\w*\b",
    r"\bstake\s+hold\w*\b",
    r"\b(?:goods|units?|shares?|assets?)\s+sold\b",
    r"\bcost\s+of\s+\w+\s+sold\b",
)


# ---------------------------------------------------------------------------
# The vocabulary
#
# Only the gaps are filled. Every intent inherits its canonical patterns;
# the literal phrases below are wording the canonical regexes demonstrably
# miss today.
# ---------------------------------------------------------------------------
_SPECS: tuple[IntentSpec, ...] = (
    # ---------------------------------------------------------- Phase 1 ----
    IntentSpec(
        intent=FinancialIntent.PE,
        family=IntentFamily.PHASE1,
        negative=_PE_AS_POSTPOSITION,
        precedence=50,
    ),
    IntentSpec(
        intent=FinancialIntent.VALUATION,
        family=IntentFamily.PHASE1,
        phrases=(
            # "Is the stock expensive?" is a valuation question — and
            # pointedly NOT a recommendation, which is where a lone
            # adjective about price would otherwise land.
            r"\bexpensive\b", r"\bcheap(?:er|est)?\b", r"\bpricey\b",
            r"\bover\s*-?\s*priced\b", r"\bunder\s*-?\s*priced\b",
            r"\bworth\s+(?:buying|investing)\b",
        ),
        aliases=("mehnga", "mehenga", "mahanga", "sasta"),
        precedence=45,
    ),
    IntentSpec(intent=FinancialIntent.PB, family=IntentFamily.PHASE1,
               precedence=50),
    IntentSpec(intent=FinancialIntent.EPS, family=IntentFamily.PHASE1,
               precedence=50),
    IntentSpec(intent=FinancialIntent.DEBT, family=IntentFamily.PHASE1,
               aliases=("karz", "karja"), precedence=50),
    IntentSpec(intent=FinancialIntent.ROE, family=IntentFamily.PHASE1,
               precedence=50),
    IntentSpec(intent=FinancialIntent.ROCE, family=IntentFamily.PHASE1,
               precedence=50),
    IntentSpec(intent=FinancialIntent.MARKET_PRICE, family=IntentFamily.PHASE1,
               precedence=50),
    # Growth: specific before generic. The specificity table in
    # financial_intent.py already encodes this for the resolver; the
    # precedence values do the same work for overlapping spans here, so
    # "revenue growth" is one intent and not two.
    IntentSpec(intent=FinancialIntent.REVENUE_GROWTH,
               family=IntentFamily.PHASE1, precedence=60),
    IntentSpec(intent=FinancialIntent.PROFIT_GROWTH,
               family=IntentFamily.PHASE1, precedence=60),

    # ------------------------------------------------------- Phase 2A ------
    IntentSpec(
        intent=FinancialIntent.OVERALL_ASSESSMENT,
        family=IntentFamily.INVESTMENT,
        phrases=(r"\bhow\s+good\s+is\b", r"\bhow\s+bad\s+is\b"),
        # Low precedence: "overall" modifies whatever else was asked for
        # rather than being a question in its own right.
        precedence=20,
    ),
    IntentSpec(
        intent=FinancialIntent.FINANCIAL_QUALITY,
        family=IntentFamily.INVESTMENT,
        phrases=(
            r"\bfinancially\s+strong\b",
            r"\bfinancially\s+(?:healthy|sound|solid|stable)\b",
            r"\bfinancial\s+health\b",
            r"\bbalance\s+sheet\s+strong\b",
            r"\bbalance\s+sheet\s+quality\b",
            r"\bfundamentally\s+strong\b",
            r"\bfundamentals?\s+(?:strong|healthy|solid)\b",
            r"\bquality\s+of\s+earnings\b",
        ),
        # Full phrases rather than the bare word "वित्तीय": the bare word
        # also appears inside "वित्तीय जोखिम" (financial risk), and a
        # two-word alias keeps the two intents from both firing.
        aliases=(
            "वित्तीय गुणवत्ता", "वित्तीय सेहत", "वित्तीय स्थिति",
            "वित्तीय मजबूती", "vittiya gunvatta", "vittiya sehat",
        ),
        precedence=50,
    ),
    IntentSpec(
        intent=FinancialIntent.GROWTH_QUALITY,
        family=IntentFamily.INVESTMENT,
        # Generic. Deliberately low precedence, so a specific growth intent
        # overlapping the same span wins.
        precedence=30,
    ),
    IntentSpec(
        intent=FinancialIntent.FINANCIAL_RISK,
        family=IntentFamily.INVESTMENT,
        phrases=(
            # "debt risk" and "liquidity risk" overlap the DEBT pattern's
            # span; the higher precedence resolves that overlap instead of
            # reporting both intents for one question.
            r"\bdebt\s+risk\b", r"\bliquidity\s+risk\b",
            r"\bsolvency\s+risk\b", r"\bbankruptcy\s+risk\b",
            r"\bdebt\s+(?:burden|trap|stress)\b",
            r"\bover\s*-?\s*leveraged\b", r"\bhighly\s+leveraged\b",
        ),
        aliases=("वित्तीय जोखिम", "वित्तीय संकट", "vittiya jokhim"),
        precedence=55,
    ),
    IntentSpec(
        intent=FinancialIntent.STRENGTHS,
        family=IntentFamily.INVESTMENT,
        phrases=(r"\bwhat\s+is\s+good\s+about\b", r"\bkey\s+positives?\b"),
        aliases=("ताकत", "मजबूती"),
        precedence=40,
    ),
    IntentSpec(
        intent=FinancialIntent.WEAKNESSES,
        family=IntentFamily.INVESTMENT,
        phrases=(r"\bwhat\s+is\s+bad\s+about\b", r"\bkey\s+negatives?\b",
                 r"\bred\s+flags?\b"),
        aliases=("कमजोरी", "कमज़ोरी"),
        precedence=40,
    ),
    IntentSpec(intent=FinancialIntent.INVESTMENT_CASE,
               family=IntentFamily.INVESTMENT, precedence=40),
    IntentSpec(
        intent=FinancialIntent.RECOMMENDATION,
        family=IntentFamily.INVESTMENT,
        phrases=(r"\bshould\s+i\s+(?:invest|buy)\b", r"\bbuy\s+hai\s+ya\s+sell\b"),
        aliases=("सिफारिश", "शिफारिश", "सलाह"),
        negative=_NOT_A_RECOMMENDATION,
        precedence=40,
    ),
)


INTENT_VOCABULARY: Mapping[FinancialIntent, IntentSpec] = MappingProxyType(
    {spec.intent: spec for spec in _SPECS}
)


def family_for(intent: FinancialIntent) -> IntentFamily:
    """Which engine owns an intent. Single source: ``INVESTMENT_INTENTS``."""
    return (
        IntentFamily.INVESTMENT if intent in INVESTMENT_INTENTS
        else IntentFamily.PHASE1
    )


_COMPILED: dict[str, re.Pattern[str]] = {}


def compiled(pattern: str) -> re.Pattern[str]:
    """Compile once per pattern.

    The planner runs once per question; recompiling the whole vocabulary on
    every question is the kind of cost that only shows up under load.
    """
    got = _COMPILED.get(pattern)
    if got is None:
        got = re.compile(pattern, re.IGNORECASE)
        _COMPILED[pattern] = got
    return got


def negative_patterns(intent: FinancialIntent) -> tuple[re.Pattern[str], ...]:
    return tuple(compiled(p) for p in INTENT_VOCABULARY[intent].negative)


# ---------------------------------------------------------------------------
# Non-intent question shapes
#
# Neither is an intent. They exist so the planner can report "ambiguous" or
# "comparison" rather than returning no intents and looking as though it
# simply failed to understand.
# ---------------------------------------------------------------------------

#: A judgement with no object. "Good company hai?" is evaluative, but which
#: supported intent was meant is a guess — recorded as ambiguity, never
#: resolved to one.
_VAGUE_EVALUATIVE = re.compile(
    r"\b(?:good|bad|nice|fine|okay|ok|decent|solid|safe|risky|best|worst)\b"
    r"[^.?!]{0,20}\b(?:company|stock|share|business|firm|it|this)\b"
    r"|\b(?:company|stock|share|business)\b[^.?!]{0,20}"
    r"\b(?:acha|achha|achhi|bura|kharab|thik|theek|best|worst)\b",
    re.IGNORECASE,
)

#: Two or more subjects placed against each other.
_COMPARISON = re.compile(
    r"\bcompar\w*\b|\bversus\b|\bvs\.?\b"
    r"|\b(?:better|worse|higher|lower|cheaper|stronger|weaker)\s+than\b"
    r"|\b(?:kaun|konsa|kaunsa)\s+(?:behtar|better|best)\b"
    r"|तुलना|\btulna\b|\bmukable\b|\bmuqable\b",
    re.IGNORECASE,
)


def is_comparison(text: str) -> bool:
    """Two subjects placed against each other, or an explicit comparison."""
    return bool(_COMPARISON.search(text or ""))


def is_vague_evaluative(text: str) -> bool:
    """A judgement about the company with no identifiable supported intent."""
    return bool(_VAGUE_EVALUATIVE.search(text or ""))


# ---------------------------------------------------------------------------
# Web research (Part 3 Phase 4A)
#
# Not an intent either. A question about a *current development* — news, an
# order win, an expansion, a deal, an appointment — asks for something the
# canonical financial store does not hold and no deterministic engine
# computes. The planner names that shape so the self-owned web research path
# can be dispatched on it later; it fetches nothing and generates no query.
#
# Two kinds of signal, either of which is sufficient once every binding
# route (source restriction, comparison, supported intents, vague
# evaluation, unsupported financial figure) has been ruled out:
#
# * **recency** — "latest", "recent", "today", "aaj", "abhi", "taaza",
#   "update", "news". A recency word next to a supported intent never
#   reaches this check, because the intent is matched first.
# * **development** — a corporate event noun: expansion, acquisition,
#   merger, contract, launch, plant, project, appointment, resignation.
#
# Matched against the RAW question only, never the planning normalisation.
# The normaliser maps "vistar se batao" (tell me in detail) to "expansion",
# which is the right reading for retrieval and the wrong one here; the raw
# words carry the distinction and the aliases below cover Hindi directly.
#
# "order" is deliberately NOT a bare signal. "What is the order book?" is a
# definitional question the internal open-ended engine already owns, and an
# order becomes a development only when qualified ("latest order", "new
# order", "order win", "bagged an order").
# ---------------------------------------------------------------------------

#: Recency signals, Latin script: time adverbs — the words that say *now*
#: without naming anything. A search query can drop them and still be about
#: the same thing, which is why they are a class of their own.
_WEB_RECENCY = re.compile(
    r"\b(?:latest|recent|recently|newest|today|todays|this\s+week|"
    r"aaj|aajkal|abhi|taaza|taaja|taza|haal\s+(?:hi|ki|me|mein)|"
    r"haal-?filhaal)\b",
    re.IGNORECASE,
)

#: News signals, Latin script: "what is happening" nouns and shapes. Unlike
#: a time adverb these ARE the topic — "news" dropped from "JSW Steel news"
#: leaves a company name, not a question.
_WEB_NEWS = re.compile(
    r"\b(?:update|updates|updated|news|headlines?|khabar|khabre+n|"
    r"khabrein|samachar|kya\s+chal\s+raha|kya\s+ho\s+raha|kya\s+hua|"
    r"kya\s+hui|what\s+happened|what\s+is\s+happening|whats\s+happening|"
    r"what's\s+happening|going\s+on)\b",
    re.IGNORECASE,
)

#: Development signals, Latin script. Corporate event nouns and verbs.
_WEB_DEVELOPMENT = re.compile(
    r"\b(?:expansions?|expanding|expanded|expand|capacity\s+addition|"
    r"acquisitions?|acquired|acquires?|acquiring|takeovers?|mergers?|"
    r"merged|merging|demergers?|joint\s+ventures?|jv|partnerships?|"
    r"tie-?ups?|agreements?|mou|mous|contracts?|deals?|"
    r"announcements?|announced|announces?|launch|launches|launched|"
    r"launching|plants?|projects?|commissioned|commissioning|"
    r"appointments?|appointed|resigns?|resigned|resignation|"
    r"stepped\s+down|steps\s+down|stake\s+sale|stake\s+buy|buy\s*-?\s*backs?|"
    r"bonus\s+issue|stock\s+split|rights\s+issue|ipo|delisting|"
    r"approvals?|regulatory\s+action|penalt(?:y|ies)|lawsuits?|litigation|"
    r"(?:workers?|labou?r|employees?)\s+strikes?|shutdowns?|layoffs?|"
    r"order\s+wins?|new\s+orders?|fresh\s+orders?|orders?\s+worth|"
    r"bag(?:s|ged)\s+(?:an?\s+|new\s+|the\s+|fresh\s+)?(?:orders?|contracts?|projects?)|"
    r"won\s+(?:an?\s+|new\s+)?orders?|naya\s+order|naye\s+orders?|"
    r"order\s+mila|vistar|adhigrahan|vilay|sauda|samjhauta|ghoshna|elaan|"
    r"ailaan|niyukti|istifa|manzoori|pariyojana)\b",
    re.IGNORECASE,
)

#: Devanagari signals. Literal substrings, for the same reason the intent
#: aliases are: a matra is a combining mark, so ``\\b`` is unreliable.
_WEB_RECENCY_ALIASES: tuple[str, ...] = (
    "ताज़ा", "ताजा", "नवीनतम", "हालिया", "हाल ही", "हाल की", "हाल में", "आज",
    "अभी",
)
_WEB_NEWS_ALIASES: tuple[str, ...] = (
    "अपडेट", "खबर", "ख़बर", "समाचार", "न्यूज़", "न्यूज",
    "क्या चल रहा", "क्या हो रहा", "क्या हुआ",
)
_WEB_DEVELOPMENT_ALIASES: tuple[str, ...] = (
    "विस्तार", "अधिग्रहण", "विलय", "सौदा", "समझौता", "घोषणा", "ऐलान",
    "नियुक्ति", "इस्तीफ़ा", "इस्तीफा", "मंज़ूरी", "मंजूरी", "परियोजना",
    "प्लांट", "लॉन्च", "ऑर्डर", "आर्डर", "कॉन्ट्रैक्ट", "अनुबंध",
)
_WEB_RESEARCH_ALIASES: tuple[str, ...] = (
    _WEB_RECENCY_ALIASES + _WEB_NEWS_ALIASES + _WEB_DEVELOPMENT_ALIASES
)

#: The signal classes, by name. Exposed so the web query generator can tell
#: a time adverb from a news noun from an event noun with the SAME
#: vocabulary that routed the question — not a second list that would
#: drift from it.
WEB_SIGNAL_RECENCY = "recency"
WEB_SIGNAL_NEWS = "news"
WEB_SIGNAL_DEVELOPMENT = "development"

#: Shapes that contain a signal word and are NOT web research. Removed from
#: the text before the signals are consulted.
_NOT_WEB_RESEARCH = re.compile(
    # "order book" is a defined term the open-ended engine explains.
    r"\border\s*-?\s*books?\b|ऑर्डर\s*बुक|आर्डर\s*बुक"
    # "in order to" is grammar, not an order.
    r"|\bin\s+order\s+to\b"
    # "vistar se" / "विस्तार से" is "in detail", not an expansion.
    r"|\b(?:vistar|vistaar)\s+se\b|विस्तार\s*(?:से|में|पूर्वक)"
    # "expand on that" is a request to elaborate.
    r"|\bexpand\s+(?:on|upon)\b"
    # "deal in"/"deal with" is what the company does, not a transaction.
    r"|\bdeals?\s+(?:in|with)\b",
    re.IGNORECASE,
)


def is_web_research(text: str) -> bool:
    """Whether the question asks about a current development.

    Pure: a regex and a substring scan over the text it is given. It
    contacts nothing, and it does not know what a company is — entity
    resolution stays with the planner.
    """
    candidate = text or ""
    if not candidate.strip():
        return False
    scrubbed = _NOT_WEB_RESEARCH.sub(" ", candidate)
    if (_WEB_RECENCY.search(scrubbed) or _WEB_NEWS.search(scrubbed)
            or _WEB_DEVELOPMENT.search(scrubbed)):
        return True
    return any(alias in scrubbed for alias in _WEB_RESEARCH_ALIASES)


def web_research_signal(term: str) -> str | None:
    """Which signal class one term or short phrase belongs to, if any.

    ``"recency"`` for a time adverb ("latest", "aaj", "ताज़ा"), ``"news"``
    for a what-is-happening noun ("news", "khabar", "update"),
    ``"development"`` for an event noun ("expansion", "विस्तार"), ``None``
    for anything else. Whole-term matching, so "order" alone is nothing and
    "new order" is a development — the same reading :func:`is_web_research`
    gives the full question.
    """
    candidate = (term or "").strip()
    if not candidate:
        return None
    if _WEB_RECENCY.fullmatch(candidate) or candidate in _WEB_RECENCY_ALIASES:
        return WEB_SIGNAL_RECENCY
    if _WEB_NEWS.fullmatch(candidate) or candidate in _WEB_NEWS_ALIASES:
        return WEB_SIGNAL_NEWS
    if (_WEB_DEVELOPMENT.fullmatch(candidate)
            or candidate in _WEB_DEVELOPMENT_ALIASES):
        return WEB_SIGNAL_DEVELOPMENT
    return None
