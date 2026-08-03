"""Module 2 — Financial Statements (weight 15).

Revenue growth, EBITDA, PAT, EPS, ROE, ROCE, debt, free cash flow, operating
margin, cash position, capital allocation.

The heaviest module in the framework, and the one where every input is
verifiable. Nothing here is inferred from prose: each factor reads canonical
`financial_facts` through the statements built by Module 2 of the platform, and
each cites the line item and fiscal year it read. If a statement is absent the
factor is MISSING — the engine does not fall back to a screener estimate and
present it as reported.

Two decisions worth stating.

**Debt is scored on net debt to EBITDA, not gross debt.** A company holding
cash equal to its borrowings is not levered, and gross debt would score it as
though it were. Where EBITDA is negative the ratio is meaningless and the
factor scores on interest coverage instead, which is stated in the reason.

**Capital allocation reads the cash flow statement, not the dividend policy.**
Where the money actually went — capex, buybacks, dividends, debt repayment —
is observable; what management says it intends is Module 5's business.
"""
from __future__ import annotations

from app.domain.ai_scoring.framework import Module
from app.domain.ai_scoring.types import FactorScore, Origin, band, scale
from app.domain.calc import safe_div
from app.services.ai_scoring.evidence import ScoringEvidence
from app.services.ai_scoring.modules.common import (
    build_module, consistency, series_cagr,
)

KEY = Module.FINANCIAL_STATEMENTS
SERVICE = "ai_scoring.financial_statements"


def _missing(key: str, label: str, weight: float, what: str) -> FactorScore:
    return FactorScore(
        key=key, label=label, weight=weight, score=5.0, origin=Origin.MISSING,
        reason=f"Not assessed: {what}",
        computed_by=SERVICE,
    )


def score(evidence: ScoringEvidence):
    factors: list[FactorScore] = []
    income = evidence.latest_income
    balance = evidence.latest_balance
    cash_flow = evidence.latest_cash_flow
    fy = income.fiscal_year if income else None

    # --- revenue growth ---------------------------------------------------
    revenues = evidence.series("income", "total_revenue", periods=6)
    growth = series_cagr(revenues)
    if growth is not None:
        periods = len([r for r in revenues if r is not None])
        factors.append(FactorScore(
            key="revenue_growth", label="Revenue Growth", weight=0.13,
            score=band(growth, [(0.20, 10), (0.14, 8.5), (0.09, 7),
                                (0.05, 5.5), (0.0, 4)]),
            origin=Origin.DERIVED, value=growth, unit="CAGR",
            reason=(
                f"Revenue compounded at {growth:.1%} a year across "
                f"{periods} reported years, ending FY{fy}."
            ),
            evidence=(f"Total revenue FY series: "
                      + ", ".join(f"{r:,.0f}" for r in revenues if r is not None)),
            citations=(evidence.statement_citation("total_revenue", fy),),
            computed_by=SERVICE,
        ))
    else:
        factors.append(_missing(
            "revenue_growth", "Revenue Growth", 0.13,
            "fewer than two years of revenue, or a non-positive base year.",
        ))

    # --- EBITDA -----------------------------------------------------------
    if income and income.ebitda_margin is not None:
        factors.append(FactorScore(
            key="ebitda", label="EBITDA", weight=0.11,
            score=band(income.ebitda_margin,
                       [(0.25, 10), (0.18, 8.5), (0.13, 7), (0.08, 5.5), (0.03, 3.5)]),
            origin=Origin.REPORTED, value=income.ebitda_margin, unit="margin",
            reason=(
                f"EBITDA of {income.ebitda:,.0f} on revenue of "
                f"{income.total_revenue:,.0f} is a margin of "
                f"{income.ebitda_margin:.1%} in FY{fy}."
            ),
            evidence=f"FY{fy} income statement, EBITDA line.",
            citations=(evidence.statement_citation("ebitda", fy),),
            computed_by=SERVICE,
        ))
    else:
        factors.append(_missing("ebitda", "EBITDA", 0.11,
                                "no income statement is held."))

    # --- PAT --------------------------------------------------------------
    if income and income.pat_margin is not None:
        pat_series = evidence.series("income", "pat", periods=6)
        profitable_years = sum(1 for p in pat_series if p is not None and p > 0)
        assessed = sum(1 for p in pat_series if p is not None)
        margin_score = band(income.pat_margin,
                            [(0.18, 10), (0.12, 8.5), (0.08, 7),
                             (0.04, 5.5), (0.0, 3.5)])
        # A company profitable in every observed year is materially different
        # from one that happens to be profitable this year, so the record
        # adjusts the margin score rather than being reported separately.
        if assessed >= 3:
            record = profitable_years / assessed
            margin_score = min(10.0, margin_score * (0.85 + 0.15 * record * 2))
        factors.append(FactorScore(
            key="pat", label="PAT", weight=0.11,
            score=min(10.0, margin_score), origin=Origin.REPORTED,
            value=income.pat_margin, unit="margin",
            reason=(
                f"Profit after tax of {income.pat:,.0f} in FY{fy} is a net "
                f"margin of {income.pat_margin:.1%}; the company was "
                f"profitable in {profitable_years} of {assessed} observed years."
            ),
            evidence=f"FY{fy} income statement, PAT line.",
            citations=(evidence.statement_citation("pat", fy),),
            computed_by=SERVICE,
        ))
    else:
        factors.append(_missing("pat", "PAT", 0.11,
                                "no income statement is held."))

    # --- EPS --------------------------------------------------------------
    eps_series = evidence.series("income", "eps_basic", periods=6)
    eps_growth = series_cagr(eps_series)
    if income and income.eps_basic is not None:
        if eps_growth is not None:
            eps_score = band(eps_growth, [(0.18, 10), (0.12, 8.5), (0.07, 7),
                                          (0.02, 5.5), (0.0, 4)])
            reason = (
                f"Basic EPS of {income.eps_basic:,.2f} in FY{fy}, compounding "
                f"at {eps_growth:.1%} a year over the observed series."
            )
        else:
            eps_score = 6.0 if income.eps_basic > 0 else 3.0
            reason = (
                f"Basic EPS of {income.eps_basic:,.2f} in FY{fy}. No growth "
                "rate is computable — the earliest observed EPS is not "
                "positive, so a CAGR would carry the wrong sign."
            )
        factors.append(FactorScore(
            key="eps", label="EPS", weight=0.09, score=eps_score,
            origin=Origin.DERIVED, value=income.eps_basic, unit="per share",
            reason=reason,
            evidence=f"FY{fy} EPS on {income.weighted_shares:,.2f} weighted shares.",
            citations=(evidence.statement_citation("eps_basic", fy),),
            computed_by=SERVICE,
        ))
    else:
        factors.append(_missing("eps", "EPS", 0.09,
                                "no per-share earnings are reported."))

    # --- ROE --------------------------------------------------------------
    roe = safe_div(income.pat if income else None,
                   evidence.avg_balance("shareholders_equity"))
    if roe is not None:
        factors.append(FactorScore(
            key="roe", label="ROE", weight=0.10,
            score=band(roe, [(0.25, 10), (0.18, 8.5), (0.14, 7),
                             (0.10, 5.5), (0.04, 3.5)]),
            origin=Origin.DERIVED, value=roe, unit="%",
            reason=(
                f"Return on average shareholders' equity of {roe:.1%} in "
                f"FY{fy}, computed on the average of opening and closing "
                "equity rather than closing alone."
            ),
            evidence=f"FY{fy} PAT over average shareholders' equity.",
            citations=(evidence.statement_citation("shareholders_equity", fy),),
            computed_by=SERVICE,
        ))
    else:
        factors.append(_missing("roe", "ROE", 0.10,
                                "equity or profit is unavailable."))

    # --- ROCE -------------------------------------------------------------
    roce = safe_div(income.ebit if income else None,
                    evidence.avg_balance("capital_employed"))
    if roce is not None:
        factors.append(FactorScore(
            key="roce", label="ROCE", weight=0.10,
            score=band(roce, [(0.22, 10), (0.16, 8.5), (0.12, 7),
                              (0.08, 5.5), (0.03, 3.5)]),
            origin=Origin.DERIVED, value=roce, unit="%",
            reason=(
                f"Pre-tax return on average capital employed of {roce:.1%} in "
                f"FY{fy}. Read pre-tax so the figure is comparable across "
                "companies on different effective tax rates."
            ),
            evidence=f"FY{fy} EBIT over average capital employed.",
            citations=(evidence.statement_citation("capital_employed", fy),),
            computed_by=SERVICE,
        ))
    else:
        factors.append(_missing("roce", "ROCE", 0.10,
                                "capital employed or EBIT is unavailable."))

    # --- debt -------------------------------------------------------------
    if balance and income:
        gross_debt = (getattr(balance, "long_term_borrowings", 0.0) or 0.0) \
            + (getattr(balance, "short_term_borrowings", 0.0) or 0.0) \
            + (getattr(balance, "current_maturities_ltd", 0.0) or 0.0)
        cash = (getattr(balance, "cash_and_bank", 0.0) or 0.0) \
            + (getattr(balance, "current_investments", 0.0) or 0.0)
        net_debt = gross_debt - cash
        ebitda = income.ebitda

        if ebitda and ebitda > 0:
            ratio = net_debt / ebitda
            factors.append(FactorScore(
                key="debt", label="Debt", weight=0.09,
                score=band(ratio, [(0.0, 10), (0.5, 9), (1.0, 8),
                                   (2.0, 6.5), (3.0, 4.5), (4.0, 2.5)],
                           higher_is_better=False),
                origin=Origin.DERIVED, value=ratio, unit="x net debt/EBITDA",
                reason=(
                    f"Net debt of {net_debt:,.0f} (gross {gross_debt:,.0f} "
                    f"less cash and current investments of {cash:,.0f}) is "
                    f"{ratio:.2f}x EBITDA. Scored net rather than gross: a "
                    "company holding cash against its borrowings is not "
                    "levered."
                ),
                evidence=f"FY{fy} balance sheet borrowings and cash lines.",
                citations=(evidence.statement_citation("long_term_borrowings", fy),
                           evidence.statement_citation("cash_and_bank", fy)),
                computed_by=SERVICE,
            ))
        else:
            coverage = safe_div(income.ebit, getattr(income, "finance_costs", None))
            factors.append(FactorScore(
                key="debt", label="Debt", weight=0.09,
                score=band(coverage, [(8, 9), (5, 7.5), (3, 6), (1.5, 4)])
                if coverage is not None else 3.5,
                origin=Origin.DERIVED if coverage is not None else Origin.REPORTED,
                value=coverage, unit="x interest cover",
                reason=(
                    "EBITDA is not positive, so net debt / EBITDA is "
                    "meaningless. "
                    + (f"Scored on interest coverage of {coverage:.1f}x instead."
                       if coverage is not None
                       else "Interest coverage is also unavailable, so the "
                            "factor is scored conservatively.")
                ),
                evidence=f"FY{fy} EBIT and finance costs.",
                citations=(evidence.statement_citation("finance_costs", fy),),
                computed_by=SERVICE,
            ))
    else:
        factors.append(_missing("debt", "Debt", 0.09,
                                "no balance sheet is held."))

    # --- free cash flow ---------------------------------------------------
    fcf_series = evidence.series("cash_flow", "free_cash_flow", periods=6)
    if cash_flow is not None and income is not None:
        fcf_margin = safe_div(cash_flow.free_cash_flow, income.total_revenue)
        positive = sum(1 for f in fcf_series if f is not None and f > 0)
        assessed = sum(1 for f in fcf_series if f is not None)
        factors.append(FactorScore(
            key="free_cash_flow", label="Free Cash Flow", weight=0.10,
            score=band(fcf_margin, [(0.15, 10), (0.10, 8.5), (0.06, 7),
                                    (0.02, 5.5), (0.0, 4)]),
            origin=Origin.REPORTED, value=fcf_margin, unit="margin",
            reason=(
                f"Free cash flow of {cash_flow.free_cash_flow:,.0f} in FY{fy} "
                + (f"is {fcf_margin:.1%} of revenue" if fcf_margin is not None
                   else "cannot be expressed as a margin")
                + f"; positive in {positive} of {assessed} observed years."
            ),
            evidence=f"FY{fy} cash flow statement, CFO less capex.",
            citations=(evidence.statement_citation("free_cash_flow", fy),),
            computed_by=SERVICE,
        ))
    else:
        factors.append(_missing("free_cash_flow", "Free Cash Flow", 0.10,
                                "no cash flow statement is held."))

    # --- operating margin -------------------------------------------------
    if income and income.ebit_margin is not None:
        margins = evidence.series("income", "ebit_margin", periods=6)
        stability = consistency(margins, higher_is_better=True)
        base = band(income.ebit_margin,
                    [(0.20, 10), (0.14, 8.5), (0.09, 7), (0.05, 5.5), (0.0, 3.5)])
        factors.append(FactorScore(
            key="operating_margin", label="Operating Margin", weight=0.09,
            score=base, origin=Origin.REPORTED,
            value=income.ebit_margin, unit="margin",
            reason=(
                f"Operating (EBIT) margin of {income.ebit_margin:.1%} in "
                f"FY{fy}"
                + (f"; margins moved upward in {stability:.0%} of observed "
                   "year-on-year steps." if stability is not None else ".")
            ),
            evidence=f"FY{fy} income statement, EBIT over total revenue.",
            citations=(evidence.statement_citation("ebit", fy),),
            computed_by=SERVICE,
        ))
    else:
        factors.append(_missing("operating_margin", "Operating Margin", 0.09,
                                "no operating margin is computable."))

    # --- cash position ----------------------------------------------------
    if balance:
        cash = (getattr(balance, "cash_and_bank", 0.0) or 0.0) \
            + (getattr(balance, "current_investments", 0.0) or 0.0)
        current_liabilities = getattr(balance, "total_current_liabilities", None)
        cover = safe_div(cash, current_liabilities)
        factors.append(FactorScore(
            key="cash_position", label="Cash Position", weight=0.04,
            score=band(cover, [(0.60, 10), (0.35, 8.5), (0.20, 7),
                               (0.10, 5.5), (0.03, 4)]),
            origin=Origin.REPORTED, value=cover, unit="x current liabilities",
            reason=(
                f"Cash and current investments of {cash:,.0f} cover "
                + (f"{cover:.2f}x current liabilities" if cover is not None
                   else "an unknown share of current liabilities")
                + f" at FY{fy} close."
            ),
            evidence=f"FY{fy} balance sheet, cash and current investments.",
            citations=(evidence.statement_citation("cash_and_bank", fy),),
            computed_by=SERVICE,
        ))
    else:
        factors.append(_missing("cash_position", "Cash Position", 0.04,
                                "no balance sheet is held."))

    # --- capital allocation -----------------------------------------------
    if cash_flow is not None:
        cfo = getattr(cash_flow, "cfo", None)
        capex = abs(getattr(cash_flow, "capex", 0.0) or 0.0)
        dividends = abs(getattr(cash_flow, "dividend_paid", 0.0) or 0.0)
        repayment = abs(getattr(cash_flow, "repayment_borrowings", 0.0) or 0.0)
        deployed = capex + dividends + repayment
        # Scored on whether operations funded the deployment. A company
        # reinvesting more than it generates is not automatically wrong, but
        # it is funding growth from the balance sheet and the score should
        # register the dependency.
        ratio = safe_div(deployed, cfo) if cfo and cfo > 0 else None
        if ratio is not None:
            allocation_score = band(
                ratio, [(0.60, 9.0), (0.85, 8.0), (1.00, 7.0), (1.30, 5.5),
                        (1.80, 4.0)], higher_is_better=False,
            )
            reason = (
                f"FY{fy} deployed {deployed:,.0f} across capex "
                f"({capex:,.0f}), dividends ({dividends:,.0f}) and debt "
                f"repayment ({repayment:,.0f}), against operating cash flow "
                f"of {cfo:,.0f} — {ratio:.2f}x. "
                + ("Self-funded." if ratio <= 1.0 else
                   "Deployment exceeded operating cash flow, so the balance "
                   "sheet funded the difference.")
            )
        else:
            allocation_score = 3.5
            reason = (
                f"FY{fy} operating cash flow is not positive, so capital "
                "deployment was necessarily funded from the balance sheet or "
                "from external capital."
            )
        factors.append(FactorScore(
            key="capital_allocation", label="Capital Allocation", weight=0.04,
            score=allocation_score, origin=Origin.REPORTED,
            value=ratio, unit="x CFO",
            reason=reason,
            evidence=f"FY{fy} cash flow statement, investing and financing sections.",
            citations=(evidence.statement_citation("cfo", fy),
                       evidence.statement_citation("capex", fy)),
            computed_by=SERVICE,
        ))
    else:
        factors.append(_missing("capital_allocation", "Capital Allocation", 0.04,
                                "no cash flow statement is held."))

    return build_module(KEY, factors)
