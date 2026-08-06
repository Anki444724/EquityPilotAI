"use client";

/**
 * Phase 2 — Company Management.
 *
 * CRUD (add/edit/soft-delete/restore/permanent-delete), CSV & Excel import,
 * CSV & Excel export, a spreadsheet-style bulk editor, duplicate merging,
 * logo upload, and version history with rollback — all in the admin panel.
 */

import { adminApi } from "@/lib/api";
import type {
  CompanyAdmin, CompanyBulkEditResult, ImportResult, MergeResult,
} from "@/lib/types";
import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle, Building2, Download, FileSpreadsheet, History, Merge,
  Pencil, Plus, RefreshCw, Trash2, UploadCloud, ArchiveRestore, X,
} from "lucide-react";

import { Button, Pager, Select, TextInput, StatusPill, formatWhen } from "./primitives";
import { Card, CardBody, Skeleton } from "@/components/ui";

const SORTABLE = ["name", "ticker", "sector", "industry", "market_cap", "listing_date"];

const EMPTY_FORM = {
  name: "", ticker: "", exchange: "NSE", isin: "", bse_code: "",
  sector: "", industry: "", market_cap: "", face_value: "", website: "",
  description: "", ceo: "", employees: "", headquarters: "",
  listing_status: "active", index_membership: "", listing_date: "",
};

export default function CompaniesView() {
  const client = useQueryClient();
  const [page, setPage] = useState(1);
  const [search, setSearch] = useState("");
  const [sector, setSector] = useState("");
  const [industry, setIndustry] = useState("");
  const [exchange, setExchange] = useState("");
  const [listingStatus, setListingStatus] = useState("");
  const [sortBy, setSortBy] = useState("market_cap");
  const [order, setOrder] = useState("desc");
  const [includeDeleted, setIncludeDeleted] = useState(false);
  const [editing, setEditing] = useState<CompanyAdmin | "new" | null>(null);
  const [bulkOpen, setBulkOpen] = useState(false);
  const [versionsFor, setVersionsFor] = useState<CompanyAdmin | null>(null);
  const [mergeOpen, setMergeOpen] = useState(false);
  const [confirm, setConfirm] = useState<null | { kind: "soft" | "permanent"; c: CompanyAdmin }>(null);
  const [importMsg, setImportMsg] = useState<ImportResult | null>(null);
  const importInput = useRef<HTMLInputElement>(null);
  const importXlsxInput = useRef<HTMLInputElement>(null);

  const { data: filters } = useQuery({
    queryKey: ["admin", "companies", "filters"], queryFn: adminApi.companies.filters,
  });

  const { data, isLoading, error } = useQuery({
    queryKey: ["admin", "companies", page, search, sector, industry, exchange,
      listingStatus, sortBy, order, includeDeleted],
    queryFn: () => adminApi.companies.list({
      page, page_size: 25, search, sector, industry, exchange, listing_status: listingStatus,
      sort_by: sortBy, order, include_deleted: includeDeleted,
    }),
  });

  const invalidate = () => {
    client.invalidateQueries({ queryKey: ["admin", "companies"] });
    client.invalidateQueries({ queryKey: ["companies"] });
  };

  const softDelete = useMutation({
    mutationFn: (id: string) => adminApi.companies.softDelete(id),
    onSuccess: () => { invalidate(); setConfirm(null); },
  });
  const restore = useMutation({
    mutationFn: (id: string) => adminApi.companies.restore(id),
    onSuccess: invalidate,
  });
  const permanentDelete = useMutation({
    mutationFn: (id: string) => adminApi.companies.permanentDelete(id),
    onSuccess: () => { invalidate(); setConfirm(null); },
  });

  const toggleSort = (key: string) => {
    if (sortBy === key) setOrder(order === "asc" ? "desc" : "asc");
    else { setSortBy(key); setOrder("asc"); }
  };

  const onImportFile = (file: File | undefined, xlsx: boolean) => {
    if (!file) return;
    const fn = xlsx ? adminApi.companies.importXlsx : adminApi.companies.importCsv;
    fn(file).then((res) => { setImportMsg(res); invalidate(); });
  };

  return (
    <div className="space-y-3">
      {/* Toolbar */}
      <div className="flex flex-wrap items-center gap-2">
        <TextInput value={search} onChange={(v) => { setSearch(v); setPage(1); }}
                   placeholder="Search name, symbol, ISIN, sector…" className="max-w-xs" />
        <Select value={sector} onChange={(v) => { setSector(v); setPage(1); }}
                options={[{ value: "", label: "All sectors" }, ...(filters?.sectors ?? []).map((s) => ({ value: s, label: s }))]} />
        <Select value={industry} onChange={(v) => { setIndustry(v); setPage(1); }}
                options={[{ value: "", label: "All industries" }, ...(filters?.industries ?? []).map((s) => ({ value: s, label: s }))]} />
        <Select value={exchange} onChange={(v) => { setExchange(v); setPage(1); }}
                options={[{ value: "", label: "All exchanges" }, { value: "NSE", label: "NSE" }, { value: "BSE", label: "BSE" }, { value: "NSE/BSE", label: "NSE/BSE" }]} />
        <Select value={listingStatus} onChange={(v) => { setListingStatus(v); setPage(1); }}
                options={[{ value: "", label: "All listing statuses" }, { value: "active", label: "Active" }, { value: "delisted", label: "Delisted" }, { value: "suspended", label: "Suspended" }]} />
        <label className="flex items-center gap-1.5 text-xs text-[var(--text-muted)]">
          <input type="checkbox" checked={includeDeleted}
                 onChange={(e) => { setIncludeDeleted(e.target.checked); setPage(1); }} />
          Include deleted
        </label>
        <div className="flex-1" />
        <input ref={importInput} type="file" accept=".csv" className="hidden"
               onChange={(e) => { onImportFile(e.target.files?.[0], false); e.target.value = ""; }} />
        <input ref={importXlsxInput} type="file" accept=".xlsx" className="hidden"
               onChange={(e) => { onImportFile(e.target.files?.[0], true); e.target.value = ""; }} />
        <Button variant="ghost" onClick={() => importInput.current?.click()}>
          <UploadCloud className="h-3.5 w-3.5" /> CSV
        </Button>
        <Button variant="ghost" onClick={() => importXlsxInput.current?.click()}>
          <FileSpreadsheet className="h-3.5 w-3.5" /> XLSX
        </Button>
        <Button variant="ghost" onClick={() => adminApi.companies.exportCsv()}>
          <Download className="h-3.5 w-3.5" />
        </Button>
        <Button variant="ghost" onClick={() => adminApi.companies.exportXlsx()}>
          <FileSpreadsheet className="h-3.5 w-3.5" />
        </Button>
        <Button variant="ghost" onClick={() => setBulkOpen(true)}>
          <FileSpreadsheet className="h-3.5 w-3.5" /> Bulk edit
        </Button>
        <Button variant="ghost" onClick={() => setMergeOpen(true)}>
          <Merge className="h-3.5 w-3.5" /> Merge
        </Button>
        <Button variant="primary" onClick={() => setEditing("new")}>
          <Plus className="h-3.5 w-3.5" /> Add company
        </Button>
      </div>

      {importMsg && (
        <Card className="border-accent-500/30">
          <CardBody className="flex items-center justify-between text-xs">
            <span>
              Imported <b>{importMsg.imported}</b> · updated <b>{importMsg.updated}</b> ·
              skipped <b>{importMsg.skipped}</b>
              {importMsg.errors.length > 0 && ` · ${importMsg.errors.length} error(s)`}
            </span>
            <Button variant="ghost" onClick={() => setImportMsg(null)}><X className="h-3.5 w-3.5" /></Button>
          </CardBody>
        </Card>
      )}

      {editing && <CompanyForm company={editing === "new" ? null : editing}
        onClose={() => setEditing(null)} onDone={invalidate} />}
      {bulkOpen && <BulkEditor onClose={() => setBulkOpen(false)} onDone={invalidate} />}
      {versionsFor && <VersionHistory company={versionsFor} onClose={() => setVersionsFor(null)} onDone={invalidate} />}
      {mergeOpen && <MergeDialog onClose={() => setMergeOpen(false)} onDone={invalidate} />}

      {/* Table */}
      <Card>
        {isLoading ? <Skeleton className="h-96" /> : error ? (
          <CardBody className="text-xs text-loss">{(error as Error).message}</CardBody>
        ) : (
          <>
            <div className="scroll-x max-h-[62vh] overflow-auto">
              <table className="grid-table" style={{ minWidth: 1000 }}>
                <thead className="sticky top-0 z-10 bg-[var(--bg-elevated)]">
                  <tr>
                    {SORTABLE.map((key) => (
                      <th key={key} className="cursor-pointer select-none" onClick={() => toggleSort(key)}>
                        <span className="inline-flex items-center gap-1 capitalize">
                          {key.replace("_", " ")}
                          {sortBy === key && <span className="text-[0.625rem]">{order === "asc" ? "▲" : "▼"}</span>}
                        </span>
                      </th>
                    ))}
                    <th>Symbol</th><th>ISIN</th><th>BSE</th><th>Face</th><th>Status</th><th />
                  </tr>
                </thead>
                <tbody>
                  {(data?.results ?? []).map((c) => (
                    <tr key={c.id} className={c.deleted_at ? "opacity-50" : ""}>
                      <td className="sticky-col font-medium">{c.name}</td>
                      <td className="num">{c.ticker}</td>
                      <td>{c.sector ?? "—"}</td>
                      <td>{c.industry ?? "—"}</td>
                      <td className="num">{c.market_cap?.toLocaleString("en-IN") ?? "—"}</td>
                      <td className="num">{c.listing_date ? new Date(c.listing_date).toLocaleDateString("en-IN") : "—"}</td>
                      <td className="num">{c.ticker}</td>
                      <td className="num">{c.isin ?? "—"}</td>
                      <td className="num">{c.bse_code ?? "—"}</td>
                      <td className="num">{c.face_value ?? "—"}</td>
                      <td>
                        {c.deleted_at
                          ? <StatusPill status="deleted" />
                          : <StatusPill status={c.listing_status === "active" ? "ok" : "warning"} />}
                      </td>
                      <td>
                        <div className="flex justify-end gap-1">
                          <Button variant="ghost" onClick={() => setEditing(c)}><Pencil className="h-3.5 w-3.5" /></Button>
                          <Button variant="ghost" onClick={() => setVersionsFor(c)}><History className="h-3.5 w-3.5" /></Button>
                          {c.deleted_at ? (
                            <>
                              <Button variant="ghost" onClick={() => restore.mutate(c.id)}><ArchiveRestore className="h-3.5 w-3.5" /></Button>
                              <Button variant="danger" onClick={() => setConfirm({ kind: "permanent", c })}><Trash2 className="h-3.5 w-3.5" /></Button>
                            </>
                          ) : (
                            <Button variant="danger" onClick={() => setConfirm({ kind: "soft", c })}><Trash2 className="h-3.5 w-3.5" /></Button>
                          )}
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <Pager page={page} pageSize={25} total={data?.total ?? 0} onChange={setPage} />
          </>
        )}
      </Card>

      {confirm && (
        <ConfirmDelete kind={confirm.kind} company={confirm.c}
          busy={softDelete.isPending || permanentDelete.isPending}
          onCancel={() => setConfirm(null)}
          onConfirm={() => confirm.kind === "soft" ? softDelete.mutate(confirm.c.id) : permanentDelete.mutate(confirm.c.id)} />
      )}
    </div>
  );
}

/* ==================================================================== */
function CompanyForm({
  company, onClose, onDone,
}: { company: CompanyAdmin | null; onClose: () => void; onDone: () => void }) {
  const [form, setForm] = useState<Record<string, string>>(() => {
    if (company) {
      return {
        name: company.name ?? "", ticker: company.ticker ?? "", exchange: company.exchange ?? "NSE",
        isin: company.isin ?? "", bse_code: company.bse_code ?? "", sector: company.sector ?? "",
        industry: company.industry ?? "", market_cap: company.market_cap?.toString() ?? "",
        face_value: company.face_value?.toString() ?? "", website: company.website ?? "",
        description: company.description ?? "", ceo: company.ceo ?? "",
        employees: company.employees?.toString() ?? "", headquarters: company.headquarters ?? "",
        listing_status: company.listing_status ?? "active",
        index_membership: company.index_membership ?? "",
        listing_date: company.listing_date ? company.listing_date.slice(0, 10) : "",
      };
    }
    return { ...EMPTY_FORM };
  });
  const set = (k: string, v: string) => setForm((f) => ({ ...f, [k]: v }));
  const [err, setErr] = useState<string | null>(null);

  const mutation = useMutation({
    mutationFn: () => {
      const body: Record<string, unknown> = {};
      for (const [k, v] of Object.entries(form)) {
        if (k === "market_cap" || k === "face_value") body[k] = v ? Number(v) : null;
        else if (k === "employees") body[k] = v ? Number(v) : null;
        else body[k] = v || null;
      }
      body["exchange"] = (body["exchange"] as string) ?? "NSE";
      return company
        ? adminApi.companies.update(company.id, body)
        : adminApi.companies.create(body);
    },
    onSuccess: () => { onDone(); onClose(); },
    onError: (e: Error) => setErr(e.message),
  });

  const fields: [string, string, "text" | "number" | "select", string[]?][] = [
    ["name", "Company Name", "text"],
    ["ticker", "NSE Symbol", "text"],
    ["exchange", "Exchange", "select", ["NSE", "BSE", "NSE/BSE", "NASDAQ", "NYSE"]],
    ["isin", "ISIN", "text"],
    ["bse_code", "BSE Symbol", "text"],
    ["sector", "Sector", "text"],
    ["industry", "Industry", "text"],
    ["market_cap", "Market Cap (₹ cr)", "number"],
    ["face_value", "Face Value", "number"],
    ["listing_date", "Listing Date", "text"],
    ["website", "Website", "text"],
    ["ceo", "CEO", "text"],
    ["employees", "Employees", "number"],
    ["headquarters", "Headquarters", "text"],
    ["index_membership", "Index Membership", "text"],
    ["listing_status", "Listing Status", "select", ["active", "delisted", "suspended"]],
  ];

  return (
    <div className="fixed inset-0 z-[80] flex items-start justify-center overflow-y-auto bg-black/50 p-4">
      <div className="mt-4 w-full max-w-2xl rounded-xl border border-[var(--border)] bg-[var(--bg-elevated)] p-5">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2 font-semibold text-[var(--text)]">
            <Building2 className="h-4 w-4 text-accent-500" />
            {company ? `Edit ${company.ticker}` : "Add company"}
          </div>
          <button onClick={onClose} aria-label="Close"><X className="h-4 w-4 text-[var(--text-muted)]" /></button>
        </div>
        <div className="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-2">
          {fields.map(([key, label, type, options]) => (
            <label key={key} className="block">
              <span className="mb-1 block text-xs text-[var(--text-muted)]">{label}</span>
              {type === "select" ? (
                <select value={form[key]} onChange={(e) => set(key, e.target.value)}
                        className="w-full rounded border border-[var(--border)] bg-[var(--bg)] px-3 py-1.5 text-sm">
                  {(options ?? []).map((o) => <option key={o} value={o}>{o}</option>)}
                </select>
              ) : (
                <input type={type} value={form[key]} onChange={(e) => set(key, e.target.value)}
                       className="w-full rounded border border-[var(--border)] bg-[var(--bg)] px-3 py-1.5 text-sm" />
              )}
            </label>
          ))}
          <label className="block sm:col-span-2">
            <span className="mb-1 block text-xs text-[var(--text-muted)]">Description</span>
            <textarea value={form.description} onChange={(e) => set("description", e.target.value)}
                      rows={3} className="w-full rounded border border-[var(--border)] bg-[var(--bg)] px-3 py-1.5 text-sm" />
          </label>
        </div>
        {err && <div className="mt-3 rounded border border-loss/30 bg-loss/5 px-3 py-2 text-xs text-loss">{err}</div>}
        <div className="mt-5 flex justify-end gap-2">
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" disabled={!form.name || !form.ticker || mutation.isPending}
                  onClick={() => mutation.mutate()}>
            {mutation.isPending ? "Saving…" : company ? "Save changes" : "Create company"}
          </Button>
        </div>
      </div>
    </div>
  );
}

/* ==================================================================== */
function BulkEditor({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const [rows, setRows] = useState<Record<string, string>[]>([
    { ticker: "", name: "", sector: "", industry: "", market_cap: "", face_value: "", website: "", description: "" },
    { ticker: "", name: "", sector: "", industry: "", market_cap: "", face_value: "", website: "", description: "" },
  ]);
  const [result, setResult] = useState<CompanyBulkEditResult | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const mutation = useMutation({
    mutationFn: () => adminApi.companies.bulkEdit(
      rows.map((r) => Object.fromEntries(
        Object.entries(r).filter(([, v]) => v !== "")
      )).filter((r) => r.ticker || r.name),
    ),
    onSuccess: (r) => { setResult(r); onDone(); },
    onError: (e: Error) => setErr(e.message),
  });

  const cols = ["ticker", "name", "sector", "industry", "market_cap", "face_value", "website", "description"];
  const setCell = (ri: number, col: string, v: string) =>
    setRows((rs) => rs.map((r, i) => (i === ri ? { ...r, [col]: v } : r)));
  const addRow = () => setRows((rs) => [...rs, Object.fromEntries(cols.map((c) => [c, ""]))]);

  return (
    <div className="fixed inset-0 z-[80] flex items-start justify-center overflow-y-auto bg-black/50 p-4">
      <div className="mt-4 w-full max-w-5xl rounded-xl border border-[var(--border)] bg-[var(--bg-elevated)] p-5">
        <div className="flex items-center justify-between">
          <div className="font-semibold text-[var(--text)]">Bulk editor — spreadsheet</div>
          <button onClick={onClose} aria-label="Close"><X className="h-4 w-4 text-[var(--text-muted)]" /></button>
        </div>
        <p className="mt-1 text-xs text-[var(--text-muted)]">
          Edit rows in place. A row with a matching ticker updates; a row with a new ticker and a name creates.
        </p>
        <div className="mt-3 max-h-[55vh] overflow-auto">
          <table className="w-full text-xs">
            <thead className="sticky top-0 bg-[var(--bg-elevated)]">
              <tr>{cols.map((c) => <th key={c} className="border border-[var(--border)] px-2 py-1.5 capitalize">{c.replace("_", " ")}</th>)}</tr>
            </thead>
            <tbody>
              {rows.map((row, ri) => (
                <tr key={ri}>
                  {cols.map((col) => (
                    <td key={col} className="border border-[var(--border)]">
                      <input value={row[col]} onChange={(e) => setCell(ri, col, e.target.value)}
                             className="w-full bg-transparent px-2 py-1.5 outline-none" />
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="mt-2 flex gap-2">
          <Button variant="ghost" onClick={addRow}><Plus className="h-3.5 w-3.5" /> Add row</Button>
          <div className="flex-1" />
          {result && <span className="text-xs text-gain">Updated {result.updated} · created {result.created}</span>}
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" disabled={mutation.isPending} onClick={() => mutation.mutate()}>
            {mutation.isPending ? "Applying…" : "Apply edits"}
          </Button>
        </div>
        {err && <div className="mt-2 rounded border border-loss/30 bg-loss/5 px-3 py-2 text-xs text-loss">{err}</div>}
      </div>
    </div>
  );
}

/* ==================================================================== */
function VersionHistory({ company, onClose, onDone }:
  { company: CompanyAdmin; onClose: () => void; onDone: () => void }) {
  const { data, isLoading } = useQuery({
    queryKey: ["admin", "companies", company.id, "versions"],
    queryFn: () => adminApi.companies.versions(company.id),
  });
  const rollback = useMutation({
    mutationFn: (version: number) => adminApi.companies.rollback(company.id, version),
    onSuccess: () => { onDone(); onClose(); },
  });

  return (
    <div className="fixed inset-0 z-[80] flex items-start justify-center overflow-y-auto bg-black/50 p-4">
      <div className="mt-4 w-full max-w-2xl rounded-xl border border-[var(--border)] bg-[var(--bg-elevated)] p-5">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2 font-semibold text-[var(--text)]">
            <History className="h-4 w-4 text-accent-500" />
            Version history — {company.ticker}
          </div>
          <button onClick={onClose} aria-label="Close"><X className="h-4 w-4 text-[var(--text-muted)]" /></button>
        </div>
        <div className="mt-3 space-y-2">
          {isLoading ? <Skeleton className="h-40" /> : (data ?? []).map((v) => (
            <div key={v.id} className="rounded border border-[var(--border)] p-3">
              <div className="flex items-center justify-between">
                <div className="text-xs">
                  <span className="font-medium text-[var(--text)]">v{v.version}</span>
                  <span className="mx-2 text-[var(--text-muted)]">· {v.change_type}</span>
                  <span className="text-[var(--text-muted)]">by {v.actor_email ?? "system"}</span>
                </div>
                <div className="flex items-center gap-2">
                  <span className="text-[0.625rem] text-[var(--text-muted)]">{formatWhen(v.created_at)}</span>
                  <Button variant="ghost" disabled={rollback.isPending} onClick={() => rollback.mutate(v.version)}>
                    <RefreshCw className="h-3 w-3" /> Rollback
                  </Button>
                </div>
              </div>
              {v.changes && Object.entries(v.changes).length > 0 && (
                <div className="mt-1 text-[0.625rem] text-[var(--text-muted)]">
                  {Object.entries(v.changes).map(([field, c]) => (
                    <div key={field} className="truncate">
                      <span className="text-[var(--text)]">{field}</span>: {String(c.from)} → {String(c.to)}
                    </div>
                  ))}
                </div>
              )}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

/* ==================================================================== */
function MergeDialog({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const [keepId, setKeepId] = useState("");
  const [deleteIds, setDeleteIds] = useState("");
  const [result, setResult] = useState<MergeResult | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const { data } = useQuery({
    queryKey: ["admin", "companies", "merge-list"],
    queryFn: () => adminApi.companies.list({ page: 1, page_size: 100, sort_by: "name", order: "asc" }),
  });

  const mutation = useMutation({
    mutationFn: () => adminApi.companies.merge(keepId, deleteIds.split(",").map((s) => s.trim()).filter(Boolean)),
    onSuccess: (r) => { setResult(r); onDone(); },
    onError: (e: Error) => setErr(e.message),
  });

  const options = (data?.results ?? []).map((c) => ({ value: c.id, label: `${c.ticker} — ${c.name}` }));

  return (
    <div className="fixed inset-0 z-[80] flex items-start justify-center overflow-y-auto bg-black/50 p-4">
      <div className="mt-4 w-full max-w-lg rounded-xl border border-[var(--border)] bg-[var(--bg-elevated)] p-5">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2 font-semibold text-[var(--text)]">
            <Merge className="h-4 w-4 text-accent-500" /> Merge duplicates
          </div>
          <button onClick={onClose} aria-label="Close"><X className="h-4 w-4 text-[var(--text-muted)]" /></button>
        </div>
        <div className="mt-3 space-y-3">
          <label className="block">
            <span className="mb-1 block text-xs text-[var(--text-muted)]">Keep company</span>
            <Select value={keepId} onChange={setKeepId} options={options} />
          </label>
          <label className="block">
            <span className="mb-1 block text-xs text-[var(--text-muted)]">
              Delete these (comma-separated IDs or tickers)
            </span>
            <TextInput value={deleteIds} onChange={setDeleteIds} placeholder="id1,id2" />
          </label>
          {result && (
            <div className="rounded border border-gain/30 bg-gain/5 px-3 py-2 text-xs text-gain">
              Merged {result.removed_count} duplicate(s) into {result.kept_ticker}.
            </div>
          )}
          {err && <div className="rounded border border-loss/30 bg-loss/5 px-3 py-2 text-xs text-loss">{err}</div>}
          <div className="flex justify-end gap-2">
            <Button onClick={onClose}>Cancel</Button>
            <Button variant="danger" disabled={!keepId || mutation.isPending} onClick={() => mutation.mutate()}>
              {mutation.isPending ? "Merging…" : "Merge"}
            </Button>
          </div>
        </div>
      </div>
    </div>
  );
}

/* ==================================================================== */
function ConfirmDelete({ kind, company, busy, onCancel, onConfirm }:
  { kind: "soft" | "permanent"; company: CompanyAdmin; busy: boolean;
    onCancel: () => void; onConfirm: () => void }) {
  return (
    <div className="fixed inset-0 z-[90] flex items-center justify-center bg-black/50 p-4">
      <div className="w-full max-w-md rounded-xl border border-[var(--border)] bg-[var(--bg-elevated)] p-5">
        <div className="flex items-center gap-2 font-semibold text-[var(--text)]">
          <AlertTriangle className={kind === "permanent" ? "h-4 w-4 text-loss" : "h-4 w-4 text-warn"} />
          {kind === "permanent" ? "Permanently delete this company?" : "Soft-delete this company?"}
        </div>
        <p className="mt-3 text-xs text-[var(--text-muted)]">
          {kind === "soft"
            ? <>Move <b>{company.ticker} — {company.name}</b> to the recycle bin? It can be restored later.</>
            : <>Permanently delete <b>{company.ticker} — {company.name}</b> and all its financial facts? This is irreversible.</>}
        </p>
        <div className="mt-5 flex justify-end gap-2">
          <Button onClick={onCancel}>Cancel</Button>
          <Button variant="danger" disabled={busy} onClick={onConfirm}>
            {busy ? "Working…" : kind === "soft" ? "Move to bin" : "Delete permanently"}
          </Button>
        </div>
      </div>
    </div>
  );
}
