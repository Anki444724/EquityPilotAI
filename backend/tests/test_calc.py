"""Unit tests for the shared calculation primitives.

These functions underpin every service, so their edge-case behaviour — above
all the None-vs-zero distinction — is tested directly rather than only through
the services that consume them.
"""
from __future__ import annotations

import pytest

from app.domain.calc import (
    DAYS_IN_YEAR, avg_balance, basis_points, cagr, clamp, consecutive_run,
    days, delta, growth, pct_of, safe_div, series_cagr, total,
)


class TestSafeDiv:
    def test_normal_division(self):
        assert safe_div(10, 4) == 2.5

    def test_zero_denominator_is_none_not_zero(self):
        """An undefined ratio must never be presented as 0."""
        assert safe_div(10, 0) is None

    def test_none_operands(self):
        assert safe_div(None, 4) is None
        assert safe_div(10, None) is None

    def test_zero_numerator_is_a_real_zero(self):
        assert safe_div(0, 4) == 0.0

    def test_negative_denominator_still_divides(self):
        assert safe_div(10, -4) == -2.5


class TestAverageBalance:
    def test_two_balances(self):
        assert avg_balance(120, 80) == 100

    def test_missing_opening_falls_back_to_closing(self):
        """First year on file has no opening balance."""
        assert avg_balance(120, None) == 120

    def test_missing_closing_falls_back_to_opening(self):
        assert avg_balance(None, 80) == 80

    def test_both_missing(self):
        assert avg_balance(None, None) is None


class TestDays:
    def test_uses_365_day_year(self):
        assert DAYS_IN_YEAR == 365
        assert days(1, 1) == 365

    def test_quarter_of_a_year(self):
        assert days(25, 100) == pytest.approx(91.25)

    def test_undefined_when_denominator_zero(self):
        assert days(10, 0) is None


class TestGrowth:
    def test_positive_growth(self):
        assert growth(110, 100) == pytest.approx(0.10)

    def test_decline(self):
        assert growth(90, 100) == pytest.approx(-0.10)

    def test_negative_base_is_undefined(self):
        """Growth off a loss-making base is not meaningful."""
        assert growth(50, -10) is None

    def test_zero_base_is_undefined(self):
        assert growth(50, 0) is None


class TestCagr:
    def test_doubling_over_one_period(self):
        assert cagr(100, 200, 1) == pytest.approx(1.0)

    def test_known_compound_rate(self):
        assert cagr(100, 161.051, 5) == pytest.approx(0.10, abs=1e-6)

    def test_negative_endpoint_is_undefined(self):
        assert cagr(-100, 200, 5) is None
        assert cagr(100, -200, 5) is None

    def test_zero_periods_is_undefined(self):
        assert cagr(100, 200, 0) is None


class TestSeriesCagr:
    def test_uses_first_and_last_present_points(self):
        assert series_cagr([100, None, None, 133.1]) == pytest.approx(0.10, abs=1e-6)

    def test_needs_two_points(self):
        assert series_cagr([100]) is None
        assert series_cagr([None, None]) is None


class TestBasisPoints:
    def test_one_percent_is_100_bps(self):
        assert basis_points(0.52, 0.51) == pytest.approx(100.0)

    def test_negative_move(self):
        assert basis_points(0.50, 0.52) == pytest.approx(-200.0)


class TestMisc:
    def test_delta(self):
        assert delta(10, 4) == 6
        assert delta(None, 4) is None

    def test_pct_of(self):
        assert pct_of(25, 100) == 0.25
        assert pct_of(25, 0) is None

    def test_total_treats_none_as_zero(self):
        assert total(1, None, 3) == 4
        assert total() == 0

    def test_clamp(self):
        assert clamp(15, 0, 10) == 10
        assert clamp(-5, 0, 10) == 0
        assert clamp(5, 0, 10) == 5

    def test_consecutive_run_counts_trailing_trues(self):
        assert consecutive_run([True, False, True, True, True]) == 3
        assert consecutive_run([True, True, False]) == 0
        assert consecutive_run([]) == 0
