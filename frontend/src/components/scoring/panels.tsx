"use client";

import { Badge, Card, CardBody, CardHeader, Stat } from "@/components/ui";
import { EM_DASH, percent } from "@/lib/format";
import type {
  CategoryScoreOut, ConfidenceOut, ExplanationResponse, PeerComparisonResponse,
  ScoreResponse, WeightProfileOut,
} from "@/lib/types";
import { cn } from "@/lib/utils";
import { AlertTriangle, Sparkles, Star } from "lucide-react";

/** Star rating in half-star steps. */
export function StarRating({ stars, size = 16 }: { stars: number; size?: number }) {
  return (
    <span className="inline-flex items-center gap-0.5" title={`${stars} of 5`}>
      {[1, 2, 3, 4, 5].map((i) => {
        const filled = stars >= i;
        const half = !filled && stars >= i - 0.5;
        return (
          <span key={i} className="relative inline-block" style={{ width: size, height: size }}>
            <Star size={size} className="absolute text-[var(--border-strong)]" />
            {(filled || half) && (
              <span
                className="absolute overflow-hidden"
                style={{ width: filled ? size : size / 2, height: size }}
              >
                <Star size={size} className="text-warn" fill="currentColor" />
              </span>
            )}
          </span>
        );
      })}
    </span>
  );
}

/** Grade badge, coloured by band. */
export function GradeBadge({ grade }: { grade: string }) {
  const tone =
    ["AAA", "AA"].includes(grade) ? "gain"
      : ["A", "BBB"].includes(grade) ? "accent"
      : grade === "BB" ? "warn" : "loss";
  return <Badge variant={tone as "gain" | "accent" | "warn" | "loss"}>{grade}</Badge>;
}

/** Recommendation badge. */
export function RecommendationBadge({ recommendation }: { recommendation: string }) {
  const tone =
    recommendation === "BUY" ? "gain"
      : recommendation === "ACCUMULATE" ? "gain"
      : recommendation === "HOLD" ? "accent"
      : recommendation === "REDUCE" ? "warn" : "loss";
  return (
    <Badge variant={tone as "gain" | "accent" | "warn" | "loss"} className="!text-xs !px-2 !py-1">
      {recommendation}
    </Badge>
  );
}

/**
 * Confidence breakdown.
 *
 * Shown alongside every score because a composite built on thin data is a
 * different claim from one built on complete filings.
 */
export function ConfidenceBar({ confidence }: { confidence: ConfidenceOut }) {
  const segments = [
    ["Verified", confidence.verified_pct, "bg-gain"],
    ["Estimated", confidence.estimated_pct, "bg-accent-500"],
    ["Analyst", confidence.analyst_pct, "bg-warn"],
    ["Missing", confidence.missing_pct, "bg-[var(--border-strong)]"],
  ] as const;

  return (
    <div>
      <div className="flex items-baseline justify-between">
        <span className="text-[0.6875rem] uppercase tracking-wider text-[var(--text-muted)]">
          Confidence
        </span>
        <span className="num text-xs font-medium">
          {percent(confidence.confidence, 0)}
          <span className="ml-1.5 text-[var(--text-muted)]">{confidence.label}</span>
        </span>
      </div>
      <div className="mt-1.5 flex h-2.5 overflow-hidden rounded-full bg-[var(--bg-subtle)]">
        {segments.map(([label, share, colour]) =>
          share > 0 ? (
            <div
              key={label}
              className={colour}
              style={{ width: `${share * 100}%` }}
              title={`${label}: ${(share * 100).toFixed(0)}%`}
            />
          ) : null,
        )}
      </div>
      <div className="mt-1.5 flex flex-wrap gap-x-3 gap-y-0.5 text-[0.625rem] text-[var(--text-muted)]">
        {segments.map(([label, share, colour]) => (
          <span key={label} className="inline-flex items-center gap-1">
            <span className={cn("h-1.5 w-1.5 rounded-full", colour)} />
            {label} {percent(share, 0)}
          </span>
        ))}
      </div>
      <p className="mt-1.5 text-[0.625rem] text-[var(--text-muted)]">
        {confidence.metrics_missing} of {confidence.metrics_total} inputs unavailable.
      </p>
    </div>
  );
}

/** Category table with expandable metric detail. */
export function CategoryTable({
  categories, expanded, onToggle,
}: {
  categories: CategoryScoreOut[];
  expanded: string | null;
  onToggle: (key: string) => void;
}) {
  return (
    <Card>
      <CardHeader
        title="Category scores"
        subtitle="Click a category to see the metrics behind it"
      />
      <div className="overflow-x-auto">
        <table className="grid-table">
          <thead>
            <tr>
              <th className="!text-left">Category</th>
              <th>Raw</th><th>Weight</th><th>Contribution</th>
              <th>Confidence</th><th className="!text-left">Assessment</th>
            </tr>
          </thead>
          <tbody>
            {categories.map((c) => (
              <>
                <tr
                  key={c.key}
                  onClick={() => onToggle(c.key)}
                  className={cn("cursor-pointer", expanded === c.key && "is-subtotal")}
                >
                  <td className="sticky-col">
                    <span className="flex items-center gap-2">
                      <span
                        className={cn(
                          "h-1.5 w-1.5 shrink-0 rounded-full",
                          c.raw_score >= 8 ? "bg-gain"
                            : c.raw_score >= 6.5 ? "bg-accent-500"
                            : c.raw_score >= 5 ? "bg-warn" : "bg-loss",
                        )}
                      />
                      {c.label}
                    </span>
                  </td>
                  <td className="num font-medium">{c.raw_score.toFixed(2)}</td>
                  <td className="num">{percent(c.weight, 1)}</td>
                  <td className="num">{c.weighted_score.toFixed(3)}</td>
                  <td className={cn("num", c.confidence.confidence < 0.5 && "text-warn")}>
                    {percent(c.confidence.confidence, 0)}
                  </td>
                  <td className="!text-left text-[0.6875rem] text-[var(--text-muted)]">
                    {c.grade_hint}
                  </td>
                </tr>
                {expanded === c.key && (
                  <tr key={`${c.key}-detail`}>
                    <td colSpan={6} className="!bg-[var(--bg-subtle)] !text-left">
                      <div className="space-y-2 py-2">
                        <p className="text-xs leading-relaxed">{c.explanation}</p>
                        <table className="w-full">
                          <tbody>
                            {c.metrics.map((m) => (
                              <tr key={m.key} className="align-top">
                                <td className="w-52 py-1 pr-3 text-[0.6875rem]">{m.label}</td>
                                <td className="num w-14 py-1 text-right text-[0.6875rem]">
                                  {m.score.toFixed(1)}
                                </td>
                                <td className="w-20 py-1 pl-2">
                                  <Badge
                                    variant={
                                      m.origin === "verified" ? "gain"
                                        : m.origin === "estimated" ? "accent"
                                        : m.origin === "analyst" ? "warn" : "neutral"
                                    }
                                    className="!text-[0.5625rem]"
                                  >
                                    {m.origin}
                                  </Badge>
                                </td>
                                <td className="py-1 pl-2 text-[0.6875rem] text-[var(--text-muted)]">
                                  {m.explanation}
                                </td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                        {c.data_sources.length > 0 && (
                          <p className="text-[0.625rem] text-[var(--text-muted)]">
                            Sources: {c.data_sources.join(" · ")}
                          </p>
                        )}
                      </div>
                    </td>
                  </tr>
                )}
              </>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

/** Weight profile selector with the philosophy each encodes. */
export function ProfilePicker({
  profiles, active, onSelect,
}: {
  profiles: WeightProfileOut[];
  active: string;
  onSelect: (key: string) => void;
}) {
  return (
    <Card>
      <CardHeader title="Weight profile" subtitle="Each encodes a different investment philosophy" />
      <CardBody className="space-y-2">
        {profiles.map((p) => (
          <button
            key={p.key}
            onClick={() => onSelect(p.key)}
            className={cn(
              "w-full rounded-md border px-3 py-2 text-left transition-colors",
              active === p.key
                ? "border-accent-500 bg-accent-500/10"
                : "border-[var(--border)] hover:border-[var(--border-strong)]",
            )}
          >
            <div className="flex items-center justify-between gap-2">
              <span className="text-xs font-medium">{p.label}</span>
              {!p.is_builtin && <Badge className="!text-[0.5625rem]">custom</Badge>}
            </div>
            <p className="mt-0.5 text-[0.625rem] leading-relaxed text-[var(--text-muted)]">
              {p.description}
            </p>
            <p className="mt-1 text-[0.5625rem] text-accent-500">
              Emphasis: {p.top_categories.join(", ")}
            </p>
          </button>
        ))}
      </CardBody>
    </Card>
  );
}

/** AI-ready explanation feed. */
export function ExplanationPanel({ data }: { data: ExplanationResponse }) {
  const Section = ({
    title, items, tone,
  }: {
    title: string;
    items: ExplanationResponse["key_positives"];
    tone: "gain" | "loss" | "neutral";
  }) => (
    <Card>
      <CardHeader title={title} />
      <CardBody className="space-y-2">
        {items.length === 0 && (
          <p className="text-xs text-[var(--text-muted)]">Nothing to report.</p>
        )}
        {items.map((item, i) => (
          <div key={`${item.category}-${item.metric}-${i}`} className="flex gap-2.5">
            <span
              className={cn(
                "num mt-px w-9 shrink-0 rounded px-1 py-0.5 text-center text-[0.625rem] font-semibold",
                tone === "gain" ? "bg-gain/15 text-gain"
                  : tone === "loss" ? "bg-loss/15 text-loss"
                  : "bg-[var(--bg-subtle)] text-[var(--text-muted)]",
              )}
            >
              {item.score.toFixed(1)}
            </span>
            <div className="min-w-0">
              <p className="text-xs leading-relaxed">{item.explanation}</p>
              <p className="mt-0.5 text-[0.625rem] text-[var(--text-muted)]">
                {item.category_label}
                {item.source && ` · ${item.source}`}
              </p>
            </div>
          </div>
        ))}
      </CardBody>
    </Card>
  );

  return (
    <div className="space-y-5">
      <Card className="border-accent-500/30">
        <CardHeader
          title="Investment summary"
          action={<Badge variant="accent"><Sparkles size={10} /> AI-ready</Badge>}
        />
        <CardBody className="space-y-2">
          <p className="text-sm leading-relaxed">{data.summary}</p>
          <p className="text-xs leading-relaxed text-[var(--text-muted)]">
            {data.recommendation_rationale}
          </p>
        </CardBody>
      </Card>

      <div className="grid gap-5 lg:grid-cols-2">
        <Section title="Key positives" items={data.key_positives} tone="gain" />
        <Section title="Key negatives" items={data.key_negatives} tone="loss" />
      </div>

      {data.data_gaps.length > 0 && (
        <Card className="border-warn/30">
          <CardHeader
            title="Data gaps"
            subtitle={`${data.data_gaps.length} inputs could not be assessed`}
            action={<AlertTriangle size={14} className="text-warn" />}
          />
          <CardBody>
            <ul className="grid gap-1 sm:grid-cols-2">
              {data.data_gaps.map((gap, i) => (
                <li key={i} className="text-[0.6875rem] text-[var(--text-muted)]">
                  <span className="text-[var(--text)]">{gap.metric_label}</span>
                  {" — "}{gap.explanation}
                </li>
              ))}
            </ul>
          </CardBody>
        </Card>
      )}
    </div>
  );
}

/** Peer comparison table. */
export function PeerTable({ data, subject }: { data: PeerComparisonResponse; subject: string }) {
  return (
    <Card>
      <CardHeader title="Peer comparison" subtitle="Scored on identical assumptions" />
      <div className="overflow-x-auto">
        <table className="grid-table">
          <thead>
            <tr>
              <th className="!text-left">Company</th><th>Score</th><th>Grade</th>
              <th>Stars</th><th>Confidence</th><th className="!text-left">Recommendation</th>
            </tr>
          </thead>
          <tbody>
            {data.peers.map((p) => (
              <tr
                key={p.company.id}
                className={cn(p.company.ticker === subject && "is-subtotal")}
              >
                <td className="sticky-col">
                  <span className="flex items-center gap-2">
                    <span className="num text-[0.6875rem] font-semibold">{p.company.ticker}</span>
                    <span className="truncate text-[0.6875rem] text-[var(--text-muted)]">
                      {p.company.name}
                    </span>
                  </span>
                </td>
                <td className="num font-medium">{p.overall_score.toFixed(1)}</td>
                <td><GradeBadge grade={p.grade} /></td>
                <td className="num">{p.stars.toFixed(1)}</td>
                <td className="num">{percent(p.confidence, 0)}</td>
                <td className="!text-left"><RecommendationBadge recommendation={p.recommendation} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}
