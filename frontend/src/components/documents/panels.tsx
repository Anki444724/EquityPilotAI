"use client";

/**
 * Presentational components for the document-intelligence screens.
 *
 * These render; they do not compute. Every number shown here — coverage,
 * confidence, page counts — arrives from the backend already calculated, which
 * is the rule that has governed the frontend since Module 1.
 */

import { Badge, Card, CardBody, CardHeader } from "@/components/ui";
import type {
  CategoryCoverage, DocCitation, DocEntity, DocFact, DocSearchHit,
  DocTable, DocumentSummary, GraphEdge, GraphNode,
} from "@/lib/types";
import { cn } from "@/lib/utils";
import {
  AlertTriangle, CheckCircle2, FileSpreadsheet, FileText, Layers,
  Presentation, Quote, ScanLine, ShieldCheck, Table2,
} from "lucide-react";
import type { ReactNode } from "react";

/* -------------------------------------------------------------- labels */

export const DOC_TYPE_LABELS: Record<string, string> = {
  annual_report: "Annual Report",
  quarterly_report: "Quarterly Report",
  investor_presentation: "Investor Presentation",
  conference_call: "Conference Call",
  credit_rating: "Credit Rating",
  drhp: "DRHP / RHP",
  esg_report: "ESG Report",
  exchange_filing: "Exchange Filing",
  research_note: "Research Note",
  other: "Unclassified",
};

export const SECTION_LABELS: Record<string, string> = {
  business_overview: "Business Overview",
  chairman_letter: "Chairman's Letter",
  management_discussion: "Management Discussion",
  risk_factors: "Risk Factors",
  financial_statements: "Financial Statements",
  notes_to_accounts: "Notes to Accounts",
  corporate_governance: "Corporate Governance",
  shareholding: "Shareholding",
  esg: "ESG",
  auditor_report: "Auditor Report",
  conference_qa: "Conference Q&A",
  management_guidance: "Management Guidance",
  directors_report: "Directors' Report",
  unknown: "Unclassified",
};

export const RELATION_LABELS: Record<string, string> = {
  subsidiary_of: "subsidiary of",
  promoter_of: "promoter of",
  director_of: "director of",
  competes_with: "competes with",
  supplies_to: "supplies to",
  customer_of: "customer of",
  sells_product: "sells",
  operates_segment: "operates segment",
  operates_in: "operates in",
  exposed_to_risk: "exposed to risk",
  acquired: "acquired",
  audited_by: "audited by",
  guides: "guidance",
  invests_in: "invests in",
};

export const UNIT_LABELS: Record<string, string> = {
  inr_cr: "₹ cr", inr_lakh: "₹ lakh", inr_mn: "₹ mn", inr_bn: "₹ bn",
  inr: "₹", percent: "%", x: "x", years: "yrs", months: "mo",
  count: "", tco2e: "tCO₂e", score: "", index: "", yes_no: "",
  text: "", units: "units", pct_of_revenue: "% of rev", unknown: "",
};

export function docTypeIcon(type: string) {
  if (type === "investor_presentation") return Presentation;
  if (type === "credit_rating") return ShieldCheck;
  if (type === "conference_call") return Quote;
  if (type === "quarterly_report") return FileSpreadsheet;
  return FileText;
}

/* --------------------------------------------------------- confidence */

/** Confidence rendered as a band, because a bare 0.74 means nothing to a reader. */
export function ConfidenceBadge({ value, className }: { value: number; className?: string }) {
  const variant =
    value >= 0.8 ? "gain" : value >= 0.6 ? "accent" : value >= 0.4 ? "warn" : "loss";
  const label = value >= 0.8 ? "High" : value >= 0.6 ? "Good" : value >= 0.4 ? "Moderate" : "Low";
  return (
    <Badge variant={variant} className={className}>
      {label} · {(value * 100).toFixed(0)}%
    </Badge>
  );
}

export function CoverageBar({ value, className }: { value: number; className?: string }) {
  const pct = Math.max(0, Math.min(1, value)) * 100;
  const colour =
    value >= 0.7 ? "bg-emerald-500" : value >= 0.4 ? "bg-amber-500" : "bg-rose-500";
  return (
    <div className={cn("h-1.5 w-full overflow-hidden rounded-full bg-[var(--bg-subtle)]", className)}>
      <div className={cn("h-full rounded-full transition-all", colour)} style={{ width: `${pct}%` }} />
    </div>
  );
}

/* ------------------------------------------------------------ documents */

export function DocumentCard({
  document, selected, onSelect,
}: { document: DocumentSummary; selected?: boolean; onSelect?: () => void }) {
  const Icon = docTypeIcon(document.doc_type);
  const superseded = document.superseded_by !== null;
  return (
    <button
      type="button"
      onClick={onSelect}
      className={cn( "w-full rounded-lg border p-3 text-left transition",
        selected
          ? "border-accent-500 bg-accent-500/10"
          : "border-[var(--border)] hover:border-accent-500/40",
        superseded && "opacity-60",
      )}
    >
      <div className="flex items-start gap-2">
        <Icon className="mt-0.5 h-4 w-4 shrink-0 text-[var(--text-muted)]" />
        <div className="min-w-0 flex-1">
          <div className="truncate text-sm font-medium text-[var(--text)]">
            {document.title ?? document.filename}
          </div>
          <div className="mt-1 flex flex-wrap items-center gap-1.5">
            <Badge variant="neutral">{DOC_TYPE_LABELS[document.doc_type] ?? document.doc_type}</Badge>
            {document.period && <Badge variant="accent">{document.period}</Badge>}
            {document.used_ocr && (
              <Badge variant="warn">
                <ScanLine className="mr-1 inline h-3 w-3" />OCR
              </Badge>
            )}
            {superseded && <Badge variant="loss">superseded</Badge>}
            {document.version > 1 && <Badge variant="neutral">v{document.version}</Badge>}
          </div>
          <div className="mt-1.5 text-xs text-[var(--text-muted)]">
            {document.page_count} pp · {document.chunk_count} chunks ·{" "}
            {document.fact_count} fields · {document.processing_ms.toFixed(0)} ms
          </div>
          <CoverageBar value={document.coverage} className="mt-2" />
        </div>
      </div>
    </button>
  );
}

/* --------------------------------------------------------------- search */

export function SearchAnswer({
  answer, confidence, unavailable, audit, tookMs,
}: {
  answer: string;
  confidence: number;
  unavailable: string | null;
  audit: { verified: boolean; unsupported_pages: number[]; cited_pages: number[] };
  tookMs: number;
}) {
  if (unavailable) {
    // The grounding rule, made visible. The platform says what it does not
    // know rather than composing something plausible from a weak match.
    return (
      <div className="rounded-lg border border-warn/40 bg-warn/10 p-4">
        <div className="flex items-start gap-2">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-warn" />
          <div>
            <div className="text-sm font-semibold text-warn">
              No supported answer
            </div>
            <p className="mt-1 text-sm text-[var(--text)]">{unavailable}</p>
          </div>
        </div>
      </div>
    );
  }
  return (
    <div className="rounded-lg border border-[var(--border)] bg-[var(--bg-elevated)] p-4">
      <div className="mb-2 flex items-center gap-2">
        <ConfidenceBadge value={confidence} />
        {audit.verified ? (
          <Badge variant="gain">
            <CheckCircle2 className="mr-1 inline h-3 w-3" />
            citations verified
          </Badge>
        ) : (
          <Badge variant="loss">
            unsupported pages: {audit.unsupported_pages.join(", ")}
          </Badge>
        )}
        <span className="ml-auto text-xs text-[var(--text-muted)]">{tookMs.toFixed(1)} ms</span>
      </div>
      <p className="whitespace-pre-wrap text-sm leading-relaxed text-[var(--text)]">
        {answer}
      </p>
    </div>
  );
}

export function HitList({ hits }: { hits: DocSearchHit[] }) {
  if (!hits.length) return null;
  return (
    <div className="space-y-2">
      {hits.map((hit) => (
        <div
          key={hit.chunk_id}
          className="rounded-lg border border-slate-200 p-3 "
        >
          <div className="mb-1.5 flex flex-wrap items-center gap-1.5 text-xs">
            <span className="font-medium text-[var(--text)]">
              {hit.document_title}
            </span>
            <Badge variant="neutral">p.{hit.page}</Badge>
            <Badge variant="accent">{SECTION_LABELS[hit.section] ?? hit.section}</Badge>
            <span className="ml-auto font-mono text-[var(--text-muted)]">
              {hit.score.toFixed(3)}
              {" · lex "}{hit.lexical_score.toFixed(2)}
              {" · sem "}{hit.semantic_score.toFixed(2)}
            </span>
          </div>
          <p className="line-clamp-3 text-xs leading-relaxed text-[var(--text-muted)]">
            {hit.text}
          </p>
        </div>
      ))}
    </div>
  );
}

export function CitationList({ citations }: { citations: DocCitation[] }) {
  if (!citations.length) return null;
  return (
    <div className="space-y-2">
      {citations.map((c) => (
        <div
          key={`${c.chunk_id}-${c.page}`}
          className="rounded border-l-2 border-accent-500 bg-[var(--bg-subtle)] py-2 pl-3 pr-2 "
        >
          <div className="text-xs font-medium text-accent-500">
            {c.reference}
          </div>
          <p className="mt-1 line-clamp-2 text-xs italic text-[var(--text-muted)]">
            “{c.quote}”
          </p>
        </div>
      ))}
    </div>
  );
}

/* -------------------------------------------------------------- coverage */

export function CoverageGrid({ categories }: { categories: CategoryCoverage[] }) {
  return (
    <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
      {categories.map((c) => (
        <div
          key={c.category}
          className="rounded-lg border border-slate-200 p-3 "
          title={c.missing.length ? `Not found: ${c.missing.join(", ")}` : "All fields found"}
        >
          <div className="flex items-baseline justify-between">
            <span className="text-xs font-semibold uppercase tracking-wide text-[var(--text-muted)]">
              {c.category}
            </span>
            <span className="font-mono text-sm text-[var(--text)]">
              {c.extracted}/{c.defined}
            </span>
          </div>
          <CoverageBar value={c.coverage} className="mt-2" />
          <div className="mt-1.5 text-xs text-[var(--text-muted)]">
            {c.extracted > 0
              ? `avg confidence ${(c.avg_confidence * 100).toFixed(0)}%`
              : "not found in any document"}
          </div>
        </div>
      ))}
    </div>
  );
}

/* ----------------------------------------------------------------- facts */

export function FactTable({ facts }: { facts: DocFact[] }) {
  if (!facts.length) {
    return <p className="p-4 text-sm text-[var(--text-muted)]">No fields extracted yet.</p>;
  }
  return (
    <div className="scroll-x">
      <table className="w-full text-sm pin-first">
        <thead>
          <tr className="border-b border-[var(--border)] text-left text-xs uppercase tracking-wide text-[var(--text-muted)]">
            <th className="px-3 py-2 font-medium">Field</th>
            <th className="px-3 py-2 font-medium">Value</th>
            <th className="px-3 py-2 font-medium">Unit</th>
            <th className="px-3 py-2 font-medium">Period</th>
            <th className="px-3 py-2 font-medium">Source</th>
            <th className="px-3 py-2 text-right font-medium">Confidence</th>
          </tr>
        </thead>
        <tbody>
          {facts.map((f) => (
            <tr
              key={f.id}
              className="border-b border-slate-100 align-top "
              title={f.evidence ?? undefined}
            >
              <td className="px-3 py-2">
                <div className="font-medium text-[var(--text)]">{f.label}</div>
                <div className="text-xs text-[var(--text-muted)]">{f.category}</div>
              </td>
              <td className="px-3 py-2 font-mono text-[var(--text)]">
                {f.value !== null
                  ? f.value.toLocaleString("en-IN", { maximumFractionDigits: 2 })
                  : <span className="font-sans text-xs italic text-[var(--text-muted)] line-clamp-2">
                      {f.text_value}
                    </span>}
              </td>
              <td className="px-3 py-2 text-xs text-[var(--text-muted)]">{UNIT_LABELS[f.unit] ?? f.unit}</td>
              <td className="px-3 py-2 text-xs text-[var(--text-muted)]">{f.period ?? "—"}</td>
              <td className="px-3 py-2 text-xs text-[var(--text-muted)]">
                p.{f.page} · {SECTION_LABELS[f.section] ?? f.section}
              </td>
              <td className="px-3 py-2 text-right">
                <ConfidenceBadge value={f.confidence} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/* ---------------------------------------------------------------- tables */

export function ExtractedTableView({ table }: { table: DocTable }) {
  return (
    <Card>
      <CardHeader
        title={table.caption ?? `Table ${table.table_index + 1}`}
        subtitle={`Page ${table.page} · ${table.n_rows} rows × ${table.n_cols} cols`}
        action={
          <div className="flex items-center gap-1.5">
            {table.unit !== "unknown" && <Badge variant="accent">{UNIT_LABELS[table.unit] ?? table.unit}</Badge>}
            {table.merged.length > 0 && <Badge variant="neutral">{table.merged.length} merged</Badge>}
            <ConfidenceBadge value={table.confidence} />
          </div>
        }
      />
      <CardBody className="scroll-x p-0">
        <table className="w-full text-xs pin-first">
          {table.header.length > 0 && (
            <thead>
              <tr className="border-b border-[var(--border)] bg-[var(--bg-subtle)]">
                {table.header.map((h, i) => (
                  <th key={i} className="px-3 py-2 text-left font-semibold text-[var(--text-muted)]">
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
          )}
          <tbody>
            {table.rows.map((row, r) => (
              <tr key={r} className="border-b border-[var(--border)]">
                {row.map((cell, c) => (
                  <td
                    key={c}
                    className={cn( "px-3 py-1.5",
                      c === 0
                        ? "text-[var(--text)]"
                        : "text-right font-mono text-[var(--text-muted)]",
                    )}
                  >
                    {cell || "—"}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </CardBody>
    </Card>
  );
}

/* -------------------------------------------------------------- entities */

export function EntityGroups({ entities }: { entities: DocEntity[] }) {
  const groups = entities.reduce<Record<string, DocEntity[]>>((acc, e) => {
    (acc[e.kind] ??= []).push(e);
    return acc;
  }, {});
  const order = Object.keys(groups).sort();
  if (!order.length) {
    return <p className="p-4 text-sm text-[var(--text-muted)]">No entities extracted yet.</p>;
  }
  return (
    <div className="space-y-4">
      {order.map((kind) => (
        <div key={kind}>
          <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-[var(--text-muted)]">
            {kind.replace(/_/g, " ")} · {groups[kind].length}
          </div>
          <div className="flex flex-wrap gap-1.5">
            {groups[kind].map((e) => (
              <span
                key={e.id}
                title={`${e.context ?? ""} (page ${e.page}, ${e.mentions} mention${e.mentions === 1 ? "" : "s"})`}
                className="inline-flex max-w-full items-center gap-1.5 rounded-full border border-[var(--border)] bg-[var(--bg-elevated)] px-2.5 py-1 text-xs"
              >
                <span className="truncate text-[var(--text)]">{e.name}</span>
                <span className="shrink-0 font-mono text-[10px] text-[var(--text-muted)]">
                  {(e.confidence * 100).toFixed(0)}%
                </span>
              </span>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}

/* --------------------------------------------------------- knowledge graph */

/**
 * The graph as a radial diagram in plain SVG.
 *
 * Deliberately not a force-directed layout: the graph is a hub-and-spoke by
 * construction — every edge touches the subject company — so a simulation
 * would spend effort discovering a shape we already know. A ring, grouped by
 * relation, is both cheaper and easier to read.
 */
export function KnowledgeGraphView({
  nodes, edges, subjectKey, width = 900, height = 620,
}: {
  nodes: GraphNode[]; edges: GraphEdge[]; subjectKey: string;
  width?: number; height?: number;
}) {
  const cx = width / 2;
  const cy = height / 2;
  const others = nodes.filter((n) => n.key !== subjectKey);
  if (!others.length) {
    return <p className="p-6 text-sm text-[var(--text-muted)]">No relationships extracted yet.</p>;
  }

  const byRelation = new Map<string, GraphEdge>();
  for (const edge of edges) {
    const other = edge.source === subjectKey ? edge.target : edge.source;
    byRelation.set(other, edge);
  }

  const grouped = [...others].sort((a, b) => {
    const ra = byRelation.get(a.key)?.relation ?? "";
    const rb = byRelation.get(b.key)?.relation ?? "";
    return ra.localeCompare(rb) || a.label.localeCompare(b.label);
  });

  // Split the ring into a left and a right column rather than distributing
  // evenly around the circle. Labels are horizontal text, so nodes near the
  // top and bottom of a true circle sit almost vertically above one another
  // and their labels overlap into an unreadable pile — which is exactly what
  // the first version produced at the poles. Two vertical columns give every
  // label its own horizontal band.
  const half = Math.ceil(grouped.length / 2);
  const columns = [grouped.slice(0, half), grouped.slice(half)];
  const radiusX = width / 2 - 200;
  const usableHeight = height - 80;

  const placed = columns.flatMap((column, side) =>
    column.map((node, i) => {
      const step = usableHeight / Math.max(column.length, 1);
      const y = 40 + step * (i + 0.5);
      const right = side === 1;
      // Bow the column outward at its vertical centre so the edges fan rather
      // than run parallel, which makes individual edges traceable.
      const t = column.length > 1 ? i / (column.length - 1) : 0.5;
      const bow = Math.sin(t * Math.PI) * 26;
      const x = right ? cx + radiusX - bow : cx - radiusX + bow;
      return {
        node,
        x,
        y,
        edge: byRelation.get(node.key),
        anchor: right ? ("start" as const) : ("end" as const),
        dx: right ? 10 : -10,
      };
    }),
  );

  const palette: Record<string, string> = {
    subsidiary_of: "#2563eb", director_of: "#7c3aed", competes_with: "#dc2626",
    customer_of: "#059669", supplies_to: "#0891b2", operates_in: "#ca8a04",
    exposed_to_risk: "#e11d48", audited_by: "#475569", guides: "#0d9488",
    invests_in: "#9333ea", promoter_of: "#c2410c", operates_segment: "#4f46e5",
    sells_product: "#16a34a", acquired: "#b91c1c",
  };

  return (
    <svg viewBox={`0 0 ${width} ${height}`} className="h-auto w-full" role="img"
         aria-label="Knowledge graph of extracted relationships">
      {placed.map(({ node, x, y, edge }) => (
        <line
          key={`e-${node.key}`}
          x1={cx} y1={cy} x2={x} y2={y}
          stroke={palette[edge?.relation ?? ""] ?? "#94a3b8"}
          strokeWidth={1 + (edge?.confidence ?? 0.4) * 1.6}
          strokeOpacity={0.45}
        />
      ))}

      <circle cx={cx} cy={cy} r={30} fill="#0f172a" />
      <text x={cx} y={cy + 4} textAnchor="middle" className="fill-white text-[11px] font-semibold">
        Company
      </text>

      {placed.map(({ node, x, y, edge, anchor, dx }) => (
        <g key={node.key}>
          <title>
            {`${node.label} — ${RELATION_LABELS[edge?.relation ?? ""] ?? edge?.relation}`
              + ` (pages ${edge?.pages.join(", ")}, confidence ${((edge?.confidence ?? 0) * 100).toFixed(0)}%)`}
          </title>
          <circle
            cx={x} cy={y} r={5 + Math.min(4, node.degree)}
            fill={palette[edge?.relation ?? ""] ?? "#94a3b8"}
            fillOpacity={0.85}
          />
          <text
            x={x + dx} y={y + 3} textAnchor={anchor}
            className="fill-[var(--text-muted)] text-[9px]"
          >
            {node.label.length > 32 ? `${node.label.slice(0, 31)}…` : node.label}
          </text>
        </g>
      ))}
    </svg>
  );
}

export function RelationLegend({ relations }: { relations: Record<string, number> }) {
  const palette: Record<string, string> = {
    subsidiary_of: "#2563eb", director_of: "#7c3aed", competes_with: "#dc2626",
    customer_of: "#059669", supplies_to: "#0891b2", operates_in: "#ca8a04",
    exposed_to_risk: "#e11d48", audited_by: "#475569", guides: "#0d9488",
    invests_in: "#9333ea", promoter_of: "#c2410c", operates_segment: "#4f46e5",
    sells_product: "#16a34a", acquired: "#b91c1c",
  };
  return (
    <div className="flex flex-wrap gap-x-4 gap-y-1.5">
      {Object.entries(relations).map(([relation, count]) => (
        <span key={relation} className="inline-flex items-center gap-1.5 text-xs text-[var(--text-muted)]">
          <span
            className="inline-block h-2.5 w-2.5 rounded-full"
            style={{ background: palette[relation] ?? "#94a3b8" }}
          />
          {RELATION_LABELS[relation] ?? relation}
          <span className="font-mono text-[var(--text-muted)]">{count}</span>
        </span>
      ))}
    </div>
  );
}

/* -------------------------------------------------------------- pipeline */

export function PipelineTrace({ stages, timings }: {
  stages: string[]; timings?: Record<string, number> | null;
}) {
  const total = timings ? Object.values(timings).reduce((a, b) => a + b, 0) : 0;
  return (
    <div className="space-y-1">
      {stages.map((stage) => {
        const ms = timings?.[stage] ?? 0;
        const share = total > 0 ? ms / total : 0;
        return (
          <div key={stage} className="flex items-center gap-2 text-xs">
            <Layers className="h-3 w-3 shrink-0 text-[var(--text-muted)]" />
            <span className="w-24 shrink-0 capitalize text-[var(--text-muted)]">
              {stage}
            </span>
            <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-[var(--bg-subtle)]">
              <div className="h-full rounded-full bg-accent-500" style={{ width: `${share * 100}%` }} />
            </div>
            <span className="w-16 shrink-0 text-right font-mono text-[var(--text-muted)]">
              {ms > 0 ? `${ms.toFixed(1)} ms` : "—"}
            </span>
          </div>
        );
      })}
    </div>
  );
}

export function InfoNote({ children, tone = "info" }: { children: ReactNode; tone?: "info" | "warning" }) {
  return (
    <div
      className={cn( "flex items-start gap-2 rounded-lg border p-3 text-xs",
        tone === "warning"
          ? "border-warn/40 bg-warn/10 text-warn"
          : "border-[var(--border)] bg-[var(--bg-subtle)] text-[var(--text-muted)]",
      )}
    >
      {tone === "warning"
        ? <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
        : <Table2 className="mt-0.5 h-3.5 w-3.5 shrink-0" />}
      <div>{children}</div>
    </div>
  );
}
