"use client";

import { AppShell } from "@/components/layout/app-shell";
import { Badge, Card, CardBody, CardHeader, Skeleton, Stat } from "@/components/ui";
import { ApiError, api } from "@/lib/api";
import { isLivePrice, marketCap, marketPrice, plainNumber, priceSourceLabel, rupees } from "@/lib/format";
import { useQuery } from "@tanstack/react-query";
import { AlertCircle } from "lucide-react";
import Link from "next/link";

export default function DashboardPage() {
  const { data, isLoading, error } = useQuery({
    queryKey: ["dashboard"],
    queryFn: api.dashboard,
  });

  return (
    <AppShell>
      <div className="mb-6">
        <h1 className="text-2xl font-semibold tracking-tight">Dashboard</h1>
        <p className="mt-1 text-sm text-[var(--text-muted)]">
          A simple overview of the companies covered by EquityPilotAI.
        </p>
      </div>

      {error && (
        <Card className="mb-6 border-loss/40">
          <CardBody className="flex items-center gap-2 text-sm text-loss">
            <AlertCircle size={16} />
            {error instanceof ApiError
              ? error.status === 401 || error.status === 403
                ? "Your session has expired. Sign in again to continue."
                : `The API returned HTTP ${error.status}: ${error.message}`
              : "Cannot reach the API. Check your connection and try again."}
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
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <Card><CardBody><Stat label="Companies" value={plainNumber(data.coverage.companies)} hint="covered" /></CardBody></Card>
            <Card><CardBody><Stat label="With Financials" value={plainNumber(data.coverage.companies_with_financials)} /></CardBody></Card>
            <Card><CardBody><Stat label="Sectors" value={plainNumber(data.coverage.sectors)} /></CardBody></Card>
            <Card><CardBody><Stat label="Financial Facts" value={plainNumber(data.coverage.fact_rows)} /></CardBody></Card>
          </div>

          <div className="grid gap-5 lg:grid-cols-3">
            <Card className="lg:col-span-2">
              <CardHeader
                title="Companies at a Glance"
                subtitle="Largest companies by market value"
                action={<Link href="/companies" className="text-xs text-accent-500">View all companies →</Link>}
              />
              <div className="scroll-x">
                <table className="grid-table">
                  <thead>
                    <tr><th>Company</th><th>Sector</th><th>Current Price</th><th>Market Cap</th></tr>
                  </thead>
                  <tbody>
                    {data.largest.map((company) => (
                      <tr key={company.id}>
                        <td className="sticky-col !text-left">
                          <Link href={`/companies/${company.id}`} className="font-medium hover:text-accent-500">
                            {company.name}
                          </Link>
                          <div className="num text-[0.6875rem] text-[var(--text-muted)]">{company.ticker}</div>
                        </td>
                        <td className="!text-left text-xs">{company.sector ?? "—"}</td>
                        <td className="num">
                          <div>{rupees(marketPrice(company))}</div>
                          <div className="mt-0.5 text-[0.625rem] font-normal text-[var(--text-muted)]">
                            {isLivePrice(company.market?.price_source) ? "Live · " : ""}
                            {priceSourceLabel(company.market?.price_source)}
                          </div>
                        </td>
                        <td className="num font-medium">{marketCap(company.market_cap)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Card>

            <Card>
              <CardHeader title="Explore by Sector" />
              <CardBody className="space-y-2">
                {data.sectors.slice(0, 10).map((sector) => (
                  <div key={sector.sector} className="flex items-center justify-between gap-3 text-sm">
                    <span>{sector.sector}</span>
                    <Badge>{sector.count} companies</Badge>
                  </div>
                ))}
                <Link href="/companies" className="mt-3 inline-block text-xs text-accent-500 hover:underline">
                  Browse sectors →
                </Link>
              </CardBody>
            </Card>
          </div>

          <Card>
            <CardHeader title="Recently Added" subtitle="Select a company for its overview, financials, charts and investment view." />
            <CardBody>
              <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
                {data.recently_added.slice(0, 8).map((company) => (
                  <Link
                    key={company.id}
                    href={`/companies/${company.id}`}
                    className="rounded-lg border border-[var(--border)] p-3 transition-colors hover:border-accent-500/50"
                  >
                    <div className="font-medium">{company.name}</div>
                    <div className="mt-1 text-xs text-[var(--text-muted)]">{company.ticker} · {company.sector ?? "Sector not listed"}</div>
                  </Link>
                ))}
              </div>
            </CardBody>
          </Card>
        </div>
      )}
    </AppShell>
  );
}
