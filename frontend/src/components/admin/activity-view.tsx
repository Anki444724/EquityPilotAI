"use client";

/**
 * Activity History — a chronological feed of everything that happened on the
 * platform, built from the append-only audit trail. Distinct from the
 * filterable Audit Log view: this is a human-readable timeline.
 */

import { adminApi } from "@/lib/api";
import type { AuditRow } from "@/lib/types";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Activity, ShieldCheck, User } from "lucide-react";

import { Pager, Select, StatusPill, formatWhen } from "./primitives";
import { Card, CardBody, EmptyState, Skeleton } from "@/components/ui";

const CATEGORY_META: Record<string, { label: string; tone: string }> = {
  auth: { label: "Authentication", tone: "text-accent-500" },
  account: { label: "Accounts", tone: "text-[var(--text)]" },
  tenant: { label: "Organisation", tone: "text-[var(--text)]" },
  billing: { label: "Billing", tone: "text-[var(--text)]" },
  research: { label: "Research", tone: "text-[var(--text)]" },
  document: { label: "Documents", tone: "text-[var(--text)]" },
  portfolio: { label: "Portfolio", tone: "text-[var(--text)]" },
  report: { label: "Reports", tone: "text-[var(--text)]" },
  ai: { label: "AI", tone: "text-[var(--text)]" },
  admin: { label: "Admin", tone: "text-[var(--text)]" },
  security: { label: "Security", tone: "text-loss" },
  system: { label: "System", tone: "text-[var(--text-muted)]" },
};

export default function ActivityView() {
  const [page, setPage] = useState(1);
  const [category, setCategory] = useState("");
  const [outcome, setOutcome] = useState("");

  const { data, isLoading, error } = useQuery({
    queryKey: ["admin", "activity", page, category, outcome],
    queryFn: () => adminApi.audit({
      page, page_size: 25, category, outcome, days: 365,
    }),
  });

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <Select value={category} onChange={(v) => { setCategory(v); setPage(1); }}
                options={[
                  { value: "", label: "All categories" },
                  { value: "auth", label: "Authentication" },
                  { value: "account", label: "Accounts" },
                  { value: "admin", label: "Admin" },
                  { value: "security", label: "Security" },
                  { value: "billing", label: "Billing" },
                  { value: "document", label: "Documents" },
                  { value: "report", label: "Reports" },
                  { value: "portfolio", label: "Portfolio" },
                  { value: "ai", label: "AI" },
                ]} />
        <Select value={outcome} onChange={(v) => { setOutcome(v); setPage(1); }}
                options={[
                  { value: "", label: "All outcomes" },
                  { value: "success", label: "Success" },
                  { value: "failure", label: "Failure" },
                  { value: "denied", label: "Denied" },
                ]} />
      </div>

      <Card>
        {isLoading ? <Skeleton className="h-96" /> : error ? (
          <CardBody className="text-xs text-loss">{(error as Error).message}</CardBody>
        ) : !data || data.items.length === 0 ? (
          <EmptyState icon={<Activity className="h-8 w-8" />} title="No activity yet" />
        ) : (
          <>
            <CardBody className="p-0">
              <div className="divide-y divide-[var(--border)]">
                {data.items.map((row) => <ActivityRow key={row.id} row={row} />)}
              </div>
            </CardBody>
            <Pager page={page} pageSize={25} total={data.total} onChange={setPage} />
          </>
        )}
      </Card>
    </div>
  );
}

function ActivityRow({ row }: { row: AuditRow }) {
  const meta = CATEGORY_META[row.category] ?? { label: row.category, tone: "text-[var(--text)]" };
  return (
    <div className="flex items-start gap-3 px-4 py-3">
      <div className="mt-0.5 grid h-7 w-7 shrink-0 place-items-center rounded-full bg-[var(--bg-subtle)]">
        {row.category === "security" || row.outcome === "denied" || row.outcome === "failure"
          ? <ShieldCheck className="h-3.5 w-3.5 text-loss" />
          : row.category === "auth"
            ? <User className="h-3.5 w-3.5 text-accent-500" />
            : <Activity className={`h-3.5 w-3.5 ${meta.tone}`} />}
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center justify-between gap-x-2">
          <code className={`text-xs font-medium ${meta.tone}`}>{row.action}</code>
          <span className="text-[0.625rem] text-[var(--text-muted)]" title={row.occurred_at}>
            {formatWhen(row.occurred_at)}
          </span>
        </div>
        <p className="mt-0.5 text-xs text-[var(--text)]">{row.summary}</p>
        <div className="mt-1 flex flex-wrap items-center gap-2 text-[0.625rem] text-[var(--text-muted)]">
          <span>{row.actor_email ?? "system"}</span>
          <StatusPill status={row.outcome} />
          {row.resource_type && (
            <span className="rounded bg-[var(--bg-subtle)] px-1 py-0.5">{row.resource_type}</span>
          )}
        </div>
      </div>
    </div>
  );
}
