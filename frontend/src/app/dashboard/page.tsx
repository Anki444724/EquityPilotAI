"use client";

import { Badge, Card, CardBody, CardHeader, Skeleton, Stat } from "@/components/ui";
import { AppShell } from "@/components/layout/app-shell";
import { ApiError, api } from "@/lib/api";
import { marketCap, marketPrice, plainNumber, rupees } from "@/lib/format";
import { useQuery } from "@tanstack/react-query";
import { AlertCircle, Database, Search } from "lucide-react";
import Link from "next/link";

export default function DashboardPage() {
  const { data, isLoading, error } = useQuery({
    queryKey: ["dashboard"],
    queryFn: api.dashboard,
  });

  return (
    <AppShell>
      <div className="mb-6 flex items-end justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Terminal</h1>
          <p className="text-sm text-[var(--text-muted)]">Live institutional research • {data ? `${data.coverage.companies} companies • ${data.coverage.fact_rows.toLocaleString()} facts` : "Loading…"}</p>
        </div>
        <div className="hidden md:flex items-center gap-2 text-xs">
          <div className="px-3 py-1 rounded-full bg-emerald-500/10 text-emerald-500 flex items-center gap-1">
            <div className="w-1.5 h-1.5 bg-emerald-500 rounded-full animate-pulse" /> Live
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
          {/* Global AI Search Row - Simple */}
          <div className="grid gap-4 lg:grid-cols-12">
            <div className="lg:col-span-5">
              <div className="relative">
                <input
                  type="text"
                  placeholder="Search companies or ask AI: “ROCE > 25% and growing”"
                  className="ai-search w-full h-11 rounded-2xl pl-11 pr-4 border"
                />
                <Search className="absolute left-4 top-3.5 text-[var(--text-muted)]" size={16} />
              </div>
            </div>
            <div className="lg:col-span-7 flex gap-3 overflow-x-auto pb-1">
              {["AI Score > 80", "Healthy Balance Sheet", "Undervalued", "High Growth"].map((q, i) => (
                <Link key={i} href={`/companies?sector=${encodeURIComponent(q)}`} className="shrink-0 rounded-full border px-4 py-1.5 text-xs hover:bg-[var(--bg-subtle)] whitespace-nowrap">
                  {q}
                </Link>
              ))}
            </div>
          </div>

          {/* Top Stats */}
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <Card><CardBody><Stat label="Companies" value={plainNumber(data.coverage.companies)} hint="universe" /></CardBody></Card>
            <Card><CardBody><Stat label="Facts" value={plainNumber(data.coverage.fact_rows)} hint="canonical" /></CardBody></Card>
            <Card><CardBody><Stat label="Sectors" value={plainNumber(data.coverage.sectors)} /></CardBody></Card>
            <Card><CardBody><Stat label="Fiscal Years" value={data.coverage.fiscal_years.length} hint={`${data.coverage.fiscal_years[0]}–${data.coverage.fiscal_years[data.coverage.fiscal_years.length-1]}`} /></CardBody></Card>
          </div>

          {/* Main Grid: Largest + Recently Added (Live) */}
          <div className="grid gap-5 lg:grid-cols-3">
            <Card className="lg:col-span-2">
              <CardHeader title="Largest by Market Cap (Live)" action={<Link href="/companies" className="text-xs text-accent-500">All companies →</Link>} />
              <div className="scroll-x">
                <table className="grid-table">
                  <thead><tr><th>Company</th><th>Sector</th><th>Price</th><th>Market Cap</th></tr></thead>
                  <tbody>
                    {data.largest.map((c) => (
                      <tr key={c.id} className="cursor-pointer hover:bg-[var(--bg-subtle)]">
                        <td className="sticky-col"><Link href={`/companies/${c.id}`} className="font-medium">{c.ticker}</Link> <span className="text-xs text-[var(--text-muted)]">{c.name.slice(0,30)}</span></td>
                        <td className="text-xs">{c.sector}</td>
                        <td className="num text-xs">{rupees(marketPrice(c))}</td>
                        <td className="num text-xs font-medium">{marketCap(c.market_cap)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Card>

            <Card>
              <CardHeader title="Recently Added (Live)" />
              <CardBody className="space-y-2">
                {data.recently_added.map((c) => (
                  <div key={c.id} className="flex justify-between text-xs border-b last:border-0 pb-1.5">
                    <Link href={`/companies/${c.id}`} className="font-medium hover:underline">{c.ticker}</Link>
                    <span className="text-[var(--text-muted)]">{c.sector ?? "—"}</span>
                  </div>
                ))}
              </CardBody>
            </Card>
          </div>

          {/* Sectors */}
          <Card>
            <CardHeader title="Sectors" subtitle="Live counts and market cap" />
            <CardBody>
              <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
                {data.sectors.slice(0,8).map((s) => (
                  <div key={s.sector} className="rounded border p-2.5 flex justify-between text-xs">
                    <span>{s.sector}</span>
                    <span className="num text-[var(--text-muted)]">{s.count} • {marketCap(s.market_cap)}</span>
                  </div>
                ))}
              </div>
            </CardBody>
          </Card>

          {/* Provenance */}
          <Card>
            <CardHeader title="Data Provenance" />
            <CardBody>
              <p className="text-xs leading-relaxed text-[var(--text-muted)]">
                54 canonical line items • Precedence: Override → Store → Alias → Absent. No invented numbers. Source tracking via ISIN → BSE code join. Market data via shared TTL cache (15s NSE live / 300s default) with non-blocking bulk_quotes.
              </p>
              <div className="mt-3 text-[10px] flex gap-2">
                <Badge variant="accent">v7 spec</Badge>
                <Badge>100% traceable</Badge>
                <Badge variant="gain">{data.coverage.companies_with_financials} with financials</Badge>
              </div>
            </CardBody>
          </Card>
        </div>
      )}
    </AppShell>
  );
}
