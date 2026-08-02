"use client";

import { Badge, Card, CardBody, CardHeader, Skeleton, Stat } from "@/components/ui";
import { AppShell } from "@/components/layout/app-shell";
import { ApiError, api } from "@/lib/api";
import { marketCap, percent, plainNumber, rupees } from "@/lib/format";
import { useQuery } from "@tanstack/react-query";
import { AlertCircle, Database } from "lucide-react";
import Link from "next/link";

export default function DashboardPage() {
  const { data, isLoading, error } = useQuery({
    queryKey: ["dashboard"],
    queryFn: api.dashboard,
  });

  return (
    <AppShell>
      <div className="mb-5">
        <h1 className="text-lg font-semibold">Dashboard</h1>
        <p className="mt-0.5 text-xs text-[var(--text-muted)]">
          Coverage overview across the research universe
        </p>
      </div>

      {error && (
        <Card className="border-loss/40">
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
          {Array.from({ length: 4 }).map((_, i) => <Skeleton key={i} className="h-24" />)}
        </div>
      )}

      {data && (
        <div className="space-y-5">
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <Card><CardBody><Stat label="Companies" value={plainNumber(data.coverage.companies)} hint="in the universe" /></CardBody></Card>
            <Card><CardBody><Stat label="With Financials" value={plainNumber(data.coverage.companies_with_financials)} hint={`${data.coverage.fiscal_years.length} fiscal years`} /></CardBody></Card>
            <Card><CardBody><Stat label="Sectors" value={plainNumber(data.coverage.sectors)} hint="distinct classifications" /></CardBody></Card>
            <Card><CardBody><Stat label="Data Points" value={plainNumber(data.coverage.fact_rows)} hint="canonical facts stored" /></CardBody></Card>
          </div>

          <div className="grid gap-5 lg:grid-cols-3">
            <Card className="lg:col-span-2">
              <CardHeader
                title="Largest by market capitalisation"
                action={<Link href="/companies" className="text-xs text-accent-500 hover:underline">View all</Link>}
              />
              <div className="scroll-x">
                <table className="grid-table">
                  <thead>
                    <tr>
                      <th>Company</th><th>Sector</th><th>Price</th><th>Market cap</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.largest.map((c) => (
                      <tr key={c.id} className="cursor-pointer">
                        <td className="sticky-col">
                          <Link href={`/companies/${c.id}`} className="flex items-center gap-2">
                            <span className="num rounded bg-[var(--bg-subtle)] px-1.5 py-0.5 text-[0.6875rem] font-semibold">
                              {c.ticker}
                            </span>
                            <span className="truncate text-[0.8125rem] hover:text-accent-500">{c.name}</span>
                          </Link>
                        </td>
                        <td className="text-left text-xs text-[var(--text-muted)]">{c.sector ?? "—"}</td>
                        <td className="num">{rupees(c.current_price)}</td>
                        <td className="num font-medium">{marketCap(c.market_cap)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Card>

            <Card>
              <CardHeader title="Sector breakdown" subtitle={`${data.sectors.length} sectors covered`} />
              <CardBody className="space-y-2.5">
                {data.sectors.slice(0, 9).map((s) => {
                  const share = s.count / data.coverage.companies;
                  return (
                    <div key={s.sector}>
                      <div className="flex items-baseline justify-between gap-2">
                        <span className="truncate text-xs">{s.sector}</span>
                        <span className="num shrink-0 text-[0.6875rem] text-[var(--text-muted)]">
                          {s.count} · {percent(share, 0)}
                        </span>
                      </div>
                      <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-[var(--bg-subtle)]">
                        <div className="h-full rounded-full bg-accent-500" style={{ width: `${share * 100}%` }} />
                      </div>
                    </div>
                  );
                })}
              </CardBody>
            </Card>
          </div>

          <Card>
            <CardHeader
              title="Data provenance"
              action={<Badge variant="accent"><Database size={10} /> v7 specification</Badge>}
            />
            <CardBody>
              <p className="text-xs leading-relaxed text-[var(--text-muted)]">
                Financials are normalised into the <strong className="text-[var(--text)]">54 canonical line
                items</strong> defined by <code className="rounded bg-[var(--bg-subtle)] px-1">0C Data Map</code>,
                resolved through the workbook&apos;s four-tier precedence chain: analyst override → company
                store → alias match → absent. Missing data is shown as an em dash and is never
                substituted with a sample figure.
              </p>
            </CardBody>
          </Card>
        </div>
      )}
    </AppShell>
  );
}
