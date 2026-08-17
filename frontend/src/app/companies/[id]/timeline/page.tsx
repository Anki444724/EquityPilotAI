"use client";

import { AppShell } from "@/components/layout/app-shell";
import { CompanyTabs } from "@/components/layout/company-tabs";
import { Card, CardBody, CardHeader, Skeleton } from "@/components/ui";
import { api } from "@/lib/api";
import { useQuery } from "@tanstack/react-query";
import { use } from "react";

export default function TimelinePage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const profile = useQuery({
    queryKey: ["company-profile", id],
    queryFn: () => api.companyProfile(id),
  });

  return (
    <AppShell>
      <CompanyTabs companyId={id} />
      {profile.isLoading && <Skeleton className="h-24" />}
      {profile.data && (
        <>
          <div className="mb-4">
            <h1 className="text-lg font-semibold">{profile.data.company.name} — Knowledge Timeline</h1>
            <p className="text-xs text-[var(--text-muted)]">Filings, news, AI scores, and document intelligence over time</p>
          </div>

          <Card>
            <CardHeader title="Timeline" subtitle="Temporal knowledge vault" />
            <CardBody className="space-y-3 text-xs">
              <div className="text-[var(--text-muted)]">
                <p>Coming soon: chronological view of:</p>
                <ul className="list-disc ml-4 mt-2 space-y-1">
                  <li>Annual filings (10-K, 20-F) and investor presentations</li>
                  <li>Quarterly results and shareholding changes</li>
                  <li>AI scoring versions (every change recorded, never overwritten)</li>
                  <li>Document ingestion events (OCR, chunking, embeddings)</li>
                  <li>Market news and corporate actions</li>
                </ul>
                <p className="mt-3">Backend already has `knowledge/vault` and `temporal` enrichment. UI will render as timeline with source links.</p>
                <p className="mt-2">Ticker: {profile.data.company.ticker} • ISIN: {profile.data.company.isin ?? "—"}</p>
              </div>
            </CardBody>
          </Card>
        </>
      )}
    </AppShell>
  );
}
