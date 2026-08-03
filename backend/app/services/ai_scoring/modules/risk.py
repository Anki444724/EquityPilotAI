"""Module 9 — Risk Score (weight 8).

Debt risk, regulatory risk, customer concentration, commodity risk, governance
risk.

**The scale is inverted throughout: 10 means low risk.** This is stated
explicitly because it is the single most likely misreading of the whole engine
— a "risk score of 9" that meant *high* risk would invert the composite, and
the guardrail in `framework.py` caps the recommendation when this module scores
*low*. A test asserts the direction.

Debt and governance are measured from the balance sheet and the shareholding
disclosures. Regulatory, commodity and concentration risk are read from
disclosed evidence: the announcement ledger for regulatory actions, the vault
for concentration and input-cost dependence. Where nothing is disclosed the
factor is MISSING rather than scored as safe — an unmeasured risk is not an
absent one, and a company scoring 10 on commodity risk purely because nobody
extracted its input-cost disclosure would be the most dangerous output this
engine could produce.
"""
from __future__ import annotations

from app.domain.ai_scoring.framework import Module
from app.domain.ai_scoring.types import FactorScore, Origin, band, scale
from app.domain.calc import safe_div
from app.services.ai_scoring.evidence import ScoringEvidence
from app.services.ai_scoring.modules.common import build_module
from app.services.ai_scoring.modules.latest_news import classify

KEY = Module.RISK
SERVICE = "ai_scoring.risk"

#: 10 = LOW risk. Restated here because the direction governs every band below.
SCALE_NOTE = "Scored 10 = low risk."

_CONCENTRATION_KEYWORDS = ("customer concentration", "top ten customers",
                           "top five customers", "single customer",
                           "largest customer", "dependence on", "key customer",
                           "revenue concentration", "client concentration")
_COMMODITY_KEYWORDS = ("raw material", "commodity", "input cost", "crude",
                       "steel price", "coal", "copper", "aluminium",
                       "price volatility", "hedging", "fuel cost",
                       "import dependence")
_GOVERNANCE_KEYWORDS = ("related party", "pledge", "pledged", "auditor",
                        "qualified opinion", "independent director",
                        "board composition", "whistle", "resignation of",
                        "SEBI", "governance")

#: Announcement patterns that indicate an adverse regulatory event, as
#: distinct from routine regulatory correspondence.
_ADVERSE_REGULATORY = ("penalt", "show cause", "fine", "prosecution",
                       "adjudicat", "search and seizure", "notice under",
                       "non-compliance", "suspension", "debarment")


def _missing(key: str, label: str, weight: float, what: str) -> FactorScore:
    return FactorScore(
        key=key, label=label, weight=weight, score=5.0, origin=Origin.MISSING,
        reason=f"Not assessed: {what} {SCALE_NOTE}", computed_by=SERVICE,
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


def score(evidence: ScoringEvidence):
    factors: list[FactorScore] = []
    income = evidence.latest_income
    balance = evidence.latest_balance
    fy = income.fiscal_year if income else None

    # --- debt risk ----------------------------------------------------------
    if balance and income:
        gross_debt = (getattr(balance, "long_term_borrowings", 0.0) or 0.0) \
            + (getattr(balance, "short_term_borrowings", 0.0) or 0.0) \
            + (getattr(balance, "current_maturities_ltd", 0.0) or 0.0)
        equity = getattr(balance, "shareholders_equity", None)
        gearing = safe_div(gross_debt, equity)
        coverage = safe_div(getattr(income, "ebit", None),
                            getattr(income, "finance_costs", None))
        current_ratio = safe_div(
            getattr(balance, "total_current_assets", None),
            getattr(balance, "total_current_liabilities", None),
        )

        components: list[float] = []
        parts: list[str] = []
        if gearing is not None:
            components.append(band(gearing, [(0.0, 10), (0.25, 9), (0.5, 8),
                                             (1.0, 6.5), (1.75, 4.5), (2.5, 2.5)],
                                   higher_is_better=False))
            parts.append(f"debt/equity {gearing:.2f}x")
        if coverage is not None:
            components.append(band(coverage, [(12, 10), (7, 9), (4, 7.5),
                                              (2, 5.5), (1, 3)]))
            parts.append(f"interest cover {coverage:.1f}x")
        if current_ratio is not None:
            components.append(band(current_ratio, [(2.0, 9.5), (1.5, 8.5),
                                                   (1.2, 7), (1.0, 5.5), (0.8, 3.5)]))
            parts.append(f"current ratio {current_ratio:.2f}x")

        if components:
            factors.append(FactorScore(
                key="debt_risk", label="Debt Risk", weight=0.30,
                score=sum(components) / len(components),
                origin=Origin.DERIVED, value=gearing, unit="x debt/equity",
                reason=(
                    f"Balance-sheet risk at FY{fy} on {len(components)} "
                    f"measures: {', '.join(parts)}. {SCALE_NOTE} An "
                    "unlevered company with strong coverage scores near 10; "
                    "one geared above 2.5x with interest cover under 1x "
                    "scores near 2."
                ),
                evidence=f"FY{fy} balance sheet borrowings, equity and current items.",
                citations=(evidence.statement_citation("long_term_borrowings", fy),
                           evidence.statement_citation("shareholders_equity", fy)),
                computed_by=SERVICE,
            ))
        else:
            factors.append(_missing("debt_risk", "Debt Risk", 0.30,
                                    "no leverage measure is computable."))
    else:
        factors.append(_missing("debt_risk", "Debt Risk", 0.30,
                                "no balance sheet is held."))

    # --- regulatory risk ----------------------------------------------------
    regulatory_news = [n for n in evidence.news if "regulatory" in classify(n)]
    adverse = [n for n in evidence.news
               if any(k in n.title.lower() for k in _ADVERSE_REGULATORY)]
    if regulatory_news or adverse:
        factors.append(FactorScore(
            key="regulatory_risk", label="Regulatory Risk", weight=0.18,
            score=band(float(len(adverse)),
                       [(0, 9.0), (1, 7.0), (2, 5.5), (4, 3.5), (6, 2.0)],
                       higher_is_better=False),
            origin=Origin.REPORTED, value=float(len(adverse)),
            unit="adverse events",
            reason=(
                f"{len(adverse)} of {len(regulatory_news)} regulatory "
                "announcements in the last twelve months are adverse — "
                "penalties, show-cause notices, adjudication or "
                "non-compliance. Routine regulatory correspondence is "
                f"excluded from the count. {SCALE_NOTE}"
            ),
            evidence="; ".join(n.title[:110] for n in (adverse or regulatory_news)[:3]),
            citations=tuple(n.citation() for n in (adverse or regulatory_news)[:4]),
            computed_by=SERVICE,
        ))
    else:
        factors.append(_missing(
            "regulatory_risk", "Regulatory Risk", 0.18,
            "no regulatory announcements have been collected. An unmeasured "
            "regulatory exposure is not an absent one, so this is reported "
            "as a gap rather than scored as safe.",
        ))

    # --- customer concentration ---------------------------------------------
    concentration_entries = _keyword_entries(evidence, _CONCENTRATION_KEYWORDS)
    if concentration_entries:
        # Where a numeric share was extracted, score it. Where only a
        # qualitative disclosure exists, score the disclosure conservatively:
        # a company that discusses concentration usually has some.
        numeric = [e for e in concentration_entries if e.value_number is not None]
        if numeric:
            share = max(e.value_number for e in numeric)
            # Extracted shares may be recorded as a fraction or a percentage.
            fraction = share / 100.0 if share > 1.0 else share
            factors.append(FactorScore(
                key="customer_concentration", label="Customer Concentration",
                weight=0.20,
                score=band(fraction, [(0.10, 9.5), (0.20, 8.5), (0.35, 7),
                                      (0.50, 5), (0.70, 3)],
                           higher_is_better=False),
                origin=Origin.EXTRACTED, value=fraction, unit="share of revenue",
                reason=(
                    f"Extracted disclosure puts the largest customer group at "
                    f"{fraction:.0%} of revenue. {SCALE_NOTE}"
                ),
                evidence="; ".join(
                    (e.value_text or e.label)[:120] for e in numeric[:2]
                ),
                citations=tuple(ScoringEvidence.vault_citation(e)
                                for e in numeric[:3]),
                computed_by=SERVICE,
            ))
        else:
            factors.append(FactorScore(
                key="customer_concentration", label="Customer Concentration",
                weight=0.20, score=6.0, origin=Origin.EXTRACTED,
                value=float(len(concentration_entries)), unit="assertions",
                reason=(
                    f"{len(concentration_entries)} assertions discuss customer "
                    "or revenue concentration but none carries a numeric "
                    "share. Scored slightly below neutral: a company that "
                    f"discusses concentration usually has some. {SCALE_NOTE}"
                ),
                evidence="; ".join(
                    (e.value_text or e.label)[:120]
                    for e in concentration_entries[:3]
                ),
                citations=tuple(ScoringEvidence.vault_citation(e)
                                for e in concentration_entries[:3]),
                computed_by=SERVICE,
            ))
    else:
        factors.append(_missing(
            "customer_concentration", "Customer Concentration", 0.20,
            "no customer-concentration disclosure has been extracted.",
        ))

    # --- commodity risk -----------------------------------------------------
    commodity_entries = _keyword_entries(evidence, _COMMODITY_KEYWORDS)
    # Raw-material intensity is a genuine measurement of commodity exposure and
    # is available whenever an income statement is.
    intensity = None
    if income and income.total_revenue:
        raw = (getattr(income, "raw_materials", 0.0) or 0.0) \
            + (getattr(income, "purchase_stock_in_trade", 0.0) or 0.0)
        intensity = raw / income.total_revenue if raw else None

    if intensity is not None:
        factors.append(FactorScore(
            key="commodity_risk", label="Commodity Risk", weight=0.16,
            score=band(intensity, [(0.15, 9.5), (0.30, 8.5), (0.45, 7),
                                   (0.60, 5), (0.75, 3.5)],
                       higher_is_better=False),
            origin=Origin.DERIVED, value=intensity, unit="share of revenue",
            reason=(
                f"Raw materials and traded goods consumed {intensity:.0%} of "
                f"revenue in FY{fy}. Input-cost intensity is the measurable "
                "part of commodity exposure: a business spending 70% of "
                "revenue on inputs transmits every price move to its margin. "
                + (f"{len(commodity_entries)} extracted assertions on input "
                   "costs or hedging corroborate this."
                   if commodity_entries else
                   "No hedging or input-cost commentary has been extracted "
                   "to indicate how the exposure is managed.")
                + f" {SCALE_NOTE}"
            ),
            evidence=f"FY{fy} income statement, raw materials and stock-in-trade.",
            citations=(
                (evidence.statement_citation("raw_materials", fy),)
                + tuple(ScoringEvidence.vault_citation(e)
                        for e in commodity_entries[:2])
            ),
            computed_by=SERVICE,
        ))
    elif commodity_entries:
        factors.append(FactorScore(
            key="commodity_risk", label="Commodity Risk", weight=0.16,
            score=6.0, origin=Origin.EXTRACTED,
            value=float(len(commodity_entries)), unit="assertions",
            reason=(
                f"{len(commodity_entries)} assertions discuss commodity or "
                "input-cost exposure, but the income statement carries no "
                "raw-material line to measure intensity against — common for "
                f"service businesses. {SCALE_NOTE}"
            ),
            evidence="; ".join(
                (e.value_text or e.label)[:120] for e in commodity_entries[:3]
            ),
            citations=tuple(ScoringEvidence.vault_citation(e)
                            for e in commodity_entries[:3]),
            computed_by=SERVICE,
        ))
    else:
        factors.append(_missing(
            "commodity_risk", "Commodity Risk", 0.16,
            "no raw-material line and no input-cost disclosure.",
        ))

    # --- governance risk ----------------------------------------------------
    governance_entries = _keyword_entries(evidence, _GOVERNANCE_KEYWORDS)
    governance_news = [n for n in evidence.news
                       if any(k in n.title.lower()
                              for k in ("resignation", "auditor", "sebi",
                                        "penalt", "related party", "pledge"))]
    if governance_entries or governance_news:
        # Adverse governance events carry more weight than the existence of
        # governance disclosure: every company discloses related-party
        # transactions, and only some have an auditor resign.
        adverse_count = len(governance_news)
        factors.append(FactorScore(
            key="governance_risk", label="Governance Risk", weight=0.16,
            score=band(float(adverse_count),
                       [(0, 8.5), (1, 7.0), (2, 5.5), (4, 3.5), (6, 2.0)],
                       higher_is_better=False),
            origin=Origin.REPORTED if governance_news else Origin.EXTRACTED,
            value=float(adverse_count), unit="events",
            reason=(
                f"{adverse_count} governance-relevant announcements "
                "(auditor changes, resignations, regulatory action, pledge or "
                f"related-party disclosures) and {len(governance_entries)} "
                "extracted governance assertions. Scored on events rather "
                "than on disclosure volume: every company discloses "
                f"related-party transactions; only some lose an auditor. "
                f"{SCALE_NOTE}"
            ),
            evidence="; ".join(
                [n.title[:110] for n in governance_news[:2]]
                + [(e.value_text or e.label)[:110] for e in governance_entries[:2]]
            ),
            citations=(
                tuple(n.citation() for n in governance_news[:3])
                + tuple(ScoringEvidence.vault_citation(e)
                        for e in governance_entries[:2])
            ),
            computed_by=SERVICE,
        ))
    else:
        factors.append(_missing(
            "governance_risk", "Governance Risk", 0.16,
            "no governance disclosures or events have been collected for "
            "this company.",
        ))

    return build_module(KEY, factors)
