"""Module 8 — Growth Score (weight 10).

Revenue CAGR, EPS CAGR, PAT CAGR, market expansion, capacity expansion.

Growth is scored on what was achieved, not on what is forecast. The platform
holds a forecast engine, and it is deliberately not read here: a forecast is an
assumption set, and scoring a company on its own projected growth makes the
score a function of the analyst's optimism rather than of the company.

**Consistency is scored alongside rate.** Three years of 30% growth followed by
two of −10% averages to something respectable and describes a business nobody
should underwrite. Each CAGR factor therefore blends the compound rate with the
share of periods that moved the right way.

**Market and capacity expansion are read from filings, not inferred.** A
company adding a plant announces it; the announcement ledger and the vault both
record it. Inferring capacity expansion from a rise in gross block would be
wrong as often as right — maintenance capex and a new line look identical in
the aggregate.
"""
from __future__ import annotations

from app.domain.ai_scoring.framework import Module
from app.domain.ai_scoring.types import FactorScore, Origin, band, scale
from app.services.ai_scoring.evidence import ScoringEvidence
from app.services.ai_scoring.modules.common import (
    build_module, consistency, series_cagr,
)
from app.services.ai_scoring.modules.latest_news import classify

KEY = Module.GROWTH
SERVICE = "ai_scoring.growth"

_MARKET_KEYWORDS = ("new market", "new geography", "export", "international",
                    "overseas", "new segment", "new product", "launch",
                    "distribution expansion", "new customer", "market entry")
_CAPACITY_KEYWORDS = ("capacity expansion", "capacity addition", "new plant",
                      "new facility", "greenfield", "brownfield",
                      "commissioned", "debottleneck", "capex programme",
                      "capex program", "expansion project", "new line")


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


def _keyword_news(evidence: ScoringEvidence, keywords: tuple[str, ...]):
    lowered = tuple(k.lower() for k in keywords)
    return [n for n in evidence.news
            if any(k in n.title.lower() for k in lowered)]


def _cagr_factor(
    evidence: ScoringEvidence, key: str, label: str, weight: float,
    attribute: str, statement: str, line: str,
    bands: list[tuple[float, float]], what: str,
) -> FactorScore:
    """A CAGR factor blending compound rate with directional consistency."""
    series = evidence.series(statement, attribute, periods=6)
    rate = series_cagr(series)
    fy = evidence.latest_income.fiscal_year if evidence.latest_income else None

    if rate is None:
        present = [v for v in series if v is not None]
        return _missing(
            key, label, weight,
            (f"fewer than two years of {what}." if len(present) < 2 else
             f"the earliest observed {what} is not positive, so a compound "
             "growth rate would carry the wrong sign. Reporting an absolute "
             "value here would show a recovering company as shrinking."),
        )

    rate_score = band(rate, bands)
    steadiness = consistency(series, higher_is_better=True)
    if steadiness is not None:
        # Consistency modulates rather than dominates: it moves the score by
        # up to ±1.5 points around the rate, so a genuinely fast grower is
        # not marked down to mediocrity by one bad year.
        blended = min(10.0, max(0.0, rate_score + (steadiness - 0.5) * 3.0))
    else:
        blended = rate_score

    periods = len([v for v in series if v is not None])
    return FactorScore(
        key=key, label=label, weight=weight, score=blended,
        origin=Origin.DERIVED, value=rate, unit="CAGR",
        reason=(
            f"{what.capitalize()} compounded at {rate:.1%} a year across "
            f"{periods} reported years to FY{fy}"
            + (f", moving upward in {steadiness:.0%} of year-on-year steps. "
               "Consistency adjusts the score by up to 1.5 points: a steady "
               "compounder and a volatile one reaching the same place are not "
               "the same investment."
               if steadiness is not None else ".")
        ),
        evidence=(f"{what.capitalize()} series: "
                  + ", ".join(f"{v:,.2f}" for v in series if v is not None)),
        citations=(evidence.statement_citation(line, fy),),
        computed_by=SERVICE,
    )


def score(evidence: ScoringEvidence):
    factors: list[FactorScore] = []

    # --- revenue CAGR ------------------------------------------------------
    factors.append(_cagr_factor(
        evidence, "revenue_cagr", "Revenue CAGR", 0.26,
        "total_revenue", "income", "total_revenue",
        [(0.22, 10), (0.15, 8.5), (0.10, 7), (0.05, 5.5), (0.0, 4)],
        "revenue",
    ))

    # --- EPS CAGR ----------------------------------------------------------
    factors.append(_cagr_factor(
        evidence, "eps_cagr", "EPS CAGR", 0.22,
        "eps_basic", "income", "eps_basic",
        [(0.20, 10), (0.14, 8.5), (0.09, 7), (0.04, 5.5), (0.0, 4)],
        "earnings per share",
    ))

    # --- PAT CAGR ----------------------------------------------------------
    factors.append(_cagr_factor(
        evidence, "pat_cagr", "PAT CAGR", 0.22,
        "pat", "income", "pat",
        [(0.20, 10), (0.14, 8.5), (0.09, 7), (0.04, 5.5), (0.0, 4)],
        "profit after tax",
    ))

    # --- market expansion ---------------------------------------------------
    market_entries = _keyword_entries(evidence, _MARKET_KEYWORDS)
    market_news = _keyword_news(evidence, _MARKET_KEYWORDS)
    if market_entries or market_news:
        count = len(market_entries) + len(market_news)
        citations = tuple(ScoringEvidence.vault_citation(e)
                          for e in market_entries[:2])
        citations += tuple(n.citation() for n in market_news[:2])
        factors.append(FactorScore(
            key="market_expansion", label="Market Expansion", weight=0.15,
            score=band(float(count), [(6, 9.5), (4, 8.5), (2, 7.5), (1, 6.5)]),
            origin=Origin.EXTRACTED, value=float(count), unit="disclosures",
            reason=(
                f"{len(market_entries)} extracted assertions and "
                f"{len(market_news)} announcements describe entry into new "
                "geographies, segments or product lines. Read from what the "
                "company disclosed, not inferred — a rise in the top line "
                "does not distinguish new markets from more of the old one."
            ),
            evidence="; ".join(
                [(e.value_text or e.label)[:110] for e in market_entries[:2]]
                + [n.title[:110] for n in market_news[:2]]
            ),
            citations=citations,
            computed_by=SERVICE,
        ))
    else:
        factors.append(_missing(
            "market_expansion", "Market Expansion", 0.15,
            "no disclosure of new markets, geographies or product lines has "
            "been extracted or collected.",
        ))

    # --- capacity expansion -------------------------------------------------
    capacity_entries = _keyword_entries(evidence, _CAPACITY_KEYWORDS)
    capacity_news = _keyword_news(evidence, _CAPACITY_KEYWORDS)
    if capacity_entries or capacity_news:
        count = len(capacity_entries) + len(capacity_news)
        citations = tuple(ScoringEvidence.vault_citation(e)
                          for e in capacity_entries[:2])
        citations += tuple(n.citation() for n in capacity_news[:2])
        factors.append(FactorScore(
            key="capacity_expansion", label="Capacity Expansion", weight=0.15,
            score=band(float(count), [(5, 9.5), (3, 8.5), (2, 7.5), (1, 6.5)]),
            origin=Origin.EXTRACTED, value=float(count), unit="disclosures",
            reason=(
                f"{len(capacity_entries)} extracted assertions and "
                f"{len(capacity_news)} announcements report capacity "
                "additions, commissioning or expansion projects. Deliberately "
                "not inferred from gross block: maintenance capex and a new "
                "production line are indistinguishable in the aggregate."
            ),
            evidence="; ".join(
                [(e.value_text or e.label)[:110] for e in capacity_entries[:2]]
                + [n.title[:110] for n in capacity_news[:2]]
            ),
            citations=citations,
            computed_by=SERVICE,
        ))
    else:
        # Capex intensity is reported as context but does not score the
        # factor: it establishes that money was spent, not that capacity grew.
        cash_flow = evidence.latest_cash_flow
        income = evidence.latest_income
        detail = ""
        if cash_flow and income and income.total_revenue:
            intensity = abs(getattr(cash_flow, "capex", 0.0) or 0.0) / income.total_revenue
            detail = (f" Capex ran at {intensity:.1%} of revenue in the latest "
                      "year, which establishes that money was spent but not "
                      "that capacity grew.")
        factors.append(_missing(
            "capacity_expansion", "Capacity Expansion", 0.15,
            "no capacity-addition disclosure has been extracted or "
            "collected." + detail,
        ))

    return build_module(KEY, factors)
