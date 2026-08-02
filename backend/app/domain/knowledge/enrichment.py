"""Domain rules for automatic memory enrichment.

The audit found the platform to be Advanced RAG rather than an AI Memory
System for one reason: nothing connected document ingestion to the vault.
163 documents had been ingested since the vault was last built and not one had
produced a vault entry. Every memory service existed, was tested and worked —
none of them was ever called automatically.

This module holds the rules that govern the automatic path. The orchestration
lives in `services/knowledge/enrichment.py`; the decisions live here so they
can be tested without a database or a network.

Three constraints shape the design, and all three come from measurement rather
than preference.

**The enrichment must not run inside the document worker.** Production has
crashed three times under that worker in a 1 GB container — most recently on a
62-page PDF that produced 516 chunks and held the process for 293 seconds.
Adding LLM summarisation and observation generation to the same loop would
guarantee a fourth crash. Enrichment is therefore a separate job kind, claimed
by the background worker, with its own lease and its own failure accounting.

**Enrichment is per-company, not per-document.** A filing crawl can deliver
twenty documents for one company inside a minute. Twenty vault rebuilds and
twenty observation regenerations would be twenty times the work for one
outcome. Jobs are deduplicated on the company and debounced, so a burst
collapses into a single pass once the burst subsides.

**Extracted financial facts never outrank filed ones.** A figure read out of a
PDF by a regex enters the canonical store at `Precedence.ALIAS`, strictly
below the `Precedence.STORE` tier that screener.in and the US pipeline write.
The canonical resolver already prefers the lower number, so an extraction can
fill a gap but can never silently overwrite an audited figure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class EnrichmentStage(StrEnum):
    """The stages of the automatic memory pass, in execution order.

    Ordered by dependency, not by importance:

    * `FINANCIAL_PROMOTION` writes canonical facts, which the observation
      generator later reads as corroboration.
    * `VAULT` promotes extracted facts into versioned knowledge.
    * `AI_NOTES` needs the vault to already hold this filing's assertions.
    * `SUMMARIES` are per-document and feed the observation generator's
      evidence, so they must precede it.
    * `OBSERVATIONS` reads summaries first and falls back to chunks.
    * `TEMPORAL_LINK` verifies the prior year once the new year exists.

    The knowledge graph is deliberately absent: it is already updated
    synchronously during ingestion, at `services/documents/service.py:370`,
    where edges seen in a second document are merged rather than duplicated.
    Re-running it here would double every weight.
    """

    FINANCIAL_PROMOTION = "financial_promotion"
    VAULT = "vault"
    AI_NOTES = "ai_notes"
    SUMMARIES = "summaries"
    OBSERVATIONS = "observations"
    TEMPORAL_LINK = "temporal_link"


#: Stages in dependency order. Iterated directly, so the tuple is the schedule.
STAGE_ORDER: tuple[EnrichmentStage, ...] = (
    EnrichmentStage.FINANCIAL_PROMOTION,
    EnrichmentStage.VAULT,
    EnrichmentStage.AI_NOTES,
    EnrichmentStage.SUMMARIES,
    EnrichmentStage.OBSERVATIONS,
    EnrichmentStage.TEMPORAL_LINK,
)

#: Stages that call an LLM, and therefore cost money and can be rate-limited.
#: Separated so a deployment without a working provider still gets the
#: structural half of the memory — vault, financials, graph — rather than
#: nothing at all.
LLM_STAGES: frozenset[EnrichmentStage] = frozenset({
    EnrichmentStage.SUMMARIES,
    EnrichmentStage.OBSERVATIONS,
    EnrichmentStage.AI_NOTES,
})


#: Extracted field key → canonical line item.
#:
#: Deliberately tiny, and the smallness is the point. The PDF extractor
#: recognises ten FINANCIAL fields; only three correspond to something the
#: canonical schema STORES. The rest are quantities it DERIVES, and the
#: distinction was checked against `LineItem` rather than assumed — a first
#: draft of this map named `pat`, `cfo` and `net_worth`, none of which exist
#: as canonical line items.
#:
#: Omitted, each for a stated reason:
#:
#:   pat, ebitda, eps, free_cash_flow, operating_cash_flow, net_worth
#:       — DERIVED by the statement builders from the stored inputs. Writing
#:         them as facts would create a second source of truth that can
#:         disagree with the first, which is exactly the failure the
#:         canonical layer exists to prevent.
#:   gross_debt
#:       — the schema splits borrowings into short-term, long-term and
#:         current maturities. A single combined figure cannot be apportioned
#:         across the three without inventing the split.
#:
#: Three fields survive. That is a small contribution to the financial store
#: and it is an honest one; the extractor's real value is narrative, and that
#: reaches memory through the vault rather than through this map.
PROMOTABLE_FIELDS: dict[str, str] = {
    "revenue": "revenue",
    "cash_and_equivalents": "cash_and_bank",
    "capex": "capex",
}

#: Below this, an extracted figure is not promoted to the canonical store at
#: all. A low-confidence regex hit on a page of prose is a plausible number in
#: the wrong place, and the canonical store is read by valuation and scoring.
MIN_PROMOTION_CONFIDENCE = 0.75

#: Seconds to wait before running an enrichment pass for a company.
#:
#: A filing crawl delivers documents in bursts — twenty for one company inside
#: a minute is normal. Debouncing collapses the burst into one pass. Long
#: enough to absorb a crawl, short enough that a single manual upload is
#: absorbed into memory while the user is still looking at the page.
DEBOUNCE_SECONDS = 120

#: Hard ceiling on documents summarised in one pass. Summaries are the most
#: expensive stage; without a cap, back-filling a company with eighty filings
#: would issue eighty LLM calls inside one lease and time it out.
MAX_SUMMARY_DOCUMENTS_PER_PASS = 3

#: Same reasoning for observations: a company with twelve fiscal years of
#: filings would otherwise regenerate all twelve on every upload.
MAX_OBSERVATION_YEARS_PER_PASS = 2


@dataclass(slots=True)
class StageOutcome:
    """What one stage did, or why it did nothing."""

    stage: EnrichmentStage
    ok: bool = True
    #: Rows written, documents summarised, years generated — stage-specific.
    written: int = 0
    skipped: bool = False
    #: Present when the stage was skipped or failed. Always specific: "no
    #: LLM provider configured" is useful, "failed" is not.
    detail: str | None = None
    ms: float = 0.0

    def as_dict(self) -> dict[str, object]:
        return {
            "stage": self.stage.value,
            "ok": self.ok,
            "written": self.written,
            "skipped": self.skipped,
            "detail": self.detail,
            "ms": round(self.ms, 1),
        }


@dataclass(slots=True)
class EnrichmentResult:
    """The whole pass for one company."""

    company_id: str
    trigger_document_id: int | None = None
    stages: list[StageOutcome] = field(default_factory=list)
    total_ms: float = 0.0

    @property
    def written(self) -> int:
        return sum(s.written for s in self.stages)

    @property
    def failed_stages(self) -> list[StageOutcome]:
        return [s for s in self.stages if not s.ok]

    def as_dict(self) -> dict[str, object]:
        return {
            "company_id": self.company_id,
            "trigger_document_id": self.trigger_document_id,
            "written": self.written,
            "failed_stages": [s.stage.value for s in self.failed_stages],
            "stages": [s.as_dict() for s in self.stages],
            "total_ms": round(self.total_ms, 1),
        }


def should_promote(confidence: float, field_key: str) -> bool:
    """Whether an extracted financial fact may enter the canonical store.

    Two gates, both necessary. The field must be one the canonical schema
    accepts as an input rather than derives, and the extraction must be
    confident enough that a wrong number is unlikely — because valuation and
    scoring read this store, and a plausible figure in the wrong place is
    worse than a gap, which at least renders as "no data".
    """
    return (
        field_key in PROMOTABLE_FIELDS
        and confidence >= MIN_PROMOTION_CONFIDENCE
    )
