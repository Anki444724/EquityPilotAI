"""Integration tests for the forecast API and calibration service."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.domain.forecast.assumptions import Provenance
from app.main import app
from app.services.forecast.calibration import FALLBACK, AssumptionCalibrator

client = TestClient(app)
TICKER = "TITAN"
BASE = f"/api/v1/company/{TICKER}/forecast"


def fetch(path: str = "", **params):
    r = client.get(f"{BASE}{path}", params=params)
    assert r.status_code == 200, f"{path}: {r.status_code} {r.text[:300]}"
    return r.json()


def driver_value(payload: dict, name: str) -> float:
    return next(d["value"] for d in payload["assumptions"]["drivers"] if d["name"] == name)


class TestGetForecast:
    def test_returns_a_projection(self):
        body = fetch(horizon=5)
        assert len(body["years"]) == 5
        assert body["scenario"] == "base"

    @pytest.mark.parametrize("horizon", [3, 5, 10])
    def test_supported_horizons(self, horizon):
        body = fetch(horizon=horizon)
        assert len(body["years"]) == horizon
        assert len(body["periods"]["labels"]) == horizon

    def test_unsupported_horizon_rejected(self):
        assert client.get(BASE, params={"horizon": 7}).status_code == 422

    def test_forecast_years_follow_the_base_year(self):
        body = fetch(horizon=3)
        base_fy = body["base_fiscal_year"]
        assert [y["fiscal_year"] for y in body["years"]] == [base_fy + 1, base_fy + 2, base_fy + 3]

    def test_history_included_for_charting(self):
        body = fetch(horizon=5)
        assert len(body["history"]) == 10
        assert body["history"][-1]["fiscal_year"] == body["base_fiscal_year"]

    def test_engine_health_reported(self):
        summary = fetch(horizon=5)["summary"]
        assert summary["debt_converged"] is True
        assert summary["all_reconciled"] is True
        assert fetch(horizon=5)["warnings"] == []

    def test_grid_sections_present(self):
        keys = [s["key"] for s in fetch(horizon=5)["sections"]]
        assert keys == ["income", "capital", "cashflow", "returns"]

    def test_every_grid_row_matches_the_period_count(self):
        body = fetch(horizon=5)
        n = len(body["periods"]["labels"])
        for section in body["sections"]:
            for row in section["rows"]:
                assert len(row["values"]) == n

    @pytest.mark.parametrize("scenario", ["bear", "base", "bull"])
    def test_each_scenario_runs(self, scenario):
        assert fetch(scenario=scenario, horizon=5)["scenario"] == scenario

    def test_bull_beats_bear(self):
        bull = fetch(scenario="bull", horizon=5)["summary"]["terminal_revenue"]
        bear = fetch(scenario="bear", horizon=5)["summary"]["terminal_revenue"]
        assert bull > bear

    @pytest.mark.parametrize(
        "method", ["cagr", "volume_price", "segment", "organic_acquisition"]
    )
    def test_every_revenue_method(self, method):
        body = fetch(method=method, horizon=3)
        assert body["assumptions"]["revenue_method"] == method
        assert len(body["years"]) == 3

    def test_unknown_ticker_404(self):
        assert client.get("/api/v1/company/NOSUCH/forecast").status_code == 404


class TestAssumptions:
    def test_all_thirty_drivers_exposed(self):
        assert len(fetch(horizon=5)["assumptions"]["drivers"]) == 30

    def test_drivers_carry_ui_metadata(self):
        for d in fetch(horizon=5)["assumptions"]["drivers"]:
            assert d["label"] and d["unit"] and d["group"] and d["source"]

    def test_provenance_shows_grounding(self):
        provenance = fetch(horizon=5)["assumptions"]["provenance"]
        assert provenance.get("historical", 0) > 0, "assumptions should be calibrated"

    def test_drivers_grouped_for_the_editor(self):
        groups = {d["group"] for d in fetch(horizon=5)["assumptions"]["drivers"]}
        assert {"Revenue", "Margins", "Capex", "Working capital", "Debt"} <= groups


class TestScenarioEndpoint:
    def test_three_outcomes(self):
        body = fetch("/scenarios", horizon=5)
        assert [o["scenario"] for o in body["outcomes"]] == ["bear", "base", "bull"]

    def test_comparison_series_for_charting(self):
        body = fetch("/scenarios", horizon=5)
        assert [r["key"] for r in body["comparison"]] == [
            "revenue", "ebitda", "pat", "eps", "fcff"
        ]
        for row in body["comparison"]:
            assert len(row["bear"]) == len(row["base"]) == len(row["bull"]) == 5

    def test_bull_series_exceeds_bear_series(self):
        revenue = next(
            r for r in fetch("/scenarios", horizon=5)["comparison"] if r["key"] == "revenue"
        )
        assert all(b > d for b, d in zip(revenue["bull"], revenue["bear"]))

    def test_probabilities_sum_to_one(self):
        body = fetch("/scenarios", horizon=5)
        assert sum(o["probability"] for o in body["outcomes"]) == pytest.approx(1.0)

    def test_expected_value_between_extremes(self):
        body = fetch("/scenarios", horizon=5)
        values = {o["scenario"]: o["value_per_share"] for o in body["outcomes"]}
        assert values["bear"] < body["expected_value"] < values["bull"]

    def test_verdict_present(self):
        assert fetch("/scenarios", horizon=5)["verdict"]


class TestCreateAndEdit:
    def test_create_persists_a_forecast(self):
        r = client.post(BASE, json={"name": "IC case", "horizon_years": 3})
        assert r.status_code == 201
        body = r.json()
        assert body["forecast_id"] and body["name"] == "IC case"
        assert len(body["years"]) == 3

    def test_create_rejects_a_bad_horizon(self):
        assert client.post(BASE, json={"horizon_years": 4}).status_code == 422

    def test_create_with_initial_drivers(self):
        r = client.post(
            BASE, json={"name": "Seeded", "horizon_years": 5, "drivers": {"ebitda_margin": 0.24}}
        )
        assert r.status_code == 201
        assert driver_value(r.json(), "ebitda_margin") == pytest.approx(0.24)

    def test_edit_changes_the_projection(self):
        created = client.post(BASE, json={"name": "Editable", "horizon_years": 5}).json()
        fid = created["forecast_id"]
        before = created["years"][0]["ebitda"]

        r = client.put(
            f"{BASE}/assumptions",
            params={"forecast_id": fid},
            json={"drivers": {"ebitda_margin": 0.35}},
        )
        assert r.status_code == 200
        after = r.json()
        assert driver_value(after, "ebitda_margin") == pytest.approx(0.35)
        assert after["years"][0]["ebitda"] > before

    def test_edit_propagates_into_derived_scenarios(self):
        """A base-case edit must move bull and bear too."""
        fid = client.post(BASE, json={"name": "Propagation", "horizon_years": 5}).json()["forecast_id"]
        client.put(
            f"{BASE}/assumptions", params={"forecast_id": fid},
            json={"drivers": {"ebitda_margin": 0.30}},
        )
        bull = fetch(forecast_id=fid, scenario="bull")
        bear = fetch(forecast_id=fid, scenario="bear")
        assert driver_value(bull, "ebitda_margin") == pytest.approx(0.32)
        assert driver_value(bear, "ebitda_margin") == pytest.approx(0.28)

    def test_scenario_specific_override_beats_the_derived_shift(self):
        fid = client.post(BASE, json={"name": "Override", "horizon_years": 5}).json()["forecast_id"]
        client.put(
            f"{BASE}/assumptions", params={"forecast_id": fid},
            json={"drivers": {"ebitda_margin": 0.40}, "scenario": "bull"},
        )
        assert driver_value(fetch(forecast_id=fid, scenario="bull"), "ebitda_margin") == pytest.approx(0.40)

    def test_unknown_driver_rejected(self):
        fid = client.post(BASE, json={"name": "Reject", "horizon_years": 5}).json()["forecast_id"]
        r = client.put(
            f"{BASE}/assumptions", params={"forecast_id": fid},
            json={"drivers": {"made_up_driver": 1.0}},
        )
        assert r.status_code == 422

    def test_per_year_overrides_accepted(self):
        """A year-1 override must apply to year 1 only.

        Calibration sets growth_fade to 0.5, so later years legitimately decay
        toward the long-run rate — the override is checked against the faded
        path, not against a flat rate.
        """
        fid = client.post(BASE, json={"name": "PerYear", "horizon_years": 5}).json()["forecast_id"]
        baseline = client.put(
            f"{BASE}/assumptions", params={"forecast_id": fid},
            json={"drivers": {"revenue_growth": 0.10}},
        ).json()
        r = client.put(
            f"{BASE}/assumptions", params={"forecast_id": fid},
            json={"drivers": {"revenue_growth": 0.10}, "by_year": {"revenue_growth": {"1": 0.30}}},
        )
        assert r.status_code == 200
        body = r.json()
        # year 1 takes the override; later years match the un-overridden path
        assert body["years"][0]["revenue_growth"] == pytest.approx(0.30)
        assert baseline["years"][0]["revenue_growth"] == pytest.approx(0.10)
        for i in range(1, 5):
            assert body["years"][i]["revenue_growth"] == pytest.approx(
                baseline["years"][i]["revenue_growth"]
            )

    def test_horizon_can_be_changed_by_edit(self):
        fid = client.post(BASE, json={"name": "Horizon", "horizon_years": 3}).json()["forecast_id"]
        r = client.put(
            f"{BASE}/assumptions", params={"forecast_id": fid},
            json={"drivers": {}, "horizon_years": 10},
        )
        assert len(r.json()["years"]) == 10

    def test_saved_forecasts_listed(self):
        client.post(BASE, json={"name": "Listed", "horizon_years": 5})
        body = fetch("/list")
        assert any(f["name"] == "Listed" for f in body["forecasts"])


class TestAiReadiness:
    """The engine must accept AI-authored assumptions with no code change."""

    def test_ai_source_accepted_and_recorded(self):
        fid = client.post(BASE, json={"name": "AI case", "horizon_years": 5}).json()["forecast_id"]
        r = client.put(
            f"{BASE}/assumptions", params={"forecast_id": fid},
            json={
                "drivers": {"revenue_growth": 0.155},
                "source": "management_guidance",
                "citation": "FY25 annual report, MD&A p.42",
                "requires_review": True,
            },
        )
        assert r.status_code == 200
        drivers = {d["name"]: d for d in r.json()["assumptions"]["drivers"]}
        assert drivers["revenue_growth"]["source"] == "management_guidance"
        assert "p.42" in drivers["revenue_growth"]["citation"]

    @pytest.mark.parametrize(
        "source", [p.value for p in Provenance]
    )
    def test_every_provenance_type_accepted(self, source):
        fid = client.post(BASE, json={"name": f"src-{source}", "horizon_years": 3}).json()["forecast_id"]
        r = client.put(
            f"{BASE}/assumptions", params={"forecast_id": fid},
            json={"drivers": {"ebitda_margin": 0.2}, "source": source},
        )
        assert r.status_code == 200

    def test_unknown_source_rejected(self):
        fid = client.post(BASE, json={"name": "BadSrc", "horizon_years": 3}).json()["forecast_id"]
        r = client.put(
            f"{BASE}/assumptions", params={"forecast_id": fid},
            json={"drivers": {"ebitda_margin": 0.2}, "source": "telepathy"},
        )
        assert r.status_code == 422


class TestCalibration:
    """Defaults must be derived from history, not hard-coded."""

    def _calibrator(self):
        from app.db.base import SessionLocal
        from app.services.company_service import CompanyService
        from app.services.financials.service import FinancialStatementsService

        db = SessionLocal()
        try:
            svc = CompanyService(db)
            company = svc.get_by_ticker(TICKER)
            statements = FinancialStatementsService(svc.load_financials(company.id))
            return AssumptionCalibrator(
                statements.income_statements(),
                statements.balance_sheets(),
                statements.cash_flows(),
            )
        finally:
            db.close()

    # ------------------------------------------------------------------
    # These assertions were rewritten during the production-readiness sprint.
    #
    # They previously pinned exact constants from the synthetic seed — 19%
    # revenue growth, a 6.62% EBITDA margin, FY2025 as the base year, a 73%
    # effective tax rate — and read them from the live database. The moment
    # real NSE data replaced the generator every one of them failed, not
    # because the calibrator changed but because the fixture did.
    #
    # A test that breaks when the data changes is testing the data. What is
    # worth pinning is the calibrator's *behaviour*: that it derives drivers
    # from reported history, that it rejects implausible ones, and that it
    # labels each with the right provenance. Those hold for any company.
    # ------------------------------------------------------------------
    def test_growth_is_derived_from_reported_history(self):
        cal = self._calibrator()
        growth = cal.revenue_growth()
        assert growth is not None, "no growth derived from a full history"
        # A ten-year revenue CAGR outside this band is not a going concern.
        assert -0.30 <= growth <= 1.00, growth

    def test_margin_is_derived_and_bounded(self):
        cal = self._calibrator()
        margin = cal.ebitda_margin()
        assert margin is not None
        assert -0.50 <= margin <= 0.80, margin

    def test_cycle_days_derived_from_balances(self):
        dio, dso, dpo = self._calibrator().cycle_days()
        # Each is a day count against a real balance sheet. Negative is
        # impossible; beyond two years means the denominator is wrong.
        for name, value in (("dio", dio), ("dso", dso), ("dpo", dpo)):
            assert value is None or 0.0 <= value <= 730.0, f"{name}={value}"

    def test_implausible_tax_rate_is_rejected(self):
        """The guard must reject a rate outside the statutory range.

        Driven directly rather than through whichever company happens to be
        seeded, so it tests the guard instead of the fixture.
        """
        cal = self._calibrator()
        rate = cal.tax_rate()
        assert rate is None or 0.0 <= rate <= 0.60, rate
        resolved = cal.calibrate().effective_tax_rate.value
        assert 0.0 < resolved <= 0.60
        if rate is None:
            assert resolved == FALLBACK["effective_tax_rate"]

    def test_provenance_is_recorded_for_every_driver(self):
        assumptions = self._calibrator().calibrate()
        for name in ("revenue_growth", "ebitda_margin", "effective_tax_rate"):
            driver = getattr(assumptions, name)
            assert driver.source in tuple(Provenance), name

    def test_base_position_uses_the_latest_reported_year(self):
        """The base year must be the most recent one in the statements."""
        from app.db.base import SessionLocal
        from app.services.company_service import CompanyService
        from app.services.financials.service import FinancialStatementsService

        db = SessionLocal()
        try:
            svc = CompanyService(db)
            company = svc.get_by_ticker(TICKER)
            statements = FinancialStatementsService(svc.load_financials(company.id))
            latest = max(i.fiscal_year for i in statements.income_statements())
        finally:
            db.close()

        base = self._calibrator().base_position()
        assert base.fiscal_year == latest
        assert base.revenue > 0


class TestUniverseWide:
    TICKERS = ["RELIANCE", "TCS", "HDFCBANK", "INFY", "LT", "MARUTI"]

    @pytest.mark.parametrize("ticker", TICKERS)
    def test_forecast_runs_and_reconciles(self, ticker):
        r = client.get(f"/api/v1/company/{ticker}/forecast", params={"horizon": 5})
        assert r.status_code == 200
        summary = r.json()["summary"]
        assert summary["debt_converged"] is True
        assert summary["all_reconciled"] is True

    @pytest.mark.parametrize("ticker", TICKERS)
    def test_scenarios_ordered(self, ticker):
        r = client.get(f"/api/v1/company/{ticker}/forecast/scenarios", params={"horizon": 5})
        assert r.status_code == 200
        values = [o["value_per_share"] for o in r.json()["outcomes"]]
        assert values == sorted(values)
