"use client";

/**
 * Research navigation for a single company.
 *
 * Financials, valuation, scoring, forecast, AI and documents were all built
 * and all deployed, but nothing linked to them: the company detail page had
 * no outbound links and the sidebar pointed at top-level routes that no page
 * ever served. The modules were reachable only by typing a URL, which made a
 * complete research platform look like a company browser.
 *
 * Defined once and rendered by every company page, so the set cannot drift
 * between them.
 */

import { cn } from "@/lib/utils";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { watchlistApi } from "@/lib/api";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Plus } from "lucide-react";

const TABS = [
  { segment: "", label: "Overview" },
  { segment: "investment", label: "AI Investment View" },
  { segment: "financials", label: "Financial Health" },
  { segment: "valuation", label: "Valuation" },
  { segment: "scoring", label: "Scoring" },
  { segment: "ai", label: "AI Analysis" },
  { segment: "forecast", label: "Forecast" },
  { segment: "documents", label: "Documents" },
  { segment: "charts", label: "Charts" },
  { segment: "peers", label: "Peers" },
  { segment: "news", label: "News" },
  { segment: "timeline", label: "Timeline" },
] as const;

export function CompanyTabs({ companyId }: { companyId: string }) {
  const pathname = usePathname();
  const base = `/companies/${companyId}`;
  const stripRef = useRef<HTMLElement>(null);
  const queryClient = useQueryClient();

  const [showPicker, setShowPicker] = useState(false);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [note, setNote] = useState("");

  const watchlistsQ = useQuery({
    queryKey: ["watchlists"],
    queryFn: () => watchlistApi.list(),
  });

  const addMut = useMutation({
    mutationFn: (wid: number) =>
      watchlistApi.add(wid, { ticker: companyId.toUpperCase(), note: note || undefined }),
    onSuccess: () => {
      setShowPicker(false);
      setNote("");
      queryClient.invalidateQueries({ queryKey: ["watchlist-rows"] });
      alert("Added to watchlist");
    },
  });

  /**
   * Bring the active tab into view on mount.
   *
   * A scrolling strip solves the wrapping problem but introduces a new one:
   * on a 320px screen only about two and a half of the seven tabs are
   * visible, so a user who lands on /documents sees the strip scrolled to
   * "Overview" and has no indication which tab they are on. Scrolling the
   * active one into view is what makes the strip legible rather than merely
   * compact.
   *
   * `block: "nearest"` keeps the vertical position untouched — without it
   * the browser scrolls the whole page to centre the strip.
   */
  useEffect(() => {
    const el = stripRef.current?.querySelector<HTMLElement>("[data-active='true']");
    el?.scrollIntoView({ block: "nearest", inline: "center", behavior: "smooth" });
  }, [pathname]);

  return (
    <>
      <nav
        ref={stripRef}
        data-tabstrip
        aria-label="Company research sections"
        className="tab-strip mb-4 gap-1 border-b border-[var(--border)] lg:mb-5 flex items-center"
      >
        {TABS.map((tab) => {
          const href = tab.segment ? `${base}/${tab.segment}` : base;
          const active = tab.segment
            ? pathname === href || pathname.startsWith(href + "/")
            : pathname === base;
          return (
            <Link
              key={tab.segment || "overview"}
              href={href}
              data-active={active}
              aria-current={active ? "page" : undefined}
              className={cn(
                "-mb-px border-b-2 px-3 py-2 text-[0.8125rem] transition-colors",
                active
                  ? "border-accent-500 font-medium text-accent-500"
                  : "border-transparent text-[var(--text-muted)] hover:text-[var(--text)]",
              )}
            >
              {tab.label}
            </Link>
          );
        })}

        {/* Add to Watchlist — present on every company subpage */}
        <button
          onClick={() => {
            setShowPicker(true);
            setSelectedId(watchlistsQ.data?.[0]?.id ?? null);
          }}
          className="ml-auto inline-flex items-center gap-1 rounded bg-accent-500/90 px-2 py-0.5 text-[10px] text-white hover:bg-accent-500"
          title="Add this company to a watchlist"
        >
          <Plus className="h-3 w-3" /> Add to Watchlist
        </button>
      </nav>

      {/* Modal for every company page */}
      {showPicker && (
        <div className="fixed inset-0 z-[80] flex items-center justify-center bg-black/50 p-4">
          <div className="w-full max-w-xs rounded-xl border border-[var(--border)] bg-[var(--bg-elevated)] p-4">
            <div className="text-sm font-medium mb-3">Add to Watchlist</div>

            <select
              value={selectedId ?? ""}
              onChange={(e) => setSelectedId(Number(e.target.value))}
              className="w-full mb-2 rounded border px-2 py-1 text-sm"
            >
              {(watchlistsQ.data ?? []).map((w) => (
                <option key={w.id} value={w.id}>{w.name}</option>
              ))}
            </select>

            <input
              value={note}
              onChange={(e) => setNote(e.target.value)}
              placeholder="Optional note"
              className="w-full mb-3 rounded border px-2 py-1 text-sm"
            />

            <div className="flex gap-2 justify-end text-xs">
              <button onClick={() => setShowPicker(false)} className="px-3 py-1 border rounded">Cancel</button>
              <button
                disabled={!selectedId || addMut.isPending}
                onClick={() => selectedId && addMut.mutate(selectedId)}
                className="px-3 py-1 bg-accent-500 text-white rounded disabled:opacity-50"
              >
                {addMut.isPending ? "Adding..." : "Add"}
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
