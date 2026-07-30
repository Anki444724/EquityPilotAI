"use client";

import { AppShell } from "@/components/layout/app-shell";
import { ContributionChart, ScoreHistoryChart, ScoreRadar } from "@/components/charts";
import { QualityBanner } from "@/components/valuation/quality-banner";
import {
  CategoryTable, ConfidenceBar, ExplanationPanel, GradeBadge, PeerTable,
  ProfilePicker, RecommendationBadge, StarRating,
} from "@/components/scoring/panels";
import { Card, CardBody, CardHeader, EmptyState, Skeleton, Stat } from "@/components/ui";
import { api, scoringApi } from "@/lib/api";
import { EM_DASH, percent } from "@/lib/format";
import { cn } from "@/lib/utils";
import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { use, useState } from "react";

const TABS = [
  { key: "overview", label: "Overview" },
  { key: "categories", label: "Categories" },
  { key: "explanation", label: "Explanation" },
  { key: "peers", label: "Peers" },
  { key: "history", label: "History" },
] as const;
type TabKey = (typeof TABS)[number]["key"];

export default function ScoringPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const [tab, setTab] = useState<TabKey>("overview");
  const [profile, setProfile] = useState("balanced");
  const [expanded, setExpanded] = useState<string | null>(null);

  const companyProfile = useQuery({
    queryKey: ["company-profile", id],
    queryFn: () => api.companyProfile(id),
  });
  const ticker = companyProfile.data?.company.ticker;

  const score = useQuery({
    queryKey: ["scoring", ticker, profile],
    queryFn: () => scoringApi.get(ticker!, profile, true),
    enabled: Boolean(ticker),
  });

  const profiles = useQuery({
    queryKey: ["scoring-profiles"],
    queryFn: () => scoringApi.profiles(),
  });

  const explanation = useQuery({
    queryKey: ["scoring-explanation", ticker, profile],
    queryFn: () => scoringApi.explanation(ticker!, profile),
    enabled: Boolean(ticker) && tab === "explanation",
  });

  const peers = useQuery({
    queryKey: ["scoring-peers", ticker, profile],
    queryFn: () => scoringApi.peers(ticker!, profile, 5),
    enabled: Boolean(ticker) && tab === "peers",
  });

  const history = useQuery({
    queryKey: ["scoring-history", ticker, profile],
    queryFn: () => scoringApi.history(ticker!, profile),
    enabled: Boolean(ticker) && tab === "history",
  });

  if (companyProfile.isLoading) return <AppShell><Skeleton className="h-32" /></AppShell>;
  if (!companyProfile.data) {
    return <AppShell><Card><EmptyState title="Company not found" /></Card></AppShell>;
  }

  const s = score.data;

  return (
    <AppShell>
      <div className="mb-4 flex flex-wrap items-end justify-between gap-3">
        <div>
          <div className="flex items-center gap-2 text-xs">
            <Link href={`/companies/${id}`} className="num text-accent-500 hover:underline">
              {companyProfile.data.company.ticker}
            </Link>
            <span className="text-[var(--text-muted)]">/</span>
            <span className="text-[var(--text-muted)]">Institutional scoring</span>
          </div>
          <h1 className="mt-1 text-lg font-semibold">{companyProfile.data.company.name}</h1>
        </div>
        <select
          value={profile}
          onChange={(e) => setProfile(e.target.value)}
          className="rounded-md border border-[var(--border)] bg-[var(--bg-elevated)] px-2.5 py-1.5 text-xs outline-none focus:border-accent-500"
        >
          {profiles.data?.profiles.map((p) => (
            <option key={p.key} value={p.key}>{p.label}</option>
          ))}
        </select>
      </div>

      {score.isLoading && <Skeleton className="h-64" />}

      {s && (
        <>
          {s.warnings.some((w) => w.includes("Illustrative")) && (
            <QualityBanner
              quality={{
                grade: "illustrative", is_illustrative: true,
                disclosure: s.warnings.find((w) => w.includes("Illustrative")) ?? null,
                headline: "", issues: [], coverage: null, history_years: null,
                synthetic_sources: [],
              }}
            />
          )}

          {/* Headline */}
          <div className="mb-5 grid gap-4 sm:grid-cols-2 lg:grid-cols-5">
            <Card className="lg:col-span-1">
              <CardBody className="text-center">
                <div className="num text-4xl font-bold">{s.overall_score.toFixed(1)}</div>
                <div className="mt-0.5 text-[0.625rem] uppercase tracking-wider text-[var(--text-muted)]">
                  out of 100
                </div>
                <div className="mt-2 flex justify-center"><StarRating stars={s.stars} /></div>
              </CardBody>
            </Card>
            <Card><CardBody>
              <div className="text-[0.6875rem] uppercase tracking-wider text-[var(--text-muted)]">Grade</div>
              <div className="mt-1.5"><GradeBadge grade={s.grade} /></div>
              <p className="mt-1.5 text-[0.625rem] leading-snug text-[var(--text-muted)]">
                {s.grade_description}
              </p>
            </CardBody></Card>
            <Card><CardBody>
              <div className="text-[0.6875rem] uppercase tracking-wider text-[var(--text-muted)]">
                Recommendation
              </div>
              <div className="mt-1.5"><RecommendationBadge recommendation={s.recommendation} /></div>
              <p className="mt-1.5 text-[0.625rem] text-[var(--text-muted)]">
                {s.conviction} conviction
              </p>
            </CardBody></Card>
            <Card className="lg:col-span-2"><CardBody>
              <ConfidenceBar confidence={s.confidence} />
            </CardBody></Card>
          </div>

          <div className="mb-5 flex flex-wrap gap-1 border-b border-[var(--border)]">
            {TABS.map((t) => (
              <button
                key={t.key}
                onClick={() => setTab(t.key)}
                className={cn(
                  "-mb-px border-b-2 px-3 py-2 text-xs font-medium transition-colors",
                  tab === t.key
                    ? "border-accent-500 text-accent-500"
                    : "border-transparent text-[var(--text-muted)] hover:text-[var(--text)]",
                )}
              >
                {t.label}
              </button>
            ))}
          </div>

          <div className="grid gap-5 xl:grid-cols-[1fr_20rem]">
            <div className="min-w-0 space-y-5">
              {tab === "overview" && (
                <>
                  <div className="grid gap-5 lg:grid-cols-2">
                    <Card>
                      <CardHeader title="Score radar" subtitle="All thirteen categories, 0–10" />
                      <CardBody>
                        <ScoreRadar
                          categories={s.categories.map((c) => c.label)}
                          series={[{ name: s.company.ticker, data: s.categories.map((c) => c.raw_score) }]}
                        />
                      </CardBody>
                    </Card>
                    <Card>
                      <CardHeader
                        title="Weighted contribution"
                        subtitle="What actually drives the composite"
                      />
                      <CardBody>
                        <ContributionChart
                          categories={s.categories.map((c) => c.label)}
                          contributions={s.categories.map((c) => c.weighted_score)}
                        />
                      </CardBody>
                    </Card>
                  </div>

                  <Card className="border-accent-500/25">
                    <CardHeader title="Assessment" />
                    <CardBody className="space-y-2">
                      <p className="text-sm leading-relaxed">{s.summary}</p>
                      <p className="text-xs leading-relaxed text-[var(--text-muted)]">
                        {s.recommendation_rationale}
                      </p>
                      {s.warnings.length > 0 && (
                        <ul className="space-y-1 pt-1">
                          {s.warnings.map((w) => (
                            <li key={w} className="text-[0.6875rem] text-warn">⚠ {w}</li>
                          ))}
                        </ul>
                      )}
                    </CardBody>
                  </Card>
                </>
              )}

              {tab === "categories" && (
                <CategoryTable
                  categories={s.categories}
                  expanded={expanded}
                  onToggle={(k) => setExpanded(expanded === k ? null : k)}
                />
              )}

              {tab === "explanation" && (
                <>
                  {explanation.isLoading && <Skeleton className="h-64" />}
                  {explanation.data && <ExplanationPanel data={explanation.data} />}
                </>
              )}

              {tab === "peers" && (
                <>
                  {peers.isLoading && <Skeleton className="h-48" />}
                  {peers.data && (
                    <>
                      <PeerTable data={peers.data} subject={s.company.ticker} />
                      <Card>
                        <CardHeader
                          title="Radar vs peer median"
                          subtitle="Dashed line is the sector median"
                        />
                        <CardBody>
                          <ScoreRadar
                            categories={s.categories.map((c) => c.label)}
                            series={[
                              { name: s.company.ticker, data: s.categories.map((c) => c.raw_score) },
                              {
                                name: "Peer median", dashed: true, color: "#8b5cf6",
                                data: s.categories.map(
                                  (c) => peers.data!.category_medians[c.key] ?? null,
                                ),
                              },
                            ]}
                          />
                        </CardBody>
                      </Card>
                    </>
                  )}
                </>
              )}

              {tab === "history" && (
                <Card>
                  <CardHeader
                    title="Score history"
                    subtitle={
                      history.data?.score_change != null
                        ? `${history.data.score_change >= 0 ? "+" : ""}${history.data.score_change.toFixed(1)} points · ${history.data.trend}`
                        : "Snapshots are recorded each time a score is run"
                    }
                  />
                  <CardBody>
                    {history.data && history.data.points.length > 1 ? (
                      <ScoreHistoryChart
                        labels={history.data.points.map((p) => p.as_of)}
                        scores={history.data.points.map((p) => p.overall_score)}
                      />
                    ) : (
                      <p className="py-8 text-center text-xs text-[var(--text-muted)]">
                        Only one snapshot on file. A trend appears once the score has been
                        run on more than one date.
                      </p>
                    )}
                  </CardBody>
                </Card>
              )}
            </div>

            <div className="min-w-0 space-y-5">
              {profiles.data && (
                <ProfilePicker
                  profiles={profiles.data.profiles}
                  active={profile}
                  onSelect={setProfile}
                />
              )}
              <Card>
                <CardHeader title="Strongest" />
                <CardBody className="space-y-1">
                  {s.strongest.map((label) => (
                    <div key={label} className="text-xs text-gain">▲ {label}</div>
                  ))}
                </CardBody>
              </Card>
              <Card>
                <CardHeader title="Weakest" />
                <CardBody className="space-y-1">
                  {s.weakest.map((label) => (
                    <div key={label} className="text-xs text-loss">▼ {label}</div>
                  ))}
                </CardBody>
              </Card>
            </div>
          </div>
        </>
      )}
    </AppShell>
  );
}
