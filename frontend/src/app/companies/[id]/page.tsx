"use client";

import { AppShell } from "@/components/layout/app-shell";
import { CompanyTabs } from "@/components/layout/company-tabs";
import { Badge, Card, CardBody, CardHeader, EmptyState, Skeleton, Stat } from "@/components/ui";
import { api, watchlistApi } from "@/lib/api";
import { crore, fiscalYear, isLivePrice, lastUpdated, marketCap, marketPrice, percent, plainNumber, priceSourceLabel, rupees, signClass } from "@/lib/format";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, CheckCircle2, Database, ExternalLink, Info, Plus } from "lucide-react";
import Link from "next/link";
import { use } from "react";
import { useState } from "react";

export default function CompanyProfilePage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const queryClient = useQueryClient();
  const [showWatchlistPicker, setShowWatchlistPicker] = useState(false);
  const [selectedWatchlistId, setSelectedWatchlistId] = useState<number | null>(null);
  const [addNote, setAddNote] = useState("");

  const { data, isLoading, error } = useQuery({
    queryKey: ["company-profile", id],
    queryFn: () => api.companyProfile(id),
  });

  // Watchlists for "Add to Watchlist"
  const watchlists = useQuery({
    queryKey: ["watchlists"],
    queryFn: () => watchlistApi.list(),
    enabled: !!data,
  });

  const addToWatchlist = useMutation({
    mutationFn: (watchlistId: number) =>
      watchlistApi.add(watchlistId, {
        ticker: data?.company?.ticker || "",
        note: addNote || undefined,
      }),
    onSuccess: () => {
      setShowWatchlistPicker(false);
      setAddNote("");
      setSelectedWatchlistId(null);
      queryClient.invalidateQueries({ queryKey: ["watchlist-rows"] });
      alert(`Added to watchlist`);
    },
    onError: (err: unknown) => alert((err as Error)?.message || "Failed to add"),
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

  // Add to Watchlist button + modal logic (after data is available)
  const handleAddToWatchlistClick = () => {
    setShowWatchlistPicker(true);
    setSelectedWatchlistId(watchlists.data?.[0]?.id ?? null);
  };

  return (
    <AppShell>
      <CompanyTabs companyId={id} />
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
            <div className="min-w-[12rem] text-left sm:text-right">
              <div className="text-xs font-medium text-[var(--text-muted)]">Current Price</div>
              <div className="mt-0.5 flex items-center gap-2 sm:justify-end">
                <div className="num text-2xl font-semibold">{rupees(marketPrice(c))}</div>
                {isLivePrice(c.market?.price_source) && <Badge variant="gain">Live</Badge>}
              </div>
              {c.market && c.market.change !== null && c.market.change !== undefined && (
                <div className={`mt-0.5 text-[0.6875rem] font-medium ${signClass(c.market.change)}`}>
                  {c.market.change >= 0 ? "+" : ""}{rupees(c.market.change)}{" "}
                  ({c.market.change_percent !== null && c.market.change_percent !== undefined && c.market.change_percent >= 0 ? "+" : ""}
                  {percent(c.market.change_percent)})
                </div>
              )}
              <div className="mt-1 text-[0.6875rem] text-[var(--text-muted)]">
                Source: {priceSourceLabel(c.market?.price_source)}
              </div>
              <div className="text-[0.6875rem] text-[var(--text-muted)]">
                Last Updated: {lastUpdated(c.market?.last_updated)}
              </div>
              <div className="mt-1 text-[0.6875rem] text-[var(--text-muted)]">
                Market Cap: {marketCap(c.market_cap)}
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
              <button
                onClick={handleAddToWatchlistClick}
                className="mt-2 inline-flex items-center gap-1.5 rounded bg-accent-500/90 px-2.5 py-1 text-[10px] font-medium text-white hover:bg-accent-500"
              >
                <Plus size={12} /> Add to Watchlist
              </button>
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
              <div className="scroll-x">
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
                <CardHeader title="Company Overview" />
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

      {/* Add to Watchlist modal (appears on every company page) */}
      {showWatchlistPicker && (
        <div className="fixed inset-0 z-[70] flex items-center justify-center bg-black/50 p-4">
          <div className="w-full max-w-sm rounded-xl border border-[var(--border)] bg-[var(--bg-elevated)] p-5">
            <div className="font-semibold mb-3">Add {c.ticker} to Watchlist</div>

            <div className="mb-3">
              <label className="text-xs block mb-1 text-[var(--text-muted)]">Select watchlist</label>
              <select
                value={selectedWatchlistId ?? ""}
                onChange={(e) => setSelectedWatchlistId(Number(e.target.value))}
                className="w-full rounded border px-3 py-1.5 text-sm bg-[var(--bg)]"
              >
                {(watchlists.data ?? []).map((w) => (
                  <option key={w.id} value={w.id}>{w.name}</option>
                ))}
              </select>
            </div>

            <div className="mb-4">
              <label className="text-xs block mb-1 text-[var(--text-muted)]">Note (optional)</label>
              <input
                value={addNote}
                onChange={(e) => setAddNote(e.target.value)}
                placeholder="Thesis note..."
                className="w-full rounded border px-3 py-1.5 text-sm bg-[var(--bg)]"
              />
            </div>

            <div className="flex gap-2 justify-end">
              <button
                onClick={() => {
                  setShowWatchlistPicker(false);
                  setAddNote("");
                }}
                className="px-3 py-1 text-xs rounded border"
              >
                Cancel
              </button>
              <button
                disabled={!selectedWatchlistId || addToWatchlist.isPending}
                onClick={() => selectedWatchlistId && addToWatchlist.mutate(selectedWatchlistId)}
                className="px-3 py-1 text-xs rounded bg-accent-500 text-white disabled:opacity-50"
              >
                {addToWatchlist.isPending ? "Adding..." : "Add"}
              </button>
            </div>
          </div>
        </div>
      )}
    </AppShell>
  );
}
