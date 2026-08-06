"use client";

/**
 * Phase 5 — Enterprise AI Operations Center.
 *
 * AI score overrides, model registry, prompt manager, AI queue, learning,
 * RAG status, cost dashboard and AI logs.
 */

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Activity, BrainCircuit, FileText, Gauge, ListChecks, Radio, Save,
  ScrollText, Sparkles, Trash2, X,
} from "lucide-react";

import { adminApi, aiOpsApi } from "@/lib/api";
import type { AIPromptInfo } from "@/lib/types";
import { Button, Select, StatusPill, formatWhen } from "./primitives";
import { Card, CardBody, CardHeader, Skeleton } from "@/components/ui";

type Tab = "overrides" | "models" | "prompts" | "queue" | "learning" | "rag" | "cost" | "logs";

const TABS: { key: Tab; label: string; icon: typeof Gauge }[] = [
  { key: "overrides", label: "AI Score Overrides", icon: Sparkles },
  { key: "models", label: "AI Models", icon: BrainCircuit },
  { key: "prompts", label: "Prompt Manager", icon: FileText },
  { key: "queue", label: "AI Queue", icon: ListChecks },
  { key: "learning", label: "Learning", icon: Activity },
  { key: "rag", label: "RAG", icon: Radio },
  { key: "cost", label: "Cost Dashboard", icon: Gauge },
  { key: "logs", label: "AI Logs", icon: ScrollText },
];

export default function AIOpsView() {
  const [tab, setTab] = useState<Tab>("overrides");
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
      {tab === "overrides" && <OverridesTab />}
      {tab === "models" && <ModelsTab />}
      {tab === "prompts" && <PromptsTab />}
      {tab === "queue" && <StateTab title="AI queue" fn={aiOpsApi.queue} />}
      {tab === "learning" && <StateTab title="Learning & feedback" fn={aiOpsApi.learning} />}
      {tab === "rag" && <StateTab title="RAG / retrieval" fn={aiOpsApi.rag} />}
      {tab === "cost" && <CostTab />}
      {tab === "logs" && <LogsTab />}
    </div>
  );
}

/* ==================================================================== */
function OverridesTab() {
  const client = useQueryClient();
  const [showForm, setShowForm] = useState(false);
  const [companyId, setCompanyId] = useState("");
  const [form, setForm] = useState<Record<string, string>>({});

  const { data: list } = useQuery({
    queryKey: ["admin", "companies", "ai-pick"],
    queryFn: () => adminApi.companies.list({ page: 1, page_size: 50, sort_by: "name", order: "asc" }),
  });
  const { data: overrides } = useQuery({
    queryKey: ["admin", "ai", "overrides"], queryFn: () => aiOpsApi.overrides(),
  });

  const invalidate = () => client.invalidateQueries({ queryKey: ["admin", "ai"] });

  const create = useMutation({
    mutationFn: () => aiOpsApi.createOverride(companyId, {
      mode: "manual",
      manual_score: form.manual_score ? Number(form.manual_score) : null,
      manual_confidence: form.manual_confidence ? Number(form.manual_confidence) : null,
      manual_risk: form.manual_risk ? Number(form.manual_risk) : null,
      manual_summary: form.manual_summary || null,
      manual_bull_case: form.manual_bull_case || null,
      manual_bear_case: form.manual_bear_case || null,
      manual_recommendation: form.manual_recommendation || null,
      reason: form.reason || null,
      expires_in_minutes: form.expires_in_minutes ? Number(form.expires_in_minutes) : null,
    }),
    onSuccess: () => { invalidate(); setShowForm(false); setForm({}); setCompanyId(""); },
  });
  const clear = useMutation({ mutationFn: (id: number) => aiOpsApi.clearOverride(id), onSuccess: invalidate });

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <Button variant="primary" onClick={() => setShowForm(true)}><Sparkles className="h-3.5 w-3.5" /> New AI override</Button>
        <div className="flex-1" />
        <span className="text-xs text-[var(--text-muted)]">Auto mode by default · manual overrides pin the score everywhere</span>
      </div>

      {showForm && (
        <Card className="border-accent-500/30">
          <CardHeader title="Manual AI score override" subtitle="Every surface — company, dashboard, portfolio, watchlist — consumes this score until it expires." />
          <CardBody className="space-y-3">
            <div className="grid grid-cols-2 gap-3 lg:grid-cols-3">
              <label className="block"><span className="mb-1 block text-xs text-[var(--text-muted)]">Company</span>
                <Select value={companyId} onChange={setCompanyId}
                        options={[{ value: "", label: "Select…" }, ...(list?.results ?? []).map((c) => ({ value: c.id, label: `${c.ticker} — ${c.name}` }))]} />
              </label>
              {["manual_score", "manual_confidence", "manual_risk", "expires_in_minutes"].map((f) => (
                <label key={f} className="block"><span className="mb-1 block text-xs text-[var(--text-muted)]">{f.replace("_", " ")}</span>
                  <input value={form[f] ?? ""} onChange={(e) => setForm((x) => ({ ...x, [f]: e.target.value }))} type="number"
                         className="w-full rounded border border-[var(--border)] bg-[var(--bg)] px-3 py-1.5 text-sm" />
                </label>
              ))}
              <label className="block"><span className="mb-1 block text-xs text-[var(--text-muted)]">Recommendation</span>
                <select value={form.manual_recommendation ?? ""} onChange={(e) => setForm((x) => ({ ...x, manual_recommendation: e.target.value }))}
                        className="w-full rounded border border-[var(--border)] bg-[var(--bg)] px-3 py-1.5 text-sm">
                  <option value="">Auto</option><option>Strong Buy</option><option>Buy</option>
                  <option>Accumulate</option><option>Hold</option><option>Reduce</option><option>Sell</option>
                </select>
              </label>
              {["manual_summary", "manual_bull_case", "manual_bear_case"].map((f) => (
                <label key={f} className="block sm:col-span-2"><span className="mb-1 block text-xs text-[var(--text-muted)]">{f.replace("_", " ")}</span>
                  <textarea value={form[f] ?? ""} onChange={(e) => setForm((x) => ({ ...x, [f]: e.target.value }))} rows={2}
                            className="w-full rounded border border-[var(--border)] bg-[var(--bg)] px-3 py-1.5 text-sm" />
                </label>
              ))}
              <label className="block sm:col-span-2"><span className="mb-1 block text-xs text-[var(--text-muted)]">Reason</span>
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
        <CardHeader title="Active AI overrides" />
        <CardBody className="p-0">
          <table className="w-full text-xs">
            <thead><tr>
              <th className="px-3 py-2 text-left text-[var(--text-muted)]">Ticker</th>
              <th className="px-3 py-2 text-right text-[var(--text-muted)]">Score</th>
              <th className="px-3 py-2 text-right text-[var(--text-muted)]">Confidence</th>
              <th className="px-3 py-2 text-right text-[var(--text-muted)]">Risk</th>
              <th className="px-3 py-2 text-left text-[var(--text-muted)]">Recommendation</th>
              <th className="px-3 py-2 text-left text-[var(--text-muted)]">Reason</th>
              <th className="px-3 py-2 text-right text-[var(--text-muted)]">Expires</th>
              <th /></tr></thead>
            <tbody>
              {(overrides ?? []).map((o) => (
                <tr key={o.id} className="border-t border-[var(--border)]">
                  <td className="px-3 py-2 font-medium text-[var(--text)]">{o.ticker}</td>
                  <td className="num px-3 py-2 text-right">{o.manual_score ?? "—"}</td>
                  <td className="num px-3 py-2 text-right">{o.manual_confidence ?? "—"}</td>
                  <td className="num px-3 py-2 text-right">{o.manual_risk ?? "—"}</td>
                  <td className="px-3 py-2">{o.manual_recommendation ?? "—"}</td>
                  <td className="px-3 py-2 text-[var(--text-muted)]">{o.reason ?? "—"}</td>
                  <td className="px-3 py-2 text-right">{o.expires_at ? formatWhen(o.expires_at) : "never"}</td>
                  <td className="px-3 py-2 text-right"><Button variant="danger" onClick={() => clear.mutate(o.id)}><Trash2 className="h-3 w-3" /></Button></td>
                </tr>
              ))}
              {(overrides ?? []).length === 0 && <tr><td colSpan={8} className="py-4 text-center text-[var(--text-muted)]">No active overrides — all scores are in auto mode.</td></tr>}
            </tbody>
          </table>
        </CardBody>
      </Card>
    </div>
  );
}

/* ==================================================================== */
function ModelsTab() {
  const { data } = useQuery({ queryKey: ["admin", "ai", "models"], queryFn: aiOpsApi.models });
  return (
    <Card>
      <CardHeader title="AI model registry" />
      <CardBody className="p-0">
        <table className="w-full text-xs">
          <thead><tr><th className="px-3 py-2 text-left text-[var(--text-muted)]">Model</th>
            <th className="px-3 py-2 text-right text-[var(--text-muted)]">Priority</th>
            <th className="px-3 py-2 text-left text-[var(--text-muted)]">Status</th></tr></thead>
          <tbody>{(data ?? []).map((m) => (
            <tr key={m.name} className="border-t border-[var(--border)]">
              <td className="px-3 py-2 font-medium text-[var(--text)]">{m.name}</td>
              <td className="num px-3 py-2 text-right">{m.priority}</td>
              <td className="px-3 py-2"><StatusPill status={m.status} /></td>
            </tr>))}
          </tbody>
        </table>
      </CardBody>
    </Card>
  );
}

/* ==================================================================== */
function PromptsTab() {
  const [sel, setSel] = useState<AIPromptInfo | null>(null);
  const { data } = useQuery({ queryKey: ["admin", "ai", "prompts"], queryFn: aiOpsApi.prompts });
  return (
    <div className="space-y-3">
      <Card>
        <CardHeader title="Prompt manager" subtitle="Edit, preview and version prompts. Built-ins are restorable." />
        <CardBody className="p-0">
          <table className="w-full text-xs">
            <thead><tr><th className="px-3 py-2 text-left text-[var(--text-muted)]">Key</th>
              <th className="px-3 py-2 text-left text-[var(--text-muted)]">Label</th>
              <th className="px-3 py-2 text-right text-[var(--text-muted)]">Version</th>
              <th className="px-3 py-2 text-right text-[var(--text-muted)]">Tokens</th>
              <th /></tr></thead>
            <tbody>{(data ?? []).map((p) => (
              <tr key={`${p.key}-${p.version}`} className="border-t border-[var(--border)]">
                <td className="px-3 py-2 font-medium text-[var(--text)]">{p.key}</td>
                <td className="px-3 py-2 text-[var(--text-muted)]">{p.label}</td>
                <td className="num px-3 py-2 text-right">v{p.version}</td>
                <td className="num px-3 py-2 text-right">{p.max_tokens}</td>
                <td className="px-3 py-2 text-right"><Button variant="ghost" onClick={() => setSel(p)}>Preview</Button></td>
              </tr>))}
              {(data ?? []).length === 0 && <tr><td colSpan={5} className="py-4 text-center text-[var(--text-muted)]">No prompts.</td></tr>}
            </tbody>
          </table>
        </CardBody>
      </Card>
      {sel && (
        <Card className="border-accent-500/30">
          <CardHeader title={`${sel.key} · v${sel.version}`} action={<Button variant="ghost" onClick={() => setSel(null)}><X className="h-3.5 w-3.5" /></Button>} />
          <CardBody className="space-y-2">
            <div className="text-xs text-[var(--text-muted)]">{sel.task}</div>
            <pre className="max-h-64 overflow-auto rounded border border-[var(--border)] bg-[var(--bg)] p-3 text-[0.6875rem] whitespace-pre-wrap">{sel.template}</pre>
          </CardBody>
        </Card>
      )}
    </div>
  );
}

/* ==================================================================== */
function StateTab({ title, fn }: { title: string; fn: () => Promise<Record<string, unknown>> }) {
  const { data } = useQuery({ queryKey: ["admin", "ai", title], queryFn: fn });
  return (
    <Card><CardHeader title={title} /><CardBody>
      {data ? (
        <div className="grid grid-cols-2 gap-2 lg:grid-cols-4">
          {Object.entries(data).map(([k, v]) => (
            <div key={k} className="rounded border border-[var(--border)] px-3 py-2">
              <div className="text-[0.625rem] uppercase tracking-wider text-[var(--text-muted)]">{k}</div>
              <div className="mt-1 text-sm font-medium text-[var(--text)]">{typeof v === "object" ? JSON.stringify(v) : String(v)}</div>
            </div>
          ))}
        </div>
      ) : <Skeleton className="h-24" />}
    </CardBody></Card>
  );
}

/* ==================================================================== */
function CostTab() {
  const { data } = useQuery({ queryKey: ["admin", "ai", "cost"], queryFn: () => aiOpsApi.cost() });
  const cards = data ? [
    { label: "Total tokens", value: data.total_tokens.toLocaleString("en-IN") },
    { label: "Requests", value: data.requests.toLocaleString("en-IN") },
    { label: "Avg latency", value: `${data.avg_latency_ms}ms` },
    { label: "Total cost", value: `$${data.total_cost_usd.toFixed(4)}` },
    { label: "Daily cost", value: `$${data.daily_cost_usd.toFixed(4)}` },
  ] : [];
  return (
    <div className="space-y-3">
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-5">
        {cards.map((c) => (
          <Card key={c.label}><CardBody>
            <div className="text-[0.625rem] uppercase tracking-wider text-[var(--text-muted)]">{c.label}</div>
            <div className="mt-1 text-lg font-semibold text-[var(--text)]">{c.value}</div>
          </CardBody></Card>
        ))}
      </div>
      <Card>
        <CardHeader title="Cost by provider" />
        <CardBody className="p-0">
          <table className="w-full text-xs">
            <thead><tr><th className="px-3 py-2 text-left text-[var(--text-muted)]">Provider</th>
              <th className="px-3 py-2 text-right text-[var(--text-muted)]">Requests</th>
              <th className="px-3 py-2 text-right text-[var(--text-muted)]">Tokens</th>
              <th className="px-3 py-2 text-right text-[var(--text-muted)]">Cost</th></tr></thead>
            <tbody>{data ? Object.entries(data.by_provider).map(([p, v]) => (
              <tr key={p} className="border-t border-[var(--border)]">
                <td className="px-3 py-2 font-medium text-[var(--text)]">{p}</td>
                <td className="num px-3 py-2 text-right">{v.requests}</td>
                <td className="num px-3 py-2 text-right">{v.tokens.toLocaleString("en-IN")}</td>
                <td className="num px-3 py-2 text-right">${v.cost.toFixed(4)}</td>
              </tr>)) : <tr><td colSpan={4} className="py-4 text-center text-[var(--text-muted)]">No usage yet.</td></tr>}
            </tbody>
          </table>
        </CardBody>
      </Card>
    </div>
  );
}

/* ==================================================================== */
function LogsTab() {
  const { data } = useQuery({ queryKey: ["admin", "ai", "logs"], queryFn: () => aiOpsApi.logs() });
  const items = (data?.items as { id: number; provider: string; model: string; prompt_tokens: number; completion_tokens: number; cost_usd: number; latency_ms: number; succeeded: boolean }[]) ?? [];
  return (
    <Card><CardHeader title="AI logs" /><CardBody className="p-0">
      <table className="w-full text-xs">
        <thead><tr><th className="px-3 py-2 text-left text-[var(--text-muted)]">Provider</th>
          <th className="px-3 py-2 text-left text-[var(--text-muted)]">Model</th>
          <th className="px-3 py-2 text-right text-[var(--text-muted)]">Tokens</th>
          <th className="px-3 py-2 text-right text-[var(--text-muted)]">Cost</th>
          <th className="px-3 py-2 text-right text-[var(--text-muted)]">Latency</th>
          <th className="px-3 py-2 text-left text-[var(--text-muted)]">Status</th></tr></thead>
        <tbody>{items.map((r) => (
          <tr key={r.id} className="border-t border-[var(--border)]">
            <td className="px-3 py-2 text-[var(--text)]">{r.provider}</td>
            <td className="px-3 py-2 text-[var(--text-muted)]">{r.model}</td>
            <td className="num px-3 py-2 text-right">{r.prompt_tokens + r.completion_tokens}</td>
            <td className="num px-3 py-2 text-right">${r.cost_usd.toFixed(4)}</td>
            <td className="num px-3 py-2 text-right">{r.latency_ms}ms</td>
            <td className="px-3 py-2"><StatusPill status={r.succeeded ? "success" : "failed"} /></td>
          </tr>))}
          {items.length === 0 && <tr><td colSpan={6} className="py-4 text-center text-[var(--text-muted)]">No AI calls logged.</td></tr>}
        </tbody>
      </table>
    </CardBody></Card>
  );
}
