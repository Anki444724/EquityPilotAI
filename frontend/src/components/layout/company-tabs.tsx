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
import { useEffect, useRef } from "react";

const TABS = [
  { segment: "", label: "Overview" },
  { segment: "financials", label: "Financials" },
  { segment: "forecast", label: "Forecast" },
  { segment: "valuation", label: "Valuation" },
  { segment: "scoring", label: "Scoring" },
  { segment: "ai", label: "AI Research" },
  { segment: "documents", label: "Documents" },
] as const;

export function CompanyTabs({ companyId }: { companyId: string }) {
  const pathname = usePathname();
  const base = `/companies/${companyId}`;
  const stripRef = useRef<HTMLElement>(null);

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
    <nav
      ref={stripRef}
      data-tabstrip
      aria-label="Company research sections"
      className="tab-strip mb-4 gap-1 border-b border-[var(--border)] lg:mb-5"
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
    </nav>
  );
}
