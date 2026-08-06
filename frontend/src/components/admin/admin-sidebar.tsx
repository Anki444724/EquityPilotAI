"use client";

/**
 * Enterprise Admin Panel — grouped sidebar navigation.
 *
 * Phase 1 sections are live. Sections belonging to later phases are shown so
 * the roadmap is visible, but rendered disabled until their phase ships. The
 * admin page holds a single piece of state (the active section) and renders
 * the matching view; this component is purely presentational.
 */

import type { LucideIcon } from "lucide-react";
import {
  Activity, BarChart3, Building2, CreditCard, Database, FileText, Gauge,
  KeyRound, LayoutDashboard, Newspaper, Puzzle, Radio, Recycle,
  ScrollText, Settings, Sparkles, Users,
} from "lucide-react";

export interface AdminNavItem {
  key: string;
  label: string;
  icon: LucideIcon;
  phase: number;
}

export interface AdminNavSection {
  title: string;
  items: AdminNavItem[];
}

//: Every section of the roadmap, grouped. Later phases are listed but disabled.
export const ADMIN_NAV: AdminNavSection[] = [
  {
    title: "Overview",
    items: [
      { key: "overview", label: "Dashboard", icon: LayoutDashboard, phase: 1 },
      { key: "activity", label: "Activity History", icon: Activity, phase: 1 },
      { key: "recycle-bin", label: "Recycle Bin", icon: Recycle, phase: 1 },
    ],
  },
  {
    title: "Content",
    items: [
      { key: "companies", label: "Companies", icon: Building2, phase: 1 },
      { key: "financials", label: "Financial Statements", icon: BarChart3, phase: 1 },
      { key: "market", label: "Live Market", icon: Radio, phase: 1 },
      { key: "ai-score", label: "AI Score", icon: Sparkles, phase: 1 },
      { key: "documents", label: "Documents", icon: FileText, phase: 1 },
      { key: "users", label: "Users & Subscriptions", icon: Users, phase: 1 },
      { key: "news", label: "News", icon: Newspaper, phase: 7 },
      { key: "sectors", label: "Sectors", icon: Puzzle, phase: 8 },
    ],
  },
  {
    title: "Platform",
    items: [
      { key: "api-settings", label: "API Settings", icon: KeyRound, phase: 10 },
      { key: "system", label: "System Settings", icon: Settings, phase: 11 },
      { key: "database", label: "Database", icon: Database, phase: 12 },
      { key: "logs", label: "Logs", icon: ScrollText, phase: 13 },
    ],
  },
  {
    title: "Governance",
    items: [
      { key: "members", label: "Members", icon: Users, phase: 1 },
      { key: "billing", label: "Subscription & Billing", icon: CreditCard, phase: 1 },
      { key: "usage", label: "Usage", icon: Gauge, phase: 1 },
      { key: "keys", label: "API Keys", icon: KeyRound, phase: 1 },
      { key: "audit", label: "Audit Log", icon: ScrollText, phase: 1 },
    ],
  },
];

export const PHASE_LABELS: Record<number, string> = {
  1: "Phase 1", 2: "Phase 2", 3: "Phase 3", 4: "Phase 4", 5: "Phase 5",
  6: "Phase 6", 7: "Phase 7", 8: "Phase 8", 9: "Phase 9", 10: "Phase 10",
  11: "Phase 11", 12: "Phase 12", 13: "Phase 13",
};

function SectionHeader({ title }: { title: string }) {
  return (
    <div className="px-3 pb-1.5 pt-4 text-[0.625rem] font-semibold uppercase tracking-wider text-[var(--text-muted)]">
      {title}
    </div>
  );
}

export default function AdminSidebar({
  active, onChange,
}: { active: string; onChange: (key: string) => void }) {
  return (
    <aside className="hidden w-60 shrink-0 flex-col border-r border-[var(--border)] bg-[var(--bg-elevated)] md:flex">
      <div className="flex items-center gap-2 border-b border-[var(--border)] px-4 py-3.5">
        <div className="grid h-7 w-7 shrink-0 place-items-center rounded bg-accent-500 text-xs font-bold text-white">
          EP
        </div>
        <div>
          <div className="text-[0.8125rem] font-semibold leading-tight text-[var(--text)]">
            EquityPilot Admin
          </div>
          <div className="text-[0.625rem] text-[var(--text-muted)]">Control room</div>
        </div>
      </div>

      <nav className="flex-1 space-y-0 overflow-y-auto p-2">
        {ADMIN_NAV.map((section) => (
          <div key={section.title}>
            <SectionHeader title={section.title} />
            <div className="space-y-0.5">
              {section.items.map((item) => {
                const Icon = item.icon;
                const enabled = item.phase <= 1;
                const isActive = active === item.key;
                if (!enabled) {
                  return (
                    <div
                      key={item.key}
                      title={`${item.label} — ${PHASE_LABELS[item.phase]}`}
                      className="flex cursor-not-allowed items-center gap-2.5 rounded px-2.5 py-2 text-[0.8125rem] text-[var(--text-muted)]/50"
                    >
                      <Icon className="h-4 w-4 shrink-0 opacity-40" />
                      <span className="flex-1">{item.label}</span>
                      <span className="rounded bg-[var(--bg-subtle)] px-1 py-0.5 text-[0.5625rem] text-[var(--text-muted)]">
                        P{item.phase}
                      </span>
                    </div>
                  );
                }
                return (
                  <button
                    type="button"
                    key={item.key}
                    onClick={() => onChange(item.key)}
                    className={
                      isActive
                        ? "flex w-full items-center gap-2.5 rounded bg-accent-500/10 px-2.5 py-2 text-left text-[0.8125rem] font-medium text-accent-500"
                        : "flex w-full items-center gap-2.5 rounded px-2.5 py-2 text-left text-[0.8125rem] text-[var(--text)] transition-colors hover:bg-[var(--bg-subtle)]"
                    }
                  >
                    <Icon className="h-4 w-4 shrink-0" />
                    <span className="flex-1">{item.label}</span>
                  </button>
                );
              })}
            </div>
          </div>
        ))}
      </nav>
    </aside>
  );
}

//: A compact horizontal variant for small screens / mobile.
export function AdminMobileNav({
  active, onChange,
}: { active: string; onChange: (key: string) => void }) {
  const items = ADMIN_NAV.flatMap((s) => s.items).filter((i) => i.phase <= 1);
  return (
    <div className="flex gap-1 overflow-x-auto pb-1 md:hidden">
      {items.map((item) => {
        const Icon = item.icon;
        return (
          <button
            key={item.key}
            type="button"
            onClick={() => onChange(item.key)}
            className={
              active === item.key
                ? "flex shrink-0 items-center gap-1.5 rounded bg-accent-500/10 px-3 py-1.5 text-xs font-medium text-accent-500"
                : "flex shrink-0 items-center gap-1.5 rounded border border-[var(--border)] px-3 py-1.5 text-xs text-[var(--text)]"
            }
          >
            <Icon className="h-3.5 w-3.5" />
            {item.label}
          </button>
        );
      })}
    </div>
  );
}
