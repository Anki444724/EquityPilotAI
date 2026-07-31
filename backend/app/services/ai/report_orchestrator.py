"""Runs a research report section by section, each from its own provider.

Previously one prompt was answered from one pool of evidence, and the largest
contributor to that pool — the scoring engine — dominated every section. This
runs each section against only the evidence its route permits, so the business
model is answered from the annual report and the institutional score from the
scoring engine, rather than both from whichever block ranked first.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import structlog

from app.data.filings.base import SourceCategory
from app.domain.ai.sourcing import SourceDirective, SourceScope
from app.services.ai.orchestration import (
    ROUTES, Provider, Section, SectionResult, SectionRoute, score_section,
    select_evidence, utc_now,
)

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class OrchestratedReport:
    ticker: str
    company: str
    sections: list[SectionResult] = field(default_factory=list)
    latency_ms: float = 0.0
    generated_at: str = ""

    @property
    def grounded_sections(self) -> int:
        return sum(1 for s in self.sections if s.has_evidence)

    def routing_table(self) -> list[dict[str, Any]]:
        """Section | Provider | Source | Confidence, as the brief asks."""
        return [
            {
                "section": s.title,
                "provider": s.provider_used.value if s.provider_used else "—",
                "source": s.source_category.value if s.source_category else "—",
                "confidence": round(s.confidence, 3),
                "citations": len(s.citations),
                "timestamp": s.timestamp,
            }
            for s in self.sections
        ]

    def provider_mix(self) -> dict[str, int]:
        """How many sections each provider answered.

        The number that would have exposed the defect: scoring answering ten
        of thirteen sections is visible here at a glance.
        """
        mix: dict[str, int] = {}
        for section in self.sections:
            key = section.provider_used.value if section.provider_used else "none"
            mix[key] = mix.get(key, 0) + 1
        return mix

    def as_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "company": self.company,
            "generated_at": self.generated_at,
            "latency_ms": round(self.latency_ms, 1),
            "section_count": len(self.sections),
            "grounded_sections": self.grounded_sections,
            "sections": [s.as_dict() for s in self.sections],
            "routing_table": self.routing_table(),
            "provider_mix": self.provider_mix(),
        }


class ReportOrchestrator:
    """Answers each section from the provider that route assigns to it."""

    def __init__(self, analyst: Any) -> None:
        self.analyst = analyst

    async def run(
        self,
        *,
        sections: list[Section] | None = None,
        question: str = "",
    ) -> OrchestratedReport:
        started = time.perf_counter()
        context = self.analyst.context()
        company = context.name or context.ticker

        wanted = set(sections or [])
        routes = [r for r in ROUTES if not wanted or r.section in wanted]

        results: list[SectionResult] = []
        # Retrieved passages accumulate so the thesis sections can reason over
        # what the earlier sections actually found, rather than re-retrieving.
        gathered: list = list(context.citations)

        for route in routes:
            result = await self._run_section(route, gathered, question)
            results.append(result)
            for citation in result.citations:
                if citation.key not in {c.key for c in gathered}:
                    gathered.append(citation)

        elapsed = (time.perf_counter() - started) * 1000
        report = OrchestratedReport(
            ticker=context.ticker, company=company, sections=results,
            latency_ms=elapsed, generated_at=utc_now(),
        )
        log.info(
            "report orchestrated", ticker=context.ticker,
            sections=len(results), grounded=report.grounded_sections,
            mix=report.provider_mix(), ms=round(elapsed, 1),
        )
        return report

    async def _run_section(
        self, route: SectionRoute, gathered: list, question: str,
    ) -> SectionResult:
        """One section, restricted to the evidence its route permits."""
        started = time.perf_counter()

        # Retrieval runs per section, using the section's own prompt, so the
        # passages fetched for "risks" are about risk rather than whatever the
        # user's opening sentence happened to mention.
        retrieved: list = []
        if any(p in (Provider.RAG, Provider.FILINGS) for p in route.providers):
            retrieved = self.analyst._retrieve(  # noqa: SLF001 — same package
                route.prompt, "chat",
            )

        pool = list(gathered)
        for citation in retrieved:
            if citation.key not in {c.key for c in pool}:
                pool.append(citation)

        selected, provider, attempted = select_evidence(pool, route)

        if not selected:
            # Reported, never omitted: a reader must be able to see that a
            # section was attempted and found nothing, which is itself a
            # finding about the company's disclosure.
            return SectionResult(
                section=route.section, title=route.title,
                content=(
                    f"**No evidence available.** None of "
                    f"{', '.join(p.value for p in route.providers)} holds "
                    f"anything bearing on this section."
                ),
                provider_used=None, source_category=None, confidence=0.0,
                attempted=attempted, timestamp=utc_now(),
            )

        directive = SourceDirective(scope=SourceScope.HYBRID)
        restricted = self.analyst.context().with_citations(selected)
        restricted = restricted.restricted_to(route.kinds)

        content = ""
        try:
            answer = await self.analyst.run(
                "chat", question=route.prompt, source=directive,
                context_override=restricted,
            )
            content = answer.display_content or answer.content
        except TypeError:
            # The analyst does not accept an override on this path; compose
            # from the evidence directly rather than failing the section.
            content = self._compose(route, selected)
        except Exception as exc:  # noqa: BLE001 — one section must not fail the report
            log.warning("section failed", section=route.section.value,
                        error=str(exc)[:160])
            content = self._compose(route, selected)

        return SectionResult(
            section=route.section, title=route.title, content=content,
            provider_used=provider, source_category=self._category(provider, route),
            confidence=score_section(provider, selected),
            citations=selected[:8], attempted=attempted,
            timestamp=utc_now(), evidence_count=len(selected),
        )

    @staticmethod
    def _category(provider: Provider | None, route: SectionRoute) -> SourceCategory:
        if provider in (Provider.RAG, Provider.FILINGS):
            return SourceCategory.ANNUAL_REPORT
        if provider is Provider.MARKET_DATA:
            return SourceCategory.MARKET_DATA
        return route.category if provider else SourceCategory.INTERNAL_DATABASE

    @staticmethod
    def _compose(route: SectionRoute, citations: list) -> str:
        """Deterministic fallback: state the evidence and cite it.

        Used when the provider path is unavailable. Deliberately plain — it
        quotes what the evidence says and marks each claim, rather than
        writing prose the evidence does not support.
        """
        lines = [f"**{route.title}**", ""]
        for citation in citations[:6]:
            value = citation.value
            if isinstance(value, float):
                value = f"{value:,.2f}"
            lines.append(f"- {citation.label}: {value} [{citation.key}]")
        lines.append("")
        lines.append(
            "_Composed directly from the cited evidence; no figure appears "
            "above that is not in the citations._"
        )
        return "\n".join(lines)
