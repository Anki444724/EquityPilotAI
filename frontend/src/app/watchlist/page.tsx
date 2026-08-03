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
import { Eye, Loader2, Plus, Pencil, Trash2, X } from "lucide-react";
import { useState, useEffect } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/components/layout/auth-provider";
import { ApiError, setSession } from "@/lib/api";

export default function WatchlistPage() {
  const queryClient = useQueryClient();
  const router = useRouter();
  const { user: authUser, initialising: authInitialising } = useAuth();

  const [selected, setSelected] = useState<number | null>(null);
  const [ticker, setTicker] = useState("");
  const [buyBelow, setBuyBelow] = useState("");
  const [error, setError] = useState<string | null>(null);

  // Create / Rename / Delete states
  const [showCreate, setShowCreate] = useState(false);
  const [newName, setNewName] = useState("");
  const [newDesc, setNewDesc] = useState("");
  const [renamingId, setRenamingId] = useState<number | null>(null);
  const [renameValue, setRenameValue] = useState("");
  const [showDeleteConfirm, setShowDeleteConfirm] = useState<number | null>(null);

  // Floating add company modal
  const [showAddCompany, setShowAddCompany] = useState(false);
  const [addCompanyTicker, setAddCompanyTicker] = useState("");
  const [addCompanyBuyBelow, setAddCompanyBuyBelow] = useState("");

  // Gate queries on auth (consistent with Portfolio)
  const lists = useQuery({
    queryKey: ["watchlists"],
    queryFn: async () => {
      try {
        return await watchlistApi.list();
      } catch (err: unknown) {
        if (err instanceof ApiError && err.status === 401) {
          setSession(null);
          throw err;
        }
        throw err;
      }
    },
    enabled: !authInitialising && !!authUser,
    retry: (failureCount, error) => {
      if (error instanceof ApiError && error.status === 401) return false;
      return failureCount < 1;
    },
  });

  // Handle 401 redirect
  useEffect(() => {
    if (lists.isError) {
      const err = lists.error;
      if (err instanceof ApiError && err.status === 401) {
        setSession(null);
        router.replace("/");
      }
    }
  }, [lists.isError, lists.error, router]);

  // Derive current from data (no sync setState)
  const current = selected ?? (lists.data && lists.data.length > 0 ? lists.data[0].id : null);

  const rows = useQuery({
    queryKey: ["watchlist-rows", current],
    queryFn: () => watchlistApi.rows(current!),
    enabled: !authInitialising && !!authUser && current !== null,
    retry: (failureCount, error) => !(error instanceof ApiError && error.status === 401) && failureCount < 1,
  });

  const add = useMutation({
    mutationFn: () =>
      watchlistApi.add(current!, {
        ticker: ticker.trim().toUpperCase(),
        ...(buyBelow ? { buy_below: Number(buyBelow) } : {}),
      }),
    onSuccess: () => {
      setTicker(""); setBuyBelow(""); setError(null);
      queryClient.invalidateQueries({ queryKey: ["watchlist-rows", current] });
    },
    onError: (err: Error) => setError(err.message),
  });

  const remove = useMutation({
    mutationFn: (entryId: number) => watchlistApi.remove(current!, entryId),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["watchlist-rows", current] }),
  });

  // Create watchlist
  const createWatchlist = useMutation({
    mutationFn: () => watchlistApi.create(newName.trim(), newDesc.trim() || undefined),
    onSuccess: (newWl) => {
      setShowCreate(false);
      setNewName("");
      setNewDesc("");
      setError(null);
      queryClient.invalidateQueries({ queryKey: ["watchlists"] });
      // auto-select the newly created one
      setSelected(newWl.id);
    },
    onError: (err: Error) => setError(err.message),
  });

  // Rename (update)
  const renameWatchlist = useMutation({
    mutationFn: ({ id, name }: { id: number; name: string }) =>
      watchlistApi.update(id, { name: name.trim() }),
    onSuccess: () => {
      setRenamingId(null);
      setRenameValue("");
      queryClient.invalidateQueries({ queryKey: ["watchlists"] });
    },
    onError: (err: Error) => setError(err.message),
  });

  // Delete watchlist
  const deleteWatchlist = useMutation({
    mutationFn: (id: number) => watchlistApi.removeWatchlist(id),
    onSuccess: () => {
      setShowDeleteConfirm(null);
      queryClient.invalidateQueries({ queryKey: ["watchlists"] });
      // if we deleted the current, pick first or null
      if (current === showDeleteConfirm) {
        setSelected(null);
      }
    },
    onError: (err: Error) => setError(err.message),
  });

  // Floating Add Company (adds to current watchlist)
  const addFloatingCompany = useMutation({
    mutationFn: () =>
      watchlistApi.add(current!, {
        ticker: addCompanyTicker.trim().toUpperCase(),
        ...(addCompanyBuyBelow ? { buy_below: Number(addCompanyBuyBelow) } : {}),
      }),
    onSuccess: () => {
      setShowAddCompany(false);
      setAddCompanyTicker("");
      setAddCompanyBuyBelow("");
      setError(null);
      queryClient.invalidateQueries({ queryKey: ["watchlist-rows", current] });
    },
    onError: (err: Error) => setError(err.message),
  });

  const triggered = (rows.data ?? []).filter((r) => r.status === "triggered");

  const isLoading = lists.isLoading || (authInitialising && !authUser);

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

          <div className="flex items-center gap-2">
            {/* Create Watchlist button */}
            <button
              type="button"
              onClick={() => setShowCreate(true)}
              className="inline-flex items-center gap-1.5 rounded border border-[var(--border)] px-3 py-1.5 text-xs font-medium hover:bg-[var(--bg-subtle)]"
            >
              <Plus className="h-3.5 w-3.5" /> New Watchlist
            </button>

            {(lists.data?.length ?? 0) > 0 && (
              <select
                value={current ?? ""}
                onChange={(e) => setSelected(Number(e.target.value))}
                className="rounded border border-[var(--border)] bg-[var(--bg-elevated)] px-3 py-1.5 text-xs text-[var(--text)]"
              >
                {(lists.data ?? []).map((w) => (
                  <option key={w.id} value={w.id}>{w.name}</option>
                ))}
              </select>
            )}

            {/* Rename / Delete controls when a watchlist is selected */}
            {current !== null && (
              <>
                <button
                  type="button"
                  onClick={() => {
                    const wl = lists.data?.find(w => w.id === current);
                    if (wl) {
                      setRenamingId(current);
                      setRenameValue(wl.name);
                    }
                  }}
                  className="inline-flex items-center gap-1.5 rounded border border-[var(--border)] px-2.5 py-1.5 text-xs hover:bg-[var(--bg-subtle)]"
                  title="Rename watchlist"
                >
                  <Pencil className="h-3.5 w-3.5" />
                </button>
                <button
                  type="button"
                  onClick={() => setShowDeleteConfirm(current)}
                  className="inline-flex items-center gap-1.5 rounded border border-[var(--border)] px-2.5 py-1.5 text-xs text-loss hover:bg-[var(--bg-subtle)]"
                  title="Delete watchlist"
                >
                  <Trash2 className="h-3.5 w-3.5" />
                </button>
              </>
            )}
          </div>
        </div>

        {error && <Note tone="warning">{error}</Note>}

        {isLoading && <Skeleton className="h-28 w-full" />}

        {lists.data?.length === 0 && !isLoading && (
          <Card>
            <EmptyState
              icon={<Eye className="h-8 w-8" />}
              title="No watchlists yet"
              description="Create your first watchlist to start tracking candidates."
              action={
                <button
                  onClick={() => setShowCreate(true)}
                  className="inline-flex items-center gap-1.5 rounded bg-accent-500 px-3 py-1.5 text-xs font-semibold text-white"
                >
                  <Plus className="h-3.5 w-3.5" /> Create Watchlist
                </button>
              }
            />
          </Card>
        )}

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

        {current !== null && (
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
            {rows.data?.length === 0 && current !== null && (
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

      {/* Floating "Add Company" button */}
      {current !== null && (
        <button
          onClick={() => setShowAddCompany(true)}
          className="fixed bottom-6 right-6 z-50 flex h-12 w-12 items-center justify-center rounded-full bg-accent-500 text-white shadow-lg hover:bg-accent-600"
          aria-label="Add company to watchlist"
          title="Add company"
        >
          <Plus className="h-6 w-6" />
        </button>
      )}

      {/* Create Watchlist Modal */}
      {showCreate && (
        <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/50 p-4">
          <div className="w-full max-w-md rounded-xl border border-[var(--border)] bg-[var(--bg-elevated)] p-5">
            <div className="flex items-center justify-between mb-4">
              <div className="font-semibold">Create new watchlist</div>
              <button onClick={() => setShowCreate(false)}><X className="h-4 w-4" /></button>
            </div>
            <div className="space-y-3">
              <div>
                <label className="block text-xs mb-1 text-[var(--text-muted)]">Name</label>
                <input
                  value={newName}
                  onChange={(e) => setNewName(e.target.value)}
                  placeholder="My India Watch"
                  className="w-full rounded border border-[var(--border)] bg-[var(--bg)] px-3 py-1.5 text-sm"
                />
              </div>
              <div>
                <label className="block text-xs mb-1 text-[var(--text-muted)]">Description (optional)</label>
                <input
                  value={newDesc}
                  onChange={(e) => setNewDesc(e.target.value)}
                  placeholder="High conviction ideas"
                  className="w-full rounded border border-[var(--border)] bg-[var(--bg)] px-3 py-1.5 text-sm"
                />
              </div>
            </div>
            <div className="mt-5 flex justify-end gap-2">
              <button onClick={() => setShowCreate(false)} className="px-4 py-1.5 text-xs rounded border">Cancel</button>
              <button
                onClick={() => createWatchlist.mutate()}
                disabled={!newName.trim() || createWatchlist.isPending}
                className="px-4 py-1.5 text-xs rounded bg-accent-500 text-white disabled:opacity-50"
              >
                {createWatchlist.isPending ? "Creating..." : "Create"}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Rename Modal */}
      {renamingId !== null && (
        <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/50 p-4">
          <div className="w-full max-w-md rounded-xl border border-[var(--border)] bg-[var(--bg-elevated)] p-5">
            <div className="font-semibold mb-4">Rename watchlist</div>
            <input
              value={renameValue}
              onChange={(e) => setRenameValue(e.target.value)}
              className="w-full rounded border border-[var(--border)] bg-[var(--bg)] px-3 py-1.5 text-sm mb-4"
            />
            <div className="flex justify-end gap-2">
              <button onClick={() => { setRenamingId(null); setRenameValue(""); }} className="px-4 py-1.5 text-xs rounded border">Cancel</button>
              <button
                onClick={() => renameWatchlist.mutate({ id: renamingId, name: renameValue })}
                disabled={!renameValue.trim() || renameWatchlist.isPending}
                className="px-4 py-1.5 text-xs rounded bg-accent-500 text-white disabled:opacity-50"
              >
                {renameWatchlist.isPending ? "Saving..." : "Save"}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Delete confirm */}
      {showDeleteConfirm !== null && (
        <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/50 p-4">
          <div className="w-full max-w-sm rounded-xl border border-[var(--border)] bg-[var(--bg-elevated)] p-5">
            <div className="font-semibold mb-2">Delete watchlist?</div>
            <p className="text-sm text-[var(--text-muted)] mb-4">This will permanently remove the list and all its entries.</p>
            <div className="flex justify-end gap-2">
              <button onClick={() => setShowDeleteConfirm(null)} className="px-4 py-1.5 text-xs rounded border">Cancel</button>
              <button
                onClick={() => deleteWatchlist.mutate(showDeleteConfirm)}
                disabled={deleteWatchlist.isPending}
                className="px-4 py-1.5 text-xs rounded bg-loss text-white disabled:opacity-50"
              >
                {deleteWatchlist.isPending ? "Deleting..." : "Delete"}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Floating Add Company modal */}
      {showAddCompany && current !== null && (
        <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/50 p-4">
          <div className="w-full max-w-sm rounded-xl border border-[var(--border)] bg-[var(--bg-elevated)] p-5">
            <div className="flex items-center justify-between mb-3">
              <div className="font-semibold">Add company to watchlist</div>
              <button onClick={() => setShowAddCompany(false)}><X className="h-4 w-4" /></button>
            </div>
            <div className="space-y-3">
              <div>
                <label className="text-xs block mb-1">Ticker</label>
                <input
                  value={addCompanyTicker}
                  onChange={(e) => setAddCompanyTicker(e.target.value)}
                  placeholder="RELIANCE"
                  className="w-full rounded border px-3 py-1.5 text-sm bg-[var(--bg)]"
                />
              </div>
              <div>
                <label className="text-xs block mb-1">Buy below (optional)</label>
                <input
                  value={addCompanyBuyBelow}
                  onChange={(e) => setAddCompanyBuyBelow(e.target.value)}
                  placeholder="derived"
                  className="w-full rounded border px-3 py-1.5 text-sm bg-[var(--bg)]"
                />
              </div>
            </div>
            <div className="mt-4 flex gap-2 justify-end">
              <button onClick={() => setShowAddCompany(false)} className="text-xs px-4 py-1.5 rounded border">Cancel</button>
              <button
                disabled={!addCompanyTicker.trim() || addFloatingCompany.isPending}
                onClick={() => addFloatingCompany.mutate()}
                className="text-xs px-4 py-1.5 rounded bg-accent-500 text-white disabled:opacity-50"
              >
                {addFloatingCompany.isPending ? "Adding..." : "Add"}
              </button>
            </div>
          </div>
        </div>
      )}
    </AppShell>
  );
}
