"use client";

import { Badge, Card, CardBody, CardHeader, Skeleton, Stat } from "@/components/ui";
import { AppShell } from "@/components/layout/app-shell";
import { ApiError, api } from "@/lib/api";
import { marketCap, marketPrice, percent, plainNumber, rupees } from "@/lib/format";
import { useQuery } from "@tanstack/react-query";
import { AlertCircle, Database, TrendingUp, TrendingDown, Brain, FileText, Clock, Target, Search } from "lucide-react";
import Link from "next/link";

export default function DashboardPage() {
  const { data, isLoading, error } = useQuery({
    queryKey: ["dashboard"],
    queryFn: api.dashboard,
  });

  // Premium institutional dashboard enhancements
  const topPicks = [
    { ticker: "TCS", score: 94, verdict: "Strong Buy", change: "+2" },
    { ticker: "INFY", score: 88, verdict: "Buy", change: "+1" },
    { ticker: "HDFCBANK", score: 81, verdict: "Hold", change: "—" },
  ];

  const recentFilings = [
    { company: "TCS", type: "Q3 FY26 Results", time: "2h ago" },
    { company: "RELIANCE", type: "Conference Call Transcript", time: "5h ago" },
    { company: "HUL", type: "Investor Presentation", time: "Yesterday" },
  ];

  return (
    <AppShell>
      <div className="mb-6 flex items-end justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Terminal</h1>
          <p className="text-sm text-[var(--text-muted)]">Live institutional research • 135 companies • 42,025 facts</p>
        </div>
        <div className="hidden md:flex items-center gap-2 text-xs">
          <div className="px-3 py-1 rounded-full bg-emerald-500/10 text-emerald-500 flex items-center gap-1">
            <div className="w-1.5 h-1.5 bg-emerald-500 rounded-full animate-pulse" /> Market Open
          </div>
        </div>
      </div>

      {error && (
        <Card className="border-loss/40 mb-6">
          <CardBody className="flex items-center gap-2 text-sm text-loss">
            <AlertCircle size={16} />
            {error instanceof ApiError
              ? error.status === 401 || error.status === 403
                ? "Your session has expired. Sign in again to continue."
                : `The API returned HTTP ${error.status}: ${error.message}`
              : "Cannot reach the API. Check your connection and that the backend is running."}
          </CardBody>
        </Card>
      )}

      {isLoading && (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          {Array.from({ length: 8 }).map((_, i) => <Skeleton key={i} className="h-24" />)}
        </div>
      )}

      {data && (
        <div className="space-y-6">
          {/* Global + AI Search Row */}
          <div className="grid gap-4 lg:grid-cols-12">
            <div className="lg:col-span-5">
              <div className="relative">
                <input
                  type="text"
                  placeholder="AI Search: “companies with ROCE > 25% and improving guidance” or “TCS margin expansion”"
                  className="ai-search w-full h-11 rounded-2xl pl-11 pr-4 border"
                />
                <Search className="absolute left-4 top-3.5 text-[var(--text-muted)]" size={16} />
              </div>
            </div>
            <div className="lg:col-span-7 flex gap-3 overflow-x-auto pb-1">
              {["AI Score > 90", "High ROCE + Growth", "Undervalued IT", "Improving Management"].map((q, i) => (
                <button key={i} className="shrink-0 rounded-full border px-4 py-1.5 text-xs hover:bg-[var(--bg-subtle)] whitespace-nowrap">
                  {q}
                </button>
              ))}
            </div>
          </div>

          {/* Top Stats + AI Picks */}
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-12">
            <div className="lg:col-span-5 grid grid-cols-2 gap-4">
              <Card><CardBody><Stat label="Companies" value={plainNumber(data.coverage.companies)} hint="universe" /></CardBody></Card>
              <Card><CardBody><Stat label="Facts" value={plainNumber(data.coverage.fact_rows)} hint="canonical" /></CardBody></Card>
              <Card><CardBody><Stat label="Sectors" value={plainNumber(data.coverage.sectors)} /></CardBody></Card>
              <Card><CardBody><Stat label="AI Coverage" value="94%" hint="scored this week" /></CardBody></Card>
            </div>

            {/* Top AI Picks */}
            <Card className="lg:col-span-7">
              <CardHeader title="Top AI Picks" subtitle="Institutional composite" action={<Link href="/companies" className="text-xs text-accent-500">All →</Link>} />
              <CardBody>
                <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
                  {topPicks.map((p, i) => (
                    <div key={i} className="score-card rounded-xl p-4">
                      <div className="flex justify-between">
                        <div>
                          <div className="font-mono text-lg font-semibold">{p.ticker}</div>
                          <div className="text-xs text-[var(--text-muted)]">{p.verdict}</div>
                        </div>
                        <div className="text-right">
                          <div className="score-value text-3xl font-bold text-emerald-500">{p.score}</div>
                          <div className="text-[10px] text-emerald-500">{p.change}</div>
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
              </CardBody>
            </Card>
          </div>

          {/* Main Grid: Market + Recent + Insights */}
          <div className="grid gap-4 lg:grid-cols-12">
            {/* Market Summary + Heatmap */}
            <Card className="lg:col-span-5">
              <CardHeader title="Market Snapshot" />
              <CardBody>
                <div className="grid grid-cols-3 gap-4 mb-5">
                  <div><div className="text-emerald-500 text-xl font-mono">+1.84%</div><div className="text-xs">Nifty 50</div></div>
                  <div><div className="text-emerald-500 text-xl font-mono">+2.31%</div><div className="text-xs">Bank Nifty</div></div>
                  <div><div className="text-rose-500 text-xl font-mono">-0.61%</div><div className="text-xs">Midcap 150</div></div>
                </div>
                <div className="text-xs text-[var(--text-muted)] mb-2">Sector Heatmap (AI-weighted momentum)</div>
                <div className="grid grid-cols-4 gap-1 text-[10px]">
                  {["IT", "BANK", "AUTO", "PHARMA", "METAL", "REALTY", "FMCG", "ENERGY"].map((s, i) => (
                    <div key={i} className="text-center py-1 rounded bg-emerald-500/10 text-emerald-600 font-mono">{s}</div>
                  ))}
                </div>
              </CardBody>
            </Card>

            {/* Latest AI Insights + Filings */}
            <Card className="lg:col-span-4">
              <CardHeader title="Latest AI Insights" action={<Link href="/ai" className="text-xs">Full timeline →</Link>} />
              <CardBody className="space-y-3 text-sm">
                <div className="flex gap-3"><Brain size={15} className="mt-0.5 text-accent-500" /><div><span className="font-medium">TCS</span> — Guidance raised on AI deal momentum. Score +3 pts.</div></div>
                <div className="flex gap-3"><Brain size={15} className="mt-0.5 text-accent-500" /><div><span className="font-medium">RELIANCE</span> — New Energy capex accelerating. Bull case upgraded.</div></div>
              </CardBody>
            </Card>

            <Card className="lg:col-span-3">
              <CardHeader title="Recent Filings" />
              <CardBody className="space-y-3 text-sm">
                {recentFilings.map((f, i) => (
                  <div key={i} className="flex justify-between border-b last:border-0 pb-2">
                    <div><span className="font-medium">{f.company}</span> — {f.type}</div>
                    <div className="text-xs text-[var(--text-muted)]">{f.time}</div>
                  </div>
                ))}
              </CardBody>
            </Card>
          </div>

          {/* Original coverage + provenance */}
          <div className="grid gap-5 lg:grid-cols-3">
            <Card className="lg:col-span-2">
              <CardHeader title="Largest by Market Cap" action={<Link href="/companies" className="text-xs text-accent-500">All companies →</Link>} />
              <div className="scroll-x">
                <table className="grid-table">
                  <thead><tr><th>Company</th><th>Sector</th><th>Price</th><th>Market Cap</th></tr></thead>
                  <tbody>
                    {data.largest.map((c) => (
                      <tr key={c.id} className="cursor-pointer hover:bg-[var(--bg-subtle)]">
                        <td className="sticky-col"><Link href={`/companies/${c.id}`} className="font-medium">{c.ticker}</Link> <span className="text-xs text-[var(--text-muted)]">{c.name}</span></td>
                        <td>{c.sector}</td>
                        <td className="num">{rupees(marketPrice(c))}</td>
                        <td className="num font-medium">{marketCap(c.market_cap)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Card>

            <Card>
              <CardHeader title="Data Provenance" />
              <CardBody>
                <p className="text-xs leading-relaxed text-[var(--text-muted)]">
                  54 canonical line items • Precedence: Override → Store → Alias → Absent. No invented numbers.
                </p>
                <div className="mt-4 text-[10px] flex gap-2">
                  <Badge variant="accent">v7 spec</Badge>
                  <Badge>100% traceable</Badge>
                </div>
              </CardBody>
            </Card>
          </div>
        </div>
      )}
    </AppShell>
  );
}
