"""Primitives for the Explainable AI Scoring Engine (3.0).

The engine's governing rule is stated in the brief and enforced here: **a
rating is never generated from a prompt or an LLM opinion.** Every number in
this module is arithmetic over observed inputs. A language model may write
prose *about* a score, and that prose is carried alongside as commentary, but
it can never move the figure — :class:`FactorScore` has no field a model
writes that participates in the arithmetic, and a test asserts it.

Three ideas separate this from Module 5's thirteen-category engine.

**Evidence is structural, not decorative.** A factor without a citation is not
a scored factor; it is an admitted gap. :class:`FactorScore` therefore carries
``citations`` as a first-class field and ``origin`` records whether the input
was read from a filing, derived, or simply absent. A panel that shows a number
without saying where it came from is exactly the black box the brief forbids.

**Missing is a state, not a zero.** An unobservable factor scores the neutral
midpoint and drops the module's coverage. Scoring it zero would punish a
company for the platform's ignorance; scoring it ten would flatter it. Both are
lies, and the coverage figure is what tells the reader which one they are
closer to.

**Every version is kept.** Nothing in this module mutates. Results are frozen
dataclasses, persisted append-only, so "what did we say in March, and on what
evidence?" stays answerable after the evidence itself has been superseded.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any, Sequence

# ---------------------------------------------------------------------------
# Scales
# ---------------------------------------------------------------------------

#: Factors and modules are scored 0-10 before weighting. The composite is
#: rescaled to 0-100 once, at the end, so intermediate arithmetic never mixes
#: two scales — the defect that made Module 5's `score_pct` (a fraction) and
#: `overall_score` (a percentage) silently incomparable.
SCORE_MIN = 0.0
SCORE_MAX = 10.0

#: Awarded when an input cannot be observed. The midpoint, deliberately.
NEUTRAL_SCORE = 5.0


def clamp(value: float) -> float:
    return max(SCORE_MIN, min(SCORE_MAX, value))


class Origin(StrEnum):
    """Where a factor's input came from. Drives coverage, never the score."""

    #: Read from a filing, an exchange disclosure or a reported statement.
    REPORTED = "reported"
    #: Computed from reported inputs (a CAGR, a ratio, a trend).
    DERIVED = "derived"
    #: Read from the knowledge vault or temporal memory — an assertion the
    #: platform extracted from a document and can cite back to a page.
    EXTRACTED = "extracted"
    #: Reference data (sector, index membership, market cap) from the universe
    #: import rather than from a filing.
    REFERENCE = "reference"
    #: Nothing observable. Scored NEUTRAL_SCORE; the gap is reported.
    MISSING = "missing"


#: How much each origin contributes to a module's coverage. Reported filings
#: are worth full weight; an extracted assertion is worth slightly less because
#: extraction can misread; reference data is thinner still because it describes
#: the company rather than measuring it.
ORIGIN_COVERAGE: dict[Origin, float] = {
    Origin.REPORTED: 1.00,
    Origin.DERIVED: 0.95,
    Origin.EXTRACTED: 0.70,
    Origin.REFERENCE: 0.55,
    Origin.MISSING: 0.00,
}


class Rating(StrEnum):
    """The brief's six-band rating scale."""

    A_PLUS = "A+"
    A = "A"
    BBB = "BBB"
    BB = "BB"
    B = "B"
    C = "C"


class Recommendation(StrEnum):
    """The brief's five-step recommendation scale."""

    STRONG_BUY = "Strong Buy"
    BUY = "Buy"
    HOLD = "Hold"
    REDUCE = "Reduce"
    AVOID = "Avoid"


#: Composite thresholds, best-first. Deliberately not evenly spaced: the gap
#: between an 82 and an 88 is a much larger difference in franchise quality
#: than the gap between a 52 and a 58, because the underlying factor
#: distributions are compressed at the top.
RATING_BANDS: tuple[tuple[float, Rating, str], ...] = (
    (85.0, Rating.A_PLUS, "Exceptional franchise, evidence-complete"),
    (75.0, Rating.A, "High quality with a durable advantage"),
    (63.0, Rating.BBB, "Sound business with identifiable weaknesses"),
    (50.0, Rating.BB, "Average quality; the case depends on execution"),
    (38.0, Rating.B, "Weak fundamentals or material unresolved concerns"),
    (0.0, Rating.C, "Distressed, governance-impaired or unassessable"),
)

#: Recommendation thresholds on the composite, before the guardrails in
#: `app.domain.ai_scoring.framework` are applied.
#:
#: **The Hold threshold is pinned to 50.0 deliberately, and must stay there.**
#: A company for which nothing is observable scores every factor at the
#: neutral midpoint of 5.0/10, which composites to exactly 50.0. With the Hold
#: floor at 52 that company received "Reduce" — a directional sell call
#: derived from having no evidence whatsoever, produced by arithmetic rather
#: than by any observation about the business. The thin-evidence guardrail did
#: not save it, because a guardrail may only cap a recommendation and never
#: raise one.
#:
#: 50.0 is the "we know nothing" point by construction, so it must map to the
#: neutral recommendation. A score below 50 now means observed weakness rather
#: than absent data. Found by `test_an_empty_company_still_scores_with_gaps_reported`.
RECOMMENDATION_BANDS: tuple[tuple[float, Recommendation], ...] = (
    (80.0, Recommendation.STRONG_BUY),
    (68.0, Recommendation.BUY),
    (50.0, Recommendation.HOLD),
    (40.0, Recommendation.REDUCE),
    (0.0, Recommendation.AVOID),
)


def rating_for(composite: float) -> tuple[Rating, str]:
    for threshold, rating, description in RATING_BANDS:
        if composite >= threshold:
            return rating, description
    return Rating.C, RATING_BANDS[-1][2]


def recommendation_for(composite: float) -> Recommendation:
    for threshold, recommendation in RECOMMENDATION_BANDS:
        if composite >= threshold:
            return recommendation
    return Recommendation.AVOID


#: The composite a company scores when nothing at all is observable: every
#: factor takes NEUTRAL_SCORE, and the framework weights sum to 100.
NEUTRAL_COMPOSITE = NEUTRAL_SCORE / SCORE_MAX * 100.0

# An absence of evidence must never read as a directional call. Asserted at
# import so a future edit to the bands cannot silently reintroduce it.
assert recommendation_for(NEUTRAL_COMPOSITE) is Recommendation.HOLD, (
    "a company with no observable evidence composites to "
    f"{NEUTRAL_COMPOSITE} and must map to Hold, not "
    f"{recommendation_for(NEUTRAL_COMPOSITE).value}"
)


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------

class CitationKind(StrEnum):
    """What kind of artefact a citation points at."""

    FILING = "filing"                 # a Document the platform holds
    ANNOUNCEMENT = "announcement"     # a DiscoveredFiling from NSE/BSE/IR
    STATEMENT = "statement"           # a canonical FinancialFact
    VAULT = "vault"                   # a KnowledgeEntry
    SUMMARY = "summary"               # a DocumentSummary
    OBSERVATION = "observation"       # a YearlyObservation
    REFERENCE = "reference"           # universe / exchange reference data
    PEER = "peer"                     # a cross-sectional peer aggregate


@dataclass(frozen=True, slots=True)
class Citation:
    """A pointer to the artefact that supports a factor.

    Everything here is resolvable: a reader given ``kind`` and ``reference``
    can find the underlying row. That is the difference between a citation and
    a footnote that says "internal analysis".
    """

    kind: CitationKind
    #: Human-readable label, e.g. "FY2025 Annual Report, p.48".
    label: str
    #: The stable identifier within `kind` — document id, fact key, entry id.
    reference: str = ""
    document_id: int | None = None
    page: int | None = None
    fiscal_year: int | None = None
    url: str | None = None
    #: Verbatim supporting text where one exists. Never paraphrased.
    excerpt: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value, "label": self.label,
            "reference": self.reference, "document_id": self.document_id,
            "page": self.page, "fiscal_year": self.fiscal_year,
            "url": self.url, "excerpt": self.excerpt,
        }


# ---------------------------------------------------------------------------
# Factors and modules
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class FactorScore:
    """One evaluated factor inside a module.

    The brief requires weight, score, reason, evidence and citations for every
    score. All five are mandatory fields on this object rather than optional
    decoration, so a factor that omits them cannot be constructed.
    """

    key: str
    label: str
    #: 0-10.
    score: float
    #: Relative importance within the module. Need not sum to 1; normalised
    #: at aggregation.
    weight: float
    origin: Origin
    #: Why it scored where it did, in plain English, naming the figure.
    reason: str
    #: The observed input, for display and audit.
    value: float | None = None
    unit: str = ""
    #: A short statement of what was actually looked at.
    evidence: str = ""
    citations: tuple[Citation, ...] = ()
    #: Which service produced the input, so a wrong number is traceable to
    #: one calculation rather than to "the scoring engine".
    computed_by: str = ""

    def __post_init__(self) -> None:
        if not (SCORE_MIN <= self.score <= SCORE_MAX):
            raise ValueError(
                f"factor '{self.key}' scored {self.score}, outside 0-10"
            )
        if self.weight < 0:
            raise ValueError(f"factor '{self.key}' has a negative weight")
        if not self.reason:
            raise ValueError(
                f"factor '{self.key}' has no reason — the engine may not "
                "produce an unexplained number"
            )

    @property
    def coverage(self) -> float:
        return ORIGIN_COVERAGE[self.origin]

    @property
    def is_missing(self) -> bool:
        return self.origin is Origin.MISSING

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key, "label": self.label,
            "score": round(self.score, 4), "weight": self.weight,
            "origin": self.origin.value, "coverage": self.coverage,
            "reason": self.reason, "value": self.value, "unit": self.unit,
            "evidence": self.evidence,
            "citations": [c.as_dict() for c in self.citations],
            "computed_by": self.computed_by,
        }


@dataclass(frozen=True, slots=True)
class ModuleScore:
    """One of the ten framework modules, fully evaluated."""

    key: str
    label: str
    #: The module's fixed framework weight, 0-100 summing to 100 across all ten.
    weight: float
    #: 0-10, weighted mean of the module's factors.
    score: float
    factors: tuple[FactorScore, ...]
    #: Module-level narrative assembled from the factors — deterministic.
    reason: str
    #: Optional LLM commentary. Never participates in the arithmetic.
    ai_commentary: str | None = None

    @property
    def contribution(self) -> float:
        """Points this module contributes to the 0-100 composite."""
        return self.score / SCORE_MAX * self.weight

    @property
    def coverage(self) -> float:
        """Weighted share of this module's inputs that were observable."""
        total = sum(f.weight for f in self.factors)
        if total <= 0:
            return 0.0
        return sum(f.coverage * f.weight for f in self.factors) / total

    @property
    def missing_factors(self) -> tuple[str, ...]:
        return tuple(f.label for f in self.factors if f.is_missing)

    @property
    def citation_count(self) -> int:
        return sum(len(f.citations) for f in self.factors)

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key, "label": self.label, "weight": self.weight,
            "score": round(self.score, 4),
            "contribution": round(self.contribution, 4),
            "coverage": round(self.coverage, 4),
            "reason": self.reason,
            "ai_commentary": self.ai_commentary,
            "missing_factors": list(self.missing_factors),
            "citation_count": self.citation_count,
            "factors": [f.as_dict() for f in self.factors],
        }


# ---------------------------------------------------------------------------
# Probability
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Probability:
    """One forward-looking probability, with its derivation stated."""

    key: str
    label: str
    #: 0-1.
    probability: float
    #: The modules that fed it and their signed influence, so the figure can
    #: be reconstructed by hand.
    drivers: tuple[tuple[str, float], ...]
    reason: str
    #: How far the raw estimate was pulled back toward 50% for thin evidence.
    #: Reported rather than hidden: a 71% on 30% coverage is a different claim
    #: from a 71% on complete filings.
    shrinkage: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key, "label": self.label,
            "probability": round(self.probability, 4),
            "percent": round(self.probability * 100, 1),
            "drivers": [{"module": k, "influence": round(v, 4)}
                        for k, v in self.drivers],
            "reason": self.reason,
            "shrinkage": round(self.shrinkage, 4),
        }


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class AIScoreResult:
    """The complete, explainable output of one scoring run."""

    company_id: str
    ticker: str
    name: str

    #: 0-100.
    overall_score: float
    rating: Rating
    rating_description: str
    recommendation: Recommendation
    recommendation_reason: str

    modules: tuple[ModuleScore, ...]
    probabilities: tuple[Probability, ...]

    #: Weighted share of all inputs that were observable, 0-1.
    coverage: float
    #: Deterministic narrative. Never model-written.
    summary: str
    warnings: tuple[str, ...] = ()
    #: Guardrails that fired and capped the recommendation.
    guardrails: tuple[str, ...] = ()

    #: Version of the framework definition that produced this. A weight change
    #: makes historical scores incomparable, and that must be visible rather
    #: than inferred from the date.
    framework_version: str = ""
    #: SHA256 over the observed inputs. Two runs with the same fingerprint
    #: saw the same world, which is how a no-op recalculation is detected
    #: without overwriting anything.
    input_fingerprint: str = ""

    def module(self, key: str) -> ModuleScore | None:
        return next((m for m in self.modules if m.key == key), None)

    def probability(self, key: str) -> Probability | None:
        return next((p for p in self.probabilities if p.key == key), None)

    @property
    def total_citations(self) -> int:
        return sum(m.citation_count for m in self.modules)

    @property
    def unexplained_factors(self) -> tuple[str, ...]:
        """Factors carrying no reason. Must always be empty — the invariant
        that makes 'never a black box' testable rather than aspirational."""
        return tuple(
            f"{m.key}.{f.key}"
            for m in self.modules for f in m.factors if not f.reason
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "company_id": self.company_id, "ticker": self.ticker,
            "name": self.name,
            "overall_score": round(self.overall_score, 2),
            "rating": self.rating.value,
            "rating_description": self.rating_description,
            "recommendation": self.recommendation.value,
            "recommendation_reason": self.recommendation_reason,
            "coverage": round(self.coverage, 4),
            "summary": self.summary,
            "warnings": list(self.warnings),
            "guardrails": list(self.guardrails),
            "framework_version": self.framework_version,
            "input_fingerprint": self.input_fingerprint,
            "total_citations": self.total_citations,
            "modules": [m.as_dict() for m in self.modules],
            "probabilities": [p.as_dict() for p in self.probabilities],
        }


# ---------------------------------------------------------------------------
# Aggregation helpers
# ---------------------------------------------------------------------------

def aggregate_factors(factors: Sequence[FactorScore]) -> float:
    """Weighted mean of factor scores on the 0-10 scale."""
    if not factors:
        return NEUTRAL_SCORE
    total = sum(f.weight for f in factors)
    if total <= 0:
        return NEUTRAL_SCORE
    return clamp(sum(f.score * f.weight for f in factors) / total)


def band(
    value: float | None,
    bands: Sequence[tuple[float, float]],
    *,
    higher_is_better: bool = True,
) -> float:
    """Map a value onto 0-10 through threshold bands, best-first.

    Bands rather than a fitted curve because analysts reason in thresholds and
    a table can be argued with. A value matching no band scores zero, which is
    correct: it fell below the worst threshold anyone thought worth naming.
    """
    if value is None:
        return NEUTRAL_SCORE
    for threshold, score in bands:
        if (higher_is_better and value >= threshold) or (
            not higher_is_better and value <= threshold
        ):
            return clamp(score)
    return SCORE_MIN


def scale(value: float | None, worst: float, best: float) -> float:
    """Linear interpolation onto 0-10. ``worst`` may exceed ``best``."""
    if value is None:
        return NEUTRAL_SCORE
    if best == worst:
        return NEUTRAL_SCORE
    return clamp((value - worst) / (best - worst) * SCORE_MAX)


def fingerprint(payload: dict[str, Any]) -> str:
    """Stable SHA256 over an input snapshot.

    ``sort_keys`` and ``default=str`` matter: without both, two runs over
    identical data produce different digests because dict ordering and
    datetime repr are not stable, and every recalculation would then look like
    a change.
    """
    encoded = json.dumps(payload, sort_keys=True, default=str,
                         separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
