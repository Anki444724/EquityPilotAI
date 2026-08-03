"""Module 1 — Company Data (weight 10).

Business profile, market position, market cap, sector, industry, competitive
landscape.

This module scores what the platform *knows about the company as an entity*,
which is deliberately distinct from what it knows about the company's numbers.
A well-documented mid-cap with a described business model and an identified
competitive set scores higher here than a large-cap the universe import gave a
ticker and nothing else — because an analyst reading the second one cannot form
a view, and the score should say so.

Market cap is scored on scale, not on size-is-good: a large-cap is more liquid
and better disclosed, which is a real advantage to an institutional owner, but
the band tops out rather than rewarding megacaps indefinitely.
"""
from __future__ import annotations

from app.domain.ai_scoring.framework import Module, MODULE_LABELS, MODULE_WEIGHTS
from app.domain.ai_scoring.types import (
    Citation, CitationKind, FactorScore, Origin, aggregate_factors, band,
)
from app.domain.knowledge.vault import VaultSection
from app.services.ai_scoring.modules.common import build_module
from app.services.ai_scoring.evidence import ScoringEvidence

KEY = Module.COMPANY_DATA
SERVICE = "ai_scoring.company_data"

#: Market-cap bands in the company's own reporting scale (₹ crore for Indian
#: listings, $m for US). Expressed as thresholds on ₹ crore and converted for
#: US companies, because a single numeric ladder applied to both currencies
#: would rate every US mid-cap as a megacap.
LARGECAP_CRORE = 75_000.0
MIDCAP_CRORE = 20_000.0
SMALLCAP_CRORE = 5_000.0

#: Approximate INR per USD used only to place a US market cap on the same
#: ladder. A band boundary is not a valuation, so a fixed rate is adequate and
#: a live FX call would make the score non-deterministic for no benefit.
USD_INR = 83.0


def _market_cap_in_crore(evidence: ScoringEvidence) -> float | None:
    company = evidence.company
    if company.market_cap is None:
        return None
    if (company.currency or "INR").upper() == "INR":
        return company.market_cap
    # US statements are stored in millions of USD.
    return company.market_cap * USD_INR / 10.0  # $m -> ₹ crore


def score(evidence: ScoringEvidence):
    company = evidence.company
    factors: list[FactorScore] = []

    # --- business profile -------------------------------------------------
    profile_entries = (
        evidence.vault(VaultSection.COMPANY_PROFILE.value)
        + evidence.vault(VaultSection.BUSINESS_MODEL.value)
    )
    if profile_entries:
        depth = min(len(profile_entries), 6)
        factors.append(FactorScore(
            key="business_profile", label="Business Profile", weight=0.22,
            score=band(float(depth), [(5, 10), (4, 8.5), (3, 7), (2, 6), (1, 5)]),
            origin=Origin.EXTRACTED,
            value=float(len(profile_entries)), unit="assertions",
            reason=(
                f"{len(profile_entries)} extracted assertions describe the "
                "business model and company profile, so the franchise can be "
                "characterised from documents rather than inferred."
            ),
            evidence="; ".join(
                (e.value_text or e.label or e.key)[:120] for e in profile_entries[:3]
            ),
            citations=tuple(ScoringEvidence.vault_citation(e)
                            for e in profile_entries[:4]),
            computed_by=SERVICE,
        ))
    elif company.description:
        factors.append(FactorScore(
            key="business_profile", label="Business Profile", weight=0.22,
            score=5.5, origin=Origin.REFERENCE,
            reason=(
                "Only a reference-data description is held; no filing-derived "
                "business profile has been extracted, so the characterisation "
                "is unverified."
            ),
            evidence=company.description[:200],
            citations=(evidence.reference_citation("description", "present"),),
            computed_by=SERVICE,
        ))
    else:
        factors.append(FactorScore(
            key="business_profile", label="Business Profile", weight=0.22,
            score=5.0, origin=Origin.MISSING,
            reason=("No business description or extracted profile is held, so "
                    "the franchise cannot be characterised."),
            computed_by=SERVICE,
        ))

    # --- market position --------------------------------------------------
    rank = evidence.peers.market_cap_rank
    if rank is not None and evidence.peers.peer_count:
        total = evidence.peers.peer_count + 1
        percentile = 1.0 - (rank - 1) / total
        factors.append(FactorScore(
            key="market_position", label="Market Position", weight=0.20,
            score=band(percentile, [(0.90, 10), (0.75, 8.5), (0.55, 7),
                                    (0.35, 5.5), (0.15, 4)]),
            origin=Origin.DERIVED, value=float(rank), unit="rank",
            reason=(
                f"Ranks {rank} of {total} listed {evidence.peers.sector} "
                f"companies by market capitalisation — the "
                f"{percentile:.0%} percentile of its sector."
            ),
            evidence=(f"Sector cohort of {total} active listings drawn from "
                      "the Nifty 500 universe."),
            citations=(evidence.peers.citation(),),
            computed_by=SERVICE,
        ))
    else:
        factors.append(FactorScore(
            key="market_position", label="Market Position", weight=0.20,
            score=5.0, origin=Origin.MISSING,
            reason=("No sector cohort or market capitalisation is available, "
                    "so relative position cannot be established."),
            computed_by=SERVICE,
        ))

    # --- market cap -------------------------------------------------------
    cap = _market_cap_in_crore(evidence)
    if cap is not None:
        factors.append(FactorScore(
            key="market_cap", label="Market Cap", weight=0.16,
            score=band(cap, [(LARGECAP_CRORE, 9.0), (MIDCAP_CRORE, 7.5),
                             (SMALLCAP_CRORE, 6.0), (1_000.0, 4.5)]),
            origin=Origin.REFERENCE, value=company.market_cap,
            unit=f"{company.currency} {company.reporting_scale}",
            reason=(
                f"Market capitalisation of {company.market_cap:,.0f} "
                f"{company.currency} {company.reporting_scale} places the "
                f"company in the {company.market_cap_category or 'unclassified'} "
                "band. Scale is scored for the liquidity and disclosure it "
                "brings, not treated as quality in itself."
            ),
            evidence=(f"NSE index membership: "
                      f"{company.index_membership or 'not recorded'}."),
            citations=(evidence.reference_citation("market_cap",
                                                   company.market_cap),),
            computed_by=SERVICE,
        ))
    else:
        factors.append(FactorScore(
            key="market_cap", label="Market Cap", weight=0.16,
            score=5.0, origin=Origin.MISSING,
            reason="No market capitalisation is recorded for this company.",
            computed_by=SERVICE,
        ))

    # --- sector -----------------------------------------------------------
    if company.sector:
        factors.append(FactorScore(
            key="sector", label="Sector", weight=0.12,
            score=8.0, origin=Origin.REFERENCE,
            reason=(
                f"Classified to the {company.sector} sector, so peer "
                f"comparison against {evidence.peers.peer_count} listed "
                "companies is available."
            ),
            evidence=f"Sector: {company.sector}.",
            citations=(evidence.reference_citation("sector", company.sector),),
            computed_by=SERVICE,
        ))
    else:
        factors.append(FactorScore(
            key="sector", label="Sector", weight=0.12,
            score=5.0, origin=Origin.MISSING,
            reason=("Unclassified by sector, so no peer set can be assembled "
                    "and every relative measure is unavailable."),
            computed_by=SERVICE,
        ))

    # --- industry ---------------------------------------------------------
    if company.industry:
        factors.append(FactorScore(
            key="industry", label="Industry", weight=0.12,
            score=8.0, origin=Origin.REFERENCE,
            reason=(f"Classified to the {company.industry} industry, a finer "
                    "cut than sector and the level at which demand drivers "
                    "actually operate."),
            evidence=f"Industry: {company.industry}.",
            citations=(evidence.reference_citation("industry", company.industry),),
            computed_by=SERVICE,
        ))
    else:
        factors.append(FactorScore(
            key="industry", label="Industry", weight=0.12,
            score=5.0, origin=Origin.MISSING,
            reason="No industry classification is held.",
            computed_by=SERVICE,
        ))

    # --- competitive landscape --------------------------------------------
    competitors = evidence.vault(VaultSection.COMPETITORS.value)
    if competitors:
        factors.append(FactorScore(
            key="competitive_landscape", label="Competitive Landscape",
            weight=0.18,
            score=band(float(len(competitors)),
                       [(4, 9.5), (3, 8.5), (2, 7.5), (1, 6.5)]),
            origin=Origin.EXTRACTED, value=float(len(competitors)),
            unit="named competitors",
            reason=(
                f"{len(competitors)} competitors have been identified from "
                "filings, so the company's position can be assessed against a "
                "named set rather than against the sector in aggregate."
            ),
            evidence="; ".join(
                (e.value_text or e.label)[:100] for e in competitors[:3]
            ),
            citations=tuple(ScoringEvidence.vault_citation(e)
                            for e in competitors[:4]),
            computed_by=SERVICE,
        ))
    elif evidence.peers.peer_count:
        factors.append(FactorScore(
            key="competitive_landscape", label="Competitive Landscape",
            weight=0.18, score=5.5, origin=Origin.REFERENCE,
            value=float(evidence.peers.peer_count), unit="sector peers",
            reason=(
                f"No competitors have been extracted from filings. A sector "
                f"cohort of {evidence.peers.peer_count} listed companies "
                "stands in, which is a weaker basis: sector membership is not "
                "the same as competing for the same customer."
            ),
            citations=(evidence.peers.citation(),),
            computed_by=SERVICE,
        ))
    else:
        factors.append(FactorScore(
            key="competitive_landscape", label="Competitive Landscape",
            weight=0.18, score=5.0, origin=Origin.MISSING,
            reason=("Neither named competitors nor a sector cohort is "
                    "available, so the competitive landscape is unassessed."),
            computed_by=SERVICE,
        ))

    return build_module(KEY, factors)
