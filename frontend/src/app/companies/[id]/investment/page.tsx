"use client";

import { AppShell } from "@/components/layout/app-shell";
import { CompanyTabs } from "@/components/layout/company-tabs";
import { Card, CardBody, CardHeader, Skeleton, Badge } from "@/components/ui";
import { api, aiApi, scoringApi, valuationApi } from "@/lib/api";
import { useQuery } from "@tanstack/react-query";
import { use } from "react";
import Link from "next/link";

export default function InvestmentViewPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const profile = useQuery({ queryKey: ["company-profile", id], queryFn: () => api.companyProfile(id) });
  const score = useQuery({ queryKey: ["scoring", id], queryFn: () => scoringApi.get(id), enabled: !!profile.data });
  const valuation = useQuery({ queryKey: ["valuation", id], queryFn: () => valuationApi.get(id), enabled: !!profile.data });
  const ai = useQuery({ queryKey: ["ai-thesis", id], queryFn: () => aiApi.analyse(id, "investment_thesis"), enabled: !!profile.data });

  const ticker = profile.data?.company.ticker;

  return (
    <AppShell>
      <CompanyTabs companyId={id} />
      {profile.isLoading && <Skeleton className="h-24" />}
      {profile.data && (
        <>
          <div className="mb-4">
            <h1 className="text-lg font-semibold">{profile.data.company.name} — AI Investment View</h1>
            <p className="text-xs text-[var(--text-muted)]">Simple: grade, recommendation, positives, risks, valuation verdict, AI conclusion. Advanced: full pillars, evidence.</p>
          </div>

          <div className="grid gap-5 lg:grid-cols-3">
            <div className="lg:col-span-2 space-y-4">
              <Card>
                <CardHeader title="Verdict" subtitle="Grade • Rating • Recommendation" />
                <CardBody className="flex items-center gap-4">
                  {score.isLoading ? <Skeleton className="h-12 w-24" /> : (
                    <>
                      <div className="text-3xl font-bold num">{score.data?.grade ?? "—"}</div>
                      <div>
                        <div className="text-sm font-medium">{score.data?.recommendation ?? "—"}</div>
                        <div className="text-xs text-[var(--text-muted)]">{score.data?.stars ? `${score.data.stars} stars` : ""} • {score.data?.overall_score ? `Score ${score.data.overall_score}` : ""}</div>
                      </div>
                      <Badge variant={score.data?.grade?.startsWith("A") ? "gain" : "neutral"}>{score.data?.grade_description ?? ""}</Badge>
                    </>
                  )}
                </CardBody>
              </Card>

              <Card>
                <CardHeader title="AI Thesis (Simple)" />
                <CardBody className="text-xs leading-relaxed">
                  {ai.isLoading && <Skeleton className="h-20" />}
                  {ai.data && <p>{(ai.data as any).content?.slice(0, 600) ?? (ai.data as any).display_content?.slice(0, 600) ?? "No thesis yet."}</p>}
                  {!ai.data && !ai.isLoading && <p className="text-[var(--text-muted)]">AI thesis not generated yet.</p>}
                  <Link href={`/companies/${id}/ai`} className="mt-2 inline-block text-[0.6875rem] text-accent-500 hover:underline">Full AI analysis →</Link>
                </CardBody>
              </Card>

              <Card>
                <CardHeader title="Valuation Verdict (Simple)" />
                <CardBody className="text-xs">
                  {valuation.isLoading ? <Skeleton className="h-12" /> : (
                    <div className="flex justify-between items-center">
                      <div>
                        <div className="text-sm font-medium">{valuation.data?.summary?.recommendation ?? "—"}</div>
                        <div className="text-[var(--text-muted)]">Intrinsic {valuation.data?.summary?.weighted_value ? `₹${valuation.data.summary.weighted_value.toFixed(1)}` : "—"} vs Market {valuation.data?.summary?.current_price ? `₹${valuation.data.summary.current_price.toFixed(1)}` : "—"}</div>
                      </div>
                      <div className="text-right">
                        <div className="num text-lg font-semibold">{valuation.data?.summary?.upside !== undefined && valuation.data?.summary?.upside !== null ? `${(valuation.data.summary.upside as number) > 0 ? "+" : ""}${(valuation.data.summary.upside as number).toFixed(1)}%` : "—"}</div>
                        <div className="text-[0.6875rem] text-[var(--text-muted)]">upside</div>
                      </div>
                    </div>
                  )}
                  <Link href={`/companies/${id}/valuation`} className="mt-2 inline-block text-[0.6875rem] text-accent-500 hover:underline">Detailed valuation →</Link>
                </CardBody>
              </Card>
            </div>

            <div className="space-y-4">
              <Card>
                <CardHeader title="Positives" />
                <CardBody className="text-xs space-y-1">
                  {(score.data?.strongest?.slice(0,5) ?? []).map((s:any,i:number) => (
                    <div key={i} className="flex gap-2"><span className="text-gain">+</span><span>{typeof s === "string" ? s : s.label ?? s.key}</span></div>
                  ))}
                  {(!score.data?.strongest || score.data.strongest.length===0) && <p className="text-[var(--text-muted)]">No positives yet.</p>}
                </CardBody>
              </Card>
              <Card>
                <CardHeader title="Risks" />
                <CardBody className="text-xs space-y-1">
                  {(score.data?.weakest?.slice(0,5) ?? []).map((s:any,i:number) => (
                    <div key={i} className="flex gap-2"><span className="text-loss">!</span><span>{typeof s === "string" ? s : s.label ?? s.key}</span></div>
                  ))}
                  {(!score.data?.weakest || score.data.weakest.length===0) && <p className="text-[var(--text-muted)]">No risks flagged.</p>}
                </CardBody>
              </Card>
              <Card>
                <CardHeader title="Source" />
                <CardBody className="text-[0.6875rem] text-[var(--text-muted)]">
                  <p>ISIN: {profile.data.company.isin ?? "—"}</p>
                  <p className="mt-2">Every number traces to canonical facts, filings, and market provider with price_source.</p>
                  <Link href={`/companies/${id}/documents`} className="text-accent-500 hover:underline mt-2 inline-block">Inspect filings →</Link>
                </CardBody>
              </Card>
            </div>
          </div>
        </>
      )}
    </AppShell>
  );
}
