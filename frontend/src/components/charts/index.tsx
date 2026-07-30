"use client";

import Highcharts from "highcharts";
import HighchartsReact from "highcharts-react-official";
import { useEffect, useMemo, useState } from "react";

/**
 * Polar (radar) support lives in `highcharts-more`, which touches browser
 * globals at module scope and therefore cannot be imported during SSR. It is
 * loaded lazily on mount instead; `Chart` already defers rendering until then,
 * so nothing draws before the module is in place.
 */
let morePromise: Promise<unknown> | null = null;
function loadHighchartsMore(): Promise<unknown> {
  if (typeof window === "undefined") return Promise.resolve();
  morePromise ??= import("highcharts/highcharts-more");
  return morePromise;
}

import { useTheme } from "@/components/layout/theme-provider";
import { crore, fiscalYear, percent, rupees } from "@/lib/format";

/* ------------------------------------------------------------------ theme */

const PALETTE = {
  accent: "#1f6feb",
  accentSoft: "rgba(31,111,235,0.18)",
  gain: "#0b7a3b",
  loss: "#b3261e",
  bull: "#0b7a3b",
  base: "#1f6feb",
  bear: "#b3261e",
  forecast: "#8b5cf6",
};

function baseOptions(dark: boolean): Highcharts.Options {
  const text = dark ? "#8fa3bf" : "#64748b";
  const grid = dark ? "#1e304c" : "#e2e8f0";
  const bg = "transparent";
  return {
    chart: {
      backgroundColor: bg,
      style: { fontFamily: "inherit" },
      spacing: [12, 8, 8, 8],
      animation: { duration: 300 },
    },
    credits: { enabled: false },
    title: { text: undefined },
    legend: {
      itemStyle: { color: text, fontSize: "11px", fontWeight: "500" },
      itemHoverStyle: { color: dark ? "#e8eef7" : "#0f172a" },
      symbolRadius: 2,
      margin: 12,
    },
    xAxis: {
      lineColor: grid,
      tickColor: grid,
      labels: { style: { color: text, fontSize: "10px" } },
      crosshair: { color: dark ? "#2b4468" : "#cbd5e1", dashStyle: "Dash" },
    },
    yAxis: {
      gridLineColor: grid,
      gridLineDashStyle: "Dot",
      title: { style: { color: text, fontSize: "10px" } },
      labels: { style: { color: text, fontSize: "10px" } },
    },
    tooltip: {
      backgroundColor: dark ? "#0d1b30" : "#ffffff",
      borderColor: grid,
      borderRadius: 6,
      shadow: false,
      style: { color: dark ? "#e8eef7" : "#0f172a", fontSize: "11px" },
      shared: true,
    },
    plotOptions: {
      series: { animation: { duration: 300 }, marker: { radius: 3, symbol: "circle" } },
      column: { borderWidth: 0, borderRadius: 2 },
    },
  };
}

/** Deep-merges chart options so callers only specify what differs. */
function merge(a: Highcharts.Options, b: Highcharts.Options): Highcharts.Options {
  const out: Record<string, unknown> = { ...a };
  for (const [k, v] of Object.entries(b)) {
    const prev = (a as Record<string, unknown>)[k];
    out[k] =
      v && typeof v === "object" && !Array.isArray(v) && prev && typeof prev === "object" && !Array.isArray(prev)
        ? merge(prev as Highcharts.Options, v as Highcharts.Options)
        : v;
  }
  return out as Highcharts.Options;
}

/** Wrapper that re-themes on light/dark toggle and avoids SSR mismatch. */
function Chart({
  options, height = 280, needsMore = false,
}: {
  options: Highcharts.Options;
  height?: number;
  /** Set for polar/radar charts, which require the highcharts-more module. */
  needsMore?: boolean;
}) {
  const { theme } = useTheme();
  const [mounted, setMounted] = useState(false);

  useEffect(() => {
    if (!needsMore) { setMounted(true); return; }
    let live = true;
    loadHighchartsMore().then(() => { if (live) setMounted(true); });
    return () => { live = false; };
  }, [needsMore]);

  const merged = useMemo(
    () => merge(baseOptions(theme === "dark"), merge(options, { chart: { height } })),
    [options, theme, height],
  );

  if (!mounted) {
    return <div style={{ height }} className="animate-pulse rounded bg-[var(--bg-subtle)]" />;
  }
  return <HighchartsReact highcharts={Highcharts} options={merged} />;
}

/* ------------------------------------------------------- history vs forecast */

export interface TrendPoint {
  fiscalYear: number;
  value: number | null;
}

/**
 * History and forecast on one axis, visually separated by a plot band and a
 * dashed forecast series — the single most important chart in the module,
 * because it shows whether a projection is a continuation or a break.
 */
export function HistoryForecastChart({
  history,
  forecast,
  label,
  unit = "₹ cr",
  height = 300,
  secondary,
}: {
  history: TrendPoint[];
  forecast: TrendPoint[];
  label: string;
  unit?: string;
  height?: number;
  secondary?: { label: string; history: TrendPoint[]; forecast: TrendPoint[]; unit: string };
}) {
  const categories = [
    ...history.map((h) => fiscalYear(h.fiscalYear)),
    ...forecast.map((f) => fiscalYear(f.fiscalYear)),
  ];
  const n = history.length;

  // Pad each series so the two occupy distinct halves of the shared axis. The
  // forecast repeats the last actual so the line is visually continuous.
  const histSeries = [...history.map((h) => h.value), ...forecast.map(() => null)];
  const fcstSeries = [
    ...history.slice(0, n - 1).map(() => null),
    history[n - 1]?.value ?? null,
    ...forecast.map((f) => f.value),
  ];

  const fmt = (v: number | null | undefined) =>
    v === null || v === undefined
      ? "—"
      : unit === "%" ? percent(v) : unit === "₹" ? rupees(v) : crore(v);

  const series: Highcharts.SeriesOptionsType[] = [
    {
      type: "column", name: `${label} — reported`, data: histSeries,
      color: PALETTE.accent, opacity: 0.95,
    },
    {
      type: "column", name: `${label} — forecast`, data: fcstSeries,
      color: PALETTE.forecast, opacity: 0.85,
    },
  ];

  if (secondary) {
    series.push({
      type: "spline", name: secondary.label, yAxis: 1,
      data: [
        ...secondary.history.map((h) => h.value),
        ...secondary.forecast.map((f) => f.value),
      ],
      color: PALETTE.gain, dashStyle: "ShortDot", marker: { enabled: false },
    });
  }

  return (
    <Chart
      height={height}
      options={{
        xAxis: {
          categories,
          plotBands: [
            {
              from: n - 0.5, to: categories.length - 0.5,
              color: PALETTE.accentSoft,
              label: {
                text: "FORECAST",
                style: { fontSize: "9px", letterSpacing: "0.08em", color: PALETTE.forecast },
                y: 14,
              },
            },
          ],
        },
        yAxis: [
          { title: { text: unit } },
          ...(secondary
            ? [{ title: { text: secondary.unit }, opposite: true,
                 labels: { formatter(this: { value: number | string }) {
                   return percent(Number(this.value), 0);
                 } } }]
            : []),
        ],
        tooltip: {
          formatter(this: Highcharts.Point) {
            const pts = (this as unknown as { points?: Highcharts.Point[] }).points ?? [this];
            const rows = pts
              .filter((p) => p.y !== null && p.y !== undefined)
              .map((p) => {
                const isPct =
                  (p.series.options as { yAxis?: number }).yAxis === 1 && secondary?.unit === "%";
                return `<div style="display:flex;gap:10px;justify-content:space-between">
                  <span style="color:${p.color}">● ${p.series.name}</span>
                  <b>${isPct ? percent(Number(p.y)) : fmt(Number(p.y))}</b></div>`;
              })
              .join("");
            return `<div style="font-size:11px"><b>${this.category ?? this.x}</b>${rows}</div>`;
          },
        },
        series,
      }}
    />
  );
}

/* --------------------------------------------------------- scenario compare */

export function ScenarioChart({
  labels, bear, base, bull, unit = "₹ cr", height = 300, title,
}: {
  labels: string[];
  bear: (number | null)[];
  base: (number | null)[];
  bull: (number | null)[];
  unit?: string;
  height?: number;
  title?: string;
}) {
  const fmt = (v: number) => (unit === "₹" ? rupees(v) : unit === "%" ? percent(v) : crore(v));
  return (
    <Chart
      height={height}
      options={{
        xAxis: { categories: labels },
        yAxis: { title: { text: title ?? unit } },
        tooltip: {
          formatter(this: Highcharts.Point) {
            const pts = (this as unknown as { points?: Highcharts.Point[] }).points ?? [this];
            const rows = pts
              .map((p) => `<div style="display:flex;gap:10px;justify-content:space-between">
                <span style="color:${p.color}">● ${p.series.name}</span>
                <b>${fmt(Number(p.y))}</b></div>`)
              .join("");
            return `<div style="font-size:11px"><b>${this.category ?? this.x}</b>${rows}</div>`;
          },
        },
        series: [
          { type: "spline", name: "Bull", data: bull, color: PALETTE.bull, marker: { enabled: false } },
          { type: "spline", name: "Base", data: base, color: PALETTE.base, lineWidth: 2.5 },
          { type: "spline", name: "Bear", data: bear, color: PALETTE.bear, marker: { enabled: false } },
        ],
      }}
    />
  );
}

/* ------------------------------------------------------------ FCF waterfall */

export function CashFlowChart({
  labels, fcff, fcfe, height = 280,
}: {
  labels: string[];
  fcff: number[];
  fcfe: number[];
  height?: number;
}) {
  return (
    <Chart
      height={height}
      options={{
        xAxis: { categories: labels },
        yAxis: {
          title: { text: "₹ cr" },
          plotLines: [{ value: 0, color: "#94a3b8", width: 1, zIndex: 3 }],
        },
        tooltip: {
          formatter(this: Highcharts.Point) {
            const pts = (this as unknown as { points?: Highcharts.Point[] }).points ?? [this];
            const rows = pts
              .map((p) => `<div style="display:flex;gap:10px;justify-content:space-between">
                <span style="color:${p.color}">● ${p.series.name}</span>
                <b>${crore(Number(p.y))}</b></div>`)
              .join("");
            return `<div style="font-size:11px"><b>${this.category ?? this.x}</b>${rows}</div>`;
          },
        },
        series: [
          {
            type: "column", name: "FCFF", data: fcff,
            color: PALETTE.accent,
            negativeColor: PALETTE.loss,
          },
          {
            type: "column", name: "FCFE", data: fcfe,
            color: PALETTE.gain,
            negativeColor: "#e0736c",
          },
        ],
      }}
    />
  );
}

/* ------------------------------------------------------ scenario value range */

export function ValueRangeChart({
  bear, base, bull, expected, currentPrice, height = 220,
}: {
  bear: number; base: number; bull: number;
  expected: number | null; currentPrice: number | null; height?: number;
}) {
  return (
    <Chart
      height={height}
      options={{
        chart: { type: "bar" },
        xAxis: { categories: ["Bear", "Base", "Bull"] },
        yAxis: {
          title: { text: "Value per share (₹)" },
          plotLines: [
            ...(currentPrice
              ? [{
                  value: currentPrice, color: PALETTE.loss, width: 2, dashStyle: "Dash" as const,
                  zIndex: 5,
                  label: { text: `CMP ${rupees(currentPrice)}`, style: { fontSize: "9px", color: PALETTE.loss } },
                }]
              : []),
            ...(expected
              ? [{
                  value: expected, color: PALETTE.forecast, width: 2, zIndex: 5,
                  label: { text: `Expected ${rupees(expected)}`, style: { fontSize: "9px", color: PALETTE.forecast } },
                }]
              : []),
          ],
        },
        legend: { enabled: false },
        tooltip: {
          formatter(this: Highcharts.Point) {
            return `<b>${this.category ?? this.x}</b>: ${rupees(Number(this.y))}`;
          },
        },
        series: [
          {
            type: "bar", name: "Value per share",
            data: [
              { y: bear, color: PALETTE.bear },
              { y: base, color: PALETTE.base },
              { y: bull, color: PALETTE.bull },
            ],
          },
        ],
      }}
    />
  );
}

export { PALETTE };

/* ------------------------------------------------------------------ radar */

export interface RadarSeries {
  name: string;
  data: (number | null)[];
  color?: string;
  dashed?: boolean;
}

/**
 * Score radar. Categories around the perimeter, 0-10 on the spoke.
 * Supports an overlay series so a company can be read against its peer median.
 */
export function ScoreRadar({
  categories, series, height = 380,
}: {
  categories: string[];
  series: RadarSeries[];
  height?: number;
}) {
  return (
    <Chart
      height={height}
      needsMore
      options={{
        chart: { polar: true, type: "line" },
        xAxis: {
          categories,
          tickmarkPlacement: "on",
          lineWidth: 0,
          labels: { style: { fontSize: "9px" } },
        },
        yAxis: {
          gridLineInterpolation: "polygon",
          min: 0, max: 10, tickInterval: 2,
          labels: { style: { fontSize: "9px" } },
          title: { text: undefined },
        },
        tooltip: {
          shared: false,
          formatter(this: Highcharts.Point) {
            return `<b>${this.category ?? this.x}</b><br/>${this.series.name}: <b>${Number(this.y).toFixed(1)}</b>/10`;
          },
        },
        legend: { align: "center", verticalAlign: "bottom", layout: "horizontal" },
        series: series.map((s) => ({
          type: "line",
          name: s.name,
          data: s.data,
          color: s.color ?? PALETTE.accent,
          dashStyle: s.dashed ? "ShortDash" : "Solid",
          lineWidth: s.dashed ? 1.5 : 2,
          marker: { radius: s.dashed ? 0 : 3 },
          fillOpacity: 0.15,
        })) as Highcharts.SeriesOptionsType[],
      }}
    />
  );
}

/* --------------------------------------------------- score contribution bar */

export function ContributionChart({
  categories, contributions, height = 340,
}: {
  categories: string[];
  contributions: number[];
  height?: number;
}) {
  return (
    <Chart
      height={height}
      options={{
        chart: { type: "bar" },
        xAxis: { categories, labels: { style: { fontSize: "10px" } } },
        yAxis: { title: { text: "Weighted contribution" } },
        legend: { enabled: false },
        tooltip: {
          formatter(this: Highcharts.Point) {
            return `<b>${this.category ?? this.x}</b>: ${Number(this.y).toFixed(3)}`;
          },
        },
        series: [{
          type: "bar", name: "Contribution", data: contributions,
          color: PALETTE.accent,
        }],
      }}
    />
  );
}

/* ------------------------------------------------------------ score history */

export function ScoreHistoryChart({
  labels, scores, height = 260,
}: {
  labels: string[];
  scores: (number | null)[];
  height?: number;
}) {
  return (
    <Chart
      height={height}
      options={{
        xAxis: { categories: labels },
        yAxis: {
          title: { text: "Composite score" },
          min: 0, max: 100,
          plotBands: [
            { from: 75, to: 100, color: "rgba(11,122,59,0.08)" },
            { from: 55, to: 75, color: "rgba(31,111,235,0.06)" },
            { from: 0, to: 45, color: "rgba(179,38,30,0.08)" },
          ],
        },
        legend: { enabled: false },
        tooltip: {
          formatter(this: Highcharts.Point) {
            return `<b>${this.category ?? this.x}</b>: ${Number(this.y).toFixed(1)}/100`;
          },
        },
        series: [{
          type: "areaspline", name: "Score", data: scores,
          color: PALETTE.accent, fillOpacity: 0.15,
        }],
      }}
    />
  );
}
