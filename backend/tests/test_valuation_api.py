"""Integration tests for the valuation API."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

#: Coherent economics — used to validate the engine end to end.
REF = "BHARATCP"
#: Crude synthetic company — used to prove the quality gate fires.
SYNTH = "TITAN"


def valuation(ticker: str = REF, **params):
    r = client.get(f"/api/v1/company/{ticker}/valuation", params=params)
    assert r.status_code == 200, f"{r.status_code} {r.text[:300]}"
    return r.json()


class TestContract:
    def test_full_bundle_returned(self):
        body = valuation()
        assert {
            "wacc", "dcf_fcff", "dcf_fcfe", "relative", "ddm",
            "replacement", "summary", "quality", "scenario_values",
        } <= body.keys()

    @pytest.mark.parametrize("horizon", [3, 5, 10])
    def test_supported_horizons(self, horizon):
        body = valuation(horizon=horizon)
        assert len(body["dcf_fcff"]["years"]) == horizon

    def test_invalid_horizon_rejected(self):
        r = client.get(f"/api/v1/company/{REF}/valuation", params={"horizon": 7})
        assert r.status_code == 422

    def test_unknown_ticker_404(self):
        assert client.get("/api/v1/company/NOSUCH/valuation").status_code == 404


class TestDCFEndpoint:
    def test_both_conventions_available(self):
        ye = valuation(convention="year_end")["dcf_fcff"]
        my = valuation(convention="mid_year")["dcf_fcff"]
        assert my["sum_pv_explicit"] > ye["sum_pv_explicit"]

    def test_mid_year_discount_periods(self):
        years = valuation(convention="mid_year")["dcf_fcff"]["years"]
        assert years[0]["discount_period"] == pytest.approx(0.5)

    def test_year_end_discount_periods(self):
        years = valuation(convention="year_end")["dcf_fcff"]["years"]
        assert years[0]["discount_period"] == pytest.approx(1.0)

    def test_both_terminal_methods(self):
        gordon = valuation(terminal_method="perpetual_growth")["dcf_fcff"]
        exit_m = valuation(terminal_method="exit_multiple")["dcf_fcff"]
        assert gordon["terminal_method"] == "perpetual_growth"
        assert exit_m["terminal_method"] == "exit_multiple"
        assert gordon["terminal_value"] != exit_m["terminal_value"]

    def test_terminal_growth_override(self):
        low = valuation(terminal_growth=0.03)["dcf_fcff"]
        high = valuation(terminal_growth=0.06)["dcf_fcff"]
        assert high["intrinsic_value_per_share"] > low["intrinsic_value_per_share"]

    def test_fcfe_has_no_bridge(self):
        fcfe = valuation()["dcf_fcfe"]
        assert fcfe["net_debt"] is None
        assert fcfe["equity_value"] == pytest.approx(fcfe["enterprise_value"])

    def test_margin_of_safety_sets_buy_price(self):
        dcf = valuation(margin_of_safety=0.30)["dcf_fcff"]
        assert dcf["maximum_buy_price"] == pytest.approx(
            dcf["intrinsic_value_per_share"] * 0.70
        )


class TestWACCEndpoint:
    def test_components_exposed(self):
        w = valuation()["wacc"]
        assert {
            "risk_free_rate", "total_erp", "levered_beta", "cost_of_equity",
            "after_tax_cost_of_debt", "weight_equity", "weight_debt", "wacc",
        } <= w.keys()

    def test_weights_sum_to_one(self):
        w = valuation()["wacc"]
        assert w["weight_equity"] + w["weight_debt"] == pytest.approx(1.0)

    def test_wacc_lies_between_the_two_costs(self):
        w = valuation()["wacc"]
        lo = min(w["cost_of_equity"], w["after_tax_cost_of_debt"])
        hi = max(w["cost_of_equity"], w["after_tax_cost_of_debt"])
        assert lo <= w["wacc"] <= hi

    def test_dynamic_schedule_returned(self):
        body = valuation(dynamic_wacc=True, horizon=5)
        assert len(body["wacc_schedule"]) == 5

    def test_static_by_default(self):
        assert valuation()["wacc_schedule"] == []

    def test_dedicated_wacc_endpoint(self):
        r = client.get(f"/api/v1/company/{REF}/valuation/wacc")
        assert r.status_code == 200
        assert r.json()["wacc_schedule"]


class TestRelativeEndpoint:
    def test_all_required_multiples(self):
        current = valuation()["relative"]["current"]
        for key in ("pe", "pb", "ev_ebitda", "ev_sales", "ev_ebit"):
            assert key in current

    def test_forward_multiples_present(self):
        assert len(valuation()["relative"]["forward"]) >= 1

    def test_target_methods_present(self):
        keys = {m["key"] for m in valuation()["relative"]["methods"]}
        assert {"pe", "pb", "ev_ebitda", "ev_sales", "dcf"} <= keys

    def test_justified_multiples_present(self):
        keys = {j["key"] for j in valuation()["relative"]["justified"]}
        assert {"forward_pe", "trailing_pe", "pb"} <= keys

    def test_blended_target_within_range(self):
        rel = valuation()["relative"]
        assert rel["target_low"] <= rel["blended_target_price"] <= rel["target_high"]


class TestOtherMethods:
    def test_ddm_returned(self):
        ddm = valuation()["ddm"]
        assert ddm["variant"] in {"gordon", "two_stage", "h_model"}

    def test_replacement_value_returned(self):
        rep = valuation()["replacement"]
        assert rep["total_replacement_cost"] > 0
        assert rep["warnings"]

    def test_sotp_endpoint(self):
        r = client.post(
            f"/api/v1/company/{REF}/valuation/sotp",
            json={
                "segments": [
                    {"name": "Foods", "basis": "ev_ebitda", "multiple": 18, "metric": 3500},
                    {"name": "Home care", "basis": "ev_sales", "multiple": 3.5, "metric": 8000},
                ],
                "holding_discount": 0.12,
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert len(body["segments"]) == 2
        assert body["value_per_share"] > 0

    def test_sotp_requires_segments(self):
        r = client.post(f"/api/v1/company/{REF}/valuation/sotp", json={"segments": []})
        assert r.status_code == 422


class TestSummary:
    def test_all_six_methods_listed(self):
        keys = {m["key"] for m in valuation()["summary"]["methods"]}
        assert keys == {"dcf_fcff", "dcf_fcfe", "relative", "ddm", "replacement", "sotp"}

    def test_weighted_value_lies_within_the_range(self):
        s = valuation()["summary"]
        assert s["low"] <= s["weighted_value"] <= s["high"]

    def test_inapplicable_methods_carry_no_weight(self):
        for m in valuation()["summary"]["methods"]:
            if not m["applicable"]:
                assert m["weight"] == 0

    def test_recommendation_present(self):
        assert valuation()["summary"]["recommendation"] in {
            "Strong Buy", "Buy", "Accumulate", "Hold", "Reduce", "Sell",
            "Not rated — no market price",
        }

    def test_scenario_values_for_all_cases(self):
        assert set(valuation()["scenario_values"]) == {"bear", "base", "bull"}

    def test_scenarios_are_ordered(self):
        values = valuation()["scenario_values"]
        assert values["bear"] < values["base"] < values["bull"]


class TestDataQualityGate:
    """The requirement: never present unrealistic upside without a warning."""

    def test_quality_block_on_every_response(self):
        for path in ("", "/sensitivity", "/simulation"):
            r = client.get(f"/api/v1/company/{REF}/valuation{path}")
            assert "quality" in r.json()

    def test_synthetic_company_is_flagged(self):
        body = valuation(ticker=SYNTH)
        assert body["quality"]["is_illustrative"] is True
        assert "Illustrative valuation only" in body["quality"]["disclosure"]

    def test_synthetic_company_flags_the_absurd_multiple(self):
        keys = {i["key"] for i in valuation(ticker=SYNTH)["quality"]["issues"]}
        assert "implausible_multiple" in keys

    def test_non_filing_source_never_investment_grade(self):
        """Reference data is coherent but is not a filing."""
        assert valuation(ticker=REF)["quality"]["grade"] != "investment_grade"

    def test_disclosure_absent_only_when_grade_is_clean(self):
        body = valuation(ticker=REF)["quality"]
        assert (body["disclosure"] is None) == (body["is_illustrative"] is False)

    def test_headline_always_present(self):
        assert valuation()["quality"]["headline"]


class TestSensitivityEndpoint:
    def test_default_grid(self):
        r = client.get(f"/api/v1/company/{REF}/valuation/sensitivity")
        assert r.status_code == 200
        body = r.json()
        assert len(body["cells"]) == 5
        assert all(len(row) == 5 for row in body["cells"])

    @pytest.mark.parametrize(
        "row,col",
        [("wacc", "terminal_growth"), ("wacc", "exit_multiple"),
         ("revenue_cagr", "ebit_margin"), ("terminal_growth", "revenue_cagr")],
    )
    def test_all_required_axes(self, row, col):
        r = client.get(f"/api/v1/company/{REF}/valuation/sensitivity",
                       params={"row": row, "col": col})
        assert r.status_code == 200
        assert r.json()["row_key"] == row and r.json()["col_key"] == col

    def test_identical_axes_rejected(self):
        r = client.get(f"/api/v1/company/{REF}/valuation/sensitivity",
                       params={"row": "wacc", "col": "wacc"})
        assert r.status_code == 422

    def test_unknown_axis_rejected(self):
        r = client.get(f"/api/v1/company/{REF}/valuation/sensitivity",
                       params={"row": "moon_phase", "col": "wacc"})
        assert r.status_code == 422

    def test_value_declines_as_wacc_rises(self):
        body = client.get(f"/api/v1/company/{REF}/valuation/sensitivity").json()
        middle = [row[len(row) // 2] for row in body["cells"]]
        assert middle == sorted(middle, reverse=True)

    def test_upside_view_returned(self):
        body = client.get(f"/api/v1/company/{REF}/valuation/sensitivity").json()
        assert len(body["upside_cells"]) == len(body["cells"])

    def test_grid_size_configurable(self):
        body = client.get(f"/api/v1/company/{REF}/valuation/sensitivity",
                          params={"steps": 3}).json()
        assert len(body["cells"]) == 7


class TestSimulationEndpoint:
    def test_runs(self):
        r = client.get(f"/api/v1/company/{REF}/valuation/simulation",
                       params={"trials": 300})
        assert r.status_code == 200
        body = r.json()
        assert body["trials"] == 300
        assert body["mean_value"] is not None

    def test_percentiles_ordered(self):
        body = client.get(f"/api/v1/company/{REF}/valuation/simulation",
                          params={"trials": 300}).json()
        values = [body["percentiles"][str(p)] for p in (5, 25, 50, 75, 95)]
        assert values == sorted(values)

    def test_histogram_returned(self):
        body = client.get(f"/api/v1/company/{REF}/valuation/simulation",
                          params={"trials": 300}).json()
        assert len(body["histogram"]) == 20

    def test_reproducible(self):
        a = client.get(f"/api/v1/company/{REF}/valuation/simulation",
                       params={"trials": 300, "seed": 11}).json()
        b = client.get(f"/api/v1/company/{REF}/valuation/simulation",
                       params={"trials": 300, "seed": 11}).json()
        assert a["mean_value"] == pytest.approx(b["mean_value"])

    def test_trial_bounds_enforced(self):
        assert client.get(f"/api/v1/company/{REF}/valuation/simulation",
                          params={"trials": 10}).status_code == 422
        assert client.get(f"/api/v1/company/{REF}/valuation/simulation",
                          params={"trials": 999999}).status_code == 422


class TestUniverseWide:
    TICKERS = ["RELIANCE", "TCS", "HDFCBANK", "INFY", "BHARATCP"]

    @pytest.mark.parametrize("ticker", TICKERS)
    def test_valuation_runs(self, ticker):
        r = client.get(f"/api/v1/company/{ticker}/valuation", params={"horizon": 5})
        assert r.status_code == 200

    @pytest.mark.parametrize("ticker", TICKERS)
    def test_quality_gate_always_applied(self, ticker):
        body = client.get(f"/api/v1/company/{ticker}/valuation").json()
        quality = body["quality"]
        # Every seeded company is non-filing data, so none may be certified.
        assert quality["is_illustrative"] is True
        assert quality["disclosure"]

    @pytest.mark.parametrize("ticker", TICKERS)
    def test_no_unqualified_extreme_upside(self, ticker):
        """The core requirement: an extreme number must carry a critical flag."""
        body = client.get(f"/api/v1/company/{ticker}/valuation").json()
        upside = body["summary"]["upside"]
        if upside is not None and (upside > 3.0 or upside < -0.9):
            severities = {i["severity"] for i in body["quality"]["issues"]}
            assert "critical" in severities
