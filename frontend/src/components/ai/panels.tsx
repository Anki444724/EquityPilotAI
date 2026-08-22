"use client";

import { Badge, Card, CardBody, CardHeader, Skeleton } from "@/components/ui";
import { percent } from "@/lib/format";
import type {
  AIAnalysisResponse, CapabilityOut, CitationOut, GuardrailOut, ProviderOut,
} from "@/lib/types";
import { cn } from "@/lib/utils";
import {
  AlertTriangle, CheckCircle2, Cpu, FileText, Quote, ShieldCheck, Sparkles,
} from "lucide-react";

/** Minimal markdown renderer — headings, bold, bullets, quotes and rules. */
export function Markdown({ text }: { text: string }) {
  const lines = text.split("\n");
  const out: React.ReactNode[] = [];
  let bullets: string[] = [];

  const flush = (key: string) => {
    if (!bullets.length) return;
    out.push(
      <ul key={key} className="my-2 space-y-1 pl-4">
        {bullets.map((b, i) => (
          <li key={i} className="list-disc text-xs leading-relaxed">
            <Inline text={b} />
          </li>
        ))}
      </ul>,
    );
    bullets = [];
  };

  lines.forEach((raw, i) => {
    const line = raw.trimEnd();
    if (/^[-*]\s+/.test(line)) { bullets.push(line.replace(/^[-*]\s+/, "")); return; }
    flush(`ul-${i}`);
    if (!line.trim()) return;
    if (line.startsWith("> ")) {
      out.push(
        <blockquote key={i} className="my-2 border-l-2 border-warn/60 bg-warn/5 px-3 py-1.5 text-xs">
          <Inline text={line.slice(2)} />
        </blockquote>,
      );
    } else if (line === "---") {
      out.push(<hr key={i} className="my-3 border-[var(--border)]" />);
    } else if (/^#{1,3}\s/.test(line)) {
      out.push(
        <h4 key={i} className="mt-3 mb-1 text-xs font-semibold uppercase tracking-wide text-accent-500">
          <Inline text={line.replace(/^#{1,3}\s/, "")} />
        </h4>,
      );
    } else {
      out.push(
        <p key={i} className="my-1.5 text-xs leading-relaxed">
          <Inline text={line} />
        </p>,
      );
    }
  });
  flush("ul-final");
  return <div>{out}</div>;
}

/** Bold, italic and citation markers. */
function Inline({ text }: { text: string }) {
  const parts = text.split(/(\*\*[^*]+\*\*|_[^_]+_|\[[^\]]+\])/g);
  return (
    <>
      {parts.map((part, i) => {
        if (part.startsWith("**") && part.endsWith("**")) {
          return <strong key={i}>{part.slice(2, -2)}</strong>;
        }
        if (part.startsWith("_") && part.endsWith("_") && part.length > 2) {
          return <em key={i} className="text-[var(--text-muted)]">{part.slice(1, -1)}</em>;
        }
        if (part.startsWith("[") && part.endsWith("]")) {
          return (
            <span
              key={i}
              title="Cited from platform data"
              className="mx-0.5 inline-flex items-center rounded bg-accent-500/15 px-1 text-[0.625rem] font-medium text-accent-400"
            >
              {part.slice(1, -1)}
            </span>
          );
        }
        return <span key={i}>{part}</span>;
      })}
    </>
  );
}

const KIND_TONE: Record<string, "gain" | "accent" | "warn" | "neutral"> = {
  statement: "gain", ratio: "gain", market: "gain",
  forecast: "warn", valuation: "accent", scoring: "accent", document: "neutral",
};

/** Evidence list backing an answer. */
export function CitationList({ citations }: { citations: CitationOut[] }) {
  if (!citations.length) {
    return (
      <p className="text-xs text-[var(--text-muted)]">
        No platform figures were cited in this answer.
      </p>
    );
  }
  return (
    <div className="space-y-1.5">
      {citations.map((c) => (
        <div key={c.key} className="flex items-start gap-2">
          <Badge variant={KIND_TONE[c.kind] ?? "neutral"} className="!text-[0.5625rem]">
            {c.kind}
          </Badge>
          <div className="min-w-0 flex-1">
            <div className="flex items-baseline justify-between gap-2">
              <span className="truncate text-[0.6875rem]">{c.label}</span>
              <span className="num shrink-0 text-[0.6875rem] font-medium">
                {typeof c.value === "number"
                  ? c.unit === "%" ? `${(c.value * 100).toFixed(2)}%`
                    : c.value.toLocaleString(undefined, { maximumFractionDigits: 2 })
                  : c.value ?? "—"}
                {c.unit && c.unit !== "%" ? ` ${c.unit}` : ""}
              </span>
            </div>
            <div className="text-[0.5625rem] text-[var(--text-muted)]">{c.source}</div>
          </div>
        </div>
      ))}
    </div>
  );
}

/** Guardrail summary: claim composition and any violations. */
export function GuardrailPanel({ guardrails, audit }: {
  guardrails: GuardrailOut | null;
  audit: AIAnalysisResponse["citation_audit"];
}) {
  if (!guardrails) return null;
  const labels: Record<string, { label: string; tone: string }> = {
    fact: { label: "Fact", tone: "bg-gain" },
    model_output: { label: "Model output", tone: "bg-accent-500" },
    interpretation: { label: "Interpretation", tone: "bg-warn" },
    opinion: { label: "Opinion", tone: "bg-loss" },
  };
  const total = Object.values(guardrails.composition).reduce((a, b) => a + b, 0) || 1;

  return (
    <Card>
      <CardHeader
        title="Claim composition"
        subtitle="What rests on filings, and what on reasoning"
        action={
          guardrails.passed
            ? <Badge variant="gain"><ShieldCheck size={10} /> Passed</Badge>
            : <Badge variant="loss"><AlertTriangle size={10} /> Flagged</Badge>
        }
      />
      <CardBody className="space-y-3">
        <div className="flex h-2.5 overflow-hidden rounded-full bg-[var(--bg-subtle)]">
          {Object.entries(guardrails.composition).map(([key, count]) =>
            count > 0 ? (
              <div key={key} className={labels[key]?.tone}
                   style={{ width: `${(count / total) * 100}%` }}
                   title={`${labels[key]?.label}: ${count}`} />
            ) : null,
          )}
        </div>
        <div className="flex flex-wrap gap-x-3 gap-y-1 text-[0.625rem] text-[var(--text-muted)]">
          {Object.entries(guardrails.composition).map(([key, count]) => (
            <span key={key} className="inline-flex items-center gap-1">
              <span className={cn("h-1.5 w-1.5 rounded-full", labels[key]?.tone)} />
              {labels[key]?.label} {count}
            </span>
          ))}
        </div>

        {audit && (
          <div className="border-t border-[var(--border)] pt-2.5">
            <div className="flex items-baseline justify-between">
              <span className="text-[0.6875rem] text-[var(--text-muted)]">
                Citation coverage
              </span>
              <span className={cn("num text-xs font-medium",
                audit.is_supported ? "text-gain" : "text-warn")}>
                {percent(audit.coverage, 0)}
              </span>
            </div>
            <p className="mt-1 text-[0.625rem] text-[var(--text-muted)]">{audit.summary}</p>
            {audit.unknown_keys.length > 0 && (
              <p className="mt-1 text-[0.625rem] text-loss">
                Unresolved references: {audit.unknown_keys.join(", ")}
              </p>
            )}
          </div>
        )}

        {guardrails.violations.length > 0 && (
          <ul className="space-y-1 border-t border-[var(--border)] pt-2.5">
            {guardrails.violations.map((v) => (
              <li key={v} className="text-[0.625rem] text-warn">⚠ {v}</li>
            ))}
          </ul>
        )}
      </CardBody>
    </Card>
  );
}

/** Provider registry and status. */
export function ProviderPanel({ providers }: { providers: ProviderOut[] }) {
  return (
    <Card>
      <CardHeader title="Providers" subtitle="Registry-driven; no vendor is hard-coded" />
      <CardBody className="space-y-1.5">
        {providers.map((p) => (
          <div key={p.name} className="flex items-center justify-between gap-2">
            <div className="min-w-0">
              <div className="flex items-center gap-1.5">
                <Cpu size={10} className="shrink-0 text-[var(--text-muted)]" />
                <span className="text-[0.6875rem]">{p.name}</span>
                <Badge className="!text-[0.5rem]">{p.payload_shape}</Badge>
              </div>
              <div className="num truncate text-[0.5625rem] text-[var(--text-muted)]">
                {p.default_model}
              </div>
            </div>
            <Badge variant={p.configured ? "gain" : "neutral"} className="!text-[0.5625rem]">
              {p.configured ? "ready" : "no key"}
            </Badge>
          </div>
        ))}
      </CardBody>
    </Card>
  );
}

/** Capability picker. */
export function CapabilityPicker({
  capabilities, active, onSelect, busy,
}: {
  capabilities: CapabilityOut[];
  active: string;
  onSelect: (key: string) => void;
  busy: boolean;
}) {
  return (
    <Card>
      <CardHeader title="Analyst capabilities" subtitle={`${capabilities.length} grounded analyses`} />
      <CardBody className="max-h-[26rem] space-y-1 overflow-y-auto">
        {capabilities.filter((c) => c.key !== "chat").map((c) => (
          <button
            key={c.key}
            disabled={busy}
            onClick={() => onSelect(c.key)}
            className={cn(
              "w-full rounded-md border px-2.5 py-1.5 text-left transition-colors disabled:opacity-50",
              active === c.key
                ? "border-accent-500 bg-accent-500/10"
                : "border-[var(--border)] hover:border-[var(--border-strong)]",
            )}
          >
            <div className="flex items-center gap-1.5">
              <FileText size={10} className="shrink-0 text-[var(--text-muted)]" />
              <span className="text-[0.6875rem] font-medium">{c.label}</span>
            </div>
            <p className="mt-0.5 text-[0.5625rem] leading-snug text-[var(--text-muted)]">
              {c.description}
            </p>
          </button>
        ))}
      </CardBody>
    </Card>
  );
}

/** Generation metadata strip. */
export function RunMeta({ result }: { result: AIAnalysisResponse }) {
  return (
    <div className="flex flex-wrap items-center gap-2 text-[0.625rem] text-[var(--text-muted)]">
      <Badge variant="accent"><Sparkles size={9} /> {result.provider}</Badge>
      <span className="num">{result.model}</span>
      <span>prompt {result.prompt_key} v{result.prompt_version}</span>
      <span className="num">{result.total_tokens.toLocaleString()} tokens</span>
      <span className="num">${result.cost_usd.toFixed(6)}</span>
      <span className="num">{result.latency_ms.toFixed(0)} ms</span>
      {result.cached && <Badge>cached</Badge>}
      {result.fell_back_from && (
        <Badge variant="warn">fell back from {result.fell_back_from}</Badge>
      )}
      {result.providers_attempted && result.providers_attempted.length > 1 && (
        <span>tried {result.providers_attempted.join(" → ")}</span>
      )}
    </div>
  );
}
