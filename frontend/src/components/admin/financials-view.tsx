"use client";

/**
 * Phase 3 — Enterprise Financial Statements.
 *
 * Editable income statement, balance sheet, cash flow and ratios; quarterly
 * results; shareholding pattern; corporate actions; bulk CSV/Excel/JSON import;
 * interactive trend charts; and fact-level version history with rollback.
 */

import Highcharts from "highcharts";
import HighchartsReact from "highcharts-react-official";
import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Plus, RefreshCw, Save, Trash2, UploadCloud, X,
} from "lucide-react";

import { adminApi } from "@/lib/api";
import type { CompanyAdmin, FinancialStatements } from "@/lib/types";
import { Button, Select, StatusPill, TextInput, formatWhen } from "./primitives";
import { Card, CardBody, CardHeader, Skeleton } from "@/components/ui";

type Tab = "income" | "balance" | "cashflow" | "ratios" | "quarterly" | "shareholding" | "actions";

const TABS: { key: Tab; label: string }[] = [
  { key: "income", label: "Income" },
  { key: "balance", label: "Balance Sheet" },
  { key: "cashflow", label: "Cash Flow" },
  { key: "ratios", label: "Ratios" },
  { key: "quarterly", label: "Quarterly" },
  { key: "shareholding", label: "Shareholding" },
  { key: "actions", label: "Corporate Actions" },
];

export default function FinancialsView() {
  const client = useQueryClient();
  const [company, setCompany] = useState<CompanyAdmin | null>(null);
  const [tab, setTab] = useState<Tab>("income");
  const [companyId, setCompanyId] = useState("");
  const [page, setPage] = useState(1);
  const [search, setSearch] = useState("");

  const { data: list } = useQuery({
    queryKey: ["admin", "companies", "pick", page, search],
    queryFn: () => adminApi.companies.list({ page, page_size: 25, search, sort_by: "name", order: "asc" }),
  });

  const selectCompany = (id: string) => {
    setCompanyId(id);
    adminApi.companies.get(id).then(setCompany);
  };

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <TextInput value={search} onChange={(v) => { setSearch(v); setPage(1); }} placeholder="Search company…" className="max-w-xs" />
        <Select value={companyId} onChange={selectCompany}
                options={[{ value: "", label: "Select a company…" },
                  ...(list?.results ?? []).map((c) => ({ value: c.id, label: `${c.ticker} — ${c.name}` }))]} />
        <div className="flex-1" />
        <Button variant="ghost" onClick={() => setPage((p) => p + 1)}>More…</Button>
      </div>

      {!company ? (
        <Card><CardBody className="text-xs text-[var(--text-muted)]">
          Select a company to view and edit its financial statements.
        </CardBody></Card>
      ) : (
        <>
          <div className="flex items-center gap-3">
            <div>
              <div className="font-semibold text-[var(--text)]">{company.name} <span className="num text-accent-500">({company.ticker})</span></div>
              <div className="text-xs text-[var(--text-muted)]">Sector {company.sector ?? "—"} · {company.industry ?? "—"}</div>
            </div>
            <div className="flex-1" />
            <span className="text-xs text-[var(--text-muted)]">Editing financials triggers AI recompute</span>
          </div>
          <div className="flex gap-1 overflow-x-auto pb-1">
            {TABS.map((t) => (
              <button key={t.key} onClick={() => setTab(t.key)}
                      className={tab === t.key
                        ? "shrink-0 rounded bg-accent-500/10 px-3 py-1.5 text-xs font-medium text-accent-500"
                        : "shrink-0 rounded border border-[var(--border)] px-3 py-1.5 text-xs text-[var(--text)]"}>
                {t.label}
              </button>
            ))}
          </div>
          {tab === "income" && <StatementsEditor companyId={company.id} statement="income" />}
          {tab === "balance" && <StatementsEditor companyId={company.id} statement="balance" />}
          {tab === "cashflow" && <StatementsEditor companyId={company.id} statement="cashflow" />}
          {tab === "ratios" && <RatiosView companyId={company.id} />}
          {tab === "quarterly" && <QuarterlyView companyId={company.id} />}
          {tab === "shareholding" && <ShareholdingView companyId={company.id} />}
          {tab === "actions" && <ActionsView companyId={company.id} />}
          <TrendsCharts companyId={company.id} />
          <VersionsPanel companyId={company.id} />
        </>
      )}
    </div>
  );
}

/* ==================================================================== */
const FACT_LABELS: Record<string, string> = {
  revenue: "Revenue", other_operating_income: "Other operating income",
  raw_materials: "Raw materials", purchase_stock_in_trade: "Purchase of stock",
  change_inventories: "Change in inventories", employee_benefit: "Employee benefits",
  other_expenses: "Other expenses", depreciation: "Depreciation",
  other_income: "Other income", finance_costs: "Finance costs",
  exceptional_items: "Exceptional items", tax_expense: "Tax expense",
  minority_interest: "Minority interest", oci: "OCI",
  cash_and_bank: "Cash & bank", current_investments: "Current investments",
  trade_receivables: "Trade receivables", inventories: "Inventories",
  other_current_assets: "Other current assets", net_block_ppe: "Net block (PPE)",
  cwip: "CWIP", goodwill: "Goodwill", other_intangibles: "Other intangibles",
  lt_investments_associates: "LT investments", other_nca: "Other non-current assets",
  deferred_tax_asset: "Deferred tax asset", trade_payables: "Trade payables",
  short_term_borrowings: "Short-term borrowings", current_maturities_ltd: "Current maturities",
  other_current_liabilities: "Other current liabilities", short_term_provisions: "Short-term provisions",
  long_term_borrowings: "Long-term borrowings", deferred_tax_liability: "Deferred tax liability",
  other_ncl: "Other non-current liabilities", equity_share_capital: "Equity share capital",
  reserves_surplus: "Reserves & surplus", minority_interest_bs: "Minority interest (BS)",
  other_noncash_adj: "Other non-cash adj", chg_inventories_cf: "Change in inventories",
  chg_receivables_cf: "Change in receivables", chg_payables_cf: "Change in payables",
  other_wc_movement: "Other WC movement", direct_taxes_paid: "Direct taxes paid",
  capex: "CAPEX", sale_fixed_assets: "Sale of fixed assets",
  purchase_sale_investments: "Purchase/sale investments", other_investing: "Other investing",
  equity_issued_buyback: "Equity issued/buyback", proceeds_borrowings: "Proceeds from borrowings",
  repayment_borrowings: "Repayment of borrowings",
};

function StatementsEditor({ companyId, statement }: { companyId: string; statement: "income" | "balance" | "cashflow" }) {
  const client = useQueryClient();
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [result, setResult] = useState<{ updated: number; created: number; errors: unknown[] } | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const { data, isLoading } = useQuery<FinancialStatements>({
    queryKey: ["admin", "financials", companyId, "statements"],
    queryFn: () => adminApi.companies.financials.statements(companyId),
  });

  const years = data?.years ?? [];
  const lineItems = data ? Object.keys(data.statements[years[0]]?.[statement] ?? {}) : [];
  const labels = lineItems.map((k) => FACT_LABELS[k] ?? k.replace(/_/g, " "));

  const getValue = (item: string, year: number) => {
    const st = data?.statements[year]?.[statement];
    if (st && item in st) return st[item];
    return draft[`${item}|${year}`] ?? "";
  };

  const save = useMutation({
    mutationFn: () => {
      const facts: Record<string, unknown>[] = [];
      for (const year of years) {
        for (const item of lineItems) {
          const v = draft[`${item}|${year}`];
          if (v !== undefined) facts.push({ fiscal_year: year, line_item: item, value: v === "" ? null : Number(v), precedence: 2, source: "admin-editor" });
        }
      }
      return adminApi.companies.financials.upsertFacts(companyId, facts);
    },
    onSuccess: (r) => {
      setResult(r);
      client.invalidateQueries({ queryKey: ["admin", "financials", companyId] });
      setDraft({});
    },
  });

  return (
    <Card>
      <CardHeader title={statement === "income" ? "Income statement (₹ cr)" : statement === "balance" ? "Balance sheet (₹ cr)" : "Cash flow (₹ cr)"}
                  action={<div className="flex gap-2">
                    <input ref={fileRef} type="file" accept=".csv,.xlsx,.json" className="hidden"
                           onChange={(e) => {
                             const f = e.target.files?.[0]; if (f) {
                               adminApi.companies.financials.bulkImport(companyId, "facts", f).then(setResult);
                               client.invalidateQueries({ queryKey: ["admin", "financials", companyId] });
                             }
                             e.target.value = "";
                           }} />
                    <Button variant="ghost" onClick={() => fileRef.current?.click()}><UploadCloud className="h-3.5 w-3.5" /> Import</Button>
                    <Button variant="primary" disabled={Object.keys(draft).length === 0 || save.isPending} onClick={() => save.mutate()}>
                      <Save className="h-3.5 w-3.5" /> {save.isPending ? "Saving…" : "Save changes"}
                    </Button>
                  </div>} />
      <CardBody className="p-0">
        {isLoading ? <Skeleton className="h-64" /> : (
          <div className="scroll-x max-h-[55vh] overflow-auto">
            <table className="grid-table" style={{ minWidth: years.length * 120 + 220 }}>
              <thead className="sticky top-0 bg-[var(--bg-elevated)]">
                <tr><th className="sticky left-0 bg-[var(--bg-elevated)]">Line item</th>
                  {years.map((y) => <th key={y} className="num">FY{y}</th>)}
                </tr>
              </thead>
              <tbody>
                {lineItems.map((item, i) => (
                  <tr key={item}>
                    <td className="sticky left-0 bg-[var(--bg-elevated)] text-xs">{labels[i]}</td>
                    {years.map((y) => (
                      <td key={y} className="p-1">
                        <input
                          value={getValue(item, y) ?? ""}
                          onChange={(e) => setDraft((d) => ({ ...d, [`${item}|${y}`]: e.target.value }))}
                          placeholder={getValue(item, y) === null && !draft[`${item}|${y}`] ? "—" : ""}
                          className="w-full bg-transparent px-2 py-1 text-right text-xs outline-none focus:bg-[var(--bg-subtle)]" />
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {result && (
          <div className="flex items-center justify-between border-t border-[var(--border)] px-4 py-2 text-xs">
            <span className="text-gain">Saved {result.updated} updated · {result.created} created · {result.errors.length} errors</span>
            <Button variant="ghost" onClick={() => setResult(null)}><X className="h-3.5 w-3.5" /></Button>
          </div>
        )}
      </CardBody>
    </Card>
  );
}

/* ==================================================================== */
function RatiosView({ companyId }: { companyId: string }) {
  const { data } = useQuery<FinancialStatements>({
    queryKey: ["admin", "financials", companyId, "statements"],
    queryFn: () => adminApi.companies.financials.statements(companyId),
  });
  const ratios = data?.ratios ?? {};
  const sections = Object.entries(ratios);

  return (
    <div className="grid gap-4 lg:grid-cols-2">
      {sections.map(([key, rows]) => (
        <Card key={key}>
          <CardHeader title={key} />
          <CardBody className="p-0">
            <table className="w-full text-xs">
              <thead><tr><th className="px-3 py-2 text-left text-[var(--text-muted)]">Metric</th>
                {(data?.years ?? []).map((y) => <th key={y} className="px-3 py-2 text-right num text-[var(--text-muted)]">FY{y}</th>)}</tr></thead>
              <tbody>
                {(rows ?? []).map((row) => (
                  <tr key={row.key} className="border-t border-[var(--border)]">
                    <td className="px-3 py-2">{row.label}</td>
                    {(row.values ?? []).map((v, i) => <td key={i} className="num px-3 py-2 text-right">{v == null ? "—" : Number(v).toFixed(2)}</td>)}
                  </tr>
                ))}
              </tbody>
            </table>
          </CardBody>
        </Card>
      ))}
      {sections.length === 0 && <Card><CardBody className="text-xs text-[var(--text-muted)]">No ratio data yet.</CardBody></Card>}
    </div>
  );
}

/* ==================================================================== */
function TrendsCharts({ companyId }: { companyId: string }) {
  const { data } = useQuery<FinancialStatements>({
    queryKey: ["admin", "financials", companyId, "statements"],
    queryFn: () => adminApi.companies.financials.statements(companyId),
  });
  const years = data?.years ?? [];
  if (!data || years.length < 2) return null;

  const series = (fn: (s: Record<string, number | null>) => number | null, name: string) => ({
    name, data: years.map((y) => fn(data?.statements[y]?.income ?? {})),
  });

  const chart = (title: string, s: { name: string; data: (number | null)[] }[], unit = "₹ cr") => (
    <Card>
      <CardHeader title={title} />
      <CardBody>
        <HighchartsReact highcharts={Highcharts} options={{
          chart: { type: "column", height: 240, backgroundColor: "transparent" },
          title: { text: undefined },
          xAxis: { categories: years.map((y) => `FY${y}`) },
          yAxis: { title: { text: unit }, gridLineColor: "#232b3a" },
          legend: { enabled: true, itemStyle: { color: "#cbd5e1" } },
          series: s.map((x) => ({ name: x.name, type: "column", data: x.data, color: "#1f6feb" })),
          credits: { enabled: false },
        }} />
      </CardBody>
    </Card>
  );

  const roe = data.ratios["return"]?.find((r) => r.key.includes("roe"));
  return (
    <div className="grid gap-4 lg:grid-cols-2">
      {chart("Revenue trend", [series((s) => s.total_revenue ?? null, "Revenue")])}
      {chart("Profit trend", [
        { name: "EBITDA", data: years.map((y) => data.statements[y].income.ebitda ?? null), },
        { name: "Net profit", data: years.map((y) => data.statements[y].income.pat ?? null) },
      ])}
      {chart("Cash flow trend", [
        { name: "Operating", data: years.map((y) => data.statements[y].cashflow.cfo ?? null) },
        { name: "Free cash flow", data: years.map((y) => data.statements[y].cashflow.free_cash_flow ?? null) },
      ])}
      {chart("Debt & capital", [
        { name: "Net debt", data: years.map((y) => data.statements[y].balance.net_debt ?? null) },
        { name: "Equity", data: years.map((y) => data.statements[y].balance.shareholders_equity ?? null) },
      ])}
      {roe && (
        <Card>
          <CardHeader title="Return on equity trend" />
          <CardBody><HighchartsReact highcharts={Highcharts} options={{
            chart: { type: "line", height: 240, backgroundColor: "transparent" },
            title: { text: undefined },
            xAxis: { categories: years.map((y) => `FY${y}`) },
            yAxis: { title: { text: "%" }, gridLineColor: "#232b3a" },
            series: [{ name: "ROE", type: "line", data: roe.values, color: "#0b7a3b" }],
            credits: { enabled: false },
          }} /></CardBody></Card>
      )}
    </div>
  );
}

/* ==================================================================== */
const QUARTER_FIELDS = ["revenue", "operating_profit", "net_profit", "eps", "operating_margin"];

function QuarterlyView({ companyId }: { companyId: string }) {
  const client = useQueryClient();
  const { data } = useQuery({ queryKey: ["admin", "financials", companyId, "quarterly"],
    queryFn: () => adminApi.companies.financials.quarterly(companyId) });
  const [rows, setRows] = useState<Record<string, string>[]>([]);
  const invalidate = () => client.invalidateQueries({ queryKey: ["admin", "financials", companyId] });

  const save = useMutation({
    mutationFn: () => adminApi.companies.financials.upsertQuarterly(companyId, rows.map((r) => ({
      fiscal_year: Number(r.fiscal_year), quarter: Number(r.quarter),
      ...Object.fromEntries(Object.entries(r).filter(([k, v]) => k !== "fiscal_year" && k !== "quarter" && v !== "")
        .map(([k, v]) => [k, Number(v)])),
    })).filter((r) => r.fiscal_year && r.quarter)),
    onSuccess: () => { invalidate(); setRows([]); },
  });

  const del = useMutation({
    mutationFn: ({ y, q }: { y: number; q: number }) => adminApi.companies.financials.deleteQuarterly(companyId, y, q),
    onSuccess: invalidate,
  });

  const addRow = () => setRows((rs) => [...rs, { fiscal_year: "", quarter: "", revenue: "", operating_profit: "", net_profit: "", eps: "", operating_margin: "" }]);
  const setCell = (ri: number, k: string, v: string) => setRows((rs) => rs.map((r, i) => (i === ri ? { ...r, [k]: v } : r)));

  return (
    <Card>
      <CardHeader title="Quarterly results"
                  action={<div className="flex gap-2"><Button variant="ghost" onClick={addRow}><Plus className="h-3.5 w-3.5" /> Add</Button>
                    <Button variant="primary" disabled={save.isPending} onClick={() => save.mutate()}><Save className="h-3.5 w-3.5" /> Save</Button></div>} />
      <CardBody className="space-y-3">
        {(rows.length > 0) && (
          <div className="scroll-x"><table className="w-full text-xs">
            <thead><tr><th>FY</th><th>Q</th><th>Revenue</th><th>Op Profit</th><th>Net Profit</th><th>EPS</th></tr></thead>
            <tbody>{rows.map((r, i) => (
              <tr key={i}>{["fiscal_year", "quarter", "revenue", "operating_profit", "net_profit", "eps"].map((k) => (
                <td key={k}><input value={r[k]} onChange={(e) => setCell(i, k, e.target.value)} className="w-20 bg-transparent px-2 py-1 outline-none" /></td>))}</tr>))}</tbody>
          </table></div>
        )}
        <div className="scroll-x"><table className="w-full text-xs">
          <thead><tr><th className="text-left">Period</th>{QUARTER_FIELDS.map((f) => <th key={f} className="text-right capitalize">{f.replace("_", " ")}</th>)}<th /></tr></thead>
          <tbody>{(data?.items ?? []).map((r) => { const q = r as Record<string, unknown>; return (
            <tr key={`${q.fiscal_year}-${q.quarter}`} className="border-t border-[var(--border)]">
              <td className="num py-2">Q{String(q.quarter)} FY{String(q.fiscal_year)}</td>
              {QUARTER_FIELDS.map((f) => <td key={f} className="num py-2 text-right">{q[f] == null ? "—" : Number(q[f]).toFixed(1)}</td>)}
              <td className="text-right"><Button variant="danger" onClick={() => del.mutate({ y: Number(q.fiscal_year), q: Number(q.quarter) })}><Trash2 className="h-3 w-3" /></Button></td>
            </tr>); })}
            {(data?.items ?? []).length === 0 && <tr><td colSpan={6} className="py-4 text-center text-[var(--text-muted)]">No quarterly results yet.</td></tr>}
          </tbody>
        </table></div>
      </CardBody>
    </Card>
  );
}

/* ==================================================================== */
function ShareholdingView({ companyId }: { companyId: string }) {
  const client = useQueryClient();
  const { data } = useQuery({ queryKey: ["admin", "financials", companyId, "shareholding"],
    queryFn: () => adminApi.companies.financials.shareholding(companyId) });
  const invalidate = () => client.invalidateQueries({ queryKey: ["admin", "financials", companyId] });
  const [rows, setRows] = useState<Record<string, string>[]>([]);

  const save = useMutation({
    mutationFn: () => adminApi.companies.financials.upsertShareholding(companyId, rows.map((r) => ({
      fiscal_year: Number(r.fiscal_year), quarter: Number(r.quarter),
      ...Object.fromEntries(Object.entries(r).filter(([k, v]) => k !== "fiscal_year" && k !== "quarter" && v !== "").map(([k, v]) => [k, Number(v)])),
    })).filter((r) => r.fiscal_year && r.quarter)),
    onSuccess: () => { invalidate(); setRows([]); },
  });

  const addRow = () => setRows((rs) => [...rs, { fiscal_year: "", quarter: "", promoter_indian: "", fii_fpi: "", mutual_funds: "", others_custodians: "" }]);
  const setCell = (ri: number, k: string, v: string) => setRows((rs) => rs.map((r, i) => (i === ri ? { ...r, [k]: v } : r)));

  return (
    <Card>
      <CardHeader title="Shareholding pattern (fractions)" action={<div className="flex gap-2"><Button variant="ghost" onClick={addRow}><Plus className="h-3.5 w-3.5" /> Add</Button><Button variant="primary" disabled={save.isPending} onClick={() => save.mutate()}><Save className="h-3.5 w-3.5" /> Save</Button></div>} />
      <CardBody className="space-y-3">
        {rows.length > 0 && <div className="scroll-x"><table className="w-full text-xs"><thead><tr><th>FY</th><th>Q</th><th>Promoter</th><th>FII</th><th>MF</th><th>Public</th></tr></thead><tbody>{rows.map((r, i) => (<tr key={i}>{["fiscal_year", "quarter", "promoter_indian", "fii_fpi", "mutual_funds", "others_custodians"].map((k) => (<td key={k}><input value={r[k]} onChange={(e) => setCell(i, k, e.target.value)} className="w-20 bg-transparent px-2 py-1 outline-none" /></td>))}</tr>))}</tbody></table></div>}
        <div className="scroll-x"><table className="w-full text-xs">
          <thead><tr><th className="text-left">Period</th><th className="text-right">Promoter</th><th className="text-right">FII/FPI</th><th className="text-right">MF</th><th className="text-right">Public</th></tr></thead>
          <tbody>{(data?.items ?? []).map((rr) => { const s = rr as Record<string, unknown>; return (
            <tr key={`${s.fiscal_year}-${s.quarter}`} className="border-t border-[var(--border)]">
              <td className="num py-2">Q{String(s.quarter)} FY{String(s.fiscal_year)}</td>
              {["promoter_indian", "fii_fpi", "mutual_funds", "others_custodians"].map((f) => <td key={f} className="num py-2 text-right">{s[f] == null ? "—" : `${(Number(s[f]) * 100).toFixed(1)}%`}</td>)}
            </tr>); })}
            {(data?.items ?? []).length === 0 && <tr><td colSpan={5} className="py-4 text-center text-[var(--text-muted)]">No shareholding data yet.</td></tr>}
          </tbody></table></div>
      </CardBody>
    </Card>
  );
}

/* ==================================================================== */
function ActionsView({ companyId }: { companyId: string }) {
  const client = useQueryClient();
  const { data } = useQuery({ queryKey: ["admin", "financials", companyId, "actions"],
    queryFn: () => adminApi.companies.financials.corporateActions(companyId) });
  const invalidate = () => client.invalidateQueries({ queryKey: ["admin", "financials", companyId] });
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState<Record<string, string>>({ action_type: "dividend" });

  const add = useMutation({
    mutationFn: () => adminApi.companies.financials.addAction(companyId, {
      action_type: form.action_type, ex_date: form.ex_date || null,
      record_date: form.record_date || null, value: form.value ? Number(form.value) : null,
      description: form.description || null,
    }),
    onSuccess: () => { invalidate(); setShowForm(false); setForm({ action_type: "dividend" }); },
  });
  const del = useMutation({
    mutationFn: (aid: number) => adminApi.companies.financials.deleteAction(companyId, aid),
    onSuccess: invalidate,
  });

  return (
    <Card>
      <CardHeader title="Corporate actions" action={<Button variant="primary" onClick={() => setShowForm(true)}><Plus className="h-3.5 w-3.5" /> Add action</Button>} />
      <CardBody className="space-y-3">
        {showForm && (
          <div className="grid grid-cols-2 gap-3 rounded border border-accent-500/30 p-3 sm:grid-cols-4">
            {["action_type", "ex_date", "record_date", "value", "description"].map((f) => (
              <label key={f} className="block"><span className="mb-1 block text-[0.625rem] text-[var(--text-muted)]">{f.replace("_", " ")}</span>
                <input value={form[f] ?? ""} onChange={(e) => setForm((x) => ({ ...x, [f]: e.target.value }))}
                       className="w-full rounded border border-[var(--border)] bg-[var(--bg)] px-2 py-1 text-xs" /></label>))}
            <div className="flex items-end gap-2">
              <Button variant="primary" disabled={add.isPending} onClick={() => add.mutate()}><Save className="h-3.5 w-3.5" /> Add</Button>
              <Button variant="ghost" onClick={() => setShowForm(false)}><X className="h-3.5 w-3.5" /></Button>
            </div>
          </div>
        )}
        <div className="scroll-x"><table className="w-full text-xs">
          <thead><tr><th className="text-left">Type</th><th className="text-left">Ex-date</th><th className="text-right">Value</th><th className="text-left">Description</th><th /></tr></thead>
          <tbody>{(data?.items ?? []).map((rr) => { const a = rr as Record<string, unknown>; return (
            <tr key={String(a.id)} className="border-t border-[var(--border)]">
              <td className="py-2"><StatusPill status={String(a.action_type)} /></td>
              <td className="py-2">{a.ex_date == null ? "—" : String(a.ex_date)}</td>
              <td className="num py-2 text-right">{a.value == null ? "—" : String(a.value)}</td>
              <td className="py-2 text-[var(--text-muted)]">{a.description == null ? "—" : String(a.description)}</td>
              <td className="py-2 text-right"><Button variant="danger" onClick={() => del.mutate(Number(a.id))}><Trash2 className="h-3 w-3" /></Button></td>
            </tr>); })}
            {(data?.items ?? []).length === 0 && <tr><td colSpan={5} className="py-4 text-center text-[var(--text-muted)]">No corporate actions.</td></tr>}
          </tbody></table></div>
      </CardBody>
    </Card>
  );
}

/* ==================================================================== */
function VersionsPanel({ companyId }: { companyId: string }) {
  const client = useQueryClient();
  const { data } = useQuery({ queryKey: ["admin", "financials", companyId, "versions"],
    queryFn: () => adminApi.companies.financials.versions(companyId) });
  const rollback = useMutation({
    mutationFn: (v: number) => adminApi.companies.financials.rollback(companyId, v),
    onSuccess: () => client.invalidateQueries({ queryKey: ["admin", "financials", companyId] }),
  });

  return (
    <Card>
      <CardHeader title="Financial version history" />
      <CardBody className="space-y-2">
        {(data ?? []).map((v) => (
          <div key={v.id} className="flex items-center justify-between rounded border border-[var(--border)] px-3 py-2">
            <div className="text-xs">
              <span className="font-medium text-[var(--text)]">v{v.version}</span>
              <span className="mx-2 text-[var(--text-muted)]">· {v.change_type}</span>
              <span className="text-[var(--text-muted)]">by {v.actor_email ?? "system"}</span>
            </div>
            <div className="flex items-center gap-2">
              <span className="text-[0.625rem] text-[var(--text-muted)]">{formatWhen(v.created_at)}</span>
              <Button variant="ghost" disabled={rollback.isPending} onClick={() => rollback.mutate(v.version)}><RefreshCw className="h-3 w-3" /> Rollback</Button>
            </div>
          </div>
        ))}
        {(data ?? []).length === 0 && <p className="text-xs text-[var(--text-muted)]">No edits recorded yet.</p>}
      </CardBody>
    </Card>
  );
}
