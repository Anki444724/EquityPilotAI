"""Deterministic web search queries from a question plan (Part 3 Phase 4B).

    QuestionPlan  ->  WebQueryGenerator  ->  WebQuerySet
                                              (a handful of short queries
                                               for the self-owned web index)

What this module is
-------------------
A bounded, provider-free rewrite of a *planned* question into the short
keyword queries a lexical/semantic index answers well. It reads the plan's
subject (the company the planner resolved) and the plan's own words, drops
the grammar, keeps the content, and emits a few variants in a fixed order.
The same plan always yields the same queries.

What it is not
--------------
* **Not a model.** No LLM, no provider, no network. Every rule below is a
  regex or a set lookup, and the package's architecture tests enforce that
  by static inspection exactly as they do for the rest of the planner.
* **Not an entity resolver.** The subject of a query is the company the
  planner resolved — ``plan.entity`` — or nothing. A company the resolver
  did not return is never inferred from the words, and an ambiguous entity
  (two companies named) yields no subject rather than a guess.
* **Not a search.** Nothing here touches the index. It produces text for
  :class:`app.services.web.index.SelfOwnedWebIndex` to search with.

Bounds, stated once
-------------------
:class:`WebQueryLimits` caps the number of queries, the tokens per query,
the characters per query and the topic terms drawn from the question. The
caps are hard: a longer question does not produce a longer query, it
produces the same-shaped query with fewer of its words.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from .types import EntityStatus, ExecutionRoute, QuestionPlan
from .vocabulary import (
    WEB_SIGNAL_DEVELOPMENT, WEB_SIGNAL_NEWS, WEB_SIGNAL_RECENCY,
    web_research_signal,
)


class WebQueryStatus(StrEnum):
    """Why a query set has the queries it has."""

    #: At least one query was produced.
    GENERATED = "generated"
    #: The plan is not on a route this generator serves. Nothing produced.
    NOT_APPLICABLE = "not_applicable"
    #: More than one company was named; no subject is chosen and no query
    #: is produced, because a query about the wrong company is worse than
    #: none.
    AMBIGUOUS_SUBJECT = "ambiguous_subject"
    #: No subject and no content words survived. Nothing to search for.
    EMPTY = "empty"


@dataclass(frozen=True, slots=True)
class WebQueryLimits:
    """Hard ceilings on what one plan may turn into.

    Every field is a cap and every cap is enforced on the output, not on
    the input: a 400-word question is cut to the same shape as a 10-word
    one. Validated at construction so a misconfigured limit fails loudly
    rather than producing an unbounded query set.
    """

    max_queries: int = 4
    max_query_tokens: int = 8
    max_query_chars: int = 96
    max_topic_terms: int = 6
    max_term_chars: int = 32

    def __post_init__(self) -> None:
        for name in ("max_queries", "max_query_tokens", "max_query_chars",
                     "max_topic_terms", "max_term_chars"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be at least 1")


DEFAULT_LIMITS = WebQueryLimits()


@dataclass(frozen=True, slots=True)
class WebQuery:
    """One search query and how it was built."""

    text: str
    terms: tuple[str, ...]
    #: Which construction rule produced it, for the audit trail.
    basis: str
    #: ``True`` when the resolved company is part of the query.
    scoped: bool

    def as_dict(self) -> dict:
        return {
            "text": self.text,
            "terms": list(self.terms),
            "basis": self.basis,
            "scoped": self.scoped,
        }


@dataclass(frozen=True, slots=True)
class WebQuerySet:
    """Everything the generator decided about one plan."""

    status: WebQueryStatus
    queries: tuple[WebQuery, ...] = ()
    #: The company used as subject, exactly as the planner resolved it
    #: (legal suffix removed). ``None`` when no company is in play.
    subject: str | None = None
    ticker: str | None = None
    company_id: str | None = None
    #: Content words drawn from the question, in question order.
    topic_terms: tuple[str, ...] = ()
    reason: str = ""

    @property
    def texts(self) -> tuple[str, ...]:
        return tuple(q.text for q in self.queries)

    @property
    def is_empty(self) -> bool:
        return not self.queries

    def as_dict(self) -> dict:
        return {
            "status": self.status.value,
            "queries": [q.as_dict() for q in self.queries],
            "subject": self.subject,
            "ticker": self.ticker,
            "company_id": self.company_id,
            "topic_terms": list(self.topic_terms),
            "reason": self.reason,
        }


# ---------------------------------------------------------------------------
# Text rules
# ---------------------------------------------------------------------------

#: A token: Latin or Devanagari letters and digits, with the joiners that
#: keep "tie-up", "L&T", "Q2FY26" and "P/E" whole. The Devanagari block is
#: named explicitly because ``\\w`` does not cover a matra, so a plain
#: ``\\w+`` would split a Hindi word at every vowel sign.
_TOKEN = re.compile(
    r"[\w\u0900-\u097F]+(?:[-'&/.][\w\u0900-\u097F]+)*",
    re.UNICODE,
)

_DEVANAGARI = re.compile(r"[\u0900-\u097F]")

#: Legal-form suffixes stripped from a company name for querying. "JSW
#: Steel Limited" is what the register says; "JSW Steel" is what a page
#: says. Trailing run only, so a suffix word inside a name is untouched.
_LEGAL_SUFFIX = re.compile(
    r"(?:\s+(?:private|pvt\.?|limited|ltd\.?|plc|inc\.?|corp\.?|"
    r"corporation|llc|llp|co\.?|company))+\s*$",
    re.IGNORECASE,
)

#: Words that carry no search content. Three scripts, one set. Kept
#: deliberately narrow on the English side: a query term that is dropped is
#: gone, and dropping "plant" or "order" would lose the question.
_FILLERS = frozenset("""
a an the of for to in on at by with from about into over under between
is are was were be been being am do does did done has have had having
can could should would will shall may might must
what which who whom whose how why when where whats what's
that this these those it its it's there their they them then than so such
any some me my mine you your yours we our us i
tell give show list explain describe please kindly pls plz let know want
need like also just only very really much many more most all
company companies stock stocks share shares firm
ka ki ke ko se me mein par pe aur ya kya kyu kyun kyon kaise kaisa kaisi
kitna kitni kitne kab kahan kaun kaunsa konsa kaunsi hai hain ho hun tha
thi the hoga hogi honge raha rahi rahe chal batao bataiye bataye bata do
dijiye karo kar kiya kiye karti karta karte hua hui hue hone ne wala wali
wale bhi hi toh na nahi nhi mujhe muje humko hume mera meri mere apna apni
apne uska uski uske iska iski iske ye yeh wo woh is us baare bare sab kuch
koi matlab jeeta jeeti jeete mila mili mile liya diya gaya gayi gaye
का की के को से में पर और या क्या क्यों कैसे कैसा कैसी कितना कितनी कितने
कब कहाँ कहां कौन कौनसा है हैं हो हूँ था थी थे होगा होगी होंगे रहा रही रहे
चल बताओ बताइए बताएं बता दो दीजिए करो कर किया किये किए करती करता करते हुआ
हुई हुए ने वाला वाली वाले भी ही तो ना नहीं मुझे हमें मेरा मेरी मेरे अपना
अपनी अपने उसका उसकी उसके इसका इसकी इसके ये यह वो वह इस उस बारे कंपनी शेयर
स्टॉक
""".split())


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall(text or "")


def _is_devanagari(token: str) -> bool:
    return bool(_DEVANAGARI.search(token))


def _clean_subject(name: str | None) -> str | None:
    """The company name as a page would print it, or ``None``."""
    candidate = " ".join((name or "").split())
    if not candidate:
        return None
    stripped = _LEGAL_SUFFIX.sub("", candidate).strip()
    return stripped or candidate


# ---------------------------------------------------------------------------
class WebQueryGenerator:
    """Turns a web-research plan into a few short, bounded search queries.

    Stateless and safe to share. ``served_routes`` names the execution
    routes the generator will produce queries for; the default is the one
    the planner created for this purpose, and a plan on any other route is
    reported as :attr:`WebQueryStatus.NOT_APPLICABLE` rather than searched.
    """

    def __init__(
        self,
        *,
        limits: WebQueryLimits | None = None,
        served_routes: tuple[ExecutionRoute, ...] = (ExecutionRoute.WEB_RESEARCH,),
    ) -> None:
        self._limits = limits or DEFAULT_LIMITS
        self._routes = tuple(served_routes)

    @property
    def limits(self) -> WebQueryLimits:
        return self._limits

    # ------------------------------------------------------------------ api
    def generate(self, plan: QuestionPlan | None) -> WebQuerySet:
        """Produce the query set for one plan. Never raises on plan content."""
        if plan is None or not (plan.original_question or "").strip():
            return WebQuerySet(
                status=WebQueryStatus.EMPTY,
                reason="no question was supplied",
            )

        if plan.execution_route not in self._routes:
            return WebQuerySet(
                status=WebQueryStatus.NOT_APPLICABLE,
                reason=(
                    f"route '{plan.execution_route.value}' is not served; "
                    "web queries are generated for "
                    + ", ".join(f"'{r.value}'" for r in self._routes)
                ),
            )

        entity = plan.entity
        if entity.status is EntityStatus.AMBIGUOUS:
            names = ", ".join(entity.candidates) or "several companies"
            return WebQuerySet(
                status=WebQueryStatus.AMBIGUOUS_SUBJECT,
                reason=(
                    f"the question names more than one company ({names}); "
                    "no subject is chosen for a web query"
                ),
            )

        subject, ticker, company_id = self._subject(plan)
        topic = self._topic_terms(plan, subject, ticker)
        leads = {s for s in (subject, ticker) if s}

        latin = [t for t in topic if not _is_devanagari(t)]
        deva = [t for t in topic if _is_devanagari(t)]

        drafts: list[tuple[list[str], str]] = []
        drafts.extend(self._family(subject, latin, script=""))
        if ticker and ticker.casefold() != (subject or "").casefold():
            core = self._without(latin, {WEB_SIGNAL_RECENCY}) or latin
            if core:
                drafts.append(([ticker] + core, "ticker + topic"))
        drafts.extend(self._family(subject, deva, script=" (devanagari)"))

        if not drafts and subject:
            drafts.append(([subject], "subject only"))

        queries = self._bound(drafts, leads)
        if not queries:
            return WebQuerySet(
                status=WebQueryStatus.EMPTY,
                subject=subject, ticker=ticker, company_id=company_id,
                topic_terms=tuple(topic),
                reason="no subject and no content words survived",
            )

        return WebQuerySet(
            status=WebQueryStatus.GENERATED,
            queries=queries,
            subject=subject, ticker=ticker, company_id=company_id,
            topic_terms=tuple(topic),
            reason=(
                f"{len(queries)} quer{'y' if len(queries) == 1 else 'ies'} "
                f"from {len(topic)} content term(s)"
                + (" scoped to the resolved company" if subject else
                   " with no resolved company")
            ),
        )

    # -------------------------------------------------------------- subject
    @staticmethod
    def _subject(plan: QuestionPlan) -> tuple[str | None, str | None, str | None]:
        """The resolved company, or nothing. Never inferred from the words."""
        entity = plan.entity
        if not entity.is_usable:
            return None, None, None
        ticker = (entity.ticker or "").strip() or None
        subject = _clean_subject(entity.name) or ticker
        return subject, ticker, entity.company_id

    # ---------------------------------------------------------------- topic
    def _topic_terms(
        self, plan: QuestionPlan, subject: str | None, ticker: str | None,
    ) -> list[str]:
        """Content words of the question, in order, subject removed.

        The raw question comes first so the user's own words lead; the
        planning normalisation is consulted afterwards only for the English
        it added ("विस्तार" -> "expansion"), because a Hindi page and an
        English page are both in the corpus and one query script alone
        would miss half of it.
        """
        subject_tokens = {
            t.casefold() for t in _tokens(subject or "")
        } | {t.casefold() for t in _tokens(ticker or "")}

        seen: set[str] = set()
        terms: list[str] = []
        for source in (plan.original_question, plan.normalized_question):
            for token in _tokens(source):
                key = token.casefold()
                if key in _FILLERS or key in subject_tokens or key in seen:
                    continue
                if len(token) > self._limits.max_term_chars:
                    continue
                seen.add(key)
                terms.append(token)
        return terms[: self._limits.max_topic_terms]

    # -------------------------------------------------------------- variants
    def _family(
        self, subject: str | None, topic: list[str], *, script: str,
    ) -> list[tuple[list[str], str]]:
        """The ordered variants for one script's topic terms."""
        if not topic:
            return []
        lead = [subject] if subject else []
        prefix = "subject + " if subject else ""

        core = self._without(topic, {WEB_SIGNAL_RECENCY})
        events = [
            t for t in topic
            if web_research_signal(t) in {WEB_SIGNAL_DEVELOPMENT, WEB_SIGNAL_NEWS}
        ]

        out: list[tuple[list[str], str]] = [
            (lead + topic, f"{prefix}topic{script}"),
        ]
        if core and core != topic:
            out.append((lead + core, f"{prefix}topic without recency{script}"))
        if events and events != core and events != topic:
            out.append((lead + events, f"{prefix}event terms{script}"))
        return out

    @staticmethod
    def _without(topic: list[str], classes: set[str]) -> list[str]:
        return [t for t in topic if web_research_signal(t) not in classes]

    # ----------------------------------------------------------------- bounds
    def _bound(
        self, drafts: list[tuple[list[str], str]], leads: set[str],
    ) -> tuple[WebQuery, ...]:
        """Apply every cap, deduplicate, and freeze."""
        limits = self._limits
        out: list[WebQuery] = []
        seen: set[str] = set()
        for terms, basis in drafts:
            kept = [t for t in terms if t][: limits.max_query_tokens]
            # Both caps are measured on the text a search engine will see:
            # whitespace-separated words and characters. A multi-word
            # subject therefore counts for every word it contains.
            while len(kept) > 1 and (
                len(" ".join(kept)) > limits.max_query_chars
                or len(" ".join(kept).split()) > limits.max_query_tokens
            ):
                kept.pop()
            text = " ".join(kept)[: limits.max_query_chars].strip()
            key = " ".join(text.casefold().split())
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(WebQuery(
                text=text,
                terms=tuple(kept),
                basis=basis,
                scoped=bool(kept) and kept[0] in leads,
            ))
            if len(out) >= limits.max_queries:
                break
        return tuple(out)


__all__ = [
    "DEFAULT_LIMITS", "WebQuery", "WebQueryGenerator", "WebQueryLimits",
    "WebQuerySet", "WebQueryStatus",
]
