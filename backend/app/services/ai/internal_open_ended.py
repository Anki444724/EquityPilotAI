"""Internal open-ended answering (Part 2D).

The layer that ``ExecutionRoute.INTERNAL_REASONING`` has always named and
never had. Part 2B wrote the route down with the comment "needs internal
reasoning that does not exist yet"; this module is that reasoning.

    question
      -> QuestionPlan            (Part 2B — unchanged)
      -> capability selection    (this module)
      -> evidence requirements   (this module, from the existing context)
      -> bounded operations      (this module, closed set)
      -> composition             (this module, canonical English)
      -> _deterministic()        (the existing funnel — unchanged)

**It is not a language model and does not try to be one.** There is no
prompt, no provider, no retrieval, no neural component and no interpreter.
The capability set is closed, the operation set is closed, and every
vocabulary is an explicit table in this file. A question this module cannot
answer from the evidence the platform already holds returns
:attr:`InternalAnswerStatus.NOT_SUPPORTED`, and the existing
retrieval/provider path answers it exactly as it did before Part 2D
existed. Silence is the failure mode this design chooses: a missing answer
is recoverable, a fabricated one is not.

**It owns exactly one route.** ``INTERNAL_REASONING`` is reached only by a
plan whose query type is ``COMPARISON`` or ``OPEN_ENDED`` — the two shapes
the deterministic engines cannot serve, because they are single-company and
single-intent by construction. Every other route keeps its existing owner:
the resolver and the two deterministic engines (Part 1/2A), the composer
(Part 2C), the source router, and the provider fallback for ``DECLINE``.
Nothing here can intercept a question an existing route already answers,
because the analyst consults this module only after those have declined.

**It invents nothing.** Every figure in an answer is either a citation the
:class:`ContextBuilder` already published, or a value derived from such
citations by one of the four arithmetic operations in
:data:`_OPERATIONS` — and a derived value is published back as a
:class:`Citation` in the same evidence architecture, so the existing
citation audit can resolve it. That is precisely what
``ContextBuilder._add_ratios`` already does for computed ratios; this
module extends the practice to the handful of figures a question asked for
that no engine pre-computed. It never creates a second citation model, a
second scoring model or a second translation path.

**Provider isolation is structural, not aspirational.** This module imports
no provider, no HTTP client and no SDK. ``tests/test_internal_open_ended_architecture.py``
enforces it by parsing this file's own imports and by importing it in a
subprocess with the provider packages blocked, so the property cannot be
satisfied by whatever else a test session happens to have loaded.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

from app.domain.ai.types import Citation, EvidenceKind
from app.domain.calc import safe_div
from app.services.ai.context_builder import GroundedContext
from app.services.ai.planner.types import (
    EntityStatus, ExecutionRoute, QueryType, QuestionPlan,
)

# ===========================================================================
# Capability taxonomy
# ===========================================================================


class OpenEndedCapability(StrEnum):
    """What kind of open-ended question a plan is being answered as.

    Closed on purpose. Adding a member means adding an executor and a test;
    a member with no executor would be a capability the module advertises
    and cannot deliver, which is the failure mode this taxonomy exists to
    prevent.
    """

    #: One attribute of the bound company, read from authoritative context.
    FACT_LOOKUP = "fact_lookup"
    #: A derived figure computed from citations that both exist.
    CALCULATION = "calculation"
    #: Two metrics of the SAME company, stated with their measurements.
    COMPARISON = "comparison"
    #: A term from the bounded internal explanation vocabulary.
    EXPLANATION = "explanation"
    #: Several bounded operations, executed in order and capped.
    MULTI_STEP_ANALYSIS = "multi_step_analysis"
    #: Recognised as open-ended, and not answerable internally. The
    #: existing fallback owns it.
    UNSUPPORTED_OPEN_ENDED = "unsupported_open_ended"
    #: The subject or the metric could not be pinned to exactly one thing.
    #: Never resolved by choosing.
    AMBIGUOUS = "ambiguous"


class InternalAnswerStatus(StrEnum):
    """Whether the internal layer answered or handed the question back."""

    #: A complete, evidence-grounded answer was produced.
    ANSWERED = "answered"
    #: Nothing was produced. The caller must fall back; there is no partial
    #: internal answer to salvage, by construction.
    NOT_SUPPORTED = "not_supported"


class OperationKind(StrEnum):
    """The closed set of arithmetic this layer may perform.

    A closed enum rather than an expression language is the security
    boundary. There is no ``eval``, no ``exec`` and no callable taken from
    data: every operation names a function defined in this module, and a
    ``kind`` that is not a member cannot be constructed.
    """

    #: Read one citation's value. No arithmetic.
    LOOKUP = "lookup"
    #: ``a - b``, both explicit.
    DIFFERENCE = "difference"
    #: ``(a - b) / |b|`` as a fraction. Undefined when ``b`` is zero.
    PERCENTAGE_CHANGE = "percentage_change"
    #: ``a / b``. Undefined when ``b`` is zero.
    RATIO = "ratio"


#: The hard ceiling on operations in one internal answer. Multi-step
#: reasoning is permitted; unbounded reasoning is not. A question needing
#: more steps than this is handed back rather than truncated, because a
#: truncated analysis reads as a complete one.
MAX_OPERATIONS = 6


# ===========================================================================
# Contracts
# ===========================================================================


@dataclass(frozen=True, slots=True)
class InternalEvidenceRequirement:
    """One piece of evidence an internal answer needs.

    Requirement metadata only — never a value, for the same reason the
    planner's ``EvidenceRequirement`` carries none: a requirement that
    already held a number would be an answer, and every safety property
    here rests on the answer containing nothing the platform did not
    compute.
    """

    key: str
    label: str
    #: Which side of an operation this feeds — "left", "right" or "value".
    role: str
    #: ``False`` for context the answer can be given without.
    required: bool = True

    def as_dict(self) -> dict:
        return {
            "key": self.key, "label": self.label,
            "role": self.role, "required": self.required,
        }


@dataclass(frozen=True, slots=True)
class InternalOperation:
    """One executed bounded operation, with its inputs and its result.

    The audit trail of an internal answer. Every figure the answer states
    is either an input citation's value or this object's ``result``, so a
    reader can recompute any claim from the citations beside it.
    """

    kind: OperationKind
    #: Citation keys consumed, in order. ``()`` for a pure lookup of one key
    #: is never used — a lookup names its single key here too.
    inputs: tuple[str, ...]
    #: The key the derived figure is published under. Equal to the input key
    #: for a lookup, so no second name for the same figure can appear.
    output_key: str
    label: str
    #: ``None`` when the operation is undefined — a missing input or a
    #: division by zero. Never coerced to ``0``: an undefined figure and a
    #: genuine zero are different facts.
    result: float | None = None
    unit: str = ""
    #: Input keys that were absent from the evidence.
    missing: tuple[str, ...] = ()
    #: Why the operation could not run, when it could not.
    reason: str = ""

    @property
    def succeeded(self) -> bool:
        return self.result is not None

    def as_dict(self) -> dict:
        return {
            "kind": self.kind.value, "inputs": list(self.inputs),
            "output_key": self.output_key, "label": self.label,
            "result": self.result, "unit": self.unit,
            "missing": list(self.missing), "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class InternalAnswerPlan:
    """How an internal answer will be produced, before it is produced.

    Kept separate from :class:`InternalAnswer` so the plan can be logged
    and asserted on independently of the prose — the same separation the
    planner draws between a plan and an answer.
    """

    capability: OpenEndedCapability
    operations: tuple[InternalOperation, ...] = ()
    requirements: tuple[InternalEvidenceRequirement, ...] = ()
    #: Why this capability was selected, for the audit trail.
    reason: str = ""

    def as_dict(self) -> dict:
        return {
            "capability": self.capability.value,
            "operations": [o.as_dict() for o in self.operations],
            "requirements": [r.as_dict() for r in self.requirements],
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class InternalAnswer:
    """A provider-free answer to an open-ended question, or a refusal.

    Carries the same three fields the existing funnel reads from
    ``DeterministicAnswer`` and ``ComposedAnswer`` — ``content``,
    ``used_citations`` and ``missing`` — so it reaches
    ``_verify_and_record`` through the same door with no second pipeline.
    ``derived_citations`` is the one addition: figures this layer computed
    from real citations, published as ``Citation`` objects so the existing
    audit can resolve them rather than flagging them as fabricated.
    """

    status: InternalAnswerStatus
    capability: OpenEndedCapability
    #: Canonical English answer text with ``[key]`` markers. Empty for a
    #: refusal — a refusal has no text to audit.
    content: str = ""
    #: Citations read from the context, in order of first use.
    used_citations: tuple[Citation, ...] = ()
    #: Figures derived here, published into the same evidence architecture.
    derived_citations: tuple[Citation, ...] = ()
    #: Evidence keys the question needed and the context did not carry.
    missing: tuple[str, ...] = ()
    #: The operations actually executed.
    operations: tuple[InternalOperation, ...] = ()
    #: Why the answer is what it is — including why it is a refusal.
    reason: str = ""

    @property
    def answered(self) -> bool:
        return self.status is InternalAnswerStatus.ANSWERED

    @property
    def all_citations(self) -> tuple[Citation, ...]:
        """Read evidence plus derived figures, in a stable order."""
        return (*self.used_citations, *self.derived_citations)

    def as_dict(self) -> dict:
        return {
            "status": self.status.value,
            "capability": self.capability.value,
            "content": self.content,
            "used_citations": [c.key for c in self.used_citations],
            "derived_citations": [c.key for c in self.derived_citations],
            "missing": list(self.missing),
            "operations": [o.as_dict() for o in self.operations],
            "reason": self.reason,
        }


# ===========================================================================
# Bounded vocabularies
#
# Three explicit tables. Nothing outside them is answerable, which is the
# whole point: coverage is grown by adding a row and a test, never by
# generalising a matcher until it guesses.
# ===========================================================================


@dataclass(frozen=True, slots=True)
class MetricSpec:
    """One comparable figure the platform publishes as a citation.

    ``key`` is the ContextBuilder's own citation key — this table maps
    surface language onto evidence, it does not define any figure. No
    formula appears here, so no formula is duplicated.
    """

    key: str
    label: str
    #: Surface patterns, matched with word boundaries on the normalised
    #: question. Ordered by specificity in :data:`METRIC_SPECS`.
    patterns: tuple[str, ...]
    #: The unit this figure is expressed in, used only to decide whether two
    #: figures may be subtracted. Read from the citation itself at answer
    #: time; recorded here so a spec can be reviewed without the data.
    unit: str


def _metric(key: str, label: str, unit: str, *patterns: str) -> MetricSpec:
    return MetricSpec(key=key, label=label, unit=unit, patterns=patterns)


#: The comparable figures. Every key is one the ContextBuilder already
#: publishes; a key here that the builder does not emit simply yields a
#: missing-evidence refusal rather than a fabricated figure.
#:
#: Ordered most-specific first: "net debt" must be matched before "debt",
#: and "revenue growth" before "revenue", or the shorter pattern would win
#: and the answer would be about a different figure than was asked for.
METRIC_SPECS: tuple[MetricSpec, ...] = (
    # --- money levels ----------------------------------------------------
    _metric("revenue", "Revenue", "₹ cr",
            r"revenue", r"sales", r"turnover", r"top ?line"),
    _metric("ebitda", "EBITDA", "₹ cr", r"ebitda"),
    _metric("ebit", "EBIT", "₹ cr", r"\bebit\b"),
    _metric("pat", "Profit after tax", "₹ cr",
            r"net ?profit", r"net ?income", r"\bpat\b", r"\bprofit\b"),
    # --- percentages -----------------------------------------------------
    _metric("ebitda_margin", "EBITDA margin", "%",
            r"ebitda ?margin", r"operating ?margin"),
    _metric("pat_margin", "Net margin", "%",
            r"net ?margin", r"profit ?margin", r"pat ?margin"),
    _metric("tax_rate", "Effective tax rate", "%", r"tax ?rate"),
    _metric("roe_avg", "Return on equity", "%", r"\broe\b", r"return on equity"),
    _metric("roce", "Return on capital employed", "%",
            r"\broce\b", r"return on capital employed"),
    _metric("roic", "Return on invested capital", "%",
            r"\broic\b", r"return on invested capital"),
    # --- balance sheet / cash flow ---------------------------------------
    _metric("gross_debt", "Gross debt", "₹ cr", r"gross ?debt", r"total ?debt"),
    _metric("net_debt", "Net debt", "₹ cr", r"net ?debt"),
    _metric("equity", "Shareholders' equity", "₹ cr",
            r"equity", r"net ?worth", r"book ?value"),
    _metric("total_assets", "Total assets", "₹ cr", r"total ?assets", r"\bassets\b"),
    _metric("cfo", "Cash flow from operations", "₹ cr",
            r"cash ?flow from operations", r"\bcfo\b", r"operating ?cash ?flow"),
    _metric("capex", "Capital expenditure", "₹ cr", r"capex", r"capital ?expenditure"),
    _metric("fcf", "Free cash flow", "₹ cr", r"free ?cash ?flow", r"\bfcf\b"),
    # --- per share and market --------------------------------------------
    _metric("eps", "EPS (basic)", "₹", r"\beps\b", r"earnings per share"),
    _metric("price", "Market price", "₹", r"share ?price", r"market ?price"),
    _metric("market_cap", "Market capitalisation", "₹ cr",
            r"market ?cap", r"market ?capitalisation"),
    # --- multiples --------------------------------------------------------
    _metric("pe_ratio", "Trailing P/E", "x", r"\bp/?e\b", r"price to earnings"),
    _metric("net_debt_ebitda", "Net debt / EBITDA", "x",
            r"net ?debt ?/ ?ebitda", r"debt to ebitda"),
    _metric("current_ratio", "Current ratio", "x", r"current ?ratio"),
    _metric("interest_coverage", "Interest coverage", "x",
            r"interest ?coverage"),
)

_METRIC_PATTERNS: tuple[tuple[MetricSpec, re.Pattern[str]], ...] = tuple(
    (spec, re.compile(rf"\b(?:{pattern})\b", re.IGNORECASE))
    for spec in METRIC_SPECS
    for pattern in spec.patterns
)


@dataclass(frozen=True, slots=True)
class AttributeSpec:
    """One non-numeric attribute of the company, read from the context.

    Deliberately tiny. The platform publishes no citation for these, so
    they are read from the same :class:`GroundedContext` fields the provider
    path receives and published as a derived citation at answer time, which
    keeps the claim auditable through the existing machinery.
    """

    key: str
    label: str
    patterns: tuple[str, ...]
    #: How the phrase is used in a sentence: "the {value} sector".
    sentence: str


#: Non-financial attributes. ``sector`` is the one the ContextBuilder's own
#: source carries on every company; anything else would be a guess.
#:
#: The Devanagari alternatives are not a translation layer. The Language
#: Adapter's inbound normalisation turns "क्या है?" into "what" but leaves a
#: transliterated noun like सेक्टर alone — it is already the word Indian
#: financial Hindi uses — so the Devanagari form has to be matched here for
#: the same question to reach the same answer in every language.
ATTRIBUTE_SPECS: tuple[AttributeSpec, ...] = (
    AttributeSpec(
        key="company_sector", label="Sector",
        patterns=(r"sector", r"industry", r"line of business", r"segment",
                  r"सेक्टर", r"उद्योग"),
        sentence="{name} is classified in the {value} sector.",
    ),
)

_ATTRIBUTE_PATTERNS: tuple[tuple[AttributeSpec, re.Pattern[str]], ...] = tuple(
    (spec, re.compile(rf"\b(?:{pattern})\b", re.IGNORECASE))
    for spec in ATTRIBUTE_SPECS
    for pattern in spec.patterns
)


@dataclass(frozen=True, slots=True)
class ExplanationSpec:
    """One term in the bounded internal explanation vocabulary."""

    term: str
    patterns: tuple[str, ...]
    definition: str
    #: Optional second sentence tying the term to the platform's own
    #: evidence, so a definition does not float free of what was measured.
    evidence_note: str = ""


#: The bounded explanation vocabulary.
#:
#: Small by design. This is not an encyclopedia and must not become one:
#: each entry is a term a user plausibly asks about that reaches this route
#: — that is, one the intent resolver does not already own — and each
#: definition states what the measure is without judging any company by it.
#: A term that is absent yields ``UNSUPPORTED_OPEN_ENDED`` and the existing
#: fallback answers, which is the correct outcome for a vocabulary this
#: module does not vouch for.
EXPLANATION_SPECS: tuple[ExplanationSpec, ...] = (
    ExplanationSpec(
        term="order book",
        patterns=(r"order ?book",),
        definition=(
            "An order book is the value of confirmed, unexecuted contracts a "
            "company has won and has yet to bill or deliver. It is a measure "
            "of committed future work, not of revenue already earned."
        ),
        evidence_note=(
            "This platform does not currently extract order-book figures, so "
            "no value for it is stated here."
        ),
    ),
    ExplanationSpec(
        term="promoter",
        patterns=(r"promoters?", r"founders? holding"),
        definition=(
            "A promoter is the person or group that founded or controls a "
            "listed company and typically holds a substantial block of its "
            "shares. Promoter holding is reported as a percentage of issued "
            "capital and is watched because a change in it is a change in "
            "control."
        ),
        evidence_note=(
            "This platform does not currently hold shareholding-pattern "
            "data, so no promoter figure is stated here."
        ),
    ),
    ExplanationSpec(
        term="peer group",
        patterns=(r"peer ?group", r"\bpeers?\b"),
        definition=(
            "A peer group is the set of comparable listed companies a "
            "business is measured against, usually chosen by sector, scale "
            "and business model. Peer comparison puts a ratio in context: a "
            "margin is only high or low relative to something."
        ),
        evidence_note=(
            "Peer figures are not assembled in this context, so no peer "
            "measurement is stated here."
        ),
    ),
    ExplanationSpec(
        term="free float",
        patterns=(r"free ?float",),
        definition=(
            "Free float is the portion of a company's shares available for "
            "public trading, excluding promoter holdings and other locked-in "
            "blocks. Index weights and liquidity are computed on free float "
            "rather than on total issued capital."
        ),
    ),
    ExplanationSpec(
        term="circuit limit",
        patterns=(r"circuit ?(?:limit|breaker|filter)", r"upper ?circuit",
                  r"lower ?circuit"),
        definition=(
            "A circuit limit is the maximum percentage move an exchange "
            "permits in a stock's price in one session. Trading in the stock "
            "halts at the limit; the limit is a market-mechanism control, "
            "not a statement about the company."
        ),
    ),
    ExplanationSpec(
        term="index weight",
        patterns=(r"index ?weight", r"weight in the index"),
        definition=(
            "An index weight is the share of an index's total value a single "
            "stock accounts for, computed from free-float market "
            "capitalisation. It determines how much of an index fund's money "
            "the stock receives."
        ),
    ),
    ExplanationSpec(
        term="fiscal year",
        patterns=(r"fiscal ?year", r"\bfy\b", r"financial ?year"),
        definition=(
            "A fiscal year is the twelve-month period a company reports "
            "against. In India it runs from 1 April to 31 March, so FY25 is "
            "the year ended 31 March 2025. Every figure this platform states "
            "belongs to one fiscal year, and figures from different years "
            "are never combined without saying so."
        ),
    ),
    ExplanationSpec(
        term="basis point",
        patterns=(r"basis ?points?", r"\bbps?\b"),
        definition=(
            "A basis point is one hundredth of one percentage point. A move "
            "from a 12.00% margin to a 12.50% margin is a gain of 50 basis "
            "points. The unit exists because 'a 4% rise' is ambiguous between "
            "4 percentage points and 4% of the previous value."
        ),
    ),
)

_EXPLANATION_PATTERNS: tuple[tuple[ExplanationSpec, re.Pattern[str]], ...] = tuple(
    (spec, re.compile(rf"\b(?:{pattern})\b", re.IGNORECASE))
    for spec in EXPLANATION_SPECS
    for pattern in spec.patterns
)


# ===========================================================================
# Detection of question shape
# ===========================================================================

#: An explicit request for a comparison between two named things. Read from
#: the planner's own ``is_comparison`` vocabulary — this module does not
#: maintain a second one, it only needs to know whether the *question* asked
#: to compare or merely mentioned two metrics.
_COMPARISON_ASK = re.compile(
    r"\bcompar\w*\b|\bversus\b|\bvs\.?\b|\bgap between\b|\bdifference between\b"
    r"|\bhigher than\b|\blower than\b|\bagainst\b|\btulna\b|\bmuqable\b"
    r"|\bmukable\b|तुलना",
    re.IGNORECASE,
)

#: Words that make a question a definition request rather than a data
#: request. "What is ROE?" asks for a definition; "What is the ROE?" asks
#: for a figure — the article is the difference, and the resolver already
#: owns the figure form for every intent it recognises.
_DEFINITION_ASK = re.compile(
    r"\bwhat (?:is|are|does|do|meant by)\b|\bdefine\b|\bmeaning of\b"
    r"|\bwhat is meant\b|\bexplain\b|\bkya (?:hai|hota|hoti)\b|\bmatlab\b"
    r"|\bअर्थ\b",
    re.IGNORECASE,
)

#: A question that actually *asks* which sector, rather than one that
#: merely contains the word. Without this guard a question like "who is the
#: best CEO in the sector?" would be answered with the sector — a true
#: statement that ignores what was asked, which is the same failure as a
#: wrong answer except that nothing downstream can detect it.
#:
#: Both word orders are matched, because both arrive. English puts the
#: interrogative first ("what is the sector"); Hindi and Hinglish put it
#: last, and the Language Adapter's inbound normalisation keeps that order
#: — "सेक्टर क्या है?" becomes "सेक्टर what", not "what सेक्टर". Matching
#: only the English order would make the same question answerable in one
#: language and not another.
_ATTRIBUTE_ASK = re.compile(
    r"\b(?:what|which|whats|what's|kaunsa|konsa|kya)\b[^.?!]{0,30}"
    r"\b(?:sector|industry|line of business|segment|सेक्टर|उद्योग)\b"
    r"|\b(?:sector|industry|सेक्टर|उद्योग)\b[^.?!]{0,20}"
    r"\b(?:what|is|hai|kya|of|for)\b",
    re.IGNORECASE,
)


def _matches(patterns: tuple[tuple, re.Pattern[str]], text: str) -> list:
    """The specs whose terms occur in ``text``, in the order they appear.

    Overlapping matches are resolved in favour of the longer one. That is
    not a refinement, it is the difference between answering the question
    and answering a different one: "ebitda margin" contains "ebitda", so a
    first-match scan reads "compare the ebitda margin and the net margin"
    as a comparison of EBITDA with a margin — two figures in different
    units, and the wrong two.

    The longest span wins, ties break on earliest position, and the result
    is then re-sorted by position so the answer follows the order the user
    asked things in. Deterministic for a given question, which is the whole
    requirement.
    """
    hits: list[tuple[int, int, object]] = []
    for spec, pattern in patterns:
        for match in pattern.finditer(text):
            hits.append((match.start(), match.end(), spec))

    hits.sort(key=lambda hit: (-(hit[1] - hit[0]), hit[0]))
    accepted: list[tuple[int, int, object]] = []
    for start, end, spec in hits:
        if any(start < kept_end and kept_start < end
               for kept_start, kept_end, _ in accepted):
            continue  # subsumed by a longer term already accepted
        accepted.append((start, end, spec))

    accepted.sort(key=lambda hit: hit[0])
    found: list = []
    seen: set[int] = set()
    for _, _, spec in accepted:
        if id(spec) not in seen:
            seen.add(id(spec))
            found.append(spec)
    return found


# ===========================================================================
# The engine
# ===========================================================================


class InternalOpenEndedEngine:
    """Answers an ``INTERNAL_REASONING`` plan from existing evidence, or refuses.

    Stateless and reusable: it holds no session, caches nothing and writes
    nothing. It is constructed once per analyst, exactly like the composer.
    """

    # ---------------------------------------------------------- entry point
    def answer(
        self, plan: QuestionPlan, context: GroundedContext,
    ) -> InternalAnswer:
        """An internal answer for ``plan``, or a refusal with a reason.

        Never raises for a question it simply cannot answer — that is what
        :attr:`InternalAnswerStatus.NOT_SUPPORTED` is for. An unexpected
        internal failure is caught at the analyst boundary so that a bug
        here costs the user a fallback rather than their answer.
        """
        # Route ownership. This module is the INTERNAL_REASONING layer and
        # nothing else; any other route still belongs to its existing owner.
        if plan.execution_route is not ExecutionRoute.INTERNAL_REASONING:
            return self._refuse(
                OpenEndedCapability.UNSUPPORTED_OPEN_ENDED,
                "the plan is not routed to internal reasoning",
            )

        # Subject safety, before any evidence is read. A question naming
        # several companies is not resolved by picking one: the guess would
        # be invisible in the output.
        #
        # ``UNRESOLVED`` is deliberately NOT refused here. It means the
        # question named nobody, not that the subject is unknown: the
        # context was bound to exactly one company before this analyst
        # existed, and using it is the rule the single-intent path has
        # always applied and that Part 2C applies. Company identity is
        # decided in one place — the analyst's
        # ``_company_identity_is_safe``, which this layer is called behind —
        # so a second, stricter rule here would be a second opinion about
        # which company an answer is about.
        if plan.entity.status is EntityStatus.AMBIGUOUS:
            return self._refuse(
                OpenEndedCapability.AMBIGUOUS,
                "the question names more than one company and this layer "
                "does not choose between them",
                missing=(
                    (
                        f"entity: disambiguate between "
                        f"{', '.join(plan.entity.candidates)}"
                        if plan.entity.candidates else
                        "entity: more than one company was named"
                    ),
                ),
            )

        # Detection runs over the raw question and the planner's normalised
        # English form together, which is what the planner itself does for
        # its own vocabulary checks. Neither alone is sufficient: Hinglish
        # keeps its English technical terms in the raw text, while a
        # Devanagari question only becomes recognisable after normalisation.
        # They are joined with a sentence break rather than a space so a
        # pattern cannot match across the boundary and invent a phrase that
        # appears in neither.
        detection_text = (
            f"{plan.original_question}. {plan.normalized_question}"
        )
        return self._answer_for(plan, context, detection_text)

    # ---------------------------------------------------------- dispatch
    def _answer_for(
        self, plan: QuestionPlan, context: GroundedContext, question: str,
    ) -> InternalAnswer:
        """Select a capability and execute it.

        ``question`` is the detection text: the raw question and the
        planner's normalised English form joined at a sentence break, so
        vocabulary written for either reaches the same capability.

        Order is the contract: a comparison the user asked for outranks a
        definition of a word inside it, and both outrank the open-ended
        refusal. Capability is chosen from the *question's* shape, never
        from which evidence happens to be present — selecting by available
        data would answer a different question than was asked.
        """
        metrics = _matches(_METRIC_PATTERNS, question)
        explanations = _matches(_EXPLANATION_PATTERNS, question)
        wants_comparison = bool(_COMPARISON_ASK.search(question))

        # An attribute counts only when the question actually asks for it.
        # "Who is the best CEO in the sector?" contains the word "sector"
        # and is not a question about the sector; answering it with the
        # sector would be a true sentence that ignores what was asked, and
        # no downstream check could tell.
        attributes = (
            _matches(_ATTRIBUTE_PATTERNS, question)
            if _ATTRIBUTE_ASK.search(question) else []
        )

        # A metric named twice is not a comparison, it is an ambiguity:
        # "compare the debt and the debt" has no two sides to state.
        if wants_comparison and len(metrics) >= 2:
            return self._comparison(plan, context, metrics, attributes)

        if wants_comparison and len(metrics) == 1:
            # One metric and a comparison word. The other side was not
            # identified — either it was not named, or it was named by a
            # word that maps to more than one published figure ("debt" is
            # both gross and net). Neither is resolved by choosing: the
            # choice would be invisible in the output.
            return self._refuse(
                OpenEndedCapability.AMBIGUOUS,
                f"a comparison was requested but its two sides could not "
                f"both be identified; only {metrics[0].label} was, and the "
                f"other is not guessed",
                missing=(f"comparison: the second figure to compare "
                         f"{metrics[0].label} with",),
            )

        # A definition request is answered from the bounded vocabulary even
        # when metrics are named, because "what is meant by net margin" asks
        # what the measure is, not what it equals.
        if explanations and _DEFINITION_ASK.search(question):
            return self._explanation(explanations[0])

        if attributes:
            return self._fact_lookup_attribute(plan, context, attributes)

        if explanations:
            return self._explanation(explanations[0])

        if metrics:
            # Figures were named but the question did not ask to compare
            # them. Answering anyway would be a multi-figure dump nobody
            # requested, and a single-figure answer to a question the
            # resolver already declined would be answering something the
            # question did not ask.
            #
            # This is why there is no single-metric fact lookup here: every
            # level figure in ``METRIC_SPECS`` is also in the planner's
            # financial vocabulary, so such a question is classified
            # UNSUPPORTED and routed to DECLINE — it never reaches this
            # layer. Level-figure questions stay on the existing provider
            # path; see the Part 2D document's limitations.
            return self._refuse(
                OpenEndedCapability.UNSUPPORTED_OPEN_ENDED,
                "figures were named but the question did not ask for a "
                "comparison between them",
            )

        return self._refuse(
            OpenEndedCapability.UNSUPPORTED_OPEN_ENDED,
            "no bounded internal capability matches this question",
        )

    # ------------------------------------------------------- capabilities
    def _fact_lookup_attribute(
        self, plan: QuestionPlan, context: GroundedContext,
        attributes: list[AttributeSpec],
    ) -> InternalAnswer:
        """One non-numeric company attribute, or a refusal when absent."""
        spec = attributes[0]
        sentence, citation, operation = self._attribute_segment(spec, context)
        if citation is None:
            return self._refuse(
                OpenEndedCapability.FACT_LOOKUP,
                f"the platform holds no {spec.label.lower()} for this company",
                missing=(spec.key,),
                operations=() if operation is None else (operation,),
            )
        return InternalAnswer(
            status=InternalAnswerStatus.ANSWERED,
            capability=OpenEndedCapability.FACT_LOOKUP,
            content=(
                f"{sentence} The classification is the one recorded on the "
                f"company's own platform record; it is not inferred here, "
                f"and no peer or sector judgement is drawn from it."
            ),
            derived_citations=(citation,),
            operations=(operation,) if operation is not None else (),
            reason=f"{spec.label} was read from the grounded context",
        )

    @staticmethod
    def _attribute_value(spec: AttributeSpec, context: GroundedContext) -> str | None:
        """The authoritative value for an attribute spec, or ``None``."""
        if spec.key == "company_sector":
            sector = (context.sector or "").strip()
            return sector or None
        return None  # pragma: no cover - table has one row

    def _explanation(self, spec: ExplanationSpec) -> InternalAnswer:
        """A definition from the bounded vocabulary. Needs no company data."""
        content = spec.definition
        if spec.evidence_note:
            content = f"{content} {spec.evidence_note}"
        content = (
            f"{content} This is a definition of the term, not a measurement "
            f"of any company."
        )
        return InternalAnswer(
            status=InternalAnswerStatus.ANSWERED,
            capability=OpenEndedCapability.EXPLANATION,
            content=content,
            reason=f"the term '{spec.term}' is in the bounded internal "
                   f"explanation vocabulary",
        )

    def _comparison(
        self, plan: QuestionPlan, context: GroundedContext,
        metrics: list[MetricSpec], attributes: list[AttributeSpec],
    ) -> InternalAnswer:
        """Two metrics of the same company, stated with their measurements.

        This is a measurement, never a ranking. No "better", "worse",
        "best" or "winner" is produced, because the platform's own
        guardrails treat an unhedged judgement as a violation and because a
        comparison of two ratios is not a verdict on a company.

        ``attributes`` carries anything else the same question asked for -
        "compare revenue and profit, and what is the sector?" asks for both.
        It is appended rather than ignored, because silently answering one
        half of a question is the partial answer this layer exists to
        prevent; when an attribute cannot be served it is said to be
        unavailable instead of being dropped.
        """
        left_spec, right_spec = metrics[0], metrics[1]
        left = self._find(context, left_spec.key)
        right = self._find(context, right_spec.key)

        missing = tuple(
            spec.key for spec, citation in
            ((left_spec, left), (right_spec, right)) if citation is None
        )
        if missing:
            return self._refuse(
                OpenEndedCapability.COMPARISON,
                "the comparison cannot be completed from the current "
                "evidence; no figure is estimated to complete it",
                missing=missing,
            )

        # A conflict is a genuine disagreement between two citations that
        # claim the same key. Answering from either would be a coin toss the
        # reader cannot see, so the question is handed back instead.
        conflict = self._conflict(context, (left_spec.key, right_spec.key))
        if conflict:
            return self._refuse(
                OpenEndedCapability.COMPARISON,
                conflict, missing=(conflict,),
            )

        operations: list[InternalOperation] = [
            InternalOperation(
                kind=OperationKind.LOOKUP, inputs=(left_spec.key,),
                output_key=left_spec.key, label=left_spec.label,
                result=_as_float(left.value), unit=left.unit,
            ),
            InternalOperation(
                kind=OperationKind.LOOKUP, inputs=(right_spec.key,),
                output_key=right_spec.key, label=right_spec.label,
                result=_as_float(right.value), unit=right.unit,
            ),
        ]

        used = (left, right)
        derived: list[Citation] = []
        sentences = [
            f"{left_spec.label} for {context.name} is "
            f"{_render_value(left)}{self._year(left)} [{left_spec.key}], and "
            f"{right_spec.label.lower()} is {_render_value(right)}"
            f"{self._year(right)} [{right_spec.key}]."
        ]

        same_unit = left.unit == right.unit and left.unit not in ("", "x")
        if same_unit:
            # Both figures are expressed in the same unit, so a difference
            # and a ratio are meaningful. Both are published as citations.
            diff_key = f"derived_difference_{left_spec.key}_{right_spec.key}"
            difference = _difference(_as_float(left.value), _as_float(right.value))
            operations.append(InternalOperation(
                kind=OperationKind.DIFFERENCE,
                inputs=(left_spec.key, right_spec.key),
                output_key=diff_key,
                label=f"Difference between {left_spec.label} and "
                      f"{right_spec.label}",
                result=difference, unit=left.unit,
                reason="" if difference is not None
                else "the difference is undefined for these inputs",
            ))
            if difference is not None:
                derived.append(Citation(
                    key=diff_key,
                    label=f"Difference between {left_spec.label} and "
                          f"{right_spec.label}",
                    kind=EvidenceKind.RATIO, value=difference, unit=left.unit,
                    source="internal arithmetic on platform figures",
                    fiscal_year=left.fiscal_year,
                ))
                sentences.append(
                    f"The difference between them is "
                    f"{_render_value(derived[-1])} [{diff_key}]."
                )

            ratio_key = f"derived_ratio_{left_spec.key}_{right_spec.key}"
            ratio = safe_div(_as_float(left.value), _as_float(right.value))
            operations.append(InternalOperation(
                kind=OperationKind.RATIO,
                inputs=(left_spec.key, right_spec.key),
                output_key=ratio_key,
                label=f"{left_spec.label} as a multiple of {right_spec.label}",
                result=ratio, unit="x",
                reason="" if ratio is not None
                else "the ratio is undefined because the second figure is zero",
            ))
            if ratio is not None:
                derived.append(Citation(
                    key=ratio_key,
                    label=f"{left_spec.label} as a multiple of "
                          f"{right_spec.label}",
                    kind=EvidenceKind.RATIO, value=ratio, unit="x",
                    source="internal arithmetic on platform figures",
                    fiscal_year=left.fiscal_year,
                ))
                sentences.append(
                    f"Expressed as a multiple, {left_spec.label.lower()} is "
                    f"{ratio:,.2f} times {right_spec.label.lower()} "
                    f"[{ratio_key}]."
                )
        else:
            # Different units: the two measurements are both stated, and the
            # arithmetic is explicitly withheld rather than performed on
            # figures that cannot be subtracted.
            sentences.append(
                f"The two are expressed in different units "
                f"({_unit_name(left.unit)} and {_unit_name(right.unit)}), so "
                f"no difference or multiple between them is computed here — "
                f"subtracting them would not be a meaningful figure."
            )

        sentences.append(
            "Both figures are the platform's own. This is a measurement, not "
            "a ranking: the two are reported side by side and no verdict is "
            "drawn from placing them together."
        )

        # Anything else the same question asked for, appended rather than
        # dropped. An attribute this context does not hold is reported as
        # unavailable, which keeps the answer whole without inventing it.
        extra_missing: list[str] = []
        for spec in attributes:
            segment, citation, operation = self._attribute_segment(
                spec, context,
            )
            sentences.append(segment)
            if citation is not None:
                derived.append(citation)
            if operation is not None:
                operations.append(operation)
            if citation is None:
                extra_missing.append(spec.key)

        capability = (
            OpenEndedCapability.MULTI_STEP_ANALYSIS
            if len(operations) > 3 or attributes
            else OpenEndedCapability.COMPARISON
        )
        return InternalAnswer(
            status=InternalAnswerStatus.ANSWERED,
            capability=capability,
            content=" ".join(sentences),
            used_citations=used,
            derived_citations=tuple(derived),
            missing=tuple(extra_missing),
            operations=tuple(operations[:MAX_OPERATIONS]),
            reason=(
                f"two figures of the same company were compared from the "
                f"grounded context ({left_spec.key}, {right_spec.key})"
            ),
        )

    def _attribute_segment(
        self, spec: AttributeSpec, context: GroundedContext,
    ) -> tuple[str, Citation | None, InternalOperation | None]:
        """One attribute's sentence, its citation and its operation.

        Returns the sentence in both cases so the caller can never silently
        omit a part of the question: when the value is absent the sentence
        says so, and no citation is produced for a figure that does not
        exist.
        """
        value = self._attribute_value(spec, context)
        if not value:
            return (
                f"The {spec.label.lower()} is not available in the "
                f"platform's current evidence for {context.name}, and it is "
                f"not inferred here.",
                None,
                InternalOperation(
                    kind=OperationKind.LOOKUP, inputs=(spec.key,),
                    output_key=spec.key, label=spec.label, result=None,
                    reason="the attribute is absent from the context",
                    missing=(spec.key,),
                ),
            )
        citation = Citation(
            key=spec.key, label=spec.label, kind=EvidenceKind.KNOWLEDGE,
            value=value, unit="", source="company record",
        )
        return (
            f"{spec.sentence.format(name=context.name, value=value)} "
            f"[{spec.key}]",
            citation,
            InternalOperation(
                kind=OperationKind.LOOKUP, inputs=(spec.key,),
                output_key=spec.key, label=spec.label, result=None,
                reason="non-numeric attribute read from the context",
            ),
        )

    # ------------------------------------------------------------ evidence
    @staticmethod
    def _find(context: GroundedContext, key: str) -> Citation | None:
        """The citation for ``key``, or ``None``.

        Same rule as ``FinancialAnswerEngine._find``: a citation whose value
        is ``None`` is an unavailable figure, not a zero, and never a blank
        to be filled.
        """
        for citation in context.citations:
            if citation.key == key and citation.value is not None:
                return citation
        return None

    @staticmethod
    def _conflict(context: GroundedContext, keys: tuple[str, ...]) -> str:
        """A description of the first key whose citations disagree, else "".

        The context can legitimately hold two citations under one key —
        computed evidence and a retrieved passage, for instance. When their
        numeric values differ, either could be quoted and the reader could
        not tell which was chosen, so the question is refused instead.
        """
        for key in keys:
            values = {
                round(float(c.value), 6)
                for c in context.citations
                if c.key == key and isinstance(c.value, (int, float))
            }
            if len(values) > 1:
                return (
                    f"the evidence holds conflicting values for '{key}' "
                    f"({', '.join(f'{v:g}' for v in sorted(values))}); "
                    f"this layer does not choose between them"
                )
        return ""

    # ------------------------------------------------------------- helpers
    @staticmethod
    def _year(citation: Citation) -> str:
        return f" for FY{str(citation.fiscal_year)[-2:]}" \
            if citation.fiscal_year else ""

    @staticmethod
    def _refuse(
        capability: OpenEndedCapability,
        reason: str,
        *,
        missing: tuple[str, ...] = (),
        operations: tuple[InternalOperation, ...] = (),
    ) -> InternalAnswer:
        """A refusal. Carries no text, so no partial answer can escape.

        ``operations`` records what was attempted before the refusal, so a
        log line can show how far the layer got. A refusal never carries
        content: the caller falls back to the existing provider path, and
        there is no half-answer for it to inherit.
        """
        return InternalAnswer(
            status=InternalAnswerStatus.NOT_SUPPORTED,
            capability=capability,
            content="",
            missing=missing,
            operations=operations,
            reason=reason,
        )


# ===========================================================================
# Rendering — identical to the existing engines, by construction
# ===========================================================================


def _render_value(citation: Citation) -> str:
    """A citation's value, formatted exactly as ``Citation.render()`` shows it.

    Re-implementing the platform's two formatting rules rather than calling
    ``Citation.render`` keeps the output a bare figure: ``render`` prefixes
    the label, and the sentence here supplies its own wording. The rules
    themselves are the same, which is what lets the citation audit match
    every number in the answer back to its source.
    """
    if citation.value is None:
        return "unavailable"
    if isinstance(citation.value, float):
        text = (f"{citation.value * 100:,.2f}" if citation.unit == "%"
                else f"{citation.value:,.2f}")
    else:
        text = str(citation.value)
    if citation.unit == "x":
        return f"{text}x"
    unit = f" {citation.unit}" if citation.unit else ""
    return f"{text}{unit}"


def _as_float(value: float | str | None) -> float | None:
    """A citation's numeric value, or ``None`` for anything non-numeric."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _difference(a: float | None, b: float | None) -> float | None:
    """``a - b``, or ``None`` when either input is missing."""
    if a is None or b is None:
        return None
    return a - b


_UNIT_NAMES = {
    "₹ cr": "₹ crore", "₹ lakh": "₹ lakh", "₹": "₹ per share",
    "%": "percentage points", "x": "a multiple", "": "no unit",
}


def _unit_name(unit: str) -> str:
    return _UNIT_NAMES.get(unit, unit or "no unit")
