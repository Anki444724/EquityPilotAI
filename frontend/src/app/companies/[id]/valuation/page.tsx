"use client";

import { AppShell } from "@/components/layout/app-shell";
import { ScenarioChart, ValueRangeChart } from "@/components/charts";
import { QualityBanner } from "@/components/valuation/quality-banner";
import {
  DCFPanel, FootballField, RelativePanel, SensitivityMatrix, SimulationPanel,
  WACCPanel,
} from "@/components/valuation/panels";
import { Badge, Card, CardBody, CardHeader, EmptyState, Skeleton, Stat } from "@/components/ui";
import { api, valuationApi } from "@/lib/api";
import { EM_DASH, percent, rupees } from "@/lib/format";
import type { ScenarioName } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { use, useState } from "react";

const TABS = [
  { key: "dashboard", label: "Dashboard" },
  { key: "dcf", label: "DCF" },
  { key: "relative", label: "Relative" },
  { key: "wacc", label: "WACC" },
  { key: "sensitivity", label: "Sensitivity" },
  { key: "simulation", label: "Monte Carlo" },
] as const;
type TabKey = (typeof TABS)[number]["key"];

const AXES = [
  { value: "wacc", label: "WACC" },
  { value: "terminal_growth", label: "Terminal growth" },
  { value: "revenue_cagr", label: "Revenue CAGR" },
  { value: "ebit_margin", label: "EBIT margin" },
  { value: "exit_multiple", label: "Exit multiple" },
];

export default function ValuationPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const [tab, setTab] = useState<TabKey>("dashboard");
  const [horizon, setHorizon] = useState(5);
  const [scenario, setScenario] = useState<ScenarioName>("base");
  const [convention, setConvention] = useState("mid_year");
  const [terminalMethod, setTerminalMethod] = useState("perpetual_growth");
  const [row, setRow] = useState("wacc");
  const [col, setCol] = useState("terminal_growth");
  const [showUpside, setShowUpside] = useState(false);

  const profile = useQuery({
    queryKey: ["company-profile", id],
    queryFn: () => api.companyProfile(id),
  });
  const ticker = profile.data?.company.ticker;

  const valuation = useQuery({
    queryKey: ["valuation", ticker, horizon, scenario, convention, terminalMethod],
    queryFn: () => valuationApi.get(ticker!, {
      horizon, scenario, convention, terminal_method: terminalMethod,
      dynamic_wacc: true,
    }),
    enabled: Boolean(ticker),
  });

  const sensitivity = useQuery({
    queryKey: ["sensitivity", ticker, row, col, horizon],
    queryFn: () => valuationApi.sensitivity(ticker!, row, col, 2, horizon),
    enabled: Boolean(ticker) && tab === "sensitivity" && row !== col,
  });

  const simulation = useQuery({
    queryKey: ["simulation", ticker, horizon],
    queryFn: () => valuationApi.simulation(ticker!, 2000, horizon),
    enabled: Boolean(ticker) && tab === "simulation",
  });

  if (profile.isLoading) return <AppShell><Skeleton className="h-32" /></AppShell>;
  if (!profile.data) {
    return <AppShell><Card><EmptyState title="Company not found" /></Card></AppShell>;
  }

  const v = valuation.data;

  return (
    <AppShell>
      <div className="mb-4 flex flex-wrap items-end justify-between gap-3">
        <div>
          <div className="flex items-center gap-2 text-xs">
            <Link href={`/companies/${id}`} className="num text-accent-500 hover:underline">
              {profile.data.company.ticker}
            </Link>
            <span className="text-[var(--text-muted)]">/</span>
            <span className="text-[var(--text-muted)]">Valuation</span>
          </div>
          <h1 className="mt-1 text-lg font-semibold">{profile.data.company.name}</h1>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <div className="flex overflow-hidden rounded-md border border-[var(--border)]">
            {[3, 5, 10].map((h) => (
              <button key={h} onClick={() => setHorizon(h)}
                className={cn("px-2.5 py-1.5 text-xs", horizon === h ? "bg-accent-500 text-white" : "hover:bg-[var(--bg-subtle)]")}>
                {h}Y
              </button>
            ))}
          </div>
          <div className="flex overflow-hidden rounded-md border border-[var(--border)]">
            {(["bear", "base", "bull"] as ScenarioName[]).map((s) => (
              <button key={s} onClick={() => setScenario(s)}
                className={cn("px-2.5 py-1.5 text-xs capitalize",
                  scenario === s
                    ? s === "bull" ? "bg-gain text-white" : s === "bear" ? "bg-loss text-white" : "bg-accent-500 text-white"
                    : "hover:bg-[var(--bg-subtle)]")}>
                {s}
              </button>
            ))}
          </div>
          <select value={convention} onChange={(e) => setConvention(e.target.value)}
            className="rounded-md border border-[var(--border)] bg-[var(--bg-elevated)] px-2 py-1.5 text-xs outline-none focus:border-accent-500">
            <option value="mid_year">Mid-year</option>
            <option value="year_end">Year-end</option>
          </select>
          <select value={terminalMethod} onChange={(e) => setTerminalMethod(e.target.value)}
            className="rounded-md border border-[var(--border)] bg-[var(--bg-elevated)] px-2 py-1.5 text-xs outline-none focus:border-accent-500">
            <option value="perpetual_growth">Perpetual growth</option>
            <option value="exit_multiple">Exit multiple</option>
          </select>
        </div>
      </div>

      {valuation.isLoading && <Skeleton className="h-64" />}

      {v && (
        <>
          <QualityBanner quality={v.quality} />

          <div className="mb-5 grid gap-4 sm:grid-cols-2 lg:grid-cols-5">
            <Card><CardBody>
              <Stat label="Intrinsic value" value={rupees(v.summary.weighted_value)}
                    hint="weighted across methods" />
            </CardBody></Card>
            <Card><CardBody>
              <Stat label="Current price" value={rupees(v.summary.current_price)} hint="market" />
            </CardBody></Card>
            <Card><CardBody>
              <Stat label="Upside" value={percent(v.summary.upside)}
                    tone={(v.summary.upside ?? 0) >= 0 ? "gain" : "loss"} hint="to intrinsic value" />
            </CardBody></Card>
            <Card><CardBody>
              <Stat label="Max buy price" value={rupees(v.summary.maximum_buy_price)}
                    hint={`${percent(v.summary.margin_of_safety, 0)} margin of safety`} />
            </CardBody></Card>
            <Card><CardBody>
              <Stat label="Recommendation" value={v.summary.recommendation} mono={false}
                    tone={v.summary.recommendation.includes("Buy") || v.summary.recommendation === "Accumulate"
                      ? "gain" : v.summary.recommendation === "Hold" ? "default" : "loss"}
                    hint={v.summary.in_buy_zone ? "in buy zone" : "outside buy zone"} />
            </CardBody></Card>
          </div>

          <div className="mb-5 flex flex-wrap gap-1 border-b border-[var(--border)]">
            {TABS.map((t) => (
              <button key={t.key} onClick={() => setTab(t.key)}
                className={cn("-mb-px border-b-2 px-3 py-2 text-xs font-medium transition-colors",
                  tab === t.key ? "border-accent-500 text-accent-500"
                    : "border-transparent text-[var(--text-muted)] hover:text-[var(--text)]")}>
                {t.label}
              </button>
            ))}
          </div>

          {tab === "dashboard" && (
            <div className="space-y-5">
              <div className="grid gap-5 lg:grid-cols-2">
                <FootballField summary={v.summary} />
                <Card>
                  <CardHeader title="Scenario valuation" subtitle="DCF value under each case" />
                  <CardBody>
                    <ValueRangeChart
                      bear={v.scenario_values.bear ?? 0}
                      base={v.scenario_values.base ?? 0}
                      bull={v.scenario_values.bull ?? 0}
                      expected={v.summary.weighted_value}
                      currentPrice={v.summary.current_price}
                    />
                  </CardBody>
                </Card>
              </div>

              <Card>
                <CardHeader title="Valuation summary" subtitle="Every methodology, side by side" />
                <div className="overflow-x-auto">
                  <table className="grid-table">
                    <thead>
                      <tr>
                        <th className="!text-left">Method</th><th>Value / share</th>
                        <th>Upside</th><th>Weight</th><th className="!text-left">Note</th>
                      </tr>
                    </thead>
                    <tbody>
                      {v.summary.methods.map((m) => (
                        <tr key={m.key} className={cn(!m.applicable && "opacity-50")}>
                          <td className="sticky-col">{m.label}</td>
                          <td className="num">{m.value_per_share === null ? EM_DASH : rupees(m.value_per_share)}</td>
                          <td className={cn("num", (m.upside ?? 0) < 0 && "text-loss")}>
                            {percent(m.upside)}
                          </td>
                          <td className="num">{percent(m.weight, 0)}</td>
                          <td className="!text-left text-[0.6875rem] text-[var(--text-muted)]">
                            {m.note ?? (m.applicable ? "" : "Not applicable")}
                          </td>
                        </tr>
                      ))}
                      <tr className="is-subtotal">
                        <td className="sticky-col font-semibold">Weighted conclusion</td>
                        <td className="num font-semibold">{rupees(v.summary.weighted_value)}</td>
                        <td className={cn("num font-semibold", (v.summary.upside ?? 0) < 0 && "text-loss")}>
                          {percent(v.summary.upside)}
                        </td>
                        <td className="num">100%</td>
                        <td className="!text-left">
                          <Badge variant={v.summary.recommendation.includes("Buy") ? "gain" : "loss"}>
                            {v.summary.recommendation}
                          </Badge>
                        </td>
                      </tr>
                    </tbody>
                  </table>
                </div>
              </Card>
            </div>
          )}

          {tab === "dcf" && (
            <div className="space-y-5">
              <DCFPanel dcf={v.dcf_fcff} title="DCF — free cash flow to firm" />
              <DCFPanel dcf={v.dcf_fcfe} title="DCF — free cash flow to equity" />
            </div>
          )}

          {tab === "relative" && <RelativePanel relative={v.relative} />}

          {tab === "wacc" && <WACCPanel wacc={v.wacc} schedule={v.wacc_schedule} />}

          {tab === "sensitivity" && (
            <Card>
              <CardHeader
                title="Sensitivity matrix"
                subtitle="Intrinsic value per share at each intersection"
                action={
                  <div className="flex flex-wrap items-center gap-2">
                    <select value={row} onChange={(e) => setRow(e.target.value)}
                      className="rounded border border-[var(--border)] bg-[var(--bg-subtle)] px-1.5 py-1 text-[0.6875rem] outline-none">
                      {AXES.map((a) => <option key={a.value} value={a.value}>{a.label}</option>)}
                    </select>
                    <span className="text-[0.6875rem] text-[var(--text-muted)]">×</span>
                    <select value={col} onChange={(e) => setCol(e.target.value)}
                      className="rounded border border-[var(--border)] bg-[var(--bg-subtle)] px-1.5 py-1 text-[0.6875rem] outline-none">
                      {AXES.filter((a) => a.value !== row).map((a) => (
                        <option key={a.value} value={a.value}>{a.label}</option>
                      ))}
                    </select>
                    <button onClick={() => setShowUpside((s) => !s)}
                      className="rounded border border-[var(--border)] px-2 py-1 text-[0.6875rem] hover:bg-[var(--bg-subtle)]">
                      {showUpside ? "Show value" : "Show upside"}
                    </button>
                  </div>
                }
              />
              {sensitivity.isLoading && <CardBody><Skeleton className="h-48" /></CardBody>}
              {sensitivity.data && (
                <SensitivityMatrix grid={sensitivity.data} showUpside={showUpside} />
              )}
            </Card>
          )}

          {tab === "simulation" && (
            <>
              {simulation.isLoading && <Skeleton className="h-64" />}
              {simulation.data && <SimulationPanel sim={simulation.data} />}
            </>
          )}
        </>
      )}
    </AppShell>
  );
}
