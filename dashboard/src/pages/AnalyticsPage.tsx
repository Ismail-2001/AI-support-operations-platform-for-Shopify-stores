import { useEffect, useState } from "react";
import { BarChart, Bar, XAxis, YAxis, ResponsiveContainer, Tooltip, CartesianGrid } from "recharts";
import { RefreshCw } from "lucide-react";
import { api } from "../lib/api";
import type { Connection } from "../lib/api";
import type { CalibrationReport, CostReport, QualityStats, SupportAnalytics } from "../lib/types";
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
  const [loading, setLoading] = useState(true);
  const { toast } = useToast();

  async function loadData() {
    setLoading(true);
    try {
      const [o, q, c, co] = await Promise.all([
        api.getAnalytics(connection),
        api.getQuality(connection),
        api.getCalibration(connection),
        api.getCosts(connection),
      ]);
      setOverview(o);
      setQuality(q);
      setCalibration(c);
      setCosts(co);
    } catch {
      toast("error", "Failed to load analytics data");
    } finally {
      setLoading(false);
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
