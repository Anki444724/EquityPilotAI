import { ArrowRight, BarChart3, Brain, Calculator, Database, FileText, ShieldCheck } from "lucide-react";
import Link from "next/link";

const FEATURES = [
  { icon: Database, title: "Universal Company Engine", body: "Select any NSE/BSE company and the entire model re-resolves — 54 canonical line items across ten years, one lookup." },
  { icon: BarChart3, title: "Historical Statements", body: "Income statement, balance sheet and cash flow with 45+ ratios, working capital, debt and capex analysis." },
  { icon: Calculator, title: "DCF & Relative Valuation", body: "FCFF, FCFE, WACC, sensitivity grids and bull/base/bear scenarios with a full EV-to-equity bridge." },
  { icon: ShieldCheck, title: "Institutional Scoring", body: "Eleven weighted pillars and a AAA–C rating, driving an automatic buy/hold/sell recommendation." },
  { icon: Brain, title: "AI Research Layer", body: "Provider-agnostic analysis across OpenRouter, OpenAI, Claude and Gemini — 21 research sections." },
  { icon: FileText, title: "Report Generation", body: "Investment-committee memos exported to PDF, Excel and Word." },
];

const STATS = [
  ["54", "Canonical line items"],
  ["45+", "Financial ratios"],
  ["11", "Scoring pillars"],
  ["21", "AI research sections"],
];

export default function LandingPage() {
  return (
    <div className="min-h-screen bg-navy-950 text-white">
      <header className="sticky top-0 z-20 border-b border-white/10 bg-navy-950/85 backdrop-blur">
        <div className="mx-auto flex max-w-6xl items-center justify-between px-6 py-3.5">
          <div className="flex items-center gap-2.5">
            <div className="grid h-8 w-8 place-items-center rounded bg-accent-500 text-sm font-bold">IE</div>
            <div className="leading-tight">
              <div className="text-sm font-semibold">Equity Research</div>
              <div className="text-[0.625rem] uppercase tracking-wider text-white/45">Institutional</div>
            </div>
          </div>
          <Link
            href="/dashboard"
            className="rounded-md bg-accent-500 px-4 py-2 text-xs font-semibold transition-colors hover:bg-accent-600"
          >
            Open Terminal
          </Link>
        </div>
      </header>

      <section className="mx-auto max-w-6xl px-6 py-20 text-center lg:py-28">
        <span className="inline-flex items-center gap-2 rounded-full border border-accent-500/30 bg-accent-500/10 px-3 py-1 text-[0.6875rem] font-medium text-accent-400">
          <span className="h-1.5 w-1.5 rounded-full bg-accent-400" />
          Institutional-grade research infrastructure
        </span>
        <h1 className="mx-auto mt-6 max-w-3xl text-4xl font-bold leading-[1.1] tracking-tight lg:text-6xl">
          Equity research at
          <span className="text-accent-400"> institutional depth</span>
        </h1>
        <p className="mx-auto mt-5 max-w-2xl text-base leading-relaxed text-white/65">
          Ten years of canonical financials, a full DCF and relative valuation stack, eleven-pillar
          institutional scoring and an AI research layer — for any listed Indian company.
        </p>
        <div className="mt-9 flex flex-wrap items-center justify-center gap-3">
          <Link
            href="/dashboard"
            className="inline-flex items-center gap-2 rounded-md bg-accent-500 px-6 py-3 text-sm font-semibold transition-colors hover:bg-accent-600"
          >
            Launch Platform <ArrowRight size={15} />
          </Link>
          <Link
            href="/companies"
            className="rounded-md border border-white/20 px-6 py-3 text-sm font-semibold transition-colors hover:bg-white/5"
          >
            Browse Coverage
          </Link>
        </div>

        <div className="mx-auto mt-16 grid max-w-3xl grid-cols-2 gap-6 border-y border-white/10 py-8 lg:grid-cols-4">
          {STATS.map(([v, l]) => (
            <div key={l}>
              <div className="num !text-3xl font-bold text-accent-400">{v}</div>
              <div className="mt-1 text-[0.6875rem] uppercase tracking-wider text-white/45">{l}</div>
            </div>
          ))}
        </div>
      </section>

      <section className="mx-auto max-w-6xl px-6 pb-24">
        <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
          {FEATURES.map(({ icon: Icon, title, body }) => (
            <div
              key={title}
              className="rounded-lg border border-white/10 bg-white/[0.03] p-5 transition-colors hover:border-accent-500/40"
            >
              <Icon size={20} className="text-accent-400" />
              <h3 className="mt-3.5 text-sm font-semibold">{title}</h3>
              <p className="mt-1.5 text-xs leading-relaxed text-white/55">{body}</p>
            </div>
          ))}
        </div>
      </section>

      <footer className="border-t border-white/10 px-6 py-6 text-center text-[0.6875rem] text-white/40">
        Financial logic derived from Institutional_Equity_Research_Platform_v7.xlsx —
        54 canonical line items, 11,647 formulas, verified 0 critical defects.
      </footer>
    </div>
  );
}
