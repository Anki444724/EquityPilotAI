"""Context builder — the grounding layer.

This is the module that makes the AI an analyst rather than a chatbot. It
harvests figures from the platform's own engines and turns each into a
:class:`Citation` with a stable key. The model is then handed *only* those
figures and instructed to cite the key beside every claim.

The consequence is structural: a number the platform did not compute is not in
the prompt, so the model has nothing to repeat. Fabrication is prevented by
omission rather than by asking the model nicely.

Every citation records its :class:`EvidenceKind`, which is what lets the
guardrail layer distinguish a reported fact from a forecast downstream.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

import structlog

from app.domain.ai.types import Citation, EvidenceKind
from app.domain.calc import safe_div
from app.domain.forecast.assumptions import Scenario
from app.domain.knowledge.temporal import YearObservation
from app.services.analysis_service import AnalysisService
from app.services.forecast.service import ForecastService
from app.services.scoring.overall_score import ScoreResult
from app.services.scoring.service import ScoringService
from app.services.valuation.service import ValuationService

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class GroundedContext:
    """Everything the model is allowed to reason over."""

    company_id: str
    ticker: str
    name: str
    sector: str | None = None
    citations: list[Citation] = field(default_factory=list)
    #: Sections the caller failed to load, reported honestly to the model.
    unavailable: list[str] = field(default_factory=list)
    #: Free-text excerpts from uploaded documents (Module 7 will populate).
    documents: list[tuple[str, str]] = field(default_factory=list)
    #: The ScoreResult behind the scoring citations, kept for consumers that
    #: interpret rather than quote — the Phase 2A deterministic
    #: investment-intelligence engine reads its category narratives and
    #: warning list from here. It is the SAME object whose figures were
    #: published as `overall_score` / `grade` / `score_{category}` citations;
    #: nothing is recomputed. ``None`` when scoring did not run.
    score: ScoreResult | None = None
    #: The domain objects behind the `temporal_timeline` citation — the yearly
    #: observations as generation produced them, read back through
    #: `TemporalMemoryService.observations`. Kept typed for consumers that
    #: interpret the series rather than quote it: the institutional
    #: intelligence engine reads the per-dimension trends and their measured
    #: counterparts here, which the rendered citation does not carry. Empty
    #: when temporal memory holds nothing readable for this company.
    temporal: list[YearObservation] = field(default_factory=list)
    #: Management credibility exactly as `TemporalMemoryService.credibility`
    #: computed it — the same numbers the `management_credibility` citation
    #: renders, so a consumer that interprets them cannot disagree with the
    #: evidence block. ``None`` when temporal memory was not read at all.
    credibility: dict[str, Any] | None = None
    #: Document ids whose pages came from the web-evidence path. Kept on the
    #: context so a source restriction can tell a passage retrieved from a
    #: fetched page apart from a passage retrieved from an upload: retrieval
    #: mints both as ``EvidenceKind.DOCUMENT`` (it sees chunks, not their
    #: provenance), so without this the only place the distinction exists is
    #: here, before the prompt is built.
    web_document_ids: frozenset[int] = frozenset()
    #: Whether `web_document_ids` is the complete answer for this company.
    #: ``False`` means the web pages could not be enumerated — a database
    #: error, or a context built without a document service — and a
    #: documents-only restriction must then withhold document passages rather
    #: than risk presenting a fetched page as an upload. Fail closed: a scope
    #: control that admits material it could not classify is not a control.
    web_ids_complete: bool = False

    def add(self, citation: Citation) -> None:
        if citation.value is not None:
            self.citations.append(citation)

    def with_citations(self, extra: list[Citation]) -> "GroundedContext":
        """A copy carrying `extra` in addition to the computed evidence.

        A copy rather than a mutation: the context is built once per analyst
        and reused across capabilities, so appending one question's retrieved
        passages in place would leak them into every later answer — including
        the citation audit, which would then "verify" markers the next
        question never retrieved.

        Retrieved passages come first so they win the relevance ordering the
        offline provider applies, and duplicates by key are dropped.
        """
        seen = {c.key for c in extra}
        return replace(
            self,
            citations=list(extra) + [c for c in self.citations if c.key not in seen],
        )

    def restricted_to(self, kinds: frozenset[EvidenceKind]) -> "GroundedContext":
        """A copy holding only evidence of the given kinds.

        Used to enforce a source restriction. The inadmissible evidence is
        removed rather than merely marked, so it never reaches the prompt:
        instructing a model to disregard text it can see is a request, not a
        control, and the failure is silent when it is not honoured.

        `documents` is cleared alongside the citations for anything but a
        document scope, since those excerpts are document evidence too.
        """
        withheld = 0
        allowed: list[Citation] = []
        #: A documents-only restriction must not be satisfiable by a passage
        #: whose document the platform fetched from the web. Retrieval labels
        #: every passage DOCUMENT — it has no reason to know where the bytes
        #: came from — so the exclusion is applied here, at the one point that
        #: still knows.
        documents_only = (
            EvidenceKind.DOCUMENT in kinds and EvidenceKind.WEB not in kinds
        )
        for citation in self.citations:
            if citation.kind not in kinds:
                continue
            if documents_only and citation.kind is EvidenceKind.DOCUMENT:
                if citation.web is not None:
                    withheld += 1
                    continue
                if citation.document_id is not None:
                    # A passage, and therefore possibly a fetched page:
                    # retrieval sees chunks, not provenance. Withheld when it
                    # is known to be web, or when it cannot be classified at
                    # all. A citation without a document id is a document
                    # *fact*, which `_add_documents` never draws from a
                    # fetched page, so it is untouched by this rule.
                    if (
                        citation.document_id in self.web_document_ids
                        or not self.web_ids_complete
                    ):
                        withheld += 1
                        continue
            allowed.append(citation)

        keep_docs = EvidenceKind.DOCUMENT in kinds and (
            not documents_only or self.web_ids_complete
        )
        extra_gaps: list[str] = []
        if withheld:
            extra_gaps.append(
                f"{withheld} passage(s) from pages the platform fetched from "
                "the web were excluded: this question was scoped to uploaded "
                "documents."
            )
        elif documents_only and not self.web_ids_complete and self.documents:
            keep_docs = False
            extra_gaps.append(
                "Document passages were withheld: the platform could not "
                "verify which of this company's documents were uploaded "
                "rather than fetched, and this question was scoped to "
                "uploaded documents."
            )
        return replace(
            self,
            citations=allowed,
            documents=list(self.documents) if keep_docs else [],
            # The honest report of what was withheld, and why.
            unavailable=[
                *self.unavailable,
                *([] if len(allowed) == len(self.citations) else [
                    f"{len(self.citations) - len(allowed)} evidence item(s) "
                    "outside the requested source were excluded."
                ]),
                *extra_gaps,
            ],
        )

    def by_kind(self, kind: EvidenceKind) -> list[Citation]:
        return [c for c in self.citations if c.kind is kind]

    def keys(self) -> set[str]:
        return {c.key for c in self.citations}

    def render_evidence(self, kinds: list[EvidenceKind] | None = None) -> str:
        """The evidence block injected into the prompt."""
        selected = [
            c for c in self.citations
            if kinds is None or c.kind in kinds
        ]
        if not selected:
            return "No platform figures are available for this company."

        grouped: dict[str, list[Citation]] = {}
        for citation in selected:
            grouped.setdefault(citation.kind.value, []).append(citation)

        # Structured memory first, raw retrieval last, with the precedence
        # stated rather than implied by position.
        #
        # The brief requires the AI to answer PRIMARILY from the Company Vault
        # and Temporal Memory, falling back to RAG only where knowledge is not
        # yet structured. Ordering alone does not achieve that: a model given
        # eight vault entries and ten raw chunks will happily quote whichever
        # passage reads best. The instruction below is what makes the
        # preference operative, and the ordering is what makes it easy to
        # follow.
        order = {
            EvidenceKind.KNOWLEDGE.value: 0,
            EvidenceKind.STATEMENT.value: 1,
            EvidenceKind.RATIO.value: 2,
            EvidenceKind.FORECAST.value: 3,
            EvidenceKind.MARKET.value: 4,
            EvidenceKind.DOCUMENT.value: 9,      # RAG — the fallback tier
            # Fetched pages, listed after RAG because a page the platform
            # fetched is weaker evidence than a document the company lodged:
            # an uploaded filing always outranks the company's own website.
            # Listed explicitly rather than left to the fallback above: an
            # unlisted kind silently inherits a position, so a new kind added
            # without an entry would render somewhere nobody chose.
            EvidenceKind.WEB.value: 10,
        }
        ordered = sorted(grouped.items(), key=lambda kv: order.get(kv[0], 5))

        blocks: list[str] = []
        has_knowledge = EvidenceKind.KNOWLEDGE.value in grouped
        has_documents = EvidenceKind.DOCUMENT.value in grouped
        has_web = EvidenceKind.WEB.value in grouped
        if has_knowledge and has_documents:
            blocks.append(
                "EVIDENCE PRECEDENCE — the KNOWLEDGE block is the platform's "
                "permanent, versioned memory of this company: assertions it "
                "has already verified, with their sources and confidence. "
                "Answer from it wherever it settles the question. The "
                "DOCUMENT block is unstructured text retrieved for this "
                "question only; use it to fill what memory does not yet "
                "cover, and prefer memory where the two overlap."
            )
        if has_web:
            # Said explicitly because the failure mode is silent: a model
            # handed a company's own marketing page alongside its filings
            # will quote whichever reads better unless told which is which.
            blocks.append(
                "WEB block: pages this platform fetched from sources pinned "
                "to this company (its website, its verified investor-"
                "relations page, exchange and regulator hosts). They are "
                "weaker evidence than uploaded filings and than the "
                "KNOWLEDGE block, and each line states where it came from, "
                "when it was published and when it was retrieved. Cite them "
                "for what the company has said publicly, never as the "
                "audited figure itself."
            )
        for kind, items in ordered:
            blocks.append(f"--- {kind.upper()} ---")
            blocks.extend(item.render() for item in items)
        return "\n".join(blocks)

    def render_gaps(self) -> str:
        if not self.unavailable:
            return ""
        return (
            "UNAVAILABLE — the platform holds no data for the following, and you "
            "must say so plainly rather than estimating:\n"
            + "\n".join(f"- {item}" for item in self.unavailable)
        )


#: Unit codes as stored by Module 7, rendered for the evidence block. Mapping
#: rather than raw codes because "inr_cr" in a prompt invites the model to
#: reproduce it verbatim in prose meant for a human.
#: A document is usable by the analyst only when ingestion finished.
#: "completed" is current; "ready" is the pre-migration spelling, retained so
#: a database upgraded in place keeps serving its existing corpus.
_INDEXED_STATUSES = frozenset({"completed", "ready"})

_DOCUMENT_UNITS: dict[str, str] = {
    "inr_cr": "₹ cr", "inr_lakh": "₹ lakh", "inr_mn": "₹ mn",
    "inr_bn": "₹ bn", "inr": "₹", "percent": "%", "x": "x",
    "years": "years", "months": "months", "count": "", "tco2e": "tCO2e",
    "score": "", "index": "", "yes_no": "", "text": "", "units": "",
    "pct_of_revenue": "% of revenue", "unknown": "",
}


def _as_datetime(value: Any) -> Any:
    """Coerce a stored timestamp to ``datetime``, or ``None``.

    Rows read back through the ORM are already datetimes; a value restored
    from JSON provenance is an ISO string. Both occur, and an unparseable one
    is treated as absent rather than raising inside a prompt build.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _web_source_class(value: Any) -> Any:
    """The stored ``source_class`` as a :class:`WebSourceClass`.

    An unknown or missing value becomes ``UNKNOWN`` — the lowest class — so a
    row written by a newer build cannot be ranked as a first-party source by a
    failed lookup.
    """
    from app.domain.web.types import WebSourceClass

    try:
        return WebSourceClass(str(value or "").strip().lower())
    except ValueError:
        return WebSourceClass.UNKNOWN


def _web_provenance(document: Any) -> Any:
    """Rebuild a :class:`WebProvenance` from a stored document row.

    Reads the ``web`` block written at ingest time and falls back to the
    document's own columns. Returns ``None`` when there is no URL at all: a
    web citation without a URL is unverifiable, and an unverifiable citation
    is not evidence.
    """
    from app.domain.ai.types import WebProvenance

    metadata = getattr(document, "doc_metadata", None) or {}
    web_meta = metadata.get("web") if isinstance(metadata, dict) else None
    if not isinstance(web_meta, dict):
        web_meta = {}

    url = (
        web_meta.get("source_url") or web_meta.get("url")
        or getattr(document, "source_url", None)
    )
    if not url:
        return None

    retrieved_at = _as_datetime(
        web_meta.get("retrieved_at")
        or getattr(document, "retrieved_at", None)
        or getattr(document, "processed_at", None)
        or getattr(document, "created_at", None)
    )
    if retrieved_at is None:
        # No retrieval time is a provenance gap the citation has to admit:
        # the field is not optional because "when did the platform read this"
        # is exactly the question a reader asks of a web citation.
        return None

    return WebProvenance(
        url=str(url),
        title=str(web_meta.get("title") or getattr(document, "title", "") or ""),
        canonical_url=str(web_meta.get("canonical_url") or ""),
        published_at=_as_datetime(
            web_meta.get("published_at")
            or getattr(document, "published_at", None)
        ),
        retrieved_at=retrieved_at,
        content_hash=str(
            web_meta.get("content_hash") or getattr(document, "content_hash", "") or ""
        ),
    )


class ContextBuilder:
    """Harvests citations from every platform engine."""

    #: Caps. A 300-page annual report can yield hundreds of facts; the prompt
    #: has a token budget, and an evidence block the model cannot read is worse
    #: than a shorter one it can.
    MAX_DOCUMENT_FACTS = 40
    MAX_DOCUMENT_EXCERPTS = 8

    def __init__(
        self,
        analysis: AnalysisService,
        forecast_service: ForecastService | None = None,
        valuation_service: ValuationService | None = None,
        scoring_service: ScoringService | None = None,
        document_service=None,
    ) -> None:
        self.analysis = analysis
        self.forecast_service = forecast_service
        self.valuation_service = valuation_service
        self.scoring_service = scoring_service
        #: Module 7. Optional, so the AI layer runs unchanged without it.
        self.document_service = document_service

    # ---------------------------------------------------------------- build
    def build(
        self,
        *,
        include_forecast: bool = True,
        include_valuation: bool = True,
        include_scoring: bool = True,
        include_documents: bool = True,
        horizon: int = 5,
    ) -> GroundedContext:
        company = self.analysis.company
        context = GroundedContext(
            company_id=company.id, ticker=company.ticker,
            name=company.name, sector=company.sector,
        )

        self._add_market(context)
        if self.analysis.has_data:
            self._add_statements(context)
            self._add_ratios(context)
        else:
            context.unavailable.append("Financial statements (no data imported)")

        if include_forecast:
            self._add_forecast(context, horizon)
        if include_valuation:
            self._add_valuation(context, horizon)
        if include_scoring:
            self._add_scoring(context)
        if include_documents:
            # Vault first, then raw document evidence. The order is the whole
            # point of the knowledge engine: a durable, versioned assertion
            # the platform has already distilled is better evidence than the
            # paragraph it came from, and reading it first is what stops every
            # question re-deriving the same conclusions from raw text.
            self._add_knowledge(context)
            self._add_documents(context)
            # Fetched pages are evidence too, but they are not uploads, and
            # the two must not be merged: `_add_documents` excludes them so
            # that a documents-only scope cannot be satisfied by a page the
            # platform fetched (see `restricted_to`).
            self._add_web(context)

        return context

    # -------------------------------------------------------------- sources
    @property
    def unit(self):
        """This company's reporting unit — ₹ cr for NSE, $ M for NASDAQ.

        Phase 3. Every monetary evidence line previously carried a hardcoded
        "₹ cr", which was correct while every company was Indian and became a
        fabrication the moment one was not: Apple's revenue would have been
        presented to the model as "416,161 ₹ cr", and the model would have
        written it up faithfully, because the figure was real and the citation
        resolved. The unit is now read from the company.
        """
        from app.domain.financials.reporting_unit import INR_CRORE

        company = getattr(self.analysis, "company", None)
        return getattr(company, "reporting_unit", None) or INR_CRORE

    def _add_market(self, context: GroundedContext) -> None:
        company = self.analysis.company
        unit = self.unit
        context.add(Citation(
            key="price", label="Current market price", kind=EvidenceKind.MARKET,
            value=company.current_price, unit=unit.per_share,
            source="Market data",
        ))
        context.add(Citation(
            key="market_cap", label="Market capitalisation", kind=EvidenceKind.MARKET,
            value=company.market_cap, unit=unit.money, source="Market data",
        ))

    #: Vault entries admitted to the prompt, highest confidence first.
    #: Enough to carry the company's shape without crowding out the computed
    #: financials, which remain the authority on any figure.
    VAULT_TOP_N = 14
    #: Stored summaries admitted. Two is a brief and an institutional note —
    #: the fastest way to give the model the filing's substance.
    SUMMARY_TOP_N = 2

    def _add_knowledge(self, context: GroundedContext) -> None:
        """Durable knowledge: vault assertions and stored AI summaries.

        This is the read-first memory. Without it every question re-reads the
        corpus; with it the model receives what the platform already concluded,
        each entry still carrying its document, page and confidence so the
        answer remains as auditable as one drawn from raw text.
        """
        company = self.analysis.company
        try:
            from app.domain.knowledge.vault import SummaryKind
            from app.services.knowledge.summaries import SummaryService
            from app.services.knowledge.vault import KnowledgeVault
        except Exception:  # noqa: BLE001 - the vault is optional
            return

        # The builder has no session of its own; the document service carries
        # the one this request is running in.
        session = getattr(getattr(self, "document_service", None), "db", None)
        if session is None:
            return

        try:
            entries = KnowledgeVault(session).read_vault(
                company.id, per_section=4,
            )
        except Exception:  # noqa: BLE001 - never break an answer on the vault
            log.exception("vault read failed", company_id=company.id)
            entries = {}

        flat = [e for section in entries.values() for e in section]
        flat.sort(key=lambda e: -(e.get("confidence") or 0.0))
        for item in flat[:self.VAULT_TOP_N]:
            citation = item.get("citation") or {}
            page = citation.get("page")
            context.add(Citation(
                key=f"vault_{item['section']}_{item['key']}"[:60],
                label=f"[Vault/{item['section']}] {item['label']}",
                kind=EvidenceKind.KNOWLEDGE,
                value=str(item.get("value"))[:600],
                unit=item.get("unit") or "",
                source=(
                    f"Knowledge Vault v{item.get('version')} — "
                    f"{citation.get('doc_type') or 'filing'}"
                    + (f" p.{page}" if page else "")
                ),
                fiscal_year=citation.get("fiscal_year"),
                document_id=citation.get("document_id"),
                page=page,
                confidence=item.get("confidence"),
                snippet=item.get("evidence"),
            ))

        try:
            summaries = SummaryService(session).for_company(
                company.id,
                kinds=[SummaryKind.INSTITUTIONAL, SummaryKind.BRIEF_100],
                limit=self.SUMMARY_TOP_N,
            )
        except Exception:  # noqa: BLE001
            summaries = []

        for summary in summaries:
            context.add(Citation(
                key=f"summary_{summary.kind}_{summary.document_id}"[:60],
                label=f"[Memory/{summary.kind}] "
                      f"FY{summary.fiscal_year or '?'} {summary.doc_type or ''}",
                kind=EvidenceKind.KNOWLEDGE,
                # Whitespace collapsed: the evidence block is parsed line by
                # line, and a multi-line passage is silently dropped. AI-003.
                value=" ".join((summary.content or "").split())[:1200],
                source=f"Stored summary (document {summary.document_id})",
                fiscal_year=summary.fiscal_year,
                document_id=summary.document_id,
                confidence=0.5 if summary.is_fallback else 0.85,
            ))

        self._add_temporal(context, session, company)

    def _add_temporal(self, context: GroundedContext, session, company) -> None:
        """The yearly observation series — the company's temporal memory.

        This is what makes "how has management guidance changed over the last
        ten years?" answerable without re-reading a decade of filings. The
        whole series is added as ONE citation rather than one per year: the
        value of the timeline is the comparison across years, and splitting it
        into ten fragments invites the model to quote a single year and call
        that a trend.

        Fallback observations are excluded. Template prose describing a year
        the model could not actually read would be indistinguishable, in the
        prompt, from analysis — and it would be cited as if it were.
        """
        try:
            from app.services.knowledge.temporal import TemporalMemoryService
        except Exception:  # noqa: BLE001 - temporal memory is optional
            return

        try:
            service = TemporalMemoryService(session)
            rows = [r for r in service.timeline(company.id, limit=12)
                    if not r.is_fallback]
            # The typed series, for consumers that reason over it rather than
            # render it. Read through the service's one parser so the trends
            # and measured counterparts on the context are the same objects
            # the timeline citation was rendered from — a second JSON parser
            # would be a second interpretation of the same column.
            context.temporal = service.observations(
                company.id, limit=12, include_fallback=False,
            )
        except Exception:  # noqa: BLE001 - never break an answer on this
            log.exception("temporal memory read failed", company_id=company.id)
            return

        if not rows:
            return

        rendered = service.render_timeline(company.id, limit=12)
        years = [r.fiscal_year for r in rows]
        # Whitespace is collapsed for the same reason as the summaries above:
        # the evidence block is parsed line by line and a multi-line passage
        # is otherwise silently dropped (AI-003).
        context.add(Citation(
            key="temporal_timeline",
            label=f"[Memory/Timeline] FY{min(years)}-FY{max(years)} "
                  f"yearly observations",
            kind=EvidenceKind.KNOWLEDGE,
            value=" ".join(rendered.split())[:2400],
            source=f"Temporal memory — {len(rows)} verified fiscal years",
            fiscal_year=max(years),
            confidence=round(
                sum(r.confidence or 0 for r in rows) / len(rows), 3,
            ),
        ))

        credibility = service.credibility(company.id)
        context.credibility = credibility
        if credibility.get("score") is not None:
            context.add(Citation(
                key="management_credibility",
                label="[Memory/Credibility] Management delivery record",
                kind=EvidenceKind.KNOWLEDGE,
                value=(
                    f"{credibility['score']:.0%} of guidance delivered across "
                    f"{credibility['years_assessed']} assessable years "
                    f"({credibility['verdicts']})"
                ),
                source="Temporal memory — prior-year guidance verified "
                       "against subsequent filings",
                confidence=0.9,
            ))

    def _add_documents(self, context: GroundedContext) -> None:
        """Harvest evidence from uploaded filings — Module 7's contribution.

        Two kinds of thing are taken. Extracted *facts* become citations like
        any other platform number, so a figure read out of an annual report is
        as auditable as one the DCF computed. Retrieved *passages* become
        document excerpts, which is what lets the model quote management's own
        words rather than paraphrase them from memory.

        Where no documents have been uploaded this records the gap explicitly.
        Saying nothing would let the model treat an empty corpus as evidence of
        absence.
        """
        if self.document_service is None:
            return

        company = self.analysis.company
        try:
            facts = self.document_service.facts(company_id=company.id)
            documents = self.document_service.list_documents(
                company.id, include_superseded=False,
            )
        except Exception:  # pragma: no cover - the AI layer must never 500
            # Logged, not merely recorded as a gap. This branch swallowed a
            # schema error for an entire session — the documents table was
            # behind a migration — and the only visible symptom was the line
            # below appearing in every prompt. A caught exception that leaves
            # no trace in the logs turns a five-minute fix into an
            # investigation of the model's behaviour.
            log.exception(
                "document context unavailable", company_id=company.id,
            )
            context.unavailable.append(
                "Uploaded documents could not be read (platform error, not an "
                "absence of documents)"
            )
            return

        # "completed" is the post-redesign terminal status; "ready" is the
        # pre-migration spelling, still present in databases upgraded in
        # place. Both mean the document is fully indexed and citable.
        # Pages the platform fetched are excluded from this whole path — the
        # facts and the excerpts both — and are served by `_add_web` instead.
        # They are documents in the database, not documents the company
        # uploaded, and letting them into this block would put a fetched page
        # behind a citation the platform labels "uploaded".
        web_ids = self._web_document_ids(documents)
        context.web_document_ids = web_ids
        context.web_ids_complete = True

        uploaded = [d for d in documents if d.id not in web_ids]
        ready = [d for d in uploaded if d.status in _INDEXED_STATUSES]
        if not ready:
            if not uploaded:
                context.unavailable.append(
                    "Uploaded filings, transcripts and rating reports "
                    "(no documents have been ingested for this company)"
                )
            else:
                context.unavailable.append(
                    "Uploaded filings, transcripts and rating reports "
                    "(none of this company's documents finished indexing)"
                )
            return

        facts = [f for f in facts if f.document_id not in web_ids]
        titles = {d.id: (d.title or d.filename) for d in documents}

        # Keep the most confident fact per field so the evidence block is not
        # dominated by one heavily-tabulated document.
        best: dict[str, object] = {}
        for fact in facts:
            current = best.get(fact.field_key)
            if current is None or fact.confidence > current.confidence:
                best[fact.field_key] = fact

        for fact in sorted(
            best.values(), key=lambda f: -f.confidence
        )[: self.MAX_DOCUMENT_FACTS]:
            title = titles.get(fact.document_id, "uploaded document")
            value = fact.value if fact.value is not None else fact.text_value
            if value is None:
                continue
            context.add(Citation(
                key=f"doc_{fact.field_key}"
                    + (f"_{fact.period.lower()}" if fact.period else ""),
                label=fact.label + (f" ({fact.period})" if fact.period else ""),
                kind=EvidenceKind.DOCUMENT,
                value=value,
                unit=_DOCUMENT_UNITS.get(fact.unit, ""),
                source=f"{title} p.{fact.page}",
                fiscal_year=fact.fiscal_year,
            ))

        for document in ready[: self.MAX_DOCUMENT_EXCERPTS]:
            context.documents.append((
                f"{titles.get(document.id, document.filename)}"
                f" ({document.doc_type}, {document.period or 'period unknown'},"
                f" {document.page_count} pages)",
                f"{document.fact_count} extracted fields, "
                f"{document.entity_count} entities, "
                f"coverage {document.coverage:.0%}",
            ))

    #: Fetched pages cited in one prompt. Smaller than the document excerpt
    #: cap on purpose: web evidence is supporting colour, and a handful of
    #: lines is enough to carry what the company has said publicly without
    #: crowding the block the model is supposed to answer from.
    MAX_WEB_EXCERPTS = 6

    @staticmethod
    def _web_document_ids(documents: list[Any]) -> frozenset[int]:
        """Ids of documents that came from the web-evidence path.

        ``doc_type`` is the discriminator rather than the presence of
        ``source_url``: a page is a web page because the ingest path declared
        it one, and a row with a stray URL must not be reclassified as fetched
        evidence on that basis alone.
        """
        from app.domain.documents.types import DocumentType

        return frozenset(
            d.id for d in documents
            if (d.doc_type or "") == DocumentType.WEB_PAGE.value
        )

    def _add_web(self, context: GroundedContext) -> None:
        """Harvest evidence from pages the platform fetched (Part 3 Phase 1).

        Separate from `_add_documents` because the provenance is different in
        the way that matters: an uploaded filing is what the company lodged, a
        fetched page is what the platform read. Each citation carries the URL,
        the class of host it came from, and both timestamps, so a reader can
        open the page and check it — and so the model is told which of its
        evidence is a filing and which is a web page.

        Only indexed pages are cited, following the same rule the document
        path uses: a page still being chunked has no verified text to quote.
        The gap is recorded instead, because "we fetched it and it is not
        ready" is a state a reader should be able to see.
        """
        if self.document_service is None:
            return

        from app.domain.documents.types import DocumentType
        from app.domain.ai.types import WebProvenance
        from app.domain.web.types import source_class_label
        from app.services.web.quality import web_authority

        company = self.analysis.company
        try:
            documents = self.document_service.list_documents(
                company.id, include_superseded=False,
            )
        except Exception:  # pragma: no cover - the AI layer must never 500
            log.exception("web evidence unavailable", company_id=company.id)
            context.unavailable.append(
                "Fetched web pages could not be read (platform error, not an "
                "absence of pages)"
            )
            return

        pages = [
            d for d in documents
            if (d.doc_type or "") == DocumentType.WEB_PAGE.value
        ]
        if not pages:
            # No gap line: a company with no fetched pages is the normal case,
            # and saying so on every prompt would train the reader — and the
            # model — to ignore the UNAVAILABLE block.
            return

        ready = [d for d in pages if d.status in _INDEXED_STATUSES]
        if not ready:
            context.unavailable.append(
                f"{len(pages)} fetched web page(s) are still being indexed "
                "and are not yet citable"
            )
            return

        # Same weight the ingest path assigned, so the order here cannot
        # disagree with the quality ranking — one notion of "this page counts
        # for more", used in both places.
        ranked = sorted(
            ready,
            key=lambda d: (
                -web_authority(
                    _web_source_class(getattr(d, "source_class", None)),
                    published_at=_as_datetime(
                        getattr(d, "published_at", None)
                    ),
                ),
                -(d.retrieved_at.timestamp() if getattr(d, "retrieved_at", None)
                  else 0.0),
                -d.id,
            ),
        )[: self.MAX_WEB_EXCERPTS]

        for document in ranked:
            provenance = _web_provenance(document)
            if provenance is None:
                continue
            label_class = source_class_label(
                getattr(document, "source_class", None)
            )
            title = provenance.title or document.filename
            retrieved = provenance.retrieved_at
            # The host is stated the same way the ingest-side citation states
            # it, so the line the model reads identifies the page it came
            # from. Two pages on one site differ by title, and a title is
            # something the page chooses; the host is not.
            from urllib.parse import urlsplit

            host = (urlsplit(provenance.url).hostname or "").lower()
            source = f"{title} — {label_class}"
            if host:
                source += f" ({host})"
            if provenance.published_at:
                source += f", published {provenance.published_at:%d %b %Y}"
            source += f", retrieved {retrieved:%d %b %Y}" if retrieved else ""
            preview = ""
            metadata = getattr(document, "doc_metadata", None) or {}
            web_meta = metadata.get("web") if isinstance(metadata, dict) else None
            if isinstance(web_meta, dict):
                preview = str(web_meta.get("preview") or "")
            context.add(Citation(
                key=provenance.citation_key(),
                label=f"[{label_class}] {title}",
                kind=EvidenceKind.WEB,
                value=preview or source,
                unit="",
                source=source,
                document_id=document.id,
                confidence=None,
                web=provenance,
            ))

    def _add_statements(self, context: GroundedContext) -> None:
        income = self.analysis.incomes[-1]
        balance = self.analysis.balances[-1]
        cash_flow = self.analysis.cash_flows[-1]
        year = income.fiscal_year
        source_is, source_bs, source_cf = (
            "06 Historical IS", "07 Historical BS", "08 Historical CF"
        )
        money, per_share = self.unit.money, self.unit.per_share

        rows = [
            ("revenue", "Revenue", income.total_revenue, money, source_is),
            ("ebitda", "EBITDA", income.ebitda, money, source_is),
            ("ebitda_margin", "EBITDA margin", income.ebitda_margin, "%", source_is),
            ("ebit", "EBIT", income.ebit, money, source_is),
            ("pat", "Profit after tax", income.pat, money, source_is),
            ("pat_margin", "Net margin", income.pat_margin, "%", source_is),
            ("eps", "EPS (basic)", income.eps_basic, per_share, source_is),
            ("tax_rate", "Effective tax rate", income.effective_tax_rate, "%", source_is),
            ("total_assets", "Total assets", balance.total_assets, money, source_bs),
            ("equity", "Shareholders' equity", balance.shareholders_equity, money, source_bs),
            ("gross_debt", "Gross debt", balance.gross_debt, money, source_bs),
            ("net_debt", "Net debt", balance.net_debt, money, source_bs),
            ("cfo", "Cash flow from operations", cash_flow.cfo, money, source_cf),
            ("capex", "Capital expenditure", abs(cash_flow.capex), money, source_cf),
            ("fcf", "Free cash flow", cash_flow.free_cash_flow, money, source_cf),
        ]
        for key, label, value, unit, source in rows:
            context.add(Citation(
                key=key, label=label, kind=EvidenceKind.STATEMENT, value=value,
                unit=unit, source=source, fiscal_year=year,
            ))

        # Growth citations come from the canonical financial engine's own
        # computation — `income_statement_sections()` is the single place the
        # year-on-year growth is derived, so it is read here rather than
        # re-derived. A second growth formula would be a second source of
        # truth, and the audit cannot tell the two apart.
        for section in self.analysis.statements.income_statement_sections():
            if section.key != "growth":
                continue
            for row in section.rows:
                if row.key in ("revenue_growth", "pat_growth") \
                        and row.values and row.values[-1] is not None:
                    context.add(Citation(
                        key=row.key, label=row.label, kind=EvidenceKind.STATEMENT,
                        value=row.values[-1], unit="%", source=source_is,
                        fiscal_year=year,
                    ))

        # A short history matters: a level without a trend invites the model to
        # infer direction it cannot see.
        if len(self.analysis.incomes) >= 3:
            history = ", ".join(
                f"FY{str(i.fiscal_year)[-2:]} {i.total_revenue:,.0f}"
                for i in self.analysis.incomes[-5:]
            )
            context.add(Citation(
                key="revenue_history", label="Revenue history",
                kind=EvidenceKind.STATEMENT, value=history, unit=money,
                source=source_is,
            ))

    def _add_ratios(self, context: GroundedContext) -> None:
        from app.services.ratios.service import RatioService


        service = RatioService(
            self.analysis.incomes, self.analysis.balances, self.analysis.cash_flows
        )
        wanted = {
            "roe_avg": ("Return on equity", "%"),
            "roce": ("Return on capital employed", "%"),
            "roic": ("Return on invested capital", "%"),
            "current_ratio": ("Current ratio", "x"),
            "net_debt_ebitda": ("Net debt / EBITDA", "x"),
            "interest_coverage": ("Interest coverage", "x"),
            "altman_z": ("Altman Z-score", ""),
        }
        for section in service.all_sections():
            for row in section.rows:
                if row.key in wanted and row.values and row.values[-1] is not None:
                    label, unit = wanted[row.key]
                    context.add(Citation(
                        key=row.key, label=label, kind=EvidenceKind.RATIO,
                        value=row.values[-1], unit=unit,
                        source="10 Ratio Analysis",
                        fiscal_year=self.analysis.incomes[-1].fiscal_year,
                    ))

    def _add_forecast(self, context: GroundedContext, horizon: int) -> None:
        if not self.forecast_service or not self.analysis.has_data:
            context.unavailable.append("Forecast projections")
            return
        try:
            ctx = self.forecast_service.build_context(
                self.analysis.company, self.analysis.statements, years=horizon
            )
            saved = self.forecast_service.active_for_company(self.analysis.company.id)
            result = self.forecast_service.run(ctx, saved, Scenario.BASE)
        except Exception:
            context.unavailable.append("Forecast projections")
            return

        terminal = result.terminal_year
        money, per_share = self.unit.money, self.unit.per_share
        rows = [
            ("forecast_revenue_cagr", "Forecast revenue CAGR", result.revenue_cagr, "%"),
            ("forecast_ebitda_cagr", "Forecast EBITDA CAGR", result.ebitda_cagr, "%"),
            ("terminal_revenue", f"Projected revenue FY+{horizon}",
             terminal.revenue if terminal else None, money),
            ("terminal_ebitda", f"Projected EBITDA FY+{horizon}",
             terminal.ebitda if terminal else None, money),
            ("terminal_eps", f"Projected EPS FY+{horizon}",
             terminal.eps if terminal else None, per_share),
            ("terminal_fcff", f"Projected FCFF FY+{horizon}",
             terminal.fcff if terminal else None, money),
        ]
        for key, label, value, unit in rows:
            context.add(Citation(
                key=key, label=label, kind=EvidenceKind.FORECAST, value=value,
                unit=unit, source="Forecast engine",
            ))

    def _add_valuation(self, context: GroundedContext, horizon: int) -> None:
        if not (self.forecast_service and self.valuation_service and self.analysis.has_data):
            context.unavailable.append("Valuation outputs")
            return
        try:
            bundle = self.valuation_service.value_company(
                self.analysis, self.forecast_service, horizon=horizon
            )
        except Exception:
            context.unavailable.append("Valuation outputs")
            return

        per_share = self.unit.per_share
        rows = [
            ("wacc", "WACC", bundle.wacc.wacc, "%", EvidenceKind.VALUATION),
            ("cost_of_equity", "Cost of equity", bundle.wacc.cost_of_equity, "%",
             EvidenceKind.VALUATION),
            ("dcf_value", "DCF intrinsic value per share",
             bundle.dcf_fcff.intrinsic_value_per_share, per_share, EvidenceKind.VALUATION),
            ("dcf_upside", "DCF upside", bundle.dcf_fcff.upside, "%",
             EvidenceKind.VALUATION),
            ("terminal_value_pct", "Terminal value share of EV",
             bundle.dcf_fcff.terminal_value_pct, "%", EvidenceKind.VALUATION),
            ("relative_target", "Blended relative target price",
             bundle.relative.blended_target_price, per_share, EvidenceKind.VALUATION),
            ("pe_ratio", "Trailing P/E", bundle.relative.current.pe, "x",
             EvidenceKind.VALUATION),
            ("ev_ebitda", "EV/EBITDA", bundle.relative.current.ev_ebitda, "x",
             EvidenceKind.VALUATION),
            ("weighted_value", "Weighted intrinsic value",
             bundle.summary.weighted_value, per_share, EvidenceKind.VALUATION),
            ("valuation_upside", "Upside to intrinsic value",
             bundle.summary.upside, "%", EvidenceKind.VALUATION),
        ]
        for key, label, value, unit, kind in rows:
            context.add(Citation(key=key, label=label, kind=kind, value=value,
                                 unit=unit, source="Valuation engine"))

        context.add(Citation(
            key="valuation_recommendation", label="Valuation recommendation",
            kind=EvidenceKind.VALUATION, value=bundle.summary.recommendation,
            source="Valuation engine",
        ))
        if bundle.quality.is_illustrative:
            context.add(Citation(
                key="data_quality", label="Data quality grade",
                kind=EvidenceKind.VALUATION, value=bundle.quality.grade.value,
                source="Data-quality engine",
            ))

    def _add_scoring(self, context: GroundedContext) -> None:
        if not (self.scoring_service and self.forecast_service
                and self.valuation_service and self.analysis.has_data):
            context.unavailable.append("Institutional score")
            return
        try:
            result = self.scoring_service.score_company(
                self.analysis, self.forecast_service, self.valuation_service
            )
        except Exception:
            context.unavailable.append("Institutional score")
            return

        # Kept for the Phase 2A deterministic engine, which interprets this
        # exact result instead of re-deriving any of it. The citations below
        # remain the audit's source of truth for every figure.
        context.score = result

        context.add(Citation(
            key="overall_score", label="Institutional score",
            kind=EvidenceKind.SCORING, value=result.overall_score, unit="/100",
            source="Scoring engine",
        ))
        context.add(Citation(
            key="grade", label="Institutional grade", kind=EvidenceKind.SCORING,
            value=result.grade, source="Scoring engine",
        ))
        context.add(Citation(
            key="recommendation", label="Scoring recommendation",
            kind=EvidenceKind.SCORING, value=result.recommendation,
            source="Scoring engine",
        ))
        context.add(Citation(
            key="confidence", label="Score confidence", kind=EvidenceKind.SCORING,
            value=result.confidence.confidence, unit="%", source="Scoring engine",
        ))
        for category in result.categories:
            context.add(Citation(
                key=f"score_{category.key}", label=f"{category.label} score",
                kind=EvidenceKind.SCORING, value=category.raw_score, unit="/10",
                source="Scoring engine",
            ))
