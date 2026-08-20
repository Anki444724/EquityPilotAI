"use client";

/**
 * Create a portfolio.
 *
 * `portfolioApi.create` has existed since Module 8 and nothing in the UI ever
 * called it, so a user with no portfolios had no way to make one — the empty
 * state told them to "create a portfolio and record transactions" and offered
 * no control that did either.
 *
 * Only `name` is required by the backend; the rest carry server defaults, so
 * the dialog asks for the two fields that change the numbers on screen and
 * leaves the others alone.
 */

import { ApiError, portfolioApi } from "@/lib/api";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { LoaderCircle } from "lucide-react";
import { useEffect, useState, type FormEvent } from "react";

export function CreatePortfolioDialog({
  open,
  onClose,
  onCreated,
}: {
  open: boolean;
  onClose: () => void;
  onCreated: (portfolioId: number) => void;
}) {
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const [benchmark, setBenchmark] = useState("NIFTY 50");
  const [currency, setCurrency] = useState("INR");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => { if (event.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  const create = useMutation({
    mutationFn: () => portfolioApi.create({
      name: name.trim(),
      benchmark: benchmark.trim() || undefined,
      base_currency: currency.trim() || undefined,
    }),
    onSuccess: async (portfolio) => {
      setName("");
      setError(null);
      await queryClient.invalidateQueries({ queryKey: ["portfolios"] });
      onCreated(portfolio.id);
      onClose();
    },
    onError: (err: unknown) => {
      setError(err instanceof ApiError
        ? err.message
        : "Could not create the portfolio. Check your connection and try again.");
    },
  });

  if (!open) return null;

  function submit(event: FormEvent) {
    event.preventDefault();
    if (!name.trim()) {
      setError("Give the portfolio a name.");
      return;
    }
    setError(null);
    create.mutate();
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
      role="dialog"
      aria-modal="true"
      aria-label="Create portfolio"
      onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}
    >
      <form
        onSubmit={submit}
        className="w-full max-w-sm rounded-lg border border-[var(--border)] bg-[var(--bg-elevated)] p-5 shadow-xl"
      >
        <h2 className="text-sm font-semibold text-[var(--text)]">New portfolio</h2>
        <p className="mt-0.5 text-xs text-[var(--text-muted)]">
          Holdings and risk are computed once transactions are recorded.
        </p>

        {error && (
          <p className="mt-3 rounded border border-loss/40 bg-loss/10 p-2 text-xs text-loss">
            {error}
          </p>
        )}

        <label className="mt-4 block">
          <span className="mb-1 block text-xs text-[var(--text-muted)]">Name</span>
          <input
            autoFocus
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder="Core equity"
            className="w-full rounded-md border border-[var(--border)] bg-[var(--bg)] px-3 py-2 text-sm text-[var(--text)] outline-none focus:border-accent-500"
          />
        </label>

        <div className="mt-3 grid grid-cols-2 gap-3">
          <label className="block">
            <span className="mb-1 block text-xs text-[var(--text-muted)]">Benchmark</span>
            <input
              value={benchmark}
              onChange={(event) => setBenchmark(event.target.value)}
              className="w-full rounded-md border border-[var(--border)] bg-[var(--bg)] px-3 py-2 text-sm text-[var(--text)] outline-none focus:border-accent-500"
            />
          </label>
          <label className="block">
            <span className="mb-1 block text-xs text-[var(--text-muted)]">Currency</span>
            <input
              value={currency}
              onChange={(event) => setCurrency(event.target.value.toUpperCase())}
              maxLength={3}
              className="w-full rounded-md border border-[var(--border)] bg-[var(--bg)] px-3 py-2 text-sm uppercase text-[var(--text)] outline-none focus:border-accent-500"
            />
          </label>
        </div>

        <div className="mt-5 flex justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            className="min-h-10 rounded-md border border-[var(--border)] px-3 py-2 text-xs text-[var(--text-muted)] transition-colors hover:text-[var(--text)]"
          >
            Cancel
          </button>
          <button
            type="submit"
            disabled={create.isPending}
            className="inline-flex min-h-10 items-center gap-2 rounded-md bg-accent-500 px-3 py-2 text-xs font-medium text-white transition-colors hover:bg-accent-600 disabled:opacity-50"
          >
            {create.isPending && <LoaderCircle size={13} className="animate-spin" />}
            Create portfolio
          </button>
        </div>
      </form>
    </div>
  );
}
