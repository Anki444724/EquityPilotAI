"use client";

/** Simple company navigation. Only shipped pages are linked. */
import { cn } from "@/lib/utils";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useRef } from "react";

const TABS = [
  { segment: "", label: "Company Overview" },
  { segment: "financials", label: "Financials" },
  { segment: "forecast", label: "Charts & Forecast" },
  { segment: "ai", label: "Investment View" },
  { segment: "valuation", label: "Value Estimate" },
  { segment: "documents", label: "Company Documents" },
] as const;

export function CompanyTabs({ companyId }: { companyId: string }) {
  const pathname = usePathname();
  const base = `/companies/${companyId}`;
  const stripRef = useRef<HTMLElement>(null);

  useEffect(() => {
    const active = stripRef.current?.querySelector<HTMLElement>("[data-active='true']");
    active?.scrollIntoView({ block: "nearest", inline: "center", behavior: "smooth" });
  }, [pathname]);

  return (
    <nav
      ref={stripRef}
      data-tabstrip
      aria-label="Company sections"
      className="tab-strip mb-4 flex items-center gap-1 border-b border-[var(--border)] lg:mb-5"
    >
      {TABS.map((tab) => {
        const href = tab.segment ? `${base}/${tab.segment}` : base;
        const active = tab.segment
          ? pathname === href || pathname.startsWith(`${href}/`)
          : pathname === base;
        return (
          <Link
            key={tab.segment || "overview"}
            href={href}
            data-active={active}
            aria-current={active ? "page" : undefined}
            className={cn(
              "-mb-px whitespace-nowrap border-b-2 px-3 py-2 text-[0.8125rem] transition-colors",
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
