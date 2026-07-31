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

from app.domain.ai.types import Citation, EvidenceKind
from app.domain.calc import safe_div
from app.domain.forecast.assumptions import Scenario
from app.services.analysis_service import AnalysisService
from app.services.forecast.service import ForecastService
from app.services.scoring.service import ScoringService
from app.services.valuation.service import ValuationService


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

        blocks: list[str] = []
        for kind, items in grouped.items():
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
            self._add_documents(context)

        return context

    # -------------------------------------------------------------- sources
    def _add_market(self, context: GroundedContext) -> None:
        company = self.analysis.company
        context.add(Citation(
            key="price", label="Current market price", kind=EvidenceKind.MARKET,
            value=company.current_price, unit="₹", source="Market data",
        ))
        context.add(Citation(
            key="market_cap", label="Market capitalisation", kind=EvidenceKind.MARKET,
            value=company.market_cap, unit="₹ cr", source="Market data",
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
            context.unavailable.append("Uploaded documents (retrieval failed)")
            return

        # "completed" is the post-redesign terminal status; "ready" is the
        # pre-migration spelling, still present in databases upgraded in
        # place. Both mean the document is fully indexed and citable.
        ready = [d for d in documents if d.status in _INDEXED_STATUSES]
        if not ready:
            context.unavailable.append(
                "Uploaded filings, transcripts and rating reports "
                "(no documents have been ingested for this company)"
            )
            return

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

    def _add_statements(self, context: GroundedContext) -> None:
        income = self.analysis.incomes[-1]
        balance = self.analysis.balances[-1]
        cash_flow = self.analysis.cash_flows[-1]
        year = income.fiscal_year
        source_is, source_bs, source_cf = (
            "06 Historical IS", "07 Historical BS", "08 Historical CF"
        )

        rows = [
            ("revenue", "Revenue", income.total_revenue, "₹ cr", source_is),
            ("ebitda", "EBITDA", income.ebitda, "₹ cr", source_is),
            ("ebitda_margin", "EBITDA margin", income.ebitda_margin, "%", source_is),
            ("ebit", "EBIT", income.ebit, "₹ cr", source_is),
            ("pat", "Profit after tax", income.pat, "₹ cr", source_is),
            ("pat_margin", "Net margin", income.pat_margin, "%", source_is),
            ("eps", "EPS (basic)", income.eps_basic, "₹", source_is),
            ("tax_rate", "Effective tax rate", income.effective_tax_rate, "%", source_is),
            ("total_assets", "Total assets", balance.total_assets, "₹ cr", source_bs),
            ("equity", "Shareholders' equity", balance.shareholders_equity, "₹ cr", source_bs),
            ("gross_debt", "Gross debt", balance.gross_debt, "₹ cr", source_bs),
            ("net_debt", "Net debt", balance.net_debt, "₹ cr", source_bs),
            ("cfo", "Cash flow from operations", cash_flow.cfo, "₹ cr", source_cf),
            ("capex", "Capital expenditure", abs(cash_flow.capex), "₹ cr", source_cf),
            ("fcf", "Free cash flow", cash_flow.free_cash_flow, "₹ cr", source_cf),
        ]
        for key, label, value, unit, source in rows:
            context.add(Citation(
                key=key, label=label, kind=EvidenceKind.STATEMENT, value=value,
                unit=unit, source=source, fiscal_year=year,
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
                kind=EvidenceKind.STATEMENT, value=history, unit="₹ cr",
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
        rows = [
            ("forecast_revenue_cagr", "Forecast revenue CAGR", result.revenue_cagr, "%"),
            ("forecast_ebitda_cagr", "Forecast EBITDA CAGR", result.ebitda_cagr, "%"),
            ("terminal_revenue", f"Projected revenue FY+{horizon}",
             terminal.revenue if terminal else None, "₹ cr"),
            ("terminal_ebitda", f"Projected EBITDA FY+{horizon}",
             terminal.ebitda if terminal else None, "₹ cr"),
            ("terminal_eps", f"Projected EPS FY+{horizon}",
             terminal.eps if terminal else None, "₹"),
            ("terminal_fcff", f"Projected FCFF FY+{horizon}",
             terminal.fcff if terminal else None, "₹ cr"),
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

        rows = [
            ("wacc", "WACC", bundle.wacc.wacc, "%", EvidenceKind.VALUATION),
            ("cost_of_equity", "Cost of equity", bundle.wacc.cost_of_equity, "%",
             EvidenceKind.VALUATION),
            ("dcf_value", "DCF intrinsic value per share",
             bundle.dcf_fcff.intrinsic_value_per_share, "₹", EvidenceKind.VALUATION),
            ("dcf_upside", "DCF upside", bundle.dcf_fcff.upside, "%",
             EvidenceKind.VALUATION),
            ("terminal_value_pct", "Terminal value share of EV",
             bundle.dcf_fcff.terminal_value_pct, "%", EvidenceKind.VALUATION),
            ("relative_target", "Blended relative target price",
             bundle.relative.blended_target_price, "₹", EvidenceKind.VALUATION),
            ("pe_ratio", "Trailing P/E", bundle.relative.current.pe, "x",
             EvidenceKind.VALUATION),
            ("ev_ebitda", "EV/EBITDA", bundle.relative.current.ev_ebitda, "x",
             EvidenceKind.VALUATION),
            ("weighted_value", "Weighted intrinsic value",
             bundle.summary.weighted_value, "₹", EvidenceKind.VALUATION),
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
