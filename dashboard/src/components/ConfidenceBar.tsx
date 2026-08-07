/**
 * The one visual idea every screen in this console shares: confidence rendered
 * as a physical instrument reading, not a percentage buried in a table cell.
 * Low readings sit in rose, mid in gold, high in teal — the same three signal
 * colors used for everything else in the app, so a glance at this bar tells
 * you as much as reading the number.
 */
export function ConfidenceBar({
  value,
  size = "md",
  showLabel = true,
}: {
  value: number;
  size?: "sm" | "md" | "lg";
  showLabel?: boolean;
}) {
  const pct = Math.round(value * 100);
  const color = value >= 0.85 ? "bg-teal" : value >= 0.6 ? "bg-gold" : "bg-rose";
  const trackHeight = size === "lg" ? "h-2.5" : size === "sm" ? "h-1.5" : "h-2";

  return (
    <div className="flex items-center gap-2.5 min-w-0">
      <div className={`relative flex-1 min-w-[64px] rounded-full bg-ink-900/[0.06] dark:bg-white/[0.06] ${trackHeight}`}>
        <div
          className={`absolute inset-y-0 left-0 rounded-full ${color} transition-[width] duration-500 ease-out`}
          style={{ width: `${pct}%` }}
        />
        {/* calibration tick at the auto-send threshold, a quiet reference mark */}
        <div className="absolute inset-y-0 left-[85%] w-px bg-ink-900/20 dark:bg-white/20" />
      </div>
      {showLabel && (
        <span className="font-mono text-xs tabular-nums text-ink-600 dark:text-ink-dark-600 shrink-0 w-9 text-right">{pct}%</span>
      )}
    </div>
  );
}
