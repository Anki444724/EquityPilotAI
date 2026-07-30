"use client";

import { Badge, Card, CardBody, CardHeader, Stat } from "@/components/ui";
import { crore, EM_DASH, multiple, percent, rupees } from "@/lib/format";
import type {
  DCFOut, RelativeOut, SensitivityOut, SimulationOut, SummaryOut, WACCOut,
  WACCScheduleRow,
} from "@/lib/types";
import { cn } from "@/lib/utils";

/** Valuation football field: each method's value against the market price. */
export function FootballField({ summary }: { summary: SummaryOut }) {
  const applicable = summary.methods.filter((m) => m.applicable && m.value_per_share);
  if (!applicable.length) return null;

  const values = applicable.map((m) => m.value_per_share as number);
  const price = summary.current_price ?? 0;
  const lo = Math.min(...values, price) * 0.9;
  const hi = Math.max(...values, price) * 1.1;
  const span = hi - lo || 1;
  const pos = (v: number) => ((v - lo) / span) * 100;

  return (
    <Card>
      <CardHeader
        title="Valuation range by method"
        subtitle="Bars show each methodology; the dashed line is the market price"
      />
      <CardBody className="space-y-3">
        {applicable.map((m) => {
          const v = m.value_per_share as number;
          const above = price > 0 && v >= price;
          return (
            <div key={m.key}>
              <div className="flex items-baseline justify-between gap-2 text-xs">
                <span className="truncate">{m.label}</span>
                <span className="num shrink-0 text-[var(--text-muted)]">
                  {rupees(v)}
                  {m.upside !== null && (
                    <span className={cn("ml-2", m.upside >= 0 ? "text-gain" : "text-loss")}>
                      {percent(m.upside, 0)}
                    </span>
                  )}
                  <span className="ml-2 opacity-60">w {percent(m.weight, 0)}</span>
                </span>
              </div>
              <div className="relative mt-1 h-2.5 rounded-full bg-[var(--bg-subtle)]">
                <div
                  className={cn("absolute h-full rounded-full", above ? "bg-gain" : "bg-loss")}
                  style={{ left: `${Math.min(pos(0), pos(v))}%`, width: `${Math.max(1.5, pos(v) - pos(lo) - pos(0) + pos(lo))}%` }}
                />
                {price > 0 && (
                  <div
                    className="absolute top-[-3px] h-[calc(100%+6px)] w-0.5 bg-[var(--text)]"
                    style={{ left: `${pos(price)}%` }}
                  />
                )}
              </div>
            </div>
          );
        })}
        {summary.weighted_value && (
          <div className="border-t border-[var(--border)] pt-3">
            <div className="flex items-baseline justify-between text-xs">
              <span className="font-semibold">Weighted conclusion</span>
              <span className="num font-semibold">{rupees(summary.weighted_value)}</span>
            </div>
          </div>
        )}
      </CardBody>
    </Card>
  );
}

/** WACC build-up, component by component. */
export function WACCPanel({
  wacc, schedule,
}: {
  wacc: WACCOut;
  schedule: WACCScheduleRow[];
}) {
  const rows: [string, string, string][] = [
    ["Risk-free rate", percent(wacc.risk_free_rate, 2), "10-year G-Sec"],
    ["Equity risk premium", percent(wacc.total_erp, 2), "mature ERP + country premium"],
    ["Unlevered beta", multiple(wacc.unlevered_beta), "sector asset beta"],
    ["Levered beta", multiple(wacc.levered_beta), `relevered at D/E ${multiple(wacc.debt_to_equity)}`],
    ["Beta used", multiple(wacc.beta_used), wacc.beta_source.replace("_", " ")],
    ["Size premium", percent(wacc.size_premium, 2), ""],
    ["Company-specific premium", percent(wacc.specific_premium, 2), ""],
    ["Cost of equity", percent(wacc.cost_of_equity, 2), "CAPM + adjustments"],
    ["Pre-tax cost of debt", percent(wacc.pre_tax_cost_of_debt, 2), ""],
    ["Tax rate", percent(wacc.marginal_tax_rate, 2), ""],
    ["After-tax cost of debt", percent(wacc.after_tax_cost_of_debt, 2), "tax shield applied"],
    ["Weight — equity", percent(wacc.weight_equity, 1), crore(wacc.market_value_equity)],
    ["Weight — debt", percent(wacc.weight_debt, 1), crore(wacc.market_value_debt)],
  ];

  return (
    <div className="space-y-5">
      <Card>
        <CardHeader
          title="Cost of capital build"
          action={<Badge variant="accent">WACC {percent(wacc.wacc, 2)}</Badge>}
        />
        <div className="overflow-x-auto">
          <table className="grid-table">
            <thead>
              <tr><th className="!text-left">Component</th><th>Value</th><th className="!text-left">Basis</th></tr>
            </thead>
            <tbody>
              {rows.map(([label, value, basis]) => (
                <tr key={label} className={cn(label.startsWith("Cost of") && "is-subtotal")}>
                  <td className="sticky-col">{label}</td>
                  <td className="num">{value}</td>
                  <td className="!text-left text-[0.6875rem] text-[var(--text-muted)]">{basis}</td>
                </tr>
              ))}
              <tr className="is-subtotal">
                <td className="sticky-col font-semibold">WACC</td>
                <td className="num font-semibold">{percent(wacc.wacc, 2)}</td>
                <td className="!text-left text-[0.6875rem] text-[var(--text-muted)]">
                  {wacc.bounded ? "bounded to a plausible range" : "weighted average"}
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </Card>

      {schedule.length > 0 && (
        <Card>
          <CardHeader title="Dynamic WACC" subtitle="Recomputed as the capital structure evolves" />
          <div className="overflow-x-auto">
            <table className="grid-table">
              <thead>
                <tr><th className="!text-left">Period</th><th>D/E</th><th>Levered beta</th><th>Cost of equity</th><th>WACC</th></tr>
              </thead>
              <tbody>
                {schedule.map((s) => (
                  <tr key={s.period}>
                    <td className="sticky-col num">FY+{s.period}</td>
                    <td className="num">{multiple(s.debt_to_equity)}</td>
                    <td className="num">{multiple(s.levered_beta)}</td>
                    <td className="num">{percent(s.cost_of_equity, 2)}</td>
                    <td className="num font-medium">{percent(s.wacc, 2)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}
    </div>
  );
}

/** DCF walk-through: discounting, terminal value and the equity bridge. */
export function DCFPanel({ dcf, title }: { dcf: DCFOut; title: string }) {
  return (
    <Card>
      <CardHeader
        title={title}
        subtitle={`${dcf.convention.replace("_", "-")} discounting · ${dcf.terminal_method.replace("_", " ")}`}
        action={
          dcf.intrinsic_value_per_share !== null ? (
            <Badge variant={(dcf.upside ?? 0) >= 0 ? "gain" : "loss"}>
              {rupees(dcf.intrinsic_value_per_share)} · {percent(dcf.upside, 0)}
            </Badge>
          ) : undefined
        }
      />
      <div className="overflow-x-auto">
        <table className="grid-table">
          <thead>
            <tr>
              <th className="!text-left">Period</th><th>Cash flow</th><th>t</th>
              <th>Rate</th><th>Discount factor</th><th>Present value</th>
            </tr>
          </thead>
          <tbody>
            {dcf.years.map((y) => (
              <tr key={y.period}>
                <td className="sticky-col num">FY+{y.period}</td>
                <td className={cn("num", y.cash_flow < 0 && "text-loss")}>{crore(y.cash_flow)}</td>
                <td className="num">{y.discount_period.toFixed(1)}</td>
                <td className="num">{percent(y.discount_rate, 2)}</td>
                <td className="num">{y.discount_factor.toFixed(4)}</td>
                <td className={cn("num", y.present_value < 0 && "text-loss")}>
                  {crore(y.present_value)}
                </td>
              </tr>
            ))}
            <tr className="is-subtotal">
              <td className="sticky-col" colSpan={5}>Sum of PV — explicit period</td>
              <td className="num">{crore(dcf.sum_pv_explicit)}</td>
            </tr>
            <tr>
              <td className="sticky-col" colSpan={5}>Terminal value</td>
              <td className="num">{crore(dcf.terminal_value)}</td>
            </tr>
            <tr>
              <td className="sticky-col" colSpan={5}>
                PV of terminal value
                {dcf.terminal_value_pct !== null && (
                  <span className="ml-2 text-[0.6875rem] text-[var(--text-muted)]">
                    {percent(dcf.terminal_value_pct, 0)} of EV
                  </span>
                )}
              </td>
              <td className="num">{crore(dcf.pv_terminal_value)}</td>
            </tr>
            <tr className="is-subtotal">
              <td className="sticky-col" colSpan={5}>Enterprise value</td>
              <td className="num">{crore(dcf.enterprise_value)}</td>
            </tr>
            {dcf.net_debt !== null && (
              <tr>
                <td className="sticky-col" colSpan={5}>Less: net debt</td>
                <td className="num text-loss">{crore(-dcf.net_debt)}</td>
              </tr>
            )}
            <tr className="is-subtotal">
              <td className="sticky-col" colSpan={5}>Equity value</td>
              <td className="num">{crore(dcf.equity_value)}</td>
            </tr>
            <tr className="is-subtotal">
              <td className="sticky-col" colSpan={5}>Intrinsic value per share</td>
              <td className="num font-semibold">
                {dcf.intrinsic_value_per_share === null ? EM_DASH : rupees(dcf.intrinsic_value_per_share)}
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      {dcf.warnings.length > 0 && (
        <CardBody className="space-y-1 border-t border-[var(--border)] pt-3">
          {dcf.warnings.map((w) => (
            <p key={w} className="text-[0.6875rem] text-warn">⚠ {w}</p>
          ))}
        </CardBody>
      )}
    </Card>
  );
}

/** Multiples, target prices and justified multiples. */
export function RelativePanel({ relative }: { relative: RelativeOut }) {
  const periods = [relative.current, ...relative.forward];
  const metrics: [string, keyof typeof relative.current, string][] = [
    ["P/E", "pe", "x"], ["P/B", "pb", "x"], ["EV/EBITDA", "ev_ebitda", "x"],
    ["EV/Sales", "ev_sales", "x"], ["EV/EBIT", "ev_ebit", "x"], ["P/FCFE", "p_fcfe", "x"],
  ];

  return (
    <div className="space-y-5">
      <Card>
        <CardHeader title="Trading multiples" subtitle="Trailing and forward" />
        <div className="overflow-x-auto">
          <table className="grid-table">
            <thead>
              <tr>
                <th className="!text-left">Multiple</th>
                {periods.map((p) => <th key={p.label}>{p.label}</th>)}
              </tr>
            </thead>
            <tbody>
              {metrics.map(([label, key]) => (
                <tr key={label}>
                  <td className="sticky-col">{label}</td>
                  {periods.map((p) => (
                    <td key={p.label} className="num">
                      {p[key] === null || p[key] === undefined
                        ? EM_DASH : multiple(p[key] as number)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>

      <Card>
        <CardHeader
          title="Target price by method"
          action={
            relative.blended_target_price ? (
              <Badge variant="accent">Blended {rupees(relative.blended_target_price)}</Badge>
            ) : undefined
          }
        />
        <div className="overflow-x-auto">
          <table className="grid-table">
            <thead>
              <tr>
                <th className="!text-left">Method</th><th>Multiple</th>
                <th className="!text-left">Metric</th><th>Target price</th>
                <th>Weight</th><th className="!text-left">Rationale</th>
              </tr>
            </thead>
            <tbody>
              {relative.methods.map((m) => (
                <tr key={m.key}>
                  <td className="sticky-col">{m.label}</td>
                  <td className="num">{m.target_multiple === null ? EM_DASH : multiple(m.target_multiple)}</td>
                  <td className="!text-left text-[0.6875rem] text-[var(--text-muted)]">{m.metric_label}</td>
                  <td className="num font-medium">{m.target_price === null ? EM_DASH : rupees(m.target_price)}</td>
                  <td className="num">{percent(m.weight, 0)}</td>
                  <td className="!text-left text-[0.6875rem] text-[var(--text-muted)]">{m.rationale}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>

      <Card>
        <CardHeader
          title="Justified multiples"
          subtitle="What fundamentals warrant, derived from the Gordon model — not borrowed from peers"
        />
        <div className="overflow-x-auto">
          <table className="grid-table">
            <thead>
              <tr>
                <th className="!text-left">Multiple</th><th className="!text-left">Formula</th>
                <th>Justified</th><th>Actual</th><th>Premium</th><th className="!text-left">Verdict</th>
              </tr>
            </thead>
            <tbody>
              {relative.justified.map((j) => (
                <tr key={j.key}>
                  <td className="sticky-col">{j.label}</td>
                  <td className="!text-left font-mono text-[0.625rem] text-[var(--text-muted)]">{j.formula}</td>
                  <td className="num">{j.justified === null ? EM_DASH : multiple(j.justified)}</td>
                  <td className="num">{j.actual === null ? EM_DASH : multiple(j.actual)}</td>
                  <td className={cn("num", (j.premium_discount ?? 0) > 0 ? "text-loss" : "text-gain")}>
                    {j.premium_discount === null ? EM_DASH : percent(j.premium_discount, 0)}
                  </td>
                  <td className="!text-left text-[0.6875rem]">{j.verdict}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {relative.warnings.length > 0 && (
          <CardBody className="space-y-1 border-t border-[var(--border)] pt-3">
            {relative.warnings.map((w) => (
              <p key={w} className="text-[0.6875rem] text-warn">⚠ {w}</p>
            ))}
          </CardBody>
        )}
      </Card>
    </div>
  );
}

/** Colour-graded sensitivity matrix. */
export function SensitivityMatrix({ grid, showUpside }: { grid: SensitivityOut; showUpside: boolean }) {
  const cells = showUpside ? grid.upside_cells : grid.cells;
  const flat = cells.flat().filter((v): v is number => v !== null);
  const lo = Math.min(...flat);
  const hi = Math.max(...flat);

  const shade = (v: number | null) => {
    if (v === null || hi === lo) return undefined;
    const t = (v - lo) / (hi - lo);
    // red (low) → neutral → green (high)
    return t < 0.5
      ? `rgba(179,38,30,${(0.5 - t) * 0.5})`
      : `rgba(11,122,59,${(t - 0.5) * 0.5})`;
  };

  const fmtAxis = (v: number, unit: string) => (unit === "%" ? percent(v, 2) : multiple(v));
  const fmtCell = (v: number | null) =>
    v === null ? EM_DASH : showUpside ? percent(v, 0) : rupees(v, 0);

  return (
    <div className="overflow-x-auto">
      <table className="grid-table">
        <thead>
          <tr>
            <th className="!text-left">
              {grid.row_label} ↓ / {grid.col_label} →
            </th>
            {grid.col_values.map((c) => (
              <th key={c}>{fmtAxis(c, grid.col_unit)}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {grid.row_values.map((r, i) => (
            <tr key={r}>
              <td className="sticky-col num font-medium">{fmtAxis(r, grid.row_unit)}</td>
              {cells[i].map((v, j) => {
                const isBase = r === grid.base_row && grid.col_values[j] === grid.base_col;
                return (
                  <td
                    key={j}
                    className={cn("num", isBase && "font-bold ring-1 ring-inset ring-accent-500")}
                    style={{ background: shade(v) }}
                  >
                    {fmtCell(v)}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Monte Carlo distribution summary. */
export function SimulationPanel({ sim }: { sim: SimulationOut }) {
  const peak = Math.max(...sim.histogram.map((b) => b.count), 1);
  return (
    <div className="space-y-5">
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Card><CardBody><Stat label="Mean value" value={rupees(sim.mean_value)} hint={`${sim.trials} trials`} /></CardBody></Card>
        <Card><CardBody><Stat label="Median value" value={rupees(sim.median_value)} hint="50th percentile" /></CardBody></Card>
        <Card><CardBody><Stat label="Std deviation" value={rupees(sim.std_dev)} hint="dispersion" /></CardBody></Card>
        <Card><CardBody>
          <Stat
            label="P(value > price)"
            value={sim.probability_above_price === null ? EM_DASH : percent(sim.probability_above_price, 0)}
            tone={(sim.probability_above_price ?? 0) > 0.5 ? "gain" : "loss"}
            hint={sim.current_price ? `vs ${rupees(sim.current_price)}` : undefined}
          />
        </CardBody></Card>
      </div>

      <Card>
        <CardHeader title="Value distribution" subtitle={`${sim.trials} simulated valuations`} />
        <CardBody className="space-y-1">
          {sim.histogram.map((b, i) => (
            <div key={i} className="flex items-center gap-2">
              <span className="num w-20 shrink-0 text-right text-[0.625rem] text-[var(--text-muted)]">
                {rupees(b.lower, 0)}
              </span>
              <div className="h-3 flex-1 overflow-hidden rounded-sm bg-[var(--bg-subtle)]">
                <div className="h-full bg-accent-500" style={{ width: `${(b.count / peak) * 100}%` }} />
              </div>
              <span className="num w-10 shrink-0 text-[0.625rem] text-[var(--text-muted)]">{b.count}</span>
            </div>
          ))}
        </CardBody>
      </Card>

      <Card>
        <CardHeader title="Percentiles" />
        <div className="overflow-x-auto">
          <table className="grid-table">
            <thead>
              <tr>
                <th className="!text-left">Percentile</th>
                {Object.keys(sim.percentiles).map((p) => <th key={p}>P{p}</th>)}
              </tr>
            </thead>
            <tbody>
              <tr>
                <td className="sticky-col">Value per share</td>
                {Object.values(sim.percentiles).map((v, i) => (
                  <td key={i} className="num">{rupees(v, 0)}</td>
                ))}
              </tr>
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  );
}
