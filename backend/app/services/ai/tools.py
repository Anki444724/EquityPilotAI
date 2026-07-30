"""Platform tools the AI can invoke.

Each tool is a thin, typed wrapper over an existing service. They exist so the
model can ask the *platform* for a number rather than recalling one — the
distinction the brief draws between explaining conclusions and generating them.

Tools return citations, not prose, so anything a tool surfaces is automatically
verifiable by the citation engine.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from app.domain.ai.types import Citation, EvidenceKind
from app.services.ai.context_builder import ContextBuilder, GroundedContext


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """A callable capability, described for the model."""

    name: str
    description: str
    parameters: dict[str, str]
    evidence_kinds: tuple[EvidenceKind, ...]


TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "financial_lookup",
        "Fetch reported income statement, balance sheet and cash-flow figures.",
        {"ticker": "company ticker", "fiscal_year": "optional year"},
        (EvidenceKind.STATEMENT,),
    ),
    ToolSpec(
        "ratio_lookup",
        "Fetch computed ratios: returns, leverage, liquidity, efficiency.",
        {"ticker": "company ticker"},
        (EvidenceKind.RATIO,),
    ),
    ToolSpec(
        "forecast_lookup",
        "Fetch projected revenue, EBITDA, EPS and free cash flow.",
        {"ticker": "company ticker", "horizon": "3, 5 or 10"},
        (EvidenceKind.FORECAST,),
    ),
    ToolSpec(
        "dcf_lookup",
        "Fetch DCF and relative valuation outputs, WACC and target prices.",
        {"ticker": "company ticker"},
        (EvidenceKind.VALUATION,),
    ),
    ToolSpec(
        "scoring_lookup",
        "Fetch the institutional score, grade, recommendation and category scores.",
        {"ticker": "company ticker", "profile": "weight profile key"},
        (EvidenceKind.SCORING,),
    ),
    ToolSpec(
        "document_search",
        "Search uploaded filings and transcripts for a phrase.",
        {"ticker": "company ticker", "query": "search text"},
        (EvidenceKind.DOCUMENT,),
    ),
)

TOOLS_BY_NAME = {tool.name: tool for tool in TOOLS}


def describe_tools() -> str:
    """Tool catalogue, for inclusion in a prompt."""
    lines = ["AVAILABLE PLATFORM TOOLS — request these rather than recalling figures:"]
    for tool in TOOLS:
        params = ", ".join(f"{k} ({v})" for k, v in tool.parameters.items())
        lines.append(f"- {tool.name}({params}): {tool.description}")
    return "\n".join(lines)


class ToolRunner:
    """Executes a tool against the platform and returns citations."""

    #: Passages returned by a document search.
    DOCUMENT_TOP_K = 6

    def __init__(self, builder: ContextBuilder) -> None:
        self.builder = builder
        self._cache: GroundedContext | None = None

    def _context(self) -> GroundedContext:
        if self._cache is None:
            self._cache = self.builder.build()
        return self._cache

    def run(self, name: str, **kwargs) -> list[Citation]:
        spec = TOOLS_BY_NAME.get(name)
        if spec is None:
            raise KeyError(f"unknown tool '{name}'")
        if name == "document_search" and kwargs.get("query"):
            # A genuine retrieval, not a projection of pre-built context. This
            # is the one tool whose answer depends on the question asked, so
            # returning the same cached citations regardless of the query — as
            # the Module 6 placeholder did — would have been a lie.
            return self._document_search(str(kwargs["query"]))
        context = self._context()
        return [c for c in context.citations if c.kind in spec.evidence_kinds]

    def _document_search(self, query: str) -> list[Citation]:
        """Search uploaded documents and return each passage as a citation."""
        service = getattr(self.builder, "document_service", None)
        if service is None:
            return []
        company = self.builder.analysis.company
        try:
            answer = service.search(
                query, company_id=company.id, top_k=self.DOCUMENT_TOP_K
            )
        except Exception:  # pragma: no cover - a tool must never break a chat
            return []

        citations: list[Citation] = []
        for index, hit in enumerate(answer.hits, start=1):
            citations.append(Citation(
                key=f"doc_passage_{index}",
                label=f"{hit.document_title} p.{hit.page}",
                kind=EvidenceKind.DOCUMENT,
                # The quoted text *is* the value. A passage citation whose
                # value were a score rather than the words would give the model
                # nothing to quote and everything to invent.
                value=hit.text[:500],
                unit="",
                source=(
                    f"{hit.document_title}, page {hit.page}"
                    + (f", {hit.section.value.replace('_', ' ')}"
                       if hit.section.value != "unknown" else "")
                ),
            ))
        return citations

    def run_many(self, names: list[str]) -> list[Citation]:
        seen: set[str] = set()
        out: list[Citation] = []
        for name in names:
            for citation in self.run(name):
                if citation.key not in seen:
                    out.append(citation)
                    seen.add(citation.key)
        return out
