"use client";

import { AppShell } from "@/components/layout/app-shell";
import { CompanyTabs } from "@/components/layout/company-tabs";
import {
  CapabilityPicker, CitationList, GuardrailPanel, Markdown, ProviderPanel, RunMeta,
} from "@/components/ai/panels";
import { Badge, Card, CardBody, CardHeader, EmptyState, Skeleton, TabStrip } from "@/components/ui";
import { LanguageSelector, storedLanguage } from "@/components/ai/language-selector";
import { aiApi, api } from "@/lib/api";
import { cn } from "@/lib/utils";
import { useMutation, useQuery } from "@tanstack/react-query";
import { AlertTriangle, Info, Send, Sparkles } from "lucide-react";
import Link from "next/link";
import { use, useEffect, useRef, useState } from "react";

const TABS = [
  { key: "analysis", label: "Analysis" },
  { key: "chat", label: "Chat" },
  { key: "report", label: "Report" },
  { key: "evidence", label: "Evidence" },
] as const;
type TabKey = (typeof TABS)[number]["key"];

interface ChatTurn {
  role: "user" | "assistant";
  text: string;
  meta?: string;
  /** BCP-47 tag, so the browser renders Devanagari with the right font
   *  and a screen reader switches voice. Without `lang`, Devanagari falls
   *  back to whatever glyphs the Latin font happens to carry. */
  lang?: string;
}

export default function AIPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const [tab, setTab] = useState<TabKey>("analysis");
  const [capability, setCapability] = useState("investment_thesis");
  const [question, setQuestion] = useState("");
  const [turns, setTurns] = useState<ChatTurn[]>([]);
  const [language, setLanguage] = useState(() => {
    // Lazy initializer: safe on client, avoids setState-in-effect
    if (typeof window === "undefined") return "auto";
    try {
      return storedLanguage();
    } catch {
      return "auto";
    }
  });
  const [lastLanguage, setLastLanguage] = useState<{
    detected?: string; translated?: boolean; note?: string;
  }>({});
  const scrollRef = useRef<HTMLDivElement>(null);

  const profile = useQuery({
    queryKey: ["company-profile", id],
    queryFn: () => api.companyProfile(id),
  });
  const ticker = profile.data?.company.ticker;

  const capabilities = useQuery({
    queryKey: ["ai-capabilities"], queryFn: () => aiApi.capabilities(),
  });
  const providers = useQuery({
    queryKey: ["ai-providers"], queryFn: () => aiApi.providers(),
  });

  const analysis = useQuery({
    queryKey: ["ai-analysis", ticker, capability],
    queryFn: () => aiApi.analyse(ticker!, capability),
    enabled: Boolean(ticker) && tab === "analysis",
  });

  const evidence = useQuery({
    queryKey: ["ai-context", ticker],
    queryFn: () => aiApi.context(ticker!),
    enabled: Boolean(ticker) && tab === "evidence",
  });

  const report = useQuery({
    queryKey: ["ai-report", ticker],
    queryFn: () => aiApi.report(ticker!),
    enabled: Boolean(ticker) && tab === "report",
  });

  const ask = useMutation({
    mutationFn: (q: string) => aiApi.chat(ticker!, q, "workspace", language),
    onSuccess: (data) => {
      const block = data.language;
      setLastLanguage({
        detected: block?.detected?.language,
        translated: block?.translation?.translated,
        note: block?.translation?.detail || undefined,
      });
      setTurns((prev) => [
        ...prev,
        {
          role: "assistant", text: data.display_content,
          lang: block?.bcp47,
          meta: [
            data.provider,
            `${data.total_tokens} tokens`,
            `${data.citations.length} citations`,
            block && block.language !== "english" ? block.native_label : null,
          ].filter(Boolean).join(" · "),
        },
      ]);
      requestAnimationFrame(() =>
        scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight }));
    },
  });

  const send = () => {
    const q = question.trim();
    if (!q || ask.isPending) return;
    setTurns((prev) => [...prev, { role: "user", text: q }]);
    setQuestion("");
    ask.mutate(q);
  };

  if (profile.isLoading) return <AppShell><Skeleton className="h-32" /></AppShell>;
  if (!profile.data) {
    return <AppShell><Card><EmptyState title="Company not found" /></Card></AppShell>;
  }

  const aiEnabled = capabilities.data?.ai_enabled ?? false;
  const liveProvider = providers.data?.providers.some(
    (p) => p.configured && p.name !== "Offline");

  return (
    <AppShell>
      <CompanyTabs companyId={id} />
      <div className="mb-4 flex flex-wrap items-end justify-between gap-3">
        <div>
          <div className="flex items-center gap-2 text-xs">
            <Link href={`/companies/${id}`} className="num text-accent-500 hover:underline">
              {profile.data.company.ticker}
            </Link>
            <span className="text-[var(--text-muted)]">/</span>
            <span className="text-[var(--text-muted)]">AI research analyst</span>
          </div>
          <h1 className="mt-1 text-lg font-semibold">{profile.data.company.name}</h1>
        </div>
        <Badge variant={aiEnabled ? "gain" : "loss"}>
          <Sparkles size={10} /> {aiEnabled ? "AI ready" : "AI unavailable"}
        </Badge>
      </div>

      {!liveProvider && (
        <div className="mb-5 rounded-lg border border-warn/50 bg-warn/10 px-4 py-3">
          <div className="flex items-start gap-3">
            <Info size={16} className="mt-0.5 shrink-0 text-warn" />
            <div>
              <p className="text-sm font-semibold text-warn">
                Running on the offline analyst — no live provider key is configured.
              </p>
              <p className="mt-1 text-xs leading-relaxed text-[var(--text-muted)]">
                Output is composed strictly from the platform&apos;s own cited figures, so
                grounding, citations and guardrails behave exactly as they will in
                production. Set an OpenRouter, OpenAI or Gemini key and the router
                will prefer it automatically — no code changes.
              </p>
            </div>
          </div>
        </div>
      )}

      <TabStrip className="mb-4 lg:mb-5" label="AI research sections">
        {TABS.map((t) => (
          <button key={t.key} onClick={() => setTab(t.key)} data-active={tab === t.key} role="tab" aria-selected={tab === t.key}
            className={cn("-mb-px border-b-2 px-3 py-2 text-xs font-medium transition-colors",
              tab === t.key ? "border-accent-500 text-accent-500"
                : "border-transparent text-[var(--text-muted)] hover:text-[var(--text)]")}>
            {t.label}
          </button>
        ))}
      </TabStrip>

      <div className="grid min-w-0-all gap-5 xl:grid-cols-[1fr_21rem]">
        <div className="min-w-0 space-y-5">
          {tab === "analysis" && (
            <>
              {analysis.isFetching && <Skeleton className="h-64" />}
              {analysis.data && !analysis.isFetching && (
                <>
                  <Card>
                    <CardHeader
                      title={capabilities.data?.capabilities.find(
                        (c) => c.key === capability)?.label ?? capability}
                      subtitle="Generated from platform figures only"
                    />
                    <CardBody>
                      <Markdown text={analysis.data.display_content} />
                      <div className="mt-4 border-t border-[var(--border)] pt-3">
                        <RunMeta result={analysis.data} />
                      </div>
                    </CardBody>
                  </Card>
                  {analysis.data.warnings.length > 0 && (
                    <Card className="border-warn/40">
                      <CardBody className="space-y-1">
                        {analysis.data.warnings.map((w) => (
                          <p key={w} className="flex items-start gap-1.5 text-[0.6875rem] text-warn">
                            <AlertTriangle size={11} className="mt-px shrink-0" /> {w}
                          </p>
                        ))}
                      </CardBody>
                    </Card>
                  )}
                </>
              )}
            </>
          )}

          {tab === "chat" && (
            <Card className="flex h-[34rem] flex-col">
              <div className="flex flex-wrap items-start justify-between gap-3 border-b border-[var(--border)] p-4">
                <div className="min-w-0">
                  <h3 className="text-sm font-semibold">Analyst chat</h3>
                  <p className="text-xs text-[var(--text-muted)]">
                    Answers are grounded in platform data. Ask in English,
                    हिन्दी or Hinglish — the reply follows your language.
                  </p>
                </div>
                <LanguageSelector
                  value={language}
                  onChange={setLanguage}
                  detected={lastLanguage.detected}
                  translated={lastLanguage.translated}
                  note={lastLanguage.note}
                />
              </div>
              <div ref={scrollRef} className="flex-1 space-y-3 overflow-y-auto p-4">
                {turns.length === 0 && (
                  <div className="py-12 text-center">
                    <Sparkles size={22} className="mx-auto text-[var(--text-muted)] opacity-50" />
                    <p className="mt-2 text-xs text-[var(--text-muted)]">
                      Ask about the financials, valuation or score. The analyst answers only
                      from figures the platform computed.
                    </p>
                  </div>
                )}
                {turns.map((turn, i) => (
                  <div key={i} className={cn("flex", turn.role === "user" && "justify-end")}>
                    <div
                      lang={turn.lang}
                      className={cn(
                        "max-w-[85%] rounded-lg px-3 py-2",
                        turn.role === "user"
                          ? "bg-accent-500 text-white"
                          : "border border-[var(--border)] bg-[var(--bg-subtle)]",
                      )}
                    >
                      {turn.role === "user"
                        ? <p className="text-xs">{turn.text}</p>
                        : <Markdown text={turn.text} />}
                      {turn.meta && (
                        <p className="mt-1.5 text-[0.5625rem] text-[var(--text-muted)]">
                          {turn.meta}
                        </p>
                      )}
                    </div>
                  </div>
                ))}
                {ask.isPending && (
                  <div className="flex gap-1 px-3">
                    {[0, 1, 2].map((i) => (
                      <span key={i}
                        className="h-1.5 w-1.5 animate-pulse rounded-full bg-accent-500"
                        style={{ animationDelay: `${i * 120}ms` }} />
                    ))}
                  </div>
                )}
              </div>
              <div className="flex gap-2 border-t border-[var(--border)] p-3">
                <input
                  value={question}
                  onChange={(e) => setQuestion(e.target.value)}
                  onKeyDown={(e) => e.key === "Enter" && send()}
                  placeholder="Ask in English, हिन्दी or Hinglish…"
                  className="flex-1 rounded-md border border-[var(--border)] bg-[var(--bg-subtle)] px-3 py-2 text-xs outline-none focus:border-accent-500"
                />
                <button onClick={send} disabled={ask.isPending || !question.trim()}
                  className="rounded-md bg-accent-500 px-3 py-2 text-white transition-colors hover:bg-accent-600 disabled:opacity-40">
                  <Send size={13} />
                </button>
              </div>
            </Card>
          )}

          {tab === "report" && (
            <>
              {report.isFetching && <Skeleton className="h-96" />}
              {report.data && !report.isFetching && (
                <>
                  <Card className="border-accent-500/30">
                    <CardBody className="flex flex-wrap items-center justify-between gap-2">
                      <span className="text-xs font-medium">
                        {report.data.sections.length} sections ·{" "}
                        {report.data.total_tokens.toLocaleString()} tokens · $
                        {report.data.total_cost_usd.toFixed(6)}
                      </span>
                      <Badge variant="accent">{report.data.generated_with}</Badge>
                    </CardBody>
                  </Card>
                  {report.data.sections.map((s) => (
                    <Card key={s.capability}>
                      <CardHeader
                        title={s.label}
                        action={s.is_supported
                          ? <Badge variant="gain">{s.citations.length} citations</Badge>
                          : <Badge variant="warn">unsupported</Badge>}
                      />
                      <CardBody><Markdown text={s.content} /></CardBody>
                    </Card>
                  ))}
                  <p className="text-[0.625rem] italic text-[var(--text-muted)]">
                    {report.data.disclosure}
                  </p>
                </>
              )}
            </>
          )}

          {tab === "evidence" && (
            <>
              {evidence.isLoading && <Skeleton className="h-96" />}
              {evidence.data && (
                <Card>
                  <CardHeader
                    title="Grounded evidence"
                    subtitle="Exactly what the model is permitted to see — nothing else"
                    action={<Badge variant="accent">{evidence.data.citation_count} figures</Badge>}
                  />
                  <div className="max-h-[36rem] overflow-y-auto">
                    <table className="grid-table">
                      <thead>
                        <tr>
                          <th className="!text-left">Key</th>
                          <th className="!text-left">Label</th>
                          <th>Value</th>
                          <th className="!text-left">Kind</th>
                          <th className="!text-left">Source</th>
                        </tr>
                      </thead>
                      <tbody>
                        {evidence.data.citations.map((c) => (
                          <tr key={c.key}>
                            <td className="sticky-col num text-[0.625rem]">{c.key}</td>
                            <td className="!text-left text-[0.6875rem]">{c.label}</td>
                            <td className="num text-[0.6875rem]">
                              {typeof c.value === "number"
                                ? c.unit === "%"
                                  ? `${(c.value * 100).toFixed(2)}%`
                                  : c.value.toLocaleString(undefined, { maximumFractionDigits: 2 })
                                : c.value ?? "—"}
                            </td>
                            <td className="!text-left">
                              <Badge className="!text-[0.5625rem]">{c.kind}</Badge>
                            </td>
                            <td className="!text-left text-[0.5625rem] text-[var(--text-muted)]">
                              {c.source}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                  {evidence.data.unavailable.length > 0 && (
                    <CardBody className="border-t border-[var(--border)]">
                      <p className="text-[0.6875rem] text-warn">
                        Unavailable to the model: {evidence.data.unavailable.join(", ")}
                      </p>
                    </CardBody>
                  )}
                </Card>
              )}
            </>
          )}
        </div>

        <div className="min-w-0 space-y-5">
          {capabilities.data && tab === "analysis" && (
            <CapabilityPicker
              capabilities={capabilities.data.capabilities}
              active={capability}
              onSelect={setCapability}
              busy={analysis.isFetching}
            />
          )}
          {tab === "analysis" && analysis.data && (
            <>
              <GuardrailPanel
                guardrails={analysis.data.guardrails}
                audit={analysis.data.citation_audit}
              />
              <Card>
                <CardHeader title="Evidence cited" subtitle="Every claim traces to these" />
                <CardBody><CitationList citations={analysis.data.citations} /></CardBody>
              </Card>
            </>
          )}
          {providers.data && <ProviderPanel providers={providers.data.providers} />}
        </div>
      </div>
    </AppShell>
  );
}
