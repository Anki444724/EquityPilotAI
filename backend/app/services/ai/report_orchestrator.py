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
    ROUTES, Provider, Section, SectionResult, SectionRoute, presentation_rank,
    score_section, select_evidence, utc_now,
)
from app.services.ai.section_writer import (
    NO_EVIDENCE, SectionBrief, build_extra, looks_unevidenced,
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
                # The evidence provider and the writer are different things
                # and are reported separately; see `SectionResult`.
                "written_by": s.writer_provider,
                "model": s.writer_model,
            }
            for s in self.sections
        ]

    @property
    def total_tokens(self) -> int:
        return sum(s.total_tokens for s in self.sections)

    @property
    def total_cost_usd(self) -> float:
        return sum(s.cost_usd for s in self.sections)

    def writer_mix(self) -> dict[str, int]:
        """How many sections each *writing* provider produced.

        The figure that shows at a glance whether the platform is serving
        live prose or has quietly degraded to the offline composer — the
        condition Phase 1 exists to end.
        """
        mix: dict[str, int] = {}
        for section in self.sections:
            mix[section.writer_provider] = mix.get(section.writer_provider, 0) + 1
        return mix

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
            "writer_mix": self.writer_mix(),
            "total_tokens": self.total_tokens,
            "total_cost_usd": round(self.total_cost_usd, 6),
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

        # Execution order is `ROUTES`; the reader's order is
        # `PRESENTATION_ORDER`. The executive summary is written last, because
        # it summarises the rest, and presented first.
        results.sort(key=lambda r: presentation_rank(r.section))

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
            # finding about the company's disclosure. No model is called —
            # there is nothing to write from, and asking a model to say
            # nothing is a slower way of getting the same sentence with a
            # fabrication risk attached.
            return SectionResult(
                section=route.section, title=route.title,
                content=NO_EVIDENCE,
                provider_used=None, source_category=None, confidence=0.0,
                attempted=attempted, timestamp=utc_now(),
                writer_provider="none", writer_model="none",
            )

        source_category = self._category(provider, route)
        confidence = score_section(provider, selected)

        directive = SourceDirective(scope=SourceScope.HYBRID)
        restricted = self.analyst.context().with_citations(selected)
        restricted = restricted.restricted_to(route.kinds)

        brief = SectionBrief(
            title=route.title,
            provider=provider.value if provider else "—",
            source=source_category.value if source_category else "—",
            confidence=confidence,
            citations=tuple(selected[:8]),
            ticker=self.analyst.context().ticker,
            company=self.analyst.context().name or self.analyst.context().ticker,
        )

        content = ""
        writer_provider = "none"
        writer_model = "none"
        prompt_tokens = completion_tokens = 0
        cost_usd = 0.0
        try:
            answer = await self.analyst.run(
                "chat", question=route.prompt, source=directive,
                context_override=restricted, extra=build_extra(brief),
                # Retrieval already ran above, against this section's own
                # prompt, and its results are in `selected`. Letting the
                # analyst retrieve again would re-admit document passages to
                # sections whose route excludes them.
                retrieve=False,
            )
            content = (answer.display_content or answer.content or "").strip()
            writer_provider = answer.provider
            writer_model = answer.model
            prompt_tokens = answer.prompt_tokens
            completion_tokens = answer.completion_tokens
            cost_usd = answer.cost_usd
        except TypeError:
            # The analyst does not accept an override on this path; compose
            # from the evidence directly rather than failing the section.
            content = self._compose(route, selected)
            writer_provider = "deterministic-composer"
        except Exception as exc:  # noqa: BLE001 — one section must not fail the report
            log.warning("section failed", section=route.section.value,
                        error=str(exc)[:160])
            content = self._compose(route, selected)
            writer_provider = "deterministic-composer"

        if not content:
            content = self._compose(route, selected)
            writer_provider = "deterministic-composer"

        # A writer that declined for want of evidence must not keep the
        # confidence its provider would have earned. The provider did supply
        # evidence — that is why we got here — but if the model judged it
        # irrelevant to the section, presenting 0.93 confidence beside the
        # sentence "no verified evidence available" is a contradiction the
        # reader would be right to distrust.
        if looks_unevidenced(content):
            confidence = 0.0

        return SectionResult(
            section=route.section, title=route.title, content=content,
            provider_used=provider, source_category=source_category,
            confidence=confidence,
            citations=selected[:8], attempted=attempted,
            timestamp=utc_now(), evidence_count=len(selected),
            writer_provider=writer_provider, writer_model=writer_model,
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
            cost_usd=cost_usd,
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
