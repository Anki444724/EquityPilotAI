"use client";

/**
 * Recycle Bin — review, restore or permanently purge soft-deleted resources.
 *
 * Every destructive action requires explicit confirmation. Restoring and
 * purging are audited on the backend.
 */

import { adminApi } from "@/lib/api";
import type { RecycleBinEntry } from "@/lib/types";
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, ArchiveRestore, Recycle, Trash2, X } from "lucide-react";

import { Button, DataTable, Pager, Select, StatusPill, TextInput, formatWhen } from "./primitives";
import { Card, CardBody, CardHeader, Skeleton } from "@/components/ui";

function ConfirmModal({
  title, body, confirmLabel, danger, onConfirm, onCancel, busy,
}: {
  title: string; body: React.ReactNode; confirmLabel: string;
  danger?: boolean; busy?: boolean;
  onConfirm: () => void; onCancel: () => void;
}) {
  return (
    <div className="fixed inset-0 z-[80] flex items-center justify-center bg-black/50 p-4">
      <div className="w-full max-w-md rounded-xl border border-[var(--border)] bg-[var(--bg-elevated)] p-5">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2 font-semibold text-[var(--text)]">
            <AlertTriangle className={danger ? "h-4 w-4 text-loss" : "h-4 w-4 text-warn"} />
            {title}
          </div>
          <button onClick={onCancel} aria-label="Close"><X className="h-4 w-4 text-[var(--text-muted)]" /></button>
        </div>
        <div className="mt-3 text-xs leading-relaxed text-[var(--text-muted)]">{body}</div>
        <div className="mt-5 flex justify-end gap-2">
          <Button onClick={onCancel}>Cancel</Button>
          <Button variant={danger ? "danger" : "primary"} disabled={busy} onClick={onConfirm}>
            {busy ? "Working…" : confirmLabel}
          </Button>
        </div>
      </div>
    </div>
  );
}

export default function RecycleBinView() {
  const client = useQueryClient();
  const [page, setPage] = useState(1);
  const [status, setStatus] = useState("");
  const [search, setSearch] = useState("");
  const [softForm, setSoftForm] = useState(false);
  const [confirm, setConfirm] = useState<null | {
    kind: "restore" | "purge" | "purge_all"; entry?: RecycleBinEntry;
  }>(null);

  const { data, isLoading, error } = useQuery({
    queryKey: ["admin", "recycle", page, status, search],
    queryFn: () => adminApi.recycleBin({ page, page_size: 25, status, search }),
  });

  const invalidate = () => client.invalidateQueries({ queryKey: ["admin", "recycle"] });

  const restore = useMutation({
    mutationFn: (id: number) => adminApi.recycleRestore(id),
    onSuccess: () => { invalidate(); setConfirm(null); },
  });
  const purge = useMutation({
    mutationFn: (id: number) => adminApi.recyclePurge(id),
    onSuccess: () => { invalidate(); setConfirm(null); },
  });
  const purgeAll = useMutation({
    mutationFn: () => adminApi.recyclePurgeAll(),
    onSuccess: () => { invalidate(); setConfirm(null); },
  });

  const busy = restore.isPending || purge.isPending || purgeAll.isPending;

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <TextInput value={search} onChange={(v) => { setSearch(v); setPage(1); }}
                   placeholder="Search name or id…" className="max-w-xs" />
        <Select value={status} onChange={(v) => { setStatus(v); setPage(1); }}
                options={[
                  { value: "", label: "All entries" },
                  { value: "active", label: "Active" },
                  { value: "restored", label: "Restored" },
                ]} />
        <div className="flex-1" />
        <Button variant="primary" onClick={() => setSoftForm(true)}>
          <Recycle className="h-3.5 w-3.5" /> Soft delete…
        </Button>
        {(data?.total ?? 0) > 0 && (
          <Button variant="danger" onClick={() => setConfirm({ kind: "purge_all" })}>
            <Trash2 className="h-3.5 w-3.5" /> Empty bin
          </Button>
        )}
      </div>

      {softForm && <SoftDeleteForm onClose={() => setSoftForm(false)} onDone={invalidate} />}

      <Card>
        {isLoading ? <Skeleton className="h-64" /> : error ? (
          <CardBody className="text-xs text-loss">{(error as Error).message}</CardBody>
        ) : (
          <DataTable<RecycleBinEntry>
            rowKey={(r) => r.id}
            rows={data?.items ?? []}
            empty="The recycle bin is empty."
            columns={[
              {
                key: "name", header: "Resource",
                render: (r) => (
                  <div className="min-w-0">
                    <div className="truncate font-medium text-[var(--text)]">
                      {r.display_name ?? r.resource_id}
                    </div>
                    <div className="truncate text-xs text-[var(--text-muted)]">
                      {r.resource_type} · {r.resource_id}
                    </div>
                  </div>
                ),
              },
              {
                key: "deleted", header: "Deleted by", width: "170px",
                render: (r) => (
                  <div className="text-xs">
                    <div className="truncate text-[var(--text-muted)]">{r.deleted_by_email ?? "system"}</div>
                    <div className="text-[var(--text-muted)]" title={r.deleted_at}>
                      {formatWhen(r.deleted_at)}
                    </div>
                  </div>
                ),
              },
              {
                key: "state", header: "State", width: "110px",
                render: (r) => (
                  r.is_active
                    ? <StatusPill status="active" />
                    : r.restored_at
                      ? <StatusPill status="restored" />
                      : <StatusPill status="revoked" />
                ),
              },
              {
                key: "actions", header: "", width: "180px", align: "right",
                render: (r) => (
                  <div className="flex justify-end gap-1">
                    {r.is_active && (
                      <Button variant="ghost"
                              onClick={() => setConfirm({ kind: "restore", entry: r })}>
                        <ArchiveRestore className="h-3.5 w-3.5" /> Restore
                      </Button>
                    )}
                    {r.is_active && (
                      <Button variant="danger"
                              onClick={() => setConfirm({ kind: "purge", entry: r })}>
                        <Trash2 className="h-3.5 w-3.5" />
                      </Button>
                    )}
                  </div>
                ),
              },
            ]}
          />
        )}
        {!isLoading && !error && (data?.total ?? 0) > 0 && (
          <Pager page={page} pageSize={25} total={data?.total ?? 0} onChange={setPage} />
        )}
      </Card>

      {confirm?.kind === "restore" && confirm.entry && (
        <ConfirmModal
          title="Restore this resource?"
          body={
            <>
              Restore <strong>{confirm.entry.display_name ?? confirm.entry.resource_id}</strong>{" "}
              ({confirm.entry.resource_type})? The resource is brought back from the bin
              and the action is written to the audit trail.
            </>
          }
          confirmLabel="Restore" busy={busy}
          onConfirm={() => restore.mutate(confirm.entry!.id)}
          onCancel={() => setConfirm(null)}
        />
      )}

      {confirm?.kind === "purge" && confirm.entry && (
        <ConfirmModal
          danger title="Permanently purge this resource?"
          body={
            <>
              Purge <strong>{confirm.entry.display_name ?? confirm.entry.resource_id}</strong>{" "}
              ({confirm.entry.resource_type})? This is <strong>irreversible</strong> — the
              snapshot is deleted forever. A permanent-purge is recorded in the audit trail.
            </>
          }
          confirmLabel="Purge permanently" busy={busy}
          onConfirm={() => purge.mutate(confirm.entry!.id)}
          onCancel={() => setConfirm(null)}
        />
      )}

      {confirm?.kind === "purge_all" && (
        <ConfirmModal
          danger title="Empty the recycle bin?"
          body={
            <>
              Permanently purge all <strong>{data?.total ?? 0}</strong> soft-deleted
              resource(s)? This is irreversible and is recorded as a critical audit event.
            </>
          }
          confirmLabel="Purge everything" busy={busy}
          onConfirm={() => purgeAll.mutate()}
          onCancel={() => setConfirm(null)}
        />
      )}
    </div>
  );
}

function SoftDeleteForm({
  onClose, onDone,
}: { onClose: () => void; onDone: () => void }) {
  const [resourceType, setResourceType] = useState("company");
  const [resourceId, setResourceId] = useState("");
  const [displayName, setDisplayName] = useState("");

  const mutation = useMutation({
    mutationFn: () => adminApi.recycleSoftDelete({
      resource_type: resourceType, resource_id: resourceId,
      ...(displayName ? { display_name: displayName } : {}),
    }),
    onSuccess: () => { onDone(); onClose(); },
  });

  return (
    <Card className="border-accent-500/30">
      <CardHeader title="Soft delete a resource"
                  subtitle="Moves it to the recycle bin; it can be restored or purged later." />
      <CardBody className="space-y-3">
        <div className="grid gap-2 sm:grid-cols-3">
          <Select value={resourceType} onChange={setResourceType}
                  options={[
                    { value: "company", label: "Company" },
                    { value: "sector", label: "Sector" },
                    { value: "news", label: "News" },
                    { value: "document", label: "Document" },
                  ]} />
          <TextInput value={resourceId} onChange={setResourceId} placeholder="resource id" />
          <TextInput value={displayName} onChange={setDisplayName} placeholder="Display name" />
        </div>
        {mutation.error && (
          <div className="rounded border border-loss/30 bg-loss/5 px-3 py-2 text-xs text-loss">
            {(mutation.error as Error).message}
          </div>
        )}
        <div className="flex gap-2">
          <Button variant="primary" disabled={!resourceId || mutation.isPending}
                  onClick={() => mutation.mutate()}>
            {mutation.isPending ? "Moving…" : "Move to bin"}
          </Button>
          <Button onClick={onClose}>Cancel</Button>
        </div>
      </CardBody>
    </Card>
  );
}
