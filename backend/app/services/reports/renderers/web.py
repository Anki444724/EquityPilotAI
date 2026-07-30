"""HTML and Markdown renderers.

HTML doubles as the in-app preview and a print target: the stylesheet carries
`@media print` rules with `@page` margins and running page numbers, so a user
can print from the browser and get essentially the PDF layout. Charts are
embedded as base64 data URIs so the file is a single self-contained artefact
with no asset directory to lose.

Markdown is the plain-text fallback — useful for diffing two versions of a
report, pasting into a note, or feeding back into the AI layer.
"""
from __future__ import annotations

import base64
import html
import time

from app.domain.reports.blocks import (
    Block, BlockKind, CalloutTone, ReportDocument, SectionKey, Theme,
)
from app.domain.reports.citations import evidence_by_source, strip_markers
from app.services.reports.renderers.base import (
    OutputFormat, RenderResult, ReportRenderer, cover_pairs, register,
    toc_entries,
)

TONE_CLASS = {
    CalloutTone.POSITIVE: "positive",
    CalloutTone.NEGATIVE: "negative",
    CalloutTone.WARNING: "warning",
    CalloutTone.NEUTRAL: "neutral",
}


def _slug(text: str) -> str:
    return "".join(
        c if c.isalnum() else "-" for c in text.lower()
    ).strip("-")


def _e(text: str) -> str:
    return html.escape(str(text), quote=True)


@register
class HtmlRenderer(ReportRenderer):
    """Renders a report to a self-contained HTML document."""

    fmt = OutputFormat.HTML

    def render(self, document: ReportDocument) -> RenderResult:
        started = time.perf_counter()
        engine = self.chart_engine(document.theme)
        evidence = document.evidence()
        dark = document.theme is Theme.DARK

        parts: list[str] = [
            "<!doctype html>",
            '<html lang="en">',
            "<head>",
            '<meta charset="utf-8">',
            '<meta name="viewport" content="width=device-width, initial-scale=1">',
            f"<title>{_e(document.cover.company_name)} — "
            f"{_e(document.cover.title)}</title>",
            f"<style>{_STYLESHEET}</style>",
            "</head>",
            f'<body class="{"dark" if dark else "light"}">',
            '<main class="report">',
        ]
        parts.append(self._cover(document))

        for section in document.ordered():
            if section.key is SectionKey.COVER:
                continue
            if section.key is SectionKey.TOC:
                parts.append(self._toc(document))
                continue
            parts.append(f'<section id="{_slug(section.title)}" class="sec">')
            parts.append(f"<h1>{_e(section.title)}</h1>")
            for block in section.blocks:
                parts.append(self._block(block, engine, evidence))
            parts.append("</section>")

        parts.extend([
            f'<footer class="disclaimer">{_e(document.disclaimer)}</footer>',
            "</main></body></html>",
        ])
        payload = "\n".join(parts).encode("utf-8")
        return RenderResult(
            payload=payload, fmt=self.fmt,
            filename=self.filename(document, self.fmt),
            took_ms=round((time.perf_counter() - started) * 1000, 2),
        )

    # ------------------------------------------------------------------
    def _cover(self, document) -> str:
        cover = document.cover
        rows = "".join(
            f"<tr><th>{_e(k)}</th><td>{_e(v)}</td></tr>"
            for k, v in cover_pairs(document)
        )
        warning = (
            f'<div class="callout warning"><strong>Data quality</strong>'
            f"<p>{_e(cover.data_warning)}</p></div>"
            if cover.data_warning else ""
        )
        return (
            '<header class="cover">'
            f'<div class="eyebrow">{_e(cover.institution.upper())}</div>'
            f"<h1>{_e(cover.company_name)}</h1>"
            f'<div class="subtitle">{_e(cover.ticker)} · {_e(cover.title)}</div>'
            f'<table class="cover-table">{rows}</table>'
            f"{warning}"
            "</header>"
        )

    @staticmethod
    def _toc(document) -> str:
        items = "".join(
            f'<li><a href="#{_slug(title)}">{_e(title)}</a></li>'
            for _, title in toc_entries(document)
        )
        return (
            '<nav class="sec toc" id="contents"><h1>Contents</h1>'
            f"<ol>{items}</ol></nav>"
        )

    def _block(self, block: Block, engine, evidence) -> str:
        kind = block.kind

        if kind is BlockKind.HEADING:
            level = min(4, max(2, block.level))
            return f"<h{level}>{_e(block.text)}</h{level}>"

        if kind is BlockKind.PARAGRAPH:
            return f"<p>{self._inline(block.text, evidence)}</p>"

        if kind is BlockKind.BULLETS:
            tag = "ol" if block.ordered else "ul"
            items = "".join(
                f"<li>{self._inline(i, evidence)}</li>" for i in block.items
            )
            return f"<{tag}>{items}</{tag}>"

        if kind is BlockKind.KEY_VALUE:
            rows = "".join(
                f"<tr><th>{_e(k)}</th><td>{_e(v)}</td></tr>"
                for k, v in block.pairs
            )
            return f'<table class="kv">{rows}</table>'

        if kind is BlockKind.TABLE:
            return self._table(block)

        if kind is BlockKind.METRIC_GRID:
            tiles = "".join(
                f'<div class="metric"><span class="label">{_e(label)}</span>'
                f'<span class="value">{_e(value)}</span>'
                f'<span class="hint">{_e(hint)}</span></div>'
                for label, value, hint in block.metrics
            )
            return (
                f'<div class="metrics" style="--cols:{block.columns}">'
                f"{tiles}</div>"
            )

        if kind is BlockKind.CHART:
            png = engine.render(block)
            if png is None:
                return ""
            encoded = base64.b64encode(png).decode("ascii")
            note = (
                f'<figcaption>{_e(block.note)}</figcaption>' if block.note else ""
            )
            return (
                '<figure class="chart">'
                f'<img alt="{_e(block.title)}" '
                f'src="data:image/png;base64,{encoded}">{note}</figure>'
            )

        if kind is BlockKind.CALLOUT:
            return (
                f'<div class="callout {TONE_CLASS[block.tone]}">'
                f"<strong>{_e(block.title)}</strong>"
                f"<p>{self._inline(block.text, evidence)}</p></div>"
            )

        if kind is BlockKind.QUOTE:
            attribution = (
                f"<cite>{_e(block.attribution)}</cite>"
                if block.attribution else ""
            )
            return f"<blockquote>{_e(block.text)}{attribution}</blockquote>"

        if kind is BlockKind.DIVIDER:
            return "<hr>"

        if kind is BlockKind.PAGE_BREAK:
            return '<div class="page-break"></div>'

        if kind is BlockKind.INSUFFICIENT:
            return (
                '<div class="callout insufficient">'
                "<strong>Insufficient evidence.</strong>"
                f"<p>{_e(block.reason)}</p></div>"
            )

        if kind is BlockKind.CITATION_LIST:
            rows = "".join(
                f"<tr><td><code>{_e(e.key)}</code></td>"
                f"<td>{_e(e.render())}</td></tr>"
                for e in block.entries
            )
            return f'<table class="evidence">{rows}</table>'

        return ""  # pragma: no cover - vocabulary is exhaustive

    @staticmethod
    def _table(block) -> str:
        head = (
            "<thead><tr>"
            + "".join(f"<th>{_e(h)}</th>" for h in block.header)
            + "</tr></thead>"
            if block.header else ""
        )
        rows = []
        for index, row in enumerate(block.rows):
            cells = "".join(
                f'<td class="{block.align[i] if i < len(block.align) else "l"}">'
                f"{_e(cell)}</td>"
                for i, cell in enumerate(row)
            )
            emphasis = ' class="emphasis"' if index in block.emphasis_rows else ""
            rows.append(f"<tr{emphasis}>{cells}</tr>")
        caption = f"<caption>{_e(block.caption)}</caption>" if block.caption else ""
        note = f'<p class="note">{_e(block.note)}</p>' if block.note else ""
        return (
            f"<table class=\"data\">{caption}{head}"
            f"<tbody>{''.join(rows)}</tbody></table>{note}"
        )

    @staticmethod
    def _inline(text: str, evidence) -> str:
        """Escape, then turn citation markers into hoverable chips."""
        import re

        lookup = {e.key: e for e in evidence}
        escaped = _e(text)

        def swap(match: re.Match) -> str:
            found = lookup.get(match.group(1))
            if found is None:
                return match.group(0)
            return (
                f'<span class="cite" title="{_e(found.render())}">'
                f"{_e(found.key)}</span>"
            )

        return re.sub(r"\[([a-z0-9_]+)\]", swap, escaped)


@register
class MarkdownRenderer(ReportRenderer):
    """Renders a report to Markdown."""

    fmt = OutputFormat.MARKDOWN

    def render(self, document: ReportDocument) -> RenderResult:
        started = time.perf_counter()
        cover = document.cover
        lines: list[str] = [
            f"# {cover.company_name} — {cover.title}",
            "",
            f"**{cover.ticker}** · {cover.institution}"
            + (f" · {cover.as_of.isoformat()}" if cover.as_of else ""),
            "",
        ]
        for key, value in cover_pairs(document):
            lines.append(f"- **{key}:** {value}")
        lines.append("")
        if cover.data_warning:
            lines.extend([f"> **Data quality.** {cover.data_warning}", ""])

        for section in document.ordered():
            if section.key is SectionKey.COVER:
                continue
            if section.key is SectionKey.TOC:
                lines.append("## Contents")
                lines.append("")
                for _, title in toc_entries(document):
                    lines.append(f"1. [{title}](#{_slug(title)})")
                lines.append("")
                continue
            lines.extend([f"## {section.title}", ""])
            for block in section.blocks:
                lines.extend(self._block(block))
                lines.append("")

        lines.extend(["---", "", f"_{document.disclaimer}_"])
        payload = "\n".join(lines).encode("utf-8")
        return RenderResult(
            payload=payload, fmt=self.fmt,
            filename=self.filename(document, self.fmt),
            took_ms=round((time.perf_counter() - started) * 1000, 2),
        )

    def _block(self, block: Block) -> list[str]:
        kind = block.kind

        if kind is BlockKind.HEADING:
            return [f"{'#' * min(6, block.level + 1)} {block.text}"]
        if kind is BlockKind.PARAGRAPH:
            return [block.text]
        if kind is BlockKind.BULLETS:
            marker = "1." if block.ordered else "-"
            return [f"{marker} {item}" for item in block.items]
        if kind is BlockKind.KEY_VALUE:
            return [f"- **{k}:** {v}" for k, v in block.pairs]
        if kind is BlockKind.TABLE:
            return self._table(block)
        if kind is BlockKind.METRIC_GRID:
            return [
                f"- **{label}:** {value}" + (f" _{hint}_" if hint else "")
                for label, value, hint in block.metrics
            ]
        if kind is BlockKind.CHART:
            # Markdown has no images without an asset directory; the underlying
            # data is emitted so the chart is still readable and diffable.
            return self._chart_table(block)
        if kind is BlockKind.CALLOUT:
            return [f"> **{block.title}.** {block.text}"]
        if kind is BlockKind.QUOTE:
            attribution = f"\n> — {block.attribution}" if block.attribution else ""
            return [f"> {block.text}{attribution}"]
        if kind is BlockKind.DIVIDER:
            return ["---"]
        if kind is BlockKind.PAGE_BREAK:
            return [""]
        if kind is BlockKind.INSUFFICIENT:
            return [f"> **Insufficient evidence.** {block.reason}".rstrip()]
        if kind is BlockKind.CITATION_LIST:
            return [f"- `{e.key}` — {e.render()}" for e in block.entries]
        return []  # pragma: no cover - vocabulary is exhaustive

    @staticmethod
    def _table(block) -> list[str]:
        if not block.header and not block.rows:
            return []
        columns = block.n_cols
        header = block.header or [""] * columns
        separator = [
            "---:" if (block.align[i] if i < len(block.align) else "l") == "r"
            else ":---"
            for i in range(columns)
        ]
        lines = []
        if block.caption:
            lines.append(f"**{block.caption}**")
            lines.append("")
        lines.append("| " + " | ".join(header) + " |")
        lines.append("| " + " | ".join(separator) + " |")
        for index, row in enumerate(block.rows):
            padded = list(row) + [""] * (columns - len(row))
            cells = [
                f"**{c}**" if index in block.emphasis_rows and c else c
                for c in padded
            ]
            lines.append("| " + " | ".join(cells) + " |")
        return lines

    @staticmethod
    def _chart_table(block) -> list[str]:
        if not block.has_data or not block.labels:
            return []
        lines = [f"**{block.title}**", ""]
        names = [name for name, _ in block.series]
        lines.append("| Period | " + " | ".join(names) + " |")
        lines.append("| :--- | " + " | ".join(["---:"] * len(names)) + " |")
        for index, label in enumerate(block.labels):
            cells = []
            for _, values in block.series:
                value = values[index] if index < len(values) else None
                cells.append("—" if value is None else f"{value:,.2f}")
            lines.append(f"| {label} | " + " | ".join(cells) + " |")
        return lines


_STYLESHEET = """
:root{--bg:#fff;--surface:#f5f7fa;--text:#0f172a;--muted:#64748b;
--rule:#e2e8f0;--accent:#1e3a8a;--pos:#059669;--neg:#dc2626;--warn:#b45309}
body.dark{--bg:#0b1220;--surface:#131c2e;--text:#e2e8f0;--muted:#94a3b8;
--rule:#1e293b;--accent:#60a5fa;--pos:#34d399;--neg:#f87171;--warn:#fbbf24}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,sans-serif}
.report{max-width:960px;margin:0 auto;padding:32px 28px 64px}
.cover{border-bottom:2px solid var(--accent);padding-bottom:28px;margin-bottom:32px}
.eyebrow{font-size:11px;letter-spacing:.14em;color:var(--muted);font-weight:600}
.cover h1{font-size:34px;margin:8px 0 4px;color:var(--accent);line-height:1.15}
.subtitle{font-size:15px;color:var(--muted);margin-bottom:20px}
.cover-table{width:100%;border-collapse:collapse;font-size:13px}
.cover-table th{text-align:left;color:var(--muted);font-weight:600;
padding:5px 12px 5px 0;width:32%;vertical-align:top}
.cover-table td{padding:5px 0;border-bottom:1px solid var(--rule)}
.sec{margin:34px 0;page-break-inside:auto}
.sec h1{font-size:20px;color:var(--accent);border-bottom:1px solid var(--rule);
padding-bottom:6px;margin-bottom:14px}
h2{font-size:15px;margin:18px 0 6px}h3{font-size:13px;margin:14px 0 5px;color:var(--muted)}
h4{font-size:12px;margin:12px 0 4px;color:var(--muted)}
p{margin:0 0 10px;text-align:justify}
ul,ol{margin:0 0 12px;padding-left:22px}li{margin-bottom:4px}
table{border-collapse:collapse;width:100%;margin:8px 0 16px;font-size:12px}
table.data caption{caption-side:top;text-align:left;font-weight:600;
color:var(--muted);font-size:12px;padding-bottom:6px}
table.data th{background:var(--accent);color:#fff;padding:7px 9px;
text-align:left;font-weight:600}
table.data td{padding:6px 9px;border:1px solid var(--rule)}
table.data td.r{text-align:right;font-variant-numeric:tabular-nums}
table.data td.c{text-align:center}
table.data tbody tr:nth-child(even){background:var(--surface)}
table.data tr.emphasis td{font-weight:700}
table.kv th{text-align:left;color:var(--muted);font-weight:600;
padding:5px 12px 5px 0;width:34%;vertical-align:top}
table.kv td{padding:5px 0;border-bottom:1px solid var(--rule)}
table.evidence td{padding:4px 8px;border-bottom:1px solid var(--rule);font-size:11px}
.metrics{display:grid;grid-template-columns:repeat(var(--cols,4),1fr);
gap:10px;margin:12px 0 18px}
.metric{background:var(--surface);border:1px solid var(--rule);
border-radius:6px;padding:10px 12px;display:flex;flex-direction:column;gap:2px}
.metric .label{font-size:9.5px;text-transform:uppercase;letter-spacing:.07em;
color:var(--muted);font-weight:600}
.metric .value{font-size:19px;font-weight:700;font-variant-numeric:tabular-nums}
.metric .hint{font-size:10px;color:var(--muted)}
.callout{background:var(--surface);border-left:3px solid var(--accent);
padding:11px 14px;margin:12px 0;border-radius:0 5px 5px 0}
.callout strong{display:block;margin-bottom:3px;color:var(--accent)}
.callout p{margin:0}
.callout.positive{border-left-color:var(--pos)}.callout.positive strong{color:var(--pos)}
.callout.negative{border-left-color:var(--neg)}.callout.negative strong{color:var(--neg)}
.callout.warning{border-left-color:var(--warn)}.callout.warning strong{color:var(--warn)}
.callout.insufficient{border-left-color:var(--muted);font-style:italic}
.callout.insufficient strong{color:var(--muted)}
.cite{display:inline-block;background:color-mix(in srgb,var(--accent) 12%,transparent);
color:var(--accent);border-radius:3px;padding:0 4px;margin-left:2px;
font:600 9.5px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;cursor:help;
vertical-align:super}
figure.chart{margin:14px 0 18px;page-break-inside:avoid}
figure.chart img{width:100%;height:auto;display:block}
figcaption{font-size:10.5px;color:var(--muted);margin-top:5px}
blockquote{margin:10px 0;padding:8px 14px;border-left:3px solid var(--rule);
color:var(--muted);font-style:italic}
blockquote cite{display:block;margin-top:5px;font-size:11px;font-style:normal}
hr{border:none;border-top:1px solid var(--rule);margin:18px 0}
.toc ol{columns:2;column-gap:36px}
.toc a{color:var(--text);text-decoration:none}
.toc a:hover{color:var(--accent);text-decoration:underline}
.note{font-size:10.5px;color:var(--muted);margin-top:-10px}
.disclaimer{margin-top:44px;padding-top:16px;border-top:1px solid var(--rule);
font-size:10.5px;color:var(--muted)}
.page-break{page-break-after:always}
@media print{
  @page{size:A4;margin:16mm 14mm 18mm}
  body{background:#fff;color:#000;font-size:9.5pt}
  .report{max-width:none;padding:0}
  .sec{page-break-inside:auto}
  .sec h1{page-break-after:avoid}
  table,figure.chart,.callout,.metrics{page-break-inside:avoid}
  .cite{vertical-align:baseline}
}
"""
