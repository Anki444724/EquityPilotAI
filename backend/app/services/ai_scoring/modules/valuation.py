"""Module 10 — Valuation Score (weight 7).

PE, PB, EV/EBITDA, DCF, relative valuation, margin of safety.

**10 means cheap.** Like the risk module the direction is inverted relative to
intuition, and it is stated on every factor: a company on 90x earnings scores
low here however good the business, and the guardrail in `framework.py` caps
the recommendation at Hold when it does.

The DCF and margin-of-safety factors read the platform's existing valuation
engine rather than re-deriving intrinsic value. That engine already applies the
Module 4 constraint the brief set out — where the underlying data is synthetic
or incomplete it flags the result illustrative — and this module propagates
that flag as a warning rather than swallowing it. A margin of safety computed
from illustrative financials is not a margin of safety, and the panel must say
so.

Absolute multiples are scored against fixed bands and relative multiples
against the sector cohort. Both are shown because they disagree in the cases
that matter most: a company on 40x in a sector trading at 60x is expensive in
absolute terms and cheap relative to its peers, and collapsing that into one
number destroys the only interesting thing about it.
"""
from __future__ import annotations

from app.domain.ai_scoring.framework import Module
from app.domain.ai_scoring.types import (
    Citation, CitationKind, FactorScore, Origin, band, scale,
)
from app.services.ai_scoring.evidence import ScoringEvidence
from app.services.ai_scoring.modules.common import build_module

KEY = Module.VALUATION
SERVICE = "ai_scoring.valuation"

#: 10 = cheap. Restated on every factor because the direction is inverted.
SCALE_NOTE = "Scored 10 = cheap."


def _missing(key: str, label: str, weight: float, what: str) -> FactorScore:
    return FactorScore(
        key=key, label=label, weight=weight, score=5.0, origin=Origin.MISSING,
        reason=f"Not assessed: {what} {SCALE_NOTE}", computed_by=SERVICE,
    )


def score(evidence: ScoringEvidence, *, sector_stats: dict | None = None):
    factors: list[FactorScore] = []
    stats = sector_stats or {}
    company = evidence.company
    valuation_citation = Citation(
        kind=CitationKind.STATEMENT,
        label="Valuation engine output (DCF, relative and comparables)",
        reference=f"valuation:{company.id}",
    )

    # --- PE ------------------------------------------------------------------
    if evidence.pe_ratio is not None and evidence.pe_ratio > 0:
        factors.append(FactorScore(
            key="pe", label="PE", weight=0.20,
            score=band(evidence.pe_ratio,
                       [(10, 10), (15, 8.5), (22, 7), (32, 5), (45, 3), (65, 1.5)],
                       higher_is_better=False),
            origin=Origin.DERIVED, value=evidence.pe_ratio, unit="x",
            reason=(
                f"Trades on {evidence.pe_ratio:.1f}x trailing earnings at a "
                f"price of {company.current_price:,.2f}. {SCALE_NOTE}"
            ),
            evidence="Current price over trailing basic EPS.",
            citations=(valuation_citation,),
            computed_by=SERVICE,
        ))
    else:
        factors.append(_missing(
            "pe", "PE", 0.20,
            ("earnings are not positive, so a price/earnings multiple is "
             "undefined. A negative PE is not a cheap one."
             if evidence.pe_ratio is not None else
             "no price/earnings multiple is available."),
        ))

    # --- PB ------------------------------------------------------------------
    if evidence.pb_ratio is not None and evidence.pb_ratio > 0:
        factors.append(FactorScore(
            key="pb", label="PB", weight=0.14,
            score=band(evidence.pb_ratio,
                       [(1.0, 10), (1.8, 8.5), (3.0, 7), (5.0, 5), (8.0, 3)],
                       higher_is_better=False),
            origin=Origin.DERIVED, value=evidence.pb_ratio, unit="x",
            reason=(
                f"Trades on {evidence.pb_ratio:.2f}x book value. Read with "
                "return on equity: a high multiple of book is justified where "
                f"the book compounds quickly. {SCALE_NOTE}"
            ),
            evidence="Current price over book value per share.",
            citations=(valuation_citation,),
            computed_by=SERVICE,
        ))
    else:
        factors.append(_missing("pb", "PB", 0.14,
                                "book value per share is not positive or not "
                                "available."))

    # --- EV/EBITDA -----------------------------------------------------------
    if evidence.ev_ebitda is not None and evidence.ev_ebitda > 0:
        factors.append(FactorScore(
            key="ev_ebitda", label="EV/EBITDA", weight=0.16,
            score=band(evidence.ev_ebitda,
                       [(6, 10), (9, 8.5), (13, 7), (18, 5), (26, 3)],
                       higher_is_better=False),
            origin=Origin.DERIVED, value=evidence.ev_ebitda, unit="x",
            reason=(
                f"Enterprise value is {evidence.ev_ebitda:.1f}x EBITDA. "
                "Capital-structure neutral, so it compares a levered and an "
                f"unlevered company on the same basis. {SCALE_NOTE}"
            ),
            evidence="Enterprise value over trailing EBITDA.",
            citations=(valuation_citation,),
            computed_by=SERVICE,
        ))
    else:
        factors.append(_missing("ev_ebitda", "EV/EBITDA", 0.16,
                                "EBITDA is not positive or enterprise value "
                                "is unavailable."))

    # --- DCF ------------------------------------------------------------------
    if evidence.upside is not None:
        illustrative = evidence.valuation_is_illustrative
        factors.append(FactorScore(
            key="dcf", label="DCF", weight=0.22,
            score=band(evidence.upside,
                       [(0.50, 10), (0.25, 8.5), (0.10, 7), (0.0, 5.5),
                        (-0.15, 4), (-0.35, 2)]),
            # An illustrative valuation is not a reported one, and marking it
            # DERIVED would let synthetic inputs contribute full coverage.
            origin=Origin.EXTRACTED if illustrative else Origin.DERIVED,
            value=evidence.upside, unit="upside to intrinsic value",
            reason=(
                f"Discounted cash flow puts intrinsic value at "
                f"{evidence.intrinsic_value:,.2f} against a market price of "
                f"{company.current_price:,.2f} — "
                f"{evidence.upside:+.1%}. "
                + ("**Illustrative valuation only. Real filings are required "
                   "for investment-grade outputs.** The underlying financials "
                   "are synthetic or incomplete, so this figure indicates "
                   "method rather than value."
                   if illustrative else
                   f"Discounted at a WACC of {evidence.wacc:.1%}."
                   if evidence.wacc else "")
                + f" {SCALE_NOTE}"
            ),
            evidence=(evidence.valuation_disclosure
                      or "Two-stage DCF from the platform valuation engine."),
            citations=(valuation_citation,),
            computed_by=SERVICE,
        ))
    else:
        factors.append(_missing(
            "dcf", "DCF", 0.22,
            "the valuation engine could not produce an intrinsic value — "
            "typically because the forecast requires history the company "
            "does not have.",
        ))

    # --- relative valuation ---------------------------------------------------
    peer_pe = stats.get("median_pe")
    peer_count = int(stats.get("peer_count") or 0)
    if peer_pe and evidence.pe_ratio and evidence.pe_ratio > 0 and peer_count >= 4:
        premium = evidence.pe_ratio / peer_pe - 1.0
        factors.append(FactorScore(
            key="relative_valuation", label="Relative Valuation", weight=0.16,
            score=band(premium,
                       [(-0.30, 10), (-0.15, 8.5), (0.0, 7), (0.20, 5.5),
                        (0.50, 3.5), (1.00, 2)],
                       higher_is_better=False),
            origin=Origin.DERIVED, value=premium, unit="premium to peer median",
            reason=(
                f"On {evidence.pe_ratio:.1f}x against a median of "
                f"{peer_pe:.1f}x across {peer_count} listed "
                f"{company.sector} peers — a {premium:+.0%} "
                f"{'premium' if premium >= 0 else 'discount'}. Shown "
                "alongside the absolute multiple because the two disagree "
                "precisely where it matters: an expensive stock in an "
                f"expensive sector is both. {SCALE_NOTE}"
            ),
            evidence=f"Sector median PE across {peer_count} active listings.",
            citations=(evidence.peers.citation(), valuation_citation),
            computed_by=SERVICE,
        ))
    else:
        factors.append(_missing(
            "relative_valuation", "Relative Valuation", 0.16,
            (f"only {peer_count} sector peers carry a usable multiple, which "
             "is too few for a median to describe the sector."
             if company.sector else
             "the company is not classified to a sector."),
        ))

    # --- margin of safety -----------------------------------------------------
    if evidence.margin_of_safety is not None:
        illustrative = evidence.valuation_is_illustrative
        factors.append(FactorScore(
            key="margin_of_safety", label="Margin of Safety", weight=0.12,
            score=band(evidence.margin_of_safety,
                       [(0.35, 10), (0.20, 8.5), (0.10, 7), (0.0, 5.5),
                        (-0.10, 3.5)]),
            origin=Origin.EXTRACTED if illustrative else Origin.DERIVED,
            value=evidence.margin_of_safety, unit="discount to intrinsic value",
            reason=(
                f"The market price sits "
                f"{evidence.margin_of_safety:+.1%} relative to intrinsic "
                "value — the cushion available if the valuation assumptions "
                "prove optimistic. "
                + ("Computed from illustrative financials, so it is not a "
                   "margin of safety in any usable sense."
                   if illustrative else "")
                + f" {SCALE_NOTE}"
            ),
            evidence="Valuation engine summary, margin of safety.",
            citations=(valuation_citation,),
            computed_by=SERVICE,
        ))
    else:
        factors.append(_missing(
            "margin_of_safety", "Margin of Safety", 0.12,
            "no intrinsic value is available, so no cushion can be measured.",
        ))

    return build_module(KEY, factors)
