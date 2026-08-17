"use client";

import { api } from "@/lib/api";
import { cn } from "@/lib/utils";
import { useQuery } from "@tanstack/react-query";
import {
  BarChart3, Briefcase, Building2, Eye, FileSearch, FileText, Gauge,
  LayoutDashboard, LineChart, LoaderCircle, Menu, Moon, Search, Settings,
  ShieldCheck, Sparkles, Sun, TrendingUp, X,
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
//
// AUDIT FIX: Primary navigation now shows only core sections (Dashboard,
// Companies, Portfolio, Watchlist, Reports, Documents). Per-company research
// (Financials, Valuation, Scoring, Forecast, AI) lives in CompanyTabs, not top rail,
// to reduce overload. This implements progressive disclosure: Simple → Advanced.
/** Roles that may see the tenant administration console. */
const ADMIN_ROLES = ["super_admin", "admin"] as const;
/** Cross-tenant operator console. Super admin only. */
const OPERATOR_ROLES = ["super_admin"] as const;

const NAV = [
  { href: "/dashboard", label: "Dashboard", icon: LayoutDashboard, key: "d" },
  { href: "/companies", label: "Companies", icon: Building2, key: "c" },
  { href: "/portfolio", label: "Portfolio", icon: Briefcase, key: "p" },
  { href: "/watchlist", label: "Watchlist", icon: Eye, key: "w" },
  { href: "/reports", label: "Reports", icon: FileText, key: "r" },
  { href: "/documents", label: "Documents", icon: FileSearch, key: "o" },
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


/** The brand lockup, shared by the desktop rail and the mobile drawer. */
function Brand({ onNavigate }: { onNavigate?: () => void }) {
  return (
    <Link
      href="/dashboard"
      onClick={onNavigate}
      className="flex items-center gap-2.5 border-b border-white/10 px-4 py-4"
    >
      <div className="grid h-8 w-8 shrink-0 place-items-center rounded bg-accent-500 text-sm font-bold text-white">
        IE
      </div>
      <div className="leading-tight">
        <div className="text-[0.8125rem] font-semibold text-white">Equity Research</div>
        <div className="text-[0.625rem] uppercase tracking-wider text-white/45">Institutional</div>
      </div>
    </Link>
  );
}

/**
 * The navigation list itself.
 *
 * Extracted so the desktop rail and the mobile drawer render the SAME list
 * from the same role filtering and the same per-company resolution. Two
 * hand-maintained copies would drift the first time a module is added, and
 * the mobile copy is the one nobody would notice was stale.
 */
function NavList({
  pathname, role, companyForNav, onNavigate,
}: {
  pathname: string;
  role: string | undefined;
  companyForNav: string | null;
  onNavigate?: () => void;
}) {
  return (
    <nav className="flex-1 space-y-0.5 overflow-y-auto p-2">
      {NAV.filter((item) => {
        const allowed = "roles" in item ? item.roles : null;
        return !allowed || (role ? (allowed as readonly string[]).includes(role) : false);
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
            onClick={onNavigate}
            title={needsCompany ? "Choose a company first" : undefined}
            className={cn(
              "flex items-center gap-2.5 rounded px-2.5 py-2 text-[0.8125rem] transition-colors",
              // 40px on touch, unchanged on desktop.
              "min-h-10 lg:min-h-0",
              active ? "bg-accent-500 text-white" : "text-white/65 hover:bg-white/10 hover:text-white",
            )}
          >
            <Icon size={15} className="shrink-0" />
            <span className="flex-1">{item.label}</span>
          </Link>
        );
      })}
    </nav>
  );
}

/** The signed-in user block at the foot of the rail and the drawer. */
function UserCard({ user }: { user: { name?: string; role?: string; is_dev_identity?: boolean } | undefined }) {
  return (
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
  );
}

export function AppShell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const { theme, toggle } = useTheme();
  const { user: sessionUser, initialising } = useAuth();
  const [lastCompanyId, setLastCompanyId] = useState<string | null>(() => {
    if (typeof window === "undefined") return null;
    try {
      return window.sessionStorage.getItem(LAST_COMPANY_KEY) || null;
    } catch {
      return null;
    }
  });
  // Mobile navigation drawer. Below `lg` the sidebar is hidden, and until now
  // nothing replaced it: Dashboard, Portfolio, Watchlist, Reports,
  // Administration and Platform Ops had no reachable link on a phone at all.
  // The only navigation that happened to work was tapping a company row.
  const [navOpen, setNavOpen] = useState(false);
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

  // Persist to storage + update state from navigation (pathname change is external system; use functional update + disable for strict rule)
  useEffect(() => {
    if (activeCompanyId) {
      setLastCompanyId(activeCompanyId); // eslint-disable-line react-hooks/set-state-in-effect
      try {
        window.sessionStorage.setItem(LAST_COMPANY_KEY, activeCompanyId);
      } catch { /* private browsing */ }
    }
  }, [activeCompanyId]);

  const companyForNav = activeCompanyId ?? lastCompanyId;

  // Closing the drawer on navigation is handled by the `onNavigate` callback
  // each link already calls, rather than by an effect watching `pathname`.
  // Both work, but the effect form calls setState synchronously during the
  // effect and triggers a cascading render — flagged by
  // react-hooks/set-state-in-effect — for a state change we already know
  // about at the point the user causes it.

  // Escape closes it, and while it is open the page behind must not scroll —
  // on iOS a scrollable body under a fixed overlay is the classic
  // "scrolling the wrong thing" bug.
  useEffect(() => {
    if (!navOpen) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setNavOpen(false); };
    window.addEventListener("keydown", onKey);
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = previous;
    };
  }, [navOpen]);

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

      {/* Sidebar — desktop rail. Unchanged at lg and above. */}
      <aside className="fixed inset-y-0 left-0 z-30 hidden w-56 flex-col border-r border-[var(--border)] bg-[var(--header)] lg:flex">
        <Brand />
        <NavList pathname={pathname} role={sessionUser?.role} companyForNav={companyForNav} />
        <UserCard user={user} />
      </aside>

      {/* Sidebar — mobile drawer. Renders the identical NavList. */}
      {navOpen && (
        <div className="fixed inset-0 z-40 lg:hidden">
          <button
            aria-label="Close navigation"
            onClick={() => setNavOpen(false)}
            className="absolute inset-0 h-full w-full bg-black/60 backdrop-blur-sm"
          />
          <aside
            id="mobile-nav"
            className={cn(
              "absolute inset-y-0 left-0 flex w-[17rem] max-w-[85vw] flex-col",
              "border-r border-[var(--border)] bg-[var(--header)] shadow-2xl animate-fade-up",
            )}
            style={{ paddingLeft: "env(safe-area-inset-left)" }}
          >
            <div className="flex items-center justify-between border-b border-white/10 pr-2">
              <div className="min-w-0 flex-1 [&>a]:border-b-0">
                <Brand onNavigate={() => setNavOpen(false)} />
              </div>
              <button
                onClick={() => setNavOpen(false)}
                aria-label="Close navigation"
                className="grid h-10 w-10 shrink-0 place-items-center rounded text-white/70 hover:bg-white/10 hover:text-white"
              >
                <X size={18} />
              </button>
            </div>
            <NavList
              pathname={pathname}
              role={sessionUser?.role}
              companyForNav={companyForNav}
              onNavigate={() => setNavOpen(false)}
            />
            <UserCard user={user} />
          </aside>
        </div>
      )}

      {/* Main */}
      <div className="flex min-w-0 flex-1 flex-col lg:pl-56">
        <header
          className="sticky top-0 z-20 flex h-14 items-center gap-2 border-b border-[var(--border)] bg-[var(--bg-elevated)]/95 px-3 backdrop-blur sm:gap-3 sm:px-4"
          style={{ paddingTop: "env(safe-area-inset-top)" }}
        >
          {/* The only entry point to navigation below `lg`. */}
          <button
            onClick={() => setNavOpen(true)}
            aria-label="Open navigation"
            aria-expanded={navOpen}
            aria-controls="mobile-nav"
            className="grid h-10 w-10 shrink-0 place-items-center rounded-md border border-[var(--border)] text-[var(--text-muted)] transition-colors hover:text-[var(--text)] lg:hidden"
          >
            <Menu size={17} />
          </button>

          <button
            onClick={() => window.dispatchEvent(new KeyboardEvent("keydown", { key: "k", metaKey: true }))}
            className="flex h-10 min-w-0 flex-1 items-center gap-2 rounded-md border border-[var(--border)] bg-[var(--bg-subtle)] px-3 text-left text-xs text-[var(--text-muted)] transition-colors hover:border-[var(--border-strong)] lg:h-auto lg:py-1.5 lg:max-w-md"
          >
            <Search size={13} className="shrink-0" />
            <span className="flex-1 truncate">Search companies…</span>
            {/* A keyboard hint is noise on a device with no keyboard. */}
            <kbd className="hidden shrink-0 rounded border border-[var(--border)] bg-[var(--bg)] px-1 py-px text-[0.625rem] sm:inline">
              ⌘K
            </kbd>
          </button>
          <button
            onClick={toggle}
            aria-label="Toggle theme"
            className="grid h-10 w-10 shrink-0 place-items-center rounded-md border border-[var(--border)] text-[var(--text-muted)] transition-colors hover:text-[var(--text)] lg:h-auto lg:w-auto lg:p-1.5"
          >
            {theme === "dark" ? <Sun size={14} /> : <Moon size={14} />}
          </button>
        </header>

        {/* `min-w-0` lets the main column shrink below its content width, which
            is what allows the scroll containers inside it to actually clip. */}
        <main className="min-w-0 flex-1 p-3 sm:p-4 lg:p-6">{children}</main>

        <footer
          className="border-t border-[var(--border)] px-3 py-3 text-[0.6875rem] text-[var(--text-muted)] sm:px-4 lg:px-6"
          style={{ paddingBottom: "max(0.75rem, env(safe-area-inset-bottom))" }}
        >
          Financial logic derived from Institutional_Equity_Research_Platform_v7.xlsx ·
          54 canonical line items · v1.0 release candidate
        </footer>
      </div>
    </div>
  );
}
