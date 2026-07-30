"""Block-tree serialisation.

A report is stored as its block tree, not only as rendered bytes. That is what
lets a user ask for a DOCX of a report generated last month as a PDF, and get
*that* report rather than a fresh one built from today's numbers.

The round trip must be lossless, and a test asserts it by rebuilding a document
and comparing the rendered Markdown byte for byte. Serialisation that silently
drops a field would produce a re-render that differs from the original in ways
nobody notices until a client compares two copies.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from app.domain.reports.blocks import (
    Block, BlockKind, Bullets, Callout, CalloutTone, Chart, ChartKind,
    CitationList, CoverMeta, Divider, Evidence, EvidenceSource, Heading,
    Insufficient, KeyValue, MetricGrid, PageBreak, Paragraph, Quote,
    ReportDocument, ReportType, Section, SectionKey, Table, Theme,
)

SCHEMA_VERSION = 1


def _iso(value: date | datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def _datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


# ---------------------------------------------------------------------------
def evidence_to_dict(evidence: Evidence) -> dict[str, Any]:
    return {
        "key": evidence.key, "label": evidence.label,
        "source": evidence.source.value, "value": evidence.value,
        "unit": evidence.unit, "detail": evidence.detail,
        "fiscal_year": evidence.fiscal_year,
    }


def evidence_from_dict(payload: dict[str, Any]) -> Evidence:
    return Evidence(
        key=payload["key"], label=payload["label"],
        source=EvidenceSource(payload["source"]), value=payload.get("value"),
        unit=payload.get("unit", ""), detail=payload.get("detail", ""),
        fiscal_year=payload.get("fiscal_year"),
    )


def block_to_dict(block: Block) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "kind": block.kind.value,
        "evidence": [evidence_to_dict(e) for e in block.evidence],
        "note": block.note,
    }
    kind = block.kind

    if kind is BlockKind.HEADING:
        payload.update(text=block.text, level=block.level)
    elif kind is BlockKind.PARAGRAPH:
        payload.update(text=block.text)
    elif kind is BlockKind.BULLETS:
        payload.update(items=list(block.items), ordered=block.ordered)
    elif kind is BlockKind.KEY_VALUE:
        payload.update(
            pairs=[list(p) for p in block.pairs], columns=block.columns,
        )
    elif kind is BlockKind.TABLE:
        payload.update(
            header=list(block.header), rows=[list(r) for r in block.rows],
            align=list(block.align), caption=block.caption,
            emphasis_rows=sorted(block.emphasis_rows),
        )
    elif kind is BlockKind.METRIC_GRID:
        payload.update(
            metrics=[list(m) for m in block.metrics], columns=block.columns,
        )
    elif kind is BlockKind.CHART:
        payload.update(
            chart_kind=block.chart_kind.value, title=block.title,
            labels=list(block.labels),
            series=[[name, list(values)] for name, values in block.series],
            secondary=sorted(block.secondary), y_unit=block.y_unit,
            matrix=[list(r) for r in block.matrix],
            row_labels=list(block.row_labels),
        )
    elif kind is BlockKind.CALLOUT:
        payload.update(
            title=block.title, text=block.text, tone=block.tone.value,
        )
    elif kind is BlockKind.QUOTE:
        payload.update(text=block.text, attribution=block.attribution)
    elif kind is BlockKind.INSUFFICIENT:
        payload.update(reason=block.reason)
    elif kind is BlockKind.CITATION_LIST:
        payload.update(entries=[evidence_to_dict(e) for e in block.entries])
    return payload


def block_from_dict(payload: dict[str, Any]) -> Block:
    kind = BlockKind(payload["kind"])
    evidence = [evidence_from_dict(e) for e in payload.get("evidence", [])]
    note = payload.get("note", "")
    common = {"evidence": evidence, "note": note}

    if kind is BlockKind.HEADING:
        return Heading(payload["text"], payload.get("level", 2), **common)
    if kind is BlockKind.PARAGRAPH:
        return Paragraph(payload["text"], **common)
    if kind is BlockKind.BULLETS:
        return Bullets(
            payload.get("items", []), payload.get("ordered", False), **common,
        )
    if kind is BlockKind.KEY_VALUE:
        return KeyValue(
            [tuple(p) for p in payload.get("pairs", [])],
            payload.get("columns", 2), **common,
        )
    if kind is BlockKind.TABLE:
        return Table(
            payload.get("header", []), payload.get("rows", []),
            align=payload.get("align") or None,
            caption=payload.get("caption", ""),
            emphasis_rows=payload.get("emphasis_rows", []), **common,
        )
    if kind is BlockKind.METRIC_GRID:
        return MetricGrid(
            [tuple(m) for m in payload.get("metrics", [])],
            payload.get("columns", 4), **common,
        )
    if kind is BlockKind.CHART:
        return Chart(
            ChartKind(payload["chart_kind"]), payload.get("title", ""),
            labels=payload.get("labels", []),
            series=[(n, v) for n, v in payload.get("series", [])],
            secondary=payload.get("secondary", []),
            y_unit=payload.get("y_unit", ""),
            matrix=payload.get("matrix", []),
            row_labels=payload.get("row_labels", []), **common,
        )
    if kind is BlockKind.CALLOUT:
        return Callout(
            payload.get("title", ""), payload.get("text", ""),
            CalloutTone(payload.get("tone", "neutral")), **common,
        )
    if kind is BlockKind.QUOTE:
        return Quote(
            payload.get("text", ""), payload.get("attribution", ""), **common,
        )
    if kind is BlockKind.DIVIDER:
        return Divider(**common)
    if kind is BlockKind.PAGE_BREAK:
        return PageBreak(**common)
    if kind is BlockKind.INSUFFICIENT:
        return Insufficient(payload.get("reason", ""), **common)
    if kind is BlockKind.CITATION_LIST:
        return CitationList(
            [evidence_from_dict(e) for e in payload.get("entries", [])],
            **common,
        )
    raise ValueError(f"unknown block kind '{payload['kind']}'")


def section_to_dict(section: Section) -> dict[str, Any]:
    return {
        "key": section.key.value, "title": section.title,
        "sufficient": section.sufficient, "reason": section.reason,
        "optional": section.optional,
        "blocks": [block_to_dict(b) for b in section.blocks],
    }


def section_from_dict(payload: dict[str, Any]) -> Section:
    section = Section(
        key=SectionKey(payload["key"]), title=payload["title"],
        sufficient=payload.get("sufficient", True),
        reason=payload.get("reason", ""),
        optional=payload.get("optional", False),
    )
    section.blocks = [block_from_dict(b) for b in payload.get("blocks", [])]
    return section


def cover_to_dict(cover: CoverMeta) -> dict[str, Any]:
    return {
        "company_name": cover.company_name, "ticker": cover.ticker,
        "report_type": cover.report_type.value, "title": cover.title,
        "subtitle": cover.subtitle, "as_of": _iso(cover.as_of),
        "analyst": cover.analyst, "institution": cover.institution,
        "exchange": cover.exchange, "sector": cover.sector,
        "industry": cover.industry, "recommendation": cover.recommendation,
        "target_price": cover.target_price,
        "current_price": cover.current_price, "upside": cover.upside,
        "rating": cover.rating, "score": cover.score,
        "market_cap": cover.market_cap, "currency": cover.currency,
        "data_warning": cover.data_warning,
    }


def cover_from_dict(payload: dict[str, Any]) -> CoverMeta:
    return CoverMeta(
        company_name=payload["company_name"], ticker=payload["ticker"],
        report_type=ReportType(payload["report_type"]), title=payload["title"],
        subtitle=payload.get("subtitle", ""), as_of=_date(payload.get("as_of")),
        analyst=payload.get("analyst", ""),
        institution=payload.get("institution", ""),
        exchange=payload.get("exchange", ""), sector=payload.get("sector"),
        industry=payload.get("industry"),
        recommendation=payload.get("recommendation"),
        target_price=payload.get("target_price"),
        current_price=payload.get("current_price"),
        upside=payload.get("upside"), rating=payload.get("rating"),
        score=payload.get("score"), market_cap=payload.get("market_cap"),
        currency=payload.get("currency", "INR"),
        data_warning=payload.get("data_warning"),
    )


def document_to_dict(document: ReportDocument) -> dict[str, Any]:
    return {
        "schema": SCHEMA_VERSION,
        "cover": cover_to_dict(document.cover),
        "theme": document.theme.value,
        "generated_at": _iso(document.generated_at),
        "version": document.version,
        "provenance": dict(document.provenance),
        "disclaimer": document.disclaimer,
        "sections": [section_to_dict(s) for s in document.sections],
    }


def document_from_dict(payload: dict[str, Any]) -> ReportDocument:
    schema = payload.get("schema", SCHEMA_VERSION)
    if schema > SCHEMA_VERSION:
        # Refuse rather than silently dropping fields a newer writer added.
        raise ValueError(
            f"report schema v{schema} is newer than this build supports "
            f"(v{SCHEMA_VERSION})"
        )
    document = ReportDocument(
        cover=cover_from_dict(payload["cover"]),
        theme=Theme(payload.get("theme", "light")),
        generated_at=_datetime(payload.get("generated_at")),
        version=payload.get("version", 1),
        provenance=dict(payload.get("provenance", {})),
        disclaimer=payload.get("disclaimer", ""),
    )
    document.sections = [
        section_from_dict(s) for s in payload.get("sections", [])
    ]
    return document
