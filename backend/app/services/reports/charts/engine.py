"""Chart engine — renders `Chart` blocks to PNG.

Matplotlib with the Agg backend, which is headless and deterministic. The
alternative was to have each renderer draw its own charts, which would mean a
PDF chart and an HTML chart could diverge in a way nobody notices until a
client asks why two versions of the same report disagree.

Two decisions worth recording:

* **A chart with no data is not drawn.** `Chart.has_data` gates every call. An
  empty axis reads as "the value was zero" rather than "we had nothing", and
  that is precisely the fabrication the brief forbids.
* **Charts are cached by content hash.** The same series rendered twice — once
  for the PDF, once for the HTML — produces one image. The key includes the
  theme and size, so a dark-mode render is a separate entry rather than a
  wrongly-reused light one.
"""
from __future__ import annotations

import hashlib
import io
import logging
import math
from dataclasses import dataclass
from typing import Sequence

import matplotlib

# Must precede pyplot: the sandbox has no display, and the interactive backend
# would fail at import rather than at draw time.
matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402

from app.domain.reports.blocks import Chart, ChartKind, Theme  # noqa: E402

logger = logging.getLogger(__name__)

#: Print resolution. 150 dpi is the threshold above which a raster stops
#: looking soft in a printed A4 report.
DPI = 150
DEFAULT_WIDTH = 7.2
DEFAULT_HEIGHT = 3.2


@dataclass(frozen=True, slots=True)
class Palette:
    """Colours for one theme. Institutional navy, not matplotlib defaults."""

    background: str
    surface: str
    text: str
    muted: str
    grid: str
    series: tuple[str, ...]
    positive: str
    negative: str

    @property
    def diverging(self) -> tuple[str, str, str]:
        return (self.negative, self.surface, self.positive)


LIGHT = Palette(
    background="#ffffff", surface="#f5f7fa", text="#0f172a", muted="#64748b",
    grid="#e2e8f0",
    series=("#1e3a8a", "#0891b2", "#059669", "#d97706", "#7c3aed", "#dc2626"),
    positive="#059669", negative="#dc2626",
)
DARK = Palette(
    background="#0b1220", surface="#131c2e", text="#e2e8f0", muted="#94a3b8",
    grid="#1e293b",
    series=("#60a5fa", "#22d3ee", "#34d399", "#fbbf24", "#a78bfa", "#f87171"),
    positive="#34d399", negative="#f87171",
)

PALETTES = {Theme.LIGHT: LIGHT, Theme.DARK: DARK}


class ChartEngine:
    """Renders charts to PNG bytes, with a content-addressed cache."""

    def __init__(self, theme: Theme = Theme.LIGHT, dpi: int = DPI) -> None:
        self.theme = theme
        self.dpi = dpi
        self.palette = PALETTES[theme]
        self._cache: dict[str, bytes] = {}
        self.hits = 0
        self.misses = 0

    # ------------------------------------------------------------------
    def cache_key(self, chart: Chart, width: float, height: float) -> str:
        payload = "|".join([
            chart.chart_kind.value, chart.title, self.theme.value,
            f"{width}x{height}x{self.dpi}",
            ";".join(chart.labels), ";".join(chart.row_labels),
            ";".join(
                f"{name}:{','.join('' if v is None else f'{v:.6g}' for v in vals)}"
                for name, vals in chart.series
            ),
            ";".join(
                ",".join("" if v is None else f"{v:.6g}" for v in row)
                for row in chart.matrix
            ),
        ])
        return hashlib.sha1(payload.encode("utf-8"), usedforsecurity=False).hexdigest()[:20]

    def render(
        self, chart: Chart, *, width: float = DEFAULT_WIDTH,
        height: float = DEFAULT_HEIGHT,
    ) -> bytes | None:
        """Render to PNG. Returns ``None`` when the chart has no data."""
        if not chart.has_data:
            return None

        key = self.cache_key(chart, width, height)
        cached = self._cache.get(key)
        if cached is not None:
            self.hits += 1
            return cached
        self.misses += 1

        try:
            payload = self._draw(chart, width, height)
        except Exception as exc:  # pragma: no cover - resilience path
            # A chart that cannot be drawn must not lose the report. The
            # section renders without it and the failure is logged.
            logger.warning("chart '%s' failed to render: %s", chart.title, exc)
            return None

        self._cache[key] = payload
        return payload

    def stats(self) -> dict[str, float]:
        total = self.hits + self.misses
        return {
            "hits": self.hits, "misses": self.misses,
            "hit_rate": round(self.hits / total, 4) if total else 0.0,
            "cached": len(self._cache),
        }

    # ------------------------------------------------------------------
    def _draw(self, chart: Chart, width: float, height: float) -> bytes:
        figure: Figure = plt.figure(
            figsize=(width, height), dpi=self.dpi,
            facecolor=self.palette.background,
        )
        try:
            if chart.chart_kind is ChartKind.SENSITIVITY and chart.matrix:
                self._heatmap(figure, chart)
            elif chart.chart_kind is ChartKind.SCORE_RADAR:
                self._radar(figure, chart)
            elif chart.chart_kind is ChartKind.PORTFOLIO_ALLOCATION:
                self._donut(figure, chart)
            elif chart.chart_kind in {ChartKind.MARGINS}:
                self._lines(figure, chart)
            elif chart.chart_kind is ChartKind.PEER_COMPARISON:
                self._barh(figure, chart)
            else:
                self._bars(figure, chart)

            buffer = io.BytesIO()
            figure.savefig(
                buffer, format="png", dpi=self.dpi,
                facecolor=self.palette.background, bbox_inches="tight",
                pad_inches=0.15,
            )
            return buffer.getvalue()
        finally:
            plt.close(figure)

    # -- styling -------------------------------------------------------
    def _axes(self, figure: Figure, chart: Chart):
        axes = figure.add_subplot(111)
        axes.set_facecolor(self.palette.background)
        for spine in ("top", "right"):
            axes.spines[spine].set_visible(False)
        for spine in ("left", "bottom"):
            axes.spines[spine].set_color(self.palette.grid)
        axes.tick_params(colors=self.palette.muted, labelsize=8)
        axes.grid(axis="y", color=self.palette.grid, linewidth=0.6, alpha=0.8)
        axes.set_axisbelow(True)
        if chart.title:
            axes.set_title(
                chart.title, color=self.palette.text, fontsize=10,
                fontweight="600", loc="left", pad=10,
            )
        return axes

    def _legend(self, axes) -> None:
        legend = axes.legend(
            frameon=False, fontsize=8, loc="upper left",
            bbox_to_anchor=(0, -0.12), ncol=4,
        )
        for text in legend.get_texts():
            text.set_color(self.palette.muted)

    @staticmethod
    def _clean(values: Sequence[float | None]) -> list[float]:
        """Nulls become NaN so matplotlib leaves a gap rather than plotting zero."""
        return [math.nan if v is None else float(v) for v in values]

    # -- chart types ---------------------------------------------------
    def _bars(self, figure: Figure, chart: Chart) -> None:
        axes = self._axes(figure, chart)
        positions = range(len(chart.labels))
        primary = [(n, v) for n, v in chart.series if n not in chart.secondary]
        secondary = [(n, v) for n, v in chart.series if n in chart.secondary]

        count = max(1, len(primary))
        bar_width = 0.8 / count
        for index, (name, values) in enumerate(primary):
            offset = (index - (count - 1) / 2) * bar_width
            axes.bar(
                [p + offset for p in positions], self._clean(values),
                width=bar_width, label=name,
                color=self.palette.series[index % len(self.palette.series)],
            )

        if secondary:
            twin = axes.twinx()
            twin.set_facecolor("none")
            twin.tick_params(colors=self.palette.muted, labelsize=8)
            for spine in twin.spines.values():
                spine.set_visible(False)
            for index, (name, values) in enumerate(secondary):
                twin.plot(
                    list(positions), [
                        math.nan if v is None else v * 100 for v in values
                    ],
                    marker="o", markersize=3, linewidth=1.6, label=name,
                    color=self.palette.series[
                        (len(primary) + index) % len(self.palette.series)
                    ],
                )
            twin.yaxis.set_major_formatter(
                matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:.0f}%")
            )

        axes.set_xticks(list(positions))
        axes.set_xticklabels(chart.labels, color=self.palette.muted)
        if chart.y_unit:
            axes.set_ylabel(chart.y_unit, color=self.palette.muted, fontsize=8)
        axes.yaxis.set_major_formatter(
            matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:,.0f}")
        )
        if len(chart.series) > 1:
            self._legend(axes)

    def _lines(self, figure: Figure, chart: Chart) -> None:
        axes = self._axes(figure, chart)
        positions = list(range(len(chart.labels)))
        percent = chart.y_unit == "%"
        for index, (name, values) in enumerate(chart.series):
            scaled = [
                math.nan if v is None else (v * 100 if percent else v)
                for v in values
            ]
            axes.plot(
                positions, scaled, marker="o", markersize=3.5, linewidth=1.8,
                label=name,
                color=self.palette.series[index % len(self.palette.series)],
            )
        axes.set_xticks(positions)
        axes.set_xticklabels(chart.labels, color=self.palette.muted)
        if percent:
            axes.yaxis.set_major_formatter(
                matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:.1f}%")
            )
        if len(chart.series) > 1:
            self._legend(axes)

    def _barh(self, figure: Figure, chart: Chart) -> None:
        axes = self._axes(figure, chart)
        name, values = chart.series[0]
        cleaned = self._clean(values)
        # Largest at the top reads naturally; matplotlib's default puts the
        # first item at the bottom.
        order = sorted(
            range(len(cleaned)),
            key=lambda i: (-1 if math.isnan(cleaned[i]) else cleaned[i]),
        )
        labels = [chart.labels[i] for i in order]
        ordered = [cleaned[i] for i in order]
        axes.barh(
            range(len(ordered)), ordered, color=self.palette.series[0],
            height=0.65,
        )
        axes.set_yticks(range(len(labels)))
        axes.set_yticklabels(labels, color=self.palette.muted, fontsize=8)
        axes.grid(axis="x", color=self.palette.grid, linewidth=0.6)
        axes.grid(axis="y", visible=False)
        axes.xaxis.set_major_formatter(
            matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:,.0f}")
        )
        if chart.y_unit:
            axes.set_xlabel(chart.y_unit, color=self.palette.muted, fontsize=8)

    def _radar(self, figure: Figure, chart: Chart) -> None:
        axes = figure.add_subplot(111, polar=True)
        axes.set_facecolor(self.palette.background)
        name, values = chart.series[0]
        cleaned = [0.0 if v is None else float(v) for v in values]
        count = len(cleaned)
        if count < 3:
            # A radar of two axes is a line. Fall back rather than draw
            # something that misrepresents the shape of the data.
            plt.close(figure)
            raise ValueError("radar needs at least three categories")

        angles = [n / count * 2 * math.pi for n in range(count)]
        closed = cleaned + cleaned[:1]
        angles_closed = angles + angles[:1]

        axes.plot(angles_closed, closed, linewidth=1.8,
                  color=self.palette.series[0])
        axes.fill(angles_closed, closed, alpha=0.25,
                  color=self.palette.series[0])
        axes.set_xticks(angles)
        axes.set_xticklabels(
            [l[:18] for l in chart.labels], color=self.palette.muted, fontsize=7,
        )
        axes.set_ylim(0, 10)
        axes.set_yticks([2, 4, 6, 8, 10])
        axes.set_yticklabels(["2", "4", "6", "8", "10"],
                             color=self.palette.muted, fontsize=6)
        axes.grid(color=self.palette.grid, linewidth=0.6)
        axes.spines["polar"].set_color(self.palette.grid)
        if chart.title:
            axes.set_title(
                chart.title, color=self.palette.text, fontsize=10,
                fontweight="600", pad=18,
            )

    def _donut(self, figure: Figure, chart: Chart) -> None:
        axes = figure.add_subplot(111)
        axes.set_facecolor(self.palette.background)
        name, values = chart.series[0]
        pairs = [
            (label, float(value))
            for label, value in zip(chart.labels, values)
            if value is not None and value > 0
        ]
        if not pairs:
            raise ValueError("no positive values to plot")
        labels, sizes = zip(*pairs)
        colours = [
            self.palette.series[i % len(self.palette.series)]
            for i in range(len(sizes))
        ]
        wedges, *_ = axes.pie(
            sizes, colors=colours, startangle=90,
            wedgeprops={"width": 0.42, "edgecolor": self.palette.background},
        )
        axes.legend(
            wedges, [f"{l} · {s / sum(sizes) * 100:.1f}%" for l, s in pairs],
            frameon=False, fontsize=7, loc="center left",
            bbox_to_anchor=(1.0, 0.5),
            labelcolor=self.palette.muted,
        )
        axes.set_aspect("equal")
        if chart.title:
            axes.set_title(
                chart.title, color=self.palette.text, fontsize=10,
                fontweight="600", loc="left",
            )

    def _heatmap(self, figure: Figure, chart: Chart) -> None:
        axes = self._axes(figure, chart)
        matrix = [[math.nan if v is None else float(v) for v in row]
                  for row in chart.matrix]
        colourmap = matplotlib.colors.LinearSegmentedColormap.from_list(
            "sensitivity", list(self.palette.diverging),
        )
        image = axes.imshow(matrix, cmap=colourmap, aspect="auto")
        axes.set_xticks(range(len(chart.labels)))
        axes.set_xticklabels(chart.labels, color=self.palette.muted, fontsize=7)
        axes.set_yticks(range(len(chart.row_labels)))
        axes.set_yticklabels(
            chart.row_labels, color=self.palette.muted, fontsize=7,
        )
        axes.grid(visible=False)

        # Annotate only when the grid is small enough for the numbers to be
        # legible; beyond that the colour carries the message.
        if len(matrix) * len(matrix[0] if matrix else []) <= 49:
            for r, row in enumerate(matrix):
                for c, value in enumerate(row):
                    if math.isnan(value):
                        continue
                    axes.text(
                        c, r, f"{value:,.0f}", ha="center", va="center",
                        fontsize=6.5, color="#ffffff",
                    )
        bar = figure.colorbar(image, ax=axes, fraction=0.03, pad=0.02)
        bar.ax.tick_params(colors=self.palette.muted, labelsize=7)
        bar.outline.set_edgecolor(self.palette.grid)
