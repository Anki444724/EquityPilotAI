"""Focused tests for the deterministic institutional-intelligence foundation.

The engine is a consumer of two artefacts the platform has already produced —
the `GroundedContext` (its citations and its `ScoreResult`) and the typed
temporal series — so most of these build those by hand, exactly as
`test_investment_answer_engine.py` does, and assert what the engine makes of
them.

Three properties are treated as contract rather than detail:

* **Nothing is fabricated.** Signal statements are figure-free by construction,
  and every evidence key a signal names is one the context actually publishes,
  so a reader can always resolve the number behind a sentence.
* **The recommendation is the scoring engine's.** It is restated verbatim and
  never derived: a cheap multiple in the evidence does not become a call here.
* **No provider is involved.** The engine imports no provider module, and the
  integration test drives it through a real analyst whose router raises on any
  completion call.
"""
from __future__ import annotations

import json
import re

import pytest

from app.domain.ai.types import Citation, EvidenceKind
from app.domain.knowledge.temporal import (
    DimensionReading, GuidanceVerdict, ObservationTrend, YearObservation,
)
from app.domain.scoring.base import (
    CategoryScore, ConfidenceBreakdown, DataOrigin, MetricScore,
)
from app.services.ai.context_builder import ContextBuilder, GroundedContext
from app.services.ai.institutional_intelligence import (
    CONTRADICTION, DIMENSION, REPEATED_DETERIORATION, REPEATED_IMPROVEMENT,
    InstitutionalIntelligenceEngine,
)
from app.services.scoring.overall_score import ScoreResult
from app.models.knowledge import YearlyObservation
from tests.test_investment_answer_engine import (
    cat, cite, make_context, make_score,
)
# The temporal-memory fixtures, reused rather than re-declared.
from tests.test_temporal_memory import company, db  # noqa: F401

NAME = "Testco Ltd"
TICKER = "TEST"

#: "FY2026" — the stored row's own fiscal year, and the only number a statement
#: is allowed to contain.
_FISCAL_YEAR = re.compile(r"FY\d{4}")


def build(context: GroundedContext, *, analysis=None, ticker: str | None = None):
    """Build a report the way a caller holding a context does."""
    return InstitutionalIntelligenceEngine(context=context).build_full_intelligence(
        analysis, ticker,
    )


def reading(dimension: str, trend: ObservationTrend, **metrics) -> DimensionReading:
    return DimensionReading(dimension=dimension, trend=trend, **metrics)


@pytest.fixture()
def db_session():
    """The seeded reference-company session the deterministic tests use."""
    from tests.conftest import TestingSession

    session = TestingSession()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


# ------------------------------------------------------------------ fixtures

def categories(**raw: float) -> list[CategoryScore]:
    """The scoring categories with the given raw scores, everything else
    omitted — a category the platform did not score is simply absent."""
    labels = {
        "business_quality": "Business Quality",
        "financial_quality": "Financial Quality",
        "management_quality": "Management Quality",
        "capital_allocation": "Capital Allocation",
        "competitive_moat": "Competitive Moat",
        "governance": "Corporate Governance",
        "financial_risk": "Financial Risk",
        "business_risk": "Business Risk",
        "valuation": "Valuation",
        "growth_quality": "Growth Quality",
        "cash_flow_quality": "Cash Flow Quality",
        "esg": "ESG",
        "momentum": "Momentum",
    }
    return [cat(key, labels[key], score) for key, score in raw.items()]


def metric(key: str, label: str, score: float, *, value: float) -> MetricScore:
    return MetricScore(
        key=key, label=label, score=score, weight=0.2,
        origin=DataOrigin.VERIFIED, value=value, unit="x",
    )


def weak_balance_sheet(raw: float = 2.0) -> list[CategoryScore]:
    """A financial-risk category whose metrics are all scored weak."""
    return [CategoryScore(
        key="financial_risk", label="Financial Risk", raw_score=raw,
        weighted_score=raw * 0.1, weight=0.1,
        confidence=ConfidenceBreakdown(0.9, 0.9, 0.0, 0.0, 0.0, 4, 0),
        metrics=[
            metric("net_debt_ebitda", "Net debt / EBITDA", 2.5, value=3.4),
            metric("interest_coverage", "Interest coverage", 2.5, value=1.6),
            metric("current_ratio", "Current ratio", 2.5, value=0.9),
            metric("altman_z", "Altman Z-score", 2.0, value=1.4),
        ],
        explanation="",
    )]


def temporal_citations(years=(2025, 2026)) -> tuple[Citation, ...]:
    """The two citations `ContextBuilder._add_temporal` publishes."""
    return (
        cite("temporal_timeline",
             f"FY{min(years)}-FY{max(years)} yearly observations",
             kind=EvidenceKind.KNOWLEDGE),
        cite("management_credibility", "100% of guidance delivered",
             kind=EvidenceKind.KNOWLEDGE),
    )


def observation(fiscal_year: int, *readings) -> YearObservation:
    return YearObservation(
        fiscal_year=fiscal_year, dimensions=list(readings), confidence=0.9,
    )


def make_intelligence_context(
    score: ScoreResult | None = None,
    *,
    temporal: list[YearObservation] | None = None,
    credibility: dict | None = None,
    drop: tuple[str, ...] = (),
    extra: tuple[Citation, ...] = (),
) -> GroundedContext:
    """A grounded context carrying the temporal memory the builder attaches.

    The typed series and the credibility read are set on the context exactly as
    `ContextBuilder._add_temporal` sets them, so the engine under test sees the
    same object shape a live request would hand it.
    """
    years = [o.fiscal_year for o in temporal] if temporal else []
    context = make_context(
        score, drop=drop,
        extra=tuple(extra) + (temporal_citations(years) if years else ()),
    )
    context.temporal = list(temporal or [])
    context.credibility = credibility
    return context


def findings(report, kind: str) -> list:
    return [s for s in report.temporal if s.kind == kind]


def digits_outside_fiscal_years(text: str) -> list[str]:
    """Digits that are not part of a fiscal-year label."""
    return re.findall(r"\d+", _FISCAL_YEAR.sub("", text))


# ------------------------------------------------------- axes: quality, risk

class TestQualityAxes:
    def test_strong_quality_and_a_supported_valuation(self):
        score = make_score(categories=categories(
            financial_quality=8.2, growth_quality=7.9, financial_risk=8.0,
            valuation=7.5, business_quality=8.0,
        ))
        report = build(make_intelligence_context(score))

        # The category's own grade_hint is the status — no new thresholds.
        assert report.financial_quality.status == "strong"
        assert report.growth.status == "good"
        # Attractive valuation + quality that does not contradict it.
        assert report.valuation.status == "supported"
        assert report.overall is not None and report.overall.status == score.grade

        # Every figure lives under a citation the context publishes.
        keys = {c.key for c in report.citations}
        assert "score_valuation" in keys
        assert {"weighted_value", "valuation_upside"} <= keys
        assert all(s.evidence for s in report.signals())

    def test_attractive_valuation_against_weak_financial_quality(self):
        """A low multiple with weak fundamentals is not treated as support."""
        score = make_score(categories=categories(
            financial_quality=3.0, growth_quality=4.0, financial_risk=6.0,
            valuation=7.5,
        ))
        report = build(make_intelligence_context(score))

        assert report.financial_quality.status == "poor"
        assert report.valuation.status == "unsupported"
        assert any("not supported by financial quality" in w
                   for w in report.warnings)

    def test_weak_financial_risk_categories_emit_measured_signals(self):
        score = make_score(
            categories=[*weak_balance_sheet(), cat("valuation", "Valuation", 5.0)],
        )
        context = make_intelligence_context(score)
        report = build(context)

        assert report.financial_risk.status == "poor"
        weak = {s.key: s for s in report.risk_signals}
        assert set(weak) == {
            "risk_net_debt_ebitda", "risk_interest_coverage",
            "risk_current_ratio", "risk_altman_z",
        }
        # Each signal points at the citation the measured figure lives under,
        # and that citation exists.
        published = {c.key for c in context.citations}
        for signal in weak.values():
            assert signal.status == "weak"
            assert signal.evidence == (
                signal.key.removeprefix("risk_"),)
            assert signal.evidence[0] in published
        # The category is at the level the scoring engine caps at REDUCE.
        assert any("Balance-sheet fragility" in w for w in report.warnings)

    def test_a_weak_metric_with_no_published_evidence_is_skipped(self):
        """A weakness the reader cannot check is not stated as one.

        `debt_equity` is scored by the risk category but published under no
        citation key, and the second metric's citation is absent from this
        context. Neither becomes a signal.
        """
        score = make_score(categories=[CategoryScore(
            key="financial_risk", label="Financial Risk", raw_score=4.5,
            weighted_score=0.45, weight=0.1,
            confidence=ConfidenceBreakdown(0.9, 0.9, 0.0, 0.0, 0.0, 2, 0),
            metrics=[
                metric("debt_equity", "Debt / equity", 2.5, value=1.6),
                metric("net_debt_ebitda", "Net debt / EBITDA", 2.5, value=3.9),
            ],
            explanation="",
        )])
        context = make_intelligence_context(score, drop=("net_debt_ebitda",))
        report = build(context)

        assert report.risk_signals == []
        assert report.financial_risk.status == "weak"

    def test_a_strong_risk_metric_is_not_reported_as_a_weakness(self):
        score = make_score(categories=[CategoryScore(
            key="financial_risk", label="Financial Risk", raw_score=9.0,
            weighted_score=0.9, weight=0.1,
            confidence=ConfidenceBreakdown(0.9, 0.9, 0.0, 0.0, 0.0, 1, 0),
            metrics=[metric("net_debt_ebitda", "Net debt / EBITDA", 8.5,
                            value=0.4)],
            explanation="",
        )])
        report = build(make_intelligence_context(score))
        assert report.risk_signals == []
        assert report.financial_risk.status == "strong"


# ------------------------------------------------------- axes: valuation

class TestValuationAxis:
    def test_expensive_valuation_despite_strong_fundamentals(self):
        score = make_score(categories=categories(
            financial_quality=8.5, growth_quality=8.0, valuation=3.0,
        ))
        report = build(make_intelligence_context(score))

        assert report.valuation.status == "constrained"
        assert report.financial_quality.status == "strong"
        assert "constraint is the price" in report.valuation.statement
        assert any("binding constraint" in w for w in report.warnings)

    def test_a_weak_but_not_capped_valuation_reads_as_expensive(self):
        """Above the scoring engine's cap threshold the recommendation is not
        constrained, but the price still does not read as supported."""
        score = make_score(categories=categories(
            financial_quality=7.5, valuation=4.0,
        ))
        report = build(make_intelligence_context(score))

        assert report.valuation.status == "expensive"
        assert "not supported by the value the platform computed" \
            in report.valuation.statement

    def test_missing_valuation_is_stated_not_filled_in(self):
        score = make_score(categories=categories(
            financial_quality=8.0, growth_quality=7.0,
        ))
        report = build(make_intelligence_context(score))

        assert report.valuation is None
        limitation = next(item for item in report.limitations
                          if item.startswith("Valuation"))
        assert "no price, target or upside is offered" in limitation

        # And no valuation figure is cited anywhere in the report: the gap is
        # stated, not bridged with evidence the platform did not use.
        assert not {"weighted_value", "valuation_upside", "pe_ratio",
                    "ev_ebitda"} & set(report.evidence_keys())

    def test_no_score_means_no_axis_signals(self):
        report = build(make_intelligence_context(score=None))

        assert report.overall is None and report.valuation is None
        assert report.financial_quality is None and report.growth is None
        assert report.recommendation is None and report.score is None
        assert any("institutional score" in w.lower() for w in report.warnings)


# ------------------------------------------------------- temporal reasoning

class TestTemporalReasoning:
    def test_improving_dimension_reads_through(self):
        context = make_intelligence_context(temporal=[
            observation(2025, reading("roce", ObservationTrend.IMPROVING)),
            observation(2026, reading("roce", ObservationTrend.IMPROVING)),
        ])
        report = build(context)

        current = findings(report, DIMENSION)
        assert [s.dimension for s in current] == ["roce"]
        assert current[0].status == "improving"
        assert current[0].years == (2026,)
        assert "ROCE" in current[0].statement

        repeated = findings(report, REPEATED_IMPROVEMENT)
        assert [s.years for s in repeated] == [(2025, 2026)]
        # Improvement is not a warning.
        assert not any("improved" in w for w in report.warnings)

    def test_deteriorating_dimension_and_a_repeated_pattern(self):
        context = make_intelligence_context(temporal=[
            observation(2025, reading("debt", ObservationTrend.DETERIORATING)),
            observation(2026, reading("debt", ObservationTrend.DETERIORATING)),
        ])
        report = build(context)

        current = findings(report, DIMENSION)[0]
        assert current.dimension == "debt"
        assert current.status == "deteriorating"

        repeated = findings(report, REPEATED_DETERIORATION)
        assert len(repeated) == 1
        assert repeated[0].years == (2025, 2026)
        assert any("has deteriorated" in w for w in report.warnings)

    def test_a_single_year_is_not_a_pattern(self):
        context = make_intelligence_context(temporal=[
            observation(2025, reading("debt", ObservationTrend.STABLE)),
            observation(2026, reading("debt", ObservationTrend.DETERIORATING)),
        ])
        report = build(context)
        assert findings(report, REPEATED_DETERIORATION) == []
        assert findings(report, DIMENSION)[0].status == "deteriorating"

    def test_measured_metrics_and_contradictions(self):
        """The stored measured counterpart drives both the direction and the
        platform's own inverse-aware contradiction flag."""
        context = make_intelligence_context(temporal=[
            observation(2025, reading(
                "debt", ObservationTrend.IMPROVING,
                metric_value=900.0, metric_prior=500.0,
            )),
            observation(2026, reading(
                "debt", ObservationTrend.IMPROVING,
                metric_value=700.0, metric_prior=900.0,
            )),
        ])
        report = build(context)

        # FY2025 is the contradiction: "improving" debt that rose.
        contradictions = findings(report, CONTRADICTION)
        assert [s.years for s in contradictions] == [(2025,)]
        assert contradictions[0].dimension == "debt"
        assert contradictions[0].status == "contradiction"
        assert any("disagrees with the measured accounts" in w
                   for w in report.warnings)

        # FY2026 agrees — debt fell — so its reading says so.
        current = findings(report, DIMENSION)[0]
        assert "lower figure than the previous year" in current.statement

    def test_a_latest_year_contradiction_is_flagged_in_the_reading(self):
        context = make_intelligence_context(temporal=[
            observation(2026, reading(
                "debt", ObservationTrend.IMPROVING,
                metric_value=900.0, metric_prior=400.0,
            )),
        ])
        report = build(context)

        current = findings(report, DIMENSION)[0]
        assert "moves against that reading" in current.statement
        assert [s.years for s in findings(report, CONTRADICTION)] == [(2026,)]

    def test_a_stable_latest_year_is_not_a_pattern(self):
        context = make_intelligence_context(temporal=[
            observation(2025, reading("debt", ObservationTrend.DETERIORATING)),
            observation(2026, reading("debt", ObservationTrend.STABLE)),
        ])
        report = build(context)
        assert findings(report, REPEATED_DETERIORATION) == []
        assert findings(report, DIMENSION)[0].status == "stable"

    def test_low_confidence_observations_are_a_limitation(self):
        context = make_intelligence_context(temporal=[
            YearObservation(fiscal_year=2025, confidence=0.2,
                            dimensions=[reading("debt", ObservationTrend.STABLE)]),
            YearObservation(fiscal_year=2026, confidence=0.2,
                            dimensions=[reading("debt", ObservationTrend.STABLE)]),
        ])
        report = build(context)
        assert any("servable confidence threshold" in item
                   for item in report.limitations)

    def test_missing_temporal_memory_is_an_absence_not_stability(self):
        context = make_intelligence_context(
            make_score(categories=categories(financial_quality=7.0)),
        )
        report = build(context)

        assert report.temporal == []
        assert report.management_credibility is None
        limitation = next(item for item in report.limitations
                          if "Temporal memory" in item)
        assert "absence of evidence" in limitation

    def test_the_context_builder_attaches_the_typed_series(self, db, company):
        """Where the engine's temporal input actually comes from.

        `_add_temporal` already read these rows to render the
        `temporal_timeline` citation; it now keeps the domain objects too. The
        test runs the real method against real rows, so it covers the read-back
        fix and the fallback exclusion together: a template year must never
        become a trend signal.
        """
        db.add_all([
            YearlyObservation(
                company_id=company.id, fiscal_year=2025, confidence=0.9,
                status="current", prior_verdict="delivered",
                # "Improving" debt that actually rose — the inverse-debt
                # contradiction, which only survives the read-back if both
                # metrics do.
                dimensions=json.dumps([{
                    "dimension": "debt", "trend": "improving",
                    "metric_value": 900.0, "metric_prior": 500.0,
                }]),
            ),
            YearlyObservation(
                company_id=company.id, fiscal_year=2026, confidence=0.9,
                status="current", prior_verdict="not_assessable",
                is_fallback=True,
                dimensions=json.dumps([
                    {"dimension": "debt", "trend": "improving"},
                ]),
            ),
        ])
        db.commit()

        context = GroundedContext(
            company_id=company.id, ticker="TEST", name="Test Ltd.",
        )
        # `_add_temporal` touches no other collaborator, so a bare builder is
        # the real code path with nothing else standing in.
        ContextBuilder(None)._add_temporal(context, db, company)  # noqa: SLF001

        assert [o.fiscal_year for o in context.temporal] == [2025]
        debt = context.temporal[0].dimensions[0]
        assert debt.trend == ObservationTrend.IMPROVING
        assert (debt.metric_value, debt.metric_prior) == (900.0, 500.0)
        assert debt.contradicts_metric is True
        assert context.credibility is not None
        assert "temporal_timeline" in {c.key for c in context.citations}


class TestCredibility:
    def test_a_delivered_guidance_record(self):
        context = make_intelligence_context(
            credibility={
                "score": 1.0, "years_assessed": 2, "years_total": 2,
                "verdicts": {"delivered": 2}, "note": None,
            },
            temporal=[
                observation(2025, reading("debt", ObservationTrend.STABLE)),
                observation(2026, reading("debt", ObservationTrend.IMPROVING)),
            ],
        )
        report = build(context)
        assert report.management_credibility.status == "strong"
        assert "management_credibility" in report.management_credibility.evidence

    def test_a_missed_guidance_record(self):
        context = make_intelligence_context(
            credibility={"score": 0.0, "years_assessed": 2, "years_total": 2,
                         "verdicts": {"missed": 2}, "note": None},
        )
        report = build(context)
        assert report.management_credibility.status == "weak"

    def test_an_unassessable_record_is_stated_rather_than_averaged(self):
        context = make_intelligence_context(credibility={
            "score": None, "years_assessed": 0, "years_total": 3,
            "verdicts": {"not_assessable": 3},
            "note": "No year carried guidance specific enough to score.",
        })
        report = build(context)
        assert report.management_credibility is None
        assert any("guidance specific enough" in item
                   for item in report.limitations)

    def test_credibility_is_derived_from_the_series_when_absent(self):
        """No credibility read on the context, but a `delivered` verdict in the
        series: the domain's own calculation is reused, not re-implemented."""
        delivered = YearObservation(
            fiscal_year=2026, confidence=0.9,
            prior_verdict=GuidanceVerdict.DELIVERED,
            dimensions=[reading("debt", ObservationTrend.STABLE)],
        )
        context = make_intelligence_context(temporal=[delivered])
        report = build(context)
        assert report.management_credibility.status == "strong"


# --------------------------------------------------- confidence / caveats

class TestConfidenceAndDataQuality:
    def test_low_confidence_is_a_warning_and_a_limitation(self):
        score = make_score(
            confidence=0.35,
            warnings=("Low confidence — 65% of weighted inputs are missing.",),
            categories=categories(financial_quality=5.0, valuation=5.0),
        )
        report = build(make_intelligence_context(score))

        # The figure-bearing warning is not copied raw; it is restated in the
        # engine's own wording, so no uncited number reaches the report.
        assert not any("65%" in w for w in report.warnings)
        assert any("Data confidence is below the platform's threshold" in w
                   for w in report.warnings)
        assert any("too many weighted inputs are missing" in item
                   for item in report.limitations)

    def test_data_quality_grade_is_carried_as_a_caveat(self):
        score = make_score(categories=categories(financial_quality=6.0))
        context = make_intelligence_context(
            score, extra=(cite("data_quality", "C", kind=EvidenceKind.VALUATION),),
        )
        report = build(context)
        assert any("data-quality engine grades the underlying data C" in w
                   for w in report.warnings)

    def test_a_context_with_no_citations_is_a_limitation(self):
        """Nothing to trace to — said plainly rather than presented as an
        evidence-backed reading."""
        report = build(GroundedContext(
            company_id="c1", ticker=TICKER, name=NAME,
        ))
        assert any("No platform citations were supplied" in item
                   for item in report.limitations)

    def test_unavailable_sections_become_limitations(self):
        context = make_intelligence_context(
            make_score(categories=categories(financial_quality=6.0)),
        )
        context.unavailable = ["Forecast projections", "Valuation outputs"]
        report = build(context)
        assert any("Forecast projections" in item for item in report.limitations)


# -------------------------------------------------------- recommendation

class TestRecommendationIsAuthoritative:
    def test_recommendation_is_restated_never_derived(self):
        """A low multiple in the evidence does not become a call here."""
        score = make_score(
            overall=55.0, grade="BBB", recommendation="HOLD", conviction="Low",
            categories=categories(financial_quality=5.5, valuation=3.0),
        )
        context = make_intelligence_context(score)
        report = build(context)

        assert report.recommendation == "HOLD"
        assert report.recommendation_signal.status == "HOLD"
        assert report.score is score, "the ScoreResult must be referenced, not copied"

        rendered = report.render()
        assert "The scoring engine's recommendation is 'HOLD'" in rendered
        # A cheap trailing P/E in the evidence does not turn into a BUY.
        assert "BUY" not in rendered.upper()

    def test_support_explains_the_overrides_the_scoring_engine_applied(self):
        # P1-1: rationale is authoritative. The engine must only claim a cap
        # when the rationale proves it bound. So this test now supplies a
        # rationale that actually contains the three cap reasons, mirroring the
        # real scoring engine's output when caps bind.
        rationale = (
            "Composite score of 70.0/100 maps to ACCUMULATE. "
            "Capped at HOLD: valuation scores 2.0/10, so the shares are expensive "
            "regardless of business quality. "
            "Capped at REDUCE: financial risk scores 2.5/10, indicating balance-sheet fragility. "
            "Capped at HOLD: confidence is only 40% (60% of weighted inputs are missing), "
            "which does not support a directional call."
        )
        score = make_score(
            recommendation="HOLD", confidence=0.40, rationale=rationale,
            categories=categories(
                financial_quality=8.0, growth_quality=7.0, valuation=2.0,
                financial_risk=2.5,
            ),
        )
        report = build(make_intelligence_context(score))
        joined = " ".join(report.recommendation_support)

        assert "supported by strong financial quality" in joined
        assert "capped by valuation" in joined
        assert "capped by balance-sheet risk" in joined
        assert "low data confidence" in joined
        # Every clause is traceable to the result the recommendation came from.
        assert "strongest scored areas" in joined
        # And the rationale is the source of truth for caps.
        assert "valuation scores" in score.recommendation_rationale.lower()
        assert "financial risk scores" in score.recommendation_rationale.lower()
        assert "confidence is only" in score.recommendation_rationale.lower()

    def test_p1_1_cap_condition_exists_but_does_not_bind(self):
        """P1-1 regression: a potential cap exists but did not bind.

        Valuation is expensive (2.0) and financial risk is fragile (2.5), but
        the composite already maps to HOLD, so neither cap actually reduced the
        recommendation. The rationale therefore does NOT contain cap reasons,
        and the engine must NOT claim that valuation or risk capped it.
        """
        # Base is already HOLD, so caps do not bind.
        rationale = "Composite score of 50.0/100 maps to HOLD."
        score = make_score(
            overall=50.0,
            recommendation="HOLD",
            rationale=rationale,
            categories=categories(
                financial_quality=8.0, growth_quality=7.0, valuation=2.0,
                financial_risk=2.5,
            ),
        )
        report = build(make_intelligence_context(score))
        joined = " ".join(report.recommendation_support).lower()
        rendered = report.render().lower()

        # Potential cap conditions exist (valuation 2.0, risk 2.5) but must NOT be claimed
        assert "capped by valuation" not in joined, "valuation cap claimed when it did not bind"
        assert "capped by balance-sheet risk" not in joined, "risk cap claimed when it did not bind"
        # Explanation must agree with rationale — rationale says HOLD from composite, no caps
        assert "composite score of 50.0/100 maps to hold" in rationale.lower()
        assert score.recommendation_rationale == rationale
        # Rendered support should not invent a cap either
        assert "capped by valuation" not in rendered
        assert "capped by balance-sheet risk" not in rendered

    def test_p1_1_valuation_cap_binds_only_when_rationale_proves_it(self):
        """Valuation expensive but base already HOLD → no cap claim; when rationale
        contains valuation cap, engine must claim it."""
        # Case 1: expensive but does NOT bind
        rationale_no_cap = "Composite score of 50.0/100 maps to HOLD."
        score_no_cap = make_score(
            overall=50.0, recommendation="HOLD", rationale=rationale_no_cap,
            categories=categories(valuation=2.0, financial_quality=8.0),
        )
        report_no_cap = build(make_intelligence_context(score_no_cap))
        assert "capped by valuation" not in " ".join(report_no_cap.recommendation_support).lower()

        # Case 2: expensive and DOES bind (base ACCUMULATE capped to HOLD)
        rationale_with_cap = (
            "Composite score of 70.0/100 maps to ACCUMULATE. "
            "Capped at HOLD: valuation scores 2.0/10, so the shares are expensive regardless of business quality."
        )
        score_with_cap = make_score(
            overall=70.0, recommendation="HOLD", rationale=rationale_with_cap,
            categories=categories(valuation=2.0, financial_quality=8.0),
        )
        report_with_cap = build(make_intelligence_context(score_with_cap))
        assert "capped by valuation" in " ".join(report_with_cap.recommendation_support).lower()
        assert "valuation scores" in score_with_cap.recommendation_rationale.lower()

    def test_p1_1_confidence_cap_binds_only_when_rationale_proves_it(self):
        """Low confidence condition exists but does NOT bind when base is HOLD."""
        rationale_no_cap = "Composite score of 50.0/100 maps to HOLD."
        score_no_cap = make_score(
            overall=50.0, recommendation="HOLD", rationale=rationale_no_cap,
            confidence=0.40,
            categories=categories(financial_quality=8.0),
        )
        report_no_cap = build(make_intelligence_context(score_no_cap))
        assert "low data confidence" not in " ".join(report_no_cap.recommendation_support).lower()

        rationale_with_cap = (
            "Composite score of 70.0/100 maps to ACCUMULATE. "
            "Capped at HOLD: confidence is only 40% (60% of weighted inputs are missing), "
            "which does not support a directional call."
        )
        score_with_cap = make_score(
            overall=70.0, recommendation="HOLD", rationale=rationale_with_cap,
            confidence=0.40,
            categories=categories(financial_quality=8.0),
        )
        report_with_cap = build(make_intelligence_context(score_with_cap))
        assert "low data confidence" in " ".join(report_with_cap.recommendation_support).lower()
        assert "confidence is only" in score_with_cap.recommendation_rationale.lower()

    def test_the_same_recommendation_survives_an_unfavourablereading(self):
        """The engine may disagree in tone with a weak company but it may not
        change the call."""
        score = make_score(
            overall=30.0, grade="C", recommendation="SELL",
            categories=[*weak_balance_sheet(), cat("valuation", "Valuation", 3.0)],
        )
        report = build(make_intelligence_context(score))
        assert report.recommendation == "SELL"
        assert report.recommendation_signal.status == "SELL"


# ------------------------------------------------------- output contract

class TestOutputContract:
    def _full_report(self):
        score = make_score(
            overall=66.0, grade="A", recommendation="ACCUMULATE",
            categories=categories(
                financial_quality=8.0, growth_quality=7.0, financial_risk=7.5,
                valuation=6.8, business_quality=7.5,
            ),
        )
        context = make_intelligence_context(
            score,
            temporal=[
                observation(2025, reading("debt", ObservationTrend.IMPROVING)),
                observation(2026,
                            reading("debt", ObservationTrend.IMPROVING),
                            reading("margins", ObservationTrend.DETERIORATING)),
            ],
            credibility={"score": 1.0, "years_assessed": 2, "years_total": 2,
                         "verdicts": {"delivered": 2}, "note": None},
        )
        return context, build(context)

    def test_statements_carry_no_fabricated_figures(self):
        context, report = self._full_report()
        published = {c.key for c in context.citations}

        text = [s.statement for s in report.signals()]
        text += [s.statement for s in report.temporal]
        text += report.warnings + report.limitations
        for sentence in text:
            assert digits_outside_fiscal_years(sentence) == [], sentence
        # And every citation a signal names is one the context publishes.
        for signal in (*report.signals(), *report.temporal):
            assert set(signal.evidence) <= published

    def test_signals_are_labelled_by_claim_type(self):
        _, report = self._full_report()
        labels = {s.key: s.claim_type.value for s in report.signals()}
        assert labels["overall_assessment"] == "model_output"
        assert labels["valuation"] == "model_output"
        assert labels["management_credibility"] == "interpretation"
        assert labels["recommendation"] == "opinion"
        assert all(s.claim_type.value == "interpretation" for s in report.temporal)

    def test_strengths_and_weaknesses_are_the_scoring_engines_ranking(self):
        score = make_score(
            strongest=("Financial Quality", "Financial Risk", "Business Quality"),
            weakest=("ESG", "Momentum", "Valuation"),
        )
        report = build(make_intelligence_context(score))
        assert report.strengths == list(score.strongest)
        assert report.weaknesses == list(score.weakest)

    def test_output_is_deterministic(self):
        context, first = self._full_report()
        second = build(context)
        assert first.render() == second.render()
        assert first.as_dict() == second.as_dict()
        assert first.evidence_keys() == second.evidence_keys()

    def test_as_dict_is_json_serialisable(self):
        _, report = self._full_report()
        payload = json.loads(json.dumps(report.as_dict()))
        assert payload["ticker"] == TICKER
        assert payload["has_score"] is True
        # No computed figure is duplicated into the payload: the keys name the
        # citations instead.
        assert payload["evidence_keys"]
        assert "overall_score" in payload["evidence_keys"]


# ------------------------------------------------------ engine integration

class StubAnalyst:
    """A duck-typed stand-in for the research analyst.

    The engine only ever asks for a grounded context, so the stub carries one
    and counts how often it was asked — which is how the tests prove the engine
    reuses the caller's context instead of building a parallel one.
    """

    def __init__(self, context=None, *, error: Exception | None = None) -> None:
        self._context = context
        self._error = error
        self.calls = 0

    def context(self):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._context


class TestEngineIntegration:
    def test_the_callers_context_is_reused_not_rebuilt(self):
        score = make_score(categories=categories(financial_quality=7.0))
        context = make_intelligence_context(score)
        analyst = StubAnalyst(context)
        engine = InstitutionalIntelligenceEngine(analyst)

        first = engine.build_full_intelligence()
        second = engine.build_full_intelligence()
        assert first.ticker == context.ticker
        assert first.score is score
        assert second.score is score
        # One read per build, and the same object each time: no second context.
        assert analyst.calls == 2

    def test_a_failing_context_read_does_not_raise(self):
        analyst = StubAnalyst(error=RuntimeError("builder exploded"))
        report = InstitutionalIntelligenceEngine(analyst).build_full_intelligence(
            None, "TATAMOTORS",
        )
        assert report.ticker == "TATAMOTORS"
        assert report.signals() == []
        assert any("not built" in w for w in report.warnings)
        assert report.render()

    def test_a_non_grounded_context_is_refused_rather_than_interpreted(self):
        """The post-filing path may hand in an analyst whose context is not a
        `GroundedContext`; the engine reports the gap instead of guessing."""
        report = InstitutionalIntelligenceEngine(
            StubAnalyst(object()),
        ).build_full_intelligence()
        assert report.limitations
        assert report.recommendation is None

    def test_falls_back_to_the_analysis_for_identity(self):
        report = InstitutionalIntelligenceEngine().build_full_intelligence(
            _AnalysisStub(), "BEL",
        )
        assert report.ticker == "BEL"
        assert report.company_id == "company-bel"
        assert report.name == "Bharat Electronics Ltd"


class _CompanyStub:
    id = "company-bel"
    ticker = "BEL"
    name = "Bharat Electronics Ltd"


class _AnalysisStub:
    company = _CompanyStub()


# --------------------------------------------------- provider independence

class TestNoExternalProvider:
    def test_the_module_reaches_for_no_provider(self):
        """The requirement is that the foundation works with no external model
        at all, so the module is checked rather than assumed."""
        import pathlib

        import app.services.ai.institutional_intelligence as module

        source = pathlib.Path(module.__file__).read_text(encoding="utf-8").lower()
        for name in ("gemini", "openai", "openrouter", "anthropic", "claude",
                     "app.services.ai.providers"):
            assert name not in source, f"{name} is referenced by the engine"

    def test_it_runs_with_no_router_configured(self, db_session):
        """End to end over the seeded reference company, through an analyst
        whose router raises on any completion call."""
        from tests.test_deterministic_analyst_path import (
            REF, _analyst, _memory, _run,
        )

        analyst, router, docs, analysis = _analyst(db_session, mode="forbid")
        engine = InstitutionalIntelligenceEngine(analyst)

        report = engine.build_full_intelligence(analysis, REF)
        assert router.complete_calls == [], "the engine called a provider"
        assert docs.search_calls == [], "the engine ran RAG retrieval"
        assert report.score is not None
        assert report.overall is not None and report.recommendation is not None
        assert report.evidence_keys()

        # Building it does not disturb the deterministic answer paths.
        phase_1 = _run(analyst.chat("What is the P/E ratio?", _memory()))
        phase_2a = _run(analyst.chat(
            "BEL ki financial quality kaisi hai?", _memory(),
        ))
        assert phase_1.provider == "deterministic"
        assert phase_2a.provider == "deterministic"
        assert router.complete_calls == []
        assert docs.search_calls == []
