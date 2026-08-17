"use client";

import { AppShell } from "@/components/layout/app-shell";
import { CompanyTabs } from "@/components/layout/company-tabs";
import { DebtPanels, NoData, ShareholdingHeader, WorkingCapitalHeader } from "@/components/analysis/panels";
import { FlagList, MetricGrid, WarningList } from "@/components/analysis/metric-grid";
import { Badge, Card, CardBody, CardHeader, Skeleton, Stat, TabStrip } from "@/components/ui";
import { analysisApi, api } from "@/lib/api";
import { crore, EM_DASH, fiscalYear, percent, plainNumber } from "@/lib/format";
import { cn } from "@/lib/utils";
import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, CheckCircle2, Info } from "lucide-react";
import Link from "next/link";
import { use, useEffect, useState } from "react";

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

function useAdvanced() {
  const [advanced, setAdvanced] = useState(false);
  useEffect(() => {
    try { const v = localStorage.getItem("ep:financials-advanced"); if (v) setAdvanced(v === "1"); } catch {}
  }, []);
  const toggle = () => {
    setAdvanced(prev => {
      const next = !prev;
      try { localStorage.setItem("ep:financials-advanced", next ? "1" : "0"); } catch {}
      return next;
    });
  };
  return { advanced, toggle };
}

function Tooltip({ text }: { text: string }) {
  return (
    <span className="group relative inline-flex cursor-help">
      <Info size={10} className="text-[var(--text-muted)]" />
      <span className="pointer-events-none absolute left-0 top-5 z-10 hidden w-56 rounded border bg-[var(--bg-elevated)] p-2 text-[0.6875rem] shadow group-hover:block">{text}</span>
    </span>
  );
}

const EXPLANATIONS: Record<string, string> = {
  "Revenue Growth": "Revenue growth shows how fast sales are expanding year over year.",
  "PAT Margin": "PAT margin = PAT / Revenue. Higher means more profit kept from sales.",
  "ROCE": "ROCE measures operating profit vs capital employed. Higher means more efficient use of capital.",
  "Debt/Equity": "Debt/Equity = total debt divided by equity. High means more leverage risk.",
  "Net Debt": "Net debt = gross debt minus cash. Negative means net cash.",
  "Operating Cash Flow": "Operating cash flow is cash generated from core business operations.",
  "Interest Coverage": "Interest coverage = EBIT / interest expense. Higher means easier to service debt.",
  "EBITDA Margin": "EBITDA margin shows operating profitability before non-cash and financing.",
};

function calcCAGR(values: (number | null)[], years: number): number | null {
  const clean = values.filter((v) => v !== null && v !== undefined && v > 0) as number[];
  if (clean.length < years + 1) return null;
  const start = clean[clean.length - 1 - years];
  const end = clean[clean.length - 1];
  if (!start || start <= 0 || !end) return null;
  return Math.pow(end / start, 1 / years) - 1;
}

export default function FinancialsPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const [tab, setTab] = useState<TabKey>("overview");

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
  const ratios = useQuery({
    queryKey: ["ratios", ticker],
    queryFn: () => analysisApi.ratios(ticker),
    enabled: !!data?.has_data,
  });
  const { advanced, toggle } = useAdvanced();

  if (isLoading) return <Loading />;
  if (!data?.has_data) return <NoData label="financial" />;

  const summary = data.summary;
  const latest = summary[summary.length - 1];
  const prev = summary.length > 1 ? summary[summary.length - 2] : null;

  const revenues = summary.map(s => s.revenue);
  const pats = summary.map(s => s.pat);
  const roces = summary.map(s => s.roce);
  const cfos = summary.map(s => s.cfo);

  const revCAGR3 = calcCAGR(revenues, 3);
  const revCAGR5 = data.revenue_cagr_5y ?? calcCAGR(revenues, 5);
  const patCAGR3 = calcCAGR(pats, 3);
  const patCAGR5 = calcCAGR(pats, 5);

  // Extract leverage and coverage from ratios if available
  let debtEquity: number | null = null;
  let interestCoverage: number | null = null;
  let netDebtEquity: number | null = null;
  try {
    const levSec = ratios.data?.sections?.find(s => s.key === "leverage");
    if (levSec) {
      const deRow = levSec.rows.find(r => r.key === "debt_equity");
      const ndeRow = levSec.rows.find(r => r.key === "net_debt_equity");
      const icRow = levSec.rows.find(r => r.key === "interest_coverage");
      if (deRow) debtEquity = deRow.values[deRow.values.length - 1] ?? null;
      if (ndeRow) netDebtEquity = ndeRow.values[ndeRow.values.length - 1] ?? null;
      if (icRow) interestCoverage = icRow.values[icRow.values.length - 1] ?? null;
    }
  } catch {}

  // Health verdict logic — thresholds documented
  // - Revenue Growth >10% = healthy (growth_quality)
  // - PAT Margin >10% = healthy (profitability)
  // - ROCE >15% = healthy (return_ratios)
  // - Debt/Equity <0.5 and Net Debt/Equity <0.3 = healthy (leverage)
  // - CFO >0 and CFO trend positive = healthy (cash_flow_quality)
  // - Interest Coverage >3x = healthy (coverage)
  const signals: { label: string; value: string; ok: boolean | null; explanation: string }[] = [];
  let okCount = 0;
  let total = 0;

  if (revCAGR5 !== null || revCAGR3 !== null) {
    const g = revCAGR5 ?? revCAGR3!;
    const ok = g > 0.1;
    signals.push({ label: "Revenue Growth", value: `${(g*100).toFixed(1)}%`, ok, explanation: EXPLANATIONS["Revenue Growth"] });
    total++; if (ok) okCount++;
  }
  if (latest.pat_margin !== null && latest.pat_margin !== undefined) {
    // PAT margin from latest - need to compute from pat/revenue if not directly available, use pat_margin from profile? Use latest pat / revenue
    const pm = latest.pat && latest.revenue ? latest.pat / latest.revenue : null;
    const val = pm !== null ? `${(pm*100).toFixed(1)}%` : latest.pat_margin ? `${(latest.pat_margin*100).toFixed(1)}%` : "—";
    const ok = (pm ?? latest.pat_margin ?? 0) > 0.1;
    signals.push({ label: "PAT Margin", value: val, ok: pm !== null ? ok : null, explanation: EXPLANATIONS["PAT Margin"] });
    if (pm !== null || latest.pat_margin !== null) { total++; if (ok) okCount++; }
  }
  if (latest.roce !== null && latest.roce !== undefined) {
    const ok = latest.roce > 0.15;
    signals.push({ label: "ROCE", value: `${(latest.roce*100).toFixed(1)}%`, ok, explanation: EXPLANATIONS["ROCE"] });
    total++; if (ok) okCount++;
  }
  if (debtEquity !== null) {
    const ok = debtEquity < 0.5;
    signals.push({ label: "Debt / Equity", value: `${debtEquity.toFixed(2)}x`, ok, explanation: EXPLANATIONS["Debt/Equity"] });
    total++; if (ok) okCount++;
  } else if (latest.net_debt !== null) {
    const ok = latest.net_debt < 0 || (latest.total_assets ? latest.net_debt / latest.total_assets < 0.2 : false);
    signals.push({ label: "Net Debt", value: `${crore(latest.net_debt)} cr`, ok, explanation: EXPLANATIONS["Net Debt"] });
    total++; if (ok) okCount++;
  }
  if (latest.cfo !== null && latest.cfo !== undefined) {
    const ok = latest.cfo > 0;
    signals.push({ label: "Operating Cash Flow", value: `${crore(latest.cfo)} cr`, ok, explanation: EXPLANATIONS["Operating Cash Flow"] });
    total++; if (ok) okCount++;
  }
  if (interestCoverage !== null) {
    const ok = interestCoverage > 3;
    signals.push({ label: "Interest Coverage", value: `${interestCoverage.toFixed(1)}x`, ok, explanation: EXPLANATIONS["Interest Coverage"] });
    total++; if (ok) okCount++;
  } else {
    signals.push({ label: "Interest Coverage", value: "—", ok: null, explanation: EXPLANATIONS["Interest Coverage"] });
  }

  let verdict: "Healthy" | "Needs Attention" | "Weak" | "Insufficient Data" = "Insufficient Data";
  if (total === 0) verdict = "Insufficient Data";
  else if (okCount >= 4) verdict = "Healthy";
  else if (okCount >= 2) verdict = "Needs Attention";
  else verdict = "Weak";

  const trend = (curr: number | null | undefined, prev: number | null | undefined) => {
    if (curr === null || curr === undefined || prev === null || prev === undefined) return "";
    if (curr > prev) return "↑";
    if (curr < prev) return "↓";
    return "→";
  };

  return (
    <div className="space-y-5">
      <WarningList warnings={data.warnings} />

      {/* Financial Health Summary */}
      <Card>
        <CardHeader title="Financial Health" subtitle="Profitability, growth, leverage and cash-flow signals from reported financial statements. Not an investment recommendation." />
        <div className="p-4 flex items-center gap-3">
          <div className={`text-lg font-semibold ${verdict === "Healthy" ? "text-green-600" : verdict === "Weak" ? "text-red-600" : "text-amber-600"}`}>
            {verdict === "Healthy" && "Healthy"} 
            {verdict === "Needs Attention" && "Needs Attention"} 
            {verdict === "Weak" && "Weak"} 
            {verdict === "Insufficient Data" && "Insufficient Data"}
          </div>
          <div className="text-xs text-[var(--text-muted)]">{okCount}/{total} signals positive • Latest FY{latest.fiscal_year}</div>
          <button onClick={toggle} className="ml-auto text-[0.6875rem] px-2.5 py-1 rounded border hover:bg-[var(--bg-subtle)]">{advanced ? "Simple" : "Advanced"}</button>
        </div>
      </Card>

      {/* Signal Cards */}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {signals.map((s, i) => (
          <Card key={i}>
            <CardBody>
              <div className="flex items-center gap-2 text-[0.6875rem] text-[var(--text-muted)]">
                {s.label} <Tooltip text={s.explanation} />
              </div>
              <div className={`mt-1 text-sm font-medium ${s.ok === true ? "text-green-600" : s.ok === false ? "text-red-600" : ""}`}>{s.value}</div>
              <div className="mt-1 text-[0.625rem] text-[var(--text-muted)]">{s.ok === true ? "Strong" : s.ok === false ? "Weak" : "Not available"}</div>
            </CardBody>
          </Card>
        ))}
      </div>

      {/* Trend Section */}
      <Card>
        <CardHeader title="Trend Section" subtitle="Revenue, PAT, ROCE, CFO historical trends" />
        <div className="p-4 grid gap-4 sm:grid-cols-2 lg:grid-cols-4 text-xs">
          <div>
            <div className="font-medium">Revenue</div>
            <div className="mt-1 space-y-0.5">
              {data.summary.slice(-5).map(s => (
                <div key={s.fiscal_year} className="flex justify-between"><span>FY{String(s.fiscal_year).slice(-2)}</span><span className="num">{crore(s.revenue)} cr</span></div>
              ))}
            </div>
          </div>
          <div>
            <div className="font-medium">PAT</div>
            <div className="mt-1 space-y-0.5">
              {data.summary.slice(-5).map(s => (
                <div key={s.fiscal_year} className="flex justify-between"><span>FY{String(s.fiscal_year).slice(-2)}</span><span className="num">{crore(s.pat)} cr {trend(s.pat, data.summary.find(x=>x.fiscal_year===s.fiscal_year-1)?.pat)}</span></div>
              ))}
            </div>
          </div>
          <div>
            <div className="font-medium">ROCE</div>
            <div className="mt-1 space-y-0.5">
              {data.summary.slice(-5).map(s => (
                <div key={s.fiscal_year} className="flex justify-between"><span>FY{String(s.fiscal_year).slice(-2)}</span><span className="num">{s.roce !== null ? percent(s.roce) : "—"}</span></div>
              ))}
            </div>
          </div>
          <div>
            <div className="font-medium">CFO</div>
            <div className="mt-1 space-y-0.5">
              {data.summary.slice(-5).map(s => (
                <div key={s.fiscal_year} className="flex justify-between"><span>FY{String(s.fiscal_year).slice(-2)}</span><span className="num">{crore(s.cfo)} cr</span></div>
              ))}
            </div>
          </div>
        </div>
        <div className="px-4 pb-3"><Link href={`/companies/${data.company.ticker}/charts`} className="text-[0.6875rem] text-accent-500 hover:underline">View detailed charts →</Link></div>
      </Card>

      {/* Profitability */}
      <Card>
        <CardHeader title="Profitability" subtitle="Latest fiscal year" />
        <div className="p-4 grid gap-4 sm:grid-cols-3 text-xs">
          <div><div className="text-[var(--text-muted)]">Revenue</div><div className="num font-medium">{crore(latest.revenue)} cr</div></div>
          <div><div className="text-[var(--text-muted)]">EBITDA</div><div className="num">{crore(latest.ebitda ?? null)} cr</div></div>
          <div><div className="text-[var(--text-muted)]">PAT</div><div className="num">{crore(latest.pat ?? null)} cr</div></div>
          <div><div className="text-[var(--text-muted)] flex items-center gap-1">EBITDA Margin <Tooltip text={EXPLANATIONS["PAT Margin"]} /></div><div className="num">{latest.ebitda_margin !== null ? percent(latest.ebitda_margin) : "—"}</div></div>
          <div><div className="text-[var(--text-muted)] flex items-center gap-1">PAT Margin <Tooltip text={EXPLANATIONS["PAT Margin"]} /></div><div className="num">{latest.pat_margin !== null ? percent(latest.pat_margin) : (latest.pat && latest.revenue ? percent(latest.pat / latest.revenue) : "—")}</div></div>
          <div><div className="text-[var(--text-muted)] flex items-center gap-1">ROCE <Tooltip text={EXPLANATIONS["ROCE"]} /></div><div className="num">{latest.roce !== null ? percent(latest.roce) : "—"} {trend(latest.roce, prev?.roce)}</div></div>
        </div>
      </Card>

      {/* Balance Sheet / Leverage */}
      <Card>
        <CardHeader title="Balance Sheet / Leverage" />
        <div className="p-4 grid gap-4 sm:grid-cols-3 text-xs">
          <div><div className="text-[var(--text-muted)] flex items-center gap-1">Debt / Equity <Tooltip text={EXPLANATIONS["Debt/Equity"]} /></div><div className="num">{debtEquity !== null ? `${debtEquity.toFixed(2)}x` : "—"}</div></div>
          <div><div className="text-[var(--text-muted)]">Net Debt</div><div className="num">{crore(latest.net_debt)} cr</div></div>
          <div><div className="text-[var(--text-muted)]">Total Debt</div><div className="num">Not available</div></div>
          <div><div className="text-[var(--text-muted)]">Cash & Equivalents</div><div className="num">—</div></div>
          <div><div className="text-[var(--text-muted)] flex items-center gap-1">Interest Coverage <Tooltip text={EXPLANATIONS["Interest Coverage"]} /></div><div className="num">{interestCoverage !== null ? `${interestCoverage.toFixed(1)}x` : "—"}</div></div>
        </div>
      </Card>

      {/* Cash Flow Quality */}
      <Card>
        <CardHeader title="Cash Flow Quality" />
        <div className="p-4 text-xs leading-relaxed">
          <p>Healthy businesses generally convert accounting profits into operating cash over time.</p>
          <div className="mt-3 grid gap-2 sm:grid-cols-3">
            <div><div className="text-[var(--text-muted)]">Operating Cash Flow</div><div className="num font-medium">{crore(latest.cfo)} cr</div></div>
            <div><div className="text-[var(--text-muted)]">CFO vs PAT</div><div className="num">{latest.cfo !== null && latest.pat !== null && latest.pat !== 0 ? `${(latest.cfo / latest.pat).toFixed(2)}x` : "—"}</div></div>
            <div><div className="text-[var(--text-muted)]">Free Cash Flow</div><div className="num">{crore(latest.free_cash_flow)} cr</div></div>
          </div>
          <div className="mt-3 text-[var(--text-muted)] text-[0.6875rem]">Shows whether PAT converts into cash. CFO &gt; PAT suggests strong cash conversion.</div>
        </div>
      </Card>

      {/* Data & Sources */}
      <Card>
        <CardHeader title="Data & Sources" />
        <div className="p-4 text-[0.6875rem] text-[var(--text-muted)] leading-relaxed">
          <p>Latest fiscal year: FY{latest.fiscal_year}</p>
          <p>Periods available: {data.periods.fiscal_years.length} years ({data.periods.fiscal_years.join(", ")})</p>
          <p>Revenue CAGR 5Y: {data.revenue_cagr_5y !== null ? percent(data.revenue_cagr_5y) : "—"} • Full: {data.revenue_cagr_full !== null ? percent(data.revenue_cagr_full) : "—"}</p>
          <p className="mt-2">Source: Canonical financial facts, precedence chain, balance_sheet_ties={String(latest.balance_sheet_ties)}</p>
          <p className="mt-1">Unavailable metrics shown as — or Not available, never fabricated.</p>
        </div>
      </Card>

      {/* Advanced Research — detailed table preserved */}
      <Card>
        <div className="p-4">
          <h3 className="text-sm font-semibold">Detailed Financials (Advanced)</h3>
          <p className="text-[0.6875rem] text-[var(--text-muted)] mb-3">Full table preserved for institutional users. Toggle Advanced to see all years.</p>
          {advanced && (
            <div className="scroll-x">
              <table className="grid-table">
                <thead><tr><th>Fiscal year</th><th>Revenue</th><th>EBITDA</th><th>Margin</th><th>PAT</th><th>EPS</th><th>CFO</th><th>FCF</th><th>Net debt</th><th>ROE</th><th>Ties</th></tr></thead>
                <tbody>
                  {data.summary.map((s) => (
                    <tr key={s.fiscal_year}>
                      <td className="num font-medium">FY{String(s.fiscal_year).slice(-2)}</td>
                      <td className="num">{crore(s.revenue)}</td>
                      <td className="num">{crore(s.ebitda)}</td>
                      <td className="num">{percent(s.ebitda_margin)}</td>
                      <td className="num">{crore(s.pat)}</td>
                      <td className="num">{s.eps === null ? EM_DASH : `Rs${s.eps.toFixed(2)}`}</td>
                      <td className="num">{crore(s.cfo)}</td>
                      <td className="num">{crore(s.free_cash_flow)}</td>
                      <td className="num">{crore(s.net_debt)}</td>
                      <td className="num">{percent(s.roe)}</td>
                      <td>{s.balance_sheet_ties ? "Y" : "N"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {!advanced && <div className="text-xs text-[var(--text-muted)]">Enable Advanced toggle to see full table.</div>}
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
