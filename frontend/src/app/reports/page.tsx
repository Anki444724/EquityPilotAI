"use client";

/**
 * Report Generator — compose, generate, preview and export.
 *
 * No business logic. Section composition, sufficiency, citation coverage and
 * every rendered byte come from the backend; this page selects a report type,
 * fires the request and displays what comes back.
 */

import { AppShell } from "@/components/layout/app-shell";
import {
  Badge, Card, CardBody, CardHeader, EmptyState, Skeleton, Stat,
} from "@/components/ui";
import { api, reportApi } from "@/lib/api";
import type { ReportDetail, ReportSummary } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle, CheckCircle2, Clock, Download, Eye, FileSpreadsheet,
  FileText, Info, Layers, Loader2, Quote, Sparkles, Trash2,
} from "lucide-react";
import { useEffect, useState } from "react";

const FORMAT_META: Record<string, { label: string; icon: typeof FileText }> = {
  pdf: { label: "PDF", icon: FileText },
  docx: { label: "Word", icon: FileText },
  xlsx: { label: "Excel", icon: FileSpreadsheet },
  html: { label: "HTML", icon: Eye },
  md: { label: "Markdown", icon: Quote },
};

const SECTION_LABELS: Record<string, string> = {
  cover: "Cover", table_of_contents: "Contents",
  executive_summary: "Executive Summary", investment_thesis: "Investment Thesis",
  business_overview: "Business Overview", industry_analysis: "Industry Analysis",
  financial_analysis: "Financial Analysis", forecast: "Forecast",
  valuation: "Valuation", dcf: "DCF", relative_valuation: "Relative Valuation",
  institutional_score: "Institutional Score", management: "Management",
  moat: "Moat", risk_analysis: "Risk Analysis",
  scenario_analysis: "Scenario Analysis", peer_comparison: "Peer Comparison",
  portfolio_fit: "Portfolio Fit", appendix: "Appendix",
};

const ALL_FORMATS = ["html", "pdf", "docx", "xlsx", "md"];

export default function ReportsPage() {
  const queryClient = useQueryClient();
  const [companyId, setCompanyId] = useState("");
  const [reportType, setReportType] = useState("institutional");
  const [formats, setFormats] = useState<string[]>(["html", "pdf"]);
  const [theme, setTheme] = useState("light");
  const [analyst, setAnalyst] = useState("Development Analyst");
  const [includeAi, setIncludeAi] = useState(true);
  const [selected, setSelected] = useState<number | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [previewHtml, setPreviewHtml] = useState<string | null>(null);

  const companies = useQuery({
    queryKey: ["companies", 1, 60],
    queryFn: () => api.listCompanies(1, 60),
  });

  useEffect(() => {
    if (!companyId && companies.data?.results.length) {
      const reference = companies.data.results.find(
        (c) => c.ticker === "BHARATCP",
      );
      setCompanyId(reference?.id ?? companies.data.results[0].id);
    }
  }, [companies.data, companyId]);

  const capabilities = useQuery({
    queryKey: ["report-capabilities"], queryFn: () => reportApi.capabilities(),
  });
  const statistics = useQuery({
    queryKey: ["report-statistics"], queryFn: () => reportApi.statistics(),
  });
  const reports = useQuery({
    queryKey: ["reports"], queryFn: () => reportApi.list(),
  });
  const detail = useQuery({
    queryKey: ["report-detail", selected],
    queryFn: () => reportApi.get(selected!),
    enabled: selected !== null,
  });

  const generate = useMutation({
    mutationFn: () =>
      reportApi.generate({
        company_id: companyId, report_type: reportType, formats,
        theme, analyst, include_ai: includeAi,
      }),
    onSuccess: (result) => {
      setNotice(result.message);
      setSelected(result.report.id);
      setPreviewHtml(null);
      for (const key of ["reports", "report-statistics"]) {
        queryClient.invalidateQueries({ queryKey: [key] });
      }
    },
    onError: (error: Error) => setNotice(`Generation failed — ${error.message}`),
  });

  const remove = useMutation({
    mutationFn: (id: number) => reportApi.remove(id),
    onSuccess: () => {
      setSelected(null);
      setPreviewHtml(null);
      queryClient.invalidateQueries({ queryKey: ["reports"] });
    },
  });

  const loadPreview = useMutation({
    mutationFn: (id: number) => reportApi.preview(id),
    onSuccess: (html) => setPreviewHtml(html),
  });

  const selectedType = capabilities.data?.report_types.find(
    (t) => t.key === reportType,
  );
  const stats = statistics.data;

  return (
    <AppShell>
      <div className="mx-auto max-w-[1500px] space-y-4 p-4">
        <div>
          <h1 className="text-xl font-semibold text-[var(--text)]">
            Research Report Generator
          </h1>
          <p className="text-xs text-[var(--text-muted)]">
            Publication-quality reports composed from platform evidence
          </p>
        </div>

        {stats && (
          <Card>
            <CardBody className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-6">
              <Stat label="Reports" value={stats.reports}
                    hint={`${stats.superseded} superseded`} />
              <Stat label="Artefacts" value={stats.artifacts}
                    hint={`${(stats.bytes_stored / 1048576).toFixed(1)} MB stored`} />
              <Stat label="Citation coverage"
                    value={`${(stats.mean_coverage * 100).toFixed(0)}%`}
                    hint="mean across reports"
                    tone={stats.mean_coverage >= 0.99 ? "gain" : "default"} />
              <Stat label="Fully cited" value={stats.citation_clean}
                    hint="no uncited claims" />
              <Stat label="Mean build"
                    value={`${(stats.mean_build_ms / 1000).toFixed(1)}s`} />
              <Stat label="Formats"
                    value={Object.keys(stats.by_format).length}
                    hint={Object.keys(stats.by_format).join(", ")} />
            </CardBody>
          </Card>
        )}

        {notice && (
          <div className={cn(
            "flex items-start gap-2 rounded-lg border p-3 text-xs",
            notice.startsWith("Generation failed")
              ? "border-warn/40 bg-warn/10 text-[var(--text)]"
              : "border-[var(--border)] bg-[var(--bg-subtle)] text-[var(--text-muted)]",
          )}>
            <Info className="mt-0.5 h-3.5 w-3.5 shrink-0" />
            <div>{notice}</div>
          </div>
        )}

        <div className="grid gap-4 lg:grid-cols-[320px_1fr]">
          {/* ------------------------------------------------ composer */}
          <div className="space-y-4">
            <Card>
              <CardHeader title="Compose" subtitle="Sections are chosen by type" />
              <CardBody className="space-y-3">
                <div>
                  <label className="mb-1 block text-xs text-[var(--text-muted)]">
                    Company
                  </label>
                  <select
                    value={companyId}
                    onChange={(e) => setCompanyId(e.target.value)}
                    className="w-full rounded border border-[var(--border)] bg-[var(--bg-elevated)] px-2.5 py-1.5 text-xs text-[var(--text)]"
                  >
                    {(companies.data?.results ?? []).map((c) => (
                      <option key={c.id} value={c.id}>
                        {c.ticker} — {c.name}
                      </option>
                    ))}
                  </select>
                </div>

                <div>
                  <label className="mb-1 block text-xs text-[var(--text-muted)]">
                    Report type
                  </label>
                  <select
                    value={reportType}
                    onChange={(e) => setReportType(e.target.value)}
                    className="w-full rounded border border-[var(--border)] bg-[var(--bg-elevated)] px-2.5 py-1.5 text-xs text-[var(--text)]"
                  >
                    {(capabilities.data?.report_types ?? []).map((t) => (
                      <option key={t.key} value={t.key}>{t.label}</option>
                    ))}
                  </select>
                  {selectedType && (
                    <p className="mt-1 text-[10px] text-[var(--text-muted)]">
                      {selectedType.sections.length} sections ·{" "}
                      {selectedType.narratives.length} AI narratives
                    </p>
                  )}
                </div>

                <div>
                  <label className="mb-1 block text-xs text-[var(--text-muted)]">
                    Formats
                  </label>
                  <div className="flex flex-wrap gap-1.5">
                    {ALL_FORMATS.map((fmt) => {
                      const active = formats.includes(fmt);
                      return (
                        <button
                          key={fmt} type="button"
                          onClick={() => setFormats(
                            active
                              ? formats.filter((f) => f !== fmt)
                              : [...formats, fmt],
                          )}
                          className={cn(
                            "rounded border px-2 py-1 text-[11px] font-medium transition",
                            active
                              ? "border-accent-500 bg-accent-500/10 text-accent-500"
                              : "border-[var(--border)] text-[var(--text-muted)] hover:bg-[var(--bg-subtle)]",
                          )}
                        >
                          {FORMAT_META[fmt]?.label ?? fmt}
                        </button>
                      );
                    })}
                  </div>
                </div>

                <div className="grid grid-cols-2 gap-2">
                  <div>
                    <label className="mb-1 block text-xs text-[var(--text-muted)]">
                      Theme
                    </label>
                    <select
                      value={theme}
                      onChange={(e) => setTheme(e.target.value)}
                      className="w-full rounded border border-[var(--border)] bg-[var(--bg-elevated)] px-2.5 py-1.5 text-xs text-[var(--text)]"
                    >
                      <option value="light">Light</option>
                      <option value="dark">Dark</option>
                    </select>
                  </div>
                  <div>
                    <label className="mb-1 block text-xs text-[var(--text-muted)]">
                      Analyst
                    </label>
                    <input
                      value={analyst}
                      onChange={(e) => setAnalyst(e.target.value)}
                      className="w-full rounded border border-[var(--border)] bg-[var(--bg-elevated)] px-2.5 py-1.5 text-xs text-[var(--text)] outline-none focus:border-accent-500"
                    />
                  </div>
                </div>

                <label className="flex items-center gap-2 text-xs text-[var(--text-muted)]">
                  <input
                    type="checkbox" checked={includeAi}
                    onChange={(e) => setIncludeAi(e.target.checked)}
                  />
                  <Sparkles className="h-3.5 w-3.5" />
                  Include AI narratives
                </label>

                <button
                  type="button"
                  onClick={() => generate.mutate()}
                  disabled={generate.isPending || !companyId || !formats.length}
                  className="inline-flex w-full items-center justify-center gap-1.5 rounded bg-accent-500 px-3 py-2 text-xs font-semibold text-white hover:bg-accent-600 disabled:opacity-50"
                >
                  {generate.isPending
                    ? <Loader2 className="h-3.5 w-3.5 animate-spin" />
                    : <FileText className="h-3.5 w-3.5" />}
                  {generate.isPending ? "Generating…" : "Generate report"}
                </button>
              </CardBody>
            </Card>

            {selectedType && (
              <Card>
                <CardHeader title="Sections in this type" />
                <CardBody>
                  <div className="flex flex-wrap gap-1">
                    {selectedType.sections.map((s) => (
                      <Badge key={s} variant="neutral">
                        {SECTION_LABELS[s] ?? s}
                      </Badge>
                    ))}
                  </div>
                </CardBody>
              </Card>
            )}

            <Card>
              <CardHeader title="Recent reports" />
              <CardBody className="max-h-[420px] space-y-1.5 overflow-y-auto p-3">
                {reports.isLoading && <Skeleton className="h-20 w-full" />}
                {reports.data?.length === 0 && (
                  <p className="text-xs text-[var(--text-muted)]">
                    No reports generated yet.
                  </p>
                )}
                {(reports.data ?? []).map((r) => (
                  <ReportRow
                    key={r.id} report={r} selected={selected === r.id}
                    onSelect={() => { setSelected(r.id); setPreviewHtml(null); }}
                  />
                ))}
              </CardBody>
            </Card>
          </div>

          {/* -------------------------------------------------- detail */}
          <div className="space-y-4">
            {selected === null && (
              <Card>
                <EmptyState
                  icon={<FileText className="h-8 w-8" />}
                  title="No report selected"
                  description="Generate a report or choose one from the list to inspect its sections, evidence and exports."
                />
              </Card>
            )}

            {detail.isLoading && <Skeleton className="h-64 w-full" />}
            {detail.data && (
              <ReportDetailPanel
                report={detail.data}
                previewHtml={previewHtml}
                loadingPreview={loadPreview.isPending}
                onPreview={() => loadPreview.mutate(detail.data!.id)}
                onClosePreview={() => setPreviewHtml(null)}
                onDelete={() => remove.mutate(detail.data!.id)}
              />
            )}
          </div>
        </div>
      </div>
    </AppShell>
  );
}

function ReportRow({
  report, selected, onSelect,
}: { report: ReportSummary; selected: boolean; onSelect: () => void }) {
  return (
    <button
      type="button" onClick={onSelect}
      className={cn(
        "w-full rounded-lg border p-2.5 text-left transition",
        selected
          ? "border-accent-500 bg-accent-500/10"
          : "border-[var(--border)] hover:border-accent-500/40",
        report.superseded_by !== null && "opacity-60",
      )}
    >
      <div className="flex items-baseline justify-between gap-2">
        <span className="truncate text-xs font-medium text-[var(--text)]">
          {report.ticker} · {report.title}
        </span>
        <span className="shrink-0 font-mono text-[10px] text-[var(--text-muted)]">
          v{report.version}
        </span>
      </div>
      <div className="mt-1 flex flex-wrap items-center gap-1">
        {report.citation_clean
          ? <Badge variant="gain">fully cited</Badge>
          : <Badge variant="warn">
              {(report.citation_coverage * 100).toFixed(0)}% cited
            </Badge>}
        {report.insufficient_count > 0 && (
          <Badge variant="warn">{report.insufficient_count} gaps</Badge>
        )}
        {report.superseded_by !== null && (
          <Badge variant="neutral">superseded</Badge>
        )}
      </div>
      <div className="mt-1 text-[10px] text-[var(--text-muted)]">
        {report.section_count} sections · {report.chart_count} charts ·{" "}
        {report.word_count.toLocaleString()} words ·{" "}
        {(report.build_ms / 1000).toFixed(1)}s
      </div>
    </button>
  );
}

function ReportDetailPanel({
  report, previewHtml, loadingPreview, onPreview, onClosePreview, onDelete,
}: {
  report: ReportDetail;
  previewHtml: string | null;
  loadingPreview: boolean;
  onPreview: () => void;
  onClosePreview: () => void;
  onDelete: () => void;
}) {
  const audit = (report.audit ?? {}) as Record<string, unknown>;
  return (
    <>
      <Card>
        <CardHeader
          title={`${report.company_name} — ${report.title}`}
          subtitle={
            `Version ${report.version} · ${report.theme} theme · `
            + `generated in ${(report.build_ms / 1000).toFixed(1)}s`
          }
          action={
            <div className="flex items-center gap-1.5">
              {report.citation_clean
                ? <Badge variant="gain">
                    <CheckCircle2 className="mr-1 inline h-3 w-3" />
                    citations verified
                  </Badge>
                : <Badge variant="warn">
                    <AlertTriangle className="mr-1 inline h-3 w-3" />
                    {(report.citation_coverage * 100).toFixed(0)}% cited
                  </Badge>}
              <button
                type="button" onClick={onDelete}
                className="rounded border border-[var(--border)] p-1.5 text-[var(--text-muted)] hover:text-loss"
                title="Delete this report"
              >
                <Trash2 className="h-3.5 w-3.5" />
              </button>
            </div>
          }
        />
        <CardBody className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-6">
          <Stat label="Sections" value={report.section_count}
                hint={report.insufficient_count
                  ? `${report.insufficient_count} insufficient` : "all populated"} />
          <Stat label="Charts" value={report.chart_count} />
          <Stat label="Tables" value={report.table_count} />
          <Stat label="Evidence" value={report.evidence_count}
                hint="cited figures" />
          <Stat label="Words" value={report.word_count.toLocaleString()} />
          <Stat label="Coverage"
                value={`${(report.citation_coverage * 100).toFixed(0)}%`}
                tone={report.citation_clean ? "gain" : "default"} />
        </CardBody>
      </Card>

      <Card>
        <CardHeader title="Exports" subtitle="Every format is generated from one block tree" />
        <CardBody className="flex flex-wrap gap-2">
          {report.artifacts.map((artifact) => {
            const meta = FORMAT_META[artifact.fmt];
            const Icon = meta?.icon ?? FileText;
            return (
              <a
                key={artifact.id}
                href={reportApi.downloadUrl(report.id, artifact.fmt)}
                download={artifact.filename}
                className="inline-flex items-center gap-2 rounded border border-[var(--border)] px-3 py-2 text-xs text-[var(--text)] hover:border-accent-500 hover:bg-[var(--bg-subtle)]"
              >
                <Icon className="h-4 w-4 text-accent-500" />
                <span className="font-medium">{meta?.label ?? artifact.fmt}</span>
                <span className="text-[10px] text-[var(--text-muted)]">
                  {(artifact.size_bytes / 1024).toFixed(0)} KB
                  {artifact.page_count ? ` · ${artifact.page_count}pp` : ""}
                </span>
                <Download className="h-3 w-3 text-[var(--text-muted)]" />
              </a>
            );
          })}
          <button
            type="button" onClick={onPreview} disabled={loadingPreview}
            className="inline-flex items-center gap-2 rounded bg-accent-500 px-3 py-2 text-xs font-semibold text-white hover:bg-accent-600 disabled:opacity-50"
          >
            {loadingPreview
              ? <Loader2 className="h-3.5 w-3.5 animate-spin" />
              : <Eye className="h-3.5 w-3.5" />}
            Preview
          </button>
        </CardBody>
      </Card>

      {previewHtml && (
        <Card>
          <CardHeader
            title="Preview"
            action={
              <button
                type="button" onClick={onClosePreview}
                className="text-xs text-[var(--text-muted)] hover:text-[var(--text)]"
              >
                close
              </button>
            }
          />
          <CardBody className="p-0">
            {/* Sandboxed: the report is platform-generated, but rendering any
                document inside the app shell without isolation would let its
                stylesheet leak into the application chrome. */}
            <iframe
              title="Report preview" srcDoc={previewHtml} sandbox=""
              className="h-[760px] w-full rounded-b-lg border-0 bg-white"
            />
          </CardBody>
        </Card>
      )}

      <Card>
        <CardHeader
          title="Sections"
          subtitle="A section without evidence says so rather than being omitted"
        />
        <CardBody className="p-0">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-[var(--border)] text-left text-xs uppercase tracking-wide text-[var(--text-muted)]">
                <th className="px-3 py-2 font-medium">Section</th>
                <th className="px-3 py-2 text-right font-medium">Blocks</th>
                <th className="px-3 py-2 text-right font-medium">Charts</th>
                <th className="px-3 py-2 text-right font-medium">Tables</th>
                <th className="px-3 py-2 text-right font-medium">Evidence</th>
                <th className="px-3 py-2 text-right font-medium">Words</th>
                <th className="px-3 py-2 font-medium">Status</th>
              </tr>
            </thead>
            <tbody>
              {report.sections.map((section) => (
                <tr key={section.key} className="border-b border-[var(--border)]">
                  <td className="px-3 py-2 text-[var(--text)]">{section.title}</td>
                  <td className="num px-3 py-2 text-right">{section.block_count}</td>
                  <td className="num px-3 py-2 text-right">{section.chart_count}</td>
                  <td className="num px-3 py-2 text-right">{section.table_count}</td>
                  <td className="num px-3 py-2 text-right">{section.evidence_count}</td>
                  <td className="num px-3 py-2 text-right">{section.word_count}</td>
                  <td className="px-3 py-2">
                    {section.sufficient
                      ? <Badge variant="gain">included</Badge>
                      : <span title={section.reason}>
                          <Badge variant="warn">insufficient evidence</Badge>
                        </span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </CardBody>
      </Card>

      <Card>
        <CardHeader title="Citation audit" subtitle="Every numeric claim must reference an engine" />
        <CardBody className="grid gap-3 sm:grid-cols-2">
          <div className="space-y-1 text-xs">
            {[
              ["Numeric claims", audit.total_claims],
              ["Supported", audit.supported_claims],
              ["Unsupported", audit.unsupported],
              ["Dangling references", (audit.dangling_markers as string[])?.length ?? 0],
            ].map(([label, value]) => (
              <div key={label as string} className="flex justify-between">
                <span className="text-[var(--text-muted)]">{label as string}</span>
                <span className="num text-[var(--text)]">{String(value ?? 0)}</span>
              </div>
            ))}
          </div>
          <div>
            <div className="mb-1 text-xs text-[var(--text-muted)]">
              Engines referenced
            </div>
            <div className="flex flex-wrap gap-1">
              {((audit.sources as string[]) ?? []).map((s) => (
                <Badge key={s} variant="accent">{s.replace(/_/g, " ")}</Badge>
              ))}
            </div>
          </div>
        </CardBody>
      </Card>
    </>
  );
}
