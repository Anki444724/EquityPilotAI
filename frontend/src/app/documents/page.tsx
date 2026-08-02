"use client";

/**
 * Corpus-level document view: every ingested document across every company,
 * plus the engine's own self-description.
 */

import { AppShell } from "@/components/layout/app-shell";
import {
  DOC_TYPE_LABELS, DocumentCard, InfoNote, PipelineTrace,
} from "@/components/documents/panels";
import {
  Badge, Card, CardBody, CardHeader, EmptyState, Skeleton, Stat,
} from "@/components/ui";
import { api, docsApi } from "@/lib/api";
import { useQuery } from "@tanstack/react-query";
import { Database, FileSearch, ScanLine } from "lucide-react";
import { useRouter } from "next/navigation";
import { useState } from "react";

export default function DocumentsIndex() {
  const router = useRouter();
  const [companyId, setCompanyId] = useState<string>("");

  const companies = useQuery({
    queryKey: ["companies", 1, 60],
    queryFn: () => api.listCompanies(1, 60),
  });

  const statistics = useQuery({
    queryKey: ["doc-statistics", "all"],
    queryFn: () => docsApi.statistics(),
  });

  const capabilities = useQuery({
    queryKey: ["doc-capabilities"],
    queryFn: () => docsApi.capabilities(),
  });

  const documents = useQuery({
    queryKey: ["documents", companyId || "all"],
    queryFn: () => docsApi.list(companyId),
    enabled: companyId.length > 0,
  });

  const stats = statistics.data;

  return (
    <AppShell>
      <div className="mx-auto max-w-[1200px] space-y-4 p-4">
        <div>
          <h1 className="text-xl font-semibold text-[var(--text)]">Document Intelligence</h1>
          <p className="text-xs text-[var(--text-muted)]">
            Knowledge acquisition across the coverage universe
          </p>
        </div>

        <Card>
          <CardBody className="grid grid-cols-2 gap-4 sm:grid-cols-4 lg:grid-cols-7">
            <Stat label="Documents" value={stats?.current_documents ?? "—"} />
            <Stat label="Pages" value={stats?.pages ?? "—"} />
            <Stat label="Chunks" value={stats?.chunks ?? "—"} />
            <Stat label="Tables" value={stats?.tables ?? "—"} />
            <Stat label="Entities" value={stats?.entities ?? "—"} />
            <Stat label="Fields" value={stats?.facts ?? "—"} />
            <Stat label="OCR'd" value={stats?.ocr_documents ?? "—"} />
          </CardBody>
        </Card>

        <div className="grid min-w-0-all gap-4 lg:grid-cols-[1fr_340px]">
          <div className="space-y-3">
            <Card>
              <CardHeader
                title="Browse by company"
                subtitle="Upload and search are per-company"
              />
              <CardBody className="space-y-3">
                <select
                  value={companyId}
                  onChange={(e) => setCompanyId(e.target.value)}
                  className="w-full rounded border border-[var(--border)] bg-[var(--bg-elevated)] px-3 py-2 text-sm text-[var(--text)]"
                >
                  <option value="">Select a company…</option>
                  {(companies.data?.results ?? []).map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.ticker} — {c.name}
                    </option>
                  ))}
                </select>

                {companyId && (
                  <button
                    type="button"
                    onClick={() => router.push(`/companies/${companyId}/documents`)}
                    className="inline-flex items-center gap-1.5 rounded bg-accent-500 px-3 py-1.5 text-xs font-semibold text-white hover:bg-accent-600"
                  >
                    <FileSearch className="h-3.5 w-3.5" />
                    Open document workspace
                  </button>
                )}
              </CardBody>
            </Card>

            {companyId && (
              <div className="space-y-2">
                {documents.isLoading && <Skeleton className="h-24 w-full" />}
                {documents.data?.length === 0 && (
                  <Card>
                    <EmptyState
                      icon={<Database className="h-8 w-8" />}
                      title="No documents for this company"
                      description="Open the workspace to upload the first filing."
                    />
                  </Card>
                )}
                {(documents.data ?? []).map((document) => (
                  <DocumentCard
                    key={document.id}
                    document={document}
                    onSelect={() => router.push(`/companies/${companyId}/documents`)}
                  />
                ))}
              </div>
            )}
          </div>

          <div className="space-y-3">
            <Card>
              <CardHeader title="Engine" />
              <CardBody className="space-y-2 text-xs">
                <div className="flex items-center justify-between">
                  <span className="text-[var(--text-muted)]">OCR</span>
                  <Badge variant={stats?.ocr.available ? "gain" : "warn"}>
                    <ScanLine className="mr-1 inline h-3 w-3" />
                    {stats?.ocr.available
                      ? `${stats.ocr.engine} ${stats.ocr.version}`
                      : "unavailable"}
                  </Badge>
                </div>
                <div className="flex items-center justify-between">
                  <span className="text-[var(--text-muted)]">Embeddings</span>
                  <span className="font-mono text-[var(--text)]">
                    {stats ? `${stats.embedding.provider} · ${stats.embedding.dimension}d` : "—"}
                  </span>
                </div>
                <div className="flex items-center justify-between">
                  <span className="text-[var(--text-muted)]">Extraction fields</span>
                  <span className="font-mono text-[var(--text)]">
                    {capabilities.data?.field_count ?? "—"}
                  </span>
                </div>
                <div className="pt-1">
                  <div className="mb-1 text-[var(--text-muted)]">Supported formats</div>
                  <div className="flex flex-wrap gap-1">
                    {(stats?.supported_formats ?? []).map((f) => (
                      <Badge key={f} variant="neutral">{f}</Badge>
                    ))}
                  </div>
                </div>
                <div className="pt-1">
                  <div className="mb-1 text-[var(--text-muted)]">Document types</div>
                  <div className="flex flex-wrap gap-1">
                    {(capabilities.data?.document_types ?? []).map((t) => (
                      <Badge key={t} variant="neutral">{DOC_TYPE_LABELS[t] ?? t}</Badge>
                    ))}
                  </div>
                </div>
              </CardBody>
            </Card>

            <Card>
              <CardHeader title="Ingestion pipeline" />
              <CardBody>
                <PipelineTrace
                  stages={capabilities.data?.pipeline_stages ?? []}
                  timings={null}
                />
              </CardBody>
            </Card>

            <InfoNote>
              Documents ingested here become citable evidence in the AI Research
              Analyst, and populate the qualitative inputs the institutional
              scorecard depends on.
            </InfoNote>
          </div>
        </div>
      </div>
    </AppShell>
  );
}
