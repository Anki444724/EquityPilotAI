"""Module 3 — Latest News (weight 8).

Positive developments, negative developments, regulatory changes, M&A, large
orders, management announcements.

News is scored from the corporate-announcement ledger the filing collector
maintains — NSE, BSE and investor-relations pages — not from a news API and not
from a model's recollection. Each factor cites the announcements it counted, so
a reader can see exactly which disclosures produced the score.

**Classification is keyword-driven and deliberately conservative.** An
announcement whose subject line does not clearly indicate a category is not
forced into one. Silently classifying ambiguous headlines produces a tidy
distribution that is mostly noise, and the missing-factor accounting is a more
honest output than a confident misreading.

**Absence of news is not good news.** A company with no announcements in twelve
months scores the neutral midpoint with the gap reported, rather than scoring
well for having nothing negative to disclose. The Indian disclosure regime
makes silence far more likely to mean the crawler has not reached the company
than that nothing happened.
"""
from __future__ import annotations

import re
from typing import Sequence

from app.domain.ai_scoring.framework import Module
from app.domain.ai_scoring.types import FactorScore, Origin, band, scale
from app.services.ai_scoring.evidence import NewsItem, ScoringEvidence
from app.services.ai_scoring.modules.common import build_module

KEY = Module.LATEST_NEWS
SERVICE = "ai_scoring.latest_news"

#: Subject-line patterns. Word-boundary anchored: an unanchored "order" matches
#: "in order to" in half the corpus, and "MA" matches inside "management".
_PATTERNS: dict[str, tuple[str, ...]] = {
    "positive": (
        r"\brecord\b", r"\bhighest ever\b", r"\bawarded\b", r"\bwins?\b",
        r"\bsecured?\b", r"\bcommission(ing|ed)?\b", r"\bexpansion\b",
        r"\bcapacity addition\b", r"\bnew (plant|facility|unit)\b",
        r"\bdividend\b", r"\bbonus issue\b", r"\bbuy ?back\b",
        r"\bupgrade[ds]?\b", r"\bcredit rating.*(upgrade|revised upward)\b",
    ),
    "negative": (
        r"\bresignation\b", r"\bresigned\b", r"\bpenalt(y|ies)\b",
        r"\bfine[ds]?\b", r"\bdefault\b", r"\bdowngrade[ds]?\b",
        r"\binsolvency\b", r"\bNCLT\b", r"\blitigation\b", r"\bshow cause\b",
        r"\bsearch and seizure\b", r"\bfraud\b", r"\bqualified opinion\b",
        r"\bimpairment\b", r"\bshut ?down\b", r"\bstrike\b", r"\bforce majeure\b",
    ),
    "regulatory": (
        r"\bSEBI\b", r"\bRBI\b", r"\bCCI\b", r"\bregulator(y)?\b",
        r"\bcompliance\b", r"\bLODR\b", r"\bregulation \d+\b",
        r"\bapproval (of|from|by)\b", r"\blicen[cs]e\b", r"\btariff\b",
        r"\bGST\b", r"\bCBDT\b", r"\bCERC\b", r"\bTRAI\b", r"\bIRDAI\b",
    ),
    "ma": (
        r"\bacquisition\b", r"\bacquire[ds]?\b", r"\bmerger\b", r"\bamalgamation\b",
        r"\bdemerger\b", r"\bdivest(ment|iture|ed)?\b", r"\bstake sale\b",
        r"\bjoint venture\b", r"\bJV\b", r"\bslump sale\b",
        r"\bscheme of arrangement\b", r"\bsubsidiar(y|ies)\b",
    ),
    "orders": (
        r"\border win\b", r"\breceipt of order\b", r"\bletter of (award|intent)\b",
        r"\bLOA\b", r"\bLOI\b", r"\bcontract (award|win|secured)\b",
        r"\bbagged?\b", r"\border book\b", r"\bwork order\b",
        r"\bpurchase order\b", r"\bnew order\b",
    ),
    "management": (
        r"\bappointment\b", r"\bappointed\b", r"\bcessation\b",
        # A resignation is both a negative development and a management
        # announcement, and it must appear in both buckets. Listing it only
        # under "negative" meant a company whose only management disclosures
        # were departures scored zero on management communication — reading
        # an absence of announcements where there were several. `classify`
        # returns a set precisely so an event can be two things at once.
        r"\bresignation\b", r"\bresigned\b",
        r"\bboard meeting\b", r"\bmanaging director\b", r"\bCEO\b", r"\bCFO\b",
        r"\bkey managerial personnel\b", r"\bKMP\b", r"\banalyst meet\b",
        r"\bearnings call\b", r"\binvestor (meet|presentation|call)\b",
        r"\bconference call\b", r"\bguidance\b",
    ),
}

_COMPILED: dict[str, tuple[re.Pattern[str], ...]] = {
    category: tuple(re.compile(p, re.IGNORECASE) for p in patterns)
    for category, patterns in _PATTERNS.items()
}


def classify(item: NewsItem) -> set[str]:
    """Categories an announcement matches. May be empty, may be several.

    An announcement can genuinely be both regulatory and M&A — a CCI approval
    for an acquisition is exactly that — so this returns a set rather than
    forcing a single label.
    """
    text = f"{item.title} {item.filing_type or ''}"
    return {
        category
        for category, patterns in _COMPILED.items()
        if any(p.search(text) for p in patterns)
    }


def _bucket(news: Sequence[NewsItem]) -> dict[str, list[NewsItem]]:
    buckets: dict[str, list[NewsItem]] = {c: [] for c in _PATTERNS}
    for item in news:
        for category in classify(item):
            buckets[category].append(item)
    return buckets


def _missing(key: str, label: str, weight: float, what: str) -> FactorScore:
    return FactorScore(
        key=key, label=label, weight=weight, score=5.0, origin=Origin.MISSING,
        reason=f"Not assessed: {what}", computed_by=SERVICE,
    )


def _counted(
    key: str, label: str, weight: float, items: list[NewsItem],
    bands: list[tuple[float, float]], reason: str, *,
    scanned: Sequence[NewsItem] = (),
    higher_is_better: bool = True,
) -> FactorScore:
    """A factor scored on how many announcements matched a category.

    When nothing matched, the factor still cites the announcements it
    *scanned*. A count of zero adverse events is a real finding — "eighteen
    disclosures were read and none was adverse" — and it is a different claim
    from "no disclosures were read", which is the MISSING case handled
    separately above. Without these citations the two are indistinguishable in
    the panel, and a zero-evidence clean bill of health looks exactly like a
    verified one. Found by the production validation harness, which flagged
    `latest_news.negative` and `latest_news.orders` as scored-but-uncited.
    """
    if items:
        citations = tuple(i.citation() for i in items[:4])
        evidence = "; ".join(i.title[:110] for i in items[:3])
    else:
        citations = tuple(i.citation() for i in list(scanned)[:3])
        evidence = (
            f"None identified. {len(scanned)} announcements in the window "
            "were scanned; the three most recent are cited as the evidence "
            "base for this negative finding."
        )
    return FactorScore(
        key=key, label=label, weight=weight,
        score=band(float(len(items)), bands, higher_is_better=higher_is_better),
        origin=Origin.REPORTED, value=float(len(items)), unit="announcements",
        reason=reason,
        evidence=evidence,
        citations=citations,
        computed_by=SERVICE,
    )


def score(evidence: ScoringEvidence):
    factors: list[FactorScore] = []
    news = evidence.news

    if not news:
        # Every factor is genuinely unassessed. Stated once per factor rather
        # than as a single module-level note, so the panel shows six explicit
        # gaps rather than one number with an asterisk.
        gap = ("no corporate announcements have been collected for this "
               "company in the last twelve months. Silence is far more likely "
               "to mean the crawler has not reached the company than that "
               "nothing happened, so this is reported as a gap rather than "
               "scored as an absence of bad news.")
        for key, label, weight in (
            ("positive", "Positive developments", 0.22),
            ("negative", "Negative developments", 0.24),
            ("regulatory", "Regulatory changes", 0.14),
            ("ma", "M&A", 0.12),
            ("orders", "Large orders", 0.14),
            ("management", "Management announcements", 0.14),
        ):
            factors.append(_missing(key, label, weight, gap))
        return build_module(KEY, factors)

    buckets = _bucket(news)
    recent = evidence.recent_news()
    window = len(news)

    # --- positive developments -------------------------------------------
    positive = buckets["positive"]
    factors.append(_counted(
        "positive", "Positive developments", 0.22, positive,
        [(6, 9.5), (4, 8.5), (2, 7.0), (1, 6.0), (0, 5.0)], scanned=news,
        reason=(
            f"{len(positive)} of {window} announcements in the last twelve "
            "months carry positive language — awards, capacity additions, "
            "record results, rating upgrades or shareholder returns."
        ),
    ))

    # --- negative developments -------------------------------------------
    # Inverted: more negative announcements is worse. Scored on the count
    # rather than on the share, because two governance events matter whether
    # the company filed ten announcements or two hundred.
    negative = buckets["negative"]
    factors.append(_counted(
        "negative", "Negative developments", 0.24, negative,
        [(0, 9.5), (1, 7.5), (2, 6.0), (4, 4.0), (7, 2.5)],
        scanned=news, higher_is_better=False,
        reason=(
            f"{len(negative)} of {window} announcements carry adverse "
            "language — penalties, defaults, downgrades, litigation, "
            "resignations or impairments. "
            + ("None identified, which is the best available reading of a "
               "clean twelve months." if not negative else
               "Each is counted individually: two governance events matter "
               "regardless of how many routine filings surround them.")
        ),
    ))

    # --- regulatory changes ----------------------------------------------
    regulatory = buckets["regulatory"]
    factors.append(_counted(
        "regulatory", "Regulatory changes", 0.14, regulatory,
        [(5, 6.5), (3, 6.0), (1, 5.5), (0, 5.0)], scanned=news,
        reason=(
            f"{len(regulatory)} announcements reference a regulator or a "
            "regulatory action. Scored close to neutral by design: regulatory "
            "traffic is a fact about the operating environment, not a verdict "
            "on the company, and the direction only becomes visible when an "
            "announcement is also classified positive or negative."
        ),
    ))

    # --- M&A ---------------------------------------------------------------
    ma = buckets["ma"]
    factors.append(_counted(
        "ma", "M&A", 0.12, ma,
        [(4, 6.0), (2, 5.8), (1, 5.5), (0, 5.0)], scanned=news,
        reason=(
            f"{len(ma)} announcements concern acquisitions, mergers, "
            "demergers, divestments or joint ventures. Deliberately scored "
            "near neutral: transaction activity is neither good nor bad "
            "until its returns are visible in the statements, which Module 2 "
            "measures."
        ),
    ))

    # --- large orders -----------------------------------------------------
    orders = buckets["orders"]
    factors.append(_counted(
        "orders", "Large orders", 0.14, orders,
        [(6, 10), (4, 9.0), (2, 7.5), (1, 6.5), (0, 5.0)], scanned=news,
        reason=(
            f"{len(orders)} announcements report order wins, letters of "
            "award or order-book updates — forward revenue visibility that "
            "the reported statements do not yet contain."
        ),
    ))

    # --- management announcements -----------------------------------------
    management = buckets["management"]
    factors.append(FactorScore(
        key="management", label="Management announcements", weight=0.14,
        score=band(float(len(management)),
                   [(8, 9.5), (5, 8.5), (3, 7.5), (1, 6.5), (0, 5.0)]),
        origin=Origin.REPORTED, value=float(len(management)),
        unit="announcements",
        reason=(
            f"{len(management)} announcements concern board changes, key "
            "managerial personnel, investor calls or guidance. Frequency is "
            "scored as a disclosure habit: a management that talks to the "
            "market regularly is more assessable than one that does not. "
            f"{len(recent)} of {window} announcements fell in the last 90 days."
        ),
        evidence=("; ".join(i.title[:110] for i in management[:3])
                  or f"None identified across {window} scanned announcements."),
        citations=tuple(i.citation()
                        for i in (management or list(news)[:3])[:4]),
        computed_by=SERVICE,
    ))

    return build_module(KEY, factors)
