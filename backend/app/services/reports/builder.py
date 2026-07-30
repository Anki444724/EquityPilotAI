"""Report builder — composes a `ReportDocument` from platform engines.

This is where "never use static templates" is honoured concretely. Each
`_build_*` method is a **section builder**: it asks the platform for data,
decides whether that data is sufficient, and emits blocks. No method contains a
layout. Report *types* differ only in which builders run, which is a lookup in
`REPORT_SECTIONS`, so a new report type is a row of configuration rather than a
new document.

Two rules govern every builder and are worth stating once:

* **Sufficiency is decided before content is written**, not discovered halfway
  through. A builder that finds its inputs missing calls
  `Section.mark_insufficient()` with a reason and returns. It never emits a
  half-section with blank cells.
* **Every figure is registered as evidence at the moment it is read.** The
  appendix is assembled from what was used, so it cannot drift from the body.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Callable, Sequence

from app.domain.calc import safe_div
from app.domain.reports.blocks import (
    Bullets, Callout, CalloutTone, Chart, ChartKind, CoverMeta, Divider,
    Evidence, EvidenceSource, Heading, Insufficient, KeyValue, MetricGrid,
    PageBreak, Paragraph, Quote, ReportDocument, ReportType, Section,
    SectionKey, Table, Theme, DEFAULT_DISCLAIMER, REPORT_TITLES,
    narratives_for, sections_for,
)
from app.domain.reports.citations import EvidenceRegistry, evidence_by_source

logger = logging.getLogger(__name__)

#: Years of history shown in the financial summary table.
HISTORY_YEARS = 5
#: Forecast years shown. Beyond this the table stops fitting a page and the
#: precision is spurious anyway.
FORECAST_YEARS = 5


def _fmt(value: float | None, places: int = 0, suffix: str = "") -> str:
    """Format a number, or an em dash. Never a zero standing in for unknown."""
    if value is None:
        return "—"
    return f"{value:,.{places}f}{suffix}"


def _pct(value: float | None, places: int = 1) -> str:
    return "—" if value is None else f"{value * 100:.{places}f}%"


def _money(value: float | None, places: int = 0) -> str:
    return "—" if value is None else f"₹{value:,.{places}f}"


def _mult(value: float | None) -> str:
    return "—" if value is None else f"{value:.1f}x"


@dataclass(slots=True)
class ReportInputs:
    """Everything the builder may read. Any field may legitimately be ``None``.

    Assembled once by the service and passed in whole, so a builder cannot
    trigger a second resolution of an engine partway through composition —
    the single-resolution rule that has held since Module 2.
    """

    company: Any
    analysis: Any = None
    ratios: Any = None
    forecast: Any = None
    forecast_scenarios: dict[str, Any] | None = None
    valuation: Any = None
    scoring: Any = None
    peers: Sequence[Any] = ()
    documents: Sequence[Any] = ()
    document_facts: Sequence[Any] = ()
    portfolio: Any = None
    holding: Any = None
    narratives: dict[str, Any] | None = None
    #: Engine failures, so a missing section can say *why* rather than merely
    #: that it is missing.
    errors: dict[str, str] | None = None

    def error_for(self, key: str) -> str:
        return (self.errors or {}).get(key, "")


class ReportBuilder:
    """Builds a `ReportDocument`. One instance per report."""

    def __init__(
        self,
        inputs: ReportInputs,
        report_type: ReportType,
        *,
        theme: Theme = Theme.LIGHT,
        analyst: str = "",
        institution: str = "Institutional Equity Research",
        as_of: date | None = None,
    ) -> None:
        self.inputs = inputs
        self.report_type = report_type
        self.theme = theme
        self.analyst = analyst
        self.institution = institution
        self.as_of = as_of or date.today()
        self.registry = EvidenceRegistry()

    # ==================================================================
    def build(self) -> ReportDocument:
        document = ReportDocument(
            cover=self._cover(),
            theme=self.theme,
            generated_at=datetime.now(timezone.utc),
            disclaimer=DEFAULT_DISCLAIMER,
            provenance=self._provenance(),
        )

        builders: dict[SectionKey, Callable[[], Section | None]] = {
            SectionKey.TOC: self._build_toc,
            SectionKey.EXECUTIVE_SUMMARY: self._build_executive_summary,
            SectionKey.INVESTMENT_THESIS: self._build_investment_thesis,
            SectionKey.BUSINESS_OVERVIEW: self._build_business_overview,
            SectionKey.INDUSTRY_ANALYSIS: self._build_industry,
            SectionKey.FINANCIAL_ANALYSIS: self._build_financial_analysis,
            SectionKey.FORECAST: self._build_forecast,
            SectionKey.VALUATION: self._build_valuation,
            SectionKey.DCF: self._build_dcf,
            SectionKey.RELATIVE_VALUATION: self._build_relative,
            SectionKey.INSTITUTIONAL_SCORE: self._build_score,
            SectionKey.MANAGEMENT: self._build_management,
            SectionKey.MOAT: self._build_moat,
            SectionKey.RISK_ANALYSIS: self._build_risk,
            SectionKey.SCENARIO_ANALYSIS: self._build_scenarios,
            SectionKey.PEER_COMPARISON: self._build_peers,
            SectionKey.PORTFOLIO_FIT: self._build_portfolio_fit,
            SectionKey.APPENDIX: self._build_appendix,
        }

        for key in sections_for(self.report_type):
            builder = builders.get(key)
            if builder is None:
                continue  # COVER is the document's own metadata
            try:
                document.add(builder())
            except Exception as exc:  # pragma: no cover - resilience path
                # One failing section must not lose the whole report. The
                # failure is rendered in place, which is both honest and far
                # easier to diagnose than an empty document.
                logger.exception("section %s failed", key.value)
                document.add(
                    Section(key, key.value.replace("_", " ").title())
                    .mark_insufficient(
                        f"This section could not be generated: "
                        f"{type(exc).__name__}."
                    )
                )
        return document

    # ==================================================================
    # Cover and provenance
    # ==================================================================
    def _cover(self) -> CoverMeta:
        company = self.inputs.company
        summary = getattr(self.inputs.valuation, "summary", None)
        scoring = self.inputs.scoring

        target = getattr(summary, "weighted_value", None)
        price = getattr(company, "current_price", None)
        quality = getattr(self.inputs.valuation, "quality", None)
        grade = getattr(getattr(quality, "grade", None), "value", None)

        warning = None
        if grade in {"unreliable", "illustrative"}:
            warning = (
                "Illustrative valuation only. Real filings are required for "
                "investment-grade outputs."
            )
            if grade == "unreliable":
                # A valuation the engine has disowned must not appear on the
                # cover as a target price. Module 8 learned this the hard way.
                target = None

        return CoverMeta(
            company_name=company.name,
            ticker=company.ticker,
            report_type=self.report_type,
            title=REPORT_TITLES[self.report_type],
            subtitle=f"{company.name} ({company.ticker})",
            as_of=self.as_of,
            analyst=self.analyst,
            institution=self.institution,
            exchange=getattr(company, "exchange", "") or "",
            sector=getattr(company, "sector", None),
            industry=getattr(company, "industry", None),
            recommendation=getattr(scoring, "recommendation", None),
            target_price=target,
            current_price=price,
            upside=safe_div(target - price, price) if target and price else None,
            rating=getattr(scoring, "grade", None),
            score=getattr(scoring, "overall_score", None),
            market_cap=getattr(company, "market_cap", None),
            data_warning=warning,
        )

    def _provenance(self) -> dict[str, str]:
        out: dict[str, str] = {
            "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "report_type": self.report_type.value,
        }
        quality = getattr(self.inputs.valuation, "quality", None)
        if quality is not None:
            grade = getattr(getattr(quality, "grade", None), "value", None)
            if grade:
                out["valuation_data_grade"] = grade
        if self.inputs.scoring is not None:
            out["scoring_profile"] = getattr(
                self.inputs.scoring, "profile_label", ""
            )
        narratives = self.inputs.narratives or {}
        if narratives:
            first = next(iter(narratives.values()))
            out["ai_provider"] = getattr(first, "provider", "offline")
        for key, message in (self.inputs.errors or {}).items():
            out[f"error_{key}"] = message
        return out

    # ==================================================================
    # Helpers
    # ==================================================================
    def _cite(
        self, key: str, label: str, source: EvidenceSource,
        value: float | str | None = None, unit: str = "", detail: str = "",
        fiscal_year: int | None = None,
    ) -> Evidence:
        return self.registry.add(
            key, label, source, value, unit, detail, fiscal_year
        )

    def _narrative(self, capability: str) -> Any | None:
        return (self.inputs.narratives or {}).get(capability)

    def _narrative_blocks(
        self, capability: str, *, heading: str | None = None
    ) -> list[Any]:
        """Render an AI narrative as blocks, with its own citations attached.

        The narrative's citations become report evidence under an `ai_` prefix,
        so a claim traceable to the AI layer is distinguishable in the appendix
        from one the financial engine produced directly.
        """
        result = self._narrative(capability)
        if result is None or not getattr(result, "content", "").strip():
            return []

        evidence: list[Evidence] = []
        for citation in getattr(result, "citations", [])[:24]:
            key = f"ai_{citation.key}"
            if key not in self.registry:
                try:
                    evidence.append(self._cite(
                        key, citation.label, EvidenceSource.AI,
                        citation.value, getattr(citation, "unit", ""),
                        getattr(citation, "source", ""),
                    ))
                except ValueError:
                    continue
            else:
                found = self.registry.get(key)
                if found is not None:
                    evidence.append(found)

        blocks: list[Any] = []
        if heading:
            blocks.append(Heading(heading, level=3))
        # The AI layer emits markdown-ish prose; paragraphs are split on blank
        # lines and bullet runs are gathered so they render as real lists.
        text = getattr(result, "display_content", None) or result.content
        for chunk in _split_prose(text):
            if chunk["kind"] == "bullets":
                blocks.append(Bullets(chunk["items"], evidence=evidence))
            else:
                blocks.append(Paragraph(chunk["text"], evidence=evidence))
        return blocks

    def _history(self) -> tuple[list[Any], list[Any], list[Any]]:
        analysis = self.inputs.analysis
        if analysis is None or not getattr(analysis, "has_data", False):
            return [], [], []
        return (
            list(analysis.incomes)[-HISTORY_YEARS:],
            list(analysis.balances)[-HISTORY_YEARS:],
            list(analysis.cash_flows)[-HISTORY_YEARS:],
        )

    # ==================================================================
    # Sections
    # ==================================================================
    def _build_toc(self) -> Section | None:
        """A placeholder the renderers fill once pagination is known.

        Page numbers do not exist until the document is laid out, so the table
        of contents is emitted as a marker section and each renderer resolves
        it. Building it here with guessed numbers would be worse than useless.
        """
        section = Section(SectionKey.TOC, "Contents")
        section.add(Paragraph("__TOC__"))
        return section

    # ------------------------------------------------------ executive
    def _build_executive_summary(self) -> Section:
        section = Section(SectionKey.EXECUTIVE_SUMMARY, "Executive Summary")
        company = self.inputs.company
        scoring = self.inputs.scoring
        summary = getattr(self.inputs.valuation, "summary", None)
        incomes, _, _ = self._history()

        if scoring is None and summary is None and not incomes:
            return section.mark_insufficient(
                "No financial statements, valuation or score are available for "
                f"{company.ticker}."
            )

        metrics: list[tuple[str, str, str]] = []
        evidence: list[Evidence] = []

        price = getattr(company, "current_price", None)
        if price is not None:
            evidence.append(self._cite(
                "price", "Current market price", EvidenceSource.MARKET,
                price, "₹",
            ))
            metrics.append(("Current price", _money(price, 2), company.ticker))

        if summary is not None and summary.weighted_value is not None:
            evidence.append(self._cite(
                "target_price", "Blended target price",
                EvidenceSource.VALUATION, summary.weighted_value, "₹",
                "weighted across methodologies",
            ))
            metrics.append((
                "Target price", _money(summary.weighted_value, 2),
                "blended, all methods",
            ))
            if summary.upside is not None:
                evidence.append(self._cite(
                    "upside", "Upside to target", EvidenceSource.VALUATION,
                    summary.upside, "%",
                ))
                metrics.append((
                    "Upside", _pct(summary.upside), "to blended target",
                ))

        if scoring is not None:
            evidence.append(self._cite(
                "score", "Institutional score", EvidenceSource.SCORING,
                scoring.overall_score, "/100", scoring.profile_label,
            ))
            evidence.append(self._cite(
                "rating", "Institutional rating", EvidenceSource.SCORING,
                scoring.grade,
            ))
            metrics.append(("Score", f"{scoring.overall_score:.1f}", "of 100"))
            metrics.append(("Rating", scoring.grade, scoring.recommendation))

        if metrics:
            section.add(MetricGrid(metrics, evidence=evidence))

        if scoring is not None:
            tone = (
                CalloutTone.POSITIVE
                if scoring.overall_score >= 65 else
                CalloutTone.WARNING if scoring.overall_score >= 50
                else CalloutTone.NEGATIVE
            )
            section.add(Callout(
                f"Recommendation — {scoring.recommendation}",
                f"{scoring.recommendation_rationale} "
                f"Conviction is {scoring.conviction.lower()} on an institutional "
                f"score of {scoring.overall_score:.1f} [score] and a rating of "
                f"{scoring.grade} [rating].",
                tone,
                evidence=self.registry.many("score", "rating"),
            ))

        section.extend(self._narrative_blocks("business_summary"))

        if summary is not None and getattr(summary, "recommendation", None):
            quality = getattr(self.inputs.valuation, "quality", None)
            disclosure = getattr(quality, "disclosure", None)
            if disclosure:
                section.add(Callout(
                    "Data quality", disclosure, CalloutTone.WARNING,
                ))
        return section

    # -------------------------------------------------------- thesis
    def _build_investment_thesis(self) -> Section:
        section = Section(SectionKey.INVESTMENT_THESIS, "Investment Thesis")
        thesis = self._narrative("investment_thesis")
        bull = self._narrative("bull_case")
        bear = self._narrative("bear_case")

        if thesis is None and bull is None and bear is None:
            return section.mark_insufficient(
                "The AI layer produced no thesis narrative for this company. "
                + (self.inputs.error_for("narratives") or
                   "No grounded evidence was available to reason over.")
            )

        section.extend(self._narrative_blocks("investment_thesis"))
        if bull is not None:
            section.extend(self._narrative_blocks("bull_case", heading="Bull case"))
        if bear is not None:
            section.extend(self._narrative_blocks("bear_case", heading="Bear case"))
        return section

    # ------------------------------------------------------ business
    def _build_business_overview(self) -> Section:
        section = Section(SectionKey.BUSINESS_OVERVIEW, "Business Overview")
        company = self.inputs.company
        pairs: list[tuple[str, str]] = [
            ("Company", company.name),
            ("Ticker", f"{company.ticker} · {getattr(company, 'exchange', '')}"),
            ("Sector", company.sector or "—"),
            ("Industry", company.industry or "—"),
        ]
        if getattr(company, "market_cap", None):
            self._cite(
                "market_cap", "Market capitalisation", EvidenceSource.MARKET,
                company.market_cap, "₹ cr",
            )
            pairs.append(("Market capitalisation", f"₹{company.market_cap:,.0f} cr"))
        if getattr(company, "shares_outstanding", None):
            pairs.append(("Shares outstanding", f"{company.shares_outstanding:,.2f} cr"))

        section.add(KeyValue(pairs, evidence=self.registry.many("market_cap")))

        if getattr(company, "description", None):
            section.add(Paragraph(company.description))

        # Entities extracted by Module 7, where a document has been ingested.
        segments = [
            e for e in self.inputs.documents
            if getattr(e, "kind", "") in {"segment", "product"}
        ]
        if segments:
            section.add(Heading("Segments and products", level=3))
            section.add(Bullets(
                [getattr(e, "name", "") for e in segments[:8]],
                evidence=[self._cite(
                    "doc_entities", "Entities extracted from filings",
                    EvidenceSource.DOCUMENT, len(segments), "entities",
                )],
            ))

        if len(section.blocks) <= 1 and not getattr(company, "description", None):
            return section.mark_insufficient(
                "No business description or segment disclosure is on file. "
                "Upload an annual report to populate this section."
            )
        return section

    def _build_industry(self) -> Section:
        section = Section(SectionKey.INDUSTRY_ANALYSIS, "Industry Analysis")
        peers = list(self.inputs.peers)
        company = self.inputs.company
        if not peers:
            return section.mark_insufficient(
                f"No peer set is available for the {company.sector or 'sector'} "
                "sector, so industry position cannot be assessed."
            )

        caps = [getattr(p, "market_cap", None) for p in peers]
        valid = [c for c in caps if c]
        total = sum(valid) if valid else None
        own = getattr(company, "market_cap", None)

        section.add(Paragraph(
            f"{company.name} competes in {company.industry or company.sector} "
            f"alongside {len(peers)} companies under platform coverage."
            + (
                f" It represents {_pct(safe_div(own, total + (own or 0)))} of the "
                f"covered sector by market capitalisation [market_cap]."
                if total and own else ""
            ),
            evidence=self.registry.many("market_cap"),
        ))
        section.add(Table(
            ["Company", "Ticker", "Market cap (₹ cr)"],
            [
                [p.name, p.ticker, _fmt(getattr(p, "market_cap", None))]
                for p in sorted(
                    peers, key=lambda p: -(getattr(p, "market_cap", 0) or 0)
                )[:12]
            ],
            caption="Sector coverage universe",
            evidence=[self._cite(
                "peer_count", "Peers under coverage", EvidenceSource.FINANCIAL,
                len(peers), "companies",
            )],
        ))
        return section

    # ----------------------------------------------------- financials
    def _build_financial_analysis(self) -> Section:
        section = Section(SectionKey.FINANCIAL_ANALYSIS, "Financial Analysis")
        incomes, balances, cash_flows = self._history()
        if not incomes:
            return section.mark_insufficient(
                "No historical financial statements have been imported for "
                f"{self.inputs.company.ticker}."
            )

        years = [f"FY{str(i.fiscal_year)[-2:]}" for i in incomes]
        latest = incomes[-1]

        self._cite(
            "revenue", "Revenue", EvidenceSource.FINANCIAL,
            latest.total_revenue, "₹ cr", "latest reported",
            latest.fiscal_year,
        )
        self._cite(
            "ebitda", "EBITDA", EvidenceSource.FINANCIAL, latest.ebitda,
            "₹ cr", fiscal_year=latest.fiscal_year,
        )
        self._cite(
            "ebitda_margin", "EBITDA margin", EvidenceSource.FINANCIAL,
            latest.ebitda_margin, "%", fiscal_year=latest.fiscal_year,
        )
        self._cite(
            "pat", "Profit after tax", EvidenceSource.FINANCIAL, latest.pat,
            "₹ cr", fiscal_year=latest.fiscal_year,
        )
        core = self.registry.many("revenue", "ebitda", "ebitda_margin", "pat")

        section.add(Paragraph(
            f"Revenue in FY{str(latest.fiscal_year)[-2:]} was "
            f"₹{latest.total_revenue:,.0f} crore [revenue], with EBITDA of "
            f"₹{latest.ebitda:,.0f} crore [ebitda] at a margin of "
            f"{_pct(latest.ebitda_margin)} [ebitda_margin]. Profit after tax "
            f"was ₹{latest.pat:,.0f} crore [pat].",
            evidence=core,
        ))

        # The workbook's `31 IC Report` section 7 row set.
        rows: list[list[str]] = [
            ["Revenue"] + [_fmt(i.total_revenue) for i in incomes],
            ["EBITDA"] + [_fmt(i.ebitda) for i in incomes],
            ["EBITDA margin"] + [_pct(i.ebitda_margin) for i in incomes],
            ["PAT"] + [_fmt(i.pat) for i in incomes],
            ["EPS (₹)"] + [_fmt(i.eps_basic, 2) for i in incomes],
        ]
        if balances:
            rows.append(
                ["Shareholders' equity"] + [_fmt(b.shareholders_equity) for b in balances]
            )
            rows.append(["Net debt"] + [_fmt(b.net_debt) for b in balances])
        if cash_flows:
            rows.append(["Cash from operations"] + [_fmt(c.cfo) for c in cash_flows])
            rows.append(["Free cash flow"] + [_fmt(c.free_cash_flow) for c in cash_flows])

        section.add(Table(
            ["₹ crore"] + years, rows,
            caption="Key financial summary", emphasis_rows=[0],
            evidence=core,
        ))

        section.add(Chart(
            ChartKind.REVENUE, "Revenue and EBITDA",
            labels=years,
            series=[
                ("Revenue", [i.total_revenue for i in incomes]),
                ("EBITDA", [i.ebitda for i in incomes]),
            ],
            y_unit="₹ cr", evidence=core,
        ))
        section.add(Chart(
            ChartKind.MARGINS, "Margin trend",
            labels=years,
            series=[
                ("EBITDA margin", [i.ebitda_margin for i in incomes]),
                ("Net margin", [i.pat_margin for i in incomes]),
            ],
            y_unit="%", evidence=core,
        ))
        if cash_flows:
            section.add(Chart(
                ChartKind.CASH_FLOW, "Cash generation",
                labels=[f"FY{str(c.fiscal_year)[-2:]}" for c in cash_flows],
                series=[
                    ("Operating cash flow", [c.cfo for c in cash_flows]),
                    ("Free cash flow", [c.free_cash_flow for c in cash_flows]),
                ],
                y_unit="₹ cr",
            ))

        ratios = self.inputs.ratios
        if ratios:
            section.extend(self._ratio_blocks(ratios))
        return section

    def _ratio_blocks(self, ratios: Any) -> list[Any]:
        """Ratio sections rendered as tables, whatever shape the service returns."""
        blocks: list[Any] = []
        sections = getattr(ratios, "sections", None) or []
        for group in sections[:4]:
            rows = []
            for metric in getattr(group, "metrics", [])[:10]:
                values = getattr(metric, "values", None) or []
                latest = next(
                    (v for v in reversed(values) if v is not None), None
                )
                if latest is None:
                    continue
                unit = str(getattr(getattr(metric, "unit", ""), "value", ""))
                formatted = (
                    _pct(latest) if unit == "percent"
                    else _mult(latest) if unit == "multiple"
                    else _fmt(latest, 2)
                )
                rows.append([metric.label, formatted])
            if rows:
                blocks.append(Table(
                    [getattr(group, "label", "Ratio"), "Latest"], rows,
                    evidence=[self._cite(
                        f"ratios_{getattr(group, 'key', 'group')}",
                        f"{getattr(group, 'label', 'Ratios')} (computed)",
                        EvidenceSource.FINANCIAL, len(rows), "metrics",
                    )],
                ))
        return blocks

    # ------------------------------------------------------- forecast
    def _build_forecast(self) -> Section:
        section = Section(SectionKey.FORECAST, "Forecast")
        forecast = self.inputs.forecast
        years = list(getattr(forecast, "years", []) or [])[:FORECAST_YEARS]
        if not years:
            return section.mark_insufficient(
                "No forecast could be produced. "
                + (self.inputs.error_for("forecast")
                   or "The forecast engine needs at least three years of history.")
            )

        labels = [f"FY{str(y.fiscal_year)[-2:]}" for y in years]
        self._cite(
            "forecast_horizon", "Forecast horizon", EvidenceSource.FORECAST,
            len(years), "years",
        )
        converged = getattr(forecast, "debt_converged", None)
        if converged is not None:
            self._cite(
                "forecast_converged", "Circular debt solver converged",
                EvidenceSource.FORECAST, "yes" if converged else "no",
                detail=f"{getattr(forecast, 'debt_iterations', 0)} iterations",
            )

        section.add(Paragraph(
            f"The base-case forecast projects {len(years)} years "
            f"[forecast_horizon] from the last reported year, driven by the "
            f"assumptions listed in the appendix.",
            evidence=self.registry.many("forecast_horizon"),
        ))

        rows = [
            ["Revenue"] + [_fmt(getattr(y, "revenue", None)) for y in years],
            ["EBITDA"] + [_fmt(getattr(y, "ebitda", None)) for y in years],
            ["EBITDA margin"] + [_pct(getattr(y, "ebitda_margin", None)) for y in years],
            ["PAT"] + [_fmt(getattr(y, "pat", None)) for y in years],
            ["Free cash flow"] + [_fmt(getattr(y, "fcff", None)) for y in years],
        ]
        section.add(Table(
            ["₹ crore"] + labels, rows, caption="Base-case projection",
            emphasis_rows=[0],
            evidence=self.registry.many("forecast_horizon"),
        ))
        section.add(Chart(
            ChartKind.PAT, "Projected revenue and profit",
            labels=labels,
            series=[
                ("Revenue", [getattr(y, "revenue", None) for y in years]),
                ("PAT", [getattr(y, "pat", None) for y in years]),
            ],
            y_unit="₹ cr",
        ))
        if converged is False:
            section.add(Callout(
                "Forecast did not fully converge",
                "The circular debt solver did not reach its tolerance. Treat "
                "the leverage and interest lines with caution.",
                CalloutTone.WARNING,
                evidence=self.registry.many("forecast_converged"),
            ))
        return section

    # ------------------------------------------------------ valuation
    def _build_valuation(self) -> Section:
        section = Section(SectionKey.VALUATION, "Valuation")
        bundle = self.inputs.valuation
        summary = getattr(bundle, "summary", None)
        if summary is None:
            return section.mark_insufficient(
                "No valuation could be produced. "
                + (self.inputs.error_for("valuation")
                   or "The valuation engine requires history and a forecast.")
            )

        quality = getattr(bundle, "quality", None)
        grade = getattr(getattr(quality, "grade", None), "value", None)
        self._cite(
            "valuation_grade", "Valuation data grade",
            EvidenceSource.VALUATION, grade or "unknown",
        )

        if grade == "unreliable":
            # State the conclusion the engine reached rather than printing a
            # number it has declared unusable.
            section.add(Callout(
                "Valuation withheld",
                "The data-quality gate graded this valuation unreliable, so no "
                "target price is published. The methodology table below is "
                "shown for transparency, not for use.",
                CalloutTone.NEGATIVE,
                evidence=self.registry.many("valuation_grade"),
            ))
        elif getattr(quality, "disclosure", None):
            section.add(Callout(
                "Data quality", quality.disclosure, CalloutTone.WARNING,
                evidence=self.registry.many("valuation_grade"),
            ))

        rows = []
        for method in getattr(summary, "methods", []):
            value = getattr(method, "value_per_share", None)
            if value is None:
                value = getattr(method, "value", None)
            rows.append([
                getattr(method, "label", getattr(method, "key", "")),
                _money(value, 2),
                _pct(getattr(method, "weight", None)),
                getattr(method, "note", "") or "",
            ])
        if rows:
            section.add(Table(
                ["Methodology", "Value / share", "Weight", "Comment"], rows,
                caption="Cross-method valuation",
                evidence=self.registry.many("valuation_grade"),
            ))

        if grade != "unreliable" and summary.weighted_value is not None:
            metrics = [
                ("Blended value", _money(summary.weighted_value, 2), "weighted"),
                ("Median", _money(summary.median_value, 2), "across methods"),
                ("Range", f"{_money(summary.low, 0)}–{_money(summary.high, 0)}",
                 "low to high"),
                ("Upside", _pct(summary.upside), "to blended"),
            ]
            section.add(MetricGrid(
                metrics, evidence=self.registry.many("target_price", "upside"),
            ))
            if getattr(summary, "maximum_buy_price", None):
                self._cite(
                    "max_buy", "Maximum buy price", EvidenceSource.VALUATION,
                    summary.maximum_buy_price, "₹",
                    f"after a {_pct(summary.margin_of_safety)} margin of safety",
                )
                section.add(Paragraph(
                    f"Applying a {_pct(summary.margin_of_safety)} margin of "
                    f"safety gives a maximum buy price of "
                    f"{_money(summary.maximum_buy_price, 2)} [max_buy].",
                    evidence=self.registry.many("max_buy"),
                ))

        section.extend(self._narrative_blocks("valuation_commentary"))
        return section

    def _build_dcf(self) -> Section:
        section = Section(SectionKey.DCF, "Discounted Cash Flow")
        bundle = self.inputs.valuation
        dcf = getattr(bundle, "dcf_fcff", None)
        wacc = getattr(bundle, "wacc", None)
        if dcf is None:
            return section.mark_insufficient(
                "No discounted cash-flow model is available for this company."
            )

        if wacc is not None:
            self._cite(
                "wacc", "Weighted average cost of capital",
                EvidenceSource.VALUATION, getattr(wacc, "wacc", None), "%",
            )
        self._cite(
            "ev", "Enterprise value", EvidenceSource.VALUATION,
            dcf.enterprise_value, "₹ cr",
        )
        self._cite(
            "tv_share", "Terminal value share of EV",
            EvidenceSource.VALUATION, dcf.terminal_value_pct, "%",
        )
        evidence = self.registry.many("wacc", "ev", "tv_share")

        section.add(MetricGrid([
            ("WACC", _pct(getattr(wacc, "wacc", None), 2), "discount rate"),
            ("Enterprise value", _money(dcf.enterprise_value), "₹ cr"),
            ("Equity value", _money(dcf.equity_value), "₹ cr"),
            ("Value / share", _money(dcf.intrinsic_value_per_share, 2),
             dcf.convention.replace("_", " ")),
        ], evidence=evidence))

        years = list(getattr(dcf, "years", []))[:FORECAST_YEARS]
        if years:
            section.add(Table(
                ["Year", "FCFF (₹ cr)", "Discount factor", "PV (₹ cr)"],
                [
                    [
                        f"FY{str(getattr(y, 'fiscal_year', ''))[-2:]}",
                        _fmt(getattr(y, "cash_flow", None)),
                        _fmt(getattr(y, "discount_factor", None), 3),
                        _fmt(getattr(y, "present_value", None)),
                    ]
                    for y in years
                ],
                caption="Explicit forecast period",
                evidence=evidence,
            ))
            section.add(Chart(
                ChartKind.DCF, "Present value of projected cash flows",
                labels=[f"FY{str(getattr(y, 'fiscal_year', ''))[-2:]}" for y in years],
                series=[
                    ("FCFF", [getattr(y, "cash_flow", None) for y in years]),
                    ("Present value", [getattr(y, "present_value", None) for y in years]),
                ],
                y_unit="₹ cr", evidence=evidence,
            ))

        if dcf.terminal_value_pct is not None:
            tone = (
                CalloutTone.WARNING if dcf.terminal_value_pct > 0.75
                else CalloutTone.NEUTRAL
            )
            section.add(Callout(
                "Terminal value dependence",
                f"The terminal value is {_pct(dcf.terminal_value_pct)} of "
                f"enterprise value [tv_share]."
                + (
                    " Above three quarters, the valuation rests more on the "
                    "perpetuity assumption than on the forecast period."
                    if dcf.terminal_value_pct > 0.75 else ""
                ),
                tone, evidence=self.registry.many("tv_share"),
            ))

        section.extend(self._narrative_blocks("dcf_interpretation"))
        return section

    def _build_relative(self) -> Section:
        section = Section(SectionKey.RELATIVE_VALUATION, "Relative Valuation")
        relative = getattr(self.inputs.valuation, "relative", None)
        if relative is None:
            return section.mark_insufficient(
                "No relative valuation is available; peer multiples could not "
                "be assembled."
            )

        current = getattr(relative, "current", None)
        if current is not None:
            rows = []
            for label, attr in (
                ("P/E", "pe"), ("EV/EBITDA", "ev_ebitda"), ("P/B", "pb"),
                ("EV/Sales", "ev_sales"), ("P/S", "ps"),
            ):
                value = getattr(current, attr, None)
                if value is not None:
                    rows.append([label, _mult(value)])
            if rows:
                section.add(Table(
                    ["Multiple", "Current"], rows,
                    caption="Trading multiples",
                    evidence=[self._cite(
                        "multiples", "Current trading multiples",
                        EvidenceSource.VALUATION, len(rows), "multiples",
                    )],
                ))

        methods = list(getattr(relative, "methods", []))
        if methods:
            section.add(Table(
                ["Method", "Target price", "Basis"],
                [
                    [
                        getattr(m, "label", ""),
                        _money(getattr(m, "target_price", None), 2),
                        getattr(m, "basis", "") or "",
                    ]
                    for m in methods
                ],
                caption="Multiple-based target prices",
            ))

        if getattr(relative, "blended_target_price", None) is not None:
            self._cite(
                "relative_target", "Relative blended target",
                EvidenceSource.VALUATION, relative.blended_target_price, "₹",
            )
            section.add(Paragraph(
                f"Blending the multiple-based methods gives a target of "
                f"{_money(relative.blended_target_price, 2)} "
                f"[relative_target], against a range of "
                f"{_money(getattr(relative, 'target_low', None), 2)} to "
                f"{_money(getattr(relative, 'target_high', None), 2)}.",
                evidence=self.registry.many("relative_target"),
            ))
        return section

    # ---------------------------------------------------------- score
    def _build_score(self) -> Section:
        section = Section(SectionKey.INSTITUTIONAL_SCORE, "Institutional Score")
        scoring = self.inputs.scoring
        if scoring is None:
            return section.mark_insufficient(
                "The scoring engine produced no result. "
                + (self.inputs.error_for("scoring") or
                   "Scoring requires imported financial statements.")
            )

        confidence = getattr(scoring, "confidence", None)
        self._cite(
            "score_confidence", "Scoring confidence", EvidenceSource.SCORING,
            getattr(confidence, "score", None), "%",
        )
        section.add(MetricGrid([
            ("Overall score", f"{scoring.overall_score:.1f}", "of 100"),
            ("Rating", scoring.grade, scoring.grade_description),
            ("Recommendation", scoring.recommendation, scoring.conviction),
            ("Confidence", _pct(getattr(confidence, "score", None)),
             "data completeness"),
        ], evidence=self.registry.many("score", "rating", "score_confidence")))

        categories = list(getattr(scoring, "categories", []))
        if categories:
            section.add(Table(
                ["Category", "Score", "Weight", "Contribution"],
                [
                    [
                        c.label, f"{c.raw_score:.1f} / 10", _pct(c.weight),
                        f"{c.weighted_score:.2f}",
                    ]
                    for c in categories
                ],
                caption="Score by category",
                evidence=self.registry.many("score"),
            ))
            section.add(Chart(
                ChartKind.SCORE_RADAR, "Score profile",
                labels=[c.label for c in categories],
                series=[("Score", [c.raw_score for c in categories])],
                y_unit="/10", evidence=self.registry.many("score"),
            ))

        strongest = list(getattr(scoring, "strongest", []))
        weakest = list(getattr(scoring, "weakest", []))
        if strongest:
            section.add(Heading("Strongest categories", level=3))
            section.add(Bullets(strongest))
        if weakest:
            section.add(Heading("Weakest categories", level=3))
            section.add(Bullets(weakest))

        section.extend(self._narrative_blocks("scoring_explanation"))
        return section

    # ----------------------------------------------------- qualitative
    def _build_management(self) -> Section:
        section = Section(SectionKey.MANAGEMENT, "Management")
        blocks = self._narrative_blocks("management_analysis")
        directors = [
            e for e in self.inputs.documents
            if getattr(e, "kind", "") in {"director", "promoter", "auditor"}
        ]
        if not blocks and not directors:
            return section.mark_insufficient(
                "No management disclosure has been ingested and the AI layer "
                "produced no management narrative."
            )
        if directors:
            section.add(Table(
                ["Name", "Role", "Confidence", "Source"],
                [
                    [
                        getattr(d, "name", ""),
                        getattr(d, "kind", "").replace("_", " ").title(),
                        _pct(getattr(d, "confidence", None)),
                        f"page {getattr(d, 'page', '—')}",
                    ]
                    for d in directors[:12]
                ],
                caption="Named individuals extracted from filings",
                evidence=[self._cite(
                    "doc_people", "People extracted from filings",
                    EvidenceSource.DOCUMENT, len(directors), "individuals",
                )],
            ))
        section.extend(blocks)
        section.extend(self._narrative_blocks("capital_allocation",
                                              heading="Capital allocation"))
        return section

    def _build_moat(self) -> Section:
        section = Section(SectionKey.MOAT, "Economic Moat")
        blocks = self._narrative_blocks("moat_analysis")
        if not blocks:
            return section.mark_insufficient(
                "No moat assessment is available; the competitive-advantage "
                "inputs are unpopulated."
            )
        section.extend(blocks)
        return section

    # ----------------------------------------------------------- risk
    def _build_risk(self) -> Section:
        section = Section(SectionKey.RISK_ANALYSIS, "Risk Analysis")
        blocks = self._narrative_blocks("risk_analysis")
        risks = [
            e for e in self.inputs.documents if getattr(e, "kind", "") == "risk"
        ]
        scoring = self.inputs.scoring
        warnings = list(getattr(scoring, "warnings", []) or [])

        if not blocks and not risks and not warnings:
            return section.mark_insufficient(
                "No risk factors have been extracted from filings and no "
                "scoring warnings were raised."
            )

        section.extend(blocks)
        if risks:
            section.add(Heading("Risk factors disclosed in filings", level=3))
            section.add(Bullets(
                [getattr(r, "name", "")[:280] for r in risks[:8]],
                evidence=[self._cite(
                    "doc_risks", "Risk factors extracted from filings",
                    EvidenceSource.DOCUMENT, len(risks), "factors",
                )],
            ))
        if warnings:
            section.add(Heading("Platform warnings", level=3))
            section.add(Bullets(
                warnings[:10],
                evidence=self.registry.many("score"),
            ))
        return section

    def _build_scenarios(self) -> Section:
        section = Section(SectionKey.SCENARIO_ANALYSIS, "Scenario Analysis")
        bundle = self.inputs.valuation
        values = dict(getattr(bundle, "scenario_values", {}) or {})
        if not any(v is not None for v in values.values()):
            return section.mark_insufficient(
                "Bull and bear scenarios could not be valued; the scenario "
                "engine produced no differentiated outputs."
            )

        price = getattr(self.inputs.company, "current_price", None)
        rows = []
        for case in ("bear", "base", "bull"):
            value = values.get(case)
            if value is None:
                continue
            self._cite(
                f"scenario_{case}", f"{case.title()} case value",
                EvidenceSource.VALUATION, value, "₹",
            )
            rows.append([
                case.title(), _money(value, 2),
                _pct(safe_div(value - price, price)) if price else "—",
            ])
        section.add(Table(
            ["Scenario", "Value / share", "Upside"], rows,
            caption="Scenario valuation",
            emphasis_rows=[1] if len(rows) > 1 else [],
            evidence=self.registry.many(
                "scenario_bear", "scenario_base", "scenario_bull"
            ),
        ))

        sensitivity = getattr(bundle, "sensitivity", None)
        if sensitivity is not None and getattr(sensitivity, "grid", None):
            section.add(Chart(
                ChartKind.SENSITIVITY, "Sensitivity to WACC and terminal growth",
                labels=[str(c) for c in getattr(sensitivity, "columns", [])],
                row_labels=[str(r) for r in getattr(sensitivity, "rows", [])],
                matrix=getattr(sensitivity, "grid", []),
                y_unit="₹",
            ))
        return section

    def _build_peers(self) -> Section:
        section = Section(SectionKey.PEER_COMPARISON, "Peer Comparison")
        peers = list(self.inputs.peers)
        if not peers:
            return section.mark_insufficient(
                "No peer companies are under coverage in this sector."
            )

        company = self.inputs.company
        rows = []
        labels: list[str] = []
        caps: list[float | None] = []
        for peer in sorted(
            peers, key=lambda p: -(getattr(p, "market_cap", 0) or 0)
        )[:10]:
            is_self = peer.ticker == company.ticker
            rows.append([
                f"{peer.name}{' ◂' if is_self else ''}",
                peer.ticker,
                _fmt(getattr(peer, "market_cap", None)),
                _money(getattr(peer, "current_price", None), 2),
            ])
            labels.append(peer.ticker)
            caps.append(getattr(peer, "market_cap", None))

        section.add(Table(
            ["Company", "Ticker", "Market cap (₹ cr)", "Price"], rows,
            caption="Sector peers by market capitalisation",
            evidence=[self._cite(
                "peer_set", "Peer set", EvidenceSource.FINANCIAL,
                len(peers), "companies", company.sector or "",
            )],
        ))
        section.add(Chart(
            ChartKind.PEER_COMPARISON, "Peer market capitalisation",
            labels=labels, series=[("Market cap", caps)], y_unit="₹ cr",
            evidence=self.registry.many("peer_set"),
        ))
        section.extend(self._narrative_blocks("peer_comparison"))
        return section

    def _build_portfolio_fit(self) -> Section:
        section = Section(SectionKey.PORTFOLIO_FIT, "Portfolio Fit")
        holding = self.inputs.holding
        portfolio = self.inputs.portfolio
        if portfolio is None:
            return section.mark_insufficient(
                "No portfolio was supplied, so position sizing and fit cannot "
                "be assessed."
            )

        summary = getattr(portfolio, "summary", None)
        pairs = [
            ("Portfolio", getattr(summary, "name", "—")),
            ("Total value", _money(getattr(summary, "total_value", None))),
            ("Positions", str(getattr(summary, "position_count", "—"))),
        ]
        if holding is not None:
            self._cite(
                "position_weight", "Current position weight",
                EvidenceSource.PORTFOLIO, getattr(holding, "weight", None), "%",
            )
            self._cite(
                "position_cap", "Maximum position size",
                EvidenceSource.PORTFOLIO,
                getattr(holding, "max_position_size", None), "%",
                "implied by institutional rating",
            )
            pairs.extend([
                ("Current weight", _pct(getattr(holding, "weight", None))),
                ("Maximum weight", _pct(getattr(holding, "max_position_size", None))),
                ("Unrealised P&L", _money(
                    getattr(getattr(holding, "position", None), "unrealised_pnl", None)
                )),
            ])
        section.add(KeyValue(
            pairs, evidence=self.registry.many("position_weight", "position_cap"),
        ))

        if holding is not None and getattr(holding, "is_oversized", False):
            section.add(Callout(
                "Position exceeds its policy ceiling",
                f"The holding is {_pct(holding.weight)} [position_weight] of "
                f"the book against a ceiling of "
                f"{_pct(holding.max_position_size)} [position_cap] implied by "
                f"its institutional rating.",
                CalloutTone.WARNING,
                evidence=self.registry.many("position_weight", "position_cap"),
            ))
        elif holding is None:
            section.add(Paragraph(
                "The company is not currently held in this portfolio. The "
                "rating-implied ceiling would apply on initiation."
            ))
        section.extend(self._narrative_blocks("portfolio_commentary"))
        return section

    # ------------------------------------------------------- appendix
    def _build_appendix(self) -> Section:
        section = Section(SectionKey.APPENDIX, "Appendix")

        section.add(Heading("Evidence and sources", level=3))
        grouped = evidence_by_source(self.registry.all())
        if grouped:
            rows = []
            for source, entries in sorted(
                grouped.items(), key=lambda kv: kv[0].value
            ):
                for entry in entries:
                    rows.append([
                        entry.key,
                        entry.label,
                        source.value.replace("_", " ").title(),
                        (
                            f"{entry.value:,.2f}"
                            if isinstance(entry.value, float)
                            else str(entry.value) if entry.value is not None
                            else "—"
                        ),
                    ])
            section.add(Table(
                ["Reference", "Description", "Engine", "Value"], rows,
                caption="Every figure cited in this report",
            ))
        else:
            section.add(Insufficient(
                "No evidence was registered — this report contains no cited "
                "figures."
            ))

        forecast = self.inputs.forecast
        assumptions = getattr(forecast, "assumptions", None)
        if assumptions is not None:
            rows = []
            for name in (
                "revenue_growth", "ebitda_margin", "tax_rate",
                "capex_pct_revenue", "terminal_growth", "wacc",
            ):
                value = getattr(assumptions, name, None)
                if value is not None and isinstance(value, (int, float)):
                    rows.append([
                        name.replace("_", " ").title(), _pct(float(value)),
                    ])
            if rows:
                section.add(Heading("Forecast assumptions", level=3))
                section.add(Table(["Assumption", "Value"], rows))

        if self.inputs.documents:
            section.add(Heading("Documents referenced", level=3))
            titles = sorted({
                getattr(d, "document_title", None) or getattr(d, "name", "")
                for d in self.inputs.documents
            })
            section.add(Bullets([t for t in titles if t][:12]))

        section.add(Heading("Disclaimer", level=3))
        section.add(Paragraph(DEFAULT_DISCLAIMER))
        return section


# ---------------------------------------------------------------------------
def _split_prose(text: str) -> list[dict]:
    """Split AI prose into paragraph and bullet-run chunks.

    The AI layer emits light markdown. Rendering it verbatim would put literal
    asterisks in a PDF, and joining bullets into a paragraph produces the
    run-on that Module 8's commentary had to fix. This splits once, here, and
    every renderer receives structured blocks.
    """
    chunks: list[dict] = []
    bullets: list[str] = []

    def flush() -> None:
        if bullets:
            chunks.append({"kind": "bullets", "items": list(bullets)})
            bullets.clear()

    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            flush()
            continue
        if line.startswith(("- ", "* ", "• ")):
            bullets.append(_clean_markdown(line[2:].strip()))
            continue
        flush()
        cleaned = _clean_markdown(line)
        if cleaned:
            chunks.append({"kind": "paragraph", "text": cleaned})
    flush()
    return chunks


def _clean_markdown(text: str) -> str:
    """Strip heading hashes and bold markers, keeping citation markers intact."""
    stripped = text.lstrip("#").strip()
    return stripped.replace("**", "").replace("__", "")
