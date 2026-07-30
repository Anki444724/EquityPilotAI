"use client";

import { Badge, Card, CardHeader } from "@/components/ui";
import {
  crore, EM_DASH, multiple, percent, plainNumber, rupees,
} from "@/lib/format";
import { cn } from "@/lib/utils";
import type { Flag, MetricRow, MetricSection, PeriodMeta } from "@/lib/types";
import { AlertTriangle, Info, ShieldCheck } from "lucide-react";

/**
 * Renders a value according to the unit the BACKEND declared.
 *
 * This is presentation only — the component never decides what a number means,
 * never derives one number from another, and never substitutes a value when
 * the API returns null. Nulls render as an em dash, always.
 */
function formatValue(value: number | null, unit: string): string {
  if (value === null || value === undefined) return EM_DASH;
  switch (unit) {
    case "%":
      return percent(value);
    case "x":
    case "ratio":
      return multiple(value);
    case "days":
      return `${value.toFixed(1)}`;
    case "bps":
      return `${value >= 0 ? "+" : ""}${value.toFixed(0)}`;
    case "₹":
      return rupees(value);
    case "count":
      return plainNumber(value, 1);
    case "₹ cr":
    default:
      return crore(value);
  }
}

function valueTone(value: number | null, unit: string): string {
  if (value === null) return "text-[var(--text-muted)]";
  if (unit === "bps") {
    if (value > 0) return "text-[var(--color-gain)]";
    if (value < 0) return "text-[var(--color-loss)]";
  }
  if (value < 0) return "text-[var(--color-loss)]";
  return "";
}

export function MetricGrid({
  sections,
  periods,
  title,
  subtitle,
  action,
}: {
  sections: MetricSection[];
  periods: PeriodMeta;
  title?: string;
  subtitle?: string;
  action?: React.ReactNode;
}) {
  return (
    <Card>
      {title && <CardHeader title={title} subtitle={subtitle} action={action} />}
      <div className="overflow-x-auto">
        <table className="grid-table">
          <thead>
            <tr>
              <th className="min-w-[19rem]">Metric</th>
              {periods.labels.map((label) => (
                <th key={label} className="min-w-[6.5rem]">{label}</th>
              ))}
              <th className="min-w-[4.5rem]">Unit</th>
            </tr>
          </thead>
          <tbody>
            {sections.map((section) => (
              <SectionRows key={section.key} section={section} />
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

function SectionRows({ section }: { section: MetricSection }) {
  return (
    <>
      <tr>
        <td
          colSpan={99}
          className="!bg-[var(--bg-subtle)] !text-left text-[0.6875rem] font-semibold uppercase tracking-[0.08em] text-[var(--text-muted)]"
        >
          {section.title}
        </td>
      </tr>
      {section.rows.map((row) => (
        <Row key={`${section.key}-${row.key}`} row={row} />
      ))}
    </>
  );
}

function Row({ row }: { row: MetricRow }) {
  return (
    <tr className={cn(row.is_subtotal && "is-subtotal")}>
      <td className="sticky-col">
        <span
          className="flex items-center gap-1.5"
          style={{ paddingLeft: `${row.indent * 0.85}rem` }}
        >
          <span className="truncate">{row.label}</span>
          {row.note && (
            <span title={row.note} className="shrink-0 cursor-help text-[var(--text-muted)]">
              <Info size={11} />
            </span>
          )}
        </span>
      </td>
      {row.values.map((value, i) => (
        <td key={i} className={cn("num", valueTone(value, row.unit))}>
          {formatValue(value, row.unit)}
        </td>
      ))}
      <td className="text-[0.6875rem] text-[var(--text-muted)]">{row.unit}</td>
    </tr>
  );
}

/** Diagnostic flag list. Severity and copy both come from the API. */
export function FlagList({ flags }: { flags: Flag[] }) {
  const triggered = flags.filter((f) => f.triggered);
  if (flags.length === 0) return null;

  if (triggered.length === 0) {
    return (
      <div className="flex items-center gap-2 rounded-md border border-gain/25 bg-gain/5 px-3 py-2">
        <ShieldCheck size={14} className="shrink-0 text-gain" />
        <span className="text-xs text-[var(--text-muted)]">
          No diagnostic flags triggered across {flags.length} checks.
        </span>
      </div>
    );
  }

  return (
    <div className="space-y-2">
      {triggered.map((flag) => (
        <div
          key={flag.key}
          className={cn(
            "flex items-start gap-2 rounded-md border px-3 py-2",
            flag.severity === "alert"
              ? "border-loss/30 bg-loss/5"
              : "border-warn/30 bg-warn/5",
          )}
        >
          <AlertTriangle
            size={14}
            className={cn(
              "mt-px shrink-0",
              flag.severity === "alert" ? "text-loss" : "text-warn",
            )}
          />
          <div className="min-w-0">
            <div className="text-xs font-medium">{flag.label}</div>
            {flag.detail && (
              <div className="mt-0.5 text-[0.6875rem] text-[var(--text-muted)]">
                {flag.detail}
              </div>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}

export function WarningList({ warnings }: { warnings: string[] }) {
  if (!warnings.length) return null;
  return (
    <div className="space-y-1.5">
      {warnings.map((w) => (
        <div
          key={w}
          className="flex items-start gap-2 rounded-md border border-loss/30 bg-loss/5 px-3 py-2 text-xs text-loss"
        >
          <AlertTriangle size={13} className="mt-px shrink-0" />
          {w}
        </div>
      ))}
    </div>
  );
}

export { formatValue };
