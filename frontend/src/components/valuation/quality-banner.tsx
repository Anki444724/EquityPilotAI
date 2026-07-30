"use client";

import { Badge } from "@/components/ui";
import type { DataQualityOut } from "@/lib/types";
import { cn } from "@/lib/utils";
import { AlertOctagon, AlertTriangle, CheckCircle2, Info } from "lucide-react";

/**
 * Data-quality disclosure.
 *
 * Rendered above every valuation output. When the underlying data is not
 * sourced from filings, or an output is implausible, the banner is prominent
 * and unmissable — a valuation the platform does not trust must not look like
 * one it does.
 */

const GRADE_STYLE: Record<
  string,
  { label: string; tone: "gain" | "accent" | "warn" | "loss"; icon: typeof Info }
> = {
  investment_grade: { label: "Investment grade", tone: "gain", icon: CheckCircle2 },
  indicative: { label: "Indicative", tone: "accent", icon: Info },
  illustrative: { label: "Illustrative only", tone: "warn", icon: AlertTriangle },
  unreliable: { label: "Unreliable", tone: "loss", icon: AlertOctagon },
};

export function QualityBanner({ quality }: { quality: DataQualityOut }) {
  const style = GRADE_STYLE[quality.grade] ?? GRADE_STYLE.indicative;
  const Icon = style.icon;
  const severe = quality.grade === "unreliable" || quality.grade === "illustrative";

  return (
    <div
      className={cn(
        "mb-5 rounded-lg border px-4 py-3",
        quality.grade === "unreliable" && "border-loss/50 bg-loss/10",
        quality.grade === "illustrative" && "border-warn/50 bg-warn/10",
        quality.grade === "indicative" && "border-accent-500/40 bg-accent-500/5",
        quality.grade === "investment_grade" && "border-gain/40 bg-gain/5",
      )}
    >
      <div className="flex items-start gap-3">
        <Icon
          size={18}
          className={cn(
            "mt-0.5 shrink-0",
            quality.grade === "unreliable" && "text-loss",
            quality.grade === "illustrative" && "text-warn",
            quality.grade === "indicative" && "text-accent-500",
            quality.grade === "investment_grade" && "text-gain",
          )}
        />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant={style.tone}>{style.label}</Badge>
            {quality.coverage !== null && (
              <span className="text-[0.6875rem] text-[var(--text-muted)]">
                {(quality.coverage * 100).toFixed(0)}% data coverage
                {quality.history_years ? ` · ${quality.history_years} years` : ""}
              </span>
            )}
          </div>

          <p
            className={cn(
              "mt-1.5 leading-relaxed",
              severe ? "text-sm font-semibold" : "text-xs",
              quality.grade === "unreliable" && "text-loss",
              quality.grade === "illustrative" && "text-warn",
            )}
          >
            {quality.disclosure ?? quality.headline}
          </p>

          {quality.issues.length > 0 && (
            <ul className="mt-2 space-y-1">
              {quality.issues.map((issue) => (
                <li
                  key={issue.key}
                  className="flex items-start gap-1.5 text-[0.6875rem] text-[var(--text-muted)]"
                >
                  <span
                    className={cn(
                      "mt-1 h-1 w-1 shrink-0 rounded-full",
                      issue.severity === "critical" ? "bg-loss"
                        : issue.severity === "warn" ? "bg-warn" : "bg-[var(--text-muted)]",
                    )}
                  />
                  <span>
                    {issue.message}
                    {issue.detail && (
                      <span className="opacity-70"> ({issue.detail})</span>
                    )}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </div>
  );
}
