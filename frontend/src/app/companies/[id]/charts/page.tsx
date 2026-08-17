"use client";

import { AppShell } from "@/components/layout/app-shell";
import { CompanyTabs } from "@/components/layout/company-tabs";
import { Card, CardBody, CardHeader, Skeleton } from "@/components/ui";
import { api, analysisApi, marketApi } from "@/lib/api";
import { useQuery } from "@tanstack/react-query";
import { use } from "react";
import { useTheme } from "@/components/layout/theme-provider";
import Highcharts from "highcharts";
import HighchartsReact from "highcharts-react-official";
import { crore, fiscalYear, percent, rupees } from "@/lib/format";

function MiniChart({ categories, data, unit, color }: { categories: string[]; data: (number | null)[]; unit: string; color?: string }) {
  const { theme } = useTheme();
  const dark = theme === "dark";
  const options: Highcharts.Options = {
    chart: { type: "line", backgroundColor: "transparent", height: 220, spacing: [8, 8, 8, 8] },
    title: { text: undefined },
    credits: { enabled: false },
    xAxis: { categories, labels: { style: { color: dark ? "#8fa3bf" : "#64748b", fontSize: "9px" } }, lineColor: dark ? "#1e304c" : "#e2e8f0", tickColor: dark ? "#1e304c" : "#e2e8f0" },
    yAxis: { title: { text: unit, style: { fontSize: "10px" } }, gridLineColor: dark ? "#1e304c" : "#e2e8f0", gridLineDashStyle: "Dot", labels: { style: { color: dark ? "#8fa3bf" : "#64748b", fontSize: "9px" } } },
    legend: { enabled: false },
    tooltip: {
      backgroundColor: dark ? "#0d1b30" : "#fff",
      borderColor: dark ? "#1e304c" : "#e2e8f0",
      shared: true,
      formatter: function () {
        const points = (this as any).points ?? [this];
        const rows = points.map((p: any) => `<div style="display:flex;gap:10px;justify-content:space-between"><span>${p.series.name}</span><b>${p.y !== null ? (unit === "₹" ? rupees(p.y) : unit === "%" ? percent(p.y) : `${crore(p.y)} cr`) : "—"}</b></div>`).join("");
        return `<div style="font-size:11px"><b>${this.x}</b>${rows}</div>`;
      },
    },
    series: [{ type: "line", name: unit, data, color: color ?? "#1f6feb", marker: { radius: 2 } } as any],
  };
  return <HighchartsReact highcharts={Highcharts} options={options} />;
}

function PriceChart({ data }: { data: { date: string; close: number | null }[] }) {
  const { theme } = useTheme();
  const dark = theme === "dark";
  const categories = data.map(d => d.date);
  const values = data.map(d => d.close);
  const options: Highcharts.Options = {
    chart: { type: "line", backgroundColor: "transparent", height: 260 },
    title: { text: undefined },
    credits: { enabled: false },
    xAxis: { categories, labels: { style: { color: dark ? "#8fa3bf" : "#64748b", fontSize: "9px" }, autoRotation: [0, -45] }, lineColor: dark ? "#1e304c" : "#e2e8f0" },
    yAxis: { title: { text: "₹ price" }, gridLineColor: dark ? "#1e304c" : "#e2e8f0", labels: { style: { color: dark ? "#8fa3bf" : "#64748b", fontSize: "9px" } } },
    legend: { enabled: false },
    tooltip: {
      shared: true,
      backgroundColor: dark ? "#0d1b30" : "#fff",
      formatter: function () {
        const p = this as any;
        return `<b>${p.x}</b>: ${p.y !== null ? rupees(p.y) : "—"}`;
      },
    },
    series: [{ type: "line", name: "Close", data: values, color: "#1f6feb", marker: { enabled: false } } as any],
  };
  return <HighchartsReact highcharts={Highcharts} options={options} />;
}

export default function ChartsPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);

  const profile = useQuery({
    queryKey: ["company-profile", id],
    queryFn: () => api.companyProfile(id),
  });

  const ticker = profile.data?.company.ticker;

  const financials = useQuery({
    queryKey: ["financials-overview", ticker],
    queryFn: () => analysisApi.financials(ticker!),
    enabled: Boolean(ticker),
  });

  const market = useQuery({
    queryKey: ["market-history", ticker],
    queryFn: () => marketApi.snapshot(ticker!, { history: true, news: false, earnings: false }),
    enabled: Boolean(ticker),
  });

  const summary = financials.data?.summary ?? [];
  const fiscalLabels = summary.map(s => fiscalYear(s.fiscal_year));
  const revenues = summary.map(s => s.revenue);
  const pats = summary.map(s => s.pat);
  const roces = summary.map(s => s.roce);

  const hasRevenue = revenues.filter(v => v !== null && v !== undefined).length >= 2;
  const hasPAT = pats.filter(v => v !== null && v !== undefined).length >= 2;
  const hasROCE = roces.filter(v => v !== null && v !== undefined).length >= 2;
  const hasPrice = market.data?.price_history && market.data.price_history.filter(p => p.close !== null).length >= 2;

  return (
    <AppShell>
      <CompanyTabs companyId={id} />
      {profile.isLoading && <Skeleton className="h-24" />}
      {profile.data && (
        <>
          <div className="mb-4">
            <h1 className="text-lg font-semibold">{profile.data.company.name} — Charts</h1>
            <p className="text-xs text-[var(--text-muted)]">Beginner-friendly: 1Y price, 5Y revenue, PAT, ROCE trends from actual canonical data. No fabricated values.</p>
          </div>

          <div className="grid gap-5 lg:grid-cols-2">
            {/* 1Y Price Chart */}
            <Card>
              <CardHeader title="1-Year Share Price" subtitle="Actual historical closes • ₹ price • Source: market provider" />
              <CardBody>
                {market.isLoading && <Skeleton className="h-56" />}
                {market.isError && <div className="text-xs text-loss">Could not load price history: {(market.error as Error).message}</div>}
                {market.data && !hasPrice && <div className="text-xs text-[var(--text-muted)]">Historical data not available — market provider returned no price_history for {ticker}. Source: {market.data.source} • Confidence {market.data.meta?.confidence ?? "—"}</div>}
                {market.data && hasPrice && (
                  <>
                    <PriceChart data={market.data.price_history.slice(-252)} />
                    <p className="mt-2 text-[0.6875rem] text-[var(--text-muted)]">Shows actual closes for last 252 trading days (~1Y) when available. Unit: ₹. Source: {market.data.source}. Last updated: {market.data.meta?.last_updated ? new Date(market.data.meta.last_updated).toLocaleDateString() : "—"}</p>
                  </>
                )}
              </CardBody>
            </Card>

            {/* ROCE Trend */}
            <Card>
              <CardHeader title="ROCE Trend" subtitle="Return on Capital Employed • % • 5Y" />
              <CardBody>
                {financials.isLoading && <Skeleton className="h-56" />}
                {financials.data && !hasROCE && <div className="text-xs text-[var(--text-muted)]">Insufficient data for trend — need at least 2 valid ROCE points. Available years: {fiscalLabels.length}.</div>}
                {financials.data && hasROCE && (
                  <>
                    <MiniChart categories={fiscalLabels.slice(-5)} data={roces.slice(-5)} unit="%" color="#0b7a3b" />
                    <p className="mt-2 text-[0.6875rem] text-[var(--text-muted)]">ROCE = EBIT / Capital Employed. Shows efficiency of capital use. Source: canonical financials, precedence chain. Unit: %.</p>
                  </>
                )}
              </CardBody>
            </Card>

            {/* 5Y Revenue */}
            <Card>
              <CardHeader title="5-Year Revenue" subtitle="₹ crore • Fiscal years actually available" />
              <CardBody>
                {financials.isLoading && <Skeleton className="h-56" />}
                {financials.data && !hasRevenue && <div className="text-xs text-[var(--text-muted)]">Historical data not available — no revenue values.</div>}
                {financials.data && hasRevenue && (
                  <>
                    <MiniChart categories={fiscalLabels.slice(-5)} data={revenues.slice(-5)} unit="₹ cr" color="#1f6feb" />
                    <p className="mt-2 text-[0.6875rem] text-[var(--text-muted)]">Revenue from reported income statements. Only years with actual reported values shown. Unit: ₹ crore. Source: canonical facts.</p>
                  </>
                )}
              </CardBody>
            </Card>

            {/* 5Y PAT */}
            <Card>
              <CardHeader title="5-Year Profit After Tax" subtitle="₹ crore • Fiscal years actually available" />
              <CardBody>
                {financials.isLoading && <Skeleton className="h-56" />}
                {financials.data && !hasPAT && <div className="text-xs text-[var(--text-muted)]">Historical data not available — no PAT values.</div>}
                {financials.data && hasPAT && (
                  <>
                    <MiniChart categories={fiscalLabels.slice(-5)} data={pats.slice(-5)} unit="₹ cr" color="#8b5cf6" />
                    <p className="mt-2 text-[0.6875rem] text-[var(--text-muted)]">PAT from reported income statements. Only actual values, never interpolated. Unit: ₹ crore.</p>
                  </>
                )}
              </CardBody>
            </Card>
          </div>
        </>
      )}
    </AppShell>
  );
}
