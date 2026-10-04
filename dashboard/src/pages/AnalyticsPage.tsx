import { useEffect, useState } from "react";
import { BarChart, Bar, XAxis, YAxis, ResponsiveContainer, Tooltip, CartesianGrid } from "recharts";
import { RefreshCw } from "lucide-react";
import { api } from "../lib/api";
import type { Connection } from "../lib/api";
import type { AutoSendReport, CalibrationReport, CostReport, QualityStats, SupportAnalytics } from "../lib/types";
import { AnalyticsSkeleton } from "../components/Skeleton";
import { useToast } from "../components/Toast";

function StatCard({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="bg-surface dark:bg-surface-dark border border-line dark:border-line-dark rounded-xl2 shadow-panel p-5">
      <p className="font-mono text-[11px] tracking-[0.14em] uppercase text-ink-400 dark:text-ink-dark-400 mb-2">{label}</p>
      <p className="font-display text-3xl text-ink-900 dark:text-ink-dark-900">{value}</p>
      {sub && <p className="text-xs text-ink-400 dark:text-ink-dark-400 mt-1">{sub}</p>}
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="mb-8">
      <h2 className="font-display text-lg text-ink-900 dark:text-ink-dark-900 mb-3">{title}</h2>
      <div className="bg-surface dark:bg-surface-dark border border-line dark:border-line-dark rounded-xl2 shadow-panel p-5">{children}</div>
    </section>
  );
}

export function AnalyticsPage({ connection }: { connection: Connection }) {
  const [overview, setOverview] = useState<SupportAnalytics | null>(null);
  const [quality, setQuality] = useState<QualityStats | null>(null);
  const [calibration, setCalibration] = useState<CalibrationReport | null>(null);
  const [costs, setCosts] = useState<CostReport | null>(null);
  const [autoSend, setAutoSend] = useState<AutoSendReport | null>(null);
  const [thresholdState, setThresholdState] = useState<string>("");
  const [loading, setLoading] = useState(true);
  const { toast } = useToast();

  async function loadData() {
    setLoading(true);
    try {
      const [o, q, c, co, as] = await Promise.all([
        api.getAnalytics(connection),
        api.getQuality(connection),
        api.getCalibration(connection),
        api.getCosts(connection),
        api.getAutoSendReport(connection),
      ]);
      setOverview(o);
      setQuality(q);
      setCalibration(c);
      setCosts(co);
      setAutoSend(as);
    } catch {
      toast("error", "Failed to load analytics data");
    } finally {
      setLoading(false);
    }
  }

  async function applyThreshold(category: string, value: number | null) {
    setThresholdState(category);
    try {
      await api.updateThreshold(connection, category, value);
      setAutoSend(await api.getAutoSendReport(connection));
      toast("success", value == null ? `Reset ${category} threshold` : `Threshold for ${category} updated — applies to the next ticket`);
    } catch {
      toast("error", "Failed to update threshold");
    } finally {
      setThresholdState("");
    }
  }

  useEffect(() => {
    loadData();
    // eslint-disable-next-line
  }, []);

  if (loading) return <AnalyticsSkeleton />;

  const qualityChartData = quality
    ? Object.entries(quality.by_category).map(([category, s]) => ({
        category,
        edit_rate: Math.round(s.edit_rate * 100),
      }))
    : [];

  const calibrationChartData = calibration
    ? Object.entries(calibration.buckets)
        .filter(([, b]) => b.count > 0)
        .map(([bucket, b]) => ({
          bucket,
          edit_rate: b.edit_rate != null ? Math.round(b.edit_rate * 100) : 0,
          count: b.count,
        }))
    : [];

  const costChartData = costs?.by_day.slice().reverse() ?? [];

  return (
    <div className="max-w-5xl">
      <header className="flex items-end justify-between mb-6">
        <div>
          <p className="font-mono text-[11px] tracking-[0.18em] uppercase text-ink-400 dark:text-ink-dark-400 mb-1">Instrumentation</p>
          <h1 className="font-display text-3xl text-ink-900 dark:text-ink-dark-900">Analytics</h1>
        </div>
        <button
          onClick={loadData}
          className="flex items-center gap-1.5 text-xs text-ink-600 dark:text-ink-dark-600 hover:text-ink-900 dark:hover:text-ink-dark-900 px-3 py-2 rounded-lg hover:bg-ink-900/5 dark:hover:bg-white/5 transition-colors"
        >
          <RefreshCw className="w-3.5 h-3.5" /> Load data
        </button>
      </header>

      <div className="grid grid-cols-4 gap-4 mb-8">
        <StatCard label="Total tickets" value={overview ? String(overview.total_tickets) : "–"} />
        <StatCard label="Open" value={overview ? String(overview.open_tickets) : "–"} />
        <StatCard
          label="Auto-resolved"
          value={
            overview?.first_contact_resolution_rate != null
              ? `${Math.round(overview.first_contact_resolution_rate * 100)}%`
              : "–"
          }
        />
        <StatCard label="Spend today" value={costs ? `$${costs.today_usd.toFixed(4)}` : "–"} />
      </div>

      <Section title="Confidence calibration">
        <p className="text-xs text-ink-600 dark:text-ink-dark-600 mb-4 leading-relaxed">{calibration?.interpretation}</p>
        {calibrationChartData.length === 0 ? (
          <p className="text-sm text-ink-400 dark:text-ink-dark-400">
            No edit data yet. This chart fills in after you approve or edit a few drafts on the Tickets page —
            it compares the agent's confidence with how often drafts actually needed changes.
          </p>
        ) : (
          <ResponsiveContainer width="100%" height={220}>
            <BarChart data={calibrationChartData}>
              <CartesianGrid strokeDasharray="3 3" stroke="#DBE1E8" vertical={false} />
              <XAxis
                dataKey="bucket"
                tick={{ fontSize: 11, fontFamily: "IBM Plex Mono", fill: "#7C8CA0" }}
                axisLine={{ stroke: "#DBE1E8" }}
                tickLine={false}
              />
              <YAxis
                tick={{ fontSize: 11, fontFamily: "IBM Plex Mono", fill: "#7C8CA0" }}
                axisLine={false}
                tickLine={false}
                unit="%"
              />
              <Tooltip
                contentStyle={{ borderRadius: 10, border: "1px solid #DBE1E8", fontSize: 12, fontFamily: "IBM Plex Sans" }}
                formatter={(v: number) => [`${v}%`, "Edit rate"]}
              />
              <Bar dataKey="edit_rate" radius={[6, 6, 0, 0]} fill="#C08A2E" />
            </BarChart>
          </ResponsiveContainer>
        )}
        {calibration?.sample_size_warning && (
          <p className="text-[11px] text-gold-700 dark:text-gold mt-2">{calibration.sample_size_warning}</p>
        )}
      </Section>

      <Section title="Auto-send thresholds">
        {!autoSend ? (
          <p className="text-sm text-ink-400 dark:text-ink-dark-400">No threshold data yet.</p>
        ) : (
          <>
            <p className="text-xs text-ink-600 dark:text-ink-dark-600 mb-4 leading-relaxed">
              {autoSend.auto_send_enabled
                ? "A reply only auto-sends when its confidence clears its category's floor. Suggestions appear after 50+ reviewed drafts per category and apply to the next ticket immediately — no restart. The daily cost cap ($"
                : "Auto-send is currently off, so every reply waits for your approval regardless of thresholds. The daily cost cap ($"}
              {autoSend.daily_cost_cap_usd.toFixed(2)}) stays active either way.
            </p>
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="text-left font-mono text-[10px] uppercase tracking-[0.1em] text-ink-400 dark:text-ink-dark-400 border-b border-line dark:border-line-dark">
                    <th className="pb-2 pr-3">Category</th>
                    <th className="pb-2 pr-3">Current</th>
                    <th className="pb-2 pr-3">Suggested</th>
                    <th className="pb-2 pr-3">Samples</th>
                    <th className="pb-2 pr-3">Edit rate</th>
                    <th className="pb-2 pr-3">Why</th>
                    <th className="pb-2" />
                  </tr>
                </thead>
                <tbody>
                  {Object.entries(autoSend.categories).map(([category, c]) => {
                    const differs = c.suggested_threshold !== c.current_threshold;
                    const hasSamples = c.reviewed_samples >= autoSend.min_samples_for_recommendation;
                    return (
                      <tr key={category} className="border-b border-line/50 dark:border-line-dark/50 text-ink-700 dark:text-ink-dark-700">
                        <td className="py-2.5 pr-3 font-medium">{category.replace("_", " ")}</td>
                        <td className="py-2.5 pr-3 font-mono">{c.current_threshold.toFixed(2)}</td>
                        <td className={`py-2.5 pr-3 font-mono ${differs && hasSamples ? "text-teal-700 dark:text-teal font-semibold" : "text-ink-400 dark:text-ink-dark-400"}`}>
                          {c.suggested_threshold.toFixed(2)}
                        </td>
                        <td className="py-2.5 pr-3 font-mono">{c.reviewed_samples}</td>
                        <td className="py-2.5 pr-3 font-mono">
                          {c.edit_rate != null ? `${Math.round(c.edit_rate * 100)}%` : "–"}
                        </td>
                        <td className="py-2.5 pr-3 text-ink-500 dark:text-ink-dark-500 max-w-[22rem]">{c.recommendation}</td>
                        <td className="py-2.5 text-right whitespace-nowrap">
                          {differs && hasSamples && (
                            <button
                              onClick={() => applyThreshold(category, c.suggested_threshold)}
                              disabled={thresholdState === category}
                              className="text-[11px] px-2.5 py-1 rounded-lg bg-teal text-white font-medium hover:bg-teal-700 disabled:opacity-40 mr-1.5"
                            >
                              {thresholdState === category ? "…" : "Apply"}
                            </button>
                          )}
                          <button
                            onClick={() => applyThreshold(category, null)}
                            disabled={thresholdState === category}
                            className="text-[11px] px-2.5 py-1 rounded-lg border border-line dark:border-line-dark text-ink-500 dark:text-ink-dark-500 hover:bg-ink-900/5 dark:hover:bg-white/5 disabled:opacity-40"
                          >
                            Reset
                          </button>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            <p className="text-[11px] text-ink-400 dark:text-ink-dark-400 mt-3">
              Never auto-sent, regardless of threshold: {autoSend.blocked_categories.join(", ")} ·
              absolute floor {autoSend.absolute_min_threshold.toFixed(2)}
            </p>
          </>
        )}
      </Section>

      <Section title="Edit rate by category">
        {qualityChartData.length === 0 ? (
          <p className="text-sm text-ink-400 dark:text-ink-dark-400">
            No edit-rate data yet. When you approve or edit a drafted reply, we record whether it needed changes —
            this chart starts filling in from your first reviewed ticket.
          </p>
        ) : (
          <ResponsiveContainer width="100%" height={220}>
            <BarChart data={qualityChartData} layout="vertical">
              <CartesianGrid strokeDasharray="3 3" stroke="#DBE1E8" horizontal={false} />
              <XAxis
                type="number"
                tick={{ fontSize: 11, fontFamily: "IBM Plex Mono", fill: "#7C8CA0" }}
                axisLine={false}
                tickLine={false}
                unit="%"
              />
              <YAxis
                type="category"
                dataKey="category"
                width={100}
                tick={{ fontSize: 11, fontFamily: "IBM Plex Sans", fill: "#46586B" }}
                axisLine={false}
                tickLine={false}
              />
              <Tooltip
                contentStyle={{ borderRadius: 10, border: "1px solid #DBE1E8", fontSize: 12 }}
                formatter={(v: number) => [`${v}%`, "Edit rate"]}
              />
              <Bar dataKey="edit_rate" radius={[0, 6, 6, 0]} fill="#2E8C82" />
            </BarChart>
          </ResponsiveContainer>
        )}
      </Section>

      <Section title="LLM spend, last 14 days">
        {costChartData.length === 0 ? (
          <p className="text-sm text-ink-400 dark:text-ink-dark-400">No spend recorded yet.</p>
        ) : (
          <ResponsiveContainer width="100%" height={200}>
            <BarChart data={costChartData}>
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
                formatter={(v: number) => [`$${v.toFixed(4)}`, "Spend"]}
              />
              <Bar dataKey="cost_usd" radius={[6, 6, 0, 0]} fill="#6B5CA5" />
            </BarChart>
          </ResponsiveContainer>
        )}
      </Section>
    </div>
  );
}
