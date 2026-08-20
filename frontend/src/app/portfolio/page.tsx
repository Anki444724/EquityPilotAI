"use client";

/**
 * Portfolio Intelligence — the main workspace.
 *
 * Six tabs over one resolved view. The backend computes every figure once per
 * request and this page selects from it; nothing here calculates a weight, a
 * return or a risk statistic.
 */

import { AppShell } from "@/components/layout/app-shell";
import {
  AlertList, AllocationPie, AttributionTable, DIMENSION_LABELS, DeltaStat,
  HoldingsTable, HoldingsTreemap, Note, RebalanceTable, RiskGrid,
  SectorHeatmap, UnderwaterChart, ValueChart, money, pct, toneOf,
} from "@/components/portfolio/panels";
import { Badge, Card, CardBody, CardHeader, EmptyState, Skeleton, TabStrip } from "@/components/ui";
import { portfolioApi } from "@/lib/api";
import { cn } from "@/lib/utils";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Activity, Bell, Briefcase, Camera, Layers, Loader2, PieChart, Plus, Sparkles,
} from "lucide-react";
import { useState } from "react";
import { useAuth } from "@/components/layout/auth-provider";
import { ApiError } from "@/lib/api";
import { CreatePortfolioDialog } from "@/components/portfolio/create-portfolio";
import { resolvePortfolioListState } from "@/lib/portfolio-view-state";

const TABS = [
  { key: "overview", label: "Overview", icon: Briefcase },
  { key: "holdings", label: "Holdings", icon: Layers },
  { key: "allocation", label: "Allocation", icon: PieChart },
  { key: "risk", label: "Risk", icon: Activity },
  { key: "alerts", label: "Alerts", icon: Bell },
  { key: "ai", label: "AI Commentary", icon: Sparkles },
] as const;
type TabKey = (typeof TABS)[number]["key"];

export default function PortfolioPage() {
  const queryClient = useQueryClient();
  const { user: authUser, initialising: authInitialising } = useAuth();
  const [tab, setTab] = useState<TabKey>("overview");
  const [selected, setSelected] = useState<number | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  // The query runs only once the session is settled, so it cannot fire without
  // an access token and produce a spurious 401 on reload. The query function
  // does not touch the session: `apiFetch` already refreshes once and retries,
  // and AuthProvider clears the client when that refresh is refused. Clearing
  // it a second time from here raced that and logged the user out on a
  // recoverable 401.
  const portfolios = useQuery({
    queryKey: ["portfolios"],
    queryFn: () => portfolioApi.list(),
    enabled: !authInitialising && !!authUser,
    retry: (failureCount, error) => {
      if (error instanceof ApiError && error.status === 401) return false;
      return failureCount < 1;
    },
  });

  // One value, one meaning each — resolved by a pure function so the rules are
  // testable rather than spread across four booleans in the JSX.
  const listState = resolvePortfolioListState({
    authInitialising,
    isAuthenticated: !!authUser,
    isPending: portfolios.isPending,
    isFetching: portfolios.isFetching,
    data: portfolios.data,
    error: portfolios.error,
  });
  const sessionExpired = listState.kind === "session-expired";
  const listPending = listState.kind === "loading";
  const listFailed = listState.kind === "error";
  const isEmpty = listState.kind === "empty";
  const list = listState.kind === "ready" ? listState.portfolios : [];

  // Derive current selection from state or first portfolio; never setState in effect
  const current = selected ?? (list.length > 0 ? list[0].id : null);

  const view = useQuery({
    queryKey: ["portfolio-view", current],
    queryFn: () => portfolioApi.view(current!),
    enabled: !!authUser && current !== null,
    retry: (failureCount, error) => !(error instanceof ApiError && error.status === 401) && failureCount < 1,
  });

  const alerts = useQuery({
    queryKey: ["portfolio-alerts", current],
    queryFn: () => portfolioApi.alerts(current!),
    enabled: !!authUser && current !== null,
    retry: (failureCount, error) => !(error instanceof ApiError && error.status === 401) && failureCount < 1,
  });

  const attribution = useQuery({
    queryKey: ["portfolio-attribution", current],
    queryFn: () => portfolioApi.attribution(current!),
    enabled: !!authUser && current !== null && tab === "allocation",
    retry: (failureCount, error) => !(error instanceof ApiError && error.status === 401) && failureCount < 1,
  });

  const commentary = useQuery({
    queryKey: ["portfolio-commentary", current],
    queryFn: () => portfolioApi.commentary(current!),
    enabled: !!authUser && current !== null && tab === "ai",
    retry: (failureCount, error) => !(error instanceof ApiError && error.status === 401) && failureCount < 1,
  });

  const snapshot = useMutation({
    mutationFn: () => portfolioApi.snapshot(current!),
    onSuccess: () => {
      setNotice("Snapshot recorded — the return series now has one more point.");
      queryClient.invalidateQueries({ queryKey: ["portfolio-view", current] });
    },
  });

  const data = view.data;
  const summary = data?.summary;
  const triggered = alerts.data?.counts.triggered ?? 0;

  return (
    <AppShell>
      <div className="mx-auto max-w-[1500px] space-y-4 p-4">
        {/* ---------------------------------------------------- header */}
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <h1 className="text-xl font-semibold text-[var(--text)]">
              Portfolio Intelligence
            </h1>
            <p className="text-xs text-[var(--text-muted)]">
              {summary
                ? `${summary.name} · benchmark ${summary.benchmark} · as at ${summary.as_of}`
                : listPending || view.isPending
                  ? "Loading…"
                  : sessionExpired
                    ? "Session expired"
                    : isEmpty
                      ? "No portfolio selected"
                      : "Select a portfolio"}
            </p>
          </div>
          <div className="flex items-center gap-2">
            {list.length > 0 && (
              <select
                value={current ?? ""}
                onChange={(e) => setSelected(Number(e.target.value))}
                className="rounded border border-[var(--border)] bg-[var(--bg-elevated)] px-3 py-1.5 text-xs text-[var(--text)]"
              >
                {list.map((p) => (
                  <option key={p.id} value={p.id}>{p.name}</option>
                ))}
              </select>
            )}
            <button
              type="button"
              onClick={() => setCreating(true)}
              className="inline-flex items-center gap-1.5 rounded border border-[var(--border)] px-3 py-1.5 text-xs font-medium text-[var(--text-muted)] hover:bg-[var(--bg-subtle)]"
            >
              <Plus className="h-3.5 w-3.5" />
              New portfolio
            </button>
            <button
              type="button"
              onClick={() => snapshot.mutate()}
              disabled={snapshot.isPending || current === null}
              className="inline-flex items-center gap-1.5 rounded border border-[var(--border)] px-3 py-1.5 text-xs font-medium text-[var(--text-muted)] hover:bg-[var(--bg-subtle)] disabled:opacity-50"
            >
              {snapshot.isPending
                ? <Loader2 className="h-3.5 w-3.5 animate-spin" />
                : <Camera className="h-3.5 w-3.5" />}
              Snapshot
            </button>
          </div>
        </div>

        {notice && <Note>{notice}</Note>}

        {sessionExpired && (
          <Card>
            <CardBody className="text-sm text-loss">
              Your session has expired. Sign in again to view your portfolios.
            </CardBody>
          </Card>
        )}

        {listFailed && (
          <Card>
            <CardBody className="flex flex-wrap items-center justify-between gap-3 text-sm text-loss">
              <span>{listFailed ? listState.message : ""}</span>
              <button
                type="button"
                onClick={() => portfolios.refetch()}
                className="rounded border border-[var(--border)] px-2.5 py-1 text-xs text-[var(--text-muted)] hover:bg-[var(--bg-subtle)]"
              >
                Retry
              </button>
            </CardBody>
          </Card>
        )}

        {listPending && <Skeleton className="h-28 w-full" />}

        {isEmpty && (
          <Card>
            <EmptyState
              icon={<Briefcase className="h-8 w-8" />}
              title="No portfolios yet"
              description="Create a portfolio, then record transactions to see holdings, allocation, risk and AI commentary."
              action={
                <button
                  type="button"
                  onClick={() => setCreating(true)}
                  className="inline-flex min-h-10 items-center gap-2 rounded-md bg-accent-500 px-3 py-2 text-xs font-medium text-white transition-colors hover:bg-accent-600"
                >
                  <Plus className="h-3.5 w-3.5" />
                  Create Portfolio
                </button>
              }
            />
          </Card>
        )}

        <CreatePortfolioDialog
          open={creating}
          onClose={() => setCreating(false)}
          onCreated={(id) => { setSelected(id); setNotice("Portfolio created."); }}
        />

        {/* ----------------------------------------------------- stats */}
        {summary && (
          <Card>
            <CardBody className="grid grid-cols-2 gap-4 sm:grid-cols-4 lg:grid-cols-7">
              <DeltaStat label="Total value" value={money(summary.total_value)}
                         hint={`${summary.position_count} positions`} />
              <DeltaStat label="Market value" value={money(summary.market_value)}
                         hint={`cost ${money(summary.cost_basis)}`} />
              <DeltaStat label="Unrealised" value={money(summary.unrealised_pnl)}
                         delta={summary.unrealised_pnl}
                         hint={pct(summary.total_return)} />
              <DeltaStat label="Realised" value={money(summary.realised_pnl)}
                         delta={summary.realised_pnl} />
              <DeltaStat label="Dividends" value={money(summary.dividends)} />
              <DeltaStat label="Cash" value={money(summary.cash)}
                         hint={pct(summary.cash_weight)} />
              <DeltaStat label="Total P&L" value={money(summary.total_pnl)}
                         delta={summary.total_pnl} />
            </CardBody>
          </Card>
        )}

        {summary && summary.unpriced.length > 0 && (
          <Note tone="warning">
            No current price for {summary.unpriced.join(", ")}. Their value is
            excluded from every figure above, so the portfolio is larger than it
            appears.
          </Note>
        )}
        {summary && Object.keys(summary.analytics_errors).length > 0 && (
          <Note tone="warning">
            Platform analytics failed for{" "}
            {Object.keys(summary.analytics_errors).join(", ")}. Alerts for those
            holdings will report as not evaluated rather than clear.
          </Note>
        )}

        {/* ------------------------------------------------------ tabs */}
        <TabStrip label="Portfolio views">
          {TABS.map(({ key, label, icon: Icon }) => (
            <button data-active={tab === key} role="tab" aria-selected={tab === key}
              key={key} type="button" onClick={() => setTab(key)}
              className={cn(
                "inline-flex items-center gap-1.5 border-b-2 px-3 py-2 text-xs font-medium transition",
                tab === key
                  ? "border-accent-500 text-[var(--text)]"
                  : "border-transparent text-[var(--text-muted)] hover:text-[var(--text)]",
              )}
            >
              <Icon className="h-3.5 w-3.5" />
              {label}
              {key === "alerts" && triggered > 0 && (
                <Badge variant="loss">{triggered}</Badge>
              )}
            </button>
          ))}
        </TabStrip>

        {view.isLoading && <Skeleton className="h-64 w-full" />}

        {/* -------------------------------------------------- overview */}
        {tab === "overview" && data && (
          <div className="space-y-4">
            <Card>
              <CardHeader
                title="Holdings treemap"
                subtitle="Area is weight, colour is return on cost"
              />
              <CardBody><HoldingsTreemap holdings={data.holdings} /></CardBody>
            </Card>

            <div className="grid min-w-0-all gap-4 lg:grid-cols-[1fr_360px]">
              <Card>
                <CardHeader
                  title="Portfolio value"
                  subtitle={
                    data.performance.twr !== null
                      ? `TWR ${pct(data.performance.twr)}`
                        + (data.performance.twr_annualised !== null
                          ? ` · ${pct(data.performance.twr_annualised)} annualised` : "")
                        + (data.performance.mwr !== null
                          ? ` · MWR ${pct(data.performance.mwr)}` : "")
                      : "Awaiting valuation snapshots"
                  }
                />
                <CardBody>
                  <ValueChart series={data.performance.series} />
                  {data.performance.underwater.length > 1 && (
                    <>
                      <div className="mt-3 text-xs font-semibold uppercase tracking-wide text-[var(--text-muted)]">
                        Drawdown from peak
                      </div>
                      <UnderwaterChart points={data.performance.underwater} />
                    </>
                  )}
                </CardBody>
              </Card>

              <div className="space-y-4">
                <Card>
                  <CardHeader title="Return measures" />
                  <CardBody className="space-y-2 text-sm">
                    {[
                      ["Time-weighted", data.performance.twr, "measures the manager"],
                      ["Annualised TWR", data.performance.twr_annualised, null],
                      ["Money-weighted", data.performance.mwr, "measures the investor"],
                      ["Benchmark", data.performance.benchmark_return, null],
                      ["Active", data.performance.active_return, null],
                    ].map(([label, value, hint]) => (
                      <div key={label as string} className="flex items-baseline justify-between">
                        <div>
                          <span className="text-[var(--text)]">{label as string}</span>
                          {hint && (
                            <span className="ml-1.5 text-[10px] text-[var(--text-muted)]">
                              {hint as string}
                            </span>
                          )}
                        </div>
                        <span className={cn("num font-medium", toneOf(value as number | null))}>
                          {pct(value as number | null)}
                        </span>
                      </div>
                    ))}
                  </CardBody>
                </Card>

                <Card>
                  <CardHeader title="Top contributors"
                              subtitle="Weight × return — what actually moved the book" />
                  <CardBody className="space-y-1.5">
                    {data.performance.contributions.slice(0, 6).map((c) => (
                      <div key={c.ticker} className="flex items-baseline justify-between text-xs">
                        <span className="text-[var(--text)]">{c.ticker}</span>
                        <span className="text-[var(--text-muted)]">{pct(c.weight)}</span>
                        <span className={cn("num font-medium", toneOf(c.contribution))}>
                          {pct(c.contribution, 2)}
                        </span>
                      </div>
                    ))}
                  </CardBody>
                </Card>
              </div>
            </div>

            <Card>
              <CardHeader
                title="Rebalancing"
                subtitle="Mechanical consequences of the weight policy — not a view on the businesses"
              />
              <CardBody className="p-0"><RebalanceTable trades={data.rebalance} /></CardBody>
            </Card>
          </div>
        )}

        {/* -------------------------------------------------- holdings */}
        {tab === "holdings" && data && (
          <div className="space-y-4">
            <Card>
              <CardHeader title="Open positions"
                          subtitle={`${data.holdings.length} holdings, derived from the transaction ledger`} />
              <CardBody className="p-0"><HoldingsTable holdings={data.holdings} /></CardBody>
            </Card>
            {data.realised.length > 0 && (
              <Card>
                <CardHeader title="Realised trades"
                            subtitle={`${data.realised.length} closed round-trips`} />
                <CardBody className="scroll-x p-0">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="border-b border-[var(--border)] text-left text-xs uppercase tracking-wide text-[var(--text-muted)]">
                        <th className="px-3 py-2 font-medium">Holding</th>
                        <th className="px-3 py-2 font-medium">Bought</th>
                        <th className="px-3 py-2 font-medium">Sold</th>
                        <th className="px-3 py-2 text-right font-medium">Qty</th>
                        <th className="px-3 py-2 text-right font-medium">P&amp;L</th>
                        <th className="px-3 py-2 text-right font-medium">Return</th>
                        <th className="px-3 py-2 text-center font-medium">Term</th>
                      </tr>
                    </thead>
                    <tbody>
                      {data.realised.map((t, i) => (
                        <tr key={i} className="border-b border-[var(--border)]">
                          <td className="px-3 py-2 font-medium text-[var(--text)]">{t.ticker}</td>
                          <td className="px-3 py-2 text-xs text-[var(--text-muted)]">{t.buy_date}</td>
                          <td className="px-3 py-2 text-xs text-[var(--text-muted)]">{t.sell_date}</td>
                          <td className="num px-3 py-2 text-right">{t.quantity}</td>
                          <td className={cn("num px-3 py-2 text-right", toneOf(t.pnl))}>
                            {money(t.pnl)}
                          </td>
                          <td className={cn("num px-3 py-2 text-right", toneOf(t.return_pct))}>
                            {pct(t.return_pct)}
                          </td>
                          <td className="px-3 py-2 text-center">
                            <Badge variant={t.is_long_term ? "gain" : "neutral"}>
                              {t.is_long_term ? "long" : "short"}
                            </Badge>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </CardBody>
              </Card>
            )}
            <Card>
              <CardHeader title="Cash ledger" />
              <CardBody className="grid grid-cols-2 gap-3 sm:grid-cols-5">
                {([
                  ["Balance", data.cash.balance], ["Deposits", data.cash.deposits],
                  ["Withdrawals", data.cash.withdrawals], ["Buys", data.cash.buys],
                  ["Sells", data.cash.sells], ["Dividends", data.cash.dividends],
                  ["Fees", data.cash.fees], ["Taxes", data.cash.taxes],
                  ["Net invested", data.cash.net_invested],
                ] as [string, number][]).map(([label, value]) => (
                  <div key={label}>
                    <div className="text-[0.6875rem] uppercase tracking-wider text-[var(--text-muted)]">
                      {label}
                    </div>
                    <div className="num text-sm font-semibold text-[var(--text)]">
                      {money(value)}
                    </div>
                  </div>
                ))}
              </CardBody>
            </Card>
          </div>
        )}

        {/* ------------------------------------------------ allocation */}
        {tab === "allocation" && data && (
          <div className="space-y-4">
            <Card>
              <CardHeader title="Sector heatmap"
                          subtitle="Size is weight, colour is unrealised P&L" />
              <CardBody>
                {data.allocations.sector && <SectorHeatmap allocation={data.allocations.sector} />}
              </CardBody>
            </Card>
            <div className="grid gap-4 lg:grid-cols-2">
              {Object.entries(data.allocations).map(([key, allocation]) => (
                <Card key={key}>
                  <CardHeader
                    title={DIMENSION_LABELS[key] ?? key}
                    subtitle={`${allocation.slices.length} buckets · ${allocation.effective_count.toFixed(1)} effective`}
                    action={
                      allocation.unclassified_value > 0
                        ? <Badge variant="warn">
                            {money(allocation.unclassified_value, true)} unclassified
                          </Badge>
                        : undefined
                    }
                  />
                  <CardBody><AllocationPie allocation={allocation} /></CardBody>
                </Card>
              ))}
            </div>
            <Card>
              <CardHeader
                title="Performance attribution"
                subtitle="Brinson-Fachler against an equal-weighted sector benchmark"
              />
              <CardBody className="p-0">
                {attribution.data
                  ? <AttributionTable rows={attribution.data.rows} />
                  : <Skeleton className="m-4 h-32" />}
              </CardBody>
            </Card>
            <Note>
              With no external index constituent data, the benchmark is modelled
              as an equal weight across the sectors this portfolio holds. That
              makes <strong>allocation</strong> the meaningful term; selection is
              near zero by construction, and is shown rather than hidden.
            </Note>
          </div>
        )}

        {/* ------------------------------------------------------ risk */}
        {tab === "risk" && data && (
          <div className="space-y-4">
            <Card>
              <CardHeader
                title="Risk dashboard"
                subtitle={`${data.risk.observations} return observations`}
              />
              <CardBody><RiskGrid risk={data.risk} /></CardBody>
            </Card>
            {data.performance.rolling.length > 1 && (
              <Card>
                <CardHeader title="Rolling returns"
                            subtitle="Consistency, not a single trailing number" />
                <CardBody>
                  <ValueChart
                    format="ratio"
                    series={data.performance.rolling.map((r) => ({
                      as_of: r.as_of, value: 1 + r.value, net_flow: 0,
                    }))}
                  />
                </CardBody>
              </Card>
            )}
            <Card>
              <CardHeader title="Concentration" />
              <CardBody className="grid grid-cols-2 gap-4 sm:grid-cols-4">
                <DeltaStat label="Diversification"
                           value={data.risk.diversification_score?.toFixed(0) ?? "—"}
                           hint="0–100" />
                <DeltaStat label="Effective positions"
                           value={data.risk.effective_positions?.toFixed(1) ?? "—"}
                           hint={`of ${data.summary.position_count} nominal`} />
                <DeltaStat label="Largest position"
                           value={pct(data.risk.largest_position_weight)} />
                <DeltaStat label="Illiquid positions"
                           value={String(data.risk.illiquid_positions)}
                           hint="> 30 days to exit" />
              </CardBody>
            </Card>
          </div>
        )}

        {/* ---------------------------------------------------- alerts */}
        {tab === "alerts" && (
          <div className="space-y-4">
            <Card>
              <CardHeader
                title="Alert rules"
                subtitle={
                  alerts.data
                    ? `${alerts.data.counts.triggered} triggered · `
                      + `${alerts.data.counts.clear} clear · `
                      + `${alerts.data.counts.unavailable} not evaluated`
                    : "Evaluating…"
                }
              />
              <CardBody>
                {alerts.data
                  ? <AlertList alerts={alerts.data.evaluations} />
                  : <Skeleton className="h-32 w-full" />}
              </CardBody>
            </Card>
            <Note>
              A rule whose input is missing reports as <strong>not evaluated</strong>,
              never as clear. The workbook treats a blank cell as zero and shows
              a tick; silence about a risk is not evidence of its absence.
            </Note>
            {alerts.data && (
              <Card>
                <CardHeader title="Not evaluated"
                            subtitle="Rules that could not run, and why" />
                <CardBody>
                  <AlertList
                    alerts={alerts.data.evaluations.filter(
                      (a) => a.status === "unavailable",
                    ).slice(0, 12)}
                    showClear
                  />
                </CardBody>
              </Card>
            )}
          </div>
        )}

        {/* -------------------------------------------------------- ai */}
        {tab === "ai" && (
          <div className="grid min-w-0-all gap-4 lg:grid-cols-[1fr_320px]">
            <div className="space-y-4">
              {commentary.isLoading && <Skeleton className="h-64 w-full" />}
              {commentary.data?.sections.map((section) => (
                <Card key={section.key}>
                  <CardHeader title={section.title} />
                  <CardBody>
                    <div className="space-y-2 text-sm leading-relaxed text-[var(--text)]">
                      {/* Split on single newlines too: the rebalancing section
                          emits a "- " bullet per line, and joining them into
                          one paragraph produced an unreadable run-on. */}
                      {section.body.split("\n").filter((l) => l.trim()).map((line, i) =>
                        line.trimStart().startsWith("- ") ? (
                          <p key={i} className="flex gap-2 pl-1">
                            <span className="text-[var(--text-muted)]">·</span>
                            <span dangerouslySetInnerHTML={{
                              __html: markup(line.trimStart().slice(2)),
                            }} />
                          </p>
                        ) : (
                          <p key={i} dangerouslySetInnerHTML={{ __html: markup(line) }} />
                        ),
                      )}
                    </div>
                  </CardBody>
                </Card>
              ))}
            </div>
            <div className="space-y-3">
              <Card>
                <CardHeader title="Evidence"
                            subtitle="Every figure the commentary may use" />
                <CardBody className="max-h-[520px] space-y-1 overflow-y-auto">
                  {(commentary.data?.citations ?? []).map((c) => (
                    <div key={c.key} className="flex items-baseline justify-between gap-2 text-xs">
                      <span className="font-mono text-accent-500">{c.key}</span>
                      <span className="truncate text-[var(--text-muted)]">{c.label}</span>
                      <span className="num shrink-0 text-[var(--text)]">
                        {typeof c.value === "number"
                          ? c.unit === "%" ? pct(c.value)
                            : c.unit === "₹" ? money(c.value, true)
                            : c.value.toFixed(2)
                          : String(c.value ?? "—")}
                      </span>
                    </div>
                  ))}
                </CardBody>
              </Card>
              {commentary.data && (
                <Note>{commentary.data.disclosure}</Note>
              )}
            </div>
          </div>
        )}
      </div>
    </AppShell>
  );
}

/** Minimal markdown: bold and citation chips. No user input reaches this. */
function markup(text: string): string {
  return text
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(
      /\[([a-z0-9_]+)\]/g,
      '<span class="ml-0.5 rounded bg-accent-500/10 px-1 font-mono text-[10px] text-accent-500">$1</span>',
    );
}
