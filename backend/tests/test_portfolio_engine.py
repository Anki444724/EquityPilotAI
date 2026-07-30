"""Unit tests for the portfolio domain layer.

Pure engines only: position replay, allocation, risk, performance and the alert
rules. No database, no HTTP.

Several tests pin defects found while building the module and say so. A
regression test whose motivation is undocumented is deleted by the next person
who finds it inconvenient.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta

import pytest

from app.domain.portfolio import allocation as alloc
from app.domain.portfolio import risk as R
from app.domain.portfolio.alerts import (
    ALL_RULES, DEFAULT_MAX_POSITION, EVENT_RULES, LIVE_RULES, PORTFOLIO_RULES,
    RATING_MAX_POSITION, RULES_BY_KEY, AlertEngine, AlertRule,
    build_position_metrics, max_position_for_rating,
)
from app.domain.portfolio.performance import (
    CashFlow, annualise, brinson_attribution, contribution_analysis,
    drawdown_series, money_weighted_return, rolling_returns,
    time_weighted_return, xirr, xnpv,
)
from app.domain.portfolio.positions import PositionEngine, enrich, sort_key
from app.domain.portfolio.types import (
    AlertCategory, AlertSeverity, AlertStatus, AllocationDimension,
    Comparator, CostBasisMethod, InsufficientHolding, InvalidTransaction,
    MarketCapBand, Position, ReturnPoint, StyleBucket, TransactionType,
)


@dataclass
class T:
    """Minimal transaction satisfying the engine's structural protocol."""

    ticker: str
    txn_type: str
    trade_date: date
    quantity: float = 0.0
    price: float = 0.0
    fees: float = 0.0
    taxes: float = 0.0
    ratio_from: float | None = None
    ratio_to: float | None = None
    sequence: int = 0


def _pos(ticker: str, value: float, **kwargs) -> Position:
    position = Position(ticker=ticker, quantity=1.0, cost=value, **kwargs)
    position.current_price = value
    return position


# ===========================================================================
# Position engine
# ===========================================================================
class TestPositionEngine:
    def test_buy_capitalises_fees_into_cost(self):
        """A round trip at an unchanged price must show the loss it is."""
        result = PositionEngine().replay([
            T("TCS", "buy", date(2024, 1, 1), 100, 3200, fees=160)
        ])
        position = result.positions["TCS"]
        assert position.quantity == 100
        assert position.cost == pytest.approx(320_160)
        assert position.average_cost == pytest.approx(3201.60)

    def test_fifo_relieves_the_earliest_lot(self):
        result = PositionEngine().replay([
            T("TCS", "buy", date(2023, 1, 10), 100, 3200, fees=160),
            T("TCS", "buy", date(2023, 6, 15), 50, 3400, fees=85),
            T("TCS", "sell", date(2024, 2, 20), 80, 4100, fees=205),
        ])
        trade = result.realised[0]
        assert trade.quantity == 80
        assert trade.cost_per_unit == pytest.approx(3201.60)
        # 20 of the first lot plus all 50 of the second survive.
        assert result.positions["TCS"].quantity == pytest.approx(70)
        assert result.positions["TCS"].cost == pytest.approx(
            20 * 3201.60 + 50 * 3401.70
        )

    def test_realised_pnl_matches_hand_calculation(self):
        result = PositionEngine().replay([
            T("TCS", "buy", date(2023, 1, 10), 100, 3200, fees=160),
            T("TCS", "sell", date(2024, 2, 20), 80, 4100, fees=205),
        ])
        net_per_unit = (80 * 4100 - 205) / 80
        assert result.realised_pnl == pytest.approx(
            80 * (net_per_unit - 3201.60)
        )

    def test_weighted_average_differs_from_fifo(self):
        ledger = [
            T("TCS", "buy", date(2023, 1, 10), 100, 3000),
            T("TCS", "buy", date(2023, 6, 15), 100, 4000),
            T("TCS", "sell", date(2024, 2, 20), 100, 5000),
        ]
        fifo = PositionEngine(CostBasisMethod.FIFO).replay(ledger)
        avg = PositionEngine(CostBasisMethod.WEIGHTED_AVERAGE).replay(ledger)
        assert fifo.realised_pnl == pytest.approx(200_000)   # relieved at 3000
        assert avg.realised_pnl == pytest.approx(150_000)    # relieved at 3500

    def test_bonus_raises_quantity_and_holds_cost(self):
        """Free shares: total cost is unchanged, cost per unit falls."""
        result = PositionEngine().replay([
            T("REL", "buy", date(2023, 3, 1), 200, 2400, fees=240),
            T("REL", "bonus", date(2023, 9, 1), ratio_from=2, ratio_to=1),
        ])
        position = result.positions["REL"]
        assert position.quantity == pytest.approx(300)
        assert position.cost == pytest.approx(480_240)
        assert position.average_cost == pytest.approx(1600.80)

    def test_split_rescales_both_sides(self):
        result = PositionEngine().replay([
            T("INFY", "buy", date(2024, 1, 5), 150, 1500),
            T("INFY", "split", date(2024, 8, 1), ratio_from=1, ratio_to=2),
        ])
        position = result.positions["INFY"]
        assert position.quantity == pytest.approx(300)
        assert position.cost == pytest.approx(225_000)
        assert position.average_cost == pytest.approx(750)

    def test_corporate_action_before_a_sale_relieves_the_adjusted_cost(self):
        """Ordering is the whole game: a bonus changes later FIFO relief."""
        result = PositionEngine().replay([
            T("REL", "buy", date(2023, 3, 1), 200, 2400),
            T("REL", "bonus", date(2023, 9, 1), ratio_from=2, ratio_to=1),
            T("REL", "sell", date(2024, 1, 5), 300, 2000),
        ])
        # Cost per share is 1600 after the bonus, so selling all 300 at 2000
        # realises 300 x 400, not a loss against the pre-bonus 2400.
        assert result.realised_pnl == pytest.approx(120_000)
        assert not result.positions["REL"].is_open

    def test_dividends_are_income_not_a_cost_reduction(self):
        """Netting dividends against cost overstates return on a yield stock."""
        result = PositionEngine().replay([
            T("TCS", "buy", date(2023, 1, 10), 100, 3000),
            T("TCS", "dividend", date(2023, 7, 1), 100, 25),
        ])
        position = result.positions["TCS"]
        assert position.cost == pytest.approx(300_000)
        assert position.dividends == pytest.approx(2_500)

    def test_dividend_tax_is_withheld(self):
        result = PositionEngine().replay([
            T("TCS", "buy", date(2023, 1, 10), 100, 3000),
            T("TCS", "dividend", date(2023, 7, 1), 100, 25, taxes=250),
        ])
        assert result.positions["TCS"].dividends == pytest.approx(2_250)

    def test_overselling_is_refused(self):
        """A negative position would misstate every weight in the book."""
        with pytest.raises(InsufficientHolding):
            PositionEngine().replay([
                T("TCS", "buy", date(2023, 1, 10), 50, 3000),
                T("TCS", "sell", date(2023, 2, 10), 80, 3200),
            ])

    def test_a_ratio_event_needs_a_ratio(self):
        with pytest.raises(InvalidTransaction):
            PositionEngine().replay([
                T("TCS", "buy", date(2023, 1, 10), 50, 3000),
                T("TCS", "bonus", date(2023, 2, 10)),
            ])

    def test_cash_ledger_balances(self):
        result = PositionEngine().replay([
            T("", "deposit", date(2023, 1, 1), 1, 1_000_000),
            T("TCS", "buy", date(2023, 1, 10), 100, 3200, fees=160),
            T("TCS", "sell", date(2024, 2, 20), 80, 4100, fees=205),
            T("TCS", "dividend", date(2023, 7, 1), 100, 24),
            T("", "withdrawal", date(2024, 3, 1), 1, 50_000),
        ])
        expected = (
            1_000_000 - (100 * 3200 + 160) + (80 * 4100 - 205)
            + 100 * 24 - 50_000
        )
        assert result.cash.balance == pytest.approx(expected)
        assert result.cash.net_invested == pytest.approx(950_000)

    def test_same_day_order_is_deterministic(self):
        """Row order is not a guarantee; `sequence` is the tiebreak."""
        ledger = [
            T("TCS", "sell", date(2023, 5, 1), 50, 4000, sequence=1),
            T("TCS", "buy", date(2023, 5, 1), 100, 3000, sequence=0),
        ]
        assert [t.txn_type for t in sorted(ledger, key=sort_key)] == ["buy", "sell"]
        result = PositionEngine().replay(ledger)
        assert result.positions["TCS"].quantity == pytest.approx(50)

    def test_fully_closed_position_is_not_open(self):
        result = PositionEngine().replay([
            T("TCS", "buy", date(2023, 1, 10), 100, 3000),
            T("TCS", "sell", date(2023, 6, 10), 100, 3500),
        ])
        assert not result.positions["TCS"].is_open
        assert result.positions["TCS"].cost == 0.0

    def test_float_residue_from_a_split_does_not_leave_a_ghost(self):
        """A 1:3 split leaves 1e-13 shares behind without the epsilon guard."""
        result = PositionEngine().replay([
            T("X", "buy", date(2023, 1, 1), 300, 100),
            T("X", "split", date(2023, 2, 1), ratio_from=3, ratio_to=1),
            T("X", "sell", date(2023, 3, 1), 100, 400),
        ])
        assert not result.positions["X"].is_open

    def test_long_term_classification(self):
        result = PositionEngine().replay([
            T("A", "buy", date(2023, 1, 1), 10, 100),
            T("A", "sell", date(2024, 1, 5), 10, 120),
            T("B", "buy", date(2024, 1, 1), 10, 100),
            T("B", "sell", date(2024, 6, 1), 10, 120),
        ])
        by_ticker = {t.ticker: t for t in result.realised}
        assert by_ticker["A"].is_long_term      # 369 days
        assert not by_ticker["B"].is_long_term  # 152 days

    def test_rights_issue_behaves_as_a_buy(self):
        result = PositionEngine().replay([
            T("X", "buy", date(2023, 1, 1), 100, 500),
            T("X", "rights", date(2023, 6, 1), 20, 400),
        ])
        assert result.positions["X"].quantity == pytest.approx(120)
        assert result.positions["X"].cost == pytest.approx(100 * 500 + 20 * 400)

    def test_enrich_leaves_an_unpriced_holding_unvalued(self):
        """A missing price must not silently value a holding at nil."""
        position = Position(ticker="X", quantity=10, cost=1000)
        enrich([position], {}, {})
        assert position.current_price is None
        assert position.market_value is None
        assert position.unrealised_pnl is None

    def test_total_pnl_combines_all_three_sources(self):
        result = PositionEngine().replay([
            T("TCS", "buy", date(2023, 1, 10), 100, 3000),
            T("TCS", "dividend", date(2023, 7, 1), 100, 25),
            T("TCS", "sell", date(2024, 1, 10), 40, 3500),
        ])
        position = result.positions["TCS"]
        position.current_price = 3600.0
        assert position.total_pnl == pytest.approx(
            position.unrealised_pnl + position.realised_pnl + position.dividends
        )


# ===========================================================================
# Allocation
# ===========================================================================
class TestAllocation:
    @pytest.mark.parametrize("cap,expected", [
        (200_000, MarketCapBand.LARGE),
        (25_000, MarketCapBand.MID),
        (5_000, MarketCapBand.SMALL),
        (500, MarketCapBand.MICRO),
        (None, MarketCapBand.UNKNOWN),
    ])
    def test_market_cap_bands(self, cap, expected):
        assert alloc.market_cap_band(cap) is expected

    def test_style_needs_a_score_to_clear_the_threshold(self):
        assert alloc.style_bucket(80, 40, 40) is StyleBucket.VALUE
        assert alloc.style_bucket(40, 80, 40) is StyleBucket.GROWTH
        assert alloc.style_bucket(40, 40, 80) is StyleBucket.QUALITY

    def test_near_ties_are_blend_not_a_coin_flip(self):
        """Calling a balanced holding "growth" on two points is noise."""
        assert alloc.style_bucket(72, 70, 30) is StyleBucket.BLEND

    def test_style_distinguishes_unknown_from_blend(self):
        assert alloc.style_bucket(None, None, None) is StyleBucket.UNKNOWN
        assert alloc.style_bucket(30, 30, 30) is StyleBucket.BLEND

    def test_weights_sum_to_one(self):
        positions = [
            _pos("A", 400, sector="IT"), _pos("B", 400, sector="IT"),
            _pos("C", 200, sector="Banking"),
        ]
        allocation = alloc.by_sector(positions)
        assert sum(s.weight for s in allocation.slices) == pytest.approx(1.0)

    def test_unclassified_is_reported_not_dropped(self):
        """Dropping it would leave the remaining weights summing above one."""
        positions = [_pos("A", 500, sector="IT"), _pos("B", 500, sector=None)]
        allocation = alloc.by_sector(positions)
        assert allocation.unclassified_value == pytest.approx(500)
        assert sum(s.weight for s in allocation.slices) == pytest.approx(1.0)

    def test_herfindahl_and_effective_count(self):
        # Distinct sectors, or the four positions collapse into one
        # "unclassified" bucket and HHI is correctly 1.0. The first version of
        # this test omitted the sectors and then blamed the engine for
        # reporting a single bucket, which is exactly what it should report.
        positions = [_pos(f"P{i}", 250, sector=f"S{i}") for i in range(4)]
        allocation = alloc.by_sector(positions)
        assert allocation.herfindahl == pytest.approx(0.25)
        assert allocation.effective_count == pytest.approx(4.0)

    def test_positions_without_a_sector_are_one_bucket(self):
        """Four unclassified holdings are one exposure, not four."""
        allocation = alloc.by_sector([_pos(f"P{i}", 250) for i in range(4)])
        assert len(allocation.slices) == 1
        assert allocation.herfindahl == pytest.approx(1.0)

    def test_drift_against_a_target(self):
        positions = [_pos("A", 600, sector="IT"), _pos("B", 400, sector="Banking")]
        allocation = alloc.by_sector(positions, targets={"IT": 0.50})
        it = next(s for s in allocation.slices if s.key == "IT")
        assert it.drift == pytest.approx(0.10)

    def test_unpriced_positions_are_excluded(self):
        priced = _pos("A", 1000, sector="IT")
        unpriced = Position(ticker="B", quantity=10, cost=500, sector="IT")
        allocation = alloc.by_sector([priced, unpriced])
        assert allocation.slices[0].position_count == 1


# ===========================================================================
# Risk
# ===========================================================================
class TestRisk:
    @pytest.fixture(scope="class")
    def series(self):
        import random

        rng = random.Random(7)
        values = [100.0]
        for _ in range(400):
            values.append(values[-1] * (1 + rng.gauss(0.0005, 0.011)))
        return values

    def test_returns_skip_a_non_positive_base(self):
        assert R.to_returns([100, 0, 50]) == pytest.approx([-1.0])

    def test_stdev_declines_on_too_few_points(self):
        assert R.stdev([0.01, 0.02]) is None

    def test_annualised_return_is_geometric(self):
        """+50% then −50% has lost a quarter, not broken even."""
        result = R.annualised_return([0.5, -0.5], periods=2)
        assert result == pytest.approx(0.75 - 1.0)

    def test_volatility_scales_with_root_time(self, series):
        returns = R.to_returns(series)
        assert R.annualised_volatility(returns) == pytest.approx(
            R.stdev(returns) * math.sqrt(R.TRADING_DAYS)
        )

    def test_sortino_uses_the_same_hurdle_in_both_terms(self, series):
        """Numerator and denominator must measure against one target."""
        returns = R.to_returns(series)
        assert R.sortino_ratio(returns) is not None
        assert R.sortino_ratio(returns) > R.sharpe_ratio(returns)

    def test_sortino_is_undefined_without_downside(self):
        """Undefined and zero are opposite statements."""
        assert R.sortino_ratio([0.01] * 30, risk_free=0.0) is None

    def test_downside_deviation_divides_by_all_observations(self):
        """Dividing by the downside count alone would flatter a rare loss."""
        one_bad_day = [0.01] * 99 + [-0.05]
        many_bad = [-0.05] * 50 + [0.01] * 50
        assert R.downside_deviation(one_bad_day) < R.downside_deviation(many_bad)

    def test_max_drawdown_finds_the_deepest_trough(self):
        drawdown = R.max_drawdown([100, 120, 90, 130, 60, 140])
        assert drawdown.depth == pytest.approx(60 / 130 - 1)
        assert drawdown.recovered

    def test_unrecovered_drawdown_is_flagged(self):
        drawdown = R.max_drawdown([100, 150, 80])
        assert not drawdown.recovered

    def test_var_is_negative_and_cvar_is_worse(self):
        """CVaR sits further into the tail than VaR, always."""
        returns = [-0.10, -0.05, -0.02, 0.0, 0.01, 0.02, 0.03, 0.05] * 6
        var = R.value_at_risk(returns, 0.95)
        cvar = R.conditional_value_at_risk(returns, 0.95)
        assert var < 0 and cvar <= var

    def test_var_99_is_worse_than_var_95(self, series):
        returns = R.to_returns(series)
        assert R.value_at_risk(returns, 0.99) <= R.value_at_risk(returns, 0.95)

    def test_beta_of_a_series_against_itself_is_one(self, series):
        returns = R.to_returns(series)
        assert R.beta(returns, returns) == pytest.approx(1.0)

    def test_alpha_against_itself_is_zero(self, series):
        returns = R.to_returns(series)
        assert R.alpha(returns, returns) == pytest.approx(0.0, abs=1e-9)

    def test_tracking_error_against_itself_is_zero(self, series):
        returns = R.to_returns(series)
        assert R.tracking_error(returns, returns) == pytest.approx(0.0, abs=1e-12)

    def test_effective_positions_exposes_false_breadth(self):
        """Ten positions with one at 90% is not ten positions."""
        weights = [0.90] + [0.0111] * 9
        assert R.effective_positions(weights) < 1.3

    def test_diversification_score_penalises_too_few_names(self):
        """Five equal names are well weighted and still too few."""
        five = R.diversification_score([0.2] * 5, target_names=15)
        fifteen = R.diversification_score([1 / 15] * 15, target_names=15)
        assert five < fifteen
        assert fifteen > 90

    def test_liquidity_days_scale_with_position_size(self):
        assert R.liquidity_days(1_000_000, 500_000) == pytest.approx(10.0)
        assert R.liquidity_days(1_000_000, None) is None

    def test_profile_records_what_it_could_not_compute(self):
        """A blank the user cannot account for is worse than no cell."""
        profile = R.build_risk_profile([100.0, 101.0])
        assert profile.sharpe is None
        assert profile.unavailable
        assert any("observations" in gap for gap in profile.unavailable)

    def test_profile_notes_a_missing_benchmark(self, series):
        profile = R.build_risk_profile(series, None, [0.5, 0.5])
        assert profile.beta is None
        assert any("benchmark" in gap.lower() for gap in profile.unavailable)

    def test_profile_is_complete_with_full_inputs(self, series):
        import random

        rng = random.Random(11)
        benchmark = [100.0]
        for _ in range(len(series) - 1):
            benchmark.append(benchmark[-1] * (1 + rng.gauss(0.0004, 0.008)))
        profile = R.build_risk_profile(series, benchmark, [0.4, 0.3, 0.3])
        for field in ("sharpe", "sortino", "beta", "alpha", "var_95", "cvar_95"):
            assert getattr(profile, field) is not None


# ===========================================================================
# Performance
# ===========================================================================
class TestPerformance:
    def test_twr_neutralises_a_deposit(self):
        """A deposit is not performance."""
        points = [
            ReturnPoint(date(2024, 1, 1), 100_000),
            ReturnPoint(date(2024, 6, 1), 110_000),
            ReturnPoint(date(2024, 7, 1), 1_110_000, net_flow=1_000_000),
            ReturnPoint(date(2024, 12, 31), 1_221_000),
        ]
        # Two sub-periods of +10% each, chain-linked.
        assert time_weighted_return(points) == pytest.approx(0.21, rel=1e-6)

    def test_twr_and_mwr_diverge_on_bad_timing(self):
        """The manager can be right while the investor is poorly timed."""
        points = [
            ReturnPoint(date(2024, 1, 1), 100_000),
            ReturnPoint(date(2024, 6, 1), 200_000),
            ReturnPoint(date(2024, 7, 1), 1_200_000, net_flow=1_000_000),
            ReturnPoint(date(2024, 12, 31), 960_000),
        ]
        twr = time_weighted_return(points)
        mwr = money_weighted_return([
            CashFlow(date(2024, 1, 1), -100_000),
            CashFlow(date(2024, 7, 1), -1_000_000),
            CashFlow(date(2024, 12, 31), 960_000),
        ])
        assert twr > 0 > mwr

    def test_twr_declines_on_a_single_point(self):
        assert time_weighted_return([ReturnPoint(date(2024, 1, 1), 100)]) is None

    def test_short_periods_are_not_annualised(self):
        """Annualising three weeks into 96% is how funds mislead."""
        assert annualise(0.04, 21) is None
        assert annualise(0.20, 730) == pytest.approx(0.0954, abs=1e-3)

    def test_xnpv_at_the_irr_is_zero(self):
        flows = [
            CashFlow(date(2023, 1, 1), -100_000),
            CashFlow(date(2024, 1, 1), 60_000),
            CashFlow(date(2025, 1, 1), 60_000),
        ]
        rate = xirr(flows)
        assert rate is not None
        assert xnpv(rate, flows) == pytest.approx(0.0, abs=1e-4)

    def test_irr_declines_without_a_sign_change(self):
        """A series of only deposits has no rate of return."""
        assert xirr([
            CashFlow(date(2023, 1, 1), -100),
            CashFlow(date(2024, 1, 1), -100),
        ]) is None

    def test_brinson_decomposition_is_exact(self):
        """The three terms must sum to active return, to floating point."""
        result = brinson_attribution([
            ("it", "IT", 0.30, 0.20, 0.15, 0.10),
            ("bank", "Banking", 0.20, 0.30, 0.05, 0.08),
            ("fmcg", "FMCG", 0.50, 0.50, 0.12, 0.11),
        ])
        assert result.residual == pytest.approx(0.0, abs=1e-12)
        assert result.total_allocation + result.total_selection + \
            result.total_interaction == pytest.approx(result.active_return)

    def test_fachler_refinement_penalises_a_lagging_overweight(self):
        """An overweight in a segment that lagged the index must not score well."""
        result = brinson_attribution([
            ("weak", "Weak", 0.60, 0.30, 0.02, 0.02),
            ("strong", "Strong", 0.40, 0.70, 0.20, 0.20),
        ])
        weak = next(r for r in result.rows if r.key == "weak")
        assert weak.allocation < 0

    def test_interaction_is_reported_separately(self):
        """Folding it into selection flatters a manager who was also overweight."""
        result = brinson_attribution([("a", "A", 0.60, 0.40, 0.20, 0.10)])
        row = result.rows[0]
        assert row.interaction == pytest.approx((0.60 - 0.40) * (0.20 - 0.10))
        assert row.selection == pytest.approx(0.40 * (0.20 - 0.10))

    def test_contribution_ranks_by_impact_not_by_return(self):
        """A 60% gain on a 1% position is a rounding error."""
        rows = contribution_analysis([
            ("SMALL", "Small", 0.01, 0.60),
            ("BIG", "Big", 0.30, 0.05),
        ])
        assert rows[0].ticker == "BIG"

    def test_rolling_returns_respect_the_window(self):
        points = [
            ReturnPoint(date(2024, 1, 1) + timedelta(days=i), 100 * (1.01 ** i))
            for i in range(30)
        ]
        rolling = rolling_returns(points, 10)
        assert len(rolling) == 20
        assert rolling[0][1] == pytest.approx(1.01 ** 10 - 1)

    def test_rolling_returns_empty_when_history_is_short(self):
        points = [ReturnPoint(date(2024, 1, 1), 100)]
        assert rolling_returns(points, 10) == []

    def test_underwater_curve_is_never_positive(self):
        points = [
            ReturnPoint(date(2024, 1, 1), 100),
            ReturnPoint(date(2024, 1, 2), 120),
            ReturnPoint(date(2024, 1, 3), 90),
        ]
        assert all(value <= 0 for _, value in drawdown_series(points))


# ===========================================================================
# Alert engine
# ===========================================================================
class TestAlertRules:
    def test_the_workbook_defines_fourteen_live_rules(self):
        """`38 Alerts` rows 9-22. The count is the contract."""
        assert len(LIVE_RULES) == 14

    def test_rule_keys_are_unique(self):
        keys = [r.key for r in ALL_RULES]
        assert len(keys) == len(set(keys))

    def test_every_brief_category_has_a_rule(self):
        covered = {r.category for r in ALL_RULES}
        assert {
            AlertCategory.PRICE, AlertCategory.VALUATION,
            AlertCategory.DCF_CHANGE, AlertCategory.SCORE_CHANGE,
            AlertCategory.RISK, AlertCategory.MANAGEMENT,
            AlertCategory.DOCUMENT, AlertCategory.QUARTERLY_RESULT,
            AlertCategory.CORPORATE_ACTION, AlertCategory.PORTFOLIO,
        } <= covered

    @pytest.mark.parametrize("rating,expected", [
        ("AAA", 0.08), ("AA", 0.06), ("A", 0.04),
        ("BBB", 0.025), ("BB", 0.01), ("B", 0.0), ("C", 0.0),
    ])
    def test_position_size_follows_the_rating(self, rating, expected):
        """Position sizing is a function of quality, per `30 Scorecard` H27:H33."""
        assert max_position_for_rating(rating) == expected

    def test_unrated_gets_a_real_limit(self):
        """An unrated name must not quietly become the largest position."""
        assert max_position_for_rating(None) == DEFAULT_MAX_POSITION
        assert max_position_for_rating("ZZZ") == DEFAULT_MAX_POSITION

    def test_missing_input_is_unavailable_not_clear(self):
        """The workbook reads a blank cell as zero and reports "clear".

        Silence about a risk is not evidence of its absence, so a rule whose
        input is missing says so.
        """
        rule = RULES_BY_KEY["leverage_stretched"]
        result = rule.evaluate({"net_debt_to_ebitda": None})
        assert result.status is AlertStatus.UNAVAILABLE
        assert "not available" in result.detail

    def test_leverage_rule_fires_above_three_times(self):
        rule = RULES_BY_KEY["leverage_stretched"]
        assert rule.evaluate({"net_debt_to_ebitda": 3.5}).is_triggered
        assert not rule.evaluate({"net_debt_to_ebitda": 2.5}).is_triggered

    def test_buy_zone_uses_the_margin_of_safety(self):
        metrics = build_position_metrics(
            price=150, intrinsic_value=200, margin_of_safety=0.20
        )
        assert metrics["buy_zone"] == pytest.approx(160.0)
        assert RULES_BY_KEY["price_below_buy_zone"].evaluate(metrics).is_triggered

    def test_rating_set_membership(self):
        rule = RULES_BY_KEY["rating_below_a"]
        assert rule.evaluate({"rating": "BBB"}).is_triggered
        assert not rule.evaluate({"rating": "AA"}).is_triggered

    def test_upside_is_derived_not_supplied(self):
        metrics = build_position_metrics(price=100, target_price=130)
        assert metrics["upside"] == pytest.approx(0.30)

    def test_comparators(self):
        assert Comparator.LT.evaluate(1, 2)
        assert Comparator.LTE.evaluate(2, 2)
        assert Comparator.GT.evaluate(3, 2)
        assert Comparator.GTE.evaluate(2, 2)
        assert Comparator.EQ.evaluate(2.0, 2.0)
        assert Comparator.IN_SET.evaluate("BB", {"BB", "B"})

    def test_comparator_rejects_non_numeric(self):
        assert not Comparator.LT.evaluate("abc", 2)

    def test_engine_sorts_triggered_first_then_by_severity(self):
        engine = AlertEngine()
        metrics = build_position_metrics(
            price=100, score=40, rating="C", weight=0.5, risk_score=0.3
        )
        results = engine.evaluate_position("X", None, metrics)
        triggered = engine.triggered(results)
        assert triggered
        severities = [t.severity.rank for t in triggered]
        assert severities == sorted(severities)

    def test_summary_counts_by_status_and_severity(self):
        engine = AlertEngine()
        metrics = build_position_metrics(price=100, score=40, weight=0.9)
        summary = engine.summarise(engine.evaluate_position("X", None, metrics))
        assert summary["total"] > 0
        assert summary["triggered"] + summary["clear"] + \
            summary["unavailable"] == summary["total"]

    def test_portfolio_rules_are_scoped_separately(self):
        engine = AlertEngine()
        results = engine.evaluate_portfolio({
            "diversification_score": 30.0, "effective_positions": 3.0,
            "largest_sector_weight": 0.5, "top_5_concentration": 0.8,
            "max_drawdown": -0.30, "cash_weight": 0.4,
            "portfolio_risk_score": 0.5,
        })
        assert all(r.ticker is None for r in results)
        assert len(engine.triggered(results)) >= 6

    def test_a_disabled_rule_does_not_evaluate(self):
        # AlertRule is slots=True, so it has no __dict__; dataclasses.replace
        # is the supported way to derive a variant.
        import dataclasses

        rule = RULES_BY_KEY["leverage_stretched"]
        disabled = dataclasses.replace(rule, enabled=False)
        engine = AlertEngine([disabled])
        assert engine.evaluate_position("X", None, {"net_debt_to_ebitda": 9}) == []


# ===========================================================================
# Architecture
# ===========================================================================
class TestArchitecture:
    def test_domain_has_no_infrastructure_imports(self):
        """The rule that has held since Module 2."""
        import pathlib

        forbidden = ("sqlalchemy", "fastapi", "httpx", "app.models", "app.api")
        for path in pathlib.Path("app/domain/portfolio").rglob("*.py"):
            source = path.read_text()
            for term in forbidden:
                assert f"import {term}" not in source, f"{path} imports {term}"
                assert f"from {term}" not in source, f"{path} imports {term}"

    def test_each_calculation_is_defined_once(self):
        """No duplicated logic — the standing constraint."""
        import ast
        import pathlib

        watched = {
            "sharpe_ratio", "sortino_ratio", "max_drawdown", "value_at_risk",
            "conditional_value_at_risk", "beta", "alpha", "herfindahl",
            "effective_positions", "diversification_score", "liquidity_days",
            "time_weighted_return", "money_weighted_return", "xirr", "xnpv",
            "brinson_attribution", "contribution_analysis", "market_cap_band",
            "style_bucket", "max_position_for_rating", "build_position_metrics",
            "annualised_return", "annualised_volatility", "to_returns",
        }
        found: dict[str, list[str]] = {}
        for root in ("app/domain/portfolio", "app/services/portfolio"):
            for path in pathlib.Path(root).rglob("*.py"):
                tree = ast.parse(path.read_text())
                for node in tree.body:
                    if isinstance(node, ast.FunctionDef) and node.name in watched:
                        found.setdefault(node.name, []).append(str(path))
        assert {k: v for k, v in found.items() if len(v) > 1} == {}

    def test_positions_are_never_persisted(self):
        """There must be no positions table: the ledger is the only truth."""
        from app.models import portfolio as models

        tables = {
            getattr(v, "__tablename__", None)
            for v in vars(models).values() if hasattr(v, "__tablename__")
        }
        assert "positions" not in tables
        assert "portfolio_positions" not in tables
        assert "portfolio_transactions" in tables


# ===========================================================================
# Performance
# ===========================================================================
class TestPerformanceBudget:
    """Guards against regressions in kind, not in noise.

    Thresholds are several times the observed figure so they fail on an
    algorithmic change rather than on a busy machine. A benchmark that goes red
    under load gets muted, and then it guards nothing.
    """

    @staticmethod
    def _ledger(count: int) -> list[T]:
        import random

        rng = random.Random(3)
        ledger = [T("", "deposit", date(2020, 1, 1), 1, 1e9, sequence=0)]
        for i in range(count):
            ledger.append(T(
                f"T{i % 200:03d}", "buy",
                date(2020, 1, 1) + timedelta(days=i % 1500),
                quantity=10, price=100 + rng.random() * 50, fees=1,
                sequence=i + 1,
            ))
        return ledger

    def test_replay_is_linear_in_transactions(self):
        """Superlinear replay is how a long ledger becomes unusable.

        Measured as **cost per transaction**, not as a ratio of two wall
        times. The original form divided a 20,000-row run by a 2,000-row one
        and demanded the quotient stay under 30. That baseline takes about
        eight milliseconds, so first-call import and allocation costs are a
        large share of it, and the quotient swung between 5 and 50 across runs
        while the engine itself never changed.

        The engine is linear — 3.8-4.0 microseconds per transaction from 2,000
        rows to 20,000 — and this asserts that directly. Each measurement is
        the best of three, because the minimum of repeated timings is the one
        least polluted by scheduler noise.
        """
        import time

        def per_transaction_us(count: int) -> float:
            ledger = self._ledger(count)
            best = min(
                self._time_replay(ledger) for _ in range(3)
            )
            return best / count * 1e6

        small = per_transaction_us(2_000)
        large = per_transaction_us(20_000)

        # Linear means the unit cost does not grow with size. A generous
        # ceiling of 3x absorbs cache effects while still catching the
        # quadratic behaviour this test exists to prevent, which would show a
        # 10x rise here.
        assert large < small * 3.0, (
            f"unit cost rose from {small:.2f}us to {large:.2f}us per "
            "transaction — replay is not linear"
        )
        assert large < 50.0, f"{large:.2f}us per transaction is too slow"

    @staticmethod
    def _time_replay(ledger) -> float:
        import time

        started = time.perf_counter()
        PositionEngine().replay(ledger)
        return time.perf_counter() - started

    def test_replay_of_a_large_ledger_is_bounded(self):
        import time

        started = time.perf_counter()
        result = PositionEngine().replay(self._ledger(20_000))
        elapsed = time.perf_counter() - started
        assert len(result.open_positions) == 200
        # Observed ~80ms. Five seconds fails only on a change in complexity.
        assert elapsed < 5.0

    def test_risk_profile_is_bounded_on_a_long_series(self):
        import random
        import time

        rng = random.Random(5)
        values = [100.0]
        for _ in range(20_000):
            values.append(values[-1] * (1 + rng.gauss(0.0005, 0.011)))
        benchmark = [100.0]
        for _ in range(20_000):
            benchmark.append(benchmark[-1] * (1 + rng.gauss(0.0004, 0.009)))

        started = time.perf_counter()
        profile = R.build_risk_profile(values, benchmark, [1 / 25] * 25)
        elapsed = time.perf_counter() - started
        assert profile.sharpe is not None
        # Observed ~48ms over 20k observations.
        assert elapsed < 3.0

    def test_irr_solver_terminates(self):
        """Bisection is bounded; a divergent solver would hang here."""
        import time

        flows = [CashFlow(date(2020, 1, 1) + timedelta(days=90 * i),
                          -10_000 if i % 3 else 25_000)
                 for i in range(40)]
        started = time.perf_counter()
        money_weighted_return(flows)
        assert time.perf_counter() - started < 1.0
