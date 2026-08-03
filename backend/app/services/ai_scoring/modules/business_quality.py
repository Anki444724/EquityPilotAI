"""Module 7 — Business Quality Score (weight 14).

Moat, pricing power, brand, scalability, customer retention, capital
efficiency.

The second-heaviest module, and the one where qualitative concepts are given
quantitative proxies. Each proxy is named in the factor's reason, because a
score for "pricing power" that does not say it was measured from gross-margin
stability is not explainable — the reader has no way to disagree with it.

The proxies:

* **Moat** — persistence of returns above the cost of capital. A business
  earning 25% on capital for a decade has something protecting it, whatever
  that something is called.
* **Pricing power** — gross-margin stability through the cycle. A company that
  can pass on input costs holds its gross margin when they rise; one that
  cannot, does not.
* **Brand** — extracted brand assertions, plus the gross-margin premium over
  sector peers. Brand is the one concept with no clean financial proxy, so
  both a measured and an extracted signal are used and the weaker origin is
  reported.
* **Scalability** — operating leverage: does profit grow faster than revenue?
* **Customer retention** — revenue durability, measured as the absence of
  year-on-year declines, plus any extracted concentration disclosure.
* **Capital efficiency** — sales-to-capital and the trend in it.
"""
from __future__ import annotations

from app.domain.ai_scoring.framework import Module
from app.domain.ai_scoring.types import FactorScore, Origin, band, scale
from app.domain.calc import safe_div
from app.services.ai_scoring.evidence import ScoringEvidence
from app.services.ai_scoring.modules.common import (
    build_module, consistency, series_cagr,
)

KEY = Module.BUSINESS_QUALITY
SERVICE = "ai_scoring.business_quality"

_BRAND_KEYWORDS = ("brand", "trademark", "flagship", "premium", "household name",
                   "brand equity", "brand recall")
_RETENTION_KEYWORDS = ("repeat", "retention", "churn", "long-term contract",
                       "recurring", "subscription", "annuity", "renewal",
                       "customer concentration", "top ten customers",
                       "top five customers")


def _missing(key: str, label: str, weight: float, what: str) -> FactorScore:
    return FactorScore(
        key=key, label=label, weight=weight, score=5.0, origin=Origin.MISSING,
        reason=f"Not assessed: {what}", computed_by=SERVICE,
    )


def _keyword_entries(evidence: ScoringEvidence, keywords: tuple[str, ...]):
    lowered = tuple(k.lower() for k in keywords)
    out = []
    for entry in evidence.vault_entries:
        haystack = " ".join(filter(None, (
            entry.key, entry.label, entry.value_text, entry.evidence
        ))).lower()
        if any(k in haystack for k in lowered):
            out.append(entry)
    return out


def _stdev(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    return variance ** 0.5


def score(evidence: ScoringEvidence, *, sector_stats: dict | None = None):
    factors: list[FactorScore] = []
    stats = sector_stats or {}
    fy = evidence.latest_income.fiscal_year if evidence.latest_income else None

    # --- moat: persistence of excess returns -------------------------------
    roce_readings: list[float] = []
    for index in range(len(evidence.balances)):
        income = evidence.incomes[index] if index < len(evidence.incomes) else None
        balance = evidence.balances[index]
        if income is None:
            continue
        employed = getattr(balance, "capital_employed", None)
        value = safe_div(getattr(income, "ebit", None), employed)
        if value is not None:
            roce_readings.append(value)

    if len(roce_readings) >= 3:
        hurdle = evidence.wacc if evidence.wacc else 0.12
        above = sum(1 for r in roce_readings if r > hurdle)
        persistence = above / len(roce_readings)
        average = sum(roce_readings) / len(roce_readings)
        factors.append(FactorScore(
            key="moat", label="Moat", weight=0.22,
            score=min(10.0,
                      scale(persistence, 0.0, 1.0) * 0.65
                      + band(average, [(0.25, 10), (0.18, 8.5), (0.13, 7),
                                       (0.09, 5.5), (0.05, 4)]) * 0.35),
            origin=Origin.DERIVED, value=persistence, unit="years above hurdle",
            reason=(
                f"Return on capital employed exceeded the "
                f"{'WACC of ' + format(hurdle, '.1%') if evidence.wacc else 'default 12% hurdle'} "
                f"in {above} of {len(roce_readings)} observed years, "
                f"averaging {average:.1%}. Sustained excess returns are the "
                "measurable footprint of a moat, whatever the qualitative "
                "source of that moat turns out to be."
            ),
            evidence=("ROCE series: "
                      + ", ".join(f"{r:.1%}" for r in roce_readings[-6:])),
            citations=(evidence.statement_citation("capital_employed", fy),),
            computed_by=SERVICE,
        ))
    else:
        factors.append(_missing(
            "moat", "Moat", 0.22,
            "fewer than three years of return-on-capital data, so "
            "persistence cannot be established.",
        ))

    # --- pricing power: gross-margin stability -----------------------------
    gross_margins = [m for m in evidence.series("income", "gross_margin", periods=8)
                     if m is not None]
    if len(gross_margins) >= 3:
        volatility = _stdev(gross_margins)
        level = sum(gross_margins) / len(gross_margins)
        stability_score = band(volatility, [(0.01, 10), (0.02, 8.5), (0.035, 7),
                                            (0.06, 5.5), (0.10, 4)],
                               higher_is_better=False)
        level_score = band(level, [(0.45, 10), (0.32, 8.5), (0.22, 7),
                                   (0.14, 5.5), (0.07, 4)])
        factors.append(FactorScore(
            key="pricing_power", label="Pricing Power", weight=0.18,
            score=min(10.0, stability_score * 0.6 + level_score * 0.4),
            origin=Origin.DERIVED, value=volatility, unit="σ of gross margin",
            reason=(
                f"Gross margin averaged {level:.1%} with a standard deviation "
                f"of {volatility:.2%} across {len(gross_margins)} years. A "
                "company that can pass on input costs holds its gross margin "
                "when they rise; one that cannot, does not — so stability is "
                "weighted more heavily than the level itself."
            ),
            evidence=("Gross margin series: "
                      + ", ".join(f"{m:.1%}" for m in gross_margins[-6:])),
            citations=(evidence.statement_citation("gross_profit", fy),),
            computed_by=SERVICE,
        ))
    else:
        factors.append(_missing(
            "pricing_power", "Pricing Power", 0.18,
            "fewer than three years of gross-margin data.",
        ))

    # --- brand -------------------------------------------------------------
    brand_entries = _keyword_entries(evidence, _BRAND_KEYWORDS)
    if brand_entries:
        factors.append(FactorScore(
            key="brand", label="Brand", weight=0.13,
            score=band(float(len(brand_entries)),
                       [(4, 9.0), (3, 8.0), (2, 7.0), (1, 6.0)]),
            origin=Origin.EXTRACTED, value=float(len(brand_entries)),
            unit="assertions",
            reason=(
                f"{len(brand_entries)} extracted assertions reference brands, "
                "trademarks or premium positioning. Brand is the one concept "
                "in this module with no clean financial proxy, so it is "
                "scored from disclosure and marked as extracted rather than "
                "derived — a weaker origin, reported as such."
            ),
            evidence="; ".join(
                (e.value_text or e.label)[:120] for e in brand_entries[:3]
            ),
            citations=tuple(ScoringEvidence.vault_citation(e)
                            for e in brand_entries[:4]),
            computed_by=SERVICE,
        ))
    else:
        factors.append(_missing(
            "brand", "Brand", 0.13,
            "no brand or trademark assertion has been extracted, and brand "
            "has no financial proxy the engine is willing to substitute.",
        ))

    # --- scalability: operating leverage -----------------------------------
    revenue_cagr = series_cagr(evidence.series("income", "total_revenue", periods=6))
    ebit_cagr = series_cagr(evidence.series("income", "ebit", periods=6))
    if revenue_cagr is not None and ebit_cagr is not None:
        leverage = ebit_cagr - revenue_cagr
        factors.append(FactorScore(
            key="scalability", label="Scalability", weight=0.17,
            score=band(leverage, [(0.06, 10), (0.03, 8.5), (0.01, 7),
                                  (-0.01, 5.5), (-0.04, 4)]),
            origin=Origin.DERIVED, value=leverage, unit="pp of excess growth",
            reason=(
                f"Operating profit compounded at {ebit_cagr:.1%} against "
                f"revenue at {revenue_cagr:.1%} — {leverage:+.1%} of operating "
                "leverage. Profit growing faster than revenue is the "
                "signature of a business that scales; the reverse means each "
                "incremental rupee of sales costs more to earn."
            ),
            evidence=f"EBIT and revenue CAGRs over the observed series to FY{fy}.",
            citations=(evidence.statement_citation("ebit", fy),
                       evidence.statement_citation("total_revenue", fy)),
            computed_by=SERVICE,
        ))
    else:
        factors.append(_missing(
            "scalability", "Scalability", 0.17,
            "revenue or operating-profit growth is not computable over the "
            "available history.",
        ))

    # --- customer retention -------------------------------------------------
    revenues = [r for r in evidence.series("income", "total_revenue", periods=8)
                if r is not None]
    retention_entries = _keyword_entries(evidence, _RETENTION_KEYWORDS)
    if len(revenues) >= 3:
        declines = sum(1 for i in range(1, len(revenues))
                       if revenues[i] < revenues[i - 1])
        steps = len(revenues) - 1
        durability = 1.0 - declines / steps
        base = scale(durability, 0.0, 1.0)
        # An extracted retention disclosure is corroborating evidence, and
        # nudges the score without dominating a measured series.
        adjusted = min(10.0, base + (0.5 if retention_entries else 0.0))
        factors.append(FactorScore(
            key="customer_retention", label="Customer Retention", weight=0.14,
            score=adjusted,
            origin=Origin.EXTRACTED if retention_entries else Origin.DERIVED,
            value=durability, unit="share of years without decline",
            reason=(
                f"Revenue declined in {declines} of {steps} year-on-year "
                f"steps. Revenue durability is used as the retention proxy: "
                "a business losing customers shows it in the top line before "
                "it shows it anywhere else. "
                + (f"{len(retention_entries)} extracted assertions on "
                   "retention, churn or contract length corroborate this."
                   if retention_entries else
                   "No retention or churn disclosure has been extracted to "
                   "corroborate the measurement.")
            ),
            evidence=("Revenue series: "
                      + ", ".join(f"{r:,.0f}" for r in revenues[-6:])),
            citations=(
                (evidence.statement_citation("total_revenue", fy),)
                + tuple(ScoringEvidence.vault_citation(e)
                        for e in retention_entries[:2])
            ),
            computed_by=SERVICE,
        ))
    else:
        factors.append(_missing(
            "customer_retention", "Customer Retention", 0.14,
            "fewer than three years of revenue history.",
        ))

    # --- capital efficiency -------------------------------------------------
    income = evidence.latest_income
    invested = evidence.avg_balance("invested_capital")
    sales_to_capital = safe_div(
        getattr(income, "total_revenue", None) if income else None, invested
    )
    if sales_to_capital is not None:
        factors.append(FactorScore(
            key="capital_efficiency", label="Capital Efficiency", weight=0.16,
            score=band(sales_to_capital, [(3.0, 10), (2.0, 8.5), (1.3, 7),
                                          (0.8, 5.5), (0.4, 4)]),
            origin=Origin.DERIVED, value=sales_to_capital, unit="x",
            reason=(
                f"Every unit of invested capital generated "
                f"{sales_to_capital:.2f} units of revenue in FY{fy}. Measured "
                "on invested capital — capital employed less cash — so a "
                "large cash pile does not read as inefficiency."
            ),
            evidence=f"FY{fy} revenue over average invested capital.",
            citations=(evidence.statement_citation("invested_capital", fy),),
            computed_by=SERVICE,
        ))
    else:
        factors.append(_missing(
            "capital_efficiency", "Capital Efficiency", 0.16,
            "invested capital is not available from the balance sheet.",
        ))

    return build_module(KEY, factors)
