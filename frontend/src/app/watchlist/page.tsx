"use client";

/**
 * Watchlist — names under coverage but not yet owned.
 *
 * The buy-below price is the point of the screen. Where the user has not set
 * one, the backend derives it from intrinsic value discounted by the
 * portfolio's margin of safety, so a row added with only a ticker is still
 * actionable rather than inert.
 */

import { AppShell } from "@/components/layout/app-shell";
import { Note, WatchlistTable } from "@/components/portfolio/panels";
import {
  Badge, Card, CardBody, CardHeader, EmptyState, Skeleton,
} from "@/components/ui";
import { watchlistApi } from "@/lib/api";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Eye, Loader2, Plus } from "lucide-react";
import { useEffect, useState } from "react";

export default function WatchlistPage() {
  const queryClient = useQueryClient();
  const [selected, setSelected] = useState<number | null>(null);
  const [ticker, setTicker] = useState("");
  const [buyBelow, setBuyBelow] = useState("");
  const [error, setError] = useState<string | null>(null);

  const lists = useQuery({
    queryKey: ["watchlists"], queryFn: () => watchlistApi.list(),
  });

  useEffect(() => {
    if (selected === null && lists.data?.length) setSelected(lists.data[0].id);
  }, [lists.data, selected]);

  const rows = useQuery({
    queryKey: ["watchlist-rows", selected],
    queryFn: () => watchlistApi.rows(selected!),
    enabled: selected !== null,
  });

  const add = useMutation({
    mutationFn: () =>
      watchlistApi.add(selected!, {
        ticker: ticker.trim().toUpperCase(),
        ...(buyBelow ? { buy_below: Number(buyBelow) } : {}),
      }),
    onSuccess: () => {
      setTicker(""); setBuyBelow(""); setError(null);
      queryClient.invalidateQueries({ queryKey: ["watchlist-rows", selected] });
    },
    onError: (err: Error) => setError(err.message),
  });

  const remove = useMutation({
    mutationFn: (entryId: number) => watchlistApi.remove(selected!, entryId),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["watchlist-rows", selected] }),
  });

  const triggered = (rows.data ?? []).filter((r) => r.status === "triggered");

  return (
    <AppShell>
      <div className="mx-auto max-w-[1200px] space-y-4 p-4">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <h1 className="text-xl font-semibold text-[var(--text)]">Watchlist</h1>
            <p className="text-xs text-[var(--text-muted)]">
              Names under coverage but not yet owned
            </p>
          </div>
          {(lists.data?.length ?? 0) > 0 && (
            <select
              value={selected ?? ""}
              onChange={(e) => setSelected(Number(e.target.value))}
              className="rounded border border-[var(--border)] bg-[var(--bg-elevated)] px-3 py-1.5 text-xs text-[var(--text)]"
            >
              {(lists.data ?? []).map((w) => (
                <option key={w.id} value={w.id}>{w.name}</option>
              ))}
            </select>
          )}
        </div>

        {(rows.data?.length ?? 0) > 0 && (
          <Card>
            <CardBody className="grid grid-cols-2 gap-4 sm:grid-cols-4">
              <div>
                <div className="text-[0.6875rem] uppercase tracking-wider text-[var(--text-muted)]">
                  Names watched
                </div>
                <div className="num text-xl font-semibold text-[var(--text)]">
                  {rows.data?.length ?? 0}
                </div>
              </div>
              <div>
                <div className="text-[0.6875rem] uppercase tracking-wider text-[var(--text-muted)]">
                  At or below buy price
                </div>
                <div className="num text-xl font-semibold text-gain">
                  {triggered.length}
                </div>
              </div>
              <div>
                <div className="text-[0.6875rem] uppercase tracking-wider text-[var(--text-muted)]">
                  Approaching
                </div>
                <div className="num text-xl font-semibold text-warn">
                  {(rows.data ?? []).filter((r) => r.status === "approaching").length}
                </div>
              </div>
              <div>
                <div className="text-[0.6875rem] uppercase tracking-wider text-[var(--text-muted)]">
                  With a usable valuation
                </div>
                <div className="num text-xl font-semibold text-[var(--text)]">
                  {(rows.data ?? []).filter((r) => r.target_price !== null).length}
                </div>
              </div>
            </CardBody>
          </Card>
        )}

        {selected !== null && (
          <Card>
            <CardHeader title="Add a name" />
            <CardBody>
              <form
                onSubmit={(e) => { e.preventDefault(); add.mutate(); }}
                className="flex flex-wrap items-end gap-2"
              >
                <div>
                  <label className="mb-1 block text-xs text-[var(--text-muted)]">
                    Ticker
                  </label>
                  <input
                    value={ticker}
                    onChange={(e) => setTicker(e.target.value)}
                    placeholder="MARUTI"
                    className="rounded border border-[var(--border)] bg-[var(--bg-elevated)] px-3 py-1.5 text-sm text-[var(--text)] outline-none focus:border-accent-500"
                  />
                </div>
                <div>
                  <label className="mb-1 block text-xs text-[var(--text-muted)]">
                    Buy below (optional)
                  </label>
                  <input
                    value={buyBelow}
                    onChange={(e) => setBuyBelow(e.target.value)}
                    placeholder="derived from fair value"
                    className="w-52 rounded border border-[var(--border)] bg-[var(--bg-elevated)] px-3 py-1.5 text-sm text-[var(--text)] outline-none focus:border-accent-500"
                  />
                </div>
                <button
                  type="submit"
                  disabled={!ticker.trim() || add.isPending}
                  className="inline-flex items-center gap-1.5 rounded bg-accent-500 px-3 py-1.5 text-xs font-semibold text-white hover:bg-accent-600 disabled:opacity-50"
                >
                  {add.isPending
                    ? <Loader2 className="h-3.5 w-3.5 animate-spin" />
                    : <Plus className="h-3.5 w-3.5" />}
                  Add
                </button>
              </form>
              {error && <Note tone="warning">{error}</Note>}
            </CardBody>
          </Card>
        )}

        <Card>
          <CardHeader
            title="Candidates"
            subtitle="Buy-below is derived from fair value where not set explicitly"
          />
          <CardBody className="p-0">
            {rows.isLoading && <Skeleton className="m-4 h-40" />}
            {rows.data?.length === 0 && (
              <EmptyState
                icon={<Eye className="h-8 w-8" />}
                title="Nothing on this list"
                description="Add a ticker above to begin tracking it."
              />
            )}
            {rows.data && rows.data.length > 0 && (
              <WatchlistTable rows={rows.data} onRemove={(id) => remove.mutate(id)} />
            )}
          </CardBody>
        </Card>

        <Note>
          A name is <strong>triggered</strong> at or below its buy price,{" "}
          <strong>approaching</strong> within 10% of it, and{" "}
          <strong>expensive</strong> at or above the platform&apos;s target.
          Where the valuation engine declines to certify a fair value, the
          upside column shows a dash rather than a number it cannot support.
        </Note>
      </div>
    </AppShell>
  );
}
