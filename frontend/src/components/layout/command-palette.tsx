"use client";

import { api } from "@/lib/api";
import { marketCap } from "@/lib/format";
import { useQuery } from "@tanstack/react-query";
import { Loader2, Search } from "lucide-react";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";

/** ⌘K / Ctrl-K global company search. */
export function CommandPalette() {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [cursor, setCursor] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const router = useRouter();

  const close = () => {
    setOpen(false);
    setQuery("");
    setCursor(0);
  };

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setOpen((v) => {
          const next = !v;
          if (!next) {
            setQuery("");
            setCursor(0);
          }
          return next;
        });
      }
      if (e.key === "Escape") close();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  useEffect(() => {
    if (open) {
      const t = setTimeout(() => inputRef.current?.focus(), 20);
      return () => clearTimeout(t);
    }
  }, [open]);

  const { data, isFetching } = useQuery({
    queryKey: ["search", query],
    queryFn: () => api.searchCompanies(query, 8),
    enabled: open && query.trim().length > 0,
  });

  const results = data?.results ?? [];

  const go = (id: string) => { setOpen(false); router.push(`/companies/${id}`); };

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center bg-black/55 pt-[12vh] backdrop-blur-sm"
      onClick={() => setOpen(false)}
    >
      <div
        className="w-full max-w-xl overflow-hidden rounded-xl border border-[var(--border-strong)] bg-[var(--bg-elevated)] shadow-2xl animate-fade-up"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-3 border-b border-[var(--border)] px-4">
          <Search size={16} className="shrink-0 text-[var(--text-muted)]" />
          <input
            ref={inputRef}
            value={query}
            onChange={(e) => { setQuery(e.target.value); setCursor(0); }}
            onKeyDown={(e) => {
              if (e.key === "ArrowDown") { e.preventDefault(); setCursor((c) => Math.min(c + 1, results.length - 1)); }
              if (e.key === "ArrowUp") { e.preventDefault(); setCursor((c) => Math.max(c - 1, 0)); }
              if (e.key === "Enter" && results[cursor]) go(results[cursor].id);
            }}
            placeholder="Search companies by name, ticker or sector…"
            className="w-full bg-transparent py-3.5 text-sm outline-none placeholder:text-[var(--text-muted)]"
          />
          {isFetching && <Loader2 size={14} className="animate-spin text-[var(--text-muted)]" />}
        </div>

        <div className="max-h-80 overflow-y-auto">
          {query && results.length === 0 && !isFetching && (
            <p className="px-4 py-8 text-center text-xs text-[var(--text-muted)]">
              No companies match “{query}”.
            </p>
          )}
          {!query && (
            <p className="px-4 py-8 text-center text-xs text-[var(--text-muted)]">
              Start typing to search the universe.
            </p>
          )}
          {results.map((c, i) => (
            <button
              key={c.id}
              onMouseEnter={() => setCursor(i)}
              onClick={() => go(c.id)}
              className={`flex w-full items-center justify-between gap-3 px-4 py-2.5 text-left ${
                i === cursor ? "bg-accent-500/10" : ""
              }`}
            >
              <span className="flex min-w-0 items-center gap-3">
                <span className="num shrink-0 rounded bg-[var(--bg-subtle)] px-1.5 py-0.5 text-[0.6875rem] font-semibold">
                  {c.ticker}
                </span>
                <span className="min-w-0">
                  <span className="block truncate text-sm">{c.name}</span>
                  <span className="block truncate text-[0.6875rem] text-[var(--text-muted)]">
                    {c.sector ?? "—"}
                  </span>
                </span>
              </span>
              <span className="num shrink-0 text-xs text-[var(--text-muted)]">
                {marketCap(c.market_cap)}
              </span>
            </button>
          ))}
        </div>

        <div className="flex items-center gap-3 border-t border-[var(--border)] bg-[var(--bg-subtle)] px-4 py-2 text-[0.6875rem] text-[var(--text-muted)]">
          <span><kbd className="rounded border px-1">↑↓</kbd> navigate</span>
          <span><kbd className="rounded border px-1">↵</kbd> open</span>
          <span><kbd className="rounded border px-1">esc</kbd> close</span>
        </div>
      </div>
    </div>
  );
}
