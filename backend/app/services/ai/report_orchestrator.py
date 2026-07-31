"""Runs a research report section by section, each from its own provider.

Previously one prompt was answered from one pool of evidence, and the largest
contributor to that pool — the scoring engine — dominated every section. This
runs each section against only the evidence its route permits, so the business
model is answered from the annual report and the institutional score from the
scoring engine, rather than both from whichever block ranked first.
"""
from __future__ import annotations

import asyncio
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
from app.services.ai.pipelines import pipeline_for
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
    #: The market pipeline that governed this report's evidence, so a reader
    #: can see that a US company was served from SEC-first and an Indian one
    #: from annual-reports-first without inferring it from the sources.
    pipeline: dict[str, Any] = field(default_factory=dict)

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

    def timings(self) -> dict[str, Any]:
        """Where the time went.

        `retrieval_ms` and `llm_ms` are *sums of work done*, not elapsed
        wall-clock: sections now run concurrently, so they routinely exceed
        `wall_ms`. That is not an error but the measure of the parallelism —
        the ratio of summed work to elapsed time is the speed-up actually
        achieved, and it is reported as `concurrency_factor` rather than left
        for a reader to infer from two numbers that look contradictory.
        """
        retrieval = sum(s.retrieval_ms for s in self.sections)
        llm = sum(s.llm_ms for s in self.sections)
        work = sum(s.total_ms for s in self.sections)
        wall = self.latency_ms or 1.0
        return {
            "wall_ms": round(self.latency_ms, 1),
            "retrieval_ms_sum": round(retrieval, 1),
            "llm_ms_sum": round(llm, 1),
            "section_work_ms_sum": round(work, 1),
            "overhead_ms_sum": round(
                sum(s.overhead_ms for s in self.sections), 1,
            ),
            "concurrency_factor": round(work / wall, 2),
            "llm_share_of_work": round(llm / work, 3) if work else 0.0,
            "cached_completions": sum(
                1 for s in self.sections if s.cached_completion
            ),
        }

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
            "timings": self.timings(),
            "pipeline": self.pipeline,
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

        # Sections run concurrently within a stage and sequentially between
        # stages. Stage 0 is the six retrieval sections the brief names plus
        # the score sections — none reads another's output. Stage 1 is the
        # thesis sections, which reason over everything stage 0 gathered.
        # Stage 2 is the executive summary, which summarises the verdict too.
        #
        # The merge after each stage is deliberately *ordered by route* rather
        # than by completion. `asyncio.gather` preserves input order in its
        # results, so evidence is folded into `gathered` in the same sequence
        # regardless of which request returned first. Merging in completion
        # order would make the thesis prompts depend on network timing, and
        # two runs of the same report would differ for no visible reason.
        for stage in sorted({r.stage for r in routes}):
            wave = [r for r in routes if r.stage == stage]
            log.info(
                "orchestration stage", stage=stage, sections=len(wave),
                titles=[r.title for r in wave],
            )
            # `gathered` is snapshotted per wave: every section in a wave sees
            # the same evidence pool, so a section cannot be influenced by a
            # sibling that happens to finish first.
            snapshot = list(gathered)
            answered = await asyncio.gather(*(
                self._run_section(route, snapshot, question) for route in wave
            ))
            results.extend(answered)
            seen = {c.key for c in gathered}
            for result in answered:
                for citation in result.citations:
                    if citation.key not in seen:
                        gathered.append(citation)
                        seen.add(citation.key)

        # Execution order is `ROUTES`; the reader's order is
        # `PRESENTATION_ORDER`. The executive summary is written last, because
        # it summarises the rest, and presented first.
        results.sort(key=lambda r: presentation_rank(r.section))

        elapsed = (time.perf_counter() - started) * 1000
        report = OrchestratedReport(
            ticker=context.ticker, company=company, sections=results,
            latency_ms=elapsed, generated_at=utc_now(),
            pipeline=pipeline_for(context.ticker).as_dict(),
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
        retrieval_started = time.perf_counter()
        if any(p in (Provider.RAG, Provider.FILINGS) for p in route.providers):
            retrieved = self.analyst._retrieve(  # noqa: SLF001 — same package
                route.prompt, "chat",
            )
        # Measured separately from the model call so the benchmark can say
        # where the time actually goes. Before this was split out, "the report
        # takes 45 seconds" was the whole of what anyone knew about it.
        retrieval_ms = (time.perf_counter() - retrieval_started) * 1000

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
                retrieval_ms=retrieval_ms,
                total_ms=(time.perf_counter() - started) * 1000,
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
        cached_completion = False
        llm_started = time.perf_counter()
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
            cached_completion = answer.cached
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

        llm_ms = (time.perf_counter() - llm_started) * 1000

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
            retrieval_ms=retrieval_ms, llm_ms=llm_ms,
            total_ms=(time.perf_counter() - started) * 1000,
            cached_completion=cached_completion,
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
