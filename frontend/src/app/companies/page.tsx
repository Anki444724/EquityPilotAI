"use client";

import { AppShell } from "@/components/layout/app-shell";
import { Card, EmptyState, Skeleton } from "@/components/ui";
import { api } from "@/lib/api";
import { marketCap, marketPrice, rupees } from "@/lib/format";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { Building2, Search } from "lucide-react";
import Link from "next/link";
import { useState } from "react";

const PAGE_SIZE = 20;

export default function CompaniesPage() {
  const [page, setPage] = useState(1);
  const [sector, setSector] = useState<string>("");
  const [query, setQuery] = useState("");

  const { data: sectors } = useQuery({ queryKey: ["sectors"], queryFn: api.sectors });

  const list = useQuery({
    queryKey: ["companies", page, sector],
    queryFn: () => api.listCompanies(page, PAGE_SIZE, sector || undefined),
    placeholderData: keepPreviousData,
    enabled: query.trim() === "",
  });

  const search = useQuery({
    queryKey: ["companies-search", query],
    queryFn: () => api.searchCompanies(query, 50),
    enabled: query.trim() !== "",
  });

  const searching = query.trim() !== "";
  const rows = searching ? search.data?.results ?? [] : list.data?.results ?? [];
  const total = searching ? search.data?.total ?? 0 : list.data?.total ?? 0;
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const loading = searching ? search.isLoading : list.isLoading;

  return (
    <AppShell>
      <div className="mb-5 flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold">Companies</h1>
          <p className="mt-0.5 text-xs text-[var(--text-muted)]">
            {total} {searching ? "matching" : "covered"} {total === 1 ? "company" : "companies"}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <div className="relative">
            <Search size={13} className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-[var(--text-muted)]" />
            <input
              value={query}
              onChange={(e) => { setQuery(e.target.value); setPage(1); }}
              placeholder="Filter…"
              className="w-48 rounded-md border border-[var(--border)] bg-[var(--bg-elevated)] py-1.5 pl-8 pr-2.5 text-xs outline-none focus:border-accent-500"
            />
          </div>
          <select
            value={sector}
            onChange={(e) => { setSector(e.target.value); setPage(1); }}
            disabled={searching}
            className="rounded-md border border-[var(--border)] bg-[var(--bg-elevated)] px-2.5 py-1.5 text-xs outline-none focus:border-accent-500 disabled:opacity-40"
          >
            <option value="">All sectors</option>
            {sectors?.map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
        </div>
      </div>

      <Card>
        {loading ? (
          <div className="space-y-px p-4">
            {Array.from({ length: 8 }).map((_, i) => <Skeleton key={i} className="h-8" />)}
          </div>
        ) : rows.length === 0 ? (
          <EmptyState
            icon={<Building2 size={28} />}
            title="No companies found"
            description={searching ? `Nothing matches “${query}”.` : "Adjust the sector filter."}
          />
        ) : (
          <div className="scroll-x">
            <table className="grid-table">
              <thead>
                <tr>
                  <th>Ticker</th><th className="!text-left">Company</th>
                  <th className="!text-left">Sector</th><th className="!text-left">Industry</th>
                  <th>Price</th><th>Market cap</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((c) => (
                  <tr key={c.id}>
                    <td className="sticky-col !text-left">
                      <Link href={`/companies/${c.id}`} className="num font-semibold text-accent-500 hover:underline">
                        {c.ticker}
                      </Link>
                    </td>
                    <td className="!text-left">
                      <Link href={`/companies/${c.id}`} className="text-[0.8125rem] hover:text-accent-500">
                        {c.name}
                      </Link>
                    </td>
                    <td className="!text-left text-xs text-[var(--text-muted)]">{c.sector ?? "—"}</td>
                    <td className="!text-left text-xs text-[var(--text-muted)]">{c.industry ?? "—"}</td>
                    <td className="num">{rupees(marketPrice(c))}</td>
                    <td className="num font-medium">{marketCap(c.market_cap)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {!searching && pages > 1 && (
          <div className="flex items-center justify-between border-t border-[var(--border)] px-4 py-2.5">
            <span className="text-xs text-[var(--text-muted)]">Page {page} of {pages}</span>
            <div className="flex gap-2">
              <button
                onClick={() => setPage((p) => Math.max(1, p - 1))}
                disabled={page === 1}
                className="rounded border border-[var(--border)] px-2.5 py-1 text-xs disabled:opacity-35"
              >
                Previous
              </button>
              <button
                onClick={() => setPage((p) => Math.min(pages, p + 1))}
                disabled={page >= pages}
                className="rounded border border-[var(--border)] px-2.5 py-1 text-xs disabled:opacity-35"
              >
                Next
              </button>
            </div>
          </div>
        )}
      </Card>
    </AppShell>
  );
}
