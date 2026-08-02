"use client";

/**
 * Presentational components for the portfolio screens.
 *
 * These render; they do not compute. Every weight, return, ratio and risk
 * statistic arrives from the API already calculated — the rule that has
 * governed the frontend since Module 1. The only arithmetic here is layout:
 * turning a weight into a rectangle, or a return into a colour.
 */

import { Badge, Card, CardBody, CardHeader } from "@/components/ui";
import type {
  Allocation, AlertEvaluation, AttributionRow, Holding, RebalanceTrade,
  RiskProfile, SeriesPoint, WatchlistRow,
} from "@/lib/types";
import { cn } from "@/lib/utils";
import { AlertTriangle, CheckCircle2, Info, TrendingDown, TrendingUp } from "lucide-react";
import type { ReactNode } from "react";

/* ------------------------------------------------------------ formatting */

export const money = (value: number | null | undefined, compact = false): string => {
  if (value === null || value === undefined) return "—";
  if (compact) {
    const abs = Math.abs(value);
    if (abs >= 1e7) return `₹${(value / 1e7).toFixed(2)}cr`;
    if (abs >= 1e5) return `₹${(value / 1e5).toFixed(2)}L`;
    if (abs >= 1e3) return `₹${(value / 1e3).toFixed(1)}k`;
  }
  return `₹${value.toLocaleString("en-IN", { maximumFractionDigits: 0 })}`;
};

export const pct = (value: number | null | undefined, places = 1): string =>
  value === null || value === undefined ? "—" : `${(value * 100).toFixed(places)}%`;

export const num = (value: number | null | undefined, places = 2): string =>
  value === null || value === undefined ? "—" : value.toFixed(places);

/** Sign-aware colour. Used everywhere a P&L figure appears. */
export const toneOf = (value: number | null | undefined): string =>
  value === null || value === undefined
    ? "text-[var(--text-muted)]"
    : value > 0 ? "text-gain" : value < 0 ? "text-loss" : "text-[var(--text)]";

export const SEVERITY_VARIANT: Record<string, "loss" | "warn" | "accent" | "neutral"> = {
  critical: "loss", high: "loss", medium: "warn", low: "accent",
};

export const CATEGORY_LABELS: Record<string, string> = {
  price: "Price", valuation: "Valuation", dcf_change: "DCF",
  score_change: "Score", risk: "Risk", management: "Management",
  document: "Document", quarterly_result: "Results",
  corporate_action: "Corporate action", portfolio: "Portfolio",
};

export const DIMENSION_LABELS: Record<string, string> = {
  sector: "Sector", industry: "Industry", market_cap: "Market cap",
  country: "Country", style: "Style",
};

/* ----------------------------------------------------------------- stats */

export function DeltaStat({
  label, value, delta, hint,
}: { label: string; value: ReactNode; delta?: number | null; hint?: ReactNode }) {
  return (
    <div className="min-w-0">
      <div className="text-[0.6875rem] font-medium uppercase tracking-wider text-[var(--text-muted)]">
        {label}
      </div>
      <div className={cn("num mt-1 truncate !text-xl font-semibold", toneOf(delta ?? null))}>
        {value}
      </div>
      {hint && (
        <div className="mt-0.5 truncate text-xs text-[var(--text-muted)]">{hint}</div>
      )}
    </div>
  );
}

/* --------------------------------------------------------------- treemap */

/**
 * Squarified treemap of holdings: area is weight, colour is return.
 *
 * A pie chart cannot show two variables at once, and a bar chart loses the
 * sense of the book as a whole. The treemap shows size and performance
 * together, which is the question a portfolio manager actually asks.
 */
export function HoldingsTreemap({
  holdings, width = 900, height = 420,
}: { holdings: Holding[]; width?: number; height?: number }) {
  const items = holdings
    .filter((h) => (h.market_value ?? 0) > 0)
    .sort((a, b) => (b.market_value ?? 0) - (a.market_value ?? 0));
  if (!items.length) {
    return <p className="p-6 text-sm text-[var(--text-muted)]">No priced holdings.</p>;
  }

  const total = items.reduce((sum, h) => sum + (h.market_value ?? 0), 0);
  const rects = squarify(
    items.map((h) => ({ item: h, value: (h.market_value ?? 0) / total })),
    0, 0, width, height,
  );

  return (
    <svg viewBox={`0 0 ${width} ${height}`} className="h-auto w-full"
         role="img" aria-label="Holdings treemap sized by weight, coloured by return">
      {rects.map(({ item, x, y, w, h }) => {
        const ret = item.unrealised_return ?? 0;
        const show = w > 54 && h > 30;
        return (
          <g key={item.ticker}>
            <title>
              {`${item.name} — ${pct(item.weight)} of book, `
                + `${money(item.market_value)}, ${pct(item.unrealised_return)} on cost`}
            </title>
            <rect
              x={x + 1} y={y + 1} width={Math.max(0, w - 2)} height={Math.max(0, h - 2)}
              fill={returnColour(ret)} rx={3}
              stroke="var(--bg)" strokeWidth={1}
            />
            {show && (
              <>
                <text x={x + 8} y={y + 19} className="fill-white text-[11px] font-semibold">
                  {item.ticker}
                </text>
                <text x={x + 8} y={y + 33} className="fill-white/85 text-[10px]">
                  {pct(item.weight)}
                </text>
                {h > 48 && (
                  <text x={x + 8} y={y + 47} className="fill-white/85 text-[10px]">
                    {pct(item.unrealised_return)}
                  </text>
                )}
              </>
            )}
          </g>
        );
      })}
    </svg>
  );
}

/** Diverging scale. Saturation tracks magnitude, capped at ±40%. */
function returnColour(ret: number): string {
  const capped = Math.max(-0.4, Math.min(0.4, ret));
  const intensity = Math.abs(capped) / 0.4;
  if (capped >= 0) {
    const light = 46 - intensity * 18;
    return `hsl(152 58% ${light}%)`;
  }
  const light = 52 - intensity * 16;
  return `hsl(358 62% ${light}%)`;
}

interface Weighted { item: Holding; value: number }
interface Placed { item: Holding; x: number; y: number; w: number; h: number }

/**
 * Squarified treemap layout (Bruls, Huizing & van Wijk).
 *
 * Chosen over a simple slice-and-dice because slice-and-dice produces long
 * thin slivers for small holdings, which are unreadable and misrepresent area
 * to the eye. Squarify keeps aspect ratios near 1.
 */
function squarify(
  items: Weighted[], x: number, y: number, width: number, height: number,
): Placed[] {
  const out: Placed[] = [];
  let remaining = [...items];
  let cx = x, cy = y, cw = width, ch = height;

  while (remaining.length) {
    const horizontal = cw >= ch;
    const side = horizontal ? ch : cw;
    const totalLeft = remaining.reduce((s, i) => s + i.value, 0);
    if (totalLeft <= 0) break;

    const row: Weighted[] = [];
    let best = Infinity;
    for (const candidate of remaining) {
      const trial = [...row, candidate];
      const ratio = worstRatio(trial, side, totalLeft, horizontal ? cw : ch);
      if (ratio > best) break;
      best = ratio;
      row.push(candidate);
    }

    const rowValue = row.reduce((s, i) => s + i.value, 0);
    const depth = (rowValue / totalLeft) * (horizontal ? cw : ch);
    let offset = 0;
    for (const entry of row) {
      const extent = (entry.value / rowValue) * side;
      out.push(horizontal
        ? { item: entry.item, x: cx, y: cy + offset, w: depth, h: extent }
        : { item: entry.item, x: cx + offset, y: cy, w: extent, h: depth });
      offset += extent;
    }

    if (horizontal) { cx += depth; cw -= depth; } else { cy += depth; ch -= depth; }
    remaining = remaining.slice(row.length);
    if (cw <= 0.5 || ch <= 0.5) break;
  }
  return out;
}

function worstRatio(
  row: Weighted[], side: number, totalLeft: number, extent: number,
): number {
  const sum = row.reduce((s, i) => s + i.value, 0);
  if (sum <= 0) return Infinity;
  const depth = (sum / totalLeft) * extent;
  let worst = 0;
  for (const entry of row) {
    const length = (entry.value / sum) * side;
    worst = Math.max(worst, Math.max(depth / length, length / depth));
  }
  return worst;
}

/* ------------------------------------------------------------------- pie */

export function AllocationPie({
  allocation, size = 200,
}: { allocation: Allocation; size?: number }) {
  const slices = allocation.slices.filter((s) => s.weight > 0);
  if (!slices.length) return null;

  const radius = size / 2 - 4;
  const centre = size / 2;
  let angle = -Math.PI / 2;

  return (
    <div className="flex flex-wrap items-center gap-4">
      <svg width={size} height={size} role="img"
           aria-label={`${allocation.dimension} allocation`}>
        {slices.map((slice, index) => {
          const sweep = slice.weight * 2 * Math.PI;
          const start = angle;
          angle += sweep;
          // A single 100% slice cannot be drawn as an arc — the start and end
          // points coincide, so the path collapses. Draw a circle instead.
          if (slice.weight >= 0.9999) {
            return (
              <circle key={slice.key} cx={centre} cy={centre} r={radius}
                      fill={sliceColour(index)} />
            );
          }
          const x1 = centre + radius * Math.cos(start);
          const y1 = centre + radius * Math.sin(start);
          const x2 = centre + radius * Math.cos(angle);
          const y2 = centre + radius * Math.sin(angle);
          const large = sweep > Math.PI ? 1 : 0;
          return (
            <path
              key={slice.key}
              d={`M ${centre} ${centre} L ${x1} ${y1} A ${radius} ${radius} 0 ${large} 1 ${x2} ${y2} Z`}
              fill={sliceColour(index)} stroke="var(--bg)" strokeWidth={1.5}
            >
              <title>{`${slice.label} — ${pct(slice.weight)} (${money(slice.market_value)})`}</title>
            </path>
          );
        })}
      </svg>
      <div className="min-w-[180px] flex-1 space-y-1">
        {slices.slice(0, 9).map((slice, index) => (
          <div key={slice.key} className="flex items-center gap-2 text-xs">
            <span className="inline-block h-2.5 w-2.5 shrink-0 rounded-sm"
                  style={{ background: sliceColour(index) }} />
            <span className="truncate text-[var(--text)]">{slice.label}</span>
            <span className="ml-auto shrink-0 font-mono text-[var(--text-muted)]">
              {pct(slice.weight)}
            </span>
            {slice.target_weight !== null && (
              <span className={cn(
                "shrink-0 font-mono text-[10px]",
                Math.abs(slice.drift ?? 0) > 0.02 ? "text-warn" : "text-[var(--text-muted)]",
              )}>
                {(slice.drift ?? 0) >= 0 ? "+" : ""}{pct(slice.drift, 1)}
              </span>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

const PALETTE = [
  "#2563eb", "#059669", "#d97706", "#7c3aed", "#dc2626", "#0891b2",
  "#ca8a04", "#4f46e5", "#16a34a", "#c2410c", "#0d9488", "#9333ea",
];
const sliceColour = (index: number) => PALETTE[index % PALETTE.length];

/* --------------------------------------------------------------- heatmap */

export function SectorHeatmap({ allocation }: { allocation: Allocation }) {
  const slices = allocation.slices.filter((s) => s.weight > 0);
  if (!slices.length) return null;
  const peak = Math.max(...slices.map((s) => Math.abs(s.unrealised_pnl ?? 0)), 1);

  return (
    <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-4">
      {slices.map((slice) => {
        const pnl = slice.unrealised_pnl ?? 0;
        const intensity = Math.min(1, Math.abs(pnl) / peak);
        const background = pnl >= 0
          ? `hsla(152, 58%, 42%, ${0.12 + intensity * 0.5})`
          : `hsla(358, 62%, 48%, ${0.12 + intensity * 0.5})`;
        return (
          <div
            key={slice.key}
            className="rounded-lg border border-[var(--border)] p-3"
            style={{ background }}
            title={`${slice.label}: ${slice.position_count} position(s), ${money(slice.market_value)}`}
          >
            <div className="truncate text-xs font-medium text-[var(--text)]">
              {slice.label}
            </div>
            <div className="num mt-1 text-sm font-semibold text-[var(--text)]">
              {pct(slice.weight)}
            </div>
            <div className={cn("mt-0.5 text-[11px] font-medium", toneOf(pnl))}>
              {pnl >= 0 ? "+" : ""}{money(pnl, true)}
            </div>
          </div>
        );
      })}
    </div>
  );
}

/* ------------------------------------------------------------ line chart */

export function ValueChart({
  series, width = 900, height = 260, format = "money",
}: {
  series: SeriesPoint[];
  width?: number; height?: number;
  /**
   * Axis formatting. The rolling-returns chart passes "percent": reusing the
   * money formatter there rendered every gridline as "₹1", because rolling
   * returns are ratios around 1.0 and `money(..., true)` rounds them all to
   * the same string.
   */
  format?: "money" | "ratio";
}) {
  if (series.length < 2) {
    return (
      <p className="p-6 text-sm text-[var(--text-muted)]">
        At least two valuation snapshots are needed to draw a performance
        chart. Record a snapshot to begin the series.
      </p>
    );
  }
  const pad = { top: 12, right: 12, bottom: 24, left: 58 };
  const innerW = width - pad.left - pad.right;
  const innerH = height - pad.top - pad.bottom;

  const values = series.map((p) => p.value);
  const low = Math.min(...values);
  const high = Math.max(...values);
  const span = high - low || 1;

  const px = (i: number) => pad.left + (i / (series.length - 1)) * innerW;
  const py = (v: number) => pad.top + innerH - ((v - low) / span) * innerH;
  const path = series.map((p, i) => `${i ? "L" : "M"} ${px(i)} ${py(p.value)}`).join(" ");
  const area = `${path} L ${px(series.length - 1)} ${pad.top + innerH} L ${pad.left} ${pad.top + innerH} Z`;

  const ticks = 4;
  return (
    <svg viewBox={`0 0 ${width} ${height}`} className="h-auto w-full"
         role="img" aria-label="Portfolio value over time">
      {Array.from({ length: ticks + 1 }, (_, i) => {
        const value = low + (span * i) / ticks;
        const y = py(value);
        return (
          <g key={i}>
            <line x1={pad.left} y1={y} x2={width - pad.right} y2={y}
                  stroke="var(--border)" strokeWidth={0.5} />
            <text x={pad.left - 6} y={y + 3} textAnchor="end"
                  className="fill-[var(--text-muted)] text-[9px]">
              {format === "ratio" ? pct(value - 1, 1) : money(value, true)}
            </text>
          </g>
        );
      })}
      <path d={area} fill="url(#pfGradient)" opacity={0.25} />
      <path d={path} fill="none" stroke="var(--color-accent-500, #2563eb)" strokeWidth={1.8} />
      <defs>
        <linearGradient id="pfGradient" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="#2563eb" stopOpacity={0.7} />
          <stop offset="100%" stopColor="#2563eb" stopOpacity={0} />
        </linearGradient>
      </defs>
      {[0, Math.floor(series.length / 2), series.length - 1].map((i) => (
        <text key={i} x={px(i)} y={height - 6} textAnchor="middle"
              className="fill-[var(--text-muted)] text-[9px]">
          {series[i].as_of.slice(0, 7)}
        </text>
      ))}
    </svg>
  );
}

export function UnderwaterChart({
  points, width = 900, height = 150,
}: { points: { as_of: string; value: number }[]; width?: number; height?: number }) {
  if (points.length < 2) return null;
  const pad = { top: 8, right: 12, bottom: 20, left: 58 };
  const innerW = width - pad.left - pad.right;
  const innerH = height - pad.top - pad.bottom;
  const worst = Math.min(...points.map((p) => p.value), -0.01);

  const px = (i: number) => pad.left + (i / (points.length - 1)) * innerW;
  const py = (v: number) => pad.top + (v / worst) * innerH;
  const path = points.map((p, i) => `${i ? "L" : "M"} ${px(i)} ${py(p.value)}`).join(" ");

  return (
    <svg viewBox={`0 0 ${width} ${height}`} className="h-auto w-full"
         role="img" aria-label="Drawdown from running peak">
      <path d={`${path} L ${px(points.length - 1)} ${pad.top} L ${pad.left} ${pad.top} Z`}
            fill="hsla(358,62%,48%,0.22)" />
      <path d={path} fill="none" stroke="hsl(358 62% 52%)" strokeWidth={1.4} />
      <text x={pad.left - 6} y={pad.top + 4} textAnchor="end"
            className="fill-[var(--text-muted)] text-[9px]">0%</text>
      <text x={pad.left - 6} y={pad.top + innerH} textAnchor="end"
            className="fill-[var(--text-muted)] text-[9px]">{pct(worst)}</text>
    </svg>
  );
}

/* -------------------------------------------------------------- holdings */

export function HoldingsTable({ holdings }: { holdings: Holding[] }) {
  if (!holdings.length) {
    return <p className="p-4 text-sm text-[var(--text-muted)]">No open positions.</p>;
  }
  return (
    <div className="scroll-x">
      <table className="w-full text-sm pin-first">
        <thead>
          <tr className="border-b border-[var(--border)] text-left text-xs uppercase tracking-wide text-[var(--text-muted)]">
            <th className="px-3 py-2 font-medium">Holding</th>
            <th className="px-3 py-2 text-right font-medium">Qty</th>
            <th className="px-3 py-2 text-right font-medium">Avg cost</th>
            <th className="px-3 py-2 text-right font-medium">Price</th>
            <th className="px-3 py-2 text-right font-medium">Value</th>
            <th className="px-3 py-2 text-right font-medium">P&amp;L</th>
            <th className="px-3 py-2 text-right font-medium">Return</th>
            <th className="px-3 py-2 text-right font-medium">Weight</th>
            <th className="px-3 py-2 text-center font-medium">Grade</th>
            <th className="px-3 py-2 text-right font-medium">Upside</th>
          </tr>
        </thead>
        <tbody>
          {holdings.map((h) => (
            <tr key={h.ticker} className="border-b border-[var(--border)]">
              <td className="px-3 py-2">
                <div className="font-medium text-[var(--text)]">{h.ticker}</div>
                <div className="truncate text-xs text-[var(--text-muted)]">
                  {h.sector ?? "—"}
                </div>
              </td>
              <td className="num px-3 py-2 text-right">{h.quantity.toLocaleString("en-IN")}</td>
              <td className="num px-3 py-2 text-right">{money(h.average_cost)}</td>
              <td className="num px-3 py-2 text-right">{money(h.current_price)}</td>
              <td className="num px-3 py-2 text-right">{money(h.market_value)}</td>
              <td className={cn("num px-3 py-2 text-right", toneOf(h.unrealised_pnl))}>
                {money(h.unrealised_pnl)}
              </td>
              <td className={cn("num px-3 py-2 text-right", toneOf(h.unrealised_return))}>
                {pct(h.unrealised_return)}
              </td>
              <td className="px-3 py-2 text-right">
                <span className="num">{pct(h.weight)}</span>
                {h.is_oversized && (
                  <span className="ml-1 text-warn" title={
                    `Above the ${pct(h.max_position_size)} cap for a `
                    + `${h.rating ?? "unrated"} holding`
                  }>▲</span>
                )}
              </td>
              <td className="px-3 py-2 text-center">
                {h.rating
                  ? <Badge variant={gradeVariant(h.rating)}>{h.rating}</Badge>
                  : <span className="text-xs text-[var(--text-muted)]">—</span>}
              </td>
              <td className={cn("num px-3 py-2 text-right", toneOf(h.upside))}>
                {h.upside === null
                  ? <span className="text-xs text-[var(--text-muted)]" title=
                      "No certified valuation — the data-quality gate declined one.">—</span>
                  : pct(h.upside)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const gradeVariant = (grade: string): "gain" | "accent" | "warn" | "loss" => {
  if (["AAA", "AA"].includes(grade)) return "gain";
  if (grade === "A") return "accent";
  if (["BBB", "BB"].includes(grade)) return "warn";
  return "loss";
};

/* ------------------------------------------------------------------ risk */

export function RiskGrid({ risk }: { risk: RiskProfile }) {
  const tiles: { label: string; value: string; hint?: string }[] = [
    { label: "Volatility", value: pct(risk.annualised_volatility), hint: "annualised" },
    { label: "Sharpe", value: num(risk.sharpe), hint: "excess / total vol" },
    { label: "Sortino", value: num(risk.sortino), hint: "excess / downside vol" },
    { label: "Max drawdown", value: pct(risk.max_drawdown),
      hint: risk.drawdown_recovered === null ? undefined
        : risk.drawdown_recovered ? "recovered" : "not recovered" },
    { label: "VaR 95%", value: pct(risk.var_95, 2), hint: "daily, historical" },
    { label: "CVaR 95%", value: pct(risk.cvar_95, 2), hint: "mean of the tail" },
    { label: "Beta", value: num(risk.beta), hint: "vs benchmark" },
    { label: "Alpha", value: pct(risk.alpha), hint: "Jensen, annualised" },
    { label: "Tracking error", value: pct(risk.tracking_error) },
    { label: "Information ratio", value: num(risk.information_ratio) },
    { label: "Effective positions", value: num(risk.effective_positions, 1), hint: "1 / HHI" },
    { label: "Top-5 weight", value: pct(risk.top_5_concentration) },
  ];
  return (
    <>
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
        {tiles.map((tile) => (
          <div key={tile.label} className="rounded-lg border border-[var(--border)] p-3">
            <div className="text-[0.6875rem] font-medium uppercase tracking-wider text-[var(--text-muted)]">
              {tile.label}
            </div>
            <div className="num mt-1 text-lg font-semibold text-[var(--text)]">
              {tile.value}
            </div>
            {tile.hint && (
              <div className="text-[10px] text-[var(--text-muted)]">{tile.hint}</div>
            )}
          </div>
        ))}
      </div>
      {risk.unavailable.length > 0 && (
        <div className="mt-3 rounded-lg border border-[var(--border)] bg-[var(--bg-subtle)] p-3">
          <div className="mb-1 flex items-center gap-1.5 text-xs font-semibold text-[var(--text)]">
            <Info className="h-3.5 w-3.5" />
            Statistics that could not be computed
          </div>
          <ul className="space-y-0.5 text-xs text-[var(--text-muted)]">
            {risk.unavailable.map((gap) => <li key={gap}>· {gap}</li>)}
          </ul>
        </div>
      )}
    </>
  );
}

/* ---------------------------------------------------------------- alerts */

export function AlertList({
  alerts, showClear = false,
}: { alerts: AlertEvaluation[]; showClear?: boolean }) {
  const shown = showClear ? alerts : alerts.filter((a) => a.status === "triggered");
  if (!shown.length) {
    return (
      <div className="flex items-center gap-2 p-4 text-sm text-[var(--text-muted)]">
        <CheckCircle2 className="h-4 w-4 text-gain" />
        No alert rules are currently triggered.
      </div>
    );
  }
  return (
    <div className="space-y-1.5">
      {shown.map((alert, index) => (
        <div
          key={`${alert.key}-${alert.ticker ?? "pf"}-${index}`}
          className="flex items-start gap-2.5 rounded-lg border border-[var(--border)] p-2.5"
        >
          <Badge variant={SEVERITY_VARIANT[alert.severity] ?? "neutral"}>
            {alert.severity}
          </Badge>
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-baseline gap-x-2">
              <span className="text-sm font-medium text-[var(--text)]">{alert.label}</span>
              {alert.ticker && (
                <span className="font-mono text-xs text-accent-500">{alert.ticker}</span>
              )}
              <Badge variant="neutral">{CATEGORY_LABELS[alert.category] ?? alert.category}</Badge>
              {alert.status === "unavailable" && (
                <Badge variant="warn">not evaluated</Badge>
              )}
            </div>
            <div className="mt-0.5 text-xs text-[var(--text-muted)]">
              {alert.condition}
              {alert.observed !== null && alert.threshold !== null && (
                <span className="ml-2 font-mono">
                  {formatObserved(alert.observed)} vs {formatObserved(alert.threshold)}
                </span>
              )}
            </div>
            {alert.detail && (
              <div className="mt-0.5 text-xs italic text-[var(--text-muted)]">
                {alert.detail}
              </div>
            )}
          </div>
          <span className="shrink-0 text-xs text-[var(--text-muted)]">{alert.action}</span>
        </div>
      ))}
    </div>
  );
}

const formatObserved = (value: number | string | null): string => {
  if (value === null) return "—";
  if (typeof value === "string") return value;
  return Math.abs(value) < 10 ? value.toFixed(4) : value.toLocaleString("en-IN");
};

/* ----------------------------------------------------------- rebalancing */

export function RebalanceTable({ trades }: { trades: RebalanceTrade[] }) {
  if (!trades.length) {
    return (
      <p className="p-4 text-sm text-[var(--text-muted)]">
        No position breaches its ceiling and no target drift exceeds the
        two-percent band.
      </p>
    );
  }
  return (
    <div className="scroll-x">
      <table className="w-full text-sm pin-first">
        <thead>
          <tr className="border-b border-[var(--border)] text-left text-xs uppercase tracking-wide text-[var(--text-muted)]">
            <th className="px-3 py-2 font-medium">Action</th>
            <th className="px-3 py-2 font-medium">Holding</th>
            <th className="px-3 py-2 text-right font-medium">Now</th>
            <th className="px-3 py-2 text-right font-medium">Target</th>
            <th className="px-3 py-2 text-right font-medium">Trade</th>
            <th className="px-3 py-2 text-right font-medium">Shares</th>
            <th className="px-3 py-2 font-medium">Why</th>
          </tr>
        </thead>
        <tbody>
          {trades.map((trade) => (
            <tr key={`${trade.ticker}-${trade.action}`} className="border-b border-[var(--border)]">
              <td className="px-3 py-2">
                <Badge variant={trade.action === "reduce" ? "loss" : "gain"}>
                  {trade.action === "reduce"
                    ? <TrendingDown className="mr-1 inline h-3 w-3" />
                    : <TrendingUp className="mr-1 inline h-3 w-3" />}
                  {trade.action}
                </Badge>
              </td>
              <td className="px-3 py-2 font-medium text-[var(--text)]">{trade.ticker}</td>
              <td className="num px-3 py-2 text-right">{pct(trade.current_weight)}</td>
              <td className="num px-3 py-2 text-right">{pct(trade.target_weight)}</td>
              <td className={cn("num px-3 py-2 text-right", toneOf(trade.value_delta))}>
                {money(Math.abs(trade.value_delta))}
              </td>
              <td className="num px-3 py-2 text-right">
                {trade.shares === null ? "—" : Math.abs(trade.shares).toLocaleString("en-IN")}
              </td>
              <td className="px-3 py-2 text-xs text-[var(--text-muted)]">{trade.reason}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/* ----------------------------------------------------------- attribution */

export function AttributionTable({ rows }: { rows: AttributionRow[] }) {
  if (!rows.length) {
    return <p className="p-4 text-sm text-[var(--text-muted)]">No attribution data.</p>;
  }
  return (
    <div className="scroll-x">
      <table className="w-full text-sm pin-first">
        <thead>
          <tr className="border-b border-[var(--border)] text-left text-xs uppercase tracking-wide text-[var(--text-muted)]">
            <th className="px-3 py-2 font-medium">Segment</th>
            <th className="px-3 py-2 text-right font-medium">Active wt</th>
            <th className="px-3 py-2 text-right font-medium">Allocation</th>
            <th className="px-3 py-2 text-right font-medium">Selection</th>
            <th className="px-3 py-2 text-right font-medium">Interaction</th>
            <th className="px-3 py-2 text-right font-medium">Total</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.key} className="border-b border-[var(--border)]">
              <td className="px-3 py-2 text-[var(--text)]">{row.label}</td>
              <td className={cn("num px-3 py-2 text-right", toneOf(row.active_weight))}>
                {pct(row.active_weight)}
              </td>
              <td className={cn("num px-3 py-2 text-right", toneOf(row.allocation))}>
                {pct(row.allocation, 2)}
              </td>
              <td className={cn("num px-3 py-2 text-right", toneOf(row.selection))}>
                {pct(row.selection, 2)}
              </td>
              <td className={cn("num px-3 py-2 text-right", toneOf(row.interaction))}>
                {pct(row.interaction, 2)}
              </td>
              <td className={cn("num px-3 py-2 text-right font-semibold", toneOf(row.total))}>
                {pct(row.total, 2)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/* ------------------------------------------------------------- watchlist */

export function WatchlistTable({
  rows, onRemove,
}: { rows: WatchlistRow[]; onRemove?: (id: number) => void }) {
  if (!rows.length) {
    return <p className="p-4 text-sm text-[var(--text-muted)]">Nothing on this list.</p>;
  }
  const statusVariant: Record<string, "gain" | "warn" | "neutral" | "loss"> = {
    triggered: "gain", approaching: "warn", watching: "neutral", expensive: "loss",
  };
  return (
    <div className="scroll-x">
      <table className="w-full text-sm pin-first">
        <thead>
          <tr className="border-b border-[var(--border)] text-left text-xs uppercase tracking-wide text-[var(--text-muted)]">
            <th className="px-3 py-2 font-medium">Company</th>
            <th className="px-3 py-2 text-right font-medium">Price</th>
            <th className="px-3 py-2 text-right font-medium">Buy below</th>
            <th className="px-3 py-2 text-right font-medium">Target</th>
            <th className="px-3 py-2 text-right font-medium">Upside</th>
            <th className="px-3 py-2 text-center font-medium">Grade</th>
            <th className="px-3 py-2 text-center font-medium">Status</th>
            {onRemove && <th className="px-3 py-2" />}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.id} className="border-b border-[var(--border)]">
              <td className="px-3 py-2">
                <div className="font-medium text-[var(--text)]">{row.ticker}</div>
                <div className="truncate text-xs text-[var(--text-muted)]">{row.name}</div>
              </td>
              <td className="num px-3 py-2 text-right">{money(row.price)}</td>
              <td className="num px-3 py-2 text-right">{money(row.buy_below)}</td>
              <td className="num px-3 py-2 text-right">{money(row.target_price)}</td>
              <td className={cn("num px-3 py-2 text-right", toneOf(row.upside))}>
                {pct(row.upside)}
              </td>
              <td className="px-3 py-2 text-center">
                {row.rating
                  ? <Badge variant={gradeVariant(row.rating)}>{row.rating}</Badge>
                  : <span className="text-xs text-[var(--text-muted)]">—</span>}
              </td>
              <td className="px-3 py-2 text-center">
                <Badge variant={statusVariant[row.status] ?? "neutral"}>{row.status}</Badge>
              </td>
              {onRemove && (
                <td className="px-3 py-2 text-right">
                  <button type="button" onClick={() => onRemove(row.id)}
                          className="text-xs text-[var(--text-muted)] hover:text-loss">
                    remove
                  </button>
                </td>
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/* ------------------------------------------------------------------ note */

export function Note({
  children, tone = "info",
}: { children: ReactNode; tone?: "info" | "warning" }) {
  return (
    <div className={cn(
      "flex items-start gap-2 rounded-lg border p-3 text-xs",
      tone === "warning"
        ? "border-warn/40 bg-warn/10 text-[var(--text)]"
        : "border-[var(--border)] bg-[var(--bg-subtle)] text-[var(--text-muted)]",
    )}>
      {tone === "warning"
        ? <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-warn" />
        : <Info className="mt-0.5 h-3.5 w-3.5 shrink-0" />}
      <div>{children}</div>
    </div>
  );
}
