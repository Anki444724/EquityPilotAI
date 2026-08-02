"""Domain rules for temporal memory — §8 and §12 of the Knowledge Engine brief.

The vault already remembers *what* a company is. This layer remembers *how it
changed*, one dated observation per fiscal year, so the AI can answer "how has
management guidance changed over the last ten years?" from memory rather than
by re-reading a decade of annual reports.

Two rules make this more than a list of notes.

**An observation is anchored to a fiscal year, not to a write time.** FY2025's
observation is about FY2025 forever, no matter when it was generated or
regenerated. Ordering a narrative by ingestion time is how a late-arriving
2019 report ends up presented as the latest thinking.

**Each year is scored against the previous year's expectations.** An
observation that says "management guidance is credible" is an opinion; one
that says "management guided to X in FY2025 and FY2026 reports X delivered" is
a track record. The verdict is stored as a distinct field rather than left
inside prose, because a track record is only useful if it can be counted.

The verdict deliberately admits `NOT_ASSESSABLE`. Most Indian filings contain
no explicit numeric guidance, and forcing a met/missed judgement where none is
supportable would manufacture a credibility score out of nothing — the exact
failure mode this layer exists to avoid.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class GuidanceVerdict(StrEnum):
    """How the prior year's stated expectations turned out."""

    DELIVERED = "delivered"
    PARTIALLY_DELIVERED = "partially_delivered"
    MISSED = "missed"
    #: No prior-year guidance specific enough to judge, or no prior year at
    #: all. Not a failure — the common case for Indian filings.
    NOT_ASSESSABLE = "not_assessable"


#: Contribution to a management-credibility score. `NOT_ASSESSABLE` is absent
#: on purpose: an unjudgeable year must not drag a score toward the middle, it
#: must be excluded from the denominator entirely.
VERDICT_WEIGHT: dict[GuidanceVerdict, float] = {
    GuidanceVerdict.DELIVERED: 1.0,
    GuidanceVerdict.PARTIALLY_DELIVERED: 0.5,
    GuidanceVerdict.MISSED: 0.0,
}


class ObservationTrend(StrEnum):
    """Direction of a tracked dimension, year on year."""

    IMPROVING = "improving"
    STABLE = "stable"
    DETERIORATING = "deteriorating"
    UNKNOWN = "unknown"


#: The dimensions a yearly observation tracks.
#:
#: Fixed rather than free-text so a ten-year series is comparable: "management
#: quality" in FY2018 must mean the same axis as in FY2027, or the timeline is
#: a pile of unrelated sentences. Each maps to something the platform can
#: corroborate from its own financial facts, which is what lets a claim be
#: checked rather than merely recorded.
TRACKED_DIMENSIONS: tuple[str, ...] = (
    "management_quality",
    "capex",
    "debt",
    "roce",
    "moat",
    "growth",
    "margins",
    "capital_allocation",
)

#: Below this, an observation is retained as history but not served as the
#: current view of that year. Mirrors the vault's own threshold so a reader
#: cannot get a low-confidence observation from one API and not another.
MIN_SERVABLE_CONFIDENCE = 0.35


#: Dimensions where "improving" means the measured quantity goes DOWN.
#:
#: Not cosmetic. "Debt improving" means debt fell, so checking a narrative
#: claim against the accounts with a single polarity marks every genuine
#: deleveraging as a contradiction and every increase in borrowing as
#: agreement — precisely inverted on the one dimension a credit analyst cares
#: most about.
INVERSE_DIMENSIONS: frozenset[str] = frozenset({"debt"})


@dataclass(slots=True)
class DimensionReading:
    """One tracked dimension in one year."""

    dimension: str
    trend: ObservationTrend = ObservationTrend.UNKNOWN
    #: The sentence supporting the reading. Without it the trend is an
    #: assertion no reader can check.
    detail: str | None = None
    #: Corroborating figure from the platform's own financial facts, where the
    #: dimension has one. `roce` improving alongside a computed ROCE that fell
    #: is a contradiction worth surfacing, not smoothing over.
    metric_value: float | None = None
    metric_prior: float | None = None

    @property
    def contradicts_metric(self) -> bool:
        """True when the narrative trend disagrees with the measured change.

        Reported rather than corrected: the filing may be discussing a segment,
        a normalised figure, or a different definition. A flag invites a
        reader to look; silently overriding the model would hide a real
        disagreement between narrative and accounts.
        """
        if self.metric_value is None or self.metric_prior is None:
            return False
        rose = self.metric_value > self.metric_prior
        fell = self.metric_value < self.metric_prior
        if self.dimension in INVERSE_DIMENSIONS:
            # Lower is better: "debt improving" should coincide with a fall.
            if self.trend == ObservationTrend.IMPROVING:
                return rose
            if self.trend == ObservationTrend.DETERIORATING:
                return fell
            return False
        if self.trend == ObservationTrend.IMPROVING:
            return fell
        if self.trend == ObservationTrend.DETERIORATING:
            return rose
        return False


@dataclass(slots=True)
class YearObservation:
    """What the platform concluded about one company in one fiscal year."""

    fiscal_year: int
    findings: list[str] = field(default_factory=list)
    dimensions: list[DimensionReading] = field(default_factory=list)
    confidence: float = 0.0

    #: What this year said it would do — the raw material for next year's
    #: verdict. Absent when the filing gives no forward statement.
    guidance: str | None = None

    #: How the PREVIOUS year's guidance turned out, judged from this year's
    #: filings.
    prior_verdict: GuidanceVerdict = GuidanceVerdict.NOT_ASSESSABLE
    verdict_reasoning: str | None = None

    citations: list[str] = field(default_factory=list)
    generated_by: str | None = None

    @property
    def servable(self) -> bool:
        return self.confidence >= MIN_SERVABLE_CONFIDENCE

    def render(self) -> str:
        """The compact form the brief shows, used in prompts and timelines."""
        lines = [f"FY{self.fiscal_year}"]
        lines.extend(f"  {finding}" for finding in self.findings)
        if self.prior_verdict != GuidanceVerdict.NOT_ASSESSABLE:
            lines.append(f"  Prior-year guidance: {self.prior_verdict.value}")
        lines.append(f"  Confidence: {round(self.confidence * 100)}%")
        return "\n".join(lines)


def credibility_score(
    observations: list[YearObservation],
) -> tuple[float | None, int]:
    """Management credibility across the series, and the years it rests on.

    Returns `(None, 0)` when no year is assessable, rather than a neutral 0.5.
    A company nobody can grade is not an average company — it is an ungraded
    one, and presenting 50% would be inventing a track record.
    """
    weights = [
        VERDICT_WEIGHT[o.prior_verdict]
        for o in observations
        if o.prior_verdict in VERDICT_WEIGHT
    ]
    if not weights:
        return None, 0
    return round(sum(weights) / len(weights), 4), len(weights)


def trend_of(current: float | None, prior: float | None,
             *, tolerance: float = 0.02) -> ObservationTrend:
    """Direction of a measured quantity, with a dead band.

    The dead band matters: without it a 0.1% move in ROCE reads as
    "improving", and a ten-year timeline becomes an alternating sequence of
    improvements and deteriorations that describes noise rather than a trend.
    """
    if current is None or prior is None:
        return ObservationTrend.UNKNOWN
    if prior == 0:
        return ObservationTrend.UNKNOWN
    change = (current - prior) / abs(prior)
    if change > tolerance:
        return ObservationTrend.IMPROVING
    if change < -tolerance:
        return ObservationTrend.DETERIORATING
    return ObservationTrend.STABLE
