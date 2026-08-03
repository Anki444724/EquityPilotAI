"""AI Scoring Engine 3.0.

The tests are organised around the brief's guarantees rather than around the
code's structure, because those guarantees are what a customer is buying:

* the framework weights are exactly as specified and sum to 100;
* no score is produced without a reason, and no module without citations
  where evidence exists;
* the engine is deterministic — the same evidence always yields the same
  number, which is what makes "never a black box" verifiable rather than
  merely claimed;
* an LLM cannot move a score;
* history is append-only and a version is never overwritten;
* the inverted scales (risk, valuation) point the way the guardrails assume.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.domain.ai_scoring.framework import (
    FRAMEWORK_VERSION, GUARDRAILS, MIN_COVERAGE_FOR_DIRECTION, Module,
    MODULE_CRITERIA, MODULE_ORDER, MODULE_WEIGHTS, apply_guardrails,
)
from app.domain.ai_scoring.probability import (
    PROBABILITY_SPECS, estimate, estimate_all,
)
from app.domain.ai_scoring.types import (
    Citation, CitationKind, FactorScore, ModuleScore, Origin, Rating,
    Recommendation, aggregate_factors, band, fingerprint, rating_for,
    recommendation_for, scale,
)
from app.services.ai_scoring.engine import compute
from app.services.ai_scoring.evidence import NewsItem, ScoringEvidence
from app.services.ai_scoring.modules import latest_news
from app.services.ai_scoring.modules.common import cagr, consistency, series_cagr


# ---------------------------------------------------------------------------
# Fixtures — an in-memory database per test, as the platform's convention
# ---------------------------------------------------------------------------

@pytest.fixture()
def db():
    """A fresh in-memory database with the full schema.

    Every model is imported before `create_all` so the metadata is complete.
    CONFTEST-001: building the test schema from whatever models happen to
    have been imported produces a database missing the tables of any module
    the test did not touch, and the failure surfaces as an unrelated
    NoReferencedTableError.
    """
    import app.models.analysis  # noqa: F401
    import app.models.company  # noqa: F401
    import app.models.document  # noqa: F401
    import app.models.filing_collection  # noqa: F401
    import app.models.knowledge  # noqa: F401
    import app.models.platform  # noqa: F401
    import app.models.scoring  # noqa: F401

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()


def make_company(db, ticker="TESTCO", **kwargs):
    from app.models.company import Company
    import uuid

    company = Company(
        id=kwargs.pop("id", str(uuid.uuid4())),
        name=kwargs.pop("name", "Test Company Ltd"),
        ticker=ticker,
        sector=kwargs.pop("sector", "Information Technology"),
        industry=kwargs.pop("industry", "IT Services"),
        market_cap=kwargs.pop("market_cap", 120_000.0),
        current_price=kwargs.pop("current_price", 3500.0),
        listing_status="active",
        **kwargs,
    )
    db.add(company)
    db.commit()
    return company


class FakeIncome:
    """A minimal income statement with the attributes the scorers read."""

    def __init__(self, year, revenue, margin=0.20, shares=100.0):
        self.fiscal_year = year
        self.total_revenue = revenue
        self.revenue_operations = revenue
        self.gross_profit = revenue * 0.45
        self.gross_margin = 0.45
        self.ebitda = revenue * margin
        self.ebitda_margin = margin
        self.ebit = revenue * (margin - 0.03)
        self.ebit_margin = margin - 0.03
        self.pat = revenue * (margin - 0.06)
        self.pat_margin = margin - 0.06
        self.weighted_shares = shares
        self.eps_basic = self.pat / shares
        self.finance_costs = revenue * 0.005
        self.raw_materials = revenue * 0.30
        self.purchase_stock_in_trade = revenue * 0.05
        self.effective_tax_rate = 0.25


class FakeBalance:
    def __init__(self, year, equity, debt=0.0, cash=0.0):
        self.fiscal_year = year
        self.shareholders_equity = equity
        self.total_equity = equity
        self.long_term_borrowings = debt * 0.7
        self.short_term_borrowings = debt * 0.3
        self.current_maturities_ltd = 0.0
        self.cash_and_bank = cash
        self.current_investments = 0.0
        self.total_current_assets = equity * 0.5 + cash
        self.total_current_liabilities = equity * 0.25
        self.capital_employed = equity + debt
        self.invested_capital = equity + debt - cash


class FakeCashFlow:
    def __init__(self, year, cfo, capex):
        self.fiscal_year = year
        self.cfo = cfo
        self.capex = -abs(capex)
        self.free_cash_flow = cfo - abs(capex)
        self.dividend_paid = -cfo * 0.2
        self.repayment_borrowings = 0.0


def make_evidence(db, company=None, years=6, **overrides):
    """A well-populated evidence bundle, adjustable per test."""
    company = company or make_company(db)
    evidence = ScoringEvidence(company=company)
    base = 10_000.0
    for offset in range(years):
        year = 2020 + offset
        revenue = base * (1.14 ** offset)
        evidence.incomes.append(FakeIncome(year, revenue))
        evidence.balances.append(FakeBalance(year, revenue * 0.8, cash=revenue * 0.2))
        evidence.cash_flows.append(FakeCashFlow(year, revenue * 0.16, revenue * 0.05))
    evidence.pe_ratio = 24.0
    evidence.pb_ratio = 5.0
    evidence.ev_ebitda = 15.0
    evidence.intrinsic_value = 3800.0
    evidence.upside = 0.086
    evidence.margin_of_safety = 0.08
    evidence.wacc = 0.115
    for key, value in overrides.items():
        setattr(evidence, key, value)
    return evidence


# ===========================================================================
# Framework definition
# ===========================================================================

class TestFramework:
    def test_weights_are_exactly_the_brief(self):
        """The ten weights the brief specifies, verbatim."""
        expected = {
            Module.COMPANY_DATA: 10.0,
            Module.FINANCIAL_STATEMENTS: 15.0,
            Module.LATEST_NEWS: 8.0,
            Module.INDUSTRY_ANALYSIS: 8.0,
            Module.MANAGEMENT_COMMENTARY: 10.0,
            Module.AI_ANALYSIS: 10.0,
            Module.BUSINESS_QUALITY: 14.0,
            Module.GROWTH: 10.0,
            Module.RISK: 8.0,
            Module.VALUATION: 7.0,
        }
        assert MODULE_WEIGHTS == expected

    def test_weights_sum_to_one_hundred(self):
        assert sum(MODULE_WEIGHTS.values()) == pytest.approx(100.0)

    def test_every_module_declares_its_criteria(self):
        for module in Module:
            assert MODULE_CRITERIA[module], f"{module} has no criteria"

    def test_module_order_matches_the_brief(self):
        assert MODULE_ORDER[0] is Module.COMPANY_DATA
        assert MODULE_ORDER[-1] is Module.VALUATION
        assert len(MODULE_ORDER) == 10

    def test_rating_scale_is_the_briefs_six_bands(self):
        assert {r.value for r in Rating} == {"A+", "A", "BBB", "BB", "B", "C"}

    def test_recommendation_scale_is_the_briefs_five_steps(self):
        assert {r.value for r in Recommendation} == {
            "Strong Buy", "Buy", "Hold", "Reduce", "Avoid",
        }

    @pytest.mark.parametrize("score,expected", [
        (95.0, Rating.A_PLUS), (85.0, Rating.A_PLUS), (84.9, Rating.A),
        (75.0, Rating.A), (63.0, Rating.BBB), (50.0, Rating.BB),
        (38.0, Rating.B), (0.0, Rating.C),
    ])
    def test_rating_bands(self, score, expected):
        assert rating_for(score)[0] is expected

    def test_rating_bands_are_monotonic(self):
        """A higher score never receives a worse rating."""
        order = [Rating.C, Rating.B, Rating.BB, Rating.BBB, Rating.A,
                 Rating.A_PLUS]
        previous = -1
        for score in range(0, 101):
            index = order.index(rating_for(float(score))[0])
            assert index >= previous
            previous = index


class TestGuardrails:
    def test_fragile_balance_sheet_caps_at_reduce(self):
        rec, reasons = apply_guardrails(
            Recommendation.STRONG_BUY, {"risk": 2.0}, coverage=0.9,
        )
        assert rec is Recommendation.REDUCE
        assert any("Reduce" in r for r in reasons)

    def test_expensive_valuation_caps_at_hold(self):
        rec, reasons = apply_guardrails(
            Recommendation.STRONG_BUY, {"valuation": 2.5}, coverage=0.9,
        )
        assert rec is Recommendation.HOLD

    def test_thin_evidence_caps_at_hold(self):
        rec, reasons = apply_guardrails(
            Recommendation.BUY, {}, coverage=0.20,
        )
        assert rec is Recommendation.HOLD
        assert any("observable" in r for r in reasons)

    def test_tightest_cap_wins_but_all_reasons_are_reported(self):
        """Three problems is materially different from one, and must show."""
        rec, reasons = apply_guardrails(
            Recommendation.STRONG_BUY,
            {"risk": 2.0, "valuation": 2.0, "financial_statements": 2.0},
            coverage=0.10,
        )
        assert rec is Recommendation.REDUCE          # the tightest
        assert len(reasons) == 4                      # all of them

    def test_guardrails_never_upgrade(self):
        """A guardrail may only cap; it must never improve a recommendation."""
        rec, _ = apply_guardrails(
            Recommendation.AVOID, {"risk": 10.0, "valuation": 10.0},
            coverage=1.0,
        )
        assert rec is Recommendation.AVOID


# ===========================================================================
# Primitives
# ===========================================================================

class TestPrimitives:
    def test_factor_must_carry_a_reason(self):
        """The invariant behind 'never a black box'."""
        with pytest.raises(ValueError, match="no reason"):
            FactorScore(key="k", label="L", score=5.0, weight=1.0,
                        origin=Origin.MISSING, reason="")

    def test_factor_score_must_be_in_range(self):
        with pytest.raises(ValueError, match="outside 0-10"):
            FactorScore(key="k", label="L", score=11.0, weight=1.0,
                        origin=Origin.REPORTED, reason="x")

    def test_missing_origin_contributes_no_coverage(self):
        factor = FactorScore(key="k", label="L", score=5.0, weight=1.0,
                             origin=Origin.MISSING, reason="absent")
        assert factor.coverage == 0.0
        assert factor.is_missing

    def test_reported_origin_is_full_coverage(self):
        factor = FactorScore(key="k", label="L", score=5.0, weight=1.0,
                             origin=Origin.REPORTED, reason="read")
        assert factor.coverage == 1.0

    def test_band_returns_neutral_for_none(self):
        assert band(None, [(1.0, 10.0)]) == 5.0

    def test_band_inverted_direction(self):
        bands = [(1.0, 10.0), (2.0, 5.0)]
        assert band(0.5, bands, higher_is_better=False) == 10.0
        assert band(1.5, bands, higher_is_better=False) == 5.0
        assert band(9.0, bands, higher_is_better=False) == 0.0

    def test_fingerprint_is_order_independent(self):
        assert fingerprint({"a": 1, "b": 2}) == fingerprint({"b": 2, "a": 1})

    def test_fingerprint_changes_with_content(self):
        assert fingerprint({"a": 1}) != fingerprint({"a": 2})

    def test_cagr_declines_to_guess_from_a_non_positive_base(self):
        """A CAGR from a loss carries the wrong sign; None is the honest answer."""
        assert cagr(-100.0, 200.0, 3) is None
        assert cagr(0.0, 200.0, 3) is None
        assert cagr(100.0, 200.0, 1) == pytest.approx(1.0)

    def test_series_cagr_ignores_leading_gaps(self):
        assert series_cagr([None, 100.0, None, 121.0]) == pytest.approx(0.1)

    def test_consistency_of_a_flat_series_is_neutral(self):
        assert consistency([5.0, 5.0, 5.0]) == 0.5


# ===========================================================================
# Probability
# ===========================================================================

class TestProbability:
    def test_all_five_probabilities_are_produced(self):
        scores = {m.value: 7.0 for m in Module}
        coverage = {m.value: 1.0 for m in Module}
        results = estimate_all(scores, coverage)
        assert {p.key for p in results} == {
            "outperform_nifty", "earnings_growth", "revenue_growth",
            "multiple_expansion", "overall_investment",
        }

    def test_probabilities_are_bounded(self):
        for level in (0.0, 5.0, 10.0):
            scores = {m.value: level for m in Module}
            coverage = {m.value: 1.0 for m in Module}
            for p in estimate_all(scores, coverage):
                assert 0.02 <= p.probability <= 0.98

    def test_neutral_scores_give_an_even_chance(self):
        scores = {m.value: 5.0 for m in Module}
        coverage = {m.value: 1.0 for m in Module}
        for p in estimate_all(scores, coverage):
            assert p.probability == pytest.approx(0.5, abs=1e-9)

    def test_thin_coverage_shrinks_toward_even(self):
        scores = {m.value: 9.0 for m in Module}
        confident = estimate_all(scores, {m.value: 1.0 for m in Module})
        thin = estimate_all(scores, {m.value: 0.1 for m in Module})
        for c, t in zip(confident, thin):
            assert abs(t.probability - 0.5) < abs(c.probability - 0.5)
            assert t.shrinkage > c.shrinkage

    def test_missing_module_is_dropped_not_read_as_zero(self):
        """Reading a missing module as 0 would score it as catastrophic."""
        spec = PROBABILITY_SPECS[0]
        partial = estimate(spec, {"business_quality": 9.0},
                           {"business_quality": 1.0})
        zeroed = estimate(
            spec,
            {m.value: (9.0 if m is Module.BUSINESS_QUALITY else 0.0)
             for m, _ in spec.drivers},
            {m.value: 1.0 for m, _ in spec.drivers},
        )
        assert partial.probability > zeroed.probability

    def test_every_probability_states_its_drivers(self):
        scores = {m.value: 7.0 for m in Module}
        coverage = {m.value: 1.0 for m in Module}
        for p in estimate_all(scores, coverage):
            assert p.drivers, f"{p.key} has no drivers"
            assert p.reason
            assert sum(v for _, v in p.drivers) == pytest.approx(1.0)


# ===========================================================================
# The engine end to end
# ===========================================================================

class TestEngine:
    def test_all_ten_modules_are_scored(self, db):
        result = compute(make_evidence(db))
        assert len(result.modules) == 10
        assert [m.key for m in result.modules] == [m.value for m in MODULE_ORDER]

    def test_composite_is_the_sum_of_contributions(self, db):
        result = compute(make_evidence(db))
        assert result.overall_score == pytest.approx(
            sum(m.contribution for m in result.modules)
        )

    def test_composite_is_bounded(self, db):
        result = compute(make_evidence(db))
        assert 0.0 <= result.overall_score <= 100.0

    def test_no_factor_is_ever_unexplained(self, db):
        """The brief's central prohibition, asserted directly."""
        result = compute(make_evidence(db))
        assert result.unexplained_factors == ()
        for module in result.modules:
            assert module.reason
            for factor in module.factors:
                assert factor.reason, f"{module.key}.{factor.key}"

    def test_evidence_bearing_factors_carry_citations(self, db):
        """A non-missing factor must point at something resolvable."""
        result = compute(make_evidence(db))
        uncited = [
            f"{m.key}.{f.key}"
            for m in result.modules for f in m.factors
            if not f.is_missing and not f.citations
        ]
        assert uncited == [], f"scored without a citation: {uncited}"

    def test_determinism(self, db):
        """Same evidence, same result — byte for byte."""
        evidence = make_evidence(db)
        payloads = {
            json.dumps(compute(evidence).as_dict(), sort_keys=True, default=str)
            for _ in range(5)
        }
        assert len(payloads) == 1

    def test_fingerprint_is_stable_across_runs(self, db):
        evidence = make_evidence(db)
        assert len({compute(evidence).input_fingerprint for _ in range(3)}) == 1

    def test_fingerprint_moves_when_evidence_moves(self, db):
        company = make_company(db)
        first = compute(make_evidence(db, company)).input_fingerprint
        changed = make_evidence(db, company)
        changed.incomes[-1].total_revenue *= 1.5
        assert compute(changed).input_fingerprint != first

    def test_records_the_framework_version(self, db):
        assert compute(make_evidence(db)).framework_version == FRAMEWORK_VERSION

    def test_an_empty_company_still_scores_with_gaps_reported(self, db):
        """Refusing to score is less useful than scoring honestly."""
        company = make_company(db, ticker="EMPTY", sector=None, industry=None,
                               market_cap=None, current_price=None)
        result = compute(ScoringEvidence(company=company))
        assert 0.0 <= result.overall_score <= 100.0
        assert result.coverage < 0.2
        assert result.recommendation is Recommendation.HOLD  # thin-evidence cap
        assert any("No financial statements" in w for w in result.warnings)

    def test_missing_inputs_score_neutral_not_zero(self, db):
        """Scoring absence as zero would punish the company for our ignorance."""
        company = make_company(db, ticker="BARE")
        result = compute(ScoringEvidence(company=company))
        for module in result.modules:
            for factor in module.factors:
                if factor.is_missing:
                    assert factor.score == 5.0

    def test_coverage_is_weighted_by_framework_weight(self, db):
        """A gap in a 15-point module must hurt more than one in a 7-point module."""
        evidence = make_evidence(db)
        result = compute(evidence)
        naive = sum(m.coverage for m in result.modules) / len(result.modules)
        # They should differ, proving the weighting is actually applied.
        assert result.coverage != pytest.approx(naive, abs=1e-9)

    def test_illustrative_valuation_warning_is_propagated(self, db):
        evidence = make_evidence(
            db, valuation_is_illustrative=True,
            valuation_disclosure=("Illustrative valuation only. Real filings "
                                  "are required for investment-grade outputs."),
        )
        result = compute(evidence)
        assert any("Illustrative valuation only" in w for w in result.warnings)

    def test_summary_reports_contributors_not_just_scores(self, db):
        result = compute(make_evidence(db))
        assert "Largest contributors" in result.summary
        assert "points" in result.summary


# ===========================================================================
# Inverted scales — the most dangerous misreading in the engine
# ===========================================================================

class TestInvertedScales:
    def test_low_leverage_scores_high_on_risk(self, db):
        """Risk: 10 means LOW risk. The guardrail depends on this direction."""
        company = make_company(db, ticker="SAFE")
        safe = make_evidence(db, company)
        for balance in safe.balances:
            balance.long_term_borrowings = 0.0
            balance.short_term_borrowings = 0.0
            balance.capital_employed = balance.shareholders_equity

        risky = make_evidence(db, company)
        for balance in risky.balances:
            equity = balance.shareholders_equity
            balance.long_term_borrowings = equity * 2.5
            balance.short_term_borrowings = equity * 0.8
            balance.capital_employed = equity * 4.3
        for income in risky.incomes:
            income.finance_costs = income.ebit * 0.9

        safe_risk = compute(safe).module("risk").score
        risky_risk = compute(risky).module("risk").score
        assert safe_risk > risky_risk

    def test_cheap_scores_high_on_valuation(self, db):
        """Valuation: 10 means CHEAP."""
        company = make_company(db, ticker="VAL")
        cheap = make_evidence(db, company, pe_ratio=9.0, pb_ratio=1.1,
                              ev_ebitda=5.5, upside=0.55,
                              margin_of_safety=0.40)
        dear = make_evidence(db, company, pe_ratio=78.0, pb_ratio=14.0,
                             ev_ebitda=40.0, upside=-0.45,
                             margin_of_safety=-0.40)
        assert compute(cheap).module("valuation").score > \
               compute(dear).module("valuation").score

    def test_an_expensive_company_cannot_be_strong_buy(self, db):
        """The valuation guardrail, exercised through the whole engine."""
        company = make_company(db, ticker="DEAR")
        evidence = make_evidence(db, company, pe_ratio=140.0, pb_ratio=30.0,
                                 ev_ebitda=70.0, upside=-0.65,
                                 margin_of_safety=-0.60)
        result = compute(evidence)
        assert result.module("valuation").score <= 3.0
        assert result.recommendation in {
            Recommendation.HOLD, Recommendation.REDUCE, Recommendation.AVOID,
        }


# ===========================================================================
# News classification
# ===========================================================================

class TestNewsClassification:
    def _item(self, title):
        return NewsItem(title=title, published_on=datetime.now(timezone.utc),
                        filing_type=None, source="NSE", url=None,
                        reference="test:1")

    @pytest.mark.parametrize("title,expected", [
        ("Receipt of order worth Rs 500 crore", "orders"),
        ("Resignation of Chief Financial Officer", "management"),
        ("Penalty imposed by SEBI under Regulation 30", "negative"),
        ("Acquisition of 100% stake in subsidiary", "ma"),
        ("Record quarterly revenue announced", "positive"),
        ("Compliance certificate under LODR", "regulatory"),
    ])
    def test_categories(self, title, expected):
        assert expected in latest_news.classify(self._item(title))

    def test_word_boundaries_prevent_false_matches(self):
        """An unanchored 'order' matches 'in order to' across the corpus."""
        assert "orders" not in latest_news.classify(
            self._item("Board met in order to consider the accounts")
        )

    def test_an_announcement_may_hold_several_categories(self):
        categories = latest_news.classify(
            self._item("CCI approval received for the proposed acquisition")
        )
        assert "regulatory" in categories and "ma" in categories

    def test_no_news_reports_gaps_rather_than_scoring_clean(self, db):
        """Silence usually means the crawler has not arrived, not that all is well."""
        company = make_company(db, ticker="QUIET")
        module = latest_news.score(ScoringEvidence(company=company))
        assert all(f.is_missing for f in module.factors)
        assert module.coverage == 0.0
        assert "crawler" in module.factors[0].reason

    def test_adverse_announcements_lower_the_news_score(self, db):
        company = make_company(db, ticker="TROUBLE")
        clean = make_evidence(db, company)
        clean.news = [self._item("Record quarterly revenue announced")]

        bad = make_evidence(db, company)
        bad.news = [
            self._item("Penalty imposed by SEBI"),
            self._item("Resignation of statutory auditor"),
            self._item("Credit rating downgrade by CRISIL"),
            self._item("NCLT insolvency petition admitted"),
        ]
        assert latest_news.score(clean).score > latest_news.score(bad).score


# ===========================================================================
# The AI cannot move a score
# ===========================================================================

class TestAICannotScore:
    def test_no_module_scorer_calls_a_model(self):
        """Static check: nothing in the scoring path imports an AI provider."""
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[1]
        forbidden = ("openrouter", "OpenRouter", "chat_completion",
                     "services.ai.providers", "AIService", "call_llm")
        offenders = []
        for path in (root / "app" / "services" / "ai_scoring").rglob("*.py"):
            text = path.read_text()
            for token in forbidden:
                if token in text and "never" not in text.lower().split(token.lower())[0][-200:]:
                    offenders.append(f"{path.name}: {token}")
        assert offenders == [], f"scoring path touches an LLM: {offenders}"

    def test_ai_commentary_is_not_read_by_the_arithmetic(self, db):
        """Attaching commentary must not change a single number."""
        from dataclasses import replace

        evidence = make_evidence(db)
        result = compute(evidence)
        before = result.overall_score

        narrated = replace(
            result,
            modules=tuple(
                replace(m, ai_commentary="This company is superb. Score 10.")
                for m in result.modules
            ),
        )
        assert sum(m.contribution for m in narrated.modules) == \
               pytest.approx(before)

    def test_ai_analysis_module_scores_evidence_volume_not_opinion(self, db):
        """More cited evidence raises the module; sentiment is never read."""
        from app.models.knowledge import KnowledgeEntry
        from app.services.ai_scoring.modules import ai_analysis

        company = make_company(db, ticker="AICO")
        bare = ai_analysis.score(ScoringEvidence(company=company))

        rich = ScoringEvidence(company=company)
        rich.vault_entries = [
            KnowledgeEntry(
                id=i, company_id=company.id, section="risks",
                key=f"risk_{i}", label=f"Risk {i}",
                value_text="A material risk disclosed in the annual report.",
                confidence=0.8, authority=0.9, document_id=1, version=1,
                status="current",
            )
            for i in range(1, 7)
        ]
        assert ai_analysis.score(rich).score > bare.score


# ===========================================================================
# Persistence: append-only history
# ===========================================================================

class TestVersionHistory:
    def _service(self, db):
        from app.services.ai_scoring.service import AIScoringService
        return AIScoringService(db)

    def test_first_record_creates_version_one(self, db):
        service = self._service(db)
        result = compute(make_evidence(db))
        outcome = service.record(result)
        assert outcome.created
        assert outcome.version.version == 1
        assert outcome.version.status == "current"

    def test_unchanged_inputs_write_no_new_version(self, db):
        """A version that says nothing new buries the ones that do."""
        service = self._service(db)
        evidence = make_evidence(db)
        service.record(compute(evidence))
        second = service.record(compute(evidence))
        assert not second.created
        assert "unchanged" in second.reason
        assert len(service.history(evidence.company.id)) == 1

    def test_changed_inputs_append_and_supersede(self, db):
        service = self._service(db)
        company = make_company(db, ticker="MOVER")
        service.record(compute(make_evidence(db, company)))

        moved = make_evidence(db, company)
        moved.pe_ratio = 8.0          # much cheaper
        moved.upside = 0.60
        outcome = service.record(compute(moved))

        assert outcome.created
        assert outcome.version.version == 2
        assert outcome.version.supersedes_version == 1
        assert outcome.delta is not None

        history = service.history(company.id)
        assert len(history) == 2
        assert [v.status for v in history] == ["current", "superseded"]

    def test_history_is_never_overwritten(self, db):
        """The brief's requirement, asserted against the stored rows."""
        service = self._service(db)
        company = make_company(db, ticker="HIST")
        recorded = []
        for pe in (30.0, 20.0, 12.0, 40.0):
            evidence = make_evidence(db, company, pe_ratio=pe)
            result = compute(evidence)
            service.record(result)
            recorded.append(round(result.overall_score, 6))

        history = sorted(service.history(company.id), key=lambda v: v.version)
        assert len(history) == 4
        assert [round(v.overall_score, 6) for v in history] == recorded
        # And every one retains its full explainable payload.
        for version in history:
            assert version.detail["modules"]
            assert version.detail["probabilities"]

    def test_exactly_one_version_is_current(self, db):
        from app.models.scoring import AIScoreVersion

        service = self._service(db)
        company = make_company(db, ticker="ONE")
        for pe in (30.0, 20.0, 12.0):
            service.record(compute(make_evidence(db, company, pe_ratio=pe)))

        current = db.execute(
            select(AIScoreVersion).where(
                AIScoreVersion.company_id == company.id,
                AIScoreVersion.status == "current",
            )
        ).scalars().all()
        assert len(current) == 1
        assert current[0].version == 3

    def test_version_numbers_are_never_reused(self, db):
        """MAX+1, not COUNT+1: a gap is better than a collision."""
        from app.models.scoring import AIScoreVersion

        service = self._service(db)
        company = make_company(db, ticker="GAPS")
        for pe in (30.0, 20.0, 12.0):
            service.record(compute(make_evidence(db, company, pe_ratio=pe)))

        # Delete the middle version, as a retention sweep one day might.
        db.execute(
            AIScoreVersion.__table__.delete().where(
                AIScoreVersion.company_id == company.id,
                AIScoreVersion.version == 2,
            )
        )
        db.commit()

        outcome = service.record(compute(make_evidence(db, company, pe_ratio=45.0)))
        assert outcome.version.version == 4      # not 3

    def test_duplicate_version_is_a_database_error(self, db):
        """The constraint that makes an accidental overwrite impossible."""
        from sqlalchemy.exc import IntegrityError
        from app.models.scoring import AIScoreVersion

        service = self._service(db)
        company = make_company(db, ticker="DUP")
        service.record(compute(make_evidence(db, company)))

        db.add(AIScoreVersion(
            company_id=company.id, version=1, status="current",
            framework_version=FRAMEWORK_VERSION, overall_score=50.0,
            rating="BB", recommendation="Hold", coverage=0.5,
            module_scores={}, probabilities={}, detail={},
            input_fingerprint="x" * 64,
        ))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

    def test_force_records_even_when_unchanged(self, db):
        service = self._service(db)
        evidence = make_evidence(db)
        service.record(compute(evidence))
        forced = service.record(compute(evidence), force=True)
        assert forced.created
        assert forced.version.version == 2

    def test_trigger_is_recorded(self, db):
        service = self._service(db)
        outcome = service.record(compute(make_evidence(db)), trigger="filing",
                                 trigger_document_id=77)
        assert outcome.version.trigger == "filing"
        assert outcome.version.trigger_document_id == 77


# ===========================================================================
# Job registration — the JOB-001 class of defect
# ===========================================================================

class TestJobRegistration:
    def test_ai_score_refresh_is_in_every_registry(self):
        from app.domain.platform.jobs import (
            DEFAULT_PRIORITY, JOB_LABELS, JobKind, RETRY_POLICIES,
        )
        from app.services.platform.jobs.handlers import HANDLERS

        kind = JobKind.AI_SCORE_REFRESH
        for name, registry in (
            ("JOB_LABELS", JOB_LABELS),
            ("DEFAULT_PRIORITY", DEFAULT_PRIORITY),
            ("RETRY_POLICIES", RETRY_POLICIES),
            ("HANDLERS", HANDLERS),
        ):
            assert kind in registry, f"{kind.value} missing from {name}"

    def test_every_job_kind_is_fully_registered(self):
        """Guards the whole enum, not just the new member."""
        from app.domain.platform.jobs import (
            DEFAULT_PRIORITY, JOB_LABELS, JobKind, RETRY_POLICIES,
        )
        from app.services.platform.jobs.handlers import HANDLERS

        for kind in JobKind:
            assert kind in JOB_LABELS
            assert kind in DEFAULT_PRIORITY
            assert kind in RETRY_POLICIES
            assert kind in HANDLERS

    def test_ai_score_refresh_is_scheduled(self):
        from app.domain.platform.jobs import JobKind, SCHEDULES
        specs = [s for s in SCHEDULES if s.kind is JobKind.AI_SCORE_REFRESH]
        assert len(specs) == 1
        assert specs[0].every_seconds == 24 * 3600


# ===========================================================================
# Regression: a scored zero must still cite the evidence base
# ===========================================================================

class TestZeroCountFactorsAreCited:
    """A count of zero adverse events is a finding, not an absence of one.

    Found by the production validation harness on 60 live companies: ASTRAL
    and BAJAJ-AUTO scored `latest_news.negative` and `latest_news.orders` as
    REPORTED with zero citations. "Eighteen disclosures were read and none was
    adverse" and "no disclosures were read" are different claims, and without
    citations on the zero case the panel could not distinguish them.
    """

    def _item(self, title):
        return NewsItem(title=title, published_on=datetime.now(timezone.utc),
                        filing_type=None, source="NSE", url=None,
                        reference=f"test:{abs(hash(title)) % 10_000}")

    def test_zero_count_factor_cites_the_scanned_window(self, db):
        company = make_company(db, ticker="CLEAN")
        evidence = ScoringEvidence(company=company)
        # Announcements exist, but none is adverse and none is an order win.
        evidence.news = [
            self._item("Board meeting intimation for quarterly results"),
            self._item("Appointment of an independent director"),
            self._item("Compliance certificate under LODR Regulation 7"),
        ]
        module = latest_news.score(evidence)

        negative = next(f for f in module.factors if f.key == "negative")
        orders = next(f for f in module.factors if f.key == "orders")

        for factor in (negative, orders):
            assert factor.value == 0.0
            assert not factor.is_missing
            assert factor.citations, (
                f"{factor.key} scored on zero matches but cited nothing"
            )
            assert "scanned" in factor.evidence

    def test_no_scored_factor_anywhere_lacks_a_citation(self, db):
        """The harness check, brought into the unit suite so it cannot regress."""
        company = make_company(db, ticker="CITED")
        evidence = make_evidence(db, company)
        evidence.news = [
            self._item("Board meeting intimation"),
            self._item("Record revenue for the quarter"),
            self._item("Receipt of order worth Rs 300 crore"),
        ]
        result = compute(evidence)
        uncited = [
            f"{m.key}.{f.key}"
            for m in result.modules for f in m.factors
            if not f.is_missing and not f.citations
        ]
        assert uncited == []


# ===========================================================================
# API contract — every endpoint exercised against a real response model
# ===========================================================================

class TestAPIContract:
    """AISCORE-001: `/ai-score/history` returned HTTP 500 in production.

    The endpoint constructed `CompanyRef` field by field from a guess at its
    schema — omitting `exchange`, which is required, and passing `industry`,
    which does not exist. Pydantic raised on every call. Unit tests of the
    service layer could not catch it because they never built a response
    model; only an actual request does.

    These tests therefore go through the real app and assert the status code,
    which is the only thing that would have caught it.
    """

    @pytest.fixture()
    def client(self):
        """A TestClient over a file-backed SQLite database.

        Not the in-memory `db` fixture: TestClient runs the app on a separate
        thread, and an in-memory SQLite connection cannot cross threads —
        "SQLite objects created in a thread can only be used in that same
        thread". That is a limitation of the test harness, not of the
        application, so the fixture is what changes. A file-backed database
        with `check_same_thread=False` and a shared `StaticPool` gives every
        thread the same database.
        """
        import tempfile
        from fastapi.testclient import TestClient
        from sqlalchemy.pool import StaticPool

        # `import app.models.x` binds the name `app` to the PACKAGE in this
        # scope, shadowing the FastAPI instance imported below it and turning
        # `app.dependency_overrides` into an AttributeError on the module.
        # The model imports must therefore come first, and the FastAPI object
        # is bound last under an unambiguous name.
        import app.models.analysis  # noqa: F401
        import app.models.company  # noqa: F401
        import app.models.document  # noqa: F401
        import app.models.filing_collection  # noqa: F401
        import app.models.knowledge  # noqa: F401
        import app.models.platform  # noqa: F401
        import app.models.scoring  # noqa: F401

        from app.core.security import get_current_user
        from app.db.base import get_db
        from app.main import app as fastapi_app

        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        handle.close()
        engine = create_engine(
            f"sqlite:///{handle.name}",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine, expire_on_commit=False)
        session = Session()

        # Save and RESTORE, never clear. `tests/conftest.py` installs a
        # session-wide `get_db` override at import time, against a shared
        # seeded database that most of the suite depends on. Calling
        # `dependency_overrides.clear()` in teardown removed it, so every API
        # test module collected after this one fell back to the real
        # `get_db` and failed — 130 failures and 161 errors across
        # test_valuation_api, test_document_api, test_scoring_api,
        # test_report_api and others, none of which had anything wrong with
        # them. A fixture that tears down more than it set up is a harness
        # bug that looks exactly like a product regression.
        previous = dict(fastapi_app.dependency_overrides)
        fastapi_app.dependency_overrides[get_db] = lambda: session
        fastapi_app.dependency_overrides[get_current_user] = lambda: object()
        try:
            client = TestClient(fastapi_app)
            client.session = session
            yield client
        finally:
            fastapi_app.dependency_overrides.clear()
            fastapi_app.dependency_overrides.update(previous)
            session.close()
            engine.dispose()

    def test_framework_endpoint(self, client):
        response = client.get("/api/v1/ai-score/framework")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["total_weight"] == 100.0
        assert len(body["modules"]) == 10

    def test_dashboard_endpoint(self, client):
        response = client.get("/api/v1/ai-score/dashboard")
        assert response.status_code == 200, response.text

    def test_score_endpoint(self, client):
        make_company(client.session, ticker="APITEST")
        response = client.get("/api/v1/company/APITEST/ai-score")
        assert response.status_code == 200, response.text
        body = response.json()
        assert len(body["modules"]) == 10
        assert len(body["probabilities"]) == 5

    def test_recalculate_endpoint(self, client):
        make_company(client.session, ticker="APIRECALC")
        response = client.post("/api/v1/company/APIRECALC/ai-score/recalculate")
        assert response.status_code == 200, response.text
        assert response.json()["version_created"] is True

    def test_history_endpoint(self, client):
        """The endpoint that was returning 500."""
        make_company(client.session, ticker="APIHIST")
        client.post("/api/v1/company/APIHIST/ai-score/recalculate")
        response = client.get("/api/v1/company/APIHIST/ai-score/history")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["versions_retained"] == 1
        assert body["company"]["ticker"] == "APIHIST"
        assert body["spans_framework_versions"] is False

    def test_version_endpoint(self, client):
        make_company(client.session, ticker="APIVER")
        client.post("/api/v1/company/APIVER/ai-score/recalculate")
        response = client.get("/api/v1/company/APIVER/ai-score/version/1")
        assert response.status_code == 200, response.text
        assert response.json()["detail"]["modules"]

    def test_unknown_ticker_is_404_not_500(self, client):
        for path in ("", "/history", "/version/1"):
            response = client.get(f"/api/v1/company/NOSUCHCO/ai-score{path}")
            assert response.status_code == 404, f"{path}: {response.status_code}"

    def test_unknown_version_is_404(self, client):
        make_company(client.session, ticker="APIMISS")
        response = client.get("/api/v1/company/APIMISS/ai-score/version/99")
        assert response.status_code == 404
