import { ArrowRight, BarChart3, Brain, Calculator, Database, FileText, ShieldCheck, Search, TrendingUp, Zap, Target, Users } from "lucide-react";
import Link from "next/link";

const FEATURES = [
  { icon: Database, title: "Universal Company Engine", body: "Select any NSE/BSE company and the entire model re-resolves — 54 canonical line items across ten years, one lookup." },
  { icon: BarChart3, title: "Historical Statements", body: "Income statement, balance sheet and cash flow with 45+ ratios, working capital, debt and capex analysis." },
  { icon: Calculator, title: "DCF & Relative Valuation", body: "FCFF, FCFE, WACC, sensitivity grids and bull/base/bear scenarios with a full EV-to-equity bridge." },
  { icon: ShieldCheck, title: "Institutional Scoring", body: "Eleven weighted pillars and a AAA–C rating, driving an automatic buy/hold/sell recommendation." },
  { icon: Brain, title: "AI Research Layer", body: "Provider-agnostic analysis across OpenRouter, OpenAI and Gemini — 21 research sections." },
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
    <div className="min-h-screen bg-[var(--bg)] text-[var(--text)]">
      {/* Premium Terminal Header */}
      <header className="sticky top-0 z-50 border-b border-[var(--border)] bg-[var(--bg-elevated)]/95 backdrop-blur-xl">
        <div className="mx-auto flex max-w-7xl items-center justify-between px-6 py-3">
          <div className="flex items-center gap-3">
            <div className="grid h-9 w-9 place-items-center rounded-lg bg-accent-500 text-white font-bold text-lg tracking-[-1px]">IE</div>
            <div>
              <div className="font-semibold tracking-tight">EquityPilot</div>
              <div className="text-[10px] uppercase tracking-[2px] text-[var(--text-muted)] -mt-1">INSTITUTIONAL AI RESEARCH</div>
            </div>
          </div>

          <div className="flex items-center gap-3">
            <Link href="/dashboard" className="rounded-lg bg-accent-500 px-5 py-2 text-sm font-semibold text-white hover:bg-accent-600 transition-colors flex items-center gap-2">
              Open Terminal <ArrowRight size={15} />
            </Link>
            <Link href="/companies" className="text-sm px-4 py-2 rounded-lg border border-[var(--border)] hover:bg-[var(--bg-subtle)]">Browse Companies</Link>
          </div>
        </div>
      </header>

      {/* Hero — Terminal style */}
      <div className="border-b border-[var(--border)] bg-[var(--bg)]">
        <div className="mx-auto max-w-7xl px-6 pt-16 pb-12">
          <div className="flex flex-col lg:flex-row gap-10 items-start">
            <div className="flex-1">
              <div className="inline-flex items-center gap-2 px-3 py-1 rounded-full border border-accent-500/30 bg-accent-500/10 text-xs font-medium text-accent-400 mb-4">
                <div className="w-1.5 h-1.5 bg-accent-400 rounded-full animate-pulse" />
                Bloomberg-grade • 135 companies • 42k facts
              </div>

              <h1 className="text-6xl lg:text-7xl font-semibold tracking-tighter leading-none">
                The AI equity<br />research terminal.
              </h1>
              <p className="mt-6 max-w-lg text-xl text-[var(--text-muted)]">
                Canonical financials. Institutional scoring. Grounded AI. Every number has a source.
              </p>

              <div className="mt-8 flex items-center gap-4">
                <Link href="/dashboard" className="inline-flex h-12 items-center gap-2.5 rounded-xl bg-accent-500 px-8 text-base font-semibold text-white hover:bg-accent-600 active:bg-accent-700 transition-all">
                  Launch Terminal <Zap size={17} />
                </Link>
                <Link href="/companies" className="inline-flex h-12 items-center gap-2.5 rounded-xl border px-6 font-medium hover:bg-[var(--bg-subtle)]">
                  Explore Coverage
                </Link>
              </div>

              <div className="mt-8 flex gap-8 text-sm">
                {STATS.map(([v, l]) => (
                  <div key={l}>
                    <div className="font-mono text-2xl font-semibold text-accent-400">{v}</div>
                    <div className="text-xs text-[var(--text-muted)] mt-0.5">{l}</div>
                  </div>
                ))}
              </div>
            </div>

            {/* Live AI Search Box */}
            <div className="flex-1 lg:max-w-md w-full">
              <div className="rounded-2xl border border-[var(--border)] bg-[var(--bg-elevated)] p-1 shadow-xl">
                <div className="px-5 py-4">
                  <div className="flex items-center gap-2 text-xs uppercase tracking-widest text-[var(--text-muted)] mb-3">
                    <Search size={14} /> AI RESEARCH
                  </div>
                  <input
                    type="text"
                    placeholder="Ask anything: “TCS margin expansion drivers 2025” or “high ROCE IT companies with improving guidance”"
                    className="w-full bg-transparent text-lg placeholder:text-[var(--text-muted)] focus:outline-none font-light"
                  />
                  <div className="mt-3 flex items-center gap-2 text-[10px] text-[var(--text-muted)]">
                    <kbd className="px-1.5 py-px rounded bg-[var(--bg-subtle)] border">⌘K</kbd>
                    <span>Universal search</span>
                    <span className="mx-1">•</span>
                    <span>English • Hindi • Hinglish</span>
                  </div>
                </div>
              </div>
              <div className="mt-3 text-xs text-[var(--text-muted)] flex items-center gap-2">
                <Target size={13} /> Powered by Retrieval 2.1 + Institutional AI 3.0
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* Premium Dashboard Grid */}
      <div className="mx-auto max-w-7xl px-6 py-10">
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-12 gap-4">
          {/* AI Picks */}
          <div className="lg:col-span-4 rounded-2xl border bg-[var(--bg-elevated)] p-5">
            <div className="flex justify-between items-center mb-4">
              <div className="font-semibold flex items-center gap-2"><Brain size={16} /> Top AI Picks</div>
              <Link href="/companies" className="text-xs text-accent-500">See all →</Link>
            </div>
            <div className="space-y-3 text-sm">
              {["TCS", "INFY", "HDFCBANK", "RELIANCE"].map((t, i) => (
                <div key={i} className="flex justify-between items-center py-1 border-b last:border-0 border-[var(--border)]">
                  <div className="font-mono font-medium">{t}</div>
                  <div className="flex items-center gap-3 text-xs">
                    <span className="text-emerald-500 font-semibold">92</span>
                    <span className="text-[var(--text-muted)]">Buy</span>
                  </div>
                </div>
              ))}
            </div>
          </div>

          {/* Market Summary + Heatmap */}
          <div className="lg:col-span-4 rounded-2xl border bg-[var(--bg-elevated)] p-5">
            <div className="font-semibold mb-4 flex items-center gap-2"><TrendingUp size={16} /> Market Summary</div>
            <div className="grid grid-cols-3 gap-3 text-center text-sm">
              <div><div className="text-emerald-500 font-mono text-xl">+1.8%</div><div className="text-xs text-[var(--text-muted)]">Nifty 50</div></div>
              <div><div className="text-emerald-500 font-mono text-xl">+2.1%</div><div className="text-xs text-[var(--text-muted)]">Bank Nifty</div></div>
              <div><div className="text-rose-500 font-mono text-xl">-0.4%</div><div className="text-xs text-[var(--text-muted)]">Midcap</div></div>
            </div>
            <div className="mt-4 text-[10px] text-[var(--text-muted)]">Sector Heatmap (AI-weighted)</div>
            <div className="mt-2 grid grid-cols-4 gap-1 text-[10px]">
              {["IT","BANK","AUTO","PHARMA","METAL","REALTY","FMCG","ENERGY"].map((s,i) => (
                <div key={i} className="bg-emerald-500/10 text-emerald-600 text-center py-1 rounded font-mono">{s}</div>
              ))}
            </div>
          </div>

          {/* Latest AI Insights */}
          <div className="lg:col-span-4 rounded-2xl border bg-[var(--bg-elevated)] p-5">
            <div className="font-semibold mb-3">Latest AI Insights</div>
            <div className="space-y-3 text-sm">
              <div className="text-xs">• <span className="font-medium">TCS</span>: Guidance raised on AI deal momentum. Confidence ↑</div>
              <div className="text-xs">• <span className="font-medium">RELIANCE</span>: New Energy capex accelerating. Bull case strengthened.</div>
              <div className="text-xs">• <span className="font-medium">HDFCBANK</span>: NIM pressure but asset quality stable.</div>
            </div>
            <Link href="/dashboard" className="mt-4 inline-block text-xs text-accent-500">View full timeline →</Link>
          </div>

          {/* Quick Actions */}
          <div className="lg:col-span-12 mt-2 grid grid-cols-2 md:grid-cols-4 gap-3">
            {[
              { icon: Search, label: "Universal Search", desc: "Companies • Documents • Metrics" },
              { icon: Zap, label: "Natural Language Screener", desc: "High ROCE + improving guidance" },
              { icon: Users, label: "Portfolio AI", desc: "Risk • Diversification • Allocation" },
              { icon: Target, label: "Watchlist Alerts", desc: "Score • Filing • Guidance changes" },
            ].map((item, i) => (
              <Link key={i} href="/dashboard" className="group flex gap-4 rounded-2xl border p-4 hover:border-accent-500/50 transition">
                <item.icon className="mt-0.5 text-accent-500" size={22} />
                <div>
                  <div className="font-medium group-hover:text-accent-500">{item.label}</div>
                  <div className="text-xs text-[var(--text-muted)]">{item.desc}</div>
                </div>
              </Link>
            ))}
          </div>
        </div>
      </div>

      <footer className="border-t border-[var(--border)] py-6 text-center text-xs text-[var(--text-muted)]">
        Built for professional investors • 100% traceable • No hallucinations
      </footer>
    </div>
  );
}
