"use client";

/**
 * Document Intelligence — upload, search, extraction, entities and the graph.
 *
 * No business logic lives here. Coverage percentages, confidence bands, table
 * units, graph edges and search relevance all arrive computed from the API;
 * this file arranges them on screen.
 */

import { AppShell } from "@/components/layout/app-shell";
import { CompanyTabs } from "@/components/layout/company-tabs";
import {
  CitationList, CoverageGrid, DOC_TYPE_LABELS, DocumentCard, EntityGroups,
  ExtractedTableView, FactTable, HitList, InfoNote, KnowledgeGraphView,
  PipelineTrace, RelationLegend, SearchAnswer, SECTION_LABELS,
} from "@/components/documents/panels";
import { Badge, Card, CardBody, CardHeader, EmptyState, Skeleton, Stat, TabStrip } from "@/components/ui";
import { api, docsApi } from "@/lib/api";
import { cn } from "@/lib/utils";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Database, FileSearch, FileUp, Loader2, Network, RefreshCw, ScanLine,
  Search, Sparkles, Table2, Upload,
} from "lucide-react";
import Link from "next/link";
import { use, useRef, useState } from "react";

const TABS = [
  { key: "library", label: "Library", icon: Database },
  { key: "search", label: "Search", icon: Search },
  { key: "extraction", label: "Extraction", icon: Table2 },
  { key: "entities", label: "Entities", icon: Sparkles },
  { key: "knowledge", label: "Knowledge Graph", icon: Network },
] as const;
type TabKey = (typeof TABS)[number]["key"];

const SAMPLE_QUERIES = [
  "What is the EBITDA margin guidance?",
  "Who are the competitors?",
  "What is the credit rating and cost of debt?",
  "What are the principal risks?",
  "List the subsidiaries",
];

export default function DocumentsPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const queryClient = useQueryClient();
  const fileInput = useRef<HTMLInputElement>(null);

  const [tab, setTab] = useState<TabKey>("library");
  const [selected, setSelected] = useState<number | null>(null);
  const [query, setQuery] = useState("");
  const [submitted, setSubmitted] = useState("");
  const [notice, setNotice] = useState<string | null>(null);

  const profile = useQuery({
    queryKey: ["company-profile", id],
    queryFn: () => api.companyProfile(id),
  });

  const documents = useQuery({
    queryKey: ["documents", id],
    queryFn: () => docsApi.list(id),
  });

  const statistics = useQuery({
    queryKey: ["doc-statistics", id],
    queryFn: () => docsApi.statistics(id),
  });

  const capabilities = useQuery({
    queryKey: ["doc-capabilities"],
    queryFn: () => docsApi.capabilities(),
  });

  const coverage = useQuery({
    queryKey: ["doc-coverage", id],
    queryFn: () => docsApi.coverage(id),
  });

  const facts = useQuery({
    queryKey: ["doc-facts", id],
    queryFn: () => docsApi.facts(id),
  });

  const entities = useQuery({
    queryKey: ["doc-entities", id],
    queryFn: () => docsApi.entities(id),
  });

  const graph = useQuery({
    queryKey: ["doc-knowledge", id],
    queryFn: () => docsApi.knowledge(id),
  });

  const detail = useQuery({
    queryKey: ["doc-detail", selected],
    queryFn: () => docsApi.get(selected!),
    enabled: selected !== null,
  });

  const tables = useQuery({
    queryKey: ["doc-tables", selected],
    queryFn: () => docsApi.tables(selected!),
    enabled: selected !== null,
  });

  const search = useQuery({
    queryKey: ["doc-search", id, submitted],
    queryFn: () => docsApi.search(submitted, id),
    enabled: submitted.length > 0,
  });

  const upload = useMutation({
    mutationFn: (file: File) => docsApi.upload(id, file),
    onSuccess: (result) => {
      setNotice(result.message);
      setSelected(result.document.id);
      for (const key of [
        "documents", "doc-statistics", "doc-coverage", "doc-facts",
        "doc-entities", "doc-knowledge",
      ]) {
        queryClient.invalidateQueries({ queryKey: [key, id] });
      }
    },
    onError: (error: Error) => setNotice(`Upload failed — ${error.message}`),
  });

  const reindex = useMutation({
    mutationFn: () => docsApi.reindex(id),
    onSuccess: (result) =>
      setNotice(`Re-embedded ${result.reindexed_chunks} chunks in ${result.took_ms.toFixed(0)} ms.`),
  });

  const stats = statistics.data;
  const docs = documents.data ?? [];
  const ready = docs.filter((d) => d.status === "ready");
  const ocrAvailable = stats?.ocr.available ?? false;

  return (
    <AppShell>
      <CompanyTabs companyId={id} />
      <div className="mx-auto max-w-[1400px] space-y-4 p-4">
        {/* ---------------------------------------------------- header */}
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <div className="flex items-center gap-2 text-xs text-[var(--text-muted)]">
              <Link href="/companies" className="hover:underline">Companies</Link>
              <span>/</span>
              <Link href={`/companies/${id}`} className="hover:underline">
                {profile.data?.company.ticker ?? "…"}
              </Link>
              <span>/</span>
              <span>Documents</span>
            </div>
            <h1 className="mt-1 text-xl font-semibold text-[var(--text)]">
              Document Intelligence
            </h1>
            <p className="text-xs text-[var(--text-muted)]">
              {profile.data?.company.name ?? "Loading…"} · knowledge acquisition engine
            </p>
          </div>

          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => reindex.mutate()}
              disabled={reindex.isPending || !ready.length}
              className="inline-flex items-center gap-1.5 rounded border border-[var(--border)] px-3 py-1.5 text-xs font-medium text-[var(--text-muted)] hover:bg-[var(--bg-subtle)] disabled:opacity-50"
            >
              {reindex.isPending
                ? <Loader2 className="h-3.5 w-3.5 animate-spin" />
                : <RefreshCw className="h-3.5 w-3.5" />}
              Re-index
            </button>
            <input
              ref={fileInput}
              type="file"
              className="hidden"
              accept=".pdf,.docx,.txt,.md,.html,.htm,.csv,.xlsx"
              onChange={(e) => {
                const file = e.target.files?.[0];
                if (file) upload.mutate(file);
                e.target.value = "";
              }}
            />
            <button
              type="button"
              onClick={() => fileInput.current?.click()}
              disabled={upload.isPending}
              className="inline-flex items-center gap-1.5 rounded bg-accent-500 px-3 py-1.5 text-xs font-semibold text-white hover:bg-accent-600 disabled:opacity-60"
            >
              {upload.isPending
                ? <Loader2 className="h-3.5 w-3.5 animate-spin" />
                : <FileUp className="h-3.5 w-3.5" />}
              {upload.isPending ? "Processing…" : "Upload document"}
            </button>
          </div>
        </div>

        {notice && (
          <InfoNote tone={notice.startsWith("Upload failed") ? "warning" : "info"}>
            {notice}
          </InfoNote>
        )}

        {/* ----------------------------------------------------- stats */}
        <Card>
          <CardBody className="grid grid-cols-2 gap-4 sm:grid-cols-4 lg:grid-cols-7">
            <Stat label="Documents" value={stats?.current_documents ?? "—"}
                  hint={stats?.superseded ? `${stats.superseded} superseded` : "all current"} />
            <Stat label="Pages" value={stats?.pages ?? "—"} />
            <Stat label="Indexed chunks" value={stats?.chunks ?? "—"} />
            <Stat label="Tables" value={stats?.tables ?? "—"} />
            <Stat label="Entities" value={stats?.entities ?? "—"} />
            <Stat label="Extracted fields" value={stats?.facts ?? "—"} />
            <Stat
              label="Field coverage"
              value={coverage.data ? `${(coverage.data.coverage * 100).toFixed(0)}%` : "—"}
              hint={coverage.data
                ? `${coverage.data.fields_extracted} of ${coverage.data.fields_defined}`
                : undefined}
              tone={coverage.data && coverage.data.coverage > 0.6 ? "gain" : "default"}
            />
          </CardBody>
        </Card>

        {/* ----------------------------------------------- engine status */}
        <div className="flex flex-wrap items-center gap-2 text-xs text-[var(--text-muted)]">
          <Badge variant={ocrAvailable ? "gain" : "warn"}>
            <ScanLine className="mr-1 inline h-3 w-3" />
            OCR {ocrAvailable ? `ready (${stats?.ocr.engine} ${stats?.ocr.version})` : "unavailable"}
          </Badge>
          {stats && (
            <Badge variant="neutral">
              embeddings: {stats.embedding.provider} · {stats.embedding.dimension}d
            </Badge>
          )}
          {capabilities.data && (
            <Badge variant="neutral">{capabilities.data.field_count} extraction fields</Badge>
          )}
          {stats && (
            <span>formats: {stats.supported_formats.join(", ")}</span>
          )}
        </div>

        {/* ------------------------------------------------------ tabs */}
        <TabStrip label="Document views">
          {TABS.map(({ key, label, icon: Icon }) => (
            <button data-active={tab === key} role="tab" aria-selected={tab === key}
              key={key}
              type="button"
              onClick={() => setTab(key)}
              className={cn(
                "inline-flex items-center gap-1.5 border-b-2 px-3 py-2 text-xs font-medium transition",
                tab === key
                  ? "border-accent-500 text-[var(--text)]"
                  : "border-transparent text-[var(--text-muted)] hover:text-[var(--text)]",
              )}
            >
              <Icon className="h-3.5 w-3.5" />
              {label}
            </button>
          ))}
        </TabStrip>

        {/* --------------------------------------------------- library */}
        {tab === "library" && (
          <div className="grid min-w-0-all gap-4 lg:grid-cols-[340px_1fr]">
            <div className="break-anywhere space-y-2">
              {documents.isLoading && <Skeleton className="h-24 w-full" />}
              {!documents.isLoading && !docs.length && (
                <Card>
                  <EmptyState
                    icon={<Upload className="h-8 w-8" />}
                    title="No documents yet"
                    description="Upload an annual report, transcript, presentation or rating report. PDF, DOCX, TXT, HTML, CSV and Excel are supported."
                  />
                </Card>
              )}
              {docs.map((document) => (
                <DocumentCard
                  key={document.id}
                  document={document}
                  selected={selected === document.id}
                  onSelect={() => setSelected(document.id)}
                />
              ))}
            </div>

            <div className="space-y-4">
              {selected === null && docs.length > 0 && (
                <Card>
                  <EmptyState
                    icon={<FileSearch className="h-8 w-8" />}
                    title="Select a document"
                    description="Choose a document to inspect its detected sections, recovered tables and processing trace."
                  />
                </Card>
              )}

              {detail.data && (
                <>
                  <Card>
                    <CardHeader
                      title={detail.data.title ?? detail.data.filename}
                      subtitle={`${DOC_TYPE_LABELS[detail.data.doc_type] ?? detail.data.doc_type} · ${detail.data.period ?? "period unknown"} · version ${detail.data.version}`}
                      action={
                        <Badge variant={detail.data.status === "ready" ? "gain" : "warn"}>
                          {detail.data.status}
                        </Badge>
                      }
                    />
                    <CardBody className="grid grid-cols-2 gap-4 sm:grid-cols-4">
                      <Stat label="Pages" value={detail.data.page_count} />
                      <Stat label="Chunks" value={detail.data.chunk_count} />
                      <Stat label="Fields" value={detail.data.fact_count} />
                      <Stat
                        label="Processing"
                        value={`${detail.data.processing_ms.toFixed(0)} ms`}
                        hint={`${(detail.data.page_count / (detail.data.processing_ms / 1000)).toFixed(1)} pages/s`}
                      />
                    </CardBody>
                  </Card>

                  <Card>
                    <CardHeader
                      title="Detected sections"
                      subtitle={`${detail.data.sections.length} identified`}
                    />
                    <CardBody className="space-y-1.5">
                      {detail.data.sections.map((section, i) => (
                        <div key={i} className="flex items-center gap-2 text-xs">
                          <Badge variant="accent">
                            {SECTION_LABELS[section.kind] ?? section.kind}
                          </Badge>
                          <span className="truncate text-[var(--text-muted)]">{section.title}</span>
                          <span className="ml-auto shrink-0 font-mono text-[var(--text-muted)]">
                            pp. {section.start_page}–{section.end_page} ·{" "}
                            {(section.confidence * 100).toFixed(0)}%
                          </span>
                        </div>
                      ))}
                      {!detail.data.sections.length && (
                        <p className="text-xs text-[var(--text-muted)]">
                          No named sections were identified in this document.
                        </p>
                      )}
                    </CardBody>
                  </Card>

                  <Card>
                    <CardHeader
                      title="Page provenance"
                      subtitle="How each page's text was obtained"
                    />
                    <CardBody>
                      <div className="flex flex-wrap gap-1">
                        {detail.data.pages.map((page) => (
                          <span
                            key={page.page_number}
                            title={`Page ${page.page_number} · ${page.text_source}${
                              page.ocr_confidence !== null
                                ? ` · OCR confidence ${(page.ocr_confidence * 100).toFixed(0)}%`
                                : ""
                            } · ${page.char_count} chars`}
                            className={cn(
                              "inline-flex h-6 w-6 items-center justify-center rounded text-[10px] font-mono",
                              page.text_source === "native"
                                ? "bg-[var(--bg-subtle)] text-[var(--text-muted)]"
                                : "bg-warn/15 text-warn",
                            )}
                          >
                            {page.page_number}
                          </span>
                        ))}
                      </div>
                      <p className="mt-2 text-xs text-[var(--text-muted)]">
                        Native text layers are read directly. OCR runs only on pages that
                        need it — {detail.data.ocr_pages} of {detail.data.page_count} here.
                      </p>
                    </CardBody>
                  </Card>

                  {(tables.data ?? []).map((table) => (
                    <ExtractedTableView key={table.id} table={table} />
                  ))}
                </>
              )}
            </div>
          </div>
        )}

        {/* ---------------------------------------------------- search */}
        {tab === "search" && (
          <div className="grid min-w-0-all gap-4 lg:grid-cols-[1fr_360px]">
            <div className="space-y-3">
              <form
                onSubmit={(e) => { e.preventDefault(); setSubmitted(query.trim()); }}
                className="flex gap-2"
              >
                <input
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  placeholder="Ask the corpus a question…"
                  className="flex-1 rounded border border-[var(--border)] bg-[var(--bg-elevated)] px-3 py-2 text-sm text-[var(--text)] outline-none focus:border-accent-500"
                />
                <button
                  type="submit"
                  disabled={!query.trim() || !ready.length}
                  className="inline-flex items-center gap-1.5 rounded bg-accent-500 px-4 py-2 text-xs font-semibold text-white hover:bg-accent-600 disabled:opacity-50"
                >
                  <Search className="h-3.5 w-3.5" />
                  Search
                </button>
              </form>

              <div className="flex flex-wrap gap-1.5">
                {SAMPLE_QUERIES.map((sample) => (
                  <button
                    key={sample}
                    type="button"
                    onClick={() => { setQuery(sample); setSubmitted(sample); }}
                    className="rounded-full border border-[var(--border)] px-2.5 py-1 text-xs text-[var(--text-muted)] hover:bg-[var(--bg-subtle)]"
                  >
                    {sample}
                  </button>
                ))}
              </div>

              {!ready.length && (
                <InfoNote tone="warning">
                  No processed documents to search. Upload a filing first.
                </InfoNote>
              )}

              {search.isFetching && <Skeleton className="h-28 w-full" />}

              {search.data && !search.isFetching && (
                <>
                  <SearchAnswer
                    answer={search.data.answer}
                    confidence={search.data.confidence}
                    unavailable={search.data.unavailable_reason}
                    audit={search.data.citation_audit}
                    tookMs={search.data.took_ms}
                  />
                  <div>
                    <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-[var(--text-muted)]">
                      Supporting passages
                    </div>
                    <HitList hits={search.data.hits} />
                  </div>
                </>
              )}
            </div>

            <div className="space-y-3">
              <Card>
                <CardHeader title="Citations" subtitle="Document · page · section · paragraph" />
                <CardBody>
                  {search.data?.citations.length
                    ? <CitationList citations={search.data.citations} />
                    : <p className="text-xs text-[var(--text-muted)]">
                        Run a search to see its evidence.
                      </p>}
                </CardBody>
              </Card>

              <Card>
                <CardHeader title="How retrieval works" />
                <CardBody className="space-y-2 text-xs text-[var(--text-muted)]">
                  <p>
                    Retrieval is hybrid: BM25 supplies exact term matching, and a vector
                    index supplies tolerance to phrasing and inflection. Both component
                    scores are shown beside every passage.
                  </p>
                  <p>
                    Answers are <strong>extractive</strong>. Every sentence returned exists
                    verbatim in a retrieved page, so the answer cannot contain a number
                    that is not in a document.
                  </p>
                  <p>
                    Where the corpus cannot support an answer the platform says so rather
                    than composing one from a weak match.
                  </p>
                </CardBody>
              </Card>
            </div>
          </div>
        )}

        {/* ------------------------------------------------ extraction */}
        {tab === "extraction" && (
          <div className="space-y-4">
            <Card>
              <CardHeader
                title="Extraction coverage"
                subtitle={coverage.data
                  ? `${coverage.data.fields_extracted} of ${coverage.data.fields_defined} specification fields found across ${coverage.data.documents_ready} processed documents`
                  : "Loading…"}
                action={coverage.data && (
                  <Badge variant={coverage.data.coverage > 0.6 ? "gain" : "warn"}>
                    {(coverage.data.coverage * 100).toFixed(0)}% coverage
                  </Badge>
                )}
              />
              <CardBody>
                {coverage.data
                  ? <CoverageGrid categories={coverage.data.categories} />
                  : <Skeleton className="h-24 w-full" />}
                <p className="mt-3 text-xs text-[var(--text-muted)]">
                  Hover a category to see which fields were <em>not</em> found. Coverage is
                  measured against the full specification, so a gap is reported rather than
                  hidden.
                </p>
              </CardBody>
            </Card>

            <Card>
              <CardHeader
                title="Structured extraction store"
                subtitle={`${facts.data?.length ?? 0} extracted values, each traceable to a page`}
              />
              <CardBody className="p-0">
                {facts.isLoading
                  ? <Skeleton className="m-4 h-40" />
                  : <FactTable facts={facts.data ?? []} />}
              </CardBody>
            </Card>
          </div>
        )}

        {/* -------------------------------------------------- entities */}
        {tab === "entities" && (
          <Card>
            <CardHeader
              title="Extracted entities"
              subtitle={`${entities.data?.length ?? 0} entities, deduplicated across all documents`}
            />
            <CardBody>
              {entities.isLoading
                ? <Skeleton className="h-40 w-full" />
                : <EntityGroups entities={entities.data ?? []} />}
              <p className="mt-4 text-xs text-[var(--text-muted)]">
                Extraction is cue-phrase based, anchored on the formulaic language that
                regulated filings are obliged to use. Precision is high and recall is
                partial; every entity carries its confidence and the sentence it came from,
                so any of them can be checked. Hover to see the evidence.
              </p>
            </CardBody>
          </Card>
        )}

        {/* ------------------------------------------------- knowledge */}
        {tab === "knowledge" && (
          <div className="grid min-w-0-all gap-4 lg:grid-cols-[1fr_320px]">
            <Card>
              <CardHeader
                title="Knowledge graph"
                subtitle={graph.data
                  ? `${graph.data.stats.nodes} nodes · ${graph.data.stats.edges} relationships`
                  : "Loading…"}
              />
              <CardBody>
                {graph.isLoading && <Skeleton className="h-96 w-full" />}
                {graph.data && (
                  <KnowledgeGraphView
                    nodes={graph.data.nodes}
                    edges={graph.data.edges}
                    subjectKey={graph.data.company.subject_key}
                  />
                )}
              </CardBody>
            </Card>

            <div className="space-y-3">
              <Card>
                <CardHeader title="Relationship types" />
                <CardBody>
                  {graph.data
                    ? <RelationLegend relations={graph.data.stats.relations} />
                    : <Skeleton className="h-20 w-full" />}
                </CardBody>
              </Card>

              <Card>
                <CardHeader title="Pipeline" subtitle="Stages executed per document" />
                <CardBody>
                  <PipelineTrace
                    stages={capabilities.data?.pipeline_stages ?? []}
                    timings={null}
                  />
                </CardBody>
              </Card>

              <Card>
                <CardHeader title="Reading the graph" />
                <CardBody className="space-y-2 text-xs text-[var(--text-muted)]">
                  <p>
                    Every edge records the pages on which the relationship was observed,
                    so a graph answer cites like any other answer. Hover an edge for its
                    evidence.
                  </p>
                  <p>
                    Nothing is inferred transitively. If A is a subsidiary of B and B of C,
                    the graph does not assert A is a subsidiary of C — the document did not
                    say so.
                  </p>
                </CardBody>
              </Card>
            </div>
          </div>
        )}
      </div>
    </AppShell>
  );
}
