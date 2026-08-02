"use client";

import { AppShell } from "@/components/layout/app-shell";
import { CompanyTabs } from "@/components/layout/company-tabs";
import { DebtPanels, NoData, ShareholdingHeader, WorkingCapitalHeader } from "@/components/analysis/panels";
import { FlagList, MetricGrid, WarningList } from "@/components/analysis/metric-grid";
import { Badge, Card, CardBody, Skeleton, Stat, TabStrip } from "@/components/ui";
import { analysisApi, api } from "@/lib/api";
import { crore, EM_DASH, percent } from "@/lib/format";
import { cn } from "@/lib/utils";
import { useQuery } from "@tanstack/react-query";
import { AlertTriangle } from "lucide-react";
import Link from "next/link";
import { use, useState } from "react";

const TABS = [
  { key: "overview", label: "Overview" },
  { key: "income-statement", label: "Income Statement" },
  { key: "balance-sheet", label: "Balance Sheet" },
  { key: "cash-flow", label: "Cash Flow" },
  { key: "ratios", label: "Ratios" },
  { key: "working-capital", label: "Working Capital" },
  { key: "debt", label: "Debt" },
  { key: "capex", label: "Capex" },
  { key: "shareholding", label: "Shareholding" },
] as const;

type TabKey = (typeof TABS)[number]["key"];

export default function FinancialsPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const [tab, setTab] = useState<TabKey>("overview");

  // Resolve the company first so we can address the analysis API by ticker.
  const profile = useQuery({
    queryKey: ["company-profile", id],
    queryFn: () => api.companyProfile(id),
  });
  const ticker = profile.data?.company.ticker;

  return (
    <AppShell>
      <CompanyTabs companyId={id} />
      {profile.isLoading && <Skeleton className="h-24" />}

      {profile.data && (
        <>
          <div className="mb-4 flex flex-wrap items-end justify-between gap-3">
            <div>
              <div className="flex items-center gap-2">
                <Link href={`/companies/${id}`} className="num text-xs text-accent-500 hover:underline">
                  {profile.data.company.ticker}
                </Link>
                <span className="text-xs text-[var(--text-muted)]">/</span>
                <span className="text-xs text-[var(--text-muted)]">Financial analysis</span>
              </div>
              <h1 className="mt-1 text-lg font-semibold">{profile.data.company.name}</h1>
            </div>
            <Badge variant="accent">
              {profile.data.coverage.fiscal_years.length} fiscal years
            </Badge>
          </div>

          {/* Tabs */}
          <TabStrip className="mb-4 lg:mb-5" label="Financial statements">
            {TABS.map((t) => (
              <button data-active={tab === t.key} role="tab" aria-selected={tab === t.key}
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
          </TabStrip>

          {ticker && <TabContent tab={tab} ticker={ticker} />}
        </>
      )}
    </AppShell>
  );
}

function TabContent({ tab, ticker }: { tab: TabKey; ticker: string }) {
  switch (tab) {
    case "overview": return <OverviewTab ticker={ticker} />;
    case "income-statement": return <StatementTab ticker={ticker} kind="income" />;
    case "balance-sheet": return <StatementTab ticker={ticker} kind="balance" />;
    case "cash-flow": return <StatementTab ticker={ticker} kind="cash" />;
    case "ratios": return <RatiosTab ticker={ticker} />;
    case "working-capital": return <WorkingCapitalTab ticker={ticker} />;
    case "debt": return <DebtTab ticker={ticker} />;
    case "capex": return <CapexTab ticker={ticker} />;
    case "shareholding": return <ShareholdingTab ticker={ticker} />;
  }
}

function Loading() {
  return (
    <div className="space-y-2">
      {Array.from({ length: 10 }).map((_, i) => <Skeleton key={i} className="h-8" />)}
    </div>
  );
}

function OverviewTab({ ticker }: { ticker: string }) {
  const { data, isLoading } = useQuery({
    queryKey: ["financials", ticker],
    queryFn: () => analysisApi.financials(ticker),
  });
  if (isLoading) return <Loading />;
  if (!data?.has_data) return <NoData label="financial" />;

  const latest = data.summary[data.summary.length - 1];
  return (
    <div className="space-y-5">
      <WarningList warnings={data.warnings} />
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Card><CardBody><Stat label="Revenue CAGR (5Y)" value={percent(data.revenue_cagr_5y)} hint="compound annual" /></CardBody></Card>
        <Card><CardBody><Stat label="Revenue CAGR (full)" value={percent(data.revenue_cagr_full)} hint={`${data.summary.length} years`} /></CardBody></Card>
        <Card><CardBody><Stat label="ROE (latest)" value={percent(latest?.roe ?? null)} hint="on average equity" /></CardBody></Card>
        <Card><CardBody><Stat label="ROCE (latest)" value={percent(latest?.roce ?? null)} hint="pre-tax" /></CardBody></Card>
      </div>

      <Card>
        <div className="scroll-x">
          <table className="grid-table">
            <thead>
              <tr>
                <th className="!text-left">Fiscal year</th>
                <th>Revenue</th><th>EBITDA</th><th>Margin</th><th>PAT</th>
                <th>EPS</th><th>CFO</th><th>FCF</th><th>Net debt</th>
                <th>ROE</th><th>Ties</th>
              </tr>
            </thead>
            <tbody>
              {data.summary.map((s) => (
                <tr key={s.fiscal_year}>
                  <td className="sticky-col num font-medium">FY{String(s.fiscal_year).slice(-2)}</td>
                  <td className="num">{crore(s.revenue)}</td>
                  <td className="num">{crore(s.ebitda)}</td>
                  <td className="num">{percent(s.ebitda_margin)}</td>
                  <td className="num">{crore(s.pat)}</td>
                  <td className="num">{s.eps === null ? EM_DASH : `₹${s.eps.toFixed(2)}`}</td>
                  <td className="num">{crore(s.cfo)}</td>
                  <td className={cn("num", (s.free_cash_flow ?? 0) < 0 && "text-loss")}>
                    {crore(s.free_cash_flow)}
                  </td>
                  <td className="num">{crore(s.net_debt)}</td>
                  <td className="num">{percent(s.roe)}</td>
                  <td>{s.balance_sheet_ties ? <Badge variant="gain">✓</Badge> : <Badge variant="loss">✗</Badge>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  );
}

function StatementTab({ ticker, kind }: { ticker: string; kind: "income" | "balance" | "cash" }) {
  const fetcher = {
    income: analysisApi.incomeStatement,
    balance: analysisApi.balanceSheet,
    cash: analysisApi.cashFlow,
  }[kind];
  const { data, isLoading } = useQuery({
    queryKey: ["statement", kind, ticker],
    queryFn: () => fetcher(ticker),
  });
  if (isLoading) return <Loading />;
  if (!data?.has_data) return <NoData label="statement" />;
  return (
    <div className="space-y-4">
      <WarningList warnings={data.warnings} />
      <MetricGrid sections={data.sections} periods={data.periods} />
    </div>
  );
}

function RatiosTab({ ticker }: { ticker: string }) {
  const [wacc, setWacc] = useState(12);
  const { data, isLoading } = useQuery({
    queryKey: ["ratios", ticker, wacc],
    queryFn: () => analysisApi.ratios(ticker, wacc / 100),
  });
  if (isLoading) return <Loading />;
  if (!data?.has_data) return <NoData label="ratio" />;
  return (
    <MetricGrid
      sections={data.sections}
      periods={data.periods}
      title="Ratio analysis"
      subtitle="Balance-sheet ratios use average opening/closing balances"
      action={
        <label className="flex items-center gap-2 text-xs text-[var(--text-muted)]">
          WACC
          <input
            type="number" min={0} max={40} step={0.5} value={wacc}
            onChange={(e) => setWacc(Number(e.target.value))}
            className="num w-16 rounded border border-[var(--border)] bg-[var(--bg-subtle)] px-1.5 py-1 text-right outline-none focus:border-accent-500"
          />
          %
        </label>
      }
    />
  );
}

function WorkingCapitalTab({ ticker }: { ticker: string }) {
  const { data, isLoading } = useQuery({
    queryKey: ["working-capital", ticker],
    queryFn: () => analysisApi.workingCapital(ticker),
  });
  if (isLoading) return <Loading />;
  if (!data?.has_data) return <NoData label="working-capital" />;
  return (
    <div className="space-y-5">
      <WorkingCapitalHeader data={data} />
      <FlagList flags={data.flags} />
      <MetricGrid
        sections={data.sections}
        periods={data.periods}
        title="Working capital"
        subtitle={
          data.cost_of_debt_assumption !== null
            ? `Funding cost at an implied ${percent(data.cost_of_debt_assumption, 2)} cost of debt`
            : undefined
        }
      />
    </div>
  );
}

function DebtTab({ ticker }: { ticker: string }) {
  const { data, isLoading } = useQuery({
    queryKey: ["debt", ticker],
    queryFn: () => analysisApi.debt(ticker),
  });
  if (isLoading) return <Loading />;
  if (!data?.has_data) return <NoData label="debt" />;
  return (
    <div className="space-y-5">
      <DebtPanels data={data} />
      <FlagList flags={data.flags} />
      <MetricGrid sections={data.sections} periods={data.periods} title="Debt history" />
    </div>
  );
}

function CapexTab({ ticker }: { ticker: string }) {
  const { data, isLoading } = useQuery({
    queryKey: ["capex", ticker],
    queryFn: () => analysisApi.capex(ticker),
  });
  if (isLoading) return <Loading />;
  if (!data?.has_data) return <NoData label="capex" />;
  return (
    <MetricGrid
      sections={data.sections}
      periods={data.periods}
      title="Capital expenditure"
      subtitle="Maintenance capex is proxied by D&A, capped at gross capex"
    />
  );
}

function ShareholdingTab({ ticker }: { ticker: string }) {
  const { data, isLoading } = useQuery({
    queryKey: ["shareholding", ticker],
    queryFn: () => analysisApi.shareholding(ticker),
  });
  if (isLoading) return <Loading />;
  if (!data?.has_data) {
    return (
      <Card>
        <CardBody className="flex items-center gap-2 text-xs text-[var(--text-muted)]">
          <AlertTriangle size={14} />
          No shareholding disclosures on file for this company.
        </CardBody>
      </Card>
    );
  }
  return (
    <div className="space-y-5">
      <ShareholdingHeader data={data} />
      <FlagList flags={data.flags} />
      <MetricGrid
        sections={data.sections}
        periods={data.periods}
        title="Shareholding pattern"
        subtitle="Quarterly disclosures, most recent last"
      />
    </div>
  );
}
