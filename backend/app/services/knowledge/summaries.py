"""Permanent AI summaries: the memory that makes the platform fast.

Nine summaries per document, generated once and stored forever. A later
question consults these rather than re-parsing a 300-page annual report, which
is the difference between an answer in two seconds and an answer in ninety.

Two properties are deliberate.

**A summary is generated from the document's own chunks, not from the whole
corpus.** Handing the model everything the platform knows would produce a
summary of the company rather than of the filing, and the point of a
per-document summary is that it is attributable to that document.

**A fallback is marked as a fallback.** With no live provider the offline
composer produces deterministic prose from the extracted evidence. That is
useful — it is grounded and cheap — but it is not analysis, and storing it
unmarked would let template text accumulate in the permanent memory
indistinguishable from model output. `is_fallback` exists so a later
regeneration can find and replace exactly those rows.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

import structlog
from sqlalchemy import select

from app.domain.knowledge.vault import SUMMARY_SPECS, SummaryKind
from app.models.document import Document, DocumentChunk
from app.models.knowledge import DocumentSummary

log = structlog.get_logger(__name__)

#: Bumped when the wording of a summary prompt changes materially. A new
#: version writes new rows rather than overwriting, so a prompt regression
#: stays diagnosable by comparing the two.
PROMPT_VERSION = 1

#: Characters of source text handed to the model per summary.
#:
#: Roughly 6,000 tokens — comfortably inside the context window and enough of
#: a filing to summarise honestly. Truncation is by leading chunks, which for
#: an annual report means the narrative sections rather than the note
#: schedules, and that is the right bias for a summary.
MAX_SOURCE_CHARS = 24_000

#: Hard ceiling on a single summary's completion reservation.
#:
#: See SUMMARY-001: providers treat max_tokens as a reservation against the
#: credit balance, so an over-generous ceiling fails the request outright
#: rather than merely allowing a long answer.
MAX_COMPLETION_TOKENS = 900


@dataclass(slots=True)
class SummaryRun:
    documents: int = 0
    generated: int = 0
    skipped_existing: int = 0
    failed: int = 0
    fallbacks: int = 0
    tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    errors: list[dict[str, str]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "documents": self.documents,
            "summaries_generated": self.generated,
            "skipped_existing": self.skipped_existing,
            "failed": self.failed,
            "fallbacks": self.fallbacks,
            "tokens": self.tokens,
            "cost_usd": round(self.cost_usd, 6),
            "latency_ms": round(self.latency_ms, 1),
            "errors": self.errors[:10],
        }


class SummaryService:
    """Generates and stores the nine permanent summaries per document."""

    def __init__(self, db: Any) -> None:
        self.db = db

    # ------------------------------------------------------------- reading
    def existing_kinds(self, document_id: int) -> set[str]:
        rows = self.db.execute(
            select(DocumentSummary.kind).where(
                DocumentSummary.document_id == document_id,
                DocumentSummary.prompt_version == PROMPT_VERSION,
            )
        ).all()
        return {r[0] for r in rows}

    def for_company(
        self, company_id: str, *, kinds: list[SummaryKind] | None = None,
        limit: int = 40,
    ) -> list[DocumentSummary]:
        """Stored summaries for a company, newest period first.

        This is the read path the AI uses *before* RAG.
        """
        query = select(DocumentSummary).where(
            DocumentSummary.company_id == company_id,
            DocumentSummary.prompt_version == PROMPT_VERSION,
        )
        if kinds:
            query = query.where(
                DocumentSummary.kind.in_([k.value for k in kinds])
            )
        return list(self.db.execute(
            query.order_by(
                DocumentSummary.fiscal_year.desc().nullslast(),
                DocumentSummary.document_id.desc(),
            ).limit(limit)
        ).scalars().all())

    def timeline(
        self, company_id: str, kind: SummaryKind,
    ) -> list[dict[str, Any]]:
        """One summary kind across every period the platform holds.

        The basis of "how has management guidance changed over ten years?" —
        answered by reading ten short summaries rather than ten annual
        reports.
        """
        rows = self.db.execute(
            select(DocumentSummary).where(
                DocumentSummary.company_id == company_id,
                DocumentSummary.kind == kind.value,
                DocumentSummary.prompt_version == PROMPT_VERSION,
            ).order_by(DocumentSummary.fiscal_year.asc().nullsfirst())
        ).scalars().all()
        return [
            {
                "fiscal_year": r.fiscal_year, "quarter": r.quarter,
                "doc_type": r.doc_type, "document_id": r.document_id,
                "content": r.content, "is_fallback": r.is_fallback,
                "model": r.model,
            }
            for r in rows
        ]

    # ---------------------------------------------------------- generation
    def _source_text(self, document: Document) -> str:
        chunks = self.db.execute(
            select(DocumentChunk)
            .where(DocumentChunk.document_id == document.id)
            .order_by(DocumentChunk.chunk_index)
        ).scalars().all()
        parts: list[str] = []
        total = 0
        for chunk in chunks:
            text = (chunk.text or "").strip()
            if not text:
                continue
            if total + len(text) > MAX_SOURCE_CHARS:
                break
            parts.append(f"[p.{chunk.page}] {text}")
            total += len(text)
        return "\n\n".join(parts)

    def generate_for_document(
        self, document: Document, *, kinds: list[SummaryKind] | None = None,
        overwrite: bool = False,
    ) -> SummaryRun:
        """Generate and persist the summaries for one document."""
        started = time.perf_counter()
        run = SummaryRun(documents=1)

        wanted = kinds or list(SummaryKind)
        have = set() if overwrite else self.existing_kinds(document.id)

        source = self._source_text(document)
        if not source:
            run.failed += len(wanted)
            run.errors.append({
                "document_id": str(document.id),
                "error": "document has no indexed text to summarise",
            })
            run.latency_ms = (time.perf_counter() - started) * 1000
            return run

        for kind in wanted:
            if kind.value in have:
                run.skipped_existing += 1
                continue
            try:
                self._generate_one(document, kind, source, run)
            except Exception as exc:  # noqa: BLE001 — one summary must not stop the rest
                run.failed += 1
                run.errors.append({
                    "document_id": str(document.id), "kind": kind.value,
                    "error": f"{type(exc).__name__}: {exc}"[:200],
                })
                log.warning("summary generation failed",
                            document_id=document.id, kind=kind.value,
                            error=str(exc)[:160])

        self.db.commit()
        run.latency_ms = (time.perf_counter() - started) * 1000
        return run

    def _generate_one(
        self, document: Document, kind: SummaryKind, source: str,
        run: SummaryRun,
    ) -> None:
        words, instruction = SUMMARY_SPECS[kind]
        prompt = (
            f"{instruction}\n\n"
            f"Target length: about {words} words.\n\n"
            "RULES:\n"
            "- Use only the filing text below. Do not draw on outside "
            "knowledge of this company.\n"
            "- Cite page numbers in square brackets where you state a figure, "
            "e.g. [p.42].\n"
            "- If the filing does not support a section of this summary, say "
            "so plainly rather than filling the gap.\n\n"
            f"FILING TEXT ({document.title or document.filename}):\n{source}"
        )

        content, provider, model, ptok, ctok, cost, fallback = self._complete(
            prompt, words,
        )
        if not content:
            raise RuntimeError("provider returned an empty summary")

        self.db.add(DocumentSummary(
            document_id=document.id,
            company_id=document.company_id,
            kind=kind.value,
            content=content,
            word_count=len(content.split()),
            fiscal_year=document.fiscal_year,
            quarter=document.period,
            doc_type=document.doc_type,
            provider=provider,
            model=model,
            prompt_version=PROMPT_VERSION,
            is_fallback=fallback,
            prompt_tokens=ptok,
            completion_tokens=ctok,
            cost_usd=cost,
        ))
        run.generated += 1
        run.tokens += ptok + ctok
        run.cost_usd += cost
        if fallback:
            run.fallbacks += 1

    def _complete(
        self, prompt: str, words: int,
    ) -> tuple[str, str, str, int, int, float, bool]:
        """One completion through the platform's provider chain."""
        from app.domain.ai.types import CompletionRequest, Message, Role
        from app.services.ai.service import _router

        # SUMMARY-001. `max_tokens` is a *reservation*, not a spend: OpenRouter
        # rejects the whole request with 402 when the ceiling exceeds the
        # affordable balance, even though the reply would cost a fraction of
        # it. Observed on the live key — "you requested up to 1500 tokens, but
        # can only afford 329" — which turned every long summary into a silent
        # fallback to the offline composer while the short one succeeded.
        #
        # Budgeting tightly (1.6 tokens per target word plus a small margin)
        # keeps the reservation near the actual cost, so a summary is refused
        # only when it genuinely cannot be paid for.
        budget = min(MAX_COMPLETION_TOKENS, int(words * 1.6) + 80)
        request = CompletionRequest(
            messages=[
                Message(Role.SYSTEM, (
                    "You are a CFA-qualified equity analyst summarising a "
                    "company filing for an institutional research database. "
                    "Be precise and quantitative. Never state a figure that "
                    "is not in the text you are given."
                )),
                Message(Role.USER, prompt),
            ],
            temperature=0.2,
            max_tokens=budget,
        )

        async def ask():
            return await _router.complete(request)

        try:
            response = asyncio.run(ask())
        except RuntimeError:
            # Already inside an event loop — the job worker may be async.
            # Nested loops are not possible, so this path is skipped rather
            # than deadlocking.
            raise RuntimeError("cannot generate inside a running event loop")

        fallback = (response.provider or "").lower() in ("offline", "mock")
        return (
            (response.content or "").strip(),
            response.provider, response.model,
            response.usage.prompt_tokens, response.usage.completion_tokens,
            response.cost_usd, fallback,
        )

    # ------------------------------------------------------------ batching
    def pending_documents(self, *, limit: int = 10) -> list[Document]:
        """Completed documents with no summaries at the current version."""
        have = {
            r[0] for r in self.db.execute(
                select(DocumentSummary.document_id).where(
                    DocumentSummary.prompt_version == PROMPT_VERSION
                ).distinct()
            ).all()
        }
        rows = self.db.execute(
            select(Document)
            .where(Document.status.in_(("completed", "ready")))
            .order_by(Document.id.desc())
        ).scalars().all()
        return [d for d in rows if d.id not in have][:limit]

    def run_batch(self, *, limit: int = 5,
                  kinds: list[SummaryKind] | None = None) -> SummaryRun:
        totals = SummaryRun()
        started = time.perf_counter()
        for document in self.pending_documents(limit=limit):
            one = self.generate_for_document(document, kinds=kinds)
            totals.documents += 1
            totals.generated += one.generated
            totals.skipped_existing += one.skipped_existing
            totals.failed += one.failed
            totals.fallbacks += one.fallbacks
            totals.tokens += one.tokens
            totals.cost_usd += one.cost_usd
            totals.errors.extend(one.errors)
        totals.latency_ms = (time.perf_counter() - started) * 1000
        log.info("summary batch complete", **{
            k: v for k, v in totals.as_dict().items() if k != "errors"
        })
        return totals
