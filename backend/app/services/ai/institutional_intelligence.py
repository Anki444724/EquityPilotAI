"""Institutional intelligence — the deterministic interpretation layer.

Where the analyst answers a *question*, this engine builds the platform's own
*reading* of a company: what its already-computed evidence says about quality,
growth, risk and valuation, and how the business has changed over the years it
has been observed. Nothing is asked of a model, and no provider is imported,
referenced or called — the output is a pure function of the evidence the
platform already holds.

Four boundaries make that safe.

**It recomputes nothing.** The ``ScoreResult``, the valuation and ratio
citations, and the yearly observations arrive already computed. This layer
reads them and states what they mean; it does not derive a second score, a
second valuation or a second trend model. ``ObservationTrend``,
``DimensionReading``, ``trend_of`` and ``credibility_score`` are the existing
temporal domain and are used as-is.

**The recommendation is not this layer's to make.** ``ScoreResult`` already
carries the platform's recommendation, its own rationale and its own override
rules — valuation, balance-sheet fragility and data confidence. This engine
restates the recommendation verbatim and explains what supports or constrains
it. It never converts a low multiple, a low P/B or a high growth rate into a
call of its own.

**Every claim is labelled and every figure is referenced.** Each signal is a
FACT, a MODEL_OUTPUT, an INTERPRETATION or an OPINION in the platform's
existing :class:`ClaimType` taxonomy, and carries the keys of the citations its
figures live under. Statement text is deliberately free of computed values:
a number stated here would be a second copy that can drift from the citation
the reader audits against.

**Absence is stated, not filled.** A valuation the platform could not compute
is reported unavailable; a company with no temporal memory is said to have
none, rather than described as having stable trends. Everything the platform
could not see is listed in ``limitations``.

This is a foundation. It produces structured signals; composing open-ended
natural-language answers across them is the next layer's job.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

import structlog

from app.domain.ai.types import Citation, ClaimType
from app.domain.knowledge.temporal import (
    MIN_SERVABLE_CONFIDENCE, DimensionReading, ObservationTrend,
    YearObservation, credibility_score, trend_of,
)
from app.domain.scoring.base import NEUTRAL_SCORE, CategoryScore
from app.services.ai.context_builder import GroundedContext
from app.services.scoring.overall_score import (
    EXPENSIVE_VALUATION_SCORE, FRAGILE_BALANCE_SHEET_SCORE,
    LOW_CONFIDENCE_THRESHOLD, ScoreResult,
)

log = structlog.get_logger(__name__)

#: A digit anywhere in a sentence. Used only to keep figure-bearing text out of
#: warnings that have no citation under them — the scoring engine's own warning
#: about missing inputs contains a percentage, and restating it here without
#: the ``confidence`` citation beside it would be an uncited figure.
_HAS_DIGIT = re.compile(r"\d")

#: `CategoryScore.grade_hint` values that count as a strong reading and as a
#: weak one. The banding itself is the domain's own (`Strong`/`Good`/`Adequate`/
#: `Weak`/`Poor`); no new thresholds are introduced here.
_STRONG_HINTS = frozenset({"Strong", "Good"})
_WEAK_HINTS = frozenset({"Weak", "Poor"})

#: Tracked dimensions whose measured counterpart the platform publishes as a
#: citation, and the citation key it lives under. The mapping is explicit
#: rather than derived from the metric key so a signal can only ever point at
#: evidence that actually exists — a risk signal with no citation under it
#: would be an assertion the reader cannot check. `debt_equity` is scored by
#: the risk category but has no published citation, so it is not mapped.
RISK_METRIC_CITATIONS: dict[str, str] = {
    "net_debt_ebitda": "net_debt_ebitda",
    "interest_coverage": "interest_coverage",
    "current_ratio": "current_ratio",
    "altman_z": "altman_z",
}

#: Display labels for the tracked dimensions. Presentation only — the stored
#: key is what the series is written and compared under.
DIMENSION_LABELS: dict[str, str] = {
    "management_quality": "Management quality",
    "capex": "Capex",
    "debt": "Debt",
    "roce": "ROCE",
    "moat": "Moat",
    "growth": "Growth",
    "margins": "Margins",
    "capital_allocation": "Capital allocation",
}

#: How the measured counterpart moved, in plain words.
#:
#: `trend_of` reports the direction of the *quantity*, not its desirability:
#: on an inverse dimension such as debt, a rising figure yields IMPROVING, and
#: whether that is good or bad is the job of the inverse rule behind
#: `DimensionReading.contradicts_metric`. The wording below follows the
#: quantity, and the contradiction flag carries the judgement.
_METRIC_MOVE: dict[ObservationTrend, str] = {
    ObservationTrend.IMPROVING: "a higher figure than the previous year",
    ObservationTrend.DETERIORATING: "a lower figure than the previous year",
    ObservationTrend.STABLE: "no material change on the previous year",
    ObservationTrend.UNKNOWN: "no comparable previous figure",
}

#: Temporal signal kinds.
DIMENSION = "dimension"
CONTRADICTION = "contradiction"
REPEATED_DETERIORATION = "repeated_deterioration"
REPEATED_IMPROVEMENT = "repeated_improvement"
CREDIBILITY = "credibility"


def _label(dimension: str) -> str:
    """Display label for a tracked dimension. The stored key is unchanged."""
    return DIMENSION_LABELS.get(dimension, dimension.replace("_", " ").capitalize())


def _spoken(dimension: str) -> str:
    """The label as it reads inside a sentence.

    Acronyms keep their case ("ROCE", not "roce"); word labels are lowercased
    because they appear mid-clause rather than at the start of one.
    """
    label = _label(dimension)
    return label if label.isupper() else label.lower()


@dataclass(slots=True)
class _Notes:
    """Caveats collected while the axes are read, in the order they are found.

    Ordered and de-duplicated rather than a set: two signals can raise the same
    caveat, and a reader should see each one once, where it first arose. Order
    is insertion order, which keeps the whole report deterministic.
    """

    warnings: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)

    @staticmethod
    def _add(items: list[str], text: str) -> None:
        text = text.strip()
        if text and text not in items:
            items.append(text)

    def warn(self, text: str) -> None:
        self._add(self.warnings, text)

    def limit(self, text: str) -> None:
        self._add(self.limitations, text)


@dataclass(frozen=True, slots=True)
class IntelligenceSignal:
    """One deterministic reading of one axis, with its support attached."""

    key: str
    label: str
    #: The classification, e.g. ``strong``, ``constrained``, ``BUY``.
    status: str
    #: What the platform concluded, in one sentence. Contains no computed
    #: value: the figures live in the citations this signal points at.
    statement: str
    claim_type: ClaimType
    #: Citation keys the statement rests on, in the order they were checked.
    evidence: tuple[str, ...] = ()
    #: The source's own confidence where it carries one (a category's
    #: provenance-weighted confidence, or the score's).
    confidence: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key, "label": self.label, "status": self.status,
            "statement": self.statement, "claim_type": self.claim_type.value,
            "evidence": list(self.evidence), "confidence": self.confidence,
        }


@dataclass(frozen=True, slots=True)
class TemporalSignal:
    """One deterministic reading of the stored yearly series."""

    kind: str
    dimension: str | None
    status: str
    statement: str
    #: The fiscal years the reading rests on, oldest first.
    years: tuple[int, ...] = ()
    claim_type: ClaimType = ClaimType.INTERPRETATION
    evidence: tuple[str, ...] = ("temporal_timeline",)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind, "dimension": self.dimension,
            "status": self.status, "statement": self.statement,
            "years": list(self.years), "claim_type": self.claim_type.value,
            "evidence": list(self.evidence),
        }


@dataclass(frozen=True, slots=True)
class InstitutionalIntelligence:
    """The platform's reading of one company, from its own computed evidence."""

    company_id: str
    ticker: str
    name: str

    #: One signal per reasoning axis. ``None`` where the platform holds no
    #: evidence for that axis — the reason is in ``limitations`` and is never
    #: filled in with a neutral-looking guess.
    overall: IntelligenceSignal | None = None
    financial_quality: IntelligenceSignal | None = None
    growth: IntelligenceSignal | None = None
    financial_risk: IntelligenceSignal | None = None
    valuation: IntelligenceSignal | None = None
    management_credibility: IntelligenceSignal | None = None
    recommendation_signal: IntelligenceSignal | None = None

    #: Balance-sheet weaknesses the financial-risk category already scored.
    risk_signals: list[IntelligenceSignal] = field(default_factory=list)
    #: Readings of the stored yearly series: per-dimension trends, repeated
    #: patterns and narrative-versus-accounts contradictions.
    temporal: list[TemporalSignal] = field(default_factory=list)

    #: The scoring engine's own ranking, copied rather than re-derived.
    strengths: list[str] = field(default_factory=list)
    weaknesses: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: What the platform could not see, stated rather than assumed.
    limitations: list[str] = field(default_factory=list)

    #: The authoritative scoring output, restated: this layer explains the
    #: recommendation and never overrides it.
    recommendation: str | None = None
    recommendation_support: list[str] = field(default_factory=list)

    #: The ``ScoreResult`` itself, referenced rather than copied — a consumer
    #: that needs a figure reads this or the citation, never a duplicate.
    score: ScoreResult | None = None
    #: The ``Citation`` objects the signals point at, taken from the context.
    citations: list[Citation] = field(default_factory=list)

    def signals(self) -> list[IntelligenceSignal]:
        """Every axis signal, in presentation order."""
        ordered = (
            self.overall, self.financial_quality, self.growth,
            self.financial_risk, self.valuation, self.management_credibility,
            self.recommendation_signal,
        )
        return [s for s in ordered if s is not None] + list(self.risk_signals)

    def evidence_keys(self) -> list[str]:
        """Every citation key the report rests on, in order of first use."""
        keys: list[str] = []
        for signal in (*self.signals(), *self.temporal):
            for key in signal.evidence:
                if key not in keys:
                    keys.append(key)
        return keys

    def render(self) -> str:
        """The reading as deterministic text, for a record or a prompt.

        Figure-free by construction, like the statements it is built from, so
        it can be quoted without a citation beside every clause.
        """
        lines = [
            f"INSTITUTIONAL INTELLIGENCE — {self.name} ({self.ticker})",
            "Deterministic composition of the platform's own computed "
            "evidence. No external model was used.",
        ]
        for signal in self.signals():
            evidence = ", ".join(signal.evidence) or "no citation"
            lines.append(
                f"{signal.label} [{signal.claim_type.value}]: {signal.statement} "
                f"(evidence: {evidence})"
            )

        if self.temporal:
            lines.append("")
            lines.append("Temporal memory:")
            lines += [f"  - [{s.kind}] {s.statement}" for s in self.temporal]

        if self.recommendation:
            lines.append("")
            lines.append(
                "Recommendation (authoritative scoring output, restated): "
                f"{self.recommendation}"
            )
            lines += [f"  - {item}" for item in self.recommendation_support]

        for title, items in (("Strengths", self.strengths),
                             ("Weaknesses", self.weaknesses),
                             ("Warnings", self.warnings),
                             ("Limitations", self.limitations)):
            if items:
                lines.append("")
                lines.append(f"{title}:")
                lines += [f"  - {item}" for item in items]
        return "\n".join(lines)

    def as_dict(self) -> dict[str, Any]:
        """A JSON-safe view. No computed figure is duplicated here: the values
        stay in the citations that ``evidence_keys`` names."""
        return {
            "company_id": self.company_id, "ticker": self.ticker,
            "name": self.name,
            "signals": [s.as_dict() for s in self.signals()],
            "temporal": [s.as_dict() for s in self.temporal],
            "strengths": list(self.strengths),
            "weaknesses": list(self.weaknesses),
            "warnings": list(self.warnings),
            "limitations": list(self.limitations),
            "recommendation": self.recommendation,
            "recommendation_support": list(self.recommendation_support),
            "evidence_keys": self.evidence_keys(),
            "has_score": self.score is not None,
        }


class InstitutionalIntelligenceEngine:
    """Turns already-computed platform evidence into deterministic signals.

    The engine is a strict consumer of one artefact — the
    :class:`GroundedContext` the analyst already built — plus the temporal
    memory the context carries as typed domain objects. It issues no query of
    its own, builds no second context and calls no provider, which is what
    makes its output reproducible from the evidence a reader can audit.
    """

    #: Consecutive years of the same direction that make a pattern rather than
    #: a single year's move.
    REPEATED_RUN = 2

    def __init__(
        self, analyst: Any = None, *, context: GroundedContext | None = None,
    ) -> None:
        #: The analyst whose grounded context this engine reads. Anything with
        #: a ``context()`` returning a ``GroundedContext`` will do; the
        #: parameter is duck-typed so a caller can hand in an already-built
        #: context instead of an analyst that would rebuild it.
        self.analyst = analyst
        self._context = context

    # -------------------------------------------------------------- context
    def context(self) -> GroundedContext | None:
        """The grounded evidence the platform already assembled.

        Reused, never rebuilt. The analyst caches one context per instance and
        every capability reads it, so building a second one here would compute
        the same scores and valuations twice and could disagree with the
        citations the answer path audits against.

        ``None`` rather than an exception on failure: this runs inside the
        post-filing workflow, where a filing must never be lost to an
        interpretation-layer problem.
        """
        if self._context is not None:
            return self._context
        getter = getattr(self.analyst, "context", None)
        if not callable(getter):
            return None
        try:
            built = getter()
        except Exception:  # noqa: BLE001 - post-filing must not fail here
            log.exception("institutional intelligence could not read the context")
            return None
        return built if isinstance(built, GroundedContext) else None

    # ---------------------------------------------------------------- build
    def build_full_intelligence(
        self, analysis: Any = None, ticker: str | None = None,
    ) -> InstitutionalIntelligence:
        """Read every axis from the grounded context.

        Kept signature-compatible with the placeholder it replaces, and safe to
        call from the post-filing path: it never raises and never calls out.
        """
        context = self.context()
        company = getattr(analysis, "company", None)

        if context is None:
            # A gap, stated. Nothing is inferred from the absence of evidence.
            notes = _Notes()
            notes.warn(
                "Institutional intelligence was not built: no grounded "
                "context was available."
            )
            notes.limit(
                "No grounded context — quality, growth, risk, valuation, "
                "temporal and credibility signals are all unavailable."
            )
            return InstitutionalIntelligence(
                company_id=str(getattr(company, "id", "") or ""),
                ticker=str(ticker or getattr(company, "ticker", "") or ""),
                name=str(getattr(company, "name", "") or ""),
                warnings=notes.warnings, limitations=notes.limitations,
            )

        score = context.score if isinstance(context.score, ScoreResult) else None
        notes = _Notes()

        overall = self._overall_signal(context, score, notes)
        quality = self._axis_signal(
            context, score, notes, key="financial_quality",
            label="Financial quality", category_key="financial_quality",
        )
        growth = self._growth_signal(context, score, notes)
        risk = self._axis_signal(
            context, score, notes, key="financial_risk",
            label="Financial risk", category_key="financial_risk",
        )
        risk_signals = self._risk_signals(context, score, notes)
        valuation = self._valuation_signal(context, score, notes)
        temporal = self._temporal_signals(context, notes)
        credibility = self._credibility_signal(context, notes)
        recommendation, support = self._recommendation(context, score, notes)

        self._evidence_notes(context, score, notes)
        strengths, weaknesses = self._ranked_areas(score)

        signals = [s for s in (overall, quality, growth, risk, valuation,
                               credibility, recommendation) if s is not None]
        used = {
            key
            for signal in (*signals, *risk_signals, *temporal)
            for key in signal.evidence
        }
        citations = [c for c in context.citations if c.key in used]

        return InstitutionalIntelligence(
            company_id=context.company_id, ticker=context.ticker,
            name=context.name,
            overall=overall, financial_quality=quality, growth=growth,
            financial_risk=risk, valuation=valuation,
            management_credibility=credibility,
            recommendation_signal=recommendation,
            risk_signals=risk_signals, temporal=temporal,
            strengths=strengths, weaknesses=weaknesses,
            warnings=notes.warnings, limitations=notes.limitations,
            recommendation=(score.recommendation if score is not None else None),
            recommendation_support=support,
            score=score, citations=citations,
        )

    # -------------------------------------------------------------- helpers
    @staticmethod
    def _citation(context: GroundedContext, key: str) -> Citation | None:
        """The platform's citation under `key` — never a default, never a
        guess at a figure the platform did not compute."""
        return next(
            (c for c in context.citations if c.key == key and c.value is not None),
            None,
        )

    @classmethod
    def _present(cls, context: GroundedContext, keys: Iterable[str]) -> tuple[str, ...]:
        """The subset of `keys` the context actually publishes.

        A signal can only point at evidence that exists. An evidence key with
        nothing under it would render as a citation a reader cannot resolve,
        which is the same failure as quoting a figure the platform never
        computed.
        """
        return tuple(key for key in keys if cls._citation(context, key) is not None)

    @staticmethod
    def _category(score: ScoreResult | None, key: str) -> CategoryScore | None:
        if score is None:
            return None
        return next((c for c in score.categories if c.key == key), None)

    @staticmethod
    def _confidence_of(score: ScoreResult | None) -> float | None:
        breakdown = getattr(score, "confidence", None)
        value = getattr(breakdown, "confidence", None)
        return round(float(value), 4) if isinstance(value, (int, float)) else None

    # ---------------------------------------------------------------- axes
    def _overall_signal(
        self, context: GroundedContext, score: ScoreResult | None, notes: _Notes,
    ) -> IntelligenceSignal | None:
        """The composite reading, taken from the scoring engine's own result."""
        if score is None:
            notes.warn(
                "The institutional score was unavailable, so no overall, "
                "quality, growth, risk, valuation or recommendation signal is "
                "stated."
            )
            notes.limit(
                "Institutional score — the platform did not score this "
                "company, so nothing is inferred in its place."
            )
            return None
        return IntelligenceSignal(
            key="overall_assessment", label="Overall assessment",
            status=score.grade,
            statement=(
                f"The scoring engine grades {score.name} {score.grade} — "
                f"{score.grade_description.lower()} — at "
                f"{str(score.conviction).lower()} conviction."
            ),
            claim_type=ClaimType.MODEL_OUTPUT,
            evidence=self._present(
                context, ("overall_score", "grade", "recommendation", "confidence"),
            ),
            confidence=self._confidence_of(score),
        )

    def _axis_signal(
        self, context: GroundedContext, score: ScoreResult | None, notes: _Notes,
        *, key: str, label: str, category_key: str,
    ) -> IntelligenceSignal | None:
        """One axis, read from its existing category score.

        `status` is the category's own `grade_hint` — the platform's existing
        banding — rather than a set of thresholds re-invented here, and the
        statement is figure-free: the numbers live in the citation this signal
        points at.
        """
        category = self._category(score, category_key)
        if category is None:
            notes.limit(
                f"{label} — no {category_key} category was scored for this "
                "company, so no signal is stated."
            )
            return None
        return IntelligenceSignal(
            key=key, label=label, status=category.grade_hint.lower(),
            statement=(
                f"{label} is graded {category.grade_hint.lower()} by the "
                f"platform's own {category.label.lower()} category."
            ),
            claim_type=ClaimType.MODEL_OUTPUT,
            evidence=self._present(context, (f"score_{category.key}",)),
            confidence=category.confidence.confidence,
        )

    def _growth_signal(
        self, context: GroundedContext, score: ScoreResult | None, notes: _Notes,
    ) -> IntelligenceSignal | None:
        """Growth, from the scoring engine's growth-quality category.

        The reported growth figures and the forecast CAGR are attached as
        evidence where the platform published them, so a reader sees history
        and projection beside the grade. The projection is labelled as such in
        the statement: it is a model output, not a reported figure.
        """
        category = self._category(score, "growth_quality")
        if category is None:
            notes.limit(
                "Growth — no growth-quality category was scored for this "
                "company, so no growth signal is stated."
            )
            return None

        evidence = self._present(context, (
            f"score_{category.key}", "revenue_growth", "pat_growth",
            "forecast_revenue_cagr",
        ))
        projected = any(key.startswith("forecast_") for key in evidence)
        statement = (
            f"Growth is graded {category.grade_hint.lower()} by the platform's "
            "own growth-quality category"
        )
        statement += (
            ", read alongside the platform's forecast, which is a projection "
            "rather than a reported figure." if projected else "."
        )
        return IntelligenceSignal(
            key="growth", label="Growth", status=category.grade_hint.lower(),
            statement=statement, claim_type=ClaimType.MODEL_OUTPUT,
            evidence=evidence, confidence=category.confidence.confidence,
        )

    def _risk_signals(
        self, context: GroundedContext, score: ScoreResult | None, notes: _Notes,
    ) -> list[IntelligenceSignal]:
        """Balance-sheet weaknesses the financial-risk category already scored.

        Each metric inside the category carries the scoring engine's own
        verdict and its own explanation. A metric below the platform's neutral
        midpoint is a weakness *as the platform scored it*; the signal adds no
        threshold of its own beyond that midpoint, and the measured figure
        stays under the citation key it was published under.
        """
        category = self._category(score, "financial_risk")
        if category is None:
            return []

        signals: list[IntelligenceSignal] = []
        for metric in category.metrics:
            citation_key = RISK_METRIC_CITATIONS.get(metric.key)
            if citation_key is None or metric.score >= NEUTRAL_SCORE:
                continue
            if not self._present(context, (citation_key,)):
                continue
            signals.append(IntelligenceSignal(
                key=f"risk_{metric.key}", label=f"Financial risk — {metric.label}",
                status="weak",
                statement=(
                    f"{metric.label} scores below the platform's neutral "
                    "midpoint inside the financial-risk category, so it is read "
                    "as a weakness rather than a strength."
                ),
                claim_type=ClaimType.MODEL_OUTPUT,
                evidence=(citation_key,), confidence=metric.confidence,
            ))

        # The same condition, and the same absence of a weight check, as the
        # scoring engine's own override — mirrored so this layer explains the
        # decision that was actually taken rather than a near-miss of it.
        if category.raw_score <= FRAGILE_BALANCE_SHEET_SCORE:
            notes.warn(
                "Balance-sheet fragility is at or below the level at which the "
                "scoring engine caps its recommendation at REDUCE."
            )
        return signals

    def _valuation_signal(
        self, context: GroundedContext, score: ScoreResult | None, notes: _Notes,
    ) -> IntelligenceSignal | None:
        """Valuation, interpreted from evidence the platform already computed.

        Five deterministic readings, all of them interpretations rather than
        new arithmetic: supported, attractive-but-unsupported, constrained,
        expensive, and unavailable. "Constrained" is the scoring engine's own
        condition — the valuation category at or below the threshold at which
        it caps a recommendation — read back, not re-invented. No target price
        is produced and no multiple is re-derived.
        """
        category = self._category(score, "valuation")
        if category is None:
            notes.limit(
                "Valuation — no valuation category was scored for this "
                "company, so no valuation signal is stated and no price, "
                "target or upside is offered."
            )
            return None

        evidence = self._present(context, (
            f"score_{category.key}", "weighted_value", "valuation_upside",
            "valuation_recommendation", "pe_ratio", "ev_ebitda",
        ))
        hint = category.grade_hint
        quality = self._category(score, "financial_quality")
        quality_strong = quality is not None and quality.grade_hint in _STRONG_HINTS
        quality_weak = quality is not None and quality.grade_hint in _WEAK_HINTS
        constrained = (
            category.raw_score <= EXPENSIVE_VALUATION_SCORE and category.weight > 0
        )

        if constrained:
            status = "constrained"
            statement = (
                "Valuation is the binding constraint: the platform's valuation "
                "category sits at or below the level at which its scoring "
                "engine caps the recommendation, whatever the business quality."
            )
            if quality_strong:
                statement += (
                    " The fundamentals are strong, so the constraint is the "
                    "price rather than the business."
                )
            notes.warn("Valuation is the binding constraint on this recommendation.")
        elif hint in _STRONG_HINTS:
            if quality_weak:
                status = "unsupported"
                statement = (
                    "Valuation screens attractively on the platform's own "
                    "model, but financial quality is weak: the platform does "
                    "not treat an attractive multiple as support where the "
                    "fundamentals do not confirm it."
                )
                notes.warn(
                    "An attractive valuation is not supported by financial "
                    "quality."
                )
            else:
                status = "supported"
                statement = (
                    "Valuation screens attractively on the platform's own "
                    "model and financial quality does not contradict it, so "
                    "the price is supported by the fundamentals as the platform "
                    "measures them."
                )
        elif hint in _WEAK_HINTS:
            status = "expensive"
            statement = (
                "Valuation screens weakly on the platform's own model: the "
                "market price is not supported by the value the platform "
                "computed for the company."
            )
        else:
            status = "neutral"
            statement = (
                "Valuation is graded adequate by the platform's own valuation "
                "category — neither a support nor a constraint on the "
                "recommendation."
            )

        return IntelligenceSignal(
            key="valuation", label="Valuation", status=status, statement=statement,
            claim_type=ClaimType.MODEL_OUTPUT, evidence=evidence,
            confidence=category.confidence.confidence,
        )

    # ------------------------------------------------------------- temporal
    def _temporal_signals(
        self, context: GroundedContext, notes: _Notes,
    ) -> list[TemporalSignal]:
        """Trends, repeated patterns and contradictions, from stored memory.

        Nothing is re-read from the database and no second trend model is
        built: the observations arrive as domain objects, `trend_of` is the
        platform's one direction calculation, and the narrative-versus-accounts
        judgement is `DimensionReading.contradicts_metric` read as computed.
        """
        observations = context.temporal
        if not observations:
            notes.limit(
                "Temporal memory — the platform holds no readable yearly "
                "observations for this company, so no trend, pattern or "
                "track-record signal is stated. This is an absence of evidence, "
                "not evidence of stability."
            )
            return []

        signals: list[TemporalSignal] = []
        latest = observations[-1]

        # Where the company stands now: one reading per tracked dimension in
        # the most recent observed year. Every year would be a wall of text;
        # the patterns below carry the multi-year story.
        for reading in latest.dimensions:
            signals.append(
                self._dimension_signal(context, reading, latest.fiscal_year)
            )

        # Contradictions are rare and each one is a real disagreement between
        # what the filing says and what the accounts show, so they are reported
        # for every observed year rather than only the latest.
        for observation in observations:
            for reading in observation.dimensions:
                if not reading.contradicts_metric:
                    continue
                signals.append(self._contradiction_signal(
                    context, reading, observation.fiscal_year,
                ))
                notes.warn(
                    f"FY{observation.fiscal_year}: the narrative reading for "
                    f"{_spoken(reading.dimension)} disagrees with the "
                    "measured accounts. Both are reported as recorded."
                )

        history: dict[str, list[tuple[int, ObservationTrend]]] = {}
        for observation in observations:
            for reading in observation.dimensions:
                history.setdefault(reading.dimension, []).append(
                    (observation.fiscal_year, reading.trend)
                )
        for dimension, series in history.items():
            pattern = self._repeated_signal(context, dimension, series)
            if pattern is None:
                continue
            signals.append(pattern)
            if pattern.kind == REPEATED_DETERIORATION:
                notes.warn(pattern.statement)

        mean_confidence = sum(o.confidence for o in observations) / len(observations)
        if mean_confidence < MIN_SERVABLE_CONFIDENCE:
            notes.limit(
                "Temporal memory reads below the platform's servable "
                "confidence threshold, so its trend signals are provisional."
            )
        return signals

    def _dimension_signal(
        self, context: GroundedContext, reading: DimensionReading, fiscal_year: int,
    ) -> TemporalSignal:
        """One dimension in the latest observed year.

        The measured direction is `trend_of` applied to the two figures the
        reading already carries — the platform's one direction calculation,
        applied to the years that are already on the row rather than to a
        newly queried series.
        """
        label = _spoken(reading.dimension)
        # Phrased around "the reading" rather than the dimension name so a
        # plural axis ("margins") and a singular one ("debt") both read
        # correctly without a hand-maintained grammar table.
        statement = (
            f"The platform's {label} reading is {reading.trend.value} "
            f"in FY{fiscal_year}" if reading.trend != ObservationTrend.UNKNOWN
            else f"The platform records no directional {label} reading "
                 f"in FY{fiscal_year}"
        )
        if reading.metric_value is not None and reading.metric_prior is not None:
            measured = trend_of(reading.metric_value, reading.metric_prior)
            statement += f", with {_METRIC_MOVE[measured]} on the accounts"
        statement += "."

        if reading.contradicts_metric:
            # Read, not recomputed: `contradicts_metric` is the domain's own
            # inverse-aware property and stays the single authority on it.
            statement += (
                " The measured figure moves against that reading; both are "
                "recorded rather than reconciled."
            )
        return TemporalSignal(
            kind=DIMENSION, dimension=reading.dimension,
            status=reading.trend.value, statement=statement,
            years=(fiscal_year,),
            evidence=self._present(context, ("temporal_timeline",)),
        )

    def _contradiction_signal(
        self, context: GroundedContext, reading: DimensionReading, fiscal_year: int,
    ) -> TemporalSignal:
        return TemporalSignal(
            kind=CONTRADICTION, dimension=reading.dimension,
            status="contradiction",
            statement=(
                f"In FY{fiscal_year} the narrative reading for "
                f"{_spoken(reading.dimension)} disagrees with the "
                "measured accounts. The platform reports the disagreement "
                "rather than choosing between them."
            ),
            years=(fiscal_year,),
            evidence=self._present(context, ("temporal_timeline",)),
        )

    def _repeated_signal(
        self, context: GroundedContext, dimension: str,
        series: Sequence[tuple[int, ObservationTrend]],
    ) -> TemporalSignal | None:
        """A run of the same direction at the end of a dimension's series.

        A single year's move can be noise; two consecutive years in the same
        direction is the smallest thing worth calling a pattern, and the
        threshold is stated here rather than implied.
        """
        if len(series) < self.REPEATED_RUN:
            return None
        trend = series[-1][1]
        if trend not in (ObservationTrend.IMPROVING, ObservationTrend.DETERIORATING):
            return None

        run = 1
        for _, earlier in reversed(list(series)[:-1]):
            if earlier != trend:
                break
            run += 1
        if run < self.REPEATED_RUN:
            return None

        years = tuple(year for year, _ in series[len(series) - run:])
        label = _spoken(dimension)
        if trend == ObservationTrend.DETERIORATING:
            return TemporalSignal(
                kind=REPEATED_DETERIORATION, dimension=dimension,
                status=trend.value,
                statement=(
                    f"The {label} reading has deteriorated in "
                    "consecutive observed years "
                    f"(FY{years[0]}–FY{years[-1]}) — a pattern rather than a "
                    "single year's move."
                ),
                years=years,
                evidence=self._present(context, ("temporal_timeline",)),
            )
        return TemporalSignal(
            kind=REPEATED_IMPROVEMENT, dimension=dimension, status=trend.value,
            statement=(
                f"The {label} reading has improved in "
                "consecutive observed years "
                f"(FY{years[0]}–FY{years[-1]}) — a pattern rather than a single "
                "year's move."
            ),
            years=years,
            evidence=self._present(context, ("temporal_timeline",)),
        )

    def _credibility_signal(
        self, context: GroundedContext, notes: _Notes,
    ) -> IntelligenceSignal | None:
        """Management's delivery record, from the stored prior-year verdicts.

        Read from the same computation the ``management_credibility`` citation
        was rendered from. Where the context carries no credibility read but
        does carry the typed series, `credibility_score` — the platform's one
        credibility calculation — is applied to it, so this engine never
        invents a second formula or a neutral 50% for a company nobody can
        grade.
        """
        credibility = context.credibility
        if credibility is None:
            score, assessed = credibility_score(context.temporal)
            credibility = {
                "score": score, "years_assessed": assessed,
                "years_total": len(context.temporal),
                "note": (
                    None if assessed
                    else "No year carried guidance specific enough to score."
                ),
            }

        score = credibility.get("score")
        if score is None:
            notes.limit(
                "Management credibility — "
                + (credibility.get("note") or "no assessable guidance record.")
            )
            return None

        status = "strong" if score >= 0.8 else "mixed" if score >= 0.5 else "weak"
        return IntelligenceSignal(
            key="management_credibility", label="Management credibility",
            status=status,
            statement=(
                f"Management's guidance delivery record is {status} across the "
                "years the platform has observed and graded, judged from "
                "prior-year guidance checked against the filings that followed."
            ),
            claim_type=ClaimType.INTERPRETATION,
            evidence=self._present(context, ("management_credibility",
                                             "temporal_timeline")),
            confidence=0.9 if credibility.get("years_assessed") else None,
        )

    # ------------------------------------------------------- recommendation
    def _recommendation(
        self, context: GroundedContext, score: ScoreResult | None, notes: _Notes,
    ) -> tuple[IntelligenceSignal | None, list[str]]:
        """The scoring engine's recommendation, restated, and what shapes it.

        The recommendation is never derived here. What this method does add is
        the *why*: which existing conditions the scoring engine applied as
        overrides — valuation, balance-sheet fragility, data confidence — and
        which scored areas support the call. Every clause is read off the same
        `ScoreResult`, so the explanation cannot drift from the decision.
        """
        if score is None:
            return None, []

        support: list[str] = []
        case = _Notes()

        if score.strongest:
            case.warn(
                "It rests on the platform's strongest scored areas: "
                + ", ".join(score.strongest) + "."
            )
        if score.weakest:
            case.warn(
                "Its weakest scored areas are "
                + ", ".join(score.weakest) + "."
            )
        quality = self._category(score, "financial_quality")
        if quality is not None and quality.grade_hint in _STRONG_HINTS:
            case.warn("It is supported by strong financial quality.")
        if quality is not None and quality.grade_hint in _WEAK_HINTS:
            case.warn("It is constrained by weak financial quality.")

        valuation = self._category(score, "valuation")
        if (valuation is not None
                and valuation.raw_score <= EXPENSIVE_VALUATION_SCORE
                and valuation.weight > 0):
            case.warn(
                "It is capped by valuation rather than driven by the composite "
                "alone, which is the scoring engine's own rule."
            )
        risk = self._category(score, "financial_risk")
        if risk is not None and risk.raw_score <= FRAGILE_BALANCE_SHEET_SCORE:
            case.warn(
                "It is capped by balance-sheet risk, which the scoring engine "
                "ranks above quality."
            )
        confidence = self._confidence_of(score)
        if confidence is not None and confidence < LOW_CONFIDENCE_THRESHOLD:
            case.warn(
                "It was pulled toward HOLD by low data confidence, as the "
                "scoring engine does when too many weighted inputs are missing."
            )
        support = case.warnings

        signal = IntelligenceSignal(
            key="recommendation", label="Recommendation",
            status=score.recommendation,
            statement=(
                f"The scoring engine's recommendation is '{score.recommendation}', "
                "restated exactly as computed. This layer explains it and does "
                "not override it."
            ),
            claim_type=ClaimType.OPINION,
            evidence=self._present(
                context, ("recommendation", "overall_score", "grade", "confidence"),
            ),
            confidence=confidence,
        )
        return signal, support

    # ------------------------------------------------------------- caveats
    def _evidence_notes(
        self, context: GroundedContext, score: ScoreResult | None, notes: _Notes,
    ) -> None:
        """Warnings and limitations from the shape of the evidence itself.

        The scoring engine's own warnings are carried across, except the ones
        that state a figure: a number here would have no citation beside it,
        and the figure-bearing low-confidence warning is restated from the
        confidence citation instead.
        """
        if score is not None:
            for warning in score.warnings:
                if _HAS_DIGIT.search(warning):
                    continue
                notes.warn(warning)

            confidence = self._confidence_of(score)
            if confidence is not None and confidence < LOW_CONFIDENCE_THRESHOLD:
                notes.warn(
                    "Data confidence is below the platform's threshold, so the "
                    "recommendation was pulled toward HOLD and every signal "
                    "here should be read as provisional."
                )
                notes.limit(
                    "Data confidence / evidence availability — too many "
                    "weighted inputs are missing for a directional call, and "
                    "the platform says so rather than scoring as though the "
                    "data were complete."
                )

        quality = self._citation(context, "data_quality")
        if quality is not None:
            notes.warn(
                "The platform's data-quality engine grades the underlying data "
                f"{quality.value}, so every figure here carries that caveat."
            )

        for item in context.unavailable:
            notes.limit(f"{item} — not available in the grounded context.")

        if not context.citations:
            notes.limit(
                "No platform citations were supplied, so nothing here can be "
                "traced to a computed figure."
            )

    @staticmethod
    def _ranked_areas(score: ScoreResult | None) -> tuple[list[str], list[str]]:
        """The scoring engine's own strongest/weakest ranking, copied.

        Not re-derived and not re-ordered: these labels are how the scoring
        engine described its own result.
        """
        if score is None:
            return [], []
        return list(score.strongest), list(score.weakest)
