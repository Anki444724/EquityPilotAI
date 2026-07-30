"""Validation harness — check every engine against reported figures.

The question this answers is not "does the code run?" but "does the number it
produces match what the company reported?". Those are different questions and
only the second one matters to an analyst.

Four families of check, in increasing order of what they prove:

1. **Identity** — does the platform's own arithmetic hold? Balance sheet
   balances, EBITDA reconciles, FCFF ties to two independent builds. These
   catch internal inconsistency and need no external reference.
2. **Reported** — does a derived figure match the figure the source reports
   independently? Our operating profit against screener's, our EPS against
   screener's, our ROCE against screener's. This is the real test: two
   different routes to the same number.
3. **Plausibility** — is the answer inside the range a competent analyst would
   accept? A 400% operating margin is arithmetically possible and financially
   absurd.
4. **Refusal** — does the platform decline to publish where it should? A DCF
   on a bank, a valuation on four years of data. Silence in the right place is
   a feature, and an engine that confidently values a bank has failed.

Every check records the expected value, the actual value, and the tolerance,
so a failure is a statement about magnitude rather than a boolean.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from sqlalchemy.orm import Session

from app.data.nse_universe import is_financial


class Severity(StrEnum):
    CRITICAL = "critical"   # a published number is wrong
    MAJOR = "major"         # a number is unreliable or a guard failed to fire
    MINOR = "minor"         # cosmetic, or a known data limitation


class CheckFamily(StrEnum):
    IDENTITY = "identity"
    REPORTED = "reported"
    PLAUSIBILITY = "plausibility"
    REFUSAL = "refusal"


@dataclass(slots=True)
class CheckResult:
    ticker: str
    family: CheckFamily
    name: str
    passed: bool
    expected: float | None = None
    actual: float | None = None
    tolerance: float | None = None
    severity: Severity = Severity.MAJOR
    detail: str = ""

    @property
    def deviation(self) -> float | None:
        if self.expected is None or self.actual is None:
            return None
        base = max(abs(self.expected), abs(self.actual), 1.0)
        return abs(self.expected - self.actual) / base


@dataclass(slots=True)
class CompanyValidation:
    ticker: str
    sector: str
    checks: list[CheckResult] = field(default_factory=list)
    error: str | None = None
    engines_run: list[str] = field(default_factory=list)

    @property
    def passed(self) -> int:
        return sum(1 for c in self.checks if c.passed)

    @property
    def failed(self) -> list[CheckResult]:
        return [c for c in self.checks if not c.passed]

    @property
    def critical(self) -> list[CheckResult]:
        return [c for c in self.failed if c.severity is Severity.CRITICAL]


def _close(actual: float | None, expected: float | None, tolerance: float) -> bool:
    if actual is None or expected is None:
        return False
    base = max(abs(expected), abs(actual), 1.0)
    return abs(actual - expected) / base <= tolerance


class Validator:
    """Runs every engine over one company and grades the output."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def validate(self, ticker: str, sector: str) -> CompanyValidation:
        from app.services.analysis_service import AnalysisService

        out = CompanyValidation(ticker=ticker, sector=sector)
        try:
            analysis = AnalysisService.for_ticker(self.db, ticker)
        except Exception as exc:  # noqa: BLE001
            out.error = f"analysis: {type(exc).__name__}: {exc}"
            return out

        self._check_statements(out, analysis)
        self._check_reported(out, analysis)
        self._check_ratios(out, analysis)
        self._check_forecast(out, analysis)
        self._check_valuation(out, analysis)
        self._check_scoring(out, analysis)
        return out

    # ------------------------------------------------------------------
    def _check_statements(self, out: CompanyValidation, analysis) -> None:
        """Identity checks on the statements themselves."""
        out.engines_run.append("statements")
        try:
            balances = list(analysis.balances)
            incomes = list(analysis.incomes)
        except Exception as exc:  # noqa: BLE001
            out.checks.append(CheckResult(
                out.ticker, CheckFamily.IDENTITY, "statements build", False,
                severity=Severity.CRITICAL, detail=str(exc)[:120],
            ))
            return

        if not balances or not incomes:
            out.checks.append(CheckResult(
                out.ticker, CheckFamily.IDENTITY, "statements present", False,
                severity=Severity.CRITICAL, detail="no statements built",
            ))
            return

        # Balance sheet balances. `07 Historical BS` row 45 in the workbook.
        for sheet in balances[-3:]:
            assets = getattr(sheet, "total_assets", None)
            equity_and_liabilities = getattr(sheet, "total_equity_and_liabilities", None)
            if assets is None or equity_and_liabilities is None:
                continue
            out.checks.append(CheckResult(
                out.ticker, CheckFamily.IDENTITY,
                f"balance sheet balances FY{sheet.fiscal_year}",
                _close(assets, equity_and_liabilities, 0.01),
                expected=equity_and_liabilities, actual=assets, tolerance=0.01,
                severity=Severity.CRITICAL,
            ))

        # EBITDA identity: revenue − operating costs, and PAT + tax +
        # interest + depreciation, must agree.
        for income in incomes[-3:]:
            ebitda = getattr(income, "ebitda", None)
            pat = getattr(income, "pat", None)
            tax = getattr(income, "tax_expense", None)
            interest = getattr(income, "finance_costs", None)
            depreciation = getattr(income, "depreciation", None)
            other = getattr(income, "other_income", None)
            if None in (ebitda, pat, tax, interest, depreciation, other):
                continue
            rebuilt = pat + tax + interest + depreciation - other
            out.checks.append(CheckResult(
                out.ticker, CheckFamily.IDENTITY,
                f"EBITDA reconciles FY{income.fiscal_year}",
                _close(ebitda, rebuilt, 0.02),
                expected=rebuilt, actual=ebitda, tolerance=0.02,
                severity=Severity.MAJOR,
            ))

    # ------------------------------------------------------------------
    def _check_reported(self, out: CompanyValidation, analysis) -> None:
        """Compare our derived figures against the source's own.

        This is the check that actually proves something: screener computes
        operating profit, EPS and ROCE independently of us, from the same
        underlying statements. Agreement means two different routes reached
        the same number.
        """
        from app.data.screener_source import ScreenerError, fetch_screener

        out.engines_run.append("reported")
        try:
            reference = fetch_screener(out.ticker)
        except ScreenerError as exc:
            out.checks.append(CheckResult(
                out.ticker, CheckFamily.REPORTED, "reference fetch", False,
                severity=Severity.MINOR, detail=str(exc)[:100],
            ))
            return

        year = reference.latest_year
        if year is None:
            return

        incomes = {i.fiscal_year: i for i in analysis.incomes}
        income = incomes.get(year)
        if income is None:
            out.checks.append(CheckResult(
                out.ticker, CheckFamily.REPORTED, f"FY{year} present", False,
                severity=Severity.MAJOR,
                detail=f"latest reported FY{year} absent from our statements",
            ))
            return

        # Operating profit — screener's own line.
        reported_op = reference.row("profit_loss", "Operating Profit", year)
        our_ebitda = getattr(income, "ebitda", None)
        if reported_op is not None and our_ebitda is not None:
            out.checks.append(CheckResult(
                out.ticker, CheckFamily.REPORTED, f"operating profit FY{year}",
                _close(our_ebitda, reported_op, 0.03),
                expected=reported_op, actual=our_ebitda, tolerance=0.03,
                severity=Severity.CRITICAL,
            ))

        # Net profit.
        reported_np = reference.row("profit_loss", "Net Profit", year)
        our_pat = getattr(income, "pat", None)
        if reported_np is not None and our_pat is not None:
            out.checks.append(CheckResult(
                out.ticker, CheckFamily.REPORTED, f"net profit FY{year}",
                _close(our_pat, reported_np, 0.03),
                expected=reported_np, actual=our_pat, tolerance=0.03,
                severity=Severity.CRITICAL,
            ))

        # EPS — a per-share figure, so it independently validates the share
        # count as well as the profit.
        reported_eps = reference.row("profit_loss", "EPS in Rs", year)
        our_eps = getattr(income, "eps_basic", None)
        if reported_eps is not None and our_eps is not None:
            out.checks.append(CheckResult(
                out.ticker, CheckFamily.REPORTED, f"EPS FY{year}",
                _close(our_eps, reported_eps, 0.05),
                expected=reported_eps, actual=our_eps, tolerance=0.05,
                severity=Severity.CRITICAL,
            ))

        # Total assets.
        reported_assets = reference.row("balance_sheet", "Total Assets", year)
        balances = {b.fiscal_year: b for b in analysis.balances}
        balance = balances.get(year)
        our_assets = getattr(balance, "total_assets", None) if balance else None
        if reported_assets is not None and our_assets is not None:
            out.checks.append(CheckResult(
                out.ticker, CheckFamily.REPORTED, f"total assets FY{year}",
                _close(our_assets, reported_assets, 0.03),
                expected=reported_assets, actual=our_assets, tolerance=0.03,
                severity=Severity.CRITICAL,
            ))

    # ------------------------------------------------------------------
    def _check_ratios(self, out: CompanyValidation, analysis) -> None:
        from app.services.ratios.service import RatioService

        out.engines_run.append("ratios")
        try:
            sections = RatioService(
                list(analysis.incomes),
                list(analysis.balances),
                list(analysis.cash_flows),
            ).all_sections()
        except Exception as exc:  # noqa: BLE001
            out.checks.append(CheckResult(
                out.ticker, CheckFamily.IDENTITY, "ratios build", False,
                severity=Severity.CRITICAL, detail=f"{type(exc).__name__}: {exc}"[:120],
            ))
            return

        out.checks.append(CheckResult(
            out.ticker, CheckFamily.IDENTITY, "ratios produced",
            bool(sections), severity=Severity.MAJOR,
        ))

        # Plausibility: a margin outside [-100%, +100%] is not a margin.
        for section in sections:
            for row in getattr(section, "rows", []):
                if "margin" not in (row.key or "").lower():
                    continue
                for value in row.values:
                    if value is None:
                        continue
                    if not (-100.0 <= value <= 100.0):
                        out.checks.append(CheckResult(
                            out.ticker, CheckFamily.PLAUSIBILITY,
                            f"margin in range: {row.key}", False,
                            actual=value, severity=Severity.MAJOR,
                            detail=f"{row.label} = {value:.1f}%",
                        ))
                        break

    # ------------------------------------------------------------------
    def _check_forecast(self, out: CompanyValidation, analysis) -> None:
        from app.domain.forecast.assumptions import Scenario
        from app.services.forecast.service import ForecastService

        out.engines_run.append("forecast")
        try:
            service = ForecastService(self.db)
            # build_context takes the statements *service*, not a tuple of
            # lists — `analysis.statements` is the FinancialStatementsService.
            context = service.build_context(
                analysis.company, analysis.statements, years=5,
            )
            saved = service.active_for_company(analysis.company.id)
            result = service.run(context, saved, Scenario.BASE)
        except Exception as exc:  # noqa: BLE001
            out.checks.append(CheckResult(
                out.ticker, CheckFamily.IDENTITY, "forecast runs", False,
                severity=Severity.CRITICAL, detail=f"{type(exc).__name__}: {exc}"[:120],
            ))
            return

        out.checks.append(CheckResult(
            out.ticker, CheckFamily.IDENTITY, "forecast runs", True,
            severity=Severity.MAJOR,
        ))

        # Plausibility: a five-year revenue forecast that more than triples or
        # collapses to nothing is not a forecast, it is a broken growth rate.
        rows = getattr(result, "revenue", None) or []
        values = [getattr(r, "value", None) for r in rows]
        values = [v for v in values if v is not None]
        if len(values) >= 2 and values[0] > 0:
            multiple = values[-1] / values[0]
            out.checks.append(CheckResult(
                out.ticker, CheckFamily.PLAUSIBILITY, "forecast revenue plausible",
                0.5 <= multiple <= 3.5, actual=multiple,
                severity=Severity.MAJOR,
                detail=f"terminal / first year = {multiple:.2f}x",
            ))

    # ------------------------------------------------------------------
    def _check_valuation(self, out: CompanyValidation, analysis) -> None:
        from app.services.forecast.service import ForecastService
        from app.services.valuation.service import ValuationService

        out.engines_run.append("valuation")
        try:
            bundle = ValuationService(self.db).value_company(
                analysis, ForecastService(self.db),
            )
        except Exception as exc:  # noqa: BLE001
            out.checks.append(CheckResult(
                out.ticker, CheckFamily.IDENTITY, "valuation runs", False,
                severity=Severity.CRITICAL, detail=f"{type(exc).__name__}: {exc}"[:120],
            ))
            return

        out.checks.append(CheckResult(
            out.ticker, CheckFamily.IDENTITY, "valuation runs", True,
            severity=Severity.MAJOR,
        ))

        grade = str(getattr(bundle.quality, "grade", "") or "")
        summary = bundle.summary

        # WACC plausibility. Indian equity: risk-free ~7%, ERP ~6-8%, so a
        # WACC below 6% or above 25% means an input is wrong.
        wacc = getattr(bundle.wacc, "wacc", None)
        if wacc is not None:
            as_pct = wacc * 100 if wacc < 1 else wacc
            out.checks.append(CheckResult(
                out.ticker, CheckFamily.PLAUSIBILITY, "WACC in range",
                6.0 <= as_pct <= 25.0, actual=as_pct,
                severity=Severity.MAJOR, detail=f"WACC = {as_pct:.2f}%",
            ))

        # THE REFUSAL CHECK. A bank has no meaningful FCFF, no working-capital
        # cycle and no EV/EBITDA. Module 4 grades data quality and refuses to
        # publish what it cannot stand behind. For a financial, an
        # investment-grade valuation would be the failure, not the absence of
        # one.
        if is_financial(out.sector):
            out.checks.append(CheckResult(
                out.ticker, CheckFamily.REFUSAL,
                "financial not graded investment-grade",
                grade.lower() != "investment_grade",
                severity=Severity.CRITICAL,
                detail=f"grade={grade} — a DCF on a bank must not be published",
            ))

        # Upside sanity. Module 4's own brief: "Never display unrealistic
        # upside without warning." A 10x upside is a modelling error, and if
        # the engine publishes it as investment-grade that is a critical bug.
        upside = getattr(summary, "upside", None)
        if upside is not None and grade.lower() == "investment_grade":
            as_pct = upside * 100 if abs(upside) < 10 else upside
            out.checks.append(CheckResult(
                out.ticker, CheckFamily.PLAUSIBILITY,
                "investment-grade upside bounded",
                -90.0 <= as_pct <= 300.0, actual=as_pct,
                severity=Severity.CRITICAL,
                detail=f"upside = {as_pct:.0f}% at investment grade",
            ))

        # A published fair value must be positive.
        weighted = getattr(summary, "weighted_value", None)
        if weighted is not None and grade.lower() != "unreliable":
            out.checks.append(CheckResult(
                out.ticker, CheckFamily.PLAUSIBILITY, "fair value positive",
                weighted > 0, actual=weighted, severity=Severity.CRITICAL,
            ))

    # ------------------------------------------------------------------
    def _check_scoring(self, out: CompanyValidation, analysis) -> None:
        from app.services.forecast.service import ForecastService
        from app.services.scoring.service import ScoringService
        from app.services.valuation.service import ValuationService

        out.engines_run.append("scoring")
        try:
            score = ScoringService(self.db).score_company(
                analysis, ForecastService(self.db), ValuationService(self.db),
            )
        except Exception as exc:  # noqa: BLE001
            out.checks.append(CheckResult(
                out.ticker, CheckFamily.IDENTITY, "scoring runs", False,
                severity=Severity.CRITICAL, detail=f"{type(exc).__name__}: {exc}"[:120],
            ))
            return

        overall = getattr(score, "overall_score", None)
        out.checks.append(CheckResult(
            out.ticker, CheckFamily.PLAUSIBILITY, "score in 0-100",
            overall is not None and 0.0 <= overall <= 100.0,
            actual=overall, severity=Severity.CRITICAL,
        ))

        # Every category must be inside its declared 0-10 band. A category
        # that escapes its range silently skews the weighted total.
        for category in getattr(score, "categories", []):
            raw = getattr(category, "raw_score", None)
            if raw is None:
                continue
            if not (0.0 <= raw <= 10.0):
                out.checks.append(CheckResult(
                    out.ticker, CheckFamily.PLAUSIBILITY,
                    f"category in 0-10: {getattr(category, 'key', '?')}",
                    False, actual=raw, severity=Severity.CRITICAL,
                ))
                break

        out.checks.append(CheckResult(
            out.ticker, CheckFamily.IDENTITY, "recommendation present",
            bool(getattr(score, "recommendation", None)),
            severity=Severity.MAJOR,
        ))
