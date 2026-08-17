"use client";

import { AppShell } from "@/components/layout/app-shell";
import { CompanyTabs } from "@/components/layout/company-tabs";
import { Card, CardBody, CardHeader, Skeleton } from "@/components/ui";
import { api, scoringApi } from "@/lib/api";
import { useQuery } from "@tanstack/react-query";
import { use } from "react";
import Link from "next/link";

export default function PeersPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const profile = useQuery({
    queryKey: ["company-profile", id],
    queryFn: () => api.companyProfile(id),
  });
  const ticker = profile.data?.company.ticker;
  const peers = useQuery({
    queryKey: ["peers", ticker],
    queryFn: () => scoringApi.peers(ticker!, undefined, 8),
    enabled: Boolean(ticker),
  });

  return (
    <AppShell>
      <CompanyTabs companyId={id} />
      {profile.isLoading && <Skeleton className="h-24" />}
      {profile.data && (
        <>
          <div className="mb-4">
            <h1 className="text-lg font-semibold">{profile.data.company.name} — Peer Comparison</h1>
            <p className="text-xs text-[var(--text-muted)]">Sector: {profile.data.company.sector ?? "—"} • Simple view shows 3 peers, advanced shows 8</p>
          </div>

          <Card>
            <CardHeader title="Peers (by sector & scoring)" subtitle="Simple view: 3 closest peers" />
            <CardBody>
              {peers.isLoading && <Skeleton className="h-24" />}
              {peers.data && (
                <div className="overflow-x-auto">
                  <table className="grid-table">
                    <thead>
                      <tr><th>Ticker</th><th>Score</th><th>Grade</th><th>Market Cap</th></tr>
                    </thead>
                    <tbody>
                      {peers.data.peers?.slice(0, 8).map((p: any) => (
                        <tr key={p.ticker}>
                          <td className="num"><Link href={`/companies/${p.company_id ?? p.ticker}`} className="text-accent-500 hover:underline">{p.ticker}</Link></td>
                          <td className="num">{p.overall_score ?? "—"}</td>
                          <td>{p.grade ?? "—"}</td>
                          <td className="num">{p.market_cap ? `${p.market_cap} cr` : "—"}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
              {!peers.data && !peers.isLoading && <p className="text-xs text-[var(--text-muted)]">No peer data — scoring may need to run.</p>}
            </CardBody>
          </Card>
        </>
      )}
    </AppShell>
  );
}
