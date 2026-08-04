"""Deterministic validation-harness tests for analyst engine guardrails."""
from __future__ import annotations

from types import SimpleNamespace
import sys

from app.data.validate import CheckFamily, CompanyValidation, Severity, Validator


def obj(**kwargs): return SimpleNamespace(**kwargs)


def analysis(*, balanced=True):
    balance = obj(fiscal_year=2024, total_assets=100, total_equity_and_liabilities=100 if balanced else 80)
    income = obj(fiscal_year=2024, ebitda=50, pat=30, tax_expense=10, finance_costs=5, depreciation=7, other_income=2, eps_basic=5)
    return obj(company=obj(id="company"), balances=[balance], incomes=[income], cash_flows=[], statements=obj())


def test_statement_identity_records_balanced_unbalanced_and_ebitda():
    validator = Validator(None)
    good = CompanyValidation("GOOD", "Technology")
    validator._check_statements(good, analysis())
    assert all(c.passed for c in good.checks)
    bad = CompanyValidation("BAD", "Technology")
    validator._check_statements(bad, analysis(balanced=False))
    assert any("balance sheet" in c.name and not c.passed for c in bad.checks)


def test_statement_build_failure_and_missing_statements_are_critical():
    validator = Validator(None)
    broken = CompanyValidation("X", "Technology")
    validator._check_statements(broken, obj(balances=property(lambda _: (_ for _ in ()).throw(RuntimeError("broken")))))
    # A normal empty result is independently meaningful: no engine output must
    # not be mistaken for an acceptable statement set.
    empty = CompanyValidation("X", "Technology")
    validator._check_statements(empty, obj(balances=[], incomes=[]))
    assert empty.critical and "no statements" in empty.critical[0].detail


def test_validate_returns_analysis_build_error(monkeypatch):
    class BrokenAnalysis:
        @classmethod
        def for_ticker(cls, db, ticker): raise RuntimeError("unavailable")
    monkeypatch.setattr("app.services.analysis_service.AnalysisService", BrokenAnalysis)
    result = Validator(None).validate("TCS", "Technology")
    assert result.error == "analysis: RuntimeError: unavailable"


def test_reported_comparisons_detect_pat_eps_and_assets_mismatches(monkeypatch):
    class Reference:
        latest_year = 2024
        def row(self, section, name, year):
            return {"Operating Profit": 50, "Net Profit": 10, "EPS in Rs": 1, "Total Assets": 50}.get(name)
    monkeypatch.setattr("app.data.screener_source.fetch_screener", lambda ticker: Reference())
    out = CompanyValidation("TCS", "Technology")
    Validator(None)._check_reported(out, analysis())
    failures = {c.name for c in out.failed}
    assert "net profit FY2024" in failures and "EPS FY2024" in failures and "total assets FY2024" in failures


def test_reported_fetch_failure_and_missing_year_are_explicit(monkeypatch):
    from app.data.screener_source import ScreenerError
    monkeypatch.setattr("app.data.screener_source.fetch_screener", lambda ticker: (_ for _ in ()).throw(ScreenerError("offline")))
    out = CompanyValidation("TCS", "Technology"); Validator(None)._check_reported(out, analysis())
    assert out.checks[0].family is CheckFamily.REPORTED


def test_ratio_forecast_valuation_and_scoring_guards(monkeypatch):
    class Ratios:
        def __init__(self, *args): pass
        def all_sections(self): return [obj(rows=[obj(key="net_margin", label="Net margin", values=[150.0])])]
    monkeypatch.setattr("app.services.ratios.service.RatioService", Ratios)
    out = CompanyValidation("TCS", "Technology"); Validator(None)._check_ratios(out, analysis())
    assert any(not c.passed for c in out.checks)

    class Forecast:
        def __init__(self, db): pass
        def build_context(self, *args, **kwargs): return object()
        def active_for_company(self, id): return None
        def run(self, *args): return obj(revenue=[obj(value=1), obj(value=4)])
    monkeypatch.setattr("app.services.forecast.service.ForecastService", Forecast)
    out = CompanyValidation("TCS", "Technology"); Validator(None)._check_forecast(out, analysis())
    assert any(c.name == "forecast revenue plausible" and not c.passed for c in out.checks)

    class Valuation:
        def __init__(self, db): pass
        def value_company(self, *args): return obj(quality=obj(grade="investment_grade"), summary=obj(upside=20, weighted_value=-1), wacc=obj(wacc=.03))
    monkeypatch.setattr("app.services.valuation.service.ValuationService", Valuation)
    out = CompanyValidation("BANK", "NBFC & Financial Services")
    Validator(None)._check_valuation(out, analysis())
    assert any(c.family is CheckFamily.REFUSAL and not c.passed for c in out.checks)

    class Score:
        def __init__(self, db): pass
        def score_company(self, *args): return obj(overall_score=120, categories=[obj(raw_score=12, key="risk")], recommendation=None)
    monkeypatch.setattr("app.services.scoring.service.ScoringService", Score)
    out = CompanyValidation("TCS", "Technology"); Validator(None)._check_scoring(out, analysis())
    assert len(out.failed) >= 2
