import { useEffect, useState } from "react";
import {
  BarChart, Bar, XAxis, YAxis, ResponsiveContainer, Tooltip, CartesianGrid, Legend,
} from "recharts";
import { RefreshCw, Download, Calculator } from "lucide-react";
import { api } from "../lib/api";
import type { Connection } from "../lib/api";
import type { RoiAssumptions, RoiReport } from "../lib/types";
import { useToast } from "../components/Toast";

const WINDOWS = [
  { id: "7", label: "7 days" },
  { id: "30", label: "30 days" },
  { id: "all", label: "All time" },
];

const input =
  "w-full rounded-lg border border-line dark:border-line-dark bg-bg dark:bg-bg-dark px-2.5 py-2 text-sm font-mono text-ink-900 dark:text-ink-dark-900 outline-none focus:border-teal focus:ring-2 focus:ring-teal/20";

function StatCard({ label, value, sub, accent }: { label: string; value: string; sub?: string; accent?: boolean }) {
  return (
    <div className="bg-surface dark:bg-surface-dark border border-line dark:border-line-dark rounded-xl2 shadow-panel p-5">
      <p className="font-mono text-[11px] tracking-[0.14em] uppercase text-ink-400 dark:text-ink-dark-400 mb-2">{label}</p>
      <p className={`font-display text-3xl ${accent ? "text-teal-700 dark:text-teal" : "text-ink-900 dark:text-ink-dark-900"}`}>{value}</p>
      {sub && <p className="text-xs text-ink-400 dark:text-ink-dark-400 mt-1">{sub}</p>}
    </div>
  );
}

export function RoiPage({ connection }: { connection: Connection }) {
  const [report, setReport] = useState<RoiReport | null>(null);
  const [days, setDays] = useState("7");
  const [loading, setLoading] = useState(true);
  const [assumptions, setAssumptions] = useState<RoiAssumptions | null>(null);
  const [savingAssumptions, setSavingAssumptions] = useState(false);
  const { toast } = useToast();

  async function load(window: string) {
    setLoading(true);
    try {
      const r = await api.getRoi(connection, window);
      setReport(r);
      setAssumptions(r.assumptions);
    } catch {
      toast("error", "Failed to load ROI data");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load(days);
    // eslint-disable-next-line
  }, [days]);

  async function saveAssumptions(e: React.FormEvent) {
    e.preventDefault();
    if (!assumptions) return;
    setSavingAssumptions(true);
    try {
      const res = await api.updateRoiSettings(connection, assumptions);
      setAssumptions(res.assumptions);
      toast("success", "Assumptions saved — estimates recomputed.");
      load(days);
    } catch {
      toast("error", "Could not save assumptions");
    } finally {
      setSavingAssumptions(false);
    }
  }

  function exportCsv() {
    if (!report) return;
    const header = "date,tickets,auto_sent,hours_saved,labor_saved_usd,llm_cost_usd,net_usd";
    const rows = report.series.map((p) =>
      [p.date, p.tickets, p.auto_sent, p.hours_saved, p.labor_saved_usd, p.llm_cost_usd, p.net_usd].join(","),
    );
    const blob = new Blob([[header, ...rows].join("\n")], { type: "text/csv" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `roi-${report.days}-days.csv`;
    a.click();
    URL.revokeObjectURL(url);
  }

  const chartData = report?.series ?? [];

  return (
    <div className="max-w-5xl">
      <header className="flex items-end justify-between mb-6">
        <div>
          <p className="font-mono text-[11px] tracking-[0.18em] uppercase text-ink-400 dark:text-ink-dark-400 mb-1">Impact</p>
          <h1 className="font-display text-3xl text-ink-900 dark:text-ink-dark-900">ROI &amp; time saved</h1>
          <p className="text-sm text-ink-600 dark:text-ink-dark-600 mt-1.5 leading-relaxed max-w-2xl">
            LLM spend is measured. Time saved is estimated from{" "}
            <em>your</em> assumptions about how long a human takes per ticket — edit them below.
            Drafts get partial credit (reviewing beats writing from scratch), and unanswered tickets get none.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <div className="flex rounded-lg border border-line dark:border-line-dark overflow-hidden text-xs">
            {WINDOWS.map((w) => (
              <button
                key={w.id}
                onClick={() => setDays(w.id)}
                className={`px-3 py-2 font-medium transition-colors ${
                  days === w.id
                    ? "bg-ink-900 dark:bg-white text-white dark:text-ink-900"
                    : "text-ink-600 dark:text-ink-dark-600 hover:bg-ink-900/5 dark:hover:bg-white/5"
                }`}
              >
                {w.label}
              </button>
            ))}
          </div>
          <button
            onClick={() => load(days)}
            className="flex items-center gap-1.5 text-xs text-ink-600 dark:text-ink-dark-600 hover:text-ink-900 dark:hover:text-ink-dark-900 px-3 py-2 rounded-lg hover:bg-ink-900/5 dark:hover:bg-white/5 transition-colors"
          >
            <RefreshCw className="w-3.5 h-3.5" />
          </button>
        </div>
      </header>

      {loading || !report ? (
        <p className="text-sm text-ink-400 dark:text-ink-dark-400">Loading…</p>
      ) : (
        <>
          <div className="grid grid-cols-4 gap-4 mb-6">
            <StatCard label="Hours saved (est.)" value={`${report.hours_saved.toFixed(1)} h`} sub={`${report.window.auto_sent} auto-sent · ${report.window.drafts_reviewed} drafts approved`} />
            <StatCard
              label="Labor saved (est.)"
              value={`$${report.labor_saved_usd.toFixed(0)}`}
              sub={`at $${report.assumptions.hourly_rate_usd.toFixed(0)}/hour`}
            />
            <StatCard label="LLM cost (measured)" value={`$${report.window.llm_cost_usd.toFixed(4)}`} sub={report.cost_per_ticket_usd != null ? `$${report.cost_per_ticket_usd.toFixed(4)} per ticket` : undefined} />
            <StatCard
              label="Net savings"
              value={`${report.net_savings_usd < 0 ? "-" : ""}$${Math.abs(report.net_savings_usd).toFixed(2)}`}
              sub={report.roi_percent != null ? `${report.roi_percent > 0 ? "" : ""}${report.roi_percent}% ROI on LLM spend` : "No LLM spend in window"}
              accent={report.net_savings_usd > 0}
            />
          </div>

          <section className="bg-surface dark:bg-surface-dark border border-line dark:border-line-dark rounded-xl2 shadow-panel p-5 mb-6">
            <div className="flex items-center justify-between mb-4">
              <h2 className="font-display text-lg text-ink-900 dark:text-ink-dark-900">Daily savings vs. spend</h2>
              <button
                onClick={exportCsv}
                disabled={chartData.length === 0}
                className="flex items-center gap-1.5 text-xs text-ink-600 dark:text-ink-dark-600 hover:text-ink-900 dark:hover:text-ink-dark-900 px-3 py-1.5 rounded-lg hover:bg-ink-900/5 dark:hover:bg-white/5 disabled:opacity-40 transition-colors"
              >
                <Download className="w-3.5 h-3.5" /> Export CSV
              </button>
            </div>
            {chartData.length === 0 ? (
              <p className="text-sm text-ink-400 dark:text-ink-dark-400">No tickets in this window yet.</p>
            ) : (
              <ResponsiveContainer width="100%" height={240}>
                <BarChart data={chartData}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#DBE1E8" vertical={false} />
                  <XAxis
                    dataKey="date"
                    tick={{ fontSize: 10, fontFamily: "IBM Plex Mono", fill: "#7C8CA0" }}
                    axisLine={{ stroke: "#DBE1E8" }}
                    tickLine={false}
                  />
                  <YAxis
                    tick={{ fontSize: 11, fontFamily: "IBM Plex Mono", fill: "#7C8CA0" }}
                    axisLine={false}
                    tickLine={false}
                    tickFormatter={(v) => `$${v}`}
                  />
                  <Tooltip
                    contentStyle={{ borderRadius: 10, border: "1px solid #DBE1E8", fontSize: 12 }}
                    formatter={(v: number, name: string) => [
                      `$${Number(v).toFixed(2)}`,
                      name === "labor_saved_usd" ? "Est. labor saved" : "LLM cost",
                    ]}
                  />
                  <Legend wrapperStyle={{ fontSize: 11 }} />
                  <Bar dataKey="labor_saved_usd" name="labor_saved_usd" radius={[6, 6, 0, 0]} fill="#2E8C82" />
                  <Bar dataKey="llm_cost_usd" name="llm_cost_usd" radius={[6, 6, 0, 0]} fill="#6B5CA5" />
                </BarChart>
              </ResponsiveContainer>
            )}
          </section>

          <div className="grid grid-cols-2 gap-6">
            <section className="bg-surface dark:bg-surface-dark border border-line dark:border-line-dark rounded-xl2 shadow-panel p-5">
              <h2 className="font-display text-lg text-ink-900 dark:text-ink-dark-900 mb-1 flex items-center gap-2">
                <Calculator className="w-4 h-4 text-teal" /> Assumptions
              </h2>
              <p className="text-xs text-ink-600 dark:text-ink-dark-600 mb-4 leading-relaxed">
                Every estimated number above is computed from these three values. Measure your own
                team's handling time for the truest figures.
              </p>
              <form onSubmit={saveAssumptions} className="space-y-3">
                <div className="grid grid-cols-3 gap-3">
                  <div>
                    <label className="block text-[11px] font-medium text-ink-600 dark:text-ink-dark-600 mb-1">Min / auto-sent</label>
                    <input
                      type="number" min={1} max={240} step={1}
                      value={assumptions?.minutes_per_auto_sent ?? 10}
                      onChange={(e) => setAssumptions((a) => (a ? { ...a, minutes_per_auto_sent: Number(e.target.value) } : a))}
                      className={input}
                    />
                  </div>
                  <div>
                    <label className="block text-[11px] font-medium text-ink-600 dark:text-ink-dark-600 mb-1">Min / draft</label>
                    <input
                      type="number" min={1} max={240} step={1}
                      value={assumptions?.minutes_per_draft ?? 5}
                      onChange={(e) => setAssumptions((a) => (a ? { ...a, minutes_per_draft: Number(e.target.value) } : a))}
                      className={input}
                    />
                  </div>
                  <div>
                    <label className="block text-[11px] font-medium text-ink-600 dark:text-ink-dark-600 mb-1">$/hour</label>
                    <input
                      type="number" min={1} max={1000} step={1}
                      value={assumptions?.hourly_rate_usd ?? 25}
                      onChange={(e) => setAssumptions((a) => (a ? { ...a, hourly_rate_usd: Number(e.target.value) } : a))}
                      className={input}
                    />
                  </div>
                </div>
                <button
                  type="submit"
                  disabled={savingAssumptions}
                  className="text-xs bg-teal text-white rounded-lg px-4 py-2 font-medium hover:bg-teal-700 disabled:opacity-40 transition-colors"
                >
                  {savingAssumptions ? "Saving…" : "Update assumptions"}
                </button>
              </form>
            </section>

            <section className="bg-surface dark:bg-surface-dark border border-line dark:border-line-dark rounded-xl2 shadow-panel p-5">
              <h2 className="font-display text-lg text-ink-900 dark:text-ink-dark-900 mb-4">Window breakdown</h2>
              <dl className="space-y-2.5 text-sm">
                {[
                  ["Tickets", String(report.window.total)],
                  ["Auto-sent (zero touch)", String(report.window.auto_sent)],
                  ["Drafts reviewed by a human", String(report.window.drafts_reviewed)],
                  ["Drafts edited before send", `${report.window.edited} (${Math.round(report.draft_edit_rate * 100)}%)`],
                  ["Channels", Object.entries(report.by_channel).map(([c, n]) => `${c} ×${n}`).join(", ") || "–"],
                ].map(([k, v]) => (
                  <div key={k} className="flex items-baseline justify-between gap-4 border-b border-line/60 dark:border-line-dark/60 pb-2">
                    <dt className="text-ink-600 dark:text-ink-dark-600">{k}</dt>
                    <dd className="font-mono text-ink-900 dark:text-ink-dark-900 text-right">{v}</dd>
                  </div>
                ))}
              </dl>
              <p className="text-[11px] text-ink-400 dark:text-ink-dark-400 mt-4 leading-relaxed">
                Edit rate is the honest quality signal: how often humans changed the AI's draft
                before sending. The credibility of the savings estimate rides on keeping it low.
              </p>
            </section>
          </div>
        </>
      )}
    </div>
  );
}
