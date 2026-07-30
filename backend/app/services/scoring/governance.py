"""Corporate governance.

Governance failures are the dominant source of permanent capital loss in Indian
mid-caps, which is why the conservative profile weights this heavily. Most
inputs are qualitative by nature; two — promoter pledge and audit
qualifications — are hard facts and are treated as verified.
"""
from __future__ import annotations

from app.domain.scoring.base import (
    DataOrigin, MetricScore, band_score, build_category,
)
from app.domain.scoring.inputs import ScoringInputs
from app.domain.scoring.weights import Category, CATEGORY_LABELS

KEY = Category.GOVERNANCE


def score(inputs: ScoringInputs, weight: float):
    metrics: list[MetricScore] = []
    q = inputs.qualitative

    # --- board independence -------------------------------------------------
    if q.board_independence is not None:
        metrics.append(MetricScore(
            key="board_independence", label="Board independence", weight=0.22,
            score=band_score(q.board_independence,
                             [(0.60, 10), (0.50, 8.5), (0.40, 6.5), (0.33, 5), (0.25, 3)]),
            origin=DataOrigin.ANALYST, value=q.board_independence, unit="%",
            explanation=(
                f"{q.board_independence:.0%} of the board is independent — "
                f"{'comfortably above' if q.board_independence >= 0.5 else 'at or below'} "
                "the one-half benchmark."
            ),
            source="Analyst input",
        ))
    else:
        metrics.append(MetricScore(
            key="board_independence", label="Board independence", weight=0.22,
            score=5.0, origin=DataOrigin.MISSING,
            explanation="Board composition has not been assessed.",
        ))

    # --- audit qualifications: a hard fact ------------------------------------
    if q.audit_qualifications is not None:
        metrics.append(MetricScore(
            key="audit_qualifications", label="Audit qualifications", weight=0.24,
            score=band_score(q.audit_qualifications, [(0, 10), (1, 5), (2, 2.5)],
                             higher_is_better=False),
            origin=DataOrigin.VERIFIED, value=float(q.audit_qualifications), unit="count",
            explanation=(
                "The auditor's report is unqualified." if q.audit_qualifications == 0
                else f"{q.audit_qualifications} audit qualification(s) recorded — a serious governance signal."
            ),
            source="Annual report",
        ))
    else:
        metrics.append(MetricScore(
            key="audit_qualifications", label="Audit qualifications", weight=0.24,
            score=5.0, origin=DataOrigin.MISSING,
            explanation="Auditor's opinion has not been reviewed.",
        ))

    # --- auditor standing ------------------------------------------------------
    if q.auditor_is_big_four is not None:
        metrics.append(MetricScore(
            key="auditor_quality", label="Auditor standing", weight=0.12,
            score=8.5 if q.auditor_is_big_four else 6.0,
            origin=DataOrigin.VERIFIED, value=1.0 if q.auditor_is_big_four else 0.0,
            unit="",
            explanation=(
                "Audited by a Big Four firm." if q.auditor_is_big_four
                else "Audited by a non-Big-Four firm, which warrants closer scrutiny of disclosures."
            ),
            source="Annual report",
        ))

    # --- related-party intensity -------------------------------------------------
    if q.related_party_intensity is not None:
        metrics.append(MetricScore(
            key="related_party", label="Related-party transactions", weight=0.20,
            score=band_score(q.related_party_intensity,
                             [(0.01, 10), (0.03, 8), (0.07, 5.5), (0.12, 3), (0.20, 1.5)],
                             higher_is_better=False),
            origin=DataOrigin.ANALYST, value=q.related_party_intensity, unit="%",
            explanation=(
                f"Related-party transactions equal {q.related_party_intensity:.1%} of revenue — "
                f"{'immaterial' if q.related_party_intensity < 0.03 else 'material enough to warrant scrutiny'}."
            ),
            source="Analyst input",
        ))
    else:
        metrics.append(MetricScore(
            key="related_party", label="Related-party transactions", weight=0.20,
            score=5.0, origin=DataOrigin.MISSING,
            explanation="Related-party exposure has not been quantified.",
        ))

    # --- promoter pledge (hard fact) ------------------------------------------------
    if q.promoter_pledge is not None:
        metrics.append(MetricScore(
            key="pledge", label="Promoter share pledge", weight=0.22,
            score=band_score(q.promoter_pledge, [(0.0, 10), (0.05, 8), (0.15, 5), (0.30, 2.5), (0.50, 1)],
                             higher_is_better=False),
            origin=DataOrigin.VERIFIED, value=q.promoter_pledge, unit="%",
            explanation=(
                "No promoter shares are pledged." if q.promoter_pledge == 0
                else f"{q.promoter_pledge:.0%} of the promoter stake is pledged, creating forced-sale risk."
            ),
            source="14 Shareholding",
        ))
    else:
        metrics.append(MetricScore(
            key="pledge", label="Promoter share pledge", weight=0.22,
            score=5.0, origin=DataOrigin.MISSING,
            explanation="Promoter pledge data not available.",
        ))

    # --- disclosure quality ----------------------------------------------------------
    if q.disclosure_quality is not None:
        metrics.append(MetricScore(
            key="disclosure", label="Disclosure quality", weight=0.14,
            score=q.disclosure_quality, origin=DataOrigin.ANALYST,
            value=q.disclosure_quality, unit="/10",
            explanation=f"Disclosure quality rated {q.disclosure_quality:.0f}/10.",
            source="Analyst input",
        ))

    return build_category(KEY.value, CATEGORY_LABELS[KEY], metrics, weight,
                          ["Annual report", "14 Shareholding", "Analyst input"])
