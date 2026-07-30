"""Integration tests for the portfolio API.

These run against the real FastAPI app and the shared seeded database. Each
test class builds its own portfolio with an explicit ledger, so every asserted
figure is traceable to transactions written in the test itself rather than to
seed data that might change.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

BASE = "/api/v1"


def _create(api_client, name: str, **kwargs) -> int:
    response = api_client.post(
        f"{BASE}/portfolios", json={"name": name, **kwargs}
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _txn(api_client, portfolio_id: int, **payload) -> dict:
    response = api_client.post(
        f"{BASE}/portfolios/{portfolio_id}/transactions", json=payload
    )
    assert response.status_code == 201, response.text
    return response.json()


# ===========================================================================
# CRUD
# ===========================================================================
class TestPortfolioCrud:
    def test_create_and_read_back(self, api_client):
        portfolio_id = _create(
            api_client, "CRUD Book", benchmark="NIFTY 50",
            max_position_size=0.15,
        )
        response = api_client.get(f"{BASE}/portfolios/{portfolio_id}")
        assert response.status_code == 200
        summary = response.json()["summary"]
        assert summary["name"] == "CRUD Book"
        assert summary["position_count"] == 0

    def test_duplicate_name_is_refused(self, api_client):
        _create(api_client, "Unique Book")
        response = api_client.post(
            f"{BASE}/portfolios", json={"name": "Unique Book"}
        )
        assert response.status_code == 400

    def test_list_returns_owned_portfolios(self, api_client):
        _create(api_client, "Listed Book")
        names = {p["name"] for p in api_client.get(f"{BASE}/portfolios").json()}
        assert "Listed Book" in names

    def test_update_policy(self, api_client):
        portfolio_id = _create(api_client, "Policy Book")
        response = api_client.patch(
            f"{BASE}/portfolios/{portfolio_id}",
            json={"max_position_size": 0.05, "margin_of_safety": 0.30},
        )
        assert response.status_code == 200
        assert response.json()["max_position_size"] == 0.05

    def test_delete(self, api_client):
        portfolio_id = _create(api_client, "Doomed Book")
        assert api_client.delete(
            f"{BASE}/portfolios/{portfolio_id}"
        ).status_code == 204
        assert api_client.get(
            f"{BASE}/portfolios/{portfolio_id}"
        ).status_code == 404

    def test_unknown_portfolio_is_404(self, api_client):
        assert api_client.get(f"{BASE}/portfolios/999999").status_code == 404

    def test_invalid_position_size_is_rejected(self, api_client):
        response = api_client.post(
            f"{BASE}/portfolios",
            json={"name": "Bad Policy", "max_position_size": 1.5},
        )
        assert response.status_code == 422


# ===========================================================================
# Transactions and derived positions
# ===========================================================================
class TestTransactions:
    @pytest.fixture(scope="class")
    def book(self, api_client) -> int:
        portfolio_id = _create(api_client, "Ledger Book")
        _txn(api_client, portfolio_id, ticker="", txn_type="deposit",
             trade_date="2023-01-01", quantity=1, price=2_000_000)
        _txn(api_client, portfolio_id, ticker="TCS", txn_type="buy",
             trade_date="2023-01-10", quantity=100, price=3200, fees=160)
        _txn(api_client, portfolio_id, ticker="TCS", txn_type="buy",
             trade_date="2023-06-15", quantity=50, price=3400, fees=85)
        _txn(api_client, portfolio_id, ticker="TCS", txn_type="dividend",
             trade_date="2023-07-01", quantity=150, price=24)
        _txn(api_client, portfolio_id, ticker="TCS", txn_type="sell",
             trade_date="2024-02-20", quantity=80, price=4100, fees=205)
        _txn(api_client, portfolio_id, ticker="RELIANCE", txn_type="buy",
             trade_date="2023-03-01", quantity=200, price=2400, fees=240)
        _txn(api_client, portfolio_id, ticker="RELIANCE", txn_type="bonus",
             trade_date="2023-09-01", ratio_from=2, ratio_to=1)
        return portfolio_id

    def test_positions_are_derived_from_the_ledger(self, api_client, book):
        holdings = {
            h["ticker"]: h
            for h in api_client.get(f"{BASE}/portfolios/{book}/holdings").json()
        }
        assert holdings["TCS"]["quantity"] == pytest.approx(70)
        assert holdings["TCS"]["cost"] == pytest.approx(234_117)
        assert holdings["TCS"]["average_cost"] == pytest.approx(3344.53, abs=0.01)

    def test_bonus_is_applied(self, api_client, book):
        holdings = {
            h["ticker"]: h
            for h in api_client.get(f"{BASE}/portfolios/{book}/holdings").json()
        }
        assert holdings["RELIANCE"]["quantity"] == pytest.approx(300)
        assert holdings["RELIANCE"]["cost"] == pytest.approx(480_240)

    def test_realised_trades_are_reported(self, api_client, book):
        realised = api_client.get(f"{BASE}/portfolios/{book}").json()["realised"]
        assert len(realised) == 1
        trade = realised[0]
        assert trade["ticker"] == "TCS"
        assert trade["pnl"] == pytest.approx(71_667)
        assert trade["is_long_term"] is True

    def test_cash_balances(self, api_client, book):
        cash = api_client.get(f"{BASE}/portfolios/{book}").json()["cash"]
        expected = (
            2_000_000 - (100 * 3200 + 160) - (50 * 3400 + 85)
            - (200 * 2400 + 240) + (80 * 4100 - 205) + 150 * 24
        )
        assert cash["balance"] == pytest.approx(expected)
        assert cash["net_invested"] == pytest.approx(2_000_000)

    def test_dividends_are_income_not_cost_relief(self, api_client, book):
        holdings = {
            h["ticker"]: h
            for h in api_client.get(f"{BASE}/portfolios/{book}/holdings").json()
        }
        assert holdings["TCS"]["dividends"] == pytest.approx(3600)
        # Cost is unaffected by the dividend.
        assert holdings["TCS"]["cost"] == pytest.approx(234_117)

    def test_overselling_is_rejected_and_nothing_is_written(self, api_client):
        portfolio_id = _create(api_client, "Oversell Book")
        _txn(api_client, portfolio_id, ticker="TCS", txn_type="buy",
             trade_date="2023-01-10", quantity=50, price=3000)
        response = api_client.post(
            f"{BASE}/portfolios/{portfolio_id}/transactions",
            json={"ticker": "TCS", "txn_type": "sell",
                  "trade_date": "2023-02-10", "quantity": 80, "price": 3200},
        )
        assert response.status_code == 400
        ledger = api_client.get(
            f"{BASE}/portfolios/{portfolio_id}/transactions"
        ).json()
        assert len(ledger) == 1

    def test_deleting_a_buy_that_orphans_a_sell_is_refused(self, api_client):
        """Refuse rather than leave a ledger that cannot replay."""
        portfolio_id = _create(api_client, "Orphan Book")
        buy = _txn(api_client, portfolio_id, ticker="TCS", txn_type="buy",
                   trade_date="2023-01-10", quantity=100, price=3000)
        _txn(api_client, portfolio_id, ticker="TCS", txn_type="sell",
             trade_date="2023-06-10", quantity=100, price=3500)
        response = api_client.delete(
            f"{BASE}/portfolios/{portfolio_id}/transactions/{buy['id']}"
        )
        assert response.status_code == 400
        assert len(api_client.get(
            f"{BASE}/portfolios/{portfolio_id}/transactions"
        ).json()) == 2

    def test_ledger_filters(self, api_client, book):
        tcs = api_client.get(
            f"{BASE}/portfolios/{book}/transactions", params={"ticker": "TCS"}
        ).json()
        assert {t["ticker"] for t in tcs} == {"TCS"}
        buys = api_client.get(
            f"{BASE}/portfolios/{book}/transactions", params={"txn_type": "buy"}
        ).json()
        assert {t["txn_type"] for t in buys} == {"buy"}

    def test_transactions_are_returned_in_replay_order(self, api_client, book):
        ledger = api_client.get(
            f"{BASE}/portfolios/{book}/transactions"
        ).json()
        keys = [(t["trade_date"], t["sequence"]) for t in ledger]
        assert keys == sorted(keys)


# ===========================================================================
# Allocation, risk and performance
# ===========================================================================
class TestAnalytics:
    @pytest.fixture(scope="class")
    def book(self, api_client) -> int:
        portfolio_id = _create(api_client, "Analytics Book", max_position_size=0.25)
        _txn(api_client, portfolio_id, ticker="", txn_type="deposit",
             trade_date="2023-01-02", quantity=1, price=5_000_000)
        for i, (ticker, quantity, price) in enumerate([
            ("RELIANCE", 200, 2400), ("TCS", 100, 3200), ("HDFCBANK", 300, 1600),
            ("INFY", 200, 1500), ("SUNPHARMA", 150, 1100), ("TITAN", 60, 3000),
        ]):
            _txn(api_client, portfolio_id, ticker=ticker, txn_type="buy",
                 trade_date=f"2023-02-{i + 1:02d}", quantity=quantity, price=price)
        return portfolio_id

    def test_all_five_dimensions_are_returned(self, api_client, book):
        allocations = api_client.get(
            f"{BASE}/portfolios/{book}/allocation"
        ).json()
        assert set(allocations) == {
            "sector", "industry", "market_cap", "country", "style",
        }

    def test_weights_sum_to_one(self, api_client, book):
        # Weights are rounded to six decimals for transport, so a seven-slice
        # allocation can sum to 0.999999. The tolerance matches the rounding
        # the API actually applies rather than demanding a precision it never
        # promised.
        sector = api_client.get(
            f"{BASE}/portfolios/{book}/allocation",
            params={"dimension": "sector"},
        ).json()
        assert sum(s["weight"] for s in sector["slices"]) == pytest.approx(
            1.0, abs=1e-5
        )

    def test_allocation_reports_concentration(self, api_client, book):
        sector = api_client.get(
            f"{BASE}/portfolios/{book}/allocation",
            params={"dimension": "sector"},
        ).json()
        assert 0 < sector["herfindahl"] <= 1
        assert sector["effective_count"] >= 1

    def test_unknown_dimension_is_404(self, api_client, book):
        assert api_client.get(
            f"{BASE}/portfolios/{book}/allocation",
            params={"dimension": "nonsense"},
        ).status_code == 422

    def test_risk_without_snapshots_declares_the_gap(self, api_client, book):
        """A blank the user cannot account for is worse than no cell."""
        risk = api_client.get(f"{BASE}/portfolios/{book}/risk").json()
        assert risk["sharpe"] is None
        assert risk["unavailable"]
        assert any("snapshot" in gap.lower() for gap in risk["unavailable"])

    def test_concentration_metrics_need_no_history(self, api_client, book):
        risk = api_client.get(f"{BASE}/portfolios/{book}/risk").json()
        assert risk["effective_positions"] is not None
        assert risk["top_5_concentration"] is not None
        assert risk["diversification_score"] is not None

    def test_risk_populates_once_snapshots_exist(self, api_client, book):
        for offset in range(0, 40, 5):
            api_client.post(
                f"{BASE}/portfolios/{book}/snapshots",
                params={"as_of": (date(2024, 1, 1) + timedelta(days=offset)).isoformat()},
            )
        risk = api_client.get(f"{BASE}/portfolios/{book}/risk").json()
        assert risk["observations"] >= 3
        assert risk["max_drawdown"] is not None

    def test_performance_returns_a_series(self, api_client, book):
        performance = api_client.get(
            f"{BASE}/portfolios/{book}/performance"
        ).json()
        assert performance["series"]
        assert "contributions" in performance

    def test_contributions_rank_by_impact(self, api_client, book):
        contributions = api_client.get(
            f"{BASE}/portfolios/{book}/performance"
        ).json()["contributions"]
        values = [c["contribution"] for c in contributions]
        assert values == sorted(values, reverse=True)

    def test_attribution_decomposition_is_exact(self, api_client, book):
        """Allocation + selection + interaction must equal active return."""
        attribution = api_client.get(
            f"{BASE}/portfolios/{book}/attribution"
        ).json()
        total = (
            attribution["total_allocation"]
            + attribution["total_selection"]
            + attribution["total_interaction"]
        )
        # The decomposition itself is exact to ~1e-18 (see the engine test).
        # Over the API the inputs are six-decimal-rounded weights, so the
        # identity holds to ~1e-7. Asserting 1e-9 here was testing the
        # transport rounding, not the mathematics.
        assert total == pytest.approx(attribution["active_return"], abs=1e-6)
        assert attribution["residual"] == pytest.approx(0.0, abs=1e-6)

    def test_holdings_carry_platform_analytics(self, api_client, book):
        holdings = api_client.get(f"{BASE}/portfolios/{book}/holdings").json()
        assert any(h["score"] is not None for h in holdings)
        assert any(h["rating"] is not None for h in holdings)

    def test_summary_reports_analytics_failures(self, api_client, book):
        """A missing score must be attributable, not merely absent."""
        summary = api_client.get(f"{BASE}/portfolios/{book}").json()["summary"]
        assert summary["analytics_errors"] == {}


# ===========================================================================
# Alerts
# ===========================================================================
class TestAlerts:
    @pytest.fixture(scope="class")
    def book(self, api_client) -> int:
        portfolio_id = _create(
            api_client, "Alert Book", max_position_size=0.10,
        )
        _txn(api_client, portfolio_id, ticker="", txn_type="deposit",
             trade_date="2024-01-01", quantity=1, price=1_000_000)
        # One large holding, deliberately, so concentration rules fire.
        _txn(api_client, portfolio_id, ticker="BHARATCP", txn_type="buy",
             trade_date="2024-01-02", quantity=2000, price=240)
        return portfolio_id

    def test_evaluation_returns_counts_and_rows(self, api_client, book):
        payload = api_client.get(f"{BASE}/portfolios/{book}/alerts").json()
        counts = payload["counts"]
        assert counts["total"] > 0
        assert counts["triggered"] + counts["clear"] + \
            counts["unavailable"] == counts["total"]

    def test_concentration_alerts_fire(self, api_client, book):
        payload = api_client.get(
            f"{BASE}/portfolios/{book}/alerts", params={"triggered_only": True}
        ).json()
        keys = {e["key"] for e in payload["evaluations"]}
        assert "position_oversized" in keys
        assert "sector_overweight" in keys

    def test_missing_input_reports_unavailable_not_clear(self, api_client, book):
        payload = api_client.get(f"{BASE}/portfolios/{book}/alerts").json()
        unavailable = [
            e for e in payload["evaluations"] if e["status"] == "unavailable"
        ]
        assert unavailable
        assert all(e["detail"] for e in unavailable)

    def test_valuation_alerts_use_a_certified_valuation(self, api_client, book):
        """Module 4 grades its own output; a valuation it calls unreliable
        must not drive an alert here.

        Before this was respected, every holding tripped "Price above target"
        because a ₹2,945 share was compared with a ₹16.79 fair value the
        valuation engine had itself disowned.
        """
        payload = api_client.get(f"{BASE}/portfolios/{book}/alerts").json()
        by_key = {e["key"]: e for e in payload["evaluations"]}
        price_alert = by_key["price_above_target"]
        # BHARATCP is the reference company and does carry a usable valuation.
        assert price_alert["status"] in {"triggered", "clear"}
        assert price_alert["threshold"] is not None
        assert price_alert["threshold"] > 1.0

    def test_suppressed_valuation_yields_unavailable(self, api_client):
        """A synthetic company whose valuation is graded unreliable."""
        portfolio_id = _create(api_client, "Synthetic Valuation Book")
        _txn(api_client, portfolio_id, ticker="RELIANCE", txn_type="buy",
             trade_date="2024-01-02", quantity=10, price=2400)
        payload = api_client.get(
            f"{BASE}/portfolios/{portfolio_id}/alerts"
        ).json()
        by_key = {e["key"]: e for e in payload["evaluations"]}
        assert by_key["price_above_target"]["status"] == "unavailable"

    def test_alerts_persist_and_deduplicate(self, api_client, book):
        api_client.get(f"{BASE}/portfolios/{book}/alerts")
        api_client.get(f"{BASE}/portfolios/{book}/alerts")
        events = api_client.get(f"{BASE}/portfolios/alerts").json()
        for event in events:
            if event["portfolio_id"] == book:
                assert event["occurrences"] >= 1
        keys = [
            (e["rule_key"], e["ticker"]) for e in events
            if e["portfolio_id"] == book and e["status"] == "triggered"
        ]
        assert len(keys) == len(set(keys)), "an open alert was duplicated"

    def test_acknowledge(self, api_client, book):
        api_client.get(f"{BASE}/portfolios/{book}/alerts")
        events = [
            e for e in api_client.get(f"{BASE}/portfolios/alerts").json()
            if e["portfolio_id"] == book and e["status"] == "triggered"
        ]
        assert events
        response = api_client.post(
            f"{BASE}/alerts/{events[0]['id']}/acknowledge"
        )
        assert response.status_code == 200
        assert response.json()["status"] == "acknowledged"

    def test_threshold_override_changes_the_outcome(self, api_client, book):
        response = api_client.post(
            f"{BASE}/portfolios/{book}/alerts/rules",
            json={"rule_key": "too_few_effective_positions", "threshold": 0.5},
        )
        assert response.status_code == 200
        assert response.json()["threshold"] == 0.5
        payload = api_client.get(f"{BASE}/portfolios/{book}/alerts").json()
        rule = next(
            e for e in payload["evaluations"]
            if e["key"] == "too_few_effective_positions"
        )
        assert rule["status"] == "clear"

    def test_a_rule_can_be_disabled(self, api_client, book):
        api_client.post(
            f"{BASE}/portfolios/{book}/alerts/rules",
            json={"rule_key": "cash_drag", "enabled": False},
        )
        payload = api_client.get(f"{BASE}/portfolios/{book}/alerts").json()
        assert "cash_drag" not in {e["key"] for e in payload["evaluations"]}


# ===========================================================================
# Watchlists
# ===========================================================================
class TestWatchlists:
    @pytest.fixture(scope="class")
    def watchlist_id(self, api_client) -> int:
        response = api_client.post(
            f"{BASE}/watchlists", json={"name": "API Candidates"}
        )
        assert response.status_code == 201
        return response.json()["id"]

    def test_add_and_list(self, api_client, watchlist_id):
        response = api_client.post(
            f"{BASE}/watchlists/{watchlist_id}/entries",
            json={"ticker": "MARUTI", "buy_below": 10_500,
                  "note": "Waiting for weakness"},
        )
        assert response.status_code == 201
        rows = api_client.get(f"{BASE}/watchlists/{watchlist_id}").json()
        assert {r["ticker"] for r in rows} == {"MARUTI"}

    def test_rows_carry_price_and_status(self, api_client, watchlist_id):
        rows = api_client.get(f"{BASE}/watchlists/{watchlist_id}").json()
        row = rows[0]
        assert row["price"] is not None
        assert row["status"] in {
            "triggered", "approaching", "watching", "expensive",
        }

    def test_buy_below_is_derived_when_absent(self, api_client, watchlist_id):
        """A row added with only a ticker must still be actionable."""
        api_client.post(
            f"{BASE}/watchlists/{watchlist_id}/entries",
            json={"ticker": "BHARATCP"},
        )
        rows = {
            r["ticker"]: r
            for r in api_client.get(f"{BASE}/watchlists/{watchlist_id}").json()
        }
        assert rows["BHARATCP"]["buy_below"] is not None

    def test_duplicate_ticker_is_refused(self, api_client, watchlist_id):
        response = api_client.post(
            f"{BASE}/watchlists/{watchlist_id}/entries",
            json={"ticker": "MARUTI"},
        )
        assert response.status_code == 400

    def test_remove(self, api_client, watchlist_id):
        rows = api_client.get(f"{BASE}/watchlists/{watchlist_id}").json()
        entry_id = rows[0]["id"]
        assert api_client.delete(
            f"{BASE}/watchlists/{watchlist_id}/entries/{entry_id}"
        ).status_code == 204

    def test_unknown_watchlist_is_404(self, api_client):
        assert api_client.get(f"{BASE}/watchlists/999999").status_code == 404


# ===========================================================================
# Targets, snapshots, commentary, capabilities
# ===========================================================================
class TestSupporting:
    @pytest.fixture(scope="class")
    def book(self, api_client) -> int:
        portfolio_id = _create(api_client, "Supporting Book")
        _txn(api_client, portfolio_id, ticker="", txn_type="deposit",
             trade_date="2024-01-01", quantity=1, price=2_000_000)
        for ticker, quantity, price in [
            ("TCS", 100, 3200), ("RELIANCE", 150, 2400), ("INFY", 200, 1500),
        ]:
            _txn(api_client, portfolio_id, ticker=ticker, txn_type="buy",
                 trade_date="2024-01-05", quantity=quantity, price=price)
        return portfolio_id

    def test_target_weights_produce_drift(self, api_client, book):
        sector = api_client.get(
            f"{BASE}/portfolios/{book}/allocation",
            params={"dimension": "sector"},
        ).json()
        bucket = sector["slices"][0]["key"]
        response = api_client.put(
            f"{BASE}/portfolios/{book}/targets",
            json={"dimension": "sector", "bucket_key": bucket,
                  "target_weight": 0.10},
        )
        assert response.status_code == 200
        refreshed = api_client.get(
            f"{BASE}/portfolios/{book}/allocation",
            params={"dimension": "sector"},
        ).json()
        target_slice = next(
            s for s in refreshed["slices"] if s["key"] == bucket
        )
        assert target_slice["drift"] is not None

    def test_snapshot_is_idempotent_per_date(self, api_client, book):
        first = api_client.post(
            f"{BASE}/portfolios/{book}/snapshots",
            params={"as_of": "2024-06-01"},
        ).json()
        second = api_client.post(
            f"{BASE}/portfolios/{book}/snapshots",
            params={"as_of": "2024-06-01"},
        ).json()
        assert first["id"] == second["id"]

    def test_snapshots_are_listed_in_order(self, api_client, book):
        api_client.post(
            f"{BASE}/portfolios/{book}/snapshots",
            params={"as_of": "2024-07-01"},
        )
        snapshots = api_client.get(
            f"{BASE}/portfolios/{book}/snapshots"
        ).json()
        dates = [s["as_of"] for s in snapshots]
        assert dates == sorted(dates)

    def test_commentary_is_grounded_in_citations(self, api_client, book):
        """Every figure in the prose must exist as a citation."""
        payload = api_client.get(f"{BASE}/portfolios/{book}/commentary").json()
        assert payload["citations"]
        assert payload["disclosure"]
        keys = {c["key"] for c in payload["citations"]}
        assert "pf_value" in keys

    def test_commentary_covers_every_required_section(self, api_client, book):
        payload = api_client.get(f"{BASE}/portfolios/{book}/commentary").json()
        sections = {s["key"] for s in payload["sections"]}
        assert sections == {
            "health", "risks", "opportunities", "rebalancing", "positions",
        }
        assert all(s["body"].strip() for s in payload["sections"])

    def test_commentary_never_invents_a_figure(self, api_client, book):
        """Any bracketed marker must resolve to a real citation key."""
        import re

        payload = api_client.get(f"{BASE}/portfolios/{book}/commentary").json()
        keys = {c["key"] for c in payload["citations"]}
        for section in payload["sections"]:
            for marker in re.findall(r"\[([a-z0-9_]+)\]", section["body"]):
                assert marker in keys, f"{marker} is not a citation"

    def test_capabilities_describe_the_engine(self, api_client):
        payload = api_client.get(f"{BASE}/portfolios/capabilities").json()
        assert len(payload["rules"]) >= 25
        assert set(payload["allocation_dimensions"]) == {
            "sector", "industry", "market_cap", "country", "style",
        }
        assert payload["rating_position_limits"]["AAA"] == 0.08
        assert "cache" in payload

    def test_capabilities_lists_every_transaction_type(self, api_client):
        types = set(
            api_client.get(f"{BASE}/portfolios/capabilities").json()[
                "transaction_types"
            ]
        )
        assert {
            "buy", "sell", "dividend", "bonus", "split", "rights",
            "deposit", "withdrawal", "fee", "tax", "interest",
        } <= types


# ===========================================================================
# Caching
# ===========================================================================
class TestCaching:
    def test_a_new_transaction_invalidates_the_view(self, api_client):
        """Content-derived keys, not a clock: a trade must show immediately."""
        portfolio_id = _create(api_client, "Cache Book")
        _txn(api_client, portfolio_id, ticker="TCS", txn_type="buy",
             trade_date="2024-01-05", quantity=100, price=3200)
        first = api_client.get(f"{BASE}/portfolios/{portfolio_id}").json()
        assert first["summary"]["position_count"] == 1

        _txn(api_client, portfolio_id, ticker="INFY", txn_type="buy",
             trade_date="2024-01-06", quantity=50, price=1500)
        second = api_client.get(f"{BASE}/portfolios/{portfolio_id}").json()
        assert second["summary"]["position_count"] == 2

    def test_repeated_reads_hit_the_cache(self, api_client):
        portfolio_id = _create(api_client, "Warm Cache Book")
        _txn(api_client, portfolio_id, ticker="TCS", txn_type="buy",
             trade_date="2024-01-05", quantity=100, price=3200)
        for _ in range(3):
            api_client.get(f"{BASE}/portfolios/{portfolio_id}")
        stats = api_client.get(f"{BASE}/portfolios/capabilities").json()["cache"]
        assert stats["hits"] > 0
