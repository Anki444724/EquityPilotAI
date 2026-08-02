"use client";

/**
 * Shared building blocks for the admin surface.
 *
 * These exist because the admin panel renders the same four shapes over and
 * over — a status pill, a quota bar, a paginated table, a filter row — and a
 * page that hand-rolls each one drifts within a week. Nothing here contains
 * business logic: a `QuotaBar` renders the utilisation the backend computed,
 * it does not compute one. That rule is the same one Modules 1-9 follow, and
 * it is why the frontend has never needed a test asserting a financial figure.
 */

import { cn } from "@/lib/utils";
import type { ReactNode } from "react";

/* --------------------------------------------------------------- status */
const STATUS_TONE: Record<string, string> = {
  // tenants and subscriptions
  active: "gain", trial: "accent", trialing: "accent",
  past_due: "warn", suspended: "loss", cancelled: "loss", expired: "loss",
  // users
  pending: "warn", disabled: "neutral",
  // jobs
  queued: "neutral", running: "accent", succeeded: "gain",
  failed: "warn", dead_letter: "loss",
  // audit
  success: "gain", failure: "warn", denied: "loss", revoked: "loss",
  // severity
  info: "neutral", notice: "accent", warning: "warn", critical: "loss",
  ok: "gain", degraded: "warn", unhealthy: "loss",
};

export function StatusPill({ status, className }: { status: string; className?: string }) {
  const tone = (STATUS_TONE[status] ?? "neutral") as
    "neutral" | "accent" | "gain" | "loss" | "warn";
  const styles = {
    neutral: "bg-[var(--bg-subtle)] text-[var(--text-muted)] border-[var(--border)]",
    accent: "bg-accent-500/10 text-accent-500 border-accent-500/25",
    gain: "bg-gain/10 text-gain border-gain/25",
    loss: "bg-loss/10 text-loss border-loss/25",
    warn: "bg-warn/10 text-warn border-warn/25",
  }[tone];

  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded border px-1.5 py-0.5",
        "text-[0.6875rem] font-medium whitespace-nowrap capitalize",
        styles, className,
      )}
    >
      {status.replace(/_/g, " ")}
    </span>
  );
}

/* ------------------------------------------------------------ quota bar */
export function QuotaBar({
  label, used, allowance, unlimited, utilisation, unit,
}: {
  label: string; used: number; allowance: number;
  unlimited: boolean; utilisation: number; unit?: string;
}) {
  // Thresholds are presentational only. Whether the quota is actually
  // exhausted is the backend's decision, delivered as `exhausted`.
  const pct = Math.min(100, Math.round(utilisation * 100));
  const tone =
    unlimited ? "bg-[var(--text-muted)]/30"
      : pct >= 100 ? "bg-loss"
      : pct >= 80 ? "bg-warn"
      : "bg-accent-500";

  return (
    <div className="min-w-0">
      <div className="flex items-baseline justify-between gap-2">
        <span className="truncate text-xs text-[var(--text-muted)]">{label}</span>
        <span className="num shrink-0 text-xs text-[var(--text)]">
          {used.toLocaleString("en-IN")}
          <span className="text-[var(--text-muted)]">
            {" / "}{unlimited ? "∞" : allowance.toLocaleString("en-IN")}
            {unit ? ` ${unit}` : ""}
          </span>
        </span>
      </div>
      <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-[var(--bg-subtle)]">
        <div
          className={cn("h-full rounded-full transition-[width]", tone)}
          style={{ width: unlimited ? "100%" : `${pct}%` }}
        />
      </div>
    </div>
  );
}

/* ---------------------------------------------------------------- table */
export function DataTable<T>({
  columns, rows, empty, onRowClick, rowKey,
}: {
  columns: { key: string; header: string; width?: string; align?: "left" | "right" | "center";
             render: (row: T) => ReactNode }[];
  rows: T[];
  empty?: ReactNode;
  onRowClick?: (row: T) => void;
  rowKey: (row: T) => string | number;
}) {
  if (rows.length === 0) {
    return (
      <div className="px-4 py-10 text-center text-xs text-[var(--text-muted)]">
        {empty ?? "Nothing to show."}
      </div>
    );
  }

  return (
    <div className="scroll-x">
      <table className="w-full min-w-full border-collapse text-sm pin-first">
        <thead>
          <tr className="border-b border-[var(--border)]">
            {columns.map((column) => (
              <th
                key={column.key}
                style={column.width ? { width: column.width } : undefined}
                className={cn(
                  "px-3 py-2 text-[0.6875rem] font-semibold uppercase",
                  "tracking-wider text-[var(--text-muted)]",
                  column.align === "right" ? "text-right"
                    : column.align === "center" ? "text-center" : "text-left",
                )}
              >
                {column.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr
              key={rowKey(row)}
              onClick={onRowClick ? () => onRowClick(row) : undefined}
              className={cn(
                "border-b border-[var(--border)]/60 last:border-0",
                onRowClick && "cursor-pointer hover:bg-[var(--bg-subtle)]",
              )}
            >
              {columns.map((column) => (
                <td
                  key={column.key}
                  className={cn(
                    "px-3 py-2 align-middle text-[var(--text)]",
                    column.align === "right" ? "text-right"
                      : column.align === "center" ? "text-center" : "text-left",
                  )}
                >
                  {column.render(row)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/* ----------------------------------------------------------- pagination */
export function Pager({
  page, pageSize, total, onChange,
}: { page: number; pageSize: number; total: number; onChange: (page: number) => void }) {
  const pages = Math.max(1, Math.ceil(total / pageSize));
  if (total === 0) return null;

  const from = (page - 1) * pageSize + 1;
  const to = Math.min(total, page * pageSize);

  return (
    <div className="flex items-center justify-between gap-3 border-t border-[var(--border)] px-3 py-2">
      <span className="text-xs text-[var(--text-muted)]">
        Showing <span className="num">{from}</span>–<span className="num">{to}</span>
        {" of "}<span className="num">{total.toLocaleString("en-IN")}</span>
      </span>
      <div className="flex items-center gap-1">
        <PagerButton disabled={page <= 1} onClick={() => onChange(page - 1)}>
          Previous
        </PagerButton>
        <span className="px-2 text-xs text-[var(--text-muted)]">
          <span className="num">{page}</span> / <span className="num">{pages}</span>
        </span>
        <PagerButton disabled={page >= pages} onClick={() => onChange(page + 1)}>
          Next
        </PagerButton>
      </div>
    </div>
  );
}

function PagerButton({
  children, disabled, onClick,
}: { children: ReactNode; disabled: boolean; onClick: () => void }) {
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onClick}
      className={cn(
        "rounded border border-[var(--border)] px-2 py-1 text-xs transition-colors",
        disabled
          ? "cursor-not-allowed text-[var(--text-muted)] opacity-50"
          : "text-[var(--text)] hover:bg-[var(--bg-subtle)]",
      )}
    >
      {children}
    </button>
  );
}

/* --------------------------------------------------------------- inputs */
export function TextInput({
  value, onChange, placeholder, type = "text", className, ...rest
}: {
  value: string; onChange: (value: string) => void; placeholder?: string;
  type?: string; className?: string;
} & Omit<React.InputHTMLAttributes<HTMLInputElement>, "value" | "onChange" | "type">) {
  return (
    <input
      {...rest}
      type={type}
      value={value}
      placeholder={placeholder}
      onChange={(event) => onChange(event.target.value)}
      className={cn(
        "w-full rounded border border-[var(--border)] bg-[var(--bg)] px-2.5 py-1.5",
        "text-sm text-[var(--text)] placeholder:text-[var(--text-muted)]",
        "focus:border-accent-500 focus:outline-none focus:ring-1 focus:ring-accent-500/30",
        className,
      )}
    />
  );
}

export function Select({
  value, onChange, options, className,
}: {
  value: string; onChange: (value: string) => void;
  options: { value: string; label: string }[]; className?: string;
}) {
  return (
    <select
      value={value}
      onChange={(event) => onChange(event.target.value)}
      className={cn(
        "rounded border border-[var(--border)] bg-[var(--bg)] px-2 py-1.5",
        "text-sm text-[var(--text)] focus:border-accent-500 focus:outline-none",
        className,
      )}
    >
      {options.map((option) => (
        <option key={option.value} value={option.value}>{option.label}</option>
      ))}
    </select>
  );
}

export function Button({
  children, onClick, variant = "default", disabled, type = "button", className,
}: {
  children: ReactNode; onClick?: () => void;
  variant?: "default" | "primary" | "danger" | "ghost";
  disabled?: boolean; type?: "button" | "submit"; className?: string;
}) {
  const variants = {
    default: "border-[var(--border)] bg-[var(--bg)] text-[var(--text)] hover:bg-[var(--bg-subtle)]",
    primary: "border-accent-500 bg-accent-500 text-white hover:bg-accent-600",
    danger: "border-loss/40 bg-loss/10 text-loss hover:bg-loss/20",
    ghost: "border-transparent text-[var(--text-muted)] hover:bg-[var(--bg-subtle)] hover:text-[var(--text)]",
  }[variant];

  return (
    <button
      type={type}
      disabled={disabled}
      onClick={onClick}
      className={cn(
        "inline-flex items-center gap-1.5 rounded border px-2.5 py-1.5",
        "text-xs font-medium transition-colors",
        disabled && "cursor-not-allowed opacity-50",
        variants, className,
      )}
    >
      {children}
    </button>
  );
}

/* ---------------------------------------------------------------- misc */
export function KeyValue({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-4 border-b border-[var(--border)]/50 py-1.5 last:border-0">
      <span className="shrink-0 text-xs text-[var(--text-muted)]">{label}</span>
      <span className="min-w-0 truncate text-right text-xs text-[var(--text)]">{value}</span>
    </div>
  );
}

export function Tabs({
  tabs, active, onChange,
}: {
  tabs: { key: string; label: string; count?: number }[];
  active: string; onChange: (key: string) => void;
}) {
  return (
    <div
      data-tabstrip
      role="tablist"
      className="tab-strip gap-1 border-b border-[var(--border)]"
    >
      {tabs.map((tab) => (
        <button
          key={tab.key}
          type="button"
          role="tab"
          data-active={active === tab.key}
          aria-selected={active === tab.key}
          onClick={() => onChange(tab.key)}
          className={cn(
            "whitespace-nowrap border-b-2 px-3 py-2 text-xs font-medium transition-colors",
            active === tab.key
              ? "border-accent-500 text-[var(--text)]"
              : "border-transparent text-[var(--text-muted)] hover:text-[var(--text)]",
          )}
        >
          {tab.label}
          {tab.count !== undefined && (
            <span className="num ml-1.5 text-[var(--text-muted)]">{tab.count}</span>
          )}
        </button>
      ))}
    </div>
  );
}

/** A sparkline drawn as an inline SVG — no chart library for eight points. */
export function Sparkline({
  points, height = 32, className,
}: { points: number[]; height?: number; className?: string }) {
  if (points.length < 2) return null;
  const max = Math.max(...points, 1);
  const min = Math.min(...points, 0);
  const range = max - min || 1;
  const width = 100;
  const path = points
    .map((value, index) => {
      const x = (index / (points.length - 1)) * width;
      const y = height - ((value - min) / range) * height;
      return `${index === 0 ? "M" : "L"}${x.toFixed(2)},${y.toFixed(2)}`;
    })
    .join(" ");

  return (
    <svg
      viewBox={`0 0 ${width} ${height}`}
      preserveAspectRatio="none"
      className={cn("h-8 w-full", className)}
      aria-hidden
    >
      <path d={path} fill="none" stroke="currentColor" strokeWidth={1.5}
            className="text-accent-500" vectorEffect="non-scaling-stroke" />
    </svg>
  );
}

export function formatBytes(bytes: number): string {
  if (bytes <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const exponent = Math.min(units.length - 1, Math.floor(Math.log(bytes) / Math.log(1024)));
  return `${(bytes / 1024 ** exponent).toFixed(exponent === 0 ? 0 : 1)} ${units[exponent]}`;
}

export function formatWhen(value: string | null | undefined): string {
  if (!value) return "—";
  const then = new Date(value).getTime();
  if (Number.isNaN(then)) return "—";
  const seconds = Math.round((Date.now() - then) / 1000);
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  if (seconds < 2592000) return `${Math.floor(seconds / 86400)}d ago`;
  return new Date(value).toLocaleDateString("en-IN", {
    day: "2-digit", month: "short", year: "numeric",
  });
}

export function formatInr(rupees: number): string {
  if (rupees >= 10_000_000) return `₹${(rupees / 10_000_000).toFixed(2)} cr`;
  if (rupees >= 100_000) return `₹${(rupees / 100_000).toFixed(2)} L`;
  return `₹${rupees.toLocaleString("en-IN")}`;
}
