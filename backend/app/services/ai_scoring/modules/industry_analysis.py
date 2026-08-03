"""Module 4 — Industry Analysis (weight 8).

Industry growth, competition, market size, government policy, demand outlook.

This module is the one where the platform's evidence is thinnest, and the
design reflects that honestly rather than papering over it.

**Industry growth is measured cross-sectionally from the platform's own
universe**, not taken from a research note. The median revenue growth of the
company's sector peers is computed from the same canonical statements that
score the company itself, so the comparison is like-for-like. That is a real
measurement — it is what the listed constituents of the sector actually did —
and it is cited as such.

**Competition is scored from sector concentration**, again measured: how much
of the sector's market capitalisation sits with its largest members. A sector
where three companies hold most of the value competes differently from one
with forty comparable participants.

**Market size, government policy and demand outlook have no structured source.**
Where the knowledge vault has extracted assertions they are used and cited;
where it has not, the factor is MISSING. Inventing a policy view from a sector
name would be the exact failure mode the brief forbids.
"""
from __future__ import annotations

from statistics import median

from sqlalchemy import select

from app.domain.ai_scoring.framework import Module
from app.domain.ai_scoring.types import (
    Citation, CitationKind, FactorScore, Origin, band,
)
from app.domain.knowledge.vault import VaultSection
from app.services.ai_scoring.evidence import ScoringEvidence
from app.services.ai_scoring.modules.common import build_module

KEY = Module.INDUSTRY_ANALYSIS
SERVICE = "ai_scoring.industry_analysis"

#: Minimum peers before a sector aggregate is worth citing. Below this the
#: "median" is one or two companies and describes them, not the sector.
MIN_PEERS_FOR_AGGREGATE = 4

#: Vault sections that can carry industry-level assertions.
_POLICY_KEYWORDS = ("policy", "regulat", "government", "subsid", "tariff",
                    "incentive", "PLI", "licence", "license")
_DEMAND_KEYWORDS = ("demand", "outlook", "guidance", "order book", "volume",
                    "capacity utilis", "capacity utiliz")
_MARKET_KEYWORDS = ("market size", "addressable", "TAM", "market share",
                    "industry size", "penetration")


def _missing(key: str, label: str, weight: float, what: str) -> FactorScore:
    return FactorScore(
        key=key, label=label, weight=weight, score=5.0, origin=Origin.MISSING,
        reason=f"Not assessed: {what}", computed_by=SERVICE,
    )


def _vault_matches(evidence: ScoringEvidence, keywords: tuple[str, ...]):
    """Vault entries whose label, key or text mentions any keyword."""
    lowered = tuple(k.lower() for k in keywords)
    out = []
    for entry in evidence.vault_entries:
        haystack = " ".join(filter(None, (
            entry.key, entry.label, entry.value_text, entry.evidence
        ))).lower()
        if any(k in haystack for k in lowered):
            out.append(entry)
    return out


def score(evidence: ScoringEvidence, *, sector_stats: dict | None = None):
    """Score the industry module.

    ``sector_stats`` is injected by the orchestrator, which computes it once
    per sector and caches it. Computing it here would issue the same aggregate
    query for every company in a batch scoring run — 500 companies producing
    500 identical sector scans.
    """
    factors: list[FactorScore] = []
    company = evidence.company
    stats = sector_stats or {}
    peer_count = int(stats.get("peer_count") or 0)
    sector_citation = Citation(
        kind=CitationKind.PEER,
        label=(f"Sector aggregate computed from canonical statements: "
               f"{company.sector or 'unclassified'} ({peer_count} peers)"),
        reference=f"sector_stats:{company.sector}",
    )

    # --- industry growth --------------------------------------------------
    growth = stats.get("median_revenue_growth")
    if growth is not None and peer_count >= MIN_PEERS_FOR_AGGREGATE:
        factors.append(FactorScore(
            key="industry_growth", label="Industry growth", weight=0.28,
            score=band(growth, [(0.18, 10), (0.12, 8.5), (0.08, 7),
                                (0.04, 5.5), (0.0, 4)]),
            origin=Origin.DERIVED, value=growth, unit="median peer CAGR",
            reason=(
                f"The median listed {company.sector} company compounded "
                f"revenue at {growth:.1%} a year across the observed series. "
                f"Measured from the canonical statements of {peer_count} "
                "peers rather than taken from a third-party forecast."
            ),
            evidence=(f"Median of per-company revenue CAGRs across "
                      f"{peer_count} active {company.sector} listings."),
            citations=(sector_citation,),
            computed_by=SERVICE,
        ))
    else:
        factors.append(_missing(
            "industry_growth", "Industry growth", 0.28,
            (f"only {peer_count} sector peers have sufficient financial "
             f"history; at least {MIN_PEERS_FOR_AGGREGATE} are required "
             "before a median describes the sector rather than its "
             "constituents.")
            if company.sector else
            "the company is not classified to a sector.",
        ))

    # --- competition ------------------------------------------------------
    concentration = stats.get("top3_market_cap_share")
    if concentration is not None and peer_count >= MIN_PEERS_FOR_AGGREGATE:
        # A concentrated sector is scored favourably: pricing power and
        # rational competition are more available where three participants
        # hold the value than where forty do. This is a statement about the
        # structure, not about this company's position within it — Module 1
        # scores that separately.
        factors.append(FactorScore(
            key="competition", label="Competition", weight=0.24,
            score=band(concentration, [(0.75, 9.0), (0.60, 8.0), (0.45, 7.0),
                                       (0.30, 5.5), (0.15, 4.5)]),
            origin=Origin.DERIVED, value=concentration,
            unit="top-3 share of sector market cap",
            reason=(
                f"The three largest {company.sector} listings hold "
                f"{concentration:.0%} of the sector's market capitalisation "
                f"across {peer_count + 1} companies. A concentrated structure "
                "supports more rational competition; a fragmented one erodes "
                "pricing. This describes the sector, not the company's place "
                "in it."
            ),
            evidence=(f"Market-capitalisation concentration across "
                      f"{peer_count + 1} active listings."),
            citations=(sector_citation,),
            computed_by=SERVICE,
        ))
    else:
        factors.append(_missing(
            "competition", "Competition", 0.24,
            "the sector cohort is too small to measure concentration.",
        ))

    # --- market size ------------------------------------------------------
    market_entries = _vault_matches(evidence, _MARKET_KEYWORDS)
    if market_entries:
        factors.append(FactorScore(
            key="market_size", label="Market size", weight=0.16,
            score=band(float(len(market_entries)),
                       [(3, 8.5), (2, 7.5), (1, 6.5)]),
            origin=Origin.EXTRACTED, value=float(len(market_entries)),
            unit="assertions",
            reason=(
                f"{len(market_entries)} extracted assertions quantify the "
                "addressable market or the company's share of it, so market "
                "size rests on filed disclosure rather than on estimate."
            ),
            evidence="; ".join(
                (e.value_text or e.label)[:120] for e in market_entries[:2]
            ),
            citations=tuple(ScoringEvidence.vault_citation(e)
                            for e in market_entries[:3]),
            computed_by=SERVICE,
        ))
    elif stats.get("sector_market_cap"):
        total = stats["sector_market_cap"]
        factors.append(FactorScore(
            key="market_size", label="Market size", weight=0.16,
            score=5.5, origin=Origin.REFERENCE, value=total,
            unit=f"{company.currency} {company.reporting_scale}",
            reason=(
                f"No addressable-market disclosure has been extracted. The "
                f"listed {company.sector} sector carries {total:,.0f} "
                f"{company.currency} {company.reporting_scale} of market "
                "capitalisation, which bounds the opportunity from below but "
                "is not the same measurement."
            ),
            citations=(sector_citation,),
            computed_by=SERVICE,
        ))
    else:
        factors.append(_missing(
            "market_size", "Market size", 0.16,
            "no addressable-market disclosure has been extracted and no "
            "sector aggregate is available.",
        ))

    # --- government policy ------------------------------------------------
    policy_entries = _vault_matches(evidence, _POLICY_KEYWORDS)
    if policy_entries:
        factors.append(FactorScore(
            key="government_policy", label="Government policy", weight=0.16,
            score=band(float(len(policy_entries)),
                       [(4, 7.5), (2, 6.5), (1, 6.0)]),
            origin=Origin.EXTRACTED, value=float(len(policy_entries)),
            unit="assertions",
            reason=(
                f"{len(policy_entries)} extracted assertions reference "
                "policy, regulation, tariffs or incentives affecting the "
                "industry. Scored above neutral for being disclosed and "
                "assessable, not for being favourable — direction is not "
                "inferred from a keyword match."
            ),
            evidence="; ".join(
                (e.value_text or e.label)[:120] for e in policy_entries[:2]
            ),
            citations=tuple(ScoringEvidence.vault_citation(e)
                            for e in policy_entries[:3]),
            computed_by=SERVICE,
        ))
    else:
        factors.append(_missing(
            "government_policy", "Government policy", 0.16,
            "no policy or regulatory assertion has been extracted from this "
            "company's filings. Inferring a policy stance from the sector "
            "name would be invention.",
        ))

    # --- demand outlook ---------------------------------------------------
    demand_entries = _vault_matches(evidence, _DEMAND_KEYWORDS)
    outlook_summaries = evidence.summaries_of("investment")
    if demand_entries or outlook_summaries:
        count = len(demand_entries) + len(outlook_summaries)
        citations = tuple(ScoringEvidence.vault_citation(e)
                          for e in demand_entries[:2])
        citations += tuple(ScoringEvidence.summary_citation(s)
                           for s in outlook_summaries[:2])
        factors.append(FactorScore(
            key="demand_outlook", label="Demand outlook", weight=0.16,
            score=band(float(count), [(4, 8.0), (2, 7.0), (1, 6.0)]),
            origin=Origin.EXTRACTED, value=float(count), unit="sources",
            reason=(
                f"{len(demand_entries)} extracted assertions and "
                f"{len(outlook_summaries)} model-written investment summaries "
                "address demand, order books or capacity utilisation. Scored "
                "for the availability of a forward view, not for its content."
            ),
            evidence="; ".join(
                (e.value_text or e.label)[:120] for e in demand_entries[:2]
            ) or "Drawn from document summaries.",
            citations=citations,
            computed_by=SERVICE,
        ))
    else:
        factors.append(_missing(
            "demand_outlook", "Demand outlook", 0.16,
            "no demand, order-book or utilisation commentary has been "
            "extracted or summarised for this company.",
        ))

    return build_module(KEY, factors)
