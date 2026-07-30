import { cn } from "@/lib/utils";
import type { ReactNode } from "react";

/* ------------------------------------------------------------------ Card */
export function Card({ className, children }: { className?: string; children: ReactNode }) {
  return (
    <div
      className={cn(
        "rounded-lg border bg-[var(--bg-elevated)] shadow-sm",
        "border-[var(--border)]",
        className,
      )}
    >
      {children}
    </div>
  );
}

export function CardHeader({
  title, subtitle, action, className,
}: { title: ReactNode; subtitle?: ReactNode; action?: ReactNode; className?: string }) {
  return (
    <div className={cn("flex items-start justify-between gap-4 border-b border-[var(--border)] px-4 py-3", className)}>
      <div className="min-w-0">
        <h3 className="text-[0.8125rem] font-semibold tracking-wide text-[var(--text)] uppercase">
          {title}
        </h3>
        {subtitle && <p className="mt-0.5 text-xs text-[var(--text-muted)]">{subtitle}</p>}
      </div>
      {action}
    </div>
  );
}

export function CardBody({ className, children }: { className?: string; children: ReactNode }) {
  return <div className={cn("p-4", className)}>{children}</div>;
}

/* ------------------------------------------------------------- Stat tile */
export function Stat({
  label, value, hint, tone = "default", mono = true,
}: {
  label: string;
  value: ReactNode;
  hint?: ReactNode;
  tone?: "default" | "gain" | "loss" | "muted";
  mono?: boolean;
}) {
  const toneClass = {
    default: "text-[var(--text)]",
    gain: "text-[var(--color-gain)]",
    loss: "text-[var(--color-loss)]",
    muted: "text-[var(--text-muted)]",
  }[tone];
  return (
    <div className="min-w-0">
      <div className="text-[0.6875rem] font-medium uppercase tracking-wider text-[var(--text-muted)]">
        {label}
      </div>
      <div className={cn("mt-1 truncate text-xl font-semibold", mono && "num !text-xl", toneClass)}>
        {value}
      </div>
      {hint && <div className="mt-0.5 truncate text-xs text-[var(--text-muted)]">{hint}</div>}
    </div>
  );
}

/* ----------------------------------------------------------------- Badge */
export function Badge({
  children, variant = "neutral", className,
}: {
  children: ReactNode;
  variant?: "neutral" | "accent" | "gain" | "loss" | "warn";
  className?: string;
}) {
  const variants = {
    neutral: "bg-[var(--bg-subtle)] text-[var(--text-muted)] border-[var(--border)]",
    accent: "bg-accent-500/10 text-accent-500 border-accent-500/25",
    gain: "bg-gain/10 text-gain border-gain/25",
    loss: "bg-loss/10 text-loss border-loss/25",
    warn: "bg-warn/10 text-warn border-warn/25",
  } as const;
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded border px-1.5 py-0.5",
        "text-[0.6875rem] font-medium whitespace-nowrap",
        variants[variant], className,
      )}
    >
      {children}
    </span>
  );
}

/* ----------------------------------------------------------- Empty state */
export function EmptyState({
  title, description, icon, action,
}: { title: string; description?: string; icon?: ReactNode; action?: ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center px-6 py-14 text-center">
      {icon && <div className="mb-3 text-[var(--text-muted)] opacity-60">{icon}</div>}
      <p className="text-sm font-medium text-[var(--text)]">{title}</p>
      {description && (
        <p className="mt-1 max-w-sm text-xs text-[var(--text-muted)]">{description}</p>
      )}
      {action && <div className="mt-4">{action}</div>}
    </div>
  );
}

/* -------------------------------------------------------------- Skeleton */
export function Skeleton({ className }: { className?: string }) {
  return <div className={cn("animate-pulse rounded bg-[var(--bg-subtle)]", className)} />;
}

/* ------------------------------------------------------------ Section hd */
export function SectionTitle({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <h2 className={cn("text-xs font-semibold uppercase tracking-[0.08em] text-[var(--text-muted)]", className)}>
      {children}
    </h2>
  );
}
