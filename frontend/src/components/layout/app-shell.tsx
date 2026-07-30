"use client";

import { api } from "@/lib/api";
import { cn } from "@/lib/utils";
import { useQuery } from "@tanstack/react-query";
import {
  BarChart3, Briefcase, Building2, Eye, FileSearch, FileText,
  LayoutDashboard, Moon, Search, Settings, ShieldCheck, Sparkles, Sun,
  TrendingUp,
} from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, type ReactNode } from "react";
import { CommandPalette } from "./command-palette";
import { useTheme } from "./theme-provider";

const NAV = [
  { href: "/dashboard", label: "Dashboard", icon: LayoutDashboard, key: "d" },
  { href: "/companies", label: "Companies", icon: Building2, key: "c" },
  { href: "/financials", label: "Financials", icon: BarChart3, module: 2 },
  { href: "/valuation", label: "Valuation", icon: TrendingUp, module: 4 },
  { href: "/ai", label: "AI Research", icon: Sparkles, module: 6 },
  { href: "/documents", label: "Documents", icon: FileSearch },
  { href: "/portfolio", label: "Portfolio", icon: Briefcase },
  { href: "/watchlist", label: "Watchlist", icon: Eye },
  { href: "/reports", label: "Reports", icon: FileText },
  // Module 10. The operator console is a separate entry rather than a tab
  // inside Administration: they answer to different permissions, and putting
  // them together invites someone to assume an org admin can reach both.
  { href: "/admin", label: "Administration", icon: Settings },
  { href: "/platform", label: "Platform Ops", icon: ShieldCheck },
] as const;

export function AppShell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const { theme, toggle } = useTheme();
  const { data: user } = useQuery({ queryKey: ["me"], queryFn: api.me });

  // g+<key> navigation shortcuts
  useEffect(() => {
    let armed = false;
    let timer: ReturnType<typeof setTimeout>;
    const onKey = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement;
      if (el?.tagName === "INPUT" || el?.tagName === "TEXTAREA") return;
      if (e.key === "g") { armed = true; clearTimeout(timer); timer = setTimeout(() => (armed = false), 900); return; }
      if (armed) {
        const hit = NAV.find((n) => "key" in n && n.key === e.key);
        if (hit) { e.preventDefault(); router.push(hit.href); }
        armed = false;
      }
    };
    window.addEventListener("keydown", onKey);
    return () => { window.removeEventListener("keydown", onKey); clearTimeout(timer); };
  }, [router]);

  return (
    <div className="flex min-h-screen">
      <CommandPalette />

      {/* Sidebar */}
      <aside className="fixed inset-y-0 left-0 z-30 hidden w-56 flex-col border-r border-[var(--border)] bg-[var(--header)] lg:flex">
        <Link href="/dashboard" className="flex items-center gap-2.5 border-b border-white/10 px-4 py-4">
          <div className="grid h-8 w-8 place-items-center rounded bg-accent-500 text-sm font-bold text-white">
            IE
          </div>
          <div className="leading-tight">
            <div className="text-[0.8125rem] font-semibold text-white">Equity Research</div>
            <div className="text-[0.625rem] uppercase tracking-wider text-white/45">Institutional</div>
          </div>
        </Link>

        <nav className="flex-1 space-y-0.5 overflow-y-auto p-2">
          {NAV.map((item) => {
            const active = pathname === item.href || pathname.startsWith(item.href + "/");
            const locked = "module" in item;
            const Icon = item.icon;
            return (
              <Link
                key={item.href}
                href={locked ? "#" : item.href}
                onClick={(e) => locked && e.preventDefault()}
                title={locked ? `Ships in Module ${item.module}` : undefined}
                className={cn(
                  "flex items-center gap-2.5 rounded px-2.5 py-2 text-[0.8125rem] transition-colors",
                  active ? "bg-accent-500 text-white" : "text-white/65 hover:bg-white/10 hover:text-white",
                  locked && "cursor-not-allowed opacity-35 hover:bg-transparent hover:text-white/65",
                )}
              >
                <Icon size={15} className="shrink-0" />
                <span className="flex-1">{item.label}</span>
                {locked && (
                  <span className="rounded bg-white/10 px-1 text-[0.5625rem] font-medium">
                    M{item.module}
                  </span>
                )}
              </Link>
            );
          })}
        </nav>

        <div className="border-t border-white/10 p-3">
          <div className="flex items-center gap-2.5">
            <div className="grid h-7 w-7 shrink-0 place-items-center rounded-full bg-white/15 text-[0.6875rem] font-semibold text-white">
              {(user?.name ?? "?").slice(0, 1)}
            </div>
            <div className="min-w-0 flex-1">
              <div className="truncate text-xs text-white">{user?.name ?? "…"}</div>
              <div className="truncate text-[0.625rem] uppercase tracking-wide text-white/45">
                {user?.role ?? ""}
              </div>
            </div>
          </div>
          {user?.is_dev_identity && (
            <p className="mt-2 rounded border border-warn/40 bg-warn/10 px-1.5 py-1 text-[0.5625rem] leading-tight text-warn">
              DEV IDENTITY — set NATIVE_AUTH=true for real sign-in
            </p>
          )}
        </div>
      </aside>

      {/* Main */}
      <div className="flex min-w-0 flex-1 flex-col lg:pl-56">
        <header className="sticky top-0 z-20 flex h-14 items-center gap-3 border-b border-[var(--border)] bg-[var(--bg-elevated)]/95 px-4 backdrop-blur">
          <button
            onClick={() => window.dispatchEvent(new KeyboardEvent("keydown", { key: "k", metaKey: true }))}
            className="flex flex-1 items-center gap-2 rounded-md border border-[var(--border)] bg-[var(--bg-subtle)] px-3 py-1.5 text-left text-xs text-[var(--text-muted)] transition-colors hover:border-[var(--border-strong)] sm:max-w-md"
          >
            <Search size={13} />
            <span className="flex-1">Search companies…</span>
            <kbd className="rounded border border-[var(--border)] bg-[var(--bg)] px-1 py-px text-[0.625rem]">⌘K</kbd>
          </button>
          <button
            onClick={toggle}
            aria-label="Toggle theme"
            className="rounded-md border border-[var(--border)] p-1.5 text-[var(--text-muted)] transition-colors hover:text-[var(--text)]"
          >
            {theme === "dark" ? <Sun size={14} /> : <Moon size={14} />}
          </button>
        </header>

        <main className="flex-1 p-4 lg:p-6">{children}</main>

        <footer className="border-t border-[var(--border)] px-4 py-3 text-[0.6875rem] text-[var(--text-muted)] lg:px-6">
          Financial logic derived from Institutional_Equity_Research_Platform_v7.xlsx ·
          54 canonical line items · v1.0 release candidate
        </footer>
      </div>
    </div>
  );
}
