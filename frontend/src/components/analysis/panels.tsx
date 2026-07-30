"use client";

import { Badge, Card, CardBody, CardHeader, EmptyState, Stat } from "@/components/ui";
import { crore, EM_DASH, multiple, percent } from "@/lib/format";
import type {
  DebtResponse, ShareholdingResponse, WorkingCapitalResponse,
} from "@/lib/types";
import { cn } from "@/lib/utils";
import { CheckCircle2, XCircle } from "lucide-react";

/** Debt-specific panels: instruments, maturity ladder, covenants. */
export function DebtPanels({ data }: { data: DebtResponse }) {
  const rec = data.reconciliation;
  return (
    <div className="space-y-5">
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Card><CardBody>
          <Stat label="Blended rate" value={percent(data.blended_rate, 2)} hint="amount-weighted" />
        </CardBody></Card>
        <Card><CardBody>
          <Stat label="Floating share" value={percent(data.floating_rate_share, 0)} hint="rate-reset exposure" />
        </CardBody></Card>
        <Card><CardBody>
          <Stat label="FX-denominated" value={percent(data.foreign_currency_share, 0)} hint="currency exposure" />
        </CardBody></Card>
        <Card><CardBody>
          <Stat
            label="Gross debt"
            value={crore(rec?.balance_sheet_gross_debt ?? null)}
            hint={rec?.reconciled ? "schedule reconciled" : "schedule does not reconcile"}
            tone={rec?.reconciled ? "default" : "loss"}
          />
        </CardBody></Card>
      </div>

      {data.instruments.length > 0 && (
        <Card>
          <CardHeader
            title="Instrument schedule"
            subtitle="Latest reported facilities"
            action={
              rec?.reconciled ? (
                <Badge variant="gain"><CheckCircle2 size={10} /> Reconciled to balance sheet</Badge>
              ) : (
                <Badge variant="loss"><XCircle size={10} /> Out by {crore(rec?.difference ?? null)}</Badge>
              )
            }
          />
          <div className="overflow-x-auto">
            <table className="grid-table">
              <thead>
                <tr>
                  <th className="!text-left">Instrument</th>
                  <th className="!text-left">Security</th>
                  <th className="!text-left">Rate type</th>
                  <th>Amount</th><th>Share</th><th>Rate</th><th>Maturity</th>
                  <th className="!text-left">Ccy</th>
                </tr>
              </thead>
              <tbody>
                {data.instruments.map((ins, i) => (
                  <tr key={`${ins.instrument}-${i}`}>
                    <td className="sticky-col">{ins.instrument}</td>
                    <td className="!text-left text-xs text-[var(--text-muted)]">{ins.security}</td>
                    <td className="!text-left text-xs text-[var(--text-muted)]">{ins.rate_type}</td>
                    <td className="num">{crore(ins.amount)}</td>
                    <td className="num">{percent(ins.share_of_debt, 1)}</td>
                    <td className="num">{percent(ins.interest_rate, 2)}</td>
                    <td className="num">{ins.maturity_year ?? EM_DASH}</td>
                    <td className="!text-left text-xs text-[var(--text-muted)]">{ins.currency}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}

      <div className="grid gap-5 lg:grid-cols-2">
        {data.maturity_ladder.length > 0 && (
          <Card>
            <CardHeader title="Maturity ladder" subtitle="Scheduled repayments by year" />
            <CardBody className="space-y-3">
              {data.maturity_ladder.map((b) => (
                <div key={b.year}>
                  <div className="flex items-baseline justify-between gap-2 text-xs">
                    <span className="num font-medium">{b.year}</span>
                    <span className="num text-[var(--text-muted)]">
                      {crore(b.amount)} · {percent(b.share_of_debt, 0)} ·{" "}
                      {b.ebitda_coverage ? `${multiple(b.ebitda_coverage)} EBITDA cover` : EM_DASH}
                    </span>
                  </div>
                  <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-[var(--bg-subtle)]">
                    <div
                      className="h-full rounded-full bg-accent-500"
                      style={{ width: `${(b.share_of_debt ?? 0) * 100}%` }}
                    />
                  </div>
                </div>
              ))}
            </CardBody>
          </Card>
        )}

        <Card>
          <CardHeader title="Covenant compliance" subtitle="Tested on the latest reported period" />
          <div className="overflow-x-auto">
            <table className="grid-table">
              <thead>
                <tr>
                  <th className="!text-left">Covenant</th>
                  <th>Threshold</th><th>Actual</th><th>Headroom</th><th>Status</th>
                </tr>
              </thead>
              <tbody>
                {data.covenants.map((c) => (
                  <tr key={c.key}>
                    <td className="sticky-col">{c.label}</td>
                    <td className="num text-[var(--text-muted)]">
                      {c.direction === "max" ? "≤" : "≥"} {c.threshold.toFixed(2)}
                    </td>
                    <td className="num">{c.actual === null ? EM_DASH : c.actual.toFixed(2)}</td>
                    <td className={cn("num", (c.headroom ?? 0) < 0 && "text-loss")}>
                      {c.headroom === null ? EM_DASH : percent(c.headroom, 0)}
                    </td>
                    <td>
                      {c.compliant === null ? (
                        <span className="text-[var(--text-muted)]">{EM_DASH}</span>
                      ) : c.compliant ? (
                        <Badge variant="gain">Pass</Badge>
                      ) : (
                        <Badge variant="loss">Breach</Badge>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      </div>
    </div>
  );
}

/** Shareholding header: ownership signal and latest split. */
export function ShareholdingHeader({ data }: { data: ShareholdingResponse }) {
  if (!data.signal) return null;
  const tone =
    data.signal.signal.includes("Accumulation") ? "gain"
      : data.signal.signal.includes("Distribution") ? "loss"
      : "neutral";
  return (
    <Card className="mb-5">
      <CardBody className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <div className="text-[0.6875rem] uppercase tracking-wider text-[var(--text-muted)]">
            Ownership signal
          </div>
          <div className="mt-1 flex items-center gap-2">
            <Badge variant={tone as "gain" | "loss" | "neutral"}>{data.signal.signal}</Badge>
            {data.signal.score !== null && (
              <span className="num text-xs text-[var(--text-muted)]">
                score {data.signal.score > 0 ? "+" : ""}{data.signal.score}
              </span>
            )}
          </div>
        </div>
        {data.signal.detail && (
          <p className="max-w-md text-xs text-[var(--text-muted)]">{data.signal.detail}</p>
        )}
      </CardBody>
    </Card>
  );
}

/** Working-capital header: the cycle at a glance. */
export function WorkingCapitalHeader({ data }: { data: WorkingCapitalResponse }) {
  const find = (key: string) => {
    for (const s of data.sections) {
      const row = s.rows.find((r) => r.key === key);
      if (row) return row.values[row.values.length - 1];
    }
    return null;
  };
  const tiles: [string, number | null, string][] = [
    ["Inventory days", find("dio"), "DIO on COGS"],
    ["Receivable days", find("dso"), "DSO on revenue"],
    ["Payable days", find("dpo"), "DPO on COGS"],
    ["Cash conversion cycle", find("ccc"), "DIO + DSO − DPO"],
  ];
  return (
    <div className="mb-5 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
      {tiles.map(([label, value, hint]) => (
        <Card key={label}>
          <CardBody>
            <Stat
              label={label}
              value={value === null ? EM_DASH : value.toFixed(1)}
              hint={hint}
            />
          </CardBody>
        </Card>
      ))}
    </div>
  );
}

export function NoData({ label }: { label: string }) {
  return (
    <Card>
      <EmptyState
        title={`No ${label} data`}
        description="Figures are never fabricated. Import the underlying disclosures to populate this view."
      />
    </Card>
  );
}
