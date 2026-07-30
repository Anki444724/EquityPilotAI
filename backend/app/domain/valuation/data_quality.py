"""Data-quality assessment for valuation outputs.

A valuation is only as credible as the data beneath it. This module grades that
data and attaches an explicit disclosure to every result, so an implausible
number can never be presented as an investment-grade conclusion.

Two distinct problems are detected:

1. **Provenance** — is the underlying data real filings, or synthetic/seeded?
2. **Plausibility** — even with real data, does the output imply something
   absurd (a 4,000% upside, a 300x EV/EBITDA)?

Either one downgrades the grade and forces a warning banner. The platform
should be willing to say "this number is not trustworthy" about its own output.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from app.domain.calc import safe_div


class DataGrade(StrEnum):
    """Confidence in the valuation output."""

    INVESTMENT_GRADE = "investment_grade"   # real filings, plausible outputs
    INDICATIVE = "indicative"               # real data, some gaps or stretch
    ILLUSTRATIVE = "illustrative"           # synthetic/incomplete — demo only
    UNRELIABLE = "unreliable"               # outputs are not usable


class Severity(StrEnum):
    INFO = "info"
    WARN = "warn"
    CRITICAL = "critical"


#: The disclosure the brief requires whenever data is synthetic or incomplete.
ILLUSTRATIVE_NOTICE = (
    "Illustrative valuation only. Real filings are required for "
    "investment-grade outputs."
)

# --- plausibility thresholds ------------------------------------------------
#: Above this the upside is almost certainly a data artefact, not an opportunity.
IMPLAUSIBLE_UPSIDE = 3.00      # +300%
STRETCHED_UPSIDE = 1.00        # +100%
IMPLAUSIBLE_DOWNSIDE = -0.90   # −90%
#: An EV/EBITDA this high means EBITDA is wrong, not that the stock is dear.
IMPLAUSIBLE_EV_EBITDA = 60.0
#: Sources accepted as authoritative. Everything else is non-filing data and
#: cannot support an investment-grade conclusion.
TRUSTED_SOURCES = {
    "filing", "annual_report", "quarterly_filing", "exchange_filing",
    "audited_statement", "regulatory_filing", "xbrl",
}

#: Coverage of the canonical 54-item grid below which the model is under-fed.
MIN_COVERAGE = 0.60
#: Minimum years of history for a defensible trend.
MIN_HISTORY_YEARS = 3


@dataclass(frozen=True, slots=True)
class QualityIssue:
    key: str
    message: str
    severity: Severity
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class DataQualityReport:
    grade: DataGrade
    is_illustrative: bool
    disclosure: str | None
    issues: list[QualityIssue] = field(default_factory=list)
    coverage: float | None = None
    history_years: int | None = None
    synthetic_sources: list[str] = field(default_factory=list)

    @property
    def blocking(self) -> list[QualityIssue]:
        return [i for i in self.issues if i.severity is Severity.CRITICAL]

    @property
    def headline(self) -> str:
        return {
            DataGrade.INVESTMENT_GRADE: "Investment-grade — sourced from reported filings.",
            DataGrade.INDICATIVE: "Indicative — usable, but review the flagged items.",
            DataGrade.ILLUSTRATIVE: ILLUSTRATIVE_NOTICE,
            DataGrade.UNRELIABLE: "Unreliable — outputs are not suitable for any decision.",
        }[self.grade]


def assess_data_quality(
    *,
    fact_sources: set[str] | None = None,
    coverage: float | None = None,
    history_years: int | None = None,
    upside: float | None = None,
    ev_ebitda: float | None = None,
    terminal_value_pct: float | None = None,
    assumption_provenance: dict[str, int] | None = None,
    balance_sheet_ties: bool = True,
    forecast_converged: bool = True,
) -> DataQualityReport:
    """Grade the inputs and outputs of a valuation."""
    issues: list[QualityIssue] = []
    synthetic: list[str] = []

    # ---- 1. provenance ---------------------------------------------------
    # Allowlist, not blocklist. Anything not positively recognised as a filing
    # is treated as non-authoritative — an unknown source must never be
    # certified investment-grade by default.
    sources = {s for s in (fact_sources or set()) if s}
    non_filing = sorted(s for s in sources if s.lower() not in TRUSTED_SOURCES)
    synthetic.extend(non_filing)

    if not sources:
        issues.append(QualityIssue(
            key="unknown_provenance",
            message="Financial data has no recorded source.",
            severity=Severity.CRITICAL,
        ))
    elif non_filing:
        issues.append(QualityIssue(
            key="synthetic_data",
            message="Financial data is not sourced from filings.",
            severity=Severity.CRITICAL,
            detail=f"source(s): {', '.join(non_filing)}",
        ))

    if coverage is not None and coverage < MIN_COVERAGE:
        issues.append(QualityIssue(
            key="low_coverage",
            message=f"Only {coverage:.0%} of the canonical line-item grid is populated.",
            severity=Severity.CRITICAL if coverage < 0.35 else Severity.WARN,
        ))

    if history_years is not None and history_years < MIN_HISTORY_YEARS:
        issues.append(QualityIssue(
            key="short_history",
            message=f"Only {history_years} year(s) of history; trends are not established.",
            severity=Severity.WARN,
        ))

    if assumption_provenance:
        total = sum(assumption_provenance.values())
        grounded = assumption_provenance.get("historical", 0)
        share = safe_div(grounded, total)
        if share is not None and share < 0.3:
            issues.append(QualityIssue(
                key="ungrounded_assumptions",
                message=f"Only {share:.0%} of assumptions are calibrated from history.",
                severity=Severity.WARN,
            ))

    # ---- 2. structural integrity ------------------------------------------
    if not balance_sheet_ties:
        issues.append(QualityIssue(
            key="balance_sheet",
            message="Balance sheet does not tie; derived capital measures are unsafe.",
            severity=Severity.CRITICAL,
        ))
    if not forecast_converged:
        issues.append(QualityIssue(
            key="no_convergence",
            message="Forecast debt schedule did not converge.",
            severity=Severity.CRITICAL,
        ))

    # ---- 3. output plausibility -------------------------------------------
    if upside is not None:
        if upside > IMPLAUSIBLE_UPSIDE:
            issues.append(QualityIssue(
                key="implausible_upside",
                message=f"Computed upside of {upside:+.0%} is implausible and most likely "
                        "reflects a data error rather than an opportunity.",
                severity=Severity.CRITICAL,
            ))
        elif upside > STRETCHED_UPSIDE:
            issues.append(QualityIssue(
                key="large_upside",
                message=f"Upside of {upside:+.0%} is unusually large; verify the inputs.",
                severity=Severity.WARN,
            ))
        elif upside < IMPLAUSIBLE_DOWNSIDE:
            issues.append(QualityIssue(
                key="implausible_downside",
                message=f"Computed downside of {upside:+.0%} implies near-total loss; "
                        "verify share count and earnings.",
                severity=Severity.CRITICAL,
            ))

    if ev_ebitda is not None and ev_ebitda > IMPLAUSIBLE_EV_EBITDA:
        issues.append(QualityIssue(
            key="implausible_multiple",
            message=f"Trading EV/EBITDA of {ev_ebitda:.0f}x suggests the earnings base or "
                    "share count is wrong.",
            severity=Severity.CRITICAL,
        ))

    if terminal_value_pct is not None and terminal_value_pct > 0.90:
        issues.append(QualityIssue(
            key="terminal_dominance",
            message=f"Terminal value is {terminal_value_pct:.0%} of enterprise value; "
                    "the explicit forecast contributes almost nothing.",
            severity=Severity.WARN,
        ))

    # ---- grade -------------------------------------------------------------
    critical = [i for i in issues if i.severity is Severity.CRITICAL]
    warnings = [i for i in issues if i.severity is Severity.WARN]

    if any(i.key in {"implausible_upside", "implausible_downside", "implausible_multiple",
                     "balance_sheet", "no_convergence"} for i in critical):
        grade = DataGrade.UNRELIABLE
    elif critical:
        grade = DataGrade.ILLUSTRATIVE
    elif warnings:
        grade = DataGrade.INDICATIVE
    else:
        grade = DataGrade.INVESTMENT_GRADE

    illustrative = grade in (DataGrade.ILLUSTRATIVE, DataGrade.UNRELIABLE)

    return DataQualityReport(
        grade=grade,
        is_illustrative=illustrative,
        disclosure=ILLUSTRATIVE_NOTICE if illustrative else None,
        issues=issues,
        coverage=coverage,
        history_years=history_years,
        synthetic_sources=sorted(set(synthetic)),
    )
