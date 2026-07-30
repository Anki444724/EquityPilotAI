"use client";

import { Badge, Card, CardBody, CardHeader } from "@/components/ui";
import type { DriverOut } from "@/lib/types";
import { cn } from "@/lib/utils";
import { Info, RotateCcw, Sparkles } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

/**
 * Generic assumption editor.
 *
 * Renders whatever drivers the API returns, grouped and formatted using the
 * metadata the backend supplies. It holds no knowledge of what any particular
 * assumption means — adding a driver on the server makes it appear here with
 * no frontend change.
 */

/** Percent-style drivers are edited in points; the API stores fractions. */
function toDisplay(d: DriverOut): number {
  return d.unit === "%" ? d.value * 100 : d.value;
}
function toApi(d: DriverOut, shown: number): number {
  return d.unit === "%" ? shown / 100 : shown;
}

function stepFor(unit: string): number {
  if (unit === "%") return 0.25;
  if (unit === "days") return 1;
  if (unit === "x") return 0.5;
  return 0.05;
}

const SOURCE_STYLE: Record<string, { label: string; variant: "gain" | "accent" | "warn" | "neutral" }> = {
  historical: { label: "History", variant: "gain" },
  analyst: { label: "Analyst", variant: "accent" },
  ai_extracted: { label: "AI", variant: "warn" },
  management_guidance: { label: "Guidance", variant: "warn" },
  default: { label: "Default", variant: "neutral" },
};

export function AssumptionEditor({
  drivers,
  onApply,
  isSaving,
}: {
  drivers: DriverOut[];
  onApply: (changes: Record<string, number>) => void;
  isSaving: boolean;
}) {
  const [edits, setEdits] = useState<Record<string, number>>({});

  // Discard pending edits whenever the server sends a new assumption set.
  useEffect(() => setEdits({}), [drivers]);

  const groups = useMemo(() => {
    const map = new Map<string, DriverOut[]>();
    for (const d of drivers) {
      if (!map.has(d.group)) map.set(d.group, []);
      map.get(d.group)!.push(d);
    }
    return [...map.entries()];
  }, [drivers]);

  const dirty = Object.keys(edits).length;

  const apply = () => {
    const payload: Record<string, number> = {};
    for (const [name, shown] of Object.entries(edits)) {
      const d = drivers.find((x) => x.name === name);
      if (d) payload[name] = toApi(d, shown);
    }
    onApply(payload);
  };

  return (
    <Card>
      <CardHeader
        title="Assumptions"
        subtitle={`${drivers.length} drivers · edit any value and re-run`}
        action={
          <div className="flex items-center gap-2">
            {dirty > 0 && (
              <button
                onClick={() => setEdits({})}
                className="flex items-center gap-1 rounded border border-[var(--border)] px-2 py-1 text-[0.6875rem] text-[var(--text-muted)] hover:text-[var(--text)]"
              >
                <RotateCcw size={10} /> Reset
              </button>
            )}
            <button
              onClick={apply}
              disabled={dirty === 0 || isSaving}
              className={cn(
                "rounded px-3 py-1 text-[0.6875rem] font-semibold transition-colors",
                dirty > 0 && !isSaving
                  ? "bg-accent-500 text-white hover:bg-accent-600"
                  : "cursor-not-allowed bg-[var(--bg-subtle)] text-[var(--text-muted)]",
              )}
            >
              {isSaving ? "Running…" : dirty > 0 ? `Apply ${dirty}` : "No changes"}
            </button>
          </div>
        }
      />
      <CardBody className="max-h-[34rem] space-y-4 overflow-y-auto">
        {groups.map(([group, items]) => (
          <div key={group}>
            <div className="mb-1.5 text-[0.625rem] font-semibold uppercase tracking-[0.08em] text-[var(--text-muted)]">
              {group}
            </div>
            <div className="space-y-1">
              {items.map((d) => {
                const shown = edits[d.name] ?? toDisplay(d);
                const changed = d.name in edits;
                const style = SOURCE_STYLE[d.source] ?? SOURCE_STYLE.default;
                return (
                  <div
                    key={d.name}
                    className={cn(
                      "flex items-center gap-2 rounded px-1.5 py-1",
                      changed && "bg-accent-500/10",
                    )}
                  >
                    <span className="flex min-w-0 flex-1 items-center gap-1.5">
                      <span className="truncate text-xs">{d.label}</span>
                      {(d.note || d.citation) && (
                        <span
                          title={d.citation ? `${d.note ?? ""} — ${d.citation}` : d.note ?? ""}
                          className="shrink-0 cursor-help text-[var(--text-muted)]"
                        >
                          <Info size={10} />
                        </span>
                      )}
                    </span>
                    <Badge variant={style.variant} className="shrink-0 !text-[0.5625rem]">
                      {d.source === "ai_extracted" && <Sparkles size={8} />}
                      {style.label}
                    </Badge>
                    <input
                      type="number"
                      step={stepFor(d.unit)}
                      value={Number.isFinite(shown) ? Number(shown.toFixed(4)) : 0}
                      onChange={(e) =>
                        setEdits((prev) => ({ ...prev, [d.name]: Number(e.target.value) }))
                      }
                      className="num w-20 shrink-0 rounded border border-[var(--border)] bg-[var(--bg-subtle)] px-1.5 py-0.5 text-right text-xs outline-none focus:border-accent-500"
                    />
                    <span className="w-8 shrink-0 text-[0.625rem] text-[var(--text-muted)]">
                      {d.unit === "₹ cr" ? "cr" : d.unit}
                    </span>
                  </div>
                );
              })}
            </div>
          </div>
        ))}
      </CardBody>
    </Card>
  );
}
