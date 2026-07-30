"""Integration tests for the Module 2 REST endpoints.

Runs the full stack — routing, dependency resolution, ORM, services and
serialisation — against an in-memory database seeded exactly as production dev
data is.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app

# Database and dependency override live in conftest.py so both API modules
# share one seeded instance (they mutate the same FastAPI app object).
client = TestClient(app)

TICKER = "TITAN"
ENDPOINTS = [
    "income-statement", "balance-sheet", "cash-flow", "ratios",
    "working-capital", "debt", "capex", "shareholding",
]


def fetch(path: str, ticker: str = TICKER, **params):
    r = client.get(f"/api/v1/company/{ticker}/{path}", params=params)
    assert r.status_code == 200, f"{path}: {r.status_code} {r.text[:200]}"
    return r.json()


def find_row(payload: dict, key: str):
    for section in payload["sections"]:
        for row in section["rows"]:
            if row["key"] == key:
                return row
    raise KeyError(f"{key} not present")


class TestContract:
    @pytest.mark.parametrize("path", ENDPOINTS)
    def test_endpoint_responds(self, path):
        fetch(path)

    @pytest.mark.parametrize("path", ENDPOINTS)
    def test_envelope_shape(self, path):
        body = fetch(path)
        assert {"company", "periods", "sections", "has_data"} <= body.keys()
        assert body["company"]["ticker"] == TICKER
        assert body["periods"]["labels"]

    @pytest.mark.parametrize("path", ENDPOINTS)
    def test_row_values_align_with_period_count(self, path):
        body = fetch(path)
        n = len(body["periods"]["labels"])
        for section in body["sections"]:
            for row in section["rows"]:
                assert len(row["values"]) == n, f"{path}/{row['key']} ragged"

    @pytest.mark.parametrize("path", ENDPOINTS)
    def test_every_row_declares_a_unit(self, path):
        for section in fetch(path)["sections"]:
            for row in section["rows"]:
                assert row["unit"], f"{row['key']} has no unit"

    @pytest.mark.parametrize("path", ENDPOINTS)
    def test_unknown_ticker_404(self, path):
        r = client.get(f"/api/v1/company/NOSUCHCO/{path}")
        assert r.status_code == 404

    def test_ticker_is_case_insensitive(self):
        assert fetch("income-statement", ticker="titan")["company"]["ticker"] == TICKER


class TestIncomeStatement:
    def test_matches_workbook_figures(self):
        """Titan fixture values recorded in QA_Report_v7.md §9.1."""
        body = fetch("income-statement")
        assert find_row(body, "total_revenue")["values"][-1] == pytest.approx(14528.6, abs=0.05)
        assert find_row(body, "ebitda")["values"][-1] == pytest.approx(961.9, abs=0.05)
        assert find_row(body, "pat")["values"][-1] == pytest.approx(125.5, abs=0.05)

    def test_ten_fiscal_years(self):
        assert len(fetch("income-statement")["periods"]["labels"]) == 10

    def test_subtotals_flagged_for_the_grid(self):
        body = fetch("income-statement")
        assert find_row(body, "ebitda")["is_subtotal"] is True
        assert find_row(body, "revenue_operations")["is_subtotal"] is False

    def test_first_period_growth_is_null(self):
        assert find_row(fetch("income-statement"), "revenue_growth")["values"][0] is None


class TestBalanceSheet:
    def test_ties_in_every_period(self):
        body = fetch("balance-sheet")
        for v in find_row(body, "balance_check")["values"]:
            assert abs(v) < 0.01
        assert body["warnings"] == []

    def test_assets_equal_equity_plus_liabilities(self):
        body = fetch("balance-sheet")
        assets = find_row(body, "total_assets")["values"]
        total = find_row(body, "total_equity_and_liabilities")["values"]
        for a, t in zip(assets, total):
            assert a == pytest.approx(t, abs=0.01)


class TestCashFlow:
    def test_reconciles_to_closing_cash(self):
        body = fetch("cash-flow")
        cfo = find_row(body, "cfo")["values"]
        cfi = find_row(body, "cfi")["values"]
        cff = find_row(body, "cff")["values"]
        net = find_row(body, "net_cash_flow")["values"]
        for i in range(len(net)):
            assert net[i] == pytest.approx(cfo[i] + cfi[i] + cff[i], abs=0.01)

    def test_closing_equals_opening_plus_net(self):
        body = fetch("cash-flow")
        opening = find_row(body, "opening_cash")["values"]
        closing = find_row(body, "closing_cash")["values"]
        net = find_row(body, "net_cash_flow")["values"]
        for i in range(len(net)):
            assert closing[i] == pytest.approx(opening[i] + net[i], abs=0.01)


class TestRatios:
    def test_six_families_and_45_plus_ratios(self):
        body = fetch("ratios")
        assert len(body["sections"]) == 6
        assert sum(len(s["rows"]) for s in body["sections"]) >= 45

    def test_wacc_query_enables_spread_and_eva(self):
        without = fetch("ratios")
        with_wacc = fetch("ratios", wacc=0.12)
        assert find_row(without, "roic_wacc_spread")["values"][-1] is None
        assert find_row(with_wacc, "roic_wacc_spread")["values"][-1] is not None
        assert with_wacc["wacc_assumption"] == 0.12

    def test_dupont_reconciles_to_roe(self):
        body = fetch("ratios")
        assert find_row(body, "dupont_roe")["values"][-1] == pytest.approx(
            find_row(body, "roe_avg")["values"][-1], rel=1e-9
        )

    def test_wacc_out_of_range_rejected(self):
        assert client.get(f"/api/v1/company/{TICKER}/ratios", params={"wacc": 1.5}).status_code == 422


class TestWorkingCapital:
    def test_ccc_equals_dio_plus_dso_minus_dpo(self):
        body = fetch("working-capital")
        dio = find_row(body, "dio")["values"][-1]
        dso = find_row(body, "dso")["values"][-1]
        dpo = find_row(body, "dpo")["values"][-1]
        assert find_row(body, "ccc")["values"][-1] == pytest.approx(dio + dso - dpo)

    def test_flags_present(self):
        keys = {f["key"] for f in fetch("working-capital")["flags"]}
        assert {"dso_outpacing_revenue", "inventory_days_high", "ccc_deteriorating"} == keys

    def test_cost_of_debt_derived_not_assumed(self):
        cod = fetch("working-capital")["cost_of_debt_assumption"]
        assert cod is not None and 0 < cod < 0.5


class TestCapex:
    def test_split_adds_to_gross(self):
        body = fetch("capex")
        gross = find_row(body, "gross_capex")["values"]
        maint = find_row(body, "maintenance_capex")["values"]
        grow = find_row(body, "growth_capex")["values"]
        for i in range(len(gross)):
            assert maint[i] + grow[i] == pytest.approx(gross[i])


class TestDebt:
    def test_instruments_reconcile_to_balance_sheet(self):
        body = fetch("debt")
        rec = body["reconciliation"]
        assert rec["reconciled"] is True
        assert rec["difference"] == pytest.approx(0.0, abs=0.01)

    def test_maturity_ladder_sums_to_gross_debt(self):
        body = fetch("debt")
        assert sum(b["amount"] for b in body["maturity_ladder"]) == pytest.approx(
            body["reconciliation"]["balance_sheet_gross_debt"], abs=0.01
        )

    def test_covenants_evaluated(self):
        body = fetch("debt")
        assert len(body["covenants"]) == 5
        assert all(c["compliant"] is not None for c in body["covenants"])

    def test_blended_rate_within_instrument_range(self):
        body = fetch("debt")
        rates = [i["interest_rate"] for i in body["instruments"] if i["interest_rate"]]
        assert min(rates) <= body["blended_rate"] <= max(rates)

    def test_instrument_shares_sum_to_one(self):
        shares = [i["share_of_debt"] for i in fetch("debt")["instruments"]]
        assert sum(shares) == pytest.approx(1.0)


class TestShareholding:
    def test_twelve_quarters(self):
        assert len(fetch("shareholding")["periods"]["labels"]) == 12

    def test_pattern_totals_one_hundred_percent(self):
        body = fetch("shareholding")
        for v in find_row(body, "total")["values"]:
            assert v == pytest.approx(1.0)

    def test_retail_residual_is_positive(self):
        for v in find_row(fetch("shareholding"), "public_retail")["values"]:
            assert v > 0, "seeded pattern over-allocates and squeezes out retail"

    def test_ownership_signal_present(self):
        signal = fetch("shareholding")["signal"]
        assert signal["signal"] in {
            "Accumulation", "Mild accumulation", "Stable",
            "Mild distribution", "Distribution",
        }

    def test_free_float_complements_promoter(self):
        body = fetch("shareholding")
        promoter = find_row(body, "promoter_total")["values"][-1]
        assert find_row(body, "free_float")["values"][-1] == pytest.approx(1 - promoter)


class TestOverview:
    def test_summary_covers_every_year(self):
        body = fetch("financials")
        assert len(body["summary"]) == len(body["periods"]["fiscal_years"])

    def test_cagr_reported(self):
        body = fetch("financials")
        assert body["revenue_cagr_full"] == pytest.approx(0.19, abs=0.005)

    def test_every_period_ties(self):
        assert all(s["balance_sheet_ties"] for s in fetch("financials")["summary"])


class TestUniverseWide:
    """Invariants that must hold for every seeded company, not just Titan."""

    TICKERS = ["RELIANCE", "TCS", "HDFCBANK", "INFY", "LT", "MARUTI", "WIPRO", "CIPLA"]

    @pytest.mark.parametrize("ticker", TICKERS)
    def test_balance_sheet_ties(self, ticker):
        body = fetch("balance-sheet", ticker=ticker)
        assert all(abs(v) < 0.01 for v in find_row(body, "balance_check")["values"])

    @pytest.mark.parametrize("ticker", TICKERS)
    def test_debt_reconciles(self, ticker):
        assert fetch("debt", ticker=ticker)["reconciliation"]["reconciled"] is True

    @pytest.mark.parametrize("ticker", TICKERS)
    def test_shareholding_totals_one(self, ticker):
        body = fetch("shareholding", ticker=ticker)
        assert all(v == pytest.approx(1.0) for v in find_row(body, "total")["values"])
