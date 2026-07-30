"use client";

import { AppShell } from "@/components/layout/app-shell";
import { MetricGrid, WarningList } from "@/components/analysis/metric-grid";
import {
  CashFlowChart, HistoryForecastChart, ScenarioChart, ValueRangeChart,
} from "@/components/charts";
import { AssumptionEditor } from "@/components/forecast/assumption-editor";
import { Badge, Card, CardBody, CardHeader, EmptyState, Skeleton, Stat } from "@/components/ui";
import { api, forecastApi } from "@/lib/api";
import { crore, EM_DASH, fiscalYear, multiple, percent, rupees } from "@/lib/format";
import type { ScenarioName } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, CheckCircle2, TrendingUp } from "lucide-react";
import Link from "next/link";
import { use, useState } from "react";

const HORIZONS = [3, 5, 10] as const;
const SCENARIOS: ScenarioName[] = ["bear", "base", "bull"];
const METHODS = [
  { value: "cagr", label: "CAGR" },
  { value: "volume_price", label: "Volume × Price" },
  { value: "organic_acquisition", label: "Organic + M&A" },
  { value: "segment", label: "Segment" },
] as const;

const TABS = [
  { key: "projection", label: "Projection" },
  { key: "charts", label: "Charts" },
  { key: "scenarios", label: "Scenarios" },
  { key: "detail", label: "Detail" },
] as const;
type TabKey = (typeof TABS)[number]["key"];

export default function ForecastPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const [horizon, setHorizon] = useState<number>(5);
  const [scenario, setScenario] = useState<ScenarioName>("base");
  const [method, setMethod] = useState<string>("cagr");
  const [tab, setTab] = useState<TabKey>("projection");
  const qc = useQueryClient();

  const profile = useQuery({
    queryKey: ["company-profile", id],
    queryFn: () => api.companyProfile(id),
  });
  const ticker = profile.data?.company.ticker;

  const forecast = useQuery({
    queryKey: ["forecast", ticker, horizon, scenario, method],
    queryFn: () => forecastApi.get(ticker!, { horizon, scenario, method }),
    enabled: Boolean(ticker),
  });

  const scenarios = useQuery({
    queryKey: ["forecast-scenarios", ticker, horizon],
    queryFn: () => forecastApi.scenarios(ticker!, horizon),
    enabled: Boolean(ticker) && tab === "scenarios",
  });

  const save = useMutation({
    mutationFn: (drivers: Record<string, number>) =>
      forecastApi.updateAssumptions(ticker!, {
        drivers,
        scenario: scenario === "base" ? null : scenario,
        horizon_years: horizon,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["forecast"] });
      qc.invalidateQueries({ queryKey: ["forecast-scenarios"] });
    },
  });

  if (profile.isLoading) {
    return <AppShell><Skeleton className="h-32" /></AppShell>;
  }
  if (!profile.data) {
    return (
      <AppShell>
        <Card><EmptyState title="Company not found" /></Card>
      </AppShell>
    );
  }

  const data = forecast.data;

  return (
    <AppShell>
      {/* Header */}
      <div className="mb-4 flex flex-wrap items-end justify-between gap-3">
        <div>
          <div className="flex items-center gap-2 text-xs">
            <Link href={`/companies/${id}`} className="num text-accent-500 hover:underline">
              {profile.data.company.ticker}
            </Link>
            <span className="text-[var(--text-muted)]">/</span>
            <span className="text-[var(--text-muted)]">Forecast engine</span>
          </div>
          <h1 className="mt-1 text-lg font-semibold">{profile.data.company.name}</h1>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <div className="flex overflow-hidden rounded-md border border-[var(--border)]">
            {HORIZONS.map((h) => (
              <button
                key={h}
                onClick={() => setHorizon(h)}
                className={cn(
                  "px-2.5 py-1.5 text-xs transition-colors",
                  horizon === h ? "bg-accent-500 text-white" : "hover:bg-[var(--bg-subtle)]",
                )}
              >
                {h}Y
              </button>
            ))}
          </div>
          <div className="flex overflow-hidden rounded-md border border-[var(--border)]">
            {SCENARIOS.map((s) => (
              <button
                key={s}
                onClick={() => setScenario(s)}
                className={cn(
                  "px-2.5 py-1.5 text-xs capitalize transition-colors",
                  scenario === s
                    ? s === "bull" ? "bg-gain text-white"
                      : s === "bear" ? "bg-loss text-white"
                      : "bg-accent-500 text-white"
                    : "hover:bg-[var(--bg-subtle)]",
                )}
              >
                {s}
              </button>
            ))}
          </div>
          <select
            value={method}
            onChange={(e) => setMethod(e.target.value)}
            className="rounded-md border border-[var(--border)] bg-[var(--bg-elevated)] px-2 py-1.5 text-xs outline-none focus:border-accent-500"
          >
            {METHODS.map((m) => <option key={m.value} value={m.value}>{m.label}</option>)}
          </select>
        </div>
      </div>

      {forecast.isLoading && <Skeleton className="h-64" />}

      {data && (
        <>
          {/* Summary tiles */}
          <div className="mb-5 grid gap-4 sm:grid-cols-2 lg:grid-cols-5">
            <Card><CardBody>
              <Stat label="Revenue CAGR" value={percent(data.summary.revenue_cagr)}
                    hint={`over ${horizon} years`} />
            </CardBody></Card>
            <Card><CardBody>
              <Stat label="EBITDA CAGR" value={percent(data.summary.ebitda_cagr)} hint="compound" />
            </CardBody></Card>
            <Card><CardBody>
              <Stat label={`Revenue FY${String(data.years.at(-1)?.fiscal_year ?? "").slice(-2)}`}
                    value={crore(data.summary.terminal_revenue)} hint="₹ crore" />
            </CardBody></Card>
            <Card><CardBody>
              <Stat label="Terminal EPS"
                    value={data.summary.terminal_eps === null ? EM_DASH : rupees(data.summary.terminal_eps)}
                    hint="per share" />
            </CardBody></Card>
            <Card><CardBody>
              <Stat label="Terminal FCFF" value={crore(data.summary.terminal_fcff)}
                    tone={(data.summary.terminal_fcff ?? 0) < 0 ? "loss" : "default"}
                    hint="free cash flow to firm" />
            </CardBody></Card>
          </div>

          {/* Engine health */}
          <div className="mb-5 flex flex-wrap items-center gap-2">
            {data.summary.debt_converged ? (
              <Badge variant="gain">
                <CheckCircle2 size={10} /> Debt schedule converged ({data.summary.debt_iterations} iterations)
              </Badge>
            ) : (
              <Badge variant="loss"><AlertTriangle size={10} /> Debt schedule did not converge</Badge>
            )}
            {data.summary.all_reconciled ? (
              <Badge variant="gain"><CheckCircle2 size={10} /> FCFF builds reconcile</Badge>
            ) : (
              <Badge variant="loss"><AlertTriangle size={10} /> FCFF mismatch</Badge>
            )}
            <Badge variant="accent">
              {data.assumptions.provenance.historical ?? 0} of {data.assumptions.drivers.length} assumptions calibrated from history
            </Badge>
          </div>

          <WarningList warnings={data.warnings} />

          {/* Tabs */}
          <div className="mb-5 mt-4 flex flex-wrap gap-1 border-b border-[var(--border)]">
            {TABS.map((t) => (
              <button
                key={t.key}
                onClick={() => setTab(t.key)}
                className={cn(
                  "-mb-px border-b-2 px-3 py-2 text-xs font-medium transition-colors",
                  tab === t.key
                    ? "border-accent-500 text-accent-500"
                    : "border-transparent text-[var(--text-muted)] hover:text-[var(--text)]",
                )}
              >
                {t.label}
              </button>
            ))}
          </div>

          <div className="grid gap-5 xl:grid-cols-[1fr_22rem]">
            <div className="min-w-0 space-y-5">
              {tab === "projection" && (
                <MetricGrid sections={data.sections} periods={data.periods} />
              )}

              {tab === "charts" && (
                <>
                  <Card>
                    <CardHeader title="Revenue — reported vs forecast"
                                subtitle="EBITDA margin on the right axis" />
                    <CardBody>
                      <HistoryForecastChart
                        label="Revenue"
                        history={data.history.map((h) => ({ fiscalYear: h.fiscal_year, value: h.revenue }))}
                        forecast={data.years.map((y) => ({ fiscalYear: y.fiscal_year, value: y.revenue }))}
                        secondary={{
                          label: "EBITDA margin", unit: "%",
                          history: data.history.map((h) => ({ fiscalYear: h.fiscal_year, value: h.ebitda_margin })),
                          forecast: data.years.map((y) => ({ fiscalYear: y.fiscal_year, value: y.ebitda_margin })),
                        }}
                      />
                    </CardBody>
                  </Card>

                  <div className="grid gap-5 lg:grid-cols-2">
                    <Card>
                      <CardHeader title="EBITDA" />
                      <CardBody>
                        <HistoryForecastChart
                          label="EBITDA" height={240}
                          history={data.history.map((h) => ({ fiscalYear: h.fiscal_year, value: h.ebitda }))}
                          forecast={data.years.map((y) => ({ fiscalYear: y.fiscal_year, value: y.ebitda }))}
                        />
                      </CardBody>
                    </Card>
                    <Card>
                      <CardHeader title="Profit after tax" />
                      <CardBody>
                        <HistoryForecastChart
                          label="PAT" height={240}
                          history={data.history.map((h) => ({ fiscalYear: h.fiscal_year, value: h.pat }))}
                          forecast={data.years.map((y) => ({ fiscalYear: y.fiscal_year, value: y.pat }))}
                        />
                      </CardBody>
                    </Card>
                    <Card>
                      <CardHeader title="Earnings per share" />
                      <CardBody>
                        <HistoryForecastChart
                          label="EPS" unit="₹" height={240}
                          history={data.history.map((h) => ({ fiscalYear: h.fiscal_year, value: h.eps }))}
                          forecast={data.years.map((y) => ({ fiscalYear: y.fiscal_year, value: y.eps }))}
                        />
                      </CardBody>
                    </Card>
                    <Card>
                      <CardHeader title="Free cash flow" subtitle="FCFF and FCFE across the horizon" />
                      <CardBody>
                        <CashFlowChart
                          height={240}
                          labels={data.years.map((y) => fiscalYear(y.fiscal_year))}
                          fcff={data.years.map((y) => y.fcff)}
                          fcfe={data.years.map((y) => y.fcfe)}
                        />
                      </CardBody>
                    </Card>
                  </div>
                </>
              )}

              {tab === "scenarios" && (
                <ScenarioPanel query={scenarios} horizon={horizon} />
              )}

              {tab === "detail" && (
                <Card>
                  <CardHeader title="Year-by-year detail" />
                  <div className="overflow-x-auto">
                    <table className="grid-table">
                      <thead>
                        <tr>
                          <th className="!text-left">Year</th>
                          <th>Revenue</th><th>Growth</th><th>EBITDA</th><th>Margin</th>
                          <th>EBIT</th><th>PAT</th><th>EPS</th><th>FCFF</th><th>FCFE</th>
                          <th>Net debt</th><th>ROE</th><th>ROIC</th>
                        </tr>
                      </thead>
                      <tbody>
                        {data.years.map((y) => (
                          <tr key={y.period}>
                            <td className="sticky-col num font-medium">{fiscalYear(y.fiscal_year)}</td>
                            <td className="num">{crore(y.revenue)}</td>
                            <td className="num">{percent(y.revenue_growth)}</td>
                            <td className="num">{crore(y.ebitda)}</td>
                            <td className="num">{percent(y.ebitda_margin)}</td>
                            <td className="num">{crore(y.ebit)}</td>
                            <td className="num">{crore(y.pat)}</td>
                            <td className="num">{y.eps === null ? EM_DASH : rupees(y.eps)}</td>
                            <td className={cn("num", y.fcff < 0 && "text-loss")}>{crore(y.fcff)}</td>
                            <td className={cn("num", y.fcfe < 0 && "text-loss")}>{crore(y.fcfe)}</td>
                            <td className="num">{crore(y.net_debt)}</td>
                            <td className="num">{percent(y.roe)}</td>
                            <td className="num">{percent(y.roic)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </Card>
              )}
            </div>

            <div className="min-w-0">
              <AssumptionEditor
                drivers={data.assumptions.drivers}
                onApply={(changes) => save.mutate(changes)}
                isSaving={save.isPending}
              />
            </div>
          </div>
        </>
      )}
    </AppShell>
  );
}

function ScenarioPanel({
  query, horizon,
}: {
  query: ReturnType<typeof useQuery<import("@/lib/types").ScenarioResponse>>;
  horizon: number;
}) {
  if (query.isLoading) return <Skeleton className="h-72" />;
  const s = query.data;
  if (!s) return null;

  const byName = Object.fromEntries(s.outcomes.map((o) => [o.scenario, o]));
  const labels = s.periods.labels;

  return (
    <div className="space-y-5">
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Card><CardBody>
          <Stat label="Expected value"
                value={s.expected_value === null ? EM_DASH : rupees(s.expected_value)}
                hint="probability-weighted" />
        </CardBody></Card>
        <Card><CardBody>
          <Stat label="Expected upside" value={percent(s.expected_upside)}
                tone={(s.expected_upside ?? 0) > 0 ? "gain" : "loss"}
                hint={s.current_price ? `vs ${rupees(s.current_price)}` : undefined} />
        </CardBody></Card>
        <Card><CardBody>
          <Stat label="Risk / reward"
                value={s.risk_reward === null ? EM_DASH : multiple(s.risk_reward)}
                hint="upside ÷ downside" />
        </CardBody></Card>
        <Card><CardBody>
          <Stat label="Dispersion"
                value={s.coefficient_of_variation === null ? EM_DASH : multiple(s.coefficient_of_variation)}
                hint="coefficient of variation" />
        </CardBody></Card>
      </div>

      <Card className="border-accent-500/30">
        <CardBody className="flex items-center gap-2.5">
          <TrendingUp size={15} className="shrink-0 text-accent-500" />
          <span className="text-sm font-medium">{s.verdict}</span>
        </CardBody>
      </Card>

      <div className="grid gap-5 lg:grid-cols-2">
        <Card>
          <CardHeader title="Value per share by scenario" />
          <CardBody>
            <ValueRangeChart
              bear={byName.bear?.value_per_share ?? 0}
              base={byName.base?.value_per_share ?? 0}
              bull={byName.bull?.value_per_share ?? 0}
              expected={s.expected_value}
              currentPrice={s.current_price}
            />
          </CardBody>
        </Card>
        <Card>
          <CardHeader title="Scenario outcomes" subtitle={`${horizon}-year horizon`} />
          <div className="overflow-x-auto">
            <table className="grid-table">
              <thead>
                <tr>
                  <th className="!text-left">Case</th><th>Prob.</th><th>CAGR</th>
                  <th>Terminal EPS</th><th>Value / share</th><th>Upside</th>
                </tr>
              </thead>
              <tbody>
                {s.outcomes.map((o) => (
                  <tr key={o.scenario}>
                    <td className="sticky-col capitalize">
                      <Badge variant={o.scenario === "bull" ? "gain" : o.scenario === "bear" ? "loss" : "accent"}>
                        {o.scenario}
                      </Badge>
                    </td>
                    <td className="num">{percent(o.probability, 0)}</td>
                    <td className="num">{percent(o.revenue_cagr)}</td>
                    <td className="num">{o.terminal_eps === null ? EM_DASH : rupees(o.terminal_eps)}</td>
                    <td className="num">{o.value_per_share === null ? EM_DASH : rupees(o.value_per_share)}</td>
                    <td className={cn("num", (o.upside ?? 0) < 0 && "text-loss")}>
                      {percent(o.upside)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      </div>

      {s.comparison.map((row) => (
        <Card key={row.key}>
          <CardHeader title={`${row.label} — scenario comparison`} />
          <CardBody>
            <ScenarioChart
              labels={labels} bear={row.bear} base={row.base} bull={row.bull}
              unit={row.unit} height={260}
            />
          </CardBody>
        </Card>
      ))}
    </div>
  );
}
