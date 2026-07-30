"use client";

import { AppShell } from "@/components/layout/app-shell";
import { Badge, Card, CardBody, CardHeader, EmptyState, Skeleton, Stat } from "@/components/ui";
import { api } from "@/lib/api";
import { crore, fiscalYear, marketCap, percent, plainNumber, rupees, signClass } from "@/lib/format";
import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, CheckCircle2, Database, ExternalLink, Info } from "lucide-react";
import Link from "next/link";
import { use } from "react";

export default function CompanyProfilePage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const { data, isLoading, error } = useQuery({
    queryKey: ["company-profile", id],
    queryFn: () => api.companyProfile(id),
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
          <EmptyState
            icon={<AlertTriangle size={28} />}
            title="Company not found"
            description="This company is not in the coverage universe."
            action={
              <Link href="/companies" className="text-xs text-accent-500 hover:underline">
                Back to companies
              </Link>
            }
          />
        </Card>
      </AppShell>
    );
  }

  const { company: c, coverage } = data;
  const hasData = coverage.has_data;

  return (
    <AppShell>
      {/* Header */}
      <Card className="mb-5">
        <CardBody>
          <div className="flex flex-wrap items-start justify-between gap-4">
            <div className="min-w-0">
              <div className="flex flex-wrap items-center gap-2">
                <span className="num rounded bg-accent-500 px-2 py-0.5 text-xs font-bold text-white">
                  {c.ticker}
                </span>
                <Badge>{c.exchange}</Badge>
                {c.sector && <Badge variant="accent">{c.sector}</Badge>}
                {hasData ? (
                  <Badge variant="gain"><CheckCircle2 size={10} /> {coverage.items_populated} facts</Badge>
                ) : (
                  <Badge variant="warn"><AlertTriangle size={10} /> No financials</Badge>
                )}
              </div>
              <h1 className="mt-2 text-xl font-semibold">{c.name}</h1>
              <p className="mt-0.5 text-xs text-[var(--text-muted)]">
                {c.industry ?? "—"}
                {c.isin && <> · ISIN {c.isin}</>}
                {c.incorporated_year && <> · Est. {c.incorporated_year}</>}
              </p>
            </div>
            <div className="text-right">
              <div className="num text-2xl font-semibold">{rupees(c.current_price)}</div>
              <div className="mt-0.5 text-[0.6875rem] uppercase tracking-wider text-[var(--text-muted)]">
                {marketCap(c.market_cap)} market cap
              </div>
              {c.website && (
                <a
                  href={c.website}
                  target="_blank"
                  rel="noreferrer"
                  className="mt-1.5 inline-flex items-center gap-1 text-[0.6875rem] text-accent-500 hover:underline"
                >
                  Website <ExternalLink size={9} />
                </a>
              )}
            </div>
          </div>
        </CardBody>
      </Card>

      {!hasData ? (
        <Card>
          <EmptyState
            icon={<Database size={28} />}
            title="No financial data available"
            description="This company has no canonical facts loaded. Figures are never fabricated — import statements to populate the model."
          />
        </Card>
      ) : (
        <>
          {/* Headline metrics */}
          <div className="mb-5 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <Card><CardBody>
              <Stat
                label={`Revenue ${data.latest_fiscal_year ? fiscalYear(data.latest_fiscal_year) : ""}`}
                value={crore(data.revenue)}
                hint="₹ crore"
              />
            </CardBody></Card>
            <Card><CardBody>
              <Stat label="EBITDA" value={crore(data.ebitda)} hint={`${percent(data.ebitda_margin)} margin`} />
            </CardBody></Card>
            <Card><CardBody>
              <Stat label="PAT" value={crore(data.pat)} hint={`${percent(data.pat_margin)} margin`} />
            </CardBody></Card>
            <Card><CardBody>
              <Stat label="EPS (basic)" value={data.eps === null ? "—" : `₹${data.eps.toFixed(2)}`} hint="per share" />
            </CardBody></Card>
          </div>

          <div className="grid gap-5 lg:grid-cols-3">
            {/* Financial position */}
            <Card className="lg:col-span-2">
              <CardHeader
                title="Financial position"
                subtitle={
                  data.latest_fiscal_year
                    ? `Latest reported — ${fiscalYear(data.latest_fiscal_year)}`
                    : undefined
                }
                action={
                  data.balance_sheet_ties ? (
                    <Badge variant="gain"><CheckCircle2 size={10} /> Balance sheet ties</Badge>
                  ) : (
                    <Badge variant="loss"><AlertTriangle size={10} /> Out of balance</Badge>
                  )
                }
              />
              <div className="overflow-x-auto">
                <table className="grid-table">
                  <thead>
                    <tr><th>Metric</th><th>Value</th><th>Unit</th></tr>
                  </thead>
                  <tbody>
                    <tr className="is-subtotal">
                      <td className="sticky-col">Total revenue</td>
                      <td className="num">{crore(data.revenue)}</td>
                      <td className="text-xs text-[var(--text-muted)]">₹ cr</td>
                    </tr>
                    <tr>
                      <td className="sticky-col">EBITDA</td>
                      <td className="num">{crore(data.ebitda)}</td>
                      <td className="text-xs text-[var(--text-muted)]">₹ cr</td>
                    </tr>
                    <tr>
                      <td className="sticky-col">EBITDA margin</td>
                      <td className="num">{percent(data.ebitda_margin)}</td>
                      <td className="text-xs text-[var(--text-muted)]">%</td>
                    </tr>
                    <tr className="is-subtotal">
                      <td className="sticky-col">Profit after tax</td>
                      <td className="num">{crore(data.pat)}</td>
                      <td className="text-xs text-[var(--text-muted)]">₹ cr</td>
                    </tr>
                    <tr>
                      <td className="sticky-col">PAT margin</td>
                      <td className="num">{percent(data.pat_margin)}</td>
                      <td className="text-xs text-[var(--text-muted)]">%</td>
                    </tr>
                    <tr>
                      <td className="sticky-col">Total assets</td>
                      <td className="num">{crore(data.total_assets)}</td>
                      <td className="text-xs text-[var(--text-muted)]">₹ cr</td>
                    </tr>
                    <tr>
                      <td className="sticky-col">Net debt</td>
                      <td className={`num ${signClass(data.net_debt === null ? null : -data.net_debt)}`}>
                        {crore(data.net_debt)}
                      </td>
                      <td className="text-xs text-[var(--text-muted)]">₹ cr</td>
                    </tr>
                    <tr>
                      <td className="sticky-col">Shares outstanding</td>
                      <td className="num">{plainNumber(c.shares_outstanding, 1)}</td>
                      <td className="text-xs text-[var(--text-muted)]">crore</td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </Card>

            {/* Coverage */}
            <div className="space-y-5">
              <Card>
                <CardHeader title="Data coverage" />
                <CardBody className="space-y-3">
                  <div>
                    <div className="flex items-baseline justify-between">
                      <span className="text-xs text-[var(--text-muted)]">Canonical grid</span>
                      <span className="num text-xs font-medium">{percent(coverage.coverage, 0)}</span>
                    </div>
                    <div className="mt-1.5 h-2 overflow-hidden rounded-full bg-[var(--bg-subtle)]">
                      <div
                        className="h-full rounded-full bg-gain"
                        style={{ width: `${coverage.coverage * 100}%` }}
                      />
                    </div>
                    <p className="mt-1.5 text-[0.6875rem] text-[var(--text-muted)]">
                      {coverage.items_populated} of {coverage.items_total * coverage.fiscal_years.length} cells
                      populated ({coverage.items_total} items × {coverage.fiscal_years.length} years)
                    </p>
                  </div>
                  <div className="border-t border-[var(--border)] pt-3">
                    <div className="text-[0.6875rem] uppercase tracking-wider text-[var(--text-muted)]">
                      Fiscal years
                    </div>
                    <div className="mt-1.5 flex flex-wrap gap-1">
                      {coverage.fiscal_years.map((y) => (
                        <span key={y} className="num rounded bg-[var(--bg-subtle)] px-1.5 py-0.5 text-[0.625rem]">
                          {fiscalYear(y)}
                        </span>
                      ))}
                    </div>
                  </div>
                </CardBody>
              </Card>

              <Card>
                <CardHeader title="About" />
                <CardBody>
                  <p className="text-xs leading-relaxed text-[var(--text-muted)]">
                    {c.description ?? "No description available."}
                  </p>
                </CardBody>
              </Card>

              <Card className="border-accent-500/25">
                <CardBody className="flex gap-2.5">
                  <Info size={14} className="mt-px shrink-0 text-accent-500" />
                  <p className="text-[0.6875rem] leading-relaxed text-[var(--text-muted)]">
                    Full statements, ratios, forecasts, DCF valuation and institutional scoring
                    arrive in Modules 2–5. These headline figures are computed by the same
                    engine that will drive them.
                  </p>
                </CardBody>
              </Card>
            </div>
          </div>
        </>
      )}
    </AppShell>
  );
}
