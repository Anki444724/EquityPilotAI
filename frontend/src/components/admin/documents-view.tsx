"use client";

/**
 * Phase 6 — Enterprise Document Intelligence Center.
 *
 * Approval workflow (uploaded → AI extracted → pending review → approved →
 * published), OCR/ingestion status, version history & comparison, RAG index
 * stats, and in-document search with highlighted matches.
 */

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Check, FileText, GitCompare, ListChecks, Radio, RefreshCw,
  Search as SearchIcon, Trash2, X,
} from "lucide-react";

import { docAdminApi } from "@/lib/api";
import type { DocAdmin } from "@/lib/types";
import { Button, Pager, Select, StatusPill, TextInput, formatWhen } from "./primitives";
import { Card, CardBody, CardHeader, Skeleton } from "@/components/ui";

type Tab = "docs" | "workflow" | "compare" | "rag" | "search";

const DOC_TYPES = [
  "annual", "quarterly", "presentation", "concall", "exchange_filing",
  "credit_rating", "announcement",
];

export default function DocumentsView() {
  const [tab, setTab] = useState<Tab>("docs");
  return (
    <div className="space-y-3">
      <div className="flex gap-1 overflow-x-auto pb-1">
        {[
          { key: "docs" as Tab, label: "Documents", icon: FileText },
          { key: "workflow" as Tab, label: "Approval Workflow", icon: ListChecks },
          { key: "compare" as Tab, label: "Version Compare", icon: GitCompare },
          { key: "rag" as Tab, label: "RAG", icon: Radio },
          { key: "search" as Tab, label: "Search", icon: SearchIcon },
        ].map((t) => {
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
      {tab === "docs" && <DocsTab />}
      {tab === "workflow" && <WorkflowTab />}
      {tab === "compare" && <CompareTab />}
      {tab === "rag" && <RagTab />}
      {tab === "search" && <SearchTab />}
    </div>
  );
}

/* ==================================================================== */
function DocsTab() {
  const client = useQueryClient();
  const [page, setPage] = useState(1);
  const [docType, setDocType] = useState("");
  const [approval, setApproval] = useState("");
  const [search, setSearch] = useState("");

  const { data, isLoading } = useQuery({
    queryKey: ["admin", "docs", page, docType, approval, search],
    queryFn: () => docAdminApi.list({
      page, page_size: 25, doc_type: docType, approval_status: approval, search,
    }),
  });

  const del = useMutation({
    mutationFn: (id: number) => docAdminApi.delete(id),
    onSuccess: () => client.invalidateQueries({ queryKey: ["admin", "docs"] }),
  });

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <TextInput value={search} onChange={(v) => { setSearch(v); setPage(1); }}
                   placeholder="Search title / filename…" className="max-w-xs" />
        <Select value={docType} onChange={(v) => { setDocType(v); setPage(1); }}
                options={[{ value: "", label: "All types" }, ...DOC_TYPES.map((t) => ({ value: t, label: t.replace("_", " ") }))]} />
        <Select value={approval} onChange={(v) => { setApproval(v); setPage(1); }}
                options={[
                  { value: "", label: "All approval states" },
                  { value: "uploaded", label: "Uploaded" },
                  { value: "ai_extracted", label: "AI Extracted" },
                  { value: "pending_review", label: "Pending Review" },
                  { value: "approved", label: "Approved" },
                  { value: "published", label: "Published" },
                ]} />
      </div>
      <Card>
        {isLoading ? <Skeleton className="h-64" /> : (
          <div className="scroll-x max-h-[60vh] overflow-auto">
            <table className="w-full text-xs">
              <thead className="sticky top-0 bg-[var(--bg-elevated)]">
                <tr><th className="px-3 py-2 text-left text-[var(--text-muted)]">Document</th>
                  <th className="px-3 py-2 text-left text-[var(--text-muted)]">Type</th>
                  <th className="px-3 py-2 text-left text-[var(--text-muted)]">Approval</th>
                  <th className="px-3 py-2 text-left text-[var(--text-muted)]">Ingestion</th>
                  <th className="px-3 py-2 text-right text-[var(--text-muted)]">Pages</th>
                  <th className="px-3 py-2 text-right text-[var(--text-muted)]">Chunks</th>
                  <th className="px-3 py-2 text-right text-[var(--text-muted)]">Facts</th>
                  <th /></tr></thead>
              <tbody>
                {(data?.items ?? []).map((d) => (
                  <tr key={d.id} className="border-t border-[var(--border)]">
                    <td className="px-3 py-2">
                      <div className="font-medium text-[var(--text)]">{d.title ?? d.filename}</div>
                      <div className="text-[0.625rem] text-[var(--text-muted)]">v{d.version} · {formatWhen(d.processed_at)}</div>
                    </td>
                    <td className="px-3 py-2 text-[var(--text-muted)]">{d.doc_type.replace("_", " ")}</td>
                    <td className="px-3 py-2"><StatusPill status={d.approval_status} /></td>
                    <td className="px-3 py-2">{d.used_ocr ? <span className="text-[var(--text-muted)]">OCR ✓</span> : <span className="text-[var(--text-muted)]">{d.status}</span>}</td>
                    <td className="num px-3 py-2 text-right">{d.page_count}</td>
                    <td className="num px-3 py-2 text-right">{d.chunk_count}</td>
                    <td className="num px-3 py-2 text-right">{d.fact_count}</td>
                    <td className="px-3 py-2 text-right"><Button variant="danger" onClick={() => del.mutate(d.id)}><Trash2 className="h-3 w-3" /></Button></td>
                  </tr>
                ))}
                {(data?.items ?? []).length === 0 && <tr><td colSpan={8} className="py-4 text-center text-[var(--text-muted)]">No documents. Upload via the Documents module.</td></tr>}
              </tbody>
            </table>
          </div>
        )}
        <Pager page={page} pageSize={25} total={data?.total ?? 0} onChange={setPage} />
      </Card>
    </div>
  );
}

/* ==================================================================== */
function WorkflowTab() {
  const client = useQueryClient();
  const [page, setPage] = useState(1);
  const [approval, setApproval] = useState("pending_review");
  const [note, setNote] = useState("");
  const [noteFor, setNoteFor] = useState<number | null>(null);

  const { data, isLoading } = useQuery({
    queryKey: ["admin", "docs", "workflow", page, approval],
    queryFn: () => docAdminApi.list({ page, page_size: 25, approval_status: approval }),
  });

  const invalidate = () => client.invalidateQueries({ queryKey: ["admin", "docs"] });

  const approve = useMutation({ mutationFn: (id: number) => docAdminApi.approve(id), onSuccess: invalidate });
  const publish = useMutation({ mutationFn: (id: number) => docAdminApi.publish(id), onSuccess: invalidate });
  const reject = useMutation({
    mutationFn: (id: number) => docAdminApi.reject(id, note || undefined),
    onSuccess: () => { invalidate(); setNoteFor(null); setNote(""); },
  });

  const steps = ["uploaded", "ai_extracted", "pending_review", "approved", "published"];

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <Select value={approval} onChange={(v) => { setApproval(v); setPage(1); }}
                options={steps.map((s) => ({ value: s, label: s.replace("_", " ") }))} />
        <div className="flex-1" />
        <span className="text-xs text-[var(--text-muted)]">Uploaded → AI extracted → Pending review → Approved → Published</span>
      </div>
      <Card>
        {isLoading ? <Skeleton className="h-48" /> : (
          <table className="w-full text-xs">
            <thead><tr><th className="px-3 py-2 text-left text-[var(--text-muted)]">Document</th>
              <th className="px-3 py-2 text-left text-[var(--text-muted)]">Type</th>
              <th className="px-3 py-2 text-left text-[var(--text-muted)]">State</th>
              <th className="px-3 py-2 text-left text-[var(--text-muted)]">Reviewer</th>
              <th className="px-3 py-2 text-right text-[var(--text-muted)]">Actions</th></tr></thead>
            <tbody>
              {(data?.items ?? []).map((d) => (
                <tr key={d.id} className="border-t border-[var(--border)]">
                  <td className="px-3 py-2 font-medium text-[var(--text)]">{d.title ?? d.filename}</td>
                  <td className="px-3 py-2 text-[var(--text-muted)]">{d.doc_type}</td>
                  <td className="px-3 py-2"><StatusPill status={d.approval_status} /></td>
                  <td className="px-3 py-2 text-[var(--text-muted)]">{d.approval_reviewer ?? "—"}</td>
                  <td className="px-3 py-2">
                    <div className="flex justify-end gap-1">
                      {(d.approval_status === "ai_extracted" || d.approval_status === "pending_review") && (
                        <>
                          <Button variant="primary" onClick={() => approve.mutate(d.id)}><Check className="h-3 w-3" /> Approve</Button>
                          <Button variant="danger" onClick={() => setNoteFor(d.id)}><X className="h-3 w-3" /> Reject</Button>
                        </>
                      )}
                      {d.approval_status === "approved" && (
                        <Button variant="primary" onClick={() => publish.mutate(d.id)}><Check className="h-3 w-3" /> Publish</Button>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
              {(data?.items ?? []).length === 0 && <tr><td colSpan={5} className="py-4 text-center text-[var(--text-muted)]">No documents in this state.</td></tr>}
            </tbody>
          </table>
        )}
      </Card>
      {noteFor !== null && (
        <div className="fixed inset-0 z-[80] flex items-center justify-center bg-black/50 p-4">
          <div className="w-full max-w-md rounded-xl border border-[var(--border)] bg-[var(--bg-elevated)] p-5">
            <div className="font-semibold text-[var(--text)]">Reject document</div>
            <textarea value={note} onChange={(e) => setNote(e.target.value)} rows={3} placeholder="Reason for rejection…"
                      className="mt-3 w-full rounded border border-[var(--border)] bg-[var(--bg)] px-3 py-1.5 text-sm" />
            <div className="mt-4 flex justify-end gap-2">
              <Button onClick={() => { setNoteFor(null); setNote(""); }}>Cancel</Button>
              <Button variant="danger" disabled={reject.isPending} onClick={() => reject.mutate(noteFor)}>Reject</Button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

/* ==================================================================== */
function CompareTab() {
  const [docId, setDocId] = useState("");
  const [otherId, setOtherId] = useState("");
  const { data: list } = useQuery({
    queryKey: ["admin", "docs", "compare-list"],
    queryFn: () => docAdminApi.list({ page: 1, page_size: 50, search: "" }),
  });
  const opts = (list?.items ?? []).map((d: DocAdmin) => ({ value: String(d.id), label: `v${d.version} ${d.title ?? d.filename}` }));

  const compare = useQuery({
    queryKey: ["admin", "docs", "compare", docId, otherId],
    queryFn: () => docAdminApi.compare(Number(docId), Number(otherId)),
    enabled: !!docId && !!otherId,
  });

  return (
    <div className="space-y-3">
      <div className="grid grid-cols-2 gap-3">
        <label className="block"><span className="mb-1 block text-xs text-[var(--text-muted)]">Old report</span>
          <Select value={docId} onChange={setDocId} options={opts} /></label>
        <label className="block"><span className="mb-1 block text-xs text-[var(--text-muted)]">New report</span>
          <Select value={otherId} onChange={setOtherId} options={opts} /></label>
      </div>
      {compare.data && (
        <Card>
          <CardHeader title={`Compare — v${compare.data.old.version} → v${compare.data.new.version}`} />
          <CardBody className="space-y-2">
            <div className="text-xs text-[var(--text-muted)]">
              <b className="text-[var(--text)]">{compare.data.changed_count}</b> field(s) changed ·
              old {compare.data.old_fact_count} facts → new {compare.data.new_fact_count} facts
            </div>
            <div className="scroll-x"><table className="w-full text-xs">
              <thead><tr><th className="px-3 py-2 text-left text-[var(--text-muted)]">Field</th>
                <th className="px-3 py-2 text-left text-[var(--text-muted)]">Old</th>
                <th className="px-3 py-2 text-left text-[var(--text-muted)]">New</th></tr></thead>
              <tbody>
                {compare.data.changed_fields.map((c) => (
                  <tr key={c.field} className="border-t border-[var(--border)]">
                    <td className="px-3 py-2 font-medium text-[var(--text)]">{c.field}</td>
                    <td className="px-3 py-2 text-[var(--text-muted)] line-through">{String(c.old)}</td>
                    <td className="px-3 py-2 font-medium text-gain">{String(c.new)}</td>
                  </tr>
                ))}
                {compare.data.changed_fields.length === 0 && <tr><td colSpan={3} className="py-4 text-center text-[var(--text-muted)]">No changes between versions.</td></tr>}
              </tbody>
            </table></div>
          </CardBody>
        </Card>
      )}
    </div>
  );
}

/* ==================================================================== */
function RagTab() {
  const client = useQueryClient();
  const { data } = useQuery({ queryKey: ["admin", "docs", "rag"], queryFn: () => docAdminApi.ragStats() });
  const cards = data ? [
    { label: "Documents", value: data.documents },
    { label: "Chunks", value: data.chunks },
    { label: "Embeddings", value: data.embeddings },
    { label: "Vectors", value: data.vector_count },
  ] : [];
  return (
    <div className="space-y-3">
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        {cards.map((c) => (
          <Card key={c.label}><CardBody>
            <div className="text-[0.625rem] uppercase tracking-wider text-[var(--text-muted)]">{c.label}</div>
            <div className="mt-1 text-xl font-semibold text-[var(--text)]">{c.value}</div>
          </CardBody></Card>
        ))}
      </div>
      <Card>
        <CardBody className="flex flex-wrap gap-2">
          <Button variant="primary" onClick={() => client.invalidateQueries({ queryKey: ["admin", "docs"] })}>
            <RefreshCw className="h-3.5 w-3.5" /> Refresh index
          </Button>
        </CardBody>
      </Card>
    </div>
  );
}

/* ==================================================================== */
function SearchTab() {
  const [q, setQ] = useState("");
  const { data } = useQuery({
    queryKey: ["admin", "docs", "search", q],
    queryFn: () => docAdminApi.search(q),
    enabled: q.trim().length > 0,
  });
  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <TextInput value={q} onChange={setQ} placeholder="Search inside documents…" className="max-w-sm" />
        <SearchIcon className="h-4 w-4 text-[var(--text-muted)]" />
      </div>
      <div className="space-y-2">
        {(data?.results ?? []).map((r) => (
          <Card key={r.chunk_id}>
            <CardBody className="space-y-1">
              <div className="flex items-center justify-between text-xs">
                <span className="font-medium text-[var(--text)]">{r.title}</span>
                <span className="text-[var(--text-muted)]">page {r.page} · score {r.score}</span>
              </div>
              <div className="text-xs leading-relaxed text-[var(--text-muted)]" dangerouslySetInnerHTML={{ __html: r.text }} />
            </CardBody>
          </Card>
        ))}
        {q && (data?.results ?? []).length === 0 && (
          <Card><CardBody className="text-xs text-[var(--text-muted)]">No matches.</CardBody></Card>
        )}
      </div>
    </div>
  );
}
