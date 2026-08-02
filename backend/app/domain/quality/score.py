"""Data Quality Score — how complete, trustworthy and current a company is.

A research answer built on two filings and no cash-flow statement is not
wrong, but it is not the same artefact as one built on ten years of audited
accounts, eight annual reports and a verified management track record. The
platform has always known the difference internally and never said so. This
module makes that difference a number, a grade and a list of what is missing.

Design rules, each of which exists because the obvious alternative misleads.

**A dimension scores what is PRESENT, never what is assumed.** Every check
resolves to a boolean or a ratio derived from a real row. Nothing is inferred
from another dimension's success, because a scorer that rewards itself for
consistency will report 100 for a company it has never seen.

**Weights sum to exactly 100.** Asserted at import, not documented. A silent
drift to 95 would make every score in the platform quietly wrong and no test
would notice.

**Partial credit within a dimension is proportional, not generous.** Holding
one of seven document classes scores 1/7 of that dimension, not "some". The
temptation is to award a floor so a sparse company does not look bad; the
result is a score that cannot distinguish sparse from adequate.

**Missing items are named, never summarised.** "Missing 3 items" tells a user
nothing they can act on. "Missing annual report, missing IR URL, missing
conference call" tells them exactly what to collect.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Dimension(StrEnum):
    """The eight scored dimensions, in the order the brief lists them."""

    IDENTITY = "identity"
    FINANCIAL_STATEMENTS = "financial_statements"
    DOCUMENTS = "documents"
    KNOWLEDGE_VAULT = "knowledge_vault"
    AI_COVERAGE = "ai_coverage"
    FRESHNESS = "freshness"
    SOURCE_QUALITY = "source_quality"
    SYSTEM_HEALTH = "system_health"


#: Weight of each dimension in the final score.
WEIGHTS: dict[Dimension, int] = {
    Dimension.IDENTITY: 5,
    Dimension.FINANCIAL_STATEMENTS: 20,
    Dimension.DOCUMENTS: 20,
    Dimension.KNOWLEDGE_VAULT: 15,
    Dimension.AI_COVERAGE: 15,
    Dimension.FRESHNESS: 10,
    Dimension.SOURCE_QUALITY: 10,
    Dimension.SYSTEM_HEALTH: 5,
}

# Enforced rather than trusted. A weight table that no longer sums to 100
# makes every score in the platform wrong by a margin nobody can see.
assert sum(WEIGHTS.values()) == 100, (
    f"data-quality weights sum to {sum(WEIGHTS.values())}, expected 100"
)

#: The individual checks within each dimension, in the brief's order.
#: Held as data so the API can describe the scheme without duplicating it,
#: and so a check cannot exist in the scorer but not in the documentation.
CHECKS: dict[Dimension, tuple[str, ...]] = {
    Dimension.IDENTITY: (
        "nse_symbol", "isin", "sector", "industry", "official_website",
    ),
    Dimension.FINANCIAL_STATEMENTS: (
        "income_statement", "balance_sheet", "cash_flow",
        "quarterly_results", "ttm", "ten_year_history",
    ),
    Dimension.DOCUMENTS: (
        "latest_annual_report", "previous_annual_reports", "quarterly_pdfs",
        "investor_presentations", "conference_call_transcripts",
        "credit_rating_reports", "esg_reports",
    ),
    Dimension.KNOWLEDGE_VAULT: (
        "business_summary", "ai_notes", "executive_summary",
        "investment_summary", "historical_observations", "temporal_memory",
    ),
    Dimension.AI_COVERAGE: (
        "business_model", "bull_thesis", "bear_thesis", "risks", "catalysts",
        "valuation", "forecast", "management_analysis", "moat",
        "confidence_score",
    ),
    Dimension.FRESHNESS: (
        "latest_filing", "latest_quarterly", "latest_annual_report",
        "latest_price",
    ),
    Dimension.SOURCE_QUALITY: (
        "official_ir", "nse", "bse", "verified_financial_database",
        "confidence",
    ),
    Dimension.SYSTEM_HEALTH: (
        "successful_parsing", "successful_extraction",
        "successful_embeddings", "successful_rag",
    ),
}

#: Human labels for the missing-data panel. A user cannot act on
#: "conference_call_transcripts"; they can act on the sentence.
MISSING_LABELS: dict[str, str] = {
    "nse_symbol": "Missing NSE symbol",
    "isin": "Missing ISIN",
    "sector": "Missing sector",
    "industry": "Missing industry",
    "official_website": "Missing official website",
    "income_statement": "Missing income statement",
    "balance_sheet": "Missing balance sheet",
    "cash_flow": "Missing cash flow statement",
    "quarterly_results": "Missing quarterly results",
    "ttm": "Missing trailing twelve months",
    "ten_year_history": "Missing 10-year history",
    "latest_annual_report": "Missing latest annual report",
    "previous_annual_reports": "Missing previous annual reports",
    "quarterly_pdfs": "Missing quarterly PDFs",
    "investor_presentations": "Missing investor presentations",
    "conference_call_transcripts": "Missing conference call transcripts",
    "credit_rating_reports": "Missing credit rating reports",
    "esg_reports": "Missing ESG reports",
    "business_summary": "Missing business summary",
    "ai_notes": "Missing AI notes",
    "executive_summary": "Missing executive summary",
    "investment_summary": "Missing investment summary",
    "historical_observations": "Missing historical observations",
    "temporal_memory": "Missing temporal memory",
    "business_model": "Missing business model analysis",
    "bull_thesis": "Missing bull thesis",
    "bear_thesis": "Missing bear thesis",
    "risks": "Missing risk analysis",
    "catalysts": "Missing catalysts",
    "valuation": "Missing valuation",
    "forecast": "Missing forecast",
    "management_analysis": "Missing management analysis",
    "moat": "Missing moat analysis",
    "confidence_score": "Missing AI confidence score",
    "latest_filing": "No recent filing",
    "latest_quarterly": "No recent quarterly result",
    "latest_price": "Missing market price",
    "official_ir": "Missing IR URL",
    "nse": "No NSE coverage",
    "bse": "No BSE coverage",
    "verified_financial_database": "Missing verified financial database",
    "confidence": "Low source confidence",
    "successful_parsing": "Document parsing incomplete",
    "successful_extraction": "Field extraction incomplete",
    "successful_embeddings": "Embeddings incomplete",
    "successful_rag": "Retrieval index incomplete",
}


class Grade(StrEnum):
    A_PLUS = "A+"
    A = "A"
    B_PLUS = "B+"
    B = "B"
    C = "C"
    D = "D"
    F = "F"


#: Score floor for each grade, highest first.
GRADE_BANDS: tuple[tuple[float, Grade], ...] = (
    (90.0, Grade.A_PLUS),
    (80.0, Grade.A),
    (70.0, Grade.B_PLUS),
    (60.0, Grade.B),
    (50.0, Grade.C),
    (40.0, Grade.D),
    (0.0, Grade.F),
)

#: Below this the AI must warn that its analysis rests on incomplete data.
#: The brief sets it; it is named here so the threshold exists once.
WARN_BELOW = 70.0

#: Freshness horizons in days. A filing older than this scores nothing for
#: that check — "we hold an annual report" is not the same claim as "we hold
#: a CURRENT annual report", and a scorer that conflates them rates a company
#: last covered in 2019 as fully covered.
FRESHNESS_HORIZON_DAYS: dict[str, int] = {
    "latest_filing": 90,
    "latest_quarterly": 120,
    "latest_annual_report": 400,
    "latest_price": 7,
}


def grade_for(score: float) -> Grade:
    for floor, grade in GRADE_BANDS:
        if score >= floor:
            return grade
    return Grade.F


@dataclass(slots=True)
class CheckResult:
    """One check within a dimension."""

    key: str
    #: 0.0–1.0. Fractional where the check is proportional (e.g. 4 of 7
    #: document classes held), boolean-valued 0 or 1 otherwise.
    value: float
    detail: str | None = None

    @property
    def passed(self) -> bool:
        # A partially-satisfied check is not "missing": a company with three
        # of seven document classes should not appear in the missing panel
        # under a heading it partly satisfies.
        return self.value > 0.0


@dataclass(slots=True)
class DimensionScore:
    dimension: Dimension
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def weight(self) -> int:
        return WEIGHTS[self.dimension]

    @property
    def ratio(self) -> float:
        """Mean of the checks, 0.0–1.0."""
        if not self.checks:
            return 0.0
        return sum(c.value for c in self.checks) / len(self.checks)

    @property
    def points(self) -> float:
        return round(self.ratio * self.weight, 2)

    @property
    def missing(self) -> list[str]:
        return [c.key for c in self.checks if not c.passed]

    def as_dict(self) -> dict[str, object]:
        return {
            "dimension": self.dimension.value,
            "weight": self.weight,
            "ratio": round(self.ratio, 4),
            "points": self.points,
            "checks": [
                {"key": c.key, "value": round(c.value, 4), "detail": c.detail}
                for c in self.checks
            ],
            "missing": self.missing,
        }


@dataclass(slots=True)
class QualityScore:
    """The complete assessment for one company."""

    company_id: str
    ticker: str
    dimensions: list[DimensionScore] = field(default_factory=list)
    #: Days since the most recent evidence of any kind. None when nothing
    #: dated exists, which is itself reported rather than shown as 0.
    last_updated_days: int | None = None
    next_crawl_at: object | None = None
    knowledge_freshness_days: int | None = None

    @property
    def score(self) -> float:
        return round(sum(d.points for d in self.dimensions), 1)

    @property
    def grade(self) -> Grade:
        return grade_for(self.score)

    @property
    def needs_warning(self) -> bool:
        return self.score < WARN_BELOW

    @property
    def missing_items(self) -> list[str]:
        """Human-readable, in dimension order. Never truncated."""
        out: list[str] = []
        for dimension in self.dimensions:
            for key in dimension.missing:
                label = MISSING_LABELS.get(key)
                if label:
                    out.append(label)
        return out

    def explanation(self) -> list[str]:
        """Plain sentences describing what the score rests on.

        Positive and negative both stated. A panel that lists only strengths
        is marketing; one that lists only gaps is discouraging and equally
        uninformative.
        """
        lines: list[str] = []
        by_dimension = {d.dimension: d for d in self.dimensions}

        band = {
            Grade.A_PLUS: "Excellent institutional coverage.",
            Grade.A: "Strong coverage across most dimensions.",
            Grade.B_PLUS: "Good coverage with some gaps.",
            Grade.B: "Adequate coverage; several dimensions incomplete.",
            Grade.C: "Limited coverage. Treat conclusions with caution.",
            Grade.D: "Sparse coverage. Analysis rests on little evidence.",
            Grade.F: "Minimal data held for this company.",
        }[self.grade]
        lines.append(band)

        documents = by_dimension.get(Dimension.DOCUMENTS)
        if documents:
            held = [c for c in documents.checks if c.passed]
            if any(c.key == "latest_annual_report" for c in held):
                lines.append("Latest annual report available.")
            else:
                lines.append("No current annual report held.")
            if any(c.key == "conference_call_transcripts" for c in held):
                lines.append("Conference call transcripts available.")

        financials = by_dimension.get(Dimension.FINANCIAL_STATEMENTS)
        if financials:
            if financials.ratio >= 0.99:
                lines.append("Financial statements complete.")
            elif financials.ratio >= 0.5:
                lines.append("Financial statements partially complete.")
            else:
                lines.append("Financial statements largely absent.")

        vault = by_dimension.get(Dimension.KNOWLEDGE_VAULT)
        if vault and vault.ratio > 0:
            lines.append("Knowledge Vault populated.")
        elif vault:
            lines.append("Knowledge Vault empty for this company.")

        if self.last_updated_days is not None:
            if self.last_updated_days == 0:
                lines.append("Last update today.")
            elif self.last_updated_days == 1:
                lines.append("Last update 1 day ago.")
            else:
                lines.append(f"Last update {self.last_updated_days} days ago.")
        else:
            lines.append("No dated evidence on record.")

        return lines

    def as_dict(self) -> dict[str, object]:
        return {
            "company_id": self.company_id,
            "ticker": self.ticker,
            "score": self.score,
            "grade": self.grade.value,
            "needs_warning": self.needs_warning,
            "explanation": self.explanation(),
            "missing_items": self.missing_items,
            "dimensions": [d.as_dict() for d in self.dimensions],
            "freshness": {
                "last_updated_days": self.last_updated_days,
                "next_crawl_at": self.next_crawl_at,
                "knowledge_freshness_days": self.knowledge_freshness_days,
            },
        }
