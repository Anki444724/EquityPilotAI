"use client";

import { api } from "@/lib/api";
import { cn } from "@/lib/utils";
import { useQuery } from "@tanstack/react-query";
import {
  BarChart3, Briefcase, Building2, Eye, FileSearch, FileText, Gauge,
  LayoutDashboard, LineChart, LoaderCircle, Moon, Search, Settings,
  ShieldCheck, Sparkles, Sun, TrendingUp,
} from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { CommandPalette } from "./command-palette";
import { useTheme } from "./theme-provider";
import { useAuth } from "./auth-provider";
import { SignIn } from "./sign-in";

// The research modules are per-company: there is no meaningful "Financials"
// page without a company to show financials *for*. They were previously
// listed here as top-level hrefs that no route ever served, and flagged
// `module: 2/4/6` — a placeholder from before those modules were built. The
// flag rendered them permanently greyed out as "Ships in Module N", so three
// fully-implemented modules looked unbuilt in production.
//
// They now resolve against the company the user is currently looking at, and
// fall back to the company list when there is none.
/** Roles that may see the tenant administration console. */
const ADMIN_ROLES = ["super_admin", "admin"] as const;
/** Cross-tenant operator console. Super admin only. */
const OPERATOR_ROLES = ["super_admin"] as const;

const NAV = [
  { href: "/dashboard", label: "Dashboard", icon: LayoutDashboard, key: "d" },
  { href: "/companies", label: "Companies", icon: Building2, key: "c" },
  { href: "/financials", label: "Financials", icon: BarChart3, perCompany: "financials" },
  { href: "/valuation", label: "Valuation", icon: TrendingUp, perCompany: "valuation" },
  { href: "/scoring", label: "Scoring", icon: Gauge, perCompany: "scoring" },
  { href: "/forecast", label: "Forecast", icon: LineChart, perCompany: "forecast" },
  { href: "/ai", label: "AI Research", icon: Sparkles, perCompany: "ai" },
  { href: "/documents", label: "Documents", icon: FileSearch },
  { href: "/portfolio", label: "Portfolio", icon: Briefcase },
  { href: "/watchlist", label: "Watchlist", icon: Eye },
  { href: "/reports", label: "Reports", icon: FileText },
  // Module 10. The operator console is a separate entry rather than a tab
  // inside Administration: they answer to different permissions, and putting
  // them together invites someone to assume an org admin can reach both.
  // Admin-only. Hidden rather than shown-and-refused: the API enforces the
  // permission regardless, so this is presentation, but offering a link that
  // always 403s trains users to ignore errors.
  { href: "/admin", label: "Administration", icon: Settings, roles: ADMIN_ROLES },
  { href: "/platform", label: "Platform Ops", icon: ShieldCheck, roles: OPERATOR_ROLES },
] as const;

/** Remembered across navigations so the research links stay usable after the
 *  user leaves the company pages. Session-scoped, not persisted. */
const LAST_COMPANY_KEY = "ierp:last-company";


export function AppShell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const { theme, toggle } = useTheme();
  const { user: sessionUser, initialising } = useAuth();
  const [lastCompanyId, setLastCompanyId] = useState<string | null>(null);
  // Only ask the API who we are once a session exists; otherwise every
  // authenticated page fires a guaranteed 401 on mount.
  const { data: user } = useQuery({
    queryKey: ["me"], queryFn: api.me, enabled: Boolean(sessionUser),
  });

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

  // Resolve the per-company research links against whichever company the user
  // is looking at. The id is remembered so the sidebar still works after
  // navigating away to, say, the dashboard.
  const activeCompanyId = useMemo(() => {
    const match = /^\/companies\/([^/]+)/.exec(pathname);
    return match?.[1] ?? null;
  }, [pathname]);

  useEffect(() => {
    if (activeCompanyId) {
      setLastCompanyId(activeCompanyId);
      try {
        window.sessionStorage.setItem(LAST_COMPANY_KEY, activeCompanyId);
      } catch { /* private browsing */ }
    }
  }, [activeCompanyId]);

  useEffect(() => {
    if (lastCompanyId) return;
    try {
      const stored = window.sessionStorage.getItem(LAST_COMPANY_KEY);
      if (stored) setLastCompanyId(stored);
    } catch { /* private browsing */ }
  }, [lastCompanyId]);

  const companyForNav = activeCompanyId ?? lastCompanyId;

  // Nothing behind the shell is reachable without a session, so gate here
  // rather than in each of the eleven pages.
  if (initialising) {
    return (
      <div className="grid min-h-screen place-items-center bg-[var(--bg)]">
        <LoaderCircle size={20} className="animate-spin text-[var(--text-muted)]" />
      </div>
    );
  }
  if (!sessionUser) return <SignIn />;

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
          {NAV.filter((item) => {
            const allowed = "roles" in item ? item.roles : null;
            return !allowed || (sessionUser?.role
              ? (allowed as readonly string[]).includes(sessionUser.role)
              : false);
          }).map((item) => {
            const segment = "perCompany" in item ? item.perCompany : null;
            // A per-company module points at the company in view; with none
            // chosen yet it sends the user to pick one rather than dead-ending.
            const href = segment
              ? (companyForNav ? `/companies/${companyForNav}/${segment}` : "/companies")
              : item.href;
            const active = segment
              ? pathname.endsWith(`/${segment}`)
              : pathname === item.href || pathname.startsWith(item.href + "/");
            const needsCompany = Boolean(segment) && !companyForNav;
            const Icon = item.icon;
            return (
              <Link
                key={item.href}
                href={href}
                title={needsCompany ? "Choose a company first" : undefined}
                className={cn(
                  "flex items-center gap-2.5 rounded px-2.5 py-2 text-[0.8125rem] transition-colors",
                  active ? "bg-accent-500 text-white" : "text-white/65 hover:bg-white/10 hover:text-white",
                )}
              >
                <Icon size={15} className="shrink-0" />
                <span className="flex-1">{item.label}</span>
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
