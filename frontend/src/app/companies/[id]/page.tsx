"use client";

import { AppShell } from "@/components/layout/app-shell";
import { CompanyTabs } from "@/components/layout/company-tabs";
import { Badge, Card, CardBody, CardHeader, EmptyState, Skeleton, Stat } from "@/components/ui";
import { api, aiApi, scoringApi, valuationApi, analysisApi, filingsApi } from "@/lib/api";
import { crore, fiscalYear, marketCap, marketPrice, percent, plainNumber, rupees, signClass } from "@/lib/format";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, CheckCircle2, Database, ExternalLink, Info, Plus, TrendingUp, ShieldCheck, Brain, Target } from "lucide-react";
import Link from "next/link";
import { use, useEffect, useState } from "react";

function useAdvanced() {
  const [advanced, setAdvanced] = useState(false);
  useEffect(() => {
    try { const v = localStorage.getItem("ep:advanced"); if (v) setAdvanced(v === "1"); } catch {}
  }, []);
  const toggle = () => {
    setAdvanced(prev => {
      const next = !prev;
      try { localStorage.setItem("ep:advanced", next ? "1" : "0"); } catch {}
      return next;
    });
  };
  return { advanced, toggle };
}

function Tooltip({ text, children }: { text: string; children?: React.ReactNode }) {
  return (
    <span className="inline-flex items-center gap-1 group relative">
      {children}
      <Info size={10} className="text-[var(--text-muted)] cursor-help" />
      <span className="pointer-events-none absolute left-0 top-5 z-10 hidden w-56 rounded border bg-[var(--bg-elevated)] p-2 text-[0.6875rem] leading-snug shadow group-hover:block">
        {text}
      </span>
    </span>
  );
}

const EXPLANATIONS: Record<string, string> = {
  "EBITDA margin": "EBITDA margin shows operating profitability before interest, tax, depreciation — higher means more efficient core business.",
  "PAT margin": "PAT margin is net profit after tax divided by revenue — shows final profitability.",
  "ROCE": "ROCE measures how efficiently the company generates operating profit from total capital invested.",
  "Net Debt": "Net debt = total debt minus cash. Negative means net cash. High net debt relative to assets signals leverage risk.",
  "PE": "PE multiple = price divided by earnings per share. Higher PE means market pays more for each rupee of profit.",
  "CAGR": "CAGR is compound annual growth rate — average yearly growth over 3 or 5 years.",
  "Intrinsic Value": "Intrinsic value is estimated fair value per share from DCF and relative models, not market price.",
  "Upside": "Upside = (intrinsic - market price)/market price. Positive means potentially cheap.",
};

function calcCAGR(values: (number | null)[], years: number): number | null {
  // values ordered oldest to newest, take last N+1 points for N-year CAGR
  const clean = values.filter((v) => v !== null && v !== undefined && v > 0) as number[];
  if (clean.length < years + 1) return null;
  const start = clean[clean.length - 1 - years];
  const end = clean[clean.length - 1];
  if (!start || start <= 0 || !end) return null;
  return Math.pow(end / start, 1 / years) - 1;
}

export default function CompanyProfilePage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const queryClient = useQueryClient();
  const { advanced, toggle } = useAdvanced();
  const [showWatchlistPicker, setShowWatchlistPicker] = useState(false);
  const [selectedWatchlistId, setSelectedWatchlistId] = useState<number | null>(null);
  const [addNote, setAddNote] = useState("");

  const { data, isLoading, error } = useQuery({
    queryKey: ["company-profile", id],
    queryFn: () => api.companyProfile(id),
  });

  const ticker = data?.company.ticker;

  const financials = useQuery({
    queryKey: ["financials-overview", ticker],
    queryFn: () => analysisApi.financials(ticker!),
    enabled: Boolean(ticker),
  });

  const score = useQuery({
    queryKey: ["scoring", id],
    queryFn: () => scoringApi.get(id),
    enabled: !!data,
  });

  const valuation = useQuery({
    queryKey: ["valuation", id],
    queryFn: () => valuationApi.get(id),
    enabled: !!data,
  });

  const aiProviders = useQuery({
    queryKey: ["ai-providers"],
    queryFn: () => aiApi.providers(),
    enabled: !!data,
  });

  const aiThesis = useQuery({
    queryKey: ["ai-thesis", id],
    queryFn: () => aiApi.analyse(id, "investment_thesis"),
    enabled: !!data,
  });

  const peers = useQuery({
    queryKey: ["peers", id],
    queryFn: () => scoringApi.peers(id, undefined, 6),
    enabled: !!data,
  });

  const filings = useQuery({
    queryKey: ["filings", ticker],
    queryFn: () => filingsApi.get(ticker!, 3),
    enabled: Boolean(ticker),
  });

  const watchlists = useQuery({
    queryKey: ["watchlists"],
    queryFn: () => import("@/lib/api").then(m => m.watchlistApi.list()),
    enabled: !!data,
  });

  const addToWatchlist = useMutation({
    mutationFn: (watchlistId: number) =>
      import("@/lib/api").then(m => m.watchlistApi.add(watchlistId, {
        ticker: data?.company?.ticker || "",
        note: addNote || undefined,
      })),
    onSuccess: () => {
      setShowWatchlistPicker(false);
      setAddNote("");
      setSelectedWatchlistId(null);
      queryClient.invalidateQueries({ queryKey: ["watchlist-rows"] });
    },
  });

  if (isLoading) {
    return (
      <AppShell>
        <Skeleton className="h-28" />
        <div className="mt-4 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          {Array.from({ length: 4 }).map((_, i) => <Skeleton key={i} className="h-24" />)}
        </div>
      </AppShell>
    );
  }

  if (error || !data) {
    return (
      <AppShell>
        <Card>
          <EmptyState icon={<AlertTriangle size={28} />} title="Company not found" description="This company is not in the coverage universe." action={<Link href="/companies" className="text-xs text-accent-500 hover:underline">Back to companies</Link>} />
        </Card>
      </AppShell>
    );
  }

  const { company: c, coverage } = data;
  const hasData = coverage.has_data;

  // Growth metrics from financials overview
  const summary = financials.data?.summary ?? [];
  const revenues = summary.map(s => s.revenue);
  const pats = summary.map(s => s.pat);
  const roces = summary.map(s => s.roce);

  const revCAGR3 = calcCAGR(revenues, 3);
  const revCAGR5 = financials.data?.revenue_cagr_5y ?? calcCAGR(revenues, 5);
  const patCAGR3 = calcCAGR(pats, 3);
  const patCAGR5 = calcCAGR(pats, 5);
  const latestROCE = roces.length ? roces[roces.length - 1] : null;
  const prevROCE = roces.length > 1 ? roces[roces.length - 2] : null;
  const roceTrend = latestROCE !== null && prevROCE !== null ? (latestROCE > prevROCE ? "↑" : latestROCE < prevROCE ? "↓" : "→") : "";

  // Financial Health multi-signal
  const signals: { label: string; value: string; ok: boolean | null }[] = [];
  let okCount = 0;
  let totalCount = 0;

  // Revenue growth
  if (revCAGR5 !== null || revCAGR3 !== null) {
    const g = revCAGR5 ?? revCAGR3;
    const ok = g !== null && g > 0.1;
    signals.push({ label: "Revenue growth", value: g !== null ? percent(g) : "—", ok });
    if (g !== null) { totalCount++; if (ok) okCount++; }
  }

  // PAT margin
  if (data.pat_margin !== null) {
    const ok = data.pat_margin > 0.1;
    signals.push({ label: "PAT margin", value: percent(data.pat_margin), ok });
    totalCount++; if (ok) okCount++;
  }

  // ROCE
  if (latestROCE !== null) {
    const ok = latestROCE > 0.15;
    signals.push({ label: "ROCE", value: percent(latestROCE), ok });
    totalCount++; if (ok) okCount++;
  }

  // Net Debt / total assets
  if (data.net_debt !== null && data.total_assets) {
    const ratio = data.net_debt / data.total_assets;
    const ok = ratio < 0.2 || data.net_debt < 0;
    const val = data.net_debt < 0 ? `Net cash ${crore(-data.net_debt)}` : `${crore(data.net_debt)} cr`;
    signals.push({ label: "Net debt", value: val, ok });
    totalCount++; if (ok) okCount++;
  }

  // Operating Cash Flow (cfo)
  const latestCFO = summary.length ? summary[summary.length - 1]?.cfo : null;
  if (latestCFO !== null && latestCFO !== undefined) {
    const ok = latestCFO > 0;
    signals.push({ label: "Operating cash flow", value: ok ? "Positive" : "Negative", ok });
    totalCount++; if (ok) okCount++;
  }

  let healthVerdict: "Healthy" | "Needs Attention" | "Weak" | "Insufficient Data" = "Insufficient Data";
  if (totalCount === 0) healthVerdict = "Insufficient Data";
  else if (okCount >= 4) healthVerdict = "Healthy";
  else if (okCount >= 2) healthVerdict = "Needs Attention";
  else healthVerdict = "Weak";

  // AI Transparency
  const liveProviders = aiProviders.data?.providers?.filter(p => p.configured && p.name !== "Offline") ?? [];
  const isLiveAI = liveProviders.length > 0;
  const aiMode = isLiveAI ? "Live AI" : "Offline/template";
  const aiProviderName = aiThesis.data?.provider ?? (isLiveAI ? liveProviders[0]?.name ?? "Live" : "Offline");
  const aiIsTemplate = !isLiveAI || (aiThesis.data?.provider?.toLowerCase().includes("offline") ?? false);

  const valuationVerdict = valuation.data?.summary?.recommendation ?? "—";
  const upside = valuation.data?.summary?.upside ?? null;

  return (
    <AppShell>
      <CompanyTabs companyId={id} />

      <div className="mb-4 flex items-center justify-between">
        <div className="text-[0.6875rem] text-[var(--text-muted)]">Simple view: key health, growth, AI summary. Advanced reveals full research.</div>
        <button onClick={toggle} className="text-[0.6875rem] px-2.5 py-1 rounded border hover:bg-[var(--bg-subtle)]">{advanced ? "Switch to Simple" : "Switch to Advanced Research"}</button>
      </div>

      <Card className="mb-5">
        <CardBody>
          <div className="flex flex-wrap items-start justify-between gap-4">
            <div className="min-w-0">
              <div className="flex flex-wrap items-center gap-2">
                <span className="num rounded bg-accent-500 px-2 py-0.5 text-xs font-bold text-white">{c.ticker}</span>
                <Badge>{c.exchange}</Badge>
                {c.sector && <Badge variant="accent">{c.sector}</Badge>}
                {hasData ? <Badge variant="gain"><CheckCircle2 size={10} /> {coverage.items_populated} facts</Badge> : <Badge variant="warn"><AlertTriangle size={10} /> No financials</Badge>}
                {score.data && <Badge variant={score.data.grade?.startsWith("A") ? "gain" : "neutral"}>{score.data.grade} • {score.data.recommendation}</Badge>}
              </div>
              <h1 className="mt-2 text-xl font-semibold">{c.name}</h1>
              <p className="mt-0.5 text-xs text-[var(--text-muted)]">{c.industry ?? "—"}{c.isin && <> · ISIN {c.isin}</>}{(c as any).bse_code && <> · BSE {(c as any).bse_code}</>}</p>
              <p className="mt-2 text-xs leading-relaxed text-[var(--text)] max-w-2xl"><span className="font-medium">What it does: </span>{c.description ? c.description.slice(0, 180) + (c.description.length > 180 ? "…" : "") : "No description available."}</p>
            </div>
            <div className="text-right">
              <div className="num text-2xl font-semibold">{rupees(marketPrice(c))}</div>
              {c.market && <div className={`mt-0.5 text-[0.6875rem] font-medium ${signClass(c.market.change)}`}>{c.market.change !== null && <> {c.market.change >= 0 ? "+" : ""}{rupees(c.market.change)} ({percent(c.market.change_percent)})</>}</div>}
              <div className="mt-0.5 text-[0.6875rem] uppercase tracking-wider text-[var(--text-muted)]">{marketCap(c.market_cap)} market cap • {c.market?.price_source ?? "Internal"} • {c.market?.market_status ?? ""}</div>
              <button onClick={() => setShowWatchlistPicker(true)} className="mt-2 inline-flex items-center gap-1.5 rounded bg-accent-500/90 px-2.5 py-1 text-[10px] font-medium text-white hover:bg-accent-500"><Plus size={12} /> Add to Watchlist</button>
            </div>
          </div>
        </CardBody>
      </Card>

      {!hasData ? (
        <Card><EmptyState icon={<Database size={28} />} title="No financial data available" description="No canonical facts loaded. Figures are never fabricated." /></Card>
      ) : (
        <div className="mb-6 space-y-4">
          {/* Growth Card — actual CAGR */}
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <Card>
              <CardBody>
                <div className="flex items-center gap-2 text-[0.6875rem] text-[var(--text-muted)]"><TrendingUp size={12} /> Growth <Tooltip text={EXPLANATIONS["CAGR"]} /></div>
                <div className="mt-2 space-y-1 text-xs">
                  <div className="flex justify-between"><span>Revenue CAGR 3Y</span><span className="num">{revCAGR3 !== null ? percent(revCAGR3) : "Insufficient data"}</span></div>
                  <div className="flex justify-between"><span>Revenue CAGR 5Y</span><span className="num">{revCAGR5 !== null ? percent(revCAGR5) : "Insufficient data"}</span></div>
                  <div className="flex justify-between"><span>PAT CAGR 3Y</span><span className="num">{patCAGR3 !== null ? percent(patCAGR3) : "Insufficient data"}</span></div>
                  <div className="flex justify-between"><span>PAT CAGR 5Y</span><span className="num">{patCAGR5 !== null ? percent(patCAGR5) : "Insufficient data"}</span></div>
                  <div className="flex justify-between"><span>ROCE <Tooltip text={EXPLANATIONS["ROCE"]} /></span><span className="num">{latestROCE !== null ? `${percent(latestROCE)} ${roceTrend}` : "Insufficient data"}</span></div>
                </div>
                <div className="mt-2 text-[0.625rem] text-[var(--text-muted)]">How calculated? CAGR = (End/Start)^(1/years)-1 from reported revenue/PAT. No fabricated values.</div>
              </CardBody>
            </Card>

            {/* Financial Health multi-signal */}
            <Card>
              <CardBody>
                <div className="flex items-center gap-2 text-[0.6875rem] text-[var(--text-muted)]"><ShieldCheck size={12} /> Financial Health</div>
                <div className="mt-1 text-sm font-medium">
                  {healthVerdict === "Healthy" && "🟢 Healthy"}
                  {healthVerdict === "Needs Attention" && "🟡 Needs Attention"}
                  {healthVerdict === "Weak" && "🔴 Weak"}
                  {healthVerdict === "Insufficient Data" && "⚪ Insufficient Data"}
                </div>
                <div className="mt-2 space-y-1 text-xs">
                  {signals.slice(0,5).map((s, i) => (
                    <div key={i} className="flex justify-between"><span>{s.label}</span><span className={`num ${s.ok === true ? "text-gain" : s.ok === false ? "text-loss" : ""}`}>{s.value}</span></div>
                  ))}
                  {signals.length === 0 && <div className="text-[var(--text-muted)]">Insufficient data</div>}
                </div>
              </CardBody>
            </Card>

            {/* Valuation */}
            <Card>
              <CardBody>
                <div className="flex items-center gap-2 text-[0.6875rem] text-[var(--text-muted)]"><Target size={12} /> Valuation <Tooltip text={EXPLANATIONS["Intrinsic Value"]} /></div>
                <div className="mt-1 text-sm font-medium">{valuationVerdict}{upside !== null && ` • ${upside > 0 ? "+" : ""}${upside.toFixed(1)}% upside`}</div>
                <div className="mt-1 text-[0.6875rem] text-[var(--text-muted)] flex items-center gap-1">Intrinsic {valuation.data?.summary?.weighted_value ? `₹${valuation.data.summary.weighted_value.toFixed(1)}` : "—"} <Tooltip text={EXPLANATIONS["Upside"]} /></div>
              </CardBody>
            </Card>

            <Card>
              <CardBody>
                <div className="flex items-center gap-2 text-[0.6875rem] text-[var(--text-muted)]"><TrendingUp size={12} /> Financial Trends</div>
                <div className="mt-2 space-y-1 text-xs">
                  <div className="flex justify-between"><span>Revenue</span><span className="num">{summary.length >= 2 && summary[summary.length-1].revenue !== null ? `${crore(summary[summary.length-1].revenue)} cr ${summary[summary.length-1].revenue! > summary[summary.length-2].revenue! ? "↑" : "↓"}` : "Insufficient data"}</span></div>
                  <div className="flex justify-between"><span>PAT</span><span className="num">{summary.length >= 2 && summary[summary.length-1].pat !== null ? `${crore(summary[summary.length-1].pat)} cr ${summary[summary.length-1].pat! > summary[summary.length-2].pat! ? "↑" : "↓"}` : "Insufficient data"}</span></div>
                  <div className="flex justify-between"><span>ROCE <Tooltip text={EXPLANATIONS["ROCE"]} /></span><span className="num">{latestROCE !== null ? `${percent(latestROCE)} ${roceTrend}` : "—"}</span></div>
                </div>
                <div className="mt-2 text-[0.625rem] text-[var(--text-muted)]">Last 5Y from canonical facts. Unit: ₹ cr / %. No fabricated values.</div>
                <Link href={`/companies/${id}/charts`} className="mt-2 inline-block text-[0.6875rem] text-accent-500 hover:underline">View detailed charts →</Link>
              </CardBody>
            </Card>
          </div>

          <div className="grid gap-4 lg:grid-cols-3">
            <Card className="lg:col-span-2">
              <CardHeader title="AI Investment View (Simple)" subtitle={`Source: AI Research Engine • Mode: ${aiMode}`} />
              <CardBody className="space-y-3">
                {aiIsTemplate && <div className="rounded border border-warn/40 bg-warn/10 px-2.5 py-1.5 text-[0.6875rem] text-warn">Offline/template research — limited AI provider configuration. {liveProviders.length === 0 ? "No live provider key configured." : ""} Never presented as live AI.</div>}
                {!aiIsTemplate && <div className="rounded border border-gain/30 bg-gain/10 px-2.5 py-1.5 text-[0.6875rem] text-gain">Live AI • Provider: {aiProviderName} • Mode: Live AI</div>}
                {aiThesis.isLoading && <Skeleton className="h-20" />}
                {aiThesis.data && <div className="text-xs leading-relaxed"><p className="line-clamp-4">{(aiThesis.data as any).display_content?.slice(0, 500) ?? (aiThesis.data as any).content?.slice(0,500) ?? "—"}</p><Link href={`/companies/${id}/ai`} className="mt-2 inline-block text-[0.6875rem] text-accent-500 hover:underline">Full AI analysis →</Link></div>}
                {!aiThesis.data && !aiThesis.isLoading && <p className="text-xs text-[var(--text-muted)]">AI thesis not yet generated.</p>}
                <div className="grid gap-3 sm:grid-cols-2 pt-3 border-t">
                  <div><div className="text-[0.6875rem] font-medium flex items-center gap-1"><CheckCircle2 size={12} className="text-gain" /> Positives</div><ul className="mt-1 space-y-1 text-[0.6875rem] list-disc ml-4">{(score.data?.strongest?.slice(0,3) ?? []).map((s:any,i:number)=><li key={i}>{typeof s==="string"?s:s.label??s.key}</li>)}</ul></div>
                  <div><div className="text-[0.6875rem] font-medium flex items-center gap-1"><AlertTriangle size={12} className="text-loss" /> Risks</div><ul className="mt-1 space-y-1 text-[0.6875rem] list-disc ml-4">{(score.data?.weakest?.slice(0,3) ?? []).map((s:any,i:number)=><li key={i}>{typeof s==="string"?s:s.label??s.key}</li>)}</ul></div>
                </div>
              </CardBody>
            </Card>

            <div className="space-y-4">
              <Card>
                <CardHeader title="Financial Health Details" />
                <CardBody className="space-y-2 text-xs">
                  <div className="flex justify-between"><span>PAT margin <Tooltip text={EXPLANATIONS["PAT margin"]} /></span><span className="num">{percent(data.pat_margin)}</span></div>
                  <div className="flex justify-between"><span>ROCE <Tooltip text={EXPLANATIONS["ROCE"]} /></span><span className="num">{latestROCE !== null ? percent(latestROCE) : "—"}</span></div>
                  <div className="flex justify-between"><span>Net debt <Tooltip text={EXPLANATIONS["Net Debt"]} /></span><span className={`num ${signClass(data.net_debt ? -data.net_debt : null)}`}>{crore(data.net_debt)}</span></div>
                  <div className="flex justify-between"><span>CFO</span><span className="num">{financials.data?.summary?.length ? crore(financials.data.summary[financials.data.summary.length-1].cfo) : "—"}</span></div>
                </CardBody>
              </Card>

              <Card>
                <CardHeader title="Peer Snapshot" subtitle="Category scores — model scores, not actual PE/ROCE/Growth" />
                <CardBody>
                  <p className="text-[0.6875rem] text-[var(--text-muted)] mb-2">Category scores are model scores, not actual PE, ROCE or revenue-growth percentages. Exact PE/ROCE/CAGR shows “—” when unavailable from existing peer endpoint (never fabricated).</p>
                  {peers.isLoading ? <Skeleton className="h-16" /> : (
                    <div className="space-y-3">
                      {(peers.data?.peers?.slice(0,3) ?? []).map((p:any) => (
                        <div key={p.company?.ticker ?? p.ticker} className="rounded border p-2.5">
                          <div className="font-medium text-xs num">{p.company?.ticker ?? p.ticker} <span className="text-[0.6875rem] text-[var(--text-muted)]">{p.company?.name?.slice(0,30) ?? ""}</span></div>
                          <div className="mt-1.5 grid grid-cols-2 gap-1 text-[0.6875rem]">
                            <div className="flex justify-between"><span>Valuation Score</span><span className="num">{p.category_scores?.valuation !== undefined ? `${p.category_scores.valuation.toFixed(1)}/10` : "—"}</span></div>
                            <div className="flex justify-between"><span>Financial Quality</span><span className="num">{p.category_scores?.financial_quality !== undefined ? `${p.category_scores.financial_quality.toFixed(1)}/10` : "—"}</span></div>
                            <div className="flex justify-between"><span>Growth Quality</span><span className="num">{p.category_scores?.growth_quality !== undefined ? `${p.category_scores.growth_quality.toFixed(1)}/10` : "—"}</span></div>
                            <div className="flex justify-between"><span>Overall Score</span><span className="num">{p.overall_score ? `${p.overall_score.toFixed(1)} • ${p.grade}` : "—"}</span></div>
                            <div className="flex justify-between"><span>Exact PE</span><span className="num">—</span></div>
                            <div className="flex justify-between"><span>Exact ROCE</span><span className="num">—</span></div>
                            <div className="flex justify-between"><span>Exact Growth</span><span className="num">—</span></div>
                          </div>
                        </div>
                      ))}
                      {(!peers.data?.peers || peers.data.peers.length===0) && <p className="text-[0.6875rem] text-[var(--text-muted)]">No peers yet. Same-sector companies with financials will appear here.</p>}
                      <Link href={`/companies/${id}/peers`} className="mt-2 inline-block text-[0.6875rem] text-accent-500 hover:underline">View detailed peer analysis →</Link>
                    </div>
                  )}
                </CardBody>
              </Card>
            </div>
          </div>

          {/* Latest News — 3 real filings */}
          <div className="grid gap-4 lg:grid-cols-3">
            <Card className="lg:col-span-2">
              <CardHeader title="Latest News & Filings" subtitle="Official NSE/BSE filings first, then market news" />
              <CardBody className="space-y-3">
                {filings.isLoading && <Skeleton className="h-24" />}
                {filings.isError && <p className="text-xs text-loss">Could not load filings: {(filings.error as Error).message}</p>}
                {filings.data && filings.data.filings.length === 0 && (
                  <div className="text-xs text-[var(--text-muted)]">
                    <p>No recent official filings found for this company.</p>
                    <p className="mt-1">Source tried: {filings.data.providers_attempted?.map((p:any) => p.provider).join(", ") || "NSE, BSE, RAG"}</p>
                  </div>
                )}
                {filings.data && filings.data.filings.slice(0,3).map((f:any, i:number) => (
                  <div key={i} className="border-b last:border-0 pb-2.5">
                    <div className="flex justify-between gap-2 text-[0.6875rem] text-[var(--text-muted)]">
                      <span>{f.filed_on ? new Date(f.filed_on).toLocaleDateString("en-IN", { day:"numeric", month:"short", year:"numeric" }) : "Date —"}</span>
                      <span className="uppercase tracking-wider">{f.exchange ?? f.category ?? "—"} • {f.category ?? ""}</span>
                    </div>
                    <div className="mt-1 text-xs font-medium leading-snug">{f.title}</div>
                    {f.summary && <div className="mt-1 text-[0.6875rem] leading-snug text-[var(--text-muted)] line-clamp-2">{f.summary}</div>}
                    {f.url && <a href={f.url} target="_blank" rel="noreferrer" className="mt-1 inline-flex items-center gap-1 text-[0.6875rem] text-accent-500 hover:underline">Read announcement → <ExternalLink size={9} /></a>}
                  </div>
                ))}
                <Link href={`/companies/${id}/news`} className="mt-2 inline-block text-[0.6875rem] text-accent-500 hover:underline">View all news →</Link>
              </CardBody>
            </Card>

            <div className="space-y-4">
              <Card>
                <CardHeader title="Source Transparency" />
                <CardBody className="text-[0.6875rem] leading-relaxed text-[var(--text-muted)]">
                  <p>Every number traces to source: {coverage.items_populated} facts across {coverage.fiscal_years.length} years, precedence chain override→store→alias→absent.</p>
                  <p className="mt-2">ISIN: {c.isin ?? "—"} • BSE: {(c as any).bse_code ?? "—"} • Exchange: {c.exchange}</p>
                  <Link href={`/companies/${id}/documents`} className="mt-2 inline-block text-accent-500 hover:underline">Inspect filings & docs →</Link>
                </CardBody>
              </Card>
              <Card>
                <CardHeader title="Advanced Research" />
                <CardBody className="text-xs">
                  <p className="text-[var(--text-muted)]">Institutional data remains under Advanced toggle: full statements, 45+ ratios, DCF assumptions, 13 scoring pillars, 21 AI sections, forecast editor, document chunks, knowledge graph.</p>
                  <button onClick={toggle} className="mt-2 px-2.5 py-1 rounded border text-[0.6875rem]">{advanced ? "Hide Advanced" : "Show Advanced"}</button>
                </CardBody>
              </Card>
            </div>
          </div>
        </div>
      )}

      {advanced && (
        <div className="mt-6 border-t pt-4">
          <h3 className="text-sm font-semibold mb-3">Advanced Research (Institutional)</h3>
          <p className="text-xs text-[var(--text-muted)] mb-3">Full 10-year statements, 54 line items, 45+ ratios, debt, working capital, capex, DCF, WACC, SOTP, sensitivity, Monte Carlo, 13 scoring pillars, 21 AI sections, documents, evidence, knowledge graph.</p>
          <div className="grid gap-4 sm:grid-cols-2">
            <Card><CardBody><div className="text-xs">Revenue {crore(data.revenue)} • EBITDA {crore(data.ebitda)} • PAT {crore(data.pat)} • EPS ₹{data.eps?.toFixed(2) ?? "—"}</div></CardBody></Card>
            <Card><CardBody><div className="text-xs">Coverage {percent(coverage.coverage,0)} • {coverage.items_populated} facts • {coverage.fiscal_years.length} years</div></CardBody></Card>
          </div>
        </div>
      )}

      {showWatchlistPicker && (
        <div className="fixed inset-0 z-[70] flex items-center justify-center bg-black/50 p-4">
          <div className="w-full max-w-sm rounded-xl border bg-[var(--bg-elevated)] p-5">
            <div className="font-semibold mb-3">Add {c.ticker} to Watchlist</div>
            <input value={addNote} onChange={(e)=>setAddNote(e.target.value)} placeholder="Thesis note..." className="w-full rounded border px-3 py-1.5 text-sm mb-3" />
            <div className="flex gap-2 justify-end"><button onClick={()=>setShowWatchlistPicker(false)} className="px-3 py-1 text-xs border rounded">Cancel</button><button disabled={!selectedWatchlistId} onClick={()=>selectedWatchlistId && addToWatchlist.mutate(selectedWatchlistId)} className="px-3 py-1 text-xs bg-accent-500 text-white rounded">Add</button></div>
          </div>
        </div>
      )}
    </AppShell>
  );
}
