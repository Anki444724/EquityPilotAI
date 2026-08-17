"use client";

import { AppShell } from "@/components/layout/app-shell";
import { CompanyTabs } from "@/components/layout/company-tabs";
import { Card, CardBody, CardHeader, Skeleton } from "@/components/ui";
import { api } from "@/lib/api";
import { useQuery } from "@tanstack/react-query";
import { use } from "react";

export default function NewsPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const profile = useQuery({
    queryKey: ["company-profile", id],
    queryFn: () => api.companyProfile(id),
  });
  const ticker = profile.data?.company.ticker;

  // Market endpoint includes news when include_news=True (default in snapshot)
  const market = useQuery({
    queryKey: ["market", ticker],
    queryFn: async () => {
      const res = await fetch(`${process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000"}/api/v1/market/${ticker}`, {
        headers: { "Content-Type": "application/json" },
        credentials: "include",
      });
      if (!res.ok) throw new Error("Failed to fetch market");
      return res.json();
    },
    enabled: Boolean(ticker),
  });

  return (
    <AppShell>
      <CompanyTabs companyId={id} />
      {profile.isLoading && <Skeleton className="h-24" />}
      {profile.data && (
        <>
          <div className="mb-4">
            <h1 className="text-lg font-semibold">{profile.data.company.name} — News</h1>
            <p className="text-xs text-[var(--text-muted)]">Recent company news from market provider (simple view: 3 items)</p>
          </div>

          <Card>
            <CardHeader title="Recent News" subtitle="From Financial Modeling Prep / Finnhub when available" />
            <CardBody className="space-y-3">
              {market.isLoading && <Skeleton className="h-20" />}
              {market.data?.news && market.data.news.length > 0 ? (
                market.data.news.slice(0, 10).map((n: any, i: number) => (
                  <div key={i} className="border-b border-[var(--border)] pb-2 last:border-0">
                    <div className="text-xs font-medium">{n.title ?? n.text ?? "Untitled"}</div>
                    <div className="text-[0.6875rem] text-[var(--text-muted)] mt-1">{n.publishedDate ?? n.date ?? ""} • {n.site ?? n.source ?? ""}</div>
                    {n.url && <a href={n.url} target="_blank" className="text-[0.6875rem] text-accent-500 hover:underline">Read →</a>}
                  </div>
                ))
              ) : (
                <div className="text-xs text-[var(--text-muted)]">
                  <p>No news available yet. Market provider news is fetched when include_news=True.</p>
                  <p className="mt-2">Ticker: {ticker}</p>
                  <p className="mt-2 text-[0.6875rem]">Advanced: Full news list with sentiment remains under Advanced Research.</p>
                </div>
              )}
            </CardBody>
          </Card>
        </>
      )}
    </AppShell>
  );
}
