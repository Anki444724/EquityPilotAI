"use client";

/**
 * Phase 4 — Enterprise Live Market Control Center.
 *
 * Provider management & health, manual market overrides, realtime dashboard,
 * cache manager, scheduler, WebSocket monitor, historical sync and logs.
 */

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Activity, Database, Gauge, ListChecks, PlugZap, Radio, RefreshCw, Save,
  ScrollText, Trash2, X,
} from "lucide-react";

import { adminApi, marketOpsApi } from "@/lib/api";
import type { MarketOverride } from "@/lib/types";
import { Button, Select, StatusPill, formatWhen } from "./primitives";
import { Card, CardBody, CardHeader, Skeleton } from "@/components/ui";

type Tab = "dashboard" | "providers" | "overrides" | "cache" | "scheduler" | "websocket" | "sync" | "logs";

const TABS: { key: Tab; label: string; icon: typeof Gauge }[] = [
  { key: "dashboard", label: "Realtime", icon: Gauge },
  { key: "providers", label: "Providers", icon: PlugZap },
  { key: "overrides", label: "Overrides", icon: Radio },
  { key: "cache", label: "Cache", icon: Database },
  { key: "scheduler", label: "Scheduler", icon: ListChecks },
  { key: "websocket", label: "WebSocket", icon: Activity },
  { key: "sync", label: "Historical Sync", icon: RefreshCw },
  { key: "logs", label: "Logs", icon: ScrollText },
];

export default function MarketOpsView() {
  const [tab, setTab] = useState<Tab>("dashboard");
  return (
    <div className="space-y-3">
      <div className="flex gap-1 overflow-x-auto pb-1">
        {TABS.map((t) => {
          const Icon = t.icon;
          return (
            <button key={t.key} onClick={() => setTab(t.key)}
                    className={tab === t.key
                      ? "flex shrink-0 items-center gap-1.5 rounded bg-accent-500/10 px-3 py-1.5 text-xs font-medium text-accent-500"
                      : "flex shrink-0 items-center gap-1.5 rounded border border-[var(--border)] px-3 py-1.5 text-xs text-[var(--text)]"}>
              <Icon className="h-3.5 w-3.5" /> {t.label}
            </button>
          );
        })}
      </div>
      {tab === "dashboard" && <DashboardTab />}
      {tab === "providers" && <ProvidersTab />}
      {tab === "overrides" && <OverridesTab />}
      {tab === "cache" && <CacheTab />}
      {tab === "scheduler" && <SchedulerTab />}
      {tab === "websocket" && <WebsocketTab />}
      {tab === "sync" && <SyncTab />}
      {tab === "logs" && <LogsTab />}
    </div>
  );
}

/* ==================================================================== */
function DashboardTab() {
  const client = useQueryClient();
  const { data, isLoading } = useQuery({
    queryKey: ["admin", "market", "dashboard"], queryFn: marketOpsApi.dashboard,
  });

  const cards = [
    { label: "Connected symbols", value: data?.connected_symbols ?? "—" },
    { label: "Cache size", value: data?.cache_size ?? "—" },
    { label: "Cache hit rate", value: data ? `${(data.cache_hit_rate * 100).toFixed(0)}%` : "—" },
    { label: "Active overrides", value: data?.active_overrides ?? "—" },
    { label: "Providers available", value: data?.providers_available ?? "—" },
    { label: "Market", value: data?.market_status ?? "—" },
    { label: "Redis", value: data?.redis.backend ?? "—" },
    { label: "TTL", value: data ? `${data.ttl_seconds}s` : "—" },
  ];

  return (
    <div className="space-y-3">
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        {cards.map((c) => (
          <Card key={c.label}><CardBody>
            <div className="text-[0.625rem] uppercase tracking-wider text-[var(--text-muted)]">{c.label}</div>
            <div className="mt-1 text-xl font-semibold capitalize text-[var(--text)]">{c.value}</div>
          </CardBody></Card>
        ))}
      </div>
      <Card>
        <CardBody className="flex items-center justify-between text-xs">
          <span className="text-[var(--text-muted)]">
            Last refresh: {data ? formatWhen(data.last_refresh) : "—"} · Memory {data ? formatBytes(data.memory_bytes) : "—"}
          </span>
          <Button variant="ghost" onClick={() => client.invalidateQueries({ queryKey: ["admin", "market"] })}>
            <RefreshCw className="h-3.5 w-3.5" /> Refresh
          </Button>
        </CardBody>
      </Card>
      {isLoading && <Skeleton className="h-24" />}
    </div>
  );
}

function formatBytes(n: number): string {
  if (!n) return "0 B";
  const u = ["B", "KB", "MB", "GB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(1)} ${u[i]}`;
}

/* ==================================================================== */
function ProvidersTab() {
  const { data, isLoading } = useQuery({
    queryKey: ["admin", "market", "providers"], queryFn: marketOpsApi.providers,
  });
  const statusTone: Record<string, "gain" | "warn" | "loss" | "neutral"> = {
    live: "gain", configured: "gain", offline: "neutral", unconfigured: "neutral",
    rate_limited: "warn", auth_failed: "loss",
  };

  return (
    <Card>
      <CardHeader title="Provider registry & health" />
      <CardBody className="p-0">
        {isLoading ? <Skeleton className="h-48" /> : (
          <table className="w-full text-xs">
            <thead><tr>
              <th className="px-3 py-2 text-left text-[var(--text-muted)]">Provider</th>
              <th className="px-3 py-2 text-right text-[var(--text-muted)]">Priority</th>
              <th className="px-3 py-2 text-right text-[var(--text-muted)]">Status</th>
              <th className="px-3 py-2 text-right text-[var(--text-muted)]">Latency</th>
              <th className="px-3 py-2 text-right text-[var(--text-muted)]">Calls</th>
              <th className="px-3 py-2 text-right text-[var(--text-muted)]">Last success</th>
            </tr></thead>
            <tbody>
              {(data ?? []).map((p) => (
                <tr key={p.name} className="border-t border-[var(--border)]">
                  <td className="px-3 py-2 font-medium text-[var(--text)]">{p.name}</td>
                  <td className="num px-3 py-2 text-right">{p.priority}</td>
                  <td className="px-3 py-2 text-right">
                    <StatusPill status={p.status} className={statusTone[p.status] === "gain" ? "text-gain" : ""} />
                  </td>
                  <td className="num px-3 py-2 text-right">{p.latency_ms == null ? "—" : `${p.latency_ms}ms`}</td>
                  <td className="num px-3 py-2 text-right">{p.calls}</td>
                  <td className="num px-3 py-2 text-right">{p.last_success ? formatWhen(p.last_success) : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </CardBody>
    </Card>
  );
}

/* ==================================================================== */
function OverridesTab() {
  const client = useQueryClient();
  const [showForm, setShowForm] = useState(false);
  const [companyId, setCompanyId] = useState("");
  const [form, setForm] = useState<Record<string, string>>({});
  const { data: list } = useQuery({
    queryKey: ["admin", "companies", "override-pick"],
    queryFn: () => adminApi.companies.list({ page: 1, page_size: 50, sort_by: "name", order: "asc" }),
  });
  const { data: overrides } = useQuery({
    queryKey: ["admin", "market", "overrides"], queryFn: () => marketOpsApi.overrides(),
  });

  const invalidate = () => client.invalidateQueries({ queryKey: ["admin", "market"] });

  const create = useMutation({
    mutationFn: () => marketOpsApi.createOverride(companyId, {
      manual_price: form.manual_price ? Number(form.manual_price) : null,
      manual_volume: form.manual_volume ? Number(form.manual_volume) : null,
      manual_market_cap: form.manual_market_cap ? Number(form.manual_market_cap) : null,
      manual_pe: form.manual_pe ? Number(form.manual_pe) : null,
      manual_pb: form.manual_pb ? Number(form.manual_pb) : null,
      reason: form.reason || null,
      expires_in_minutes: form.expires_in_minutes ? Number(form.expires_in_minutes) : null,
      auto_revert: form.auto_revert === "true",
    }),
    onSuccess: () => { invalidate(); setShowForm(false); setForm({}); setCompanyId(""); },
  });
  const clear = useMutation({ mutationFn: (id: number) => marketOpsApi.clearOverride(id), onSuccess: invalidate });

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <Button variant="primary" onClick={() => setShowForm(true)}><Radio className="h-3.5 w-3.5" /> New override</Button>
        <div className="flex-1" />
        <Button variant="danger" onClick={() => marketOpsApi.clearAllOverrides().then(invalidate)}><Trash2 className="h-3.5 w-3.5" /> Clear all</Button>
      </div>

      {showForm && (
        <Card className="border-accent-500/30">
          <CardHeader title="Manual market override" subtitle="Pins this company's snapshot to manual values until expiry or revert." />
          <CardBody className="space-y-3">
            <div className="grid grid-cols-2 gap-3 lg:grid-cols-3">
              <label className="block"><span className="mb-1 block text-xs text-[var(--text-muted)]">Company</span>
                <Select value={companyId} onChange={setCompanyId}
                        options={[{ value: "", label: "Select…" }, ...(list?.results ?? []).map((c) => ({ value: c.id, label: `${c.ticker} — ${c.name}` }))]} />
              </label>
              {["manual_price", "manual_volume", "manual_market_cap", "manual_pe", "manual_pb", "expires_in_minutes"].map((f) => (
                <label key={f} className="block"><span className="mb-1 block text-xs text-[var(--text-muted)]">{f.replace("_", " ")}</span>
                  <input value={form[f] ?? ""} onChange={(e) => setForm((x) => ({ ...x, [f]: e.target.value }))} type="number"
                         className="w-full rounded border border-[var(--border)] bg-[var(--bg)] px-3 py-1.5 text-sm" />
                </label>
              ))}
              <label className="block"><span className="mb-1 block text-xs text-[var(--text-muted)]">Auto revert</span>
                <select value={form.auto_revert ?? "false"} onChange={(e) => setForm((x) => ({ ...x, auto_revert: e.target.value }))}
                        className="w-full rounded border border-[var(--border)] bg-[var(--bg)] px-3 py-1.5 text-sm">
                  <option value="false">No</option><option value="true">Yes</option>
                </select>
              </label>
              <label className="block col-span-2"><span className="mb-1 block text-xs text-[var(--text-muted)]">Reason</span>
                <input value={form.reason ?? ""} onChange={(e) => setForm((x) => ({ ...x, reason: e.target.value }))}
                       className="w-full rounded border border-[var(--border)] bg-[var(--bg)] px-3 py-1.5 text-sm" />
              </label>
            </div>
            <div className="flex gap-2">
              <Button variant="primary" disabled={!companyId || create.isPending} onClick={() => create.mutate()}><Save className="h-3.5 w-3.5" /> Apply</Button>
              <Button onClick={() => setShowForm(false)}><X className="h-3.5 w-3.5" /></Button>
            </div>
          </CardBody>
        </Card>
      )}

      <Card>
        <CardHeader title="Active overrides" />
        <CardBody className="p-0">
          <table className="w-full text-xs">
            <thead><tr>
              <th className="px-3 py-2 text-left text-[var(--text-muted)]">Ticker</th>
              <th className="px-3 py-2 text-right text-[var(--text-muted)]">Price</th>
              <th className="px-3 py-2 text-right text-[var(--text-muted)]">Volume</th>
              <th className="px-3 py-2 text-right text-[var(--text-muted)]">PE</th>
              <th className="px-3 py-2 text-left text-[var(--text-muted)]">Reason</th>
              <th className="px-3 py-2 text-right text-[var(--text-muted)]">Expires</th>
              <th /></tr></thead>
            <tbody>
              {(overrides ?? []).map((o: MarketOverride) => (
                <tr key={o.id} className="border-t border-[var(--border)]">
                  <td className="px-3 py-2 font-medium text-[var(--text)]">{o.ticker}</td>
                  <td className="num px-3 py-2 text-right">{o.manual_price ?? "—"}</td>
                  <td className="num px-3 py-2 text-right">{o.manual_volume ?? "—"}</td>
                  <td className="num px-3 py-2 text-right">{o.manual_pe ?? "—"}</td>
                  <td className="px-3 py-2 text-[var(--text-muted)]">{o.reason ?? "—"}</td>
                  <td className="px-3 py-2 text-right">{o.expires_at ? formatWhen(o.expires_at) : "never"}</td>
                  <td className="px-3 py-2 text-right"><Button variant="danger" onClick={() => clear.mutate(o.id)}><Trash2 className="h-3 w-3" /></Button></td>
                </tr>
              ))}
              {(overrides ?? []).length === 0 && <tr><td colSpan={7} className="py-4 text-center text-[var(--text-muted)]">No active overrides.</td></tr>}
            </tbody>
          </table>
        </CardBody>
      </Card>
    </div>
  );
}

/* ==================================================================== */
function CacheTab() {
  const client = useQueryClient();
  const invalidate = () => client.invalidateQueries({ queryKey: ["admin", "market"] });
  return (
    <Card>
      <CardHeader title="Cache manager" />
      <CardBody className="flex flex-wrap gap-2">
        <Button variant="primary" onClick={() => marketOpsApi.clearCache().then(invalidate)}><Database className="h-3.5 w-3.5" /> Clear cache</Button>
        <Button variant="ghost" onClick={() => marketOpsApi.refreshCache().then(invalidate)}><RefreshCw className="h-3.5 w-3.5" /> Refresh cache</Button>
      </CardBody>
    </Card>
  );
}

function SchedulerTab() {
  const { data } = useQuery({ queryKey: ["admin", "market", "scheduler"], queryFn: marketOpsApi.scheduler });
  return <InfoCard title="Scheduler" data={data} />;
}
function WebsocketTab() {
  const { data } = useQuery({ queryKey: ["admin", "market", "websocket"], queryFn: marketOpsApi.websocket });
  return <InfoCard title="WebSocket monitor" data={data} />;
}
function SyncTab() {
  const { data } = useQuery({ queryKey: ["admin", "market", "sync"], queryFn: marketOpsApi.sync });
  return <InfoCard title="Historical sync" data={data} />;
}
function LogsTab() {
  const { data } = useQuery({ queryKey: ["admin", "market", "logs"], queryFn: () => marketOpsApi.logs() });
  return <InfoCard title="Market & provider logs" data={data} />;
}

function InfoCard({ title, data }: { title: string; data: Record<string, unknown> | undefined }) {
  return (
    <Card>
      <CardHeader title={title} />
      <CardBody>
        {data ? (
          <div className="grid grid-cols-2 gap-2 lg:grid-cols-4">
            {Object.entries(data).map(([k, v]) => (
              <div key={k} className="rounded border border-[var(--border)] px-3 py-2">
                <div className="text-[0.625rem] uppercase tracking-wider text-[var(--text-muted)]">{k}</div>
                <div className="mt-1 text-sm font-medium capitalize text-[var(--text)]">
                  {typeof v === "object" ? JSON.stringify(v) : String(v)}
                </div>
              </div>
            ))}
          </div>
        ) : <Skeleton className="h-24" />}
      </CardBody>
    </Card>
  );
}
